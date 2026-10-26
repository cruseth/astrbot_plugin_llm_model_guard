"""调用统计聚合：每个 provider 的次数、成功率、延迟分位与最近错误。

口径（规划文档 §13）：

* 一次 *attempt* 计一次 ``calls``，包含包装层内部重试与候选切换；
* 延迟样本按 ``stats_window_seconds`` 窗口内保留，超过 512 个按 2 倍抽稀；
* 只记录白名单字段，绝不记录 prompt / contexts / 用户消息原文。
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any, Deque, Iterable, Mapping

from .models import AttemptRecord

#: 延迟样本上限；超过后按 2 倍抽稀（规划文档 §13.3）。
MAX_LATENCY_SAMPLES = 512

#: ``recent_events`` 默认上限。
DEFAULT_RECENT_EVENTS = 200


class _ProviderStat:
    """单个 provider 的累计统计。"""

    __slots__ = (
        "calls",
        "ok",
        "errors",
        "consecutive_errors",
        "error_classes",
        "last_error",
        "last_ok_at",
        "last_error_at",
        "updated_at",
        "latency_last",
        "samples",
    )

    def __init__(self) -> None:
        self.calls = 0
        self.ok = 0
        self.errors = 0
        self.consecutive_errors = 0
        self.error_classes: dict[str, int] = {}
        self.last_error: dict[str, Any] | None = None
        self.last_ok_at: float | None = None
        self.last_error_at: float | None = None
        self.updated_at = 0.0
        self.latency_last: int | None = None
        self.samples: Deque[tuple[float, int]] = deque(maxlen=MAX_LATENCY_SAMPLES)

    def to_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "ok": self.ok,
            "errors": self.errors,
            "consecutive_errors": self.consecutive_errors,
            "success_rate": round(self.ok / self.calls, 4) if self.calls else None,
            "error_classes": dict(self.error_classes),
            "last_error": dict(self.last_error) if self.last_error else None,
            "last_ok_at": self.last_ok_at,
            "last_error_at": self.last_error_at,
            "updated_at": self.updated_at,
            "latency_last_ms": self.latency_last,
            "sample_count": len(self.samples),
        }

    def load(self, data: Mapping[str, Any]) -> None:
        self.calls = int(data.get("calls") or 0)
        self.ok = int(data.get("ok") or 0)
        self.errors = int(data.get("errors") or 0)
        self.consecutive_errors = int(data.get("consecutive_errors") or 0)
        error_classes = data.get("error_classes")
        if isinstance(error_classes, Mapping):
            self.error_classes = {str(k): int(v or 0) for k, v in error_classes.items()}
        last_error = data.get("last_error")
        if isinstance(last_error, Mapping):
            self.last_error = dict(last_error)
        self.last_ok_at = _opt_float(data.get("last_ok_at"))
        self.last_error_at = _opt_float(data.get("last_error_at"))
        self.updated_at = float(data.get("updated_at") or 0.0)
        latency = data.get("latency_ms")
        if isinstance(latency, Mapping):
            self.latency_last = _opt_int(latency.get("last"))
        last_ms = _opt_int(data.get("latency_last_ms"))
        if last_ms is not None:
            self.latency_last = last_ms
        self.samples.clear()


def _opt_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _opt_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _percentile(sorted_values: list[int], ratio: float) -> int:
    if not sorted_values:
        return 0
    if len(sorted_values) == 1:
        return sorted_values[0]
    index = int(round((len(sorted_values) - 1) * ratio))
    return sorted_values[max(0, min(index, len(sorted_values) - 1))]


class Metrics:
    """统计聚合器：单线程事件循环内使用，无需加锁。"""

    def __init__(
        self,
        *,
        window_s: int = 900,
        recent_cap: int = DEFAULT_RECENT_EVENTS,
        clock: Any = None,
    ) -> None:
        self.window_s = max(0, int(window_s))
        self.recent_cap = max(0, int(recent_cap))
        self._clock = clock or time.time
        self._providers: dict[str, _ProviderStat] = {}
        self._events: Deque[dict[str, Any]] = deque(maxlen=max(1, self.recent_cap))
        self._probes: dict[str, dict[str, Any]] = {}
        self.counters: dict[str, int] = {
            "framework_fallback_hops": 0,
            "guard_internal_errors": 0,
            "all_candidates_marked": 0,
            "corrupt_state": 0,
        }

    # ---- 写入 ----

    def record_attempt(self, rec: AttemptRecord) -> None:
        """记录一次真实 provider 调用。"""
        stat = self._providers.setdefault(rec.used, _ProviderStat())
        now = rec.ts or self._clock()
        stat.calls += 1
        stat.updated_at = now
        stat.latency_last = int(rec.total_ms or 0)
        stat.samples.append((now, int(rec.total_ms or 0)))
        if rec.ok:
            stat.ok += 1
            stat.consecutive_errors = 0
            stat.last_ok_at = now
        else:
            stat.errors += 1
            stat.consecutive_errors += 1
            stat.last_error_at = now
            category = str(rec.error_class or "unknown")
            stat.error_classes[category] = stat.error_classes.get(category, 0) + 1
            stat.last_error = {
                "at": now,
                "class": category,
                "type": rec.error_type,
                "message": rec.error_message,
                "status": rec.http_status,
            }
        # 注意：``framework_fallback_hops`` 的计数在包装层（被跳过的"框架二次进入"），
        # 不再按 attempt_index 推断，避免把同模型重试误算成框架降级（P3-1）。
        self._append_event(rec.to_dict())

    def record_probe(self, provider_id: str, *, ok: bool, latency_ms: int) -> None:
        self._probes[provider_id] = {
            "last_ok": bool(ok),
            "last_at": self._clock(),
            "latency_ms": int(latency_ms),
        }

    def note_counter(self, name: str, amount: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + amount

    def _append_event(self, payload: dict[str, Any]) -> None:
        if self.recent_cap <= 0:
            return
        self._events.append(payload)

    # ---- 读取 ----

    def per_provider(self, *, window_s: int | None = None) -> dict[str, dict[str, Any]]:
        """每个 provider 的统计，含窗口内 p50/p95 延迟。"""
        window = self.window_s if window_s is None else int(window_s)
        now = self._clock()
        result: dict[str, dict[str, Any]] = {}
        for provider_id, stat in self._providers.items():
            data = stat.to_dict()
            data["latency_ms"] = self._latency_summary(stat, window=window, now=now)
            probe = self._probes.get(provider_id)
            if probe is not None:
                data["probe"] = dict(probe)
            result[provider_id] = data
        return result

    def _latency_summary(
        self, stat: _ProviderStat, *, window: int, now: float
    ) -> dict[str, Any]:
        if window > 0:
            values = [ms for ts, ms in stat.samples if now - ts <= window]
        else:
            values = [ms for _, ms in stat.samples]
        if not values:
            values = [stat.latency_last] if stat.latency_last is not None else []
        ordered = sorted(int(v) for v in values)
        avg = int(round(sum(ordered) / len(ordered))) if ordered else 0
        return {
            "last": stat.latency_last or 0,
            "avg": avg,
            "p50": _percentile(ordered, 0.5),
            "p95": _percentile(ordered, 0.95),
            "n": len(ordered),
        }

    def totals(self) -> dict[str, Any]:
        calls = sum(stat.calls for stat in self._providers.values())
        ok = sum(stat.ok for stat in self._providers.values())
        errors = sum(stat.errors for stat in self._providers.values())
        return {
            "calls": calls,
            "ok": ok,
            "errors": errors,
            "success_rate": round(ok / calls, 4) if calls else None,
            "providers": len(self._providers),
        }

    def overview(self, *, window_s: int | None = None) -> dict[str, Any]:
        """可直接 JSON 序列化的汇总（供命令与 Web API 复用）。"""
        return {
            "window_s": self.window_s if window_s is None else int(window_s),
            "totals": self.totals(),
            "counters": dict(self.counters),
            "providers": self.per_provider(window_s=window_s),
        }

    def recent_events(
        self,
        *,
        limit: int = 50,
        provider_id: str | None = None,
        profile_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """最近调用记录，按时间倒序。"""
        count = max(0, min(int(limit), 500))
        items: list[dict[str, Any]] = []
        for event in reversed(self._events):
            if provider_id and event.get("used") != provider_id:
                continue
            if profile_id and event.get("profile_id") != profile_id:
                continue
            items.append(dict(event))
            if len(items) >= count:
                break
        return items

    def events_total(self) -> int:
        return len(self._events)

    # ---- 持久化 ----

    def export_state(self) -> dict[str, Any]:
        """导出到 ``state.json`` 的结构（规划文档 §8.2）。"""
        providers = {pid: stat.to_dict() for pid, stat in self._providers.items()}
        for pid, data in providers.items():
            data["latency_ms"] = self._latency_summary(
                self._providers[pid], window=0, now=self._clock()
            )
        return {
            "providers": providers,
            "recent_events": list(self._events),
            "probes": {pid: dict(value) for pid, value in self._probes.items()},
            "runtime": {
                "framework_fallback_hops": self.counters.get("framework_fallback_hops", 0),
                "guard_internal_errors": self.counters.get("guard_internal_errors", 0),
                "all_candidates_marked": self.counters.get("all_candidates_marked", 0),
                "corrupt_state": self.counters.get("corrupt_state", 0),
            },
        }

    def import_state(self, data: Mapping[str, Any] | None) -> None:
        if not data:
            return
        providers = data.get("providers")
        if isinstance(providers, Mapping):
            for pid, raw in providers.items():
                if not isinstance(raw, Mapping):
                    continue
                stat = _ProviderStat()
                stat.load(raw)
                self._providers[str(pid)] = stat
        events = data.get("recent_events")
        if isinstance(events, Iterable):
            for raw in events:
                if isinstance(raw, Mapping) and self.recent_cap > 0:
                    self._events.append(_sanitize_event(raw))
        probes = data.get("probes")
        if isinstance(probes, Mapping):
            for pid, raw in probes.items():
                if isinstance(raw, Mapping):
                    self._probes[str(pid)] = {
                        "last_ok": bool(raw.get("last_ok")),
                        "last_at": _opt_float(raw.get("last_at")),
                        "latency_ms": _opt_int(raw.get("latency_ms")) or 0,
                    }
        runtime = data.get("runtime")
        if isinstance(runtime, Mapping):
            for key in self.counters:
                value = _opt_int(runtime.get(key))
                if value is not None:
                    self.counters[key] = value

    # ---- 清理 ----

    def prune(
        self,
        *,
        retention_days: int = 0,
        keep_providers: Iterable[str] | None = None,
        now: float | None = None,
    ) -> int:
        """按保留期清理 provider 统计；返回被清理的 provider 数量。"""
        current = self._clock() if now is None else now
        removed = 0
        keep = set(keep_providers) if keep_providers is not None else None
        if retention_days > 0:
            deadline = current - retention_days * 86400
            for pid in list(self._providers):
                stat = self._providers[pid]
                if keep is not None and pid in keep:
                    continue
                if stat.updated_at and stat.updated_at < deadline:
                    del self._providers[pid]
                    self._probes.pop(pid, None)
                    removed += 1
        if self.recent_cap > 0:
            while len(self._events) > self.recent_cap:
                self._events.popleft()
        return removed

    def known_providers(self) -> set[str]:
        return set(self._providers)


_SENSITIVE_KEYS = frozenset({"prompt", "contexts", "messages", "system_prompt", "api_key", "key"})


def _sanitize_event(raw: Mapping[str, Any]) -> dict[str, Any]:
    """白名单过滤：落盘的最近事件里不允许出现提示词类字段。"""
    return {str(k): v for k, v in raw.items() if str(k) not in _SENSITIVE_KEYS}

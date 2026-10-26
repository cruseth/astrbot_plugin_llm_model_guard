"""标记状态机与测活 supervisor。

职责（规划文档 §11 / §12 / D7）：

* ``mark`` / ``unmark`` 维护临时标记，含 TTL、双击确认（``CONFIRMING``）与抖动冷却；
* 单个 supervisor 协程按固定 tick 扫描「该测活了」的模型，用信号量限制并发；
* 测活失败**不新增标记**，只重置双击确认进度与退避下一次测活；
* ``shutdown()`` 严格按 §12.3 的顺序清理，所有 ``except`` 都先重抛
  ``asyncio.CancelledError``。

本模块不导入 AstrBot，网络调用只是「调用传入的 provider」，不做任何 SDK 依赖。
"""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import time
from typing import Any, Callable, Mapping

from .metrics import Metrics
from .models import (
    LOG_PREFIX,
    MARK_SCOPE_PROFILE,
    PROBE_PROMPT,
    CallKind,
    ErrorClass,
    GuardSettings,
    MarkBlock,
    MarkSource,
    MarkState,
    MarkView,
    ProbeResult,
    log_allowed,
)
from .store import StateStore

#: 测活旁路标记：为真时包装层只记统计、不重试/不降级/不标记（规划文档 §6 D7）。
PROBE_CONTEXT: contextvars.ContextVar[int] = contextvars.ContextVar(
    "llm_model_guard_probe_depth", default=0
)

#: 解除后再次标记的「抖动冷却」窗口（秒，规划文档 §11.3-7）。
FLAPPING_WINDOW_S = 60.0

#: TTL 到期的判定与清理在 supervisor 里做，扫描时不早于该精度。
MIN_TICK_S = 1.0


class _ProbeCaller:
    """按优先级尝试三种测活入口，并记录实际走的路径。"""

    @staticmethod
    def _supports_timeout(func: Callable[..., Any]) -> bool:
        try:
            signature = inspect.signature(func)
        except (TypeError, ValueError):  # pragma: no cover - 内建函数
            return False
        for parameter in signature.parameters.values():
            if parameter.name == "timeout":
                return True
            if parameter.kind is inspect.Parameter.VAR_KEYWORD:
                return True
        return False

    @staticmethod
    def _framework_default_test(provider: Any) -> Any:
        """框架基类自带的 ``test`` 实现（用 ``type(p).test`` 比较用）。"""
        for klass in type(provider).__mro__:
            if klass.__name__ not in ("Provider", "AbstractProvider"):
                continue
            candidate = klass.__dict__.get("test")
            if callable(candidate):
                return candidate
        return None

    @classmethod
    def _uses_custom_test(cls, provider: Any) -> bool:
        """provider 是否自定义了 ``test``（含从自定义父类继承来的，P3-15）。

        框架默认的 ``Provider.test`` 本身就是 ``wait_for(text_chat)``，走它反而会
        进实例包装层，所以这种情况一律改用类级 ``text_chat`` 直连。
        """
        test_attr = getattr(type(provider), "test", None)
        if not callable(test_attr):
            return False
        default_test = cls._framework_default_test(provider)
        return default_test is None or test_attr is not default_test

    @classmethod
    async def call(cls, provider: Any, timeout: float) -> str:
        """探测一次；返回实际使用的路径名，异常原样上抛。"""
        if cls._uses_custom_test(provider):
            test_attr = getattr(type(provider), "test")
            if cls._supports_timeout(test_attr):
                await asyncio.wait_for(
                    provider.test(timeout=timeout), timeout=timeout + 1.0
                )
                return "test_timeout"
            await asyncio.wait_for(provider.test(), timeout=timeout + 1.0)
            return "test"

        impl = getattr(type(provider), "text_chat", None)
        if callable(impl):
            # 直接取类实现：绕过实例上的包装层，杜绝「测活 → 包装层 → 标记」递归。
            await asyncio.wait_for(impl(provider, prompt=PROBE_PROMPT), timeout=timeout)
            return "text_chat"

        test = getattr(provider, "test", None)
        if callable(test):
            await asyncio.wait_for(test(), timeout=timeout + 1.0)
            return "test_fallback"

        chat = getattr(provider, "text_chat", None)
        if callable(chat):
            await asyncio.wait_for(chat(prompt=PROBE_PROMPT), timeout=timeout)
            return "text_chat_fallback"

        raise AttributeError("provider 没有可用的测活入口（test/text_chat 均缺失）")


class Guardian:
    """标记状态机 + 测活调度器。"""

    def __init__(
        self,
        *,
        config_reader: Callable[[], Mapping[str, Any]],
        store: StateStore,
        metrics: Metrics,
        provider_getter: Callable[[str], Any | None] | None = None,
        provider_ids_getter: Callable[[], Any] | None = None,
        classify: Callable[..., Any] | None = None,
        clock: Callable[[], float] | None = None,
        logger: Any = None,
        event_recorder: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._config_reader = config_reader
        self._store = store
        self._metrics = metrics
        self._provider_getter = provider_getter
        self._provider_ids_getter = provider_ids_getter
        self._classify = classify
        self._clock = clock or time.time
        self._logger = logger
        self._event_recorder = event_recorder

        self._marks: dict[str, MarkBlock] = {}
        self._marks_by_profile: dict[str, MarkBlock] = {}
        self._snapshot: dict[str, MarkView] = {}
        self._snapshot_by_profile: dict[str, MarkView] = {}
        self._streaks: dict[str, int] = {}
        self._last_unmark: dict[str, tuple[float, float, str]] = {}
        self._flapping_count = 0

        self._lock = asyncio.Lock()
        self._stopping = False
        self._supervisor_task: asyncio.Task[Any] | None = None
        self._probe_tasks: set[asyncio.Task[Any]] = set()
        self._probe_inflight: set[str] = set()
        self._probe_semaphore = asyncio.Semaphore(2)
        self._active_calls: dict[str, int] = {}
        self._supervisor_started_at: float | None = None
        self._settings_cache: tuple[str, Any] | None = None

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------

    def settings(self) -> GuardSettings:
        """读配置快照；同一配置指纹最多复用（请求内多次读取只算一次）。"""
        try:
            raw = self._config_reader() or {}
        except Exception:  # pragma: no cover - 配置对象异常
            raw = {}
        snapshot = GuardSettings.from_mapping(raw)
        fingerprint = snapshot.fingerprint()
        cached = self._settings_cache
        if cached is not None and cached[0] == fingerprint:
            return cached[1]
        self._settings_cache = (fingerprint, snapshot)
        return snapshot

    # ------------------------------------------------------------------
    # 标记视图（热路径，无 await）
    # ------------------------------------------------------------------

    def marks(self) -> Mapping[str, MarkView]:
        """全局作用域的标记视图（兼容旧调用点）。"""
        return self._snapshot

    def all_marks(self) -> dict[str, MarkView]:
        """全局 + 各配置文件作用域的**全部**标记，供页面与命令展示（P1-1）。

        键是展示用的唯一键：全局标记用 ``provider_id``，profile 标记用
        ``"<profile>::<provider_id>"``；同一 provider 全局标记优先。
        """
        merged: dict[str, MarkView] = {}
        globally_marked: set[str] = set()
        for provider_id, view in self._snapshot.items():
            merged[provider_id] = view
            globally_marked.add(provider_id)
        for key, view in self._snapshot_by_profile.items():
            if view.provider_id in globally_marked:
                continue
            merged.setdefault(key, view)
        return merged

    def mark_index(self) -> dict[str, MarkView]:
        """``provider_id → MarkView``（全局优先），用于按模型展示标记状态。"""
        index: dict[str, MarkView] = {}
        for view in self.all_marks().values():
            index.setdefault(view.provider_id, view)
        return index

    def marks_for(self, profile_id: str | None = None) -> Mapping[str, MarkView]:
        """给出该配置文件应当绕过的标记视图（P1-1）。

        规则：全局标记对所有配置文件生效；``mark_scope="profile"`` 下的作用域标记
        只对它自己的配置文件生效。两套存储始终参与计算，因此切换 ``mark_scope``
        不会让既有标记失去旁路效果。
        """
        merged: dict[str, MarkView] = dict(self._snapshot)
        if not self._snapshot_by_profile:
            return merged
        profile = profile_id or ""
        for key, view in self._snapshot_by_profile.items():
            owner, _, provider_id = key.partition("::")
            if owner in (profile, "*", ""):
                merged.setdefault(provider_id or owner, view)
        return merged

    def is_marked(self, provider_id: str, *, profile_id: str | None = None) -> bool:
        """是否被标记；不给 ``profile_id`` 时只要任意作用域有标记就算被标记。"""
        if profile_id is not None:
            return provider_id in self.marks_for(profile_id)
        return bool(self._blocks_for(provider_id))

    def has_marked(self) -> bool:
        return bool(self._snapshot or self._snapshot_by_profile)

    def marked_ids(self) -> tuple[str, ...]:
        ids = set(self._snapshot) | {
            key.partition("::")[2] or key for key in self._snapshot_by_profile
        }
        return tuple(sorted(ids))

    def active_calls(self) -> Mapping[str, int]:
        return dict(self._active_calls)

    def inc_active(self, provider_id: str) -> None:
        self._active_calls[provider_id] = self._active_calls.get(provider_id, 0) + 1

    def dec_active(self, provider_id: str) -> None:
        current = self._active_calls.get(provider_id, 0)
        if current <= 1:
            self._active_calls.pop(provider_id, None)
        else:
            self._active_calls[provider_id] = current - 1

    def _scope_key(self, provider_id: str, profile_hint: str) -> tuple[str, str]:
        settings = self.settings()
        if settings.mark_scope == MARK_SCOPE_PROFILE:
            owner = profile_hint or "*"
            return f"{owner}::{provider_id}", "profile"
        return provider_id, "global"

    def _blocks_for(self, provider_id: str) -> list[tuple[str, MarkBlock]]:
        """收集该 provider 在所有作用域里的标记块（P1-1 的核心：不按当前 scope 猜 key）。"""
        blocks: list[tuple[str, MarkBlock]] = []
        global_block = self._marks.get(provider_id)
        if global_block is not None:
            blocks.append((provider_id, global_block))
        for key, block in self._marks_by_profile.items():
            if key.partition("::")[2] == provider_id:
                blocks.append((key, block))
        return blocks

    def _lookup_block(self, provider_id: str, profile_hint: str = "") -> MarkBlock | None:
        """取该 provider 的标记块：当前 scope 优先，另一套存储兜底（P1-1）。"""
        key, scope = self._scope_key(provider_id, profile_hint)
        if scope == "profile":
            block = self._marks_by_profile.get(key)
            if block is not None:
                return block
            block = self._marks.get(provider_id)
            if block is not None:
                return block
            blocks = self._blocks_for(provider_id)
            return blocks[0][1] if blocks else None
        block = self._marks.get(provider_id)
        if block is not None:
            return block
        blocks = self._blocks_for(provider_id)
        return blocks[0][1] if blocks else None

    def _delete_block_key(self, key: str) -> MarkBlock | None:
        """按"存储位置"删除单个标记块（key 为全局 provider_id 或 ``profile::pid``）。"""
        if "::" in key:
            return self._marks_by_profile.pop(key, None)
        return self._marks.pop(key, None)

    # ------------------------------------------------------------------
    # 标记变更
    # ------------------------------------------------------------------

    async def mark(
        self,
        provider_id: str,
        *,
        reason: str,
        ttl: int,
        source: str = MarkSource.AUTO,
        detail: str = "",
        delay: int = 1,
        profile_hint: str = "",
    ) -> bool:
        """标记（或续期）一个 provider；返回是否**真的**写入了标记。

        ``delay > 1`` 时要求连续失败达到阈值才真正标记（空输出、流式中断用）。
        """
        if not provider_id:
            return False
        settings = self.settings()
        if not settings.enable or not settings.enable_marking:
            return False

        self._streaks[provider_id] = self._streaks.get(provider_id, 0) + 1
        threshold = max(1, int(delay))
        if self._streaks[provider_id] < threshold:
            self._debug(
                f"{provider_id} 连续失败 {self._streaks[provider_id]}/{threshold} 次，暂不标记"
            )
            return False

        now = self._clock()
        key, scope = self._scope_key(provider_id, profile_hint)
        async with self._lock:
            container = self._marks_by_profile if scope == "profile" else self._marks
            existing = container.get(key)
            if existing is None:
                # 另一套存储里已有该 provider 的标记时续期那一条，避免双份标记（P1-1）。
                blocks = self._blocks_for(provider_id)
                if blocks:
                    existing = blocks[0][1]
            expires_at = now + ttl if ttl > 0 else None

            flapping = self._apply_flapping_cooldown(provider_id, reason, now)

            if existing is None:
                block = MarkBlock(
                    provider_id=provider_id,
                    state=MarkState.MARKED,
                    reason=reason,
                    detail=detail,
                    marked_at=flapping or now,
                    expires_at=expires_at,
                    next_probe_at=now,
                    source=str(source),
                    profile_hint=profile_hint,
                )
                container[key] = block
                action = "已标记"
            else:
                existing.state = MarkState.MARKED
                existing.reason = reason
                existing.detail = detail or existing.detail
                existing.expires_at = expires_at if ttl > 0 else existing.expires_at
                existing.mark_count += 1
                existing.source = str(source)
                existing.first_success_at = None
                existing.next_probe_at = now
                if profile_hint:
                    existing.profile_hint = profile_hint
                block = existing
                action = "续期"

            self._rebuild_snapshot_locked()
            state = block.to_dict()

        await self._persist(force=True)
        self._info(
            f"{action} {provider_id}（原因={reason}，TTL={ttl or '永久'}，来源={source}）"
        )
        self._record_event(state, kind=str(source))
        return True

    def _apply_flapping_cooldown(
        self, provider_id: str, reason: str, now: float
    ) -> float | None:
        """解除后 60 秒内同类错误再次标记：沿用首次 ``marked_at`` 并计数。"""
        previous = self._last_unmark.get(provider_id)
        if previous is None:
            return None
        unmarked_at, original_marked_at, previous_reason = previous
        if now - unmarked_at > FLAPPING_WINDOW_S:
            self._last_unmark.pop(provider_id, None)
            return None
        if previous_reason and reason and previous_reason != reason:
            return None
        self._flapping_count += 1
        self._metrics.note_counter("flapping")
        self._warn(
            f"{provider_id} 解除 {int(now - unmarked_at)} 秒后因同类错误再次标记（疑似抖动）"
        )
        return original_marked_at or None

    async def unmark(self, provider_id: str, *, by: str = "manual") -> bool:
        """解除标记；返回是否真的删掉了一条标记。"""
        if not provider_id:
            return False
        now = self._clock()
        async with self._lock:
            removed: list[MarkBlock] = []
            block = self._marks.pop(provider_id, None)
            if block is not None:
                removed.append(block)
            for key in list(self._marks_by_profile):
                owner, _, pid = key.partition("::")
                if pid == provider_id:
                    removed.append(self._marks_by_profile.pop(key))
            self._streaks.pop(provider_id, None)
            self._rebuild_snapshot_locked()

        if not removed:
            return False

        first = removed[0]
        self._last_unmark[provider_id] = (now, first.marked_at, first.reason)
        await self._persist(force=True)
        self._info(f"解除 {provider_id} 的标记（操作者={by}）")
        self._record_event(first.to_dict(), kind=CallKind.MANUAL)
        return True

    def note_success(self, provider_id: str) -> None:
        """一次成功调用后清零连续失败计数。"""
        self._streaks.pop(provider_id, None)

    def clear_streaks(self) -> None:
        self._streaks.clear()

    def _rebuild_snapshot_locked(self) -> None:
        self._snapshot = {
            pid: block.view() for pid, block in self._marks.items()
        }
        self._snapshot_by_profile = {
            key: block.view() for key, block in self._marks_by_profile.items()
        }

    # ------------------------------------------------------------------
    # 测活
    # ------------------------------------------------------------------

    def _probe_timeout(self, provider_id: str) -> float:
        settings = self.settings()
        timeout = float(settings.probe_timeout_seconds)
        block = self._lookup_block(provider_id)
        if block is not None:
            for route in settings.routes:
                if provider_id in (route.primary, *route.fallbacks) and route.probe_timeout:
                    timeout = float(route.probe_timeout)
                    break
        return max(1.0, timeout)

    async def probe_now(
        self,
        provider_id: str,
        *,
        timeout: float | None = None,
        mode: str = "scheduler",
    ) -> ProbeResult:
        """立刻测活一次；异常不外抛，统一变成 :class:`ProbeResult`。"""
        settings = self.settings()
        effective_timeout = float(timeout) if timeout else self._probe_timeout(provider_id)
        provider = self._get_provider(provider_id)
        if provider is None:
            self._warn(f"测活 {provider_id} 跳过：该 provider 不存在")
            return ProbeResult(
                provider_id=provider_id,
                ok=False,
                error_class=str(ErrorClass.MODEL_NOT_FOUND),
                error_message="provider 不存在",
                mode="missing",
            )

        token = PROBE_CONTEXT.set(PROBE_CONTEXT.get() + 1)
        started = time.monotonic()
        try:
            used_mode = await _ProbeCaller.call(provider, effective_timeout)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            latency = int((time.monotonic() - started) * 1000)
            classification = self._classify_error(exc)
            detail = getattr(classification, "detail", "") or str(exc)
            state_after = await self._on_probe_failure(provider_id, detail=detail)
            self._metrics.record_probe(provider_id, ok=False, latency_ms=latency)
            self._warn(
                f"测活失败 {provider_id}（{type(exc).__name__}，{latency}ms，类别="
                f"{getattr(classification, 'error_class', 'unknown')}）"
            )
            return ProbeResult(
                provider_id=provider_id,
                ok=False,
                latency_ms=latency,
                error_class=str(getattr(classification, "error_class", "unknown")),
                error_message=detail[:200],
                mode=mode,
                timed_out=isinstance(exc, (asyncio.TimeoutError, TimeoutError)),
                state_after=state_after,
            )
        finally:
            PROBE_CONTEXT.reset(token)

        latency = int((time.monotonic() - started) * 1000)
        state_after = await self._on_probe_success(provider_id, latency=latency)
        self._metrics.record_probe(provider_id, ok=True, latency_ms=latency)
        self._info(f"测活成功 {provider_id}（{latency}ms，路径={used_mode}，状态={state_after}）")
        return ProbeResult(
            provider_id=provider_id,
            ok=True,
            latency_ms=latency,
            mode=used_mode,
            state_after=state_after,
        )

    def _get_provider(self, provider_id: str) -> Any | None:
        if self._provider_getter is None:
            return None
        try:
            return self._provider_getter(provider_id)
        except Exception:  # pragma: no cover - provider_manager 异常
            return None

    def _known_provider_ids(self) -> set[str]:
        """当前仍存在的 provider id 集合；用于统计清理时保住它们。"""
        if self._provider_ids_getter is None:
            return set()
        try:
            return {str(item) for item in self._provider_ids_getter() or ()}
        except Exception:  # pragma: no cover
            return set()

    def _classify_error(self, exc: BaseException) -> Any:
        if self._classify is None:
            return None
        try:
            return self._classify(exc)
        except Exception:  # pragma: no cover - 分类器异常不应影响测活
            return None

    async def _on_probe_success(self, provider_id: str, *, latency: int) -> str | None:
        """测活成功：按双击确认规则推进状态机（所有作用域里的标记一起推进，P1-1）。"""
        settings = self.settings()
        now = self._clock()
        removed: list[MarkBlock] = []
        state_after: str | None = None
        async with self._lock:
            blocks = self._blocks_for(provider_id)
            if not blocks:
                return None
            for key, block in blocks:
                block.probe_attempts += 1
                block.last_probe_at = now
                block.consecutive_probe_failures = 0
                block.last_probe_error = ""
                if settings.double_confirm and block.state is MarkState.MARKED:
                    block.state = MarkState.CONFIRMING
                    block.first_success_at = now
                    block.next_probe_at = now + max(1, settings.confirm_delay_seconds)
                    state_after = str(MarkState.CONFIRMING)
                else:
                    self._delete_block_key(key)
                    removed.append(block)
                    state_after = "healthy"
            self._rebuild_snapshot_locked()
        for block in removed:
            self._last_unmark[provider_id] = (now, block.marked_at, block.reason)
            self._info(f"测活确认通过，解除 {provider_id} 的标记（延迟 {latency}ms）")
            self._record_event(block.to_dict(), kind=CallKind.PROBE)
        if not removed:
            self._info(
                f"{provider_id} 首次测活成功，进入双击确认，{settings.confirm_delay_seconds} 秒后再测一次"
            )
        await self._persist(force=True)
        return state_after

    async def _on_probe_failure(self, provider_id: str, *, detail: str) -> str | None:
        """测活失败：回到 MARKED，重置确认进度并轻度退避。"""
        settings = self.settings()
        now = self._clock()
        async with self._lock:
            blocks = self._blocks_for(provider_id)
            if not blocks:
                return None
            for _key, block in blocks:
                block.state = MarkState.MARKED
                block.first_success_at = None
                block.consecutive_probe_failures += 1
                block.probe_attempts += 1
                block.last_probe_at = now
                block.last_probe_error = (detail or "")[:200]
                backoff = min(max(block.consecutive_probe_failures, 1), 4)
                interval = max(1.0, float(settings.probe_interval_seconds))
                block.next_probe_at = now + min(interval * backoff, interval * 8)
            self._rebuild_snapshot_locked()
            state = str(blocks[0][1].state)
        await self._persist(force=True)
        return state

    async def probe_all(self, *, timeout: float | None = None) -> list[ProbeResult]:
        """对当前所有被标记的模型依次测活（供页面/命令的“全部测活”）。"""
        provider_ids: list[str] = []
        for _key, block in self._all_blocks():
            if block.provider_id not in provider_ids:
                provider_ids.append(block.provider_id)
        results: list[ProbeResult] = []
        for provider_id in provider_ids:
            results.append(await self.probe_now(provider_id, timeout=timeout))
        return results

    # ------------------------------------------------------------------
    # supervisor
    # ------------------------------------------------------------------

    async def ensure_started(self) -> None:
        """幂等启动 supervisor（``__init__`` 时机拿不到 provider，故延迟启动）。

        即使 ``enable_probe=false``，只要存在带 ``expires_at`` 的标记，也启动一个
        只负责 TTL 扫描的 supervisor，保证"限时标记"一定会到期（P2-9）。
        """
        settings = self.settings()
        if not settings.enable:
            return
        if self._stopping:
            return
        if self._supervisor_task is not None and not self._supervisor_task.done():
            return
        if not settings.enable_probe and not self._has_expiring_marks():
            return
        if settings.enable_probe and settings.probe_on_startup:
            now = self._clock()
            async with self._lock:
                for block in list(self._marks.values()) + list(self._marks_by_profile.values()):
                    block.next_probe_at = block.next_probe_at or now
                self._rebuild_snapshot_locked()
        self._supervisor_task = asyncio.create_task(
            self._supervisor_loop(), name="llm_model_guard.supervisor"
        )
        self._supervisor_started_at = self._clock()
        self._info(
            "守护 supervisor 已启动（"
            + ("含测活" if settings.enable_probe else "仅 TTL 扫描")
            + "）"
        )

    def _has_expiring_marks(self) -> bool:
        return any(
            block.expires_at is not None for _key, block in self._all_blocks()
        )

    async def _supervisor_loop(self) -> None:
        while not self._stopping:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - 防止 supervisor 崩掉
                self._error(f"测活 supervisor 单轮异常：{exc}")
            interval = max(MIN_TICK_S, float(self.settings().probe_tick_seconds))
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise

    async def tick(self) -> list[str]:
        """单轮扫描：TTL 到期 + 到期测活 + 统计节流落盘；返回本轮派发的 provider id。

        TTL 扫描排在 ``enable_probe`` 判断之前：关闭测活不等于关闭标记过期（P2-9）。
        """
        settings = self.settings()
        if not settings.enable:
            return []
        await self.expire_ttl()
        if not settings.enable_probe:
            await self._housekeeping(settings)
            return []

        # 并发上限可以在配置里改；变化时按新值重建信号量。
        if getattr(self._probe_semaphore, "_value", None) != settings.probe_max_concurrency:
            self._probe_semaphore = asyncio.Semaphore(settings.probe_max_concurrency)

        now = self._clock()
        due: list[str] = []
        seen: set[str] = set()
        for _key, block in self._all_blocks():
            provider_id = block.provider_id
            if provider_id in seen:
                continue
            seen.add(provider_id)
            if provider_id in self._probe_inflight:
                continue
            if block.next_probe_at is not None and block.next_probe_at > now:
                continue
            if settings.probe_skip_when_busy and self._active_calls.get(provider_id):
                continue
            due.append(provider_id)

        dispatched: list[str] = []
        for provider_id in due:
            if self._stopping:
                break
            self._probe_inflight.add(provider_id)
            task = asyncio.create_task(
                self._guarded_probe(provider_id),
                name=f"llm_model_guard.probe.{provider_id}",
            )
            self._probe_tasks.add(task)
            task.add_done_callback(self._probe_tasks.discard)
            dispatched.append(provider_id)

        await self._housekeeping(settings)
        return dispatched

    async def _housekeeping(self, settings: GuardSettings) -> None:
        """统计节流落盘 + 按 ``stats_retention_days`` 清理过期统计（P2-11）。"""
        if self._store.schedule_save({}, interval=float(settings.stats_persist_interval_seconds)):
            await self._persist(force=False)
        self._metrics.prune(
            retention_days=int(settings.stats_retention_days),
            keep_providers=self._known_provider_ids(),
        )

    async def _guarded_probe(self, provider_id: str) -> None:
        try:
            async with self._probe_semaphore:
                await self.probe_now(provider_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - 防御性
            self._error(f"测活任务异常 {provider_id}: {exc}")
        finally:
            self._probe_inflight.discard(provider_id)

    def _all_blocks(self) -> list[tuple[str, MarkBlock]]:
        items: list[tuple[str, MarkBlock]] = list(self._marks.items())
        items.extend(self._marks_by_profile.items())
        return items

    async def expire_ttl(self) -> list[str]:
        """TTL 到期自动解除（由 supervisor 驱动，不依赖访问时的惰性判断）。"""
        now = self._clock()
        expired: list[MarkBlock] = []
        async with self._lock:
            for provider_id, block in list(self._marks.items()):
                if block.expires_at is not None and block.expires_at <= now:
                    expired.append(self._marks.pop(provider_id))
            for key, block in list(self._marks_by_profile.items()):
                if block.expires_at is not None and block.expires_at <= now:
                    expired.append(self._marks_by_profile.pop(key))
            if expired:
                self._rebuild_snapshot_locked()
        if not expired:
            return []
        for block in expired:
            self._info(f"{block.provider_id} 的标记 TTL 到期，自动解除")
            self._record_event(block.to_dict(), kind="ttl_expired")
        await self._persist(force=True)
        return [block.provider_id for block in expired]

    async def shutdown(self) -> None:
        """严格按规划文档 §12.3 的顺序清理。"""
        self._stopping = True
        supervisor = self._supervisor_task
        self._supervisor_task = None
        if supervisor is not None:
            supervisor.cancel()
            await asyncio.gather(supervisor, return_exceptions=True)

        tasks = [task for task in self._probe_tasks if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=5)
            for task in pending:  # pragma: no cover - 极端慢取消
                task.cancel()
            if pending:
                await asyncio.wait(pending, timeout=1)
        self._probe_tasks.clear()
        self._probe_inflight.clear()
        await self._persist(force=True)

    @property
    def stopping(self) -> bool:
        return self._stopping

    @property
    def supervisor_running(self) -> bool:
        return self._supervisor_task is not None and not self._supervisor_task.done()

    # ------------------------------------------------------------------
    # 状态导入导出
    # ------------------------------------------------------------------

    def load_state(self, state: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """从状态文件恢复标记与统计。"""
        payload = dict(state) if state is not None else self._store.load()
        self._marks.clear()
        self._marks_by_profile.clear()
        raw_marks = payload.get("marks")
        if isinstance(raw_marks, Mapping):
            for provider_id, raw in raw_marks.items():
                if isinstance(raw, Mapping):
                    self._marks[str(provider_id)] = MarkBlock.from_dict(str(provider_id), raw)
        raw_profile_marks = payload.get("marks_by_profile")
        if isinstance(raw_profile_marks, Mapping):
            for key, raw in raw_profile_marks.items():
                if isinstance(raw, Mapping):
                    provider_id = str(key).partition("::")[2] or str(key)
                    self._marks_by_profile[str(key)] = MarkBlock.from_dict(provider_id, raw)
        self._metrics.import_state(payload)
        self._rebuild_snapshot_locked()
        return payload

    def export_state(self, *, include_stats: bool = True) -> dict[str, Any]:
        """导出完整状态（标记 + 统计 + 运行时计数）。

        ``include_stats=False`` 时只导出标记与运行时计数：总开关关闭后
        "零介入"语义要求不落盘统计（P3-11）。
        """
        payload: dict[str, Any] = {
            "schema_version": self._store.SCHEMA_VERSION,
            "plugin_version": self._store.plugin_version,
            "saved_at": self._clock(),
            "marks": {pid: block.to_dict() for pid, block in self._marks.items()},
            "marks_by_profile": {
                key: block.to_dict() for key, block in self._marks_by_profile.items()
            },
        }
        if include_stats:
            payload.update(self._metrics.export_state())
        else:
            payload.update(
                {
                    "providers": {},
                    "recent_events": [],
                    "probes": {},
                    "runtime": {},
                }
            )
        runtime = dict(payload.get("runtime") or {})
        runtime.update(
            {
                "probe_supervisor_started_at": self._supervisor_started_at,
                "flapping": self._flapping_count,
                "marked": len(self._marks) + len(self._marks_by_profile),
                "supervisor_running": self.supervisor_running,
            }
        )
        payload["runtime"] = runtime
        return payload

    async def _persist(self, *, force: bool = False) -> None:
        payload = self.export_state()
        try:
            await self._store.flush(payload, force=force, recent_cap=self.settings().stats_recent_events)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - 落盘失败不应影响对话
            self._error(f"状态落盘失败：{exc}")

    async def flush(self, *, include_stats: bool = True) -> bool:
        """强制落盘一次（供 ``terminate`` 与命令使用）。"""
        payload = self.export_state(include_stats=include_stats)
        return await self._store.flush(
            payload,
            force=True,
            recent_cap=self.settings().stats_recent_events,
        )

    def snapshot(self) -> dict[str, Any]:
        """给命令与 Web API 用的只读汇总（含全部作用域的标记，P1-1）。"""
        settings = self.settings()
        all_marks = self.all_marks()
        return {
            "marked": [
                dict(view.to_dict(), scope_key=key) for key, view in all_marks.items()
            ],
            "marked_by_profile": [
                dict(view.to_dict(), scope_key=key)
                for key, view in self._snapshot_by_profile.items()
            ],
            "counters": dict(self._metrics.counters),
            "supervisor_running": self.supervisor_running,
            "settings": settings.to_public_dict(),
        }

    # ------------------------------------------------------------------
    # 事件与日志
    # ------------------------------------------------------------------

    def _record_event(self, payload: dict[str, Any], *, kind: str) -> None:
        if self._event_recorder is None:
            return
        try:
            self._event_recorder(
                {
                    "kind": str(kind),
                    "provider_id": payload.get("provider_id"),
                    "state": payload.get("state"),
                    "reason": payload.get("reason"),
                    "at": self._clock(),
                }
            )
        except Exception:  # pragma: no cover
            pass

    def _info(self, message: str) -> None:
        if self._logger is not None and log_allowed(self.settings().log_level, "info"):
            self._logger.info(f"{LOG_PREFIX} {message}")

    def _warn(self, message: str) -> None:
        if self._logger is not None and log_allowed(self.settings().log_level, "warning"):
            self._logger.warning(f"{LOG_PREFIX} {message}")

    def _debug(self, message: str) -> None:
        if self._logger is not None and log_allowed(self.settings().log_level, "debug"):
            self._logger.debug(f"{LOG_PREFIX} {message}")

    def _error(self, message: str) -> None:
        if self._logger is not None:
            self._logger.error(f"{LOG_PREFIX} {message}")

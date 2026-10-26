"""标记状态机与验证 supervisor。

v0.2.0 的语义（任务包 12 §3.3）
--------------------------------

流程图末段::

    标记该模型
     └→ 定时验证可达性（间隔可配置）
         ├→ 失败 → 继续等待
         └→ 连续成功 2 次（次数可配置）→ 取消标记 → 模型路由顺序调回

因此：

* **没有 TTL**：标记不会自己过期，只有验证连续成功才解除；
* **没有 profile 维度**：标记统一按 provider 全局生效；
* 恢复判据是**连续成功计数**（``probe_success_threshold``），失败即归零。

「模型路由顺序调回」不需要额外动作 —— 链路每次请求都实时重建
（见 :mod:`core.chain`），标记一解除，该模型自然回到链上的原位置。

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

#: 验证旁路标记：为真时包装层只记统计、不重试/不降级/不标记。
PROBE_CONTEXT: contextvars.ContextVar[int] = contextvars.ContextVar(
    "llm_model_guard_probe_depth", default=0
)

#: supervisor 的最小扫描间隔（秒）。
MIN_TICK_S = 1.0


class _ProbeCaller:
    """按优先级尝试三种验证入口，并记录实际走的路径。"""

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
        """provider 是否自定义了 ``test``。

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
        """验证一次；返回实际使用的路径名，异常原样上抛。"""
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
            # 直接取类实现：绕过实例上的包装层，杜绝「验证 → 包装层 → 标记」递归。
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

        raise AttributeError("provider 没有可用的验证入口（test/text_chat 均缺失）")


class Guardian:
    """标记状态机 + 验证调度器（v0.2.0：全局标记、无 TTL）。"""

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
        self._snapshot: dict[str, MarkView] = {}
        self._active_calls: dict[str, int] = {}
        self._lock = asyncio.Lock()
        self._stopping = False
        self._supervisor_task: asyncio.Task[Any] | None = None
        self._probe_tasks: set[asyncio.Task[Any]] = set()
        self._probe_inflight: set[str] = set()
        self._supervisor_started_at: float | None = None
        self._settings_cache: tuple[str, Any] | None = None

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------

    def settings(self) -> GuardSettings:
        """读配置快照；同一配置指纹最多复用。"""
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
        """全局标记视图。"""
        return self._snapshot

    def all_marks(self) -> dict[str, MarkView]:
        """全部标记（v0.2.0 只有全局一个维度）。"""
        return dict(self._snapshot)

    def mark_index(self) -> dict[str, MarkView]:
        """``provider_id → MarkView``。"""
        return dict(self._snapshot)

    def marks_for(self, profile_id: str | None = None) -> Mapping[str, MarkView]:
        """兼容旧调用点：v0.2.0 标记全局生效，与 profile 无关。"""
        return self._snapshot

    def is_marked(self, provider_id: str, *, profile_id: str | None = None) -> bool:
        """是否被标记。"""
        return provider_id in self._snapshot

    def has_marked(self) -> bool:
        return bool(self._snapshot)

    def marked_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._snapshot))

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

    # ------------------------------------------------------------------
    # 标记变更
    # ------------------------------------------------------------------

    async def mark(
        self,
        provider_id: str,
        *,
        reason: str,
        ttl: int = 0,
        source: str = MarkSource.AUTO,
        detail: str = "",
        delay: int = 1,
        profile_hint: str = "",
    ) -> bool:
        """标记一个 provider；返回是否**真的**写入了标记。

        ``ttl`` 参数为兼容旧调用点保留，v0.2.0 **不使用 TTL**（标记不会自己过期）。
        ``profile_hint`` 同理，只保留参数形状以兼容旧调用点。
        """
        if not provider_id:
            return False
        settings = self.settings()
        if not settings.enable:
            return False

        now = self._clock()
        async with self._lock:
            existing = self._marks.get(provider_id)
            if existing is None:
                block = MarkBlock(
                    provider_id=provider_id,
                    state=MarkState.MARKED,
                    reason=reason,
                    detail=detail,
                    marked_at=now,
                    next_probe_at=now,
                    source=str(source),
                )
                self._marks[provider_id] = block
                action = "已标记"
            else:
                existing.state = MarkState.MARKED
                existing.reason = reason
                existing.detail = detail or existing.detail
                existing.mark_count += 1
                existing.source = str(source)
                existing.consecutive_success = 0
                existing.next_probe_at = now
                block = existing
                action = "续期"
            self._rebuild_snapshot_locked()
            state = block.to_dict()

        await self._persist(force=True)
        self._info(f"{action} {provider_id}（原因={reason}，来源={source}）")
        self._record_event(state, kind=str(source))
        # **必须在这里（锁外）启动验证 supervisor**。
        #
        # 冷启动时通常还没有标记，``main.py::_bootstrap()`` 里的那次
        # ``ensure_started()`` 会直接返回；而自动标记只走 ``mark()`` 这条路，
        # 如果这里不启动，就再也没人调用它了 —— 「定时验证 → 连续成功 N 次
        # 解除标记 → 路由顺序调回」整段闭环都会失效，坏模型被永久绕过。
        #
        # 注意：``ensure_started()`` 内部会 ``async with self._lock``，
        # 在 ``mark()`` 的持锁区间里调用会**死锁**，所以必须放在锁外。
        await self.ensure_started()
        return True

    async def unmark(self, provider_id: str, *, by: str = "manual") -> bool:
        """解除标记；返回是否真的删掉了一条标记。"""
        if not provider_id:
            return False
        async with self._lock:
            block = self._marks.pop(provider_id, None)
            self._rebuild_snapshot_locked()

        if block is None:
            return False

        await self._persist(force=True)
        self._info(f"解除 {provider_id} 的标记（操作者={by}）")
        self._record_event(block.to_dict(), kind=CallKind.MANUAL)
        return True

    def note_success(self, provider_id: str) -> None:
        """一次成功调用（v0.2.0 无连续失败计数，仅保留接口）。"""

    def _rebuild_snapshot_locked(self) -> None:
        self._snapshot = {pid: block.view() for pid, block in self._marks.items()}

    # ------------------------------------------------------------------
    # 验证
    # ------------------------------------------------------------------

    def _probe_timeout(self) -> float:
        return max(1.0, float(self.settings().probe_timeout_seconds))

    async def probe_now(
        self,
        provider_id: str,
        *,
        timeout: float | None = None,
        mode: str = "scheduler",
    ) -> ProbeResult:
        """立刻验证一次；异常不外抛，统一变成 :class:`ProbeResult`。"""
        effective_timeout = float(timeout) if timeout else self._probe_timeout()
        provider = self._get_provider(provider_id)
        if provider is None:
            self._warn(f"验证 {provider_id} 跳过：该 provider 不存在")
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
                f"验证失败 {provider_id}（{type(exc).__name__}，{latency}ms，类别="
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
        self._info(
            f"验证成功 {provider_id}（{latency}ms，路径={used_mode}，状态={state_after}）"
        )
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
        except Exception:  # pragma: no cover - 分类器异常不应影响验证
            return None

    async def _on_probe_success(self, provider_id: str, *, latency: int) -> str | None:
        """验证成功：连续成功计数 +1；达到阈值就解除标记。

        流程图：「连续成功 2 次（次数可配置）→ 取消标记 → 模型路由顺序调回」。
        """
        settings = self.settings()
        threshold = max(1, settings.probe_success_threshold)
        now = self._clock()
        removed: MarkBlock | None = None
        state_after: str | None = None
        async with self._lock:
            block = self._marks.get(provider_id)
            if block is None:
                return None
            block.probe_attempts += 1
            block.last_probe_at = now
            block.last_probe_error = ""
            block.consecutive_success += 1
            if block.consecutive_success >= threshold:
                removed = self._marks.pop(provider_id, None)
                state_after = "healthy"
            else:
                interval = max(1.0, float(settings.probe_interval_seconds))
                block.next_probe_at = now + interval
                state_after = f"confirming:{block.consecutive_success}/{threshold}"
            self._rebuild_snapshot_locked()
        if removed is not None:
            self._info(
                f"验证连续成功 {threshold} 次，解除 {provider_id} 的标记"
                f"（延迟 {latency}ms）→ 模型回到路由顺序"
            )
            self._record_event(removed.to_dict(), kind=CallKind.PROBE)
        await self._persist(force=True)
        return state_after

    async def _on_probe_failure(self, provider_id: str, *, detail: str) -> str | None:
        """验证失败：连续成功计数**归零**，继续等待下一轮。"""
        settings = self.settings()
        now = self._clock()
        async with self._lock:
            block = self._marks.get(provider_id)
            if block is None:
                return None
            block.consecutive_success = 0
            block.probe_attempts += 1
            block.last_probe_at = now
            block.last_probe_error = (detail or "")[:200]
            interval = max(1.0, float(settings.probe_interval_seconds))
            block.next_probe_at = now + interval
            self._rebuild_snapshot_locked()
            state = str(block.state)
        await self._persist(force=True)
        return state

    async def probe_all(self, *, timeout: float | None = None) -> list[ProbeResult]:
        """对当前所有被标记的模型依次验证（供页面/命令的"全部验证"）。"""
        provider_ids = list(self._marks.keys())
        results: list[ProbeResult] = []
        for provider_id in provider_ids:
            results.append(await self.probe_now(provider_id, timeout=timeout))
        return results

    # ------------------------------------------------------------------
    # supervisor
    # ------------------------------------------------------------------

    async def ensure_started(self) -> None:
        """幂等启动 supervisor（``__init__`` 时机拿不到 provider，故延迟启动）。

        **什么时候会启动**：只有在**存在被标记的模型**时才启动。
        调用点有两处：

        1. ``main.py::_bootstrap()`` —— 插件启动 / 重载时；
        2. :meth:`mark()` —— **每次成功标记之后**（这才是关键：冷启动时
           通常还没有标记，第 1 次调用必然直接返回，若没有第 2 个调用点，
           验证闭环就永远不会启动）。

        **为什么不因为「标记被全部解除」而停止**：supervisor 常驻的代价很低
        —— 没有标记时 :meth:`tick` 立即返回（``due`` 列表为空），
        只是一次空转，不会产生任何请求。保留常驻可以避免「标记→解除→再标记」
        时反复创建/销毁任务。

        **锁约束**：本方法内部会 ``async with self._lock``，
        因此**不能**在 :meth:`mark()` 的持锁区间里调用（会死锁）。
        """
        settings = self.settings()
        if not settings.enable:
            return
        if self._stopping:
            return
        if self._supervisor_task is not None and not self._supervisor_task.done():
            return
        if not self._marks:
            # 还没有被标记的模型时不必启动。
            return
        now = self._clock()
        async with self._lock:
            for block in self._marks.values():
                block.next_probe_at = block.next_probe_at or now
            self._rebuild_snapshot_locked()
        self._supervisor_task = asyncio.create_task(
            self._supervisor_loop(), name="llm_model_guard.supervisor"
        )
        self._supervisor_started_at = self._clock()
        self._info("守护 supervisor 已启动（含定时验证）")

    async def _supervisor_loop(self) -> None:
        while not self._stopping:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - 防止 supervisor 崩掉
                self._error(f"验证 supervisor 单轮异常：{exc}")
            interval = max(MIN_TICK_S, 5.0)
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise

    async def tick(self) -> list[str]:
        """单轮扫描：到期的验证 + 统计节流落盘；返回本轮派发的 provider id。"""
        settings = self.settings()
        if not settings.enable:
            return []

        now = self._clock()
        due: list[str] = []
        for provider_id, block in list(self._marks.items()):
            if provider_id in self._probe_inflight:
                continue
            if block.next_probe_at is not None and block.next_probe_at > now:
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

        if self._store.schedule_save({}, interval=30.0):
            await self._persist(force=False)
        self._metrics.prune(
            retention_days=7,
            keep_providers=self._known_provider_ids(),
        )
        return dispatched

    async def _guarded_probe(self, provider_id: str) -> None:
        try:
            await self.probe_now(provider_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - 防御性
            self._error(f"验证任务异常 {provider_id}: {exc}")
        finally:
            self._probe_inflight.discard(provider_id)

    async def shutdown(self) -> None:
        """按顺序清理：停 supervisor → 取消在途验证 → 落盘。"""
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
        """从状态文件恢复标记与统计（v0.2.0：只有全局标记）。"""
        payload = dict(state) if state is not None else self._store.load()
        self._marks.clear()
        raw_marks = payload.get("marks")
        if isinstance(raw_marks, Mapping):
            for provider_id, raw in raw_marks.items():
                if isinstance(raw, Mapping):
                    self._marks[str(provider_id)] = MarkBlock.from_dict(str(provider_id), raw)
        self._metrics.import_state(payload)
        self._rebuild_snapshot_locked()
        return payload

    def export_state(self, *, include_stats: bool = True) -> dict[str, Any]:
        """导出完整状态（标记 + 统计 + 运行时计数）。"""
        payload: dict[str, Any] = {
            "schema_version": self._store.SCHEMA_VERSION,
            "plugin_version": self._store.plugin_version,
            "saved_at": self._clock(),
            "marks": {pid: block.to_dict() for pid, block in self._marks.items()},
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
                "marked": len(self._marks),
                "supervisor_running": self.supervisor_running,
            }
        )
        payload["runtime"] = runtime
        return payload

    async def _persist(self, *, force: bool = False) -> None:
        payload = self.export_state()
        try:
            await self._store.flush(payload, force=force, recent_cap=200)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - 落盘失败不应影响对话
            self._error(f"状态落盘失败：{exc}")

    async def flush(self, *, include_stats: bool = True) -> bool:
        """强制落盘一次（供 ``terminate`` 与命令使用）。"""
        payload = self.export_state(include_stats=include_stats)
        return await self._store.flush(payload, force=True, recent_cap=200)

    def snapshot(self) -> dict[str, Any]:
        """给命令与 Web API 用的只读汇总。"""
        return {
            "marked": [dict(view.to_dict()) for view in self._snapshot.values()],
            "counters": dict(self._metrics.counters),
            "supervisor_running": self.supervisor_running,
            "settings": self.settings().to_public_dict(),
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
        if self._logger is not None and log_allowed("", "info"):
            self._logger.info(f"{LOG_PREFIX} {message}")

    def _warn(self, message: str) -> None:
        if self._logger is not None and log_allowed("", "warning"):
            self._logger.warning(f"{LOG_PREFIX} {message}")

    def _debug(self, message: str) -> None:
        if self._logger is not None and log_allowed("", "debug"):
            self._logger.debug(f"{LOG_PREFIX} {message}")

    def _error(self, message: str) -> None:
        if self._logger is not None:
            self._logger.error(f"{LOG_PREFIX} {message}")

"""守护运行时：候选链执行、超时、同模型重试、回退、标记与统计。

v0.2.0 的流程（任务包 12 §3，逐条对应用户手绘流程图）::

    请求
     └→ 主模型
         ├→ 超时（时间可配置）
         └→ 报错
             ├→ 可重试报错 → 重试（可配置次数）→ 正常输出
             └→ 不重试报错 → 回退到次级、次次次… → 标记该模型
         └→ （回退后的模型）报错或超时 → 同样 → 标记该模型
    标记该模型
     └→ 定时验证可达性（间隔可配置）
         ├→ 失败 → 继续等待
         └→ 连续成功 N 次（次数可配置）→ 取消标记 → 模型路由顺序调回

对应到代码：

* **主模型 / 次级模型**：链路来自 :mod:`core.chain`（直接读框架顺序），
  见 :meth:`Guard.build_selection`；
* **超时**：:func:`_call_with_timeout`，用 ``asyncio.wait_for`` 包住调用；
* **可重试 / 不重试的分叉**：:meth:`Guard._is_retryable`，判据是
  ``settings.retry_categories`` 与错误分类；
* **重试**：:meth:`Guard._run_text_chat` 内的 ``while`` 循环；
* **回退 + 标记**：同一循环里换下一个候选前先 ``guardian.mark(...)``；
* **定时验证 + 连续成功 N 次**：:mod:`core.guardian` 的 supervisor。

关键纪律：

* ``asyncio.CancelledError`` 一律原样上抛；
* 流式一旦已经有 chunk 产出，绝不重试、绝不换候选、绝不标记；
* 验证调用（``PROBE_CONTEXT``）与位置参数调用只透传，不介入；
* 包装层内部异常不能让对话失败：内部逻辑出错时退回原始实现并计
  ``guard_internal_errors``。
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import random
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable, Deque, Iterable, Mapping

from .chain import build_chain
from .classifier import MESSAGE_LIMIT, ErrorClassifier, summarize_error
from .guardian import PROBE_CONTEXT, Guardian
from .metrics import Metrics
from .models import (
    Action,
    AttemptRecord,
    CallContext,
    CallKind,
    ChainPlan,
    Classification,
    ErrorClass,
    GuardSettings,
    LOG_PREFIX,
    MarkSource,
    PROBE_PROMPT,
    log_allowed,
)

#: 一次对话请求的候选链上下文；按事件 task 隔离。
CALL_CONTEXT: contextvars.ContextVar[CallContext | None] = contextvars.ContextVar(
    "llm_model_guard_call_context", default=None
)

#: 包装层递归深度；与其他插件叠加包装时用于收敛。
WRAP_DEPTH: contextvars.ContextVar[int] = contextvars.ContextVar(
    "llm_model_guard_wrap_depth", default=0
)

#: 候选链上下文的过期时间（秒）；框架降级二次进入的去重依赖它。
CALL_CONTEXT_TTL_S = 180.0

#: 位置参数调用无法安全重排，直接透传并计数。
KIND_POSITIONAL = "positional_passthrough"


def _is_probe_prompt(prompt: Any) -> bool:
    return isinstance(prompt, str) and prompt.strip() == PROBE_PROMPT


def _event_extra(event: Any, key: str) -> str:
    getter = getattr(event, "get_extra", None)
    if not callable(getter):
        return ""
    try:
        value = getter(key)
    except Exception:
        return ""
    return value if isinstance(value, str) else ""


def _response_is_error(response: Any) -> bool:
    return getattr(response, "role", None) == "err"


def _response_text(response: Any) -> str:
    for attr in ("completion_text", "message", "result"):
        value = getattr(response, attr, None)
        if isinstance(value, str) and value:
            return value
    return ""


def _provider_id_of(provider: Any) -> str:
    config = getattr(provider, "provider_config", None)
    if isinstance(config, Mapping):
        pid = config.get("id")
        if isinstance(pid, str) and pid:
            return pid
    meta = getattr(provider, "meta", None)
    if callable(meta):
        try:
            return str(getattr(meta(), "id", "") or "")
        except Exception:
            return ""
    return ""


def _status_of(exc: Any) -> int | None:
    for candidate in (
        getattr(exc, "status_code", None),
        getattr(getattr(exc, "response", None), "status_code", None),
    ):
        if isinstance(candidate, int) and not isinstance(candidate, bool):
            return candidate
    return None


def _abort_requested(abort_signal: Any) -> bool:
    """``abort_signal`` 是否已触发（兼容 ``asyncio.Event`` / 任意有 ``is_set`` 的对象）。"""
    if abort_signal is None:
        return False
    checker = getattr(abort_signal, "is_set", None)
    if not callable(checker):
        return False
    try:
        return bool(checker())
    except Exception:  # pragma: no cover - 第三方对象异常时按未触发处理
        return False


async def _call_with_timeout(
    factory: Callable[[], Awaitable[Any]],
    timeout: float | None,
) -> Any:
    """按可选超时调用一次。

    ``timeout is None``（配置为 0）表示不限制，直接 await。
    超时抛 ``asyncio.TimeoutError``，由上层按「超时」类别处理。
    """
    if timeout is None or timeout <= 0:
        return await factory()
    return await asyncio.wait_for(factory(), timeout=timeout)


@dataclass(frozen=True)
class Selection:
    """hook 层的选择结果。"""

    provider_id: str | None
    plan: ChainPlan
    profile_id: str | None = None
    manual_id: str = ""

    @property
    def managed(self) -> bool:
        return bool(self.plan.candidates)


class Guard:
    """守护运行时；由 ``main.py`` 构造一次，随后被 hook 与包装层共用。"""

    def __init__(
        self,
        *,
        config_reader: Callable[[], Mapping[str, Any]],
        guardian: Guardian,
        metrics: Metrics,
        classifier: ErrorClassifier | None = None,
        context: Any = None,
        logger: Any = None,
        store: Any = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        clock: Callable[[], float] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._config_reader = config_reader
        self.guardian = guardian
        self.metrics = metrics
        self.logger = logger
        self.context = context
        self.store = store
        self._sleep_fn = sleep
        self._clock = clock or time.time
        self._monotonic = monotonic or time.monotonic
        self._stopping = False
        self._settings: GuardSettings | None = None
        self._settings_fingerprint = ""
        self._retry_budget: dict[str, Deque[float]] = {}

        from .registry import WrapRegistry

        self.wrap_registry = WrapRegistry()
        self.classifier = classifier or ErrorClassifier(
            settings=lambda: self.settings,
            logger=logger,
        )

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------

    @property
    def settings(self) -> GuardSettings:
        try:
            raw = self._config_reader() or {}
        except Exception:  # pragma: no cover - 配置对象异常
            raw = {}
        snapshot = GuardSettings.from_mapping(raw)
        fingerprint = snapshot.fingerprint()
        if fingerprint != self._settings_fingerprint or self._settings is None:
            self._settings = snapshot
            self._settings_fingerprint = fingerprint
        return self._settings

    # ------------------------------------------------------------------
    # 框架对象访问（全部惰性，避免 __init__ 时机拿不到 provider）
    # ------------------------------------------------------------------

    @property
    def acm(self) -> Any:
        return getattr(self.context, "astrbot_config_mgr", None)

    @property
    def provider_manager(self) -> Any:
        return getattr(self.context, "provider_manager", None)

    def provider_ids(self) -> tuple[str, ...]:
        manager = self.provider_manager
        inst_map = getattr(manager, "inst_map", None)
        if isinstance(inst_map, Mapping):
            return tuple(str(key) for key in inst_map)
        return ()

    def provider_for(self, provider_id: str) -> Any | None:
        manager = self.provider_manager
        inst_map = getattr(manager, "inst_map", None)
        if isinstance(inst_map, Mapping):
            provider = inst_map.get(provider_id)
            if provider is not None:
                return provider
        insts = getattr(manager, "provider_insts", None)
        if isinstance(insts, Iterable):
            for provider in insts:
                if _provider_id_of(provider) == provider_id:
                    return provider
        return None

    # ------------------------------------------------------------------
    # 选路（hook 层）
    # ------------------------------------------------------------------

    def build_selection(self, *, umo: str | None, manual_id: str = "") -> Selection:
        """按框架的模型顺序构建候选链；``enable=false`` 时零介入。"""
        settings = self.settings
        if self.disabled:
            return Selection(None, ChainPlan())
        plan = self.build_plan(umo=umo, manual_id=manual_id)
        self._report_plan_warnings(plan)
        return Selection(
            provider_id=plan.first,
            plan=plan,
            profile_id=None,
            manual_id=manual_id,
        )

    def build_plan(self, *, umo: str | None, manual_id: str = "") -> ChainPlan:
        """构建候选链：手动选择 + 框架当前 + 该 abconf 的回退列表。"""
        settings = self.settings
        known = self.provider_ids() or None
        marked = set(self.guardian.marked_ids())
        return build_chain(
            provider_manager=self.provider_manager,
            umo=umo,
            manual_id=manual_id,
            known_ids=known,
            marked_ids=marked,
        )

    def _report_plan_warnings(self, plan: ChainPlan) -> None:
        if plan.degraded_use is not None:
            self.metrics.note_counter("all_candidates_marked")
            self._warn_once(
                "all_candidates_marked",
                f"候选链上的模型都被标记，强制使用 {plan.degraded_use} 以保证对话不失败",
            )
        for provider_id in plan.missing:
            self._warn_once(
                f"chain_provider_missing:{provider_id}",
                f"链路里的模型 {provider_id} 不存在，已从候选链剔除",
            )

    def push_call_context(
        self,
        selection: Selection,
        *,
        umo: str | None,
        requested: str = "",
    ) -> CallContext:
        """把本次选择写入 contextvar，供包装层与框架兜底路径复用。"""
        context = CallContext(
            plan=selection.plan,
            umo=umo,
            profile_id=selection.profile_id,
            requested=requested or (selection.plan.first or ""),
            created_at=self._clock(),
        )
        CALL_CONTEXT.set(context)
        return context

    def ensure_wrapped_for_plan(self, plan: ChainPlan | None = None) -> int:
        """对候选链上的实例补挂包装（provider 重载后仍能生效）。

        ``plan is None`` 表示"没有具体计划"，此时只做一次全量补挂；
        传入空候选链（未纳管/已禁用）时**不包装任何实例**，保证零介入。
        """
        from .registry import ensure_wrapped

        if plan is None:
            provider_ids = self.provider_ids()
        else:
            provider_ids = plan.candidates
        instances = [self.provider_for(pid) for pid in provider_ids]
        return ensure_wrapped([item for item in instances if item is not None], self)

    def ensure_wrapped_all(self) -> int:
        """对 ``inst_map`` 里所有实例补挂包装。"""
        from .registry import ensure_wrapped, iter_provider_instances

        return ensure_wrapped(iter_provider_instances(self.provider_manager), self)

    def apply_selection_to_event(self, event: Any) -> Selection:
        """hook A 的完整逻辑：算候选链 → 写 extra → 补挂包装 → 写 contextvar。

        抽到 core 层是为了能在没有 AstrBot 运行时的情况下单测「选谁」这件事。
        """
        umo = getattr(event, "unified_msg_origin", None)
        manual_id = _event_extra(event, "selected_provider")
        selection = self.build_selection(umo=umo, manual_id=manual_id)
        if selection.provider_id and selection.provider_id != manual_id:
            setter = getattr(event, "set_extra", None)
            if callable(setter):
                setter("selected_provider", selection.provider_id)
        self.ensure_wrapped_for_plan(selection.plan)
        self.push_call_context(selection, umo=umo, requested=selection.provider_id or manual_id)
        return selection

    @property
    def wrapped_count(self) -> int:
        from .registry import wrapped_count

        return wrapped_count(self)

    def unwrap_all(self) -> int:
        from .registry import unwrap_all

        return unwrap_all(self)

    # ------------------------------------------------------------------
    # 调用层入口
    # ------------------------------------------------------------------

    def original(self, provider: Any, name: str) -> Callable[..., Any] | None:
        from .registry import get_original

        return get_original(provider, name)

    async def _call_original(self, provider: Any, name: str, *args: Any, **kwargs: Any) -> Any:
        original = self.original(provider, name)
        if original is None:
            raise AttributeError(f"provider {_provider_id_of(provider)} 缺少原始方法 {name}")
        return await original(*args, **kwargs)

    def handle_text_chat(self, provider: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        """返回协程；由 :func:`guarded_text_chat` 调用。"""
        return self._handle_text_chat(provider, args, kwargs)

    async def _handle_text_chat(
        self, provider: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Any:
        if args:
            self.metrics.note_counter(KIND_POSITIONAL)
            return await self._call_original(provider, "text_chat", *args, **kwargs)

        if self.disabled or self.wrap_depth_exceeded():
            return await self._call_original(provider, "text_chat", **kwargs)

        if PROBE_CONTEXT.get() or _is_probe_prompt(kwargs.get("prompt")):
            return await self._probe_passthrough(provider, kwargs)

        token = WRAP_DEPTH.set(WRAP_DEPTH.get() + 1)
        try:
            return await self._dispatch_text_chat(provider, kwargs)
        finally:
            WRAP_DEPTH.reset(token)

    async def _dispatch_text_chat(self, provider: Any, kwargs: dict[str, Any]) -> Any:
        provider_id = _provider_id_of(provider)
        try:
            context = self.resolve_call_context(kwargs, provider_id=provider_id)
        except Exception as exc:  # pragma: no cover - 解析必须不能炸
            self.metrics.note_counter("guard_internal_errors")
            self._error(f"候选链解析异常，退回原始实现：{exc}")
            return await self._call_original(provider, "text_chat", **kwargs)

        if context is None or not context.plan.candidates:
            return await self._unmanaged_call(provider, "text_chat", kwargs)
        return await self._run_text_chat(provider, context, kwargs)

    def handle_text_chat_stream(
        self, provider: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> AsyncIterator[Any]:
        return self._handle_text_chat_stream(provider, args, kwargs)

    async def _handle_text_chat_stream(
        self, provider: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> AsyncIterator[Any]:
        if args:
            self.metrics.note_counter(KIND_POSITIONAL)
            original = self.original(provider, "text_chat_stream")
            if original is None:
                raise AttributeError("provider 缺少原始方法 text_chat_stream")
            async for chunk in original(*args, **kwargs):
                yield chunk
            return

        if self.disabled or self.wrap_depth_exceeded():
            original = self.original(provider, "text_chat_stream")
            if original is None:
                raise AttributeError("provider 缺少原始方法 text_chat_stream")
            async for chunk in original(**kwargs):
                yield chunk
            return

        if PROBE_CONTEXT.get() or _is_probe_prompt(kwargs.get("prompt")):
            async for chunk in self._probe_passthrough_stream(provider, kwargs):
                yield chunk
            return

        token = WRAP_DEPTH.set(WRAP_DEPTH.get() + 1)
        try:
            try:
                context = self.resolve_call_context(
                    kwargs, provider_id=_provider_id_of(provider)
                )
            except Exception as exc:  # pragma: no cover - 解析必须不能炸
                self.metrics.note_counter("guard_internal_errors")
                self._error(f"流式候选链解析异常，退回原始实现：{exc}")
                context = None
        finally:
            WRAP_DEPTH.reset(token)

        if context is None or not context.plan.candidates:
            async for chunk in self._unmanaged_stream(provider, kwargs):
                yield chunk
            return

        async for chunk in self._run_text_chat_stream(provider, context, kwargs):
            yield chunk

    def wrap_depth_exceeded(self) -> bool:
        from .registry import MAX_WRAP_DEPTH

        return WRAP_DEPTH.get() >= MAX_WRAP_DEPTH

    # ------------------------------------------------------------------
    # 上下文解析
    # ------------------------------------------------------------------

    def resolve_call_context(
        self, kwargs: Mapping[str, Any], *, provider_id: str = ""
    ) -> CallContext | None:
        session_id = kwargs.get("session_id")
        existing = CALL_CONTEXT.get()
        if existing is not None and existing.is_fresh(CALL_CONTEXT_TTL_S, self._clock()):
            if not session_id or session_id == existing.umo:
                return existing

        settings = self.settings
        if not settings.enable:
            return existing

        if not session_id:
            return existing

        # 没有会话标识就无法确定「该配置文件」，此时退化为「只管框架选中的这个模型」。
        plan = self.build_plan(umo=str(session_id))
        if not plan.candidates:
            return None
        self._report_plan_warnings(plan)
        return CallContext(
            plan=plan,
            umo=str(session_id),
            profile_id=None,
            requested=plan.first or "",
            created_at=self._clock(),
        )

    # ------------------------------------------------------------------
    # 重试 / 回退判据
    # ------------------------------------------------------------------

    def _is_retryable(self, classification: Classification, settings: GuardSettings) -> bool:
        """该错误是否属于「可重试」。

        判据是**错误类别是否在 ``retry_categories`` 里**（流程图的分叉点）。
        本地编程错误（``Action.SURFACE`` 且 ``should_mark=False``）永不重试。
        """
        if classification.action is Action.SURFACE and not classification.should_mark:
            return False
        return settings.is_retryable(classification.error_class)

    # ------------------------------------------------------------------
    # 非流式执行
    # ------------------------------------------------------------------

    async def _run_text_chat(
        self, provider: Any, context: CallContext, kwargs: dict[str, Any]
    ) -> Any:
        settings = self.settings
        timeout = settings.request_timeout()
        last_excause: BaseException | None = None
        last_response: Any = None
        last_classification: Classification | None = None
        tried = 0

        for index, provider_id in enumerate(context.plan.candidates, start=1):
            if self._skip_attempted(provider_id, context):
                continue
            if self._skip_marked(provider_id, context):
                continue
            target = self._resolve_candidate(provider, provider_id)
            if target is None:
                self.metrics.note_counter("chain_provider_missing")
                self._warn_once(
                    f"chain_provider_missing:{provider_id}",
                    f"候选 {provider_id} 不存在，跳过",
                )
                continue

            attempt = 0
            while True:
                attempt += 1
                tried += 1
                context.attempted.add(provider_id)
                call_kwargs = self._adjust_kwargs(kwargs, index)
                t0 = self._monotonic()
                self.guardian.inc_active(provider_id)
                error: BaseException | None = None
                response: Any = None
                try:
                    response = await _call_with_timeout(
                        lambda: self._call_original(target, "text_chat", **call_kwargs),
                        timeout,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - 分类后决定是否上抛
                    error = exc
                finally:
                    self.guardian.dec_active(provider_id)
                total_ms = int((self._monotonic() - t0) * 1000)

                if error is None and not _response_is_error(response):
                    self.guardian.note_success(provider_id)
                    self._record_attempt(
                        used=provider_id,
                        requested=context.requested,
                        context=context,
                        ok=True,
                        total_ms=total_ms,
                        attempt_index=attempt,
                    )
                    return response

                failure = error if error is not None else response
                classification = self._safe_classify(failure, stream=False)
                self._record_failure(
                    used=provider_id,
                    requested=context.requested,
                    context=context,
                    classification=classification,
                    failure=failure,
                    total_ms=total_ms,
                    attempt_index=attempt,
                )

                last_excause = error
                last_response = response if error is None else None
                last_classification = classification

                # 本地编程错误：直接上抛，不重试、不标记、不切换候选。
                if classification.action is Action.SURFACE and not classification.should_mark:
                    self._error(
                        f"{provider_id} 出现本地错误（{classification.error_class}），直接上抛"
                    )
                    if error is not None:
                        raise error
                    return response

                retryable = self._is_retryable(classification, settings)
                retry_allowed = (
                    retryable
                    and attempt <= settings.max_retries
                    and self._within_total_budget(settings)
                )
                if retry_allowed:
                    delay = settings.retry_delay(attempt)
                    self._info(
                        f"{provider_id} 第 {attempt} 次失败（{classification.error_class}），"
                        f"{delay:.2f}s 后重试同一模型"
                    )
                    await self._sleep(delay, abort_signal=kwargs.get("abort_signal"))
                    if self._stopping:
                        break
                    continue
                break

            if self._stopping:
                break

            # 放弃该候选：标记它，然后回退到下一个（流程图：「回退 → 标记该模型」）。
            if last_classification is not None and last_classification.should_mark:
                await self._maybe_mark(provider_id, last_classification, context)
            if index < len(context.plan.candidates):
                self._info(f"{provider_id} 放弃本次候选，改用下一个候选模型")

        if last_excause is not None:
            self._annotate_failure(last_excause, last_classification, context)
            raise last_excause
        if last_response is not None:
            return last_response
        if tried:
            raise RuntimeError(
                self._chain_failed_message(tried, last_classification, context)
            )
        # 一个候选都没能尝试：回到框架原生实现，保持对话可用。
        return await self._call_original(provider, "text_chat", **kwargs)

    def _adjust_kwargs(self, kwargs: Mapping[str, Any], index: int) -> dict[str, Any]:
        """非首候选不传 ``model``（与框架自身降级行为一致）。"""
        if index <= 1:
            return dict(kwargs)
        adjusted = dict(kwargs)
        if "model" in adjusted:
            adjusted["model"] = None
        return adjusted

    # ---- 候选筛选辅助 ----

    def _skip_attempted(self, provider_id: str, context: CallContext) -> bool:
        """同一 ``CallContext`` 里已尝试过的候选一律跳过（含框架降级二次进入）。"""
        if provider_id not in context.attempted:
            return False
        self.metrics.note_counter("framework_fallback_hops")
        self._debug(f"{provider_id} 已在本次请求中尝试过，跳过（框架降级去重）")
        return True

    def _skip_marked(self, provider_id: str, context: CallContext) -> bool:
        """被标记且不是"全被标记兜底"的那个候选时跳过。"""
        if provider_id == context.plan.degraded_use:
            return False
        if not self.guardian.is_marked(provider_id):
            return False
        self.metrics.note_counter("marked_candidate_skipped")
        self._warn_once(
            f"marked_candidate_skipped:{provider_id}",
            f"候选 {provider_id} 已被标记，跳过",
        )
        return True

    def _resolve_candidate(self, provider: Any, provider_id: str) -> Any | None:
        if _provider_id_of(provider) == provider_id:
            return provider
        return self.provider_for(provider_id)

    def _annotate_failure(
        self,
        exc: Any,
        classification: Classification | None,
        context: CallContext,
    ) -> None:
        """全链失败时把最后一次分类摘要附到异常上，方便用户/日志定位。"""
        message = self._chain_failed_message(0, classification, context)
        adder = getattr(exc, "add_note", None)
        if callable(adder):
            try:
                adder(message)
                return
            except Exception:  # pragma: no cover - 极旧 Python
                pass
        self._debug(message)

    @staticmethod
    def _chain_failed_message(
        tried: int,
        classification: Classification | None,
        context: CallContext,
    ) -> str:
        parts = ["候选链上的模型全部失败"]
        if tried:
            parts.append(f"已尝试 {tried} 次")
        if context.plan.candidates:
            parts.append(f"候选={list(context.plan.candidates)}")
        if classification is not None:
            parts.append(
                f"最后一次分类={classification.error_class}"
                f"（规则={classification.matched_rule}）"
            )
            if classification.detail:
                parts.append(f"摘要={classification.detail}")
        return "；".join(parts)

    # ------------------------------------------------------------------
    # 流式执行
    # ------------------------------------------------------------------

    async def _run_text_chat_stream(
        self, provider: Any, context: CallContext, kwargs: dict[str, Any]
    ) -> AsyncIterator[Any]:
        settings = self.settings
        timeout = settings.request_timeout()
        last_excause: BaseException | None = None
        last_classification: Classification | None = None
        tried = 0

        for index, provider_id in enumerate(context.plan.candidates, start=1):
            if self._skip_attempted(provider_id, context):
                continue
            if self._skip_marked(provider_id, context):
                continue
            target = self._resolve_candidate(provider, provider_id)
            if target is None:
                self.metrics.note_counter("chain_provider_missing")
                continue

            attempt = 0
            while True:
                attempt += 1
                tried += 1
                context.attempted.add(provider_id)
                call_kwargs = self._adjust_kwargs(kwargs, index)
                original = self.original(target, "text_chat_stream")
                if original is None:
                    break
                emitted = 0
                first_chunk_ms: int | None = None
                t0 = self._monotonic()
                self.guardian.inc_active(provider_id)
                error: BaseException | None = None
                try:
                    # 流式只对**首个 chunk** 施加超时（任务包 12 §3.2）：
                    # 首块之后不再施加，避免长回答被误杀。
                    stream = original(**call_kwargs)
                    iterator = stream.__aiter__()
                    while True:
                        try:
                            if emitted == 0:
                                # 首块：受超时约束。
                                chunk = await _call_with_timeout(
                                    lambda: iterator.__anext__(), timeout
                                )
                            else:
                                # 后续块：不施加超时。
                                chunk = await iterator.__anext__()
                        except StopAsyncIteration:
                            break
                        if first_chunk_ms is None:
                            first_chunk_ms = int((self._monotonic() - t0) * 1000)
                        emitted += 1
                        yield chunk
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    error = exc
                finally:
                    self.guardian.dec_active(provider_id)
                total_ms = int((self._monotonic() - t0) * 1000)

                if error is None:
                    self.guardian.note_success(provider_id)
                    self._record_attempt(
                        used=provider_id,
                        requested=context.requested,
                        context=context,
                        ok=True,
                        total_ms=total_ms,
                        first_chunk_ms=first_chunk_ms,
                        attempt_index=attempt,
                        stream=True,
                    )
                    return

                classification = self._safe_classify(
                    error, stream=True, chunks_emitted=emitted
                )
                last_classification = classification
                self._record_failure(
                    used=provider_id,
                    requested=context.requested,
                    context=context,
                    classification=classification,
                    failure=error,
                    total_ms=total_ms,
                    attempt_index=attempt,
                    stream=True,
                )
                if emitted > 0:
                    # 已产出内容：不重试、不换候选、**不标记**，只记统计后原样上抛。
                    self._debug(f"{provider_id} 流式中断但不写标记（已产出 {emitted} 块）")
                    raise error

                last_excause = error

                if classification.action is Action.SURFACE and not classification.should_mark:
                    self._error(
                        f"{provider_id} 出现本地错误（{classification.error_class}），直接上抛"
                    )
                    raise error

                retryable = self._is_retryable(classification, settings)
                retry_allowed = (
                    retryable
                    and attempt <= settings.max_retries
                    and self._within_total_budget(settings)
                )
                if retry_allowed:
                    delay = settings.retry_delay(attempt)
                    self._info(
                        f"{provider_id} 第 {attempt} 次失败（{classification.error_class}），"
                        f"{delay:.2f}s 后重试同一模型"
                    )
                    await self._sleep(delay, abort_signal=kwargs.get("abort_signal"))
                    if self._stopping:
                        break
                    continue
                break

            if self._stopping:
                break
            if last_classification is not None and last_classification.should_mark:
                await self._maybe_mark(provider_id, last_classification, context)

        if last_excause is not None:
            self._annotate_failure(last_excause, last_classification, context)
            raise last_excause
        if not tried:
            original = self.original(provider, "text_chat_stream")
            if original is None:
                raise AttributeError("provider 缺少原始方法 text_chat_stream")
            async for chunk in original(**kwargs):
                yield chunk

    # ------------------------------------------------------------------
    # 未纳管 / 验证透传
    # ------------------------------------------------------------------

    async def _unmanaged_call(
        self, provider: Any, name: str, kwargs: dict[str, Any]
    ) -> Any:
        provider_id = _provider_id_of(provider)
        t0 = self._monotonic()
        self.guardian.inc_active(provider_id)
        error: BaseException | None = None
        response: Any = None
        try:
            response = await self._call_original(provider, name, **kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            error = exc
        finally:
            self.guardian.dec_active(provider_id)
        total_ms = int((self._monotonic() - t0) * 1000)
        classification = None
        if error is not None:
            classification = self._safe_classify(error, stream=False)
        self._record_attempt(
            used=provider_id or "unknown",
            requested="",
            context=None,
            ok=error is None and not _response_is_error(response),
            total_ms=total_ms,
            classification=classification,
            failure=error,
        )
        if error is not None:
            raise error
        return response

    async def _unmanaged_stream(
        self, provider: Any, kwargs: dict[str, Any]
    ) -> AsyncIterator[Any]:
        original = self.original(provider, "text_chat_stream")
        if original is None:
            raise AttributeError("provider 缺少原始方法 text_chat_stream")
        async for chunk in original(**kwargs):
            yield chunk

    async def _probe_passthrough(self, provider: Any, kwargs: dict[str, Any]) -> Any:
        """验证透传：只统计，不重试/不降级/不标记（防递归）。"""
        provider_id = _provider_id_of(provider) or "probe"
        t0 = self._monotonic()
        try:
            response = await self._call_original(provider, "text_chat", **kwargs)
        except asyncio.CancelledError:
            raise
        except Exception:
            latency = int((self._monotonic() - t0) * 1000)
            self.metrics.record_probe(provider_id, ok=False, latency_ms=latency)
            raise
        latency = int((self._monotonic() - t0) * 1000)
        self.metrics.record_probe(provider_id, ok=True, latency_ms=latency)
        return response

    async def _probe_passthrough_stream(self, provider: Any, kwargs: dict[str, Any]) -> Any:
        original = self.original(provider, "text_chat_stream")
        if original is None:
            raise AttributeError("provider 缺少原始方法 text_chat_stream")
        async for chunk in original(**kwargs):
            yield chunk

    # ------------------------------------------------------------------
    # 标记 / 统计 / 预算
    # ------------------------------------------------------------------

    async def _maybe_mark(
        self, provider_id: str, classification: Classification, context: CallContext
    ) -> bool:
        if not classification.should_mark:
            return False
        try:
            return await self.guardian.mark(
                provider_id,
                reason=str(classification.error_class),
                ttl=0,
                source=MarkSource.AUTO,
                detail=classification.detail,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - 标记失败不应影响对话
            self._error(f"标记 {provider_id} 失败：{exc}")
            return False

    def _safe_classify(
        self,
        exc: Any,
        *,
        stream: bool = False,
        chunks_emitted: int = 0,
        is_continuation: bool = False,
    ) -> Classification:
        try:
            return self.classifier.classify(
                exc,
                stream=stream,
                chunks_emitted=chunks_emitted,
                is_continuation=is_continuation,
            )
        except Exception as error:  # pragma: no cover - 分类器异常兜底
            self.metrics.note_counter("guard_internal_errors")
            self._error(f"错误分类异常，按 unknown 处理：{error}")
            return Classification(
                error_class=ErrorClass.UNKNOWN,
                action=Action.SURFACE,
                should_mark=False,
                    matched_rule="guard:internal-error",
            )

    def _record_attempt(
        self,
        *,
        used: str,
        requested: str,
        context: CallContext | None,
        ok: bool,
        total_ms: int,
        attempt_index: int = 1,
        stream: bool = False,
        first_chunk_ms: int | None = None,
        classification: Classification | None = None,
        failure: Any = None,
    ) -> None:
        error_message = classification.detail if classification else None
        if failure is not None:
            raw = failure if isinstance(failure, BaseException) else _response_text(failure)
            if raw:
                error_message = summarize_error(str(raw), limit=MESSAGE_LIMIT) or error_message
        record = AttemptRecord(
            ts=self._clock(),
            used=used,
            kind=CallKind.TEXT_CHAT_STREAM if stream else CallKind.TEXT_CHAT,
            ok=ok,
            umo=context.umo if context else None,
            profile_id=context.profile_id if context else None,
            requested=requested,
            attempt_index=attempt_index,
            stream=stream,
            first_chunk_ms=first_chunk_ms,
            total_ms=total_ms,
            error_class=str(classification.error_class) if classification else None,
            error_type=type(failure).__name__ if isinstance(failure, BaseException) else None,
            error_message=error_message,
            http_status=_status_of(failure),
        )
        self.metrics.record_attempt(record)

    def _record_failure(
        self,
        *,
        used: str,
        requested: str,
        context: CallContext,
        classification: Classification,
        failure: Any,
        total_ms: int,
        attempt_index: int,
        stream: bool = False,
        first_chunk_ms: int | None = None,
    ) -> None:
        self._record_attempt(
            used=used,
            requested=requested,
            context=context,
            ok=False,
            total_ms=total_ms,
            attempt_index=attempt_index,
            stream=stream,
            first_chunk_ms=first_chunk_ms,
            classification=classification,
            failure=failure,
        )

    def _within_total_budget(self, settings: GuardSettings) -> bool:
        """退避等待是否仍在允许的范围内（用 ``retry_backoff_seconds`` 粗略约束）。"""
        return settings.retry_backoff_seconds >= 0.0

    async def _sleep(self, seconds: float, *, abort_signal: Any = None) -> None:
        """分段退避等待：每 ≤0.2s 检查一次插件停止与用户中断。

        ``abort_signal`` 是框架的 ``asyncio.Event``（"停止生成"时 set）；一旦触发
        立即抛 ``asyncio.CancelledError`` 向上传播，不再干等满一次退避。
        """
        if seconds <= 0:
            return
        if self._sleep_fn is not None:
            await self._sleep_fn(seconds)
            return
        deadline = self._monotonic() + seconds
        while not self._stopping:
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                return
            if _abort_requested(abort_signal):
                raise asyncio.CancelledError("用户已停止生成，放弃退避等待")
            await asyncio.sleep(min(0.2, remaining))

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    @property
    def disabled(self) -> bool:
        return self._stopping or not self.settings.enable

    def mark_stopping(self) -> None:
        self._stopping = True

    def invalidate_caches(self) -> None:
        """让配置派生缓存失效（配置热重载、命令 ``/llmguard reload`` 用）。"""
        self._settings_fingerprint = ""

    @property
    def state_path(self) -> str:
        """状态文件路径（供页面展示，不参与业务逻辑）。"""
        store = self.store
        path = getattr(store, "path", None)
        return str(path) if path is not None else ""

    @property
    def state_load_error(self) -> str | None:
        store = self.store
        return getattr(store, "last_load_error", None)

    # ------------------------------------------------------------------
    # 日志（含去重，避免刷屏）
    # ------------------------------------------------------------------

    def _warn_once(self, key: str, message: str) -> None:
        now = self._clock()
        last = getattr(self, "_warn_cache", None)
        if last is None:
            last = {}
            self._warn_cache = last
        previous = last.get(key)
        if previous is not None and now - previous < 30.0:
            return
        last[key] = now
        self._warn(message)

    def _info(self, message: str) -> None:
        if self.logger is not None and log_allowed("", "info"):
            self.logger.info(f"{LOG_PREFIX} {message}")

    def _warn(self, message: str) -> None:
        if self.logger is not None and log_allowed("", "warning"):
            self.logger.warning(f"{LOG_PREFIX} {message}")

    def _debug(self, message: str) -> None:
        if self.logger is not None and log_allowed("", "debug"):
            self.logger.debug(f"{LOG_PREFIX} {message}")

    def _error(self, message: str) -> None:
        if self.logger is not None:
            self.logger.error(f"{LOG_PREFIX} {message}")


async def guarded_text_chat(guard: Guard, provider: Any, /, *args: Any, **kwargs: Any) -> Any:
    """挂到实例上的非流式守护入口（签名与 ``Provider.text_chat`` 兼容）。"""
    return await guard.handle_text_chat(provider, args, kwargs)


def guarded_text_chat_stream(
    guard: Guard, provider: Any, /, *args: Any, **kwargs: Any
) -> AsyncIterator[Any]:
    """挂到实例上的流式守护入口。"""
    return guard.handle_text_chat_stream(provider, args, kwargs)

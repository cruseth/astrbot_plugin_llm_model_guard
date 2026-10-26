"""守护运行时：候选链执行、同模型重试、降级、标记与统计。

本模块是插件最核心的一层，由三部分构成：

1. :class:`Guard`：运行时的唯一入口，持有配置快照、路由表、分类器、包装登记表；
2. :func:`guarded_text_chat` / :func:`guarded_text_chat_stream`：挂到 provider
   实例上的守护方法（由 :mod:`core.registry` 调用）；
3. 一次调用内的状态机：可重试则退避重试，不可重试则换下一个候选，全部失败才上抛。

关键纪律（规划文档 §10 / §12）：

* ``asyncio.CancelledError`` 一律原样上抛；
* 流式一旦已经有 chunk 产出，绝不重试、绝不换候选、绝不合成内容；
* 测活调用（``PROBE_CONTEXT``）与位置参数调用只透传，不介入；
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
    RouteConfig,
    log_allowed,
)
from .routing import (
    WARN_ALL_CANDIDATES_MARKED,
    WARN_ROUTE_PROVIDER_MISSING,
    build_candidate_chain,
    build_merged_route_table,
    collect_profile_entries,
    resolve_disabled_profiles,
    resolve_profile_id,
    select_route,
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

#: 框架配置文件列表的缓存时长（秒）。
#: 框架的 ``get_conf_list()`` 每次都要查 SharedPreferences 数据库，是热路径上的
#: 同步 IO；这里做短 TTL 缓存，代价是新建/删除配置文件最多延迟一个 TTL 才可见。
PROFILE_ENTRIES_TTL_SECONDS = 30.0

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


def _is_continuation(kwargs: Mapping[str, Any]) -> bool:
    if kwargs.get("tool_calls_result"):
        return True
    contexts = kwargs.get("contexts") or ()
    for item in contexts:
        role = item.get("role") if isinstance(item, Mapping) else getattr(item, "role", None)
        if role == "tool":
            return True
    return False


def _adjust_kwargs(kwargs: Mapping[str, Any], index: int) -> dict[str, Any]:
    """非首候选不传 ``model``（与框架自身降级行为一致，规划文档 §10.3）。"""
    if index <= 1:
        return dict(kwargs)
    adjusted = dict(kwargs)
    if "model" in adjusted:
        adjusted["model"] = None
    return adjusted


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


#: 请求参数 → provider_config["modalities"] 里对应的能力名（与框架一致）。
_MODALITY_REQUIREMENTS: tuple[tuple[str, str], ...] = (
    ("image_urls", "image"),
    ("audio_urls", "audio"),
    ("func_tool", "tool_use"),
)


def _required_modalities(kwargs: Mapping[str, Any]) -> set[str]:
    """本次请求实际需要哪些多模态能力（P2-3）。"""
    needed: set[str] = set()
    for key, modality in _MODALITY_REQUIREMENTS:
        value = kwargs.get(key)
        if value:
            needed.add(modality)
    return needed


def _modalities_of(provider: Any) -> list[str]:
    """provider 声明的 modalities；空/缺失表示"未配置=全支持"（与框架语义一致）。"""
    config = getattr(provider, "provider_config", None)
    if not isinstance(config, Mapping):
        return []
    modalities = config.get("modalities")
    if isinstance(modalities, (list, tuple)):
        return [str(item) for item in modalities]
    return []


def _candidate_supports(provider: Any, needed: set[str]) -> bool:
    """候选模型是否支持本次请求所需的多模态能力。"""
    if not needed:
        return True
    declared = _modalities_of(provider)
    if not declared:
        return True
    return needed <= set(declared)


def _abort_requested(abort_signal: Any) -> bool:
    """``abort_signal`` 是否已触发（兼容 ``asyncio.Event`` / 任意带 ``is_set`` 的对象）。"""
    if abort_signal is None:
        return False
    checker = getattr(abort_signal, "is_set", None)
    if not callable(checker):
        return False
    try:
        return bool(checker())
    except Exception:  # pragma: no cover - 第三方对象异常时按未触发处理
        return False


@dataclass(frozen=True)
class Selection:
    """hook 层的选择结果。"""

    provider_id: str | None
    plan: ChainPlan
    profile_id: str | None = None
    route: RouteConfig | None = None
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
        self._route_table: dict[str, RouteConfig] = {}
        self._route_warnings: list[str] = []
        self._route_fingerprint = ""
        self._profile_entries: list[dict[str, str]] = []
        self._profile_entries_fingerprint = ""
        self._profile_entries_fetched_at = -1e18
        self._disabled_profile_ids: frozenset[str] = frozenset()
        self._disabled_fingerprint = ""
        self._retry_budget: dict[str, Deque[float]] = {}
        self._classifier_installed: int = 0
        self._rules_signature: str = ""

        from .registry import WrapRegistry

        self.wrap_registry = WrapRegistry()
        self.classifier = classifier or ErrorClassifier(
            settings=lambda: self.settings,
            logger=logger,
        )
        self._sync_classifier_rules()

    # ------------------------------------------------------------------
    # 配置与路由表（带指纹缓存）
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

    @property
    def disabled(self) -> bool:
        return self._stopping or not self.settings.enable

    @property
    def profile_entries(self) -> list[dict[str, str]]:
        """框架里的配置文件列表（id / name / path），带 TTL 缓存。

        框架的 ``get_conf_list()`` 每次都会查一次 SharedPreferences 数据库，
        属于热路径上的同步 IO，因此这里做 **TTL 缓存**：默认
        :data:`PROFILE_ENTRIES_TTL_SECONDS` 秒内复用上一次结果。

        缓存过期后重新拉取：框架新建/删除配置文件最多延迟一个 TTL 才会被看到
        （新建后可用 ``/llmguard reload`` 立即刷新）。
        """
        now = self._monotonic()
        if (
            self._profile_entries
            and (now - self._profile_entries_fetched_at) < PROFILE_ENTRIES_TTL_SECONDS
        ):
            return self._profile_entries
        entries = collect_profile_entries(self.acm)
        self._profile_entries = entries
        self._profile_entries_fetched_at = now
        self._profile_entries_fingerprint = "|".join(
            sorted(f"{item['id']}::{item['name']}" for item in entries)
        )
        return self._profile_entries

    def refresh_profile_entries(self) -> list[dict[str, str]]:
        """强制重新拉取框架配置文件列表（忽略 TTL 缓存）。

        用于「用户在框架里刚新建了 abconf，想立刻在插件配置页看到它」的场景。
        """
        self._profile_entries_fetched_at = -1e18
        return self.profile_entries

    @property
    def route_table(self) -> dict[str, RouteConfig]:
        settings = self.settings
        entries = self.profile_entries
        # 指纹同时包含配置与配置文件列表：框架里新增/重命名 abconf 后，
        # 按名称配置的路由才能被重新解析。
        fingerprint = f"{settings.fingerprint()}\x1e{self._profile_entries_fingerprint}"
        if fingerprint != self._route_fingerprint:
            warnings: list[str] = []
            table, _resolved = build_merged_route_table(
                settings.routes,
                settings.profiles,
                entries=entries,
                warnings=warnings,
            )
            self._route_table = table
            self._route_warnings = warnings
            self._route_fingerprint = fingerprint
            self._sync_classifier_rules()
            for code in warnings:
                self._warn(f"路由配置告警：{code}")
        return self._route_table

    @property
    def disabled_profile_ids(self) -> frozenset[str]:
        """``disabled_profiles`` 解析后的真实 id 集合（支持填名称）。"""
        settings = self.settings
        entries = self.profile_entries
        fingerprint = f"{settings.fingerprint()}\x1e{self._profile_entries_fingerprint}"
        if fingerprint != self._disabled_fingerprint:
            warnings: list[str] = []
            resolved = resolve_disabled_profiles(
                settings.disabled_profiles,
                entries=entries,
                warnings=warnings,
            )
            self._disabled_profile_ids = frozenset(resolved)
            self._disabled_fingerprint = fingerprint
            for code in warnings:
                self._warn(f"不接管列表告警：{code}")
        return self._disabled_profile_ids

    def _sync_classifier_rules(self) -> None:
        """把配置里的自定义规则同步到分类器；规则内容变化才重新编译。"""
        rules = [dict(rule) for rule in self.settings.error_rules]
        signature = json.dumps(
            rules,
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
        if signature == self._rules_signature:
            return
        count = self.classifier.reload_rules(rules)
        self._rules_signature = signature
        self._classifier_installed = len(rules)
        if rules:
            self._debug(f"已编译 {count}/{len(rules)} 条自定义错误规则")

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

    def native_provider_id(self, umo: str | None) -> str | None:
        getter = getattr(self.context, "get_using_provider", None)
        if not callable(getter) or not umo:
            return None
        try:
            provider = getter(umo=umo)
        except TypeError:
            try:
                provider = getter(umo)
            except Exception:
                return None
        except Exception:
            return None
        if provider is None:
            return None
        return _provider_id_of(provider) or None

    def framework_fallback_ids(self, umo: str | None) -> list[str]:
        acm = self.acm
        get_conf = getattr(acm, "get_conf", None)
        if not callable(get_conf) or not umo:
            return []
        try:
            conf = get_conf(umo)
        except Exception:
            return []
        settings_block = conf.get("provider_settings") if isinstance(conf, Mapping) else None
        if not isinstance(settings_block, Mapping):
            return []
        fallbacks = settings_block.get("fallback_chat_models") or []
        if not isinstance(fallbacks, Iterable) or isinstance(fallbacks, (str, bytes)):
            return []
        return [str(item) for item in fallbacks if isinstance(item, str) and item.strip()]

    # ------------------------------------------------------------------
    # 选路（hook 层）
    # ------------------------------------------------------------------

    def build_selection(self, *, umo: str | None, manual_id: str = "") -> Selection:
        """解析配置文件 → 路由 → 候选链；``enable=false`` 时零介入。"""
        settings = self.settings
        if self.disabled:
            return Selection(None, ChainPlan())
        profile_id = resolve_profile_id(umo, self.acm)
        route: RouteConfig | None = None
        if settings.enable_routing:
            route = select_route(
                profile_id=profile_id,
                table=self.route_table,
                disabled_profiles=self.disabled_profile_ids,
            )
        native_id = self.native_provider_id(umo)
        marks = self.guardian.marks_for(profile_id)
        known_ids = self.provider_ids() or None
        warnings: list[str] = []
        # 手动选择（webchat）与会话级切换在框架里同源：都是 event 的
        # ``selected_provider`` extra，因此两项开关任一开启即尊重该选择。
        respect_manual = (
            settings.respect_manual_selection or settings.respect_session_selection
        )
        plan = build_candidate_chain(
            route=route,
            requested_id=manual_id,
            native_id=native_id,
            framework_fallback_ids=self.framework_fallback_ids(umo),
            fallback_pool=settings.fallback_pool,
            marks=marks,
            union_framework_fallbacks=settings.union_framework_fallbacks,
            respect_manual_selection=respect_manual,
            known_ids=known_ids,
            warnings=warnings,
        )
        self._report_plan_warnings(plan)
        selection = Selection(
            provider_id=plan.first,
            plan=plan,
            profile_id=profile_id,
            route=route,
            manual_id=manual_id,
        )
        return selection

    def _report_plan_warnings(self, plan: ChainPlan) -> None:
        if plan.degraded_use is not None:
            self.metrics.note_counter("all_candidates_marked")
            self._warn_once(
                "all_candidates_marked",
                f"候选链上的模型都被标记，强制使用 {plan.degraded_use} 以保证对话不失败",
            )
        for provider_id in plan.missing:
            self._warn_once(
                f"route_provider_missing:{provider_id}",
                f"路由引用的模型 {provider_id} 不存在，已从候选链剔除",
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
            route=selection.route,
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
    # 上下文解析（D2：session_id → contextvar → 未纳管）
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

        if not settings.enable_routing:
            # 不接管选择，但仍对「框架选中的这个模型」做重试/标记/统计。
            if not provider_id:
                return existing
            return CallContext(
                plan=ChainPlan(candidates=(provider_id,), source="native"),
                umo=str(session_id) if session_id else None,
                profile_id=resolve_profile_id(str(session_id), self.acm) if session_id else None,
                requested=provider_id,
                created_at=self._clock(),
            )

        if not session_id:
            return existing

        profile_id = resolve_profile_id(str(session_id), self.acm)
        if profile_id is None:
            return None
        route = select_route(
            profile_id=profile_id,
            table=self.route_table,
            disabled_profiles=self.disabled_profile_ids,
        )
        if route is None:
            return None
        marks = self.guardian.marks_for(profile_id)
        plan = build_candidate_chain(
            route=route,
            framework_fallback_ids=self.framework_fallback_ids(str(session_id)),
            fallback_pool=settings.fallback_pool,
            marks=marks,
            union_framework_fallbacks=settings.union_framework_fallbacks,
            respect_manual_selection=False,
            known_ids=self.provider_ids() or None,
        )
        if not plan.candidates:
            return None
        self._report_plan_warnings(plan)
        return CallContext(
            plan=plan,
            umo=str(session_id),
            profile_id=profile_id,
            requested=plan.first or "",
            route=route,
            created_at=self._clock(),
        )

    # ------------------------------------------------------------------
    # 非流式执行
    # ------------------------------------------------------------------

    async def _run_text_chat(
        self, provider: Any, context: CallContext, kwargs: dict[str, Any]
    ) -> Any:
        settings = self.settings
        is_continuation = _is_continuation(kwargs)
        abort_signal = kwargs.get("abort_signal")
        needed_modalities = _required_modalities(kwargs)
        started_all = self._monotonic()
        last_excause: BaseException | None = None
        last_response: Any = None
        last_classification: Classification | None = None
        tried = 0
        allow_fallback = True

        for index, provider_id in enumerate(context.plan.candidates, start=1):
            if self._skip_attempted(provider_id, context):
                continue
            if self._skip_marked(provider_id, context):
                continue
            target = self._resolve_candidate(provider, provider_id)
            if target is None:
                self.metrics.note_counter("route_provider_missing")
                self._warn_once(
                    f"route_provider_missing:{provider_id}",
                    f"候选 {provider_id} 不存在，跳过",
                )
                continue
            if index > 1 and not _candidate_supports(target, needed_modalities):
                self._skip_modality_incompatible(provider_id, target, needed_modalities)
                continue

            per_model_retries, _, _ = settings.route_execution(context.route)
            attempt = 0
            while True:
                attempt += 1
                tried += 1
                context.attempted.add(provider_id)
                call_kwargs = _adjust_kwargs(kwargs, index)
                t0 = self._monotonic()
                self.guardian.inc_active(provider_id)
                error: BaseException | None = None
                response: Any = None
                try:
                    response = await self._call_original(target, "text_chat", **call_kwargs)
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
                classification = self._safe_classify(
                    failure,
                    stream=False,
                    chunks_emitted=0,
                    is_continuation=is_continuation,
                )
                self._record_failure(
                    used=provider_id,
                    requested=context.requested,
                    context=context,
                    classification=classification,
                    failure=failure,
                    total_ms=total_ms,
                    attempt_index=attempt,
                )
                await self._maybe_mark(provider_id, classification, context)

                last_excause = error
                last_response = response if error is None else None
                last_classification = classification
                allow_fallback = classification.allows_fallback

                if error is None and not allow_fallback:
                    # 不重试也不降级：把 err 响应原样交给框架。
                    return response

                retry_allowed = (
                    classification.allows_retry
                    and attempt <= per_model_retries
                    and self._within_total_budget(started_all, settings)
                    and self._consume_retry_budget(context.umo, settings)
                )
                if retry_allowed:
                    delay = self.backoff_delay(attempt, classification.retry_after, settings)
                    self._info(
                        f"{provider_id} 第 {attempt} 次失败（{classification.error_class}），"
                        f"{delay:.2f}s 后重试同一模型"
                    )
                    await self._sleep(delay, abort_signal=abort_signal)
                    if self._stopping:
                        break
                    continue
                break

            if self._stopping:
                break
            if not allow_fallback:
                break
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

    # ---- 候选筛选辅助（P2-1 / P2-3）----

    def _skip_attempted(self, provider_id: str, context: CallContext) -> bool:
        """同一 ``CallContext`` 里已尝试过的候选一律跳过（含框架降级二次进入）。"""
        if provider_id not in context.attempted:
            return False
        self.metrics.note_counter("framework_fallback_hops")
        self._debug(f"{provider_id} 已在本次请求中尝试过，跳过（框架降级去重）")
        return True

    def _skip_marked(self, provider_id: str, context: CallContext) -> bool:
        """被标记且不是"全被标记兜底"的那个候选时跳过（P2-1 第二道保险）。"""
        if provider_id == context.plan.degraded_use:
            return False
        if not self.guardian.is_marked(provider_id, profile_id=context.profile_id):
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

    def _skip_modality_incompatible(
        self, provider_id: str, target: Any, needed: set[str]
    ) -> None:
        self.metrics.note_counter("candidate_modality_incompatible")
        self._warn_once(
            f"modality:{provider_id}",
            f"跳过候选 {provider_id}：本次请求需要 {sorted(needed)}，"
            f"该模型 modalities={_modalities_of(target)}（空表示支持全部）",
        )

    def _annotate_failure(
        self,
        exc: BaseException,
        classification: Classification | None,
        context: CallContext,
    ) -> None:
        """全链失败时把最后一次分类摘要附到异常上，方便用户/日志定位（P3-9）。"""
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
        is_continuation = _is_continuation(kwargs)
        abort_signal = kwargs.get("abort_signal")
        needed_modalities = _required_modalities(kwargs)
        started_all = self._monotonic()
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
                self.metrics.note_counter("route_provider_missing")
                continue
            if index > 1 and not _candidate_supports(target, needed_modalities):
                self._skip_modality_incompatible(provider_id, target, needed_modalities)
                continue

            per_model_retries, _, _ = settings.route_execution(context.route)
            attempt = 0
            while True:
                attempt += 1
                tried += 1
                context.attempted.add(provider_id)
                call_kwargs = _adjust_kwargs(kwargs, index)
                original = self.original(target, "text_chat_stream")
                if original is None:
                    break
                emitted = 0
                first_chunk_ms: int | None = None
                t0 = self._monotonic()
                self.guardian.inc_active(provider_id)
                error: BaseException | None = None
                try:
                    async for chunk in original(**call_kwargs):
                        if first_chunk_ms is None:
                            first_chunk_ms = int((self._monotonic() - t0) * 1000)
                        emitted += 1
                        yield chunk
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - 分类后决定是否上抛
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
                        stream=True,
                        attempt_index=attempt,
                    )
                    return

                classification = self._safe_classify(
                    error,
                    stream=True,
                    chunks_emitted=emitted,
                    is_continuation=is_continuation,
                )
                last_classification = classification
                self._record_failure(
                    used=provider_id,
                    requested=context.requested,
                    context=context,
                    classification=classification,
                    failure=error,
                    total_ms=total_ms,
                    first_chunk_ms=first_chunk_ms,
                    stream=True,
                    attempt_index=attempt,
                )
                if emitted > 0:
                    # 已产出内容：不重试、不换候选、**不标记**，只记统计后原样上抛
                    # （任务包 02 §3.4 / 裁决 P2-5）。
                    self._debug(f"{provider_id} 流式中断但不写标记（已产出 {emitted} 块）")
                    raise error

                await self._maybe_mark(provider_id, classification, context)
                last_excause = error

                retry_allowed = (
                    classification.allows_retry
                    and attempt <= per_model_retries
                    and self._within_total_budget(started_all, settings)
                    and self._consume_retry_budget(context.umo, settings)
                )
                if retry_allowed:
                    delay = self.backoff_delay(attempt, classification.retry_after, settings)
                    self._info(
                        f"{provider_id} 流式第 {attempt} 次失败（{classification.error_class}），"
                        f"{delay:.2f}s 后重试同一模型"
                    )
                    await self._sleep(delay, abort_signal=abort_signal)
                    if self._stopping:
                        break
                    continue
                break

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
    # 未纳管 / 测活透传
    # ------------------------------------------------------------------

    async def _unmanaged_call(
        self, provider: Any, name: str, kwargs: dict[str, Any]
    ) -> Any:
        settings = self.settings
        provider_id = _provider_id_of(provider)
        if not settings.enable_monitoring or not settings.unmanaged_monitoring:
            return await self._call_original(provider, name, **kwargs)
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
            classification = self._safe_classify(error, stream=False, chunks_emitted=0)
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
        """测活透传：只统计，不重试、不降级、不标记（D7 防递归）。"""
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

    async def _probe_passthrough_stream(
        self, provider: Any, kwargs: dict[str, Any]
    ) -> AsyncIterator[Any]:
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
                ttl=classification.mark_ttl,
                source=MarkSource.AUTO,
                detail=classification.detail,
                delay=classification.mark_delay,
                profile_hint=context.profile_id or "",
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
                mark_ttl=0,
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
        if not self.settings.enable_monitoring:
            return
        error_message = classification.detail if classification else None
        if failure is not None:
            raw = failure if isinstance(failure, BaseException) else _response_text(failure)
            if raw:
                # 最近事件里的错误摘要允许到 500 字符（落盘字段白名单仍受约束）。
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

    def _within_total_budget(self, started_all: float, settings: GuardSettings) -> bool:
        if settings.retry_total_timeout <= 0:
            return True
        return (self._monotonic() - started_all) < settings.retry_total_timeout

    def _consume_retry_budget(self, umo: str | None, settings: GuardSettings) -> bool:
        limit = settings.retry_budget_per_umo_per_minute
        if limit <= 0:
            return True
        key = umo or ""
        now = self._clock()
        bucket = self._retry_budget.setdefault(key, deque())
        while bucket and now - bucket[0] > 60.0:
            bucket.popleft()
        if len(bucket) >= limit:
            self._warn_once(
                f"retry_budget:{key}",
                f"会话 {key or '(空)'} 每分钟重试次数已达上限 {limit}，本次不再重试",
            )
            return False
        bucket.append(now)
        return True

    def backoff_delay(
        self, attempt: int, retry_after: float | None, settings: GuardSettings
    ) -> float:
        if retry_after is not None and retry_after >= 0:
            return min(max(0.0, retry_after), settings.retry_backoff_max or retry_after)
        raw = settings.retry_backoff_base * (settings.retry_backoff_factor ** max(0, attempt - 1))
        raw = min(raw, settings.retry_backoff_max)
        ratio = settings.retry_jitter_ratio
        if ratio > 0:
            raw *= 1.0 + random.uniform(-ratio, ratio)
        return max(0.0, raw)

    async def _sleep(self, seconds: float, *, abort_signal: Any = None) -> None:
        """分段退避等待：每 ≤0.2s 检查一次插件停止与用户中断（P2-2）。

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

    def mark_stopping(self) -> None:
        self._stopping = True

    def invalidate_caches(self) -> None:
        """让配置派生缓存失效（配置热重载、命令 ``/llmguard reload`` 用）。"""
        self._settings_fingerprint = ""
        self._route_fingerprint = ""
        self._disabled_fingerprint = ""
        # 配置文件列表也立刻重取，方便刚在 WebUI 新建的 abconf 马上可用。
        self._profile_entries_fetched_at = -1e18

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
        if self.logger is not None and log_allowed(self.settings.log_level, "info"):
            self.logger.info(f"{LOG_PREFIX} {message}")

    def _warn(self, message: str) -> None:
        if self.logger is not None and log_allowed(self.settings.log_level, "warning"):
            self.logger.warning(f"{LOG_PREFIX} {message}")

    def _debug(self, message: str) -> None:
        if self.logger is not None and log_allowed(self.settings.log_level, "debug"):
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

"""纯数据模型：核心枚举、配置快照与运行时视图。

设计约束（见规划文档 §4.2）：

* 本模块只依赖 Python 标准库，**不导入 AstrBot**，也不做任何 I/O；
* 所有 dataclass 都是不可变的（``frozen=True``），便于在热路径安全共享；
* 其余 ``core`` 模块都只依赖本模块，依赖方向严格单向。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Iterable, Mapping, Sequence

#: 插件名，必须与目录名一致（``metadata.yaml`` 的 ``name`` 同值）。
PLUGIN_NAME = "astrbot_plugin_llm_model_guard"

#: 统一日志前缀。
LOG_PREFIX = "[llm_model_guard]"

#: 测活使用的提示词，与框架 ``Provider.test()`` 完全一致。
PROBE_PROMPT = "REPLY `PONG` ONLY"

#: 标记作用域的两种取值。
MARK_SCOPE_GLOBAL = "global"
MARK_SCOPE_PROFILE = "profile"

#: 兜底路由的 profile_id 归一化值。
WILDCARD_PROFILE = "*"

#: 日志级别顺序（数值越大越啰嗦）。
LOG_LEVEL_ORDER: dict[str, int] = {
    "error": 0,
    "warning": 1,
    "info": 2,
    "debug": 3,
}


def log_allowed(configured: str, level: str) -> bool:
    """``log_level`` 配置是否允许输出该级别的插件日志。"""
    threshold = LOG_LEVEL_ORDER.get(str(configured or "info").strip().lower(), 2)
    current = LOG_LEVEL_ORDER.get(str(level).strip().lower(), 2)
    return current <= threshold


class ErrorClass(StrEnum):
    """错误分类枚举（规划文档 §9.2 + 审查修复 P2-6）。"""

    AUTH = "auth"
    MODEL_NOT_FOUND = "model_not_found"
    BAD_REQUEST = "bad_request"
    #: 400/422 但消息没有命中任何细化规则：允许降级，但**不标记**
    #: （避免把插件自身 payload 不兼容误判成模型永久损坏）。
    BAD_REQUEST_SOFT = "bad_request_soft"
    CONTEXT_TOO_LONG = "context_too_long"
    CONTENT_FILTER = "content_filter"
    QUOTA_EXHAUSTED = "quota_exhausted"
    RATE_LIMIT = "rate_limit"
    OVERLOADED = "overloaded"
    SERVER_ERROR = "server_error"
    TIMEOUT = "timeout"
    NETWORK = "network"
    EMPTY_OUTPUT = "empty_output"
    UNKNOWN = "unknown"


class Action(StrEnum):
    """对一次失败的处置动作（规划文档 §4.3）。"""

    PASSTHROUGH = "passthrough"
    RETRY_SAME = "retry_same"
    FALLBACK = "fallback"
    RETRY_THEN_FALLBACK = "retry_then_fallback"
    MARK_AND_FALLBACK = "mark_and_fallback"
    SURFACE = "surface"


class MarkState(StrEnum):
    """标记状态机的两种落盘状态（规划文档 §11.1）。

    ``HEALTHY`` 与 ``EXPIRED`` 不落盘：前者表示键不存在，后者是"删除键"这一
    动作本身，所以这里只有两个状态。
    """

    MARKED = "marked"
    CONFIRMING = "confirming"


class MarkSource(StrEnum):
    """标记来源。"""

    AUTO = "auto"
    MANUAL = "manual"


class CallKind(StrEnum):
    """统计里的调用种类。"""

    TEXT_CHAT = "text_chat"
    TEXT_CHAT_STREAM = "text_chat_stream"
    PROBE = "probe"
    MANUAL = "manual"


@dataclass(frozen=True)
class Classification:
    """一次错误分类的结果，必须可解释（规则名 + 脱敏摘要）。"""

    error_class: ErrorClass
    action: Action
    should_mark: bool
    mark_ttl: int
    mark_delay: int = 1
    retry_after: float | None = None
    matched_rule: str = ""
    detail: str = ""

    @property
    def allows_retry(self) -> bool:
        return self.action in (Action.RETRY_SAME, Action.RETRY_THEN_FALLBACK)

    @property
    def allows_fallback(self) -> bool:
        return self.action in (
            Action.FALLBACK,
            Action.RETRY_THEN_FALLBACK,
            Action.MARK_AND_FALLBACK,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_class": str(self.error_class),
            "action": str(self.action),
            "should_mark": self.should_mark,
            "mark_ttl": self.mark_ttl,
            "mark_delay": self.mark_delay,
            "retry_after": self.retry_after,
            "matched_rule": self.matched_rule,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class RouteConfig:
    """一条路由：某个配置文件的主模型与有序回退链。"""

    profile_id: str
    primary: str = ""
    fallbacks: tuple[str, ...] = ()
    max_retries: int | None = None
    probe_interval: int | None = None
    probe_timeout: float | None = None
    enable: bool = True
    note: str = ""

    @property
    def is_wildcard(self) -> bool:
        return self.profile_id == WILDCARD_PROFILE

    @property
    def is_usable(self) -> bool:
        """``enable=false`` 或 ``primary`` 为空都视为禁用（规划文档 §7.2-3）。"""
        return self.enable and bool(self.primary)

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "primary": self.primary,
            "fallbacks": list(self.fallbacks),
            "max_retries": self.max_retries,
            "probe_interval": self.probe_interval,
            "probe_timeout": self.probe_timeout,
            "enable": self.enable,
            "note": self.note,
        }


@dataclass(frozen=True)
class ChainPlan:
    """一次调用的候选链计划。"""

    candidates: tuple[str, ...] = ()
    bypassed: tuple[str, ...] = ()
    degraded_use: str | None = None
    source: str = "unmanaged"
    missing: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def first(self) -> str | None:
        return self.candidates[0] if self.candidates else None

    @property
    def managed(self) -> bool:
        return self.source in ("route", "native") and bool(self.candidates)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidates": list(self.candidates),
            "bypassed": list(self.bypassed),
            "degraded_use": self.degraded_use,
            "source": self.source,
            "missing": list(self.missing),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class MarkView:
    """标记的只读视图（热路径读它，不加锁）。"""

    provider_id: str
    state: MarkState = MarkState.MARKED
    reason: str = ""
    detail: str = ""
    marked_at: float = 0.0
    expires_at: float | None = None
    first_success_at: float | None = None
    consecutive_probe_failures: int = 0
    probe_attempts: int = 0
    last_probe_at: float | None = None
    next_probe_at: float | None = None
    source: str = MarkSource.AUTO
    profile_hint: str = ""
    mark_count: int = 1
    last_probe_error: str = ""

    @property
    def is_pending_confirm(self) -> bool:
        return self.state is MarkState.CONFIRMING

    def is_expired(self, now: float | None = None) -> bool:
        if self.expires_at is None:
            return False
        return self.expires_at <= (time.time() if now is None else now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "state": str(self.state),
            "reason": self.reason,
            "detail": self.detail,
            "marked_at": self.marked_at,
            "expires_at": self.expires_at,
            "first_success_at": self.first_success_at,
            "consecutive_probe_failures": self.consecutive_probe_failures,
            "probe_attempts": self.probe_attempts,
            "last_probe_at": self.last_probe_at,
            "next_probe_at": self.next_probe_at,
            "source": str(self.source),
            "profile_hint": self.profile_hint,
            "mark_count": self.mark_count,
            "last_probe_error": self.last_probe_error,
        }


@dataclass(frozen=True)
class AttemptRecord:
    """一次真实 provider 调用的统计记录（不含任何敏感内容）。"""

    ts: float
    used: str
    kind: str = CallKind.TEXT_CHAT
    ok: bool = True
    umo: str | None = None
    profile_id: str | None = None
    requested: str = ""
    attempt_index: int = 1
    stream: bool = False
    first_chunk_ms: int | None = None
    total_ms: int = 0
    error_class: str | None = None
    error_type: str | None = None
    error_message: str | None = None
    http_status: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "umo": self.umo,
            "profile_id": self.profile_id,
            "requested": self.requested,
            "used": self.used,
            "attempt_index": self.attempt_index,
            "kind": str(self.kind),
            "stream": self.stream,
            "ok": self.ok,
            "first_chunk_ms": self.first_chunk_ms,
            "total_ms": self.total_ms,
            "error_class": self.error_class,
            "error_type": self.error_type,
            "error_message": self.error_message,
            "http_status": self.http_status,
        }


@dataclass(frozen=True)
class ProbeResult:
    """一次测活结果。"""

    provider_id: str
    ok: bool
    latency_ms: int = 0
    error_class: str | None = None
    error_message: str | None = None
    mode: str = "text_chat"
    timed_out: bool = False
    state_after: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "ok": self.ok,
            "latency_ms": self.latency_ms,
            "error_class": self.error_class,
            "error_message": self.error_message,
            "mode": self.mode,
            "timed_out": self.timed_out,
            "state_after": self.state_after,
        }


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("1", "true", "yes", "on"):
            return True
        if low in ("0", "false", "no", "off", ""):
            return False
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_str(value: Any, default: str = "") -> str:
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return default
    return str(value).strip()


def _as_str_list(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        text = value.strip()
        return (text,) if text else ()
    if isinstance(value, Sequence):
        result: list[str] = []
        for item in value:
            text = _as_str(item)
            if text:
                result.append(text)
        return tuple(result)
    return ()


@dataclass(frozen=True)
class GuardSettings:
    """插件配置的不可变快照。

    ``from_mapping()`` 对键名做与 ``_conf_schema.json`` 一致的读取，并对类型做宽松
    强制转换：配置被手工改坏时退回默认值，而不是让插件崩在启动期。
    """

    enable: bool = True
    enable_routing: bool = True
    enable_retry: bool = True
    enable_fallback: bool = True
    enable_marking: bool = True
    enable_probe: bool = True
    enable_monitoring: bool = True
    unmanaged_monitoring: bool = True
    respect_manual_selection: bool = True
    respect_session_selection: bool = True
    mark_scope: str = MARK_SCOPE_GLOBAL
    union_framework_fallbacks: bool = True
    allow_cross_provider_switch_mid_run: bool = False
    max_retries_per_model: int = 1
    retry_backoff_base: float = 0.5
    retry_backoff_factor: float = 2.0
    retry_backoff_max: float = 8.0
    retry_jitter_ratio: float = 0.2
    retry_total_timeout: float = 30.0
    retry_budget_per_umo_per_minute: int = 20
    retry_empty_output: bool = False
    mark_on_empty_output_after: int = 3
    mark_ttl_default: int = 0
    mark_ttl_rate_limit: int = 300
    mark_ttl_server_error: int = 300
    mark_ttl_network: int = 120
    double_confirm: bool = True
    confirm_delay_seconds: int = 60
    probe_interval_seconds: int = 120
    probe_timeout_seconds: float = 30.0
    probe_max_concurrency: int = 2
    probe_skip_when_busy: bool = True
    probe_tick_seconds: int = 5
    probe_on_startup: bool = True
    stats_window_seconds: int = 900
    stats_recent_events: int = 200
    stats_persist_interval_seconds: int = 30
    #: 统计保留期（天）：仍存在的 provider 永久保留，已删除的按此天数清理；0 = 永久保留。
    stats_retention_days: int = 7
    allow_webui_actions: bool = False
    log_level: str = "info"
    routes: tuple[RouteConfig, ...] = ()
    error_rules: tuple[Mapping[str, Any], ...] = ()
    disabled_profiles: frozenset[str] = frozenset()

    # ---- 派生值 ----

    def ttl_for(self, error_class: ErrorClass) -> int:
        """按分类取标记时长；0 表示不过期（规划文档 §9.3）。"""
        if error_class in (
            ErrorClass.RATE_LIMIT,
            ErrorClass.OVERLOADED,
            ErrorClass.SERVER_ERROR,
        ):
            if error_class is ErrorClass.RATE_LIMIT:
                return max(0, self.mark_ttl_rate_limit)
            return max(0, self.mark_ttl_server_error)
        if error_class in (ErrorClass.TIMEOUT, ErrorClass.NETWORK):
            return max(0, self.mark_ttl_network)
        return max(0, self.mark_ttl_default)

    def route_execution(self, route: RouteConfig | None) -> tuple[int, float, float]:
        """路由级覆盖：返回 (重试次数, 测活间隔, 测活超时)。"""
        retries = self.max_retries_per_model
        interval = float(self.probe_interval_seconds)
        timeout = self.probe_timeout_seconds
        if route is not None:
            if route.max_retries is not None:
                retries = route.max_retries
            if route.probe_interval is not None:
                interval = float(route.probe_interval)
            if route.probe_timeout is not None:
                timeout = float(route.probe_timeout)
        return max(0, retries), max(1.0, interval), max(1.0, timeout)

    def fingerprint(self) -> str:
        """配置指纹，用于缓存路由表与规则编译结果（规划文档 §7.2-5）。"""
        parts: list[str] = [
            str(int(self.enable)),
            str(int(self.enable_routing)),
            str(int(self.enable_retry)),
            str(int(self.enable_fallback)),
            str(int(self.enable_marking)),
            str(int(self.enable_probe)),
            str(int(self.enable_monitoring)),
            str(int(self.unmanaged_monitoring)),
            str(int(self.respect_manual_selection)),
            str(int(self.respect_session_selection)),
            self.mark_scope,
            str(int(self.union_framework_fallbacks)),
            str(int(self.allow_cross_provider_switch_mid_run)),
            str(self.max_retries_per_model),
            str(self.retry_backoff_base),
            str(self.retry_backoff_factor),
            str(self.retry_backoff_max),
            str(self.retry_jitter_ratio),
            str(self.retry_total_timeout),
            str(self.retry_budget_per_umo_per_minute),
            str(int(self.retry_empty_output)),
            str(self.mark_on_empty_output_after),
            str(self.mark_ttl_default),
            str(self.mark_ttl_rate_limit),
            str(self.mark_ttl_server_error),
            str(self.mark_ttl_network),
            str(int(self.double_confirm)),
            str(self.confirm_delay_seconds),
            str(self.probe_interval_seconds),
            str(self.probe_timeout_seconds),
            str(self.probe_max_concurrency),
            str(int(self.probe_skip_when_busy)),
            str(self.probe_tick_seconds),
            str(int(self.probe_on_startup)),
            str(self.stats_window_seconds),
            str(self.stats_recent_events),
            str(self.stats_persist_interval_seconds),
            str(self.stats_retention_days),
            str(int(self.allow_webui_actions)),
            self.log_level,
        ]
        for route in self.routes:
            parts.append(
                "|".join(
                    [
                        route.profile_id,
                        route.primary,
                        ",".join(route.fallbacks),
                        str(route.max_retries),
                        str(route.probe_interval),
                        str(route.probe_timeout),
                        str(int(route.enable)),
                        route.note,
                    ]
                )
            )
        for rule in self.error_rules:
            parts.append(
                "|".join(
                    [
                        _as_str(rule.get("id")),
                        str(int(_as_bool(rule.get("enabled"), True))),
                        str(_as_int(rule.get("priority"), 10)),
                        ",".join(str(v) for v in (rule.get("match_status") or [])),
                        _as_str(rule.get("match_exc_regex")),
                        _as_str(rule.get("match_msg_regex")),
                        _as_str(rule.get("action")),
                        str(_as_int(rule.get("mark_ttl_seconds"), 0)),
                    ]
                )
            )
        parts.append(",".join(sorted(self.disabled_profiles)))
        return "\x1f".join(parts)

    def to_public_dict(self) -> dict[str, Any]:
        """给 Web API / 命令用的只读摘要（不含路由细节）。"""
        return {
            "enable": self.enable,
            "enable_routing": self.enable_routing,
            "enable_retry": self.enable_retry,
            "enable_fallback": self.enable_fallback,
            "enable_marking": self.enable_marking,
            "enable_probe": self.enable_probe,
            "enable_monitoring": self.enable_monitoring,
            "mark_scope": self.mark_scope,
            "max_retries_per_model": self.max_retries_per_model,
            "probe_interval_seconds": self.probe_interval_seconds,
            "probe_timeout_seconds": self.probe_timeout_seconds,
            "double_confirm": self.double_confirm,
            "confirm_delay_seconds": self.confirm_delay_seconds,
            "mark_ttl_default": self.mark_ttl_default,
            "stats_retention_days": self.stats_retention_days,
            "allow_webui_actions": self.allow_webui_actions,
            "log_level": self.log_level,
            "route_count": len(self.routes),
            "error_rule_count": len(self.error_rules),
            "disabled_profiles": sorted(self.disabled_profiles),
        }

    @classmethod
    def from_mapping(cls, config: Mapping[str, Any] | None) -> "GuardSettings":
        """从插件配置（dict 或 AstrBotConfig）构造快照。"""
        data: Mapping[str, Any] = config or {}

        def get(key: str, default: Any) -> Any:
            try:
                value = data.get(key, default)
            except AttributeError:  # pragma: no cover - 防御性
                return default
            return default if value is None else value

        routes: list[RouteConfig] = []
        raw_routes = get("routes", []) or []
        if isinstance(raw_routes, Mapping):
            raw_routes = list(raw_routes.values())
        if isinstance(raw_routes, Iterable) and not isinstance(raw_routes, (str, bytes)):
            for item in raw_routes:
                if isinstance(item, Mapping):
                    route = route_from_mapping(item)
                    if route is not None:
                        routes.append(route)

        rules: list[Mapping[str, Any]] = []
        raw_rules = get("error_rules", []) or []
        if isinstance(raw_rules, Mapping):
            raw_rules = list(raw_rules.values())
        if isinstance(raw_rules, Iterable) and not isinstance(raw_rules, (str, bytes)):
            for item in raw_rules:
                if isinstance(item, Mapping):
                    rules.append(dict(item))

        return cls(
            enable=_as_bool(get("enable", True), True),
            enable_routing=_as_bool(get("enable_routing", True), True),
            enable_retry=_as_bool(get("enable_retry", True), True),
            enable_fallback=_as_bool(get("enable_fallback", True), True),
            enable_marking=_as_bool(get("enable_marking", True), True),
            enable_probe=_as_bool(get("enable_probe", True), True),
            enable_monitoring=_as_bool(get("enable_monitoring", True), True),
            unmanaged_monitoring=_as_bool(get("unmanaged_monitoring", True), True),
            respect_manual_selection=_as_bool(get("respect_manual_selection", True), True),
            respect_session_selection=_as_bool(get("respect_session_selection", True), True),
            mark_scope=_as_str(get("mark_scope", MARK_SCOPE_GLOBAL), MARK_SCOPE_GLOBAL)
            or MARK_SCOPE_GLOBAL,
            union_framework_fallbacks=_as_bool(get("union_framework_fallbacks", True), True),
            allow_cross_provider_switch_mid_run=_as_bool(
                get("allow_cross_provider_switch_mid_run", False), False
            ),
            max_retries_per_model=max(0, _as_int(get("max_retries_per_model", 1), 1)),
            retry_backoff_base=max(0.0, _as_float(get("retry_backoff_base", 0.5), 0.5)),
            retry_backoff_factor=max(1.0, _as_float(get("retry_backoff_factor", 2.0), 2.0)),
            retry_backoff_max=max(0.0, _as_float(get("retry_backoff_max", 8.0), 8.0)),
            retry_jitter_ratio=min(1.0, max(0.0, _as_float(get("retry_jitter_ratio", 0.2), 0.2))),
            retry_total_timeout=max(0.0, _as_float(get("retry_total_timeout", 30.0), 30.0)),
            retry_budget_per_umo_per_minute=max(
                0, _as_int(get("retry_budget_per_umo_per_minute", 20), 20)
            ),
            retry_empty_output=_as_bool(get("retry_empty_output", False), False),
            mark_on_empty_output_after=max(0, _as_int(get("mark_on_empty_output_after", 3), 3)),
            mark_ttl_default=max(0, _as_int(get("mark_ttl_default", 0), 0)),
            mark_ttl_rate_limit=max(0, _as_int(get("mark_ttl_rate_limit", 300), 300)),
            mark_ttl_server_error=max(0, _as_int(get("mark_ttl_server_error", 300), 300)),
            mark_ttl_network=max(0, _as_int(get("mark_ttl_network", 120), 120)),
            double_confirm=_as_bool(get("double_confirm", True), True),
            confirm_delay_seconds=max(0, _as_int(get("confirm_delay_seconds", 60), 60)),
            probe_interval_seconds=max(1, _as_int(get("probe_interval_seconds", 120), 120)),
            probe_timeout_seconds=max(1.0, _as_float(get("probe_timeout_seconds", 30.0), 30.0)),
            probe_max_concurrency=max(1, _as_int(get("probe_max_concurrency", 2), 2)),
            probe_skip_when_busy=_as_bool(get("probe_skip_when_busy", True), True),
            probe_tick_seconds=max(1, _as_int(get("probe_tick_seconds", 5), 5)),
            probe_on_startup=_as_bool(get("probe_on_startup", True), True),
            stats_window_seconds=max(0, _as_int(get("stats_window_seconds", 900), 900)),
            stats_recent_events=max(0, _as_int(get("stats_recent_events", 200), 200)),
            stats_persist_interval_seconds=max(
                1, _as_int(get("stats_persist_interval_seconds", 30), 30)
            ),
            stats_retention_days=max(0, _as_int(get("stats_retention_days", 7), 7)),
            allow_webui_actions=_as_bool(get("allow_webui_actions", False), False),
            log_level=_as_str(get("log_level", "info"), "info") or "info",
            routes=tuple(routes),
            error_rules=tuple(rules),
            disabled_profiles=frozenset(_as_str_list(get("disabled_profiles", []))),
        )


def route_from_mapping(item: Mapping[str, Any]) -> RouteConfig | None:
    """把 ``routes``/``default_route`` 里的一条配置转成 :class:`RouteConfig`。

    ``-1`` 表示继承全局设置；``profile_id`` 为 ``*``/空视为兜底路由。
    """
    if not isinstance(item, Mapping):
        return None

    raw_profile = _as_str(item.get("profile_id"))
    profile_id = raw_profile if raw_profile else WILDCARD_PROFILE
    primary = _as_str(item.get("primary"))
    fallbacks = tuple(
        fid for fid in _as_str_list(item.get("fallbacks", ())) if fid and fid != primary
    )

    def _optional_int(key: str) -> int | None:
        value = item.get(key, -1)
        number = _as_int(value, -1)
        return None if number < 0 else number

    def _optional_float(key: str) -> float | None:
        value = item.get(key, -1)
        number = _as_float(value, -1.0)
        return None if number < 0 else number

    return RouteConfig(
        profile_id=profile_id,
        primary=primary,
        fallbacks=fallbacks,
        max_retries=_optional_int("max_retries"),
        probe_interval=_optional_int("probe_interval_seconds"),
        probe_timeout=_optional_float("probe_timeout_seconds"),
        enable=_as_bool(item.get("enable", True), True),
        note=_as_str(item.get("note")),
    )


@dataclass
class MarkBlock:
    """一条标记的完整状态（可变，由 Guardian 在锁内更新）。"""

    provider_id: str
    state: MarkState = MarkState.MARKED
    reason: str = ""
    detail: str = ""
    marked_at: float = 0.0
    expires_at: float | None = None
    first_success_at: float | None = None
    consecutive_probe_failures: int = 0
    probe_attempts: int = 0
    last_probe_at: float | None = None
    next_probe_at: float | None = None
    source: str = MarkSource.AUTO
    profile_hint: str = ""
    mark_count: int = 1
    last_probe_error: str = ""

    def view(self) -> MarkView:
        return MarkView(
            provider_id=self.provider_id,
            state=self.state,
            reason=self.reason,
            detail=self.detail,
            marked_at=self.marked_at,
            expires_at=self.expires_at,
            first_success_at=self.first_success_at,
            consecutive_probe_failures=self.consecutive_probe_failures,
            probe_attempts=self.probe_attempts,
            last_probe_at=self.last_probe_at,
            next_probe_at=self.next_probe_at,
            source=self.source,
            profile_hint=self.profile_hint,
            mark_count=self.mark_count,
            last_probe_error=self.last_probe_error,
        )

    def to_dict(self) -> dict[str, Any]:
        return self.view().to_dict()

    @classmethod
    def from_dict(cls, provider_id: str, data: Mapping[str, Any]) -> "MarkBlock":
        """从状态文件恢复；字段缺失用默认值补（规划文档 §8.3）。"""
        state_raw = _as_str(data.get("state"), str(MarkState.MARKED))
        try:
            state = MarkState(state_raw)
        except ValueError:
            state = MarkState.MARKED
        expires = data.get("expires_at")
        next_probe = data.get("next_probe_at")
        first_success = data.get("first_success_at")
        last_probe = data.get("last_probe_at")
        return cls(
            provider_id=provider_id,
            state=state,
            reason=_as_str(data.get("reason")),
            detail=_as_str(data.get("detail")),
            marked_at=_as_float(data.get("marked_at"), 0.0),
            expires_at=_as_float(expires, 0.0) if expires not in (None, "") else None,
            first_success_at=_as_float(first_success, 0.0) if first_success not in (None, "") else None,
            consecutive_probe_failures=max(0, _as_int(data.get("consecutive_probe_failures"), 0)),
            probe_attempts=max(0, _as_int(data.get("probe_attempts"), 0)),
            last_probe_at=_as_float(last_probe, 0.0) if last_probe not in (None, "") else None,
            next_probe_at=_as_float(next_probe, 0.0) if next_probe not in (None, "") else None,
            source=_as_str(data.get("source"), MarkSource.AUTO) or MarkSource.AUTO,
            profile_hint=_as_str(data.get("profile_hint")),
            mark_count=max(1, _as_int(data.get("mark_count"), 1)),
            last_probe_error=_as_str(data.get("last_probe_error")),
        )


@dataclass
class CallContext:
    """一次对话请求的包装层上下文（存于 ``contextvars``）。

    ``attempted`` 用于与框架自身的 fallback 去重：框架再次进入包装层时，
    已经尝试过的 provider id 会被跳过，从而不会对同一个坏模型重复发请求。
    """

    plan: ChainPlan
    umo: str | None = None
    profile_id: str | None = None
    requested: str = ""
    route: RouteConfig | None = None
    created_at: float = field(default_factory=time.time)
    attempted: set[str] = field(default_factory=set)

    def is_fresh(self, ttl: float = 180.0, now: float | None = None) -> bool:
        current = time.time() if now is None else now
        return (current - self.created_at) <= ttl

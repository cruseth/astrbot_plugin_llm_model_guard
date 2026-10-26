"""纯数据模型：核心枚举、配置快照与运行时视图。

v0.2.0 的设计要点（任务包 12）
----------------------------

配置被压缩到 **8 个全局键**，其中**没有任何 provider id / 配置文件 id**：

* 插件不再要求用户描述每个配置文件的模型顺序 —— 链路直接读框架
  （``core/chain.py``）；
* 因此也不需要"下拉选择配置文件"：那不是没实现，而是不再需要。

本模块只依赖 Python 标准库，**不导入 AstrBot**，也不做任何 I/O；
其余 ``core`` 模块都只依赖本模块，依赖方向严格单向。
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

#: 验证使用的提示词，与框架 ``Provider.test()`` 完全一致。
PROBE_PROMPT = "REPLY `PONG` ONLY"

#: 默认可重试的错误类别（``retry_categories`` 的默认值）。
#:
#: ``overloaded``（上游瞬时过载）默认**开启**：它通常是稍后就会恢复的瞬时错误，
#: 不重试就直接标记属于过度惩罚。
#: ``empty_output`` 默认**不开启**：框架自身对空输出已有 tenacity 重试，
#: 插件再叠加会放大请求量。
DEFAULT_RETRY_CATEGORIES: tuple[str, ...] = (
    "timeout",
    "rate_limit",
    "server_error",
    "network",
    "overloaded",
)

#: 全部合法的 ``retry_categories`` 取值。
VALID_RETRY_CATEGORIES: frozenset[str] = frozenset(
    {
        "timeout",
        "rate_limit",
        "server_error",
        "network",
        "overloaded",
        "empty_output",
    }
)

#: 重试退避的等待上限（秒）。
MAX_RETRY_BACKOFF_SECONDS = 10.0


def log_allowed(configured: str, level: str) -> bool:
    """保留旧接口：v0.2.0 已移除 ``log_level`` 配置，恒为 True。

    旧版本插件曾有 ``log_level`` 键；v0.2.0 不再提供该配置项，
    但保留这个函数以便旧调用点平滑过渡。
    """
    return True


class ErrorClass(StrEnum):
    """错误分类枚举（v0.2.0 保留内置分类表，删除用户自定义规则）。"""

    AUTH = "auth"
    MODEL_NOT_FOUND = "model_not_found"
    BAD_REQUEST = "bad_request"
    #: 400/422 但消息没有命中任何细化规则：允许降级，但**不标记**
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
    """对一次失败的处置动作。"""

    PASSTHROUGH = "passthrough"
    RETRY_SAME = "retry_same"
    RETRY_THEN_FALLBACK = "retry_then_fallback"
    FALLBACK = "fallback"
    MARK_AND_FALLBACK = "mark_and_fallback"
    SURFACE = "surface"


class MarkState(StrEnum):
    """标记状态。

    v0.2.0 去掉了 ``CONFIRMING`` 双击确认态：恢复条件改为
    「连续成功 N 次」（``probe_success_threshold``），由计数而非状态机表达。
    """

    MARKED = "marked"


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
    """一次错误分类的结果，必须可解释（规则名 + 脱敏摘要）。

    v0.2.0：**没有 TTL**，所以没有 ``mark_ttl``；``mark_delay`` 固定为 1
    （「连续 N 次才标记」随 v0.1.x 的 ``mark_on_empty_output_after`` 一起移除）。
    """

    error_class: ErrorClass
    action: Action
    should_mark: bool
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
            "mark_delay": self.mark_delay,
            "retry_after": self.retry_after,
            "matched_rule": self.matched_rule,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ChainPlan:
    """一次调用的候选链计划（由 :mod:`core.chain` 构建）。"""

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
    #: 连续验证成功次数；达到 ``probe_success_threshold`` 即解除标记。
    consecutive_success: int = 0
    probe_attempts: int = 0
    last_probe_at: float | None = None
    next_probe_at: float | None = None
    source: MarkSource = MarkSource.AUTO
    mark_count: int = 1
    last_probe_error: str = ""

    @property
    def is_pending_confirm(self) -> bool:
        """兼容旧字段：v0.2.0 用连续成功计数表达，恒为 False。"""
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "state": str(self.state),
            "reason": self.reason,
            "detail": self.detail,
            "marked_at": self.marked_at,
            "consecutive_success": self.consecutive_success,
            "probe_attempts": self.probe_attempts,
            "last_probe_at": self.last_probe_at,
            "next_probe_at": self.next_probe_at,
            "source": str(self.source),
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
    """一次验证的结果。"""

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
    """插件配置的不可变快照（v0.2.0：8 个全局键）。

    **旧配置里的未知键被静默忽略** —— ``from_mapping`` 只按下面的键名取值，
    v0.1.x 的 ``routes`` / ``profiles`` / ``mark_scope`` / ``enable_*`` 等键
    一律不看、不报错。
    """

    enable: bool = True
    request_timeout_seconds: int = 120
    max_retries: int = 1
    retry_backoff_seconds: float = 1.5
    retry_categories: tuple[str, ...] = DEFAULT_RETRY_CATEGORIES
    probe_interval_seconds: int = 60
    probe_timeout_seconds: int = 30
    probe_success_threshold: int = 2
    #: 页面写操作总开关（schema 的第 9 个键）。
    #:
    #: 默认**关闭**：AstrBot 面板的登录态是**全局**的 —— 默认开启意味着任何
    #: 能登录 Dashboard 的人都能改全局标记状态，相对 v0.1.x 扩大了误操作面。
    #: 需要时在插件配置里显式开启即可（该键在 schema 里，用户看得见）。
    allow_webui_actions: bool = False

    # ---- 派生值 ----

    def is_retryable(self, error_class: Any) -> bool:
        """该错误类别是否属于「可重试」。

        对应流程图里「可重试报错 → 重试」与「不重试报错 → 直接回退」的分叉。
        """
        name = str(error_class)
        return name in self.retry_categories

    def retry_delay(self, attempt: int) -> float:
        """第 ``attempt`` 次重试前的等待秒数（指数退避，上限 10 秒）。

        ``attempt`` 从 1 开始：第一次重试等待 ``base``，之后翻倍。
        """
        base = max(0.0, float(self.retry_backoff_seconds))
        exponent = max(0, int(attempt) - 1)
        return min(base * (2.0**exponent), MAX_RETRY_BACKOFF_SECONDS)

    def request_timeout(self) -> float | None:
        """单次调用超时秒数；``0`` 表示不限制（返回 ``None``）。"""
        if self.request_timeout_seconds <= 0:
            return None
        return float(self.request_timeout_seconds)

    def fingerprint(self) -> str:
        """配置指纹，用于缓存路由表与规则编译结果。"""
        return "\x1f".join(
            [
                str(int(self.enable)),
                str(self.request_timeout_seconds),
                str(self.max_retries),
                f"{self.retry_backoff_seconds:.4f}",
                ",".join(self.retry_categories),
                str(self.probe_interval_seconds),
                str(self.probe_timeout_seconds),
                str(self.probe_success_threshold),
                str(int(self.allow_webui_actions)),
            ]
        )

    def to_public_dict(self) -> dict[str, Any]:
        """给 Web API / 命令用的只读摘要。"""
        return {
            "enable": self.enable,
            "request_timeout_seconds": self.request_timeout_seconds,
            "max_retries": self.max_retries,
            "retry_backoff_seconds": self.retry_backoff_seconds,
            "retry_categories": list(self.retry_categories),
            "probe_interval_seconds": self.probe_interval_seconds,
            "probe_timeout_seconds": self.probe_timeout_seconds,
            "probe_success_threshold": self.probe_success_threshold,
            "allow_webui_actions": self.allow_webui_actions,
        }

    @classmethod
    def from_mapping(cls, config: Mapping[str, Any] | None) -> "GuardSettings":
        """从插件配置构造快照。

        **只读取 8 个新键**，其余键（旧版本遗留）一律静默忽略。
        非法值回退默认并由调用方告警一次。
        """
        data: Mapping[str, Any] = config or {}

        def get(key: str, default: Any) -> Any:
            try:
                value = data.get(key, default)
            except AttributeError:  # pragma: no cover - 防御性
                return default
            return default if value is None else value

        raw_categories = _as_str_list(get("retry_categories", DEFAULT_RETRY_CATEGORIES))
        categories = tuple(
            item for item in raw_categories if item in VALID_RETRY_CATEGORIES
        )
        if not categories:
            categories = DEFAULT_RETRY_CATEGORIES

        return cls(
            enable=_as_bool(get("enable", True), True),
            request_timeout_seconds=max(0, _as_int(get("request_timeout_seconds", 120), 120)),
            max_retries=max(0, _as_int(get("max_retries", 1), 1)),
            retry_backoff_seconds=max(
                0.0, _as_float(get("retry_backoff_seconds", 1.5), 1.5)
            ),
            retry_categories=categories,
            probe_interval_seconds=max(1, _as_int(get("probe_interval_seconds", 60), 60)),
            probe_timeout_seconds=max(1, _as_int(get("probe_timeout_seconds", 30), 30)),
            probe_success_threshold=max(
                1, _as_int(get("probe_success_threshold", 2), 2)
            ),
            allow_webui_actions=_as_bool(get("allow_webui_actions", False), False),
        )


@dataclass
class MarkBlock:
    """一条标记的完整状态（可变，由 Guardian 在锁内更新）。"""

    provider_id: str
    state: MarkState = MarkState.MARKED
    reason: str = ""
    detail: str = ""
    marked_at: float = 0.0
    #: 连续验证成功次数（v0.2.0 的恢复判据）。
    consecutive_success: int = 0
    probe_attempts: int = 0
    last_probe_at: float | None = None
    next_probe_at: float | None = None
    source: MarkSource = MarkSource.AUTO
    mark_count: int = 1
    last_probe_error: str = ""

    def view(self) -> MarkView:
        return MarkView(
            provider_id=self.provider_id,
            state=self.state,
            reason=self.reason,
            detail=self.detail,
            marked_at=self.marked_at,
            consecutive_success=self.consecutive_success,
            probe_attempts=self.probe_attempts,
            last_probe_at=self.last_probe_at,
            next_probe_at=self.next_probe_at,
            source=self.source,
            mark_count=self.mark_count,
            last_probe_error=self.last_probe_error,
        )

    def to_dict(self) -> dict[str, Any]:
        return self.view().to_dict()

    @classmethod
    def from_dict(cls, provider_id: str, data: Mapping[str, Any]) -> "MarkBlock":
        """从状态文件恢复；字段缺失用默认值补。"""
        state_raw = _as_str(data.get("state"), str(MarkState.MARKED))
        try:
            state = MarkState(state_raw)
        except ValueError:
            state = MarkState.MARKED
        next_probe = data.get("next_probe_at")
        last_probe = data.get("last_probe_at")
        return cls(
            provider_id=provider_id,
            state=state,
            reason=_as_str(data.get("reason")),
            detail=_as_str(data.get("detail")),
            marked_at=_as_float(data.get("marked_at"), 0.0),
            consecutive_success=max(
                0, _as_int(data.get("consecutive_success"), 0)
            ),
            probe_attempts=max(0, _as_int(data.get("probe_attempts"), 0)),
            last_probe_at=(
                _as_float(last_probe, 0.0) if last_probe not in (None, "") else None
            ),
            next_probe_at=(
                _as_float(next_probe, 0.0) if next_probe not in (None, "") else None
            ),
            source=_as_str(data.get("source"), str(MarkSource.AUTO))
            or str(MarkSource.AUTO),
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
    route: Any | None = None
    created_at: float = field(default_factory=time.time)
    attempted: set[str] = field(default_factory=set)

    def is_fresh(self, ttl: float = 180.0, now: float | None = None) -> bool:
        current = time.time() if now is None else now
        return (current - self.created_at) <= ttl

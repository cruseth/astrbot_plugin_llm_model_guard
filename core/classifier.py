"""错误分类器：把异常对象映射到「错误类别 + 处置动作」。

规则来源（规划文档 §9）：

1. 信号提取：HTTP 状态码、异常类名集合、异常消息、``Retry-After``；
2. 内置规则表：优先级升序匹配，命中即停，优先级数字越小越先匹配；
3. 用户自定义规则：``priority < 100`` 插到内置规则之前；非法规则只记 warning；
4. 处置动作再按配置开关与运行上下文（流式已产出、工具循环后续轮）叠加修正。

本模块不导入任何 LLM SDK，也**不**依赖 AstrBot，只用标准库（规划文档 §9.1）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Sequence

from .models import (
    Action,
    Classification,
    ErrorClass,
    GuardSettings,
    LOG_PREFIX,
)

#: 落盘摘要的最大长度（规划文档 §13.8）。
DETAIL_LIMIT = 200

#: 最近错误记录的摘要长度上限。
MESSAGE_LIMIT = 500

#: 内置规则的优先级基准；小于它的用户规则先于内置规则匹配。
BUILTIN_PRIORITY_BASE = 100

#: 本地编程错误的规则名（P2-8）。
LOCAL_FATAL_RULE = "builtin:local-fatal"

_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\b(sk-[A-Za-z0-9_\-]{6,})"),
    re.compile(r"(?i)\b(api[_-]?key|apikey|access[_-]?token|secret|password|authorization)\b\s*[:=]?\s*\S+"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"\b[A-Za-z0-9_\-]{40,}\b"),
)
_REDACTED = "***"


def redact(text: str) -> str:
    """把疑似密钥的片段替换为 ``***``（规划文档 §3.3-4）。"""
    cleaned = text
    for pattern, replacement in (
        (_SECRET_PATTERNS[0], "sk-***"),
        (_SECRET_PATTERNS[1], r"\1=***"),
        (_SECRET_PATTERNS[2], "Bearer ***"),
        (_SECRET_PATTERNS[3], _REDACTED),
    ):
        cleaned = pattern.sub(replacement, cleaned)
    return cleaned


def summarize_error(text: str, *, limit: int = DETAIL_LIMIT) -> str:
    """脱敏 + 压缩空白 + 截断，用于落盘与展示。"""
    if not text:
        return ""
    collapsed = " ".join(str(text).split())
    redacted = redact(collapsed)
    if len(redacted) <= limit:
        return redacted
    return redacted[: max(0, limit - 1)] + "…"


# ---------------------------------------------------------------------------
# 信号提取
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Signals:
    """一次分类所需的全部判据。"""

    status: int | None
    names: frozenset[str]
    module: str
    message: str
    retry_after: float | None
    is_err_response: bool = False

    @property
    def lower_message(self) -> str:
        return self.message.lower()


def _coerce_status(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return int(text)
    return None


def _first_status(exc: BaseException) -> int | None:
    for candidate in (
        getattr(exc, "status_code", None),
        getattr(exc, "status", None),
        getattr(getattr(exc, "response", None), "status_code", None),
        getattr(exc, "code", None),
    ):
        status = _coerce_status(candidate)
        if status is not None:
            return status
    return None


def _parse_retry_after(headers: Any) -> float | None:
    if headers is None:
        return None
    raw: Any = None
    try:
        if hasattr(headers, "get"):
            raw = headers.get("retry-after") or headers.get("Retry-After")
    except Exception:  # pragma: no cover - 第三方 header 容错
        raw = None
    if raw is None:
        return None
    try:
        seconds = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if seconds < 0:
        return None
    return seconds


def _is_err_response(obj: Any) -> bool:
    return getattr(obj, "role", None) == "err"


def _response_text(obj: Any) -> str:
    for attr in ("completion_text", "message", "result"):
        value = getattr(obj, attr, None)
        if isinstance(value, str) and value:
            return value
    return ""


_INNER_ERROR_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*(?:Error|Exception|Timeout))\s*:")


def _inner_error_names(message: str) -> set[str]:
    """从框架合成的 "All chat models failed: XxxError: ..." 里抽取内层类型名。"""
    names = {match.group(1) for match in _INNER_ERROR_RE.finditer(message)}
    return names


def extract_signals(exc: Any) -> Signals:
    """从异常或 ``LLMResponse(role="err")`` 提取判据（规划文档 §9.1）。"""
    if exc is None:
        return Signals(None, frozenset(), "", "", None)

    if not isinstance(exc, BaseException):
        message = _response_text(exc)
        names = {"LLMResponse"} | _inner_error_names(message)
        return Signals(
            None,
            frozenset(names),
            type(exc).__module__,
            message,
            None,
            is_err_response=_is_err_response(exc),
        )

    names = {klass.__name__ for klass in type(exc).__mro__}
    # 沿 __cause__ 链收集类型名：框架/SDK 常把底层 httpx 异常包一层再抛出
    # （规划文档 §9.2 network 行要求复刻框架 is_connection_error 的递归语义）。
    cause = exc.__cause__
    depth = 0
    while cause is not None and depth < 4:
        names.update(klass.__name__ for klass in type(cause).__mro__)
        cause = cause.__cause__
        depth += 1
    message = str(exc)
    status = _first_status(exc)
    retry_after = _parse_retry_after(getattr(getattr(exc, "response", None), "headers", None))

    # 框架合成的 "All chat models failed: XxxError: ..." 会把内层类型名写进消息里。
    if _is_err_response(exc) or "all chat models failed" in message.lower():
        names |= _inner_error_names(message)

    return Signals(
        status=status,
        names=frozenset(names),
        module=type(exc).__module__,
        message=message,
        retry_after=retry_after,
        is_err_response=_is_err_response(exc),
    )


# ---------------------------------------------------------------------------
# 内置规则表（规划文档 §9.2）
# ---------------------------------------------------------------------------

_QUOTA_RE = re.compile(
    r"(insufficient[_ ]quota|exceeded your current quota|billing|quota exceeded|credit balance|余额不足|配额|欠费)"
)
_CONTENT_FILTER_RE = re.compile(
    r"(content management policy|content[_ ]policy|content filter|safety system|safety filter|flagged|blocked by|审核|敏感词)"
)
_CONTEXT_TOO_LONG_RE = re.compile(
    r"(maximum context length|context[_ ]length_exceeded|too many tokens|prompt is too long|reduce the length of the messages|上下文(长度|过长))"
)
_AUTH_RE = re.compile(
    r"(invalid api key|incorrect api key|invalid_api_key|unauthorized|authentication failed|api key.*(invalid|expired)|令牌.*(无效|过期))"
)
_MODEL_NOT_FOUND_RE = re.compile(
    r"(model.*(not[ _-]*found|not[ _-]*exist|does[ _-]*not[ _-]*exist|not[ _-]*supported|unsupported)|no such model|invalid model|unknown model|模型.*(不存在|未找到))"
)
_BAD_REQUEST_DETAIL_RE = re.compile(
    r"(model.*(not[ _-]*found|not[ _-]*exist|does[ _-]*not[ _-]*exist|invalid|unsupported|unknown)"
    r"|context[_ ]length_exceeded|maximum context length|too many tokens"
    r"|not a vlm|does not support|unsupported (parameter|model)"
    r"|invalid_request_error|is not supported|参数.*(错误|不支持))"
)
_OVERLOADED_RE = re.compile(
    r"(overloaded|server is busy|temporarily unavailable|service unavailable)"
)
_RATE_LIMIT_RE = re.compile(
    r"(rate limit|too many requests|requests per (minute|day)|tokens per (minute|day)|tpm|rpm|频率.{0,4}限制)"
)
_SERVER_ERROR_RE = re.compile(
    r"(internal server error|bad gateway|gateway timeout|server error|upstream error)"
)
_TIMEOUT_RE = re.compile(r"(timed out|timeout|超时)")
_NETWORK_RE = re.compile(
    r"(connection (error|reset|refused|aborted)|cannot connect|eof occurred|ssl|proxy error|network (error|unreachable)|网络.*(错误|不可达))"
)
_EMPTY_OUTPUT_RE = re.compile(r"(empty (model )?output|no choices|no usable assistant output|空输出)")

_AUTH_NAMES = frozenset(
    {
        "AuthenticationError",
        "PermissionDeniedError",
        "AuthenticationException",
        "UnauthorizedError",
    }
)
_MODEL_NOT_FOUND_NAMES = frozenset({"NotFoundError", "ModelNotFoundError"})
_BAD_REQUEST_NAMES = frozenset(
    {"BadRequestError", "UnprocessableEntityError", "InvalidRequestError"}
)
_OVERLOADED_NAMES = frozenset({"OverloadedError", "ServiceUnavailableError"})
_RATE_LIMIT_NAMES = frozenset({"RateLimitError", "TooManyRequestsError"})
_SERVER_ERROR_NAMES = frozenset({"InternalServerError", "APIError", "ServerError"})
_TIMEOUT_NAMES = frozenset(
    {
        "APITimeoutError",
        "TimeoutException",
        "Timeout",
        "TimeoutError",
        "ReadTimeout",
        "WriteTimeout",
        "PoolTimeout",
    }
)
_NETWORK_NAMES = frozenset(
    {
        "ConnectError",
        "ConnectionError",
        "ConnectTimeout",
        "NetworkError",
        "RemoteProtocolError",
        "ReadError",
        "WriteError",
        "ProxyError",
        "SSLError",
    }
)
_EMPTY_OUTPUT_NAMES = frozenset({"EmptyModelOutputError"})


def _match_quota(sig: Signals) -> bool:
    if sig.status not in (402, 429, 403):
        return False
    return bool(_QUOTA_RE.search(sig.lower_message)) or bool(
        sig.names & {"InsufficientQuotaError", "BillingError"}
    )


def _match_content_filter(sig: Signals) -> bool:
    return bool(_CONTENT_FILTER_RE.search(sig.lower_message))


def _match_context_too_long(sig: Signals) -> bool:
    return bool(_CONTEXT_TOO_LONG_RE.search(sig.lower_message)) or bool(
        sig.names & {"ContextLengthExceededError"}
    )


def _match_auth(sig: Signals) -> bool:
    return (
        sig.status in (401, 403)
        or bool(sig.names & _AUTH_NAMES)
        or bool(_AUTH_RE.search(sig.lower_message))
    )


def _match_model_not_found(sig: Signals) -> bool:
    return (
        sig.status == 404
        or bool(sig.names & _MODEL_NOT_FOUND_NAMES)
        or bool(_MODEL_NOT_FOUND_RE.search(sig.lower_message))
    )


def _match_bad_request(sig: Signals) -> bool:
    """400/422 且消息命中细化特征：确实是模型/参数不兼容，值得标记（P2-6）。"""
    if sig.status in (400, 422):
        return bool(_BAD_REQUEST_DETAIL_RE.search(sig.lower_message))
    if bool(sig.names & _BAD_REQUEST_NAMES):
        return bool(_BAD_REQUEST_DETAIL_RE.search(sig.lower_message))
    return False


def _match_bad_request_generic(sig: Signals) -> bool:
    """其余 400/422：允许降级但不标记，避免把插件自身的 payload 问题算到模型头上。"""
    return sig.status in (400, 422) or bool(sig.names & _BAD_REQUEST_NAMES)


def _match_retryable_status(sig: Signals) -> bool:
    """408/409/425 属于"暂时性失败"，按超时语义重试（P2-7）。"""
    return sig.status in (408, 409, 425)


def _match_overloaded(sig: Signals) -> bool:
    return (
        sig.status in (503, 529)
        or bool(sig.names & _OVERLOADED_NAMES)
        or bool(_OVERLOADED_RE.search(sig.lower_message))
    )


def _match_rate_limit(sig: Signals) -> bool:
    return (
        sig.status == 429
        or bool(sig.names & _RATE_LIMIT_NAMES)
        or bool(_RATE_LIMIT_RE.search(sig.lower_message))
    )


def _match_server_error(sig: Signals) -> bool:
    if sig.status is not None and 500 <= sig.status < 600:
        return True
    if sig.names & _SERVER_ERROR_NAMES and (
        sig.status is None or 500 <= sig.status < 600
    ):
        if bool(_SERVER_ERROR_RE.search(sig.lower_message)):
            return True
        if sig.status is not None:
            return True
    return bool(_SERVER_ERROR_RE.search(sig.lower_message))


def _match_timeout(sig: Signals) -> bool:
    if sig.names & _TIMEOUT_NAMES:
        return True
    return bool(_TIMEOUT_RE.search(sig.lower_message))


def _match_network(sig: Signals) -> bool:
    if sig.names & _NETWORK_NAMES:
        return True
    return bool(_NETWORK_RE.search(sig.lower_message))


def _match_empty_output(sig: Signals) -> bool:
    if sig.names & _EMPTY_OUTPUT_NAMES:
        return True
    return bool(_EMPTY_OUTPUT_RE.search(sig.lower_message))


@dataclass(frozen=True)
class _BuiltinRule:
    priority: int
    name: str
    error_class: ErrorClass
    matcher: Callable[[Signals], bool]


#: 优先级顺序敏感：细化规则必须排在粗粒度状态码规则之前（规划文档 §9.2）。
_BUILTIN_RULES: tuple[_BuiltinRule, ...] = (
    _BuiltinRule(10, "builtin:quota_exhausted", ErrorClass.QUOTA_EXHAUSTED, _match_quota),
    _BuiltinRule(12, "builtin:content_filter", ErrorClass.CONTENT_FILTER, _match_content_filter),
    _BuiltinRule(15, "builtin:context_too_long", ErrorClass.CONTEXT_TOO_LONG, _match_context_too_long),
    _BuiltinRule(20, "builtin:auth", ErrorClass.AUTH, _match_auth),
    _BuiltinRule(22, "builtin:model_not_found", ErrorClass.MODEL_NOT_FOUND, _match_model_not_found),
    _BuiltinRule(25, "builtin:bad_request", ErrorClass.BAD_REQUEST, _match_bad_request),
    _BuiltinRule(
        27,
        "builtin:retryable_status",
        ErrorClass.TIMEOUT,
        _match_retryable_status,
    ),
    _BuiltinRule(
        28,
        "builtin:bad_request_generic",
        ErrorClass.BAD_REQUEST_SOFT,
        _match_bad_request_generic,
    ),
    _BuiltinRule(30, "builtin:overloaded", ErrorClass.OVERLOADED, _match_overloaded),
    _BuiltinRule(35, "builtin:rate_limit", ErrorClass.RATE_LIMIT, _match_rate_limit),
    _BuiltinRule(40, "builtin:server_error", ErrorClass.SERVER_ERROR, _match_server_error),
    _BuiltinRule(45, "builtin:timeout", ErrorClass.TIMEOUT, _match_timeout),
    _BuiltinRule(50, "builtin:network", ErrorClass.NETWORK, _match_network),
    _BuiltinRule(55, "builtin:empty_output", ErrorClass.EMPTY_OUTPUT, _match_empty_output),
)


# ---------------------------------------------------------------------------
# 处置策略（规划文档 §9.3）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _BasePolicy:
    retry: bool
    fallback: bool
    mark: bool


_POLICIES: dict[ErrorClass, _BasePolicy] = {
    ErrorClass.AUTH: _BasePolicy(retry=False, fallback=True, mark=True),
    ErrorClass.MODEL_NOT_FOUND: _BasePolicy(retry=False, fallback=True, mark=True),
    ErrorClass.BAD_REQUEST: _BasePolicy(retry=False, fallback=True, mark=True),
    ErrorClass.BAD_REQUEST_SOFT: _BasePolicy(retry=False, fallback=True, mark=False),
    ErrorClass.CONTEXT_TOO_LONG: _BasePolicy(retry=False, fallback=True, mark=False),
    ErrorClass.CONTENT_FILTER: _BasePolicy(retry=False, fallback=True, mark=False),
    ErrorClass.QUOTA_EXHAUSTED: _BasePolicy(retry=False, fallback=True, mark=True),
    ErrorClass.RATE_LIMIT: _BasePolicy(retry=True, fallback=True, mark=True),
    ErrorClass.OVERLOADED: _BasePolicy(retry=True, fallback=True, mark=True),
    ErrorClass.SERVER_ERROR: _BasePolicy(retry=True, fallback=True, mark=True),
    ErrorClass.TIMEOUT: _BasePolicy(retry=True, fallback=True, mark=True),
    ErrorClass.NETWORK: _BasePolicy(retry=True, fallback=True, mark=True),
    ErrorClass.EMPTY_OUTPUT: _BasePolicy(retry=True, fallback=True, mark=True),
    ErrorClass.UNKNOWN: _BasePolicy(retry=False, fallback=True, mark=False),
}

#: 用户规则动作名 → 内部动作。
_RULE_ACTIONS: dict[str, Action] = {
    "retry_same": Action.RETRY_SAME,
    "retry": Action.RETRY_SAME,
    "fallback": Action.FALLBACK,
    "mark": Action.MARK_AND_FALLBACK,
    "mark_and_fallback": Action.MARK_AND_FALLBACK,
    "surface": Action.SURFACE,
    "ignore": Action.PASSTHROUGH,
    "passthrough": Action.PASSTHROUGH,
}


@dataclass(frozen=True)
class _UserRule:
    rule_id: str
    priority: int
    statuses: frozenset[int]
    exc_regex: re.Pattern[str] | None
    msg_regex: re.Pattern[str] | None
    action: Action
    mark_ttl: int
    should_mark: bool

    def matches(self, sig: Signals) -> bool:
        if self.statuses and sig.status not in self.statuses:
            return False
        if self.exc_regex is not None and not any(
            self.exc_regex.search(name) for name in sig.names
        ):
            return False
        if self.msg_regex is not None and not self.msg_regex.search(sig.message):
            return False
        return True


class ErrorClassifier:
    """错误分类器：内置规则 + 用户规则，输出 :class:`Classification`。"""

    def __init__(
        self,
        user_rules: Sequence[Mapping[str, Any]] | None = None,
        *,
        settings: Callable[[], GuardSettings] | None = None,
        logger: Any = None,
    ) -> None:
        self._rules: tuple[_UserRule, ...] = ()
        self._settings = settings or GuardSettings
        self._logger = logger
        self._raw_rules: Sequence[Mapping[str, Any]] | None = None
        if user_rules:
            self.reload_rules(user_rules)

    # ---- 规则编译 ----

    @property
    def rule_count(self) -> int:
        return len(self._rules)

    def reload_rules(self, user_rules: Sequence[Mapping[str, Any]] | None) -> int:
        """编译用户规则，返回成功编译条数；非法规则只记日志、不抛异常。"""
        self._raw_rules = user_rules
        compiled: list[_UserRule] = []
        if user_rules:
            for index, raw in enumerate(user_rules):
                if not isinstance(raw, Mapping):
                    self._warn(f"忽略第 {index + 1} 条错误规则：不是映射结构")
                    continue
                rule = self._compile_rule(raw, index)
                if rule is not None:
                    compiled.append(rule)
        compiled.sort(key=lambda item: item.priority)
        self._rules = tuple(compiled)
        return len(self._rules)

    def _compile_rule(self, raw: Mapping[str, Any], index: int) -> _UserRule | None:
        rule_id = str(raw.get("id") or f"user-rule-{index + 1}").strip() or f"user-rule-{index + 1}"
        if not bool(raw.get("enabled", True)):
            return None

        try:
            priority = int(raw.get("priority", 10))
        except (TypeError, ValueError):
            self._warn(f"忽略错误规则 {rule_id}：priority 不是整数")
            return None

        statuses: set[int] = set()
        raw_statuses = raw.get("match_status") or []
        if isinstance(raw_statuses, (str, int)):
            raw_statuses = [raw_statuses]
        if isinstance(raw_statuses, Iterable) and not isinstance(raw_statuses, (str, bytes)):
            for item in raw_statuses:
                try:
                    statuses.add(int(item))
                except (TypeError, ValueError):
                    self._warn(f"忽略错误规则 {rule_id}：match_status 含非整数 {item!r}")
                    return None

        exc_regex = self._compile_regex(raw.get("match_exc_regex"), rule_id, "match_exc_regex")
        if exc_regex is False:
            return None
        msg_regex = self._compile_regex(raw.get("match_msg_regex"), rule_id, "match_msg_regex")
        if msg_regex is False:
            return None

        action_name = str(raw.get("action", "mark_and_fallback")).strip().lower()
        action = _RULE_ACTIONS.get(action_name)
        if action is None:
            self._warn(f"忽略错误规则 {rule_id}：未知 action {action_name!r}")
            return None

        try:
            mark_ttl = max(0, int(raw.get("mark_ttl_seconds", 0)))
        except (TypeError, ValueError):
            mark_ttl = 0

        return _UserRule(
            rule_id=rule_id,
            priority=priority,
            statuses=frozenset(statuses),
            exc_regex=exc_regex,
            msg_regex=msg_regex,
            action=action,
            mark_ttl=mark_ttl,
            should_mark=action in (Action.MARK_AND_FALLBACK, Action.FALLBACK)
            and action_name in ("mark", "mark_and_fallback"),
        )

    def _compile_regex(
        self, pattern: Any, rule_id: str, field_name: str
    ) -> re.Pattern[str] | None | bool:
        if pattern is None:
            return None
        text = str(pattern).strip()
        if not text:
            return None
        try:
            return re.compile(text, re.IGNORECASE)
        except re.error as exc:
            self._warn(f"忽略错误规则 {rule_id}：{field_name} 正则非法（{exc}）")
            return False

    def _warn(self, message: str) -> None:
        if self._logger is not None:
            self._logger.warning(f"{LOG_PREFIX} {message}")

    # ---- 分类 ----

    def classify(
        self,
        exc: Any,
        *,
        stream: bool = False,
        chunks_emitted: int = 0,
        is_continuation: bool = False,
    ) -> Classification:
        """把一次失败映射成处置动作；永不返回 ``None``，``unknown`` 兜底。"""
        settings = self._settings()
        sig = extract_signals(exc)
        error_class, builtin_rule = self._match_builtin(sig)
        user_rule = self._match_user(sig)
        local_fatal = self._is_local_fatal(exc, sig, error_class, user_rule)

        if local_fatal:
            # 本地编程错误（TypeError/ValueError 且无状态码、无规则命中）：直接上抛，
            # 不重试、不降级、不标记，避免把插件/框架的参数 bug 伪装成模型故障（P2-8）。
            action = Action.SURFACE
            should_mark = False
            mark_ttl = 0
            matched_rule = LOCAL_FATAL_RULE
        elif user_rule is not None:
            action = user_rule.action
            should_mark = user_rule.should_mark
            mark_ttl = user_rule.mark_ttl
            matched_rule = f"user:{user_rule.rule_id}"
        else:
            policy = _POLICIES.get(error_class, _POLICIES[ErrorClass.UNKNOWN])
            action = self._action_from_policy(policy)
            should_mark = policy.mark
            mark_ttl = settings.ttl_for(error_class)
            matched_rule = builtin_rule

        # 空输出按「连续 N 次」才标记（规划文档 §9.3 叠加规则）。
        mark_delay = 1
        if error_class is ErrorClass.EMPTY_OUTPUT:
            threshold = settings.mark_on_empty_output_after
            should_mark = threshold > 0
            mark_delay = max(1, threshold)

        # 流式已产出内容：不重试、不降级、**不标记**（任务包 02 §3.4 / 裁决 P2-5）。
        if stream and chunks_emitted > 0:
            action = Action.SURFACE
            should_mark = False

        # 工具循环后续轮：默认不跨 provider 切换。
        if (
            is_continuation
            and not settings.allow_cross_provider_switch_mid_run
            and action is not Action.PASSTHROUGH
        ):
            action = Action.RETRY_SAME if action in (
                Action.RETRY_SAME,
                Action.RETRY_THEN_FALLBACK,
                Action.FALLBACK,
                Action.MARK_AND_FALLBACK,
            ) else action

        # 配置开关叠加。
        if action is not Action.PASSTHROUGH:
            if local_fatal:
                # 本地编程错误不受 enable_retry/enable_fallback 影响：永远上抛。
                action = Action.SURFACE
                should_mark = False
            if not settings.enable_retry and action in (
                Action.RETRY_SAME,
                Action.RETRY_THEN_FALLBACK,
            ):
                action = (
                    Action.FALLBACK
                    if action is Action.RETRY_THEN_FALLBACK
                    else Action.SURFACE
                )
            if not settings.enable_fallback and action in (
                Action.FALLBACK,
                Action.RETRY_THEN_FALLBACK,
                Action.MARK_AND_FALLBACK,
            ):
                action = Action.SURFACE
            if not settings.enable_marking:
                should_mark = False
            if not settings.retry_empty_output and error_class is ErrorClass.EMPTY_OUTPUT:
                if action in (Action.RETRY_SAME, Action.RETRY_THEN_FALLBACK):
                    action = Action.FALLBACK

        if not should_mark:
            mark_ttl = 0

        return Classification(
            error_class=error_class,
            action=action,
            should_mark=should_mark,
            mark_ttl=mark_ttl,
            mark_delay=mark_delay,
            retry_after=sig.retry_after,
            matched_rule=matched_rule,
            detail=summarize_error(sig.message),
        )

    @staticmethod
    def _is_local_fatal(
        exc: Any,
        sig: Signals,
        error_class: ErrorClass,
        user_rule: _UserRule | None,
    ) -> bool:
        """``TypeError`` / ``ValueError`` 且无状态码、无规则命中 → fatal（P2-8）。"""
        if user_rule is not None:
            return False
        if sig.status is not None:
            return False
        if error_class is not ErrorClass.UNKNOWN:
            return False
        return isinstance(exc, (TypeError, ValueError))

    def _match_builtin(self, sig: Signals) -> tuple[ErrorClass, str]:
        for rule in _BUILTIN_RULES:
            try:
                if rule.matcher(sig):
                    return rule.error_class, rule.name
            except Exception:  # pragma: no cover - 规则自身异常不应影响分类
                continue
        return ErrorClass.UNKNOWN, "builtin:unknown"

    def _match_user(self, sig: Signals) -> _UserRule | None:
        for rule in self._rules:
            try:
                if rule.matches(sig):
                    return rule
            except Exception:  # pragma: no cover
                continue
        return None

    @staticmethod
    def _action_from_policy(policy: _BasePolicy) -> Action:
        if policy.retry and policy.fallback:
            return Action.RETRY_THEN_FALLBACK
        if policy.retry:
            return Action.RETRY_SAME
        if policy.fallback:
            return Action.MARK_AND_FALLBACK if policy.mark else Action.FALLBACK
        return Action.SURFACE

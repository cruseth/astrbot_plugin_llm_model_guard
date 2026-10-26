"""错误分类器单测：状态码、异常类型、消息正则、用户规则与叠加开关。"""

from __future__ import annotations

import pytest

from astrbot_plugin_llm_model_guard.core.classifier import (
    DETAIL_LIMIT,
    ErrorClassifier,
    extract_signals,
    redact,
    summarize_error,
)
from astrbot_plugin_llm_model_guard.core.models import (
    Action,
    ErrorClass,
    GuardSettings,
)

from .support import FakeResponse, ResponseLike, StatusError, err_response


def make_classifier(config: dict | None = None, rules: list[dict] | None = None) -> ErrorClassifier:
    settings = GuardSettings.from_mapping(config or {})
    return ErrorClassifier(rules or [], settings=lambda: settings)


def test_status_code_auth() -> None:
    classifier = make_classifier()
    result = classifier.classify(StatusError("invalid api key", 401))
    assert result.error_class is ErrorClass.AUTH
    assert result.should_mark is True
    assert result.action is Action.MARK_AND_FALLBACK
    assert result.matched_rule == "builtin:auth"
    assert "api key" in result.detail


def test_403_permission_also_auth() -> None:
    classifier = make_classifier()
    result = classifier.classify(StatusError("permission denied by provider", 403))
    assert result.error_class is ErrorClass.AUTH
    assert result.allows_retry is False
    assert result.allows_fallback is True


def test_404_model_not_found() -> None:
    classifier = make_classifier()
    result = classifier.classify(StatusError("model does not exist", 404))
    assert result.error_class is ErrorClass.MODEL_NOT_FOUND
    assert result.mark_ttl == 0


def test_400_model_not_found_by_message() -> None:
    classifier = make_classifier()
    result = classifier.classify(StatusError("invalid model: gpt-9", 400))
    assert result.error_class is ErrorClass.MODEL_NOT_FOUND
    assert result.should_mark is True


def test_429_quota_beats_rate_limit() -> None:
    classifier = make_classifier()
    result = classifier.classify(StatusError("You exceeded your current quota", 429))
    assert result.error_class is ErrorClass.QUOTA_EXHAUSTED
    assert "quota" in result.matched_rule
    assert result.allows_retry is False


def test_429_rate_limit_retryable_and_ttl() -> None:
    classifier = make_classifier({"mark_ttl_rate_limit": 42})
    result = classifier.classify(StatusError("Rate limit reached for requests", 429))
    assert result.error_class is ErrorClass.RATE_LIMIT
    assert result.allows_retry is True
    assert result.mark_ttl == 42


def test_content_filter_before_bad_request() -> None:
    classifier = make_classifier()
    result = classifier.classify(StatusError("Blocked by content management policy", 400))
    assert result.error_class is ErrorClass.CONTENT_FILTER
    assert result.should_mark is False
    assert result.allows_fallback is True
    assert result.allows_retry is False


def test_context_too_long() -> None:
    classifier = make_classifier()
    result = classifier.classify(
        StatusError("This model's maximum context length is 8192 tokens", 400)
    )
    assert result.error_class is ErrorClass.CONTEXT_TOO_LONG
    assert result.should_mark is False


def test_timeout_by_type() -> None:
    class APITimeoutError(Exception):
        pass

    classifier = make_classifier()
    result = classifier.classify(APITimeoutError("request timed out"))
    assert result.error_class is ErrorClass.TIMEOUT
    assert result.allows_retry is True


def test_network_by_cause_chain() -> None:
    class WrapperError(Exception):
        pass

    cause = ConnectionResetError("connection reset by peer")
    error = WrapperError("sdk failed")
    error.__cause__ = cause
    classifier = make_classifier()
    result = classifier.classify(error)
    assert result.error_class is ErrorClass.NETWORK
    assert result.mark_ttl == 120


def test_server_error_503_is_overloaded() -> None:
    classifier = make_classifier()
    assert classifier.classify(StatusError("service unavailable", 503)).error_class is (
        ErrorClass.OVERLOADED
    )
    assert classifier.classify(StatusError("bad gateway", 502)).error_class is (
        ErrorClass.SERVER_ERROR
    )
    assert classifier.classify(StatusError("internal server error", 500)).error_class is (
        ErrorClass.SERVER_ERROR
    )


def test_empty_output_by_name() -> None:
    class EmptyModelOutputError(Exception):
        pass

    classifier = make_classifier({"mark_on_empty_output_after": 3})
    result = classifier.classify(EmptyModelOutputError("empty output"))
    assert result.error_class is ErrorClass.EMPTY_OUTPUT
    assert result.mark_delay == 3
    # 默认 retry_empty_output=false：空输出交给框架自己的 tenacity（P2-4）
    assert result.allows_retry is False
    assert result.allows_fallback is True

    retrying = make_classifier(
        {"mark_on_empty_output_after": 3, "retry_empty_output": True}
    )
    assert retrying.classify(EmptyModelOutputError("empty output")).allows_retry is True


def test_unknown_fallback() -> None:
    classifier = make_classifier()
    result = classifier.classify(RuntimeError("something nobody predicted"))
    assert result.error_class is ErrorClass.UNKNOWN
    assert result.matched_rule == "builtin:unknown"
    assert result.should_mark is False
    assert result.allows_fallback is True


def test_user_rule_priority() -> None:
    rules = [
        {
            "id": "my-rule",
            "enabled": True,
            "priority": 1,
            "match_status": [500],
            "match_msg_regex": "内部错误",
            "action": "retry_same",
            "mark_ttl_seconds": 7,
        }
    ]
    classifier = make_classifier(rules=rules)
    result = classifier.classify(StatusError("内部错误，请稍后重试", 500))
    assert result.matched_rule == "user:my-rule"
    assert result.action is Action.RETRY_SAME
    assert result.error_class is ErrorClass.SERVER_ERROR


def test_invalid_user_regex_skipped() -> None:
    rules = [
        {"id": "bad", "priority": 1, "match_msg_regex": "([unclosed", "action": "surface"},
        {"id": "bad-action", "priority": 2, "action": "explode"},
        {"id": "good", "priority": 3, "match_msg_regex": "boom", "action": "surface"},
    ]
    classifier = make_classifier(rules=rules)
    assert classifier.rule_count == 1
    result = classifier.classify(RuntimeError("boom"))
    assert result.matched_rule == "user:good"
    assert result.action is Action.SURFACE


def test_err_response_classification() -> None:
    classifier = make_classifier()
    response = err_response("All chat models failed: RateLimitError: slow down")
    signals = extract_signals(response)
    assert signals.is_err_response is True
    result = classifier.classify(response)
    assert result.error_class is ErrorClass.RATE_LIMIT
    assert "RateLimitError" in signals.names


def test_stream_chunks_force_surface() -> None:
    classifier = make_classifier()
    result = classifier.classify(
        StatusError("rate limit", 429), stream=True, chunks_emitted=3
    )
    assert result.action is Action.SURFACE
    # 已产出内容后既不重试也不标记（任务包 02 §3.4 / 裁决 P2-5）
    assert result.should_mark is False
    assert result.mark_ttl == 0


def test_continuation_blocks_cross_provider_switch() -> None:
    classifier = make_classifier()
    result = classifier.classify(StatusError("invalid api key", 401), is_continuation=True)
    assert result.action is Action.RETRY_SAME
    assert result.allows_fallback is False

    relaxed = make_classifier({"allow_cross_provider_switch_mid_run": True})
    assert relaxed.classify(
        StatusError("invalid api key", 401), is_continuation=True
    ).allows_fallback is True


def test_switches_disable_actions() -> None:
    no_mark = make_classifier({"enable_marking": False})
    result = no_mark.classify(StatusError("invalid api key", 401))
    assert result.should_mark is False
    assert result.mark_ttl == 0

    no_retry = make_classifier({"enable_retry": False})
    assert no_retry.classify(StatusError("rate limit", 429)).allows_retry is False

    no_fallback = make_classifier({"enable_fallback": False})
    assert no_fallback.classify(StatusError("rate limit", 429)).action is Action.SURFACE


def test_retry_after_is_parsed() -> None:
    error = StatusError("rate limit", 429)
    error.response = ResponseLike(429, {"retry-after": "2.5"})
    classifier = make_classifier()
    result = classifier.classify(error)
    assert result.retry_after == pytest.approx(2.5)


def test_detail_is_redacted_and_truncated() -> None:
    classifier = make_classifier()
    long_message = "boom sk-abcdef123456 " + "x" * (DETAIL_LIMIT * 2)
    result = classifier.classify(StatusError(long_message, 500))
    assert "sk-abcdef123456" not in result.detail
    assert len(result.detail) <= DETAIL_LIMIT


def test_redact_and_summarize_helpers() -> None:
    assert "sk-***" in redact("key sk-abcdef123456 leaked")
    assert "api_key=***" in redact("api_key=super-secret-value")
    assert summarize_error("a\nb\tc") == "a b c"


def test_unknown_never_none() -> None:
    classifier = ErrorClassifier([], settings=lambda: GuardSettings())
    assert classifier.classify(None).error_class is ErrorClass.UNKNOWN
    assert classifier.classify(FakeResponse("普通成功响应")).error_class is ErrorClass.UNKNOWN


# ---------------------------------------------------------------------------
# 第 2 轮修复：P2-6 / P2-7 / P2-8
# ---------------------------------------------------------------------------


def test_400_generic_does_not_mark() -> None:
    """普通 400/422（例如插件自己造成的 payload 不兼容）允许降级但不标记（P2-6）。"""
    classifier = make_classifier()
    result = classifier.classify(StatusError("invalid payload shape", 400))
    assert result.error_class is ErrorClass.BAD_REQUEST_SOFT
    assert result.matched_rule == "builtin:bad_request_generic"
    assert result.should_mark is False
    assert result.mark_ttl == 0
    assert result.allows_fallback is True
    assert result.allows_retry is False


def test_422_generic_does_not_mark() -> None:
    classifier = make_classifier()
    result = classifier.classify(StatusError("unprocessable entity", 422))
    assert result.error_class is ErrorClass.BAD_REQUEST_SOFT
    assert result.should_mark is False


def test_400_model_not_found_marks() -> None:
    """400/422 且消息命中细化特征（not a VLM / does not support）才标记。"""
    classifier = make_classifier()
    result = classifier.classify(StatusError("this model is not a VLM", 400))
    assert result.error_class is ErrorClass.BAD_REQUEST
    assert result.matched_rule == "builtin:bad_request"
    assert result.should_mark is True

    generic = classifier.classify(StatusError("request body invalid", 422))
    assert generic.error_class is ErrorClass.BAD_REQUEST_SOFT


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (400, "model_not_found"),
        (422, "model_not_found"),
        (400, "model-not-found"),
        (422, "model not found"),
    ],
)
def test_model_not_found_separator_variants_mark(status: int, message: str) -> None:
    """400/422 下的模型不存在错误码，不论分隔符均必须标记。"""
    classifier = make_classifier()
    result = classifier.classify(StatusError(message, status))
    assert result.error_class is ErrorClass.MODEL_NOT_FOUND
    assert result.matched_rule == "builtin:model_not_found"
    assert result.should_mark is True
    assert result.allows_fallback is True


@pytest.mark.parametrize("status", [408, 409, 425])
def test_retryable_status_codes(status: int) -> None:
    """408/409/425 按超时语义可重试（P2-7）。"""
    classifier = make_classifier()
    result = classifier.classify(StatusError("transient failure", status))
    assert result.error_class is ErrorClass.TIMEOUT
    assert result.matched_rule == "builtin:retryable_status"
    assert result.allows_retry is True
    assert result.allows_fallback is True


def test_local_type_error_is_fatal() -> None:
    """本地编程错误（TypeError/ValueError 且无状态码）直接上抛，不降级不标记（P2-8）。"""
    classifier = make_classifier()
    result = classifier.classify(TypeError("unexpected keyword argument 'foo'"))
    assert result.action is Action.SURFACE
    assert result.should_mark is False
    assert result.matched_rule == "builtin:local-fatal"
    assert result.allows_fallback is False

    value_error = classifier.classify(ValueError("bad parameter"))
    assert value_error.action is Action.SURFACE
    assert value_error.matched_rule == "builtin:local-fatal"


def test_local_type_error_with_status_still_classified() -> None:
    """带状态码的 TypeError/ValueError 说明是远端错误，仍按状态码分类。"""
    classifier = make_classifier()
    result = classifier.classify(StatusError("rate limit", 429))
    assert result.error_class is ErrorClass.RATE_LIMIT
    assert result.matched_rule != "builtin:local-fatal"


def test_key_error_is_not_fatal() -> None:
    """KeyError/AttributeError 不在本次裁决范围内，保持原有 unknown→降级行为（P2-8）。"""
    classifier = make_classifier()
    result = classifier.classify(KeyError("missing"))
    assert result.matched_rule == "builtin:unknown"
    assert result.action is Action.FALLBACK

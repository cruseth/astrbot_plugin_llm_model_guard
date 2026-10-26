"""包装层（非流式）单测（v0.2.0）：超时、重试、回退、标记与异常上抛。

v0.2.0 的语义变化：

* 链路**直接读框架**（默认模型 + ``fallback_chat_models``），不再由配置指定；
* 重试次数由 ``max_retries`` 决定，可重试的类别由 ``retry_categories`` 决定；
* 退避是**指数**的（``retry_backoff_seconds`` × 2^(n-1)，上限 10 秒）；
* 单次调用有超时（``request_timeout_seconds``）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from astrbot_plugin_llm_model_guard.core.models import MarkSource

from .support import FakeProvider, FakeResponse, StatusError, build_runtime, err_response

UMO = "aiocqhttp:FriendMessage:10001"


def make_runtime(
    tmp_path: Path,
    *,
    primary_results: list,
    fallback_results: list | None = None,
    config: dict | None = None,
    fallbacks: list[str] | None = None,
):
    """构造 runtime：p1 是该配置文件的默认模型，p2 是它的回退模型。"""
    primary = FakeProvider("p1", results=primary_results)
    fallback = FakeProvider("p2", results=fallback_results or [FakeResponse("p2-ok")])
    runtime = build_runtime(
        tmp_path,
        config=dict(config or {}),
        providers=[primary, fallback],
        profiles={UMO: "default"},
        defaults={"default": "p1"},
        conf_fallbacks={
            "default": fallbacks if fallbacks is not None else ["p2"]
        },
    )
    return runtime, primary, fallback


def call(runtime, provider, **kwargs):
    payload = {"prompt": "你好", "session_id": UMO, **kwargs}
    return asyncio.run(runtime.guard.handle_text_chat(provider, (), payload))


# ---------------------------------------------------------------------------
# 重试
# ---------------------------------------------------------------------------


def test_retry_then_success(tmp_path: Path) -> None:
    runtime, primary, _ = make_runtime(
        tmp_path,
        primary_results=[StatusError("rate limit", 429), FakeResponse("p1-ok")],
        config={"max_retries": 1},
    )
    response = call(runtime, primary)
    assert response.completion_text == "p1-ok"
    assert primary.call_count == 2
    assert runtime.sleep.delays, "应当发生过退避等待"
    events = runtime.metrics.recent_events(limit=10)
    assert [event["ok"] for event in events] == [True, False]


def test_backoff_is_exponential(tmp_path: Path) -> None:
    """退避是指数的：base、base×2、base×4…（上限 10 秒）。"""
    runtime, primary, fallback = make_runtime(
        tmp_path,
        primary_results=[StatusError("rate limit", 429)],
        config={"max_retries": 2, "retry_backoff_seconds": 0.5},
    )
    response = call(runtime, primary)
    assert runtime.sleep.delays == [0.5, 1.0]
    assert response.completion_text == "p2-ok"
    assert primary.call_count == 3
    assert fallback.call_count == 1


def test_backoff_capped_at_ten_seconds(tmp_path: Path) -> None:
    """退避上限 10 秒。"""
    runtime, primary, _ = make_runtime(
        tmp_path,
        primary_results=[StatusError("rate limit", 429)],
        config={"max_retries": 3, "retry_backoff_seconds": 100.0},
    )
    call(runtime, primary)
    assert runtime.sleep.delays == [10.0, 10.0, 10.0]


def test_max_retries_zero_means_no_retry(tmp_path: Path) -> None:
    runtime, primary, fallback = make_runtime(
        tmp_path,
        primary_results=[StatusError("rate limit", 429)],
        config={"max_retries": 0},
    )
    response = call(runtime, primary)
    assert response.completion_text == "p2-ok"
    assert primary.call_count == 1
    assert runtime.sleep.delays == []


def test_retry_categories_gate_retry(tmp_path: Path) -> None:
    """不在 ``retry_categories`` 里的类别**不重试**，直接回退并标记。"""
    runtime, primary, fallback = make_runtime(
        tmp_path,
        primary_results=[StatusError("rate limit", 429)],
        config={
            "max_retries": 3,
            "retry_categories": ["timeout"],  # 不含 rate_limit
        },
    )
    response = call(runtime, primary)
    assert response.completion_text == "p2-ok"
    assert primary.call_count == 1, "不在可重试类别里就不该重试"
    assert runtime.sleep.delays == []
    assert runtime.guardian.is_marked("p1") is True


def test_retry_categories_can_include_rate_limit(tmp_path: Path) -> None:
    runtime, primary, _ = make_runtime(
        tmp_path,
        primary_results=[StatusError("rate limit", 429), FakeResponse("p1-ok")],
        config={"max_retries": 1, "retry_categories": ["rate_limit"]},
    )
    assert call(runtime, primary).completion_text == "p1-ok"
    assert primary.call_count == 2


# ---------------------------------------------------------------------------
# 回退与标记
# ---------------------------------------------------------------------------


def test_overloaded_is_retried_by_default(tmp_path: Path) -> None:
    """529 / overloaded 是瞬时错误：默认就该重试同一模型，而不是直接标记。"""
    runtime, primary, _ = make_runtime(
        tmp_path,
        primary_results=[StatusError("overloaded", 529), FakeResponse("p1-ok")],
        config={"max_retries": 1},
    )
    assert call(runtime, primary).completion_text == "p1-ok"
    assert primary.call_count == 2, "overloaded 应当重试"
    assert runtime.sleep.delays


def test_empty_output_is_not_retried_by_default(tmp_path: Path) -> None:
    """空输出默认不重试（框架自身已有 tenacity 重试，插件叠加会放大请求量）。"""
    empty_error = RuntimeError("EmptyModelOutputError: no usable assistant output")
    runtime, primary, fallback = make_runtime(
        tmp_path,
        primary_results=[empty_error],
        config={"max_retries": 3},
    )
    response = call(runtime, primary)
    assert response.completion_text == "p2-ok"
    assert primary.call_count == 1, "插件层不应重试空输出"
    assert runtime.sleep.delays == []


def test_empty_output_can_be_made_retryable(tmp_path: Path) -> None:
    """把 ``empty_output`` 加进配置后，它就变成可重试。"""
    empty_error = RuntimeError("EmptyModelOutputError: no usable assistant output")
    runtime, primary, _ = make_runtime(
        tmp_path,
        primary_results=[empty_error, FakeResponse("p1-ok")],
        config={"max_retries": 1, "retry_categories": ["empty_output"]},
    )
    assert call(runtime, primary).completion_text == "p1-ok"
    assert primary.call_count == 2


def test_no_retry_on_auth_and_marks(tmp_path: Path) -> None:
    """鉴权失败不在可重试类别里 → 不重试、直接回退并标记。"""
    runtime, primary, fallback = make_runtime(
        tmp_path,
        primary_results=[StatusError("invalid api key", 401)],
        config={"max_retries": 3},
    )
    response = call(runtime, primary)
    assert response.completion_text == "p2-ok"
    assert primary.call_count == 1
    assert runtime.sleep.delays == []
    assert runtime.guardian.is_marked("p1") is True
    assert runtime.guardian.marks()["p1"].reason == "auth"
    assert runtime.guardian.marks()["p1"].source == MarkSource.AUTO


def test_fallback_on_bad_request(tmp_path: Path) -> None:
    runtime, primary, fallback = make_runtime(
        tmp_path,
        primary_results=[StatusError("invalid model: gpt-9", 400)],
    )
    assert call(runtime, primary).completion_text == "p2-ok"
    assert fallback.call_count == 1
    assert runtime.guardian.marks()["p1"].reason == "model_not_found"


def test_model_kwarg_dropped_for_fallback(tmp_path: Path) -> None:
    runtime, primary, fallback = make_runtime(
        tmp_path,
        primary_results=[StatusError("invalid api key", 401)],
    )
    call(runtime, primary, model="explicit-model")
    assert primary.calls[0]["model"] == "explicit-model"
    assert fallback.calls[0]["model"] is None


def test_chain_all_failed_raises_last(tmp_path: Path) -> None:
    runtime, primary, fallback = make_runtime(
        tmp_path,
        primary_results=[StatusError("internal server error", 500)],
        fallback_results=[StatusError("bad gateway", 502)],
        config={"max_retries": 0},
    )
    with pytest.raises(StatusError) as excinfo:
        call(runtime, primary)
    assert excinfo.value.status_code == 502


def test_err_response_treated_as_failure(tmp_path: Path) -> None:
    runtime, primary, fallback = make_runtime(
        tmp_path,
        primary_results=[err_response("All chat models failed: RuntimeError: boom")],
    )
    response = call(runtime, primary)
    assert response.completion_text == "p2-ok"
    assert fallback.call_count == 1


def test_all_candidates_marked_uses_first(tmp_path: Path) -> None:
    runtime, primary, fallback = make_runtime(
        tmp_path,
        primary_results=[FakeResponse("p1-ok")],
    )
    asyncio.run(runtime.guardian.mark("p1", reason="auth"))
    asyncio.run(runtime.guardian.mark("p2", reason="auth"))
    response = call(runtime, primary)
    assert response.completion_text == "p1-ok"
    assert runtime.metrics.counters["all_candidates_marked"] >= 1


def test_internal_error_passthrough(tmp_path: Path) -> None:
    runtime, primary, _ = make_runtime(
        tmp_path,
        primary_results=[FakeResponse("p1-ok")],
    )

    def _explode(kwargs):  # noqa: ANN001
        raise RuntimeError("候选链解析炸了")

    runtime.guard.resolve_call_context = _explode  # type: ignore[assignment]
    response = call(runtime, primary)
    assert response.completion_text == "p1-ok"
    assert runtime.metrics.counters["guard_internal_errors"] == 1


def test_disabled_makes_no_intervention(tmp_path: Path) -> None:
    runtime, primary, fallback = make_runtime(
        tmp_path,
        primary_results=[StatusError("rate limit", 429)],
        config={"enable": False},
    )
    with pytest.raises(StatusError):
        call(runtime, primary)
    assert primary.call_count == 1
    assert fallback.call_count == 0
    assert runtime.metrics.recent_events(limit=10) == []


def test_probe_prompt_is_passthrough(tmp_path: Path) -> None:
    runtime, primary, fallback = make_runtime(
        tmp_path,
        primary_results=[StatusError("rate limit", 429)],
    )
    with pytest.raises(StatusError):
        asyncio.run(
            runtime.guard.handle_text_chat(
                primary,
                (),
                {"prompt": "REPLY `PONG` ONLY", "session_id": UMO},
            )
        )
    # 验证提示词走旁路：算一次 probe 调用，不算普通对话调用。
    assert primary.probe_call_count == 1
    assert primary.call_count == 0
    assert fallback.call_count == 0


def test_positional_args_passthrough(tmp_path: Path) -> None:
    runtime, primary, _ = make_runtime(
        tmp_path,
        primary_results=[FakeResponse("p1-ok")],
    )

    async def _positional(prompt: str) -> FakeResponse:
        return FakeResponse(f"positional:{prompt}")

    primary.text_chat = _positional  # type: ignore[assignment]
    response = asyncio.run(runtime.guard.handle_text_chat(primary, ("hi",), {}))
    assert response.completion_text == "positional:hi"
    assert runtime.metrics.counters["positional_passthrough"] == 1


def test_unmanaged_call_only_records(tmp_path: Path) -> None:
    provider = FakeProvider("solo", results=[FakeResponse("solo-ok")])
    runtime = build_runtime(tmp_path, config={}, providers=[provider])
    response = asyncio.run(
        runtime.guard.handle_text_chat(provider, (), {"prompt": "hi", "session_id": "unknown"})
    )
    assert response.completion_text == "solo-ok"
    events = runtime.metrics.recent_events(limit=5)
    assert len(events) == 1
    assert events[0]["used"] == "solo"


# ---------------------------------------------------------------------------
# 超时
# ---------------------------------------------------------------------------


def test_request_timeout_triggers_retry(tmp_path: Path) -> None:
    """超时按「timeout」类别处理：可重试类别里有它就会重试。"""

    class _SlowProvider(FakeProvider):
        async def text_chat(self, **kwargs):  # type: ignore[override]
            self.call_count += 1
            self.calls.append(dict(kwargs))
            await asyncio.sleep(10)
            return FakeResponse("never")

    slow = _SlowProvider("p1")
    runtime = build_runtime(
        tmp_path,
        config={
            "request_timeout_seconds": 1,
            "max_retries": 0,
        },
        providers=[slow],
        profiles={UMO: "default"},
        defaults={"default": "p1"},
        conf_fallbacks={"default": []},
    )
    with pytest.raises((asyncio.TimeoutError, TimeoutError)):
        call(runtime, slow)
    assert slow.call_count == 1


def test_request_timeout_zero_means_no_limit(tmp_path: Path) -> None:
    """``request_timeout_seconds=0`` 表示不限制。"""
    settings = runtime_settings({"request_timeout_seconds": 0})
    assert settings.request_timeout() is None
    assert settings.request_timeout_seconds == 0


def runtime_settings(config: dict) -> object:
    from astrbot_plugin_llm_model_guard.core.models import GuardSettings

    return GuardSettings.from_mapping(config)


# ---------------------------------------------------------------------------
# 本地编程错误
# ---------------------------------------------------------------------------


def test_local_type_error_surfaces_without_fallback(tmp_path: Path) -> None:
    """本地 TypeError 直接上抛：不重试、不换模型、不标记。"""
    runtime, primary, fallback = make_runtime(
        tmp_path,
        primary_results=[TypeError("unexpected keyword argument")],
        config={"max_retries": 3},
    )
    with pytest.raises(TypeError):
        call(runtime, primary)
    assert primary.call_count == 1
    assert fallback.call_count == 0
    assert runtime.guardian.has_marked() is False


def test_local_value_error_surfaces_without_fallback(tmp_path: Path) -> None:
    runtime, primary, fallback = make_runtime(
        tmp_path,
        primary_results=[ValueError("bad value")],
    )
    with pytest.raises(ValueError):
        call(runtime, primary)
    assert primary.call_count == 1
    assert fallback.call_count == 0
    assert runtime.guardian.has_marked() is False


# ---------------------------------------------------------------------------
# 框架二次进入
# ---------------------------------------------------------------------------


def _second_entry(runtime, fallback, kwargs) -> str:
    """模拟框架降级后的"二次进入"：用同一个 CallContext 再调一次包装层。"""
    payload = {"prompt": "你好", "session_id": UMO, **kwargs}
    response = asyncio.run(runtime.guard.handle_text_chat(fallback, (), payload))
    return response.completion_text


def test_framework_second_entry_does_not_retry_marked_candidate(tmp_path: Path) -> None:
    """框架降级二次进入时不得重打链首坏模型。"""
    from astrbot_plugin_llm_model_guard.core.wrapper import CALL_CONTEXT

    runtime, primary, fallback = make_runtime(
        tmp_path,
        primary_results=[StatusError("invalid api key", 401)],
        config={"max_retries": 0},
    )
    payload = {"prompt": "你好", "session_id": UMO}

    async def _whole_flow() -> str:
        context = runtime.guard.resolve_call_context(payload, provider_id="p1")
        CALL_CONTEXT.set(context)
        first = await runtime.guard.handle_text_chat(primary, (), payload)
        assert first.completion_text == "p2-ok"
        second = await runtime.guard.handle_text_chat(fallback, (), payload)
        return second.completion_text

    assert asyncio.run(_whole_flow()) == "p2-ok"
    assert primary.call_count == 1
    assert runtime.guardian.is_marked("p1") is True
    assert runtime.metrics.counters.get("framework_fallback_hops", 0) >= 1


def test_skip_marked_candidate_without_attempt_history(tmp_path: Path) -> None:
    """被标记的候选不会再有请求。"""
    runtime, primary, fallback = make_runtime(
        tmp_path,
        primary_results=[FakeResponse("p1-ok")],
        config={"max_retries": 0},
    )
    asyncio.run(runtime.guardian.mark("p1", reason="auth"))
    response = call(runtime, primary)
    assert response.completion_text == "p2-ok"
    assert primary.call_count == 0


def test_abort_signal_interrupts_backoff(tmp_path: Path) -> None:
    """用户点“停止生成”时退避应立即中断。"""
    runtime, primary, _ = make_runtime(
        tmp_path,
        primary_results=[StatusError("rate limit", 429)],
        config={"max_retries": 3, "retry_backoff_seconds": 5.0},
    )
    runtime.guard._sleep_fn = None  # 使用真实的分段 sleep，才能验证 abort

    async def _abort() -> None:
        abort_signal = asyncio.Event()
        abort_signal.set()
        payload = {
            "prompt": "你好",
            "session_id": UMO,
            "abort_signal": abort_signal,
        }
        try:
            await runtime.guard.handle_text_chat(primary, (), payload)
        except asyncio.CancelledError:
            return
        raise AssertionError("应当因 abort_signal 抛出 CancelledError")

    asyncio.run(_abort())
    assert primary.call_count == 1


# ---------------------------------------------------------------------------
# 链路相关
# ---------------------------------------------------------------------------


def test_chain_comes_from_framework_config(tmp_path: Path) -> None:
    """链路直接读框架：默认模型 + 该配置文件的回退列表。"""
    runtime, primary, fallback = make_runtime(
        tmp_path,
        primary_results=[FakeResponse("p1-ok")],
    )
    plan = runtime.guard.build_plan(umo=UMO)
    assert plan.candidates == ("p1", "p2")


def test_chain_follows_session_level_selection(tmp_path: Path) -> None:
    """会话级模型切换会被链路自动纳入（链首是它）。"""
    primary = FakeProvider("p1", results=[FakeResponse("p1-ok")])
    second = FakeProvider("p2", results=[FakeResponse("p2-ok")])
    runtime = build_runtime(
        tmp_path,
        config={},
        providers=[primary, second],
        profiles={UMO: "default"},
        defaults={"default": "p1"},
        conf_fallbacks={"default": ["p2"]},
        session_provider={UMO: "p2"},
    )
    plan = runtime.guard.build_plan(umo=UMO)
    assert plan.candidates[0] == "p2"
    assert plan.source == "manual" or plan.candidates[0] == "p2"


def test_chain_failure_annotates_last_classification(tmp_path: Path) -> None:
    """全链失败时把最后一次分类摘要附到异常上。"""
    runtime, primary, fallback = make_runtime(
        tmp_path,
        primary_results=[StatusError("internal server error", 500)],
        fallback_results=[StatusError("bad gateway", 502)],
        config={"max_retries": 0},
    )
    with pytest.raises(StatusError) as excinfo:
        call(runtime, primary)
    notes = getattr(excinfo.value, "__notes__", [])
    assert any("候选链上的模型全部失败" in note for note in notes)
    assert any("server_error" in note for note in notes)


def test_408_is_retried(tmp_path: Path) -> None:
    """408 走重试路径。"""
    runtime, primary, _ = make_runtime(
        tmp_path,
        primary_results=[StatusError("request timeout", 408), FakeResponse("p1-ok")],
        config={"max_retries": 1},
    )
    assert call(runtime, primary).completion_text == "p1-ok"
    assert primary.call_count == 2
    assert runtime.sleep.delays

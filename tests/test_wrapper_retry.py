"""包装层（非流式）单测：重试、退避、降级、标记与异常上抛。"""

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
):
    primary = FakeProvider("p1", results=primary_results)
    fallback = FakeProvider("p2", results=fallback_results or [FakeResponse("p2-ok")])
    merged = {
        "routes": [
            {"profile_id": "default", "primary": "p1", "fallbacks": ["p2"]},
        ],
        **(config or {}),
    }
    runtime = build_runtime(
        tmp_path,
        config=merged,
        providers=[primary, fallback],
        profiles={UMO: "default"},
    )
    return runtime, primary, fallback


def call(runtime, provider, **kwargs):
    payload = {"prompt": "你好", "session_id": UMO, **kwargs}
    return asyncio.run(runtime.guard.handle_text_chat(provider, (), payload))


def test_retry_then_success(tmp_path: Path) -> None:
    runtime, primary, _ = make_runtime(
        tmp_path,
        primary_results=[StatusError("rate limit", 429), FakeResponse("p1-ok")],
        config={"max_retries_per_model": 2},
    )
    response = call(runtime, primary)
    assert response.completion_text == "p1-ok"
    assert primary.call_count == 2
    assert runtime.sleep.delays, "应当发生过退避等待"
    events = runtime.metrics.recent_events(limit=10)
    assert [event["ok"] for event in events] == [True, False]


def test_backoff_sequence(tmp_path: Path) -> None:
    runtime, primary, fallback = make_runtime(
        tmp_path,
        primary_results=[StatusError("rate limit", 429)],
        config={
            "max_retries_per_model": 2,
            "retry_backoff_base": 0.5,
            "retry_backoff_factor": 2.0,
            "retry_jitter_ratio": 0.0,
            "retry_backoff_max": 100.0,
        },
    )
    response = call(runtime, primary)
    # 重试两次（0.5s、1.0s）后仍失败 → 降级到 p2 成功
    assert runtime.sleep.delays == [0.5, 1.0]
    assert response.completion_text == "p2-ok"
    assert primary.call_count == 3
    assert fallback.call_count == 1


def test_retry_after_honored(tmp_path: Path) -> None:
    error = StatusError("rate limit", 429)
    from .support import ResponseLike

    error.response = ResponseLike(429, {"retry-after": "2"})
    runtime, primary, _ = make_runtime(
        tmp_path,
        primary_results=[error, FakeResponse("p1-ok")],
        config={"max_retries_per_model": 1, "retry_backoff_max": 8.0},
    )
    assert call(runtime, primary).completion_text == "p1-ok"
    assert runtime.sleep.delays == [2.0]


def test_retry_budget_exhausted(tmp_path: Path) -> None:
    runtime, primary, fallback = make_runtime(
        tmp_path,
        primary_results=[StatusError("rate limit", 429)],
        config={"max_retries_per_model": 3, "retry_budget_per_umo_per_minute": 1},
    )
    response = call(runtime, primary)
    assert response.completion_text == "p2-ok"
    assert len(runtime.sleep.delays) == 1
    assert fallback.call_count == 1


def test_no_retry_on_auth_and_marks(tmp_path: Path) -> None:
    runtime, primary, fallback = make_runtime(
        tmp_path,
        primary_results=[StatusError("invalid api key", 401)],
        config={"max_retries_per_model": 3},
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


def test_continuation_no_cross_switch(tmp_path: Path) -> None:
    runtime, primary, fallback = make_runtime(
        tmp_path,
        primary_results=[StatusError("invalid api key", 401)],
        config={"max_retries_per_model": 1},
    )
    with pytest.raises(StatusError):
        call(runtime, primary, tool_calls_result=[{"tool_call_id": "abc"}])
    assert fallback.call_count == 0
    assert primary.call_count == 2  # 只重试同一模型


def test_chain_all_failed_raises_last(tmp_path: Path) -> None:
    runtime, primary, fallback = make_runtime(
        tmp_path,
        primary_results=[StatusError("internal server error", 500)],
        fallback_results=[StatusError("bad gateway", 502)],
        config={"max_retries_per_model": 0},
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
    asyncio.run(runtime.guardian.mark("p1", reason="auth", ttl=0))
    asyncio.run(runtime.guardian.mark("p2", reason="auth", ttl=0))
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
    assert primary.call_count == 1
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


def test_routing_disabled_still_retries_same_model(tmp_path: Path) -> None:
    """关掉「接管选择」只影响选谁，不影响对框架所选模型的重试/标记。"""
    runtime, primary, fallback = make_runtime(
        tmp_path,
        primary_results=[StatusError("rate limit", 429), FakeResponse("p1-ok")],
        config={"enable_routing": False, "max_retries_per_model": 1},
    )
    response = call(runtime, primary)
    assert response.completion_text == "p1-ok"
    assert primary.call_count == 2
    assert fallback.call_count == 0


# ---------------------------------------------------------------------------
# 第 2 轮修复：P2-1 / P2-2 / P2-3 / P2-4 / P3-1 / P3-9
# ---------------------------------------------------------------------------


def _second_entry(runtime, fallback, kwargs) -> str:
    """模拟框架降级后的"二次进入"：用同一个 CallContext 再调一次包装层。"""
    payload = {"prompt": "你好", "session_id": UMO, **kwargs}
    response = asyncio.run(runtime.guard.handle_text_chat(fallback, (), payload))
    return response.completion_text


def test_framework_second_entry_does_not_retry_marked_candidate(tmp_path: Path) -> None:
    """框架降级二次进入时不得重打链首坏模型（P2-1）。"""
    from astrbot_plugin_llm_model_guard.core.wrapper import CALL_CONTEXT

    runtime, primary, fallback = make_runtime(
        tmp_path,
        primary_results=[StatusError("invalid api key", 401)],
        config={"max_retries_per_model": 0},
    )
    payload = {"prompt": "你好", "session_id": UMO}

    async def _whole_flow() -> str:
        # 模拟 hook 已经把 CallContext 写进 contextvar（真实链路就是这样）
        context = runtime.guard.resolve_call_context(payload, provider_id="p1")
        CALL_CONTEXT.set(context)
        first = await runtime.guard.handle_text_chat(primary, (), payload)
        assert first.completion_text == "p2-ok"
        # 框架降级后再次进入包装层：这次 provider 是 p2，但共享同一个 CallContext
        second = await runtime.guard.handle_text_chat(fallback, (), payload)
        return second.completion_text

    assert asyncio.run(_whole_flow()) == "p2-ok"
    # 链首坏模型只被请求 1 次，且已被标记
    assert primary.call_count == 1
    assert runtime.guardian.is_marked("p1") is True
    assert runtime.metrics.counters.get("framework_fallback_hops", 0) >= 1


def test_skip_marked_candidate_without_attempt_history(tmp_path: Path) -> None:
    """被标记的候选不会再有请求（P2-1：即使没有 attempt 记录也不打）。"""
    runtime, primary, fallback = make_runtime(
        tmp_path,
        primary_results=[FakeResponse("p1-ok")],
        config={"max_retries_per_model": 0},
    )
    asyncio.run(runtime.guardian.mark("p1", reason="auth", ttl=0))
    response = call(runtime, primary)
    assert response.completion_text == "p2-ok"
    assert primary.call_count == 0


def test_abort_signal_interrupts_backoff(tmp_path: Path) -> None:
    """用户点“停止生成”时退避应立即中断（P2-2）。"""
    runtime, primary, _ = make_runtime(
        tmp_path,
        primary_results=[StatusError("rate limit", 429)],
        config={
            "max_retries_per_model": 3,
            "retry_backoff_base": 5.0,
            "retry_backoff_max": 5.0,
            "retry_jitter_ratio": 0.0,
        },
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


def test_modality_incompatible_fallback_is_skipped(tmp_path: Path) -> None:
    """请求带图片而回退模型没声明 image 能力时跳过该候选（P2-3）。"""
    primary = FakeProvider("p1", results=[StatusError("invalid api key", 401)])
    fallback = FakeProvider("p2", results=[FakeResponse("p2-ok")])
    fallback.provider_config["modalities"] = ["text"]
    third = FakeProvider("p3", results=[FakeResponse("p3-ok")])
    third.provider_config["modalities"] = ["image", "text"]

    runtime = build_runtime(
        tmp_path,
        config={
            "routes": [
                {"profile_id": "default", "primary": "p1", "fallbacks": ["p2", "p3"]},
            ],
            "max_retries_per_model": 0,
        },
        providers=[primary, fallback, third],
        profiles={UMO: "default"},
    )
    response = asyncio.run(
        runtime.guard.handle_text_chat(
            primary,
            (),
            {"prompt": "看图", "session_id": UMO, "image_urls": ["http://img"]},
        )
    )
    assert response.completion_text == "p3-ok"
    assert fallback.call_count == 0
    assert runtime.metrics.counters.get("candidate_modality_incompatible", 0) >= 1


def test_modality_unknown_means_support_all(tmp_path: Path) -> None:
    """modalities 为空 = 未配置 = 全支持（与框架语义一致）。"""
    primary = FakeProvider("p1", results=[StatusError("invalid api key", 401)])
    fallback = FakeProvider("p2", results=[FakeResponse("p2-ok")])
    runtime = build_runtime(
        tmp_path,
        config={
            "routes": [
                {"profile_id": "default", "primary": "p1", "fallbacks": ["p2"]},
            ],
            "max_retries_per_model": 0,
        },
        providers=[primary, fallback],
        profiles={UMO: "default"},
    )
    response = asyncio.run(
        runtime.guard.handle_text_chat(
            primary,
            (),
            {"prompt": "看图", "session_id": UMO, "image_urls": ["http://img"]},
        )
    )
    assert response.completion_text == "p2-ok"
    assert fallback.call_count == 1


def test_defaults_do_not_retry_empty_output(tmp_path: Path) -> None:
    """默认配置下空输出不在插件层重试，交给框架 tenacity（P2-4）。"""
    empty_error = RuntimeError("EmptyModelOutputError: no usable assistant output")
    primary = FakeProvider("p1", results=[empty_error])
    fallback = FakeProvider("p2", results=[FakeResponse("p2-ok")])
    runtime = build_runtime(
        tmp_path,
        config={
            "routes": [
                {"profile_id": "default", "primary": "p1", "fallbacks": ["p2"]},
            ]
        },
        providers=[primary, fallback],
        profiles={UMO: "default"},
    )
    settings = runtime.guard.settings
    assert settings.max_retries_per_model == 1
    assert settings.retry_empty_output is False

    response = asyncio.run(
        runtime.guard.handle_text_chat(
            primary, (), {"prompt": "hi", "session_id": UMO}
        )
    )
    assert response.completion_text == "p2-ok"
    assert primary.call_count == 1  # 插件层没有重试空输出
    assert runtime.sleep.delays == []


def test_chain_failure_annotates_last_classification(tmp_path: Path) -> None:
    """全链失败时把最后一次分类摘要附到异常上（P3-9）。"""
    runtime, primary, fallback = make_runtime(
        tmp_path,
        primary_results=[StatusError("internal server error", 500)],
        fallback_results=[StatusError("bad gateway", 502)],
        config={"max_retries_per_model": 0},
    )
    with pytest.raises(StatusError) as excinfo:
        call(runtime, primary)
    notes = getattr(excinfo.value, "__notes__", [])
    assert any("候选链上的模型全部失败" in note for note in notes)
    assert any("server_error" in note for note in notes)


def test_local_type_error_surfaces_without_fallback(tmp_path: Path) -> None:
    """本地 TypeError 直接上抛，不再换模型（P2-8 在包装层的行为）。"""
    runtime, primary, fallback = make_runtime(
        tmp_path,
        primary_results=[TypeError("unexpected keyword argument")],
        config={"max_retries_per_model": 3},
    )
    with pytest.raises(TypeError):
        call(runtime, primary)
    assert primary.call_count == 1
    assert fallback.call_count == 0
    assert runtime.guardian.has_marked() is False


def test_408_is_retried(tmp_path: Path) -> None:
    """408/409/425 走重试路径（P2-7 在包装层的验证）。"""
    runtime, primary, _ = make_runtime(
        tmp_path,
        primary_results=[StatusError("request timeout", 408), FakeResponse("p1-ok")],
        config={"max_retries_per_model": 1},
    )
    assert call(runtime, primary).completion_text == "p1-ok"
    assert primary.call_count == 2
    assert runtime.sleep.delays

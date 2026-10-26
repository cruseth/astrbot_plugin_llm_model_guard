"""包装层（流式）单测：首块前可重试/降级，已产出内容一律原样抛出。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from .support import FakeProvider, FakeResponse, StatusError, build_runtime, err_response

UMO = "aiocqhttp:FriendMessage:20002"


def make_runtime(
    tmp_path: Path,
    *,
    primary_streams: list,
    fallback_streams: list | None = None,
    config: dict | None = None,
):
    primary = FakeProvider("p1", stream_results=primary_streams)
    fallback = FakeProvider(
        "p2", stream_results=fallback_streams or [[FakeResponse("p2-chunk")]]
    )
    runtime = build_runtime(
        tmp_path,
        config=dict(config or {}),
        providers=[primary, fallback],
        profiles={UMO: "default"},
        defaults={"default": "p1"},
        conf_fallbacks={"default": ["p2"]},
    )
    return runtime, primary, fallback


async def collect(iterator) -> list[str]:
    texts: list[str] = []
    async for chunk in iterator:
        texts.append(getattr(chunk, "completion_text", ""))
    return texts


def stream(runtime, provider, **kwargs) -> list[str]:
    payload = {"prompt": "你好", "session_id": UMO, **kwargs}
    return asyncio.run(collect(runtime.guard.handle_text_chat_stream(provider, (), payload)))


def test_retry_before_first_chunk(tmp_path: Path) -> None:
    runtime, primary, _ = make_runtime(
        tmp_path,
        primary_streams=[
            [StatusError("internal server error", 500)],
            [FakeResponse("c1"), FakeResponse("c2")],
        ],
        config={"max_retries": 1},
    )
    assert stream(runtime, primary) == ["c1", "c2"]
    assert primary.stream_call_count == 2
    assert runtime.sleep.delays


def test_fallback_before_first_chunk(tmp_path: Path) -> None:
    runtime, primary, fallback = make_runtime(
        tmp_path,
        primary_streams=[[StatusError("invalid api key", 401)]],
        config={"max_retries": 0},
    )
    assert stream(runtime, primary) == ["p2-chunk"]
    assert fallback.stream_call_count == 1
    assert runtime.guardian.is_marked("p1") is True


def test_no_retry_after_chunk(tmp_path: Path) -> None:
    runtime, primary, fallback = make_runtime(
        tmp_path,
        primary_streams=[[FakeResponse("a"), RuntimeError("stream broke")]],
        config={"max_retries": 3},
    )
    with pytest.raises(RuntimeError):
        stream(runtime, primary)
    assert primary.stream_call_count == 1
    assert fallback.stream_call_count == 0
    assert runtime.sleep.delays == []


def test_no_content_mixing(tmp_path: Path) -> None:
    runtime, primary, _ = make_runtime(
        tmp_path,
        primary_streams=[[FakeResponse("a"), FakeResponse("b"), RuntimeError("boom")]],
        config={"max_retries": 2},
    )
    captured: list[str] = []

    async def _run() -> None:
        payload = {"prompt": "hi", "session_id": UMO}
        try:
            async for chunk in runtime.guard.handle_text_chat_stream(primary, (), payload):
                captured.append(chunk.completion_text)
        except RuntimeError:
            pass

    asyncio.run(_run())
    assert captured == ["a", "b"]


def test_stream_error_after_chunk_never_marks(tmp_path: Path) -> None:
    """流式已产出内容后中断：不重试、不换候选、**不写标记**（P2-5 裁决）。"""
    runtime, primary, _ = make_runtime(
        tmp_path,
        primary_streams=[[FakeResponse("a"), RuntimeError("boom")]],
    )
    payload = {"prompt": "hi", "session_id": UMO}
    for _ in range(3):
        with pytest.raises(RuntimeError):
            stream(runtime, primary)
        assert runtime.guardian.has_marked() is False
    # 只记统计（3 次失败事件），仍然没有标记
    events = runtime.metrics.recent_events(limit=10)
    assert len(events) == 3
    assert all(event["ok"] is False for event in events)
    assert runtime.guardian.all_marks() == {}


def test_cancel_propagates(tmp_path: Path) -> None:
    runtime, primary, _ = make_runtime(
        tmp_path,
        primary_streams=[[asyncio.CancelledError()]],
    )
    caught: list[str] = []

    async def _run() -> None:
        payload = {"prompt": "hi", "session_id": UMO}
        try:
            async for _chunk in runtime.guard.handle_text_chat_stream(primary, (), payload):
                pass
        except asyncio.CancelledError:
            caught.append("cancelled")

    asyncio.run(_run())
    assert caught == ["cancelled"]
    assert runtime.guardian.has_marked() is False


def test_first_chunk_ms_recorded(tmp_path: Path) -> None:
    runtime, primary, _ = make_runtime(
        tmp_path,
        primary_streams=[[FakeResponse("c1"), FakeResponse("c2")]],
    )
    assert stream(runtime, primary) == ["c1", "c2"]
    events = runtime.metrics.recent_events(limit=5)
    assert len(events) == 1
    assert events[0]["stream"] is True
    assert events[0]["first_chunk_ms"] is not None
    assert events[0]["ok"] is True


def test_err_chunk_counts_as_success(tmp_path: Path) -> None:
    """流式正常结束即算成功；是否 err 由框架按内容判断。"""
    runtime, primary, _ = make_runtime(
        tmp_path,
        primary_streams=[[err_response("partial")]],
    )
    assert stream(runtime, primary) == ["partial"]


def test_stream_via_instance_wrapper(tmp_path: Path) -> None:
    runtime, primary, _ = make_runtime(
        tmp_path,
        primary_streams=[[FakeResponse("c1")]],
    )
    assert runtime.guard.ensure_wrapped_all() == 2
    iterator = primary.text_chat_stream(prompt="hi", session_id=UMO)
    assert asyncio.run(collect(iterator)) == ["c1"]


def test_stream_disabled_passthrough(tmp_path: Path) -> None:
    runtime, primary, fallback = make_runtime(
        tmp_path,
        primary_streams=[[StatusError("rate limit", 429)]],
        config={"enable": False},
    )
    with pytest.raises(StatusError):
        stream(runtime, primary)
    assert primary.stream_call_count == 1
    assert fallback.stream_call_count == 0

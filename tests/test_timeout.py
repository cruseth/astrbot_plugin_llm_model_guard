"""超时单测（v0.2.0）：``request_timeout_seconds`` 生效、0 表示不限制。

对应用户流程图里的「超时（时间可配置）」分支：

* 非流式：``asyncio.wait_for`` 包住整个调用；
* 流式：**只对首个 chunk** 施加超时，首块之后不再施加（避免长回答被误杀）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from .support import FakeProvider, FakeResponse, StatusError, build_runtime

UMO = "aiocqhttp:FriendMessage:30009"


class _SlowProvider(FakeProvider):
    """永远睡下去的 provider。"""

    async def text_chat(self, **kwargs):  # type: ignore[override]
        self.call_count += 1
        self.calls.append(dict(kwargs))
        await asyncio.sleep(30)
        return FakeResponse("never")


class _SlowStreamProvider(FakeProvider):
    """首块也睡很久的流式 provider。"""

    def __init__(self, provider_id: str = "p1") -> None:
        super().__init__(provider_id)
        self.stream_call_count = 0

    async def _stream(self, kwargs):  # type: ignore[override]
        self.stream_call_count += 1
        await asyncio.sleep(30)
        yield FakeResponse("never")

    def text_chat_stream(self, **kwargs):  # type: ignore[override]
        return self._stream(kwargs)


def test_non_stream_timeout_fires(tmp_path: Path) -> None:
    """非流式：超时被捕获成 TimeoutError。"""
    slow = _SlowProvider("p1")
    runtime = build_runtime(
        tmp_path,
        config={"request_timeout_seconds": 1, "max_retries": 0},
        providers=[slow],
        profiles={UMO: "default"},
        defaults={"default": "p1"},
        conf_fallbacks={"default": []},
    )
    with pytest.raises((asyncio.TimeoutError, TimeoutError)):
        asyncio.run(
            runtime.guard.handle_text_chat(
                slow, (), {"prompt": "hi", "session_id": UMO}
            )
        )
    assert slow.call_count == 1


def test_timeout_is_retryable_when_in_categories(tmp_path: Path) -> None:
    """超时属于「timeout」类别，默认在可重试列表里 → 会重试。"""
    slow = _SlowProvider("p1")
    runtime = build_runtime(
        tmp_path,
        config={
            "request_timeout_seconds": 1,
            "max_retries": 1,
            "retry_categories": ["timeout"],
        },
        providers=[slow],
        profiles={UMO: "default"},
        defaults={"default": "p1"},
        conf_fallbacks={"default": []},
    )
    with pytest.raises((asyncio.TimeoutError, TimeoutError)):
        asyncio.run(
            runtime.guard.handle_text_chat(
                slow, (), {"prompt": "hi", "session_id": UMO}
            )
        )
    # 首次 + 1 次重试
    assert slow.call_count == 2
    assert runtime.sleep.delays, "应当发生过退避等待"


def test_timeout_not_retryable_when_excluded(tmp_path: Path) -> None:
    """把 timeout 从可重试类别里去掉 → 不重试。"""
    slow = _SlowProvider("p1")
    runtime = build_runtime(
        tmp_path,
        config={
            "request_timeout_seconds": 1,
            "max_retries": 3,
            "retry_categories": ["rate_limit"],
        },
        providers=[slow],
        profiles={UMO: "default"},
        defaults={"default": "p1"},
        conf_fallbacks={"default": []},
    )
    with pytest.raises((asyncio.TimeoutError, TimeoutError)):
        asyncio.run(
            runtime.guard.handle_text_chat(
                slow, (), {"prompt": "hi", "session_id": UMO}
            )
        )
    assert slow.call_count == 1
    assert runtime.sleep.delays == []


def test_timeout_zero_means_no_limit(tmp_path: Path) -> None:
    """``request_timeout_seconds=0`` → 不施加超时。"""
    provider = FakeProvider("p1", results=[FakeResponse("p1-ok")])
    runtime = build_runtime(
        tmp_path,
        config={"request_timeout_seconds": 0},
        providers=[provider],
        profiles={UMO: "default"},
        defaults={"default": "p1"},
        conf_fallbacks={"default": []},
    )
    response = asyncio.run(
        runtime.guard.handle_text_chat(
            provider, (), {"prompt": "hi", "session_id": UMO}
        )
    )
    assert response.completion_text == "p1-ok"


def test_stream_timeout_applies_to_first_chunk(tmp_path: Path) -> None:
    """流式：首个 chunk 也要受超时约束。"""
    slow = _SlowStreamProvider("p1")
    runtime = build_runtime(
        tmp_path,
        config={"request_timeout_seconds": 1, "max_retries": 0},
        providers=[slow],
        profiles={UMO: "default"},
        defaults={"default": "p1"},
        conf_fallbacks={"default": []},
    )

    async def _run() -> None:
        async for _chunk in runtime.guard.handle_text_chat_stream(
            slow, (), {"prompt": "hi", "session_id": UMO}
        ):
            pass

    with pytest.raises((asyncio.TimeoutError, TimeoutError)):
        asyncio.run(_run())
    assert slow.stream_call_count == 1


def test_stream_timeout_not_applied_after_first_chunk(tmp_path: Path) -> None:
    """流式：首块之后不再施加超时（长回答不会被误杀）。"""
    import time

    class _SlowTailProvider(FakeProvider):
        def __init__(self) -> None:
            super().__init__("p1")
            self.stream_call_count = 0

        async def _stream(self, kwargs):  # type: ignore[override]
            self.stream_call_count += 1
            yield FakeResponse("first")
            # 首块之后的慢：不应被超时打断
            await asyncio.sleep(0.3)
            yield FakeResponse("second")

        def text_chat_stream(self, **kwargs):  # type: ignore[override]
            return self._stream(kwargs)

    provider = _SlowTailProvider()
    runtime = build_runtime(
        tmp_path,
        # 超时 1 秒 > 尾部延迟 0.3 秒：若超时错误地施加到后续块，
        # 这里不会失败；真正的判据是「尾部延迟被完整等待」。
        config={"request_timeout_seconds": 1, "max_retries": 0},
        providers=[provider],
        profiles={UMO: "default"},
        defaults={"default": "p1"},
        conf_fallbacks={"default": []},
    )
    started = time.monotonic()

    async def _collect() -> list[str]:
        texts: list[str] = []
        async for chunk in runtime.guard.handle_text_chat_stream(
            provider, (), {"prompt": "hi", "session_id": UMO}
        ):
            texts.append(chunk.completion_text)
        return texts

    texts = asyncio.run(_collect())
    assert texts == ["first", "second"]
    # 尾部那 0.3 秒被完整等待了（没被超时打断，也没被提前取消）。
    # 留一点余量：asyncio.sleep 的定时器唤醒可能略早于设定值。
    assert time.monotonic() - started >= 0.25


def test_stream_timeout_after_first_chunk_is_not_applied(tmp_path: Path) -> None:
    """首块之后即使超过超时时长也不打断（长回答不会被误杀）。

    用一个**明显超过超时**的尾部延迟：若超时错误地施加到后续块，
    这里会抛 TimeoutError；正确实现应当正常收完。
    """

    class _VerySlowTailProvider(FakeProvider):
        def __init__(self) -> None:
            super().__init__("p1")
            self.stream_call_count = 0

        async def _stream(self, kwargs):  # type: ignore[override]
            self.stream_call_count += 1
            yield FakeResponse("first")
            await asyncio.sleep(1.5)  # 明显超过 1 秒超时
            yield FakeResponse("second")

        def text_chat_stream(self, **kwargs):  # type: ignore[override]
            return self._stream(kwargs)

    provider = _VerySlowTailProvider()
    runtime = build_runtime(
        tmp_path,
        config={"request_timeout_seconds": 1, "max_retries": 0},
        providers=[provider],
        profiles={UMO: "default"},
        defaults={"default": "p1"},
        conf_fallbacks={"default": []},
    )

    async def _collect() -> list[str]:
        texts: list[str] = []
        async for chunk in runtime.guard.handle_text_chat_stream(
            provider, (), {"prompt": "hi", "session_id": UMO}
        ):
            texts.append(chunk.completion_text)
        return texts

    texts = asyncio.run(_collect())
    assert texts == ["first", "second"]

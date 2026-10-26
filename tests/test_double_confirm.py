"""双击确认与测活单测：两次成功才解除、失败回退、超时与旁路。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from astrbot_plugin_llm_model_guard.core.guardian import PROBE_CONTEXT
from astrbot_plugin_llm_model_guard.core.models import MarkState

from .support import FakeProvider, FakeResponse, build_runtime


def run(coro):
    return asyncio.run(coro)


class NoTimeoutTestProvider(FakeProvider):
    """只有 ``test()``（不带 timeout 形参）的 provider。"""

    def __init__(self, provider_id: str, **kwargs) -> None:
        super().__init__(provider_id, **kwargs)
        self.test_calls = 0

    async def test(self) -> None:
        self.test_calls += 1
        await super().text_chat(prompt="probe")


class TimeoutTestProvider(FakeProvider):
    """``test(timeout=...)`` 风格的 provider。"""

    def __init__(self, provider_id: str, **kwargs) -> None:
        super().__init__(provider_id, **kwargs)
        self.timeouts: list[float] = []

    async def test(self, timeout: float = 45.0) -> None:
        self.timeouts.append(timeout)
        await super().text_chat(prompt="probe")


def test_confirm_then_unmark(tmp_path: Path) -> None:
    provider = FakeProvider("p1")
    runtime = build_runtime(
        tmp_path,
        config={"double_confirm": True, "confirm_delay_seconds": 30},
        providers=[provider],
    )
    run(runtime.guardian.mark("p1", reason="auth", ttl=0))
    assert runtime.guardian.marks()["p1"].state is MarkState.MARKED

    first = run(runtime.guardian.probe_now("p1"))
    assert first.ok is True
    assert first.state_after == "confirming"
    view = runtime.guardian.marks()["p1"]
    assert view.state is MarkState.CONFIRMING
    assert view.first_success_at is not None
    assert view.next_probe_at == view.first_success_at + 30
    # 确认期间仍然视为被标记（仍被绕过）
    assert runtime.guardian.is_marked("p1") is True

    runtime.clock.advance(31)
    second = run(runtime.guardian.probe_now("p1"))
    assert second.ok is True
    assert second.state_after == "healthy"
    assert runtime.guardian.has_marked() is False


def test_confirm_fail_returns_marked(tmp_path: Path) -> None:
    provider = FakeProvider("p1", results=[FakeResponse("pong"), RuntimeError("boom")])
    runtime = build_runtime(
        tmp_path,
        config={"double_confirm": True, "confirm_delay_seconds": 10, "probe_interval_seconds": 60},
        providers=[provider],
    )
    run(runtime.guardian.mark("p1", reason="auth", ttl=0))
    assert run(runtime.guardian.probe_now("p1")).ok is True
    assert runtime.guardian.marks()["p1"].state is MarkState.CONFIRMING

    runtime.clock.advance(11)
    failed = run(runtime.guardian.probe_now("p1"))
    assert failed.ok is False
    view = runtime.guardian.marks()["p1"]
    assert view.state is MarkState.MARKED
    assert view.first_success_at is None
    assert view.consecutive_probe_failures == 1
    assert view.last_probe_error
    # 失败后 next_probe_at 被退避到 probe_interval * 失败次数
    assert view.next_probe_at == runtime.clock.now + 60


def test_double_confirm_disabled(tmp_path: Path) -> None:
    provider = FakeProvider("p1")
    runtime = build_runtime(
        tmp_path,
        config={"double_confirm": False},
        providers=[provider],
    )
    run(runtime.guardian.mark("p1", reason="auth", ttl=0))
    result = run(runtime.guardian.probe_now("p1"))
    assert result.ok is True
    assert result.state_after == "healthy"
    assert runtime.guardian.has_marked() is False


def test_probe_failure_backoff(tmp_path: Path) -> None:
    provider = FakeProvider("p1", results=[RuntimeError("down")])
    runtime = build_runtime(
        tmp_path,
        config={"probe_interval_seconds": 60},
        providers=[provider],
    )
    run(runtime.guardian.mark("p1", reason="auth", ttl=0))
    for attempt in range(1, 4):
        runtime.clock.advance(10_000)
        run(runtime.guardian.probe_now("p1"))
        view = runtime.guardian.marks()["p1"]
        assert view.consecutive_probe_failures == attempt
        assert view.next_probe_at == runtime.clock.now + 60 * attempt


def test_probe_timeout_effective(tmp_path: Path) -> None:
    provider = FakeProvider("p1", hang=True)
    runtime = build_runtime(tmp_path, providers=[provider])
    run(runtime.guardian.mark("p1", reason="auth", ttl=0))
    result = run(runtime.guardian.probe_now("p1", timeout=0.05))
    assert result.ok is False
    assert result.timed_out is True
    assert result.error_class == "timeout"


def test_probe_test_with_timeout_signature(tmp_path: Path) -> None:
    provider = TimeoutTestProvider("p1")
    runtime = build_runtime(tmp_path, providers=[provider])
    run(runtime.guardian.mark("p1", reason="auth", ttl=0))
    result = run(runtime.guardian.probe_now("p1", timeout=3.0))
    assert result.ok is True
    assert result.mode == "test_timeout"
    assert provider.timeouts == [3.0]


def test_probe_test_without_timeout_signature(tmp_path: Path) -> None:
    provider = NoTimeoutTestProvider("p1")
    runtime = build_runtime(tmp_path, providers=[provider])
    run(runtime.guardian.mark("p1", reason="auth", ttl=0))
    result = run(runtime.guardian.probe_now("p1", timeout=3.0))
    assert result.ok is True
    assert result.mode == "test"
    assert provider.test_calls == 1


def test_probe_bypasses_instance_wrapper(tmp_path: Path) -> None:
    provider = FakeProvider("p1")
    runtime = build_runtime(tmp_path, providers=[provider])
    assert runtime.guard.ensure_wrapped_all() == 1
    assert runtime.guard.wrapped_count == 1

    async def _boom(**_: object) -> object:
        raise AssertionError("测活不应该经过实例包装层")

    provider.text_chat = _boom  # type: ignore[assignment]
    result = run(runtime.guardian.probe_now("p1", timeout=1.0))
    assert result.ok is True
    assert result.mode == "text_chat"
    assert provider.call_count == 1


def test_probe_never_marks(tmp_path: Path) -> None:
    provider = FakeProvider("p1", results=[RuntimeError("still broken")])
    runtime = build_runtime(tmp_path, providers=[provider])
    result = run(runtime.guardian.probe_now("p1"))
    assert result.ok is False
    assert runtime.guardian.has_marked() is False


class ProbeContextProvider(FakeProvider):
    """在类实现里记录旁路标记，用来验证测活确实设置了 ``PROBE_CONTEXT``。"""

    def __init__(self, provider_id: str, **kwargs) -> None:
        super().__init__(provider_id, **kwargs)
        self.observed_depth: list[int] = []

    async def text_chat(self, **kwargs):  # type: ignore[override]
        self.observed_depth.append(PROBE_CONTEXT.get())
        return FakeResponse("pong")


def test_probe_uses_probe_context(tmp_path: Path) -> None:
    provider = ProbeContextProvider("p1")
    runtime = build_runtime(tmp_path, providers=[provider])
    run(runtime.guardian.mark("p1", reason="auth", ttl=0))
    result = run(runtime.guardian.probe_now("p1", timeout=1.0))
    assert result.ok is True
    assert provider.observed_depth and provider.observed_depth[0] >= 1


def test_probe_missing_provider(tmp_path: Path) -> None:
    runtime = build_runtime(tmp_path, providers=[])
    result = run(runtime.guardian.probe_now("nope"))
    assert result.ok is False
    assert result.mode == "missing"
    assert result.error_class == "model_not_found"


# ---------------------------------------------------------------------------
# 第 2 轮修复：P1-1（profile 作用域闭环）与 P3-15（继承来的 test 也要识别）
# ---------------------------------------------------------------------------


def test_probe_clears_profile_scoped_mark(tmp_path: Path) -> None:
    """``mark_scope="profile"`` 下自动标记 → 双击确认 → 解除，闭环必须成立（P1-1）。"""
    provider = FakeProvider("p1")
    runtime = build_runtime(
        tmp_path,
        config={
            "mark_scope": "profile",
            "double_confirm": True,
            "confirm_delay_seconds": 10,
        },
        providers=[provider],
        profiles={"u": "default"},
    )
    run(runtime.guardian.mark("p1", reason="auth", ttl=0, profile_hint="default"))
    assert runtime.guardian.is_marked("p1", profile_id="default") is True

    first = run(runtime.guardian.probe_now("p1", timeout=1.0))
    assert first.ok is True
    assert first.state_after == "confirming"
    assert runtime.guardian.is_marked("p1", profile_id="default") is True

    runtime.clock.advance(11)
    second = run(runtime.guardian.probe_now("p1", timeout=1.0))
    assert second.ok is True
    assert second.state_after == "healthy"
    assert runtime.guardian.is_marked("p1", profile_id="default") is False
    assert runtime.guardian.has_marked() is False


def test_probe_failure_resets_profile_scoped_mark(tmp_path: Path) -> None:
    provider = FakeProvider("p1", results=[RuntimeError("down")])
    runtime = build_runtime(
        tmp_path,
        config={"mark_scope": "profile", "probe_interval_seconds": 60},
        providers=[provider],
    )
    run(runtime.guardian.mark("p1", reason="auth", ttl=0, profile_hint="uuid-x"))
    result = run(runtime.guardian.probe_now("p1", timeout=1.0))
    assert result.ok is False
    view = runtime.guardian.all_marks()["uuid-x::p1"]
    assert view.state is MarkState.MARKED
    assert view.consecutive_probe_failures == 1


class InheritedTestProvider(FakeProvider):
    """``test`` 定义在中间父类里：旧实现会漏判，P3-15 要能识别。"""

    async def test(self) -> None:
        self.inherited_calls += 1
        await super().text_chat(prompt="probe")


class MiddleProvider(InheritedTestProvider):
    def __init__(self, provider_id: str, **kwargs) -> None:
        super().__init__(provider_id, **kwargs)
        self.inherited_calls = 0


def test_probe_uses_inherited_custom_test(tmp_path: Path) -> None:
    provider = MiddleProvider("p1")
    runtime = build_runtime(tmp_path, providers=[provider])
    run(runtime.guardian.mark("p1", reason="auth", ttl=0))
    result = run(runtime.guardian.probe_now("p1", timeout=1.0))
    assert result.ok is True
    assert result.mode == "test"
    assert provider.inherited_calls == 1

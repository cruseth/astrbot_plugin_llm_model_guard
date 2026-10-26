"""标记状态机单测（v0.2.0）：全局标记、无 TTL、连续成功 N 次解除。

v0.2.0 相对 v0.1.x 的变化：

* **没有 TTL**：标记不会自己过期，只有连续验证成功才解除；
* **没有 mark_scope**：标记统一按 provider 全局生效；
* **没有双击确认态**：恢复判据是「连续成功 N 次」（``probe_success_threshold``）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from astrbot_plugin_llm_model_guard.core.models import AttemptRecord, MarkSource, MarkState

from .support import FakeProvider, FakeResponse, StatusError, build_runtime


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 标记
# ---------------------------------------------------------------------------


def test_mark_records_reason_and_no_ttl(tmp_path: Path) -> None:
    runtime = build_runtime(tmp_path, providers=[FakeProvider("p1")])
    marked = run(
        runtime.guardian.mark(
            "p1", reason="rate_limit", source=MarkSource.AUTO, detail="429"
        )
    )
    assert marked is True
    view = runtime.guardian.marks()["p1"]
    assert view.state is MarkState.MARKED
    assert view.reason == "rate_limit"
    # v0.2.0 没有 TTL：标记不会自己过期。
    assert not hasattr(view, "expires_at")
    assert view.next_probe_at == view.marked_at
    assert runtime.guardian.is_marked("p1") is True
    assert runtime.guardian.has_marked() is True


def test_mark_renewal_resets_success_counter(tmp_path: Path) -> None:
    """重新标记会把连续成功计数归零（要重新累计）。"""
    runtime = build_runtime(tmp_path, providers=[FakeProvider("p1")])
    run(runtime.guardian.mark("p1", reason="auth"))
    runtime.guardian._marks["p1"].consecutive_success = 1
    run(runtime.guardian.mark("p1", reason="auth"))
    assert runtime.guardian.marks()["p1"].consecutive_success == 0
    assert runtime.guardian.marks()["p1"].mark_count == 2


def test_manual_unmark(tmp_path: Path) -> None:
    runtime = build_runtime(tmp_path, providers=[FakeProvider("p1")])
    run(runtime.guardian.mark("p1", reason="auth"))
    assert run(runtime.guardian.unmark("p1", by="command")) is True
    assert runtime.guardian.has_marked() is False
    assert run(runtime.guardian.unmark("p1", by="command")) is False


def test_snapshot_is_immutable_view(tmp_path: Path) -> None:
    runtime = build_runtime(tmp_path, providers=[FakeProvider("p1")])
    snapshot_before = runtime.guardian.marks()
    run(runtime.guardian.mark("p1", reason="auth"))
    assert "p1" not in snapshot_before
    snapshot_after = runtime.guardian.marks()
    assert "p1" in snapshot_after
    try:
        snapshot_after["p1"].reason = "changed"  # type: ignore[misc]
    except Exception as exc:
        assert "cannot assign" in str(exc).lower() or "frozen" in str(exc).lower()
    else:  # pragma: no cover - 不应走到这里
        raise AssertionError("MarkView 应该是不可变的")


def test_marks_are_global_not_per_profile(tmp_path: Path) -> None:
    """v0.2.0：标记统一按 provider 全局生效，与 profile 无关。"""
    runtime = build_runtime(
        tmp_path,
        providers=[FakeProvider("p1")],
    )
    run(runtime.guardian.mark("p1", reason="auth"))
    assert runtime.guardian.is_marked("p1") is True
    assert runtime.guardian.is_marked("p1", profile_id="default") is True
    assert runtime.guardian.is_marked("p1", profile_id="other") is True
    state = runtime.guardian.export_state()
    assert "p1" in state["marks"]
    assert "marks_by_profile" not in state


def test_state_roundtrip(tmp_path: Path) -> None:
    runtime = build_runtime(tmp_path, providers=[FakeProvider("p1")])
    run(runtime.guardian.mark("p1", reason="auth", detail="401"))
    exported = runtime.guardian.export_state()

    fresh = build_runtime(tmp_path / "fresh", providers=[FakeProvider("p1")])
    fresh.guardian.load_state(exported)
    assert fresh.guardian.is_marked("p1") is True
    assert fresh.guardian.marks()["p1"].reason == "auth"


def test_marking_disabled_when_plugin_disabled(tmp_path: Path) -> None:
    """总开关关闭时不写标记。"""
    runtime = build_runtime(
        tmp_path,
        config={"enable": False},
        providers=[FakeProvider("p1")],
    )
    assert run(runtime.guardian.mark("p1", reason="auth")) is False
    assert runtime.guardian.has_marked() is False


# ---------------------------------------------------------------------------
# 验证与恢复（连续成功 N 次）
# ---------------------------------------------------------------------------


def test_probe_success_resets_counter_on_failure(tmp_path: Path) -> None:
    """验证失败 → 连续成功计数归零，继续等待。"""

    class _FlakyProvider(FakeProvider):
        """第一次成功，之后一直失败。"""

        def __init__(self) -> None:
            super().__init__("p1")
            self.calls_count = 0

        async def text_chat(self, **kwargs):  # type: ignore[override]
            self.call_count += 1
            self.calls.append(dict(kwargs))
            if self.call_count == 1:
                return FakeResponse("ok")
            raise StatusError("still down", 500)

    provider = _FlakyProvider()
    runtime = build_runtime(
        tmp_path,
        config={"probe_success_threshold": 2},
        providers=[provider],
    )
    run(runtime.guardian.mark("p1", reason="server_error"))
    # 先成功一次
    run(runtime.guardian.probe_now("p1"))
    assert runtime.guardian.marks()["p1"].consecutive_success == 1
    # 再失败一次 → 归零（仍未达到阈值，标记保留）
    run(runtime.guardian.probe_now("p1"))
    assert runtime.guardian.marks()["p1"].consecutive_success == 0
    assert runtime.guardian.is_marked("p1") is True


def test_probe_unmarks_after_threshold_consecutive_successes(tmp_path: Path) -> None:
    """连续成功达到阈值 → 解除标记。"""
    provider = FakeProvider("p1", results=[None])
    runtime = build_runtime(
        tmp_path,
        config={"probe_success_threshold": 2},
        providers=[provider],
    )
    run(runtime.guardian.mark("p1", reason="server_error"))

    first = run(runtime.guardian.probe_now("p1"))
    assert first.ok is True
    assert first.state_after == "confirming:1/2"
    assert runtime.guardian.is_marked("p1") is True

    second = run(runtime.guardian.probe_now("p1"))
    assert second.state_after == "healthy"
    assert runtime.guardian.is_marked("p1") is False
    assert runtime.guardian.has_marked() is False


def test_probe_threshold_one_unmarks_immediately(tmp_path: Path) -> None:
    """阈值 = 1 时一次成功即解除。"""
    provider = FakeProvider("p1", results=[None])
    runtime = build_runtime(
        tmp_path,
        config={"probe_success_threshold": 1},
        providers=[provider],
    )
    run(runtime.guardian.mark("p1", reason="server_error"))
    result = run(runtime.guardian.probe_now("p1"))
    assert result.state_after == "healthy"
    assert runtime.guardian.is_marked("p1") is False


def test_probe_bypasses_wrapper(tmp_path: Path) -> None:
    """验证走类级实现，绕过包装层（不会「验证 → 标记 → 再验证」递归）。"""
    provider = FakeProvider("p1", results=[None])
    runtime = build_runtime(tmp_path, providers=[provider])
    run(runtime.guardian.mark("p1", reason="server_error"))
    result = run(runtime.guardian.probe_now("p1"))
    assert result.ok is True
    assert result.mode in ("text_chat", "test")


def test_tick_dispatches_due_probe(tmp_path: Path) -> None:
    """supervisor 会给到期的模型派发验证。

    注意：``mark()`` 现在会启动 supervisor（任务包 16 的 P1 修复），
    所以验证可能由 supervisor 自己派发；这里断言「确实发出了验证请求」，
    而不是「由哪一方派发」。
    """

    async def _scenario() -> None:
        provider = FakeProvider("p1", results=[None])
        runtime = build_runtime(tmp_path, providers=[provider])
        await runtime.guardian.mark("p1", reason="server_error")

        for _ in range(50):
            await asyncio.sleep(0.01)
            if provider.probe_call_count >= 1:
                break
        assert provider.probe_call_count >= 1, "应当发出过一次验证请求"
        # 默认阈值 2：首次成功还不够，仍算被标记
        assert runtime.guardian.is_marked("p1") is True
        await runtime.guardian.shutdown()

    run(_scenario())


def test_tick_respects_probe_interval(tmp_path: Path) -> None:
    """验证过一次后，没到间隔就不再派发。

    这里**不启动 supervisor**（直接操作标记块），以便精确断言间隔行为 ——
    supervisor 抢跑会让「谁派发的」变得不确定。
    """

    async def _scenario() -> None:
        provider = FakeProvider("p1", results=[None])
        runtime = build_runtime(
            tmp_path,
            config={"probe_interval_seconds": 60},
            providers=[provider],
        )
        await runtime.guardian.mark("p1", reason="server_error")
        # 停掉 supervisor，只手动驱动 tick。
        await runtime.guardian.shutdown()
        runtime.guardian._stopping = False
        runtime.guardian._supervisor_task = None

        # 第一次验证成功 → 连续成功计数 1，下次验证排到 60 秒后
        await runtime.guardian.probe_now("p1")
        runtime.clock.advance(10)
        assert await runtime.guardian.tick() == []
        runtime.clock.advance(60)
        assert await runtime.guardian.tick() == ["p1"]

    run(_scenario())


def test_supervisor_not_started_without_marks(tmp_path: Path) -> None:
    """没有被标记的模型时不必起 supervisor。"""
    runtime = build_runtime(tmp_path, providers=[FakeProvider("p1")])

    async def _start() -> bool:
        await runtime.guardian.ensure_started()
        running = runtime.guardian.supervisor_running
        await runtime.guardian.shutdown()
        return running

    assert run(_start()) is False


# ---------------------------------------------------------------------------
# 落盘
# ---------------------------------------------------------------------------


def test_flush_without_stats_when_disabled(tmp_path: Path) -> None:
    """总开关关闭时 terminate 只写既有标记，不写统计。"""
    runtime = build_runtime(
        tmp_path,
        config={"enable": False},
        providers=[FakeProvider("p1")],
    )
    runtime.guardian.load_state(
        {
            "schema_version": 2,
            "marks": {"p1": {"state": "marked", "reason": "auth"}},
        }
    )
    runtime.metrics.record_attempt(
        AttemptRecord(ts=runtime.clock.now, used="p1", ok=True, total_ms=10)
    )
    assert run(runtime.guardian.flush(include_stats=False)) is True
    data = json.loads(runtime.store.path.read_text(encoding="utf-8"))
    assert "p1" in data["marks"]
    assert data["providers"] == {}
    assert data["recent_events"] == []
    assert data["schema_version"] == 2

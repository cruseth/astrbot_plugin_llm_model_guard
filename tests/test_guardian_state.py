"""标记状态机单测：TTL、手动解除、抖动冷却、连续失败阈值与作用域。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from astrbot_plugin_llm_model_guard.core.models import MarkSource, MarkState

from .support import FakeProvider, build_runtime


def run(coro):
    return asyncio.run(coro)


def test_mark_sets_ttl(tmp_path: Path) -> None:
    runtime = build_runtime(tmp_path, providers=[FakeProvider("p1")])
    marked = run(
        runtime.guardian.mark(
            "p1", reason="rate_limit", ttl=300, source=MarkSource.AUTO, detail="429"
        )
    )
    assert marked is True
    view = runtime.guardian.marks()["p1"]
    assert view.state is MarkState.MARKED
    assert view.reason == "rate_limit"
    assert view.expires_at == view.marked_at + 300
    assert view.next_probe_at == view.marked_at
    assert runtime.guardian.is_marked("p1") is True
    assert runtime.guardian.has_marked() is True


def test_permanent_mark_has_no_expiry(tmp_path: Path) -> None:
    runtime = build_runtime(tmp_path, providers=[FakeProvider("p1")])
    run(runtime.guardian.mark("p1", reason="auth", ttl=0))
    assert runtime.guardian.marks()["p1"].expires_at is None


def test_ttl_expiry_by_supervisor(tmp_path: Path) -> None:
    runtime = build_runtime(tmp_path, providers=[FakeProvider("p1")])
    run(runtime.guardian.mark("p1", reason="rate_limit", ttl=60))
    runtime.clock.advance(30)
    assert run(runtime.guardian.expire_ttl()) == []
    assert runtime.guardian.is_marked("p1") is True

    runtime.clock.advance(31)
    assert run(runtime.guardian.expire_ttl()) == ["p1"]
    assert runtime.guardian.is_marked("p1") is False
    assert runtime.guardian.has_marked() is False


def test_tick_dispatches_due_probe(tmp_path: Path) -> None:
    provider = FakeProvider("p1")
    runtime = build_runtime(tmp_path, providers=[provider])
    run(runtime.guardian.mark("p1", reason="rate_limit", ttl=0))

    async def _tick_once() -> list[str]:
        dispatched = await runtime.guardian.tick()
        for _ in range(20):
            await asyncio.sleep(0.01)
        return dispatched

    assert run(_tick_once()) == ["p1"]
    assert provider.call_count == 1
    # 默认双击确认：首次测活成功后进入 confirming，仍算被标记
    assert runtime.guardian.is_marked("p1") is True


def test_tick_skips_busy_provider(tmp_path: Path) -> None:
    runtime = build_runtime(tmp_path, providers=[FakeProvider("p1")])
    run(runtime.guardian.mark("p1", reason="rate_limit", ttl=0))
    runtime.guardian.inc_active("p1")
    assert run(runtime.guardian.tick()) == []
    runtime.guardian.dec_active("p1")
    assert run(runtime.guardian.tick()) == ["p1"]
    # 清理 tick 里派发的任务，避免测试结束时留下未完成的协程
    run(runtime.guardian.shutdown())


def test_manual_unmark(tmp_path: Path) -> None:
    runtime = build_runtime(tmp_path, providers=[FakeProvider("p1")])
    run(runtime.guardian.mark("p1", reason="auth", ttl=0))
    assert run(runtime.guardian.unmark("p1", by="command")) is True
    assert runtime.guardian.has_marked() is False
    # 再解一次返回 False（没有标记可解）
    assert run(runtime.guardian.unmark("p1", by="command")) is False


def test_mark_renewal_keeps_marked_at(tmp_path: Path) -> None:
    runtime = build_runtime(tmp_path, providers=[FakeProvider("p1")])
    run(runtime.guardian.mark("p1", reason="auth", ttl=0))
    first = runtime.guardian.marks()["p1"]
    runtime.clock.advance(10)
    run(runtime.guardian.mark("p1", reason="auth", ttl=0))
    second = runtime.guardian.marks()["p1"]
    assert second.marked_at == first.marked_at
    assert second.mark_count == 2


def test_flapping_cooldown(tmp_path: Path) -> None:
    runtime = build_runtime(tmp_path, providers=[FakeProvider("p1")])
    run(runtime.guardian.mark("p1", reason="network", ttl=0))
    first_marked_at = runtime.guardian.marks()["p1"].marked_at
    run(runtime.guardian.unmark("p1", by="command"))

    runtime.clock.advance(5)
    run(runtime.guardian.mark("p1", reason="network", ttl=0))
    view = runtime.guardian.marks()["p1"]
    assert view.marked_at == first_marked_at
    assert runtime.metrics.counters.get("flapping") == 1


def test_mark_delay_requires_consecutive_failures(tmp_path: Path) -> None:
    runtime = build_runtime(tmp_path, providers=[FakeProvider("p1")])
    assert run(runtime.guardian.mark("p1", reason="empty_output", ttl=120, delay=2)) is False
    assert runtime.guardian.has_marked() is False
    assert run(runtime.guardian.mark("p1", reason="empty_output", ttl=120, delay=2)) is True
    assert runtime.guardian.is_marked("p1") is True


def test_success_clears_failure_streak(tmp_path: Path) -> None:
    runtime = build_runtime(tmp_path, providers=[FakeProvider("p1")])
    run(runtime.guardian.mark("p1", reason="empty_output", ttl=120, delay=2))
    runtime.guardian.note_success("p1")
    assert run(runtime.guardian.mark("p1", reason="empty_output", ttl=120, delay=2)) is False


def test_snapshot_is_immutable_view(tmp_path: Path) -> None:
    runtime = build_runtime(tmp_path, providers=[FakeProvider("p1")])
    snapshot_before = runtime.guardian.marks()
    run(runtime.guardian.mark("p1", reason="auth", ttl=0))
    assert "p1" not in snapshot_before
    snapshot_after = runtime.guardian.marks()
    assert "p1" in snapshot_after
    # MarkView 是 frozen dataclass：不能被就地改写
    try:
        snapshot_after["p1"].reason = "changed"  # type: ignore[misc]
    except Exception as exc:
        assert "cannot assign" in str(exc).lower() or "frozen" in str(exc).lower()
    else:  # pragma: no cover - 不应走到这里
        raise AssertionError("MarkView 应该是不可变的")


def test_mark_scope_profile_key(tmp_path: Path) -> None:
    runtime = build_runtime(
        tmp_path,
        config={"mark_scope": "profile"},
        providers=[FakeProvider("p1")],
    )
    run(runtime.guardian.mark("p1", reason="auth", ttl=0, profile_hint="default"))
    assert runtime.guardian.marks() == {}
    assert "p1" in runtime.guardian.marks_for("default")
    assert "p1" not in runtime.guardian.marks_for("other-profile")
    assert runtime.guardian.is_marked("p1", profile_id="default") is True

    state = runtime.guardian.export_state()
    assert "default::p1" in state["marks_by_profile"]


def test_state_roundtrip(tmp_path: Path) -> None:
    runtime = build_runtime(tmp_path, providers=[FakeProvider("p1")])
    run(runtime.guardian.mark("p1", reason="auth", ttl=0, detail="401"))
    exported = runtime.guardian.export_state()

    fresh = build_runtime(tmp_path / "fresh", providers=[FakeProvider("p1")])
    fresh.guardian.load_state(exported)
    assert fresh.guardian.is_marked("p1") is True
    assert fresh.guardian.marks()["p1"].reason == "auth"


def test_marking_disabled_by_config(tmp_path: Path) -> None:
    runtime = build_runtime(
        tmp_path,
        config={"enable_marking": False},
        providers=[FakeProvider("p1")],
    )
    assert run(runtime.guardian.mark("p1", reason="auth", ttl=0)) is False
    assert runtime.guardian.has_marked() is False


# ---------------------------------------------------------------------------
# 第 2 轮修复：P1-1 / P2-9 / P2-11
# ---------------------------------------------------------------------------


def test_switch_scope_keeps_bypass(tmp_path: Path) -> None:
    """global → profile 切换后，既有全局标记仍然生效（P1-1）。"""
    runtime = build_runtime(
        tmp_path,
        config={"mark_scope": "global"},
        providers=[FakeProvider("p1")],
    )
    run(runtime.guardian.mark("p1", reason="auth", ttl=0))
    assert runtime.guardian.is_marked("p1") is True

    runtime.config["mark_scope"] = "profile"
    assert runtime.guardian.is_marked("p1", profile_id="default") is True
    assert "p1" in runtime.guardian.marks_for("default")

    # profile 模式写入的标记，切回 global 后仍对该 profile 生效
    run(runtime.guardian.mark("p2", reason="auth", ttl=0, profile_hint="uuid-x"))
    runtime.config["mark_scope"] = "global"
    assert runtime.guardian.is_marked("p2", profile_id="uuid-x") is True
    assert runtime.guardian.is_marked("p2", profile_id="other") is False


def test_profile_marks_listed_in_all_marks(tmp_path: Path) -> None:
    runtime = build_runtime(
        tmp_path,
        config={"mark_scope": "profile"},
        providers=[FakeProvider("p1"), FakeProvider("p2")],
    )
    run(runtime.guardian.mark("p1", reason="auth", ttl=0, profile_hint="default"))
    run(runtime.guardian.mark("p2", reason="rate_limit", ttl=60))

    all_marks = runtime.guardian.all_marks()
    keys = set(all_marks)
    assert "default::p1" in keys
    # profile 模式下未指定 profile 的手动标记落在通配作用域 "*"（对所有配置文件生效）
    assert "*::p2" in keys
    assert set(runtime.guardian.mark_index()) == {"p1", "p2"}
    snapshot = runtime.guardian.snapshot()
    assert len(snapshot["marked"]) == 2


def test_ttl_expires_when_probe_disabled(tmp_path: Path) -> None:
    """enable_probe=false 时 TTL 仍然会到期解除（P2-9）。"""
    runtime = build_runtime(
        tmp_path,
        config={"enable_probe": False, "mark_ttl_network": 5},
        providers=[FakeProvider("p1")],
    )
    run(runtime.guardian.mark("p1", reason="network", ttl=5))
    assert runtime.guardian.is_marked("p1") is True

    runtime.clock.advance(60)
    assert run(runtime.guardian.tick()) == []
    assert runtime.guardian.is_marked("p1") is False


def test_supervisor_starts_for_ttl_only_when_probe_disabled(tmp_path: Path) -> None:
    runtime = build_runtime(
        tmp_path,
        config={"enable_probe": False},
        providers=[FakeProvider("p1")],
    )

    async def _start() -> bool:
        await runtime.guardian.ensure_started()
        running = runtime.guardian.supervisor_running
        await runtime.guardian.shutdown()
        return running

    # 没有会过期的标记：不必起 supervisor
    assert run(_start()) is False

    run(runtime.guardian.mark("p1", reason="rate_limit", ttl=60))
    # 上一段 shutdown 已置位停止标志，这里重置后再验证
    runtime.guardian._stopping = False
    runtime.guardian._supervisor_task = None
    assert run(_start()) is True


def test_stats_retention_days_from_config(tmp_path: Path) -> None:
    """``stats_retention_days`` 真正接到清理逻辑（P2-11）。"""
    from astrbot_plugin_llm_model_guard.core.models import AttemptRecord

    runtime = build_runtime(
        tmp_path,
        config={"stats_retention_days": 1, "enable_probe": False},
        providers=[FakeProvider("p1")],
    )
    runtime.metrics.record_attempt(
        AttemptRecord(ts=runtime.clock.now, used="ghost", ok=True, total_ms=10)
    )
    runtime.clock.advance(2 * 86400)
    run(runtime.guardian.tick())
    assert "ghost" not in runtime.metrics.per_provider()

    # 0 表示永久保留
    keep_runtime = build_runtime(
        tmp_path / "keep",
        config={"stats_retention_days": 0, "enable_probe": False},
        providers=[FakeProvider("p1")],
    )
    keep_runtime.metrics.record_attempt(
        AttemptRecord(ts=keep_runtime.clock.now, used="ghost", ok=True, total_ms=10)
    )
    keep_runtime.clock.advance(30 * 86400)
    run(keep_runtime.guardian.tick())
    assert "ghost" in keep_runtime.metrics.per_provider()


def test_flush_without_stats_when_disabled(tmp_path: Path) -> None:
    """总开关关闭时 terminate 只写既有标记，不写统计（P3-11）。"""
    import json

    from astrbot_plugin_llm_model_guard.core.models import AttemptRecord

    runtime = build_runtime(
        tmp_path,
        config={"enable": False},
        providers=[FakeProvider("p1")],
    )
    runtime.guardian.load_state(
        {
            "schema_version": 1,
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

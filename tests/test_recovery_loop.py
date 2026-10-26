"""端到端回归（任务包 16）：自动标记 → 验证 → 解除 → 回到链路候选。

**这是 v0.2.0 的一处 P1 修复的回归测试**：

冷启动时通常还没有被标记的模型，``main.py::_bootstrap()`` 里的那次
``ensure_started()`` 会直接返回；而自动标记只走 ``Guardian.mark()`` 这条路。
如果 ``mark()`` 里不启动 supervisor，就再也没人调用它了 ——
「定时验证 → 失败继续等待 → 连续成功 N 次解除标记 → 路由顺序调回」
整段闭环都会失效，坏模型被**永久绕过**。

本文件覆盖整条闭环：

1. 冷启动无标记 → 包装层遇到 401 → 自动标记 → **supervisor 必须已启动**；
2. 验证失败 → 连续成功计数归零 → 继续等待；
3. 连续成功达阈值 → 标记解除 → 该模型**重新出现在链路候选里**。

注意：supervisor 任务与调用方必须**在同一个事件循环**里，所以每个用例都把
整个流程放进一次 ``asyncio.run()``。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from .support import FakeProvider, FakeResponse, StatusError, build_runtime

UMO = "aiocqhttp:FriendMessage:50005"


def _runtime(tmp_path: Path, **kwargs):
    """p1 是默认模型（会 401），p2 是回退模型（成功）。"""
    p1 = FakeProvider("p1", results=[StatusError("invalid api key", 401)])
    p2 = FakeProvider("p2", results=[FakeResponse("p2-ok")])
    runtime = build_runtime(
        tmp_path,
        config=kwargs.pop("config", {}),
        providers=[p1, p2],
        profiles={UMO: "default"},
        defaults={"default": "p1"},
        conf_fallbacks={"default": ["p2"]},
        **kwargs,
    )
    return runtime, p1, p2


# ---------------------------------------------------------------------------
# 1. P1 回归：自动标记后 supervisor 必须启动
# ---------------------------------------------------------------------------


def test_auto_mark_starts_supervisor(tmp_path: Path) -> None:
    """冷启动无标记 → 401 自动标记 → supervisor_running 必须为 True。

    这是任务包 16 的 P1 核心断言。
    """

    async def _scenario() -> None:
        runtime, p1, _ = _runtime(tmp_path)

        # 冷启动：没有标记，supervisor 不该在跑
        assert runtime.guardian.all_marks() == {}
        assert runtime.guardian.supervisor_running is False

        # 模拟插件 bootstrap：此时仍无标记，那次 ensure_started() 必然直接返回
        await runtime.guardian.ensure_started()
        assert runtime.guardian.supervisor_running is False

        # 经包装层触发真实的自动标记（401 → 不在可重试类别 → 直接回退 + 标记）
        response = await runtime.guard.handle_text_chat(
            p1, (), {"prompt": "hi", "session_id": UMO}
        )
        assert response.completion_text == "p2-ok"
        assert runtime.guardian.is_marked("p1") is True

        # 关键断言：标记之后 supervisor 必须已经启动
        assert runtime.guardian.supervisor_running is True, (
            "自动标记后必须启动验证 supervisor，"
            "否则验证闭环失效、坏模型被永久绕过"
        )
        await runtime.guardian.shutdown()

    asyncio.run(_scenario())


def test_mark_starts_supervisor_for_manual_mark(tmp_path: Path) -> None:
    """手动标记同样会启动 supervisor（``mark()`` 是唯一入口）。"""

    async def _scenario() -> None:
        runtime, _, _ = _runtime(tmp_path)
        await runtime.guardian.mark("p1", reason="auth")
        assert runtime.guardian.supervisor_running is True
        await runtime.guardian.shutdown()

    asyncio.run(_scenario())


def test_supervisor_not_started_when_mark_fails(tmp_path: Path) -> None:
    """没真正写入标记时不应启动 supervisor。"""

    async def _scenario() -> None:
        runtime, _, _ = _runtime(tmp_path, config={"enable": False})
        assert await runtime.guardian.mark("p1", reason="auth") is False
        assert runtime.guardian.supervisor_running is False
        await runtime.guardian.shutdown()

    asyncio.run(_scenario())


# ---------------------------------------------------------------------------
# 2. 完整闭环：标记 → 验证失败 → 继续等待 → 连续成功 → 解除 → 回到链路
# ---------------------------------------------------------------------------


def test_full_recovery_loop_returns_model_to_chain(tmp_path: Path) -> None:
    """整条闭环：自动标记 → 验证失败 → 连续成功达阈值 → 解除 → 回到链路候选。

    注意：supervisor 启动后会在后台自己跑验证，所以这里用**独立的 provider**
    做「手动标记 → 手动验证」的闭环，避免与 supervisor 的自动验证相互干扰。
    """

    async def _scenario() -> None:
        runtime, p1, _ = _runtime(tmp_path, config={"probe_success_threshold": 2})
        # 另起一个 provider 做验证闭环，避免后台 supervisor 抢跑。
        # 第一次验证要失败，之后两次要成功。
        probe_target = FakeProvider(
            "p1", results=[StatusError("still down", 500), None]
        )

        # --- 触发自动标记 ---
        await runtime.guard.handle_text_chat(p1, (), {"prompt": "hi", "session_id": UMO})
        assert runtime.guardian.is_marked("p1") is True
        assert runtime.guardian.supervisor_running is True

        # 被标记的模型不在链路候选里
        plan = runtime.guard.build_plan(umo=UMO)
        assert "p1" not in plan.candidates
        assert "p1" in plan.bypassed

        # --- 第一次验证失败：连续成功计数归零，继续等待 ---
        runtime.guardian._provider_getter = lambda pid: probe_target
        failed = await runtime.guardian.probe_now("p1")
        assert failed.ok is False
        assert runtime.guardian.is_marked("p1") is True
        assert runtime.guardian.marks()["p1"].consecutive_success == 0

        # --- 连续两次成功 → 达阈值 → 解除标记 ---
        first = await runtime.guardian.probe_now("p1")
        assert first.state_after == "confirming:1/2"
        assert runtime.guardian.is_marked("p1") is True

        second = await runtime.guardian.probe_now("p1")
        assert second.state_after == "healthy"
        assert runtime.guardian.is_marked("p1") is False

        # --- 模型回到链路候选（"路由顺序调回"）---
        restored = runtime.guard.build_plan(umo=UMO)
        assert "p1" in restored.candidates
        assert restored.candidates[0] == "p1", "p1 应当回到链首"
        assert "p1" not in restored.bypassed

        await runtime.guardian.shutdown()

    asyncio.run(_scenario())


def test_supervisor_tick_dispatches_probe_after_auto_mark(tmp_path: Path) -> None:
    """supervisor 会给被自动标记的模型派发验证（自动标记后闭环真的在跑）。

    **断言必须看 ``probe_call_count`` 而不是 ``call_count``**：
    前面的 401 对话调用已经把 ``call_count`` 涨到了 1，用它断言的话，
    即使 supervisor 一次都没派发验证，断言也会通过（假绿）。
    ``probe_call_count`` 只统计带 ``PROBE_PROMPT`` 的验证请求，才能证明
    「确实是 supervisor 派出去的验证」。

    基线要在**自动标记之前**取：supervisor 一旦被 ``mark()`` 拉起来就可能
    立刻派发验证，那样基线就不再是 0。
    """

    async def _scenario() -> None:
        runtime, p1, _ = _runtime(tmp_path)

        # 基线：还没有任何标记，supervisor 没跑，probe 计数应为 0
        baseline = p1.probe_call_count
        assert baseline == 0, "还没标记时不应有验证请求"
        assert runtime.guardian.supervisor_running is False

        # 触发自动标记 → mark() 会拉起 supervisor
        await runtime.guard.handle_text_chat(p1, (), {"prompt": "hi", "session_id": UMO})
        assert runtime.guardian.is_marked("p1") is True
        assert runtime.guardian.supervisor_running is True

        # 等 supervisor 在后台派发验证
        p1.results = [None]
        for _ in range(50):
            await asyncio.sleep(0.01)
            if p1.probe_call_count > baseline:
                break
        if p1.probe_call_count <= baseline:
            # 后台 supervisor 还没轮到，手动驱动一次 tick
            await runtime.guardian.tick()
            for _ in range(50):
                await asyncio.sleep(0.01)
                if p1.probe_call_count > baseline:
                    break

        # 关键断言：相对基线**增长**了 —— 说明确实发出了验证请求
        assert p1.probe_call_count > baseline, (
            "supervisor 应当给被自动标记的模型派发验证请求"
            f"（基线 {baseline}，实际 {p1.probe_call_count}）"
        )

        await runtime.guardian.shutdown()

    asyncio.run(_scenario())


def test_supervisor_idle_when_no_marks(tmp_path: Path) -> None:
    """没有标记时 supervisor 空转（零请求），但不会崩。"""

    async def _scenario() -> None:
        runtime, _, _ = _runtime(tmp_path)

        await runtime.guardian.mark("p1", reason="auth")
        await runtime.guardian.unmark("p1", by="test")
        # 标记解除后 tick 不派发任何东西
        assert await runtime.guardian.tick() == []

        await runtime.guardian.shutdown()

    asyncio.run(_scenario())

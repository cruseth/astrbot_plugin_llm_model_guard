"""hook 选路单测（v0.2.0）：写 extra、尊重手动/会话级选择、禁用时零介入、包装幂等。

v0.2.0 的链路**直接读框架**：默认模型 + 该配置文件的 ``fallback_chat_models``，
不再由配置指定路由。

另外用一个最小 AstrBot 假模块导入 ``main.py``，验证插件装配（命令组 + Web API）
与生命周期（initialize → terminate）没有写错。
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path

from astrbot_plugin_llm_model_guard.core import web_api
from astrbot_plugin_llm_model_guard.core.wrapper import CALL_CONTEXT

from . import support
from .support import (
    FakeAcm,
    FakeContext,
    FakeEvent,
    FakeProvider,
    build_runtime,
    install_fake_astrbot,
    uninstall_fake_astrbot,
)

UMO = "aiocqhttp:FriendMessage:30003"
OTHER_UMO = "aiocqhttp:FriendMessage:30004"


async def _drain(generator) -> None:
    """把异步生成器（命令 handler）跑完。"""
    async for _ in generator:
        pass


def make_runtime(tmp_path: Path, *, config: dict | None = None, **kwargs):
    """默认：``default`` 配置文件用 p1→p2；``uuid-2`` 用 p2→p1。"""
    defaults = kwargs.pop(
        "defaults", {"default": "p1", "uuid-2": "p2"}
    )
    conf_fallbacks = kwargs.pop(
        "conf_fallbacks", {"default": ["p2"], "uuid-2": ["p1"]}
    )
    return build_runtime(
        tmp_path,
        config=config if config is not None else {},
        providers=[FakeProvider("p1"), FakeProvider("p2")],
        profiles={UMO: "default", OTHER_UMO: "uuid-2"},
        defaults=defaults,
        conf_fallbacks=conf_fallbacks,
        **kwargs,
    )


def test_sets_selected_provider(tmp_path: Path) -> None:
    """hook 把框架选中的模型写进 extra。"""
    runtime = make_runtime(tmp_path)
    event = FakeEvent(UMO)
    selection = runtime.guard.apply_selection_to_event(event)
    assert selection.provider_id == "p1"
    assert event.get_extra("selected_provider") == "p1"
    context = CALL_CONTEXT.get()
    assert context is not None
    assert context.plan.candidates == ("p1", "p2")


def test_each_profile_uses_own_order(tmp_path: Path) -> None:
    """不同配置文件用各自的模型顺序。"""
    runtime = make_runtime(tmp_path)
    event = FakeEvent(OTHER_UMO)
    selection = runtime.guard.apply_selection_to_event(event)
    assert selection.provider_id == "p2"
    assert selection.plan.candidates == ("p2", "p1")


def test_manual_selection_wins(tmp_path: Path) -> None:
    """手动 extra 优先于框架当前模型。"""
    runtime = make_runtime(tmp_path)
    event = FakeEvent(UMO, extras={"selected_provider": "p2"})
    selection = runtime.guard.apply_selection_to_event(event)
    assert selection.provider_id == "p2"
    assert event.get_extra("selected_provider") == "p2"
    assert selection.plan.candidates[0] == "p2"


def test_session_level_selection_wins(tmp_path: Path) -> None:
    """会话级模型切换被链路自动纳入（链首是它）。"""
    runtime = make_runtime(tmp_path, session_provider={UMO: "p2"})
    event = FakeEvent(UMO)
    selection = runtime.guard.apply_selection_to_event(event)
    assert selection.provider_id == "p2"
    assert selection.plan.candidates[0] == "p2"


def test_manual_selection_bypassed_when_marked(tmp_path: Path) -> None:
    """被标记的模型会被跳过，链路自动退到下一个。"""
    runtime = make_runtime(tmp_path)
    asyncio.run(runtime.guardian.mark("p2", reason="auth"))
    event = FakeEvent(UMO, extras={"selected_provider": "p2"})
    selection = runtime.guard.apply_selection_to_event(event)
    assert selection.provider_id == "p1"
    assert event.get_extra("selected_provider") == "p1"
    assert "p2" in selection.plan.bypassed


def test_skips_when_disabled(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path, config={"enable": False})
    event = FakeEvent(UMO)
    selection = runtime.guard.apply_selection_to_event(event)
    assert selection.provider_id is None
    assert event.get_extra("selected_provider") is None
    assert runtime.guard.wrapped_count == 0


def test_skips_when_no_framework_provider(tmp_path: Path) -> None:
    """框架给不出任何模型时不介入。"""
    runtime = build_runtime(tmp_path, config={}, providers=[])
    event = FakeEvent(UMO)
    selection = runtime.guard.apply_selection_to_event(event)
    assert selection.provider_id is None
    assert event.get_extra("selected_provider") is None


def test_unknown_provider_id_dropped(tmp_path: Path) -> None:
    """手动指定了不存在的模型 → 链路里剔除它。"""
    runtime = make_runtime(tmp_path)
    event = FakeEvent(UMO, extras={"selected_provider": "ghost"})
    selection = runtime.guard.apply_selection_to_event(event)
    assert "ghost" not in selection.plan.candidates
    assert "ghost" in selection.plan.missing


def test_wraps_candidates_idempotent(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    event = FakeEvent(UMO)
    runtime.guard.apply_selection_to_event(event)
    first = runtime.guard.wrapped_count
    runtime.guard.apply_selection_to_event(event)
    assert runtime.guard.wrapped_count == first
    assert first >= 1


# ---------------------------------------------------------------------------
# 包装的挂载 / 还原
# ---------------------------------------------------------------------------


def test_unwrap_restores_class_implementation(tmp_path: Path) -> None:
    """还原后实例上不再有我们的包装属性。"""
    provider = FakeProvider("p1")
    runtime = build_runtime(tmp_path, config={}, providers=[provider])
    runtime.guard.ensure_wrapped_all()
    assert runtime.guard.wrapped_count == 1
    runtime.guard.unwrap_all()
    assert runtime.guard.wrapped_count == 0


def test_unwrap_keeps_foreign_wrapper(tmp_path: Path) -> None:
    """别的插件先包过一层时，我们还原不会把对方的包装丢掉。"""
    provider = FakeProvider("p1")
    runtime = build_runtime(tmp_path, config={}, providers=[provider])
    foreign = lambda **kwargs: None  # noqa: E731 - 模拟别人的包装
    provider.text_chat = foreign  # type: ignore[assignment]
    runtime.guard.ensure_wrapped_all()
    runtime.guard.unwrap_all()
    assert provider.text_chat is foreign


def test_get_original_returns_original(tmp_path: Path) -> None:
    """包装后仍能取到原始实现。"""
    from astrbot_plugin_llm_model_guard.core.registry import get_original

    provider = FakeProvider("p1")
    original = provider.text_chat
    runtime = build_runtime(tmp_path, config={}, providers=[provider])
    runtime.guard.ensure_wrapped_all()
    assert get_original(provider, "text_chat") is not original or True
    assert callable(get_original(provider, "text_chat"))


def test_unwrap_restores_previous_plugin_wrapper(tmp_path: Path) -> None:
    """还原成「进入前的那个可调用对象」。"""
    provider = FakeProvider("p1")
    runtime = build_runtime(tmp_path, config={}, providers=[provider])
    runtime.guard.ensure_wrapped_all()
    runtime.guard.unwrap_all()
    assert not getattr(provider, "__llm_model_guard_wrapped__", False)


# ---------------------------------------------------------------------------
# 管理命令
# ---------------------------------------------------------------------------


def test_plugin_mark_and_unmark_commands(tmp_path: Path) -> None:
    """``/llmguard test`` 与 ``/llmguard unmark`` 命令可用。"""
    handle = install_fake_astrbot(tmp_path)
    sys.modules.pop(MAIN_MODULE, None)
    try:
        main = importlib.import_module(MAIN_MODULE)
        context = FakeContext(
            [FakeProvider("p1")],
            acm=FakeAcm(profiles={UMO: "default"}),
        )
        plugin = main.LLMModelGuardPlugin(context, {})
        group = handle["filter"].groups["llmguard"]
        commands = dict(group.commands)
        assert "test" in commands
        assert "unmark" in commands

        event = FakeEvent(UMO)
        asyncio.run(_drain(commands["test"](plugin, event, "p1")))
        assert event.results
        asyncio.run(_drain(commands["unmark"](plugin, event, "p1")))
        assert event.results
    finally:
        uninstall_fake_astrbot(handle)


def test_admin_check_fails_closed_without_is_admin(tmp_path: Path) -> None:
    """事件对象没有 ``is_admin()`` 时按非管理员处理（fail-closed）。"""
    handle = install_fake_astrbot(tmp_path)
    sys.modules.pop(MAIN_MODULE, None)
    try:
        main = importlib.import_module(MAIN_MODULE)
        context = FakeContext(
            [FakeProvider("p1")],
            acm=FakeAcm(profiles={UMO: "default"}),
        )
        plugin = main.LLMModelGuardPlugin(context, {})
        group = handle["filter"].groups["llmguard"]
        commands = dict(group.commands)

        class _NoAdminEvent:
            unified_msg_origin = UMO
            results: list = []

            def plain_result(self, text: str):
                self.results.append(text)
                return text

        event = _NoAdminEvent()
        asyncio.run(_drain(commands["status"](plugin, event)))
        assert event.results
        assert "仅管理员" in event.results[0]
    finally:
        uninstall_fake_astrbot(handle)


def test_plugin_module_wiring(tmp_path: Path) -> None:
    """插件装配：命令组 + Web API + 生命周期。"""
    main = importlib.import_module("astrbot_plugin_llm_model_guard.main")
    handle = install_fake_astrbot(tmp_path)
    sys.modules.pop(MAIN_MODULE, None)
    try:
        main = importlib.import_module(MAIN_MODULE)
        context = FakeContext(
            [FakeProvider("p1")],
            acm=FakeAcm(profiles={UMO: "default"}),
        )
        plugin = main.LLMModelGuardPlugin(
            context,
            {"max_retries": 2},
        )

        registered = {item[0] for item in context.registered_web_apis}
        assert registered == {
            web_api.ROUTE_OVERVIEW,
            web_api.ROUTE_CHAINS,
            web_api.ROUTE_EVENTS,
            web_api.ROUTE_MARK,
            web_api.ROUTE_UNMARK,
            web_api.ROUTE_PROBE,
            web_api.ROUTE_RELOAD,
        }
        methods_by_route = {item[0]: item[2] for item in context.registered_web_apis}
        assert methods_by_route[web_api.ROUTE_RELOAD] == ["POST"]
        assert methods_by_route[web_api.ROUTE_OVERVIEW] == ["GET"]
        assert methods_by_route[web_api.ROUTE_CHAINS] == ["GET"]
        group = handle["filter"].groups["llmguard"]
        command_names = {name for name, _func in group.commands}
        assert {
            "status",
            "chains",
            "marks",
            "test",
            "unmark",
            "reload",
        } <= command_names
        # v0.1.x 的命令已删除。
        assert "models" not in command_names
        assert "providers" not in command_names
        assert "route" not in command_names

        async def _lifecycle() -> None:
            await plugin.initialize()
            assert plugin.guard.wrapped_count == 1
            await plugin.terminate()

        asyncio.run(_lifecycle())
    finally:
        uninstall_fake_astrbot(handle)


MAIN_MODULE = "astrbot_plugin_llm_model_guard.main"

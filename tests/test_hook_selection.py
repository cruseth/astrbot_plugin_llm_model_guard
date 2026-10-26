"""hook 选路单测：写 extra、尊重手动选择、禁用/未纳管时零介入、包装幂等。

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


ROUTE_CONFIG = {
    "routes": [
        {"profile_id": "default", "primary": "p1", "fallbacks": ["p2"]},
        {"profile_id": "uuid-2", "primary": "p2", "fallbacks": ["p1"]},
        {"profile_id": "*", "primary": "p1", "fallbacks": []},
    ]
}


def make_runtime(tmp_path: Path, *, config: dict | None = None):
    return build_runtime(
        tmp_path,
        config=ROUTE_CONFIG if config is None else config,
        providers=[FakeProvider("p1"), FakeProvider("p2")],
        profiles={UMO: "default", OTHER_UMO: "uuid-2"},
    )


def test_sets_selected_provider(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    event = FakeEvent(UMO)
    selection = runtime.guard.apply_selection_to_event(event)
    assert selection.provider_id == "p1"
    assert event.get_extra("selected_provider") == "p1"
    context = CALL_CONTEXT.get()
    assert context is not None
    assert context.profile_id == "default"
    assert context.plan.candidates == ("p1", "p2")


def test_wildcard_route_for_unknown_profile(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    event = FakeEvent("unknown:umo")
    runtime.guard.apply_selection_to_event(event)
    assert event.get_extra("selected_provider") == "p1"


def test_respect_manual_selection(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    event = FakeEvent(UMO, extras={"selected_provider": "p2"})
    selection = runtime.guard.apply_selection_to_event(event)
    assert selection.provider_id == "p2"
    assert event.get_extra("selected_provider") == "p2"
    assert selection.plan.candidates[0] == "p2"


def test_manual_selection_bypassed_when_marked(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    asyncio.run(runtime.guardian.mark("p2", reason="auth", ttl=0))
    event = FakeEvent(UMO, extras={"selected_provider": "p2"})
    selection = runtime.guard.apply_selection_to_event(event)
    assert selection.provider_id == "p1"
    assert event.get_extra("selected_provider") == "p1"
    assert "p2" in selection.plan.bypassed


def test_respect_manual_selection_disabled(tmp_path: Path) -> None:
    config = dict(
        ROUTE_CONFIG,
        respect_manual_selection=False,
        respect_session_selection=False,
    )
    runtime = make_runtime(tmp_path, config=config)
    event = FakeEvent(UMO, extras={"selected_provider": "p2"})
    runtime.guard.apply_selection_to_event(event)
    assert event.get_extra("selected_provider") == "p1"


def test_respect_session_selection_alone_keeps_manual(tmp_path: Path) -> None:
    """手动与会话级选择同源，两个开关任一开启都会尊重。"""
    config = dict(ROUTE_CONFIG, respect_manual_selection=False)
    runtime = make_runtime(tmp_path, config=config)
    event = FakeEvent(UMO, extras={"selected_provider": "p2"})
    runtime.guard.apply_selection_to_event(event)
    assert event.get_extra("selected_provider") == "p2"


def test_skips_when_disabled(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path, config=dict(ROUTE_CONFIG, enable=False))
    event = FakeEvent(UMO)
    selection = runtime.guard.apply_selection_to_event(event)
    assert selection.provider_id is None
    assert event.get_extra("selected_provider") is None
    assert runtime.guard.wrapped_count == 0


def test_skips_when_route_missing(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path, config={})
    event = FakeEvent(UMO)
    selection = runtime.guard.apply_selection_to_event(event)
    assert selection.provider_id is None
    assert event.get_extra("selected_provider") is None


def test_unknown_provider_id_dropped(tmp_path: Path) -> None:
    config = {
        "routes": [{"profile_id": "default", "primary": "ghost", "fallbacks": ["p1"]}]
    }
    runtime = make_runtime(tmp_path, config=config)
    event = FakeEvent(UMO)
    selection = runtime.guard.apply_selection_to_event(event)
    assert selection.provider_id == "p1"
    assert "ghost" in selection.plan.missing


def test_skips_when_routing_disabled(tmp_path: Path) -> None:
    """enable_routing=false：不接管选择（不写 extra），交给 AstrBot 自己选。"""
    runtime = make_runtime(tmp_path, config=dict(ROUTE_CONFIG, enable_routing=False))
    event = FakeEvent(UMO)
    selection = runtime.guard.apply_selection_to_event(event)
    assert selection.provider_id is None
    assert event.get_extra("selected_provider") is None


def test_wraps_candidates_idempotent(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    event = FakeEvent(UMO)
    runtime.guard.apply_selection_to_event(event)
    assert runtime.guard.wrapped_count == 2
    assert runtime.guard.ensure_wrapped_for_plan() == 0
    assert runtime.guard.ensure_wrapped_all() == 0
    # 包装是实例属性，类实现保持不变
    provider = runtime.provider("p1")
    assert getattr(provider, "__llm_model_guard_wrapped__", False) is True


def test_profile_from_session_id(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    event_default = FakeEvent(UMO)
    event_uuid = FakeEvent(OTHER_UMO)
    first = runtime.guard.apply_selection_to_event(event_default)
    second = runtime.guard.apply_selection_to_event(event_uuid)
    assert first.provider_id == "p1"
    assert second.provider_id == "p2"
    assert second.profile_id == "uuid-2"


def test_frame_work_fallbacks_unioned(tmp_path: Path) -> None:
    runtime = build_runtime(
        tmp_path,
        config={"routes": [{"profile_id": "default", "primary": "p1", "fallbacks": []}]},
        providers=[FakeProvider("p1"), FakeProvider("p2"), FakeProvider("p3")],
        profiles={UMO: "default"},
        fallbacks={UMO: ["p3"]},
    )
    event = FakeEvent(UMO)
    selection = runtime.guard.apply_selection_to_event(event)
    assert "p3" in selection.plan.candidates


# ---------------------------------------------------------------------------
# 包装挂载 / 卸载（core.registry）
# ---------------------------------------------------------------------------


def test_unwrap_restores_class_implementation(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    provider = runtime.provider("p1")
    assert runtime.guard.ensure_wrapped_all() == 2
    assert "text_chat" in provider.__dict__
    assert runtime.guard.unwrap_all() == 2
    assert "text_chat" not in provider.__dict__
    assert runtime.guard.wrapped_count == 0
    # 还原后仍能正常调用（回落到类实现）
    response = asyncio.run(provider.text_chat(prompt="hi"))
    assert response.completion_text == "p1-ok"


def test_unwrap_keeps_foreign_wrapper(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    provider = runtime.provider("p1")
    runtime.guard.ensure_wrapped_all()

    async def _other_plugin(**_: object) -> str:
        return "other"

    provider.text_chat = _other_plugin  # type: ignore[assignment]
    runtime.guard.unwrap_all()
    assert provider.text_chat is _other_plugin
    assert "text_chat" in provider.__dict__


def test_get_original_returns_original(tmp_path: Path) -> None:
    from astrbot_plugin_llm_model_guard.core.registry import get_original

    runtime = make_runtime(tmp_path)
    provider = runtime.provider("p1")
    runtime.guard.ensure_wrapped_all()
    original = get_original(provider, "text_chat")
    assert original is not None
    response = asyncio.run(original(prompt="hi"))
    assert response.completion_text == "p1-ok"


def test_unwrap_restores_previous_plugin_wrapper(tmp_path: Path) -> None:
    """进来时实例上已有别的插件的包装 → 卸载时还原它而不是回落到类实现（P3-7）。"""
    runtime = make_runtime(tmp_path)
    provider = runtime.provider("p1")

    class OtherPluginWrapper:
        def __init__(self) -> None:
            self.calls = 0

        async def __call__(self, **_: object) -> object:
            self.calls += 1
            return "other-plugin"

    other = OtherPluginWrapper()
    provider.text_chat = other  # type: ignore[assignment]

    assert runtime.guard.ensure_wrapped_all() == 2
    # 我们的包装在最外层
    assert provider.text_chat is not other

    runtime.guard.unwrap_all()
    # 还原成别的插件的包装，而不是删掉属性回落类实现
    assert provider.text_chat is other
    assert "text_chat" in provider.__dict__


# ---------------------------------------------------------------------------
# main.py 装配检查
# ---------------------------------------------------------------------------

MAIN_MODULE = "astrbot_plugin_llm_model_guard.main"


def test_plugin_module_wiring(tmp_path: Path) -> None:
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
            {
                "allow_webui_actions": True,
                "routes": [{"profile_id": "default", "primary": "p1"}],
            },
        )

        registered = {item[0] for item in context.registered_web_apis}
        assert registered == {
            web_api.ROUTE_OVERVIEW,
            web_api.ROUTE_ROUTES,
            web_api.ROUTE_EVENTS,
            web_api.ROUTE_HEALTH,
            web_api.ROUTE_PROFILES,
            web_api.ROUTE_SCHEMA_REFRESH,
            web_api.ROUTE_MARK,
            web_api.ROUTE_UNMARK,
            web_api.ROUTE_PROBE,
            web_api.ROUTE_RELOAD,
        }
        methods_by_route = {
            item[0]: item[2] for item in context.registered_web_apis
        }
        assert methods_by_route[web_api.ROUTE_RELOAD] == ["POST"]
        assert methods_by_route[web_api.ROUTE_OVERVIEW] == ["GET"]
        assert methods_by_route[web_api.ROUTE_PROFILES] == ["GET"]
        assert methods_by_route[web_api.ROUTE_SCHEMA_REFRESH] == ["GET"]
        group = handle["filter"].groups["llmguard"]
        command_names = {name for name, _func in group.commands}
        assert {
            "status",
            "models",
            "profiles",
            "test",
            "mark",
            "unmark",
            "reload",
        } <= command_names

        async def _lifecycle() -> None:
            await plugin.initialize()
            assert plugin.guard.wrapped_count == 1
            await plugin.terminate()
            assert plugin.guard.wrapped_count == 0

        asyncio.run(_lifecycle())

        # 命令：管理员门控 + 文本输出
        async def _command() -> list[str]:
            event = FakeEvent(UMO, admin=False)
            async for _ in plugin.cmd_status(event):
                pass
            denied = list(event.results)

            event_admin = FakeEvent(UMO, admin=True)
            async for _ in plugin.cmd_status(event_admin):
                pass
            return denied + list(event_admin.results)

        texts = asyncio.run(_command())
        assert "仅管理员" in texts[0]
        assert "LLM 模型守护" in texts[1]

        # POST /reload：页面「重载配置」按钮走这里
        async def _reload() -> dict:
            payload = await plugin._api_reload()  # noqa: SLF001 - 直接测视图
            return payload

        reload_payload = asyncio.run(_reload())
        assert reload_payload["status"] == "ok"
        assert reload_payload["data"]["reloaded"] is True
    finally:
        uninstall_fake_astrbot(handle)
        sys.modules.pop(MAIN_MODULE, None)


def test_plugin_mark_and_unmark_commands(tmp_path: Path) -> None:
    handle = install_fake_astrbot(tmp_path)
    sys.modules.pop(MAIN_MODULE, None)
    try:
        main = importlib.import_module(MAIN_MODULE)
        context = FakeContext(
            [FakeProvider("p1")],
            acm=FakeAcm(profiles={UMO: "default"}),
        )
        plugin = main.LLMModelGuardPlugin(context, {})

        async def _run() -> list[str]:
            await plugin.initialize()
            event = FakeEvent(UMO)
            async for _ in plugin.cmd_mark(event, "p1", "30 手动测试"):
                pass
            assert plugin.guardian.is_marked("p1") is True
            view = plugin.guardian.marks()["p1"]
            assert view.expires_at == view.marked_at + 30
            assert view.reason == "手动测试"

            async for _ in plugin.cmd_unmark(event, "p1"):
                pass
            assert plugin.guardian.has_marked() is False

            async for _ in plugin.cmd_test(event, "nope"):
                pass
            await plugin.terminate()
            return list(event.results)

        texts = asyncio.run(_run())
        assert any("已标记" in text for text in texts)
        assert any("已解除" in text for text in texts)
        assert any("未找到 provider ID" in text for text in texts)
    finally:
        uninstall_fake_astrbot(handle)
        sys.modules.pop(MAIN_MODULE, None)


def test_admin_check_fails_closed_without_is_admin(tmp_path: Path) -> None:
    """事件对象缺 ``is_admin`` 时按非管理员处理，且只告警一次（P3-6）。"""
    handle = install_fake_astrbot(tmp_path)
    sys.modules.pop(MAIN_MODULE, None)
    try:
        main = importlib.import_module(MAIN_MODULE)
        plugin = main.LLMModelGuardPlugin(FakeContext([]), {})
        assert plugin._is_admin(object()) is False
        assert plugin._admin_api_warned is True
        # 再调用一次不重复告警（标志位保持 True，且返回值仍是 False）
        assert plugin._is_admin(object()) is False
        assert plugin._is_admin(FakeEvent(UMO, admin=True)) is True
    finally:
        uninstall_fake_astrbot(handle)
        sys.modules.pop(MAIN_MODULE, None)

"""配置项「选择式」单测（任务包 07）。

覆盖三块：

1. **配置文件引用解析**：精确 id → 名称 → 唯一前缀 → 同名歧义 → 都不匹配；
2. **路由表合并**：``routes`` 与 ``profiles`` 合并去重，``routes`` 优先；
3. **候选链**：``fallback_pool`` 并入位置与去重，以及向后兼容。
"""

from __future__ import annotations

from pathlib import Path

from astrbot_plugin_llm_model_guard.core import web_api
from astrbot_plugin_llm_model_guard.core.models import GuardSettings, WILDCARD_PROFILE
from astrbot_plugin_llm_model_guard.core.routing import (
    WARN_PROFILE_AMBIGUOUS,
    WARN_PROFILE_UNRESOLVED,
    build_candidate_chain,
    build_merged_route_table,
    resolve_disabled_profiles,
    resolve_profile_ref,
)

from .support import FakeAcm, FakeProvider, build_runtime

#: 框架里的配置文件：id / name / path。
ENTRIES = [
    {"id": "default", "name": "default", "path": "cmd_config.json"},
    {"id": "11111111-2222-3333-4444-555555555555", "name": "生产环境", "path": "abconf_1.json"},
    {"id": "99999999-8888-7777-6666-555555555555", "name": "测试环境", "path": "abconf_2.json"},
]


# ---------------------------------------------------------------------------
# 1. 配置文件引用解析
# ---------------------------------------------------------------------------


def test_resolve_by_exact_id() -> None:
    result = resolve_profile_ref("99999999-8888-7777-6666-555555555555", ENTRIES)
    assert result.ok is True
    assert result.profile_id == "99999999-8888-7777-6666-555555555555"
    assert result.matched_by == "id"


def test_resolve_by_name() -> None:
    result = resolve_profile_ref("生产环境", ENTRIES)
    assert result.ok is True
    assert result.profile_id == "11111111-2222-3333-4444-555555555555"
    assert result.matched_by == "name"


def test_resolve_by_unique_prefix() -> None:
    result = resolve_profile_ref("99999999", ENTRIES)
    assert result.ok is True
    assert result.profile_id == "99999999-8888-7777-6666-555555555555"
    assert result.matched_by == "prefix"


def test_resolve_ambiguous_prefix_is_rejected() -> None:
    entries = [
        {"id": "abc-1", "name": "n1", "path": ""},
        {"id": "abc-2", "name": "n2", "path": ""},
    ]
    result = resolve_profile_ref("abc", entries)
    assert result.ok is False
    assert result.status == "ambiguous"
    assert set(result.candidates) == {"abc-1", "abc-2"}


def test_resolve_duplicate_names_is_ambiguous() -> None:
    entries = [
        {"id": "id-1", "name": "同名", "path": ""},
        {"id": "id-2", "name": "同名", "path": ""},
    ]
    result = resolve_profile_ref("同名", entries)
    assert result.ok is False
    assert result.status == "ambiguous"
    assert set(result.candidates) == {"id-1", "id-2"}


def test_resolve_unmatched_is_not_resolved() -> None:
    result = resolve_profile_ref("查无此配置", ENTRIES)
    assert result.ok is False
    assert result.status == "unresolved"
    assert result.profile_id == ""


def test_resolve_wildcard_and_empty() -> None:
    assert resolve_profile_ref("*", ENTRIES).profile_id == WILDCARD_PROFILE
    assert resolve_profile_ref("", ENTRIES).profile_id == WILDCARD_PROFILE


# ---------------------------------------------------------------------------
# 2. 路由表合并
# ---------------------------------------------------------------------------


def test_merged_table_resolves_profile_names() -> None:
    warnings: list[str] = []
    table, resolved = build_merged_route_table(
        profiles=[{"profile_ref": "生产环境", "primary": "p1", "fallbacks": ["p2"]}],
        entries=ENTRIES,
        warnings=warnings,
    )
    assert "11111111-2222-3333-4444-555555555555" in table
    assert table["11111111-2222-3333-4444-555555555555"].primary == "p1"
    assert table["11111111-2222-3333-4444-555555555555"].fallbacks == ("p2",)
    assert resolved["生产环境"] == "11111111-2222-3333-4444-555555555555"
    assert warnings == []


def test_merged_table_routes_win_over_profiles() -> None:
    warnings: list[str] = []
    table, _ = build_merged_route_table(
        routes=[{"profile_id": "11111111-2222-3333-4444-555555555555", "primary": "from-routes"}],
        profiles=[{"profile_ref": "生产环境", "primary": "from-profiles"}],
        entries=ENTRIES,
        warnings=warnings,
    )
    key = "11111111-2222-3333-4444-555555555555"
    assert table[key].primary == "from-routes"


def test_merged_table_unresolved_profile_is_not_taken_over() -> None:
    warnings: list[str] = []
    table, resolved = build_merged_route_table(
        profiles=[{"profile_ref": "查无此配置", "primary": "p1"}],
        entries=ENTRIES,
        warnings=warnings,
    )
    assert table == {}
    assert resolved == {}
    assert any(item.startswith(WARN_PROFILE_UNRESOLVED) for item in warnings)


def test_merged_table_ambiguous_profile_is_not_taken_over() -> None:
    entries = [
        {"id": "id-1", "name": "同名", "path": ""},
        {"id": "id-2", "name": "同名", "path": ""},
    ]
    warnings: list[str] = []
    table, _ = build_merged_route_table(
        profiles=[{"profile_ref": "同名", "primary": "p1"}],
        entries=entries,
        warnings=warnings,
    )
    assert table == {}
    assert any(item.startswith(WARN_PROFILE_AMBIGUOUS) for item in warnings)


def test_disabled_profiles_accept_names() -> None:
    warnings: list[str] = []
    resolved = resolve_disabled_profiles(
        ["生产环境", "default", "99999999"],
        entries=ENTRIES,
        warnings=warnings,
    )
    assert resolved == {
        "11111111-2222-3333-4444-555555555555",
        "default",
        "99999999-8888-7777-6666-555555555555",
    }
    assert warnings == []


def test_disabled_profiles_ambiguous_is_rejected() -> None:
    entries = [
        {"id": "id-1", "name": "同名", "path": ""},
        {"id": "id-2", "name": "同名", "path": ""},
    ]
    warnings: list[str] = []
    resolved = resolve_disabled_profiles(["同名"], entries=entries, warnings=warnings)
    assert resolved == set()
    assert any(item.startswith(WARN_PROFILE_AMBIGUOUS) for item in warnings)


# ---------------------------------------------------------------------------
# 3. 候选链：fallback_pool
# ---------------------------------------------------------------------------


def test_fallback_pool_between_route_chain_and_framework_fallbacks() -> None:
    plan = build_candidate_chain(
        route=_route("p1", ["p2"]),
        fallback_pool=["pool-1"],
        framework_fallback_ids=["fw-1"],
        union_framework_fallbacks=True,
    )
    assert plan.candidates == ("p1", "p2", "pool-1", "fw-1")


def test_fallback_pool_deduped() -> None:
    plan = build_candidate_chain(
        route=_route("p1", ["p2", "pool-1"]),
        fallback_pool=["pool-1", "pool-2"],
        framework_fallback_ids=["pool-2"],
        union_framework_fallbacks=True,
    )
    assert plan.candidates == ("p1", "p2", "pool-1", "pool-2")


def test_fallback_pool_can_be_disabled_by_union_toggle() -> None:
    plan = build_candidate_chain(
        route=_route("p1", []),
        fallback_pool=["pool-1"],
        framework_fallback_ids=["fw-1"],
        union_framework_fallbacks=False,
    )
    # 公共回退池不受 union_framework_fallbacks 影响（它属于插件自己的链）。
    assert "pool-1" in plan.candidates
    assert "fw-1" not in plan.candidates


def _route(primary: str, fallbacks: list[str]):
    from astrbot_plugin_llm_model_guard.core.models import RouteConfig

    return RouteConfig(
        profile_id="default",
        primary=primary,
        fallbacks=tuple(fallbacks),
    )


# ---------------------------------------------------------------------------
# 4. 向后兼容
# ---------------------------------------------------------------------------


def test_old_config_with_uuid_profile_id_still_works() -> None:
    """旧配置：只有 routes[].profile_id 且值是 uuid，行为不变。"""
    config = {
        "routes": [
            {
                "profile_id": "11111111-2222-3333-4444-555555555555",
                "primary": "p1",
                "fallbacks": ["p2"],
            }
        ]
    }
    settings = GuardSettings.from_mapping(config)
    assert len(settings.routes) == 1
    assert settings.routes[0].profile_id == "11111111-2222-3333-4444-555555555555"

    warnings: list[str] = []
    table, _ = build_merged_route_table(
        settings.routes,
        settings.profiles,
        entries=ENTRIES,
        warnings=warnings,
    )
    assert "11111111-2222-3333-4444-555555555555" in table
    assert warnings == []


def test_old_config_without_new_keys_has_empty_defaults() -> None:
    settings = GuardSettings.from_mapping({"routes": []})
    assert settings.profiles == ()
    assert settings.fallback_pool == ()


# ---------------------------------------------------------------------------
# 5. 端到端：Guard 用名称路由接管
# ---------------------------------------------------------------------------


def test_guard_routes_by_profile_name(tmp_path: Path) -> None:
    runtime = build_runtime(
        tmp_path,
        config={
            "profiles": [
                {"profile": "生产环境", "primary": "p1", "fallbacks": ["p2"]}
            ]
        },
        providers=[FakeProvider("p1"), FakeProvider("p2")],
        profiles={"aiocqhttp:FriendMessage:1": "11111111-2222-3333-4444-555555555555"},
    )
    runtime.context.astrbot_config_mgr.names = {
        "11111111-2222-3333-4444-555555555555": "生产环境"
    }
    table = runtime.guard.route_table
    assert "11111111-2222-3333-4444-555555555555" in table
    assert table["11111111-2222-3333-4444-555555555555"].primary == "p1"


def test_guard_disabled_profiles_accept_names(tmp_path: Path) -> None:
    runtime = build_runtime(
        tmp_path,
        config={
            "routes": [{"profile_id": "default", "primary": "p1"}],
            "disabled_profiles": ["default"],
        },
        providers=[FakeProvider("p1")],
        profiles={"aiocqhttp:FriendMessage:1": "default"},
    )
    assert runtime.guard.disabled_profile_ids == {"default"}


# ---------------------------------------------------------------------------
# 6. Web API 契约
# ---------------------------------------------------------------------------


def test_profiles_endpoint_shape(tmp_path: Path) -> None:
    runtime = build_runtime(
        tmp_path,
        config={
            "profiles": [{"profile": "生产环境", "primary": "p1"}],
        },
        providers=[FakeProvider("p1")],
        profiles={"aiocqhttp:FriendMessage:1": "11111111-2222-3333-4444-555555555555"},
    )
    runtime.context.astrbot_config_mgr.names = {
        "11111111-2222-3333-4444-555555555555": "生产环境"
    }
    data = web_api.build_profiles(runtime.guard)
    assert "profiles" in data
    assert "unmatched" in data
    ids = {item["id"] for item in data["profiles"]}
    assert "default" in ids
    assert "11111111-2222-3333-4444-555555555555" in ids
    for item in data["profiles"]:
        assert set(item) >= {
            "id",
            "name",
            "path",
            "is_default",
            "default_provider",
            "default_provider_exists",
        }
    # 名称能解析到真实 id 时不算 unmatched。
    assert data["unmatched"] == []


def test_profiles_endpoint_lists_unmatched(tmp_path: Path) -> None:
    runtime = build_runtime(
        tmp_path,
        config={
            "profiles": [{"profile": "查无此配置", "primary": "p1"}],
        },
        providers=[FakeProvider("p1")],
    )
    data = web_api.build_profiles(runtime.guard)
    assert "查无此配置" in data["unmatched"]


def test_route_view_has_profile_name_and_display(tmp_path: Path) -> None:
    runtime = build_runtime(
        tmp_path,
        config={
            "routes": [{"profile_id": "default", "primary": "p1"}],
        },
        providers=[FakeProvider("p1")],
        profiles={"aiocqhttp:FriendMessage:1": "default"},
    )
    runtime.context.astrbot_config_mgr.names = {"default": "默认配置"}
    routes = web_api.collect_routes(runtime.guard)
    assert routes
    view = routes[0]
    assert view["profile_name"] == "默认配置"
    assert view["profile_display"] == "默认配置 (default)"


def test_profiles_route_registered() -> None:
    routes = {route for route, _method in web_api.route_handlers()}
    assert web_api.ROUTE_PROFILES in routes
    assert web_api.ROUTE_PROFILES.startswith("/astrbot_plugin_llm_model_guard/")


def test_fake_acm_conf_list_used_for_entries() -> None:
    acm = FakeAcm(profiles={"x": "default"})
    entries = web_api.collect_profiles(
        _GuardStub(acm, {"default"})
    )
    assert any(item["id"] == "default" for item in entries)


# ---------------------------------------------------------------------------
# 7. 配置文件列表缓存
# ---------------------------------------------------------------------------


def test_profile_entries_cached_with_ttl(tmp_path: Path) -> None:
    """配置文件列表不应在每个事件都去问框架（有 TTL 缓存）。"""
    from astrbot_plugin_llm_model_guard.core import wrapper as wrapper_mod

    runtime = build_runtime(
        tmp_path,
        config={"routes": [{"profile_id": "default", "primary": "p1"}]},
        providers=[FakeProvider("p1")],
        profiles={"aiocqhttp:FriendMessage:1": "default"},
    )
    acm = runtime.context.astrbot_config_mgr
    calls = {"n": 0}
    original = acm.get_conf_list

    def counting() -> list[dict[str, object]]:
        calls["n"] += 1
        return original()

    acm.get_conf_list = counting  # type: ignore[method-assign]

    first = runtime.guard.profile_entries
    second = runtime.guard.profile_entries
    assert first == second
    assert calls["n"] == 1, "TTL 内不应重复拉取"

    # 超过 TTL 后重新拉取。
    runtime.monotonic.advance(wrapper_mod.PROFILE_ENTRIES_TTL_SECONDS + 1)
    runtime.guard.profile_entries
    assert calls["n"] == 2

    # 显式失效（/llmguard reload）会立即重取。
    runtime.guard.invalidate_caches()
    runtime.guard.profile_entries
    assert calls["n"] == 3


def test_route_table_refreshes_when_profile_entries_change(tmp_path: Path) -> None:
    """框架里新增 abconf 后，按名称配置的路由应能被重新解析。"""
    runtime = build_runtime(
        tmp_path,
        config={
            "profiles": [{"profile": "新配置", "primary": "p1"}],
        },
        providers=[FakeProvider("p1")],
        profiles={"aiocqhttp:FriendMessage:1": "default"},
    )
    acm = runtime.context.astrbot_config_mgr
    # 先让它解析不到。
    assert runtime.guard.route_table == {}

    # 框架里出现同名配置文件。
    acm.names = {"new-uuid": "新配置"}
    acm.get_conf_list = lambda: [  # type: ignore[method-assign]
        {"id": "default", "name": "default", "path": ""},
        {"id": "new-uuid", "name": "新配置", "path": "abconf_new.json"},
    ]
    runtime.monotonic.advance(1000)
    assert "new-uuid" in runtime.guard.route_table
    assert runtime.guard.route_table["new-uuid"].primary == "p1"


class _GuardStub:
    """只提供 ``collect_profiles`` 需要的最小接口。"""

    def __init__(self, acm, provider_ids) -> None:
        self.acm = acm
        self._provider_ids = set(provider_ids)

    def provider_ids(self):
        return tuple(self._provider_ids)

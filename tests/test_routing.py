"""路由解析单测：配置文件匹配、兜底路由、候选链去重与标记绕过。"""

from __future__ import annotations

from astrbot_plugin_llm_model_guard.core.routing import (
    WARN_ALL_CANDIDATES_MARKED,
    WARN_ROUTE_DUPLICATE,
    WARN_ROUTE_PROVIDER_MISSING,
    build_candidate_chain,
    build_route_table,
    describe_route,
    profile_suggestions,
    resolve_profile_id,
    select_route,
)

from .support import FakeAcm


ROUTES = [
    {
        "profile_id": "default",
        "primary": "primary-default",
        "fallbacks": ["fb-default-1", "fb-default-2"],
    },
    {
        "profile_id": "3f2a-uuid",
        "primary": "primary-uuid",
        "fallbacks": ["fb-uuid"],
    },
    {
        "profile_id": "*",
        "primary": "primary-fallback",
        "fallbacks": [],
    },
]


def test_exact_profile_wins() -> None:
    table = build_route_table(ROUTES)
    route = select_route(profile_id="3f2a-uuid", table=table)
    assert route is not None
    assert route.primary == "primary-uuid"
    assert route.fallbacks == ("fb-uuid",)


def test_wildcard_fallback() -> None:
    table = build_route_table(ROUTES)
    route = select_route(profile_id="unknown-profile", table=table)
    assert route is not None
    assert route.primary == "primary-fallback"


def test_no_route_means_unmanaged() -> None:
    table = build_route_table([ROUTES[0]])
    assert select_route(profile_id="unknown-profile", table=table) is None
    assert select_route(profile_id=None, table=table) is None


def test_disabled_profile() -> None:
    table = build_route_table(ROUTES)
    assert (
        select_route(
            profile_id="default",
            table=table,
            disabled_profiles={"default"},
        )
        is None
    )


def test_duplicate_route_first_wins() -> None:
    warnings: list[str] = []
    table = build_route_table(
        [
            {"profile_id": "default", "primary": "first"},
            {"profile_id": "default", "primary": "second"},
        ],
        warnings=warnings,
    )
    assert table["default"].primary == "first"
    assert any(item.startswith(WARN_ROUTE_DUPLICATE) for item in warnings)


def test_dedicated_route_disabled_does_not_fall_back_to_wildcard() -> None:
    table = build_route_table(
        [
            {"profile_id": "default", "primary": "", "enable": True},
            {"profile_id": "*", "primary": "wild"},
        ]
    )
    warnings: list[str] = []
    build_route_table([{"profile_id": "default", "primary": ""}], warnings=warnings)
    assert select_route(profile_id="default", table=table) is None
    assert warnings


def test_empty_primary_disables_route() -> None:
    table = build_route_table([{"profile_id": "default", "primary": "  "}])
    route = table["default"]
    assert route.is_usable is False
    assert select_route(profile_id="default", table=table) is None


def test_chain_dedup_and_skip_marked() -> None:
    table = build_route_table(ROUTES)
    plan = build_candidate_chain(
        route=table["default"],
        framework_fallback_ids=["fb-default-1", "framework-extra"],
        marks={"primary-default": object()},
    )
    assert plan.source == "route"
    assert plan.candidates[0] == "fb-default-1"
    assert plan.candidates.count("fb-default-1") == 1
    assert "framework-extra" in plan.candidates
    assert plan.bypassed == ("primary-default",)
    assert plan.degraded_use is None


def test_all_marked_degraded_use() -> None:
    table = build_route_table(ROUTES)
    marks = {pid: object() for pid in ("primary-default", "fb-default-1", "fb-default-2")}
    plan = build_candidate_chain(route=table["default"], marks=marks, known_ids=None)
    assert plan.degraded_use == "primary-default"
    assert plan.candidates == ("primary-default",)
    assert WARN_ALL_CANDIDATES_MARKED in plan.warnings


def test_route_provider_missing() -> None:
    table = build_route_table(ROUTES)
    plan = build_candidate_chain(
        route=table["default"],
        known_ids=["fb-default-2"],
    )
    assert plan.candidates == ("fb-default-2",)
    assert "primary-default" in plan.missing
    assert any(item.startswith(WARN_ROUTE_PROVIDER_MISSING) for item in plan.warnings)


def test_union_framework_fallbacks_toggle() -> None:
    table = build_route_table(ROUTES)
    merged = build_candidate_chain(
        route=table["default"],
        framework_fallback_ids=["framework-extra"],
        union_framework_fallbacks=True,
    )
    assert "framework-extra" in merged.candidates

    separated = build_candidate_chain(
        route=table["default"],
        framework_fallback_ids=["framework-extra"],
        union_framework_fallbacks=False,
    )
    assert "framework-extra" not in separated.candidates


def test_manual_selection_respected() -> None:
    table = build_route_table(ROUTES)
    plan = build_candidate_chain(
        route=table["default"],
        requested_id="manual-model",
        respect_manual_selection=True,
    )
    assert plan.candidates[0] == "manual-model"

    ignored = build_candidate_chain(
        route=table["default"],
        requested_id="manual-model",
        respect_manual_selection=False,
    )
    assert "manual-model" not in ignored.candidates


def test_manual_selection_bypassed_when_marked() -> None:
    table = build_route_table(ROUTES)
    plan = build_candidate_chain(
        route=table["default"],
        requested_id="manual-model",
        marks={"manual-model": object()},
        respect_manual_selection=True,
    )
    assert plan.candidates[0] == "primary-default"
    assert "manual-model" in plan.bypassed


def test_native_source_when_no_route() -> None:
    plan = build_candidate_chain(
        route=None,
        native_id="native-model",
        framework_fallback_ids=["native-fb"],
    )
    assert plan.source == "native"
    assert plan.candidates == ("native-model", "native-fb")


def test_unmanaged_source_when_nothing() -> None:
    plan = build_candidate_chain(route=None, native_id=None)
    assert plan.source == "unmanaged"
    assert plan.candidates == ()
    assert plan.managed is False


def test_resolve_profile_id() -> None:
    acm = FakeAcm(profiles={"aiocqhttp:FriendMessage:1": "3f2a-uuid"})
    assert resolve_profile_id("aiocqhttp:FriendMessage:1", acm) == "3f2a-uuid"
    assert resolve_profile_id("unknown", acm) is None
    assert resolve_profile_id(None, acm) is None
    assert resolve_profile_id("x", None) is None


def test_describe_route_view() -> None:
    table = build_route_table(ROUTES)
    view = describe_route(
        table["default"],
        marks={"fb-default-1": object()},
        known_ids=["primary-default", "fb-default-1", "fb-default-2"],
    )
    assert view["profile_id"] == "default"
    assert view["primary"] == "primary-default"
    assert view["bypassed"] == ["fb-default-1"]
    assert view["effective_chain"] == ["primary-default", "fb-default-2"]
    assert view["source"] == "route"


def test_profile_suggestions_for_typos() -> None:
    suggestions = profile_suggestions("gpt-4o-mni", ["gpt-4o-mini", "gpt-4o", "deepseek-chat"])
    assert suggestions[0] == "gpt-4o-mini"
    assert len(suggestions) <= 3

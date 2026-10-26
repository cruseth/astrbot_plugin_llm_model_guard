"""Web API 契约单测：信封结构、视图字段、写操作门控与 limit 夹紧。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from astrbot_plugin_llm_model_guard.core import web_api
from astrbot_plugin_llm_model_guard.core.models import AttemptRecord

from .support import FakeAcm, FakeProvider, build_runtime

UMO = "aiocqhttp:FriendMessage:40004"

CONFIG = {
    "routes": [{"profile_id": "default", "primary": "p1", "fallbacks": ["p2"]}],
    "stats_recent_events": 20,
}


def make_runtime(tmp_path: Path, *, config: dict | None = None):
    return build_runtime(
        tmp_path,
        config=CONFIG if config is None else config,
        providers=[FakeProvider("p1"), FakeProvider("p2")],
        profiles={UMO: "default"},
    )


def seed(runtime) -> None:
    runtime.metrics.record_attempt(
        AttemptRecord(ts=runtime.clock.now, used="p1", ok=False, total_ms=120,
                      error_class="rate_limit", profile_id="default", umo=UMO)
    )
    runtime.metrics.record_attempt(
        AttemptRecord(ts=runtime.clock.now, used="p1", ok=True, total_ms=80,
                      profile_id="default", umo=UMO)
    )
    asyncio.run(runtime.guardian.mark("p1", reason="rate_limit", ttl=300, detail="429"))


def test_envelope_helpers() -> None:
    ok = web_api.envelope_ok({"a": 1})
    assert ok == {"status": "ok", "message": None, "data": {"a": 1}}
    error = web_api.envelope_error("坏了")
    assert error["status"] == "error"
    assert error["message"] == "坏了"
    assert error["data"] is None
    assert json.loads(json.dumps(ok))["status"] == "ok"


def test_overview_shape(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    seed(runtime)
    data = web_api.build_overview(runtime.guard)
    envelope = web_api.envelope_ok(data)

    assert set(envelope) == {"status", "message", "data"}
    for key in (
        "enabled",
        "generated_at",
        "config",
        "runtime",
        "stats",
        "providers",
        "marks",
        "routes",
    ):
        assert key in data, key

    runtime_block = data["runtime"]
    for key in (
        "wrapped",
        "supervisor",
        "marked",
        "pending_confirm",
        "allow_actions",
        "provider_manager",
        "internal_errors",
        "framework_fallback_hops",
        "all_candidates_marked",
        "corrupt_state",
    ):
        assert key in runtime_block, key
    assert runtime_block["marked"] == 1
    assert data["stats"]["totals"]["calls"] == 2

    provider_view = {item["provider_id"]: item for item in data["providers"]}
    assert provider_view["p1"]["marked_state"] == "marked"
    assert provider_view["p1"]["errors"] == 1
    assert provider_view["p2"]["marked_state"] is None
    for key in ("calls", "ok", "errors", "success_rate", "latency_ms", "last_error", "model"):
        assert key in provider_view["p1"], key

    route_view = data["routes"][0]
    for key in (
        "profile_id",
        "primary",
        "fallbacks",
        "effective_chain",
        "source",
        "marked",
        "bypassed",
        "degraded_use",
        "warnings",
    ):
        assert key in route_view, key
    assert route_view["bypassed"] == ["p1"]

    json.dumps(data, ensure_ascii=False)  # 必须可直接序列化


def test_routes_view(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    data = web_api.build_routes(runtime.guard)
    assert data["profiles"][0]["profile_id"] == "default"
    assert len(data["routes"]) == 1
    filtered = web_api.build_routes(runtime.guard, profile_id="nope")
    assert filtered["routes"] == []


def test_events_limit_clamped(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    seed(runtime)
    assert web_api.build_events(runtime.guard, limit=9999)["limit"] == web_api.MAX_EVENTS_LIMIT
    assert web_api.build_events(runtime.guard, limit=0)["limit"] == 1
    assert web_api.build_events(runtime.guard, limit="abc")["limit"] == 50
    events = web_api.build_events(runtime.guard, limit=10)["events"]
    assert events and "prompt" not in events[0]
    assert web_api.parse_limit("7") == 7


def test_health_view(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    data = web_api.build_health(runtime.guard)
    for key in (
        "wrapped",
        "supervisor",
        "internal_errors",
        "provider_manager",
        "enabled",
        "allow_actions",
        "state_file",
    ):
        assert key in data, key
    assert data["supervisor"] in ("running", "stopped")
    assert data["enabled"] is True


def test_write_actions_require_allow_actions(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    gated = asyncio.run(web_api.perform_action(runtime.guard, "mark", {"provider_id": "p1"}))
    assert gated["status"] == "error"
    assert "allow_webui_actions" in gated["message"]
    assert runtime.guardian.has_marked() is False


def test_mark_and_unmark_via_api(tmp_path: Path) -> None:
    config = dict(CONFIG, allow_webui_actions=True)
    runtime = make_runtime(tmp_path, config=config)

    marked = asyncio.run(
        web_api.perform_action(
            runtime.guard, "mark", {"provider_id": "p1", "ttl": 60, "reason": "手动"}
        )
    )
    assert marked["status"] == "ok"
    assert marked["data"]["marked"] is True
    assert runtime.guardian.marks()["p1"].reason == "手动"

    unmarked = asyncio.run(
        web_api.perform_action(runtime.guard, "unmark", {"provider_id": "p1"})
    )
    assert unmarked["status"] == "ok"
    assert unmarked["data"]["unmarked"] is True
    assert runtime.guardian.has_marked() is False


def test_probe_via_api(tmp_path: Path) -> None:
    config = dict(CONFIG, allow_webui_actions=True)
    runtime = make_runtime(tmp_path, config=config)
    result = asyncio.run(
        web_api.perform_action(runtime.guard, "probe", {"provider_id": "p1"})
    )
    assert result["status"] == "ok"
    assert result["data"]["provider_id"] == "p1"
    assert result["data"]["ok"] is True


def test_action_errors_are_envelopes(tmp_path: Path) -> None:
    config = dict(CONFIG, allow_webui_actions=True)
    runtime = make_runtime(tmp_path, config=config)

    unknown = asyncio.run(web_api.perform_action(runtime.guard, "explode", {}))
    assert unknown["status"] == "error"
    assert "未知操作" in unknown["message"]

    missing = asyncio.run(web_api.perform_action(runtime.guard, "mark", {}))
    assert missing["status"] == "error"
    assert "provider_id" in missing["message"]

    reloaded = asyncio.run(web_api.perform_action(runtime.guard, "reload", {}))
    assert reloaded["status"] == "error"  # 没有注入 reload_callback

    async def _reload() -> None:
        return None

    reloaded_ok = asyncio.run(
        web_api.perform_action(runtime.guard, "reload", {}, reload_callback=_reload)
    )
    assert reloaded_ok["status"] == "ok"


def test_probe_all_via_api(tmp_path: Path) -> None:
    config = dict(CONFIG, allow_webui_actions=True, double_confirm=False)
    runtime = make_runtime(tmp_path, config=config)
    asyncio.run(runtime.guardian.mark("p1", reason="auth", ttl=0))
    result = asyncio.run(web_api.perform_action(runtime.guard, "probe_all", {}))
    assert result["status"] == "ok"
    assert len(result["data"]["results"]) == 1
    assert runtime.guardian.has_marked() is False


def test_disabled_plugin_rejects_actions(tmp_path: Path) -> None:
    config = dict(CONFIG, enable=False, allow_webui_actions=True)
    runtime = make_runtime(tmp_path, config=config)
    result = asyncio.run(web_api.perform_action(runtime.guard, "mark", {"provider_id": "p1"}))
    assert result["status"] == "error"
    assert "enable=false" in result["message"]


def test_reload_endpoint(tmp_path: Path) -> None:
    config = dict(CONFIG, allow_webui_actions=True)
    runtime = make_runtime(tmp_path, config=config)

    async def _reload() -> str:
        return "已重载：路由 1 条"

    result = asyncio.run(
        web_api.perform_action(runtime.guard, "reload", {}, reload_callback=_reload)
    )
    assert result["status"] == "ok"
    assert result["data"]["reloaded"] is True
    assert "已重载" in result["data"]["summary"]

    # 默认（allow_webui_actions=false）时必须被门控
    gated_runtime = make_runtime(tmp_path / "gated")
    gated = asyncio.run(
        web_api.perform_action(gated_runtime.guard, "reload", {}, reload_callback=_reload)
    )
    assert gated["status"] == "error"
    assert "allow_webui_actions" in gated["message"]


def test_probe_response_has_ok_and_latency(tmp_path: Path) -> None:
    config = dict(CONFIG, allow_webui_actions=True)
    runtime = make_runtime(tmp_path, config=config)
    result = asyncio.run(
        web_api.perform_action(runtime.guard, "probe", {"provider_id": "p1"})
    )
    assert result["status"] == "ok"
    data = result["data"]
    assert data["ok"] is True
    assert isinstance(data["latency_ms"], int)
    assert data["provider_id"] == "p1"


def test_route_handlers_use_plugin_prefix() -> None:
    routes = {route for route, _method in web_api.route_handlers()}
    assert all(route.startswith("/astrbot_plugin_llm_model_guard/") for route in routes)
    assert web_api.ROUTE_PREFIX == "/astrbot_plugin_llm_model_guard"
    assert web_api.ROUTE_RELOAD in routes


def test_profiles_fallback_to_default(tmp_path: Path) -> None:
    runtime = build_runtime(
        tmp_path,
        config=CONFIG,
        providers=[FakeProvider("p1")],
    )
    runtime.context.astrbot_config_mgr = FakeAcm()
    profiles = web_api.collect_profiles(runtime.guard)
    assert any(item["profile_id"] == "default" for item in profiles)


def test_provider_model_name(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    providers = web_api.collect_providers(runtime.guard)
    p1 = next(item for item in providers if item["provider_id"] == "p1")
    assert p1["model"] == "p1-model"
    assert p1["exists"] is True


def test_no_duplicate_success_records(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    seed(runtime)
    totals = runtime.metrics.totals()
    assert totals["calls"] == 2
    events = web_api.build_events(runtime.guard, limit=10)
    assert events["total"] == 2
    assert all(set(event) >= {"ts", "used", "ok"} for event in events["events"])


# ---------------------------------------------------------------------------
# 第 2 轮修复：P1-1 profile 作用域标记在页面/命令里可见
# ---------------------------------------------------------------------------


def test_profile_marks_visible_in_api(tmp_path: Path) -> None:
    config = dict(CONFIG, mark_scope="profile")
    runtime = build_runtime(
        tmp_path,
        config=config,
        providers=[FakeProvider("p1"), FakeProvider("p2")],
        profiles={UMO: "default"},
    )
    asyncio.run(
        runtime.guardian.mark("p1", reason="auth", ttl=300, profile_hint="default")
    )

    data = web_api.build_overview(runtime.guard)
    assert data["runtime"]["marked"] == 1
    provider_view = {item["provider_id"]: item for item in data["providers"]}
    assert provider_view["p1"]["marked_state"] == "marked"
    assert provider_view["p1"]["marked_reason"] == "auth"
    assert any(item["provider_id"] == "p1" for item in data["marks"])

    route_view = data["routes"][0]
    assert "p1" in route_view["bypassed"]
    assert route_view["effective_chain"] == ["p2"]


def test_profile_marks_visible_in_commands(tmp_path: Path) -> None:
    from astrbot_plugin_llm_model_guard.core import commands as command_text

    config = dict(CONFIG, mark_scope="profile")
    runtime = build_runtime(
        tmp_path,
        config=config,
        providers=[FakeProvider("p1"), FakeProvider("p2")],
        profiles={UMO: "default"},
    )
    asyncio.run(
        runtime.guardian.mark("p1", reason="auth", ttl=300, profile_hint="default")
    )

    marks_text = command_text.format_marks(runtime.guard)
    assert "p1" in marks_text
    assert "作用域=default" in marks_text

    status_text = command_text.format_status(runtime.guard)
    assert "已标记: 1 个" in status_text

    models_text = command_text.format_models(runtime.guard)
    assert "[已标记]" in models_text

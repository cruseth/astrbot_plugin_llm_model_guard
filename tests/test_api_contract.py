"""Web API 契约单测（v0.2.0）：信封结构、链路视图、事件与写操作门控。

v0.2.0 的端点：``overview`` / ``chains`` / ``events`` / ``mark`` / ``unmark`` /
``probe`` / ``reload``（已删除 ``profiles``、``schema_refresh``、``routes``、
``health``）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from astrbot_plugin_llm_model_guard.core import web_api
from astrbot_plugin_llm_model_guard.core.models import AttemptRecord

from .support import FakeAcm, FakeProvider, build_runtime

UMO = "aiocqhttp:FriendMessage:40004"

CONFIG: dict = {}


def make_runtime(tmp_path: Path, *, config: dict | None = None):
    return build_runtime(
        tmp_path,
        config=CONFIG if config is None else config,
        providers=[FakeProvider("p1"), FakeProvider("p2")],
        profiles={UMO: "default"},
        defaults={"default": "p1"},
        conf_fallbacks={"default": ["p2"]},
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
    asyncio.run(runtime.guardian.mark("p1", reason="rate_limit"))


# ---------------------------------------------------------------------------
# 信封
# ---------------------------------------------------------------------------


def test_envelope_helpers() -> None:
    ok = web_api.envelope_ok({"a": 1})
    assert ok == {"status": "ok", "message": None, "data": {"a": 1}}
    error = web_api.envelope_error("坏了")
    assert error["status"] == "error"
    assert error["message"] == "坏了"
    assert error["data"] is None
    assert json.loads(json.dumps(ok))["status"] == "ok"


# ---------------------------------------------------------------------------
# overview
# ---------------------------------------------------------------------------


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
        "chains",
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

    # v0.2.0 的配置摘要就是那 8 个键。
    for key in (
        "enable",
        "request_timeout_seconds",
        "max_retries",
        "retry_backoff_seconds",
        "retry_categories",
        "probe_interval_seconds",
        "probe_timeout_seconds",
        "probe_success_threshold",
    ):
        assert key in data["config"], key

    json.dumps(data, ensure_ascii=False)  # 必须可直接序列化


# ---------------------------------------------------------------------------
# chains
# ---------------------------------------------------------------------------


def test_chains_view(tmp_path: Path) -> None:
    """``/chains`` 展示每个配置文件的模型顺序。"""
    runtime = make_runtime(tmp_path)
    data = web_api.build_chains(runtime.guard)
    assert "profiles" in data
    assert "chains" in data
    chains = data["chains"]
    assert chains
    item = chains[0]
    for key in (
        "profile_id",
        "profile_name",
        "profile_display",
        "default_provider",
        "fallbacks",
        "chain",
        "marked",
        "missing",
    ):
        assert key in item, key
    assert item["default_provider"] == "p1"
    assert item["fallbacks"] == ["p2"]
    assert item["chain"] == ["p1", "p2"]


def test_chains_filter_by_profile(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    filtered = web_api.build_chains(runtime.guard, profile_id="nope")
    assert filtered["chains"] == []


def test_chains_shows_marked(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    asyncio.run(runtime.guardian.mark("p1", reason="rate_limit"))
    chains = web_api.build_chains(runtime.guard)["chains"]
    assert chains[0]["marked"] == ["p1"]
    assert chains[0]["chain"] == ["p2"]


def test_chains_fallback_to_default_profile(tmp_path: Path) -> None:
    """读不到配置文件列表时至少有 default。"""
    runtime = build_runtime(
        tmp_path,
        config=CONFIG,
        providers=[FakeProvider("p1")],
    )
    runtime.context.astrbot_config_mgr = FakeAcm()
    chains = web_api.collect_chains(runtime.guard)
    assert any(item["profile_id"] == "default" for item in chains)


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------


def test_events_limit_clamped(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    seed(runtime)
    assert web_api.build_events(runtime.guard, limit=9999)["limit"] == web_api.MAX_EVENTS_LIMIT
    assert web_api.build_events(runtime.guard, limit=0)["limit"] == 1
    assert web_api.build_events(runtime.guard, limit="abc")["limit"] == 50
    events = web_api.build_events(runtime.guard, limit=10)["events"]
    assert events and "prompt" not in events[0]
    assert web_api.parse_limit("7") == 7


# ---------------------------------------------------------------------------
# 写操作
# ---------------------------------------------------------------------------


def test_write_actions_require_allow_actions(tmp_path: Path) -> None:
    """显式关闭 ``allow_webui_actions`` 时写操作被拒绝。"""
    runtime = make_runtime(tmp_path, config={"allow_webui_actions": False})
    gated = asyncio.run(web_api.perform_action(runtime.guard, "mark", {"provider_id": "p1"}))
    assert gated["status"] == "error"
    assert "allow_webui_actions" in gated["message"]
    assert runtime.guardian.has_marked() is False


def test_write_actions_allowed_by_default(tmp_path: Path) -> None:
    """默认**关闭**写操作；显式开启后可用。"""
    runtime = make_runtime(tmp_path)
    assert runtime.guard.settings.allow_webui_actions is False
    blocked = asyncio.run(
        web_api.perform_action(runtime.guard, "mark", {"provider_id": "p1"})
    )
    assert blocked["status"] == "error"
    assert "allow_webui_actions" in blocked["message"]

    enabled = make_runtime(tmp_path / "on", config={"allow_webui_actions": True})
    result = asyncio.run(
        web_api.perform_action(enabled.guard, "mark", {"provider_id": "p1"})
    )
    assert result["status"] == "ok"
    assert result["data"]["marked"] is True


def test_overview_allow_actions_reflects_config(tmp_path: Path) -> None:
    """``/overview`` 的 ``runtime.allow_actions`` 反映真实配置值。"""
    disabled = make_runtime(tmp_path)
    assert web_api.build_overview(disabled.guard)["runtime"]["allow_actions"] is False

    enabled = make_runtime(
        tmp_path / "on", config={"allow_webui_actions": True}
    )
    assert web_api.build_overview(enabled.guard)["runtime"]["allow_actions"] is True


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
    config = dict(CONFIG, allow_webui_actions=True, probe_success_threshold=1)
    runtime = make_runtime(tmp_path, config=config)
    asyncio.run(runtime.guardian.mark("p1", reason="auth"))
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
        return "已重载"

    result = asyncio.run(
        web_api.perform_action(runtime.guard, "reload", {}, reload_callback=_reload)
    )
    assert result["status"] == "ok"
    assert result["data"]["reloaded"] is True
    assert "已重载" in result["data"]["summary"]

    gated_runtime = make_runtime(
        tmp_path / "gated", config={"allow_webui_actions": False}
    )
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


# ---------------------------------------------------------------------------
# 路由清单
# ---------------------------------------------------------------------------


def test_route_handlers_use_plugin_prefix() -> None:
    routes = {route for route, _method in web_api.route_handlers()}
    assert all(route.startswith("/astrbot_plugin_llm_model_guard/") for route in routes)
    assert web_api.ROUTE_PREFIX == "/astrbot_plugin_llm_model_guard"
    assert web_api.ROUTE_RELOAD in routes


def test_v2_route_set_is_exactly_seven() -> None:
    """v0.2.0 的端点集合固定为 7 个。"""
    routes = {route for route, _method in web_api.route_handlers()}
    assert routes == {
        web_api.ROUTE_OVERVIEW,
        web_api.ROUTE_CHAINS,
        web_api.ROUTE_EVENTS,
        web_api.ROUTE_MARK,
        web_api.ROUTE_UNMARK,
        web_api.ROUTE_PROBE,
        web_api.ROUTE_RELOAD,
    }


def test_removed_endpoints_are_gone() -> None:
    """v0.1.x 的 ``/profiles``、``/schema_refresh``、``/routes``、``/health`` 已删除。"""
    routes = {route for route, _method in web_api.route_handlers()}
    assert "/astrbot_plugin_llm_model_guard/profiles" not in routes
    assert "/astrbot_plugin_llm_model_guard/schema_refresh" not in routes
    assert "/astrbot_plugin_llm_model_guard/routes" not in routes
    assert "/astrbot_plugin_llm_model_guard/health" not in routes


# ---------------------------------------------------------------------------
# 其它
# ---------------------------------------------------------------------------


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


def test_marks_visible_in_api(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    seed(runtime)
    data = web_api.build_overview(runtime.guard)
    assert data["runtime"]["marked"] == 1
    provider_view = {item["provider_id"]: item for item in data["providers"]}
    assert provider_view["p1"]["marked_state"] == "marked"
    assert provider_view["p1"]["marked_reason"] == "rate_limit"
    assert any(item["provider_id"] == "p1" for item in data["marks"])


def test_marks_visible_in_commands(tmp_path: Path) -> None:
    from astrbot_plugin_llm_model_guard.core import commands as command_text

    runtime = make_runtime(tmp_path)
    seed(runtime)

    marks_text = command_text.format_marks(runtime.guard)
    assert "p1" in marks_text
    assert "连续成功" in marks_text

    status_text = command_text.format_status(runtime.guard)
    assert "已标记: 1 个" in status_text

    chains_text = command_text.format_chains(runtime.guard)
    assert "p1" in chains_text

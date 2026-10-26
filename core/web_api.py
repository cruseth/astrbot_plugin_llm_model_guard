"""Web API 视图：只读汇总 + 受开关约束的写操作。

约定（规划文档 §14.2 + 主 Agent 裁决）：

* 注册 route 必须形如 ``/astrbot_plugin_llm_model_guard/<endpoint>``，
  因为前端桥接把 endpoint 拼成 ``/api/plug/<pluginName>/<endpoint>``；
* 响应体是仪表盘信封 ``{"status": "ok", "data": {...}}`` /
  ``{"status": "error", "message": "..."}``；
* 写操作（mark / unmark / probe）受 ``allow_webui_actions`` 门控，默认关闭。

本模块不导入 AstrBot：视图函数返回纯 dict，``main.py`` 负责 ``jsonify``。
"""

from __future__ import annotations

import time
from typing import Any, Awaitable, Callable, Mapping, Sequence

from .models import MARK_SCOPE_PROFILE, MarkState
from .routing import describe_route, describe_warning, resolve_profile_ref

#: 注册路由的统一前缀（必须与插件名一致）。
ROUTE_PREFIX = "/astrbot_plugin_llm_model_guard"

#: 只读端点。
ROUTE_OVERVIEW = f"{ROUTE_PREFIX}/overview"
ROUTE_ROUTES = f"{ROUTE_PREFIX}/routes"
ROUTE_EVENTS = f"{ROUTE_PREFIX}/events"
ROUTE_HEALTH = f"{ROUTE_PREFIX}/health"
ROUTE_PROFILES = f"{ROUTE_PREFIX}/profiles"
#: 重新注入 schema 选项（幂等）；框架里新建/删除配置文件后可手动触发。
ROUTE_SCHEMA_REFRESH = f"{ROUTE_PREFIX}/schema_refresh"

#: 写端点。
ROUTE_MARK = f"{ROUTE_PREFIX}/mark"
ROUTE_UNMARK = f"{ROUTE_PREFIX}/unmark"
ROUTE_PROBE = f"{ROUTE_PREFIX}/probe"
ROUTE_RELOAD = f"{ROUTE_PREFIX}/reload"

#: 需要 ``allow_webui_actions`` 才能执行的动作。
WRITE_ACTIONS = frozenset({"mark", "unmark", "probe", "probe_all", "test", "reload"})

#: events 端点的 limit 上限。
MAX_EVENTS_LIMIT = 500


def envelope_ok(data: Mapping[str, Any] | None = None, message: str | None = None) -> dict[str, Any]:
    """成功信封；字段名与 ``dashboard.routes.route.Response`` 一致。"""
    return {"status": "ok", "message": message, "data": dict(data or {})}


def envelope_error(message: str) -> dict[str, Any]:
    """失败信封。"""
    return {"status": "error", "message": message, "data": None}


def make_response(
    *, status: str, message: str | None = None, data: Any = None
) -> dict[str, Any]:
    """优先复用框架 ``Response``；导入不可用时退化为等价 dict。"""
    try:  # pragma: no cover - 依赖真实 AstrBot 运行时
        from astrbot.dashboard.routes.route import Response  # type: ignore
    except Exception:
        Response = None  # type: ignore[assignment]
    if Response is not None:
        try:  # pragma: no cover - 依赖真实 AstrBot 运行时
            response = Response(status=status, message=message, data=data)
            payload = getattr(response, "__dict__", None)
            if isinstance(payload, dict) and payload.get("status"):
                return payload
        except Exception:
            pass
    return {"status": status, "message": message, "data": data}


def _provider_model_name(guard: Any, provider_id: str) -> str:
    provider = guard.provider_for(provider_id)
    if provider is None:
        return ""
    getter = getattr(provider, "get_model", None)
    if callable(getter):
        try:
            return str(getter())
        except Exception:
            return ""
    config = getattr(provider, "provider_config", None)
    if isinstance(config, Mapping):
        model = config.get("model")
        if isinstance(model, str):
            return model
    return ""


def collect_providers(guard: Any) -> list[dict[str, Any]]:
    """``ProviderStat`` 列表：统计 + 模型名 + 标记状态。

    标记状态取**全部作用域**的视图（``mark_index``），因此
    ``mark_scope="profile"`` 下的标记也能在页面看到（P1-1）。
    """
    stats = guard.metrics.per_provider()
    marks = guard.guardian.mark_index()
    known_ids = list(guard.provider_ids())
    ordered_ids = list(dict.fromkeys([*known_ids, *stats.keys()]))
    result: list[dict[str, Any]] = []
    for provider_id in ordered_ids:
        stat = stats.get(provider_id) or {}
        view = marks.get(provider_id)
        result.append(
            {
                "provider_id": provider_id,
                "model": _provider_model_name(guard, provider_id),
                "exists": provider_id in known_ids,
                "calls": stat.get("calls", 0),
                "ok": stat.get("ok", 0),
                "errors": stat.get("errors", 0),
                "consecutive_errors": stat.get("consecutive_errors", 0),
                "success_rate": stat.get("success_rate"),
                "error_classes": stat.get("error_classes") or {},
                "last_error": stat.get("last_error"),
                "last_ok_at": stat.get("last_ok_at"),
                "last_error_at": stat.get("last_error_at"),
                "latency_ms": stat.get("latency_ms")
                or {"last": 0, "avg": 0, "p50": 0, "p95": 0, "n": 0},
                "probe": stat.get("probe"),
                "marked_state": str(view.state) if view is not None else None,
                "marked_reason": view.reason if view is not None else None,
            }
        )
    return result


def collect_routes(guard: Any) -> list[dict[str, Any]]:
    """``RouteView`` 列表：按路由表顺序输出（标记视图取全部作用域，P1-1）。"""
    settings = guard.settings
    marks = guard.guardian.mark_index()
    known_ids = guard.provider_ids()
    names = profile_name_map(guard)
    result: list[dict[str, Any]] = []
    for profile_id, route in guard.route_table.items():
        result.append(
            describe_route(
                route,
                marks=marks,
                known_ids=known_ids,
                fallback_ids=(),
                fallback_pool=settings.fallback_pool,
                union_framework_fallbacks=settings.union_framework_fallbacks,
                profile_name=names.get(profile_id),
            )
        )
    if settings.disabled_profiles:
        for code in sorted(settings.disabled_profiles):
            result.append(
                {
                    "profile_id": code,
                    "profile_name": names.get(code, code),
                    "profile_display": (
                        f"{names[code]} ({code})" if code in names else code
                    ),
                    "primary": "",
                    "fallbacks": [],
                    "effective_chain": [],
                    "source": "disabled",
                    "marked": [],
                    "bypassed": [],
                    "degraded_use": None,
                    "missing": [],
                    "warnings": [
                        {"code": "profile_disabled", "message": describe_warning("profile_disabled")}
                    ],
                    "enable": False,
                    "note": "",
                }
            )
    return result


def profile_name_map(guard: Any) -> dict[str, str]:
    """``{profile_id: 配置文件名}``；取不到名称时返回空映射。"""
    entries = getattr(guard, "profile_entries", None) or []
    result: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        profile_id = str(entry.get("id") or "").strip()
        if not profile_id:
            continue
        name = str(entry.get("name") or "").strip()
        if name and name != profile_id:
            result[profile_id] = name
    return result


def collect_profiles(guard: Any) -> list[dict[str, Any]]:
    """所有配置文件（abconf）的 id、名字与当前默认模型，便于页面展示与排查。

    框架没有 abconf 选择器，插件用这个只读接口把「可选的配置文件」暴露给页面，
    用户不必手抄 uuid。
    """
    acm = guard.acm
    known_ids = set(guard.provider_ids())
    getter = getattr(acm, "get_conf_list", None)
    profiles: list[dict[str, Any]] = []
    if callable(getter):
        try:
            for info in getter() or []:
                if isinstance(info, Mapping):
                    profiles.append(
                        {
                            "profile_id": str(info.get("id") or ""),
                            "name": str(info.get("name") or info.get("id") or ""),
                            "path": str(info.get("path") or ""),
                        }
                    )
        except Exception:
            profiles = []
    if not any(item.get("profile_id") == "default" for item in profiles):
        profiles.insert(0, {"profile_id": "default", "name": "default", "path": ""})

    result: list[dict[str, Any]] = []
    for item in profiles:
        profile_id = item["profile_id"]
        default_provider = _profile_default_provider(guard, profile_id)
        result.append(
            {
                "id": profile_id,
                "profile_id": profile_id,
                "name": item["name"],
                "path": item["path"],
                "is_default": profile_id == "default",
                "default_provider": default_provider,
                "default_provider_exists": (
                    default_provider in known_ids if default_provider else None
                ),
                "display": (
                    f"{item['name']} ({profile_id})"
                    if item["name"] and item["name"] != profile_id
                    else profile_id
                ),
            }
        )
    return result


def _profile_default_provider(guard: Any, profile_id: str) -> str:
    """读某个配置文件当前的默认 provider（``provider_settings.default_provider_id``）。"""
    acm = guard.acm
    getter = getattr(acm, "get_conf", None)
    if not callable(getter):
        return ""
    conf = None
    # 框架的 ``get_conf(umo)`` 需要 umo；配置文件 id 走 ``confs`` 字典更直接。
    confs = getattr(acm, "confs", None)
    if isinstance(confs, Mapping) and profile_id in confs:
        conf = confs[profile_id]
    else:
        try:
            conf = getter(profile_id)
        except Exception:
            conf = None
    if not isinstance(conf, Mapping):
        return ""
    settings_block = conf.get("provider_settings")
    if not isinstance(settings_block, Mapping):
        return ""
    value = settings_block.get("default_provider_id")
    return value.strip() if isinstance(value, str) else ""


def build_overview(guard: Any) -> dict[str, Any]:
    """``GET /overview`` 的 ``data``。"""
    settings = guard.settings
    metrics = guard.metrics
    guardian = guard.guardian
    overview = metrics.overview(window_s=settings.stats_window_seconds)
    supervisors = "running" if guardian.supervisor_running else "stopped"
    marks = guardian.all_marks()
    return {
        "enabled": settings.enable,
        "generated_at": time.time(),
        "config": settings.to_public_dict(),
        "runtime": {
            "wrapped": guard.wrapped_count,
            "supervisor": supervisors,
            "marked": len(marks),
            "pending_confirm": sum(
                1 for view in marks.values() if view.state is MarkState.CONFIRMING
            ),
            "allow_actions": bool(settings.allow_webui_actions),
            "provider_manager": guard.provider_manager is not None,
            "mark_scope": settings.mark_scope,
            "mark_scope_is_profile": settings.mark_scope == MARK_SCOPE_PROFILE,
            "internal_errors": metrics.counters.get("guard_internal_errors", 0),
            "framework_fallback_hops": metrics.counters.get("framework_fallback_hops", 0),
            "all_candidates_marked": metrics.counters.get("all_candidates_marked", 0),
            "corrupt_state": metrics.counters.get("corrupt_state", 0),
            "state_file": str(guard.state_path) if getattr(guard, "state_path", None) else "",
        },
        "stats": {
            "window_s": overview.get("window_s"),
            "totals": overview.get("totals") or {},
            "counters": overview.get("counters") or {},
        },
        "providers": collect_providers(guard),
        "marks": [
            dict(view.to_dict(), scope_key=key) for key, view in marks.items()
        ],
        "routes": collect_routes(guard),
    }


def build_routes(guard: Any, *, profile_id: str | None = None) -> dict[str, Any]:
    """``GET /routes`` 的 ``data``。"""
    routes = collect_routes(guard)
    if profile_id:
        routes = [item for item in routes if item.get("profile_id") == profile_id]
    return {"profiles": collect_profiles(guard), "routes": routes}


def build_profiles(guard: Any) -> dict[str, Any]:
    """``GET /profiles`` 的 ``data``。

    返回框架的配置文件列表（id / name / path / 默认模型），以及**没能解析**
    的路由引用（``unmatched``），让页面能明确提示「这条路由没接管任何配置文件」。
    """
    profiles = collect_profiles(guard)
    entries = [
        {"id": item["id"], "name": item["name"], "path": item["path"]}
        for item in profiles
    ]
    unmatched: list[str] = []
    for route in guard.settings.profiles:
        ref = route.profile_ref
        if not ref:
            continue
        result = resolve_profile_ref(ref, entries)
        if not result.ok:
            unmatched.append(ref)
    for ref in sorted(guard.settings.disabled_profiles):
        result = resolve_profile_ref(ref, entries)
        if not result.ok:
            unmatched.append(ref)
    return {
        "profiles": profiles,
        "unmatched": sorted(set(unmatched)),
    }


def build_events(
    guard: Any,
    *,
    limit: int = 50,
    provider_id: str | None = None,
    profile_id: str | None = None,
) -> dict[str, Any]:
    """``GET /events`` 的 ``data``；``limit`` 被夹在 1..500。"""
    try:
        requested = int(limit)
    except (TypeError, ValueError):
        requested = 50
    clamped = max(1, min(requested, MAX_EVENTS_LIMIT))
    events = guard.metrics.recent_events(
        limit=clamped,
        provider_id=provider_id or None,
        profile_id=profile_id or None,
    )
    return {
        "events": events,
        "total": guard.metrics.events_total(),
        "limit": clamped,
    }


def build_health(guard: Any) -> dict[str, Any]:
    """``GET /health`` 的 ``data``。"""
    metrics = guard.metrics
    guardian = guard.guardian
    return {
        "wrapped": guard.wrapped_count,
        "supervisor": "running" if guardian.supervisor_running else "stopped",
        "internal_errors": metrics.counters.get("guard_internal_errors", 0),
        "provider_manager": guard.provider_manager is not None,
        "enabled": guard.settings.enable,
        "allow_actions": bool(guard.settings.allow_webui_actions),
        "state_file": str(getattr(guard, "state_path", "") or ""),
        "last_load_error": getattr(guard, "state_load_error", None),
    }


async def perform_action(
    guard: Any,
    action: str,
    body: Mapping[str, Any] | None = None,
    *,
    reload_callback: Callable[[], Awaitable[Any]] | None = None,
) -> dict[str, Any]:
    """执行页面写操作，返回信封 dict（成功或失败都用信封）。"""
    payload = dict(body or {})
    name = (action or "").strip().lower()
    settings = guard.settings

    if not settings.enable:
        return envelope_error("插件已被禁用（配置项 enable=false），页面操作不可用。")
    if name in WRITE_ACTIONS and not settings.allow_webui_actions:
        return envelope_error(
            "页面写操作已在插件配置中禁用（allow_webui_actions=false），"
            "请改用管理员命令 /llmguard，或在插件配置里开启该开关。"
        )

    if name in ("mark",):
        provider_id = str(payload.get("provider_id") or "").strip()
        if not provider_id:
            return envelope_error("缺少 provider_id。")
        try:
            ttl = max(0, int(payload.get("ttl") or 0))
        except (TypeError, ValueError):
            ttl = 0
        reason = str(payload.get("reason") or "manual").strip() or "manual"
        marked = await guard.guardian.mark(
            provider_id,
            reason=reason,
            ttl=ttl,
            source="manual",
            detail="通过 Web 页面手动标记",
            profile_hint="",
        )
        return envelope_ok({"provider_id": provider_id, "marked": bool(marked)})

    if name in ("unmark",):
        provider_id = str(payload.get("provider_id") or "").strip()
        if not provider_id:
            return envelope_error("缺少 provider_id。")
        removed = await guard.guardian.unmark(provider_id, by="webui")
        return envelope_ok({"provider_id": provider_id, "unmarked": bool(removed)})

    if name in ("probe", "test"):
        provider_id = str(payload.get("provider_id") or "").strip()
        if not provider_id:
            return envelope_error("缺少 provider_id。")
        result = await guard.guardian.probe_now(provider_id)
        return envelope_ok(result.to_dict())

    if name == "probe_all":
        results = await guard.guardian.probe_all()
        return envelope_ok({"results": [item.to_dict() for item in results]})

    if name == "reload":
        if reload_callback is None:
            return envelope_error("当前环境不支持热重载，请重启插件或 AstrBot。")
        result = await reload_callback()
        summary = result if isinstance(result, str) else ""
        data: dict[str, Any] = {"reloaded": True}
        if summary:
            data["summary"] = summary
        return envelope_ok(data)

    return envelope_error(f"未知操作：{action}（支持 mark/unmark/probe/probe_all/reload）")


def parse_limit(value: Any, default: int = 50) -> int:
    """把 query 里的字符串 limit 转成夹紧后的整数。"""
    try:
        requested = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(requested, MAX_EVENTS_LIMIT))


def route_handlers() -> Sequence[tuple[str, str]]:
    """返回 ``(route, method)`` 清单，便于 main.py 注册与自检。"""
    return (
        (ROUTE_OVERVIEW, "GET"),
        (ROUTE_ROUTES, "GET"),
        (ROUTE_EVENTS, "GET"),
        (ROUTE_HEALTH, "GET"),
        (ROUTE_PROFILES, "GET"),
        (ROUTE_SCHEMA_REFRESH, "GET"),
        (ROUTE_MARK, "POST"),
        (ROUTE_UNMARK, "POST"),
        (ROUTE_PROBE, "POST"),
        (ROUTE_RELOAD, "POST"),
    )

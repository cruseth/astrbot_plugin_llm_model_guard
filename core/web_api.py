"""Web API 视图：只读汇总 + 受开关约束的写操作。

v0.2.0 的端点清单（任务包 12 §4.3）：

* ``GET  /overview`` —— 总览（开关、统计、模型列表、标记、链路、配置）
* ``GET  /chains`` —— **每个框架配置文件的只读链路视图**
* ``GET  /events`` —— 最近调用记录
* ``POST /mark``、``/unmark``、``/probe``、``/reload``

已删除（v0.1.x）：``/profiles``、``/schema_refresh``、``/routes``、``/health``。

约定：

* 注册 route 必须形如 ``/astrbot_plugin_llm_model_guard/<endpoint>``，
  因为前端桥接把 endpoint 拼成 ``/api/plug/<pluginName>/<endpoint>``；
* 响应体是仪表盘信封 ``{"status": "ok", "data": {...}}`` /
  ``{"status": "error", "message": "..."}``；
* 写操作受 ``allow_webui_actions`` 门控，**默认关闭**（该键在 schema 里，
  用户可在插件配置中开启）。``/overview`` 的 ``runtime.allow_actions``
  反映真实配置值，页面据此显示/隐藏操作按钮。

本模块不导入 AstrBot：视图函数返回纯 dict，``main.py`` 负责 ``jsonify``。
"""

from __future__ import annotations

import time
from typing import Any, Awaitable, Callable, Mapping

from .models import MarkState

#: 注册路由的统一前缀（必须与插件名一致）。
ROUTE_PREFIX = "/astrbot_plugin_llm_model_guard"

#: 只读端点。
ROUTE_OVERVIEW = f"{ROUTE_PREFIX}/overview"
ROUTE_CHAINS = f"{ROUTE_PREFIX}/chains"
ROUTE_EVENTS = f"{ROUTE_PREFIX}/events"

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
    """``ProviderStat`` 列表：统计 + 模型名 + 标记状态。"""
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


def _conf_list(guard: Any) -> list[dict[str, str]]:
    """框架里的配置文件列表（id / name / path）。"""
    acm = guard.acm
    getter = getattr(acm, "get_conf_list", None)
    entries: list[dict[str, str]] = []
    if callable(getter):
        try:
            for info in getter() or []:
                if isinstance(info, Mapping):
                    profile_id = str(info.get("id") or "").strip()
                    if not profile_id:
                        continue
                    entries.append(
                        {
                            "id": profile_id,
                            "name": str(info.get("name") or profile_id),
                            "path": str(info.get("path") or ""),
                        }
                    )
        except Exception:
            entries = []
    if not any(item["id"] == "default" for item in entries):
        entries.insert(0, {"id": "default", "name": "default", "path": ""})
    return entries


def _abconf_of(guard: Any, profile_id: str) -> Mapping[str, Any] | None:
    """取某个配置文件的 abconf 内容（用于读默认模型与回退列表）。"""
    acm = guard.acm
    confs = getattr(acm, "confs", None)
    if isinstance(confs, Mapping) and profile_id in confs:
        conf = confs[profile_id]
        if isinstance(conf, Mapping):
            return conf
    getter = getattr(acm, "get_conf", None)
    if callable(getter):
        try:
            conf = getter(profile_id)
        except Exception:
            return None
        if isinstance(conf, Mapping):
            return conf
    return None


def _default_provider_of(conf: Mapping[str, Any] | None) -> str:
    if not isinstance(conf, Mapping):
        return ""
    settings = conf.get("provider_settings")
    if not isinstance(settings, Mapping):
        return ""
    value = settings.get("default_provider_id")
    return value.strip() if isinstance(value, str) else ""


def _fallbacks_of(conf: Mapping[str, Any] | None) -> list[str]:
    if not isinstance(conf, Mapping):
        return []
    settings = conf.get("provider_settings")
    if not isinstance(settings, Mapping):
        return []
    fallbacks = settings.get("fallback_chat_models")
    if not isinstance(fallbacks, (list, tuple)):
        return []
    return [item.strip() for item in fallbacks if isinstance(item, str) and item.strip()]


def collect_chains(guard: Any) -> list[dict[str, Any]]:
    """**每个框架配置文件**的只读链路视图。

    这是「兼容框架每个配置文件（模型顺序）」的可视化：链路直接来自框架配置，
    插件**没有**对应的配置项（v0.2.0 的配置里零 id）。
    """
    marked = set(guard.guardian.marked_ids())
    known = set(guard.provider_ids())
    result: list[dict[str, Any]] = []
    for entry in _conf_list(guard):
        profile_id = entry["id"]
        conf = _abconf_of(guard, profile_id)
        default_provider = _default_provider_of(conf)
        fallbacks = _fallbacks_of(conf)

        ordered: list[str] = []
        for provider_id in (default_provider, *fallbacks):
            if provider_id and provider_id not in ordered:
                ordered.append(provider_id)
        missing = [pid for pid in ordered if known and pid not in known]
        bypassed = [pid for pid in ordered if pid in marked]

        result.append(
            {
                "profile_id": profile_id,
                "profile_name": entry["name"],
                "profile_display": (
                    f"{entry['name']} ({profile_id})"
                    if entry["name"] and entry["name"] != profile_id
                    else profile_id
                ),
                "path": entry["path"],
                "is_default": profile_id == "default",
                "default_provider": default_provider,
                "fallbacks": fallbacks,
                "chain": [pid for pid in ordered if pid not in bypassed],
                "marked": bypassed,
                "missing": missing,
            }
        )
    return result


def build_overview(guard: Any) -> dict[str, Any]:
    """``GET /overview`` 的 ``data``。"""
    settings = guard.settings
    metrics = guard.metrics
    guardian = guard.guardian
    overview = metrics.overview(window_s=900)
    marks = guardian.all_marks()
    return {
        "enabled": settings.enable,
        "generated_at": time.time(),
        "config": settings.to_public_dict(),
        "runtime": {
            "wrapped": guard.wrapped_count,
            "supervisor": "running" if guardian.supervisor_running else "stopped",
            "marked": len(marks),
            "pending_confirm": 0,
            # 反映真实配置值：页面据此决定是否显示「测活 / 标记 / 解除标记」按钮。
            "allow_actions": bool(settings.allow_webui_actions),
            "provider_manager": guard.provider_manager is not None,
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
        "marks": [dict(view.to_dict()) for view in marks.values()],
        "chains": collect_chains(guard),
    }


def build_chains(guard: Any, *, profile_id: str | None = None) -> dict[str, Any]:
    """``GET /chains`` 的 ``data``。"""
    chains = collect_chains(guard)
    if profile_id:
        chains = [item for item in chains if item.get("profile_id") == profile_id]
    return {"profiles": chains, "chains": chains}


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
            "请改用管理员命令 /llmguard。"
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


def route_handlers() -> tuple[tuple[str, str], ...]:
    """返回 ``(route, method)`` 清单，便于 main.py 注册与自检。"""
    return (
        (ROUTE_OVERVIEW, "GET"),
        (ROUTE_CHAINS, "GET"),
        (ROUTE_EVENTS, "GET"),
        (ROUTE_MARK, "POST"),
        (ROUTE_UNMARK, "POST"),
        (ROUTE_PROBE, "POST"),
        (ROUTE_RELOAD, "POST"),
    )

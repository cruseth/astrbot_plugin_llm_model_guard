"""路由解析：``umo`` → 配置文件 → 主模型与候选链。

职责边界（规划文档 §4.2）：

* 本模块只做纯计算，不访问 ``context``、不做 I/O；
* AstrBot 的配置管理器通过参数注入（鸭子类型，只需要 ``get_conf_info``）；
* 标记信息以 ``Mapping[str, MarkView]`` 传入，本模块不持有状态。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from .models import (
    ChainPlan,
    MarkView,
    RouteConfig,
    WILDCARD_PROFILE,
)

#: 路由表构建时的告警码（用于页面与日志，不静默）。
WARN_ROUTE_DUPLICATE = "route_duplicate"
WARN_ROUTE_PROVIDER_MISSING = "route_provider_missing"
WARN_ROUTE_PRIMARY_EMPTY = "route_primary_empty"
WARN_ALL_CANDIDATES_MARKED = "all_candidates_marked"
WARN_PROFILE_DISABLED = "profile_disabled"


def resolve_profile_id(umo: str | None, acm: Any) -> str | None:
    """``umo`` → ``"default"`` 或 abconf 的 uuid；解析失败返回 ``None``。

    依据框架事实：``AstrBotConfigManager.get_conf_info(umo)`` 返回
    ``{"id","name","path"}``，``id`` 为 ``default`` 或 abconf uuid。
    """
    if not umo or acm is None:
        return None
    getter = getattr(acm, "get_conf_info", None)
    if not callable(getter):
        return None
    try:
        info = getter(umo)
    except Exception:
        return None
    if isinstance(info, Mapping):
        profile_id = info.get("id")
        if isinstance(profile_id, str) and profile_id.strip():
            return profile_id.strip()
        return None
    if isinstance(info, str) and info.strip():
        return info.strip()
    return None


def build_route_table(
    routes_conf: Sequence[Mapping[str, Any]] | Iterable[Mapping[str, Any]] | None,
    *,
    warnings: list[str] | None = None,
) -> dict[str, RouteConfig]:
    """配置 → ``{profile_id: RouteConfig}``；``*``/空键归一化为兜底路由。

    同一 ``profile_id`` 出现多条时**第一条生效**，其余记 ``route_duplicate``
    告警但不覆盖（规划文档 §7.2-1）。
    """
    table: dict[str, RouteConfig] = {}
    if not routes_conf:
        return table
    for raw in routes_conf:
        route = raw if isinstance(raw, RouteConfig) else None
        if route is None:
            route = _route_from_any(raw)
        if route is None:
            continue
        key = route.profile_id or WILDCARD_PROFILE
        if key in table:
            _warn(warnings, f"{WARN_ROUTE_DUPLICATE}:{key}")
            continue
        if not route.primary:
            _warn(warnings, f"{WARN_ROUTE_PRIMARY_EMPTY}:{key}")
        table[key] = route
    return table


def _route_from_any(raw: Any) -> RouteConfig | None:
    from .models import route_from_mapping

    if not isinstance(raw, Mapping):
        return None
    return route_from_mapping(raw)


def _warn(warnings: list[str] | None, code: str) -> None:
    if warnings is not None and code not in warnings:
        warnings.append(code)


def select_route(
    *,
    profile_id: str | None,
    table: Mapping[str, RouteConfig],
    disabled_profiles: Iterable[str] = (),
) -> RouteConfig | None:
    """精确 profile → 兜底路由 → ``None``（不接管）。

    精确路由存在但被禁用（``enable=false`` 或 ``primary`` 为空）时**不**回落到
    兜底路由，避免掩盖用户的显式配置（规划文档 §7.2-3）。
    """
    if not table:
        return None
    disabled = set(disabled_profiles or ())
    if profile_id and profile_id in disabled:
        return None

    if profile_id:
        exact = table.get(profile_id)
        if exact is not None:
            return exact if exact.is_usable else None

    wildcard = table.get(WILDCARD_PROFILE)
    if wildcard is not None and wildcard.is_usable:
        return wildcard
    return None


def resolve_route(
    *,
    umo: str | None,
    acm: Any,
    table: Mapping[str, RouteConfig],
    disabled_profiles: Iterable[str] = (),
) -> tuple[str | None, RouteConfig | None]:
    """一次拿到 ``(profile_id, route)``，供 hook 与包装层共用。"""
    profile_id = resolve_profile_id(umo, acm)
    return profile_id, select_route(
        profile_id=profile_id,
        table=table,
        disabled_profiles=disabled_profiles,
    )


def _dedupe(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        pid = (item or "").strip() if isinstance(item, str) else ""
        if not pid or pid in seen:
            continue
        seen.add(pid)
        result.append(pid)
    return result


def _is_marked(marks: Mapping[str, Any] | None, provider_id: str) -> bool:
    if not marks:
        return False
    value = marks.get(provider_id)
    return value is not None and value is not False


def build_candidate_chain(
    *,
    route: RouteConfig | None,
    requested_id: str = "",
    native_id: str | None = None,
    framework_fallback_ids: Sequence[str] = (),
    marks: Mapping[str, Any] | None = None,
    union_framework_fallbacks: bool = True,
    respect_manual_selection: bool = True,
    known_ids: Iterable[str] | None = None,
    warnings: list[str] | None = None,
) -> ChainPlan:
    """构建本次调用的候选链（规划文档 §4.3 ``ChainPlan``）。

    * ``source="route"``：命中路由，路由链是权威链；
    * ``source="native"``：未命中路由但有框架当前 provider，按其单点降级；
    * ``source="unmanaged"``：两者都没有，插件不接管（``candidates`` 为空）。

    全部候选都被标记时**不返回空链**：取链首候选并把 ``degraded_use`` 置位，
    由调用方告警，保证对话不会直接失败（规划文档 D4）。
    """
    base: list[str] = []
    source = "unmanaged"
    if warnings is None:
        warnings = []

    if route is not None and route.is_usable:
        base = [route.primary, *route.fallbacks]
        source = "route"
        if union_framework_fallbacks:
            base.extend(framework_fallback_ids)
    elif native_id:
        base = [native_id, *framework_fallback_ids]
        source = "native"

    if respect_manual_selection and requested_id:
        # 手动选中的模型排在链首；若它已被标记，下面的过滤会把它挪进 bypassed，
        # 从而既尊重选择又不会打到坏模型上。
        base.insert(0, requested_id)

    ordered = _dedupe(base)

    missing: list[str] = []
    if known_ids is not None:
        known = set(known_ids)
        kept: list[str] = []
        for pid in ordered:
            if pid in known:
                kept.append(pid)
            else:
                missing.append(pid)
        ordered = kept
        for pid in missing:
            _warn(warnings, f"{WARN_ROUTE_PROVIDER_MISSING}:{pid}")

    bypassed = [pid for pid in ordered if _is_marked(marks, pid)]
    candidates = [pid for pid in ordered if pid not in bypassed]

    degraded_use: str | None = None
    if not candidates and ordered:
        degraded_use = ordered[0]
        candidates = [degraded_use]
        _warn(warnings, WARN_ALL_CANDIDATES_MARKED)

    return ChainPlan(
        candidates=tuple(candidates),
        bypassed=tuple(bypassed),
        degraded_use=degraded_use,
        source=source,
        missing=tuple(missing),
        warnings=tuple(warnings or ()),
    )


def describe_route(
    route: RouteConfig,
    *,
    marks: Mapping[str, Any] | None = None,
    known_ids: Iterable[str] | None = None,
    fallback_ids: Sequence[str] = (),
    union_framework_fallbacks: bool = True,
    profile_name: str | None = None,
) -> dict[str, Any]:
    """给 Web API / 命令用的路由视图（规划文档 §14.2 ``RouteView``）。"""
    warnings: list[str] = []
    known = set(known_ids) if known_ids is not None else None
    chain = build_candidate_chain(
        route=route,
        framework_fallback_ids=fallback_ids,
        marks=marks,
        union_framework_fallbacks=union_framework_fallbacks,
        known_ids=known,
        warnings=warnings,
    )
    missing_ids = [pid for pid in chain.missing]
    marked_ids = [
        pid
        for pid in (route.primary, *route.fallbacks)
        if _is_marked(marks, pid)
    ]
    return {
        "profile_id": route.profile_id,
        "profile_name": profile_name or route.profile_id,
        "primary": route.primary,
        "fallbacks": list(route.fallbacks),
        "effective_chain": list(chain.candidates),
        "source": chain.source,
        "marked": marked_ids,
        "bypassed": list(chain.bypassed),
        "degraded_use": chain.degraded_use,
        "missing": missing_ids,
        "warnings": [
            {"code": code, "message": describe_warning(code)} for code in chain.warnings
        ],
        "enable": route.enable,
        "note": route.note,
    }


_WARNING_TEXT: dict[str, str] = {
    WARN_ROUTE_DUPLICATE: "同一配置文件配置了多条路由，只有第一条生效。",
    WARN_ROUTE_PROVIDER_MISSING: "路由引用的模型 ID 在“服务提供商”里不存在，已从候选链中剔除。",
    WARN_ROUTE_PRIMARY_EMPTY: "路由的主模型为空，该路由视为未启用。",
    WARN_ALL_CANDIDATES_MARKED: "全部候选模型都被标记，已强制使用链首候选以保证对话不失败。",
    WARN_PROFILE_DISABLED: "该配置文件在“不接管选择的配置文件”列表中，插件不做选择/重试/降级。",
}


def describe_warning(code: str) -> str:
    """把告警码翻译成中文说明（保留码便于程序判断）。"""
    key = code.split(":", 1)[0]
    return _WARNING_TEXT.get(key, code)


def profile_suggestions(unknown_id: str, known_ids: Iterable[str], limit: int = 3) -> list[str]:
    """给拼错的 provider id 找最接近的三个候选（编辑距离）。"""
    candidates = sorted({pid for pid in known_ids if pid and pid != unknown_id})
    if not candidates:
        return []
    scored = sorted(
        candidates,
        key=lambda pid: (_edit_distance(unknown_id.lower(), pid.lower()), pid),
    )
    return scored[:limit]


def _edit_distance(left: str, right: str) -> int:
    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)
    previous = list(range(len(right) + 1))
    for i, left_ch in enumerate(left, start=1):
        current = [i]
        for j, right_ch in enumerate(right, start=1):
            cost = 0 if left_ch == right_ch else 1
            current.append(
                min(
                    previous[j] + 1,
                    current[j - 1] + 1,
                    previous[j - 1] + cost,
                )
            )
        previous = current
    return previous[-1]

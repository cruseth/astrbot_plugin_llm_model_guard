"""管理员命令的文本渲染与入参解析（``/llmguard ...``）。

本模块只做"把状态渲染成文本"与"校验入参"，不注册任何 handler；注册在
``main.py``，这样命令逻辑可以脱离 AstrBot 单测。
"""

from __future__ import annotations

import time
from typing import Any, Iterable

from .models import Action, MarkState
from .routing import profile_suggestions

#: 命令输出最多展示的模型条数（规划文档 §14.1）。
MANAGED_PROVIDER_LIMIT = 15

#: 状态命令展示的最近窗口（秒）。
STATUS_WINDOW_S = 300


def _yes_no(flag: bool) -> str:
    return "开启" if flag else "关闭"


def _percent(value: Any) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return "—"


def _fmt_ts(ts: float | None, *, now: float | None = None) -> str:
    if not ts:
        return "—"
    current = time.time() if now is None else now
    delta = int(current - ts)
    if delta < 0:
        delta = 0
    if delta < 60:
        return f"{delta} 秒前"
    if delta < 3600:
        return f"{delta // 60} 分钟前"
    if delta < 86400:
        return f"{delta // 3600} 小时前"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _fmt_due(ts: float | None, *, now: float | None = None) -> str:
    """把"下次测活时间"渲染成"还有多久"或"已到期"。"""
    if not ts:
        return "已到期"
    current = time.time() if now is None else now
    delta = int(ts - current)
    if delta <= 0:
        return "已到期"
    if delta < 60:
        return f"{delta} 秒后"
    if delta < 3600:
        return f"{delta // 60} 分钟后"
    if delta < 86400:
        return f"{delta // 3600} 小时后"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def format_status(guard: Any, *, now: float | None = None) -> str:
    """``/llmguard status``：单条消息、≤20 行。"""
    settings = guard.settings
    metrics = guard.metrics
    guardian = guard.guardian
    current = time.time() if now is None else now

    overview = metrics.overview(window_s=STATUS_WINDOW_S)
    totals = overview.get("totals") or {}
    # 取全部作用域的标记，profile 标记也能在状态里看到（P1-1）。
    marked = guardian.all_marks()
    pending = [
        view.provider_id for view in marked.values() if view.state is MarkState.CONFIRMING
    ]

    lines = [
        "【LLM 模型守护】状态",
        f"总开关: {_yes_no(settings.enable)}｜路由: {_yes_no(settings.enable_routing)}"
        f"｜重试: {_yes_no(settings.enable_retry)}｜降级: {_yes_no(settings.enable_fallback)}",
        f"标记: {_yes_no(settings.enable_marking)}｜测活: {_yes_no(settings.enable_probe)}"
        f"｜监控: {_yes_no(settings.enable_monitoring)}｜页面写操作: {_yes_no(settings.allow_webui_actions)}",
        f"已包装模型: {guard.wrapped_count} 个｜已标记: {len(marked)} 个"
        f"｜测活调度: {'运行中' if guardian.supervisor_running else '未运行'}",
        f"近 {STATUS_WINDOW_S // 60} 分钟: 调用 {totals.get('calls', 0)} 次，"
        f"成功 {totals.get('ok', 0)} 次，错误 {totals.get('errors', 0)} 次，"
        f"成功率 {_percent(totals.get('success_rate'))}",
        f"框架二次进入跳过: {metrics.counters.get('framework_fallback_hops', 0)}"
        f"｜被标记跳过的候选: {metrics.counters.get('marked_candidate_skipped', 0)}"
        f"｜全候选被标记: {metrics.counters.get('all_candidates_marked', 0)}"
        f"｜内部异常: {metrics.counters.get('guard_internal_errors', 0)}",
    ]
    if pending:
        lines.append("双击确认中: " + "、".join(sorted(pending)[:5]))
    if marked:
        latest = sorted(marked.values(), key=lambda view: view.marked_at or 0)[-1]
        lines.append(
            f"最近标记: {latest.provider_id}（{latest.reason or '未注明'}，"
            f"{_fmt_ts(latest.marked_at, now=current)}）"
        )
    else:
        lines.append("当前没有被标记的模型。")
    lines.append("提示: /llmguard models 查看模型 ID；/llmguard route 查看生效链。")
    return "\n".join(lines)


def format_models(guard: Any, *, limit: int = 40) -> str:
    """``/llmguard models``：列出全部 chat provider id 与模型名。"""
    provider_ids = list(guard.provider_ids())
    if not provider_ids:
        return "当前没有任何服务提供商实例；请先在 WebUI 配置“服务提供商”。"
    marked = guard.guardian.mark_index()
    lines = ["【可用模型】provider ID → 模型"]
    for provider_id in provider_ids[: max(1, limit)]:
        provider = guard.provider_for(provider_id)
        model = ""
        getter = getattr(provider, "get_model", None)
        if callable(getter):
            try:
                model = str(getter())
            except Exception:
                model = ""
        suffix = "　[已标记]" if provider_id in marked else ""
        lines.append(f"- {provider_id} → {model or '（未知）'}{suffix}")
    if len(provider_ids) > limit:
        lines.append(f"…共 {len(provider_ids)} 个，已截断；完整列表请看监控页面。")
    return "\n".join(lines)


def format_marks(guard: Any, *, now: float | None = None) -> str:
    """``/llmguard marks``：标记列表与下次测活时间（含 profile 作用域，P1-1）。"""
    current = time.time() if now is None else now
    marked = guard.guardian.all_marks()
    if not marked:
        return "当前没有被标记的模型。"
    lines = ["【已标记模型】"]
    for key, view in sorted(marked.items(), key=lambda item: item[1].marked_at or 0):
        state = "双击确认中" if view.state is MarkState.CONFIRMING else "已标记"
        ttl = "永久" if not view.expires_at else f"剩余 {max(0, int(view.expires_at - current))} 秒"
        next_probe = _fmt_due(view.next_probe_at, now=current)
        scope = "" if key == view.provider_id else f"｜作用域={key.partition('::')[0]}"
        lines.append(
            f"- {view.provider_id}｜{state}｜原因={view.reason or '未注明'}"
            f"｜TTL={ttl}｜下次测活≈{next_probe}{scope}"
        )
        if view.detail:
            lines.append(f"    最近错误: {view.detail[:120]}")
    lines.append("提示: /llmguard unmark <provider_id> 立即解除；/llmguard test <provider_id> 立刻测活。")
    return "\n".join(lines)


def format_route(
    guard: Any,
    *,
    umo: str | None = None,
    profile_id: str | None = None,
) -> str:
    """``/llmguard route``：当前会话（或指定 profile）的生效链。"""
    resolved_profile = profile_id
    if resolved_profile is None and umo:
        from .routing import resolve_profile_id

        resolved_profile = resolve_profile_id(umo, guard.acm)
    selection = guard.build_selection(umo=umo)
    plan = selection.plan
    source_text = {
        "route": "路由接管",
        "native": "沿用框架自身选择",
        "unmanaged": "未纳管（插件不介入）",
    }.get(plan.source, plan.source)

    lines = [f"【路由】配置文件: {resolved_profile or selection.profile_id or '未知'}｜来源: {source_text}"]
    if selection.route is not None:
        lines.append(
            f"主模型: {selection.route.primary or '（空）'}"
            f"｜回退链: {'、'.join(selection.route.fallbacks) or '（无）'}"
        )
    lines.append(f"本次候选链: {' → '.join(plan.candidates) or '（空，不接管）'}")
    if plan.bypassed:
        lines.append("因标记被跳过: " + "、".join(plan.bypassed))
    if plan.missing:
        suggestions = profile_suggestions(plan.missing[0], guard.provider_ids())
        hint = f"（最接近的 ID: {'、'.join(suggestions)}）" if suggestions else ""
        lines.append("配置里不存在、已剔除: " + "、".join(plan.missing) + hint)
    if plan.degraded_use:
        lines.append(f"注意: 全部候选都被标记，已强制使用 {plan.degraded_use}")
    return "\n".join(lines)


def format_probe(result: Any) -> str:
    """``/llmguard test <id>`` 的结果文本。"""
    provider_id = getattr(result, "provider_id", "?")
    ok = bool(getattr(result, "ok", False))
    latency = getattr(result, "latency_ms", 0)
    if ok:
        state = getattr(result, "state_after", None)
        if state == "confirming":
            tail = "首次测活成功，已进入双击确认，稍后会再测一次。"
        elif state == "healthy":
            tail = "两次测活都成功，标记已解除。"
        elif state is None:
            tail = "该模型当前没有被标记。"
        else:
            tail = f"当前状态: {state}"
        return f"测活成功：{provider_id}，耗时 {latency}ms（{getattr(result, 'mode', 'text_chat')}）。{tail}"
    error_class = getattr(result, "error_class", None) or "unknown"
    message = getattr(result, "error_message", "") or ""
    timed_out = "（超时）" if getattr(result, "timed_out", False) else ""
    return (
        f"测活失败：{provider_id}，耗时 {latency}ms{timed_out}，"
        f"类别={error_class}，错误={message[:160]}"
    )


def format_providers(guard: Any, *, limit: int = MANAGED_PROVIDER_LIMIT) -> str:
    """``/llmguard providers``：每个模型的调用与错误摘要。"""
    per_provider = guard.metrics.per_provider()
    if not per_provider:
        return "还没有调用统计。"
    marked = guard.guardian.mark_index()
    ordered = sorted(
        per_provider.items(),
        key=lambda item: (-(item[1].get("errors") or 0), item[0]),
    )
    lines = ["【模型统计】调用/成功/错误｜最近错误分类｜平均延迟"]
    for provider_id, stat in ordered[:limit]:
        last_error = stat.get("last_error") or {}
        latency = stat.get("latency_ms") or {}
        flags = "　[已标记]" if provider_id in marked else ""
        lines.append(
            f"- {provider_id}{flags}：{stat.get('calls', 0)}/{stat.get('ok', 0)}/"
            f"{stat.get('errors', 0)}｜{last_error.get('class') or '—'}"
            f"｜均 {latency.get('avg', 0)}ms"
        )
    if len(ordered) > limit:
        lines.append(f"…共 {len(ordered)} 个模型，已截断；完整数据请看监控页面。")
    return "\n".join(lines)


def validate_provider_id(guard: Any, provider_id: str) -> tuple[bool, str]:
    """provider id 存在性校验；不存在时给出最接近的候选。"""
    candidate = (provider_id or "").strip()
    if not candidate:
        return False, "请提供 provider ID，例如：/llmguard mark <provider_id>"
    known = guard.provider_ids()
    if not known:
        return False, "当前没有任何服务提供商实例，请先在 WebUI 配置“服务提供商”。"
    if candidate in known:
        return True, ""
    suggestions = profile_suggestions(candidate, known)
    hint = f"；最接近的是：{'、'.join(suggestions)}" if suggestions else ""
    return False, f"未找到 provider ID {candidate}{hint}。可用 /llmguard models 查看全部 ID。"


def parse_mark_args(args: str) -> tuple[str, int, str]:
    """解析 ``mark`` 的附加参数：``<provider_id> [ttl] [原因]``。

    返回 ``(provider_id, ttl_seconds, reason)``；``ttl`` 缺省为 0（永久，直到测活通过）。
    """
    parts = (args or "").split()
    if not parts:
        return "", 0, ""
    provider_id = parts[0]
    ttl = 0
    rest: list[str] = []
    for item in parts[1:]:
        if not rest and item.isdigit():
            ttl = int(item)
            continue
        rest.append(item)
    return provider_id, max(0, ttl), " ".join(rest).strip()


def summarize_actions(actions: Iterable[Action]) -> str:
    """把动作枚举拼成可读文本（调试用）。"""
    return "、".join(str(item) for item in actions) or "无"

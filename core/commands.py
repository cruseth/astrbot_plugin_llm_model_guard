"""管理员命令的文本渲染与入参解析（``/llmguard ...``）。

v0.2.0 的命令清单（任务包 12 §4.4）：

* ``/llmguard status`` —— 总览
* ``/llmguard chains`` —— 每个配置文件的模型顺序与被标记项
* ``/llmguard marks`` —— 当前被标记的模型
* ``/llmguard test <provider_id>`` —— 立刻验证一次
* ``/llmguard unmark <provider_id>`` —— 手动解除标记
* ``/llmguard reload`` —— 重载配置

本模块只做"把状态渲染成文本"与"校验入参"，不注册任何 handler；
注册在 ``main.py``，这样命令逻辑可以脱离 AstrBot 单测。
"""

from __future__ import annotations

import time
from typing import Any

from .chain import profile_suggestions
from .models import MarkState

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
    """把"下次验证时间"渲染成"还有多久"或"已到期"。"""
    if not ts:
        return "待安排"
    current = time.time() if now is None else now
    delta = int(ts - current)
    if delta <= 0:
        return "已到期"
    if delta < 60:
        return f"{delta} 秒后"
    if delta < 3600:
        return f"{delta // 60} 分钟后"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def format_status(guard: Any, *, now: float | None = None) -> str:
    """``/llmguard status``：单条消息、≤20 行。"""
    settings = guard.settings
    metrics = guard.metrics
    guardian = guard.guardian
    current = time.time() if now is None else now

    overview = metrics.overview(window_s=STATUS_WINDOW_S)
    totals = overview.get("totals") or {}
    marked = guardian.all_marks()

    lines = [
        "【LLM 模型守护】状态",
        f"总开关: {_yes_no(settings.enable)}",
        f"调用超时: {settings.request_timeout_seconds or '不限制'} 秒"
        f"｜重试: {settings.max_retries} 次"
        f"｜退避基数: {settings.retry_backoff_seconds} 秒",
        "可重试类别: " + ("、".join(settings.retry_categories) or "（无，全部直接回退）"),
        f"验证间隔: {settings.probe_interval_seconds} 秒"
        f"｜验证超时: {settings.probe_timeout_seconds} 秒"
        f"｜连续成功 {settings.probe_success_threshold} 次解除标记",
        f"已包装模型: {guard.wrapped_count} 个｜已标记: {len(marked)} 个"
        f"｜验证调度: {'运行中' if guardian.supervisor_running else '未运行'}",
        f"近 {STATUS_WINDOW_S // 60} 分钟: 调用 {totals.get('calls', 0)} 次，"
        f"成功 {totals.get('ok', 0)} 次，错误 {totals.get('errors', 0)} 次，"
        f"成功率 {_percent(totals.get('success_rate'))}",
    ]
    if marked:
        latest = sorted(marked.values(), key=lambda view: view.marked_at or 0)[-1]
        lines.append(
            f"最近标记: {latest.provider_id}（{latest.reason or '未注明'}，"
            f"{_fmt_ts(latest.marked_at, now=current)}）"
        )
    else:
        lines.append("当前没有被标记的模型。")
    lines.append("提示: /llmguard chains 查看各配置文件的模型顺序。")
    return "\n".join(lines)


def format_chains(guard: Any) -> str:
    """``/llmguard chains``：每个配置文件的模型顺序与被标记项。"""
    from .web_api import collect_chains

    try:
        chains = collect_chains(guard)
    except Exception as exc:  # pragma: no cover - 防御性
        return f"读取配置文件列表失败：{exc}"
    if not chains:
        return "当前读不到任何配置文件信息。"

    lines = ["【模型链路】按框架各自的配置顺序（插件不改写这些顺序）"]
    for item in chains:
        default_provider = item.get("default_provider") or "（未设置）"
        fallbacks = item.get("fallbacks") or []
        lines.append(f"- {item.get('profile_display') or item.get('profile_id')}")
        lines.append(f"    默认模型: {default_provider}")
        lines.append(
            "    回退模型: " + ("、".join(fallbacks) if fallbacks else "（未设置）")
        )
        marked = item.get("marked") or []
        if marked:
            lines.append("    被标记已跳过: " + "、".join(marked))
    return "\n".join(lines)


def format_marks(guard: Any, *, now: float | None = None) -> str:
    """``/llmguard marks``：标记列表与连续成功进度。"""
    current = time.time() if now is None else now
    marked = guard.guardian.all_marks()
    if not marked:
        return "当前没有被标记的模型。"
    threshold = max(1, guard.settings.probe_success_threshold)
    lines = ["【已标记模型】"]
    for _key, view in sorted(marked.items(), key=lambda item: item[1].marked_at or 0):
        state = "已标记" if view.state is MarkState.MARKED else str(view.state)
        progress = f"{view.consecutive_success}/{threshold}"
        next_probe = _fmt_due(view.next_probe_at, now=current)
        lines.append(
            f"- {view.provider_id}｜{state}｜原因={view.reason or '未注明'}"
            f"｜连续成功={progress}｜下次验证≈{next_probe}"
        )
        if view.detail:
            lines.append(f"    最近错误: {view.detail[:120]}")
    lines.append(
        "提示: /llmguard unmark <provider_id> 立即解除；"
        "/llmguard test <provider_id> 立刻验证。"
    )
    return "\n".join(lines)


def format_probe(result: Any) -> str:
    """``/llmguard test <id>`` 的结果文本。"""
    provider_id = getattr(result, "provider_id", "?")
    ok = bool(getattr(result, "ok", False))
    latency = getattr(result, "latency_ms", 0)
    if ok:
        state = getattr(result, "state_after", None)
        if state == "healthy":
            tail = "连续成功已达阈值，标记已解除，模型回到路由顺序。"
        elif state and str(state).startswith("confirming"):
            progress = str(state).split(":", 1)[-1]
            tail = f"验证成功（连续成功 {progress}），还没达到解除标记的次数。"
        else:
            tail = "该模型当前没有被标记。"
        return (
            f"验证成功：{provider_id}，耗时 {latency}ms"
            f"（{getattr(result, 'mode', 'text_chat')}）。{tail}"
        )
    error_class = getattr(result, "error_class", None) or "unknown"
    message = getattr(result, "error_message", "") or ""
    timed_out = "（超时）" if getattr(result, "timed_out", False) else ""
    return (
        f"验证失败：{provider_id}，耗时 {latency}ms{timed_out}，"
        f"类别={error_class}，错误={message[:160]}"
    )


def validate_provider_id(guard: Any, provider_id: str) -> tuple[bool, str]:
    """provider id 存在性校验；不存在时给出最接近的候选。"""
    candidate = (provider_id or "").strip()
    if not candidate:
        return False, "请提供 provider ID，例如：/llmguard test <provider_id>"
    known = guard.provider_ids()
    if not known:
        return False, "当前没有任何服务提供商实例，请先在 WebUI 配置“服务提供商”。"
    if candidate in known:
        return True, ""
    suggestions = profile_suggestions(candidate, known)
    hint = f"；最接近的是：{'、'.join(suggestions)}" if suggestions else ""
    return (
        False,
        f"未找到 provider ID {candidate}{hint}。"
        "可用 /llmguard chains 查看各配置文件的模型顺序。",
    )


def parse_mark_args(args: str) -> tuple[str, int, str]:
    """解析 ``mark`` 的附加参数：``<provider_id> [ttl] [原因]``。

    返回 ``(provider_id, ttl_seconds, reason)``；``ttl`` 缺省为 0。
    v0.2.0 已移除 TTL 语义，这里保留解析形状以兼容旧调用点。
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

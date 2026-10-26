"""链路构建：直接读框架的模型顺序，不再要求用户配置。

v0.2.0 的核心思想（任务包 12 §1）
--------------------------------

**插件不要求用户描述模型顺序，而是直接读框架已有的顺序**：

1. ``ProviderManager.get_using_provider(CHAT_COMPLETION, umo)`` 已经内置完整优先级：
   * **会话级隔离**（``sp.get("provider_perf_chat_completion", scope="umo")``）→
   * 该 umo 所属 abconf 的 ``provider_settings.default_provider_id`` →
   * 兜底 ``provider_insts[0]``；
   → 所以「会话级模型选择」天然被涵盖，插件不需要自己解析 SharedPreferences。
2. ``provider_settings.fallback_chat_models`` 就是**该 abconf 的有序回退链**
   （框架 ``astr_main_agent.py:1176-1208`` 按顺序取、去重、排除主 provider）。
3. ``astrbot_config_mgr.get_conf(umo)`` 返回该 umo 所属 abconf。

因此链路为::

    chain = dedupe([
        manual_selected,                # event.get_extra("selected_provider")
        framework_current,              # get_using_provider(CHAT_COMPLETION, umo)
        *abconf.fallback_chat_models,   # 该 abconf 的有序回退列表
    ])

这直接满足「兼容框架的每个配置文件（模型顺序），分别回退及验证」：
**每个配置文件用自己的模型顺序**，插件每次请求实时重建链路。

本模块只做纯计算与容错，不访问 ``context``（context 由调用方注入），
也不导入 AstrBot。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from .models import ChainPlan

#: 链路来源：会话级手动选择。
SOURCE_MANUAL = "manual"
#: 链路来源：框架当前选择（会话级隔离 / abconf 默认 / 首个 provider）。
SOURCE_FRAMEWORK = "framework"
#: 链路来源：未纳管（框架没给出任何模型）。
SOURCE_UNMANAGED = "unmanaged"

#: 告警码。
WARN_CHAIN_EMPTY = "chain_empty"
WARN_CHAIN_ALL_MARKED = "chain_all_marked"
WARN_PROVIDER_MISSING = "chain_provider_missing"


def _provider_id_of(provider: Any) -> str:
    """从 provider 实例取 id（兼容 ``provider_config`` 与 ``meta``）。"""
    if provider is None:
        return ""
    config = getattr(provider, "provider_config", None)
    if isinstance(config, Mapping):
        pid = config.get("id")
        if isinstance(pid, str) and pid:
            return pid
    meta = getattr(provider, "meta", None)
    if callable(meta):
        try:
            return str(getattr(meta(), "id", "") or "")
        except Exception:
            return ""
    return ""


def _chat_completion_type() -> Any:
    """取框架的 ``ProviderType.CHAT_COMPLETION`` 枚举。

    框架的 ``get_using_provider`` 形参是 ``ProviderType`` 枚举，直接传字符串
    会走不进 ``CHAT_COMPLETION`` 分支。这里做**延迟导入**：拿不到就退回
    字符串（框架某些版本容忍字符串比较），仍拿不到就返回 ``None``，
    由调用方走「框架没给出模型」的降级路径。
    """
    try:
        from astrbot.core.provider.entities import ProviderType  # type: ignore

        return ProviderType.CHAT_COMPLETION
    except Exception:
        return None


def _call_get_using_provider(provider_manager: Any, umo: str | None) -> Any:
    """调用框架的 ``get_using_provider``，把各种签名差异都吃掉。

    依次尝试：``(ProviderType.CHAT_COMPLETION, umo)`` → ``("chat_completion", umo)``
    → ``(umo)``。全部失败返回 ``None``。
    """
    getter = getattr(provider_manager, "get_using_provider", None)
    if not callable(getter):
        return None

    provider_type = _chat_completion_type()
    attempts: list[tuple[Any, ...]] = []
    if provider_type is not None:
        attempts.append((provider_type, umo))
    attempts.append(("chat_completion", umo))
    attempts.append((umo,))

    for args in attempts:
        try:
            return getter(*args)
        except TypeError:
            # 签名不匹配：试下一种。
            continue
        except Exception:
            # 框架内部异常：视为「拿不到当前模型」，不外抛。
            return None
    return None


def resolve_framework_current(
    *,
    provider_manager: Any,
    umo: str | None,
) -> tuple[str, str]:
    """取框架当前使用的 chat provider id。

    返回 ``(provider_id, source)``；``source`` 说明它来自哪一级优先级：

    * ``"session"``：会话级隔离命中；
    * ``"default"``：该 abconf 的 ``default_provider_id``；
    * ``"first"``：兜底 ``provider_insts[0]``；
    * ``"none"``：框架没给出任何模型。

    全程容错：``provider_manager`` 缺失、``get_using_provider`` 抛异常、返回
    ``None`` 都退化为 ``("", "none")``，绝不把异常抛给调用方。
    """
    if provider_manager is None:
        return "", "none"

    provider = _call_get_using_provider(provider_manager, umo)
    provider_id = _provider_id_of(provider)
    if not provider_id:
        return "", "none"

    # 判断它来自哪一级优先级：会话级隔离 → abconf 默认 → 首个 provider。
    if _session_provider_id(provider_manager, umo) == provider_id:
        return provider_id, "session"
    if _abconf_default_provider_id(provider_manager, umo) == provider_id:
        return provider_id, "default"
    return provider_id, "first"


def _session_provider_id(provider_manager: Any, umo: str | None) -> str:
    """会话级隔离的 provider id（读不到就返回空串）。

    只用于**标注来源**，不参与链路构建 —— 真正的会话级优先级由框架
    ``get_using_provider`` 负责。框架没有公开的「只查会话级」接口，
    因此这里按框架的键名约定去读 ``provider_manager`` 上的
    ``session_provider`` 之类的映射；读不到就返回空串（来源标注退化为
    ``default``/``first``，不影响链路本身）。
    """
    if not umo:
        return ""
    for attr in ("session_provider", "session_providers", "umo_provider"):
        mapping = getattr(provider_manager, attr, None)
        if isinstance(mapping, Mapping):
            value = mapping.get(umo)
            if isinstance(value, str) and value:
                return value
    return ""


def _abconf_default_provider_id(provider_manager: Any, umo: str | None) -> str:
    """该 umo 所属 abconf 的 ``provider_settings.default_provider_id``。"""
    acm = getattr(provider_manager, "acm", None)
    if acm is None:
        return ""
    getter = getattr(acm, "get_conf", None)
    if not callable(getter):
        return ""
    try:
        conf = getter(umo)
    except Exception:
        return ""
    if not isinstance(conf, Mapping):
        return ""
    settings = conf.get("provider_settings")
    if not isinstance(settings, Mapping):
        return ""
    value = settings.get("default_provider_id")
    return value.strip() if isinstance(value, str) else ""


def resolve_fallback_ids(
    *,
    provider_manager: Any,
    umo: str | None,
) -> list[str]:
    """取该 umo 所属 abconf 的有序回退列表（``fallback_chat_models``）。

    容错：abconf 缺失、配置块缺失、值不是 list、元素不是字符串 —— 一律按空列表处理。
    """
    acm = getattr(provider_manager, "acm", None)
    if acm is None:
        return []
    getter = getattr(acm, "get_conf", None)
    if not callable(getter):
        return []
    try:
        conf = getter(umo)
    except Exception:
        return []
    if not isinstance(conf, Mapping):
        return []
    settings = conf.get("provider_settings")
    if not isinstance(settings, Mapping):
        return []
    fallbacks = settings.get("fallback_chat_models")
    if not isinstance(fallbacks, (list, tuple)):
        return []
    result: list[str] = []
    for item in fallbacks:
        if isinstance(item, str) and item.strip():
            result.append(item.strip())
    return result


def build_chain(
    *,
    provider_manager: Any,
    umo: str | None = None,
    manual_id: str = "",
    known_ids: Iterable[str] | None = None,
    marked_ids: Iterable[str] | None = None,
) -> ChainPlan:
    """按 §1 的规则构建本次调用的候选链。

    步骤：

    1. 收集候选：``[手动选择, 框架当前, *abconf 回退列表]``，去重保序；
    2. 剔除**当前不存在**的 provider id（记 ``missing``）；
    3. 剔除**被标记**的 provider id（记 ``bypassed``）；
    4. 全部被标记时用链首兜底并记告警，保证对话不直接失败。

    任何一步的输入缺失都只会让链路变短，**不会抛异常**。
    """
    warnings: list[str] = []
    known = set(known_ids) if known_ids is not None else None
    marked = set(marked_ids or ())

    current_id, current_source = resolve_framework_current(
        provider_manager=provider_manager,
        umo=umo,
    )
    fallbacks = resolve_fallback_ids(provider_manager=provider_manager, umo=umo)

    # 1. 收集候选（去重保序）。
    ordered: list[str] = []
    source = SOURCE_UNMANAGED
    if manual_id:
        ordered.append(manual_id)
        source = SOURCE_MANUAL
    if current_id:
        ordered.append(current_id)
        if source == SOURCE_UNMANAGED:
            source = SOURCE_FRAMEWORK
    for fallback_id in fallbacks:
        ordered.append(fallback_id)
    if source == SOURCE_UNMANAGED and ordered:
        source = SOURCE_FRAMEWORK

    # 去重保序。
    deduped: list[str] = []
    seen: set[str] = set()
    for provider_id in ordered:
        if not provider_id or provider_id in seen:
            continue
        seen.add(provider_id)
        deduped.append(provider_id)

    # 2. 剔除不存在的 provider。
    missing: list[str] = []
    if known is not None:
        kept: list[str] = []
        for provider_id in deduped:
            if provider_id in known:
                kept.append(provider_id)
            else:
                missing.append(provider_id)
                warnings.append(f"{WARN_PROVIDER_MISSING}:{provider_id}")
        deduped = kept

    # 3. 剔除被标记的。
    bypassed = [pid for pid in deduped if pid in marked]
    candidates = [pid for pid in deduped if pid not in marked]

    # 4. 全被标记 → 用链首兜底。
    degraded_use: str | None = None
    if not candidates and deduped:
        degraded_use = deduped[0]
        candidates = [degraded_use]
        warnings.append(WARN_CHAIN_ALL_MARKED)

    if not candidates:
        warnings.append(WARN_CHAIN_EMPTY)

    return ChainPlan(
        candidates=tuple(candidates),
        bypassed=tuple(bypassed),
        degraded_use=degraded_use,
        source=source,
        missing=tuple(missing),
        warnings=tuple(warnings),
    )


def describe_chain(
    chain: ChainPlan,
    *,
    profile_id: str = "",
    profile_name: str = "",
    default_provider: str = "",
    fallbacks: Sequence[str] = (),
) -> dict[str, Any]:
    """给 Web API / 命令用的链路视图（只读）。"""
    name = profile_name or profile_id
    display = f"{name} ({profile_id})" if name and name != profile_id else profile_id
    return {
        "profile_id": profile_id,
        "profile_name": name,
        "profile_display": display,
        "default_provider": default_provider,
        "fallbacks": list(fallbacks),
        "chain": list(chain.candidates),
        "bypassed": list(chain.bypassed),
        "missing": list(chain.missing),
        "degraded_use": chain.degraded_use,
        "source": chain.source,
        "warnings": list(chain.warnings),
    }


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

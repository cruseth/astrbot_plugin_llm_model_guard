"""动态注入框架配置文件到插件 schema 的 ``options`` / ``labels``。

背景（任务包 09）
----------------

框架**没有** abconf（配置文件）选择器，但插件配置页的 schema 来自
``GET /api/config/get?plugin_name=...``，后端 ``dashboard/routes/config.py:1607-1613``
直接引用插件实例持有的 ``plugin_md.config.schema`` **对象**::

    ret["metadata"] = {
        plugin_name: {
            "type": "object",
            "items": plugin_md.config.schema,   # ← 引用，不是拷贝
        },
    }

而 ``AstrBotConfig.schema`` 是普通可变 dict（``astrbot/core/config/astrbot_config.py:36``），
插件实例拿到的 ``config`` 就是同一个对象。因此**插件在运行时原地改写
``config.schema``，配置页就会渲染出真正动态的下拉**。

设计要点
--------

* **原地修改**：只改嵌套 dict（``node["options"] = ...``），**绝不替换 schema 对象本身**，
  也不给 ``config.schema`` 赋值 —— ``AstrBotConfig.__setattr__`` 会把赋值写进配置
  字典本身（``self[key] = value``），那样会污染用户配置。
* **失败即降级**：拿不到配置文件列表、schema 不是 dict、字段不存在时，**保持原 schema
  不变**并返回失败原因，绝不影响插件运行。
* **两阶段提交（原子性）**：阶段一只读预检（定位全部目标、构造 options/labels、
  校验长度相等），全部通过才进入阶段二赋值。**任一环节失败，一个字段都不写**，
  避免出现「一部分是下拉、一部分是文本框」的不一致状态。
* **并发**：整个「预检 + 提交」过程由模块级 :data:`_INJECT_LOCK` 保护，
  防止两个线程交叉写入导致 options 与 labels 配对错位。
* **历史值并入**：用户之前按**名称**填过的值（任务包 07 支持名称解析）不在框架当前
  列表里。若不下拉显示，``v-select`` 会渲染成空白，用户会以为配置丢了。因此把
  「已存配置里出现过的所有引用」与「框架当前列表」取并集，历史值以
  ``名称（历史配置值，框架中未找到）`` 追加。
* **兜底语义**：``routes[].profile_id`` 的 ``*``（兜底路由）必须留在 ``options`` 里。
* **静态 schema 不写 options**：``_conf_schema.json`` 里**不**预置 ``options``。
  因为 JS 里空数组 ``[]`` 是 truthy，一旦静态写死 ``options: []``，即使动态注入
  失败，控件也会渲染成**没有任何选项的 v-select**，用户将无法手填。保持字段没有
  ``options`` 时，注入失败会退化成普通文本框 —— 仍然可用。

本模块只依赖标准库，不导入 AstrBot，也不做任何 I/O。
"""

from __future__ import annotations

import threading
from typing import Any, Iterable, Mapping, Sequence

#: 保护「预检 + 提交」全过程的模块级锁。
#:
#: 注入要写两个必须成对出现的字段（``options`` 与 ``labels``）。若两个线程
#: 交叉执行「写 options → 写 labels」，中间态可能出现 labels 与 options
#: 不配对，前端就会显示错位。用一把可重入锁把整个过程串行化。
_INJECT_LOCK = threading.RLock()

#: 兜底路由的取值（与 ``core.models.WILDCARD_PROFILE`` 一致）。
WILDCARD = "*"

#: 注入目标：(顶层键, template 键, 字段名, 是否包含兜底项)。
#: ``template`` 为 ``None`` 表示该字段在顶层，不在 template_list 里。
#:
#: ``*`` 只加进 ``routes[].profile_id``（任务包 09 §3.1 明确要求）；
#: ``profiles[].profile`` 与 ``disabled_profiles`` 只给配置文件列表。
INJECT_TARGETS: tuple[tuple[str, str | None, str, bool], ...] = (
    # 旧字段：填的是 abconf id，保留 * 的兜底语义。
    ("routes", "route", "profile_id", True),
    # 新字段：可以填名称，也可以填 id。
    ("profiles", "profile_route", "profile", False),
    # 不接管列表：只填具体配置文件。
    ("disabled_profiles", None, "", False),
)

#: 历史配置值的标签后缀。
HISTORICAL_LABEL_SUFFIX = "（历史配置值，框架中未找到）"

#: 兜底项的标签。
WILDCARD_LABEL = "*（兜底：适用于所有未单独配置的配置文件）"


def collect_historical_refs(config: Any) -> list[str]:
    """收集「已存配置里出现过的所有配置文件引用」。

    覆盖三处来源：``routes[].profile_id``、``profiles[].profile``、
    ``disabled_profiles``。空字符串被忽略（空 = 兜底语义，由 ``*`` 覆盖）。

    用鸭子类型读值：``AstrBotConfig`` 是 dict 子类，普通 dict 与测试替身
    也支持 ``.get()``，因此不强制要求 ``isinstance(config, Mapping)``。
    """
    refs: list[str] = []
    getter = getattr(config, "get", None)
    if not callable(getter):
        return refs

    def _value(key: str) -> Any:
        try:
            return getter(key, None)
        except TypeError:  # pragma: no cover - 非标准 get 签名
            try:
                return getter(key)
            except Exception:
                return None
        except Exception:  # pragma: no cover - 配置对象异常
            return None

    def _push(value: Any) -> None:
        if isinstance(value, str):
            text = value.strip()
            if text:
                refs.append(text)

    for item in _iter_items(_value("routes")):
        if isinstance(item, Mapping):
            _push(item.get("profile_id"))
    for item in _iter_items(_value("profiles")):
        if isinstance(item, Mapping):
            _push(item.get("profile"))
            # 兼容可能出现的别名键。
            _push(item.get("profile_ref"))
    disabled = _value("disabled_profiles")
    if isinstance(disabled, str):
        _push(disabled)
    elif isinstance(disabled, Iterable) and not isinstance(disabled, (bytes, Mapping)):
        for item in disabled:
            _push(item)
    # 去重但保持出现顺序。
    seen: set[str] = set()
    unique: list[str] = []
    for ref in refs:
        if ref in seen:
            continue
        seen.add(ref)
        unique.append(ref)
    return unique


def _iter_items(value: Any) -> list[Any]:
    if isinstance(value, Mapping):
        return list(value.values())
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def build_profile_options(
    entries: Sequence[Mapping[str, str]],
    historical_refs: Sequence[str] = (),
    *,
    include_wildcard: bool = False,
) -> tuple[list[str], list[str]]:
    """构造 ``(options, labels)``。

    * ``options``：存储用的值。框架配置文件用 **abconf id**（``default`` 或 uuid），
      保证存储稳定、解析无歧义；历史值沿用用户原来填的字符串。
    * ``labels``：与 ``options`` **一一对应**的显示名，格式 ``名称（id）``；
      同名时靠 id 区分；历史值标注 ``（历史配置值，框架中未找到）``。

    返回的 ``labels`` 长度恒等于 ``options`` 长度。
    """
    options: list[str] = []
    labels: list[str] = []

    def _add(value: str, label: str) -> None:
        if not value or value in options:
            return
        options.append(value)
        labels.append(label)

    # 1. 框架当前的配置文件列表。
    for entry in entries or ():
        if not isinstance(entry, Mapping):
            continue
        profile_id = str(entry.get("id") or "").strip()
        if not profile_id:
            continue
        name = str(entry.get("name") or "").strip()
        label = f"{name}（{profile_id}）" if name and name != profile_id else profile_id
        _add(profile_id, label)

    # 2. 兜底项（routes[].profile_id / profiles[].profile 的 "*" 语义）。
    if include_wildcard:
        _add(WILDCARD, WILDCARD_LABEL)

    # 3. 历史值并入：不在框架列表里的旧值也要能显示出来，否则下拉会显示空白。
    for ref in historical_refs or ():
        text = str(ref or "").strip()
        if not text or text in options:
            continue
        _add(text, f"{text}{HISTORICAL_LABEL_SUFFIX}")

    return options, labels


def _find_target_node(
    schema: Mapping[str, Any],
    top_key: str,
    template_key: str | None,
    field_name: str,
) -> Mapping[str, Any] | None:
    """在 schema 里定位要注入的字段节点（``template_list`` 的子项或顶层字段）。"""
    node = schema.get(top_key)
    if not isinstance(node, Mapping):
        return None

    if node.get("type") != "template_list":
        # 顶层字段（``disabled_profiles``）。
        return node

    templates = node.get("templates")
    if not isinstance(templates, Mapping):
        return None

    candidates: list[Mapping[str, Any]] = []
    if template_key and isinstance(templates.get(template_key), Mapping):
        candidates.append(templates[template_key])
    else:
        candidates.extend(
            item for item in templates.values() if isinstance(item, Mapping)
        )

    for template in candidates:
        items = template.get("items")
        if not isinstance(items, Mapping):
            continue
        target = items.get(field_name)
        if isinstance(target, Mapping):
            return target
    return None


def _target_path(top_key: str, template_key: str | None, field_name: str) -> str:
    """注入目标的字段路径（用于报告与日志）。"""
    if template_key:
        return f"{top_key}.{template_key}.{field_name}"
    return top_key


def _plan_injection(
    schema: Mapping[str, Any],
    entries: Sequence[Mapping[str, str]],
    historical_refs: Sequence[str],
) -> tuple[list[tuple[str, Mapping[str, Any], list[str], list[str]]], str]:
    """阶段一（只读）：定位全部目标并构造各自的 options/labels。

    返回 ``(计划, 失败原因)``；``失败原因`` 非空表示预检未通过，
    此时**不应写入任何字段**。

    计划里每一项是 ``(字段路径, 目标节点, options, labels)``。
    """
    plan: list[tuple[str, Mapping[str, Any], list[str], list[str]]] = []
    for top_key, template_key, field_name, include_wildcard in INJECT_TARGETS:
        node = _find_target_node(schema, top_key, template_key, field_name)
        if node is None:
            return [], f"missing_target:{_target_path(top_key, template_key, field_name)}"
        options, labels = build_profile_options(
            entries,
            historical_refs,
            include_wildcard=include_wildcard,
        )
        if len(options) != len(labels):
            return [], f"length_mismatch:{_target_path(top_key, template_key, field_name)}"
        plan.append((_target_path(top_key, template_key, field_name), node, options, labels))
    if not plan:
        return [], "no_targets"
    return plan, ""


def inject_profile_options(
    config: Any,
    entries: Sequence[Mapping[str, str]] = (),
    *,
    historical_refs: Sequence[str] | None = None,
) -> dict[str, Any]:
    """把框架配置文件列表注入 ``config.schema`` 的 ``options`` / ``labels``。

    **原地修改** schema 里对应字段的 dict（``node["options"] = ...``），
    不替换 schema 对象、不给 ``config.schema`` 赋值。

    **两阶段提交**（原子性）：

    1. **阶段一（只读预检）**：定位全部注入目标、构造各自的 options/labels，
       校验长度相等。任一目标缺失或长度不匹配 → **一个字段都不写**，
       返回 ``ok=False`` 与明确 ``reason``；
    2. **阶段二（提交）**：仅当阶段一全部通过才逐项赋值。

    因此失败时 schema 与调用前**完全相等**，不会出现「一部分下拉、一部分文本框」
    的不一致状态（任务包 11 §2）。

    返回报告::

        {"ok": bool, "reason": str, "injected": [字段路径...],
         "options_count": int, "historical_added": [值...]}

    失败时 ``injected`` 恒为空列表。
    """
    report: dict[str, Any] = {
        "ok": False,
        "reason": "",
        "injected": [],
        "options_count": 0,
        "historical_added": [],
    }

    # 退化情况：配置是普通 dict（单测），没有 .schema 属性 —— 跳过而不是报错。
    schema = getattr(config, "schema", None)
    if not isinstance(schema, Mapping):
        report["reason"] = "schema_unavailable"
        return report

    if not entries:
        report["reason"] = "no_profile_entries"
        return report

    if historical_refs is None:
        historical_refs = collect_historical_refs(config)

    framework_ids = {
        str(entry.get("id") or "").strip()
        for entry in entries
        if isinstance(entry, Mapping)
    }

    # 整个「预检 + 提交」串行化：防止并发调用交叉写入导致 options/labels 错位。
    with _INJECT_LOCK:
        # ---- 阶段一：只读预检 ----
        plan, failure = _plan_injection(schema, entries, historical_refs)
        if failure:
            report["reason"] = failure
            return report

        # ---- 阶段二：提交 ----
        for path, node, options, labels in plan:
            # 原地改写：只动这个字段节点的 dict，不替换 schema 对象。
            node["options"] = options
            node["labels"] = labels
            report["injected"].append(path)
            report["options_count"] += len(options)
            report["historical_added"].extend(
                value for value in options if value not in framework_ids
            )

        report["ok"] = True
        return report

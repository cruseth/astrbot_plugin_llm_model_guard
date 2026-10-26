"""动态注入 schema 选项的单测（任务包 09）。

覆盖：

1. ``options`` / ``labels`` 正确且一一对应；
2. 历史值（不在框架列表里）被并入；
3. ``*`` 出现在 ``routes[].profile_id`` 的 options 中；
4. 注入失败时 schema 不被破坏；
5. 幂等：连续注入两次结果一致；
6. 退化情况：config 是普通 dict（无 ``.schema``）时跳过而不报错。
"""

from __future__ import annotations

import copy
import importlib
import json
from pathlib import Path
from typing import Any

import pytest

from astrbot_plugin_llm_model_guard.core import schema_sync

from .support import FakeAcm, FakeProvider, build_runtime

#: 框架里的配置文件。
ENTRIES = [
    {"id": "default", "name": "default", "path": "cmd_config.json"},
    {
        "id": "11111111-2222-3333-4444-555555555555",
        "name": "生产环境",
        "path": "abconf_1.json",
    },
    {
        "id": "99999999-8888-7777-6666-555555555555",
        "name": "测试环境",
        "path": "abconf_2.json",
    },
]


class FakeConfig:
    """最小 ``AstrBotConfig`` 替身：持有 schema 引用 + 配置值。

    刻意模仿真实框架：``schema`` 是一个**独立属性**，但配置值放在 dict 里。
    """

    def __init__(self, schema: dict[str, Any], values: dict[str, Any] | None = None) -> None:
        self.schema = schema
        self._values = dict(values or {})

    def get(self, key: str, default: Any = None) -> Any:
        return self._values.get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self._values[key]

    def __contains__(self, key: str) -> bool:
        return key in self._values

    def keys(self):
        return self._values.keys()

    def items(self):
        return self._values.items()


def load_schema() -> dict[str, Any]:
    """加载插件真实 schema（验证注入针对的就是实际下发的结构）。"""
    path = Path(__file__).resolve().parent.parent / "_conf_schema.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _node(schema: dict[str, Any], top: str, template: str | None, field: str) -> dict[str, Any]:
    node = schema[top]
    if template is None:
        return node
    return node["templates"][template]["items"][field]


# ---------------------------------------------------------------------------
# 1. 基本注入
# ---------------------------------------------------------------------------


def test_inject_options_and_labels_align() -> None:
    schema = load_schema()
    report = schema_sync.inject_profile_options(FakeConfig(schema), ENTRIES)
    assert report["ok"] is True

    for top, template, field in (
        ("routes", "route", "profile_id"),
        ("profiles", "profile_route", "profile"),
        ("disabled_profiles", None, ""),
    ):
        node = _node(schema, top, template, field)
        assert "options" in node, (top, field)
        assert "labels" in node, (top, field)
        assert len(node["options"]) == len(node["labels"]), (top, field)
        assert node["options"], (top, field)


def test_inject_uses_abconf_id_as_value() -> None:
    schema = load_schema()
    schema_sync.inject_profile_options(FakeConfig(schema), ENTRIES)
    node = _node(schema, "profiles", "profile_route", "profile")
    assert "default" in node["options"]
    assert "11111111-2222-3333-4444-555555555555" in node["options"]
    # 显示名带名称与 id。
    labels = dict(zip(node["options"], node["labels"]))
    assert "生产环境" in labels["11111111-2222-3333-4444-555555555555"]
    assert "11111111-2222-3333-4444-555555555555" in labels[
        "11111111-2222-3333-4444-555555555555"
    ]


def test_wildcard_only_in_routes_profile_id() -> None:
    schema = load_schema()
    schema_sync.inject_profile_options(FakeConfig(schema), ENTRIES)

    routes_node = _node(schema, "routes", "route", "profile_id")
    assert "*" in routes_node["options"]

    profiles_node = _node(schema, "profiles", "profile_route", "profile")
    assert "*" not in profiles_node["options"]

    disabled_node = _node(schema, "disabled_profiles", None, "")
    assert "*" not in disabled_node["options"]


# ---------------------------------------------------------------------------
# 2. 历史值并入
# ---------------------------------------------------------------------------


def test_historical_values_merged_into_options() -> None:
    schema = load_schema()
    config = FakeConfig(
        schema,
        {
            "routes": [{"profile_id": "11111111-2222-3333-4444-555555555555"}],
            "profiles": [{"profile": "生产环境"}],
            "disabled_profiles": ["历史配置名"],
        },
    )
    report = schema_sync.inject_profile_options(config, ENTRIES)
    assert report["ok"] is True

    node = _node(schema, "profiles", "profile_route", "profile")
    # 历史值「生产环境」不在框架 id 列表里，但必须出现在 options 里。
    assert "生产环境" in node["options"]
    labels = dict(zip(node["options"], node["labels"]))
    assert "历史" in labels["生产环境"]
    assert len(node["options"]) == len(node["labels"])


def test_historical_value_collected_from_all_sources() -> None:
    refs = schema_sync.collect_historical_refs(
        {
            "routes": [{"profile_id": "r1"}, {"profile_id": ""}],
            "profiles": [{"profile": "p1"}, {"profile_ref": "p2"}],
            "disabled_profiles": ["d1"],
        }
    )
    assert refs == ["r1", "p1", "p2", "d1"]


def test_duplicate_historical_values_deduped() -> None:
    refs = schema_sync.collect_historical_refs(
        {
            "routes": [{"profile_id": "same"}],
            "profiles": [{"profile": "same"}],
        }
    )
    assert refs == ["same"]


# ---------------------------------------------------------------------------
# 3. 幂等
# ---------------------------------------------------------------------------


def test_injection_is_idempotent() -> None:
    schema = load_schema()
    config = FakeConfig(schema, {"profiles": [{"profile": "历史名"}]})
    first = schema_sync.inject_profile_options(config, ENTRIES)
    snapshot = copy.deepcopy(schema)
    second = schema_sync.inject_profile_options(config, ENTRIES)
    assert first["ok"] is True and second["ok"] is True
    assert schema == snapshot, "重复注入不应改变结果"


# ---------------------------------------------------------------------------
# 4. 失败安全
# ---------------------------------------------------------------------------


def test_injection_failure_keeps_schema_unchanged() -> None:
    schema = load_schema()
    snapshot = copy.deepcopy(schema)
    report = schema_sync.inject_profile_options(FakeConfig(schema), [])
    assert report["ok"] is False
    assert report["reason"] == "no_profile_entries"
    assert schema == snapshot, "拿不到配置文件列表时不得修改 schema"


def test_missing_target_field_keeps_schema_unchanged() -> None:
    """旧行为（部分写入）已修复：任一目标缺失时**一个字段都不写**。

    这是任务包 11 的核心回归用例；参数化版本见下方。
    """
    schema = load_schema()
    del schema["profiles"]["templates"]["profile_route"]["items"]["profile"]
    snapshot = copy.deepcopy(schema)
    report = schema_sync.inject_profile_options(FakeConfig(schema), ENTRIES)
    assert report["ok"] is False
    assert report["reason"].startswith("missing_target")
    assert report["injected"] == []
    assert schema == snapshot, "目标缺失时不得写入任何字段"


# ---------------------------------------------------------------------------
# 4b. 原子性（任务包 11）：任一目标缺失 → schema 一字不变
# ---------------------------------------------------------------------------

#: 三个注入目标各自的「删除方式」。
#: 每项是 ``(schema 里的字段路径, 删除方式, 报告里用的目标路径)``。
_MISSING_TARGET_CASES = (
    (
        "routes.templates.route.items.profile_id",
        lambda s: s["routes"]["templates"]["route"]["items"].pop("profile_id"),
        "routes.route.profile_id",
    ),
    (
        "profiles.templates.profile_route.items.profile",
        lambda s: s["profiles"]["templates"]["profile_route"]["items"].pop("profile"),
        "profiles.profile_route.profile",
    ),
    (
        "disabled_profiles",
        lambda s: s.pop("disabled_profiles"),
        "disabled_profiles",
    ),
)


@pytest.mark.parametrize(
    ("target_path", "mutate", "reason_path"),
    _MISSING_TARGET_CASES,
    ids=[case[0] for case in _MISSING_TARGET_CASES],
)
def test_injection_is_atomic_when_target_missing(
    target_path: str, mutate: Any, reason_path: str
) -> None:
    """任一注入目标缺失时，schema 必须与调用前的 deepcopy **完全相等**。"""
    schema = load_schema()
    mutate(schema)
    snapshot = copy.deepcopy(schema)

    report = schema_sync.inject_profile_options(FakeConfig(schema), ENTRIES)

    # 核心断言：整个 schema 一字未改。
    assert schema == snapshot, f"删除 {target_path} 后 schema 仍被部分修改"
    # 报告必须明确失败，且没有任何字段被记为已注入。
    assert report["ok"] is False
    assert report["reason"].startswith("missing_target")
    assert reason_path in report["reason"]
    assert report["injected"] == []
    assert report["options_count"] == 0
    assert report["historical_added"] == []


@pytest.mark.parametrize(
    ("target_path", "mutate", "reason_path"),
    _MISSING_TARGET_CASES,
    ids=[case[0] for case in _MISSING_TARGET_CASES],
)
def test_atomic_failure_leaves_no_options_anywhere(
    target_path: str, mutate: Any, reason_path: str
) -> None:
    """失败时不得留下任何半成品 options（避免「部分下拉、部分文本框」）。"""
    schema = load_schema()
    mutate(schema)
    schema_sync.inject_profile_options(FakeConfig(schema), ENTRIES)

    for top, template, field in (
        ("routes", "route", "profile_id"),
        ("profiles", "profile_route", "profile"),
        ("disabled_profiles", None, ""),
    ):
        try:
            node = _node(schema, top, template, field)
        except KeyError:
            continue
        assert "options" not in node, f"{top}.{field} 不应被写入 options"
        assert "labels" not in node, f"{top}.{field} 不应被写入 labels"


def test_length_mismatch_is_atomic() -> None:
    """options/labels 长度不一致时也不写任何字段。"""
    schema = load_schema()
    snapshot = copy.deepcopy(schema)

    original = schema_sync.build_profile_options

    def _broken(entries, historical_refs=(), *, include_wildcard=False):
        options, labels = original(
            entries, historical_refs, include_wildcard=include_wildcard
        )
        # 故意少一个 label，模拟配对错位。
        return options, labels[:-1]

    schema_sync.build_profile_options = _broken  # type: ignore[assignment]
    try:
        report = schema_sync.inject_profile_options(FakeConfig(schema), ENTRIES)
    finally:
        schema_sync.build_profile_options = original  # type: ignore[assignment]

    assert report["ok"] is False
    assert report["reason"].startswith("length_mismatch")
    assert report["injected"] == []
    assert schema == snapshot, "长度不匹配时不得写入任何字段"


def test_concurrent_injection_keeps_options_labels_paired() -> None:
    """并发调用后，options 与 labels 仍必须一一对应（锁的作用）。"""
    import threading

    schema = load_schema()
    config = FakeConfig(schema)
    errors: list[BaseException] = []

    def _worker() -> None:
        try:
            for _ in range(20):
                schema_sync.inject_profile_options(config, ENTRIES)
        except BaseException as exc:  # pragma: no cover - 只在真出错时触发
            errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, errors
    for top, template, field in (
        ("routes", "route", "profile_id"),
        ("profiles", "profile_route", "profile"),
        ("disabled_profiles", None, ""),
    ):
        node = _node(schema, top, template, field)
        assert len(node["options"]) == len(node["labels"]), (top, field)


def test_plain_dict_config_is_skipped() -> None:
    """退化情况：config 是普通 dict（无 .schema）时跳过而不是报错。"""
    report = schema_sync.inject_profile_options({"routes": []}, ENTRIES)
    assert report["ok"] is False
    assert report["reason"] == "schema_unavailable"


def test_none_config_is_skipped() -> None:
    report = schema_sync.inject_profile_options(None, ENTRIES)
    assert report["ok"] is False
    assert report["reason"] == "schema_unavailable"


def test_schema_is_mutated_in_place_not_replaced() -> None:
    """schema 对象本身不能被替换 —— 框架持有的是同一个引用。"""
    schema = load_schema()
    config = FakeConfig(schema)
    original = config.schema
    schema_sync.inject_profile_options(config, ENTRIES)
    assert config.schema is original, "不能替换 schema 对象"


def test_config_values_are_not_polluted() -> None:
    """注入不得把 options 写进配置值本身（AstrBotConfig.__setattr__ 的坑）。"""
    schema = load_schema()
    config = FakeConfig(schema, {"routes": []})
    schema_sync.inject_profile_options(config, ENTRIES)
    assert "options" not in config
    assert "labels" not in config
    assert config.get("routes") == []


# ---------------------------------------------------------------------------
# 5. 与插件运行时联动
# ---------------------------------------------------------------------------


def test_plugin_injects_on_init(tmp_path: Path) -> None:
    """插件初始化时会注入一次。"""
    import sys

    from .support import install_fake_astrbot, uninstall_fake_astrbot

    schema = load_schema()
    handle = install_fake_astrbot(tmp_path)
    sys.modules.pop("astrbot_plugin_llm_model_guard.main", None)
    try:
        main = importlib.import_module("astrbot_plugin_llm_model_guard.main")
    finally:
        uninstall_fake_astrbot(handle)
    class _Ctx:
        def __init__(self) -> None:
            self.provider_manager = type(
                "PM", (), {"inst_map": {"p1": FakeProvider("p1")}, "provider_insts": []}
            )()
            self.astrbot_config_mgr = FakeAcm(profiles={"u": "default"})
            self.registered_web_apis = []

        def get_using_provider(self, umo=None):
            return None

        def get_config(self, umo=None):
            return {}

        def register_web_api(self, route, handler, methods, desc):
            self.registered_web_apis.append((route, handler, list(methods), desc))

    config = FakeConfig(schema)
    plugin = main.LLMModelGuardPlugin(_Ctx(), config)
    node = _node(schema, "routes", "route", "profile_id")
    assert node["options"], "初始化后应注入 options"
    assert "default" in node["options"]
    assert plugin is not None


def test_schema_refresh_endpoint_registered() -> None:
    from astrbot_plugin_llm_model_guard.core import web_api

    routes = {route for route, _method in web_api.route_handlers()}
    assert web_api.ROUTE_SCHEMA_REFRESH in routes
    assert web_api.ROUTE_SCHEMA_REFRESH.startswith("/astrbot_plugin_llm_model_guard/")


def test_guard_refresh_profile_entries_bypasses_ttl(tmp_path: Path) -> None:
    runtime = build_runtime(
        tmp_path,
        config={},
        providers=[FakeProvider("p1")],
        profiles={"u": "default"},
    )
    runtime.guard.profile_entries
    runtime.guard.refresh_profile_entries()
    assert runtime.guard.profile_entries

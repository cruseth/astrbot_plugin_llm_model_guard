"""链路构建单测（v0.2.0）：直接读框架的模型顺序。

覆盖任务包 12 §5 的链构建部分：

* 会话级选择优先；
* abconf 默认模型；
* ``fallback_chat_models`` 顺序；
* 手动 extra 优先；
* 被标记项被绕过；
* 全部被标记时兜底；
* 缺失 provider 记 missing；
* 框架异常时降级为「未纳管不干预」。
"""

from __future__ import annotations

from pathlib import Path

from astrbot_plugin_llm_model_guard.core.chain import (
    build_chain,
    describe_chain,
    resolve_fallback_ids,
    resolve_framework_current,
)

from .support import FakeAcm, FakeProvider, build_runtime

UMO = "aiocqhttp:FriendMessage:1"


def _runtime(tmp_path: Path, **kwargs):
    return build_runtime(
        tmp_path,
        providers=[FakeProvider("p1"), FakeProvider("p2"), FakeProvider("p3")],
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 1. 框架当前模型
# ---------------------------------------------------------------------------


def test_framework_current_prefers_session_provider(tmp_path: Path) -> None:
    """会话级选择优先于 abconf 默认模型。"""
    runtime = _runtime(
        tmp_path,
        profiles={UMO: "default"},
        defaults={"default": "p2"},
        session_provider={UMO: "p1"},
    )
    provider_id, source = resolve_framework_current(
        provider_manager=runtime.context.provider_manager,
        umo=UMO,
    )
    assert provider_id == "p1"
    assert source == "session"


def test_framework_current_falls_back_to_abconf_default(tmp_path: Path) -> None:
    runtime = _runtime(
        tmp_path,
        profiles={UMO: "default"},
        defaults={"default": "p2"},
    )
    provider_id, source = resolve_framework_current(
        provider_manager=runtime.context.provider_manager,
        umo=UMO,
    )
    assert provider_id == "p2"
    assert source == "default"


def test_framework_current_falls_back_to_first_instance(tmp_path: Path) -> None:
    """没有会话级也没有默认模型时，兜底第一个 provider。"""
    runtime = _runtime(tmp_path, profiles={UMO: "default"})
    provider_id, source = resolve_framework_current(
        provider_manager=runtime.context.provider_manager,
        umo=UMO,
    )
    assert provider_id == "p1"
    assert source == "first"


def test_framework_current_none_when_no_providers(tmp_path: Path) -> None:
    runtime = build_runtime(tmp_path)
    provider_id, source = resolve_framework_current(
        provider_manager=runtime.context.provider_manager,
        umo=UMO,
    )
    assert provider_id == ""
    assert source == "none"


# ---------------------------------------------------------------------------
# 2. 回退列表
# ---------------------------------------------------------------------------


def test_fallback_ids_read_from_abconf(tmp_path: Path) -> None:
    runtime = _runtime(
        tmp_path,
        profiles={UMO: "default"},
        conf_fallbacks={"default": ["p2", "p3"]},
    )
    assert resolve_fallback_ids(
        provider_manager=runtime.context.provider_manager,
        umo=UMO,
    ) == ["p2", "p3"]


def test_fallback_ids_empty_when_not_list(tmp_path: Path) -> None:
    """``fallback_chat_models`` 不是 list 时按空处理，不抛异常。"""
    acm = FakeAcm(profiles={UMO: "default"})

    class _BadAcm(FakeAcm):
        def get_conf(self, umo: str):  # type: ignore[override]
            return {"provider_settings": {"fallback_chat_models": "not-a-list"}}

    bad = _BadAcm(profiles={UMO: "default"})
    runtime = _runtime(tmp_path, profiles={UMO: "default"})
    runtime.context.provider_manager.acm = bad
    assert resolve_fallback_ids(
        provider_manager=runtime.context.provider_manager,
        umo=UMO,
    ) == []
    assert acm is not None


# ---------------------------------------------------------------------------
# 3. 链路构建
# ---------------------------------------------------------------------------


def test_chain_order_default_then_fallbacks(tmp_path: Path) -> None:
    """链路 = 框架当前 + 该 abconf 的有序回退列表。"""
    runtime = _runtime(
        tmp_path,
        profiles={UMO: "default"},
        defaults={"default": "p1"},
        conf_fallbacks={"default": ["p2", "p3"]},
    )
    plan = build_chain(
        provider_manager=runtime.context.provider_manager,
        umo=UMO,
        known_ids={"p1", "p2", "p3"},
    )
    assert plan.candidates == ("p1", "p2", "p3")


def test_chain_manual_selection_first(tmp_path: Path) -> None:
    """手动 extra 优先于框架当前模型。"""
    runtime = _runtime(
        tmp_path,
        profiles={UMO: "default"},
        defaults={"default": "p1"},
        conf_fallbacks={"default": ["p2"]},
    )
    plan = build_chain(
        provider_manager=runtime.context.provider_manager,
        umo=UMO,
        manual_id="p3",
        known_ids={"p1", "p2", "p3"},
    )
    assert plan.candidates[0] == "p3"
    assert plan.source == "manual"


def test_chain_dedupes(tmp_path: Path) -> None:
    """回退列表里重复出现的 id 只保留一次。"""
    runtime = _runtime(
        tmp_path,
        profiles={UMO: "default"},
        defaults={"default": "p1"},
        conf_fallbacks={"default": ["p1", "p2", "p1"]},
    )
    plan = build_chain(
        provider_manager=runtime.context.provider_manager,
        umo=UMO,
        known_ids={"p1", "p2"},
    )
    assert plan.candidates == ("p1", "p2")


def test_chain_filters_missing_providers(tmp_path: Path) -> None:
    """不存在的 provider id 被剔除并记入 missing。"""
    runtime = _runtime(
        tmp_path,
        profiles={UMO: "default"},
        defaults={"default": "p1"},
        conf_fallbacks={"default": ["ghost", "p2"]},
    )
    plan = build_chain(
        provider_manager=runtime.context.provider_manager,
        umo=UMO,
        known_ids={"p1", "p2"},
    )
    assert plan.candidates == ("p1", "p2")
    assert plan.missing == ("ghost",)


def test_chain_bypasses_marked(tmp_path: Path) -> None:
    """被标记的模型被移到 bypassed，不在候选链里。"""
    runtime = _runtime(
        tmp_path,
        profiles={UMO: "default"},
        defaults={"default": "p1"},
        conf_fallbacks={"default": ["p2"]},
    )
    plan = build_chain(
        provider_manager=runtime.context.provider_manager,
        umo=UMO,
        known_ids={"p1", "p2"},
        marked_ids={"p1"},
    )
    assert plan.candidates == ("p2",)
    assert plan.bypassed == ("p1",)


def test_chain_all_marked_uses_head(tmp_path: Path) -> None:
    """全部被标记时用链首兜底并告警。"""
    runtime = _runtime(
        tmp_path,
        profiles={UMO: "default"},
        defaults={"default": "p1"},
    )
    plan = build_chain(
        provider_manager=runtime.context.provider_manager,
        umo=UMO,
        known_ids={"p1"},
        marked_ids={"p1"},
    )
    assert plan.candidates == ("p1",)
    assert plan.degraded_use == "p1"
    assert any("all_marked" in w for w in plan.warnings)


def test_chain_degrades_when_framework_broken(tmp_path: Path) -> None:
    """框架异常时降级为「未纳管不干预」，不抛异常。"""

    class _BrokenManager:
        def get_using_provider(self, *args: object, **kwargs: object):
            raise RuntimeError("框架炸了")

    plan = build_chain(
        provider_manager=_BrokenManager(),
        umo=UMO,
        known_ids={"p1"},
    )
    assert plan.candidates == ()
    assert plan.source == "unmanaged"


def test_chain_with_no_provider_manager(tmp_path: Path) -> None:
    plan = build_chain(provider_manager=None, umo=UMO, known_ids={"p1"})
    assert plan.candidates == ()
    assert plan.source == "unmanaged"


# ---------------------------------------------------------------------------
# 4. 每个配置文件用自己的顺序
# ---------------------------------------------------------------------------


def test_each_profile_has_own_order(tmp_path: Path) -> None:
    """不同配置文件用各自的模型顺序。"""
    runtime = build_runtime(
        tmp_path,
        providers=[FakeProvider("p1"), FakeProvider("p2"), FakeProvider("p3")],
        profiles={"umo-a": "default", "umo-b": "other"},
        defaults={"default": "p1", "other": "p3"},
        conf_fallbacks={"default": ["p2"], "other": ["p1"]},
    )
    plan_a = build_chain(
        provider_manager=runtime.context.provider_manager,
        umo="umo-a",
        known_ids={"p1", "p2", "p3"},
    )
    plan_b = build_chain(
        provider_manager=runtime.context.provider_manager,
        umo="umo-b",
        known_ids={"p1", "p2", "p3"},
    )
    assert plan_a.candidates == ("p1", "p2")
    assert plan_b.candidates == ("p3", "p1")


# ---------------------------------------------------------------------------
# 5. describe_chain
# ---------------------------------------------------------------------------


def test_describe_chain_output(tmp_path: Path) -> None:
    runtime = _runtime(
        tmp_path,
        profiles={UMO: "default"},
        defaults={"default": "p1"},
        conf_fallbacks={"default": ["p2"]},
    )
    plan = build_chain(
        provider_manager=runtime.context.provider_manager,
        umo=UMO,
        known_ids={"p1", "p2"},
    )
    view = describe_chain(
        plan,
        profile_id="default",
        profile_name="默认配置",
        default_provider="p1",
        fallbacks=["p2"],
    )
    assert view["profile_id"] == "default"
    assert view["profile_display"] == "默认配置 (default)"
    assert view["chain"] == ["p1", "p2"]
    assert view["default_provider"] == "p1"
    assert view["fallbacks"] == ["p2"]

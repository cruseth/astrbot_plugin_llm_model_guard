"""配置快照单测（v0.2.0）：8 个全局键、旧键被静默忽略、取值保护。

v0.2.0 的核心承诺（任务包 12 §2）：

* 配置只有 **8 个键**，其中**没有任何 provider id / 配置文件 id**；
* **旧配置里的未知键必须被静默忽略**（不报错、不崩溃）。
"""

from __future__ import annotations

from pathlib import Path

from astrbot_plugin_llm_model_guard.core.models import (
    DEFAULT_RETRY_CATEGORIES,
    GuardSettings,
)

from .support import FakeProvider, build_runtime


# ---------------------------------------------------------------------------
# 键集合
# ---------------------------------------------------------------------------


def test_schema_has_exactly_nine_keys() -> None:
    """``_conf_schema.json`` 恰好 9 个键（8 个行为配置 + ``allow_webui_actions``）。"""
    import json
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "_conf_schema.json"
    schema = json.loads(path.read_text(encoding="utf-8"))
    assert len(schema) == 9
    assert sorted(schema) == [
        "allow_webui_actions",
        "enable",
        "max_retries",
        "probe_interval_seconds",
        "probe_success_threshold",
        "probe_timeout_seconds",
        "request_timeout_seconds",
        "retry_backoff_seconds",
        "retry_categories",
    ]


def test_schema_has_no_ids() -> None:
    """schema 里不允许出现 provider id / 配置文件 id 字段。"""
    import json
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "_conf_schema.json"
    schema = json.loads(path.read_text(encoding="utf-8"))
    text = path.read_text(encoding="utf-8")
    for banned in ("routes", "profiles", "fallback_pool", "disabled_profiles",
                   "error_rules", "mark_scope"):
        assert banned not in schema, banned
        assert f'"{banned}"' not in text, banned


# ---------------------------------------------------------------------------
# 默认值
# ---------------------------------------------------------------------------


def test_defaults_match_task_spec() -> None:
    settings = GuardSettings.from_mapping({})
    assert settings.enable is True
    assert settings.request_timeout_seconds == 120
    assert settings.max_retries == 1
    assert settings.retry_backoff_seconds == 1.5
    assert settings.retry_categories == DEFAULT_RETRY_CATEGORIES
    assert settings.probe_interval_seconds == 60
    assert settings.probe_timeout_seconds == 30
    assert settings.probe_success_threshold == 2
    # 页面写操作默认关闭（面板登录态是全局的，默认开启会扩大误操作面）。
    assert settings.allow_webui_actions is False


def test_retry_categories_default_value() -> None:
    assert DEFAULT_RETRY_CATEGORIES == (
        "timeout",
        "rate_limit",
        "server_error",
        "network",
        "overloaded",
    )


# ---------------------------------------------------------------------------
# retry_categories：overloaded 默认可重试，empty_output 默认不可重试
# ---------------------------------------------------------------------------


def test_overloaded_is_retryable_by_default() -> None:
    """529 / overloaded 是瞬时错误，默认就该重试（不重试等于过度惩罚）。"""
    settings = GuardSettings.from_mapping({})
    assert settings.is_retryable("overloaded") is True


def test_empty_output_is_not_retryable_by_default() -> None:
    """空输出默认不重试：框架自身已有 tenacity 重试，插件叠加会放大请求量。"""
    settings = GuardSettings.from_mapping({})
    assert settings.is_retryable("empty_output") is False


def test_empty_output_becomes_retryable_when_configured() -> None:
    """把 ``empty_output`` 加进配置后，它就变成可重试。"""
    settings = GuardSettings.from_mapping(
        {"retry_categories": ["timeout", "empty_output"]}
    )
    assert settings.is_retryable("empty_output") is True
    assert settings.is_retryable("timeout") is True
    assert settings.is_retryable("rate_limit") is False


def test_overloaded_can_be_removed_from_categories() -> None:
    """用户可以把 ``overloaded`` 从列表里去掉（恢复「不重试直接标记」）。"""
    settings = GuardSettings.from_mapping({"retry_categories": ["timeout"]})
    assert settings.is_retryable("overloaded") is False


def test_both_new_categories_are_valid_options() -> None:
    """``overloaded`` 与 ``empty_output`` 都是合法取值（不会被过滤掉）。"""
    settings = GuardSettings.from_mapping(
        {"retry_categories": ["overloaded", "empty_output"]}
    )
    assert set(settings.retry_categories) == {"overloaded", "empty_output"}


def test_schema_retry_categories_options_include_new_categories() -> None:
    """schema 的 ``options`` 含两个新类别，``default`` 只含 ``overloaded``。"""
    import json
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "_conf_schema.json"
    schema = json.loads(path.read_text(encoding="utf-8"))
    block = schema["retry_categories"]
    assert "overloaded" in block["options"]
    assert "empty_output" in block["options"]
    # empty_output 默认不重试。
    assert "overloaded" in block["default"]
    assert "empty_output" not in block["default"]


# ---------------------------------------------------------------------------
# allow_webui_actions
# ---------------------------------------------------------------------------


def test_allow_webui_actions_defaults_to_false() -> None:
    """默认关闭：面板登录态是全局的，默认开启会扩大误操作面。"""
    assert GuardSettings.from_mapping({}).allow_webui_actions is False
    assert GuardSettings.from_mapping({"enable": True}).allow_webui_actions is False


def test_allow_webui_actions_can_be_enabled() -> None:
    """显式配置 true 时开启写操作（该键在 schema 里，用户看得见）。"""
    settings = GuardSettings.from_mapping({"allow_webui_actions": True})
    assert settings.allow_webui_actions is True


# ---------------------------------------------------------------------------
# 旧键被静默忽略
# ---------------------------------------------------------------------------


def test_old_config_keys_are_ignored() -> None:
    """含 v0.1.x 全部旧键的配置不会报错、也不会影响行为。"""
    old_config = {
        # v0.1.x 的路由相关
        "routes": [
            {
                "profile_id": "default",
                "primary": "p1",
                "fallbacks": ["p2"],
                "max_retries": 5,
                "enable": True,
            }
        ],
        "profiles": [{"profile": "生产环境", "primary": "p1"}],
        "fallback_pool": ["p2"],
        "disabled_profiles": ["default"],
        "error_rules": [
            {
                "id": "r1",
                "match_status": [500],
                "action": "retry_same",
            }
        ],
        # v0.1.x 的开关与统计
        "enable_routing": True,
        "enable_retry": True,
        "enable_fallback": True,
        "enable_marking": True,
        "enable_probe": True,
        "enable_monitoring": True,
        "unmanaged_monitoring": True,
        "respect_manual_selection": True,
        "respect_session_selection": True,
        "mark_scope": "profile",
        "union_framework_fallbacks": True,
        "allow_cross_provider_switch_mid_run": True,
        "max_retries_per_model": 9,
        "retry_backoff_base": 3.0,
        "retry_backoff_factor": 2.0,
        "retry_backoff_max": 30.0,
        "retry_jitter_ratio": 0.5,
        "retry_total_timeout": 60.0,
        "retry_budget_per_umo_per_minute": 5,
        "retry_empty_output": True,
        "mark_on_empty_output_after": 3,
        "mark_ttl_default": 100,
        "mark_ttl_rate_limit": 100,
        "mark_ttl_server_error": 100,
        "mark_ttl_network": 100,
        "double_confirm": True,
        "confirm_delay_seconds": 30,
        "probe_interval_seconds": 999,
        "probe_timeout_seconds": 999,
        "probe_max_concurrency": 4,
        "probe_skip_when_busy": False,
        "probe_tick_seconds": 9,
        "probe_on_startup": False,
        "stats_window_seconds": 60,
        "stats_recent_events": 10,
        "stats_persist_interval_seconds": 5,
        "stats_retention_days": 1,
        "allow_webui_actions": True,
        "log_level": "debug",
    }
    settings = GuardSettings.from_mapping(old_config)
    # 全部回落到 v0.2.0 的默认值（旧键一律不看）。
    assert settings.enable is True
    assert settings.request_timeout_seconds == 120
    assert settings.max_retries == 1
    assert settings.retry_backoff_seconds == 1.5
    assert settings.retry_categories == DEFAULT_RETRY_CATEGORIES
    # ``probe_interval_seconds`` / ``probe_timeout_seconds`` 在 v0.2.0 **同名保留**，
    # 所以旧配置里的值会延续下来（这是有意的：键名与语义都没变）。
    assert settings.probe_interval_seconds == 999
    assert settings.probe_timeout_seconds == 999
    # ``probe_success_threshold`` 是 v0.2.0 新增的键 → 用默认值。
    assert settings.probe_success_threshold == 2
    # 旧键没有对应的属性了。
    for gone in ("routes", "profiles", "fallback_pool", "disabled_profiles",
                 "error_rules", "mark_scope", "max_retries_per_model"):
        assert not hasattr(settings, gone), gone


def test_old_config_does_not_crash_plugin(tmp_path: Path) -> None:
    """含旧键的配置能让插件正常启动。"""
    runtime = build_runtime(
        tmp_path,
        config={
            "routes": [{"profile_id": "default", "primary": "p1"}],
            "mark_scope": "profile",
            "enable_routing": True,
        },
        providers=[FakeProvider("p1")],
    )
    assert runtime.guard.settings.enable is True
    assert runtime.guard.settings.max_retries == 1


# ---------------------------------------------------------------------------
# 取值保护
# ---------------------------------------------------------------------------


def test_negative_values_are_clamped() -> None:
    settings = GuardSettings.from_mapping(
        {
            "request_timeout_seconds": -5,
            "max_retries": -3,
            "retry_backoff_seconds": -1.0,
            "probe_interval_seconds": 0,
            "probe_timeout_seconds": 0,
            "probe_success_threshold": 0,
        }
    )
    assert settings.request_timeout_seconds == 0
    assert settings.max_retries == 0
    assert settings.retry_backoff_seconds == 0.0
    assert settings.probe_interval_seconds == 1
    assert settings.probe_timeout_seconds == 1
    assert settings.probe_success_threshold == 1


def test_invalid_types_fall_back_to_defaults() -> None:
    settings = GuardSettings.from_mapping(
        {
            "request_timeout_seconds": "not-a-number",
            "max_retries": None,
            "retry_backoff_seconds": "abc",
            "probe_success_threshold": {},
        }
    )
    assert settings.request_timeout_seconds == 120
    assert settings.max_retries == 1
    assert settings.retry_backoff_seconds == 1.5
    assert settings.probe_success_threshold == 2


def test_invalid_retry_categories_filtered() -> None:
    """非法类别被剔除；全非法时回退默认。"""
    settings = GuardSettings.from_mapping(
        {"retry_categories": ["timeout", "not_a_category"]}
    )
    assert settings.retry_categories == ("timeout",)

    fallback = GuardSettings.from_mapping({"retry_categories": ["nope"]})
    assert fallback.retry_categories == DEFAULT_RETRY_CATEGORIES

    empty = GuardSettings.from_mapping({"retry_categories": []})
    assert empty.retry_categories == DEFAULT_RETRY_CATEGORIES


# ---------------------------------------------------------------------------
# 派生行为
# ---------------------------------------------------------------------------


def test_is_retryable_follows_categories() -> None:
    settings = GuardSettings.from_mapping({"retry_categories": ["timeout"]})
    assert settings.is_retryable("timeout") is True
    assert settings.is_retryable("rate_limit") is False
    assert settings.is_retryable("server_error") is False


def test_retry_delay_is_exponential_capped() -> None:
    settings = GuardSettings.from_mapping(
        {"retry_backoff_seconds": 2.0, "max_retries": 5}
    )
    assert settings.retry_delay(1) == 2.0
    assert settings.retry_delay(2) == 4.0
    assert settings.retry_delay(3) == 8.0
    assert settings.retry_delay(4) == 10.0  # 上限
    assert settings.retry_delay(10) == 10.0


def test_request_timeout_zero_means_unlimited() -> None:
    assert GuardSettings.from_mapping({"request_timeout_seconds": 0}).request_timeout() is None
    assert (
        GuardSettings.from_mapping({"request_timeout_seconds": 30}).request_timeout() == 30.0
    )


def test_fingerprint_changes_with_config() -> None:
    a = GuardSettings.from_mapping({"max_retries": 1})
    b = GuardSettings.from_mapping({"max_retries": 2})
    assert a.fingerprint() != b.fingerprint()
    assert a.fingerprint() == GuardSettings.from_mapping({"max_retries": 1}).fingerprint()

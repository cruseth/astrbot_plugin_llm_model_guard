"""统计聚合单测：计数、成功率、延迟分位、最近事件上限与导出结构。"""

from __future__ import annotations

from astrbot_plugin_llm_model_guard.core.metrics import Metrics
from astrbot_plugin_llm_model_guard.core.models import AttemptRecord

from .support import FakeClock


def make_metrics(*, window_s: int = 900, recent_cap: int = 200) -> tuple[Metrics, FakeClock]:
    clock = FakeClock()
    return Metrics(window_s=window_s, recent_cap=recent_cap, clock=clock), clock


def record(
    metrics: Metrics,
    *,
    used: str = "p1",
    ok: bool = True,
    total_ms: int = 100,
    error_class: str | None = None,
    attempt_index: int = 1,
) -> None:
    metrics.record_attempt(
        AttemptRecord(
            ts=metrics._clock(),
            used=used,
            ok=ok,
            total_ms=total_ms,
            error_class=error_class,
            attempt_index=attempt_index,
        )
    )


def test_counts_and_rates() -> None:
    metrics, _ = make_metrics()
    record(metrics, ok=True)
    record(metrics, ok=True)
    record(metrics, ok=False, error_class="rate_limit")
    record(metrics, ok=False, error_class="auth")

    totals = metrics.totals()
    assert totals["calls"] == 4
    assert totals["ok"] == 2
    assert totals["errors"] == 2
    assert totals["success_rate"] == 0.5

    stat = metrics.per_provider()["p1"]
    assert stat["calls"] == 4
    assert stat["consecutive_errors"] == 2
    assert stat["error_classes"] == {"rate_limit": 1, "auth": 1}
    assert stat["last_error"]["class"] == "auth"


def test_p50_p95_window() -> None:
    metrics, clock = make_metrics(window_s=60)
    for value in (100, 200, 300, 400, 500):
        record(metrics, total_ms=value)
        clock.advance(1)
    latency = metrics.per_provider()["p1"]["latency_ms"]
    assert latency["n"] == 5
    assert latency["p50"] == 300
    assert latency["p95"] == 500
    assert latency["avg"] == 300

    # 跳出窗口后不再计入分位
    clock.advance(120)
    record(metrics, total_ms=900)
    latency = metrics.per_provider()["p1"]["latency_ms"]
    assert latency["n"] == 1
    assert latency["p50"] == 900


def test_recent_events_cap() -> None:
    metrics, _ = make_metrics(recent_cap=5)
    for index in range(12):
        record(metrics, total_ms=index)
    events = metrics.recent_events(limit=50)
    assert len(events) == 5
    assert events[0]["total_ms"] == 11  # 最新在前
    assert metrics.events_total() == 5


def test_recent_events_filters() -> None:
    metrics, _ = make_metrics()
    now = metrics._clock()
    metrics.record_attempt(
        AttemptRecord(ts=now, used="p1", ok=True, profile_id="default", umo="u1")
    )
    metrics.record_attempt(
        AttemptRecord(ts=now, used="p2", ok=False, profile_id="other", umo="u2")
    )
    assert [item["used"] for item in metrics.recent_events(limit=10, provider_id="p1")] == ["p1"]
    assert [
        item["used"] for item in metrics.recent_events(limit=10, profile_id="other")
    ] == ["p2"]


def test_overview_shape() -> None:
    metrics, _ = make_metrics()
    record(metrics, ok=False, error_class="timeout")
    overview = metrics.overview()
    assert set(overview) == {"window_s", "totals", "counters", "providers"}
    assert "guard_internal_errors" in overview["counters"]
    assert overview["providers"]["p1"]["latency_ms"]["last"] == 100


def test_export_import_roundtrip() -> None:
    metrics, _ = make_metrics()
    record(metrics, ok=False, error_class="network")
    metrics.record_probe("p1", ok=False, latency_ms=123)
    exported = metrics.export_state()
    assert "providers" in exported and "recent_events" in exported
    assert exported["probes"]["p1"]["latency_ms"] == 123

    restored, _ = make_metrics()
    restored.import_state(exported)
    stat = restored.per_provider()["p1"]
    assert stat["calls"] == 1
    assert stat["errors"] == 1
    assert stat["probe"]["last_ok"] is False


def test_fallback_hops_counter_not_inferred_from_attempt() -> None:
    """P3-1：``framework_fallback_hops`` 不再按 attempt_index 推断（由包装层计数）。"""
    metrics, _ = make_metrics()
    record(metrics, attempt_index=2)
    record(metrics, attempt_index=1)
    assert metrics.counters["framework_fallback_hops"] == 0
    metrics.note_counter("framework_fallback_hops")
    assert metrics.counters["framework_fallback_hops"] == 1


def test_prune_by_retention() -> None:
    metrics, clock = make_metrics()
    record(metrics, used="old")
    clock.advance(10 * 86400)
    record(metrics, used="fresh")
    removed = metrics.prune(retention_days=7)
    assert removed == 1
    assert set(metrics.per_provider()) == {"fresh"}


def test_sensitive_keys_dropped_on_import() -> None:
    metrics, _ = make_metrics()
    metrics.import_state(
        {
            "recent_events": [
                {"ts": 1.0, "used": "p1", "prompt": "用户消息", "contexts": []}
            ]
        }
    )
    events = metrics.recent_events(limit=5)
    assert events and "prompt" not in events[0]
    assert "contexts" not in events[0]

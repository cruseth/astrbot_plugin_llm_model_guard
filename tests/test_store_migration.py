"""状态持久化单测：缺文件、损坏备份、版本迁移与原子替换。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from astrbot_plugin_llm_model_guard.core.store import SCHEMA_VERSION, StateStore

from .support import FakeClock


def make_store(tmp_path: Path, **kwargs) -> StateStore:
    return StateStore(tmp_path, clock=FakeClock(), **kwargs)


def test_missing_file(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    state = store.load()
    assert state["schema_version"] == SCHEMA_VERSION
    assert state["marks"] == {}
    assert state["recent_events"] == []
    assert store.path.exists() is False
    assert store.last_load_error is None


def test_corrupt_json_backup(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.path.write_text("{ this is not json", encoding="utf-8")
    state = store.load()
    assert state["schema_version"] == SCHEMA_VERSION
    assert store.last_load_error is not None
    backups = list(tmp_path.glob("state.corrupt-*.json"))
    assert len(backups) == 1
    assert "this is not json" in backups[0].read_text(encoding="utf-8")


def test_version_zero_migrate(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.path.write_text(
        json.dumps({"marks": {"a": {"state": "marked"}}, "recent_events": []}),
        encoding="utf-8",
    )
    state = store.load()
    assert state["schema_version"] == SCHEMA_VERSION
    assert "a" in state["marks"]
    assert (tmp_path / "state.v0.bak.json").exists()


def test_future_version_backup_and_reset(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.path.write_text(
        json.dumps({"schema_version": 99, "marks": {"a": {"state": "marked"}}}),
        encoding="utf-8",
    )
    state = store.load()
    assert state["marks"] == {}
    assert state["schema_version"] == SCHEMA_VERSION
    assert (tmp_path / "state.v99.bak.json").exists()
    assert store.last_load_error is not None


def test_migrate_is_pure_function(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    raw = {"marks": {"x": {"state": "marked"}}}
    migrated = store.migrate(raw, 0)
    assert migrated["schema_version"] == SCHEMA_VERSION
    assert raw == {"marks": {"x": {"state": "marked"}}}  # 输入未被修改


def test_prune_recent_events(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    events = [{"ts": float(i), "used": "p"} for i in range(10)]
    payload = store.build_payload(
        {"schema_version": SCHEMA_VERSION, "recent_events": events},
        recent_cap=3,
    )
    assert len(payload["recent_events"]) == 3
    assert payload["recent_events"][-1]["ts"] == 9.0


def test_no_sensitive_fields(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    payload = store.build_payload(
        {
            "schema_version": SCHEMA_VERSION,
            "recent_events": [
                {
                    "ts": 1.0,
                    "used": "p",
                    "prompt": "用户的原话",
                    "contexts": [{"role": "user", "content": "秘密"}],
                    "system_prompt": "系统提示词",
                    "error_message": "boom",
                }
            ],
        }
    )
    encoded = json.dumps(payload, ensure_ascii=False)
    for banned in ("prompt", "contexts", "system_prompt", "用户的原话", "秘密"):
        assert banned not in encoded
    assert "error_message" in encoded


def test_atomic_replace(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.write_sync({"schema_version": SCHEMA_VERSION, "marks": {"a": {"state": "marked"}}})
    assert store.path.exists()
    assert not store.path.with_suffix(".json.tmp").exists()
    data = json.loads(store.path.read_text(encoding="utf-8"))
    assert data["marks"]["a"]["state"] == "marked"


def test_flush_writes_file(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    state = {
        "schema_version": SCHEMA_VERSION,
        "marks": {"a": {"state": "confirming"}},
        "providers": {"a": {"calls": 1}},
    }
    assert asyncio.run(store.flush(state, force=True)) is True
    loaded = json.loads(store.path.read_text(encoding="utf-8"))
    assert loaded["marks"]["a"]["state"] == "confirming"
    assert loaded["schema_version"] == SCHEMA_VERSION
    assert store.write_count == 1
    # 再次 load 应得到同样的标记
    assert store.load()["marks"]["a"]["state"] == "confirming"


def test_schedule_save_throttle(tmp_path: Path) -> None:
    clock = FakeClock()
    store = StateStore(tmp_path, clock=clock)
    state: dict = {"schema_version": SCHEMA_VERSION}
    assert store.schedule_save(state, interval=10) is True
    assert store.schedule_save(state, interval=10) is False
    clock.advance(11)
    assert store.schedule_save(state, interval=10) is True


def test_oversized_state_is_shrunk(tmp_path: Path) -> None:
    store = make_store(tmp_path, max_bytes=4096)
    events = [
        {"ts": float(i), "used": "p", "error_message": "x" * 500} for i in range(50)
    ]
    payload = store.build_payload(
        {"schema_version": SCHEMA_VERSION, "recent_events": events},
        recent_cap=50,
    )
    size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    assert size <= 4096
    assert len(payload["recent_events"]) < 50


# ---------------------------------------------------------------------------
# 第 2 轮修复：P2-10 并发落盘
# ---------------------------------------------------------------------------


def test_concurrent_flush_keeps_file_valid(tmp_path: Path) -> None:
    """并发 10 次 flush 后状态文件必须是完整合法 JSON（P2-10 写盘锁 + 唯一 tmp）。"""
    store = make_store(tmp_path)

    async def _burst() -> list[bool]:
        async def _one(index: int) -> bool:
            return await store.flush(
                {
                    "schema_version": SCHEMA_VERSION,
                    "marks": {
                        f"p{index}": {"state": "marked", "provider_id": f"p{index}"}
                    },
                    "providers": {f"p{index}": {"calls": index}},
                },
                force=True,
            )

        return await asyncio.gather(*(_one(i) for i in range(10)))

    results = asyncio.run(_burst())
    assert all(results) is True
    data = json.loads(store.path.read_text(encoding="utf-8"))
    assert data["schema_version"] == SCHEMA_VERSION
    assert isinstance(data["marks"], dict)
    # 每次写入都是完整 payload（10 个标记之一，不会出现混合/截断）
    assert len(data["marks"]) == 1
    assert len(data["providers"]) == 1
    # 没有残留的临时文件
    assert list(tmp_path.glob("*.tmp")) == []


def test_write_sync_uses_unique_tmp_files(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    first = store.write_sync({"schema_version": SCHEMA_VERSION, "marks": {}})
    second = store.write_sync({"schema_version": SCHEMA_VERSION, "marks": {}})
    assert first == second == store.path
    assert list(tmp_path.glob("state.json.*.tmp")) == []

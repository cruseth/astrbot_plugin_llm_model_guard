"""状态持久化：``state.json`` 的原子读写、版本迁移与损坏容错。

要点（规划文档 §8）：

* 路径由调用方注入（生产环境是 ``StarTools.get_data_dir(插件名)``），单测注入临时目录；
* 写入「临时文件 + ``os.replace``」原子替换，并且在线程池里执行，不阻塞事件循环；
* JSON 损坏或版本过新时**备份后以空状态运行**，绝不抛异常给调用方；
* 写盘前按白名单裁剪，保证落盘内容里没有 prompt / contexts 之类的敏感字段。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from .models import LOG_PREFIX, PLUGIN_NAME

#: 当前状态文件版本号。
SCHEMA_VERSION = 1

#: 单文件体积上限（规划文档 §8.3）。
MAX_STATE_BYTES = 4 * 1024 * 1024

STATE_FILE_NAME = "state.json"


def empty_state(*, plugin_version: str = "0.1.0") -> dict[str, Any]:
    """空状态骨架；所有读取路径都以它为底。"""
    return {
        "schema_version": SCHEMA_VERSION,
        "plugin_version": plugin_version,
        "saved_at": 0.0,
        "marks": {},
        "marks_by_profile": {},
        "providers": {},
        "recent_events": [],
        "probes": {},
        "runtime": {},
    }


class StateStore:
    """``state.json`` 的读写器；不做任何业务判断。"""

    SCHEMA_VERSION = SCHEMA_VERSION

    def __init__(
        self,
        data_dir: Path | str,
        *,
        clock: Callable[[], float] | None = None,
        logger: Any = None,
        max_bytes: int = MAX_STATE_BYTES,
        plugin_version: str = "0.1.0",
    ) -> None:
        self.data_dir = Path(data_dir)
        self._clock = clock or time.time
        self._logger = logger
        self.max_bytes = max(1024, int(max_bytes))
        self.plugin_version = plugin_version
        self._last_flush_at = 0.0
        self._last_load_error: str | None = None
        self._writes = 0
        #: 写盘锁：并发 flush 串行化（规划文档 §12.1 的 ``_persist_lock``，P2-10）。
        self._write_lock = asyncio.Lock()
        self._write_lock_loop: Any = None
        self._tmp_seq = 0

    # ---- 路径与状态 ----

    @property
    def path(self) -> Path:
        return self.data_dir / STATE_FILE_NAME

    @property
    def last_load_error(self) -> str | None:
        return self._last_load_error

    @property
    def write_count(self) -> int:
        return self._writes

    # ---- 读取 ----

    def load(self) -> dict[str, Any]:
        """读取状态；任何异常都退化为空状态并留下备份与日志。"""
        self._last_load_error = None
        path = self.path
        try:
            if not path.exists():
                return empty_state(plugin_version=self.plugin_version)
            raw_text = path.read_text(encoding="utf-8")
        except OSError as exc:
            self._last_load_error = f"读取失败：{exc}"
            self._error(f"读取状态文件失败，按空状态运行：{exc}")
            return empty_state(plugin_version=self.plugin_version)

        try:
            raw = json.loads(raw_text)
        except (json.JSONDecodeError, ValueError) as exc:
            self._last_load_error = f"JSON 损坏：{exc}"
            backup = self._backup("corrupt")
            self._error(f"状态文件损坏（{exc}），已备份为 {backup}，按空状态运行")
            return empty_state(plugin_version=self.plugin_version)

        if not isinstance(raw, dict):
            self._last_load_error = "顶层不是对象"
            backup = self._backup("corrupt")
            self._error(f"状态文件顶层不是对象，已备份为 {backup}，按空状态运行")
            return empty_state(plugin_version=self.plugin_version)

        version = raw.get("schema_version")
        try:
            version_int = int(version) if version is not None else 0
        except (TypeError, ValueError):
            version_int = 0

        if version_int > SCHEMA_VERSION:
            backup = self._backup(f"v{version_int}")
            self._last_load_error = f"状态文件版本 {version_int} 高于插件支持的 {SCHEMA_VERSION}"
            self._error(f"{self._last_load_error}，已备份为 {backup}，按空状态运行")
            return empty_state(plugin_version=self.plugin_version)

        if version_int < SCHEMA_VERSION:
            backup = self._backup(f"v{version_int}")
            self._info(f"状态文件版本 {version_int} < {SCHEMA_VERSION}，已备份为 {backup} 并迁移")
            return self.migrate(raw, version_int)

        return self.normalize(raw)

    def normalize(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        """补齐缺失字段并做类型收敛，保持结构稳定。"""
        state = empty_state(plugin_version=self.plugin_version)
        state.update({key: value for key, value in raw.items() if key not in ("saved_at",)})
        state["schema_version"] = SCHEMA_VERSION
        for key, default in (
            ("marks", {}),
            ("marks_by_profile", {}),
            ("providers", {}),
            ("probes", {}),
            ("runtime", {}),
        ):
            if not isinstance(state.get(key), Mapping):
                state[key] = dict(default)
            else:
                state[key] = dict(state[key])
        if not isinstance(state.get("recent_events"), list):
            state["recent_events"] = []
        state["saved_at"] = float(state.get("saved_at") or 0.0)
        return state

    def migrate(self, raw: Mapping[str, Any], from_version: int) -> dict[str, Any]:
        """纯函数式迁移：从 ``from_version`` 逐级升级到当前版本。"""
        current = dict(raw)
        version = from_version
        while version < SCHEMA_VERSION:
            migrate_step = _MIGRATIONS.get(version)
            if migrate_step is not None:
                current = migrate_step(current)
            version += 1
        return self.normalize(current)

    def _backup(self, tag: str) -> str:
        """把当前状态文件复制成备份，返回备份文件名。"""
        suffix = f"state.{tag}"
        if tag.isdigit() or (tag.startswith("v") and tag[1:].isdigit()):
            name = f"{suffix}.bak.json"
        else:
            name = f"state.{tag}-{int(self._clock())}.json"
        target = self.data_dir / name
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            target.write_bytes(self.path.read_bytes())
        except OSError as exc:  # pragma: no cover - 磁盘异常
            self._error(f"备份状态文件失败：{exc}")
            return name
        return name

    # ---- 写入 ----

    def schedule_save(
        self,
        state: dict[str, Any],
        *,
        interval: float = 2.0,
        now: float | None = None,
    ) -> bool:
        """去抖判断：返回 ``True`` 表示调用方应立即排一次 :meth:`flush`。

        纯统计落盘走节流；标记变更由调用方直接 ``flush(force=True)``。
        """
        current = self._clock() if now is None else now
        if interval <= 0:
            self._last_flush_at = current
            return True
        if current - self._last_flush_at < interval:
            return False
        self._last_flush_at = current
        return True

    def build_payload(self, state: Mapping[str, Any], *, recent_cap: int = 200) -> dict[str, Any]:
        """裁剪 + 白名单化，返回可直接序列化的 payload。"""
        payload = self.normalize(state)
        payload["schema_version"] = SCHEMA_VERSION
        payload["plugin_version"] = self.plugin_version
        payload["saved_at"] = float(self._clock())

        events = payload.get("recent_events") or []
        if not isinstance(events, list):
            events = []
        if recent_cap > 0:
            events = events[-recent_cap:]
        payload["recent_events"] = [_scrub_event(event) for event in events if isinstance(event, Mapping)]
        return self._shrink(payload)

    def _shrink(self, payload: dict[str, Any]) -> dict[str, Any]:
        """保证落盘体积不超过上限：先删旧事件，再精简延迟样本。"""
        encoded = _dumps(payload)
        if len(encoded.encode("utf-8")) <= self.max_bytes:
            return payload

        events = payload.get("recent_events") or []
        while events and len(_dumps(payload).encode("utf-8")) > self.max_bytes:
            events = events[1:]
            payload["recent_events"] = events
        if len(_dumps(payload).encode("utf-8")) <= self.max_bytes:
            return payload

        providers = payload.get("providers")
        if isinstance(providers, Mapping):
            for raw in providers.values():
                if isinstance(raw, dict):
                    raw.pop("latency_ms", None)
                    raw.pop("error_classes", None)
        return payload

    async def flush(
        self,
        state: Mapping[str, Any],
        *,
        force: bool = False,
        recent_cap: int = 200,
    ) -> bool:
        """异步落盘；带写锁，保证同一时刻只有一个写盘任务（P2-10）。"""
        payload = self.build_payload(state, recent_cap=recent_cap)
        lock = self._current_write_lock()
        async with lock:
            try:
                await asyncio.to_thread(self.write_sync, payload)
            except OSError as exc:  # pragma: no cover - 磁盘异常
                self._error(f"写入状态文件失败：{exc}")
                return False
            self._writes += 1
            if force:
                self._last_flush_at = float(self._clock())
        return True

    def _current_write_lock(self) -> asyncio.Lock:
        """按事件循环复用写锁（``asyncio.Lock`` 绑定 loop，跨 loop 复用会报错）。"""
        try:
            loop: Any = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - 无 loop 时不应调用 flush
            loop = None
        if self._write_lock_loop is not loop:
            self._write_lock = asyncio.Lock()
            self._write_lock_loop = loop
        return self._write_lock

    def _next_tmp_path(self) -> Path:
        """唯一的临时文件名：并发写盘不会共用同一个 tmp（P2-10）。"""
        return self.data_dir / f"{STATE_FILE_NAME}.{os.getpid()}-{self._tmp_seq}.tmp"

    def write_sync(self, payload: Mapping[str, Any]) -> Path:
        """同步原子写入：唯一 tmp 文件 → ``os.replace``。"""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._tmp_seq += 1
        tmp_path = self._next_tmp_path()
        text = _dumps(payload)
        try:
            with open(tmp_path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self.path)
        except OSError:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:  # pragma: no cover - 清理失败不影响主流程
                pass
            raise
        return self.path

    # ---- 日志 ----

    def _info(self, message: str) -> None:
        if self._logger is not None:
            self._logger.info(f"{LOG_PREFIX} {message}")

    def _error(self, message: str) -> None:
        if self._logger is not None:
            self._logger.error(f"{LOG_PREFIX} {message}")


def _dumps(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)


_SENSITIVE_EVENT_KEYS = frozenset(
    {"prompt", "contexts", "messages", "system_prompt", "api_key", "key", "authorization"}
)


def _scrub_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """最近事件字段白名单（规划文档 §13.8：不记录提示词与用户内容）。"""
    return {
        key: value
        for key, value in event.items()
        if str(key) not in _SENSITIVE_EVENT_KEYS
    }


def _migrate_v0_to_v1(raw: Mapping[str, Any]) -> dict[str, Any]:
    """v0（无版本号的历史文件）→ v1：补 schema_version 与运行时计数。"""
    migrated = dict(raw)
    migrated["schema_version"] = 1
    migrated.setdefault("marks", {})
    migrated.setdefault("marks_by_profile", {})
    migrated.setdefault("providers", {})
    migrated.setdefault("recent_events", [])
    migrated.setdefault("probes", {})
    migrated.setdefault("runtime", {})
    return migrated


_MIGRATIONS: dict[int, Callable[[Mapping[str, Any]], dict[str, Any]]] = {
    0: _migrate_v0_to_v1,
}


def default_data_dir() -> Path | None:
    """生产路径：``StarTools.get_data_dir(插件名)``；不可用时返回 ``None``。"""
    try:  # 延迟导入，避免测试环境必须装 AstrBot
        from astrbot.api.star import StarTools  # type: ignore
    except Exception:
        return None
    getter = getattr(StarTools, "get_data_dir", None)
    if not callable(getter):
        return None
    try:
        result = getter(PLUGIN_NAME)
    except Exception:
        return None
    if result is None:
        return None
    return Path(result)

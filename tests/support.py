"""插件单测的共享夹具与假对象。

设计原则：

* **不依赖真实 AstrBot**：``FakeProvider`` / ``FakeAcm`` / ``FakeContext``
  只实现插件真正用到的那几个接口，签名与官方实现保持一致；
* **不依赖真实网络与 SDK**：错误用自造异常类 + ``status_code`` 属性模拟；
* **可控时间**：``FakeClock`` 让 TTL、测活间隔、退避都可以精确断言；
* **不真的 sleep**：重试退避通过注入的 ``sleep`` 记录器收集，测试瞬间完成。

说明：``devtools/astrbot_stub.py`` 的占位装饰器会把 ``filter.command_group(...)``
原样返回被装饰函数，而真实框架返回 ``RegisteringCommandable``（带 ``.command``）。
``main.py`` 用的是官方命令组写法，因此这里额外提供一个最小 fake，专门用于
「导入 ``main.py`` 检查装配」的用例。
"""

from __future__ import annotations

import asyncio
import sys
import types
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

from astrbot_plugin_llm_model_guard.core.guardian import Guardian
from astrbot_plugin_llm_model_guard.core.metrics import Metrics
from astrbot_plugin_llm_model_guard.core.models import GuardSettings
from astrbot_plugin_llm_model_guard.core.store import StateStore
from astrbot_plugin_llm_model_guard.core.wrapper import Guard


# ---------------------------------------------------------------------------
# 时间与 sleep
# ---------------------------------------------------------------------------


class FakeClock:
    """可推进的时钟，用来测 TTL / 测活间隔 / 退避。"""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


class FakeMonotonic:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


class SleepRecorder:
    """替代 ``asyncio.sleep``：既记录退避时长，也推进单调时钟。"""

    def __init__(self, monotonic: FakeMonotonic | None = None) -> None:
        self.delays: list[float] = []
        self._monotonic = monotonic

    async def __call__(self, seconds: float) -> None:
        self.delays.append(float(seconds))
        if self._monotonic is not None:
            self._monotonic.advance(seconds)
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# 响应与异常
# ---------------------------------------------------------------------------


class FakeResponse:
    """最小 ``LLMResponse`` 替身。"""

    def __init__(self, text: str = "ok", role: str = "assistant") -> None:
        self.completion_text = text
        self.role = role

    def __repr__(self) -> str:
        return f"FakeResponse(text={self.completion_text!r}, role={self.role!r})"


def err_response(text: str) -> FakeResponse:
    return FakeResponse(text=text, role="err")


class StatusError(Exception):
    """带 ``status_code`` 的异常，模拟各 SDK 的 APIError。"""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        if status_code is not None:
            self.status_code = status_code


class ResponseLike:
    def __init__(self, status_code: int, headers: Mapping[str, str] | None = None) -> None:
        self.status_code = status_code
        self.headers = dict(headers or {})


# ---------------------------------------------------------------------------
# Provider / ProviderManager
# ---------------------------------------------------------------------------


class FakeProvider:
    """可编排行为的假 provider。

    * ``results``：依次消费的结果，元素可以是异常实例、``FakeResponse`` 或可调用对象；
      消费完最后一个后一直复用最后一个；
    * ``stream_results``：每个元素是"一次流式调用要 yield 的块序列"，
      序列元素也可以是异常实例（表示在该位置抛错）；
    * ``hang``：``text_chat`` 永不返回，用来测测活超时。
    """

    def __init__(
        self,
        provider_id: str,
        *,
        model: str | None = None,
        results: Sequence[Any] | None = None,
        stream_results: Sequence[Sequence[Any]] | None = None,
        hang: bool = False,
    ) -> None:
        self.provider_config = {
            "id": provider_id,
            "type": "fake",
            "model": model or f"{provider_id}-model",
        }
        self._results: list[Any] = list(results or [])
        self._stream_results: list[Sequence[Any]] = list(stream_results or [])
        self.hang = hang
        self.call_count = 0
        self.stream_call_count = 0
        self.calls: list[dict[str, Any]] = []
        self.stream_calls: list[dict[str, Any]] = []

    # ---- 元信息 ----

    def meta(self) -> Any:
        return types.SimpleNamespace(id=self.provider_id, model=self.get_model())

    @property
    def provider_id(self) -> str:
        return str(self.provider_config["id"])

    def get_model(self) -> str:
        return str(self.provider_config.get("model") or "")

    # ---- 调用 ----

    async def text_chat(self, **kwargs: Any) -> Any:
        self.call_count += 1
        self.calls.append(dict(kwargs))
        if self.hang:
            await asyncio.sleep(3600)
        item = self._next_result()
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return item(**kwargs)
        if item is None:
            return FakeResponse(f"{self.provider_id}-ok")
        return item

    def text_chat_stream(self, **kwargs: Any) -> Any:
        self.stream_calls.append(dict(kwargs))
        return self._stream(kwargs)

    async def _stream(self, kwargs: Mapping[str, Any]) -> Any:
        self.stream_call_count += 1
        chunks = self._next_stream()
        for chunk in chunks:
            if isinstance(chunk, BaseException):
                raise chunk
            yield chunk

    # ---- 行为编排 ----

    def _next_result(self) -> Any:
        if not self._results:
            return None
        if len(self._results) > 1:
            return self._results.pop(0)
        return self._results[0]

    def _next_stream(self) -> Sequence[Any]:
        if not self._stream_results:
            return [FakeResponse(f"{self.provider_id}-chunk")]
        if len(self._stream_results) > 1:
            return self._stream_results.pop(0)
        return self._stream_results[0]


class FakeProviderManager:
    def __init__(self, providers: Iterable[FakeProvider] = ()) -> None:
        self.inst_map: dict[str, FakeProvider] = {p.provider_id: p for p in providers}
        self.provider_insts: list[FakeProvider] = list(self.inst_map.values())

    def add(self, provider: FakeProvider) -> FakeProvider:
        self.inst_map[provider.provider_id] = provider
        if provider not in self.provider_insts:
            self.provider_insts.append(provider)
        return provider


# ---------------------------------------------------------------------------
# 配置管理器与 context
# ---------------------------------------------------------------------------


class FakeAcm:
    """最小 ``AstrBotConfigManager`` 替身。"""

    def __init__(
        self,
        *,
        profiles: Mapping[str, str] | None = None,
        fallbacks: Mapping[str, Sequence[str]] | None = None,
        names: Mapping[str, str] | None = None,
    ) -> None:
        self.profiles = dict(profiles or {})
        self.fallbacks = {key: list(value) for key, value in (fallbacks or {}).items()}
        self.names = dict(names or {})

    def get_conf_info(self, umo: str) -> dict[str, Any] | None:
        profile_id = self.profiles.get(umo)
        if profile_id is None:
            return None
        return {"id": profile_id, "name": self.names.get(profile_id, profile_id), "path": ""}

    def get_conf(self, umo: str) -> dict[str, Any]:
        return {
            "provider_settings": {
                "fallback_chat_models": list(self.fallbacks.get(umo, [])),
            }
        }

    def get_conf_list(self) -> list[dict[str, Any]]:
        seen: list[str] = []
        for profile_id in self.profiles.values():
            if profile_id not in seen:
                seen.append(profile_id)
        return [
            {"id": profile_id, "name": self.names.get(profile_id, profile_id), "path": ""}
            for profile_id in seen
        ]


class FakeContext:
    """最小 ``Context`` 替身：只提供插件用到的成员。"""

    def __init__(
        self,
        providers: Iterable[FakeProvider] = (),
        *,
        acm: Any = None,
        current_provider: FakeProvider | None = None,
    ) -> None:
        self.provider_manager = FakeProviderManager(providers)
        self.astrbot_config_mgr = acm if acm is not None else FakeAcm()
        self._current_provider = current_provider
        self.registered_web_apis: list[tuple[Any, ...]] = []
        self.config: dict[str, Any] = {}

    def get_using_provider(self, umo: str | None = None) -> Any:
        return self._current_provider

    def set_using_provider(self, provider: FakeProvider | None) -> None:
        self._current_provider = provider

    def get_config(self, umo: str | None = None) -> Any:
        return self.config

    def register_web_api(
        self, route: str, handler: Any, methods: Sequence[str], desc: str
    ) -> None:
        self.registered_web_apis.append((route, handler, list(methods), desc))


class FakeEvent:
    """最小 ``AstrMessageEvent`` 替身。"""

    def __init__(
        self,
        umo: str = "aiocqhttp:FriendMessage:10001",
        *,
        extras: Mapping[str, Any] | None = None,
        admin: bool = True,
    ) -> None:
        self.unified_msg_origin = umo
        self._extras = dict(extras or {})
        self._admin = admin
        self.results: list[str] = []

    def get_extra(self, key: str, default: Any = None) -> Any:
        return self._extras.get(key, default)

    def set_extra(self, key: str, value: Any) -> None:
        self._extras[key] = value

    def is_admin(self) -> bool:
        return self._admin

    def plain_result(self, text: str) -> Any:
        self.results.append(text)
        return types.SimpleNamespace(text=text)

    @property
    def selected_provider(self) -> Any:
        return self._extras.get("selected_provider")


# ---------------------------------------------------------------------------
# 运行时装配
# ---------------------------------------------------------------------------


@dataclass
class Runtime:
    """一套装配好的 core 运行时，测试里直接用。"""

    guard: Guard
    guardian: Guardian
    metrics: Metrics
    store: StateStore
    context: FakeContext
    clock: FakeClock
    monotonic: FakeMonotonic
    sleep: SleepRecorder
    config: dict[str, Any] = field(default_factory=dict)

    def provider(self, provider_id: str) -> Any:
        return self.context.provider_manager.inst_map.get(provider_id)


def build_runtime(
    tmp_path: Any,
    *,
    config: Mapping[str, Any] | None = None,
    providers: Iterable[FakeProvider] = (),
    profiles: Mapping[str, str] | None = None,
    fallbacks: Mapping[str, Sequence[str]] | None = None,
    current_provider: FakeProvider | None = None,
    use_real_sleep: bool = False,
) -> Runtime:
    """构造 Guardian + Guard + FakeContext；时间与 sleep 全部可控。

    ``use_real_sleep=True`` 时使用 Guard 自带的分段 ``asyncio.sleep``（用于验证
    ``abort_signal`` 能真的打断退避，见 P2-2 的用例）。
    """
    reset_call_context()
    config_dict: dict[str, Any] = dict(config or {})
    clock = FakeClock()
    monotonic = FakeMonotonic()
    sleep = SleepRecorder(monotonic)
    sleep_fn = None if use_real_sleep else sleep
    provider_list = list(providers)
    context = FakeContext(
        provider_list,
        acm=FakeAcm(profiles=profiles, fallbacks=fallbacks),
        current_provider=current_provider,
    )
    metrics = Metrics(window_s=900, recent_cap=200, clock=clock)
    store = StateStore(tmp_path, clock=clock, plugin_version="0.1.0")

    holder: dict[str, Any] = {}

    def _classify(exc: Any, **kwargs: Any) -> Any:
        return holder["guard"].classifier.classify(exc, **kwargs)

    guardian = Guardian(
        config_reader=lambda: config_dict,
        store=store,
        metrics=metrics,
        provider_getter=lambda pid: context.provider_manager.inst_map.get(pid),
        provider_ids_getter=lambda: list(context.provider_manager.inst_map.keys()),
        classify=_classify,
        clock=clock,
    )
    guard = Guard(
        config_reader=lambda: config_dict,
        guardian=guardian,
        metrics=metrics,
        context=context,
        store=store,
        sleep=sleep_fn,
        clock=clock,
        monotonic=monotonic,
    )
    holder["guard"] = guard
    return Runtime(
        guard=guard,
        guardian=guardian,
        metrics=metrics,
        store=store,
        context=context,
        clock=clock,
        monotonic=monotonic,
        sleep=sleep,
        config=config_dict,
    )


def settings_of(config: Mapping[str, Any]) -> GuardSettings:
    return GuardSettings.from_mapping(config)


def reset_call_context() -> None:
    """清空候选链 contextvar，避免用例之间互相污染。"""
    from astrbot_plugin_llm_model_guard.core.wrapper import CALL_CONTEXT

    CALL_CONTEXT.set(None)


# ---------------------------------------------------------------------------
# 导入 main.py 用的最小 AstrBot 假模块
# ---------------------------------------------------------------------------


class _Group:
    """等价于框架的 ``RegisteringCommandable``：可继续注册子命令。"""

    def __init__(self) -> None:
        self.commands: list[tuple[str, Callable[..., Any]]] = []

    def command(self, name: str, **_: Any) -> Callable[[Callable[..., Any]], Any]:
        def decorator(func: Callable[..., Any]) -> Any:
            self.commands.append((name, func))
            return func

        return decorator


class _FilterNamespace:
    """装饰器命名空间：所有 hook 装饰器都原样返回被装饰对象。"""

    PermissionType = types.SimpleNamespace(ADMIN="admin")

    def __init__(self) -> None:
        self.groups: dict[str, _Group] = {}
        self.hooks: dict[str, Callable[..., Any]] = {}

    def _identity(self, *args: Any, **kwargs: Any) -> Callable[[Any], Any]:
        def decorator(func: Any) -> Any:
            self.hooks[getattr(func, "__name__", "hook")] = func
            return func

        return decorator

    def command_group(self, name: str, **_: Any) -> Callable[[Any], _Group]:
        group = _Group()

        def decorator(func: Any) -> _Group:
            self.groups[name] = group
            return group

        return decorator

    def command(self, *args: Any, **kwargs: Any) -> Callable[[Any], Any]:
        return self._identity()

    def permission_type(self, *args: Any, **kwargs: Any) -> Callable[[Any], Any]:
        return self._identity()

    def on_waiting_llm_request(self, *args: Any, **kwargs: Any) -> Callable[[Any], Any]:
        return self._identity()

    def on_llm_request(self, *args: Any, **kwargs: Any) -> Callable[[Any], Any]:
        return self._identity()

    def on_astrbot_loaded(self, *args: Any, **kwargs: Any) -> Callable[[Any], Any]:
        return self._identity()


def install_fake_astrbot(data_dir: Any) -> dict[str, Any]:
    """安装最小 AstrBot 假模块到 ``sys.modules``，返回可检查的句柄。

    调用方负责在结束时用 :func:`uninstall_fake_astrbot` 恢复。
    """
    saved: dict[str, Any] = {}
    names = [
        "astrbot",
        "astrbot.api",
        "astrbot.api.event",
        "astrbot.api.star",
        "astrbot.core",
        "astrbot.core.star",
        "astrbot.core.star.filter",
        "astrbot.core.star.filter.command",
    ]
    for name in names:
        saved[name] = sys.modules.get(name)
        module = types.ModuleType(name)
        # 让 ``astrbot`` / ``astrbot.api`` 等看起来像包，避免导入子模块时报
        # “not a package”（子模块本身已经预置在 sys.modules 里）。
        module.__path__ = []  # type: ignore[attr-defined]
        sys.modules[name] = module

    filter_ns = _FilterNamespace()
    event_module = sys.modules["astrbot.api.event"]
    event_module.filter = filter_ns  # type: ignore[attr-defined]
    event_module.AstrMessageEvent = FakeEvent  # type: ignore[attr-defined]

    logger_module = types.ModuleType("astrbot.api.logger")
    logger_module.info = lambda *a, **k: None  # type: ignore[attr-defined]
    logger_module.warning = lambda *a, **k: None  # type: ignore[attr-defined]
    logger_module.error = lambda *a, **k: None  # type: ignore[attr-defined]
    logger_module.debug = lambda *a, **k: None  # type: ignore[attr-defined]

    class _Star:
        def __init__(self, context: Any, config: Any = None) -> None:
            self.context = context
            self.config = config

        async def terminate(self) -> None:  # pragma: no cover - 生命周期占位
            return None

    class _StarTools:
        @staticmethod
        def get_data_dir(plugin_name: str) -> Any:
            path = data_dir / plugin_name
            path.mkdir(parents=True, exist_ok=True)
            return path

    def _register(*args: Any, **kwargs: Any) -> Callable[[Any], Any]:
        def decorator(cls: Any) -> Any:
            return cls

        return decorator

    star_module = sys.modules["astrbot.api.star"]
    star_module.Star = _Star  # type: ignore[attr-defined]
    star_module.Context = object  # type: ignore[attr-defined]
    star_module.StarTools = _StarTools  # type: ignore[attr-defined]
    star_module.register = _register  # type: ignore[attr-defined]

    class GreedyStr(str):
        pass

    sys.modules["astrbot.core.star.filter.command"].GreedyStr = GreedyStr  # type: ignore[attr-defined]

    api_module = sys.modules["astrbot.api"]
    api_module.logger = logger_module  # type: ignore[attr-defined]
    sys.modules["astrbot"].logger = logger_module  # type: ignore[attr-defined]

    return {"filter": filter_ns, "saved": saved, "GreedyStr": GreedyStr}


def uninstall_fake_astrbot(handle: Mapping[str, Any]) -> None:
    for name, module in (handle.get("saved") or {}).items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module

"""provider 实例的包装挂载与卸载。

为什么是「实例级」而不是「类级 monkeypatch」（规划文档 §6 D1）：

* provider 实例会在配置保存/重载时被重建，类级补丁无法按作用域撤销；
* 类级补丁会影响 TTS/STT 等无关 provider，也无法与其他类级补丁共存。

本模块只做「挂/摘」与身份校验，不含重试逻辑；真正的守护流程在
:mod:`core.wrapper`。为避免循环导入，闭包里对 wrapper 做延迟导入。
"""

from __future__ import annotations

import weakref
from typing import Any, Callable, Iterable

#: 包装标记（存在即为已包装）。
WRAP_FLAG = "__llm_model_guard_wrapped__"

#: 原始方法持有处（dict：方法名 → 可调用对象）。
STATE_ATTR = "__llm_model_guard_state__"

#: 包装方法的身份校验标记。
WRAPPER_ATTR = "__llm_model_guard_wrapper__"

#: 递归深度上限；超过就原样透传，避免和其他插件的包装叠成无限递归。
MAX_WRAP_DEPTH = 3


class WrapRegistry:
    """记录"我包过哪些实例"，用于 ``terminate`` 时精确回收。

    用弱引用保存，避免插件持有 provider 实例导致无法释放。
    """

    def __init__(self) -> None:
        self._refs: dict[int, weakref.ref[Any]] = {}

    def register(self, provider: Any) -> None:
        key = id(provider)
        if key in self._refs:
            return

        def _cleanup(_ref: weakref.ref[Any], key: int = key) -> None:
            self._refs.pop(key, None)

        try:
            self._refs[key] = weakref.ref(provider, _cleanup)
        except TypeError:  # pragma: no cover - 不支持弱引用的对象
            return

    def discard(self, provider: Any) -> None:
        self._refs.pop(id(provider), None)

    def instances(self) -> list[Any]:
        alive: list[Any] = []
        for key in list(self._refs):
            reference = self._refs.get(key)
            provider = reference() if reference is not None else None
            if provider is None:
                self._refs.pop(key, None)
                continue
            alive.append(provider)
        return alive

    def __len__(self) -> int:
        return len(self.instances())


def registry_of(guard: Any) -> WrapRegistry:
    """取（或惰性创建）挂在 guard 上的包装登记表。"""
    registry = getattr(guard, "wrap_registry", None)
    if isinstance(registry, WrapRegistry):
        return registry
    registry = WrapRegistry()
    try:
        guard.wrap_registry = registry
    except AttributeError:  # pragma: no cover - 只读对象
        pass
    return registry


def is_wrapped(provider: Any) -> bool:
    return bool(getattr(provider, WRAP_FLAG, False))


def get_original(provider: Any, name: str = "text_chat") -> Callable[..., Any] | None:
    """取包装前的原始可调用对象；未包装时返回实例上当前的方法。"""
    state = getattr(provider, STATE_ATTR, None)
    if isinstance(state, dict):
        original = state.get(name)
        if callable(original):
            return original
        return None
    candidate = getattr(provider, name, None)
    return candidate if callable(candidate) else None


def ensure_wrapped(providers: Iterable[Any], guard: Any) -> int:
    """幂等包装：返回本次新包装的实例数量。"""
    registry = registry_of(guard)
    wrapped = 0
    for provider in providers:
        if provider is None:
            continue
        if is_wrapped(provider):
            registry.register(provider)
            continue
        if not _wrap_one(provider, guard):
            continue
        registry.register(provider)
        wrapped += 1
    return wrapped


def _wrap_one(provider: Any, guard: Any) -> bool:
    """给单个实例挂上守护方法；缺少必要方法时返回 ``False``。"""
    chat = getattr(provider, "text_chat", None)
    if not callable(chat):
        return False
    stream = getattr(provider, "text_chat_stream", None)

    instance_dict = getattr(provider, "__dict__", {})
    chat_wrapper = _make_chat_wrapper(provider, guard)
    state: dict[str, Any] = {
        "text_chat": chat,
        "text_chat_stream": stream if callable(stream) else None,
        "text_chat_wrapper": chat_wrapper,
        "text_chat_stream_wrapper": None,
        # 进来时这两个方法是否已经是实例属性（说明别的插件先包过一层，P3-7）。
        "text_chat_was_instance_attr": "text_chat" in instance_dict,
        "text_chat_stream_was_instance_attr": "text_chat_stream" in instance_dict,
    }

    try:
        setattr(provider, STATE_ATTR, state)
        setattr(provider, "text_chat", chat_wrapper)
        if callable(stream):
            stream_wrapper = _make_stream_wrapper(provider, guard)
            state["text_chat_stream_wrapper"] = stream_wrapper
            setattr(provider, "text_chat_stream", stream_wrapper)
        setattr(provider, WRAP_FLAG, True)
    except (AttributeError, TypeError):
        # provider 用 __slots__ 或只读属性时无法包装：不算错误，只是不接管。
        _cleanup_failed_wrap(provider)
        return False
    return True


def _cleanup_failed_wrap(provider: Any) -> None:
    for attr in (WRAP_FLAG, STATE_ATTR):
        try:
            delattr(provider, attr)
        except (AttributeError, TypeError):
            pass


def _make_chat_wrapper(provider: Any, guard: Any) -> Callable[..., Any]:
    async def _guarded_text_chat(*args: Any, **kwargs: Any) -> Any:
        from .wrapper import guarded_text_chat

        return await guarded_text_chat(guard, provider, *args, **kwargs)

    setattr(_guarded_text_chat, WRAPPER_ATTR, True)
    return _guarded_text_chat


def _make_stream_wrapper(provider: Any, guard: Any) -> Callable[..., Any]:
    def _guarded_text_chat_stream(*args: Any, **kwargs: Any) -> Any:
        from .wrapper import guarded_text_chat_stream

        return guarded_text_chat_stream(guard, provider, *args, **kwargs)

    setattr(_guarded_text_chat_stream, WRAPPER_ATTR, True)
    return _guarded_text_chat_stream


def unwrap(provider: Any, guard: Any) -> bool:
    """还原单个实例。

    两种情况：
    1. 栈顶仍是我们的包装 → 还原成**进入前的那个可调用对象**：如果进来时它已经是
       实例属性（别的插件先包过），就 ``setattr`` 回它，而不是 ``delattr`` 回落到
       类实现，避免连带丢掉对方的包装（P3-7）；
    2. 栈顶已被人改包 → 不动手，只记录（规划文档 §12.3-4）。
    """
    state = getattr(provider, STATE_ATTR, None)
    if not isinstance(state, dict):
        return False

    restored = False
    for name, wrapper_key, flag_key in (
        ("text_chat", "text_chat_wrapper", "text_chat_was_instance_attr"),
        ("text_chat_stream", "text_chat_stream_wrapper", "text_chat_stream_was_instance_attr"),
    ):
        wrapper = state.get(wrapper_key)
        if wrapper is None:
            continue
        current = getattr(provider, "__dict__", {}).get(name)
        if current is not wrapper:
            # 包装链已被其他插件改写：不动手，只记录。
            _log(guard, f"{name} 的包装链已变更，跳过还原（provider={_provider_id(provider)}）")
            continue
        previous = state.get(name)
        try:
            if bool(state.get(flag_key)) and callable(previous):
                setattr(provider, name, previous)
            else:
                delattr(provider, name)
            restored = True
        except (AttributeError, TypeError):  # pragma: no cover
            continue

    for attr in (WRAP_FLAG, STATE_ATTR):
        try:
            delattr(provider, attr)
        except (AttributeError, TypeError):
            pass
    registry_of(guard).discard(provider)
    return restored


def unwrap_all(guard: Any) -> int:
    """还原全部已包装实例，返回还原数量。"""
    registry = registry_of(guard)
    count = 0
    for provider in registry.instances():
        if unwrap(provider, guard):
            count += 1
    return count


def wrapped_count(guard: Any) -> int:
    registry = registry_of(guard)
    return sum(1 for provider in registry.instances() if is_wrapped(provider))


def iter_provider_instances(provider_manager: Any) -> list[Any]:
    """从 provider_manager 取全部实例（兼容 ``inst_map`` / ``provider_insts``）。"""
    if provider_manager is None:
        return []
    result: list[Any] = []
    inst_map = getattr(provider_manager, "inst_map", None)
    if isinstance(inst_map, dict):
        result.extend(value for value in inst_map.values() if value is not None)
    insts = getattr(provider_manager, "provider_insts", None)
    if isinstance(insts, (list, tuple, set)):
        result.extend(item for item in insts if item is not None)
    seen: set[int] = set()
    unique: list[Any] = []
    for provider in result:
        if id(provider) in seen:
            continue
        seen.add(id(provider))
        unique.append(provider)
    return unique


def _provider_id(provider: Any) -> str:
    config = getattr(provider, "provider_config", None)
    if isinstance(config, dict):
        pid = config.get("id")
        if isinstance(pid, str):
            return pid
    return type(provider).__name__


def _log(guard: Any, message: str) -> None:
    logger = getattr(guard, "logger", None)
    if logger is not None:
        logger.warning(f"[llm_model_guard] {message}")

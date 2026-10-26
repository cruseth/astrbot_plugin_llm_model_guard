"""``astrbot_plugin_llm_model_guard`` 插件入口（v0.2.0）。

接入点：

1. ``on_waiting_llm_request``：按框架的模型顺序算候选链 → ``set_extra("selected_provider")``；
2. ``on_llm_request``：只补挂包装，不修改 ``req``；
3. provider 实例的 ``text_chat`` / ``text_chat_stream``（由 ``core.registry`` 挂载）；
4. 后台验证 supervisor（``core.guardian``）；
5. ``on_astrbot_loaded``：首次启动 supervisor（幂等）；
6. 管理面：``/llmguard`` 命令组 + ``/astrbot_plugin_llm_model_guard/*`` Web API。

v0.2.0 的核心变化：**配置里没有任何 id**。模型顺序直接读框架
（会话级选择 → abconf 默认模型 → abconf 的 ``fallback_chat_models``），
每个配置文件用自己的顺序，见 :mod:`core.chain`。

纪律：本文件之外不得访问 ``context``；插件类**禁止**定义 ``__del__``，
否则框架不会调用 ``terminate``（官方 ``star_manager._terminate_plugin`` 的行为）。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Mapping

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

from .core import commands as command_text
from .core import web_api
from .core.guardian import Guardian
from .core.metrics import Metrics
from .core.models import LOG_PREFIX, PLUGIN_NAME, GuardSettings
from .core.store import StateStore, default_data_dir
from .core.wrapper import Guard

try:  # 框架用 GreedyStr 标注"吃掉剩余参数"的最后一个形参
    from astrbot.core.star.filter.command import GreedyStr  # type: ignore
except Exception:  # pragma: no cover - 兼容旧版本框架
    GreedyStr = str  # type: ignore[assignment,misc]

PLUGIN_VERSION = "0.2.0"

__all__ = ["LLMModelGuardPlugin"]


@register(
    PLUGIN_NAME,
    "AstrBot Agent",
    "LLM 模型守护：按框架的模型顺序自动超时、重试、回退、标记与验证恢复",
    PLUGIN_VERSION,
)
class LLMModelGuardPlugin(Star):
    """插件主体：装配 core 各模块并注册 hook / 命令 / Web API。"""

    def __init__(self, context: Context, config: Mapping[str, Any] | None = None) -> None:
        super().__init__(context, config)
        # 保留框架注入的配置对象本身：WebUI 保存配置后内存值会变化，
        # 复制成普通 dict 会丢掉热更新。
        self.config = config if config is not None else {}
        self._bootstrapped = False
        self._web_api_errors: list[str] = []
        self._reload_count = 0
        self._admin_api_warned = False
        self._disabled_warned = False

        settings = GuardSettings.from_mapping(self.config)
        self.metrics = Metrics(
            window_s=900,
            recent_cap=200,
        )
        self.store = StateStore(
            self._resolve_data_dir(),
            logger=logger,
            plugin_version=PLUGIN_VERSION,
        )
        self.guardian = Guardian(
            config_reader=self._read_config,
            store=self.store,
            metrics=self.metrics,
            provider_getter=self._get_provider,
            provider_ids_getter=lambda: self.guard.provider_ids(),
            classify=self._classify_error,
            logger=logger,
        )
        self.guard = Guard(
            config_reader=self._read_config,
            guardian=self.guardian,
            metrics=self.metrics,
            context=context,
            logger=logger,
            store=self.store,
        )

        self._load_state()
        self._register_web_api()
        logger.info(
            f"{LOG_PREFIX} 插件已加载：状态文件={self.store.path}，"
            f"重试={settings.max_retries} 次，Web API 失败={len(self._web_api_errors)}"
        )

    # ------------------------------------------------------------------
    # 装配辅助
    # ------------------------------------------------------------------

    def _resolve_data_dir(self) -> Path:
        """插件数据目录：``StarTools.get_data_dir(插件名)``，失败时退回临时目录。"""
        getter = getattr(StarTools, "get_data_dir", None)
        if callable(getter):
            try:
                path = getter(PLUGIN_NAME)
                if path:
                    return Path(path)
            except Exception as exc:  # pragma: no cover - 框架异常
                logger.warning(f"{LOG_PREFIX} 获取插件数据目录失败：{exc}")
        secondary = default_data_dir()
        if secondary is not None:
            return secondary
        fallback = Path(tempfile.gettempdir()) / PLUGIN_NAME
        logger.warning(f"{LOG_PREFIX} 退回临时数据目录：{fallback}")
        return fallback

    def _read_config(self) -> Mapping[str, Any]:
        """配置读取入口；热更新时 WebUI 保存的新值会在这里被读到。"""
        config = getattr(self, "config", None)
        if isinstance(config, Mapping):
            return config
        getter = getattr(self.context, "get_config", None)
        if callable(getter):
            try:
                loaded = getter()
                if isinstance(loaded, Mapping):
                    return loaded
            except Exception:  # pragma: no cover
                pass
        return {}

    def _get_provider(self, provider_id: str) -> Any | None:
        manager = getattr(self.context, "provider_manager", None)
        inst_map = getattr(manager, "inst_map", None)
        if isinstance(inst_map, Mapping):
            return inst_map.get(provider_id)
        return None

    def _classify_error(self, exc: Any) -> Any:
        return self.guard.classifier.classify(exc)

    def _load_state(self) -> None:
        try:
            state = self.store.load()
            self.guardian.load_state(state)
            if self.store.last_load_error:
                self.metrics.note_counter("corrupt_state")
                logger.warning(
                    f"{LOG_PREFIX} 状态文件异常已按空状态运行：{self.store.last_load_error}"
                )
        except Exception as exc:  # pragma: no cover - 状态损坏也不能阻止插件加载
            logger.error(f"{LOG_PREFIX} 读取状态失败，按空状态运行：{exc}")

    async def _bootstrap(self) -> None:
        """幂等启动：全量补挂包装 + 启动验证 supervisor。

        总开关关闭时**不置位** ``_bootstrapped``：之后用户把开关打开（不重启插件）
        仍会在下一个事件里补挂包装并启动 supervisor。
        """
        if self._bootstrapped:
            return
        settings = self.guard.settings
        if not settings.enable:
            self._warn_disabled_once()
            return
        try:
            wrapped = self.guard.ensure_wrapped_all()
            logger.info(f"{LOG_PREFIX} 启动补挂包装：新增 {wrapped} 个 provider 实例")
        except Exception as exc:  # pragma: no cover
            logger.error(f"{LOG_PREFIX} 启动补挂包装失败：{exc}")
        try:
            await self.guardian.ensure_started()
        except Exception as exc:  # pragma: no cover
            logger.error(f"{LOG_PREFIX} 启动验证 supervisor 失败：{exc}")
        self._bootstrapped = True

    def _warn_disabled_once(self) -> None:
        if self._disabled_warned:
            return
        self._disabled_warned = True
        logger.info(f"{LOG_PREFIX} 总开关关闭：不包装、不接管、不验证")

    async def initialize(self) -> None:
        """``Star`` 生命周期钩子：插件被激活时调用。"""
        await self._bootstrap()

    async def terminate(self) -> None:
        """插件禁用/重载时清理：停守护 → 停 supervisor → 拆包装 → 落盘。"""
        self.guard.mark_stopping()
        try:
            await self.guardian.shutdown()
        except Exception as exc:  # pragma: no cover
            logger.error(f"{LOG_PREFIX} 停止验证 supervisor 失败：{exc}")
        try:
            unwrapped = self.guard.unwrap_all()
            logger.info(f"{LOG_PREFIX} 已还原 {unwrapped} 个 provider 包装")
        except Exception as exc:  # pragma: no cover
            logger.error(f"{LOG_PREFIX} 还原 provider 包装失败：{exc}")
        try:
            # 总开关关闭时只写既有标记，不落盘统计。
            await self.guardian.flush(include_stats=self.guard.settings.enable)
        except Exception as exc:  # pragma: no cover
            logger.error(f"{LOG_PREFIX} 最终落盘失败：{exc}")
        self._bootstrapped = False

    # ------------------------------------------------------------------
    # 接入点 5：AstrBot 加载完成
    # ------------------------------------------------------------------

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self) -> None:
        await self._bootstrap()

    # ------------------------------------------------------------------
    # 接入点 1：on_waiting_llm_request（选 provider）
    # ------------------------------------------------------------------

    @filter.on_waiting_llm_request(priority=100)
    async def on_waiting_llm_request(self, event: AstrMessageEvent) -> None:
        try:
            await self._bootstrap()
            if not self.guard.settings.enable:
                return
            self.guard.apply_selection_to_event(event)
        except Exception as exc:  # pragma: no cover - hook 不能炸对话
            logger.error(f"{LOG_PREFIX} on_waiting_llm_request 异常：{exc}")

    # ------------------------------------------------------------------
    # 接入点 2：on_llm_request（只补挂包装）
    # ------------------------------------------------------------------

    @filter.on_llm_request(priority=100)
    async def on_llm_request(self, event: AstrMessageEvent, req: Any) -> None:
        try:
            if not self.guard.settings.enable:
                return
            # provider 可能刚被 ProviderManager.reload() 重建：廉价补挂一次。
            self.guard.ensure_wrapped_all()
        except Exception as exc:  # pragma: no cover
            logger.debug(f"{LOG_PREFIX} on_llm_request 补挂包装失败：{exc}")

    # ------------------------------------------------------------------
    # 接入点 6a：管理员命令 /llmguard
    # ------------------------------------------------------------------

    @filter.command_group("llmguard")
    def llmguard(self) -> None:
        """LLM 模型守护管理命令。"""

    @llmguard.command("status")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_status(self, event: AstrMessageEvent):
        """查看守护总览：开关、链路参数、标记数、成功率。"""
        if not self._is_admin(event):
            yield event.plain_result("仅管理员可使用该命令。")
            return
        yield event.plain_result(command_text.format_status(self.guard))

    @llmguard.command("chains")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_chains(self, event: AstrMessageEvent):
        """列出每个配置文件的模型顺序与被标记项。"""
        if not self._is_admin(event):
            yield event.plain_result("仅管理员可使用该命令。")
            return
        yield event.plain_result(command_text.format_chains(self.guard))

    @llmguard.command("marks")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_marks(self, event: AstrMessageEvent):
        """查看当前被标记的模型与验证进度。"""
        if not self._is_admin(event):
            yield event.plain_result("仅管理员可使用该命令。")
            return
        yield event.plain_result(command_text.format_marks(self.guard))

    @llmguard.command("test")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_test(self, event: AstrMessageEvent, provider_id: str = ""):
        """立刻验证一次：/llmguard test <provider_id>"""
        if not self._is_admin(event):
            yield event.plain_result("仅管理员可使用该命令。")
            return
        ok, message = command_text.validate_provider_id(self.guard, provider_id)
        if not ok:
            yield event.plain_result(message)
            return
        result = await self.guardian.probe_now(provider_id.strip())
        yield event.plain_result(command_text.format_probe(result))

    @llmguard.command("unmark")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_unmark(self, event: AstrMessageEvent, provider_id: str = ""):
        """解除标记：/llmguard unmark <provider_id>"""
        if not self._is_admin(event):
            yield event.plain_result("仅管理员可使用该命令。")
            return
        ok, message = command_text.validate_provider_id(self.guard, provider_id)
        if not ok:
            yield event.plain_result(message)
            return
        removed = await self.guardian.unmark(provider_id.strip(), by="command")
        if removed:
            yield event.plain_result(f"已解除 {provider_id.strip()} 的标记。")
        else:
            yield event.plain_result(f"{provider_id.strip()} 当前没有被标记。")

    @llmguard.command("reload")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_reload(self, event: AstrMessageEvent):
        """重载插件配置与链路（不重启 AstrBot）。"""
        if not self._is_admin(event):
            yield event.plain_result("仅管理员可使用该命令。")
            return
        summary = await self._reload_runtime()
        yield event.plain_result(summary)

    # ------------------------------------------------------------------
    # 接入点 6b：Web API
    # ------------------------------------------------------------------

    def _register_web_api(self) -> None:
        register_api = getattr(self.context, "register_web_api", None)
        if not callable(register_api):
            self._web_api_errors.append("当前框架不支持 register_web_api，只有命令可用")
            logger.warning(f"{LOG_PREFIX} 当前框架不支持 register_web_api，只有命令可用")
            return
        for route, handler, methods, desc in self._web_api_handlers():
            try:
                register_api(route, handler, methods, desc)
            except Exception as exc:  # pragma: no cover - 旧版本框架降级
                self._web_api_errors.append(f"{route}: {exc}")
                logger.warning(f"{LOG_PREFIX} 注册 Web API {route} 失败：{exc}")

    def _web_api_handlers(self) -> list[tuple[str, Any, list[str], str]]:
        return [
            (web_api.ROUTE_OVERVIEW, self._api_overview, ["GET"], "LLM 模型守护：总览"),
            (
                web_api.ROUTE_CHAINS,
                self._api_chains,
                ["GET"],
                "LLM 模型守护：各配置文件的模型链路",
            ),
            (web_api.ROUTE_EVENTS, self._api_events, ["GET"], "LLM 模型守护：最近调用记录"),
            (web_api.ROUTE_MARK, self._api_mark, ["POST"], "LLM 模型守护：手动标记"),
            (web_api.ROUTE_UNMARK, self._api_unmark, ["POST"], "LLM 模型守护：解除标记"),
            (web_api.ROUTE_PROBE, self._api_probe, ["POST"], "LLM 模型守护：立刻验证"),
            (web_api.ROUTE_RELOAD, self._api_reload, ["POST"], "LLM 模型守护：重载配置"),
        ]

    async def _api_overview(self, **_: Any) -> Any:
        return self._respond(self._safe_build(web_api.build_overview))

    async def _api_chains(self, **_: Any) -> Any:
        profile_id = self._query_arg("profile_id")
        return self._respond(
            self._safe_build(lambda guard: web_api.build_chains(guard, profile_id=profile_id))
        )

    async def _api_events(self, **_: Any) -> Any:
        limit = web_api.parse_limit(self._query_arg("limit"), default=50)
        provider_id = self._query_arg("provider_id")
        profile_id = self._query_arg("profile_id")
        return self._respond(
            self._safe_build(
                lambda guard: web_api.build_events(
                    guard,
                    limit=limit,
                    provider_id=provider_id,
                    profile_id=profile_id,
                )
            )
        )

    async def _api_mark(self, **_: Any) -> Any:
        return await self._api_action("mark")

    async def _api_unmark(self, **_: Any) -> Any:
        return await self._api_action("unmark")

    async def _api_probe(self, **_: Any) -> Any:
        return await self._api_action("probe")

    async def _api_reload(self, **_: Any) -> Any:
        return await self._api_action("reload")

    async def _api_action(self, action: str) -> Any:
        try:
            body = await self._read_json_body()
            payload = await web_api.perform_action(
                self.guard,
                action,
                body,
                reload_callback=self._reload_runtime,
            )
        except Exception as exc:  # pragma: no cover - 任何异常都返回信封而不是 500
            payload = web_api.envelope_error(f"操作失败：{exc}")
        return self._respond(payload)

    def _safe_build(self, builder: Any) -> dict[str, Any]:
        """视图构建异常也要返回信封，不能抛 500 堆栈。"""
        try:
            return web_api.envelope_ok(builder(self.guard))
        except Exception as exc:  # pragma: no cover - 防御性
            logger.error(f"{LOG_PREFIX} 构建 Web API 响应失败：{exc}")
            return web_api.envelope_error(f"构建响应失败：{exc}")

    def _respond(self, payload: Mapping[str, Any]) -> Any:
        """能导入 ``quart.jsonify`` 就返回 Response，否则直接返回 dict。"""
        try:
            from quart import jsonify  # type: ignore

            return jsonify(dict(payload))
        except Exception:  # pragma: no cover - 非 quart 环境（单测）
            return dict(payload)

    def _query_arg(self, name: str) -> str | None:
        try:
            from quart import request  # type: ignore

            value = request.args.get(name)
        except Exception:  # pragma: no cover - 单测环境
            return None
        return value or None

    async def _read_json_body(self) -> dict[str, Any]:
        try:
            from quart import request  # type: ignore
        except Exception:  # pragma: no cover - 单测环境
            return {}
        try:
            body = await request.get_json(silent=True)
        except Exception:
            return {}
        return dict(body) if isinstance(body, Mapping) else {}

    # ------------------------------------------------------------------
    # 公共小工具
    # ------------------------------------------------------------------

    def _is_admin(self, event: Any) -> bool:
        """管理员判定；事件对象缺 ``is_admin`` 时 **fail-closed**。"""
        checker = getattr(event, "is_admin", None)
        if not callable(checker):
            self._warn_admin_api_missing()
            return False
        try:
            return bool(checker())
        except Exception:  # pragma: no cover
            return False

    def _warn_admin_api_missing(self) -> None:
        """只在第一次缺失 ``is_admin`` 时告警，避免刷屏。"""
        if self._admin_api_warned:
            return
        self._admin_api_warned = True
        logger.warning(
            f"{LOG_PREFIX} 当前事件对象没有 is_admin()，管理命令按“非管理员”处理；"
            "命令级权限过滤器仍会兜底"
        )

    async def _reload_runtime(self) -> str:
        """重载配置派生状态：链路、包装、supervisor。"""
        self._reload_count += 1
        settings = self.guard.settings
        # 触发指纹失效：下次访问会重建配置快照。
        self.guard.invalidate_caches()
        self.metrics.window_s = 900
        self.metrics.recent_cap = 200
        wrapped = self.guard.ensure_wrapped_all() if settings.enable else 0
        if settings.enable:
            await self.guardian.ensure_started()
        logger.info(
            f"{LOG_PREFIX} 重载完成：新增包装 {wrapped} 个，"
            f"重试={settings.max_retries} 次，"
            f"可重试类别={','.join(settings.retry_categories)}"
        )
        return (
            f"已重载：新增包装 {wrapped} 个 provider，"
            f"重试 {settings.max_retries} 次，"
            f"可重试类别 {'、'.join(settings.retry_categories) or '（无）'}。"
            f"总开关={'开启' if settings.enable else '关闭'}"
        )


def _entrypoint_smoke() -> None:  # pragma: no cover - 仅用于手工自检
    """占位：真正的运行时验证需要 Python 3.12 的 AstrBot 环境。"""
    logger.info(f"{LOG_PREFIX} {PLUGIN_NAME} v{PLUGIN_VERSION}")

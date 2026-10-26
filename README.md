# astrbot_plugin_llm_model_guard

按 AstrBot 配置文件（abconf）接管 LLM 模型选择：**错误分类 → 同模型重试 → 降级回退链 → 临时标记 → 定期测活恢复**，同时提供每个模型的调用统计与只读监控页面。

面向 AstrBot **4.25+**（参考基线 4.25.3）。仅使用 Python 标准库与 AstrBot 内置能力，**无新增第三方依赖**。

---

## 1. 它解决什么问题

1. 主模型偶发限流/超时：自动退避重试同一模型，用户几乎无感。
2. 主模型 key 失效或模型下线：立刻切到回退链上的下一个模型，并把坏模型**临时标记**，后续请求直接绕过。
3. 坏模型什么时候恢复没人知道：插件对已标记模型**定期测活**，成功后按「双击确认」再测一次，两次都成功才解除标记。
4. 想知道每个模型最近表现：调用次数、成功率、错误分类、平均/分位延迟、最近错误摘要，命令与页面都能看。

---

## 2. 工作原理

```
消息事件
  └─ on_waiting_llm_request（插件 hook A）
       umo → 配置文件 ID → 路由 → 候选链（跳过被标记模型）
       event.set_extra("selected_provider", 链首)
       event.set_extra 之外还会给候选 provider 实例挂上守护包装
  └─ build_main_agent 选中链首 provider
  └─ provider.text_chat / text_chat_stream（插件包装层）
       ├─ 可重试错误 → 退避后重试同一模型（受重试次数/总预算/会话配额约束，
       │  退避等待会响应“停止生成”，见 abort_signal）
       ├─ 需要降级 → 标记当前模型 + 切下一个候选（非首候选不传 model；
       │  目标模型不支持本次请求的多模态能力时跳过该候选）
       ├─ 流式已产出内容 → 不重试、不降级、**不标记**，只记统计后原样上抛
       └─ 全部失败 → 上抛最后一个异常（附最后一次分类摘要），交给 AstrBot 自己的兜底
  └─ 后台 supervisor（常驻，每 probe_tick_seconds 扫一次）
       存在被标记模型时按 probe_interval 测活 → 成功进 CONFIRMING → 确认间隔后再测 → 成功解除
       关闭测活时仍保留 TTL 扫描，保证限时标记会到期
```

关键设计：

- **实例级包装，不做类级 monkeypatch**：provider 重载后包装会丢失，因此每个事件都会幂等补挂；
- **测活绕过包装**：直接调用类的 `text_chat`（或 provider 自己声明的 `test`）+ `asyncio.wait_for`，并用 contextvar 旁路标记，绝不会「测活 → 标记 → 再测活」递归；
- **零介入**：`enable=false` 时不包装、不写 extra、不测活、不落盘统计（`terminate` 只写既有标记）；
- **不改用户配置**：不会改写 `provider_settings` / `fallback_chat_models`，也不会调用 `save_config()`。

---

## 3. 安装

1. 把本目录放到 AstrBot 的 `data/plugins/astrbot_plugin_llm_model_guard/`；
2. 重启 AstrBot（或在 WebUI 插件页启用）；
3. 打开插件配置页填写「路由」：`profile_id` = `default` 或某个配置文件的 UUID，`primary` = 服务提供商 ID，`fallbacks` = 有序回退链。

> 服务提供商 ID 可以先用 `/llmguard models` 查看，避免手抄错。

---

## 4. 配置项（`_conf_schema.json`）

### 4.1 总开关

| 键 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `enable` | bool | `true` | 总开关；关闭后完全零介入，只保留只读监控 |
| `enable_routing` | bool | `true` | 是否接管模型选择 |
| `enable_retry` | bool | `true` | 同模型重试 |
| `enable_fallback` | bool | `true` | 降级到回退链 |
| `enable_marking` | bool | `true` | 临时标记坏模型 |
| `enable_probe` | bool | `true` | 定期测活 |
| `enable_monitoring` | bool | `true` | 调用统计 |
| `unmanaged_monitoring` | bool | `true` | 未纳管配置文件的调用是否也统计 |

### 4.2 路由

| 键 | 类型 | 说明 |
| --- | --- | --- |
| `routes` | template_list(`route`) | 每条含 `profile_id`、`primary`、`fallbacks`、`max_retries`(默认 -1 继承)、`probe_interval_seconds`(-1)、`probe_timeout_seconds`(-1)、`enable`、`note` |
| `disabled_profiles` | list | 不接管选择的配置文件 ID |
| `mark_scope` | string | `global`（默认，同一模型全局绕过）/ `profile`（仅该配置文件内绕过） |
| `respect_manual_selection` | bool | 尊重 WebChat 等入口手动指定的模型 |
| `respect_session_selection` | bool | 尊重会话级模型切换（与上一项在框架里同源，任一开启即尊重） |
| `union_framework_fallbacks` | bool | 把 AstrBot 自身 `fallback_chat_models` 并入候选链 |
| `allow_cross_provider_switch_mid_run` | bool | 允许工具循环中途跨模型降级（默认关闭） |

### 4.3 重试

`max_retries_per_model`(**1**)、`retry_backoff_base`(0.5)、`retry_backoff_factor`(2.0)、`retry_backoff_max`(8.0)、`retry_jitter_ratio`(0.2)、`retry_total_timeout`(30.0)、`retry_budget_per_umo_per_minute`(20)、`retry_empty_output`(**false**)、`mark_on_empty_output_after`(3)。

**与 provider 内部重试的叠加关系**：AstrBot 的 OpenAI 兼容 provider 自己就有一层最多 10 次的重试
（`openai_source.py` 的 `max_retries = 10`），空输出还会被框架的 tenacity 重试最多 3 次。
因此本插件的默认值是「每模型再补 1 次」，并且**默认不重试空输出**（交给框架那层），
避免同一模型被放大成 10 × 2 甚至 3 × 3 次请求。需要更激进的策略时再自行调大。

退避等待用自实现的**分段 `asyncio.sleep`**（不是 tenacity）：每 ≤0.2 秒检查一次插件是否正在停止、
以及框架的 `abort_signal`（用户点「停止生成」）是否已置位；置位立即抛 `CancelledError` 结束等待。

### 4.4 标记与测活

`mark_ttl_default`(0=永久)、`mark_ttl_rate_limit`(300)、`mark_ttl_server_error`(300)、`mark_ttl_network`(120)、`double_confirm`(true)、`confirm_delay_seconds`(60)、`probe_interval_seconds`(120)、`probe_timeout_seconds`(30.0)、`probe_max_concurrency`(2)、`probe_skip_when_busy`(true)、`probe_tick_seconds`(5)、`probe_on_startup`(true)。

- **流式已产出后中断不写标记**：一旦已经向用户吐出内容，任何错误都只记统计、原样上抛，避免把网络抖动误判成模型故障；
- **400/422 分两级**：消息命中「模型不存在 / 参数不支持 / not a VLM」这类细化特征才标记；其余 400/422 只降级不标记；
- **408/409/425** 按超时语义重试；`TypeError`/`ValueError` 这类本地编程错误直接上抛（不重试、不降级、不标记）；
- **TTL 与测活解耦**：即使 `enable_probe=false`，带 TTL 的标记也会到期自动解除。

### 4.5 统计与页面

`stats_window_seconds`(900)、`stats_recent_events`(200)、`stats_persist_interval_seconds`(30)、`stats_retention_days`(7，0=永久保留)、`allow_webui_actions`(false，页面写操作总开关)、`log_level`(info)。

`stats_retention_days`：仍然存在的服务提供商会一直保留统计；已经从配置里删掉的模型按此天数清理，避免状态文件无限增长。

### 4.6 自定义错误规则

`error_rules` 是 template_list(`rule`)，每条含：

`id`、`enabled`、`priority`（升序匹配；内置规则的优先级是 10–55，填更小的数字即可抢在内置规则之前）、`match_status`（HTTP 状态码列表）、`match_exc_regex`（对异常类名）、`match_msg_regex`（对错误消息，忽略大小写）、`action`（`retry_same`/`fallback`/`mark`/`mark_and_fallback`/`surface`/`ignore`）、`mark_ttl_seconds`、`note`。

非法正则或非法 `action` 只会被跳过并记 warning，不影响其他规则。

---

## 5. 命令（管理员）

| 命令 | 说明 |
| --- | --- |
| `/llmguard status` | 开关、包装数、标记数、近 5 分钟成功率、测活调度状态 |
| `/llmguard models` | 列出全部服务提供商 ID 与模型名（填配置用） |
| `/llmguard providers` | 每个模型的调用/成功/错误、最近错误分类、平均延迟 |
| `/llmguard marks` | 当前标记（状态、原因、剩余 TTL、下次测活时间） |
| `/llmguard route` | 当前会话所属配置文件的生效候选链与跳过项 |
| `/llmguard test <provider_id>` | 立刻测活一次（含双击确认说明） |
| `/llmguard mark <provider_id> [TTL 秒] [原因]` | 手动标记，默认永久 |
| `/llmguard unmark <provider_id>` | 手动解除标记 |
| `/llmguard reload` | 重载配置派生状态（路由表、规则、包装、supervisor） |

---

## 6. Web API

注册路由以插件名开头（前端桥接会拼成 `/api/plug/<插件名>/<endpoint>`）：

| 方法 | 路由 | 说明 |
| --- | --- | --- |
| GET | `/astrbot_plugin_llm_model_guard/overview` | 总览（开关、运行时、统计、模型列表、标记、路由） |
| GET | `/astrbot_plugin_llm_model_guard/routes` | 路由视图 + 配置文件列表（query `profile_id` 可选） |
| GET | `/astrbot_plugin_llm_model_guard/events` | 最近调用记录（query `limit` ≤500、`provider_id`、`profile_id`） |
| GET | `/astrbot_plugin_llm_model_guard/health` | 包装数、supervisor 状态、内部错误计数 |
| POST | `/astrbot_plugin_llm_model_guard/mark` | body `{provider_id, ttl?, reason?}` |
| POST | `/astrbot_plugin_llm_model_guard/unmark` | body `{provider_id}` |
| POST | `/astrbot_plugin_llm_model_guard/probe` | body `{provider_id}`，返回含 `ok`、`latency_ms` |
| POST | `/astrbot_plugin_llm_model_guard/reload` | 重载配置派生状态（路由表/规则/包装/supervisor），返回 `{reloaded: true, summary}` |

响应统一是仪表盘信封：

```json
{"status": "ok", "message": null, "data": { }}
{"status": "error", "message": "页面写操作已在插件配置中禁用（allow_webui_actions=false）", "data": null}
```

写操作（mark / unmark / probe / reload）受 `allow_webui_actions` 控制，默认关闭。

---

## 7. 运行状态与数据

- 状态文件：`data/plugin_data/astrbot_plugin_llm_model_guard/state.json`（唯一临时文件 + `os.replace` 原子替换；
  同一时刻只有一个写盘任务，并发标记不会互相截断）；
- 备份：`state.corrupt-<ts>.json`（JSON 损坏）、`state.v<n>.bak.json`（版本迁移/版本过新）；
- 目录里的 `schema_version=1`；状态内含 `marks`、`marks_by_profile`、`providers`、`recent_events`、`probes`、`runtime`；
- **不记录** prompt、contexts、用户消息原文。分类摘要（`Classification.detail`）截断 200 字符，
  最近事件里的 `error_message` 允许到 500 字符，两者都经过密钥脱敏；
- 总开关 `enable=false` 时 `terminate` 只写既有标记，不写统计数据。

---

## 8. 已知限制

- 本仓库的单测基于桩件与自建假 provider，**不代表真实运行时语义**；
- `hook` 时序、实例包装在 `ProviderManager.reload()` 后的重新挂载、各 SDK 的真实异常类型/`status_code`、
  `/api/plug` 的鉴权粒度，都需要真实 AstrBot 环境回归；
- AstrBot 自身合成的 `role="err"` 响应不经过 provider，因此统计口径以「包装层 attempt」为准；
- **上下文裁剪仍按链首模型计算**：框架在 `build_main_agent` 阶段用链首模型的 `max_context_tokens` /
  `modalities` 做裁剪；插件只在内部换候选，不改变框架这一视角。为避免“把图片发给不支持图片的模型”，
  插件会在切换候选前比对目标模型的 `provider_config["modalities"]` 并跳过不兼容的候选；
- 与 webchat 手动选模型的最终优先级需要真实环境回归（默认 `respect_manual_selection=true`）；
- **不做主动通知**：模型被标记时只写日志 + 页面展示，不推送管理员消息（原任务包的
  `notify_admin_on_mark` 已书面豁免）；
- 用户自定义错误规则的正则没有超时保护（只有管理员可配置，风险有限）；
- `CALL_CONTEXT` 在同一个事件 task 内会复用（TTL 180 秒），第三方插件在同一事件里调用
  `context.llm_generate()` 时可能被强制走上本插件的候选链（已知待优化项）。

待优化（性能类，不影响正确性）：`Guard.settings` 每次访问都会重建配置快照（目前靠指纹比较短路，
后续可缓存对象身份）；`store._shrink` 在超大状态时用循环重算体积，可改为一次估算 + 二分裁剪。

---

## 9. 开发与测试

```powershell
# 在仓库工作区根目录执行
python devtools/run_tests.py --plugin astrbot_plugin_llm_model_guard
python devtools/check_plugin_structure.py astrbot_plugin_llm_model_guard
python devtools/check_encoding.py astrbot_plugin_llm_model_guard
```

模块划分：`core/models.py`（数据模型）、`core/classifier.py`（错误分类）、`core/routing.py`（路由）、`core/registry.py`（包装挂载）、`core/wrapper.py`（守护运行时）、`core/guardian.py`（标记 + 测活）、`core/store.py`（持久化）、`core/metrics.py`（统计）、`core/commands.py`（命令文本）、`core/web_api.py`（Web API 视图）。

依赖方向严格单向：`models ← classifier/routing/metrics/store ← guardian ← wrapper ← main`。

---

## 10. 许可

MIT，见 `LICENSE`。

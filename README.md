# astrbot_plugin_llm_model_guard

按 **AstrBot 自己的模型顺序**接管 LLM 模型选择：**超时 → 同模型重试 → 回退到下一个模型 → 标记坏模型 → 定时验证恢复**。

面向 AstrBot **4.25+**（参考基线 4.25.3）。仅使用 Python 标准库与 AstrBot 内置能力，**无新增第三方依赖**。

> **v0.2.0 是一次大幅简化**：配置从 43 个键压到 **9 个**，
> 并且**配置里不再有任何模型 id、也没有任何配置文件 id**。
> 插件直接读框架已经算好的模型顺序，所以「下拉选择配置文件」这个需求
> **不再需要** —— 不是没实现，而是设计上就不该存在。

---

## 1. 它解决什么问题

1. 主模型偶发限流/超时：自动退避重试同一模型，用户几乎无感。
2. 主模型 key 失效或模型下线：立刻切到**框架配置里的下一个模型**，并把坏模型**临时标记**，后续请求直接绕过。
3. 坏模型什么时候恢复没人知道：插件对已标记模型**定时验证**，**连续成功 N 次**才解除标记、让它回到原来的位置。
4. 想知道每个模型最近表现：调用次数、成功率、错误分类、延迟，命令与页面都能看。

---

## 2. 工作原理

```
请求
 └→ 框架选中的模型（会话级选择 → 配置文件默认模型 → 首个 provider）
     ├→ 超时（request_timeout_seconds 可配）
     └→ 报错
         ├→ 可重试类别（retry_categories 可配）→ 重试 max_retries 次 → 正常输出
         └→ 不可重试 → 回退到次级、次次次… → 标记该模型
     └→ （回退后的模型）报错或超时 → 同样 → 标记该模型
标记该模型
 └→ 定时验证可达性（probe_interval_seconds 可配）
     ├→ 失败 → 连续成功计数归零，继续等待
     └→ 连续成功 probe_success_threshold 次 → 取消标记 → 模型回到路由顺序
```

关键设计：

- **不重复描述模型顺序**：框架的 `ProviderManager.get_using_provider(CHAT_COMPLETION, umo)`
  已经内置了「会话级选择 → 该配置文件默认模型 → 首个 provider」的优先级；
  `provider_settings.fallback_chat_models` 就是该配置文件的有序回退链。
  插件每次请求**实时重建链路**，所以**每个配置文件用各自的模型顺序**，
  分别回退、分别验证。
- **实例级包装，不做类级 monkeypatch**：provider 重载后包装会丢失，因此每个事件都会幂等补挂；
- **零介入**：`enable=false` 时不包装、不写 extra、不验证、不落盘统计；
- **不改用户配置**：不会改写 `provider_settings` / `fallback_chat_models`，也不会调用 `save_config()`。

---

## 3. 安装

1. 把本目录放到 AstrBot 的 `data/plugins/astrbot_plugin_llm_model_guard/`；
2. 重启 AstrBot（或在 WebUI 插件页启用）；
3. **不用配置任何模型** —— 插件直接读框架的顺序，开箱即用。
   想调整行为时只改下面 9 个键。

---

## 4. 配置项（`_conf_schema.json`）

**只有 9 个键，全部是全局的，没有任何 id。**

| 键 | 类型 | 默认值 | 含义（对应流程图） |
| --- | --- | --- | --- |
| `enable` | bool | `true` | 总开关。关闭后插件完全不介入。 |
| `request_timeout_seconds` | int | `120` | **单次模型调用超时**（秒）。0 表示不限制。对应「超时（时间可配置）」。 |
| `max_retries` | int | `1` | **同一模型可重试次数**（不含首次）。0 表示不重试，直接回退。 |
| `retry_backoff_seconds` | float | `1.5` | 重试间隔基数（指数退避：base × 2^(n-1)，上限 10 秒）。 |
| `retry_categories` | list | `["timeout","rate_limit","server_error","network","overloaded"]` | **哪些错误算「可重试」**。不在列表里的错误按「不重试」处理（直接回退 + 标记）。可选值：`timeout`、`rate_limit`、`server_error`、`network`、`overloaded`、`empty_output`。 |
| `probe_interval_seconds` | int | `60` | **被标记模型的验证间隔**（秒）。对应「定时验证可达性」。 |
| `probe_timeout_seconds` | int | `30` | 单次验证请求超时（秒）。 |
| `probe_success_threshold` | int | `2` | **连续成功多少次才取消标记**。设为 1 表示一次成功即恢复。 |
| `allow_webui_actions` | bool | `false` | **允许在监控页手动操作**（测活 / 标记 / 解除标记）。默认关闭以保证安全（面板登录态是全局的，任何能登录的人都能改全局标记状态），需要时在插件配置里开启。 |

#### 关于 `retry_categories` 的两个取舍

- **`overloaded`（HTTP 529，上游瞬时过载）默认重试**：它通常是稍后就会恢复的瞬时错误，
  不重试就直接标记属于**过度惩罚**。要恢复「不重试直接标记」的行为，把它从列表里去掉即可。
- **`empty_output`（模型空输出）默认不重试**：框架自身对空输出已有 tenacity 重试，
  插件再叠加会**放大请求量**。确实需要时把它加进列表即可。

### 4.1 为什么不需要配置模型顺序

插件**不要求**用户描述「哪个配置文件用哪个模型、失败后回退到哪个」。
原因是框架已经把这些信息算好了：

| 层级 | 来源 | 说明 |
| --- | --- | --- |
| 1 | 会话级隔离 | 用户在某个会话里手动切过模型 → 框架的 `sp.get("provider_perf_chat_completion", scope="umo")` |
| 2 | 配置文件默认模型 | 该会话所属 abconf 的 `provider_settings.default_provider_id` |
| 3 | 首个 provider | 上面都没有时的兜底 |
| 回退 | `provider_settings.fallback_chat_models` | **该配置文件自己的有序回退链** |

插件的链路 = `[手动选择] + [框架当前] + [该配置文件的 fallback_chat_models]`，去重后
剔除不存在的模型、跳过被标记的模型。

所以：

- **每个配置文件用各自的模型顺序**，分别回退、分别验证 —— 天然满足；
- **会话级模型选择天然被涵盖** —— 用户在某个会话里切过模型，插件就会跟着用；
- **配置里零 id** —— 不需要「下拉选择配置文件」，也不需要手抄 uuid。

### 4.2 与框架 `fallback_chat_models` 的关系

框架自己在主模型失败时也会遍历 `fallback_chat_models`。插件做的是**同一件事，但更早**：

- 插件在**同一次请求内**完成「重试 → 回退 → 标记」，框架的兜底是**下一次请求**；
- 插件会把坏模型**标记**并跳过，框架不会 —— 所以插件能避免反复打坏模型；
- 插件**不改写** `fallback_chat_models`，只读它。

### 4.3 会话级模型选择怎么生效

- **WebChat / 命令里手动选模型**：框架把它放在 `event.get_extra("selected_provider")`，
  插件读同一个 extra，把它放在链首（优先于会话级隔离）；
- **在某个会话里 `/provider` 切换过的模型**：框架的会话级隔离（`scope="umo"`），
  由 `get_using_provider` 自动带出来，插件直接沿用。

### 4.4 迁移说明（从 v0.1.x 升级）

- **旧配置键会被静默忽略**：`routes` / `profiles` / `fallback_pool` / `disabled_profiles` /
  `error_rules` / `mark_scope` / `enable_*` / `stats_*` / `retry_*_base` 等
  全部**不看、不报错**，插件启动与运行都不受影响；
- `probe_interval_seconds` / `probe_timeout_seconds` **键名没变**，旧值会延续下来；
- `allow_webui_actions` 在 v0.2.0 里进了 schema（这是与 v0.1.x 的关键差别：以前键不在
  schema 里，用户根本不知道有这个开关），**默认 false**。想用监控页的手动
  测活 / 标记 / 解除标记，需要在插件配置里显式开启；
- `retry_categories` 的默认值多了 `overloaded`（若旧配置里显式写过这个键，
  则**以旧配置为准**，不会被默认值覆盖）；
- 状态文件自动从 `schema_version=1` 迁移到 `2`：
  **丢弃 profile 维度的标记、保留全局标记**，并清掉 TTL 字段
  （v0.2.0 没有 TTL，标记只靠「连续成功 N 次」解除）；
- **建议**：在 WebUI 里把旧配置项删掉（它们已经不生效了，留着只是看着乱）。

---

## 5. 行为细节

### 5.1 重试与回退

- **可重试类别**由 `retry_categories` 决定（`timeout` / `rate_limit` / `server_error` / `network`）。
  不在列表里的错误**不重试**，直接回退到下一个模型**并标记**它。
- **不重试的错误也要标记**（流程图明确）。
- **本地编程错误**（`TypeError` / `ValueError` 且无 HTTP 状态码）直接上抛，
  **不重试、不换模型、不标记** —— 避免把插件/框架的参数 bug 伪装成模型故障。
- 重试耗尽后**标记该模型并回退**。

### 5.2 超时

- 非流式：`asyncio.wait_for` 包住整个调用；
- 流式：**只对首个 chunk** 施加超时，首块之后不再施加（避免长回答被误杀）；
- 超时按 `timeout` 类别参与 `retry_categories` 判定。

### 5.3 流式

**首个 chunk 之前**允许重试 / 回退；**已吐出内容后**一律不重试、不切候选、不标记，
原样上抛（避免输出割裂与误标记）。

### 5.4 标记与恢复

- 标记**全局生效**（按 provider），没有 TTL；
- 恢复条件：**连续成功 `probe_success_threshold` 次**；
  验证失败则连续成功计数**归零**，继续等待下一轮；
- 解除标记后**不需要额外动作**：链路每次请求都实时重建，顺序自动恢复。

---

## 6. 命令（管理员）

| 命令 | 说明 |
| --- | --- |
| `/llmguard status` | 开关、链路参数、标记数、近 5 分钟成功率 |
| `/llmguard chains` | **每个配置文件的模型顺序**与被标记项 |
| `/llmguard marks` | 当前标记（原因、连续成功进度、下次验证时间） |
| `/llmguard test <provider_id>` | 立刻验证一次 |
| `/llmguard unmark <provider_id>` | 手动解除标记 |
| `/llmguard reload` | 重载配置派生状态 |

---

## 7. Web API

注册路由以插件名开头（前端桥接会拼成 `/api/plug/<插件名>/<endpoint>`）：

| 方法 | 路由 | 说明 |
| --- | --- | --- |
| GET | `/astrbot_plugin_llm_model_guard/overview` | 总览（开关、运行时、统计、模型列表、标记、链路、配置） |
| GET | `/astrbot_plugin_llm_model_guard/chains` | **每个配置文件的模型顺序**（只读，无对应配置项） |
| GET | `/astrbot_plugin_llm_model_guard/events` | 最近调用记录（query `limit` ≤500、`provider_id`、`profile_id`） |
| POST | `/astrbot_plugin_llm_model_guard/mark` | body `{provider_id, ttl?, reason?}` |
| POST | `/astrbot_plugin_llm_model_guard/unmark` | body `{provider_id}` |
| POST | `/astrbot_plugin_llm_model_guard/probe` | body `{provider_id}`，返回含 `ok`、`latency_ms` |
| POST | `/astrbot_plugin_llm_model_guard/reload` | 重载配置派生状态，返回 `{reloaded: true, summary}` |

响应统一是仪表盘信封：

```json
{"status": "ok", "message": null, "data": { }}
{"status": "error", "message": "页面写操作已在插件配置中禁用", "data": null}
```

写操作（mark / unmark / probe / reload）受 `allow_webui_actions` 控制，
**默认关闭** —— AstrBot 面板的登录态是**全局**的，默认开启意味着任何能登录
Dashboard 的人都能改全局标记状态。关闭后监控页是只读的（操作按钮隐藏）。
需要时在插件配置里开启即可（该键在 schema 里，看得见）。

---

## 8. 运行状态与数据

- 状态文件：`data/plugin_data/astrbot_plugin_llm_model_guard/state.json`（唯一临时文件 + `os.replace` 原子替换）；
- 备份：`state.corrupt-<ts>.json`（JSON 损坏）、`state.v<n>.bak.json`（版本迁移/版本过新）；
- 目录里的 `schema_version=2`；状态内含 `marks`（provider → 标记信息 + `consecutive_success` + `next_probe_at` + `reason`）、
  `providers`（统计）、`recent_events`、`probes`；
- **不记录** prompt、contexts、用户消息原文。分类摘要截断 200 字符，
  最近事件里的 `error_message` 允许到 500 字符，两者都经过密钥脱敏；
- 总开关 `enable=false` 时 `terminate` 只写既有标记，不写统计数据。

---

## 9. 已知限制

- 本仓库的单测基于桩件与自建假 provider，**不代表真实运行时语义**；
- `hook` 时序、实例包装在 `ProviderManager.reload()` 后的重新挂载、各 SDK 的真实异常类型/`status_code`、
  `/api/plug` 的鉴权粒度，都需要真实 AstrBot 环境回归；
- AstrBot 自身合成的 `role="err"` 响应不经过 provider，因此统计口径以「包装层 attempt」为准；
- **上下文裁剪仍按链首模型计算**：框架在 `build_main_agent` 阶段用链首模型的 `max_context_tokens` /
  `modalities` 做裁剪；插件只在内部换候选，不改变框架这一视角；
- 与 webchat 手动选模型的最终优先级需要真实环境回归；
- **不做主动通知**：模型被标记时只写日志 + 页面展示，不推送管理员消息；
- 用户自定义错误规则已随 v0.2.0 移除（内置分类表保留）；
- `CALL_CONTEXT` 在同一个事件 task 内会复用（TTL 180 秒），第三方插件在同一事件里调用
  `context.llm_generate()` 时可能被强制走上本插件的候选链（已知待优化项）。

---

## 10. 开发与测试

```powershell
# 在仓库工作区根目录执行
python devtools/run_tests.py --plugin astrbot_plugin_llm_model_guard
python devtools/check_plugin_structure.py astrbot_plugin_llm_model_guard
python devtools/check_encoding.py astrbot_plugin_llm_model_guard
```

模块划分：`core/models.py`（数据模型）、`core/chain.py`（链路构建）、
`core/classifier.py`（错误分类）、`core/registry.py`（包装挂载）、
`core/wrapper.py`（守护运行时）、`core/guardian.py`（标记 + 验证）、
`core/store.py`（持久化）、`core/metrics.py`（统计）、`core/commands.py`（命令文本）、
`core/web_api.py`（Web API 视图）。

依赖方向严格单向：`models ← chain/classifier/metrics/store ← guardian ← wrapper ← main`。

---

## 11. 许可

MIT，见 `LICENSE`。

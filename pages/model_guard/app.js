/* LLM 模型守护 · 插件页面脚本（v0.2.0 链路视图）
 *
 * 免构建：原生 ES 模块，无外部依赖、无 CDN、无框架。
 * 数据通过 AstrBot 注入的 window.AstrBotPluginPage 桥接获取：
 *   apiGet(endpoint, params) / apiPost(endpoint, body)
 * 桥接内部会把 endpoint 拼成 /api/plug/<插件名>/<endpoint>，因此这里只传
 * 相对端点名（overview / chains / events / probe / mark / unmark / reload）。
 * 已删除的端点（/profiles、/schema_refresh）不再调用。
 * 桥接只把响应信封里的 data 交给页面，status="error" 会被 reject 成 Error。
 *
 * 安全约定：所有后端返回的文本一律走 textContent，不使用 innerHTML。
 */

const DEFAULT_INTERVAL_MS = 15000;
const READY_TIMEOUT_MS = 10000;
const REQUEST_TIMEOUT_MS = 20000;
const TOAST_TTL_MS = 4500;
const TOAST_MAX = 4;
const EVENTS_LIMIT = 50;

const bridge =
  typeof window !== "undefined" && window.AstrBotPluginPage
    ? window.AstrBotPluginPage
    : null;

const pageState = {
  bridgeReady: false,
  overview: null,
  events: undefined,
  eventsUnsupported: false,
  eventProviderFilter: "",
  chains: undefined,
  chainsUnsupported: false,
  chainsError: "",
  autoRefresh: true,
  intervalMs: DEFAULT_INTERVAL_MS,
  timerId: null,
  loading: false,
  lastLoadedAt: null,
  loadError: null,
  themeLocked: false,
  allowActions: false,
  sortKey: "errors",
  providerFilter: "",
  busy: new Set(),
};

const ui = {};

/* ---------- 通用小工具 ---------- */

function $(id) {
  return document.getElementById(id);
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function clearNode(node) {
  if (!node) return;
  while (node.firstChild) node.removeChild(node.firstChild);
}

/** 焦点操作在非浏览器/测试环境下可能不存在，统一做兜底。 */
function focusIfPossible(node) {
  if (node && typeof node.focus === "function") node.focus();
}

function errorText(error) {
  if (!error) return "未知错误";
  const message = typeof error === "string" ? error : error.message;
  return message ? String(message) : "未知错误";
}

function withTimeout(promise, ms, label) {
  let timer = null;
  const timeout = new Promise((resolve, reject) => {
    timer = window.setTimeout(() => reject(new Error(label)), ms);
  });
  return Promise.race([promise, timeout]).finally(() => {
    if (timer) window.clearTimeout(timer);
  });
}

function readPath(source, path) {
  let current = source;
  const parts = String(path).split(".");
  for (const part of parts) {
    if (current === null || typeof current !== "object" || !(part in current)) {
      return undefined;
    }
    current = current[part];
  }
  return current;
}

/** 按候选路径取第一个「有内容」的值，用于兼容后端字段命名差异。 */
function pick(source, paths) {
  for (const path of paths) {
    const value = readPath(source, path);
    if (value === undefined || value === null || value === "") continue;
    return value;
  }
  return undefined;
}

/** 依次在多个容器里按候选路径取值，兼容 stats/totals/顶层三种组织方式。 */
function pickFrom(sources, paths) {
  for (const source of sources) {
    const value = pick(source, paths);
    if (value !== undefined) return value;
  }
  return undefined;
}

function asNumber(value) {
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function asBool(value) {
  if (typeof value === "boolean") return value;
  if (typeof value === "string") {
    const lower = value.trim().toLowerCase();
    if (["true", "1", "yes", "on"].includes(lower)) return true;
    if (["false", "0", "no", "off"].includes(lower)) return false;
  }
  if (typeof value === "number") return value !== 0;
  return undefined;
}

function fmtInt(value) {
  const parsed = asNumber(value);
  return parsed === null ? "—" : Math.round(parsed).toLocaleString("zh-CN");
}

function fmtMs(value) {
  const parsed = asNumber(value);
  if (parsed === null) return "—";
  if (parsed < 1000) return `${Math.round(parsed)} ms`;
  return `${(parsed / 1000).toFixed(2)} s`;
}

function fmtSeconds(value) {
  const parsed = asNumber(value);
  return parsed === null ? "—" : `${Math.round(parsed)} 秒`;
}

/** 带单位的整数展示，例如「2 次」。 */
function fmtCount(value, unit) {
  const parsed = asNumber(value);
  return parsed === null ? "—" : `${Math.round(parsed)} ${unit}`;
}

function fmtPercent(ratio) {
  const parsed = asNumber(ratio);
  if (parsed === null) return "—";
  return `${(parsed * 100).toFixed(1)}%`;
}

/** 后端时间戳可能是 epoch 秒/毫秒，也可能是 ISO 字符串。 */
function toDate(value) {
  const numeric = asNumber(value);
  if (numeric !== null) {
    const ms = Math.abs(numeric) > 1e12 ? numeric : numeric * 1000;
    const fromNumber = new Date(ms);
    return Number.isNaN(fromNumber.getTime()) ? null : fromNumber;
  }
  if (typeof value === "string" && value.trim()) {
    const fromString = new Date(value);
    return Number.isNaN(fromString.getTime()) ? null : fromString;
  }
  return null;
}

function pad2(value) {
  return String(value).padStart(2, "0");
}

function fmtTime(value) {
  const date = toDate(value);
  if (!date) {
    return typeof value === "string" && value.trim() ? value : "—";
  }
  return [
    `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`,
    `${pad2(date.getHours())}:${pad2(date.getMinutes())}:${pad2(date.getSeconds())}`,
  ].join(" ");
}

function fmtClock(date) {
  if (!(date instanceof Date)) return "—";
  return `${pad2(date.getHours())}:${pad2(date.getMinutes())}:${pad2(date.getSeconds())}`;
}

/* ---------- 提示条 / toast ---------- */

const banners = new Map();

function setBanner(key, kind, title, message) {
  banners.set(key, { kind, title, message });
  renderBanners();
}

function clearBanner(key) {
  if (banners.delete(key)) renderBanners();
}

function renderBanners() {
  if (!ui.banners) return;
  clearNode(ui.banners);
  banners.forEach((item) => {
    const box = el("div", `lm-banner lm-banner--${item.kind}`);
    box.appendChild(el("strong", null, `${item.title}：`));
    box.appendChild(el("span", null, item.message || ""));
    ui.banners.appendChild(box);
  });
}

function pushToast(kind, text) {
  if (!ui.toasts) return;
  while (ui.toasts.childElementCount >= TOAST_MAX) {
    ui.toasts.removeChild(ui.toasts.firstElementChild);
  }
  const toast = el("div", `lm-toast lm-toast--${kind}`, text);
  ui.toasts.appendChild(toast);
  window.setTimeout(() => {
    if (toast.parentNode === ui.toasts) ui.toasts.removeChild(toast);
  }, TOAST_TTL_MS);
}

/* ---------- 桥接调用 ---------- */

function unwrapEnvelope(payload) {
  // 桥接通常已经把信封里的 data 交给页面；这里再兼容一次仍未解包的情况。
  if (
    payload &&
    typeof payload === "object" &&
    !Array.isArray(payload) &&
    payload.data &&
    typeof payload.data === "object"
  ) {
    const inner = payload.data;
    // chains 端点的 data 就是 {profiles:[...]}，所以 profiles 也在识别列表里。
    const looksLikeData =
      inner.providers !== undefined ||
      inner.chains !== undefined ||
      inner.profiles !== undefined ||
      inner.marks !== undefined ||
      inner.events !== undefined ||
      inner.stats !== undefined ||
      inner.runtime !== undefined ||
      inner.enabled !== undefined;
    if (looksLikeData) return inner;
  }
  return payload;
}

async function apiGet(endpoint, params) {
  if (!bridge || typeof bridge.apiGet !== "function") {
    throw new Error("页面桥接不可用，无法调用接口");
  }
  const response = await withTimeout(
    bridge.apiGet(endpoint, params || {}),
    REQUEST_TIMEOUT_MS,
    `接口 ${endpoint} 请求超时`,
  );
  return unwrapEnvelope(response);
}

async function apiPost(endpoint, body) {
  if (!bridge || typeof bridge.apiPost !== "function") {
    throw new Error("页面桥接不可用，无法调用接口");
  }
  const response = await withTimeout(
    bridge.apiPost(endpoint, body || {}),
    REQUEST_TIMEOUT_MS,
    `接口 ${endpoint} 请求超时`,
  );
  return unwrapEnvelope(response);
}

function isRouteMissing(error) {
  const message = errorText(error);
  return /(^|\D)404(\D|$)|not found|Not Found/i.test(message);
}

/* ---------- 数据归一化 ---------- */

const MARK_STATE_LABELS = {
  healthy: { text: "健康", tone: "ok" },
  marked: { text: "已标记", tone: "danger" },
  confirming: { text: "待验证", tone: "warn" },
  probing: { text: "验证中", tone: "warn" },
  normal: { text: "健康", tone: "ok" },
  ok: { text: "健康", tone: "ok" },
};

/** marked_state 里表示「正常」的取值；其余非空值都按被标记处理。 */
const HEALTHY_MARK_STATES = new Set(["healthy", "ok", "normal", "none"]);

function isMarkedStateValue(state) {
  return Boolean(state) && !HEALTHY_MARK_STATES.has(state);
}

function readMarkState(...sources) {
  for (const source of sources) {
    if (!source) continue;
    if (typeof source === "string") {
      const lower = source.trim().toLowerCase();
      if (lower && lower !== "none") return lower;
      continue;
    }
    if (typeof source === "object") {
      const state = typeof source.state === "string" ? source.state.trim().toLowerCase() : "";
      if (state && state !== "none") return state;
      if (asBool(source.pending_confirm) === true) return "confirming";
    }
  }
  return "";
}

function markBadge(state) {
  const meta = MARK_STATE_LABELS[state];
  if (meta) return el("span", `lm-badge lm-badge--${meta.tone}`, meta.text);
  // 未知状态原样展示，避免把异常状态误报成「正常」。
  if (state) return el("span", "lm-badge lm-badge--warn", state);
  return el("span", "lm-badge lm-badge--ok", "正常");
}

function providerIdOf(entry) {
  const value = pick(entry, ["provider_id", "id", "provider", "model_id", "name"]);
  return value === undefined ? "" : String(value);
}

function normalizeMarks(rawMarks) {
  const list = [];
  if (Array.isArray(rawMarks)) {
    for (const item of rawMarks) {
      if (item && typeof item === "object") list.push(item);
    }
  } else if (rawMarks && typeof rawMarks === "object") {
    for (const key of Object.keys(rawMarks)) {
      const item = rawMarks[key];
      if (!item || typeof item !== "object") continue;
      list.push(
        Object.assign({ provider_id: item.provider_id || key }, item),
      );
    }
  }
  return list;
}

function normalizeProviders(rawProviders, marksById) {
  const list = [];
  const push = (entry, fallbackId) => {
    if (!entry || typeof entry !== "object") return;
    const id = providerIdOf(entry) || (fallbackId ? String(fallbackId) : "");
    if (!id) return;
    const markEntry = marksById.get(id) || null;
    const rawMarkState = readMarkState(entry.marked_state, markEntry);
    const markedFlag = asBool(pick(entry, ["marked"]));
    // marked_state 可能是 "marked" 或 "healthy"：只有非健康值才算被标记。
    const marked =
      isMarkedStateValue(rawMarkState) || markedFlag === true || Boolean(markEntry);
    const state = marked
      ? rawMarkState && isMarkedStateValue(rawMarkState)
        ? rawMarkState
        : "marked"
      : "healthy";
    const calls = asNumber(
      pick(entry, ["calls", "stats.calls", "stats.total", "total_calls"]),
    );
    const ok = asNumber(pick(entry, ["ok", "stats.ok", "stats.success", "success"]));
    const errors = asNumber(
      pick(entry, ["errors", "stats.errors", "stats.error", "stats.error_count"]),
    );
    let successRate = asNumber(pick(entry, ["success_rate", "stats.success_rate"]));
    if (successRate === null && calls !== null && calls > 0 && ok !== null) {
      successRate = ok / calls;
    }
    const latencyAvg = asNumber(
      pick(entry, [
        "latency_ms.avg",
        "avg_latency_ms",
        "stats.latency_ms.avg",
        "stats.avg_latency_ms",
        "average_latency_ms",
      ]),
    );
    const latencyP95 = asNumber(
      pick(entry, ["latency_ms.p95", "stats.latency_ms.p95", "stats.p95_latency_ms"]),
    );
    const errorClass = pick(entry, [
      "last_error.class",
      "last_error.error_class",
      "stats.last_error.class",
      "stats.last_error_class",
      "last_error_class",
    ]);
    const errorMessage = pick(entry, [
      "last_error.message",
      "stats.last_error.message",
      "stats.last_error",
      "last_error_message",
      "last_error",
    ]);
    // 最近错误类别：优先 last_error.class；v0.2.0 只给 error_classes 计数，
    // 这时取出现次数最多的类别，退而使用标记原因。
    const errorClasses =
      entry.error_classes && typeof entry.error_classes === "object" ? entry.error_classes : null;
    let errorClassText = errorClass === undefined ? "" : String(errorClass);
    if (!errorClassText && errorClasses) {
      let bestClass = "";
      let bestCount = -1;
      for (const name of Object.keys(errorClasses)) {
        const count = asNumber(errorClasses[name]);
        const numeric = count === null ? 0 : count;
        if (numeric > bestCount) {
          bestCount = numeric;
          bestClass = name;
        }
      }
      errorClassText = bestClass;
    }
    if (!errorClassText) {
      errorClassText = String(pick(entry, ["marked_reason", "reason"]) || "");
    }
    list.push({
      id,
      model:
        pick(entry, ["model", "model_name", "model_id", "meta.model", "provider_config.model"]) ||
        "",
      calls,
      ok,
      errors,
      successRate,
      latencyAvg,
      latencyP95,
      errorClass: errorClassText,
      errorClasses: errorClasses || {},
      errorMessage:
        typeof errorMessage === "string"
          ? errorMessage
          : errorMessage && typeof errorMessage === "object"
            ? String(pick(errorMessage, ["message", "detail"]) || "")
            : "",
      consecutiveErrors: asNumber(pick(entry, ["consecutive_errors", "stats.consecutive_errors"])),
      // 连续验证成功次数可能在 provider.probe 里，也可能只在标记记录里。
      consecutiveSuccess:
        asNumber(
          pick(entry, [
            "probe.consecutive_success",
            "consecutive_success",
            "stats.consecutive_success",
            "probe_success_count",
          ]),
        ) ?? asNumber(pick(markEntry || {}, ["consecutive_success"])),
      probeOk: asBool(pick(entry, ["probe.last_ok", "probe.ok"])),
      marked,
      state,
      mark: markEntry,
      raw: entry,
    });
  };

  if (Array.isArray(rawProviders)) {
    rawProviders.forEach((entry) => push(entry, null));
  } else if (rawProviders && typeof rawProviders === "object") {
    Object.keys(rawProviders).forEach((key) => push(rawProviders[key], key));
  }
  return list;
}

function toStringList(value) {
  return Array.isArray(value) ? value.map(String).filter(Boolean) : [];
}

/**
 * GET /chains 的 data：{profiles:[{...}]}。
 * 每项：id/profile_id、name、path、is_default、current_provider/default_provider、
 * fallbacks、chain、bypassed/marked、missing。
 * 注意：后端实现的 chain 已剔除被跳过的模型，为了能在流水线里标出「已标记」，
 * 这里按框架顺序（当前模型 + 回退列表）重建展示顺序，再并入后端给的 chain 与 marked。
 */
function normalizeChains(raw) {
  const source = unwrapEnvelope(raw);
  let list = [];
  if (Array.isArray(source)) {
    list = source;
  } else if (source && typeof source === "object") {
    if (Array.isArray(source.profiles)) list = source.profiles;
    else if (Array.isArray(source.chains)) list = source.chains;
    else if (Array.isArray(source.items)) list = source.items;
  }

  return list
    .filter((item) => item && typeof item === "object")
    .map((item) => {
      const currentProvider = String(
        pick(item, ["current_provider", "default_provider", "current", "primary", "main"]) || "",
      );
      const fallbacks = toStringList(
        pick(item, ["fallbacks", "fallback", "fallback_chain"]),
      );
      const backendChain = toStringList(pick(item, ["chain", "effective_chain", "candidates"]));
      const bypassed = toStringList(pick(item, ["bypassed", "marked", "skipped", "marked_ids"]));

      // 展示用的顺序：框架展开顺序优先，再补后端 chain 里多出来的项，
      // 最后补上后端单独标记但不在上述两个列表里的 id。
      const orderedChain = [];
      for (const providerId of [currentProvider, ...fallbacks, ...backendChain, ...bypassed]) {
        if (providerId && !orderedChain.includes(providerId)) orderedChain.push(providerId);
      }
      const chain = orderedChain;
      return {
        id: String(pick(item, ["id", "profile_id", "conf_id"]) || ""),
        name: String(pick(item, ["name", "profile_name", "conf_name"]) || ""),
        path: String(pick(item, ["path", "file", "config_path"]) || ""),
        isDefault: asBool(pick(item, ["is_default", "default"])) === true,
        currentProvider,
        fallbacks,
        chain,
        bypassed,
        missing: toStringList(pick(item, ["missing", "missing_ids"])),
      };
    })
    .filter((item) => item.id || item.name || item.chain.length);
}

function normalizeOverview(raw) {
  const source = unwrapEnvelope(raw) || {};
  // 后端可能把标记列表放在 marks，也可能沿用 guardian.snapshot() 的 marked。
  const marks = normalizeMarks(source.marks !== undefined ? source.marks : source.marked);
  const marksById = new Map();
  for (const mark of marks) {
    const id = providerIdOf(mark);
    if (id) marksById.set(id, mark);
  }
  const providers = normalizeProviders(
    source.providers !== undefined ? source.providers : pick(source, ["metrics.providers"]),
    marksById,
  );
  const runtime =
    source.runtime && typeof source.runtime === "object" ? source.runtime : {};
  const stats = source.stats && typeof source.stats === "object" ? source.stats : {};
  const totals = source.totals && typeof source.totals === "object" ? source.totals : {};
  const statSources = [stats, totals, source];
  const config = source.config && typeof source.config === "object" ? source.config : {};
  const counters = Object.assign(
    {},
    pick(source, ["counters"]) || {},
    pick(stats, ["counters"]) || {},
    pick(runtime, ["counters"]) || {},
  );

  let totalCalls = asNumber(
    pickFrom(statSources, ["total_calls", "calls", "total", "totals.calls", "metrics.totals.calls"]),
  );
  if (totalCalls === null) {
    totalCalls = providers.reduce((sum, item) => sum + (item.calls || 0), 0);
  }
  let totalErrors = asNumber(
    pickFrom(statSources, ["total_errors", "errors", "error_count", "totals.errors", "metrics.totals.errors"]),
  );
  if (totalErrors === null) {
    totalErrors = providers.reduce((sum, item) => sum + (item.errors || 0), 0);
  }
  let successRate = asNumber(
    pickFrom(statSources, ["success_rate", "ok_rate", "totals.success_rate", "metrics.totals.success_rate"]),
  );
  if (successRate === null && totalCalls > 0) {
    successRate = (totalCalls - totalErrors) / totalCalls;
  }

  let markedCount = asNumber(pick(stats, ["marked", "marked_count", "marks"]));
  if (markedCount === null) markedCount = asNumber(pick(runtime, ["marked"]));
  if (markedCount === null) {
    markedCount = marks.length || providers.filter((item) => item.marked).length;
  }

  const enabled = asBool(pick(source, ["enabled", "runtime.enabled", "config.enabled", "config.enable"]));
  const allowActions = asBool(
    pick(runtime, ["allow_actions", "allow_webui_actions"]) ??
      pick(source, ["allow_actions", "allow_webui_actions", "config.allow_webui_actions"]),
  );

  const supervisorValue = pick(runtime, ["supervisor", "supervisor_state"]);
  const supervisorRunning = asBool(pick(runtime, ["supervisor_running", "supervisor.running"]));
  let supervisor = supervisorValue ? String(supervisorValue) : "";
  if (!supervisor && supervisorRunning === true) supervisor = "running";
  if (!supervisor && supervisorRunning === false) supervisor = "stopped";

  const internalErrors = asNumber(
    pick(runtime, ["internal_errors", "guard_internal_errors"]) ??
      counters.guard_internal_errors,
  );
  const corruptCount = asNumber(counters.corrupt_state);
  const stateResetFlag = asBool(
    pick(source, ["state_reset", "runtime.state_reset", "runtime.corrupt_state"]),
  );

  return {
    enabled,
    allowActions: allowActions === true,
    providers,
    marks,
    stats: {
      totalCalls,
      totalErrors,
      successRate,
      markedCount,
      windowSeconds: asNumber(pickFrom(statSources, ["window_s"])),
    },
    runtime,
    counters,
    config: {
      // v0.2.0：8 个全局键，配置里不再有 provider id / 配置文件 id。
      requestTimeout: asNumber(
        pick(config, ["request_timeout_seconds", "request_timeout", "timeout_seconds"]),
      ),
      maxRetries: asNumber(pick(config, ["max_retries", "retries"])),
      retryBackoff: asNumber(
        pick(config, ["retry_backoff_seconds", "retry_backoff", "backoff_seconds"]),
      ),
      retryCategories: toStringList(
        pick(config, ["retry_categories", "retry_category", "categories"]),
      ),
      probeInterval: asNumber(pick(config, ["probe_interval_seconds", "probe_interval"])),
      probeTimeout: asNumber(pick(config, ["probe_timeout_seconds", "probe_timeout"])),
      probeSuccessThreshold: asNumber(
        pick(config, ["probe_success_threshold", "success_threshold", "probe_threshold"]),
      ),
      raw: config,
    },
    chains: normalizeChains(pick(source, ["chains"])),
    wrappedCount: asNumber(pick(runtime, ["wrapped", "wrapped_count"])),
    providerManager: asBool(pick(runtime, ["provider_manager"])),
    supervisor,
    internalErrors,
    lastError: pick(runtime, ["last_error", "last_error.message"]),
    warnings: Array.isArray(source.warnings) ? source.warnings : [],
    stateReset: stateResetFlag === true || (corruptCount !== null && corruptCount > 0),
  };
}

function normalizeEvents(raw) {
  const source = unwrapEnvelope(raw);
  let list = [];
  if (Array.isArray(source)) {
    list = source;
  } else if (source && typeof source === "object") {
    if (Array.isArray(source.events)) list = source.events;
    else if (Array.isArray(source.records)) list = source.records;
    else if (Array.isArray(source.items)) list = source.items;
  }
  return list.filter((item) => item && typeof item === "object");
}

/* ---------- 渲染：顶栏与卡片 ---------- */

function renderSummary(data) {
  const badge = ui.enableBadge;
  if (badge) {
    if (data && data.enabled === true) {
      badge.className = "lm-badge lm-badge--ok";
      badge.textContent = "已启用";
    } else if (data && data.enabled === false) {
      badge.className = "lm-badge lm-badge--danger";
      badge.textContent = "已禁用";
    } else {
      badge.className = "lm-badge lm-badge--mute";
      badge.textContent = "状态未知";
    }
  }

  const summary = ui.configSummary;
  if (!summary) return;
  clearNode(summary);
  const config = data ? data.config : {};
  const items = [
    `请求超时 ${fmtSeconds(config.requestTimeout)}`,
    `最大重试 ${fmtCount(config.maxRetries, "次")}`,
    `验证间隔 ${fmtSeconds(config.probeInterval)}`,
    `连续成功阈值 ${fmtCount(config.probeSuccessThreshold, "次")}`,
  ];
  items.push(`上次刷新 ${pageState.lastLoadedAt ? fmtClock(pageState.lastLoadedAt) : "—"}`);
  for (const text of items) {
    summary.appendChild(el("span", null, text));
  }
}

function buildCard(label, value, note, tone) {
  const card = el("div", "lm-card");
  card.appendChild(el("div", "lm-card-label", label));
  card.appendChild(
    el("div", tone ? `lm-card-value lm-card-value--${tone}` : "lm-card-value", value),
  );
  card.appendChild(el("div", "lm-card-note", note));
  return card;
}

function renderCards(data) {
  if (!ui.cards) return;
  clearNode(ui.cards);
  if (!data) {
    ui.cards.appendChild(buildCard("插件状态", "—", "尚未获取到数据"));
    return;
  }
  const stats = data.stats;
  const config = data.config;
  const rateTone =
    stats.successRate === null
      ? undefined
      : stats.successRate >= 0.99
        ? "ok"
        : stats.successRate >= 0.9
          ? "warn"
          : "danger";

  ui.cards.appendChild(
    buildCard(
      "插件状态",
      data.enabled === true ? "已启用" : data.enabled === false ? "已禁用" : "未知",
      data.enabled === false ? "插件当前不接管模型，本页为历史数据" : "正在守护模型调用",
      data.enabled === false ? "danger" : data.enabled === true ? "ok" : undefined,
    ),
  );
  ui.cards.appendChild(
    buildCard(
      "成功率",
      fmtPercent(stats.successRate),
      `共 ${fmtInt(stats.totalCalls)} 次调用${
        stats.totalErrors ? ` · ${fmtInt(stats.totalErrors)} 次失败` : ""
      }`,
      rateTone,
    ),
  );
  ui.cards.appendChild(
    buildCard(
      "被标记模型",
      fmtInt(stats.markedCount),
      stats.markedCount > 0 ? "被标记的模型会在链路中被跳过" : "当前没有被标记的模型",
      stats.markedCount > 0 ? "warn" : undefined,
    ),
  );
  ui.cards.appendChild(
    buildCard(
      "请求超时",
      fmtSeconds(config.requestTimeout),
      config.retryBackoff === null
        ? "超出后按错误类别决定是否重试"
        : `重试退避 ${fmtSeconds(config.retryBackoff)}`,
    ),
  );
  ui.cards.appendChild(
    buildCard(
      "最大重试",
      fmtCount(config.maxRetries, "次"),
      config.retryCategories.length
        ? `重试类别：${config.retryCategories.join("、")}`
        : "仅对可重试的错误类别生效",
    ),
  );
  ui.cards.appendChild(
    buildCard(
      "验证间隔",
      fmtSeconds(config.probeInterval),
      config.probeTimeout === null
        ? "对已标记模型定期验证"
        : `单次验证超时 ${fmtSeconds(config.probeTimeout)}`,
    ),
  );
  ui.cards.appendChild(
    buildCard(
      "连续成功阈值",
      fmtCount(config.probeSuccessThreshold, "次"),
      "验证连续成功达到该次数后自动解除标记",
    ),
  );
}

/* ---------- 渲染：链路视图（核心） ---------- */

/** 流水线里的一步：provider id（等宽字体）；被标记的加删除线与「已标记」标签。 */
function chainStep(providerId, bypassedSet) {
  const marked = bypassedSet.has(providerId);
  const step = el(
    "span",
    marked ? "lm-pipeline-step lm-pipeline-step--marked" : "lm-pipeline-step",
  );
  step.appendChild(el("span", "lm-pipeline-id lm-mono", providerId));
  if (marked) {
    step.appendChild(el("span", "lm-badge lm-badge--danger", "已标记"));
  }
  return step;
}

function renderChains() {
  const wrap = ui.chainsWrap;
  if (!wrap) return;
  clearNode(wrap);

  const state = pageState.chains;

  if (pageState.chainsUnsupported) {
    if (ui.chainsCount) ui.chainsCount.textContent = "—";
    if (ui.chainsHint) ui.chainsHint.textContent = "";
    const empty = el("div", "lm-empty");
    empty.appendChild(el("strong", null, "当前后端未提供链路信息"));
    empty.appendChild(
      el("span", null, "后端尚未注册 /chains 接口；升级插件后刷新本页即可看到链路。"),
    );
    wrap.appendChild(empty);
    return;
  }

  if (state === undefined) {
    if (ui.chainsCount) ui.chainsCount.textContent = "—";
    if (ui.chainsHint) ui.chainsHint.textContent = "";
    wrap.appendChild(el("div", "lm-empty", "正在加载链路信息…"));
    return;
  }

  if (state === null) {
    if (ui.chainsCount) ui.chainsCount.textContent = "—";
    if (ui.chainsHint) ui.chainsHint.textContent = "";
    const box = el("div", "lm-inline-notice lm-inline-notice--error");
    box.appendChild(el("strong", null, "链路信息加载失败："));
    box.appendChild(el("span", null, pageState.chainsError || "未知错误"));
    wrap.appendChild(box);
    const retryRow = el("div", "lm-retry-row");
    const retry = el("button", "lm-btn lm-btn--sm", "重试");
    retry.type = "button";
    retry.addEventListener("click", () => {
      loadChains();
    });
    retryRow.appendChild(retry);
    wrap.appendChild(retryRow);
    return;
  }

  const profiles = state;
  if (ui.chainsCount) ui.chainsCount.textContent = String(profiles.length);
  if (ui.chainsHint) {
    ui.chainsHint.textContent = profiles.length ? "按框架配置文件展示实际尝试顺序" : "";
  }

  if (!profiles.length) {
    const empty = el("div", "lm-empty");
    empty.appendChild(el("strong", null, "没有读取到配置文件链路"));
    empty.appendChild(
      el("span", null, "框架未返回任何配置文件；先在 AstrBot 里保存一次配置文件后刷新本页。"),
    );
    wrap.appendChild(empty);
    return;
  }

  const list = el("ul", "lm-chain-list");
  for (const profile of profiles) {
    const item = el("li", "lm-chain-item");

    const head = el("div", "lm-chain-head");
    const nameLine = el("div");
    nameLine.appendChild(
      el("span", "lm-chain-name", profile.name || profile.id || "（未命名配置文件）"),
    );
    if (profile.isDefault) {
      nameLine.appendChild(el("span", "lm-badge lm-badge--ok lm-badge--inline", "默认"));
    }
    head.appendChild(nameLine);

    const metaParts = [];
    if (profile.id) metaParts.push(profile.id);
    if (profile.path) metaParts.push(profile.path);
    if (metaParts.length) {
      head.appendChild(el("span", "lm-chain-meta lm-mono", metaParts.join(" · ")));
    }
    item.appendChild(head);

    const pipeline = el("div", "lm-pipeline");
    if (!profile.chain.length) {
      pipeline.appendChild(el("span", "lm-hint", "该配置文件未设置默认模型"));
    } else {
      const bypassedSet = new Set(profile.bypassed);
      profile.chain.forEach((providerId, index) => {
        if (index > 0) pipeline.appendChild(el("span", "lm-pipeline-arrow", "→"));
        pipeline.appendChild(chainStep(providerId, bypassedSet));
      });
    }
    item.appendChild(pipeline);

    if (profile.chain.length && !profile.currentProvider) {
      item.appendChild(
        el("p", "lm-chain-note", "该配置文件未设置默认模型，链路来自框架的回退配置。"),
      );
    }
    if (profile.missing.length) {
      item.appendChild(
        el(
          "p",
          "lm-chain-note lm-chain-note--warn",
          `配置里引用了不存在的模型：${profile.missing.join("、")}`,
        ),
      );
    }
    const markedOutside = profile.bypassed.filter((id) => !profile.chain.includes(id));
    if (markedOutside.length) {
      item.appendChild(
        el("p", "lm-chain-note", `已标记但不在当前链路中：${markedOutside.join("、")}`),
      );
    }

    list.appendChild(item);
  }
  wrap.appendChild(list);
}

/* ---------- 渲染：模型统计表 ---------- */

function filteredProviders(data) {
  if (!data) return [];
  const keyword = pageState.providerFilter.trim().toLowerCase();
  let list = data.providers.slice();
  if (keyword) {
    list = list.filter(
      (item) =>
        item.id.toLowerCase().includes(keyword) ||
        String(item.model || "").toLowerCase().includes(keyword),
    );
  }
  const key = pageState.sortKey;
  list.sort((a, b) => {
    if (key === "latency") {
      return (b.latencyAvg || -1) - (a.latencyAvg || -1);
    }
    const errorDiff = (b.errors || 0) - (a.errors || 0);
    if (errorDiff !== 0) return errorDiff;
    return (b.calls || 0) - (a.calls || 0);
  });
  return list;
}

function actionButton(label, action, providerId, extraClass) {
  const button = el(
    "button",
    `lm-btn lm-btn--sm${extraClass ? ` ${extraClass}` : ""}`,
    label,
  );
  button.type = "button";
  button.dataset.action = action;
  button.dataset.providerId = providerId;
  button.addEventListener("click", () => {
    runProviderAction(providerId, action, button);
  });
  return button;
}

/** 状态列第二行：被标记的模型显示验证进度（连续成功 N/阈值）。 */
function statusSuccessNote(provider, config) {
  const success = provider.consecutiveSuccess;
  const threshold = config ? config.probeSuccessThreshold : null;
  if (!provider.marked) {
    return success !== null && success > 0 ? `验证连续成功 ${fmtInt(success)} 次` : "";
  }
  if (success === null) return "";
  if (threshold !== null) {
    return `验证连续成功 ${fmtInt(success)}/${fmtInt(threshold)} 次`;
  }
  return `验证连续成功 ${fmtInt(success)} 次`;
}

function renderProviders(data) {
  const wrap = ui.providersWrap;
  if (!wrap) return;
  clearNode(wrap);
  const providers = data ? data.providers : [];

  if (ui.providersCount) {
    ui.providersCount.textContent = data ? String(providers.length) : "—";
  }

  if (!data) {
    wrap.appendChild(el("div", "lm-empty", "尚未获取到模型统计数据。"));
    return;
  }
  if (!providers.length) {
    const empty = el("div", "lm-empty");
    empty.appendChild(el("strong", null, "没有可展示的模型"));
    empty.appendChild(
      el(
        "span",
        null,
        "可能原因：尚未产生任何调用、全部模型都未初始化，或插件未接管任何 provider。产生一次对话后刷新本页。",
      ),
    );
    wrap.appendChild(empty);
    return;
  }

  const rows = filteredProviders(data);
  if (!rows.length) {
    wrap.appendChild(el("div", "lm-empty", "没有匹配当前筛选条件的模型。"));
    return;
  }

  const showActions = pageState.allowActions;
  const scroll = el("div", "lm-table-scroll");
  const table = el("table", "lm-table");
  const thead = el("thead");
  const headRow = el("tr");
  const headers = ["模型 ID", "模型名", "调用", "成功率", "平均延迟", "最近错误类别", "状态"];
  if (showActions) headers.push("操作");
  for (const title of headers) headRow.appendChild(el("th", null, title));
  thead.appendChild(headRow);
  table.appendChild(thead);

  const tbody = el("tbody");
  for (const item of rows) {
    const row = el("tr");

    const idCell = el("td", "lm-mono");
    idCell.textContent = item.id;
    row.appendChild(idCell);

    row.appendChild(el("td", "lm-mono", item.model || "—"));

    const callsCell = el("td", "lm-num");
    callsCell.textContent = fmtInt(item.calls);
    if (item.consecutiveErrors) {
      callsCell.appendChild(
        el("span", "lm-cell-note", `连续失败 ${fmtInt(item.consecutiveErrors)} 次`),
      );
    }
    row.appendChild(callsCell);

    row.appendChild(el("td", "lm-num", fmtPercent(item.successRate)));

    const latencyCell = el("td", "lm-num");
    latencyCell.textContent = fmtMs(item.latencyAvg);
    if (item.latencyP95 !== null) {
      latencyCell.appendChild(el("span", "lm-cell-note", `p95 ${fmtMs(item.latencyP95)}`));
    }
    row.appendChild(latencyCell);

    const errorCell = el("td", "lm-mono");
    if (item.errorClass) {
      errorCell.textContent = item.errorClass;
      // 鼠标悬停给出各错误类别的次数与最近错误摘要（都用 textContent/title，不拼 HTML）。
      const classCounts = Object.keys(item.errorClasses || {})
        .map((name) => `${name}×${item.errorClasses[name]}`)
        .join("，");
      const titleParts = [classCounts, item.errorMessage].filter(Boolean);
      if (titleParts.length) errorCell.title = titleParts.join(" | ");
    } else {
      errorCell.textContent = "—";
    }
    row.appendChild(errorCell);

    const stateCell = el("td");
    stateCell.appendChild(markBadge(item.state));
    const successNote = statusSuccessNote(item, data.config);
    if (successNote) stateCell.appendChild(el("span", "lm-cell-note", successNote));
    row.appendChild(stateCell);

    if (showActions) {
      const actionCell = el("td");
      const group = el("div", "lm-row-actions");
      group.appendChild(actionButton("测活", "probe", item.id));
      if (item.marked) {
        group.appendChild(actionButton("解除标记", "unmark", item.id, "lm-btn--danger"));
      } else {
        group.appendChild(actionButton("标记", "mark", item.id, "lm-btn--warn"));
      }
      actionCell.appendChild(group);
      row.appendChild(actionCell);
    }

    tbody.appendChild(row);
  }
  table.appendChild(tbody);
  scroll.appendChild(table);
  wrap.appendChild(scroll);
}

/* ---------- 渲染：最近事件 ---------- */

function renderEvents() {
  if (!ui.eventsPanel || !ui.eventsWrap) return;
  // 总览都没拿到时先整块隐藏，避免只显示一个永远停在「正在加载」的空壳。
  if (pageState.eventsUnsupported || !pageState.overview) {
    ui.eventsPanel.hidden = true;
    return;
  }
  ui.eventsPanel.hidden = false;
  clearNode(ui.eventsWrap);

  const all = pageState.events;
  if (all === undefined) {
    if (ui.eventsCount) ui.eventsCount.textContent = "—";
    ui.eventsWrap.appendChild(el("div", "lm-empty", "正在加载最近事件…"));
    return;
  }
  if (all === null) {
    if (ui.eventsCount) ui.eventsCount.textContent = "—";
    ui.eventsWrap.appendChild(el("div", "lm-empty", "最近事件加载失败，稍后重试。"));
    return;
  }
  if (!all.length) {
    if (ui.eventsCount) ui.eventsCount.textContent = "0";
    ui.eventsWrap.appendChild(el("div", "lm-empty", "暂无调用事件记录。"));
    return;
  }

  const filter = pageState.eventProviderFilter;
  const rows = filter
    ? all.filter((event) => {
        const ids = [
          pick(event, ["requested", "requested_provider"]),
          pick(event, ["used", "used_provider"]),
          pick(event, ["provider_id"]),
        ]
          .filter(Boolean)
          .map(String);
        return ids.includes(filter);
      })
    : all;

  if (ui.eventsCount) ui.eventsCount.textContent = String(rows.length);
  if (!rows.length) {
    ui.eventsWrap.appendChild(el("div", "lm-empty", "当前筛选条件下没有事件。"));
    return;
  }

  const scroll = el("div", "lm-table-scroll");
  const table = el("table", "lm-table");
  const thead = el("thead");
  const headRow = el("tr");
  for (const title of ["时间", "请求模型", "实际使用", "结果", "总耗时", "错误类别"]) {
    headRow.appendChild(el("th", null, title));
  }
  thead.appendChild(headRow);
  table.appendChild(thead);

  const tbody = el("tbody");
  for (const event of rows) {
    const row = el("tr");
    row.appendChild(el("td", "lm-num", fmtTime(pick(event, ["ts", "time", "at", "timestamp"]))));
    row.appendChild(
      el("td", "lm-mono", String(pick(event, ["requested", "requested_provider"]) || "—")),
    );
    row.appendChild(el("td", "lm-mono", String(pick(event, ["used", "used_provider"]) || "—")));

    const okFlag = asBool(pick(event, ["ok", "success"]));
    const resultCell = el("td");
    resultCell.appendChild(
      okFlag === true
        ? el("span", "lm-badge lm-badge--ok", "成功")
        : el("span", "lm-badge lm-badge--danger", "失败"),
    );
    const attempt = pick(event, ["attempt_index", "attempt"]);
    if (attempt !== undefined) {
      resultCell.appendChild(
        el("span", "lm-cell-note", `第 ${fmtInt(attempt)} 次尝试`),
      );
    }
    row.appendChild(resultCell);

    row.appendChild(el("td", "lm-num", fmtMs(pick(event, ["total_ms", "latency_ms", "duration_ms"]))));

    const errorCell = el("td", "lm-mono");
    const errorSummary = [
      pick(event, ["error_class", "class"]),
      pick(event, ["error_type"]),
      pick(event, ["http_status", "status_code"]),
    ]
      .filter(Boolean)
      .map(String)
      .join(" · ");
    errorCell.textContent = errorSummary || "—";
    row.appendChild(errorCell);

    tbody.appendChild(row);
  }
  table.appendChild(tbody);
  scroll.appendChild(table);
  ui.eventsWrap.appendChild(scroll);
}

function syncEventFilterOptions(data) {
  if (!ui.eventProviderFilter) return;
  const current = pageState.eventProviderFilter;
  const ids = new Set();
  if (data) {
    for (const item of data.providers) ids.add(item.id);
    for (const mark of data.marks) {
      const id = providerIdOf(mark);
      if (id) ids.add(id);
    }
  }
  clearNode(ui.eventProviderFilter);
  const allOption = el("option", null, "全部");
  allOption.value = "";
  ui.eventProviderFilter.appendChild(allOption);
  for (const id of Array.from(ids).sort()) {
    const option = el("option", null, id);
    option.value = id;
    ui.eventProviderFilter.appendChild(option);
  }
  ui.eventProviderFilter.value = ids.has(current) ? current : "";
  pageState.eventProviderFilter = ui.eventProviderFilter.value;
}

/* ---------- 总渲染 ---------- */

function renderAll() {
  const data = pageState.overview;
  renderSummary(data);
  renderCards(data);
  renderChains();
  renderProviders(data);
  syncEventFilterOptions(data);
  renderEvents();
  renderActionHint(data);
  renderNotices(data);
}

function renderActionHint(data) {
  if (!ui.actionHint) return;
  if (!data) {
    ui.actionHint.hidden = true;
    return;
  }
  if (pageState.allowActions) {
    ui.actionHint.hidden = true;
    return;
  }
  ui.actionHint.hidden = false;
  ui.actionHint.textContent =
    "写操作已关闭，页面以只读模式展示。需要测活/标记操作时，请在插件配置里打开 allow_webui_actions，或使用管理员命令 /llmguard。";
}

function renderNotices(data) {
  if (!data) return;
  if (data.providerManager === false) {
    setBanner(
      "providerManager",
      "error",
      "未获取到 provider_manager",
      "插件无法读取模型列表，当前不接管任何模型；页面只展示历史统计。请检查 AstrBot 日志中的加载顺序。",
    );
  } else {
    clearBanner("providerManager");
  }
  if (data.enabled === false) {
    setBanner("enabled", "warn", "插件已禁用", "当前 enable=false，插件不接管模型选择；本页数据为历史统计。");
  } else {
    clearBanner("enabled");
  }
  if (data.stateReset) {
    setBanner(
      "stateReset",
      "warn",
      "状态已重置",
      "state.json 解析失败或版本过新，已备份并以空状态运行，历史标记信息可能丢失。",
    );
  } else {
    clearBanner("stateReset");
  }
  if (data.lastError) {
    setBanner("lastError", "error", "最近内部错误", String(data.lastError));
  } else {
    clearBanner("lastError");
  }
  if (data.internalErrors !== null && data.internalErrors > 0) {
    setBanner(
      "internalErrors",
      "warn",
      "包装层内部异常",
      `已发生 ${fmtInt(data.internalErrors)} 次内部异常（每次都已回退为直接调用，对话不受影响），建议查看 AstrBot 日志定位。`,
    );
  } else {
    clearBanner("internalErrors");
  }
}

/* ---------- 数据加载 ---------- */

function setBusy(button, busy) {
  if (!button) return;
  if (busy) {
    if (button.dataset.idleLabel === undefined) {
      button.dataset.idleLabel = button.textContent;
    }
    button.disabled = true;
    button.classList.add("lm-btn--busy");
    button.textContent = "刷新中";
  } else {
    button.disabled = false;
    button.classList.remove("lm-btn--busy");
    button.textContent =
      button.dataset.idleLabel === undefined ? button.textContent : button.dataset.idleLabel;
  }
}

async function refreshAll() {
  if (!pageState.bridgeReady || pageState.loading) return;
  pageState.loading = true;
  setBusy(ui.refreshButton, true);
  try {
    const raw = await apiGet("overview");
    pageState.overview = normalizeOverview(raw);
    pageState.allowActions = pageState.overview.allowActions;
    pageState.lastLoadedAt = new Date();
    pageState.loadError = null;
    clearBanner("load");
    if (ui.reloadButton) ui.reloadButton.hidden = !pageState.allowActions;
    renderAll();
    loadEvents();
  } catch (error) {
    pageState.loadError = errorText(error);
    setBanner(
      "load",
      "error",
      "数据加载失败",
      `${pageState.loadError}。已保留上一次成功加载的数据，请检查后端接口与 AstrBot 日志。`,
    );
    renderAll();
  } finally {
    pageState.loading = false;
    setBusy(ui.refreshButton, false);
    renderSummary(pageState.overview);
    // 链路区独立加载：即使 overview 失败，它也能给出自己的提示。
    loadChains();
  }
}

/** GET /chains：独立容错，失败不影响其它区块。 */
async function loadChains() {
  // 已确认后端没有该端点时不再每轮轮询，避免每次刷新都打一次 404。
  if (pageState.chainsUnsupported) return;
  try {
    const raw = await apiGet("chains");
    pageState.chains = normalizeChains(raw);
    pageState.chainsUnsupported = false;
    pageState.chainsError = "";
  } catch (error) {
    if (isRouteMissing(error)) {
      // 后端尚未注册 /chains：若 overview 里已带同一份链路就直接用，
      // 否则显示「未提供」，不当作错误。
      const fallback = pageState.overview ? pageState.overview.chains : null;
      if (fallback && fallback.length) {
        pageState.chains = fallback;
        pageState.chainsUnsupported = false;
        pageState.chainsError = "";
      } else {
        pageState.chainsUnsupported = true;
        pageState.chains = null;
        pageState.chainsError = "";
      }
    } else {
      pageState.chains = null;
      pageState.chainsError = errorText(error);
    }
  }
  renderChains();
}

async function loadEvents() {
  if (pageState.eventsUnsupported) return;
  try {
    const raw = await apiGet("events", { limit: EVENTS_LIMIT });
    pageState.events = normalizeEvents(raw);
    renderEvents();
  } catch (error) {
    if (isRouteMissing(error)) {
      // 后端未提供 events 端点：整块隐藏，不刷错误提示。
      pageState.eventsUnsupported = true;
      pageState.events = null;
      renderEvents();
      return;
    }
    pageState.events = null;
    renderEvents();
  }
}

/* ---------- 写操作 ---------- */

function markActionKey(providerId, action) {
  return `${providerId}::${action}`;
}

function setActionBusy(providerId, action, busy) {
  const key = markActionKey(providerId, action);
  if (busy) pageState.busy.add(key);
  else pageState.busy.delete(key);
  if (!ui.providersWrap) return;
  // 不拼接选择器：provider id 可能含引号等字符，直接比对 dataset 更安全。
  const buttons = Array.from(ui.providersWrap.querySelectorAll("button[data-action]"));
  for (const button of buttons) {
    if (button.dataset.providerId !== providerId || button.dataset.action !== action) {
      continue;
    }
    if (busy) {
      button.disabled = true;
      if (button.dataset.idleLabel === undefined) {
        button.dataset.idleLabel = button.textContent;
      }
      button.textContent = "处理中";
    } else {
      button.disabled = false;
      button.textContent =
        button.dataset.idleLabel === undefined ? button.textContent : button.dataset.idleLabel;
    }
  }
}

function describeAction(action, result, providerId) {
  const data = result && typeof result === "object" ? result : {};
  if (action === "probe") {
    const okFlag = asBool(pick(data, ["ok", "success"]));
    const latency = pick(data, ["latency_ms", "total_ms"]);
    const errorClass = pick(data, ["error_class", "class"]);
    const parts = [
      okFlag === true ? "测活成功" : okFlag === false ? "测活失败" : "测活已执行",
      providerId,
    ];
    if (latency !== undefined) parts.push(`耗时 ${fmtMs(latency)}`);
    if (okFlag === false && errorClass) parts.push(`分类 ${errorClass}`);
    return parts.join(" · ");
  }
  if (action === "mark") {
    const marked = asBool(pick(data, ["marked"]));
    return marked === false ? `标记未生效：${providerId}` : `已标记 ${providerId}`;
  }
  if (action === "unmark") {
    const unmarked = asBool(pick(data, ["unmarked"]));
    return unmarked === false ? `${providerId} 未被标记` : `已解除标记 ${providerId}`;
  }
  if (action === "reload") {
    return "配置已重载";
  }
  return `${providerId} 操作完成`;
}

async function runProviderAction(providerId, action, button) {
  if (!pageState.allowActions) {
    pushToast("error", "写操作已在插件配置中关闭（allow_webui_actions=false）");
    return;
  }
  if (pageState.busy.has(markActionKey(providerId, action))) return;

  const body = { provider_id: providerId };
  if (action === "mark") {
    const form = await openMarkModal(providerId);
    if (!form) return;
    if (form.reason) body.reason = form.reason;
  }

  setActionBusy(providerId, action, true);
  if (button) button.classList.add("lm-btn--busy");
  try {
    // 直接 POST 具体端点（probe/mark/unmark）；失败时把后端错误文本原样展示。
    const result = await apiPost(action, body);
    pushToast("ok", describeAction(action, result, providerId));
    await refreshAll();
  } catch (error) {
    pushToast("error", `操作失败：${errorText(error)}`);
  } finally {
    setActionBusy(providerId, action, false);
    if (button) button.classList.remove("lm-btn--busy");
  }
}

async function runReload() {
  if (!pageState.allowActions) {
    pushToast("error", "写操作已在插件配置中关闭（allow_webui_actions=false）");
    return;
  }
  if (!ui.reloadButton || ui.reloadButton.disabled) return;
  ui.reloadButton.disabled = true;
  const idleLabel = ui.reloadButton.dataset.idleLabel || ui.reloadButton.textContent;
  ui.reloadButton.dataset.idleLabel = idleLabel;
  ui.reloadButton.textContent = "重载中";
  try {
    // reload 端点若后端未注册会直接 404，此时把错误文本交给用户。
    await apiPost("reload", {});
    pushToast("ok", "配置已重载");
    await refreshAll();
  } catch (error) {
    pushToast("error", `重载失败：${errorText(error)}`);
  } finally {
    ui.reloadButton.disabled = false;
    ui.reloadButton.textContent = idleLabel;
  }
}

/* ---------- 标记弹窗 ---------- */

let modalResolve = null;

function openMarkModal(providerId) {
  if (!ui.modal) return Promise.resolve(null);
  return new Promise((resolve) => {
    modalResolve = resolve;
    if (ui.modalDesc) {
      ui.modalDesc.textContent = `为模型 ${providerId} 手动打上标记。标记后请求会在链路里跳过它，直到验证连续成功达到阈值后自动解除。`;
    }
    if (ui.markReason) ui.markReason.value = "";
    ui.modal.hidden = false;
    focusIfPossible(ui.markReason);
  });
}

function closeModal(result) {
  if (ui.modal) ui.modal.hidden = true;
  const resolve = modalResolve;
  modalResolve = null;
  if (resolve) resolve(result);
}

function confirmModal() {
  const reason = ui.markReason ? ui.markReason.value.trim() : "";
  closeModal({ reason });
}

/* ---------- 自动刷新 ---------- */

function stopAutoRefresh() {
  if (pageState.timerId !== null) {
    window.clearInterval(pageState.timerId);
    pageState.timerId = null;
  }
}

function startAutoRefresh() {
  stopAutoRefresh();
  if (!pageState.autoRefresh) return;
  pageState.timerId = window.setInterval(() => {
    if (document.visibilityState === "hidden") return;
    if (!pageState.bridgeReady || pageState.loading) return;
    refreshAll();
  }, pageState.intervalMs);
}

/* ---------- 主题与上下文 ---------- */

function applyTheme(isDark) {
  if (typeof isDark !== "boolean") return;
  document.documentElement.setAttribute("data-theme", isDark ? "dark" : "light");
  pageState.themeLocked = true;
}

function applyInitialTheme() {
  const root = document.documentElement;
  if (root.getAttribute("data-theme")) {
    pageState.themeLocked = true;
    return;
  }
  const media = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
  applyTheme(Boolean(media && media.matches));
  pageState.themeLocked = false;
  if (media && typeof media.addEventListener === "function") {
    media.addEventListener("change", (event) => {
      if (pageState.themeLocked) return;
      applyTheme(event.matches);
      pageState.themeLocked = false;
    });
  }
}

function applyContext(context) {
  if (!context || typeof context !== "object") return;
  if (typeof context.isDark === "boolean") applyTheme(context.isDark);
}

/* ---------- 事件绑定 ---------- */

function cacheDom() {
  ui.banners = $("lmBanners");
  ui.cards = $("lmCards");
  ui.enableBadge = $("lmEnableBadge");
  ui.configSummary = $("lmConfigSummary");
  ui.refreshButton = $("lmRefreshBtn");
  ui.reloadButton = $("lmReloadBtn");
  ui.autoToggle = $("lmAutoToggle");
  ui.intervalSelect = $("lmIntervalSelect");
  ui.chainsWrap = $("lmChainsWrap");
  ui.chainsCount = $("lmChainsCount");
  ui.chainsHint = $("lmChainsHint");
  ui.providersWrap = $("lmProvidersWrap");
  ui.providersCount = $("lmProvidersCount");
  ui.providerFilter = $("lmProviderFilter");
  ui.sortErrors = $("lmSortErrors");
  ui.sortLatency = $("lmSortLatency");
  ui.actionHint = $("lmActionHint");
  ui.eventsPanel = $("lmEventsPanel");
  ui.eventsWrap = $("lmEventsWrap");
  ui.eventsCount = $("lmEventsCount");
  ui.eventProviderFilter = $("lmEventProviderFilter");
  ui.toasts = $("lmToastLayer");
  ui.modal = $("lmModal");
  ui.modalTitle = $("lmModalTitle");
  ui.modalDesc = $("lmModalDesc");
  ui.markReason = $("lmMarkReason");
  ui.modalConfirm = $("lmModalConfirm");
  ui.modalCancel = $("lmModalCancel");
}

function setSort(key) {
  pageState.sortKey = key;
  if (ui.sortErrors) {
    ui.sortErrors.setAttribute("aria-pressed", key === "errors" ? "true" : "false");
  }
  if (ui.sortLatency) {
    ui.sortLatency.setAttribute("aria-pressed", key === "latency" ? "true" : "false");
  }
  renderProviders(pageState.overview);
}

function bindEvents() {
  if (ui.refreshButton) {
    ui.refreshButton.addEventListener("click", () => {
      refreshAll();
    });
  }
  if (ui.reloadButton) {
    ui.reloadButton.addEventListener("click", () => {
      runReload();
    });
  }
  if (ui.autoToggle) {
    ui.autoToggle.addEventListener("change", () => {
      pageState.autoRefresh = ui.autoToggle.checked;
      startAutoRefresh();
    });
  }
  if (ui.intervalSelect) {
    ui.intervalSelect.addEventListener("change", () => {
      const parsed = Number(ui.intervalSelect.value);
      if (Number.isFinite(parsed) && parsed >= 1000) {
        pageState.intervalMs = parsed;
        startAutoRefresh();
      }
    });
  }
  if (ui.providerFilter) {
    ui.providerFilter.addEventListener("input", () => {
      pageState.providerFilter = ui.providerFilter.value || "";
      renderProviders(pageState.overview);
    });
  }
  if (ui.sortErrors) {
    ui.sortErrors.addEventListener("click", () => setSort("errors"));
  }
  if (ui.sortLatency) {
    ui.sortLatency.addEventListener("click", () => setSort("latency"));
  }
  if (ui.eventProviderFilter) {
    ui.eventProviderFilter.addEventListener("change", () => {
      pageState.eventProviderFilter = ui.eventProviderFilter.value || "";
      renderEvents();
    });
  }
  if (ui.modalConfirm) ui.modalConfirm.addEventListener("click", confirmModal);
  if (ui.modalCancel) ui.modalCancel.addEventListener("click", () => closeModal(null));
  if (ui.modal) {
    ui.modal.addEventListener("click", (event) => {
      if (event.target === ui.modal) closeModal(null);
    });
  }

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && ui.modal && !ui.modal.hidden) closeModal(null);
  });

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && pageState.bridgeReady) {
      refreshAll();
    }
  });
}

/* ---------- 启动 ---------- */

function showBridgeMissing() {
  setBanner(
    "bridge",
    "warn",
    "未检测到 AstrBot 页面桥接",
    "当前页面没有拿到 window.AstrBotPluginPage。请从 AstrBot WebUI「插件」页进入本页面；直接双击打开 index.html 无法加载数据。",
  );
  if (ui.refreshButton) ui.refreshButton.disabled = true;
  if (ui.autoToggle) ui.autoToggle.disabled = true;
  const message = "无法连接 AstrBot（缺少页面桥接），请在 WebUI 中打开本页面。";
  for (const wrap of [ui.chainsWrap, ui.providersWrap]) {
    if (!wrap) continue;
    clearNode(wrap);
    wrap.appendChild(el("div", "lm-empty", message));
  }
  if (ui.eventsPanel) ui.eventsPanel.hidden = true;
}

async function waitForBridge() {
  try {
    await withTimeout(
      Promise.resolve(bridge.ready()),
      READY_TIMEOUT_MS,
      "等待 AstrBot WebUI 上下文超时",
    );
    pageState.bridgeReady = true;
    clearBanner("bridge");
  } catch (error) {
    pageState.bridgeReady = false;
    setBanner(
      "bridge",
      "warn",
      "等待 WebUI 上下文超时",
      `${errorText(error)}。如果你是在浏览器里直接打开本页面，请改从 AstrBot WebUI 的插件页面进入。`,
    );
    return;
  }

  if (typeof bridge.onContext === "function") {
    bridge.onContext((context) => {
      applyContext(context);
    });
  }
  if (typeof bridge.getContext === "function") {
    applyContext(bridge.getContext());
  }
  await refreshAll();
}

function boot() {
  cacheDom();
  applyInitialTheme();
  bindEvents();
  startAutoRefresh();
  if (!bridge) {
    showBridgeMissing();
    return;
  }
  waitForBridge();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot, { once: true });
} else {
  boot();
}

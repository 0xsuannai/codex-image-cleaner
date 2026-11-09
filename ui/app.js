"use strict";

const $ = (id) => document.getElementById(id);
const token =
  new URLSearchParams(location.hash.slice(1)).get("token") ||
  sessionStorage.getItem("cleaner-token") ||
  "";
if (token) sessionStorage.setItem("cleaner-token", token);
history.replaceState(null, "", "/");
let state = { sessions: [], busy: "", revision: -1, report: null };
let selected = new Set();
let connected = false;
let pageSize = 60;
let challenge = null;
let submitting = false;
let lastReport = "";
let detailTitle = "";
let toastTimer;
let exiting = false;
let pollTimer;
let polling = false;
const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");
if (navigator.platform.includes("Mac")) document.querySelector(".search kbd").textContent = "⌘ K";

function feedback(node, frames, duration = 220) {
  if (!node || reducedMotion.matches || document.hidden) return;
  node.animate(frames, { duration, easing: "cubic-bezier(.2,.7,.3,1)" });
}
reducedMotion.addEventListener("change", () => {
  if (reducedMotion.matches) document.getAnimations().forEach((animation) => animation.cancel());
});
function syncMotionVisibility() {
  document.documentElement.dataset.motionPaused = String(document.hidden);
}
document.addEventListener("visibilitychange", syncMotionVisibility);
syncMotionVisibility();

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", "#i-" + name);
  svg.setAttribute("aria-hidden", "true");
  svg.append(use);
  return svg;
}
function toast(message) {
  clearTimeout(toastTimer);
  $("toast").textContent = message;
  $("toast").hidden = false;
  toastTimer = setTimeout(() => {
    $("toast").hidden = true;
  }, 5000);
}
async function api(path, body) {
  const response = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers: {
      Authorization: "Bearer " + token,
      ...(body === undefined ? {} : { "Content-Type": "application/json" }),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store",
    signal: AbortSignal.timeout(20000),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "操作未完成，请重试");
  return data;
}
function filtered() {
  const query = $("search").value.trim().toLocaleLowerCase();
  return state.sessions
    .filter((info) => {
      if ($("images-only").checked && info.scanned && !info.eligible && !info.error && !info.recovery_backup) return false;
      return (
        !query ||
        [info.title, info.preview, info.cwd, info.thread_id]
          .join(" ")
          .toLocaleLowerCase()
          .includes(query)
      );
    })
    .sort((a, b) => b.modified - a.modified);
}
function chosen() {
  return state.sessions.filter((info) => selected.has(info.key));
}
function timestamp(value) {
  return value
    ? new Date(value * 1000).toLocaleString("zh-CN", {
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      })
    : "暂无时间记录";
}
function bytes(value) {
  for (const unit of ["B", "KB", "MB", "GB"]) {
    if (value < 1024 || unit === "GB") return value.toFixed(1) + " " + unit;
    value /= 1024;
  }
}
function select(key, checked) {
  if (checked) selected.add(key);
  else selected.delete(key);
  renderList();
  if (checked) {
    const row = [...$("session-list").children].find((item) => item.dataset.key === key);
    if (row)
      feedback(
        row,
        [
          { backgroundColor: "#d5ebdc", boxShadow: "inset 2px 0 #438261" },
          {
            backgroundColor: getComputedStyle(row).backgroundColor,
            boxShadow: "inset 2px 0 transparent",
          },
        ],
        260,
      );
  }
}
function renderList() {
  const items = filtered();
  const list = $("session-list");
  const scroll = list.scrollTop;
  const focused = list.contains(document.activeElement)
    ? { key: document.activeElement.closest(".session-row")?.dataset.key,
        selector: document.activeElement.matches("input") ? "input" : ".row-detail" }
    : null;
  const fragment = document.createDocumentFragment();
  items.slice(0, pageSize).forEach((info) => {
    const row = element("div", "session-row" + (selected.has(info.key) ? " selected" : ""));
    row.dataset.key = info.key;
    const checkbox = element("input");
    checkbox.type = "checkbox";
    checkbox.checked = selected.has(info.key);
    checkbox.setAttribute("aria-label", "选择会话：" + info.title);
    checkbox.addEventListener("click", (event) => event.stopPropagation());
    checkbox.addEventListener("change", () => select(info.key, checkbox.checked));
    const thumbnail = element("span", "session-icon" + (info.eligible ? " has-images" : ""));
    thumbnail.append(icon(info.eligible ? "image" : "file"));
    const copy = element("div", "session-copy");
    const title = element("span", "session-title", info.title || "未命名会话");
    title.title = info.title;
    copy.append(title);
    const message = info.preview?.replace(/\s+/g, " ").trim();
    const workspace = info.cwd?.split(/[\\/]/).filter(Boolean).pop();
    const preview = info.recovery_backup
      ? "上次清理中断，请先单独选择并修复"
      : info.error || (message && message !== info.title ? message : "") ||
        (workspace ? "工作区 · " + workspace : "暂无消息预览");
    const subtitle = element("span", "session-preview", preview);
    subtitle.title = preview;
    copy.append(subtitle);
    const modified = element("span", "session-time", timestamp(info.modified));
    const badge = element("span", "session-badge");
    if (info.recovery_backup) {
      badge.textContent = "待修复";
      badge.classList.add("warning");
    } else if (info.error) {
      badge.textContent = "无法读取";
      badge.classList.add("warning");
    } else if (!info.scanned) {
      badge.textContent = "检查中";
      badge.classList.add("muted");
    } else if (info.eligible) {
      badge.textContent = "有图片";
      badge.title = "图片上下文约 " + bytes((info.saving || 0) + (info.cache_saving || 0));
    } else {
      badge.textContent = "纯文字";
      badge.classList.add("muted");
    }
    const details = element("button", "row-detail", "›");
    details.setAttribute("aria-label", "查看会话详情：" + info.title);
    details.addEventListener("click", (event) => {
      event.stopPropagation();
      showDetails(info);
    });
    row.append(checkbox, thumbnail, copy, modified, badge, details);
    row.addEventListener("click", (event) => {
      if (event.detail > 1) return;
      select(info.key, !selected.has(info.key));
    });
    row.addEventListener("dblclick", () => showDetails(info));
    fragment.append(row);
  });
  list.replaceChildren(fragment);
  list.hidden = items.length === 0;
  if (focused) {
    [...list.children].find((row) => row.dataset.key === focused.key)
      ?.querySelector(focused.selector)?.focus({ preventScroll: true });
  }
  list.scrollTop = scroll;
  $("session-count").textContent = state.sessions.length;
  const eligible = state.sessions.filter((info) => info.eligible).length;
  const pending = state.sessions.filter((info) => !info.scanned).length;
  $("eligible-count").textContent = eligible;
  $("scan-summary").textContent = pending
    ? pending + " 个会话待检查"
    : "共 " + state.sessions.length + " 个本地会话";
  $("list-summary").textContent =
    items.length + " 个会话 · " + items.filter((info) => info.eligible).length + " 个可清理图片";
  $("select-images").textContent = selected.size ? "清除全部选择" : "选择有图片的会话";
  $("load-more").hidden = items.length <= pageSize;
  renderAction(items);
}
function renderEmpty(items) {
  const hasFilter = !!$("search").value.trim() || $("images-only").checked;
  const empty = !items.length;
  $("empty").hidden = !empty;
  $("reset-filters").hidden = !empty || !hasFilter;
  $("choose-directory").hidden = !empty || hasFilter || !connected || !!state.busy;
  if (!connected) {
    $("empty-title").textContent = token ? "等待连接本地工具" : "请从启动工具打开";
    $("empty-text").textContent = token
      ? "连接恢复后，会话会自动显示。"
      : "双击“启动工具.cmd”或运行应用，即可读取本地会话。";
  } else if (state.busy === "scan") {
    $("empty-title").textContent = "正在查找本地会话";
    $("empty-text").textContent = "会话名称会先显示，图片检查在后台继续。";
  } else {
    $("empty-title").textContent = hasFilter ? "没有匹配的会话" : "还没有找到本地会话";
    $("empty-text").textContent = hasFilter
      ? "换个关键词，或清除筛选后查看全部会话。"
      : "检查是否选对了 Codex 会话目录，通常是用户目录下的 .codex。";
  }
}
function renderAction(visibleItems = filtered()) {
  renderEmpty(visibleItems);
  document.documentElement.dataset.operation = state.busy || "idle";
  $("clean").dataset.working = String(state.busy === "clean" || state.busy === "restore");
  document.querySelector(".sessions-panel").setAttribute("aria-busy", String(!!state.busy));
  const items = chosen();
  const cleanable = items.filter((info) => info.eligible && !info.recovery_backup);
  const saving = cleanable.reduce((total, info) => total + (info.saving || 0) + (info.cache_saving || 0), 0);
  $("selection-saving").hidden = !saving;
  $("selection-saving").replaceChildren(
    element("small", "", "图片上下文约减少"),
    element("strong", "", bytes(saving)),
  );
  const repair = items.length === 1 && items[0].recovery_backup;
  const needsRepair = items.some((info) => info.recovery_backup);
  const visibleKeys = new Set(visibleItems.slice(0, pageSize).map((info) => info.key));
  const hiddenCount = items.filter((info) => !visibleKeys.has(info.key)).length;
  $("clear-selection").hidden = !items.length;
  $("clear-selection").disabled = !!state.busy || submitting;
  const pending = items.some((info) => !info.scanned);
  $("selection-title").textContent = repair
    ? "上次操作中断，请先修复"
    : items.length
      ? "已选 " + items.length + " 个会话 · " + cleanable.length + " 个可清理"
      : "选择需要清理的会话";
  $("selection-title").title = items.map((info) => info.title).join("\n");
  let hint = "先关闭目标会话，清理完成后再重新打开。";
  if (repair) hint = "先恢复原状态。修复完成前，请勿重新打开这个会话。";
  else if (needsRepair) hint = "请先单独选择待修复会话，恢复原状态后再清理。";
  else if (pending) hint = "正在检查所选会话中的图片，请稍候。";
  else if (items.length && !cleanable.length)
    hint = items.some((info) => info.error)
      ? "会话暂时无法处理，可点击行末箭头查看原因。"
      : "没有可清理的图片。若仍重连或卡思考，请检查网络、代理或服务状态。";
  else if (cleanable.length) hint = "先关闭目标会话。工具会自动备份，清理后重新打开即可。";
  $("selection-hint").textContent = (hiddenCount ? hiddenCount + " 个已选会话不在当前列表中。" : "") + hint;
  $("clean").disabled =
    !connected || !!state.busy || submitting || (!repair && (needsRepair || !cleanable.length || pending));
  $("clean").querySelector("span").textContent =
    state.busy === "clean"
      ? "正在备份并清理…"
      : state.busy === "restore"
        ? "正在恢复图片…"
        : repair
          ? "修复上次中断"
          : cleanable.length > 1
            ? "清理 " + cleanable.length + " 个会话"
            : "清理会话图片";
  for (const id of [
    "refresh",
    "select-images",
    "open-backups",
    "open-settings",
    "export-diagnostics",
    "quit",
  ])
    $(id).disabled = !connected || !!state.busy;
  document.querySelectorAll("[data-action]").forEach((button) => {
    button.disabled = $(button.dataset.action).disabled;
  });
  $("stop").hidden = state.busy !== "scan";
  $("progress").hidden = !state.busy;
  $("progress").max = Math.max(1, state.progress?.[1] || 0);
  $("progress").value = state.progress?.[0] || 0;
}
function openDialog(id) {
  $("more-menu").open = false;
  $(id).showModal();
}
function showDetails(info) {
  detailTitle = info.title;
  $("copy-name").textContent = "复制名称";
  $("detail-title").textContent = info.title;
  $("detail-preview").textContent = info.error || info.preview || "暂无用户消息预览。";
  const meta = $("detail-meta");
  meta.replaceChildren();
  for (const [name, value] of [
    ["工作区", info.cwd || "未记录"],
    ["图片上下文约减少", bytes(info.saving + info.cache_saving)],
    ["会话 ID", info.thread_id],
    ["说明", "清理保留日志原有大小及历史位置，不用于释放磁盘空间。"],
  ])
    meta.append(element("dt", "", name), element("dd", "", value));
  openDialog("details-dialog");
}
async function prepare(body) {
  if (submitting || state.busy) return;
  submitting = true;
  renderAction();
  try {
    challenge = await api("/api/prepare", body);
    $("confirm-sessions").replaceChildren(
      ...challenge.titles.map((name) => element("li", "", name)),
    );
    $("confirm-title").textContent =
      challenge.kind === "clean" ? "清理前，先关闭目标会话" : "恢复前，先关闭目标会话";
    $("confirm-description").textContent =
      challenge.kind === "clean"
        ? "清理历史图片上下文，保留文字和原始图片文件。每个会话都会先备份。"
        : "恢复所选会话的图片和历史缓存。若会话已有新消息，会拒绝覆盖。";
    $("confirm-execute").textContent = challenge.kind === "clean" ? "备份并清理" : "恢复图片";
    $("confirm-error").textContent = "";
    document.querySelector('input[name="mode"][value="online"]').checked = true;
    $("confirm-dialog").querySelector("details").open = false;
    resetAcknowledgement();
    openDialog("confirm-dialog");
  } catch (error) {
    toast(error.message);
  } finally {
    submitting = false;
    renderAction();
  }
}
function resetAcknowledgement() {
  $("acknowledged").checked = false;
  $("confirm-execute").disabled = true;
  $("ack-text").textContent =
    document.querySelector('input[name="mode"]:checked').value === "online"
      ? "我已停止任务并关闭这些目标会话，操作期间不再打开。"
      : "我已完全退出 Codex，包括 IDE 中的 Codex 插件。";
}
async function poll() {
  if (exiting || polling) return;
  clearTimeout(pollTimer);
  polling = true;
  $("reconnect").disabled = true;
  try {
    if (!token) throw new Error("请通过启动工具打开界面");
    const update = await api("/api/state?revision=" + state.revision);
    connected = true;
    const changed = update.sessions !== undefined;
    state = { ...state, ...update };
    document.documentElement.dataset.connected = "true";
    $("connection-notice").hidden = true;
    $("version").textContent = "v" + state.version;
    $("status").textContent = state.status;
    if (changed) {
      const keys = new Set(state.sessions.map((info) => info.key));
      selected = new Set([...selected].filter((key) => keys.has(key)));
      renderList();
    }
    renderAction();
    if (state.report && JSON.stringify(state.report) !== lastReport) {
      lastReport = JSON.stringify(state.report);
      $("notice-title").textContent = state.report.message;
      $("notice-next").textContent = state.report.next_step || "可查看详情了解原因。";
      $("notice").classList.toggle(
        "warning",
        !!state.report.failed?.length || !!state.report.skipped?.length,
      );
      $("notice").hidden = false;
      feedback(
        $("notice"),
        [
          { opacity: 0.35, boxShadow: "inset 0 0 0 1px #a5c9b166" },
          { opacity: 1, boxShadow: "inset 0 0 0 1px transparent" },
        ],
        280,
      );
    }
  } catch (error) {
    connected = false;
    document.documentElement.dataset.connected = "false";
    $("connection-notice").hidden = false;
    $("connection-title").textContent = token ? "本地连接暂时中断" : "请从启动工具打开";
    $("connection-text").textContent = token
      ? "已保留你的选择，正在自动重连。若工具已退出，请重新启动。"
      : "此页面需要应用启动时提供的本地连接，请重新启动工具。";
    $("reconnect").hidden = !token;
    $("status").textContent = "本地连接已断开，请重新启动工具。";
    renderAction();
    if (!token) {
      $("empty").hidden = false;
      $("empty-title").textContent = error.message;
    }
  } finally {
    polling = false;
    $("reconnect").disabled = false;
    if (!exiting) pollTimer = setTimeout(poll, connected ? 850 : 3000);
  }
}
$("search").addEventListener("input", () => {
  pageSize = 60;
  renderList();
});
$("reconnect").addEventListener("click", poll);
$("clear-selection").addEventListener("click", () => {
  selected.clear();
  renderList();
});
$("images-only").addEventListener("change", () => {
  pageSize = 60;
  renderList();
});
$("reset-filters").addEventListener("click", () => {
  $("search").value = "";
  $("images-only").checked = false;
  pageSize = 60;
  renderList();
  $("search").focus();
});
$("load-more").addEventListener("click", () => {
  pageSize += 60;
  renderList();
});
$("select-images").addEventListener("click", () => {
  selected = selected.size
    ? new Set()
    : new Set(
        filtered()
          .filter((info) => info.eligible && !info.recovery_backup)
          .slice(0, 1000)
          .map((info) => info.key),
      );
  renderList();
});
$("clean").addEventListener("click", () => {
  const items = chosen();
  if (items.length === 1 && items[0].recovery_backup)
    prepare({ kind: "repair", key: items[0].key });
  else
    prepare({ kind: "clean", keys: items.filter((info) => info.eligible).map((info) => info.key) });
});
$("refresh").addEventListener("click", async () => {
  try {
    await api("/api/scan", {});
  } catch (error) {
    toast(error.message);
  }
});
$("stop").addEventListener("click", async () => {
  try {
    await api("/api/stop", {});
  } catch (error) {
    toast(error.message);
  }
});
$("acknowledged").addEventListener("change", () => {
  $("confirm-execute").disabled = !$("acknowledged").checked || submitting;
});
document
  .querySelectorAll('input[name="mode"]')
  .forEach((input) => input.addEventListener("change", resetAcknowledgement));
document.querySelectorAll("[data-close]").forEach((button) =>
  button.addEventListener("click", () => {
    if (!submitting) button.closest("dialog").close();
  }),
);
document.querySelectorAll("[data-action]").forEach((button) => {
  button.addEventListener("click", () => $(button.dataset.action).click());
});
$("confirm-dialog").addEventListener("cancel", (event) => {
  if (submitting) event.preventDefault();
});
$("confirm-execute").addEventListener("click", async () => {
  if (!challenge || submitting || !$("acknowledged").checked) return;
  submitting = true;
  $("confirm-execute").disabled = true;
  try {
    await api("/api/execute", {
      confirmation: challenge.confirmation,
      acknowledged: true,
      mode: document.querySelector('input[name="mode"]:checked').value,
    });
    state.busy = challenge.kind === "clean" ? "clean" : "restore";
    challenge = null;
    $("confirm-dialog").close();
    $("status").textContent = "正在处理，请稍候…";
  } catch (error) {
    $("confirm-error").textContent = error.message;
    resetAcknowledgement();
  } finally {
    submitting = false;
    renderAction();
  }
});
$("copy-name").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(detailTitle);
    $("copy-name").textContent = "已复制";
  } catch {
    toast("无法复制，可在上方选中名称后复制。");
  }
});
$("dismiss-notice").addEventListener("click", () => {
  $("notice").hidden = true;
});
$("show-report").addEventListener("click", () => {
  const report = state.report;
  if (!report) return;
  $("report-title").textContent = report.message;
  const content = $("report-content");
  content.replaceChildren();
  if (report.next_step) content.append(element("p", "", report.next_step));
  for (const [key, title] of [
    ["completed", "已完成"],
    ["skipped", "仍被占用，已跳过"],
    ["failed", "未完成"],
  ]) {
    if (report[key]?.length) content.append(element("h3", "", title));
    for (const item of report[key] || [])
      content.append(
        element("p", "", item.title),
        element("small", "", item.error || item.backup || ""),
      );
  }
  openDialog("report-dialog");
});
$("open-settings").addEventListener("click", () => {
  $("home-input").value = state.home;
  $("backups-input").value = state.backups;
  $("settings-error").textContent = "";
  openDialog("settings-dialog");
});
$("save-settings").addEventListener("click", async () => {
  $("save-settings").disabled = true;
  try {
    await api("/api/settings", {
      home: $("home-input").value.trim(),
      backups: $("backups-input").value.trim(),
    });
    $("settings-dialog").close();
    selected.clear();
  } catch (error) {
    $("settings-error").textContent = error.message;
  } finally {
    $("save-settings").disabled = false;
  }
});
$("open-backups").addEventListener("click", async () => {
  $("backup-list").replaceChildren(element("p", "fine-print", "正在读取备份…"));
  $("backups-error").textContent = "";
  openDialog("backups-dialog");
  try {
    const { backups } = await api("/api/backups");
    const list = $("backup-list");
    list.replaceChildren();
    if (!backups.length)
      list.append(element("p", "fine-print", "还没有备份，首次清理时会自动创建。"));
    for (const backup of backups) {
      const item = element("div", "backup-item");
      const copy = element("div");
      copy.append(
        element("strong", "", backup.title),
        element("small", "", timestamp(backup.modified)),
      );
      const button = element(
        "button",
        "secondary",
        backup.status === "restored" ? "已恢复" : "恢复图片",
      );
      button.disabled = !["cleaned", "prepared", "restoring"].includes(backup.status);
      button.addEventListener("click", () => {
        $("backups-dialog").close();
        prepare({ kind: "restore", key: backup.key });
      });
      item.append(copy, button);
      list.append(item);
    }
  } catch (error) {
    $("backups-error").textContent = error.message;
  }
});
$("export-diagnostics").addEventListener("click", async () => {
  $("more-menu").open = false;
  try {
    const report = await api("/api/diagnostics", {});
    const url = URL.createObjectURL(
      new Blob([JSON.stringify(report, null, 2)], { type: "application/json" }),
    );
    const link = element("a");
    link.href = url;
    link.download = "codex-cleaner-diagnostics.json";
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    toast("诊断已导出，不包含会话正文或本机路径。");
  } catch (error) {
    toast(error.message);
  }
});
$("quit").addEventListener("click", async () => {
  try {
    await api("/api/shutdown", {});
    exiting = true;
    connected = false;
    $("status").textContent = "工具已退出，可以关闭窗口。";
    renderAction();
    toast("工具已退出，可以关闭此窗口。");
  } catch (error) {
    toast(error.message);
  }
});
document.addEventListener("keydown", (event) => {
  if (
    (event.ctrlKey || event.metaKey) &&
    ["k", "f"].includes(event.key.toLowerCase()) &&
    !document.querySelector("dialog[open]")
  ) {
    event.preventDefault();
    $("search").focus();
  }
  if (event.key === "Escape" && !document.querySelector("dialog[open]")) {
    selected.clear();
    renderList();
  }
});
document.addEventListener("click", (event) => {
  if (!$("more-menu").contains(event.target)) $("more-menu").open = false;
});
window.addEventListener("beforeunload", (event) => {
  if (state.busy && state.busy !== "scan") {
    event.preventDefault();
    event.returnValue = "";
  }
});
renderList();
poll();

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
let deleteBackupChallenge = null;
let submitting = false;
let lastReport = "";
let detailTitle = "";
let toastTimer;
let exiting = false;
let pollTimer;
let polling = false;
const expandedGroups = new Set();
const collapsedGroups = new Set();
let visibleSessionKeys = new Set();
let visibleRowCount = 0;
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
function sessionHierarchy() {
  const byId = new Map(state.sessions.map((info) => [info.thread_id, info]));
  const byKey = new Map(state.sessions.map((info) => [info.key, info]));
  const parents = new Map();
  for (const info of state.sessions) {
    const parent = byId.get(info.parent_thread_id);
    if (info.is_subagent && parent && parent.key !== info.key) parents.set(info.key, parent.key);
  }
  // Malformed ancestry must never hide a session or recurse indefinitely.
  for (const info of state.sessions) {
    const seen = new Set([info.key]);
    let cursor = info.key;
    while (parents.has(cursor)) {
      cursor = parents.get(cursor);
      if (seen.has(cursor)) { parents.delete(info.key); break; }
      seen.add(cursor);
    }
  }
  const children = new Map();
  for (const info of state.sessions) {
    const parent = parents.get(info.key);
    if (!parent) continue;
    if (!children.has(parent)) children.set(parent, []);
    children.get(parent).push(info);
  }
  const order = state.sessions.filter((info) => !parents.has(info.key));
  for (let index = 0; index < order.length; index++) {
    for (const child of children.get(order[index].key) || []) order.push(child);
  }
  const totals = new Map();
  for (const info of order.reverse()) {
    const size = sizes(info);
    const total = { ...size, count: 1, selectedCount: Number(selected.has(info.key)),
      diskSize: info.size, cleaned: !!info.last_cleanup };
    for (const child of children.get(info.key) || []) {
      const subtotal = totals.get(child.key);
      total.before += subtotal.before;
      total.after = total.after === null || subtotal.after === null ? null : total.after + subtotal.after;
      total.count += subtotal.count;
      total.selectedCount += subtotal.selectedCount;
      total.diskSize += subtotal.diskSize;
      total.cleaned &&= subtotal.cleaned;
    }
    totals.set(info.key, total);
  }
  return { byKey, parents, children, totals };
}
function groupedRows(hierarchy) {
  const matches = filtered();
  const included = new Set(matches.map((info) => info.key));
  const { byKey, parents } = hierarchy;
  const reveal = new Set();
  const hasFilter = !!$("search").value.trim() || $("images-only").checked;
  for (const info of matches) {
    let cursor = info.key;
    while (parents.has(cursor)) {
      cursor = parents.get(cursor);
      included.add(cursor);
      if (hasFilter) reveal.add(cursor);
    }
  }
  const children = new Map();
  const roots = [], orphans = [];
  for (const key of included) {
    const info = byKey.get(key);
    const parent = parents.get(key);
    if (parent) {
      if (!children.has(parent)) children.set(parent, []);
      children.get(parent).push(info);
    } else if (info.is_subagent) orphans.push(info);
    else roots.push(info);
  }
  const latest = (a, b) => b.modified - a.modified;
  roots.sort(latest);
  orphans.sort(latest);
  for (const entries of children.values()) entries.sort(latest);
  const rows = [];
  function append(info, parentKey, depth) {
    const descendants = children.get(info.key) || [];
    const open = !collapsedGroups.has(info.key) && (expandedGroups.has(info.key) || reveal.has(info.key));
    rows.push({ info, parentKey, depth, childCount: descendants.length, open });
    if (open) for (const child of descendants) append(child, info.key, depth + 1);
  }
  for (const info of roots) append(info, "", 0);
  if (orphans.length) {
    const key = "orphan-subtasks";
    const open = !collapsedGroups.has(key) && (expandedGroups.has(key) || hasFilter);
    rows.push({ orphan: true, key, childCount: orphans.length, open });
    if (open) for (const info of orphans) append(info, key, 1);
  }
  return { rows, mainCount: roots.length, subCount: [...included].filter((key) => byKey.get(key).is_subagent).length };
}
function toggleGroup(key, open) {
  if (open) { expandedGroups.delete(key); collapsedGroups.add(key); }
  else { expandedGroups.add(key); collapsedGroups.delete(key); }
  renderList();
}
function chosen() {
  return state.sessions.filter((info) => selected.has(info.key));
}
function actionable(info) {
  return !info.error && !info.recovery_backup && (info.eligible || !info.scanned);
}
function sizes(info) {
  return {
    before: info.last_cleanup?.before_size ?? (info.scanned ? info.content_size : info.size),
    after: info.last_cleanup?.after_size ??
      (info.scanned ? Math.max(0, info.content_size - info.saving) : null),
  };
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
  if (!Number.isFinite(value)) return "—";
  for (const unit of ["B", "KB", "MB", "GB"]) {
    if (value < 1024 || unit === "GB") return value.toFixed(1) + " " + unit;
    value /= 1024;
  }
}
function select(key, checked) {
  const { children } = sessionHierarchy();
  const pending = [key];
  while (pending.length) {
    const current = pending.pop();
    if (checked) selected.add(current);
    else selected.delete(current);
    for (const child of children.get(current) || []) pending.push(child.key);
  }
  renderList();
  if (checked) {
    const row = [...$("session-list").querySelectorAll(".session-row")].find((item) => item.dataset.key === key);
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
  const hierarchy = sessionHierarchy();
  const view = groupedRows(hierarchy);
  visibleRowCount = view.rows.length;
  visibleSessionKeys = new Set();
  const list = $("session-list");
  const scroll = list.scrollTop;
  const focused = list.contains(document.activeElement)
    ? { key: document.activeElement.closest(".session-row")?.dataset.key,
        orphan: document.activeElement.id === "orphan-toggle",
        selector: document.activeElement.matches("input") ? "input" :
          document.activeElement.matches(".subtask-toggle") ? ".subtask-toggle" : ".row-detail" }
    : null;
  const fragment = document.createDocumentFragment();
  const containers = new Map([["", fragment]]);
  view.rows.slice(0, pageSize).forEach((entry) => {
    if (entry.orphan) {
      const section = element("div", "orphan-group");
      const toggle = element("button", "orphan-toggle", (entry.open ? "▾ " : "▸ ") + "未找到主任务的子任务（" + entry.childCount + "）");
      toggle.id = "orphan-toggle";
      toggle.setAttribute("aria-expanded", String(entry.open));
      toggle.addEventListener("click", () => toggleGroup(entry.key, entry.open));
      const children = element("div", "session-children");
      children.setAttribute("role", "group");
      children.setAttribute("aria-label", "未找到主任务的子任务");
      children.hidden = !entry.open;
      section.append(toggle, children);
      fragment.append(section);
      containers.set(entry.key, children);
      return;
    }
    const { info, parentKey, depth, childCount, open } = entry;
    const total = hierarchy.totals.get(info.key);
    const hasChildren = total.count > 1;
    const checked = total.selectedCount === total.count;
    visibleSessionKeys.add(info.key);
    const group = element("div", "session-group");
    const row = element("div", "session-row" + (depth ? " subtask-row" : "") + (selected.has(info.key) ? " selected" : ""));
    row.dataset.key = info.key;
    row.dataset.parent = parentKey;
    const checkbox = element("input");
    checkbox.type = "checkbox";
    checkbox.checked = checked;
    checkbox.indeterminate = total.selectedCount > 0 && !checked;
    checkbox.setAttribute("aria-label", "选择会话：" + info.title);
    checkbox.addEventListener("click", (event) => event.stopPropagation());
    checkbox.addEventListener("change", () => select(info.key, checkbox.checked));
    const copy = element("div", "session-copy");
    const title = element("span", "session-title", info.title || "未命名会话");
    title.title = info.title;
    copy.append(title);
    const workspace = info.cwd?.split(/[\\/]/).filter(Boolean).pop();
    const preview = info.recovery_backup
      ? "上次清理中断，请先单独选择并修复"
      : info.error || [workspace, timestamp(info.modified)].filter(Boolean).join(" · ");
    const subtitle = element("span", "session-preview", preview);
    subtitle.title = preview;
    const meta = element("div", "session-meta");
    meta.append(subtitle);
    if (childCount) {
      const toggle = element("button", "subtask-toggle", (open ? "▾ " : "▸ ") + "子任务 " + childCount);
      toggle.setAttribute("aria-expanded", String(open));
      toggle.setAttribute("aria-label", (open ? "收起" : "展开") + info.title + "的子任务");
      toggle.addEventListener("click", (event) => {
        event.stopPropagation();
        toggleGroup(info.key, open);
      });
      toggle.addEventListener("dblclick", (event) => event.stopPropagation());
      meta.append(toggle);
    }
    copy.append(meta);
    const size = total;
    const before = element("span", "session-size", bytes(size.before));
    before.title = (hasChildren ? "本任务及全部下级的有效内容总大小；磁盘文件合计 " : "有效内容大小；磁盘文件 ") + bytes(total.diskSize);
    if (hasChildren) before.append(element("small", "", "合计 · " + total.count + " 个会话"));
    const after = element("span", "session-size after-size", bytes(size.after));
    after.title = (hasChildren ? "本任务及全部下级：" : "") + (total.cleaned ? "本次清理后的实际内容大小" : "预计清理后的内容大小");
    after.append(element("small", "", total.cleaned ? "已清理" : size.after !== null ? "预计" : "待检查"));
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
    row.append(checkbox, copy, before, after, badge, details);
    row.addEventListener("click", (event) => {
      if (event.detail > 1) return;
      select(info.key, !checked);
    });
    row.addEventListener("dblclick", () => showDetails(info));
    group.append(row);
    if (childCount) {
      const children = element("div", "session-children");
      children.setAttribute("role", "group");
      children.setAttribute("aria-label", info.title + "的子任务");
      children.hidden = !open;
      group.append(children);
      containers.set(info.key, children);
    }
    containers.get(parentKey).append(group);
  });
  list.replaceChildren(fragment);
  list.hidden = view.rows.length === 0;
  if (focused) {
    if (focused.orphan) $("orphan-toggle")?.focus({ preventScroll: true });
    else [...list.querySelectorAll(".session-row")].find((row) => row.dataset.key === focused.key)
      ?.querySelector(focused.selector)?.focus({ preventScroll: true });
  }
  list.scrollTop = scroll;
  $("session-count").textContent = state.sessions.length;
  $("list-summary").textContent =
    view.mainCount + " 个主任务" + (view.subCount ? " · " + view.subCount + " 个子任务" : "");
  $("select-images").textContent = selected.size ? "清除全部选择" : "选择有图片的会话";
  $("load-more").hidden = view.rows.length <= pageSize;
  renderAction();
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
  const cleanable = items.filter(actionable);
  const saving = cleanable.reduce((total, info) => total + (info.saving || 0) + (info.cache_saving || 0), 0);
  $("selection-saving").hidden = !saving;
  $("selection-saving").replaceChildren(
    element("small", "", "图片上下文约减少"),
    element("strong", "", bytes(saving)),
  );
  const repair = items.length === 1 && items[0].recovery_backup;
  const needsRepair = items.some((info) => info.recovery_backup);
  const hiddenCount = items.filter((info) => !visibleSessionKeys.has(info.key)).length;
  $("clear-selection").hidden = !items.length;
  const blocked = !!state.busy && state.busy !== "scan";
  $("clear-selection").disabled = blocked || submitting;
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
  else if (pending) hint = "无需等待扫描结束，点击清理会优先检查所选会话。";
  else if (items.length && !cleanable.length)
    hint = items.some((info) => info.error)
      ? "会话暂时无法处理，可点击行末箭头查看原因。"
      : "没有可清理的图片。若仍重连或卡思考，请检查网络、代理或服务状态。";
  else if (cleanable.length) hint = "可立即清理；检测到锁时支持关闭 Codex，完成后自动重启。";
  $("selection-hint").textContent = (hiddenCount ? hiddenCount + " 个已选会话已折叠或不在当前列表中。" : "") + hint;
  $("clean").disabled =
    !connected || blocked || submitting || (!repair && (needsRepair || !cleanable.length));
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
    $(id).disabled = !connected || (id === "select-images" ? blocked : !!state.busy);
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
    ["原始内容大小", bytes(sizes(info).before)],
    [info.last_cleanup ? "清理后内容大小" : "预计清理后大小", bytes(sizes(info).after)],
    ["磁盘文件大小", bytes(info.size)],
    ["历史缓存图片约减少", bytes(info.cache_saving)],
    ["会话 ID", info.thread_id],
    ["说明", "清理保留日志原有大小及历史位置，不用于释放磁盘空间。"],
  ])
    meta.append(element("dt", "", name), element("dd", "", value));
  openDialog("details-dialog");
}
async function prepare(body) {
  if (submitting || (state.busy && state.busy !== "scan")) return;
  submitting = true;
  renderAction();
  try {
    challenge = await api("/api/prepare", body);
    $("confirm-sessions").replaceChildren(
      ...challenge.titles.map((name) => element("li", "", name)),
    );
    $("confirm-title").textContent =
      challenge.kind === "clean" ? "确认清理所选会话" : "确认恢复会话图片";
    $("confirm-description").textContent =
      challenge.kind === "clean"
        ? "清理历史图片上下文，保留文字和原始图片文件。每个会话都会先备份。"
        : "恢复所选会话的图片和历史缓存。若会话已有新消息，会拒绝覆盖。";
    $("lock-notice").hidden = !challenge.lock?.requires_close;
    $("lock-reason").textContent = challenge.lock?.reason || "";
    $("close-codex-option").hidden = !challenge.lock?.can_close;
    $("close-codex").checked = !!challenge.lock?.can_close;
    $("confirm-error").textContent = "";
    document.querySelector('input[name="mode"][value="online"]').checked = true;
    if (challenge.lock?.requires_close && !challenge.lock?.can_close)
      document.querySelector('input[name="mode"][value="offline"]').checked = true;
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
    $("close-codex").checked
      ? "我已保存工作并停止任务，允许关闭 Codex，处理结束后自动重启。"
      : document.querySelector('input[name="mode"]:checked').value === "online"
      ? "我已停止任务并关闭这些目标会话，操作期间不再打开。"
      : "我已完全退出 Codex，包括 IDE 中的 Codex 插件。";
  $("confirm-execute").textContent = $("close-codex").checked
    ? "关闭 Codex 并" + (challenge?.kind === "clean" ? "清理" : "恢复")
    : challenge?.kind === "clean" ? "备份并清理" : "恢复图片";
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
    if (state.report && !state.busy && !submitting && JSON.stringify(state.report) !== lastReport) {
      lastReport = JSON.stringify(state.report);
      $("notice-title").textContent = state.report.message;
      $("notice-next").textContent = state.report.next_step || "可查看详情了解原因。";
      $("notice").classList.toggle(
        "warning",
        !!state.report.failed?.length || !!state.report.skipped?.length,
      );
      $("notice").hidden = false;
      $("show-report").click();
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
  collapsedGroups.clear();
  renderList();
});
$("reconnect").addEventListener("click", poll);
$("clear-selection").addEventListener("click", () => {
  selected.clear();
  renderList();
});
$("images-only").addEventListener("change", () => {
  pageSize = 60;
  collapsedGroups.clear();
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
$("session-list").addEventListener("scroll", () => {
  const list = $("session-list");
  if (pageSize < visibleRowCount && list.scrollTop + list.clientHeight >= list.scrollHeight - 100) {
    pageSize += 60;
    renderList();
  }
}, { passive: true });
$("select-images").addEventListener("click", () => {
  selected = selected.size
    ? new Set()
    : new Set(
        filtered()
          .filter((info) => visibleSessionKeys.has(info.key) && info.eligible && !info.recovery_backup)
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
    prepare({ kind: "clean", keys: items.filter(actionable).map((info) => info.key) });
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
$("close-codex").addEventListener("change", resetAcknowledgement);
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
      close_codex: $("close-codex").checked,
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
  if (report.kind === "clean" && report.completed?.length) {
    const totals = report.totals;
    const summary = element("div", "result-sizes");
    for (const [label, value] of [["原始内容大小", totals.before_size], ["清理后内容大小", totals.after_size],
      ["内容减少", Math.max(0, totals.before_size - totals.after_size)]]) {
      const card = element("div");
      card.append(element("small", "", label), element("strong", "", bytes(value)));
      summary.append(card);
    }
    content.append(summary, element("p", "fine-print", "磁盘文件：" + bytes(totals.disk_before) + " → " + bytes(totals.disk_after) +
      "。保留填充以兼容历史位置；备份另占空间。"));
    if (totals.cache_reduced) content.append(element("p", "fine-print", "另减少历史缓存图片内容 " + bytes(totals.cache_reduced)));
  }
  for (const [key, title] of [
    ["completed", "已完成"],
    ["skipped", "仍被占用，已跳过"],
    ["failed", "未完成"],
  ]) {
    if (report[key]?.length) content.append(element("h3", "", title));
    for (const item of report[key] || []) {
      content.append(
        element("p", "", item.title),
        element("small", "", item.error || item.backup || ""),
      );
      if (item.before_size !== undefined) content.append(element("small", "result-comparison",
        bytes(item.before_size) + " → " + bytes(item.after_size)));
    }
  }
  $("retry-locked").hidden = !report.retry_keys?.length;
  openDialog("report-dialog");
});
$("retry-locked").addEventListener("click", () => {
  const keys = state.report?.retry_keys || [];
  $("report-dialog").close();
  prepare({ kind: "clean", keys });
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
async function loadBackups() {
  $("backup-list").replaceChildren(element("p", "fine-print", "正在读取备份…"));
  $("backups-error").textContent = "";
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
        element("small", "", ({ cleaned: "可恢复", restored: "已恢复", rolled_back: "已回滚", prepared: "待修复", restoring: "待修复" })[backup.status] || "状态未知"),
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
      const remove = element("button", "danger backup-delete", "删除备份");
      remove.disabled = ["prepared", "restoring"].includes(backup.status);
      if (remove.disabled) remove.title = "请先修复未完成的操作，再删除备份";
      remove.addEventListener("click", () => prepareBackupDeletion(backup.key));
      const actions = element("div", "backup-actions");
      actions.append(button, remove);
      item.append(copy, actions);
      list.append(item);
    }
  } catch (error) {
    $("backups-error").textContent = error.message;
  }
}
$("open-backups").addEventListener("click", () => {
  openDialog("backups-dialog");
  loadBackups();
});
async function prepareBackupDeletion(key) {
  if (submitting || state.busy) return;
  submitting = true;
  $("backups-error").textContent = "";
  try {
    deleteBackupChallenge = await api("/api/prepare", { kind: "delete_backup", key });
    $("delete-backup-name").textContent = deleteBackupChallenge.title;
    $("delete-backup-meta").textContent = deleteBackupChallenge.folder + " · 占用 " + bytes(deleteBackupChallenge.size);
    $("delete-backup-acknowledged").checked = false;
    $("delete-backup-execute").disabled = true;
    $("delete-backup-error").textContent = "";
    openDialog("delete-backup-dialog");
  } catch (error) {
    $("backups-error").textContent = error.message;
  } finally {
    submitting = false;
  }
}
$("delete-backup-acknowledged").addEventListener("change", () => {
  $("delete-backup-execute").disabled = !deleteBackupChallenge || !$("delete-backup-acknowledged").checked || submitting;
});
$("delete-backup-dialog").addEventListener("cancel", (event) => {
  if (submitting) event.preventDefault();
});
$("delete-backup-execute").addEventListener("click", async () => {
  if (!deleteBackupChallenge || submitting || !$("delete-backup-acknowledged").checked) return;
  submitting = true;
  $("delete-backup-execute").disabled = true;
  try {
    await api("/api/backups/delete", { confirmation: deleteBackupChallenge.confirmation, acknowledged: true });
    $("delete-backup-dialog").close();
    toast("备份已删除，当前会话保持不变。");
    await loadBackups();
  } catch (error) {
    $("delete-backup-error").textContent = error.message + "。请关闭此窗口后重新选择备份。";
  } finally {
    deleteBackupChallenge = null;
    submitting = false;
    renderAction();
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

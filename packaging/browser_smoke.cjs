// Browser interaction acceptance using Edge's DevTools protocol. No npm dependencies.
"use strict";
const fs = require("node:fs");
const path = require("node:path");
const { spawn, spawnSync } = require("node:child_process");
const assert = require("node:assert/strict");
const [url, executable, profile, output] = process.argv.slice(2);
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const browser = spawn(
  executable,
  [
    "--headless=new",
    "--remote-debugging-port=0",
    "--user-data-dir=" + profile,
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-gpu",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    url,
  ],
  { stdio: "ignore", windowsHide: true },
);
const pending = new Map();
let sequence = 0,
  socket;
const errors = [],
  requests = [];
const loadedDocuments = new Set();
async function until(fn, timeout = 15000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const result = await fn();
    if (result) return result;
    await delay(70);
  }
  throw new Error("Browser acceptance timed out");
}
function send(method, params = {}) {
  const caller = new Error().stack;
  return new Promise((resolve, reject) => {
    const id = ++sequence;
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new Error("DevTools timed out: " + method + " " + (params.type || "") + "\n" + caller));
    }, 20000);
    pending.set(id, { resolve, reject, timer });
    socket.send(JSON.stringify({ id, method, params }));
  });
}
async function evaluate(expression) {
  const response = await send("Runtime.evaluate", {
    expression,
    awaitPromise: true,
    returnByValue: true,
  });
  if (response.exceptionDetails)
    throw new Error(response.exceptionDetails.exception?.description || "Page evaluation failed");
  return response.result?.value;
}
async function waitPage(expression) {
  return until(() => evaluate(expression));
}
async function click(selector) {
  return evaluate(`document.querySelector(${JSON.stringify(selector)}).click()`);
}
async function input(selector, value) {
  return evaluate(
    `(()=>{const e=document.querySelector(${JSON.stringify(selector)});e.value=${JSON.stringify(value)};e.dispatchEvent(new Event('input',{bubbles:true}));})()`,
  );
}
async function screenshot(name) {
  await delay(200);
  const { data } = await send("Page.captureScreenshot", {
    format: "png",
    captureBeyondViewport: false,
  });
  fs.writeFileSync(path.join(output, name + ".png"), Buffer.from(data, "base64"));
}
async function modeOffline() {
  await click('input[name="mode"][value="offline"]');
}
async function fixedLayout(label, checkList = true) {
  const bounds = await evaluate(`(() => {
    const rect = (selector) => {
      const r = document.querySelector(selector).getBoundingClientRect();
      return {left:r.left, top:r.top, right:r.right, bottom:r.bottom, height:r.height};
    };
    return {height:innerHeight, width:innerWidth, documentHeight:document.documentElement.scrollHeight,
      documentWidth:document.documentElement.scrollWidth, scrollY,
      button:rect('#clean'), list:rect('#session-list'), action:rect('.action-bar'), footer:rect('footer')};
  })()`);
  assert.ok(bounds.documentHeight <= bounds.height, label + ": whole-page vertical overflow");
  assert.ok(bounds.documentWidth <= bounds.width, label + ": horizontal overflow");
  assert.equal(bounds.scrollY, 0, label + ": document moved");
  assert.ok(
    bounds.button.top >= 0 && bounds.button.bottom <= bounds.height,
    label + ": primary action clipped",
  );
  assert.ok(
    bounds.button.left >= 0 && bounds.button.right <= bounds.width,
    label + ": primary action clipped horizontally",
  );
  assert.ok(bounds.footer.bottom <= bounds.height, label + ": footer clipped");
  if (checkList) {
    assert.ok(bounds.list.height >= 85, label + ": list has no usable space " + JSON.stringify(bounds));
    assert.ok(bounds.list.bottom <= bounds.action.top, label + ": action overlaps list");
  }
}
async function listScroll(label) {
  // Add temporary rows to exercise overflow without changing any conversation data.
  await evaluate(`(() => {
    const list=document.getElementById('session-list'), source=list.firstElementChild;
    for(let n=0;n<60;n++){const row=source.cloneNode(true);row.dataset.layoutFixture='true';list.append(row);}
  })()`);
  try {
    const before = await evaluate(`(() => {
      const r=document.getElementById('session-list').getBoundingClientRect();
      return {x:r.left+r.width/2,y:r.top+r.height/2,action:document.querySelector('.action-bar').getBoundingClientRect().top};
    })()`);
    await send("Input.dispatchMouseEvent", {
      type: "mouseWheel",
      x: before.x,
      y: before.y,
      deltaX: 0,
      deltaY: 480,
    });
    await waitPage("document.getElementById('session-list').scrollTop>0");
    assert.equal(await evaluate("scrollY"), 0, label + ": wheel scrolled the whole window");
    assert.equal(
      await evaluate("document.querySelector('.action-bar').getBoundingClientRect().top"),
      before.action,
      label + ": action moved with the list",
    );
    await fixedLayout(label);
  } finally {
    await evaluate(
      "document.querySelectorAll('[data-layout-fixture]').forEach(row=>row.remove());document.getElementById('session-list').scrollTop=0",
    );
  }
}
async function main() {
  const activePort = path.join(profile, "DevToolsActivePort");
  const port = await until(() => {
    try {
      const value = fs.readFileSync(activePort, "utf8").split(/\r?\n/)[0];
      return /^\d+$/.test(value) ? value : null;
    } catch (error) {
      if (["ENOENT", "EBUSY", "EACCES"].includes(error.code)) return null;
      throw error;
    }
  });
  const target = await until(async () => {
    const pages = await (await fetch("http://127.0.0.1:" + port + "/json/list")).json();
    return pages.find((page) => page.type === "page" && page.url.startsWith(new URL(url).origin));
  });
  socket = new WebSocket(target.webSocketDebuggerUrl);
  socket.addEventListener("message", ({ data }) => {
    const message = JSON.parse(data);
    if (message.id && pending.has(message.id)) {
      const promise = pending.get(message.id);
      pending.delete(message.id);
      clearTimeout(promise.timer);
      if (message.error) promise.reject(new Error(message.error.message));
      else promise.resolve(message.result);
    } else if (message.method === "Runtime.exceptionThrown")
      errors.push(message.params.exceptionDetails.text);
    else if (message.method === "Network.requestWillBeSent")
      requests.push(message.params.request.url);
    else if (message.method === "Page.lifecycleEvent" && message.params.name === "load")
      loadedDocuments.add(message.params.loaderId);
  });
  await new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", reject, { once: true });
  });
  await send("Runtime.enable");
  await send("Page.enable");
  await send("Page.setLifecycleEventsEnabled", { enabled: true });
  await send("Page.bringToFront");
  await send("Network.enable");
  await send("Emulation.setEmulatedMedia", {
    features: [{ name: "prefers-reduced-motion", value: "no-preference" }],
  });
  await send("Emulation.setDeviceMetricsOverride", {
    width: 1120,
    height: 920,
    deviceScaleFactor: 1,
    mobile: false,
  });
  // Reload after instrumentation so CSP failures and all network requests are observed.
  const previousDocument = (await send("Page.getFrameTree")).frameTree.frame.loaderId;
  await send("Page.reload");
  await until(async () => {
    const current = (await send("Page.getFrameTree")).frameTree.frame.loaderId;
    return current !== previousDocument && loadedDocuments.has(current);
  });
  await waitPage(
    "document.documentElement?.dataset.connected==='true' && document.querySelectorAll('.session-row').length===3",
  );
  assert.equal(await evaluate("document.querySelectorAll('.session-row img').length"), 0);
  assert.equal(await evaluate("window.injected===true"), false);
  const firstKey = await evaluate("document.querySelector('.session-row').dataset.key");
  assert.equal(
    await evaluate(`(() => {
    document.querySelector('.session-row input').click();
    return document.querySelector('.session-row.selected').getAnimations().some(a=>a.playState==='running');
  })()`),
    true,
    "selection should provide short visual feedback",
  );
  assert.equal(await evaluate("document.getElementById('clean').disabled"), false);
  assert.equal(await evaluate(`(() => {
    const checkbox=document.querySelector('.session-row input');
    checkbox.focus(); checkbox.click();
    return document.activeElement.closest('.session-row')?.dataset.key===${JSON.stringify(firstKey)};
  })()`), true, "selection must retain keyboard focus after rendering");
  await click(".session-row input");
  await input("#search", "整理项目文档");
  assert.match(await evaluate("document.getElementById('selection-hint').textContent"), /不在当前列表/,
    "filtered selections must remain visible in the action summary");
  await click("#clear-selection");
  assert.equal(await evaluate("document.getElementById('clean').disabled"), true);
  await input("#search", "");
  await click(".session-row input");
  assert.equal(
    await evaluate("document.getElementById('clean').getBoundingClientRect().bottom<=innerHeight"),
    true,
  );
  await click("#images-only");
  await screenshot("web-main");
  await click("#images-only");
  const commandPoint = await evaluate(`(() => {
    const r=document.getElementById('clean').getBoundingClientRect();
    return {x:r.left+r.width/2,y:r.top+r.height/2};
  })()`);
  await send("Input.dispatchMouseEvent", { type: "mouseMoved", ...commandPoint });
  await waitPage(
    "getComputedStyle(document.getElementById('clean'),'::after').animationName==='command-sweep'",
  );
  await send("Input.dispatchMouseEvent", { type: "mouseMoved", x: 1, y: 1 });
  await click("#refresh");
  // Resolve CSS in the same sample as the real job state. A headless host may
  // defer compositor frames, so transient playState is not a portable clock.
  await waitPage(
    "document.documentElement.dataset.operation==='scan' && getComputedStyle(document.querySelector('.sessions-panel'),'::before').animationName==='activity-trace'",
  );
  await screenshot("web-activity");
  assert.equal(await evaluate("document.getElementById('clean').disabled"), false,
    "cleanup must remain available during the global scan");
  await click("#clean");
  await waitPage("document.getElementById('confirm-dialog').open");
  await click("#confirm-dialog [data-close]");
  await waitPage("document.documentElement.dataset.operation==='idle' && !state.busy");
  assert.equal(
    await evaluate(
      "document.getAnimations().some(a=>['activity-trace','scan-turn','activity-pulse'].includes(a.animationName))",
    ),
    false,
    "activity must stop when the job finishes",
  );
  await send("Emulation.setEmulatedMedia", {
    features: [{ name: "prefers-reduced-motion", value: "reduce" }],
  });
  assert.equal(
    await evaluate(`(() => {
    document.querySelector('.session-row input').click();
    document.querySelector('.session-row input').click();
    return document.getAnimations().filter(a=>a.playState==='running').length;
  })()`),
    0,
    "selection feedback must honor reduced motion",
  );
  await click("#refresh");
  await waitPage("document.documentElement.dataset.operation==='scan'");
  assert.equal(
    await evaluate("document.getAnimations().filter(a=>a.playState==='running').length"),
    0,
    "busy feedback must honor reduced motion",
  );
  await waitPage("document.documentElement.dataset.operation==='idle' && !state.busy");
  await send("Emulation.setEmulatedMedia", {
    features: [{ name: "prefers-reduced-motion", value: "no-preference" }],
  });
  await fixedLayout("desktop");
  await listScroll("desktop scroll");
  await input("#search", "整理项目文档");
  assert.equal(await evaluate("document.querySelectorAll('.session-row').length"), 1);
  // Change selection to the text-only row and check the disabled state and useful hint.
  await click("#select-images");
  await click(".session-row input");
  assert.equal(await evaluate("document.getElementById('clean').disabled"), true);
  assert.match(
    await evaluate("document.getElementById('selection-hint').textContent"),
    /没有可清理/,
  );
  await input("#search", "");
  await click("#images-only");
  assert.equal(await evaluate("document.querySelectorAll('.session-row').length"), 2);
  await click("#select-images");
  await click(`.session-row[data-key="${firstKey}"] input`);
  // Exercise lock handling without permission to close a real desktop app.
  await evaluate(`window.lockFixtureFetch=window.fetch;
    window.fetch=async (url, options)=>{
      const response=await window.lockFixtureFetch(url, options);
      if (!String(url).startsWith('/api/prepare')) return response;
      const data=await response.json();
      data.lock={requires_close:true,can_close:true,reason:'检测到会话锁，请关闭 Codex 后清理。'};
      return new Response(JSON.stringify(data),{status:response.status,headers:{'Content-Type':'application/json'}});
    };`);
  await click("#clean");
  await waitPage("document.getElementById('confirm-dialog').open");
  assert.equal(await evaluate("document.getElementById('lock-notice').hidden"), false);
  assert.equal(await evaluate("document.getElementById('close-codex').checked"), true);
  assert.match(await evaluate("document.getElementById('confirm-execute').textContent"), /关闭 Codex 并清理/);
  assert.match(await evaluate("document.getElementById('ack-text').textContent"), /自动重启/);
  await screenshot("web-locked");
  await click("#confirm-dialog [data-close]");
  await evaluate("window.fetch=window.lockFixtureFetch");
  await click("#clean");
  await waitPage("document.getElementById('confirm-dialog').open");
  assert.equal(await evaluate("document.getElementById('confirm-execute').disabled"), true);
  await click("#acknowledged");
  assert.equal(await evaluate("document.getElementById('confirm-execute').disabled"), false);
  await evaluate("document.querySelector('#confirm-dialog .advanced').open=true");
  await modeOffline();
  assert.equal(await evaluate("document.getElementById('confirm-execute').disabled"), true);
  await click("#acknowledged");
  await screenshot("web-confirm");
  await click("#confirm-execute");
  await waitPage(
    "!document.getElementById('confirm-dialog').open && !state.busy && state.report?.completed?.length===1",
  );
  assert.match(await evaluate("document.getElementById('notice-next').textContent"), /重新打开/);
  await screenshot("web-result");
  assert.equal(await evaluate("document.getElementById('report-dialog').open"), true,
    "completion must automatically open the result dialog");
  assert.match(await evaluate("document.querySelector('.result-sizes').textContent"), /原始内容大小.*清理后内容大小/);
  assert.equal(await evaluate("state.report.totals.after_size < state.report.totals.before_size"), true);
  await click("#report-dialog [data-close]");
  await fixedLayout("result banner");
  await evaluate("document.getElementById('more-menu').open=true");
  await click("#open-backups");
  await waitPage("document.querySelectorAll('.backup-item').length===1");
  await click(".backup-item button");
  await waitPage("document.getElementById('confirm-dialog').open");
  await evaluate("document.querySelector('#confirm-dialog .advanced').open=true");
  await modeOffline();
  await click("#acknowledged");
  await click("#confirm-execute");
  await waitPage(
    "!document.getElementById('confirm-dialog').open && !state.busy && state.report?.message==='已恢复会话图片'",
  );
  await waitPage("document.getElementById('report-dialog').open");
  await click("#report-dialog [data-close]");
  await click("#images-only");
  await send("Emulation.setDeviceMetricsOverride", {
    width: 390,
    height: 844,
    deviceScaleFactor: 1,
    mobile: false,
  });
  await delay(120);
  assert.equal(await evaluate("document.documentElement.scrollWidth <= innerWidth"), true);
  await click("#images-only");
  await screenshot("web-small");
  await click("#images-only");
  await fixedLayout("narrow viewport");
  await listScroll("narrow scroll");
  await send("Emulation.setDeviceMetricsOverride", {
    width: 860,
    height: 660,
    deviceScaleFactor: 1,
    mobile: false,
  });
  await fixedLayout("short desktop");
  await listScroll("short desktop scroll");
  await click("#images-only");
  await fixedLayout("filtered compact layout");
  await screenshot("web-compact");
  await click("#images-only");
  await send("Emulation.setDeviceMetricsOverride", {
    width: 720,
    height: 540,
    deviceScaleFactor: 1,
    mobile: false,
  });
  await fixedLayout("short scaled desktop");
  await listScroll("short scaled desktop scroll");
  await input("#search", "no-matching-session-for-layout");
  await fixedLayout("empty search", false);
  assert.equal(
    await evaluate(
      "(()=>{const r=document.getElementById('clean').getBoundingClientRect();return r.left>=0&&r.right<=innerWidth&&r.top>=0&&r.bottom<=innerHeight})()",
    ),
    true,
  );
  await click("#reset-filters");
  assert.equal(await evaluate("document.querySelectorAll('.session-row').length"), 3);
  assert.equal(await evaluate("document.activeElement.id"), "search");
  await evaluate("document.getElementById('more-menu').open=true");
  await click('[data-action="open-settings"]');
  assert.equal(await evaluate("document.getElementById('settings-dialog').open"), true,
    "short windows must retain access to directory settings");
  await click("#settings-dialog [data-close]");
  // Read-only presentation fixtures exercise long titles, pagination and recovery states.
  await send("Emulation.setDeviceMetricsOverride", {
    width: 1120, height: 850, deviceScaleFactor: 1, mobile: false,
  });
  await evaluate(`(() => {
    window.presentationFixture={sessions:state.sessions, selected:[...selected]};
    const source=state.sessions[0];
    const names=['制作产品介绍视频','根据截图调整网页首页','整理项目文档与发布说明',
      '设计一套清晰的登录和注册流程，兼顾桌面端与移动端的完整交互体验',
      '把数据分析结果整理成可视化报告','优化移动端的导航与阅读体验','检查备份与恢复流程','继续上次的界面迭代'];
    state.sessions=Array.from({length:75},(_,n)=>({...source,key:'presentation-'+n,
      title:names[n%names.length]+(n>=8?' · '+(n+1):''),
      preview:names[n%names.length],cwd:'D:/projects/creative-studio',modified:source.modified-n*3600,
      saving:1048576*(n%7+1),cache_saving:0,
      size:1048576*(n%7+3),content_size:1048576*(n%7+3),
      error:n===6?'这个会话暂时无法读取，请查看详情并重新扫描。':'',
      recovery_backup:n===7?'synthetic-recovery':'',
      eligible:n%3!==2&&n!==6&&n!==7,scanned:true}));
    selected=new Set(['presentation-0','presentation-1']);
    document.getElementById('notice').hidden=true;
    renderList();
  })()`);
  assert.equal(await evaluate("document.querySelectorAll('.session-row').length"), 60);
  await fixedLayout("populated workspace");
  const listSpace = await evaluate(`(() => {
    const list=document.getElementById('session-list').getBoundingClientRect();
    return {width:list.width,height:list.height,viewportWidth:innerWidth,viewportHeight:innerHeight};
  })()`);
  assert.ok(listSpace.width >= listSpace.viewportWidth * .95 &&
    listSpace.height >= listSpace.viewportHeight * .60,
    "the conversation list must occupy the main workspace: " + JSON.stringify(listSpace));
  assert.equal(await evaluate(`(() => {
    const title=[...document.querySelectorAll('.session-title')].find(n=>n.textContent.length>30);
    return getComputedStyle(title).whiteSpace==='normal' && title.scrollHeight<=title.clientHeight;
  })()`), true, "long conversation titles must wrap without being clipped");
  await screenshot("web-polished-workspace");
  await evaluate("document.getElementById('session-list').scrollTop=document.getElementById('session-list').scrollHeight");
  await waitPage("document.querySelectorAll('.session-row').length===75");
  assert.equal(await evaluate("document.querySelectorAll('.session-row').length"), 75);
  await evaluate("document.getElementById('session-list').scrollTop=0");
  await click("#clear-selection");
  await click('.session-row[data-key="presentation-7"] input');
  assert.equal(await evaluate("document.getElementById('clean').disabled"), false);
  assert.match(await evaluate("document.getElementById('clean').textContent"), /修复/);
  await click('.session-row[data-key="presentation-0"] input');
  assert.equal(await evaluate("document.getElementById('clean').disabled"), true);
  assert.match(await evaluate("document.getElementById('selection-hint').textContent"), /单独选择/);
  await click("#clear-selection");
  await input("#search", "no-such-presentation");
  await screenshot("web-empty-search");
  await click("#reset-filters");
  await evaluate(`(() => {
    const source=window.presentationFixture.sessions[0];
    const task=(key,title,parent='',extra={})=>({...source,key,thread_id:key,title,
      preview:title,parent_thread_id:parent,is_subagent:!!parent,...extra});
    state.sessions=[task('tree-main','制作产品介绍视频','',{eligible:false,saving:0}),
      task('tree-child','独立子任务检索','tree-main'),
      task('tree-grandchild','核对字幕','tree-child'),
      task('tree-other','整理项目文档'),
      task('tree-orphan','主任务暂缺的子任务','missing-main')];
    selected.clear();expandedGroups.clear();collapsedGroups.clear();renderList();
  })()`);
  assert.equal(await evaluate("document.querySelectorAll('.session-row').length"), 2,
    "subtasks must be folded under their parents by default");
  assert.equal(await evaluate("document.querySelectorAll('.subtask-row').length"), 0);
  assert.equal(await evaluate("document.querySelectorAll('.orphan-group').length"), 1);
  await click('.session-row[data-key="tree-main"] .subtask-toggle');
  assert.equal(await evaluate("document.querySelector('.session-children .session-row[data-key=tree-child]')!==null"), true);
  await click('.session-row[data-key="tree-child"] .subtask-toggle');
  assert.equal(await evaluate("document.querySelector('.session-children .session-children .session-row[data-key=tree-grandchild]')!==null"), true);
  await screenshot("web-subtasks");
  await click('.session-row[data-key="tree-main"] input');
  assert.deepEqual(await evaluate("[...selected]"), ['tree-main'],
    "selecting the main task must not implicitly select descendants");
  await click('.session-row[data-key="tree-child"] input');
  await click('.session-row[data-key="tree-main"] .subtask-toggle');
  assert.equal(await evaluate("selected.has('tree-child')"), true);
  assert.match(await evaluate("document.getElementById('selection-hint').textContent"), /已折叠/);
  await input("#search", "独立子任务检索");
  assert.equal(await evaluate("document.querySelector('.session-row[data-key=tree-main]')!==null"), true,
    "search must retain the parent context and reveal the matching child");
  assert.equal(await evaluate("document.querySelector('.session-children .session-row[data-key=tree-child]')!==null"), true);
  await click("#reset-filters");
  await click("#images-only");
  assert.equal(await evaluate("document.querySelector('.session-row[data-key=tree-main]')!==null"), true,
    "an image-only filter must keep the text-only parent of an image subtask");
  await click("#images-only");
  await click("#orphan-toggle");
  assert.equal(await evaluate("document.querySelector('.orphan-group .session-children .session-row[data-key=tree-orphan]')!==null"), true);
  await evaluate(`(() => {
    const source=state.sessions[0];
    state.sessions=[{...source,key:'cycle-a',thread_id:'cycle-a',is_subagent:true,parent_thread_id:'cycle-b'},
      {...source,key:'cycle-b',thread_id:'cycle-b',is_subagent:true,parent_thread_id:'cycle-a'}];
    expandedGroups.clear();collapsedGroups.clear();selected.clear();renderList();
  })()`);
  await click("#orphan-toggle");
  await click(".subtask-toggle");
  assert.equal(await evaluate("document.querySelectorAll('.session-row').length"), 2,
    "invalid cyclic ancestry must remain reachable without infinite recursion");
  await evaluate("state.sessions=[];renderList()");
  assert.equal(await evaluate("document.getElementById('choose-directory').hidden"), false);
  await screenshot("web-empty-directory");
  await evaluate(`state.sessions=window.presentationFixture.sessions;
    selected=new Set(window.presentationFixture.selected);renderList();
    window.presentationFetch=window.fetch;
    window.fetch=(url,options)=>String(url).startsWith('/api/state')
      ?Promise.reject(new TypeError('Synthetic connection interruption'))
      :window.presentationFetch(url,options);`);
  await evaluate("poll()");
  await waitPage("!document.getElementById('connection-notice').hidden");
  assert.equal(await evaluate("document.getElementById('clean').disabled"), true);
  await fixedLayout("disconnected workspace");
  await screenshot("web-disconnected");
  await evaluate("window.fetch=window.presentationFetch");
  await click("#reconnect");
  await waitPage("document.documentElement.dataset.connected==='true'");
  assert.equal(await evaluate("selected.size"), 1, "reconnect must preserve selection");
  assert.equal(await evaluate("document.getElementById('connection-notice').hidden"), true);
  assert.deepEqual(errors, []);
  assert.equal(
    requests.every((value) => new URL(value).origin === new URL(url).origin),
    true,
  );
  // Do not log the launch URL or token.
  console.log(
    JSON.stringify({
      rendered_ui: "ok",
      search_selection: "ok",
      confirmation_gate: "ok",
      cleanup_restore: "ok",
      small_layout: "ok",
      fixed_viewport: "ok",
      list_scroll: "ok",
      motion_feedback: "ok",
      activity_state: "ok",
      reduced_motion: "ok",
      selection_scope: "ok",
      recovery_guidance: "ok",
      empty_states: "ok",
      connection_recovery: "ok",
      pagination: "ok",
      subtask_hierarchy: "ok",
      script_errors: 0,
      external_requests: 0,
    }),
  );
  await evaluate(
    "fetch('/api/shutdown',{method:'POST',headers:{'Authorization':'Bearer '+token,'Content-Type':'application/json'},body:'{}'}).then(r=>r.ok)",
  );
}
main()
  .catch((error) => {
    console.error(error.stack);
    process.exitCode = 1;
  })
  .finally(async () => {
    if (socket?.readyState === WebSocket.OPEN) {
      try {
        await send("Browser.close");
      } catch {
        /* Browser exit can close the transport first. */
      }
      socket.close();
    }
    for (const item of pending.values()) clearTimeout(item.timer);
    if (browser.exitCode === null) {
      await delay(400);
      if (browser.exitCode === null) {
        if (process.platform === "win32")
          spawnSync("taskkill", ["/PID", String(browser.pid), "/T", "/F"], {
            stdio: "ignore",
            windowsHide: true,
          });
        else browser.kill("SIGKILL");
      }
    }
  });

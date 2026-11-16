/* Activity analytics -- the "Activity" pane of the Usage tab, plus the
 * browser-side beacon for interactions that never reach a server route.
 *
 * Server side (app/activity.py) already records every API action on every
 * channel: web UI, in-app chat, remote MCP, stdio MCP, API scripts. What only
 * the browser knows -- which tab someone opened, which palette command they
 * ran, whether they finished a tour -- is sent here, batched, against a fixed
 * allowlist the server enforces. Never text the person typed, never names of
 * orgs, fields or components.
 *
 * Loaded after app.js / chat.js and before home.js / guide.js. It hooks the
 * global functions it cares about lazily (on DOMContentLoaded), so load order
 * of the later scripts does not matter and nothing else has to know it exists.
 */

// =====================================================================
// 1. beacon
// =====================================================================

const ACT_QUEUE = [];
let ACT_TIMER = null;
const ACT_FLUSH_MS = 8000;
const ACT_MAX_QUEUE = 40;

/** Queue one UI interaction. `meta` values must be short app-authored slugs. */
function trackUi(action, meta) {
  try {
    if (typeof CURRENT_USER === "undefined" || !CURRENT_USER) return;
    ACT_QUEUE.push({ action, meta: meta || {}, at: Date.now() });
    if (ACT_QUEUE.length >= ACT_MAX_QUEUE) { flushUiEvents(); return; }
    if (!ACT_TIMER) ACT_TIMER = setTimeout(flushUiEvents, ACT_FLUSH_MS);
  } catch (e) { /* analytics never breaks the page */ }
}

function flushUiEvents(useBeacon = false) {
  if (ACT_TIMER) { clearTimeout(ACT_TIMER); ACT_TIMER = null; }
  if (!ACT_QUEUE.length) return;
  const batch = ACT_QUEUE.splice(0, ACT_QUEUE.length);
  const body = JSON.stringify({ events: batch });
  try {
    if (useBeacon && navigator.sendBeacon) {
      navigator.sendBeacon("/api/activity/events", new Blob([body], { type: "application/json" }));
      return;
    }
    // Raw fetch, not api(): a 401 from a beacon must not bounce the page to
    // the login screen -- the session check belongs to real requests.
    fetch("/api/activity/events", { method: "POST", headers: { "Content-Type": "application/json" },
                                    body, keepalive: true }).catch(() => {});
  } catch (e) { /* ignore */ }
}

function actSlug(s) {
  return String(s || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 50);
}

/** Wrap a global function so calling it also records `fn(args)`'s event.
 *  The original still runs first-class: same `this`, same return value. */
function actHook(name, toEvent) {
  const orig = window[name];
  if (typeof orig !== "function" || orig.__actHooked) return;
  const wrapped = function (...args) {
    try { const ev = toEvent(...args); if (ev) trackUi(ev[0], ev[1]); } catch (e) { /* ignore */ }
    return orig.apply(this, args);
  };
  wrapped.__actHooked = true;
  window[name] = wrapped;
}

function installActivityHooks() {
  actHook("showView", name => ["ui.view", { view: actSlug(name) }]);
  actHook("openPalette", () => ["ui.palette_open"]);
  actHook("runPaletteItem", idx => {
    const it = (typeof PALETTE !== "undefined" && PALETTE.shown) ? PALETTE.shown[idx] : null;
    if (!it) return null;
    // Org and account entries carry customer names: record only the group.
    const cmd = (it.group === "Actions" || it.group === "Go to") ? actSlug(it.label) : actSlug(it.group);
    return ["ui.palette_run", { command: cmd, kind: actSlug(it.group) }];
  });
  actHook("startTour", id => ["ui.tour_start", { tour: actSlug(id) }]);
  actHook("guideTourDone", id => ["ui.tour_done", { tour: actSlug(id) }]);
  actHook("openHelp", () => ["ui.help_open"]);
  actHook("startDemo", () => ["ui.demo_open"]);
  actHook("runTriage", () => {
    const el = document.getElementById("triageInput");
    const kind = (typeof classifyTriage === "function" && el) ? (classifyTriage(el.value.trim()) || {}).kind : null;
    return kind && kind !== "empty" ? ["ui.triage_submit", { kind: actSlug(kind) }] : null;
  });
  actHook("toggleChatDock", open => (open === false ? null : ["ui.chat_dock"]));
  actHook("copyText", id => ["ui.copy", { target: actSlug(id) }]);
  actHook("copyToken", () => ["ui.copy", { target: "api-token" }]);
  actHook("downloadCurrentNormalized", () => ["ui.export", { target: "normalized-log" }]);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") flushUiEvents(true);
  });
  window.addEventListener("pagehide", () => flushUiEvents(true));
}

if (typeof document !== "undefined" && document.addEventListener) {
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", installActivityHooks);
  else installActivityHooks();
}

// =====================================================================
// 2. the Usage tab's three panes
// =====================================================================

// The Usage tab always opens on the executive summary. Anything that wants a
// different pane -- the quota chip wants the token pane -- asks for it with
// openUsagePane() rather than remembering the last pane, so the nav button is
// predictable: Usage means "the summary".
let USAGE_PANE = "summary";
let USAGE_NEXT_PANE = null;
const USAGE_PANES = { summary: "usagePaneSummary", activity: "usagePaneActivity", tokens: "usagePaneTokens" };

function openUsagePane(name) {
  USAGE_NEXT_PANE = USAGE_PANES[name] ? name : null;
  showView("usage");
}

function showUsagePane(name, opts = {}) {
  if (!USAGE_PANES[name]) name = "summary";
  USAGE_PANE = name;
  document.querySelectorAll("#usageSwitch [data-usage-pane]").forEach(b => {
    const on = b.dataset.usagePane === name;
    b.classList.toggle("active", on);
    b.setAttribute("aria-selected", on ? "true" : "false");
  });
  for (const [pane, id] of Object.entries(USAGE_PANES)) {
    const el = document.getElementById(id);
    if (el) el.hidden = pane !== name;
  }
  if (!opts.silent) trackUi("ui.view", { view: `usage-${name}` });
  if (name === "activity") loadActivityView();
  if (name === "summary" && typeof loadExecSummary === "function") loadExecSummary();
}

// app.js's loadUsageView still fills the token pane (quota, LLM connection,
// token tables). The wrapper picks the pane first, so the one on screen
// starts loading immediately and is never blank while the token pane loads.
if (typeof window !== "undefined") {
  const wrapUsage = () => {
    const orig = window.loadUsageView;
    if (typeof orig !== "function" || orig.__actWrapped) return;
    const w = async function (...args) {
      const pane = USAGE_NEXT_PANE || "summary";
      USAGE_NEXT_PANE = null;
      showUsagePane(pane, { silent: true });
      return orig.apply(this, args);
    };
    w.__actWrapped = true;
    window.loadUsageView = w;
  };
  if (typeof document !== "undefined" && document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wrapUsage);
  } else wrapUsage();
}

// =====================================================================
// 3. rendering
// =====================================================================

const ACT_CHANNEL_ORDER = ["web", "chat", "mcp-remote", "mcp-stdio", "api"];
const ACT_CHANNEL_SHORT = { web: "Web", chat: "AI chat", "mcp-remote": "MCP remote",
                            "mcp-stdio": "MCP stdio", api: "API" };

function actEsc(s) { return typeof escapeHtml === "function" ? escapeHtml(s) : String(s ?? ""); }
function actNum(n) { return typeof fmtCompact === "function" ? fmtCompact(n || 0) : String(n || 0); }
function actWhen(ts) { return ts ? String(ts).replace("T", " ").replace("Z", "").slice(0, 16) : "-"; }
function actMs(ms) {
  if (ms === null || ms === undefined) return "-";
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`;
}

function actChannelChip(ch) {
  return `<span class="ch-chip ch-${actEsc(ch)}">${actEsc(ACT_CHANNEL_SHORT[ch] || ch)}</span>`;
}

/** A 100%-wide stacked bar of {channel: count}. */
function actStack(channels, title = "") {
  const total = Object.values(channels || {}).reduce((a, b) => a + (b || 0), 0);
  if (!total) return `<div class="ch-stack empty"></div>`;
  const segs = ACT_CHANNEL_ORDER.concat(Object.keys(channels).filter(k => !ACT_CHANNEL_ORDER.includes(k)))
    .filter(k => channels[k])
    .map(k => {
      const pct = (channels[k] / total) * 100;
      return `<span class="ch-seg ch-bg-${actEsc(k)}" style="width:${pct.toFixed(2)}%"
        title="${actEsc(ACT_CHANNEL_SHORT[k] || k)}: ${channels[k]}"></span>`;
    }).join("");
  return `<div class="ch-stack" title="${actEsc(title)}">${segs}</div>`;
}

function actLegend() {
  return `<div class="ch-legend">${ACT_CHANNEL_ORDER.map(k =>
    `<span><i class="ch-bg-${k}"></i>${actEsc(ACT_CHANNEL_SHORT[k])}</span>`).join("")}</div>`;
}

function actKpi(value, label, note = "") {
  return `<div class="kpi"><div class="kpi-value">${value}</div><div class="kpi-label">${actEsc(label)}</div>${
    note ? `<div class="kpi-note">${note}</div>` : ""}</div>`;
}

/** Per-day stacked-by-channel bars. */
function renderActivityTrend(r, hostId) {
  const host = document.getElementById(hostId);
  if (!host) return;
  const days = r.by_day || [];
  if (!days.length) { host.innerHTML = ""; return; }
  const peak = Math.max(1, ...days.map(d => d.events || 0));
  const bars = days.map(d => {
    const v = d.events || 0;
    const h = v ? Math.max(2, Math.round((v / peak) * 100)) : 0;
    const segs = ACT_CHANNEL_ORDER.filter(k => (d.channels || {})[k]).map(k =>
      `<span class="ch-bg-${k}" style="flex:${d.channels[k]} 0 0"></span>`).join("");
    const title = `${d.date}: ${v} action(s), ${d.active_users} user(s)` +
      ACT_CHANNEL_ORDER.filter(k => (d.channels || {})[k]).map(k => ` · ${ACT_CHANNEL_SHORT[k]} ${d.channels[k]}`).join("");
    return `<div class="usage-bar-wrap" title="${actEsc(title)}">
      <div class="act-bar ${v ? "" : "empty"}" style="height:${h}%">${segs}</div></div>`;
  }).join("");
  host.innerHTML = `<div class="usage-chart">${bars}</div>
    <div class="usage-axis"><span>${actEsc(r.from)}</span><span>${actEsc(r.to)}</span></div>
    ${actLegend()}
    <div class="usage-legend">Actions per day, coloured by channel. Peak: ${peak}. Hover a bar for the split.</div>`;
}

function renderActFeed(hostId, rows, showUser) {
  const host = document.getElementById(hostId);
  if (!host) return;
  if (!rows || !rows.length) { host.innerHTML = `<p class="muted">Nothing recorded in this period yet.</p>`; return; }
  host.innerHTML = rows.map(e => `<div class="act-row${e.ok === false ? " failed" : ""}">
      <span class="act-when">${actEsc(actWhen(e.at))}</span>
      ${actChannelChip(e.channel || "web")}
      ${showUser ? `<b>${actEsc(e.username || "(link viewer)")}</b>` : ""}
      <span class="act-what">${actEsc(e.label || e.action)}${e.tool ? ` <code>${actEsc(e.tool)}</code>` : ""}${
        e.org_id ? ` <span class="muted">· ${actEsc(e.org_id)}</span>` : ""}${
        e.client ? ` <span class="muted">· ${actEsc(e.client)}</span>` : ""}</span>
      ${e.ok === false ? `<span class="badge high">${actEsc(e.status || "failed")}</span>` : ""}
    </div>`).join("");
}

function actFill(tbodyId, rows, colspan, empty, rowHtml) {
  const tbody = document.getElementById(tbodyId);
  if (!tbody) return;
  if (!rows || !rows.length) {
    tbody.innerHTML = `<tr class="empty-row"><td colspan="${colspan}">${actEsc(empty)}</td></tr>`;
    return;
  }
  tbody.innerHTML = rows.map(rowHtml).join("");
}

// ---------- your own activity ----------

async function renderMyActivity() {
  const kpis = document.getElementById("myActivityKpis");
  if (!kpis) return;
  const days = (document.getElementById("myActDays") || {}).value || 30;
  if (typeof setBusy === "function") setBusy(kpis);
  const r = await apiJson(`/api/activity/me?days=${encodeURIComponent(days)}`, {}, null);
  if (!r) { kpis.innerHTML = `<p class="status-line error">Could not load your activity.</p>`; return; }
  const t = r.totals || {};
  const me = r.me || {};
  kpis.innerHTML = [
    actKpi(t.actions || 0, "Actions"),
    actKpi(t.active_days || 0, "Active days"),
    actKpi(t.kb_lookups || 0, "Org lookups"),
    actKpi(t.logs_parsed || 0, "Logs parsed"),
    actKpi(t.mcp_calls || 0, "MCP tool calls", t.mcp_calls ? "" : "Claude/Copilot over MCP counts here"),
    actKpi(t.llm_turns || 0, "AI questions"),
    actKpi(t.incidents_filed || 0, "Incidents filed"),
    actKpi(t.resolutions || 0, "Fixes recorded"),
  ].join("");

  const chanHost = document.getElementById("myActivityChannels");
  const chans = {};
  (r.by_channel || []).forEach(c => { chans[c.channel] = c.events; });
  if (chanHost) {
    chanHost.innerHTML = (r.by_channel || []).length
      ? `<div class="act-sub">How you worked${me.segment ? ` &mdash; <b>${actEsc(me.segment)}</b>` : ""}</div>
         ${actStack(chans)}${actLegend()}
         ${(r.mcp_clients || []).length ? `<p class="muted" style="margin-top:8px;">MCP clients: ${
           r.mcp_clients.map(c => `<code>${actEsc(c.client)}</code> (${c.calls})`).join(", ")}</p>` : ""}`
      : "";
  }
  const feats = (r.features || []).filter(f => f.kind === "action").slice(0, 10);
  actFill("myActivityFeatures", feats, 3, "Nothing yet.", f => `<tr>
      <td>${actEsc(f.label)}</td><td class="num">${f.count}</td>
      <td>${Object.keys(f.channels || {}).map(actChannelChip).join(" ")}</td></tr>`);
  renderActFeed("myActivityRecent", (r.recent || []).filter(e => e.kind !== "ui").slice(0, 15), false);
}

// ---------- admin ----------

async function loadActivityView() {
  await renderMyActivity();
  if (typeof CURRENT_USER === "undefined" || !CURRENT_USER || CURRENT_USER.role !== "admin") return;
  await populateActivityUserPicker();
  await loadActivity();
}

async function populateActivityUserPicker() {
  const sel = document.getElementById("actUser");
  if (!sel) return;
  const current = sel.value;
  const users = await apiJson("/api/admin/users", {}, {}) || {};
  sel.innerHTML = `<option value="">Everyone</option>` + Object.keys(users).sort()
    .map(u => `<option value="${actEsc(u)}">${actEsc(u)}</option>`).join("");
  sel.value = current;
}

function activityQuery() {
  const days = (document.getElementById("actDays") || {}).value || 30;
  const user = (document.getElementById("actUser") || {}).value || "";
  return `days=${encodeURIComponent(days)}${user ? `&username=${encodeURIComponent(user)}` : ""}`;
}

function exportActivity() {
  trackUi("ui.export", { target: "activity-csv" });
  window.location.href = `/api/admin/activity/export?${activityQuery()}`;
}

function focusActivityUser(username) {
  const sel = document.getElementById("actUser");
  if (!sel) return;
  // The picker lists current accounts; a row can name one that is not in it
  // (renamed, or only seen in the LLM ledger), so add it rather than fail.
  if (![...sel.options].some(o => o.value === username)) {
    const opt = document.createElement("option");
    opt.value = username; opt.textContent = username;
    sel.appendChild(opt);
  }
  sel.value = username;
  loadActivity();
  document.getElementById("actKpis").scrollIntoView({ behavior: "smooth", block: "start" });
}

async function loadActivity() {
  const kpis = document.getElementById("actKpis");
  if (!kpis) return;
  if (typeof setBusy === "function") setBusy(kpis);
  const r = await apiJson(`/api/admin/activity?${activityQuery()}`, {}, null);
  if (!r) { kpis.innerHTML = `<p class="status-line error">Could not load activity.</p>`; return; }
  renderAdminActivity(r);
}

function renderAdminActivity(r) {
  const t = r.totals || {};
  document.getElementById("actKpis").innerHTML = [
    actKpi(t.active_users || 0, "Active users"),
    actKpi(actNum(t.actions), "Actions"),
    actKpi(t.non_llm_users || 0, "Users with no AI spend", "active, but zero in-app LLM turns"),
    actKpi(actNum(t.mcp_calls), "MCP tool calls"),
    actKpi(actNum(t.chat_tool_calls), "Chat tool calls"),
    actKpi(actNum(t.kb_lookups), "Org lookups"),
    actKpi(actNum(t.logs_parsed), "Logs parsed"),
    actKpi(t.incidents_filed || 0, "Incidents filed"),
    actKpi(t.resolutions || 0, "Fixes recorded"),
    t.errors ? actKpi(t.errors, "Failed actions") : "",
  ].join("");
  renderActivityTrend(r, "actTrend");

  // segments
  const segHost = document.getElementById("actSegments");
  const segTotal = (r.segments || []).reduce((a, s) => a + s.users, 0) || 1;
  segHost.innerHTML = (r.segments || []).length ? `<table><tbody>${r.segments.map(s => `<tr>
      <td>${actEsc(s.segment)}</td><td class="num">${s.users} user${s.users === 1 ? "" : "s"}</td>
      <td class="num">${actNum(s.events)} actions</td>
      <td style="width:35%"><div class="usage-bar-mini" style="width:${Math.max(2, Math.round(s.users / segTotal * 100))}%"></div></td>
    </tr>`).join("")}</tbody></table>` : `<p class="muted">No active accounts in this period.</p>`;

  // ladder
  const lad = r.ladder || [];
  const top = (lad[0] || {}).users || 1;
  document.getElementById("actLadder").innerHTML = lad.map(s => `<div class="ladder-row">
      <div class="ladder-label">${actEsc(s.label)}</div>
      <div class="ladder-track"><div class="ladder-fill" style="width:${Math.max(s.users ? 3 : 0, Math.round(s.users / top * 100))}%"></div></div>
      <div class="ladder-num">${s.users}${s.step !== "active" && top ? ` <span class="muted">(${Math.round(s.users / top * 100)}%)</span>` : ""}</div>
    </div>`).join("");

  // channels
  const chTotal = (r.by_channel || []).reduce((a, c) => a + c.events, 0) || 1;
  actFill("actChannels", r.by_channel, 5, "No actions in this period.", c => `<tr>
      <td>${actChannelChip(c.channel)} ${actEsc(c.label)}</td><td class="num">${actNum(c.events)}</td>
      <td class="num">${c.users}</td><td class="num">${c.errors || "-"}</td>
      <td style="width:30%"><div class="usage-bar-mini ch-bg-${actEsc(c.channel)}" style="width:${Math.max(2, Math.round(c.events / chTotal * 100))}%"></div></td></tr>`);

  // features
  document.getElementById("actCategories").innerHTML = (r.by_category || []).map(c =>
    `<span class="pill">${actEsc(c.category)} <b>${actNum(c.count)}</b> <span class="muted">· ${c.users} users</span></span>`).join("");
  actFill("actFeatures", (r.features || []).slice(0, 40), 7, "No actions in this period.", f => `<tr>
      <td>${actEsc(f.label)} <span class="muted mono act-code">${actEsc(f.action)}</span></td>
      <td>${actEsc(f.category)}</td><td class="num">${actNum(f.count)}</td><td class="num">${f.users}</td>
      <td class="num">${f.errors || "-"}</td><td class="num">${actMs(f.p95_ms)}</td>
      <td style="min-width:110px">${actStack(f.channels)}</td></tr>`);

  actFill("actTools", r.tools, 6, "No MCP or chat tool calls in this period.", x => `<tr>
      <td class="mono">${actEsc(x.tool)}</td><td class="num">${actNum(x.calls)}</td><td class="num">${x.users}</td>
      <td class="num">${x.errors || "-"}</td><td class="num">${actMs(x.p95_ms)}</td>
      <td style="min-width:90px">${actStack(x.channels)}</td></tr>`);

  actFill("actClients", r.mcp_clients, 5, "No MCP clients in this period.", c => `<tr>
      <td>${actEsc(c.client)} ${(c.channels || []).map(actChannelChip).join(" ")}</td>
      <td class="num">${actNum(c.calls)}</td><td class="num">${c.connects || "-"}</td>
      <td class="num">${c.users}</td><td>${actEsc(actWhen(c.last_seen))}</td></tr>`);

  // log parser
  const lp = r.log_parser || {};
  const storedPct = lp.parses ? Math.round((lp.stored || 0) / Math.max(1, lp.parses - (lp.failed || 0)) * 100) : 0;
  document.getElementById("actLogParser").innerHTML = `<div class="usage-kpis">
      ${actKpi(lp.parses || 0, "Logs parsed")}
      ${actKpi(lp.users || 0, "People")}
      ${actKpi(`${lp.total_mb || 0} MB`, "Raw log volume")}
      ${actKpi(lp.avg_kb != null ? `${lp.avg_kb} KB` : "-", "Avg log size", lp.max_kb ? `largest ${lp.max_kb} KB` : "")}
      ${actKpi(actMs(lp.p95_parse_ms), "p95 parse time", lp.p50_parse_ms != null ? `median ${actMs(lp.p50_parse_ms)}` : "")}
      ${actKpi(`${storedPct}%`, "Kept in library")}
      ${actKpi(lp.logs_with_exceptions || 0, "Logs with exceptions", `${lp.exceptions_found || 0} exceptions found`)}
      ${lp.via_incident ? actKpi(lp.via_incident, "Via incidents") : ""}
      ${lp.failed ? actKpi(lp.failed, "Failed parses") : ""}
    </div>
    ${lp.parses ? `<div class="act-sub">By channel</div>${actStack(lp.by_channel || {})}${actLegend()}` : ""}`;

  // users
  const me = typeof currentUsername === "function" ? currentUsername() : null;
  actFill("actUsers", r.by_user, 12, "No active accounts in this period.", u => `<tr class="act-user-row"
        data-user="${actEsc(u.username)}" title="Narrow the page to ${actEsc(u.username)}">
      <td>${actEsc(u.username)}${u.username === me ? " <span class='badge'>you</span>" : ""}</td>
      <td><span class="seg-tag">${actEsc(u.segment)}</span></td>
      <td class="num">${actNum(u.events)}</td><td class="num">${u.active_days}</td>
      <td class="num">${u.llm_turns || "-"}</td><td class="num">${u.mcp_calls || "-"}</td>
      <td class="num">${u.kb_lookups || "-"}</td><td class="num">${u.logs_parsed || "-"}</td>
      <td class="num">${u.incidents_filed || "-"}</td><td class="num">${u.resolutions || "-"}</td>
      <td>${(u.channels || []).map(actChannelChip).join(" ")}</td>
      <td>${actEsc(actWhen(u.last_seen))}</td></tr>`);
  // Delegated rather than inline onclick: no username ever has to survive
  // being escaped into a JavaScript string literal.
  document.getElementById("actUsers").onclick = e => {
    const tr = e.target.closest && e.target.closest("tr[data-user]");
    if (tr) focusActivityUser(tr.dataset.user);
  };

  actFill("actAccounts", r.by_account, 4, "No org-scoped actions in this period.", a => `<tr>
      <td>${a.account === "(unassigned)" ? "<span class='muted'>unassigned</span>" : actEsc(a.account)}</td>
      <td class="num">${a.orgs}</td><td class="num">${actNum(a.events)}</td><td class="num">${a.incidents || "-"}</td></tr>`);
  actFill("actOrgs", (r.by_org || []).slice(0, 15), 4, "No org-scoped actions in this period.", o => `<tr>
      <td>${actEsc(o.org_name || o.org_id)}${o.org_name && o.org_name !== o.org_id ? ` <span class="muted mono">${actEsc(o.org_id)}</span>` : ""}</td>
      <td class="num">${actNum(o.events)}</td><td class="num">${o.users}</td>
      <td>${actEsc(o.top_action_label || "-")}</td></tr>`);

  // hours
  const hrs = r.by_hour || [];
  const hPeak = Math.max(1, ...hrs);
  document.getElementById("actHours").innerHTML = `<div class="usage-chart act-hours">${hrs.map((v, h) =>
      `<div class="usage-bar-wrap" title="${String(h).padStart(2, "0")}:00 UTC: ${v} action(s)">
        <div class="usage-bar ${v ? "" : "empty"}" style="height:${v ? Math.max(2, Math.round(v / hPeak * 100)) : 0}%"></div></div>`).join("")}</div>
    <div class="usage-axis"><span>00</span><span>06</span><span>12</span><span>18</span><span>23</span></div>`;

  actFill("actErrors", (r.errors || []).slice(0, 10), 4, "No failed actions in this period.", e => `<tr>
      <td>${actEsc(e.label || e.action)}</td><td class="num">${actEsc(e.status)}</td>
      <td class="num">${e.count}</td><td class="num">${e.users}</td></tr>`);

  renderActFeed("actRecent", (r.recent || []).slice(0, 60), true);
}

// =====================================================================
// 4. executive summary (the Usage tab's default pane)
// =====================================================================
//
// One call to /api/usage/summary: the period's headline figures, the same
// figures for the period immediately before it, point-in-time inventory
// (orgs, accounts, components indexed) and the breakdowns underneath. The
// server scopes it -- an admin sees everyone, anyone else sees themselves --
// so this code renders whatever comes back and only hides the people table.

let EXEC_LAST = null;
let EXEC_SEQ = 0;
let EXEC_TREND_METRIC = "questions";
let EXEC_USERS_LOADED = false;

const EXEC_TREND_LABELS = { questions: "AI questions", tokens: "Tokens", actions: "Actions", active_users: "People active" };

function execIsAdmin() {
  return typeof CURRENT_USER !== "undefined" && !!CURRENT_USER && CURRENT_USER.role === "admin";
}

/** Change vs the previous period, as {html, text}. `lowerIsBetter` flips the
 *  colour (failures, answer time); `neutral` never colours (tokens, cost --
 *  more is not good or bad by itself). */
function execDelta(cur, prev, hasPrev, opts = {}) {
  const flat = (t, title = "") => ({ html: `<span class="exec-delta flat"${title ? ` title="${actEsc(title)}"` : ""}>${t}</span>`, text: t });
  if (!hasPrev) return flat("no prior data", "Nothing was recorded in the previous period");
  cur = cur || 0; prev = prev || 0;
  if (!cur && !prev) return flat("no change");
  const prevTxt = opts.fmt ? opts.fmt(prev) : actNum(prev);
  if (!prev) {
    return { html: `<span class="exec-delta up ${opts.neutral ? "neutral" : (opts.lowerIsBetter ? "bad" : "good")}"
      title="None in the previous period">new</span>`, text: "new (none in previous period)" };
  }
  const pct = Math.round(((cur - prev) / prev) * 100);
  if (pct === 0) return flat("no change", `Previous period: ${prevTxt}`);
  const up = pct > 0;
  const tone = opts.neutral ? "neutral" : (up !== !!opts.lowerIsBetter ? "good" : "bad");
  const arrow = up ? "&#9650;" : "&#9660;";
  return { html: `<span class="exec-delta ${up ? "up" : "down"} ${tone}" title="Previous period: ${actEsc(prevTxt)}">${arrow} ${Math.abs(pct)}%</span>`,
           text: `${up ? "up" : "down"} ${Math.abs(pct)}% vs previous (${prevTxt})` };
}

function execRangeLabel(r) {
  return Number(r.days) === 1 ? "Today" : `In the last ${r.days} days`;
}

function execScope(r) {
  if (!execIsAdmin()) return { self: true, label: "you" };
  if (r.scope && r.scope !== "all users") return { self: true, label: r.scope };
  return { self: false, label: "all users" };
}

/** The tiles, as data, so the page and "Copy summary" say the same thing. */
function execTiles(r) {
  const c = r.current || {}, p = r.previous || {}, inv = r.inventory || {};
  const has = !!r.previous_has_data;
  const scope = execScope(r);
  const d = (k, o) => execDelta(c[k], p[k], has, o);
  const none = { html: "", text: "" };
  const n = (v, one, many) => `${actNum(v || 0)} ${(v || 0) === 1 ? one : many}`;
  const tiles = [
    { key: "questions", label: "AI questions asked", value: actNum(c.questions), delta: d("questions"),
      note: c.failed_questions ? `${actNum(c.failed_questions)} failed` : (c.questions ? `${c.answer_rate}% answered` : "") },
    { key: "tokens", label: "Tokens consumed", value: actNum(c.tokens), delta: d("tokens", { neutral: true }),
      note: c.tokens ? `${actNum(c.prompt_tokens)} in · ${actNum(c.completion_tokens)} out` : "" },
    { key: "orgs", label: scope.self && !execIsAdmin() ? "Orgs you can see" : "Orgs connected", value: actNum(inv.orgs_total),
      delta: c.orgs_connected
        ? { html: `<span class="exec-delta up good">+${c.orgs_connected} new</span>`, text: `+${c.orgs_connected} new in period` }
        : { html: `<span class="exec-delta flat">none new</span>`, text: "none new in period" },
      note: `${c.orgs_worked_on || 0} worked on` },
    scope.self
      ? { key: "days", label: "Days active", value: actNum(c.active_days), delta: d("active_days"),
          note: n(c.actions, "action", "actions") }
      : { key: "people", label: "Active people", value: actNum(c.active_users), delta: d("active_users"),
          note: inv.users_total != null ? `of ${inv.users_total} accounts` : "" },
    { key: "accounts", label: "Customer accounts", value: actNum(inv.accounts_total),
      delta: none, note: `${c.accounts_worked_on || 0} worked on` },
    { key: "lookups", label: "Org lookups", value: actNum(c.kb_lookups), delta: d("kb_lookups"),
      note: c.mcp_calls ? n(c.mcp_calls, "MCP tool call", "MCP tool calls") : "" },
    { key: "logs", label: "Debug logs parsed", value: actNum(c.logs_parsed), delta: d("logs_parsed"),
      note: c.exceptions_found ? n(c.exceptions_found, "exception found", "exceptions found") : "" },
    { key: "incidents", label: "Incidents filed", value: actNum(c.incidents_filed), delta: d("incidents_filed"),
      note: n(c.resolutions, "fix recorded", "fixes recorded") },
  ];
  if (c.cost_available) {
    tiles.push({ key: "cost", label: "AI cost", value: typeof fmtMoney === "function" ? fmtMoney(c.cost) : String(c.cost),
                 delta: d("cost", { neutral: true, fmt: v => (typeof fmtMoney === "function" ? fmtMoney(v) : String(v)) }),
                 note: "as reported by the provider" });
  }
  return tiles;
}

/** One sentence a manager can read without looking at a single tile. */
function execHeadline(r, asText = false) {
  const c = r.current || {};
  const b = s => (asText ? s : `<b>${s}</b>`);
  const plural = (n, one, many) => `${actNum(n)} ${n === 1 ? one : many}`;
  if (!c.actions && !c.questions) return `${execRangeLabel(r)}: no activity recorded.`;
  const scope = execScope(r);
  const who = scope.self ? (scope.label === "you" ? "you" : (asText ? scope.label : actEsc(scope.label)))
                         : plural(c.active_users, "person", "people");
  const parts = [];
  if (c.questions) parts.push(`asked ${b(plural(c.questions, "AI question", "AI questions"))} (${b(actNum(c.tokens) + " tokens")})`);
  if (c.orgs_worked_on) {
    parts.push(`worked across ${b(plural(c.orgs_worked_on, "org", "orgs"))}` +
      (c.accounts_worked_on ? ` in ${b(plural(c.accounts_worked_on, "customer account", "customer accounts"))}` : ""));
  }
  const extras = [];
  if (c.kb_lookups) extras.push(plural(c.kb_lookups, "org lookup", "org lookups"));
  if (c.mcp_calls) extras.push(plural(c.mcp_calls, "MCP tool call", "MCP tool calls"));
  if (c.logs_parsed) extras.push(plural(c.logs_parsed, "log parsed", "logs parsed"));
  if (c.incidents_filed) extras.push(plural(c.incidents_filed, "incident filed", "incidents filed"));
  if (c.resolutions) extras.push(plural(c.resolutions, "fix recorded", "fixes recorded"));
  if (c.orgs_connected) extras.push(plural(c.orgs_connected, "new org connected", "new orgs connected"));
  if (!parts.length) parts.push(`took ${b(plural(c.actions, "action", "actions"))}`);
  let s = `${execRangeLabel(r)}, ${who} ${parts.join(" and ")}`;
  if (extras.length) s += ` &mdash; ${extras.join(", ")}`;
  s += ".";
  if (asText) s = s.replace(/&mdash;/g, "-");
  return s.charAt(0).toUpperCase() + s.slice(1);
}

async function populateExecUserPicker() {
  const sel = document.getElementById("execUser");
  if (!sel || !execIsAdmin() || EXEC_USERS_LOADED) return;
  const users = await apiJson("/api/admin/users", {}, null);
  if (!users) return;
  const current = sel.value;
  sel.innerHTML = `<option value="">Everyone</option>` + Object.keys(users).sort()
    .map(u => `<option value="${actEsc(u)}">${actEsc(u)}</option>`).join("");
  sel.value = current;
  EXEC_USERS_LOADED = true;
}

async function loadExecSummary() {
  const kpis = document.getElementById("execKpis");
  if (!kpis) return;
  const seq = ++EXEC_SEQ;
  const days = (document.getElementById("execDays") || {}).value || 30;
  const user = execIsAdmin() ? ((document.getElementById("execUser") || {}).value || "") : "";
  if (typeof setBusy === "function") setBusy(kpis);
  populateExecUserPicker();
  const r = await apiJson(`/api/usage/summary?days=${encodeURIComponent(days)}${
    user ? `&username=${encodeURIComponent(user)}` : ""}`, {}, null);
  if (seq !== EXEC_SEQ) return;   // a newer period was picked while this one loaded
  if (!r) { kpis.innerHTML = `<p class="status-line error">Could not load the summary.</p>`; return; }
  EXEC_LAST = r;
  renderExecSummary(r);
}

function renderExecSummary(r) {
  const c = r.current || {}, p = r.previous || {};
  const has = !!r.previous_has_data;
  const scope = execScope(r);

  const title = document.getElementById("execTitle");
  if (title) title.textContent = scope.self && scope.label !== "you"
    ? `Executive summary · ${scope.label}` : (scope.self ? "Your summary" : "Executive summary");
  const range = document.getElementById("execRange");
  if (range) range.innerHTML = `${actEsc(r.from)} &rarr; ${actEsc(r.to)} UTC
    <span class="muted">&middot; compared with ${actEsc(r.previous_from)} &rarr; ${actEsc(r.previous_to)}</span>`;
  document.getElementById("execHeadline").innerHTML = execHeadline(r);

  document.getElementById("execKpis").innerHTML = execTiles(r).map(t => `<div class="exec-kpi" data-kpi="${t.key}">
      <div class="exec-kpi-label">${actEsc(t.label)}</div>
      <div class="exec-kpi-value">${t.value}</div>
      <div class="exec-kpi-meta">${t.delta.html}${t.note ? `<span class="exec-kpi-note">${actEsc(t.note)}</span>` : ""}</div>
    </div>`).join("");

  const foot = document.getElementById("execFoot");
  if (foot) foot.innerHTML = `AI questions and tokens are the in-app assistant. Claude/Copilot over MCP runs on
    the client's own model, so it shows as MCP tool calls and org lookups, not tokens.
    ${c.cost_available ? "" : "Cost is not reported by this provider (Azure bills the subscription, not the call). "}
    Counts only &mdash; no question text, log content or customer data is kept.`;

  renderExecTrend(r);

  // --- this period vs the last
  const sec = v => `${v || 0}s`;
  const pctf = v => (v == null ? "-" : `${v}%`);
  const rows = [
    ["AI questions", "questions"],
    ["Tokens", "tokens", { neutral: true }],
    ["Avg tokens per question", "avg_tokens_per_question", { neutral: true }],
    ["Avg answer time", "avg_answer_seconds", { lowerIsBetter: true, fmt: sec }],
    ["Questions answered", "answer_rate", { fmt: pctf }],
    !scope.self ? ["Active people", "active_users"] : null,
    ["Days with activity", "active_days"],
    ["Actions, all channels", "actions"],
    ["MCP tool calls", "mcp_calls"],
    ["Org lookups", "kb_lookups"],
    ["Debug logs parsed", "logs_parsed"],
    ["Incidents filed", "incidents_filed"],
    ["Fixes recorded", "resolutions"],
    ["Orgs worked on", "orgs_worked_on"],
    ["Customer accounts worked on", "accounts_worked_on"],
    ["New orgs connected", "orgs_connected"],
    ["Org refreshes", "org_refreshes"],
    (c.new_users != null) ? ["New user accounts", "new_users"] : null,
    ["Failed actions", "errors", { lowerIsBetter: true }],
    c.cost_available ? ["AI cost", "cost", { neutral: true, fmt: v => fmtMoney(v) }] : null,
  ].filter(Boolean);
  // Two side-by-side tables rather than one long one: twenty rows in a
  // single column left the card beside it mostly empty.
  const half = Math.ceil(rows.length / 2);
  const table = part => `<table class="exec-compare"><thead><tr><th>Measure</th>
      <th class="num">This period</th><th class="num">Previous</th><th class="num">Change</th></tr></thead><tbody>${
    part.map(([label, k, o = {}]) => {
      const f = o.fmt || actNum;
      return `<tr><td>${actEsc(label)}</td><td class="num"><b>${f(c[k])}</b></td>
        <td class="num muted">${has ? f(p[k]) : "-"}</td><td class="num">${execDelta(c[k], p[k], has, o).html}</td></tr>`;
    }).join("")}</tbody></table>`;
  document.getElementById("execCompare").innerHTML =
    `<div class="exec-compare-grid">${table(rows.slice(0, half))}${table(rows.slice(half))}</div>`;

  // --- where the effort went
  actFill("execAccounts", r.top_accounts, 6, "No org-scoped activity in this period.", a => `<tr>
      <td>${a.account === "(unassigned)" ? "<span class='muted'>unassigned</span>" : actEsc(a.account)}</td>
      <td class="num">${a.orgs}</td><td class="num">${actNum(a.questions)}</td><td class="num">${actNum(a.tokens)}</td>
      <td class="num">${actNum(a.actions)}</td><td class="num">${a.incidents || "-"}</td></tr>`);
  actFill("execOrgs", r.top_orgs, 5, "No org-scoped activity in this period.", o => `<tr>
      <td>${actEsc(o.org_name || o.org_id)}${o.account ? ` <span class="muted">· ${actEsc(o.account)}</span>` : ""}</td>
      <td class="num">${actNum(o.questions)}</td><td class="num">${actNum(o.tokens)}</td>
      <td class="num">${actNum(o.actions)}</td><td class="num">${o.users}</td></tr>`);

  // --- how it's used
  const ch = r.channels || {};
  const chTotal = Object.values(ch).reduce((a, b) => a + b, 0);
  document.getElementById("execChannels").innerHTML = chTotal
    ? `${actStack(ch)}${actLegend()}<div class="exec-chips">${Object.entries(ch).sort((a, b) => b[1] - a[1]).map(([k, v]) =>
        `${actChannelChip(k)} <span class="muted">${Math.round(v / chTotal * 100)}%</span>`).join(" &nbsp; ")}</div>`
    : `<p class="muted">No actions in this period.</p>`;
  const feats = r.top_features || [];
  const fTop = Math.max(1, ...feats.map(f => f.count));
  document.getElementById("execFeatures").innerHTML = feats.length ? feats.map(f => `<div class="exec-bar-row">
      <div class="exec-bar-label">${actEsc(f.label)}</div>
      <div class="ladder-track"><div class="ladder-fill" style="width:${Math.max(3, Math.round(f.count / fTop * 100))}%"></div></div>
      <div class="exec-bar-num">${actNum(f.count)}</div></div>`).join("")
    : `<p class="muted">Nothing yet.</p>`;
  const models = r.top_models || [];
  document.getElementById("execModels").innerHTML = models.length ? models.map(m => `<div class="exec-bar-row">
      <div class="exec-bar-label mono">${actEsc(m.model)}</div>
      <div class="exec-bar-num wide">${actNum(m.questions)} Qs · ${actNum(m.tokens)} tokens</div></div>`).join("")
    : `<p class="muted">No AI questions in this period.</p>`;

  // --- people (admin, everyone)
  const card = document.getElementById("execPeopleCard");
  if (card) card.style.display = execIsAdmin() && r.top_users ? "" : "none";
  if (execIsAdmin() && r.top_users) {
    const me = typeof currentUsername === "function" ? currentUsername() : null;
    actFill("execPeople", r.top_users, 8, "No active accounts in this period.", u => `<tr class="act-user-row"
        data-user="${actEsc(u.username)}" title="Narrow the summary to ${actEsc(u.username)}">
      <td>${actEsc(u.username)}${u.username === me ? " <span class='badge'>you</span>" : ""}</td>
      <td class="num">${actNum(u.questions)}</td><td class="num">${actNum(u.tokens)}</td>
      <td class="num">${actNum(u.actions)}</td><td class="num">${u.logs || "-"}</td>
      <td class="num">${u.incidents || "-"}</td><td class="num">${u.active_days}</td>
      <td>${(u.channels || []).map(actChannelChip).join(" ")}</td></tr>`);
    document.getElementById("execPeople").onclick = e => {
      const tr = e.target.closest && e.target.closest("tr[data-user]");
      if (tr) focusExecUser(tr.dataset.user);
    };
  }
}

function focusExecUser(username) {
  const sel = document.getElementById("execUser");
  if (!sel) return;
  if (![...sel.options].some(o => o.value === username)) {
    const opt = document.createElement("option");
    opt.value = username; opt.textContent = username;
    sel.appendChild(opt);
  }
  sel.value = username;
  loadExecSummary();
  const hero = document.getElementById("execKpis");
  if (hero && hero.scrollIntoView) hero.scrollIntoView({ behavior: "smooth", block: "start" });
}

function setExecTrendMetric(metric) {
  if (!EXEC_TREND_LABELS[metric]) return;
  EXEC_TREND_METRIC = metric;
  document.querySelectorAll("#execTrendSwitch [data-metric]").forEach(b =>
    b.classList.toggle("active", b.dataset.metric === metric));
  if (EXEC_LAST) renderExecTrend(EXEC_LAST);
}

function renderExecTrend(r) {
  const host = document.getElementById("execTrend");
  if (!host) return;
  const days = r.trend || [];
  if (!days.length) { host.innerHTML = ""; return; }
  const k = EXEC_TREND_METRIC;
  const peak = Math.max(1, ...days.map(d => d[k] || 0));
  const total = days.reduce((a, d) => a + (d[k] || 0), 0);
  const bars = days.map(d => {
    const v = d[k] || 0;
    const h = v ? Math.max(2, Math.round((v / peak) * 100)) : 0;
    const title = `${d.date}: ${d.questions} question(s), ${actNum(d.tokens)} tokens, ${d.actions} action(s), ${d.active_users} active`;
    return `<div class="usage-bar-wrap" title="${actEsc(title)}"><div class="usage-bar ${v ? "" : "empty"}" style="height:${h}%"></div></div>`;
  }).join("");
  const label = EXEC_TREND_LABELS[k];
  const summary = k === "active_users"
    ? `Peak: ${peak} people in a day.`
    : `${actNum(total)} in the period · peak ${actNum(peak)} in a day.`;
  host.innerHTML = `<div class="usage-chart">${bars}</div>
    <div class="usage-axis"><span>${actEsc(r.from)}</span><span>${actEsc(r.to)}</span></div>
    <div class="usage-legend">${actEsc(label)} per day. ${summary} Hover a bar for that day.</div>`;
}

/** Plain text for Teams / email. Same tiles and sentence as the page. */
function execSummaryText(r) {
  const scope = execScope(r);
  const lines = [
    `TS Intelligent Debug Helper - usage summary`,
    `${execRangeLabel(r)} (${r.from} to ${r.to} UTC), ${scope.label === "you" ? currentUsername() || "you" : scope.label}`,
    "",
    execHeadline(r, true),
    "",
    ...execTiles(r).map(t => `- ${t.label}: ${t.value.replace(/&[a-z]+;/g, "")}` +
      (t.delta.text ? ` (${t.delta.text})` : "") + (t.note ? ` - ${t.note}` : "")),
  ];
  const accts = (r.top_accounts || []).filter(a => a.account !== "(unassigned)").slice(0, 5);
  if (accts.length) {
    lines.push("", "Top customer accounts:");
    accts.forEach(a => lines.push(`- ${a.account}: ${a.questions} questions, ${actNum(a.tokens)} tokens, ${a.actions} actions`));
  }
  return lines.join("\n");
}

async function copyExecSummary() {
  if (!EXEC_LAST) return;
  const text = execSummaryText(EXEC_LAST);
  trackUi("ui.copy", { target: "exec-summary" });
  try {
    await navigator.clipboard.writeText(text);
    if (typeof toast === "function") toast("Summary copied - paste it into Teams or an email.", "ok", 3000);
  } catch (e) {
    // Clipboard API needs a secure context (https or localhost). Fall back to
    // a selectable textarea rather than failing silently.
    const ta = document.createElement("textarea");
    ta.value = text; ta.setAttribute("readonly", ""); ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (e2) { ok = false; }
    ta.remove();
    if (typeof toast === "function") toast(ok ? "Summary copied." : "Could not copy - your browser blocked clipboard access.", ok ? "ok" : "error", 4000);
  }
}

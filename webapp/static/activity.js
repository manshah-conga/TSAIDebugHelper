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
// 2. the Usage tab's two panes
// =====================================================================

let USAGE_PANE = "activity";

function showUsagePane(name) {
  USAGE_PANE = name;
  document.querySelectorAll("#usageSwitch [data-usage-pane]").forEach(b => {
    const on = b.dataset.usagePane === name;
    b.classList.toggle("active", on);
    b.setAttribute("aria-selected", on ? "true" : "false");
  });
  const a = document.getElementById("usagePaneActivity");
  const t = document.getElementById("usagePaneTokens");
  if (a) a.hidden = name !== "activity";
  if (t) t.hidden = name !== "tokens";
  trackUi("ui.view", { view: `usage-${name}` });
  if (name === "activity") loadActivityView();
}

// The Usage tab still loads the token pane via app.js's loadUsageView; the
// activity pane loads alongside it so the default pane is never empty.
if (typeof window !== "undefined") {
  const wrapUsage = () => {
    const orig = window.loadUsageView;
    if (typeof orig !== "function" || orig.__actWrapped) return;
    const w = async function (...args) {
      const p = orig.apply(this, args);
      if (USAGE_PANE === "activity") loadActivityView();
      return p;
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

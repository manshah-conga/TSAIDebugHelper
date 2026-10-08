/* TS Intelligent Debug Helper -- the home page and the command palette.
 *
 * Layered over app.js (it reads ORGS, CURRENT_ORG, CURRENT_USER and uses the
 * shared helpers there) and consulted by it only through guarded calls, so
 * app.js still runs on its own in the render tests.
 *
 *   1. home data      -- /api/home, the admin strip, the reader's fixes feed,
 *                        the MCP setup card, the quota chip
 *   2. orgs           -- cards/table toggle, pins, freshness, the collapsible
 *                        Connect card
 *   3. "What's broken?" -- one input that routes whatever the engineer has in
 *                        hand to the right tool
 *   4. command palette -- Ctrl/Cmd+K
 */

const HOME = { summary: null, loadedAt: 0, loading: null, file: null, seq: 0, localPrefs: {} };
const STALE_DAYS = 30;

// ---------- small helpers ----------

function guideLoaded() {
  return typeof GUIDE !== "undefined" && !!GUIDE;
}

/** Preferences normally live in the server-side guide document. If that
 *  could not be loaded -- an older server without /api/me/guide, or a
 *  transient error -- they are kept in memory for this page instead, so a
 *  click on "Connect a new org" or a pin still does what it says. */
function homePrefs() {
  return guideLoaded() ? (GUIDE.prefs || {}) : HOME.localPrefs;
}

function savePrefs(patch) {
  if (guideLoaded() && typeof guideSavePrefs === "function") return guideSavePrefs(patch);
  Object.entries(patch).forEach(([k, v]) => { if (v === null) delete HOME.localPrefs[k]; else HOME.localPrefs[k] = v; });
  return Promise.resolve();
}

function canWriteRole() {
  return !!CURRENT_USER && CURRENT_USER.role !== "reader";
}

function daysSince(iso) {
  if (!iso) return null;
  let t = Date.parse(iso);
  if (Number.isNaN(t) && /^\d{8}T\d{6}Z$/.test(iso)) {
    t = Date.parse(`${iso.slice(0, 4)}-${iso.slice(4, 6)}-${iso.slice(6, 8)}T${iso.slice(9, 11)}:${iso.slice(11, 13)}:${iso.slice(13, 15)}Z`);
  }
  if (Number.isNaN(t)) return null;
  return Math.max(0, Math.floor((Date.now() - t) / 86400000));
}

function agoText(days) {
  if (days === null) return "never";
  if (days === 0) return "today";
  if (days === 1) return "yesterday";
  if (days < 60) return `${days}d ago`;
  return `${Math.round(days / 30)} months ago`;
}

function jsArg(s) {
  // For values interpolated into an inline onclick="fn('...')".
  return escapeHtml(String(s).replace(/\\/g, "\\\\").replace(/'/g, "\\'"));
}

function isPinned(id) {
  return (homePrefs().pinned_orgs || []).includes(id);
}

function pinButton(id) {
  const on = isPinned(id);
  return `<button type="button" class="pin-btn${on ? " on" : ""}" aria-pressed="${on}"
    title="${on ? "Unpin" : "Pin to the top"}" onclick="event.stopPropagation(); togglePin('${jsArg(id)}')">${
    on ? "&#9733;" : "&#9734;"}</button>`;
}

async function togglePin(id) {
  const pins = new Set(homePrefs().pinned_orgs || []);
  if (pins.has(id)) pins.delete(id); else pins.add(id);
  await savePrefs({ pinned_orgs: [...pins] });
  renderOrgsTable();
  renderHomeOrgs();
  renderOrgPicker();
  if (typeof renderDashHead === "function") renderDashHead();
}

// =====================================================================
// 1. home data
// =====================================================================

function initHome() {
  const input = document.getElementById("triageInput");
  if (input && !input.dataset.wired) {
    input.dataset.wired = "1";
    input.addEventListener("input", () => { autoGrow(input); showTriageKind(); });
    input.addEventListener("keydown", e => {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); runTriage(); }
    });
    input.addEventListener("paste", () => setTimeout(() => { autoGrow(input); showTriageKind(); }, 0));
    const file = document.getElementById("triageFile");
    file.addEventListener("change", () => { if (file.files.length) triageLog(file.files[0]); file.value = ""; });
    const card = document.getElementById("triageCard");
    card.addEventListener("dragover", e => { e.preventDefault(); card.classList.add("drag"); });
    card.addEventListener("dragleave", e => { if (e.target === card) card.classList.remove("drag"); });
    card.addEventListener("drop", e => {
      e.preventDefault(); card.classList.remove("drag");
      const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      if (f) triageLog(f);
    });
  }
  const kbd = document.getElementById("paletteKbd");
  if (kbd && /Mac|iPhone|iPad/.test(navigator.platform || "")) kbd.textContent = "\u2318K";

  // Role-specific wording. A reader cannot upload a log, so their bar is a
  // lookup rather than a diagnosis.
  const reader = !canWriteRole();
  document.getElementById("triageTitle").textContent = reader ? "Look something up" : "What's broken?";
  document.getElementById("triageSub").innerHTML = reader
    ? "Paste an exception message, a field API name or a component name. Exception text is checked "
      + "against Known Issues in every org you can see."
    : "Paste an exception message, a field API name or a component name &mdash; or drop a debug log. "
      + "It goes to the right tool and checks Known Issues in every org you can see.";
  document.getElementById("triageGo").textContent = reader ? "Look up" : "Diagnose";
  showTriageKind();

  loadHome(true);
  refreshQuotaChip();
  if (!HOME.quotaTimer) HOME.quotaTimer = setInterval(refreshQuotaChip, 120000);
}

async function loadHome(force = false) {
  if (!CURRENT_USER) return;
  if (!force && Date.now() - HOME.loadedAt < 15000) { renderHome(); return; }
  if (HOME.loading) return HOME.loading;
  HOME.loading = (async () => {
    const s = await apiJson("/api/home", {}, null);
    HOME.loading = null;
    if (!s) return;
    HOME.summary = s;
    HOME.loadedAt = Date.now();
    renderHome();
  })();
  return HOME.loading;
}

function renderHome() {
  renderAdminStrip();
  renderReaderFixes();
  renderMcpCard();
  renderHomeOrgs();
  if (typeof renderDashHead === "function") renderDashHead();
  showTriageKind();
}

function renderAdminStrip() {
  const el = document.getElementById("adminStrip");
  if (!el) return;
  const a = HOME.summary && HOME.summary.admin;
  if (!a || !CURRENT_USER || CURRENT_USER.role !== "admin") { el.innerHTML = ""; return; }
  const ids = Object.keys(ORGS);
  const stale = ids.filter(id => (daysSince(ORGS[id].last_extracted_at) ?? 0) > STALE_DAYS).length;
  const chip = (cls, label, value, onclick, title = "") =>
    `<button type="button" class="health-chip ${cls}" onclick="${onclick}" title="${escapeHtml(title)}">
       <span class="health-label">${escapeHtml(label)}</span><span class="health-value">${value}</span></button>`;
  const llm = a.llm || {};
  const top = (a.top_users || []).map(u => `${escapeHtml(u.username)} ${fmtCompact(u.total_tokens)}`).join(" &middot; ");
  el.innerHTML = `
    <span class="health-title">System health</span>
    ${chip(llm.configured && !llm.problem ? "ok" : "bad", "LLM",
           llm.configured ? `${escapeHtml(llm.provider || "")}${llm.default_model ? " &middot; " + escapeHtml(llm.default_model) : ""}${llm.problem ? " &middot; problem" : " &middot; ready"}` : "not configured",
           "showView('usage')", llm.problem || "")}
    ${chip(a.unverified.length ? "warn" : "ok", "Signups waiting",
           a.unverified.length ? `${a.unverified.length} to verify` : "none",
           "showView('admin')", a.unverified.join(", "))}
    ${chip("neutral", "This week", `${fmtCompact(a.week_tokens)} tokens${top ? " &middot; " + top : ""}`,
           "showView('usage')", "Heaviest users over the last 7 days")}
    ${chip(stale ? "warn" : "ok", "Orgs", `${ids.length}${stale ? ` &middot; ${stale} stale` : ""}`,
           "document.getElementById('orgsCard').scrollIntoView({behavior:'smooth'})",
           `Stale = not refreshed in ${STALE_DAYS} days`)}`;
}

function renderReaderFixes() {
  const host = document.getElementById("readerFixes");
  if (!host) return;
  if (!CURRENT_USER || CURRENT_USER.role !== "reader") { host.innerHTML = ""; return; }
  const fixes = (HOME.summary && HOME.summary.recent_fixes) || [];
  host.innerHTML = `<div class="card" data-tour="known-feed">
      <h2>Latest fixes on file</h2>
      <p class="muted">The most recently recorded resolutions across every org you can see.</p>
      ${fixes.length ? fixes.map(f => `
        <div class="known resolved">
          <div class="known-head">
            <span class="badge low">FIX ON FILE</span>
            <b>${escapeHtml(f.kind === "field_report" ? `Field report: ${f.field}` : (f.type || "Exception"))}</b>
            <span class="badge recurrence">${f.occurrences}&times;</span>
            <span class="muted">in ${escapeHtml(f.org_id)}</span>
          </div>
          ${f.message_sample ? `<div class="known-msg">${escapeHtml(f.message_sample)}</div>` : ""}
          <div class="known-res"><b>Fix:</b> ${escapeHtml(f.resolution)}
            ${f.resolution_recorded_at ? `<span class="muted"> -- recorded ${fmtWhen(f.resolution_recorded_at)}</span>` : ""}</div>
          <div class="known-actions"><button class="secondary"
            onclick="openKnownIssue('${jsArg(f.org_id)}', '${jsArg(f.signature)}')">Open in Known Issues</button></div>
        </div>`).join("")
      : emptyStateHtml({ title: "No fixes recorded yet",
          body: "When someone records a fix against an incident in an org you can see, it appears here.",
          actions: [{ label: "Play the demo case", onclick: "startDemo()" }] })}
    </div>`;
  if (typeof decorateCards === "function") decorateCards(host);
}

function mcpSetupHtml() {
  const url = `${location.origin}/mcp`;
  const code = (s, id) => `<div class="copy-row"><code id="${id}">${escapeHtml(s)}</code>
      <button type="button" class="secondary" onclick="copyText('${id}')">Copy</button></div>`;
  return `
    <ol class="setup-steps">
      <li><b>Create an API token</b> on the API Tokens tab. It carries your role and your org
        visibility, and it is shown once.
        <div><button type="button" class="secondary" onclick="closeAnyModal(); showView('tokens');
          document.getElementById('tokenLabel').focus()">Create a token</button></div></li>
      <li><b>Claude Desktop / claude.ai:</b> Settings &rarr; Connectors &rarr; Add custom connector, with this URL:
        ${code(url, "mcpUrl")}
        <span class="muted">If the connector form has no header field, append <span class="mono">?token=&lt;token&gt;</span>
        &mdash; use a reader token with a short expiry for that, since URLs end up in logs.</span></li>
      <li><b>Claude Code:</b>
        ${code(`claude mcp add --transport http ts-debug-helper ${url} --header "Authorization: Bearer <token>"`, "mcpCli")}</li>
      <li><b>Try it:</b> ask Claude <i>"Which orgs can I see in the TS Debug Helper?"</i></li>
    </ol>`;
}

function renderMcpCard() {
  const host = document.getElementById("mcpCardHost");
  if (!host) return;
  const s = HOME.summary;
  if (!s || s.has_api_token || homePrefs().mcp_card_dismissed) { host.innerHTML = ""; return; }
  host.innerHTML = `<div class="card mcp-card" data-tour="mcp">
      <div class="card-head"><h2>Use these tools from Claude Desktop</h2>
        <button type="button" class="link-btn" onclick="dismissMcpCard()">Not now</button></div>
      <p class="muted">Everything on this site is also available to Claude as MCP tools, so you can
        investigate from the chat you already have open. Three steps:</p>
      ${mcpSetupHtml()}
    </div>`;
  if (typeof decorateCards === "function") decorateCards(host);
}

async function dismissMcpCard() {
  await savePrefs({ mcp_card_dismissed: true });
  renderMcpCard();
  toast("Hidden. The setup steps are always in Help (the ? in the header).", "info", 6000);
}

function openMcpSetup() {
  const back = document.createElement("div");
  back.className = "modal-backdrop";
  back.innerHTML = `<div class="modal modal-wide" role="dialog" aria-modal="true">
      <h3>Connect Claude to the TS Debug Helper</h3>${mcpSetupHtml()}
      <div class="modal-actions"><button type="button" class="primary" data-close>Done</button></div></div>`;
  back.onclick = e => { if (e.target === back || e.target.hasAttribute("data-close")) back.remove(); };
  document.body.appendChild(back);
}

function closeAnyModal() {
  document.querySelectorAll(".modal-backdrop").forEach(b => { if (!b.id) b.remove(); });
}

async function copyText(id) {
  const text = document.getElementById(id).textContent;
  try { await navigator.clipboard.writeText(text); toast("Copied.", "ok", 2500); }
  catch (e) { toast("Clipboard blocked -- select the text and copy it manually.", "error"); }
}

async function refreshQuotaChip() {
  const chip = document.getElementById("quotaChip");
  if (!chip || !CURRENT_USER) return;
  const r = await apiJson("/api/usage/me?days=1", {}, null);
  const q = r && r.quota;
  if (!q) { chip.style.display = "none"; return; }
  chip.style.display = "";
  if (q.unlimited) {
    chip.className = "quota-chip ok";
    chip.innerHTML = `<span class="quota-chip-dot"></span>LLM: no cap`;
    chip.title = "No LLM limit on this account.";
    return;
  }
  // Whichever of the two windows is tighter is the one that matters.
  const parts = [q.daily, q.window].filter(p => p && p.limit !== null && p.limit !== undefined);
  if (!parts.length) { chip.className = "quota-chip ok"; chip.innerHTML = `<span class="quota-chip-dot"></span>LLM: no cap`; return; }
  const tight = parts.reduce((a, b) => (b.pct > a.pct ? b : a));
  const left = Math.max(0, Math.round(100 - tight.pct));
  const band = q.exceeded || tight.exceeded ? "over" : tight.pct >= 75 ? "warn" : "ok";
  chip.className = `quota-chip ${band}`;
  chip.innerHTML = `<span class="quota-chip-dot"></span>${band === "over" ? "LLM allowance used up" : `LLM ${left}% left`}`;
  chip.title = `${fmtCompact(tight.used)} of ${fmtCompact(tight.limit)} tokens used`
    + `${tight === q.daily ? " today" : ` in the last ${q.window_days} days`}. Click for details.`;
}

// =====================================================================
// 2. orgs: cards, table, connect card
// =====================================================================

function setOrgView(view) {
  savePrefs({ org_view: view });
  applyOrgView(view);
}

function applyOrgView(view) {
  view = view || homePrefs().org_view || "cards";
  document.getElementById("orgCards").style.display = view === "cards" ? "" : "none";
  document.getElementById("orgTableWrap").style.display = view === "table" ? "" : "none";
  document.getElementById("orgViewCards").classList.toggle("on", view === "cards");
  document.getElementById("orgViewTable").classList.toggle("on", view === "table");
}

// ---------- account grouping (Home) ----------
//
// The org list is grouped by customer account: a heading per account with
// its environment mix, freshness and open incidents, and the orgs stacked
// under it. On a wide screen a rail on the left lists the accounts, so a
// person supporting twenty customers picks one instead of scrolling past
// everyone else's sandboxes. Collapsed and pinned accounts are per-person
// preferences (collapsed_accounts / pinned_accounts), like pinned orgs.

function orgFilterText() {
  const el = document.getElementById("orgFilter");
  return el ? (el.value || "").trim().toLowerCase() : "";
}

function orgMatchesFilter(id, o) {
  const q = orgFilterText();
  if (!q) return true;
  return [id, o.name, o.owner, o.account, ENV_META[orgEnv(o)]?.label]
    .some(v => String(v || "").toLowerCase().includes(q));
}

/** The account the rail has narrowed the list to, or null for all. */
function accountFocusKey() {
  const k = HOME.accountFocus;
  if (!k) return null;
  // Forget a focus on an account that no longer exists (renamed, emptied).
  if (!Object.values(ORGS).some(o => accountKey(o.account) === k)) { HOME.accountFocus = null; return null; }
  return k;
}

function accountInFocus(key) {
  const f = accountFocusKey();
  return !f || f === key;
}

function focusAccount(key) {
  HOME.accountFocus = key && HOME.accountFocus !== key ? key : null;
  renderHomeOrgs();
  renderOrgsTable();
  markInFlightOrgs();
}

async function toggleAccountCollapsed(key) {
  const set = new Set(homePrefs().collapsed_accounts || []);
  if (set.has(key)) set.delete(key); else set.add(key);
  await savePrefs({ collapsed_accounts: [...set] });
  renderHomeOrgs(); renderOrgsTable(); markInFlightOrgs();
}

async function setAllAccountsCollapsed(collapsed) {
  const keys = accountGroups(Object.entries(ORGS)).map(g => g.key);
  await savePrefs({ collapsed_accounts: collapsed ? keys : [] });
  renderHomeOrgs(); renderOrgsTable(); markInFlightOrgs();
}

async function toggleAccountPin(key) {
  const set = new Set(homePrefs().pinned_accounts || []);
  if (set.has(key)) set.delete(key); else set.add(key);
  await savePrefs({ pinned_accounts: [...set] });
  renderHomeOrgs(); renderOrgsTable(); renderOrgPicker();
}

const AVATAR_TINTS = ["purple", "green", "yellow", "orange", "harbor"];

function accountAvatar(g) {
  if (g.key === UNASSIGNED_KEY) return `<span class="acct-avatar unassigned" aria-hidden="true">?</span>`;
  const words = g.name.split(/[\s\-_.]+/).filter(Boolean);
  const initials = (words.length > 1 ? words[0][0] + words[1][0] : g.name.slice(0, 2)).toUpperCase();
  let h = 0;
  for (const ch of g.key) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return `<span class="acct-avatar tint-${AVATAR_TINTS[h % AVATAR_TINTS.length]}" aria-hidden="true">${escapeHtml(initials)}</span>`;
}

/** Roll-up of one account's orgs for its heading and the rail. */
function accountStats(g) {
  const stats = (HOME.summary && HOME.summary.orgs) || {};
  let stale = 0, unresolved = 0, incidents = 0, manageable = 0, freshest = null;
  g.orgs.forEach(([id, o]) => {
    const d = daysSince(o.last_extracted_at);
    if (d !== null && d > STALE_DAYS) stale++;
    if (d !== null && (freshest === null || d < freshest)) freshest = d;
    const st = stats[id] || {};
    unresolved += st.unresolved || 0;
    incidents += st.incidents || 0;
    if (o.can_manage) manageable++;
  });
  return { stale, unresolved, incidents, manageable, freshest,
           all_manageable: manageable === g.orgs.length, has_active: g.orgs.some(([id]) => id === CURRENT_ORG) };
}

/** One account heading. Used by the cards (full) and as the table's group
 *  row (`compact`). Clicking the name area folds the group. */
function accountHeadHtml(g, { compact = false } = {}) {
  const s = accountStats(g);
  const k = jsArg(g.key);
  const unassigned = g.key === UNASSIGNED_KEY;
  const filtering = !!orgFilterText() || !!accountFocusKey();
  const collapsed = !filtering && isAccountCollapsed(g.key);
  const pinned = isAccountPinned(g.key);
  const chips = [
    s.unresolved ? `<span class="acct-chip warn" title="Incidents with no fix recorded yet">${s.unresolved} without a fix</span>` : "",
    s.stale ? `<span class="acct-chip stale" title="Not refreshed in ${STALE_DAYS} days">${s.stale} stale</span>` : "",
    collapsed && s.has_active ? `<span class="active-tag">active org inside</span>` : "",
  ].join("");
  const acts = [];
  if (!unassigned && canWriteRole())
    acts.push(`<button type="button" class="link-btn" onclick="event.stopPropagation(); connectToAccount('${jsArg(g.name)}')" title="Connect another org for ${escapeHtml(g.name)}">+ Add org</button>`);
  if (!unassigned && s.all_manageable)
    acts.push(`<button type="button" class="link-btn" onclick="event.stopPropagation(); renameAccount('${jsArg(g.name)}')">Rename</button>`);
  if (unassigned && s.manageable)
    acts.push(`<button type="button" class="link-btn" onclick="event.stopPropagation(); organizeUnassigned()">Organize into accounts</button>`);
  return `<div class="acct-head${compact ? " compact" : ""}${unassigned ? " unassigned" : ""}">
      <button type="button" class="acct-toggle" aria-expanded="${!collapsed}" ${filtering ? "disabled" : ""}
              onclick="toggleAccountCollapsed('${k}')" title="${collapsed ? "Expand" : "Collapse"}">
        <span class="acct-chevron${collapsed ? "" : " open"}" aria-hidden="true">&#9656;</span>
        ${accountAvatar(g)}
        <span class="acct-title">
          <span class="acct-name">${unassigned ? "Unassigned" : escapeHtml(g.name)}</span>
          <span class="acct-meta">${g.orgs.length} org${g.orgs.length === 1 ? "" : "s"} &middot; ${escapeHtml(envSummary(g.orgs))}${
            unassigned ? " &middot; not grouped under a customer yet" : ""}${
            s.freshest !== null ? ` &middot; last refresh ${agoText(s.freshest)}` : ""}</span>
        </span>
      </button>
      <span class="acct-chips">${chips}</span>
      <span class="acct-actions">${acts.join("")}
        ${unassigned ? "" : `<button type="button" class="pin-btn${pinned ? " on" : ""}" aria-pressed="${pinned}"
          title="${pinned ? "Unpin account" : "Pin account to the top"}"
          onclick="event.stopPropagation(); toggleAccountPin('${k}')">${pinned ? "&#9733;" : "&#9734;"}</button>`}
      </span>
    </div>`;
}

/** The left rail: every account with its org count and a warning dot. */
function accountRailHtml(groups) {
  const focus = accountFocusKey();
  const total = groups.reduce((n, g) => n + g.orgs.length, 0);
  const item = (g) => {
    const s = accountStats(g);
    const dot = s.unresolved ? "warn" : s.stale ? "stale" : "";
    return `<button type="button" class="rail-item${focus === g.key ? " on" : ""}${g.key === UNASSIGNED_KEY ? " unassigned" : ""}"
        onclick="focusAccount('${jsArg(g.key)}')" aria-pressed="${focus === g.key}"
        title="${escapeHtml(g.name || "Unassigned")}: ${g.orgs.length} org(s)${s.unresolved ? `, ${s.unresolved} incident(s) without a fix` : ""}${s.stale ? `, ${s.stale} stale` : ""}">
        ${accountAvatar(g)}
        <span class="rail-name">${g.key === UNASSIGNED_KEY ? "Unassigned" : escapeHtml(g.name)}</span>
        ${isAccountPinned(g.key) ? `<span class="rail-pin" aria-label="pinned">&#9733;</span>` : ""}
        ${dot ? `<span class="rail-dot ${dot}" aria-hidden="true"></span>` : ""}
        <span class="rail-count">${g.orgs.length}</span>
      </button>`;
  };
  return `<div class="rail-title">Accounts</div>
    <button type="button" class="rail-item all${focus ? "" : " on"}" onclick="focusAccount(null)" aria-pressed="${!focus}">
      <span class="acct-avatar all" aria-hidden="true">&#9776;</span><span class="rail-name">All accounts</span>
      <span class="rail-count">${total}</span></button>
    <div class="rail-sep"></div>
    ${groups.map(item).join("")}`;
}

function renderHomeOrgs() {
  const host = document.getElementById("orgCards");
  if (!host) return;
  applyOrgView();
  applyConnectState();
  refreshAccountOptions();
  const q = orgFilterText();
  const stats = (HOME.summary && HOME.summary.orgs) || {};
  const all = Object.entries(ORGS);
  document.getElementById("orgFilter").style.display = all.length > 4 ? "" : "none";

  const allGroups = accountGroups(all);
  const headers = showAccountHeaders(allGroups);
  const rail = document.getElementById("acctRail");
  const railOn = headers && allGroups.length >= 3;
  if (rail) {
    rail.innerHTML = railOn ? accountRailHtml(allGroups) : "";
    rail.style.display = railOn ? "" : "none";
    document.getElementById("orgsLayout").classList.toggle("with-rail", railOn);
  }
  if (!railOn) HOME.accountFocus = null;
  const tools = document.getElementById("acctTools");
  if (tools) tools.style.display = headers && allGroups.length > 1 ? "" : "none";
  renderAccountNudge(allGroups, headers);

  if (!all.length) {
    host.innerHTML = emptyStateHtml({
      icon: "&#9729;",
      title: "No orgs you can see yet",
      body: canWriteRole()
        ? "Connecting an org fetches its Apex, triggers, Flows, Process Builder, Workflow field updates and LWC "
          + "metadata, and builds the knowledgebase every other tab works from. It takes a few minutes on a large org."
        : "Readers work against orgs other people have connected. Ask a colleague to make theirs public "
          + "-- or see what an investigation looks like with the demo case.",
      actions: [
        { label: "Connect your first org", onclick: "toggleConnect(true)", primary: true, role: "user" },
        { label: "Play the demo case", onclick: "startDemo()", primary: !canWriteRole() },
      ],
    });
    return;
  }

  const groups = accountGroups(all.filter(([id, o]) => orgMatchesFilter(id, o))).filter(g => accountInFocus(g.key));
  if (!groups.length) {
    host.innerHTML = `<p class="muted">No org matches "${escapeHtml(q)}".</p>`;
    return;
  }
  if (!headers) {
    host.innerHTML = `<div class="org-cards">${groups[0].orgs.map(([id, o]) => orgCardHtml(id, o, stats[id])).join("")}</div>`;
  } else {
    const expandAll = !!q || !!accountFocusKey();
    host.innerHTML = groups.map(g => {
      const collapsed = !expandAll && isAccountCollapsed(g.key);
      return `<section class="acct-group${collapsed ? " collapsed" : ""}${g.key === UNASSIGNED_KEY ? " unassigned" : ""}" data-account="${escapeHtml(g.key)}">
          ${accountHeadHtml(g)}
          ${collapsed ? "" : `<div class="org-cards">${g.orgs.map(([id, o]) => orgCardHtml(id, o, stats[id])).join("")}</div>`}
        </section>`;
    }).join("");
  }
  showTriageKind();   // its "lookups use <org>" line follows the active org
}

/** Nobody has used accounts yet: one slim prompt to start, instead of a
 *  single "Unassigned" heading over everything. */
function renderAccountNudge(groups, headers) {
  const el = document.getElementById("acctNudge");
  if (!el) return;
  const manageable = Object.values(ORGS).some(o => o.can_manage && !o.account);
  if (headers || !manageable || Object.keys(ORGS).length < 2) { el.innerHTML = ""; return; }
  el.innerHTML = `<div class="acct-nudge">
      <span><b>Group orgs by customer.</b> Put a customer's production org and its sandboxes under one
        account so they stack together here, in the org picker and in search. Suggestions come from each
        org's My Domain.</span>
      <button type="button" class="primary" onclick="organizeUnassigned()">Organize into accounts</button>
    </div>`;
}

function orgCardHtml(id, o, st) {
  const c = o.component_counts || {};
  const days = daysSince(o.last_extracted_at);
  const stale = days !== null && days > STALE_DAYS;
  const fresh = days === null ? "unknown" : days <= 7 ? "ok" : stale ? "stale" : "warn";
  const num = n => (n === undefined || n === null ? "-" : Number(n).toLocaleString());
  const active = id === CURRENT_ORG;
  const a = jsArg(id);
  let incidents = `<span class="muted">No incidents filed yet</span>`;
  if (st && st.incidents) {
    incidents = `<a class="link" onclick="setActiveOrg('${a}'); showView('incidents')">${st.incidents} incident${st.incidents === 1 ? "" : "s"}</a>`
      + (st.unresolved ? ` &middot; <a class="link warn-text" onclick="openUnresolved('${a}')">${st.unresolved} without a fix</a>`
                       : st.known ? ` &middot; <span class="ok-text">all fixes recorded</span>` : "")
      + (st.last_incident_at ? ` &middot; <span class="muted">last ${agoText(daysSince(st.last_incident_at))}</span>` : "");
  }
  return `<div class="org-card${active ? " active" : ""}${stale ? " stale" : ""}" data-org="${escapeHtml(id)}">
      <div class="org-card-top">
        ${pinButton(id)}
        <div class="org-card-title">
          <a class="org-card-name" title="${escapeHtml(o.name || id)}" onclick="openOrgDashboard('${a}')">${escapeHtml(o.name || id)}</a>
          <div class="muted mono">${envBadge(o)}${escapeHtml(id)}${active ? ` <span class="active-tag">active</span>` : ""}</div>
        </div>
        <div class="org-card-vis">${visibilityCell(id, o)}</div>
      </div>
      <div class="org-card-counts">
        <div><b>${num(c.apex_classes)}</b><span>Classes</span></div>
        <div><b>${num(c.apex_triggers)}</b><span>Triggers</span></div>
        <div><b>${num(c.flows)}</b><span>Flows</span></div>
        <div><b>${num(c.lwc_components)}</b><span>LWC</span></div>
      </div>
      <div class="org-card-fresh">
        <span class="fresh-dot ${fresh}" aria-hidden="true"></span>
        ${stale ? `<b>Stale</b> &mdash; refreshed ${agoText(days)}; refresh before trusting results`
                : `Refreshed ${agoText(days)}`}
        <span class="muted">&middot; owner ${o.owner ? escapeHtml(o.owner) : "(none)"}</span>
        ${changesHint(o)}
      </div>
      <div class="org-card-incidents">${incidents}</div>
      <div class="org-card-inflight"></div>
      <div class="org-card-actions">
        <button type="button" class="secondary" onclick="setActiveOrg('${a}'); openChatFull()">Ask</button>
        <button type="button" class="secondary" onclick="openOrgDashboard('${a}')">Dashboard</button>
        <button type="button" class="secondary" onclick="setActiveOrg('${a}'); showView('incidents')">Incidents</button>
        ${o.can_manage ? `<button type="button" class="secondary${stale ? " emphasis" : ""}" onclick="refreshOrg('${a}')">Refresh</button>` : ""}
        <span class="org-card-links">
          ${o.can_manage ? `<button type="button" class="link-btn" onclick="moveOrgToAccount('${a}')" title="Change which customer account this org is under">Move</button>` : ""}
          ${!active ? `<button type="button" class="link-btn" onclick="setActiveOrg('${a}')">Make active</button>` : ""}
        </span>
      </div>
    </div>`;
}

// ---------- account actions ----------

/** Best guess at an org's account: a sibling on the same My Domain that
 *  already has one, else the My Domain name itself. */
function suggestAccountFor(url, skipId = null) {
  const dom = myDomainOf(url);
  if (!dom) return { suggestion: "", matched: null, dom: null };
  const sib = Object.entries(ORGS).find(([id, o]) => id !== skipId && o.account && orgMyDomain(o) === dom);
  const matched = sib ? accountDisplayName(sib[1].account) : null;
  return { suggestion: matched || dom, matched, dom };
}

async function patchOrgAccount(id, account) {
  const res = await api(`/api/orgs/${encodeURIComponent(id)}/account`, {
    method: "PATCH", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ account: account || null }),
  });
  if (!res.ok) return { ok: false, error: await errorText(res) };
  return { ok: true, ...(await res.json()) };
}

async function moveOrgToAccount(id) {
  const o = ORGS[id] || {};
  const guess = o.account ? null : suggestAccountFor(o.instance_url, id);
  const answer = await modal({
    title: `Move ${id}`,
    body: `Choose the customer account <b>${escapeHtml(o.name || id)}</b> belongs to. Pick an existing
           account to stack it with that customer's other orgs, or type a new name.`,
    fields: [{ name: "account", label: "Customer account", value: o.account || (guess && guess.suggestion) || "",
               placeholder: "e.g. Acme Corp", options: accountNames(),
               hint: o.account ? "Clear the box to leave it unassigned."
                 : guess && guess.matched ? `Suggested: another org on the <span class="mono">${escapeHtml(guess.dom)}</span> My Domain is under <b>${escapeHtml(guess.matched)}</b>.`
                 : guess && guess.dom ? `Suggested from the org's My Domain (<span class="mono">${escapeHtml(guess.dom)}</span>) &mdash; rename it to the customer's name if you like.`
                 : "Clear the box to leave it unassigned." }],
    submitLabel: "Move",
  });
  if (!answer) return;
  const next = answer.account.replace(/\s+/g, " ").trim();
  if (accountKey(next) === accountKey(o.account)) return;
  const r = await patchOrgAccount(id, next);
  if (!r.ok) { toast("Could not move the org: " + r.error, "error"); return; }
  toast(r.account ? `${id} is now under ${r.account}.` : `${id} is no longer under an account.`, "ok");
  await loadOrgs();
}

async function renameAccount(name) {
  const members = Object.entries(ORGS).filter(([, o]) => accountKey(o.account) === accountKey(name));
  const answer = await modal({
    title: `Rename ${name}`,
    body: `Renames the account on its ${members.length} org${members.length === 1 ? "" : "s"}. Typing the name of
           another account merges the two.`,
    fields: [{ name: "account", label: "Account name", value: name, options: accountNames().filter(n => accountKey(n) !== accountKey(name)),
               hint: "Clear the box to ungroup these orgs." }],
    submitLabel: "Rename",
  });
  if (!answer) return;
  const next = answer.account.replace(/\s+/g, " ").trim();
  if (next === name) return;
  const into = accountNames().find(n => accountKey(n) === accountKey(next) && accountKey(n) !== accountKey(name));
  if (into && !await confirmModal(`Merge ${name} into ${into}?`,
      `The ${members.length} org(s) under ${escapeHtml(name)} will be listed under ${escapeHtml(into)}.`, "Merge")) return;
  if (!next && !await confirmModal(`Ungroup ${name}?`,
      `Its ${members.length} org(s) go back to Unassigned. Nothing about the orgs themselves changes.`, "Ungroup")) return;
  const res = await api("/api/accounts/rename", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ from_account: name, to_account: next || null }),
  });
  if (!res.ok) { toast("Could not rename: " + await errorText(res), "error"); return; }
  const r = await res.json();
  // Carry the person's fold/pin choices over to the new name.
  const oldKey = accountKey(name), newKey = accountKey(r.to);
  const prefs = homePrefs(), patch = {};
  ["collapsed_accounts", "pinned_accounts"].forEach(k => {
    const list = prefs[k] || [];
    if (list.includes(oldKey)) patch[k] = [...new Set(list.filter(x => x !== oldKey).concat(r.to ? [newKey] : []))];
  });
  if (Object.keys(patch).length) await savePrefs(patch);
  if (HOME.accountFocus === oldKey) HOME.accountFocus = r.to ? newKey : null;
  toast(r.to ? `${name} renamed to ${r.to} (${r.orgs.length} org(s)).` : `${name} ungrouped.`, "ok");
  await loadOrgs();
}

/** Bulk-assign every unassigned org this person manages, with suggestions
 *  pre-filled so the common case is one click. */
async function organizeUnassigned() {
  const rows = Object.entries(ORGS).filter(([, o]) => !o.account && o.can_manage)
    .map(([id, o]) => ({ id, o, ...suggestAccountFor(o.instance_url, id) }))
    .sort((a, b) => (a.suggestion || "~").localeCompare(b.suggestion || "~") || a.id.localeCompare(b.id));
  if (!rows.length) { toast("Every org you manage is already under an account.", "info"); return; }
  const names = accountNames();
  const back = document.createElement("div");
  back.className = "modal-backdrop";
  back.innerHTML = `
    <div class="modal modal-wide organize-modal" role="dialog" aria-modal="true">
      <h3>Organize orgs into accounts</h3>
      <p class="muted">Each org is pre-filled with a suggestion: the account of another org on the same
        My Domain (a sandbox shares its production org's), otherwise the My Domain name. Renaming a
        suggestion renames it on every row that shares it. Untick what you want to leave for later.</p>
      <datalist id="orgAcctOptions">${[...new Set(names.concat(rows.map(r => r.suggestion).filter(Boolean)))]
        .map(n => `<option value="${escapeHtml(n)}"></option>`).join("")}</datalist>
      <div class="organize-table-wrap"><table class="organize-table">
        <thead><tr><th><input type="checkbox" id="orgAllChk" checked aria-label="Select all"></th>
          <th>Org</th><th>Env</th><th>Customer account</th></tr></thead>
        <tbody>${rows.map((r, i) => `<tr>
            <td><input type="checkbox" class="org-chk" data-i="${i}" ${r.suggestion ? "checked" : ""} aria-label="Include ${escapeHtml(r.id)}"></td>
            <td class="org-name-cell"><b>${escapeHtml(r.o.name || r.id)}</b><div class="muted mono">${escapeHtml(r.id)}</div></td>
            <td>${envBadge(r.o) || "<span class='muted'>-</span>"}</td>
            <td><input class="org-acct" data-i="${i}" list="orgAcctOptions" autocomplete="off"
                 value="${escapeHtml(r.suggestion || "")}" placeholder="Customer name">
              ${r.matched ? `<div class="field-hint">matches an existing account</div>` : ""}</td>
          </tr>`).join("")}</tbody></table></div>
      <div class="status-line" id="orgStatus"></div>
      <div class="modal-actions">
        <button type="button" class="secondary" data-cancel>Cancel</button>
        <button type="button" class="primary" id="orgApply">Apply</button>
      </div>
    </div>`;
  document.body.appendChild(back);
  const close = () => back.remove();
  back.querySelector("[data-cancel]").onclick = close;
  back.onclick = e => { if (e.target === back) close(); };
  const chks = [...back.querySelectorAll(".org-chk")];
  const inputs = [...back.querySelectorAll(".org-acct")];
  const count = () => {
    const n = chks.filter((c, i) => c.checked && inputs[i].value.trim()).length;
    back.querySelector("#orgApply").textContent = n ? `Apply to ${n} org${n === 1 ? "" : "s"}` : "Apply";
    back.querySelector("#orgApply").disabled = !n;
  };
  back.querySelector("#orgAllChk").onchange = e => { chks.forEach(c => { c.checked = e.target.checked; }); count(); };
  chks.forEach(c => { c.onchange = count; });
  // Renaming one suggestion renames it on every row that shared it and has
  // not been edited by hand: "apttus2" -> "Conga" once, not once per sandbox.
  inputs.forEach((inp, i) => {
    inp.dataset.orig = inp.value;
    inp.oninput = () => {
      const before = inp.dataset.last ?? inp.dataset.orig;
      inp.dataset.edited = "1";
      inputs.forEach((other, j) => {
        if (j !== i && !other.dataset.edited && other.value === before && before) {
          other.value = inp.value; if (inp.value.trim()) chks[j].checked = true;
        }
      });
      inp.dataset.last = inp.value;
      if (inp.value.trim()) chks[i].checked = true;
      count();
    };
  });
  count();
  back.querySelector("#orgApply").onclick = async () => {
    const btn = back.querySelector("#orgApply");
    btn.disabled = true;
    let done = 0; const failed = [];
    for (let i = 0; i < rows.length; i++) {
      const name = inputs[i].value.replace(/\s+/g, " ").trim();
      if (!chks[i].checked || !name) continue;
      back.querySelector("#orgStatus").textContent = `Moving ${rows[i].id}...`;
      const r = await patchOrgAccount(rows[i].id, name);
      if (r.ok) done++; else failed.push(`${rows[i].id}: ${r.error}`);
    }
    close();
    toast(failed.length ? `Grouped ${done} org(s); ${failed.length} failed -- ${failed[0]}` : `Grouped ${done} org(s) into accounts.`,
          failed.length ? "error" : "ok", 6000);
    await loadOrgs();
  };
  setTimeout(() => (inputs[0] || back.querySelector("#orgApply")).focus(), 0);
}

// ---------- Connect form: account field ----------

function refreshAccountOptions() {
  const dl = document.getElementById("accountOptions");
  if (!dl) return;
  dl.innerHTML = accountNames().map(n => `<option value="${escapeHtml(n)}"></option>`).join("");
}

/** As the Instance URL is typed, pre-fill the account from its My Domain --
 *  unless the person has already typed one themselves. */
function suggestNewOrgAccount() {
  const input = document.getElementById("newAccount");
  const hint = document.getElementById("newAccountHint");
  if (!input || !hint) return;
  const url = document.getElementById("newInstanceUrl").value;
  const env = envFromUrl(url);
  const g = suggestAccountFor(url);
  const envText = url.trim() && ENV_META[env] && env !== "unknown" ? `Looks like a <b>${ENV_META[env].label.toLowerCase()}</b> org. ` : "";
  if (input.dataset.touched) {
    hint.innerHTML = envText + (input.value.trim() ? `Will be listed under <b>${escapeHtml(input.value.trim())}</b>.` : "Leave empty to decide later.");
    return;
  }
  input.value = g.suggestion || "";
  hint.innerHTML = envText + (g.matched
    ? `Suggested <b>${escapeHtml(g.matched)}</b>: another org you can see is on the same My Domain.`
    : g.dom ? `Suggested from the My Domain <span class="mono">${escapeHtml(g.dom)}</span> &mdash; change it to the customer's name if you prefer.`
    : "Groups this org with the customer's other orgs on Home. Leave empty to decide later.");
}

function newAccountEdited() {
  const input = document.getElementById("newAccount");
  if (input.value.trim()) input.dataset.touched = "1"; else delete input.dataset.touched;
  suggestNewOrgAccount();
}

/** "+ Add org" on an account heading: open Connect with the account set. */
function connectToAccount(name) {
  toggleConnect(true);
  const input = document.getElementById("newAccount");
  if (!input) return;
  input.value = name;
  input.dataset.touched = "1";
  suggestNewOrgAccount();
}

function openUnresolved(id) {
  setActiveOrg(id);
  showView("known");
  document.getElementById("knownUnresolved").checked = true;
  renderKnownIssues();
}

function openKnownIssue(orgId, signature) {
  if (ORGS[orgId] && orgId !== CURRENT_ORG) setActiveOrg(orgId);
  document.getElementById("knownFilter").value = signature;
  document.getElementById("knownUnresolved").checked = false;
  showView("known");
}

function openIncidentFrom(orgId, incidentId) {
  if (ORGS[orgId] && orgId !== CURRENT_ORG) setActiveOrg(orgId);
  showView("incidents");
  showIncidentDetail(incidentId);
}

/** Open by default only for someone who has nothing to look at yet; after
 *  that it stays however the person last left it. */
function applyConnectState() {
  if (HOME.connectOverride !== undefined) return setConnectOpen(HOME.connectOverride);
  const pref = homePrefs().connect_open;
  setConnectOpen(typeof pref === "boolean" ? pref : Object.keys(ORGS).length === 0);
}

function setConnectOpen(open) {
  const card = document.getElementById("connectCard");
  if (!card) return;
  card.classList.toggle("open", open);
  document.getElementById("connectToggle").setAttribute("aria-expanded", String(open));
  const n = Object.keys(ORGS).length;
  document.getElementById("connectHint").textContent = open ? ""
    : `${n ? `${n} org${n === 1 ? "" : "s"} connected. ` : ""}Instance URL + access token; only the derived knowledgebase is stored.`;
}

/** `persist: false` is for tours: open the card to point at it without
 *  overwriting how the person chose to leave it. */
function toggleConnect(force, { persist = true } = {}) {
  const card = document.getElementById("connectCard");
  const open = force === undefined ? !card.classList.contains("open") : !!force;
  if (persist) {
    HOME.connectOverride = undefined;
    savePrefs({ connect_open: open });
  } else {
    HOME.connectOverride = open;
  }
  applyConnectState();
  if (open) {
    showView("connections");
    card.scrollIntoView({ behavior: "smooth", block: "start" });
    if (persist) setTimeout(() => document.getElementById("newOrgId").focus(), 250);
  }
}

// =====================================================================
// 3. "What's broken?"
// =====================================================================

const LOG_HINT = /(EXECUTION_STARTED|CODE_UNIT_STARTED|USER_DEBUG|SOQL_EXECUTE_BEGIN|\|APEX_CODE|\d\d:\d\d:\d\d\.\d+ \(\d+\)\|)/;
const EXC_HINT = /(exception|\berror\b|FIELD_[A-Z_]{3,}|INVALID_[A-Z_]{3,}|de-reference|too many|UNABLE_TO_LOCK_ROW|DUPLICATE_VALUE|REQUIRED_FIELD_MISSING|LIMIT_EXCEEDED|CANNOT_[A-Z_]{3,}|ENTITY_IS_DELETED|STRING_TOO_LONG|FATAL_ERROR|\bSystem\.[A-Z]\w+)/i;
const FIELD_RE = /^(?:[A-Za-z]\w*\.)?([A-Za-z]\w*__[cr])$/;
const NAME_RE = /^[A-Za-z][\w.]*$/;

function classifyTriage(text) {
  const t = (text || "").trim();
  if (!t) return { kind: "empty" };
  if (t.includes("\n") && LOG_HINT.test(t)) return { kind: "log" };
  const field = t.match(FIELD_RE);
  if (field) return { kind: "field", value: field[1] };
  if (EXC_HINT.test(t)) return { kind: "exception" };
  if (NAME_RE.test(t)) return { kind: "search", value: t };
  return { kind: "question" };
}

const TRIAGE_KIND_LABEL = {
  log: "Pasted debug log &rarr; normalize it, then check Known Issues",
  field: "Field API name &rarr; who writes it",
  exception: "Exception message &rarr; Known Issues in every org you can see, plus the classes in the stack",
  search: "Name &rarr; search the knowledgebase",
  question: "Question &rarr; opens the assistant and asks it",
};

function showTriageKind() {
  const el = document.getElementById("triageKind");
  const orgEl = document.getElementById("triageOrg");
  if (!el) return;
  const c = classifyTriage(document.getElementById("triageInput").value);
  el.innerHTML = c.kind === "empty" ? "" : `<span class="triage-kind-chip">${TRIAGE_KIND_LABEL[c.kind]}</span>`;
  if (orgEl) {
    orgEl.innerHTML = CURRENT_ORG
      ? `Org lookups use <b>${escapeHtml(CURRENT_ORG)}</b>`
      : (Object.keys(ORGS).length ? "Pick an org in the header for org lookups" : "");
  }
}

function autoGrow(el) {
  el.style.height = "auto";
  el.style.height = `${Math.min(el.scrollHeight, 180)}px`;
}

function triageHost(html) {
  const el = document.getElementById("triageResult");
  el.innerHTML = html;
  return el;
}

function triageNeedsOrg() {
  triageHost(`<div class="triage-panel">${emptyStateHtml({
    title: "Pick an org first",
    body: "Field and component lookups run against one org's knowledgebase. Choose one in the header "
        + "(or with Ctrl+K), or connect one below.",
    actions: [{ label: "Connect an org", onclick: "toggleConnect(true)", role: "user", primary: true }],
  })}</div>`);
}

async function runTriage() {
  const input = document.getElementById("triageInput");
  const text = input.value.trim();
  const c = classifyTriage(text);
  if (c.kind === "empty") {
    input.focus();
    triageHost(`<p class="muted">Paste something first &mdash; an error from the case, a field name, or a class name.</p>`);
    return;
  }
  if (c.kind === "log") return triageLog(new File([text], "pasted.log", { type: "text/plain" }));
  if (c.kind === "field") return triageField(c.value);
  if (c.kind === "exception") return triageException(text);
  if (c.kind === "search") return triageSearch(c.value);
  return triageQuestion(text);
}

function matchCardHtml(m) {
  const title = m.kind === "field_report" ? `Field report: ${m.field}` : (m.type || "Exception");
  return `<div class="known ${m.resolution ? "resolved" : "unresolved"} match">
      <div class="known-head">
        <span class="badge ${m.resolution ? "low" : "medium"}">${m.resolution ? "FIX ON FILE" : "NO FIX YET"}</span>
        <b>${escapeHtml(title)}</b>
        <span class="badge recurrence">${m.occurrences}&times;</span>
        <span class="muted">in ${escapeHtml(m.org_id)} &middot; last seen ${fmtWhen(m.last_seen)}</span>
        <span class="match-score" title="How closely the text matches">${m.score}% match</span>
      </div>
      ${m.message_sample ? `<div class="known-msg">${escapeHtml(m.message_sample)}</div>` : ""}
      ${m.resolution ? `<div class="known-res"><b>Fix:</b> ${escapeHtml(m.resolution)}</div>` : ""}
      <div class="known-actions">
        <button class="secondary" onclick="openKnownIssue('${jsArg(m.org_id)}', '${jsArg(m.signature)}')">Open in Known Issues</button>
        ${m.latest_incident ? `<button class="secondary" onclick="openIncidentFrom('${jsArg(m.org_id)}', '${jsArg(m.latest_incident)}')">Latest incident</button>` : ""}
      </div>
    </div>`;
}

async function knownMatchesHtml(text) {
  const r = await apiJson(`/api/triage/known?q=${encodeURIComponent(text.slice(0, 2000))}`, {}, null);
  const matches = (r && r.matches) || [];
  if (!matches.length) {
    return `<div class="triage-verdict new"><b>Not seen before</b> in any org you can see.
      ${canWriteRole() ? "Filing it as an incident (with the debug log) starts its history." : ""}</div>`;
  }
  const fixed = matches.filter(m => m.resolution).length;
  return `<div class="triage-verdict ${fixed ? "seen" : "partial"}"><b>${fixed ? "Seen before &mdash; fix on file" : "Seen before, no fix recorded yet"}</b>
      &middot; ${matches.length} similar issue${matches.length === 1 ? "" : "s"}</div>
    ${matches.slice(0, 4).map(matchCardHtml).join("")}
    ${matches.length > 4 ? `<details><summary>${matches.length - 4} weaker match(es)</summary>${matches.slice(4).map(matchCardHtml).join("")}</details>` : ""}`;
}

/** Class and trigger names named in a pasted stack, checked against the
 *  active org so only real components become links. */
async function stackComponentsHtml(text) {
  if (!CURRENT_ORG) return "";
  const names = [...new Set([...text.matchAll(/\b(?:Class|Trigger)\.([A-Za-z_]\w*)/g)].map(m => m[1]))].slice(0, 5);
  if (!names.length) return "";
  const found = [];
  for (const n of names) {
    const r = await apiJson(`/api/orgs/${encodeURIComponent(CURRENT_ORG)}/search?q=${encodeURIComponent(n)}`, {}, null);
    if (r && (r.components || []).includes(n)) found.push(n);
  }
  if (!found.length) return "";
  return `<h3>In the stack, and in ${escapeHtml(CURRENT_ORG)}</h3>
    <div>${found.map(n => `<span class="pill link" onclick="openComponent('${jsArg(n)}')">${escapeHtml(n)}</span>`).join(" ")}</div>`;
}

function askButton(question, label = "Ask the assistant") {
  return `<button type="button" class="secondary" onclick="askAbout(${escapeHtml(JSON.stringify(question))})">${escapeHtml(label)}</button>`;
}

async function triageException(text) {
  const seq = ++HOME.seq;
  triageHost(`<div class="triage-panel"><p class="muted loading">Checking Known Issues</p></div>`);
  const [matches, stack] = await Promise.all([knownMatchesHtml(text), stackComponentsHtml(text)]);
  if (seq !== HOME.seq) return;
  const q = `A customer hit this${CURRENT_ORG ? ` in ${CURRENT_ORG}` : ""}:\n\n${text}\n\n`
          + `Has it been seen before, what is the most likely cause, and what should I check first?`;
  triageHost(`<div class="triage-panel">
      ${matches}
      ${stack}
      <div class="triage-actions">
        ${askButton(q)}
        ${canWriteRole() ? `<button type="button" class="secondary" onclick="document.getElementById('triageFile').click()">Add the debug log</button>` : ""}
      </div>
    </div>`);
}

async function triageField(field) {
  if (!CURRENT_ORG) return triageNeedsOrg();
  const seq = ++HOME.seq;
  triageHost(`<div class="triage-panel"><p class="muted loading">Looking up writers of ${escapeHtml(field)}</p></div>`);
  const data = await apiJson(`/api/orgs/${encodeURIComponent(CURRENT_ORG)}/field-writers/${encodeURIComponent(field)}`, {}, null);
  if (seq !== HOME.seq) return;
  if (data) track("writers");
  triageHost(`<div class="triage-panel">
      <div class="triage-verdict neutral"><b>Who writes ${escapeHtml(field)}</b> in ${escapeHtml(CURRENT_ORG)}
        ${data && data.writers ? `&middot; ${data.writers.length} writer${data.writers.length === 1 ? "" : "s"}` : ""}</div>
      ${renderFieldWriters(data, field)}
      <div class="triage-actions">
        <button type="button" class="secondary" onclick="showFieldWriters('${jsArg(field)}')">Open in Dashboard</button>
      </div>
    </div>`);
}

async function triageSearch(q) {
  if (!CURRENT_ORG) return triageNeedsOrg();
  const seq = ++HOME.seq;
  triageHost(`<div class="triage-panel"><p class="muted loading">Searching ${escapeHtml(CURRENT_ORG)}</p></div>`);
  const d = await apiJson(`/api/orgs/${encodeURIComponent(CURRENT_ORG)}/search?q=${encodeURIComponent(q)}`, {}, null);
  if (seq !== HOME.seq) return;
  if (d) track("search");
  const pills = (items, fn) => items.map(i => `<span class="pill link" onclick="${fn}('${jsArg(i)}')">${escapeHtml(i)}</span>`).join(" ");
  const any = d && (d.components.length || d.objects.length || d.fields.length);
  triageHost(`<div class="triage-panel">
      ${any ? `
        ${d.components.length ? `<p><b>Components:</b> ${pills(d.components, "openComponent")}</p>` : ""}
        ${d.fields.length ? `<p><b>Fields</b> <span class="muted">(click for writers)</span>: ${pills(d.fields, "triageField")}</p>` : ""}
        ${d.objects.length ? `<p><b>Objects</b> <span class="muted">(click for what touches them)</span>: ${pills(d.objects, "openObjectTouch")}</p>` : ""}`
      : `<p class="muted">Nothing matching "${escapeHtml(q)}" in ${escapeHtml(CURRENT_ORG)}'s customer-authored components.</p>`}
      <div class="triage-actions">${askButton(`Tell me about ${q} in ${CURRENT_ORG}: what it does, what calls it, and what could make it fail.`,
        `Ask about ${q}`)}</div>
    </div>`);
}

function triageQuestion(text) {
  ++HOME.seq;
  // A question goes straight to the assistant: full-screen chat, a fresh
  // conversation, already sent. The panel left behind says where it went and
  // offers the way back, for someone who returns to Home afterwards.
  triageHost(`<div class="triage-panel">
      <div class="triage-verdict neutral"><b>Sent to the assistant.</b> It answers from
        ${CURRENT_ORG ? `${escapeHtml(CURRENT_ORG)}'s` : "the"} knowledgebase tools, not general Salesforce knowledge.</div>
      <div class="triage-actions">
        <button type="button" class="secondary" onclick="showView('chat')">Back to the conversation</button>
      </div>
    </div>`);
  if (typeof askNow === "function") askNow(text);
  else askAbout(text);
}

async function triageLog(file) {
  if (!canWriteRole()) {
    triageHost(`<div class="triage-panel">${emptyStateHtml({
      title: "Readers can't upload logs",
      body: "Paste the exception message from the log instead -- it is checked against Known Issues the same way -- "
          + "or ask a writer to file the incident.",
    })}</div>`);
    return;
  }
  HOME.file = file;
  const seq = ++HOME.seq;
  const mb = (file.size / 1048576).toFixed(1);
  triageHost(`<div class="triage-panel"><p class="muted loading">Normalizing ${escapeHtml(file.name)} (${mb} MB)</p></div>`);
  const form = new FormData();
  form.append("log_file", file);
  form.append("store", "false");
  const res = await api("/api/logs/normalize", { method: "POST", body: form });
  if (seq !== HOME.seq) return;
  if (!res.ok) {
    triageHost(`<div class="triage-panel"><p class="status-line error">Could not normalize that log: ${escapeHtml(await errorText(res))}</p></div>`);
    return;
  }
  const n = (await res.json()).normalized_log || {};
  track("normalize");
  const exc = (n.exceptions || [])[0];
  const excText = exc ? `${exc.type || ""}: ${exc.message || ""}\n${(exc.stack || []).join("\n")}` : "";
  const [matches, stack] = exc ? await Promise.all([knownMatchesHtml(excText), stackComponentsHtml(excText)]) : ["", ""];
  if (seq !== HOME.seq) return;
  const summary = exc
    ? `<b>${escapeHtml(exc.type || "Exception")}</b> &mdash; ${escapeHtml(exc.message || "")}`
    : "<b>No exception</b> &mdash; the transaction completed. If a value came out wrong, paste the field name.";
  triageHost(`<div class="triage-panel">
      <div class="triage-verdict ${exc ? "bad" : "neutral"}">${summary}
        <div class="muted">${(n.execution_units || []).length} execution unit(s) &middot; ${(n.exceptions || []).length} exception(s)
          &middot; the raw log was not stored</div></div>
      ${matches}
      ${stack}
      <details class="triage-log"><summary>Normalized log</summary>${renderNormalizedLog(n)}</details>
      <div class="triage-actions">
        ${CURRENT_ORG ? `<button type="button" class="primary" onclick="triageFileIncident()">File as an incident in ${escapeHtml(CURRENT_ORG)}</button>` : ""}
        <button type="button" class="secondary" onclick="triageStoreLog()">Keep in the log library</button>
        ${askButton(exc ? `This debug log fails with ${exc.type}: ${exc.message}. What is the most likely root cause${CURRENT_ORG ? ` in ${CURRENT_ORG}` : ""}?`
                        : `This debug log completed without an exception. What should I check if a value came out wrong?`,
                    "Ask about this log")}
      </div>
    </div>`);
}

async function triageFileIncident() {
  if (!HOME.file || !CURRENT_ORG) return;
  const form = new FormData();
  form.append("log_file", HOME.file);
  const res = await api(`/api/orgs/${encodeURIComponent(CURRENT_ORG)}/incidents`, { method: "POST", body: form });
  if (!res.ok) { toast("Could not file it: " + await errorText(res), "error"); return; }
  const m = (await res.json()).meta;
  track("incident");
  toast(m.recurrence ? `Recurrence -- seen ${m.prior_occurrences} time(s) before.` : "Filed as a new issue.", "ok");
  HOME.loadedAt = 0;
  showView("incidents");
  await loadIncidents();
  showIncidentDetail(m.incident_id);
}

async function triageStoreLog() {
  if (!HOME.file) return;
  const form = new FormData();
  form.append("log_file", HOME.file);
  form.append("store", "true");
  // Kept from Home while working an org: tag it with that org (and so its
  // account) so it can be found by customer in the library later.
  if (CURRENT_ORG && ORGS[CURRENT_ORG]) form.append("org_id", CURRENT_ORG);
  const res = await api("/api/logs/normalize", { method: "POST", body: form });
  if (!res.ok) { toast("Could not store it: " + await errorText(res), "error"); return; }
  const d = await res.json();
  const m = d.meta || {};
  const where = [m.account, m.org_id].filter(Boolean).join(" · ");
  toast(`Stored as ${d.log_id}${where ? ` under ${where}` : ""} -- only the normalized JSON is kept. Retag it in the Log Normalizer.`, "ok");
}

async function openComponent(id) {
  showView("dashboard");
  document.getElementById("searchBox").value = id;
  await runSearch();
  await showComponent(id);
  document.getElementById("searchResults").scrollIntoView({ behavior: "smooth", block: "start" });
}

async function openObjectTouch(name) {
  showView("dashboard");
  document.getElementById("searchBox").value = name;
  await runSearch();
  await showObjectTouch(name);
  document.getElementById("searchResults").scrollIntoView({ behavior: "smooth", block: "start" });
}

// =====================================================================
// 4. command palette (Ctrl/Cmd+K)
// =====================================================================

const PALETTE = { items: [], shown: [], active: 0, seq: 0, timer: null, dynamic: [] };

function paletteStaticItems() {
  const role = CURRENT_USER ? CURRENT_USER.role : "reader";
  const write = role !== "reader";
  const views = [
    ["connections", "Home", "orgs what's broken triage"],
    ["dashboard", "Org Dashboard", "stats search field writers"],
    ["incidents", "Incidents", "rca report file"],
    ["known", "Known Issues", "fixes resolutions signatures"],
    ["logs", "Log Normalizer", "debug log normalize"],
    ["tokens", "API Tokens", "mcp claude desktop token"],
    ["usage", "Usage", "quota tokens llm activity analytics mcp adoption"],
  ];
  if (role === "admin") views.push(["admin", "Admin", "users quota verify signups"]);
  const items = views.map(([v, label, kw]) => ({ group: "Go to", label, keywords: kw, run: () => showView(v) }));
  const acts = [
    { label: "Diagnose something (What's broken?)", keywords: "triage exception paste", run: () => { showView("connections"); document.getElementById("triageInput").focus(); } },
    write && { label: "Connect a new org", keywords: "add org salesforce", run: () => toggleConnect(true) },
    write && { label: "Normalize a debug log", keywords: "upload log", run: () => { showView("logs"); document.getElementById("logFile").click(); } },
    write && { label: "File an incident", keywords: "new incident rca", run: () => { showView("incidents"); document.getElementById("incLabel").focus(); } },
    { label: "Find who writes a field", keywords: "field writers", run: () => { showView("dashboard"); document.getElementById("fieldWriterBox").focus(); } },
    { label: "Ask the assistant", keywords: "chat llm question", run: () => openChatFull() },
    { label: "Play the demo case", keywords: "tour tutorial walkthrough help", run: () => startDemo() },
    { label: "Quick tour of the screen", keywords: "tour help", run: () => startTour("screen") },
    { label: "Open help", keywords: "help glossary shortcuts what's new", run: () => openHelp() },
    { label: "Connect Claude Desktop (MCP setup)", keywords: "mcp token claude", run: () => openMcpSetup() },
    { label: "Change password", keywords: "account", run: () => changeOwnPassword() },
  ].filter(Boolean).map(a => ({ group: "Actions", ...a }));
  const orgs = Object.entries(ORGS).map(([id, o]) => ({
    group: "Orgs", label: `${o.name || id}`,
    hint: [accountDisplayName(o.account), id, id === CURRENT_ORG ? "active" : null].filter(Boolean).join(" · "),
    keywords: `${id} ${o.account || ""} ${orgEnv(o)} org switch`, run: () => { setActiveOrg(id); },
  }));
  const accts = accountGroups(Object.entries(ORGS)).filter(g => g.key !== UNASSIGNED_KEY).map(g => ({
    group: "Accounts", label: g.name, hint: `${g.orgs.length} org${g.orgs.length === 1 ? "" : "s"} · ${envSummary(g.orgs)}`,
    keywords: `account customer ${g.orgs.map(([id]) => id).join(" ")}`,
    run: () => { showView("connections"); HOME.accountFocus = null; focusAccount(g.key);
                 document.getElementById("orgsCard").scrollIntoView({ behavior: "smooth" }); },
  }));
  return [...acts, ...items, ...accts, ...orgs];
}

function paletteScore(item, q) {
  if (!q) return 1;
  const label = item.label.toLowerCase();
  const hay = `${label} ${(item.hint || "").toLowerCase()} ${(item.keywords || "").toLowerCase()}`;
  const i = label.indexOf(q);
  if (i === 0) return 100;
  if (i > 0) return 90 - Math.min(i, 30);
  if (hay.includes(q)) return 60;
  // subsequence, for "odash" -> "Org Dashboard"
  let k = 0;
  for (const ch of label) if (ch === q[k]) k++;
  return k === q.length ? 40 : 0;
}

function openPalette(initial = "") {
  if (document.getElementById("palette")) { document.getElementById("paletteInput").focus(); return; }
  const back = document.createElement("div");
  back.className = "palette-backdrop";
  back.id = "palette";
  back.innerHTML = `<div class="palette" role="dialog" aria-modal="true" aria-label="Command palette">
      <input id="paletteInput" autocomplete="off" spellcheck="false"
        placeholder="Jump to a tab, org, component or field -- or type a question">
      <div class="palette-list" id="paletteList" role="listbox"></div>
      <div class="palette-foot"><span><kbd>&uarr;</kbd><kbd>&darr;</kbd> move</span>
        <span><kbd>Enter</kbd> open</span><span><kbd>Esc</kbd> close</span>
        <span class="muted">${CURRENT_ORG ? `Searching ${escapeHtml(CURRENT_ORG)}` : "Pick an org to search components"}</span></div>
    </div>`;
  back.addEventListener("mousedown", e => { if (e.target === back) closePalette(); });
  document.body.appendChild(back);
  PALETTE.items = paletteStaticItems();
  PALETTE.dynamic = [];
  const input = document.getElementById("paletteInput");
  input.value = initial;
  input.addEventListener("input", () => { PALETTE.active = 0; renderPalette(); schedulePaletteSearch(); });
  input.addEventListener("keydown", e => {
    if (e.key === "ArrowDown") { e.preventDefault(); movePalette(1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); movePalette(-1); }
    else if (e.key === "Enter") { e.preventDefault(); runPaletteItem(PALETTE.active); }
    else if (e.key === "Escape") { e.preventDefault(); closePalette(); }
  });
  renderPalette();
  input.focus();
}

function closePalette() {
  const el = document.getElementById("palette");
  if (el) el.remove();
  clearTimeout(PALETTE.timer);
}

function schedulePaletteSearch() {
  clearTimeout(PALETTE.timer);
  const q = document.getElementById("paletteInput").value.trim();
  PALETTE.dynamic = [];
  if (!CURRENT_ORG || q.length < 2 || q.includes(" ")) return;
  const seq = ++PALETTE.seq;
  PALETTE.timer = setTimeout(async () => {
    const d = await apiJson(`/api/orgs/${encodeURIComponent(CURRENT_ORG)}/search?q=${encodeURIComponent(q)}`, {}, null);
    if (seq !== PALETTE.seq || !d || !document.getElementById("palette")) return;
    const g = `In ${CURRENT_ORG}`;
    PALETTE.dynamic = [
      ...d.components.slice(0, 6).map(c => ({ group: g, label: c, hint: "component", run: () => openComponent(c) })),
      ...d.fields.slice(0, 5).map(f => ({ group: g, label: f, hint: "field · who writes it", run: () => showFieldWriters(f) })),
      ...d.objects.slice(0, 4).map(o => ({ group: g, label: o, hint: "object · what touches it", run: () => openObjectTouch(o) })),
    ];
    renderPalette();
  }, 180);
}

function renderPalette() {
  const list = document.getElementById("paletteList");
  if (!list) return;
  const q = document.getElementById("paletteInput").value.trim();
  const ql = q.toLowerCase();
  const scored = PALETTE.items.map(i => ({ i, s: paletteScore(i, ql) })).filter(x => x.s > 0);
  scored.sort((a, b) => b.s - a.s);
  let shown = scored.slice(0, q ? 12 : 30).map(x => x.i);
  if (!q) {
    // With nothing typed, keep the natural order so it reads like a menu.
    const order = ["Actions", "Go to", "Orgs"];
    shown.sort((a, b) => order.indexOf(a.group) - order.indexOf(b.group));
  }
  shown = [...shown, ...PALETTE.dynamic];
  shown.push(q
    ? { group: "Ask", label: `Ask the assistant: \u201c${q}\u201d`, run: () => askAbout(q) }
    : { group: "Ask", label: "Ask the assistant", run: () => openChatFull() });
  PALETTE.shown = shown;
  if (PALETTE.active >= shown.length) PALETTE.active = shown.length - 1;
  let lastGroup = null;
  list.innerHTML = shown.map((it, idx) => {
    const head = it.group !== lastGroup ? `<div class="palette-group">${escapeHtml(it.group)}</div>` : "";
    lastGroup = it.group;
    return `${head}<div class="palette-item${idx === PALETTE.active ? " active" : ""}" role="option"
        data-idx="${idx}" aria-selected="${idx === PALETTE.active}">
        <span>${escapeHtml(it.label)}</span>${it.hint ? `<span class="palette-hint">${escapeHtml(it.hint)}</span>` : ""}</div>`;
  }).join("");
  list.querySelectorAll(".palette-item").forEach(el => {
    el.addEventListener("mousemove", () => {
      const idx = Number(el.dataset.idx);
      if (idx !== PALETTE.active) { PALETTE.active = idx; highlightPalette(); }
    });
    el.addEventListener("click", () => runPaletteItem(Number(el.dataset.idx)));
  });
}

function highlightPalette() {
  document.querySelectorAll("#paletteList .palette-item").forEach(el => {
    const on = Number(el.dataset.idx) === PALETTE.active;
    el.classList.toggle("active", on);
    el.setAttribute("aria-selected", String(on));
    if (on) el.scrollIntoView({ block: "nearest" });
  });
}

function movePalette(d) {
  const n = PALETTE.shown.length;
  if (!n) return;
  PALETTE.active = (PALETTE.active + d + n) % n;
  highlightPalette();
}

function runPaletteItem(idx) {
  const it = PALETTE.shown[idx];
  if (!it) return;
  closePalette();
  try { it.run(); } catch (e) { console.error(e); }
}

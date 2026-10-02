/* TS Intelligent Debug Helper -- onboarding and help.
 *
 *   1. state          -- /api/me/guide: what this account has done and seen
 *   2. first run      -- welcome, the "new here?" nudge, nav dots, checklist
 *   3. spotlight tours -- one engine; a quick tour of the screen plus a short
 *                        tour for each card (the small "?" on its heading)
 *   4. demo case      -- a 90-second walkthrough of one investigation, on
 *                        made-up data, using the app's real renderers
 *   5. help drawer    -- the "?" in the header: replay, tours, checklist,
 *                        what's new, glossary, shortcuts, ask about the app
 *
 * "New here" ends when the checklist is complete or dismissed (the server
 * decides; see app/guide.py). Until then the nav shows dots on unvisited
 * tabs and Home shows the checklist. After that only the help drawer
 * remains -- always one click away.
 */

let GUIDE = null;

// Newest first. The first entry's version is what the help dot compares
// against, so adding an entry here is all it takes to announce something.
const WHATS_NEW = [
  { version: "2026.09.24", date: "24 Sep 2026", title: "A real home page, a command palette, and a guided tour",
    items: [
      "<b>What's broken?</b> on Home: paste an exception, a field or a class name, or drop a debug log, and it goes to the right tool -- and checks Known Issues in every org you can see.",
      "Org <b>cards</b> with freshness, incident counts and quick actions. Star an org to pin it; switch to the table any time.",
      "<b>Ctrl+K</b> (Cmd+K on Mac) opens a command palette: tabs, orgs, components, fields, or a question for the assistant.",
      "A 90-second <b>demo case</b>, a quick tour of the screen, and a <b>?</b> on each card for a tour of just that card.",
      "A getting-started checklist that ticks itself as you go, and your LLM allowance in the header.",
      "The assistant now knows how this app works -- ask it \"how do I...\" from Help.",
    ] },
  { version: "2026.09.22", date: "22 Sep 2026", title: "Self sign-up, LLM quotas and a redesigned chat",
    items: [
      "Create your own account from the sign-in screen (writer or reader).",
      "Per-account LLM token allowances; see yours on the Usage tab.",
      "Chat opens full screen from Ask, or docked beside the page from the side-panel button.",
    ] },
  { version: "2026.09.21", date: "21 Sep 2026", title: "Built-in assistant and Conga design tokens",
    items: [
      "Ask questions in the app itself; answers are grounded in the knowledgebase tools.",
      "One shared LLM connection, configured on the server by an admin.",
      "The UI now follows Conga's design tokens.",
    ] },
  { version: "2026.09.08", date: "8 Sep 2026", title: "Remote MCP endpoint",
    items: ["Point Claude Desktop, Claude Code or Copilot Studio at <span class='mono'>/mcp</span> with an API token -- no local script needed."] },
];
const LATEST_VERSION = WHATS_NEW[0].version;

const GLOSSARY = [
  ["Knowledgebase", "What connecting an org builds: a card per Apex class, trigger, Flow, Process Builder, Workflow field update and LWC, plus indexes of which fields and objects each one touches. Derived JSON only -- no source code is stored."],
  ["Component card", "The extracted facts about one component: type, SOQL, DML, callouts, fields written, objects referenced, what it calls, whether it is managed."],
  ["Field writer", "Anything that can set a field's value: Apex assignments, Flow updates, Process Builder actions, Workflow field updates. \"Find who writes a field\" lists all of them for one field."],
  ["Entry point", "What fires when a record of an object is saved: triggers, record-triggered Flows, Process Builder, Workflow rules."],
  ["Inbound references", "Everything that invokes a component: classes calling it, Flows using it as an action or subflow."],
  ["Normalized log", "A raw Salesforce debug log condensed to what matters: exceptions, execution units, SOQL and DML summaries, callouts, governor limits, debug lines. The raw log is never stored."],
  ["Execution unit", "One trigger, Flow or entry point that ran in the transaction, in order and nesting."],
  ["Incident", "A debug log and/or a suspect field filed against an org. Produces an RCA report and a failure signature."],
  ["Prime suspects", "The components in an incident's scope, ranked: named in the log, recently changed, writes the suspect field, runs implicitly (trigger or Flow), does DML or callouts. Managed and test code sinks."],
  ["Failure signature", "A fingerprint of an exception (type + normalized message + first custom stack frame), or of a field report. Two incidents with the same signature are the same issue."],
  ["Recurrence", "An incident whose signature has been filed before in the same org. If a fix was recorded, the report shows it at the top."],
  ["Known issue", "A signature and everything learned about it: how often, when, which incidents, and the fix if someone recorded one."],
  ["Field report", "An incident with no exception -- \"this field came out wrong\" -- identified by the field name."],
  ["Customer-authored / managed", "Customer code can be changed in the org; managed-package code cannot, so it is ranked lower and hidden from search by default."],
  ["Active org", "The org chosen in the header. Dashboard, Incidents, Known Issues and the assistant all act on it."],
  ["Private / public org", "Private orgs are visible to their owner and admins; public ones to everyone signed in. Only the owner or an admin can refresh either."],
  ["Stale org", `Not refreshed in more than ${typeof STALE_DAYS === "number" ? STALE_DAYS : 30} days. Its knowledgebase may not match what is deployed now.`],
  ["Roles", "Reader: look only. Writer (user): also connect orgs, file incidents, normalize logs, record fixes. Admin: everything, plus accounts and quotas."],
  ["API token", "A credential for MCP clients. It acts as you, with your role and org visibility, and is shown only once."],
  ["MCP", "Model Context Protocol: how Claude Desktop, Claude Code and other clients call this app's tools."],
];

const SHORTCUTS = [
  ["Ctrl/⌘ + K", "Command palette: jump anywhere, or ask the assistant"],
  ["?", "Open this help (when you are not typing)"],
  ["Enter", "Run What's broken? (Shift+Enter for a new line)"],
  ["Ctrl/⌘ + Enter", "Send in chat"],
  ["Esc", "Close a dialog, the palette, a tour, or full-screen chat"],
  ["← →", "Back / next in a tour or the demo case"],
];

// ---------- 1. state ----------

async function guidePost(body) {
  const res = await api("/api/me/guide", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  if (!res.ok) return null;
  const g = await res.json();
  GUIDE = g;
  return g;
}

async function loadGuide() {
  GUIDE = await apiJson("/api/me/guide", {}, null);
  return GUIDE;
}

/** Called by app.js/home.js on a successful action. Cheap when it is
 *  already recorded: no request at all. */
async function guideMark(event) {
  if (!GUIDE) return;
  const before = GUIDE.checklist_done;
  const item = GUIDE.checklist.find(i => i.id === event);
  if (item && item.done) return;
  if (!item && (GUIDE._marked || {})[event]) return;
  (GUIDE._marked = GUIDE._marked || {})[event] = true;
  const g = await guidePost({ event });
  if (!g) return;
  refreshGuideUi();
  if (g.checklist_done > before && item) {
    const msg = g.checklist_done === g.checklist_total
      ? "Getting started: all done. Help (the ? in the header) is there whenever you need it."
      : `Getting started: ${g.checklist_done} of ${g.checklist_total} -- "${item.label}" done.`;
    toast(msg, "ok", 5000);
  }
}

async function guideTabSeen(view) {
  if (!GUIDE || !view) return;
  document.querySelectorAll(`nav button[data-view="${view}"] .nav-dot`).forEach(d => d.remove());
  if ((GUIDE.tabs_seen || []).includes(view)) return;
  GUIDE.tabs_seen = [...(GUIDE.tabs_seen || []), view];
  const before = GUIDE.checklist_done;
  const g = await guidePost({ tab_seen: view });
  if (g && g.checklist_done > before) refreshGuideUi();
}

async function guideTourDone(id) {
  if (!GUIDE) return;
  if ((GUIDE.tours_done || []).includes(id)) return;
  await guidePost({ tour_done: id });
  refreshGuideUi();
}

/** Preferences are applied locally first so the UI never waits on them. */
function guideSavePrefs(patch) {
  if (GUIDE) {
    GUIDE.prefs = GUIDE.prefs || {};
    Object.entries(patch).forEach(([k, v]) => { if (v === null) delete GUIDE.prefs[k]; else GUIDE.prefs[k] = v; });
  }
  return api("/api/me/guide", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ prefs: patch }),
  }).catch(() => null);
}

// ---------- 2. first run ----------

async function initGuide() {
  await loadGuide();
  if (!GUIDE) return;
  // A visit is a browser session, not a page load: reloading is not coming back.
  let counted = false;
  try { counted = sessionStorage.getItem("ts_guide_visit") === "1"; } catch (e) { /* ignore */ }
  if (!counted) {
    try { sessionStorage.setItem("ts_guide_visit", "1"); } catch (e) { /* ignore */ }
    await guidePost({ visit: true });
  }
  guideTabSeen((document.querySelector(".view.active") || {}).id?.replace("view-", "") || "connections");
  decorateCards(document);
  refreshGuideUi();
  // Prefs arrive with the guide, so the org layout and pins can apply now.
  if (typeof renderHomeOrgs === "function") { renderOrgsTable(); renderHomeOrgs(); }
  if (!GUIDE.welcome_seen) setTimeout(showWelcome, 500);
}

function refreshGuideUi() {
  renderNavDots();
  renderChecklist();
  renderNudge();
  renderHelpBadge();
  if (document.getElementById("helpDrawer")) renderHelpDrawer();
}

function renderNavDots() {
  document.querySelectorAll("nav .nav-dot").forEach(d => d.remove());
  if (!GUIDE || !GUIDE.learning) return;
  const seen = new Set(GUIDE.tabs_seen || []);
  document.querySelectorAll("nav button[data-view]").forEach(b => {
    if (b.style.display === "none" || seen.has(b.dataset.view)) return;
    const dot = document.createElement("span");
    dot.className = "nav-dot";
    dot.title = "You haven't opened this yet";
    b.appendChild(dot);
  });
  const ask = document.getElementById("navChat");
  if (ask && !seen.has("chat") && !GUIDE.checklist.find(i => i.id === "ask" && i.done)) {
    const dot = document.createElement("span");
    dot.className = "nav-dot";
    ask.appendChild(dot);
  }
}

function renderHelpBadge() {
  const btn = document.getElementById("helpBtn");
  const dot = document.getElementById("helpDot");
  if (!btn || !GUIDE) return;
  dot.style.display = GUIDE.seen_version !== LATEST_VERSION ? "" : "none";
  // A progress ring around the ? while the person is still learning.
  if (GUIDE.learning && GUIDE.checklist_total) {
    btn.classList.add("ring");
    btn.style.setProperty("--p", `${Math.round(100 * GUIDE.checklist_done / GUIDE.checklist_total)}%`);
    btn.title = `Help, tours and what's new -- getting started ${GUIDE.checklist_done}/${GUIDE.checklist_total}`;
  } else {
    btn.classList.remove("ring");
    btn.title = "Help, tours and what's new";
  }
}

const CHECK_ACTIONS = {
  tour: () => startDemo(),
  connect: () => startTour("connect"),
  search: () => startTour("dashboard-search"),
  writers: () => startTour("dashboard-writers"),
  normalize: () => startTour("logs"),
  incident: () => startTour("incidents"),
  fix: () => startTour("fix"),
  known: () => startTour("known"),
  ask: () => startTour("ask"),
  token: () => startTour("tokens"),
  admin: () => startTour("admin"),
};

function checklistItemsHtml(compact = false) {
  return GUIDE.checklist.map(i => `
    <li class="check-item${i.done ? " done" : ""}">
      <span class="check-mark" aria-hidden="true">${i.done ? "&#10003;" : ""}</span>
      <span class="check-text"><b>${escapeHtml(i.label)}</b>${compact ? "" : `<span class="muted">${escapeHtml(i.hint)}</span>`}</span>
      ${i.done ? "" : `<button type="button" class="link-btn" onclick="closeHelp(); CHECK_ACTIONS['${i.id}']()">Show me</button>`}
    </li>`).join("");
}

function renderChecklist() {
  const host = document.getElementById("guideChecklistHost");
  if (!host) return;
  if (!GUIDE || !GUIDE.learning) { host.innerHTML = ""; return; }
  const pct = Math.round(100 * GUIDE.checklist_done / Math.max(1, GUIDE.checklist_total));
  const next = GUIDE.checklist.find(i => !i.done);
  host.innerHTML = `<div class="card checklist-card">
      <div class="card-head">
        <div>
          <h2>Getting started</h2>
          <div class="muted">${GUIDE.checklist_done} of ${GUIDE.checklist_total} done &middot; each item ticks itself when you do it for real</div>
        </div>
        <button type="button" class="link-btn" onclick="dismissChecklist()">Hide checklist</button>
      </div>
      <div class="checklist-progress"><div style="width:${pct}%"></div></div>
      <ul class="checklist">${checklistItemsHtml()}</ul>
      ${next ? `<div class="checklist-next">Next up: <b>${escapeHtml(next.label)}</b>
        <button type="button" class="secondary" onclick="CHECK_ACTIONS['${next.id}']()">Show me</button></div>` : ""}
    </div>`;
}

async function dismissChecklist() {
  await guidePost({ checklist_dismissed: true });
  refreshGuideUi();
  toast("Checklist hidden. It lives in Help (the ? in the header) if you want it back.", "info", 6000);
}

async function restoreChecklist() {
  await guidePost({ checklist_dismissed: false });
  refreshGuideUi();
  showView("connections");
}

function renderNudge() {
  const host = document.getElementById("guideNudge");
  if (!host) return;
  let hidden = false;
  try { hidden = sessionStorage.getItem("ts_guide_nudge") === "0"; } catch (e) { /* ignore */ }
  const show = GUIDE && GUIDE.learning && GUIDE.welcome_seen && GUIDE.visits <= 3
    && !(GUIDE.tours_done || []).includes("demo") && !hidden;
  host.innerHTML = show ? `<div class="nudge">
      <span class="nudge-icon" aria-hidden="true">&#9654;</span>
      <span><b>New here?</b> The 90-second demo case walks one investigation end to end, on made-up data.</span>
      <button type="button" class="primary" onclick="startDemo()">Play it</button>
      <button type="button" class="link-btn" onclick="hideNudge()">Not now</button>
    </div>` : "";
}

function hideNudge() {
  try { sessionStorage.setItem("ts_guide_nudge", "0"); } catch (e) { /* ignore */ }
  renderNudge();
}

function loopDiagramHtml(activeIdx = -1) {
  const steps = [
    ["Connect", "an org's metadata becomes a knowledgebase"],
    ["Normalize", "a debug log, condensed"],
    ["File", "an incident: ranked suspects"],
    ["Investigate", "field writers, the assistant"],
    ["Record", "the fix"],
    ["Recognise", "it next time, instantly"],
  ];
  return `<div class="loop">
      ${steps.map(([t, d], i) => `<div class="loop-step${i === activeIdx ? " on" : ""}">
          <span class="loop-num">${i + 1}</span><b>${t}</b><span>${d}</span></div>${
          i < steps.length - 1 ? `<span class="loop-arrow" aria-hidden="true">&rarr;</span>` : ""}`).join("")}
      <div class="loop-back" aria-hidden="true"><span>&#8630; every fix makes the next investigation shorter</span></div>
    </div>`;
}

function showWelcome() {
  if (!GUIDE || document.getElementById("welcomeModal")) return;
  const back = document.createElement("div");
  back.className = "modal-backdrop";
  back.id = "welcomeModal";
  const name = GUIDE.username;
  back.innerHTML = `<div class="modal welcome" role="dialog" aria-modal="true" aria-labelledby="welcomeTitle">
      <div class="welcome-kicker">Welcome${name ? `, ${escapeHtml(name)}` : ""}</div>
      <h3 id="welcomeTitle">Find the root cause of a Salesforce issue from what's already in the org</h3>
      <p class="muted">This app turns a customer org's customization into a searchable knowledgebase, then uses it
        to explain failures -- and remembers every fix, so the same issue is faster the second time.
        Every tab is one step of this loop:</p>
      ${loopDiagramHtml()}
      <div class="welcome-choices">
        <button type="button" class="welcome-choice primary-choice" data-go="demo">
          <b>Play the demo case</b><span>90 seconds &middot; one investigation end to end, on made-up data</span></button>
        <button type="button" class="welcome-choice" data-go="screen">
          <b>Quick tour of the screen</b><span>30 seconds &middot; where everything is</span></button>
        <button type="button" class="welcome-choice" data-go="skip">
          <b>I'll explore on my own</b><span>A checklist on Home ticks itself as you go</span></button>
      </div>
      <p class="muted welcome-foot">All of this is in <b>Help</b> -- the <span class="help-inline">?</span> at the top right -- whenever you want it again.</p>
    </div>`;
  const close = async go => {
    back.remove();
    document.removeEventListener("keydown", onKey);
    await guidePost({ welcome_seen: true });
    refreshGuideUi();
    if (go === "demo") startDemo();
    else if (go === "screen") startTour("screen");
  };
  const onKey = e => { if (e.key === "Escape") close("skip"); };
  back.querySelectorAll("[data-go]").forEach(b => b.onclick = () => close(b.dataset.go));
  document.addEventListener("keydown", onKey);
  document.body.appendChild(back);
  back.querySelector("[data-go=demo]").focus();
}

// ---------- 3. spotlight tours ----------
//
// A step is { el, title, body, view?, before?, roles? }. `el` is a selector
// or a function returning an element. Missing or hidden targets are not an
// error: the step shows centred with no spotlight, because a tour that
// breaks when one card is hidden for this role is worse than no tour.

const TOURS = {
  screen: { title: "Quick tour", steps: [
    { el: "nav", title: "One tab per step of the loop",
      body: "Home, then the active org's Dashboard, its Incidents and its Known Issues. The Log Normalizer works without any org." },
    { el: "#triageCard", view: "connections", title: "Start from what you have",
      body: "Paste the error from the case, a field name or a class name -- or drop the debug log. It picks the right tool and checks Known Issues in every org you can see." },
    { el: "#orgsCard", view: "connections", title: "Your orgs",
      body: "Freshness, open incidents and quick actions on each card. Star the ones you work on to keep them at the top." },
    { el: "#orgPicker", title: "The active org",
      body: "Dashboard, Incidents, Known Issues and the assistant all act on the org chosen here." },
    { el: "#paletteTrigger", title: "Jump anywhere",
      body: "Ctrl+K (Cmd+K on Mac) opens this from any screen: tabs, orgs, components, fields -- or type a question for the assistant." },
    { el: "#navChat", title: "Ask the assistant",
      body: "It answers by calling the same knowledgebase tools, not from general Salesforce knowledge. The side-panel button next to it opens it beside the page instead." },
    { el: "#quotaChip", title: "Your LLM allowance",
      body: "How much of your assistant allowance is left. Click it for the details on the Usage tab." },
    { el: "#helpBtn", title: "Help lives here",
      body: "The demo case, this tour, a tour of each feature, the glossary, shortcuts and what's new. Cards also have their own small ? for a tour of just that card." },
  ] },
  triage: { title: "What's broken?", steps: [
    { el: "#triageInput", view: "connections", title: "Paste anything from the case",
      body: "An exception message (checked against Known Issues everywhere), a field API name (who writes it), a class name (search), or a whole debug log." },
    { el: "#triageKind", view: "connections", title: "It tells you what it will do",
      body: "As you type, this line shows how the text was read and which tool it will go to." },
    { el: ".triage-file", view: "connections", roles: ["user", "admin"], title: "Or drop the log",
      body: "Drop a .log file anywhere on this card. It is normalized in memory -- nothing is stored unless you file it or keep it." },
    { el: "#triageGo", view: "connections", title: "Enter runs it",
      body: "Results appear under the bar, with the next step as a button: open the known issue, file the incident, or ask the assistant." },
  ] },
  orgs: { title: "Org cards", steps: [
    { el: ".seg", view: "connections", title: "Cards or table",
      body: "Same orgs either way. Your choice is remembered." },
    { el: ".org-card .pin-btn", view: "connections", title: "Pin",
      body: "Starred orgs stay at the top, for you only." },
    { el: ".org-card .org-card-fresh", view: "connections", title: "Freshness",
      body: "When the knowledgebase was last rebuilt. After 30 days a card is marked stale -- refresh before trusting a negative result." },
    { el: ".org-card .org-card-incidents", view: "connections", title: "Incidents at a glance",
      body: "How many have been filed and how many still have no fix recorded. Click either to jump there." },
    { el: ".org-card .org-card-actions", view: "connections", title: "Quick actions",
      body: "Each one makes the org active first, then opens the assistant, Dashboard or Incidents." },
  ] },
  connect: { title: "Connecting an org", steps: [
    { el: "#connectToggle", view: "connections", before: () => toggleConnect(true, { persist: false }),
      title: "Folded away when you don't need it",
      body: "Open by default until you can see an org, then collapsed. It remembers how you left it." },
    { el: "#newOrgId", view: "connections", title: "Pick a short id",
      body: "How everyone refers to this org from now on. Ids are shared across the app, so if it's taken you'll be asked for another." },
    { el: "#newInstanceUrl", view: "connections", title: "Instance URL and access token",
      body: "The token is used for this one fetch and is never stored. Get one from Workbench, a Connected App, or an existing session." },
    { el: "#newVisToggle", view: "connections", title: "Private by default",
      body: "Private: only you and admins. Public: everyone signed in can investigate against it; only you or an admin can refresh it." },
  ] },
  "dashboard-search": { title: "Search", steps: [
    { el: "#searchBox", view: "dashboard", title: "Partial names work",
      body: "Components, objects and fields in the active org whose names contain what you type. Managed-package internals are left out." },
    { el: "#searchResults", view: "dashboard", title: "Click through",
      body: "A component opens its card; an object shows everything that touches it; a field shows who writes it." },
  ] },
  "dashboard-writers": { title: "Field writers", steps: [
    { el: "#fieldWriterBox", view: "dashboard", title: "\"Why did this field get this value?\"",
      body: "Enter a field's API name to list every Apex, Flow, Process Builder and Workflow writer of it -- no exception or log needed." },
    { el: "#fieldWriterResults", view: "dashboard", title: "Read the risk and the example",
      body: "Each writer shows a risk level, how recently it changed, and one sample of what it assigns. A reference rather than a literal means the real value is set upstream in that component." },
  ] },
  incidents: { title: "Filing an incident", steps: [
    { el: "#incLogFile", view: "incidents", title: "The debug log",
      body: "Optional, but it is what names the components that actually ran." },
    { el: "#incField", view: "incidents", title: "...and/or the suspect field",
      body: "For \"wrong value, no exception\" reports, or to add a field's writers to the suspects." },
    { el: "#incidentsTable", view: "incidents", title: "The report",
      body: "Opens straight after filing: a recurrence banner if it's been seen, the failure, and the prime suspects ranked by how likely each is." },
  ] },
  fix: { title: "Recording a fix", steps: [
    { el: "#incidentsTable", view: "incidents", title: "Open an incident",
      body: "The bottom of every incident report has a Record the resolution box. What you write is saved against the failure signature." },
    { el: "nav button[data-view=known]", title: "...or from Known Issues",
      body: "Every issue without a fix has a Record a fix button. The next person who hits the same signature sees it at the top of their report." },
  ] },
  known: { title: "Known Issues", steps: [
    { el: "#knownFilter", view: "known", title: "The org's memory",
      body: "Every distinct failure filed against this org. Filter by exception type, message, field, signature or fix text." },
    { el: "#knownUnresolved", view: "known", title: "What still needs writing down",
      body: "Tick this to see only issues with no fix recorded -- the ones that will cost someone time again." },
  ] },
  logs: { title: "Log Normalizer", steps: [
    { el: "#logDrop", view: "logs", title: "No org needed",
      body: "Drop a raw debug log here. It is condensed into exceptions, execution units, database activity and limits. The raw log never touches disk." },
    { el: "#logOrg", view: "logs", title: "Tag where it came from",
      body: "Pick the org (its account comes along) or just type the customer account. A log tagged to a private org is only visible to people who can see that org." },
    { el: "#logStore", view: "logs", title: "Keep it if it's worth keeping",
      body: "Only the normalized JSON is stored, owned by you. The assistant (or Claude over MCP) can pull it back up later." },
    { el: "#logSearch", view: "logs", title: "Find it again",
      body: "Search by account, org, label, owner or exception, or click an account chip. You (or an admin) can archive or delete the logs you stored." },
  ] },
  tokens: { title: "API tokens", steps: [
    { el: "#tokenLabel", view: "tokens", title: "A token per client",
      body: "Label it after the machine or tool that will use it. It carries your role and your org visibility." },
    { el: "#tokensTable", view: "tokens", title: "Revoke any time",
      body: "The token is shown once, when created. Lost it? Revoke it and make another. Help has the Claude Desktop setup steps." },
  ] },
  usage: { title: "Usage", steps: [
    { el: "#myActivityKpis", view: "usage", title: "Your activity, every channel",
      body: "Lookups, parsed logs, incidents and fixes -- whether you worked here, in the assistant, or from Claude/Copilot over MCP. Counts only; no search or log text is kept." },
    { el: "#usageSwitch", view: "usage", title: "Your AI allowance",
      body: "Switch to \"AI chat tokens\" for today's and the rolling window's token use against your limit. New self-registered accounts start lower until an admin verifies them." },
  ] },
  ask: { title: "The assistant", steps: [
    { el: "#navChat", title: "Full screen for a real investigation",
      body: "It calls the knowledgebase tools -- field writers, entry points, known issues -- and shows each call it made." },
    { el: "#navChatDock", title: "...or beside the page",
      body: "The dock keeps what you're looking at on screen. Every \"Ask about this\" button in the app opens it with the question filled in." },
  ] },
  admin: { title: "Admin", steps: [
    { el: "#admUser", view: "admin", title: "Create accounts",
      body: "People can also sign themselves up; those accounts start unverified, on the lower LLM allowance." },
    { el: "#limitsEditor", view: "admin", title: "Quota limits",
      body: "Token caps per tier. Blank means unlimited. Admins are never capped." },
    { el: "#usersTable", view: "admin", title: "Verify, change roles, reset passwords",
      body: "Verifying moves an account to the higher tier and grants nothing else." },
  ] },
  mcp: { title: "Claude Desktop", steps: [
    { el: "#mcpCardHost .setup-steps", view: "connections", title: "Same tools, in your Claude chat",
      body: "Create a token, add the URL as a connector, and ask Claude about any org you can see here." },
  ] },
  "known-feed": { title: "Latest fixes", steps: [
    { el: "#readerFixes .known", view: "connections", title: "Fixes others recorded",
      body: "The newest resolutions across every org you can see. Open one to see its full history in Known Issues." },
  ] },
};

const TOUR = { id: null, steps: [], i: 0, layer: null, target: null };

function startTour(id) {
  const def = TOURS[id];
  if (!def) return;
  closeHelp();
  endTour(false);
  const role = CURRENT_USER ? CURRENT_USER.role : "reader";
  TOUR.id = id;
  TOUR.title = def.title;
  TOUR.steps = def.steps.filter(s => !s.roles || s.roles.includes(role));
  TOUR.i = 0;
  const layer = document.createElement("div");
  layer.className = "tour-layer";
  layer.innerHTML = `<div class="tour-hole"></div>
    <div class="tour-pop" role="dialog" aria-live="polite">
      <div class="tour-kicker"></div><div class="tour-title"></div><div class="tour-body"></div>
      <div class="tour-foot">
        <button type="button" class="link-btn" data-t="skip">Skip tour</button>
        <span class="tour-dots"></span>
        <button type="button" class="secondary" data-t="back">Back</button>
        <button type="button" class="primary" data-t="next">Next</button>
      </div>
    </div>`;
  layer.querySelector("[data-t=skip]").onclick = () => endTour(false);
  layer.querySelector("[data-t=back]").onclick = () => tourGo(TOUR.i - 1);
  layer.querySelector("[data-t=next]").onclick = () => tourGo(TOUR.i + 1);
  document.body.appendChild(layer);
  TOUR.layer = layer;
  document.addEventListener("keydown", tourKey, true);
  window.addEventListener("resize", tourPlace);
  window.addEventListener("scroll", tourPlace, true);
  tourGo(0);
}

function tourKey(e) {
  if (!TOUR.layer) return;
  if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); endTour(false); }
  else if (e.key === "ArrowRight" || e.key === "Enter") { e.preventDefault(); tourGo(TOUR.i + 1); }
  else if (e.key === "ArrowLeft") { e.preventDefault(); tourGo(TOUR.i - 1); }
}

function visibleEl(el) {
  if (!el) return null;
  const r = el.getBoundingClientRect();
  return (r.width > 0 || r.height > 0) && el.offsetParent !== null ? el : (el.tagName === "NAV" ? el : null);
}

function findTarget(step) {
  try {
    const el = typeof step.el === "function" ? step.el() : document.querySelector(step.el);
    return visibleEl(el);
  } catch (e) { return null; }
}

async function tourGo(i) {
  if (i < 0) return;
  if (i >= TOUR.steps.length) { endTour(true); return; }
  TOUR.i = i;
  const step = TOUR.steps[i];
  if (step.view && !document.getElementById(`view-${step.view}`)?.classList.contains("active")) showView(step.view);
  if (step.before) { try { step.before(); } catch (e) { /* a tour must not break the app */ } }
  let target = null;
  for (let n = 0; n < 12 && !(target = findTarget(step)); n++) await new Promise(r => setTimeout(r, 100));
  if (!TOUR.layer || TOUR.i !== i) return;
  TOUR.target = target;
  if (target) target.scrollIntoView({ block: "center", behavior: "smooth" });
  const pop = TOUR.layer.querySelector(".tour-pop");
  pop.querySelector(".tour-kicker").textContent = `${TOUR.title} · ${i + 1} of ${TOUR.steps.length}`;
  pop.querySelector(".tour-title").textContent = step.title;
  pop.querySelector(".tour-body").textContent = step.body;
  pop.querySelector(".tour-dots").innerHTML = TOUR.steps.map((_, k) => `<i class="${k === i ? "on" : ""}"></i>`).join("");
  pop.querySelector("[data-t=back]").style.visibility = i === 0 ? "hidden" : "";
  pop.querySelector("[data-t=next]").textContent = i === TOUR.steps.length - 1 ? "Done" : "Next";
  tourPlace();
  setTimeout(tourPlace, 350);   // after the smooth scroll settles
  pop.querySelector("[data-t=next]").focus({ preventScroll: true });
}

function tourPlace() {
  if (!TOUR.layer) return;
  const hole = TOUR.layer.querySelector(".tour-hole");
  const pop = TOUR.layer.querySelector(".tour-pop");
  const vw = window.innerWidth, vh = window.innerHeight;
  const pw = Math.min(360, vw - 32);
  pop.style.width = `${pw}px`;
  if (!TOUR.target || !document.body.contains(TOUR.target)) {
    hole.style.display = "none";
    TOUR.layer.classList.add("dim");
    pop.style.left = `${(vw - pw) / 2}px`;
    pop.style.top = `${Math.max(16, vh / 2 - pop.offsetHeight / 2)}px`;
    return;
  }
  TOUR.layer.classList.remove("dim");
  const r = TOUR.target.getBoundingClientRect();
  const pad = 6;
  Object.assign(hole.style, {
    display: "", left: `${r.left - pad}px`, top: `${r.top - pad}px`,
    width: `${r.width + pad * 2}px`, height: `${r.height + pad * 2}px`,
  });
  const ph = pop.offsetHeight;
  let top = r.bottom + 14;
  if (top + ph > vh - 12) top = r.top - ph - 14;          // above
  if (top < 12) top = Math.min(vh - ph - 12, Math.max(12, r.top));  // beside
  let left = r.left + r.width / 2 - pw / 2;
  if (top === Math.min(vh - ph - 12, Math.max(12, r.top)) && r.right + pw + 20 < vw) left = r.right + 14;
  left = Math.max(12, Math.min(vw - pw - 12, left));
  pop.style.left = `${left}px`;
  pop.style.top = `${top}px`;
}

function endTour(completed) {
  const id = TOUR.id;
  // A tour may have opened the Connect card just to point at it; put it
  // back the way the person had it.
  if (typeof HOME !== "undefined" && HOME.connectOverride !== undefined && id) {
    HOME.connectOverride = undefined;
    applyConnectState();
  }
  if (TOUR.layer) TOUR.layer.remove();
  TOUR.layer = null; TOUR.id = null; TOUR.target = null;
  document.removeEventListener("keydown", tourKey, true);
  window.removeEventListener("resize", tourPlace);
  window.removeEventListener("scroll", tourPlace, true);
  if (completed && id) {
    guideTourDone(id);
    if (id === "screen" && GUIDE && !(GUIDE.tours_done || []).includes("demo")) {
      toast("Tour done. The demo case (in Help) shows a whole investigation in 90 seconds.", "info", 6000);
    }
  }
}

/** A small "?" on each card heading that has a tour. */
function decorateCards(root) {
  (root || document).querySelectorAll("[data-tour]").forEach(card => {
    const id = card.dataset.tour;
    if (!TOURS[id] || card.querySelector(":scope .card-help")) return;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "card-help";
    btn.title = `How "${TOURS[id].title}" works`;
    btn.setAttribute("aria-label", btn.title);
    btn.textContent = "?";
    btn.onclick = e => { e.stopPropagation(); e.preventDefault(); startTour(id); };
    const h2 = card.querySelector("h2");
    if (h2 && !h2.closest("button")) h2.appendChild(btn);
    else { btn.classList.add("floating"); card.appendChild(btn); }
  });
}

// ---------- 4. the demo case ----------
//
// Everything below is invented: the org, the components, the log, the fix.
// It is rendered with the app's own renderers (renderNormalizedLog,
// rankSuspects/suspectRow, renderFieldWriters), so what someone learns here
// is exactly what they will see on a real incident.

const DEMO = {
  org: { name: "Acme Corp (demo)", owner: "demo", visibility: "public", can_manage: false,
    last_extracted_at: new Date(Date.now() - 3 * 86400000).toISOString(),
    component_counts: { apex_classes: 412, apex_triggers: 38, flows: 57, lwc_components: 24 },
    last_refresh_changes: { changed: 3, added: 1, removed: 0 } },
  orgStats: { incidents: 6, known: 4, unresolved: 1, last_incident_at: null },
  log: {
    execution_units: [
      { label: "AgreementTrigger on Agreement__c trigger event AfterUpdate", depth: 0, had_exception: true },
      { label: "Flow: Agreement_Notify_Owner", depth: 1, had_exception: false },
    ],
    exceptions: [{ type: "System.NullPointerException",
      message: "Attempt to de-reference a null object",
      stack: ["Class.AgreementShareHelper.createShares: line 62, column 1",
              "Class.AgreementTriggerHandler.afterUpdate: line 31, column 1",
              "Trigger.AgreementTrigger: line 4, column 1"] }],
    soql_summary: [{ object: "Account", occurrences: 1 }, { object: "User", occurrences: 1 }, { object: "Group", occurrences: 1 }],
    dml_summary: [{ operation: "Update", object: "Agreement__c", occurrences: 1, total_rows: 1 }],
    callouts: [], validation_failures: [],
    user_debug: ["createShares: account 001Dm00000QxDemo region=null"],
    limits_final: { "SOQL queries": { used: 7, limit: 100 }, "DML statements": { used: 2, limit: 150 },
                    "CPU time (ms)": { used: 214, limit: 10000 } },
    involved_components: ["AgreementTrigger", "AgreementTriggerHandler", "AgreementShareHelper"],
  },
  pack: {
    normalized_log: { involved_components: ["AgreementTrigger", "AgreementTriggerHandler", "AgreementShareHelper"] },
    suspect_field: "Region__c",
    suspect_field_writers: [{ component: "Account_Set_Region" }, { component: "RegionBackfillBatch" }],
    recently_changed_components: [{ id: "Account_Set_Region", age_days: 2 }],
    primary_components: {
      AgreementShareHelper: { type: "ApexClass", loc: 214, soql: [{}, {}], dml: [{}],
        objects_referenced: ["Agreement__c", "Account", "User", "Group"], file: "AgreementShareHelper.cls" },
      AgreementTrigger: { type: "ApexTrigger", loc: 12, objects_referenced: ["Agreement__c"], file: "AgreementTrigger.trigger" },
      AgreementTriggerHandler: { type: "ApexClass", loc: 96, dml: [{}], objects_referenced: ["Agreement__c"], file: "AgreementTriggerHandler.cls" },
      Account_Set_Region: { type: "Flow", objects_referenced: ["Account"], file: "Account_Set_Region.flow-meta.xml" },
      RegionBackfillBatch: { type: "ApexClass", loc: 58, soql: [{}], dml: [{}], objects_referenced: ["Account"], file: "RegionBackfillBatch.cls" },
      AgreementShareHelper_Test: { type: "ApexClass", is_test_class: true, loc: 140, file: "AgreementShareHelper_Test.cls" },
    },
  },
  writers: { writers: [
    { mechanism: "Flow", risk: "high", component: "Account_Set_Region", object: "Account", age_days: 2,
      reason: "Record-triggered on Account; the decision now has outcomes for Americas only, and no default outcome.",
      example: "'AMER'" },
    { mechanism: "Apex", risk: "medium", component: "RegionBackfillBatch", object: "Account", age_days: 210,
      reason: "Nightly batch; only touches accounts created before 2025.", example: "regionMap.get(a.BillingCountry)" },
    { mechanism: "Workflow/Approval field update", risk: "low", component: "Account.Default_Region", object: "Account",
      reason: "Inactive rule.", example: "'GLOBAL'" },
  ], used_in_entry_criteria_of: ["Agreement_Notify_Owner"] },
  fix: "Last Tuesday's deploy removed the default outcome from Account_Set_Region, so non-Americas accounts kept a null Region__c and AgreementShareHelper.createShares (line 62) dereferenced it. Restored the default outcome, added a null guard in the helper, and re-ran RegionBackfillBatch for accounts created this week.",
};

const DEMO_STEPS = [
  { title: "The loop", kicker: "How it fits together",
    body: `<p>Every tab in this app is one step of the same loop. You connect an org once; after that, each
      incident you file teaches the org's <b>Known Issues</b>, so the same failure costs minutes the second time.</p>
      <p>This demo follows one made-up case through the whole loop. Nothing here touches a real org.</p>`,
    screen: () => loopDiagramHtml() },
  { title: "A case comes in", kicker: "Start from the evidence", loop: 0,
    body: `<p>Acme's EMEA team can't save an Agreement. The customer attached a debug log.</p>
      <p>You don't need to know the org's code. You need the log, and a knowledgebase of what's in the org.</p>`,
    screen: () => `<div class="demo-ticket">
        <div class="demo-ticket-head"><b>Case 00482119</b><span class="badge high">High</span><span class="muted">Acme Corp &middot; opened 20 min ago</span></div>
        <div class="demo-ticket-title">Agreements fail to save for EMEA users</div>
        <p>"Since yesterday, saving any Agreement for an EMEA account shows:
          <i>Attempt to de-reference a null object</i>. US users are fine."</p>
        <div class="demo-attach">&#128206; agreement_save_emea.log &middot; 1.8 MB</div></div>` },
  { title: "The org is already indexed", kicker: "Step 1 &middot; Connect", loop: 0,
    body: `<p>Someone connected Acme's org earlier. That fetched its metadata -- Apex, triggers, Flows,
      Process Builder, Workflow, LWC -- and kept only the derived knowledgebase. No source code, no token.</p>
      <p>On Home, each org is a card: counts, how fresh it is, and its incidents. It says <b>3 changed</b> on the
      last refresh -- worth remembering.</p>`,
    screen: () => `<div class="inert org-cards">${orgCardHtml("acme_demo", DEMO.org, DEMO.orgStats)}</div>` },
  { title: "Normalize the log", kicker: "Step 2 &middot; Normalize", loop: 1,
    body: `<p>Dropping the log on <b>What's broken?</b> condenses 1.8 MB into this: the exception, what ran, the
      database activity and the limits. The raw log is never stored.</p>
      <p>The stack says line 62 of <span class="mono">AgreementShareHelper</span>. The debug line says
      <span class="mono">region=null</span>.</p>`,
    screen: () => renderNormalizedLog(DEMO.log) },
  { title: "File the incident", kicker: "Step 3 &middot; File", loop: 2,
    body: `<p>Line 62 reads the account's <span class="mono">Region__c</span>, so you file the log <b>and</b>
      Region__c as the suspect field.</p>
      <p>The report ranks every component in scope. The top suspect isn't even in the log: a Flow that
      <b>writes Region__c</b> and <b>changed two days ago</b>. Test and managed code sinks to the bottom.</p>`,
    screen: () => `<div class="banner new"><div class="banner-title">New issue</div>
        <div class="banner-body">First time this signature has been filed for acme_demo.</div></div>
        ${rankSuspects(DEMO.pack).slice(0, 4).map((s, i) => suspectRow(s, i + 1)).join("")}` },
  { title: "Follow the null", kicker: "Step 4 &middot; Investigate", loop: 3,
    body: `<p><b>Find who writes a field</b> lists every writer of Region__c: Apex, Flow, Process Builder,
      Workflow. Each shows its risk, how recently it changed, and a sample of what it assigns.</p>
      <p>The Flow's decision only has an Americas outcome now, and no default. Every other account keeps a
      null region.</p>`,
    screen: () => renderFieldWriters(DEMO.writers, "Region__c") },
  { title: "Ask the assistant", kicker: "Step 4 &middot; Investigate", loop: 3,
    body: `<p>Or ask. The assistant calls the same tools -- you can see each call -- and answers from them,
      not from general Salesforce knowledge.</p>
      <p>This answer is a sample, written for the demo.</p>`,
    screen: () => `<div class="demo-chat">
        <div class="demo-bubble user">Agreements fail with a NullPointerException in AgreementShareHelper line 62 for EMEA accounts only. Why?</div>
        <div class="demo-tools"><span>&#10003; find_field_writers(Region__c)</span><span>&#10003; get_component(Account_Set_Region)</span><span>&#10003; list_known_issues</span></div>
        <div class="demo-bubble bot"><p><b>Most likely cause: Account_Set_Region</b> (record-triggered Flow on Account, changed 2 days ago).</p>
          <p>Its decision has an outcome for Americas countries only and no default, so EMEA accounts are saved with
          Region__c = null. AgreementShareHelper.createShares reads it at line 62 without a null check.</p>
          <p><b>Check first:</b> the Flow's version history for the removed default outcome. No matching known issue is on file.</p>
          <p>Want me to list every other component that reads Region__c, in case they fail the same way?</p></div>
      </div>` },
  { title: "Record the fix", kicker: "Step 5 &middot; Record", loop: 4,
    body: `<p>Once it's fixed, write down what was wrong and what fixed it, at the bottom of the incident report.</p>
      <p>It's saved against the failure's <b>signature</b> -- a fingerprint of the exception type, message and
      first custom stack frame -- not against this one incident.</p>`,
    screen: () => `<div class="section"><h3>Record the resolution</h3>
        <textarea id="demoFix" rows="4" readonly></textarea>
        <div class="demo-saved" id="demoSaved" style="visibility:hidden;">&#10003; Resolution saved to Known Issues</div></div>`,
    after: () => typeInto("demoFix", DEMO.fix, () => {
      const s = document.getElementById("demoSaved"); if (s) s.style.visibility = "visible"; }) },
  { title: "Next time: two minutes", kicker: "Step 6 &middot; Recognise", loop: 5,
    body: `<p>A month later a colleague pastes the same error into <b>What's broken?</b> -- in any org they can see.
      It's recognised immediately, with your fix.</p>
      <p>If they file the incident, the report opens with a <b>Seen before</b> banner. That's the payoff for
      every fix anyone records.</p>`,
    screen: () => `<div class="triage-verdict seen"><b>Seen before &mdash; fix on file</b> &middot; 1 similar issue</div>
        ${matchCardHtml({ org_id: "acme_demo", signature: "3f9c1a7d20b44e61", score: 96, kind: "exception",
          type: "System.NullPointerException", message_sample: "Attempt to de-reference a null object",
          resolution: DEMO.fix, occurrences: 2, last_seen: new Date().toISOString().slice(0, 19) + "Z", latest_incident: null })}
        <div class="banner recurrence"><div class="banner-title">Seen before -- 1 prior occurrence(s)</div>
          <div class="banner-body"><b>Resolution on file:</b> ${escapeHtml(DEMO.fix)}</div></div>` },
  { title: "Your turn", kicker: "That's the whole loop", loop: -1,
    body: `<p>Connect, normalize, file, investigate, record, recognise. The checklist on Home ticks itself as
      you do each for real.</p>
      <p>Everything here is in Help -- the <span class="help-inline">?</span> at the top right -- including a tour of
      each card.</p>`,
    screen: () => {
      const write = canWriteRole();
      return `<div class="demo-cta">
        ${write && !Object.keys(ORGS).length ? `<button type="button" class="welcome-choice primary-choice" onclick="endDemo(); toggleConnect(true)">
          <b>Connect your first org</b><span>Instance URL + access token</span></button>` : ""}
        <button type="button" class="welcome-choice${write && Object.keys(ORGS).length ? " primary-choice" : ""}" onclick="endDemo(); tryTriageSample()">
          <b>Try What's broken?</b><span>With this demo's error pasted in</span></button>
        <button type="button" class="welcome-choice" onclick="endDemo(); startTour('screen')">
          <b>Quick tour of the screen</b><span>Where everything is</span></button>
        <button type="button" class="welcome-choice" onclick="endDemo()">
          <b>Close</b><span>Back to where you were</span></button>
      </div>`;
    } },
];

const DEMO_STATE = { i: 0, el: null, typing: null };

function startDemo(step = 0) {
  closeHelp();
  endTour(false);
  document.querySelectorAll("#welcomeModal").forEach(m => m.remove());
  endDemo(false);
  const el = document.createElement("div");
  el.className = "demo-backdrop";
  el.id = "demo";
  el.innerHTML = `<div class="demo" role="dialog" aria-modal="true" aria-labelledby="demoTitle">
      <div class="demo-top">
        <b>Demo case</b><span class="demo-flag">Made-up data &middot; nothing here touches a real org</span>
        <button type="button" class="icon-btn demo-close" aria-label="Close the demo" onclick="endDemo()">&times;</button>
      </div>
      <div class="demo-body">
        <ol class="demo-rail" id="demoRail"></ol>
        <div class="demo-main">
          <div class="demo-narr"><div class="demo-kicker" id="demoKicker"></div>
            <h3 id="demoTitle"></h3><div id="demoText"></div></div>
          <div class="demo-screen" id="demoScreen"></div>
        </div>
      </div>
      <div class="demo-foot">
        <div class="demo-progress"><div id="demoBar"></div></div>
        <span class="muted">&larr; &rarr; to move &middot; Esc to close</span>
        <button type="button" class="secondary" id="demoBack">Back</button>
        <button type="button" class="primary" id="demoNext">Next</button>
      </div>
    </div>`;
  el.addEventListener("mousedown", e => { if (e.target === el) endDemo(); });
  document.body.appendChild(el);
  DEMO_STATE.el = el;
  el.querySelector("#demoBack").onclick = () => demoGo(DEMO_STATE.i - 1);
  el.querySelector("#demoNext").onclick = () => demoGo(DEMO_STATE.i + 1);
  document.addEventListener("keydown", demoKey, true);
  demoGo(Math.max(0, Math.min(step, DEMO_STEPS.length - 1)));
}

function demoKey(e) {
  if (!DEMO_STATE.el) return;
  if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); endDemo(); }
  else if (e.key === "ArrowRight") { e.preventDefault(); demoGo(DEMO_STATE.i + 1); }
  else if (e.key === "ArrowLeft") { e.preventDefault(); demoGo(DEMO_STATE.i - 1); }
}

function demoGo(i) {
  if (!DEMO_STATE.el) return;
  if (i < 0) return;
  if (i >= DEMO_STEPS.length) { endDemo(); return; }
  DEMO_STATE.i = i;
  clearInterval(DEMO_STATE.typing);
  const s = DEMO_STEPS[i];
  const el = DEMO_STATE.el;
  el.querySelector("#demoRail").innerHTML = DEMO_STEPS.map((st, k) =>
    `<li class="${k === i ? "on" : k < i ? "done" : ""}"><button type="button" onclick="demoGo(${k})">
      <span class="demo-rail-num">${k < i ? "&#10003;" : k + 1}</span>${escapeHtml(st.title)}</button></li>`).join("");
  el.querySelector("#demoKicker").innerHTML = s.kicker;
  el.querySelector("#demoTitle").textContent = s.title;
  el.querySelector("#demoText").innerHTML = s.body + (s.loop !== undefined && s.loop >= 0
    ? `<div class="demo-loop-mini">${loopDiagramHtml(s.loop)}</div>` : "");
  const screen = el.querySelector("#demoScreen");
  screen.innerHTML = s.screen();
  screen.scrollTop = 0;
  el.querySelector("#demoBar").style.width = `${Math.round(100 * (i + 1) / DEMO_STEPS.length)}%`;
  el.querySelector("#demoBack").style.visibility = i === 0 ? "hidden" : "";
  const next = el.querySelector("#demoNext");
  next.textContent = i === DEMO_STEPS.length - 1 ? "Finish" : i === 0 ? "Start" : "Next";
  next.focus({ preventScroll: true });
  if (s.after) s.after();
  if (i === DEMO_STEPS.length - 1) guideTourDone("demo");
}

function endDemo() {
  clearInterval(DEMO_STATE.typing);
  if (DEMO_STATE.el) DEMO_STATE.el.remove();
  DEMO_STATE.el = null;
  document.removeEventListener("keydown", demoKey, true);
}

function typeInto(id, text, done) {
  const el = document.getElementById(id);
  if (!el) return;
  let n = 0;
  clearInterval(DEMO_STATE.typing);
  DEMO_STATE.typing = setInterval(() => {
    n = Math.min(text.length, n + 4);
    el.value = text.slice(0, n);
    el.scrollTop = el.scrollHeight;
    if (n >= text.length) { clearInterval(DEMO_STATE.typing); if (done) done(); }
  }, 18);
}

function tryTriageSample() {
  showView("connections");
  const input = document.getElementById("triageInput");
  input.value = "System.NullPointerException: Attempt to de-reference a null object\n"
    + "Class.AgreementShareHelper.createShares: line 62, column 1";
  input.dispatchEvent(new Event("input"));
  input.focus();
  document.getElementById("triageCard").scrollIntoView({ behavior: "smooth", block: "start" });
  toast("This is the demo's error. Press Enter to check it against Known Issues in your orgs.", "info", 6000);
}

// ---------- 5. help drawer ----------

function openHelp() {
  if (document.getElementById("helpDrawer")) { document.getElementById("helpSearch").focus(); return; }
  const back = document.createElement("div");
  back.className = "help-backdrop";
  back.id = "helpBackdrop";
  back.onclick = closeHelp;
  const el = document.createElement("aside");
  el.className = "help-drawer";
  el.id = "helpDrawer";
  el.setAttribute("role", "dialog");
  el.setAttribute("aria-label", "Help");
  document.body.appendChild(back);
  document.body.appendChild(el);
  renderHelpDrawer();
  requestAnimationFrame(() => { el.classList.add("open"); back.classList.add("open"); });
  document.getElementById("helpSearch").focus();
  document.addEventListener("keydown", helpKey, true);
  if (GUIDE && GUIDE.seen_version !== LATEST_VERSION) {
    const unseen = GUIDE.seen_version;
    HELP_UNSEEN_FROM = unseen || "";
    guidePost({ seen_version: LATEST_VERSION }).then(renderHelpBadge);
  }
}

let HELP_UNSEEN_FROM = null;

function helpKey(e) {
  if (e.key === "Escape" && document.getElementById("helpDrawer")) { e.preventDefault(); e.stopPropagation(); closeHelp(); }
}

function closeHelp() {
  const el = document.getElementById("helpDrawer");
  const back = document.getElementById("helpBackdrop");
  if (el) el.remove();
  if (back) back.remove();
  document.removeEventListener("keydown", helpKey, true);
}

function helpSection(id, title, body, open = true) {
  return `<details class="help-section" data-sec="${id}" ${open ? "open" : ""}><summary>${title}</summary>${body}</details>`;
}

function renderHelpDrawer() {
  const el = document.getElementById("helpDrawer");
  if (!el) return;
  const q = (document.getElementById("helpSearch") || {}).value || "";
  const role = CURRENT_USER ? CURRENT_USER.role : "reader";
  const g = GUIDE || { checklist: [], checklist_done: 0, checklist_total: 0 };
  const tourList = Object.entries(TOURS).filter(([id]) => id !== "screen" && id !== "mcp" && id !== "known-feed")
    .filter(([id]) => !(id === "admin" && role !== "admin"))
    .filter(([id]) => !(["connect", "incidents", "logs", "fix"].includes(id) && role === "reader"));
  const newFrom = HELP_UNSEEN_FROM;
  const isNew = v => newFrom !== null && (newFrom === "" ? v === LATEST_VERSION : v > newFrom);

  el.innerHTML = `
    <div class="help-head">
      <div><div class="help-kicker">Help</div><b>TS Intelligent Debug Helper</b></div>
      <button type="button" class="icon-btn" aria-label="Close help" onclick="closeHelp()">&times;</button>
    </div>
    <input id="helpSearch" class="help-search" placeholder="Search help, tours and the glossary" value="${escapeHtml(q)}">
    <div class="help-scroll" id="helpScroll">
      <div class="help-start">
        <button type="button" class="welcome-choice primary-choice" onclick="startDemo()">
          <b>Play the demo case</b><span>90 s &middot; one investigation end to end${(g.tours_done || []).includes("demo") ? " &middot; watched" : ""}</span></button>
        <button type="button" class="welcome-choice" onclick="startTour('screen')">
          <b>Quick tour of the screen</b><span>30 s &middot; where everything is</span></button>
      </div>

      ${helpSection("progress", `Getting started <span class="help-count">${g.checklist_done}/${g.checklist_total}</span>`, `
        <div class="checklist-progress"><div style="width:${Math.round(100 * g.checklist_done / Math.max(1, g.checklist_total))}%"></div></div>
        <ul class="checklist compact">${g.checklist ? checklistItemsHtml(true) : ""}</ul>
        <div class="help-links">
          ${g.checklist_dismissed ? `<button type="button" class="link-btn" onclick="closeHelp(); restoreChecklist()">Show the checklist on Home again</button>` : ""}
          <button type="button" class="link-btn" onclick="resetGuide()">Start the introduction over</button>
        </div>`, !!(GUIDE && GUIDE.learning))}

      ${helpSection("tours", "Tours of each feature", `<div class="help-tours">
        ${tourList.map(([id, t]) => `<button type="button" class="help-tour" data-search="${escapeHtml(t.title + " " + t.steps.map(s => s.title + " " + s.body).join(" "))}"
          onclick="startTour('${id}')"><span>${escapeHtml(t.title)}</span><span class="muted">${t.steps.length} step${t.steps.length === 1 ? "" : "s"}</span></button>`).join("")}
        </div><p class="muted">Cards with a small <span class="help-inline">?</span> on their heading start the same tour for just that card.</p>`)}

      ${helpSection("ask", "Ask about this app", `
        <p class="muted">The assistant knows how this app works. Ask how to do something.</p>
        <div class="help-ask"><input id="helpAskInput" placeholder="How do I ...?"
          onkeydown="if(event.key==='Enter') helpAsk()"><button type="button" class="secondary" onclick="helpAsk()">Ask</button></div>
        <div class="help-chips">
          ${["How do I find out why a field got the wrong value?", "What's the difference between private and public orgs?",
             "How do I use these tools from Claude Desktop?"].map(s =>
            `<button type="button" class="starter" onclick="helpAsk(${escapeHtml(JSON.stringify(s))})">${escapeHtml(s)}</button>`).join("")}
        </div>`)}

      ${helpSection("new", `What's new${newFrom !== null && WHATS_NEW.some(w => isNew(w.version)) ? ` <span class="badge new">new</span>` : ""}`,
        WHATS_NEW.map((w, k) => `<div class="whats-new${isNew(w.version) ? " unseen" : ""}" data-search="${escapeHtml(w.title + " " + w.items.join(" ").replace(/<[^>]+>/g, ""))}">
          <div class="whats-new-head"><b>${escapeHtml(w.title)}</b><span class="muted">${escapeHtml(w.date)}</span>
            ${isNew(w.version) ? `<span class="badge new">new</span>` : ""}</div>
          ${k === 0 || isNew(w.version) ? `<ul class="tight">${w.items.map(i => `<li>${i}</li>`).join("")}</ul>`
            : `<details><summary class="muted">${w.items.length} change${w.items.length === 1 ? "" : "s"}</summary><ul class="tight">${w.items.map(i => `<li>${i}</li>`).join("")}</ul></details>`}
        </div>`).join(""), !!(newFrom !== null && WHATS_NEW.some(w => isNew(w.version))))}

      ${helpSection("glossary", "Glossary", `<dl class="glossary">${GLOSSARY.map(([t, d]) =>
        `<div class="gloss" data-search="${escapeHtml(t + " " + d)}"><dt>${escapeHtml(t)}</dt><dd>${escapeHtml(d)}</dd></div>`).join("")}</dl>`, false)}

      ${helpSection("keys", "Keyboard shortcuts", `<table class="shortcuts">${SHORTCUTS.map(([k, d]) =>
        `<tr data-search="${escapeHtml(k + " " + d)}"><td><kbd>${escapeHtml(k)}</kbd></td><td>${escapeHtml(d)}</td></tr>`).join("")}</table>`, false)}

      ${helpSection("mcp", "Use it from Claude Desktop", `<p class="muted">Every tool on this site is also available to
        Claude Desktop, Claude Code and other MCP clients.</p>
        <button type="button" class="secondary" onclick="closeHelp(); openMcpSetup()">Show the setup steps</button>`, false)}
      <div id="helpNoResults" class="muted" style="display:none; padding:12px 0;">Nothing in help matches that.
        <button type="button" class="link-btn" onclick="helpAsk(document.getElementById('helpSearch').value)">Ask the assistant instead</button></div>
    </div>`;
  const search = document.getElementById("helpSearch");
  search.addEventListener("input", filterHelp);
  if (q) filterHelp();
}

function filterHelp() {
  const q = document.getElementById("helpSearch").value.trim().toLowerCase();
  const drawer = document.getElementById("helpDrawer");
  let any = false;
  drawer.querySelectorAll("[data-search]").forEach(el => {
    const hit = !q || el.dataset.search.toLowerCase().includes(q);
    el.style.display = hit ? "" : "none";
    if (hit && q) any = true;
  });
  drawer.querySelectorAll(".help-section").forEach(sec => {
    const items = sec.querySelectorAll("[data-search]");
    if (!items.length) { sec.style.display = q ? "none" : ""; return; }
    const shown = [...items].some(i => i.style.display !== "none");
    sec.style.display = shown ? "" : "none";
    if (q && shown) sec.open = true;
  });
  drawer.querySelector(".help-start").style.display = q ? "none" : "";
  document.getElementById("helpNoResults").style.display = q && !any ? "" : "none";
}

function helpAsk(text) {
  const q = (text || (document.getElementById("helpAskInput") || {}).value || "").trim();
  if (!q) return;
  closeHelp();
  if (typeof askAbout === "function") askAbout(q);
}

async function resetGuide() {
  if (!await confirmModal("Start the introduction over?",
      "Brings back the welcome, the nav dots and the checklist. What you've actually done stays ticked.",
      "Start over")) return;
  try { sessionStorage.removeItem("ts_guide_nudge"); } catch (e) { /* ignore */ }
  await guidePost({ reset: true });
  closeHelp();
  refreshGuideUi();
  showView("connections");
  showWelcome();
}

// "?" opens help from anywhere, as long as the person is not typing.
document.addEventListener("keydown", e => {
  if (e.key !== "?" || e.ctrlKey || e.metaKey || e.altKey) return;
  const t = e.target;
  if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
  if (document.querySelector(".modal-backdrop, .demo-backdrop, .palette-backdrop, .tour-layer")) return;
  if (!CURRENT_USER) return;
  e.preventDefault();
  openHelp();
});

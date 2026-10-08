/* Home page, command palette and onboarding checks (static/home.js,
 * static/guide.js), against the REAL index.html in jsdom -- same approach and
 * the same reason as test_ui_chat_modes.js: the scripts share top-level
 * `let`/`const`, so they and the checks are evaluated as one script.
 *
 *     npm install jsdom
 *     node tests/test_ui_home_guide.js
 *
 * Skips cleanly when jsdom is absent.
 */
const fs = require("fs");
const path = require("path");

let JSDOM;
try {
  ({ JSDOM } = require("jsdom"));
} catch (e) {
  try {
    ({ JSDOM } = require(path.join("/tmp", "node_modules", "jsdom")));
  } catch (e2) {
    console.log("SKIP: jsdom is not installed (npm install jsdom to run these checks).");
    process.exit(0);
  }
}

const STATIC = path.join(__dirname, "..", "static");
const read = f => fs.readFileSync(path.join(STATIC, f), "utf8");

const dom = new JSDOM(read("index.html"), { runScripts: "outside-only", url: "http://localhost/" });
const { window } = dom;
window.Element.prototype.scrollIntoView = function () {};
window.requestAnimationFrame = fn => setTimeout(fn, 0);

// ---- fixtures ---------------------------------------------------------
const guideFor = (role, over = {}) => {
  const items = {
    reader: ["tour", "search", "writers", "known", "ask", "token"],
    user: ["tour", "connect", "search", "writers", "normalize", "incident", "fix", "ask", "token"],
    admin: ["tour", "connect", "search", "writers", "normalize", "incident", "fix", "ask", "token", "admin"],
  }[role];
  return {
    username: "dana", role, visits: 1, welcome_seen: false, tours_done: [], tabs_seen: [],
    seen_version: null, checklist_dismissed: false, checklist_completed_at: null, learning: true, prefs: {},
    checklist: items.map((id, i) => ({ id, label: `Label ${id}`, hint: `Hint ${id}`, done: i === 2 })),
    checklist_done: 1, checklist_total: items.length, ...over,
  };
};

const ORG_FIXTURE = {
  acme: { name: "Acme Prod", owner: "dana", visibility: "private", can_manage: true,
    last_extracted_at: new Date(Date.now() - 2 * 86400000).toISOString(),
    component_counts: { apex_classes: 120, apex_triggers: 9, flows: 14, lwc_components: 3 } },
  zeta: { name: "Zeta Sandbox", owner: "sam", visibility: "public", can_manage: false,
    last_extracted_at: "2026-01-01T00:00:00Z",
    component_counts: { apex_classes: 40, apex_triggers: 2, flows: 5, lwc_components: 0 } },
};

const state = {
  guide: guideFor("user"),
  orgs: ORG_FIXTURE,
  home: {
    orgs: { acme: { incidents: 3, known: 2, unresolved: 1, last_incident_at: "20260920T101010Z" } },
    recent_fixes: [{ org_id: "zeta", signature: "sig1", kind: "exception", type: "System.DmlException",
      message_sample: "UNABLE_TO_LOCK_ROW", resolution: "Serialized the batch.", occurrences: 3,
      resolution_recorded_at: "2026-09-20T00:00:00Z" }],
    has_api_token: false,
  },
  posts: [],
};

const routes = {
  "/api/auth/me": () => ({ status: 401 }),
  "/api/me/guide": (url, opts) => {
    if (opts && opts.method === "POST") {
      const body = JSON.parse(opts.body);
      state.posts.push(body);
      const g = state.guide;
      if (body.welcome_seen) g.welcome_seen = true;
      if (body.tab_seen && !g.tabs_seen.includes(body.tab_seen)) g.tabs_seen.push(body.tab_seen);
      if (body.tour_done && !g.tours_done.includes(body.tour_done)) g.tours_done.push(body.tour_done);
      if (body.event) {
        const it = g.checklist.find(i => i.id === body.event);
        if (it && !it.done) { it.done = true; g.checklist_done++; }
      }
      if (body.checklist_dismissed !== undefined) { g.checklist_dismissed = body.checklist_dismissed; g.learning = !body.checklist_dismissed; }
      if (body.seen_version) g.seen_version = body.seen_version;
      if (body.prefs) Object.assign(g.prefs, body.prefs);
    }
    return { json: state.guide };
  },
  "/api/home": () => ({ json: state.home }),
  "/api/orgs/acme/status": () => ({ json: { status: "done" } }),
  "/api/orgs/zeta/status": () => ({ json: { status: "done" } }),
  "/api/orgs/acme/search": url => ({ json: String(url).includes("AgreementShareHelper")
    ? { components: ["AgreementShareHelper"], objects: [], fields: [] }
    : { components: ["AccountTriggerHandler"], objects: ["Account"], fields: ["Region__c"] } }),
  "/api/orgs/acme/field-writers": () => ({ json: { writers: [
    { mechanism: "Flow", risk: "high", component: "Account_Set_Region", object: "Account", example: "'AMER'" }] } }),
  "/api/orgs": () => ({ json: state.orgs }),
  "/api/triage/known": url => ({ json: { matches: String(url).includes("de-reference") ? [{
    org_id: "acme", signature: "abc123", score: 92, kind: "exception", type: "System.NullPointerException",
    message_sample: "Attempt to de-reference a null object", resolution: "Restored the Flow's default outcome.",
    occurrences: 2, last_seen: "2026-09-20T00:00:00Z", latest_incident: "20260920T101010Z_x" }] : [] } }),
  "/api/usage/me": () => ({ json: { totals: {}, by_day: [], quota: { unlimited: false, exceeded: false, window_days: 30,
    daily: { used: 80, limit: 100, remaining: 20, pct: 80, exceeded: false },
    window: { used: 100, limit: 1000, remaining: 900, pct: 10, exceeded: false } } } }),
  "/api/chats": () => ({ json: [] }),
  "/api/llm": () => ({ json: { configured: true } }),
};
window.fetch = async (url, opts) => {
  const u = String(url);
  const key = Object.keys(routes).sort((a, b) => b.length - a.length).find(k => u.startsWith(k));
  const r = key ? routes[key](u, opts) : { json: {} };
  return { ok: r.status ? r.status < 400 : true, status: r.status || 200,
           json: async () => r.json || {}, text: async () => JSON.stringify(r.json || {}) };
};
window.localStorage.clear();

const failures = [];
window.__check = (label, cond, extra) => {
  console.log(`  [${cond ? "PASS" : "FAIL"}] ${label}` + (!cond && extra ? `  ${extra}` : ""));
  if (!cond) failures.push(label);
};
window.__log = msg => console.log(msg);
window.__state = state;
window.__guideFor = guideFor;
window.__done = () => {
  console.log();
  if (failures.length) {
    console.log(`${failures.length} FAILURE(S):`);
    failures.forEach(f => console.log("  - " + f));
    process.exit(1);
  }
  console.log("All home / guide checks passed.");
  process.exit(0);
};

const CHECKS = `
(async () => {
const check = window.__check, log = window.__log, S = window.__state;
const $ = id => document.getElementById(id);
const tick = (ms = 30) => new Promise(r => setTimeout(r, ms));
try {

log("\\n-- a writer signs in --");
CURRENT_USER = { username: "dana", role: "user" };
enterApp();
await tick(120);
check("org cards render", document.querySelectorAll(".org-card").length === 2,
  String(document.querySelectorAll(".org-card").length));
check("cards are the default layout", $("orgCards").style.display === "" && $("orgTableWrap").style.display === "none");
check("the table is still built for the toggle", $("orgsTable").querySelectorAll("tr").length === 2);
check("the Connect card starts collapsed when orgs exist", !$("connectCard").classList.contains("open"));
check("...and says how many orgs there are", $("connectHint").textContent.includes("2 orgs connected"));
const acme = document.querySelector('.org-card[data-org="acme"]');
check("incident counts show on the card", acme.textContent.includes("3 incidents") && acme.textContent.includes("1 without a fix"));
check("a manager gets a Refresh button", acme.textContent.includes("Refresh"));
const zeta = document.querySelector('.org-card[data-org="zeta"]');
check("an old org is marked stale", zeta.classList.contains("stale") && zeta.textContent.includes("Stale"));
check("a non-manager gets no Refresh", !zeta.textContent.includes("Refresh"));
check("the active org is flagged", acme.classList.contains("active") && acme.textContent.includes("active"));
check("the triage bar is titled for a writer", $("triageTitle").textContent === "What's broken?");
check("...and keeps its card ? after the retitle", !!document.querySelector("#triageCard h2 .card-help"));
check("writers see the log file option", document.querySelector(".triage-file").style.display === "");
check("the MCP card shows when there is no token", !!$("mcpCardHost").querySelector(".mcp-card"));
check("...with this server's /mcp URL", $("mcpCardHost").textContent.includes("http://localhost/mcp"));
check("the quota chip shows the tighter window", $("quotaChip").textContent.includes("20% left")
  && $("quotaChip").classList.contains("warn"), $("quotaChip").textContent);
check("readers' fixes feed is not shown to a writer", $("readerFixes").innerHTML === "");

log("\\n-- pins and layout --");
await togglePin("zeta");
check("pinning saves a preference", S.posts.some(p => p.prefs && (p.prefs.pinned_orgs || []).includes("zeta")));
check("a pinned org moves to the top", document.querySelector(".org-card").dataset.org === "zeta");
check("...in the table too", $("orgsTable").querySelector("a.link").textContent === "zeta");
setOrgView("table");
check("switching to the table", $("orgTableWrap").style.display === "" && $("orgCards").style.display === "none");
check("...is remembered", GUIDE.prefs.org_view === "table");
setOrgView("cards");
toggleConnect();
check("the Connect card opens on click", $("connectCard").classList.contains("open"));
check("...and the choice is saved", S.posts.some(p => p.prefs && p.prefs.connect_open === true));
toggleConnect();

log("\\n-- the private/public switch --");
const accToggle = () => document.querySelector('.org-card[data-org="acme"] .vis-toggle');
check("a manager gets a switch, not a picklist", !!accToggle() && !document.querySelector(".org-card select.vis-select"));
check("...showing private as off", accToggle().getAttribute("aria-checked") === "false" && accToggle().textContent.includes("Private"));
check("a non-manager sees a badge", !document.querySelector('.org-card[data-org="zeta"] .vis-toggle')
  && !!document.querySelector('.org-card[data-org="zeta"] .badge.visibility-public'));
const clicking = toggleOrgVisibility("acme", accToggle());
await tick(20);
check("going public asks first", !!document.querySelector(".modal-backdrop") &&
  document.querySelector(".modal-backdrop").textContent.includes("Make acme public?"));
document.querySelector(".modal-backdrop [data-cancel]").click();
await clicking;
check("cancelling leaves it private", accToggle().getAttribute("aria-checked") === "false");
ORGS.acme.visibility = "public";
renderHomeOrgs();
check("a public org shows the switch on", accToggle().classList.contains("on") && accToggle().textContent.includes("Public"));
let patched = null;
const realFetch = window.fetch;
window.fetch = async (u, o) => { if (o && o.method === "PATCH") patched = JSON.parse(o.body); return realFetch(u, o); };
await toggleOrgVisibility("acme", accToggle());
window.fetch = realFetch;
check("going private needs no confirmation and is sent", patched && patched.visibility === "private", JSON.stringify(patched));
ORGS.acme.visibility = "private";
renderHomeOrgs();
check("the Connect form uses a switch too", $("newVisToggle").getAttribute("role") === "switch");
check("...defaulting to private", $("newVisibility").value === "private");
// jsdom's "outside-only" mode does not run inline onclick attributes, so
// call the handler the attribute names.
check("the switch is wired to its handler", $("newVisToggle").getAttribute("onclick") === "toggleNewOrgVisibility(this)");
toggleNewOrgVisibility($("newVisToggle"));
check("clicking it sets public for createOrg", $("newVisibility").value === "public"
  && $("newVisToggle").classList.contains("on") && $("newVisDesc").textContent.includes("Everyone"));
toggleNewOrgVisibility($("newVisToggle"));
check("...and back", $("newVisibility").value === "private");

log("\\n-- what's broken? --");
check("an exception is recognised", classifyTriage("System.NullPointerException: Attempt to de-reference a null object").kind === "exception");
check("a field is recognised", classifyTriage("Region__c").kind === "field" && classifyTriage("Account.Region__c").value === "Region__c");
check("a name is a search", classifyTriage("AccountTriggerHandler").kind === "search");
check("a raw log is recognised", classifyTriage("12:00:00.1 (1)|EXECUTION_STARTED\\n12:00:00.2 (2)|CODE_UNIT_STARTED").kind === "log");
check("a sentence is a question", classifyTriage("why do quotes not recalc for partners").kind === "question");
$("triageInput").value = "System.NullPointerException: Attempt to de-reference a null object\\nClass.AgreementShareHelper.createShares: line 62, column 1";
$("triageInput").dispatchEvent(new Event("input"));
check("the kind chip explains the route", $("triageKind").textContent.includes("Known Issues"));
await runTriage();
await tick(60);
const res = $("triageResult").innerHTML;
check("a match with a fix is shown", res.includes("Seen before") && res.includes("Restored the Flow"), res.slice(0, 200));
check("the stack's class is linked when it exists in the org", res.includes("AgreementShareHelper") && res.includes("openComponent"));
check("there's a way to ask the assistant", res.includes("askAbout("));
$("triageInput").value = "Region__c";
await runTriage();
check("a field shows its writers", $("triageResult").innerHTML.includes("Account_Set_Region"));
check("...and ticks the checklist item", S.posts.some(p => p.event === "writers"));
$("triageInput").value = "AccountTriggerHandler";
await runTriage();
check("a name searches the org", $("triageResult").innerHTML.includes("Components:"));
$("triageInput").value = "totally unseen RandomException happened";
await runTriage(); await tick(40);
check("an unseen exception says so", $("triageResult").innerHTML.includes("Not seen before"));
{
  const calls = [];
  const realFetch = window.fetch;
  window.fetch = async (u, o) => { if (o && o.method === "POST" && String(u).startsWith("/api/chats")) calls.push([String(u), o.body]);
    if (String(u) === "/api/chats" && o && o.method === "POST") return { ok: true, status: 200, json: async () => ({ chat_id: "c9" }) };
    if (String(u).startsWith("/api/chats/c9/messages")) return { ok: false, status: 503, json: async () => ({ detail: "no llm in tests" }), text: async () => "" };
    return realFetch(u, o); };
  localStorage.setItem("ts_chat_mode", "dock");
  CHAT.keyState = { ready: true };   // no LLM in tests: say one is configured
  CHAT.model = "test-model";
  CHAT.messages = [{ role: "user", content: "an older conversation" }];
  CHAT.chatId = "old1";
  $("triageInput").value = "why do quotes not recalc for partner accounts";
  await runTriage(); await tick(80);
  window.fetch = realFetch;
  check("a question goes straight to full-screen chat", $("view-chat").classList.contains("active")
    && document.body.classList.contains("chat-fullscreen"));
  check("...in a fresh conversation, not the old one", CHAT.chatId !== "old1"
    && !CHAT.messages.some(m => m.content === "an older conversation"));
  check("...and the question is sent, not just pre-filled",
    calls.some(([u, b]) => u === "/api/chats/c9/messages" && (b || "").includes("partner accounts")), JSON.stringify(calls));
  check("the dock/full preference is left alone", localStorage.getItem("ts_chat_mode") === "dock");
  check("Home keeps a way back to it", $("triageResult").innerHTML.includes("Back to the conversation"));
  CHAT.streaming = false;
  leaveChatFull();
  showView("connections");
}

log("\\n-- command palette --");
document.dispatchEvent(new KeyboardEvent("keydown", { key: "k", ctrlKey: true, bubbles: true }));
check("Ctrl+K opens the palette (not chat)", !!$("palette") && !document.body.classList.contains("chat-fullscreen"));
$("paletteInput").value = "known";
$("paletteInput").dispatchEvent(new Event("input"));
check("typing filters", [...document.querySelectorAll(".palette-item")][0].textContent.includes("Known Issues"));
check("asking is always the last option",
  [...document.querySelectorAll(".palette-item")].pop().textContent.includes("Ask the assistant"));
$("paletteInput").dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
check("Enter runs the top item", !$("palette") && $("view-known").classList.contains("active"));
openPalette("zeta");
check("orgs are in the palette", [...document.querySelectorAll(".palette-item")].some(e => e.textContent.includes("Zeta Sandbox")));
$("paletteInput").dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
check("Esc closes it", !$("palette"));

log("\\n-- first run --");
await tick(600);
check("the welcome shows for someone who hasn't seen it", !!$("welcomeModal"));
$("welcomeModal").querySelector("[data-go=skip]").click();
await tick(30);
check("skipping records welcome_seen", S.posts.some(p => p.welcome_seen));
check("the checklist renders on Home", !!$("guideChecklistHost").querySelector(".checklist-card"));
check("it lists this role's items", $("guideChecklistHost").querySelectorAll(".check-item").length === 9);
check("the nudge offers the demo after the welcome", !!$("guideNudge").querySelector(".nudge"));
const dots = () => [...document.querySelectorAll("nav button[data-view] .nav-dot")].map(d => d.parentElement.dataset.view);
check("unvisited tabs have dots", dots().includes("incidents") && dots().includes("logs"), dots().join(","));
check("the admin tab (hidden) gets no dot", !dots().includes("admin"));
showView("incidents");
await tick(30);
check("visiting a tab clears its dot", !dots().includes("incidents"));
check("...and records it", S.posts.some(p => p.tab_seen === "incidents"));
check("the help button shows a progress ring", $("helpBtn").classList.contains("ring"));
check("...and a what's-new dot", $("helpDot").style.display === "");

log("\\n-- card tours --");
check("cards with a tour get a ?", !!document.querySelector('[data-tour="incidents"] .card-help'));
check("the collapsible card's ? is not nested in its button", !document.querySelector("#connectToggle .card-help"));
document.querySelector('[data-tour="incidents"] .card-help').click();
await tick(1400);
check("clicking it starts a spotlight tour", !!document.querySelector(".tour-layer"));
check("step counter", document.querySelector(".tour-kicker").textContent.includes("1 of 3"),
  document.querySelector(".tour-kicker").textContent);
document.querySelector(".tour-layer [data-t=next]").click(); await tick(1400);
check("next advances", document.querySelector(".tour-kicker").textContent.includes("2 of 3"));
document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
check("Esc ends it", !document.querySelector(".tour-layer"));
startTour("screen"); await tick(1400);
for (let n = 0; n < 12 && document.querySelector(".tour-layer"); n++) {
  document.querySelector(".tour-layer [data-t=next]").click(); await tick(1300);
}
check("finishing the screen tour records it", S.posts.some(p => p.tour_done === "screen"));

log("\\n-- the demo case --");
startDemo();
check("the demo opens", !!$("demo"));
check("it says the data is made up", $("demo").textContent.includes("Made-up data"));
let errors = 0;
for (let i = 0; i < DEMO_STEPS.length; i++) {
  try { demoGo(i); if (!$("demoScreen").innerHTML.trim()) errors++; } catch (e) { errors++; log(String(e)); }
}
check("every step renders", errors === 0, String(errors));
demoGo(4);
check("the suspects step ranks the recently changed field writer first",
  $("demoScreen").querySelector(".suspect b").textContent === "Account_Set_Region",
  $("demoScreen").querySelector(".suspect b").textContent);
demoGo(5);
check("the field-writer step hides the real 'ask' link", $("demoScreen").querySelector(".ask-about-bar") !== null);
demoGo(DEMO_STEPS.length - 1);
await tick(30);
check("reaching the end records the demo", S.posts.some(p => p.tour_done === "demo"));
endDemo();
check("it closes", !$("demo"));
startDemo(8);
check("deep links open a given step", $("demoTitle").textContent.includes("Next time"));
document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
check("Esc closes the demo", !$("demo"));

log("\\n-- help drawer --");
openHelp();
await tick(30);
check("the drawer opens", !!$("helpDrawer"));
check("it has the start buttons", $("helpDrawer").textContent.includes("Play the demo case"));
check("...what's new", $("helpDrawer").textContent.includes(WHATS_NEW[0].title));
check("opening it marks what's new as seen", S.posts.some(p => p.seen_version === LATEST_VERSION));
$("helpSearch").value = "signature";
filterHelp();
const shownGloss = [...document.querySelectorAll(".gloss")].filter(g => g.style.display !== "none").map(g => g.querySelector("dt").textContent);
check("search filters the glossary", shownGloss.includes("Failure signature") && !shownGloss.includes("MCP"), shownGloss.join(","));
$("helpSearch").value = "zzzzqqq";
filterHelp();
check("no results offers the assistant", $("helpNoResults").style.display === "");
closeHelp();
check("it closes", !$("helpDrawer"));
document.body.dispatchEvent(new KeyboardEvent("keydown", { key: "?", bubbles: true }));
check("? opens help when not typing", !!$("helpDrawer"));
closeHelp();

log("\\n-- guide marks --");
S.posts.length = 0;
await guideMark("search");
check("an already-done item sends nothing", S.posts.length === 0, JSON.stringify(S.posts));
await guideMark("normalize");
check("a new one is recorded", S.posts.some(p => p.event === "normalize"));
await dismissChecklist();
check("dismissing hides the checklist", $("guideChecklistHost").innerHTML === "");
check("...and the nav dots", dots().length === 0);

log("\\n-- customer accounts --");
check("no account headings while nobody uses accounts", !document.querySelector(".acct-group") && !!document.querySelector("#orgCards > .org-cards"));
check("...but a nudge to start grouping", $("acctNudge").textContent.includes("Organize into accounts"));
const recent = new Date(Date.now() - 86400000).toISOString();
const savedOrgs = ORGS;
ORGS = {
  acme_uat: { name: "Acme UAT", account: "acme ", instance_url: "https://acme--uat.sandbox.my.salesforce.com", environment: "sandbox",
    my_domain: "acme", can_manage: true, owner: "dana", visibility: "private", last_extracted_at: recent, component_counts: {} },
  acme_prod: { name: "Acme Prod", account: "Acme", instance_url: "https://acme.my.salesforce.com", environment: "production",
    my_domain: "acme", can_manage: true, owner: "dana", visibility: "private", last_extracted_at: recent, component_counts: {} },
  globex: { name: "Globex", account: "Globex", instance_url: "https://globex.my.salesforce.com", environment: "production",
    my_domain: "globex", can_manage: false, owner: "sam", visibility: "public", last_extracted_at: "2026-01-01T00:00:00Z", component_counts: {} },
  initech_dev: { name: "Initech Dev", instance_url: "https://initech--dev.sandbox.my.salesforce.com", can_manage: true,
    owner: "dana", visibility: "private", last_extracted_at: recent, component_counts: {} },
};
CURRENT_ORG = "acme_prod";
renderOrgPicker(); renderHomeOrgs(); renderOrgsTable();
const groupKeys = () => [...document.querySelectorAll("#orgCards .acct-group")].map(g => g.dataset.account);
check("grouped by account, case-insensitively, unassigned last", JSON.stringify(groupKeys()) === '["acme","globex","__unassigned__"]', JSON.stringify(groupKeys()));
const acmeGroup = () => document.querySelector('#orgCards .acct-group[data-account="acme"]');
check("production stacks before its sandbox", [...acmeGroup().querySelectorAll(".org-card")].map(c => c.dataset.org).join() === "acme_prod,acme_uat");
check("the heading rolls up the environments", acmeGroup().querySelector(".acct-head").textContent.includes("2 orgs")
  && acmeGroup().querySelector(".acct-head").textContent.includes("1 Production \\u00b7 1 Sandbox"), acmeGroup().querySelector(".acct-head").textContent);
check("environment is inferred when the server omits it", !!document.querySelector('.org-card[data-org="initech_dev"] .env-badge.env-sandbox'));
check("the stale org shows on its account", document.querySelector('.acct-group[data-account="globex"] .acct-chip.stale') !== null);
check("the rail lists every account", $("acctRail").style.display === "" && $("acctRail").querySelectorAll(".rail-item").length === 4);
check("Rename only where you manage every org", acmeGroup().textContent.includes("Rename")
  && !document.querySelector('.acct-group[data-account="globex"]').textContent.includes("Rename"));
check("Unassigned offers to organize", document.querySelector('.acct-group[data-account="__unassigned__"]').textContent.includes("Organize into accounts"));
check("the nudge goes once accounts exist", $("acctNudge").innerHTML === "");
check("table gets one header row per account", $("orgsTable").querySelectorAll("tr.acct-row").length === 3
  && $("orgsTable").querySelectorAll("tr[data-org]").length === 4);
check("header switcher names the active org's account", $("orgSwitchBtn").textContent.includes("Acme"));
openOrgSwitcher();
check("header switcher groups by account", [...document.querySelectorAll("#orgSwitchList .org-switch-group-name")].map(g => g.textContent.trim()).join() === "Acme,Globex,Unassigned");
check("...and marks the active org", document.querySelector("#orgSwitchList .org-switch-item.current").dataset.org === "acme_prod");
$("orgSwitchInput").value = "globex"; $("orgSwitchInput").dispatchEvent(new window.Event("input"));
check("...filters by account name", [...document.querySelectorAll("#orgSwitchList .org-switch-item")].map(i => i.dataset.org).join() === "globex");
closeOrgSwitcher();
check("...and closes", !$("orgSwitchPop"));
showView("dashboard");
check("the org list moves to the Dashboard", $("orgsCard").parentElement.id === "orgsSlotDash" && $("orgsCard").classList.contains("in-dashboard"));
showView("connections");
check("...and back to Home", $("orgsCard").parentElement.id === "orgsSlotHome" && !$("orgsCard").classList.contains("in-dashboard"));

await toggleAccountCollapsed("acme");
check("folding saves a preference", (GUIDE.prefs.collapsed_accounts || []).includes("acme"));
check("...hides its cards", !document.querySelector('.org-card[data-org="acme_prod"]'));
check("...says the active org is inside", acmeGroup().textContent.includes("active org inside"));
check("...and folds the table too", !$("orgsTable").querySelector('tr[data-org="acme_prod"]') && !!$("orgsTable").querySelector('tr.acct-row[data-account="acme"]'));
$("orgFilter").value = "acme";
renderHomeOrgs();
check("a filter opens folded groups and matches account names", !!document.querySelector('.org-card[data-org="acme_uat"]')
  && !document.querySelector('.org-card[data-org="globex"]'));
$("orgFilter").value = "";
await toggleAccountCollapsed("acme");
focusAccount("globex");
check("the rail narrows to one account", JSON.stringify(groupKeys()) === '["globex"]');
check("...in the table as well", $("orgsTable").querySelectorAll("tr[data-org]").length === 1);
focusAccount("globex");
check("clicking it again shows every account", groupKeys().length === 3);
await toggleAccountPin("globex");
check("a pinned account moves to the top", groupKeys()[0] === "globex");
await toggleAccountPin("globex");

$("newInstanceUrl").value = "https://acme--full.sandbox.my.salesforce.com";
suggestNewOrgAccount();
check("Connect suggests a sibling's account", $("newAccount").value === "Acme" && $("newAccountHint").textContent.includes("sandbox"), $("newAccount").value);
check("...and offers existing accounts", $("accountOptions").querySelectorAll("option").length === 2);
$("newAccount").value = "Custom"; newAccountEdited();
$("newInstanceUrl").value = "https://other.my.salesforce.com"; suggestNewOrgAccount();
check("...but never overwrites what you typed", $("newAccount").value === "Custom");
connectToAccount("Globex");
check("+ Add org pre-fills the account", $("newAccount").value === "Globex" && $("connectCard").classList.contains("open"));
toggleConnect(false);
$("newAccount").value = ""; delete $("newAccount").dataset.touched; $("newInstanceUrl").value = "";

let sentAcct = null;
const realFetch2 = window.fetch;
window.fetch = async (u, o) => {
  if (o && o.method === "PATCH" && String(u).includes("/account")) {
    sentAcct = { u: String(u), body: JSON.parse(o.body) };
    return { ok: true, status: 200, json: async () => ({ org_id: "initech_dev", account: "Initech" }), text: async () => "" };
  }
  return realFetch2(u, o);
};
const moving = moveOrgToAccount("initech_dev");
await tick(20);
const mv = document.querySelector(".modal-backdrop #mf-account");
check("Move suggests the org's My Domain", mv && mv.value === "initech", mv && mv.value);
check("...and lists existing accounts", document.querySelectorAll("#mf-account-list option").length === 2);
mv.value = "Initech";
document.querySelector(".modal-backdrop form").dispatchEvent(new window.Event("submit", { cancelable: true }));
await moving;
window.fetch = realFetch2;
check("Move sends the PATCH", sentAcct && sentAcct.u.includes("/api/orgs/initech_dev/account") && sentAcct.body.account === "Initech", JSON.stringify(sentAcct));

ORGS = {
  s1: { name: "S1", instance_url: "https://apttus2--uat.sandbox.my.salesforce.com", can_manage: true },
  s2: { name: "S2", instance_url: "https://apttus2--dev.sandbox.my.salesforce.com", can_manage: true },
  s3: { name: "S3", instance_url: "https://box--uat.sandbox.my.salesforce.com", can_manage: true },
};
organizeUnassigned();
await tick(10);
const orgInputs = [...document.querySelectorAll(".organize-modal .org-acct")];
check("Organize pre-fills from My Domain", orgInputs.map(i => i.value).join() === "apttus2,apttus2,box", orgInputs.map(i => i.value).join());
orgInputs[0].value = "Conga"; orgInputs[0].oninput();
check("renaming one suggestion renames its siblings", orgInputs[1].value === "Conga" && orgInputs[2].value === "box");
orgInputs[1].value = "Other"; orgInputs[1].oninput();
check("...but not a row edited by hand", orgInputs[0].value === "Conga");
check("the apply button counts the rows", document.querySelector("#orgApply").textContent === "Apply to 3 orgs");
document.querySelector(".organize-modal [data-cancel]").click();
ORGS = { a1: { name: "A", account: "Acme", instance_url: "https://acme.my.salesforce.com" } };
const pal = paletteStaticItems().filter(i => i.group === "Accounts");
check("the palette can jump to an account", pal.length === 1 && pal[0].label === "Acme");
ORGS = savedOrgs;
CURRENT_ORG = "acme";
renderOrgPicker(); renderHomeOrgs(); renderOrgsTable();

log("\\n-- empty states --");
ORGS = {};
renderOrgsTable(); renderHomeOrgs();
check("no orgs: a teaching empty state", $("orgCards").textContent.includes("No orgs you can see yet"));
check("...with an action to connect", $("orgCards").innerHTML.includes("toggleConnect(true)"));
check("the Connect card opens automatically when there's nothing to see",
  GUIDE.prefs.connect_open === undefined ? $("connectCard").classList.contains("open") : true);
CURRENT_USER = { username: "rex", role: "reader" };
check("reader empty states drop write-only actions",
  !emptyStateHtml({ title: "t", actions: [{ label: "W", onclick: "x()", role: "user" }, { label: "R", onclick: "y()" }] }).includes(">W<"));

log("\\n-- a reader --");
ORGS = JSON.parse(JSON.stringify(${JSON.stringify(ORG_FIXTURE)}));
S.guide = window.__guideFor("reader");
GUIDE = S.guide;
applyRole();
initHome();
await tick(80);
check("the bar becomes a lookup", $("triageTitle").textContent === "Look something up");
check("...without losing the card ?", !!document.querySelector("#triageCard h2 .card-help"));
check("the Connect card is hidden", $("connectCard").style.display === "none");
check("the log option is hidden", document.querySelector(".triage-file").style.display === "none");
check("the latest fixes feed shows", $("readerFixes").textContent.includes("Serialized the batch."));
await triageLog(new File(["x"], "a.log"));
check("dropping a log explains readers can't upload", $("triageResult").textContent.includes("Readers can't upload logs"));

log("\\n-- an admin --");
CURRENT_USER = { username: "root2", role: "admin" };
S.home.admin = { llm: { configured: true, provider: "azure", default_model: "gpt-x", problem: null },
  unverified: ["newbie"], users: 4, top_users: [{ username: "dana", total_tokens: 12000, turns: 3 }], week_tokens: 15000 };
applyRole();
await loadHome(true);
check("admins get the health strip", $("adminStrip").textContent.includes("System health"));
check("...with signups waiting", $("adminStrip").textContent.includes("1 to verify"));
check("...and the LLM state", $("adminStrip").textContent.includes("azure"));

log("\\n-- a server without the guide API (older process still running) --");
GUIDE = null;
HOME.localPrefs = {};
CURRENT_USER = { username: "dana", role: "user" };
applyRole();
ORGS = JSON.parse(JSON.stringify(${JSON.stringify(ORG_FIXTURE)}));
renderHomeOrgs();
check("the Connect card starts collapsed", !$("connectCard").classList.contains("open"));
toggleConnect();
check("...and still opens on click with no guide loaded", $("connectCard").classList.contains("open"));
renderHomeOrgs();
check("...and stays open across a re-render", $("connectCard").classList.contains("open"));
await togglePin("acme");
check("pins work without the guide too", isPinned("acme"));

} catch (e) { window.__check("no exception thrown: " + e.message + "\\n" + e.stack, false); }
window.__done();
})();
`;

window.eval(read("app.js") + "\n;\n" + read("chat.js") + "\n;\n" + read("home.js") + "\n;\n" + read("guide.js") + "\n;\n" + CHECKS);

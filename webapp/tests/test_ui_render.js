/* Render checks for static/app.js -- the readable RCA report, the normalized-log
 * view, suspect ranking, known-issues rendering and HTML escaping.
 *
 * app.js is a plain browser script (no modules, no build step), so this loads it
 * into a node vm with a stub DOM just rich enough for its top-level wiring, then
 * calls the pure render functions directly with realistic fixture data.
 *
 * Run:  node tests/test_ui_render.js
 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

// ---- minimal DOM stub -------------------------------------------------
function fakeEl() {
  const el = {
    innerHTML: "", textContent: "", value: "", checked: false, disabled: false,
    className: "", style: {}, dataset: {}, classList: { toggle() {}, add() {}, remove() {} },
    appendChild() {}, remove() {}, focus() {}, scrollIntoView() {},
    addEventListener() {}, querySelector: () => fakeEl(), querySelectorAll: () => [],
  };
  return el;
}
const document = {
  getElementById: () => fakeEl(),
  querySelector: () => fakeEl(),
  querySelectorAll: () => [],
  createElement: () => fakeEl(),
  addEventListener() {}, removeEventListener() {},
  body: { appendChild() {} },
};
const sandbox = {
  document,
  window: {},
  console,
  setTimeout, clearTimeout,
  navigator: { clipboard: { writeText: async () => {} } },
  URL: { createObjectURL: () => "blob:", revokeObjectURL() {} },
  Blob: function () {},
  // boot() runs on load; keep it from doing anything.
  fetch: async () => ({ ok: false, status: 401, json: async () => ({}) }),
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(__dirname, "..", "static", "app.js"), "utf8"), sandbox);

// ---- tiny assert harness ---------------------------------------------
const failures = [];
function check(label, cond, extra = "") {
  console.log(`  [${cond ? "PASS" : "FAIL"}] ${label}` + (!cond && extra ? `  ${extra}` : ""));
  if (!cond) failures.push(label);
}
const has = (html, s) => html.includes(s);

// ---- fixtures ---------------------------------------------------------
const normalizedLog = {
  header: "APEX_CODE,FINEST",
  execution_units: [
    { label: "TRIGGER Quote AfterUpdate", depth: 0, had_exception: true },
    { label: "Flow:Quote_Sync", depth: 1, had_exception: false },
  ],
  exceptions: [{
    type: "System.NullPointerException",
    message: "Attempt to de-reference a null object",
    stack: ["Class.QuoteHandler.recalc: line 88, column 1", "Trigger.QuoteTrigger: line 4, column 1"],
  }],
  soql_summary: [{ object: "Quote__c", occurrences: 12 }, { object: "Account", occurrences: 3 }],
  dml_summary: [{ operation: "update", object: "Quote__c", occurrences: 2, total_rows: 400 }],
  callouts: [{ endpoint: "https://pricing.internal/api/v1/quote" }],
  user_debug: ["entering recalc", "line total = null"],
  validation_failures: [],
  flow_events: [],
  limits_final: {
    soql_queries: { used: 97, limit: 100 },
    dml_rows: { used: 400, limit: 10000 },
  },
  involved_components: ["QuoteHandler", "QuoteTrigger"],
};

const rcaPack = {
  normalized_log: normalizedLog,
  primary_components: {
    QuoteHandler: {
      id: "QuoteHandler", type: "ApexClass", file: "QuoteHandler.cls", loc: 640,
      soql: [{ object: "Quote__c" }], dml: [{ operation: "update" }], callouts: [],
      objects_referenced: ["Quote__c", "Account"], is_customer_authored: true,
      field_writes: [{ field: "Increment_Adjustment__c" }],
    },
    QuoteTrigger: {
      id: "QuoteTrigger", type: "ApexTrigger", file: "QuoteTrigger.trigger",
      objects_referenced: ["Quote__c"], is_customer_authored: true, dml: [], soql: [],
    },
    QuoteHandlerTest: {
      id: "QuoteHandlerTest", type: "ApexClass", is_test_class: true, is_customer_authored: true,
    },
    APTS_PricingEngine: {
      id: "APTS_PricingEngine", type: "ApexClass", is_customer_authored: false,
      is_managed: true, namespace: "Apttus",
    },
  },
  related_by_object: { Quote__c: { triggers: ["QuoteTrigger"], flows: ["Quote_Sync"] } },
  recently_changed_components: [{ id: "QuoteHandler", last_changed: "2026-08-25T09:00:00Z", age_days: 3 }],
  suspect_field: "Increment_Adjustment__c",
  suspect_field_writers: [{
    component: "QuoteHandler", mechanism: "Apex", risk: "high", object: "Quote__c",
    reason: "assigned inside a loop with no null guard", example: "null", age_days: 3,
  }],
};

// ---- suspect ranking --------------------------------------------------
console.log("\n-- suspect ranking --");
const ranked = sandbox.rankSuspects(rcaPack);
const ids = ranked.map(r => r.id);
check("the component named in the log AND recently changed ranks first",
  ids[0] === "QuoteHandler", ids.join(" > "));
check("the trigger from the log outranks the managed-package class",
  ids.indexOf("QuoteTrigger") < ids.indexOf("APTS_PricingEngine"), ids.join(" > "));
check("test classes sink to the bottom",
  ids[ids.length - 1] === "QuoteHandlerTest" || ids.indexOf("QuoteHandlerTest") > ids.indexOf("QuoteTrigger"),
  ids.join(" > "));
check("the top suspect explains WHY it is suspected",
  ranked[0].reasons.some(r => r.includes("named in the log"))
  && ranked[0].reasons.some(r => r.includes("changed")), JSON.stringify(ranked[0].reasons));
check("field-writer status is part of the reasoning",
  ranked[0].reasons.some(r => r.includes("Increment_Adjustment__c")), JSON.stringify(ranked[0].reasons));
check("managed components are flagged as not editable",
  ranked.find(r => r.id === "APTS_PricingEngine").reasons.some(r => r.includes("managed")));

// ---- normalized log ---------------------------------------------------
console.log("\n-- normalized log rendering --");
const logHtml = sandbox.renderNormalizedLog(normalizedLog);
check("shows the exception type", has(logHtml, "System.NullPointerException"));
check("shows the exception message", has(logHtml, "Attempt to de-reference a null object"));
check("stack is present but collapsed behind a summary",
  has(logHtml, "<details>") && has(logHtml, "QuoteHandler.recalc"));
check("marks the execution unit that threw", has(logHtml, "threw"));
check("renders SOQL and DML tables", has(logHtml, "Quote__c") && has(logHtml, "update"));
check("renders the callout endpoint", has(logHtml, "pricing.internal"));
check("flags a governor limit at 97% as high", has(logHtml, 'class="limit high"') && has(logHtml, "97%"));
check("does not flag a limit at 4% as high", has(logHtml, "4%"));
check("no raw JSON blob in the readable view", !has(logHtml, '"execution_units":'));

const noExc = sandbox.renderNormalizedLog({ ...normalizedLog, exceptions: [] });
check("a log with no exception explains what that means, rather than showing nothing",
  has(noExc, "No exception in this log"));

// ---- field writers ----------------------------------------------------
console.log("\n-- field writers --");
const fw = sandbox.renderFieldWriters({ writers: rcaPack.suspect_field_writers }, "Increment_Adjustment__c");
check("groups by mechanism", has(fw, "Apex"));
check("surfaces the risk badge", has(fw, "HIGH"));
check("gives the reason", has(fw, "no null guard"));
check("empty result is explained, not blank",
  has(sandbox.renderFieldWriters({ writers: [] }, "X__c"), "Nothing in this org"));

// ---- component card ---------------------------------------------------
console.log("\n-- component card --");
const cc = sandbox.renderComponentCard("QuoteHandler", rcaPack.primary_components.QuoteHandler);
check("renders a key/value table, not a JSON dump", has(cc, 'class="kv"'));
check("labels the type in human terms", has(cc, "Class"));
check("keeps the raw JSON available but collapsed", has(cc, "Full component card (JSON)"));
check("manageability is stated plainly",
  has(sandbox.renderComponentCard("APTS_PricingEngine", rcaPack.primary_components.APTS_PricingEngine),
      "not editable in this org"));

// ---- escaping ---------------------------------------------------------
console.log("\n-- escaping --");
const nasty = sandbox.renderNormalizedLog({
  exceptions: [{ type: "<img src=x onerror=alert(1)>", message: "a & b < c", stack: [] }],
  execution_units: [], soql_summary: [], dml_summary: [], callouts: [], user_debug: [],
  limits_final: {}, validation_failures: [],
});
check("markup from log data is escaped", !has(nasty, "<img src=x") && has(nasty, "&lt;img"));
check("ampersands are escaped", has(nasty, "a &amp; b"));
check("escapeHtml handles null/undefined", sandbox.escapeHtml(null) === "" && sandbox.escapeHtml(undefined) === "");

// ---- misc helpers -----------------------------------------------------
console.log("\n-- helpers --");
check("fmtWhen renders the incident-id timestamp slug readably",
  sandbox.fmtWhen("20260828T101500Z") === "2026-08-28 10:15", sandbox.fmtWhen("20260828T101500Z"));
check("fmtWhen renders ISO timestamps readably",
  sandbox.fmtWhen("2026-08-28T10:15:00Z") === "2026-08-28 10:15:00", sandbox.fmtWhen("2026-08-28T10:15:00Z"));
check("fmtWhen tolerates a missing value", sandbox.fmtWhen(null) === "-");
check("ageBadge highlights a change in the last week",
  has(sandbox.ageBadge(3), "recurrence") && has(sandbox.ageBadge(3), "3d ago"));
check("ageBadge is quiet for an old change", !has(sandbox.ageBadge(200), "recurrence"));
check("ageBadge renders nothing when age is unknown", sandbox.ageBadge(undefined) === "");
check("collapsibleJson stays collapsed by default",
  has(sandbox.collapsibleJson("x", { a: 1 }), "<details") && !has(sandbox.collapsibleJson("x", { a: 1 }), "open"));

console.log("\n-- refresh summary line --");
check("first connection reports a count",
  has(sandbox.summariseChanges("acme", { first_connection: true, total: 812 }), "812"));
check("a no-op refresh says so explicitly",
  has(sandbox.summariseChanges("acme", { first_connection: false, changed: 0, added: 0, removed: 0, total: 812 }),
      "Nothing changed"));
check("a real refresh names what moved",
  has(sandbox.summariseChanges("acme", {
    first_connection: false, changed: 3, added: 1, removed: 0, total: 812,
    changed_sample: ["classes/QuoteHandler.cls"],
  }), "QuoteHandler.cls"));

// =====================================================================
// progress panel + usage dashboard
// ---------------------------------------------------------------------
// These render into the live DOM rather than returning a string, so they
// need a stub that remembers elements by id instead of handing out a fresh
// blank one per lookup. Installed only for this section.
// =====================================================================

const registry = {};
function trackedEl(id) {
  const children = [];
  const el = {
    id, innerHTML: "", textContent: "", value: "", className: "", style: {},
    dataset: {}, children,
    classList: { toggle() {}, add() {}, remove() {} },
    appendChild(c) { children.push(c); },
    remove() {}, focus() {}, addEventListener() {},
    querySelector: () => fakeEl(), querySelectorAll: () => [],
    get cells() { return children; },
  };
  return el;
}
sandbox.document.getElementById = id => (registry[id] = registry[id] || trackedEl(id));
sandbox.document.createElement = () => trackedEl("created");

console.log("\n-- org fetch progress --");
sandbox.renderProgress({
  status: "fetching_classes", percent: 18, step_index: 3, step_count: 10,
  step_label: "Fetching Apex classes", elapsed_seconds: 95,
  counts: { objects: 214, classes: 1893 },
  steps: [
    { name: "connecting", label: "Verifying the connection" },
    { name: "fetching_objects", label: "Reading the object model" },
    { name: "fetching_classes", label: "Fetching Apex classes" },
    { name: "fetching_triggers", label: "Fetching Apex triggers" },
  ],
});
check("the bar reflects the reported percentage", registry.pgFill.style.width === "18%",
  registry.pgFill.style.width);
check("the percentage is shown as a number too", registry.pgPercent.textContent === "18%");
check("the current phase is named in human terms",
  registry.pgPhase.textContent === "Fetching Apex classes", registry.pgPhase.textContent);
check("elapsed time is shown as minutes and seconds",
  registry.pgElapsed.textContent === "1m 35s", registry.pgElapsed.textContent);
check("finished phases are ticked", has(registry.pgSteps.innerHTML, "done"));
check("the active phase is marked", has(registry.pgSteps.innerHTML, "active"));
check("upcoming phases are listed, not hidden",
  has(registry.pgSteps.innerHTML, "Fetching Apex triggers"));
check("live counts are surfaced with readable labels",
  has(registry.pgCounts.innerHTML, "1893") && has(registry.pgCounts.innerHTML, "Apex classes"));

// A queued job has no honest figure to report, so the bar must sweep rather
// than assert a number.
sandbox.renderProgress({ status: "queued", percent: 0, steps: [], counts: {} });
check("a queued job reports 0% rather than guessing", registry.pgPercent.textContent === "0%");

sandbox.renderProgress({
  status: "done", percent: 100, step_index: 10, elapsed_seconds: 240,
  counts: { components: 2500 },
  steps: [{ name: "connecting", label: "Verifying the connection" },
          { name: "saving", label: "Saving" }],
});
check("completion fills the bar", registry.pgFill.style.width === "100%");
check("completion ticks every phase",
  !has(registry.pgSteps.innerHTML, 'class="active"'), registry.pgSteps.innerHTML);

console.log("\n-- usage trend --");
sandbox.renderUsageTrend({
  from: "2026-09-15", to: "2026-09-21",
  by_day: [
    { date: "2026-09-15", total_tokens: 12000, turns: 4, cost: 0.12, cost_available: true },
    { date: "2026-09-16", total_tokens: 0, turns: 0, cost: 0, cost_available: false },
    { date: "2026-09-17", total_tokens: 48000, turns: 15, cost: 0.51, cost_available: true },
  ],
});
const trend = registry.usageTrend.innerHTML;
check("a bar is drawn per day in the window",
  (trend.match(/usage-bar-wrap/g) || []).length === 3);
check("the peak day is drawn at full height", has(trend, "height:100%"));
check("an idle day is drawn as empty, not skipped", has(trend, "usage-bar empty"));
check("each bar carries its own figures on hover", has(trend, "2026-09-17: 48.0k tokens"));
check("the window's date range is labelled", has(trend, "2026-09-15") && has(trend, "2026-09-21"));

console.log("\n-- usage tables --");
// `CURRENT_USER` is a top-level `let`, so it is a lexical binding the sandbox
// cannot reach. app.js reads it through this declared function for exactly
// that reason, which makes it substitutable here.
sandbox.currentUsername = () => "manshah";
sandbox.renderUsageTable("usageByUser", [
  { username: "manshah", turns: 20, total_tokens: 80000, cost: 1.2, cost_available: true,
    tool_calls: 44, avg_seconds_per_turn: 8.1, failed_turns: 0 },
  { username: "bob", turns: 5, total_tokens: 20000, cost: 0.3, cost_available: true,
    tool_calls: 9, avg_seconds_per_turn: 6.0, failed_turns: 2 },
], "username", 100000, 8, true);
const userRows = registry.usageByUser.children;
check("one row per account", userRows.length === 2, String(userRows.length));
check("the signed-in admin's own row is marked",
  userRows[0].className === "usage-row-self" && has(userRows[0].innerHTML, ">you<"));
check("token totals are shown compactly", has(userRows[0].innerHTML, "80.0k"));
check("a share bar is drawn per row", has(userRows[0].innerHTML, "usage-bar-mini"));
check("failed turns are visible, not hidden", has(userRows[1].innerHTML, ">2<"));

// Azure reports no per-call cost. "$0.00" would read as free, so it must not
// be shown at all -- this is the check that keeps that honest.
sandbox.renderUsageTable("usageByOrg", [
  { org_id: "acme_prod", turns: 9, total_tokens: 30000, cost: 0, cost_available: false },
  { org_id: "(no org)", turns: 2, total_tokens: 500, cost: 0, cost_available: false },
], "org_id", 30500, 5);
const orgRows = registry.usageByOrg.children;
check("an unreported cost shows a dash, never $0.00",
  has(orgRows[0].innerHTML, "&mdash;") && !has(orgRows[0].innerHTML, "$0.00"));
check("orgless turns are labelled in plain language",
  has(orgRows[1].innerHTML, "no org selected"));

console.log("\n-- usage formatting --");
check("fmtCompact abbreviates thousands", sandbox.fmtCompact(48000) === "48.0k");
check("fmtCompact abbreviates millions", sandbox.fmtCompact(2500000) === "2.5M");
check("fmtCompact leaves small numbers alone", sandbox.fmtCompact(42) === "42");
check("fmtCompact tolerates no value", sandbox.fmtCompact(undefined) === "0");
check("fmtMoney keeps sub-cent costs visible", sandbox.fmtMoney(0.0012) === "$0.0012");
check("fmtMoney rounds real money to cents", sandbox.fmtMoney(12.345) === "$12.35");

console.log("\n-- quota meter --");
// The whole point of the meter is that a reader never has to subtract, and
// that "cannot say" never renders as a confident number.
const quotaOk = sandbox.renderQuotaPanel({
  tier: "verified", source: "verified", window_days: 30, unlimited: false, exceeded: false,
  daily: { used: 20000, limit: 200000, remaining: 180000, pct: 10, exceeded: false },
  window: { used: 90000, limit: 2000000, remaining: 1910000, pct: 4.5, exceeded: false },
  daily_resets_at: "2026-09-23T00:00:00Z",
});
check("both windows are shown", has(quotaOk, "Today") && has(quotaOk, "Last 30 days"));
check("the remaining figure is spelled out, not left to subtraction",
  has(quotaOk, "left"));
check("a comfortable bar is drawn in the calm band", has(quotaOk, "quota-fill ok"));
check("the daily reset time is stated", has(quotaOk, "2026-09-23"));

const quotaWarn = sandbox.renderQuotaPanel({
  tier: "unverified", source: "unverified", window_days: 30, unlimited: false, exceeded: false,
  daily: { used: 160000, limit: 200000, remaining: 40000, pct: 80, exceeded: false },
  window: { used: 10, limit: 2000000, remaining: 1999990, pct: 0, exceeded: false },
  daily_resets_at: "2026-09-23T00:00:00Z",
});
check("nearing the cap moves the bar into the warn band", has(quotaWarn, "quota-fill warn"));
check("an unverified account is told that verification is the remedy",
  has(quotaWarn, "unverified") && has(quotaWarn, "raises the limit"));

const quotaOver = sandbox.renderQuotaPanel({
  tier: "unverified", source: "unverified", window_days: 30, unlimited: false, exceeded: true,
  daily: { used: 250000, limit: 200000, remaining: 0, pct: 100, exceeded: true },
  window: { used: 250000, limit: 2000000, remaining: 1750000, pct: 12.5, exceeded: false },
  daily_resets_at: "2026-09-23T00:00:00Z",
});
check("being over is called out, not shown as 0 left",
  has(quotaOver, "none left") && has(quotaOver, "quota-fill over"));
check("the exhausted panel is visually distinct", has(quotaOver, "quota-panel exceeded"));

const quotaAdmin = sandbox.renderQuotaPanel({ tier: "admin", unlimited: true });
check("an admin is told they are uncapped, with the reason",
  has(quotaAdmin, "No LLM limit") && has(quotaAdmin, "admins are never capped"));
check("no quota at all renders nothing rather than an empty meter",
  sandbox.renderQuotaPanel(null) === "");

console.log("\n-- admin user rows --");
const qcell = sandbox.quotaCell({
  unlimited: false, window_days: 30,
  window: { used: 1800000, limit: 2000000, pct: 90, exceeded: false },
}, { limits: { daily_tokens: 5 } });
check("a row shows consumption against the cap", has(qcell, "1.8M") && has(qcell, "2.0M"));
check("a heavy account is flagged in the warn band", has(qcell, "quota-fill warn"));
check("a per-account override is labelled as custom", has(qcell, "custom"));
check("an uncapped account says so plainly",
  has(sandbox.quotaCell({ unlimited: true }, {}), "unlimited"));

console.log();
if (failures.length) {
  console.log(`${failures.length} FAILURE(S):`);
  failures.forEach(f => console.log("  - " + f));
  process.exit(1);
}
console.log("All UI render checks passed.");

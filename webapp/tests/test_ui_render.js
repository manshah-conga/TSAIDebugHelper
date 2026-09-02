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

console.log();
if (failures.length) {
  console.log(`${failures.length} FAILURE(S):`);
  failures.forEach(f => console.log("  - " + f));
  process.exit(1);
}
console.log("All UI render checks passed.");

/* "File a new incident" card (static/app.js "incidents") against the REAL
 * index.html in jsdom: log source switch, library picker scoping + search,
 * "Filed here" badges, what is posted, busy/double-submit guard, reset after
 * filing, warnings, and "File incident" from the log library.
 *
 *     npm install jsdom
 *     node tests/test_ui_incident_form.js
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
window.URL.createObjectURL = () => "blob:x";
window.URL.revokeObjectURL = () => {};

const ORGS_FIX = {
  acmeprod: { name: "Acme Prod", owner: "dana", visibility: "public", can_manage: true, account: "Acme Corp",
    instance_url: "https://acme.my.salesforce.com", environment: "production", component_counts: {} },
  acmeuat: { name: "Acme UAT", owner: "dana", visibility: "private", can_manage: true, account: "Acme Corp",
    instance_url: "https://acme--uat.sandbox.my.salesforce.com", environment: "sandbox", component_counts: {} },
  globex: { name: "Globex", owner: "dana", visibility: "public", can_manage: true, account: "Globex",
    instance_url: "https://globex.my.salesforce.com", environment: "production", component_counts: {} },
};
const LOG = (id, o) => Object.assign({ log_id: id, label: null, source_log: "x.log", timestamp: "2026-09-28T10:00:00Z",
  owner: "dana", org_id: null, account: null, archived: false, can_manage: true, exception_count: 0,
  top_exception: null, involved_components: [], incidents: [] }, o);

const state = {
  logs: [
    LOG("20260928T100000Z_quote", { label: "Quote NPE", org_id: "acmeprod", account: "Acme Corp",
      exception_count: 2, top_exception: "System.NullPointerException" }),
    LOG("20260927T100000Z_uat", { label: "UAT renewal", org_id: "acmeuat", account: "Acme Corp",
      top_exception: "System.DmlException", exception_count: 1 }),
    LOG("20260926T100000Z_globex", { label: "Globex batch", org_id: "globex", account: "Globex", owner: "sam",
      top_exception: "System.LimitException", exception_count: 1 }),
    LOG("20260925T100000Z_loose", { label: "Loose one", owner: "sam" }),
  ],
  filed: [], incidents: [], delay: 0, warnings: [],
};

const routes = {
  "/api/auth/me": () => ({ status: 401 }),
  "/api/build": () => ({ json: { build: 30 } }),
  "/api/me/guide": () => ({ status: 404 }),
  "/api/home": () => ({ json: { orgs: {} } }),
  "/api/orgs/": (url, opts) => {
    const m = String(url).match(/^\/api\/orgs\/([^/]+)\/incidents(?:\/([^/?]+))?/);
    if (m && opts && opts.method === "POST") {
      const f = {}; opts.body.forEach((v, k) => { f[k] = v; });
      state.filed.push({ org: m[1], form: f });
      const meta = { incident_id: `20261002T100000Z_${state.filed.length}`, org_id: m[1], timestamp: "2026-10-02T10:00:00Z",
        label: f.label || null, recurrence: false, signature: "abc", signature_source: "exception",
        source_log_id: f.log_id || (f.save_log ? "20261002T100000Z_kept" : null),
        source_log: f.log_file ? f.log_file.name : f.log_id ? "x.log" : null, suspect_field: f.field || null,
        filed_by: "dana", top_exception: "System.NullPointerException", prior_incident_ids: [] };
      state.incidents.unshift(meta);
      if (f.log_id) {
        const l = state.logs.find(x => x.log_id === f.log_id);
        if (l) l.incidents.push({ org_id: m[1], incident_id: meta.incident_id });
      }
      const res = { json: { meta, field_writers: null, warnings: state.warnings } };
      return state.delay ? new Promise(r => setTimeout(() => r(res), state.delay)) : res;
    }
    if (m && m[2]) return { json: { meta: state.incidents.find(i => i.incident_id === m[2]) || {}, rca_context_pack: {}, normalized_log: {} } };
    if (m) return { json: state.incidents.filter(i => i.org_id === m[1]) };
    return { status: 404 };   // stats, known issues, ... -- not what this test is about
  },
  "/api/orgs": () => ({ json: ORGS_FIX }),
  "/api/logs": (url) => {
    const m = String(url).match(/^\/api\/logs\/([^/?]+)/);
    if (m) {
      const log = state.logs.find(l => l.log_id === m[1]);
      return log ? { json: { meta: log, normalized_log: { exceptions: [], execution_units: [] } } } : { status: 404 };
    }
    return { json: String(url).includes("status=all") ? state.logs : state.logs.filter(l => !l.archived) };
  },
  "/api/chats": () => ({ json: [] }),
  "/api/llm": () => ({ json: { configured: true } }),
  "/api/usage/me": () => ({ json: {} }),
};
window.fetch = async (url, opts) => {
  const u = String(url);
  const key = Object.keys(routes).sort((a, b) => b.length - a.length).find(k => u.startsWith(k));
  const r = await (key ? routes[key](u, opts) : { json: {} });
  return { ok: r.status ? r.status < 400 : true, status: r.status || 200,
           json: async () => r.json || {}, text: async () => JSON.stringify(r.json || {}) };
};

const failures = [];
window.__check = (label, cond, extra) => {
  console.log(`  [${cond ? "PASS" : "FAIL"}] ${label}` + (!cond && extra ? `  ${extra}` : ""));
  if (!cond) failures.push(label);
};
window.__log = msg => console.log(msg);
window.__state = state;
window.__done = () => {
  console.log();
  if (failures.length) {
    console.log(`${failures.length} FAILURE(S):`);
    failures.forEach(f => console.log("  - " + f));
    process.exit(1);
  }
  console.log("All incident form UI checks passed.");
  process.exit(0);
};

const CHECKS = `
(async () => {
const check = window.__check, log = window.__log, S = window.__state;
const $ = id => document.getElementById(id);
const tick = (ms = 30) => new Promise(r => setTimeout(r, ms));
const items = () => [...$("incLibList").querySelectorAll(".inc-lib-item")].map(r => r.dataset.logId);
try {

CURRENT_USER = { username: "dana", role: "user" };
ORGS = ${JSON.stringify(ORGS_FIX)};
CURRENT_ORG = "acmeprod";
showView("incidents");
await tick(60);

log("\\n-- defaults --");
check("upload is the default source", $("incSrcUpload").classList.contains("on") && $("incPaneUpload").style.display === "");
check("card names the org it files into", $("incOrgChip").textContent.includes("acmeprod"));
check("button disabled with nothing to file", $("incFileBtn").disabled);
check("library count shown on the tab", $("incLibCount").textContent === "4", $("incLibCount").textContent);

log("\\n-- suspect field only --");
$("incField").value = "Discount__c"; incFormChanged();
check("field enables filing", !$("incFileBtn").disabled);
check("summary says what will be checked", $("incSummary").textContent.includes("Discount__c") && $("incSummary").textContent.includes("acmeprod"));
setIncSource("none");
check("no-log pane shown", $("incPaneNone").style.display === "" && $("incPaneUpload").style.display === "none");
await fileIncident(); await tick();
check("posted the field only", S.filed[0].form.field === "Discount__c" && !S.filed[0].form.log_file && !S.filed[0].form.log_id, JSON.stringify(S.filed[0].form));
check("form reset after filing", $("incField").value === "" && $("incFileBtn").disabled);
check("report opened", $("incidentDetailCard").style.display === "block");

log("\\n-- library picker --");
setIncSource("library");
await tick();
check("library pane shown", $("incPaneLibrary").style.display === "");
check("defaults to this org's logs", JSON.stringify(items()) === JSON.stringify(["20260928T100000Z_quote"]), items());
setIncLibScope("account");
check("account scope adds the sibling org's logs", items().includes("20260927T100000Z_uat") && !items().includes("20260926T100000Z_globex"), items());
setIncLibScope("all");
check("all scope shows everything visible", items().length === 4, items());
$("incLibSearch").value = "limitexception"; renderIncLibrary();
check("search filters by exception", JSON.stringify(items()) === JSON.stringify(["20260926T100000Z_globex"]), items());
$("incLibSearch").value = ""; renderIncLibrary();
check("still disabled until a log is picked", $("incFileBtn").disabled);
pickIncLog("20260927T100000Z_uat");
check("picked row highlighted", $("incLibList").querySelector(".inc-lib-item.selected").dataset.logId === "20260927T100000Z_uat");
check("other-org log flagged in the summary", $("incSummary").textContent.includes("tagged to acmeuat"), $("incSummary").textContent);
check("label placeholder follows the log", $("incLabel").placeholder === "UAT renewal");
pickIncLog("20260927T100000Z_uat");
check("clicking again un-picks", INC.logId === null && $("incFileBtn").disabled);
pickIncLog("20260928T100000Z_quote");
$("incLabel").value = "Quote NPE again";
await fileIncident(); await tick(60);
const f2 = S.filed[1].form;
check("posted log_id, no file", f2.log_id === "20260928T100000Z_quote" && !f2.log_file, JSON.stringify(f2));
check("label sent", f2.label === "Quote NPE again");
check("library source stays selected after filing", $("incSrcLibrary").classList.contains("on"));
check("picker reloaded with a Filed here badge",
  $("incLibList").querySelector('[data-log-id="20260928T100000Z_quote"]').textContent.includes("Filed here"));
pickIncLog("20260928T100000Z_quote");
check("re-picking a filed log warns", $("incSummary").textContent.includes("Already filed here"), $("incSummary").textContent);
pickIncLog("20260928T100000Z_quote");

log("\\n-- upload + keep --");
setIncSource("upload");
setIncFile(new File(["x"], "renewal.log"));
check("file enables filing", !$("incFileBtn").disabled && $("incFileName").textContent === "renewal.log");
$("incSaveLog").checked = true;
S.warnings = ["This log is tagged to org 'x'."];
await fileIncident(); await tick(30);
const f3 = S.filed[2].form;
check("posted file + save_log", f3.log_file && f3.log_file.name === "renewal.log" && f3.save_log === "true" && !f3.log_id, JSON.stringify(Object.keys(f3)));
check("warnings shown under the verdict", $("incidentStatus").textContent.includes("tagged to org 'x'"));
check("file cleared after filing", INC.file === null && !$("incSaveLog").checked);
S.warnings = [];

log("\\n-- double submit --");
S.delay = 60;
$("incField").value = "X__c"; incFormChanged();
const p1 = fileIncident(); const p2 = fileIncident();
check("button disabled while filing", $("incFileBtn").disabled && $("incFileBtn").textContent.includes("Filing"));
await Promise.all([p1, p2]); await tick();
check("only one filing posted", S.filed.length === 4, S.filed.length);
S.delay = 0;

log("\\n-- incidents table --");
check("table shows the label, not just the id", $("incidentsTable").textContent.includes("Quote NPE again"));
check("table marks library-sourced incidents", $("incidentsTable").textContent.includes("library"));

log("\\n-- from the log library --");
showView("logs"); await tick(60);
const row = $("logsTable").querySelector('tr[data-log-id="20260926T100000Z_globex"]');
check("library rows offer File incident", row && row.textContent.includes("File incident"));
await fileIncidentFromLog("20260926T100000Z_globex"); await tick(60);
check("switched to the log's org", CURRENT_ORG === "globex");
check("incidents view open", $("view-incidents").classList.contains("active"));
check("library source with the log picked", INC.source === "library" && INC.logId === "20260926T100000Z_globex");
check("picked log visible", items().includes("20260926T100000Z_globex"), items());
check("ready to file", !$("incFileBtn").disabled);
await fileIncidentFromLog("20260925T100000Z_loose"); await tick(60);
check("untagged log files into the current org", CURRENT_ORG === "globex" && INC.logId === "20260925T100000Z_loose");
check("...and is visible even outside the default scope", items().includes("20260925T100000Z_loose"), items());

await showLogDetail("20260928T100000Z_quote", { scroll: false });
check("log detail offers File as incident in its org", $("logDetailActions").textContent.includes("File as incident in acmeprod"));
check("log detail lists the incidents filed from it", $("logDetailMeta").textContent.includes("acmeprod / 20261002T100000Z_2"));

log("\\n-- a reader --");
CURRENT_USER = { username: "rita", role: "reader" };
await loadLogs(); await tick();
check("no File incident for readers", !$("logsTable").textContent.includes("File incident"));

} catch (e) { window.__check("no exception thrown: " + e.message + "\\n" + e.stack, false); }
window.__done();
})();
`;

window.eval(read("app.js") + "\n;\n" + read("chat.js") + "\n;\n" + read("home.js") + "\n;\n" + read("guide.js") + "\n;\n" + CHECKS);

/* Log Normalizer page (static/app.js "log normalizer + log library") against
 * the REAL index.html in jsdom: upload tags, owner column, search + account /
 * org / owner / status filters, archive, delete, bulk selection.
 *
 *     npm install jsdom
 *     node tests/test_ui_log_library.js
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
  loose: { name: "Loose", owner: "dana", visibility: "public", can_manage: true,
    instance_url: "https://loose.my.salesforce.com", environment: "production", component_counts: {} },
};
const LOG = (id, o) => Object.assign({ log_id: id, label: null, source_log: "x.log", timestamp: "2026-09-28T10:00:00Z",
  owner: "dana", org_id: null, account: null, archived: false, can_manage: true, exception_count: 0,
  top_exception: null, involved_components: [] }, o);

const state = {
  logs: [
    LOG("20260928T100000Z_quote", { label: "Quote NPE", org_id: "acmeprod", account: "Acme Corp", owner: "dana",
      exception_count: 2, top_exception: "System.NullPointerException", org_environment: "production" }),
    LOG("20260927T100000Z_globex", { label: "Globex batch", account: "Globex", owner: "sam", can_manage: false,
      top_exception: "System.LimitException", exception_count: 1 }),
    LOG("20260926T100000Z_old", { label: "Old one", owner: "dana", archived: true, archived_by: "dana" }),
    LOG("20250101T000000Z_legacy", { owner: null, can_manage: false }),
  ],
  uploads: [], patches: [], deletes: [], serverBuild: 30,
};

const routes = {
  "/api/auth/me": () => ({ status: 401 }),
  "/api/build": () => ({ json: { build: state.serverBuild } }),
  "/api/me/guide": () => ({ status: 404 }),
  "/api/home": () => ({ json: { orgs: {} } }),
  "/api/orgs": () => ({ json: ORGS_FIX }),
  "/api/logs/normalize": (url, opts) => {
    const f = {}; opts.body.forEach((v, k) => { f[k] = v; });
    state.uploads.push(f);
    const stored = f.store === "true";
    const meta = stored ? LOG("20260928T120000Z_new", { label: f.label || null, org_id: f.org_id || null,
      account: f.org_id ? ORGS_FIX[f.org_id].account || f.account || null : f.account || null }) : null;
    if (stored && !state.serverBuild) { delete meta.owner; meta.org_id = null; meta.account = null; }  // old server
    if (stored) state.logs.unshift(meta);
    return { json: { normalized_log: { exceptions: [], execution_units: [{}] }, stored, log_id: stored ? meta.log_id : null, meta } };
  },
  "/api/logs": (url, opts) => {
    const u = String(url);
    const m = u.match(/^\/api\/logs\/([^/?]+)/);
    if (m && opts && opts.method === "PATCH") {
      const body = JSON.parse(opts.body);
      state.patches.push({ id: m[1], body });
      const log = state.logs.find(l => l.log_id === m[1]);
      Object.assign(log, body);
      return { json: log };
    }
    if (m && opts && opts.method === "DELETE") {
      state.deletes.push(m[1]);
      state.logs = state.logs.filter(l => l.log_id !== m[1]);
      return { json: { deleted: true } };
    }
    if (m) {
      const log = state.logs.find(l => l.log_id === m[1]);
      return log ? { json: { meta: log, normalized_log: { exceptions: [], execution_units: [] } } } : { status: 404 };
    }
    return { json: state.logs };
  },
  "/api/chats": () => ({ json: [] }),
  "/api/llm": () => ({ json: { configured: true } }),
  "/api/usage/me": () => ({ json: {} }),
};
window.fetch = async (url, opts) => {
  const u = String(url);
  const key = Object.keys(routes).sort((a, b) => b.length - a.length).find(k => u.startsWith(k));
  const r = key ? routes[key](u, opts) : { json: {} };
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
  console.log("All log library UI checks passed.");
  process.exit(0);
};

const CHECKS = `
(async () => {
const check = window.__check, log = window.__log, S = window.__state;
const $ = id => document.getElementById(id);
const tick = (ms = 30) => new Promise(r => setTimeout(r, ms));
const rows = () => [...$("logsTable").querySelectorAll("tr[data-log-id]")].map(r => r.dataset.logId);
const confirmNext = async () => { await tick(10); const b = document.querySelector(".modal-backdrop button[type=submit]"); if (b) b.click(); };
try {

CURRENT_USER = { username: "dana", role: "user" };
ORGS = ${JSON.stringify(ORGS_FIX)};
CURRENT_ORG = "acmeprod";
showView("logs");
await tick(80);

log("\\n-- upload form --");
check("normalize is disabled until a file is chosen", $("logNormalizeBtn").disabled);
check("org picker lists orgs grouped by account", !!$("logOrg").querySelector('optgroup[label="Acme Corp"] option[value="acmeuat"]'));
check("defaults to the active org", $("logOrg").value === "acmeprod");
check("the org's account fills and locks the account box", $("logAccount").value === "Acme Corp" && $("logAccount").disabled);
check("account suggestions include log-only accounts", !!$("logAccountList").querySelector('option[value="Globex"]'));
$("logOrg").value = "loose"; logOrgChanged();
check("an org with no account frees the box and clears the auto value", !$("logAccount").disabled && $("logAccount").value === "");
check("choosing a tag ticks Store", $("logStore").checked);
$("logAccount").value = "Loose Co"; logTagEdited();
check("hint explains a no-account org", $("logTagHint").textContent.includes("has no account yet"));
$("logOrg").value = "acmeuat"; logOrgChanged();
check("hint warns about a private org", $("logTagHint").textContent.includes("private"));
setLogFile(new File(["64.0 APEX_CODE,FINEST"], "case123.log"));
check("the chosen file shows", $("logFileName").textContent === "case123.log" && !$("logNormalizeBtn").disabled);
$("logLabel").value = "Case 123";
await normalizeLog();
await tick(40);
const up = S.uploads[S.uploads.length - 1];
check("upload carries store + org tag + label", up.store === "true" && up.org_id === "acmeuat" && up.label === "Case 123", JSON.stringify(up));
check("status names where it was stored", $("logStatus").textContent.includes("Acme Corp"));
check("new log appears at the top of the library", rows()[0] === "20260928T120000Z_new");
check("result offers Open in library", $("logResultActions").textContent.includes("Open in library"));

$("logStore").checked = false; LOGLIB.storeTouched = true;
await normalizeLog();
await tick(20);
check("normalize-only sends no tags", S.uploads[S.uploads.length - 1].store === "false" && !S.uploads[S.uploads.length - 1].org_id);
check("...and offers Save to library", $("logResultActions").textContent.includes("Save to library"));

log("\\n-- library table --");
check("archived hidden by default", !rows().includes("20260926T100000Z_old"));
check("owner column shows You for mine", $("logsTable").querySelector('tr[data-log-id="20260928T100000Z_quote"]').textContent.includes("You"));
check("...and the owner's name for others", $("logsTable").querySelector('tr[data-log-id="20260927T100000Z_globex"]').textContent.includes("sam"));
check("legacy logs show no owner", $("logsTable").querySelector('tr[data-log-id="20250101T000000Z_legacy"] .owner-tag') === null);
check("account and org shown", $("logsTable").querySelector('tr[data-log-id="20260928T100000Z_quote"]').textContent.includes("acmeprod"));
check("non-owner row has no Delete", !$("logsTable").querySelector('tr[data-log-id="20260927T100000Z_globex"]').textContent.includes("Delete"));
check("non-owner row checkbox disabled", $("logsTable").querySelector('tr[data-log-id="20260927T100000Z_globex"] input[type=checkbox]').disabled);
check("count chip shows the active total", $("logCount").textContent === "4", $("logCount").textContent);

log("\\n-- search and filters --");
$("logSearch").value = "globex"; scheduleLogFilter(); await tick(160);
check("search by account", rows().length === 1 && rows()[0] === "20260927T100000Z_globex", rows().join());
check("count reads N of M", $("logCount").textContent === "1 of 4");
$("logSearch").value = "acme npe"; scheduleLogFilter(); await tick(160);
check("multi-word search ANDs", rows().join() === "20260928T100000Z_quote", rows().join());
$("logSearch").value = "old one"; scheduleLogFilter(); await tick(160);
check("an archived-only match explains itself", $("logsTable").textContent.includes("archived logs match"));
clearLogFilters();
check("clear filters restores the list", rows().length === 4);
const acmeChip = [...$("logAccountChips").querySelectorAll("button")].find(b => b.textContent.includes("Acme Corp"));
check("account chips show counts", acmeChip && acmeChip.textContent.includes("2"), acmeChip && acmeChip.textContent);
check("the chip's handler targets that account", acmeChip.getAttribute("onclick").includes('"acme corp"'), acmeChip.getAttribute("onclick"));
setLogFilter("account", accountKey("Acme Corp"));   // inline handlers do not run in jsdom
check("the chip is marked selected", [...$("logAccountChips").querySelectorAll("button.acc")].some(b => b.textContent.includes("Acme Corp")));
check("filtering by an account chip", rows().every(id => ["20260928T100000Z_quote", "20260928T120000Z_new"].includes(id)) && rows().length === 2, rows().join());
check("org picker narrows to the account's orgs", [...$("logFilterOrg").options].map(o => o.value).join() === ",acmeprod,acmeuat");
setLogFilter("org_id", "acmeprod");
check("org filter", rows().join() === "20260928T100000Z_quote");
clearLogFilters();
setLogFilter("account", "__unassigned__");
check("unassigned chip", rows().join() === "20250101T000000Z_legacy", rows().join());
clearLogFilters();
setLogFilter("owner", "me");
check("Mine", rows().length === 2 && !rows().includes("20260927T100000Z_globex"), rows().join());
setLogFilter("owner", "");
setLogFilter("status", "archived");
check("Archived view", rows().join() === "20260926T100000Z_old");
check("archived row is marked", $("logsTable").textContent.includes("Archived") && $("logsTable").textContent.includes("Restore"));
setLogFilter("status", "active");

log("\\n-- archive, detail, delete --");
await setLogArchived("20260928T100000Z_quote", true);
await tick(20);
check("archive PATCHes archived:true", S.patches.some(p => p.id === "20260928T100000Z_quote" && p.body.archived === true));
check("archived row leaves the active view", !rows().includes("20260928T100000Z_quote"));
await setLogArchived("20260928T100000Z_quote", false);
await tick(20);
await showLogDetail("20260927T100000Z_globex");
check("detail shows owner and account", $("logDetailMeta").textContent.includes("sam") && $("logDetailMeta").textContent.includes("Globex"));
check("detail tells a non-owner who can manage it", $("logDetailActions").textContent.includes("Only sam or an admin"));
check("opened row is highlighted", $("logsTable").querySelector("tr.open").dataset.logId === "20260927T100000Z_globex");
await showLogDetail("20260928T100000Z_quote");
check("owner's detail offers Archive + Delete", $("logDetailActions").textContent.includes("Archive") && $("logDetailActions").textContent.includes("Delete"));
const del = deleteLogs(["20260928T100000Z_quote"]);
await tick(15);
check("delete asks first", document.querySelector(".modal-backdrop").textContent.includes("can't be undone"));
await confirmNext(); await del; await tick(20);
check("delete calls the API", S.deletes.includes("20260928T100000Z_quote"));
check("deleted log leaves the table and closes the detail", !rows().includes("20260928T100000Z_quote") && $("logDetailCard").style.display === "none");

log("\\n-- bulk --");
toggleAllLogs(true);
check("select-all only picks manageable logs", [...LOGLIB.selected].join() === "20260928T120000Z_new", [...LOGLIB.selected].join());
check("bulk bar appears", $("logBulkBar").style.display === "flex" && $("logBulkBar").textContent.includes("1 selected"));
await bulkArchive(true);
await tick(20);
check("bulk archive", S.patches.some(p => p.id === "20260928T120000Z_new" && p.body.archived === true) && $("logBulkBar").style.display === "none");

setLogFilter("status", "all"); toggleAllLogs(true);
const nSel = LOGLIB.selected.size;
setLogFilter("account", accountKey("Globex"));
check("filtering drops hidden rows from the selection", nSel > 0 && LOGLIB.selected.size === 0, nSel + "/" + LOGLIB.selected.size);
clearLogFilters();

log("\\n-- new page, old server --");
S.serverBuild = 0;
await checkServerBuild();
check("a stale server raises the banner", $("buildBanner").style.display === "" && $("buildBanner").textContent.includes("Restart"));
const ownerlessMeta = S.logs[0];
delete ownerlessMeta.can_manage;
await showLogDetail(ownerlessMeta.log_id, { scroll: false });
check("detail blames the stale server, not permissions", $("logDetailActions").textContent.includes("older code")
  && !$("logDetailActions").textContent.includes("Only an admin"), $("logDetailActions").textContent);
ownerlessMeta.can_manage = true;
setLogFile(new File(["x"], "Non-working 1.txt"));
$("logOrg").value = "acmeprod"; logOrgChanged(); $("logStore").checked = true;
await normalizeLog();
await tick(20);
check("an upload whose tags were dropped says why", $("logStatus").textContent.includes("ignored the owner and tags"), $("logStatus").textContent);
S.serverBuild = 30;
await checkServerBuild();
check("matching builds hide the banner", $("buildBanner").style.display === "none");

log("\\n-- edit dialog lists stored accounts and orgs (2026-10-08 regression) --");
{
  const target = S.logs.find(l => l.can_manage) || S.logs[0];
  target.org_id = null; target.account = "acme corp";
  const pending = editLogTags(target.log_id);
  await tick(5);
  const orgSel = document.querySelector("#mf-org_id"), acctSel = document.querySelector("#mf-account");
  check("org is a real dropdown of visible orgs", orgSel && orgSel.tagName === "SELECT"
    && [...orgSel.options].some(o => o.value === "acmeprod"));
  check("account is a real dropdown of stored accounts", acctSel && acctSel.tagName === "SELECT"
    && [...acctSel.options].some(o => o.value === "Acme Corp"));
  check("a differently-cased saved account selects the stored one", acctSel && acctSel.value === "Acme Corp", acctSel && acctSel.value);
  orgSel.value = "loose"; orgSel.dispatchEvent(new window.Event("change"));
  check("an org without an account leaves the account alone", acctSel.value === "Acme Corp");
  acctSel.value = "__other__"; acctSel.dispatchEvent(new window.Event("change"));
  const other = document.querySelector("#mf-account-other");
  check("New account... reveals a text box", other && other.style.display !== "none");
  other.value = "Globex";
  let patched = null;
  const origPatch = patchLog;
  patchLog = async (id, body) => { patched = body; return { ok: true }; };
  document.querySelector(".modal form").dispatchEvent(new window.Event("submit", { cancelable: true }));
  await pending;
  patchLog = origPatch;
  check("saves the picked org and the typed account", patched && patched.org_id === "loose"
    && patched.account === "Globex", JSON.stringify(patched));
}

log("\\n-- a reader --");
CURRENT_USER = { username: "rita", role: "reader" };
S.logs.forEach(l => { l.can_manage = false; });
await loadLogs();
check("no selection column when nothing is manageable", $("logLibraryCard").classList.contains("no-select"));
check("no Delete anywhere", !$("logsTable").textContent.includes("Delete"));

} catch (e) { window.__check("no exception thrown: " + e.message + "\\n" + e.stack, false); }
window.__done();
})();
`;

window.eval(read("app.js") + "\n;\n" + read("chat.js") + "\n;\n" + read("home.js") + "\n;\n" + read("guide.js") + "\n;\n" + CHECKS);

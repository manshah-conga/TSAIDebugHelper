/* TS Intelligent Debug Helper -- web UI.
 *
 * Layout of this file:
 *   1. shared UX layer  -- api() wrapper, session expiry, toasts, modal, tables
 *   2. renderers        -- readable views of the RCA pack / normalized log / cards
 *   3. views            -- connections, dashboard, incidents, known issues, logs,
 *                          tokens, admin
 *   4. auth + boot
 *
 * Everything goes through api()/apiJson() rather than raw fetch, so a expired
 * session is caught in exactly one place instead of silently rendering empty
 * tables everywhere.
 */
let CURRENT_ORG = null;
let ORGS = {};

// =====================================================================
// 1. shared UX layer
// =====================================================================

let SESSION_DEAD = false;

/** Single choke point for every server call. Returns the Response as-is so
 *  callers can still branch on res.ok, but handles the two failure modes the
 *  old code ignored: an expired session (401 -> back to the login screen with
 *  an explanation, instead of tables full of nothing) and an unreachable
 *  server (fetch rejects -> a toast, instead of a silent console error). */
async function api(path, opts = {}) {
  let res;
  try {
    res = await fetch(path, opts);
  } catch (e) {
    if (!SESSION_DEAD) toast("Can't reach the server -- is it still running?", "error");
    throw e;
  }
  if (res.status === 401 && !path.startsWith("/api/auth/")) sessionExpired();
  return res;
}

/** api() + JSON decode. Returns `fallback` (default null) for any non-2xx, so
 *  a caller that just wants data can stay linear. */
async function apiJson(path, opts = {}, fallback = null) {
  try {
    const res = await api(path, opts);
    if (!res.ok) return fallback;
    return await res.json();
  } catch (e) {
    return fallback;
  }
}

/** Best-effort human message out of a FastAPI error response. */
async function errorText(res) {
  try {
    const body = await res.json();
    if (typeof body.detail === "string") return body.detail;
    if (body.detail) return JSON.stringify(body.detail);
    return JSON.stringify(body);
  } catch (e) {
    return `${res.status} ${res.statusText}`;
  }
}

function sessionExpired() {
  if (SESSION_DEAD) return;   // one bounce, however many requests were in flight
  SESSION_DEAD = true;
  CURRENT_USER = null;
  document.getElementById("appRoot").style.display = "none";
  const overlay = document.getElementById("loginOverlay");
  overlay.style.display = "flex";
  const s = document.getElementById("loginStatus");
  s.textContent = "Your session expired. Please sign in again.";
  s.className = "status-line error";
  document.getElementById("loginUser").focus();
}

// ---------- toasts ----------

function toast(message, kind = "info", ms = 5000) {
  if (SESSION_DEAD && kind === "error") return;  // don't pile errors on the login screen
  const host = document.getElementById("toasts");
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = message;
  el.onclick = () => el.remove();
  host.appendChild(el);
  setTimeout(() => el.remove(), ms);
}

// ---------- modal (replaces prompt()/alert() for anything with input) ----------

/** modal({title, body, fields:[{name,label,type,placeholder,value,options,hint}], submitLabel})
 *  `options` (string[]) adds a datalist of suggestions; `hint` a line under the input.
 *  -> Promise<null | {name: value}>.  Escape / Cancel / backdrop resolve null. */
function modal({ title, body = "", fields = [], submitLabel = "OK", danger = false }) {
  return new Promise(resolve => {
    const back = document.createElement("div");
    back.className = "modal-backdrop";
    back.innerHTML = `
      <div class="modal" role="dialog" aria-modal="true">
        <h3>${escapeHtml(title)}</h3>
        ${body ? `<p class="muted">${body}</p>` : ""}
        <form>
          ${fields.map(f => `
            <label for="mf-${f.name}">${escapeHtml(f.label)}</label>
            <input id="mf-${f.name}" name="${f.name}" type="${f.type || "text"}"
                   placeholder="${escapeHtml(f.placeholder || "")}" value="${escapeHtml(f.value || "")}"
                   ${f.options ? `list="mf-${f.name}-list" autocomplete="off"` : ""}>
            ${f.options ? `<datalist id="mf-${f.name}-list">${f.options.map(o =>
              `<option value="${escapeHtml(o)}"></option>`).join("")}</datalist>` : ""}
            ${f.hint ? `<div class="field-hint">${f.hint}</div>` : ""}
          `).join("")}
          <div class="modal-actions">
            <button type="button" class="secondary" data-cancel>Cancel</button>
            <button type="submit" class="primary ${danger ? "danger" : ""}">${escapeHtml(submitLabel)}</button>
          </div>
        </form>
      </div>`;
    const close = value => { document.removeEventListener("keydown", onKey); back.remove(); resolve(value); };
    const onKey = e => { if (e.key === "Escape") close(null); };
    back.querySelector("[data-cancel]").onclick = () => close(null);
    back.onclick = e => { if (e.target === back) close(null); };
    back.querySelector("form").onsubmit = e => {
      e.preventDefault();
      const out = {};
      fields.forEach(f => { out[f.name] = back.querySelector(`#mf-${f.name}`).value; });
      close(out);
    };
    document.addEventListener("keydown", onKey);
    document.body.appendChild(back);
    const first = back.querySelector("input");
    if (first) { first.focus(); if (first.value) first.select(); } else back.querySelector("[type=submit]").focus();
  });
}

function confirmModal(title, body, submitLabel = "Confirm") {
  return modal({ title, body, fields: [], submitLabel, danger: true }).then(r => r !== null);
}

// ---------- small render helpers ----------

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/** Fill a <tbody>, with a proper empty state instead of a blank void.
 *  `emptyMessage` is either plain text or an emptyStateHtml() spec -- the
 *  latter for tables where "nothing here" is the moment to explain what
 *  belongs here and offer the one action that puts it there. */
function fillTable(tbody, rows, colspan, emptyMessage, rowFn) {
  tbody.innerHTML = "";
  if (!rows || !rows.length) {
    const rich = emptyMessage && typeof emptyMessage === "object";
    tbody.innerHTML = `<tr class="empty-row${rich ? " rich" : ""}"><td colspan="${colspan}">${
      rich ? emptyStateHtml(emptyMessage) : escapeHtml(emptyMessage)}</td></tr>`;
    return;
  }
  rows.forEach(r => tbody.appendChild(rowFn(r)));
}

/** A teaching empty state: what goes here, why it matters, and a button to
 *  do it. `actions[].onclick` is app-authored code, never user data.
 *  `role: "user"` hides an action from readers, who could not complete it. */
function emptyStateHtml({ title, body = "", actions = [], icon = "" }) {
  const canWrite = !CURRENT_USER || CURRENT_USER.role !== "reader";
  const acts = actions.filter(a => a.role !== "user" || canWrite);
  return `<div class="empty-state">
      ${icon ? `<div class="empty-icon" aria-hidden="true">${icon}</div>` : ""}
      <div class="empty-title">${escapeHtml(title)}</div>
      ${body ? `<div class="empty-body">${body}</div>` : ""}
      ${acts.length ? `<div class="empty-actions">${acts.map(a =>
        `<button type="button" class="${a.primary ? "primary" : "secondary"}" onclick="${escapeHtml(a.onclick)}">${
          escapeHtml(a.label)}</button>`).join("")}</div>` : ""}
    </div>`;
}

/** Tell the onboarding guide something happened (guide.js). Guarded: the
 *  render tests load app.js on its own. */
function track(event) {
  if (typeof guideMark === "function") guideMark(event);
}

function setBusy(el, message = "Loading...") {
  if (typeof el === "string") el = document.getElementById(el);
  if (el) el.innerHTML = `<p class="muted loading">${escapeHtml(message)}</p>`;
}

function collapsibleJson(label, obj) {
  return `<details class="raw-json"><summary>${escapeHtml(label)}</summary>
    <pre>${escapeHtml(JSON.stringify(obj, null, 2))}</pre></details>`;
}

function fmtWhen(ts) {
  if (!ts) return "-";
  // Stored as 20260828T101500Z-ish or ISO; show it readably without a date lib.
  const iso = /^\d{8}T\d{6}Z/.test(ts)
    ? `${ts.slice(0, 4)}-${ts.slice(4, 6)}-${ts.slice(6, 8)} ${ts.slice(9, 11)}:${ts.slice(11, 13)}`
    : String(ts).replace("T", " ").replace("Z", "");
  return iso;
}

function ageBadge(days) {
  if (days === null || days === undefined) return "";
  const cls = days <= 7 ? "recurrence" : days <= 30 ? "medium" : "";
  return `<span class="badge ${cls}">changed ${days}d ago</span>`;
}

// ---------- nav ----------

document.querySelectorAll("nav button[data-view]").forEach(btn => {
  btn.addEventListener("click", () => showView(btn.dataset.view));
});
let LAST_VIEW = "connections";

function showView(name) {
  const current = (document.querySelector(".view.active") || {}).id || "";
  // Remembered so Esc out of full-screen chat lands back where the engineer
  // was, rather than dumping them on the default tab.
  if (current && current !== "view-chat") LAST_VIEW = current.replace("view-", "");
  document.querySelectorAll("nav button").forEach(b => b.classList.toggle("active", b.dataset.view === name));
  document.querySelectorAll(".view").forEach(v => v.classList.toggle("active", v.id === `view-${name}`));
  // Full-screen chat takes over the window, so the page must not also scroll
  // behind it -- two scrollbars over one conversation is disorienting.
  document.body.classList.toggle("chat-fullscreen", name === "chat");
  if (typeof guideTabSeen === "function") guideTabSeen(name);
  if (name === "connections" && typeof loadHome === "function") loadHome();
  if (name === "dashboard") loadDashboard();
  if (name === "incidents") loadIncidents();
  if (name === "known") loadKnownIssues();
  if (name === "logs") loadLogs();
  if (name === "tokens") loadTokens();
  if (name === "usage") loadUsageView();
  if (name === "admin") loadUsers();
  if (name === "chat" && typeof mountChatFull === "function") mountChatFull();
}

function renderOrgPicker() {
  const el = document.getElementById("orgPicker");
  const ids = Object.keys(ORGS);
  if (!ids.length) { el.textContent = "No orgs connected yet"; return; }
  if (!CURRENT_ORG || !ORGS[CURRENT_ORG]) CURRENT_ORG = ids[0];
  el.innerHTML = "Active org: <select id='orgSelect'></select>";
  const sel = document.getElementById("orgSelect");
  // Grouped under the customer account once anyone uses accounts, so a
  // customer's production org and its sandboxes sit together here as well.
  const groups = accountGroups(Object.entries(ORGS));
  const grouped = showAccountHeaders(groups);
  groups.forEach(g => {
    const parent = grouped ? document.createElement("optgroup") : sel;
    if (grouped) { parent.label = g.name || "Unassigned"; sel.appendChild(parent); }
    g.orgs.forEach(([id, o]) => {
      const opt = document.createElement("option");
      opt.value = id; opt.textContent = `${id} (${o.name})`;
      if (id === CURRENT_ORG) opt.selected = true;
      parent.appendChild(opt);
    });
  });
  sel.addEventListener("change", () => setActiveOrg(sel.value));
}

/** Switching org has to invalidate every org-scoped view, not just the two
 *  that used to be refreshed -- otherwise the Known Issues tab keeps showing
 *  the previous org's data. */
function setActiveOrg(id) {
  CURRENT_ORG = id;
  document.getElementById("incidentDetailCard").style.display = "none";
  document.getElementById("searchResults").innerHTML = "";
  document.getElementById("fieldWriterResults").innerHTML = "";
  renderOrgPicker();
  loadDashboard(); loadIncidents(); loadKnownIssues();
  if (typeof renderHomeOrgs === "function") renderHomeOrgs();
  // The chat dock scopes its tools to CURRENT_ORG, so it has to hear about
  // this too -- otherwise its org chip quietly disagrees with the rest of the
  // app and the assistant answers about the wrong org.
  if (typeof chatOrgChanged === "function") chatOrgChanged();
}

// =====================================================================
// 2. renderers -- the readable views
// =====================================================================

const TYPE_LABEL = {
  ApexClass: "Class", ApexTrigger: "Trigger", ApexInterface: "Interface",
  ApexEnum: "Enum", Flow: "Flow", LWC: "LWC", WorkflowFieldUpdate: "Field update",
};

/** Rank the components in an RCA pack so the likely culprits are at the top
 *  instead of alphabetical in a JSON blob. Weighting, highest first:
 *    - named directly in the log (not just a call-graph neighbour)
 *    - recently changed (a change right before an incident is the classic cause)
 *    - writes the suspect field
 *    - is a trigger/flow (runs implicitly -- easy to forget)
 *    - does DML / callouts (can fail or cascade)
 *  Managed-package components sink: you usually can't fix those anyway. */
function rankSuspects(pack) {
  const named = new Set((pack.normalized_log || {}).involved_components || []);
  const recent = {};
  (pack.recently_changed_components || []).forEach(r => { recent[r.id] = r.age_days; });
  const fieldWriters = new Set((pack.suspect_field_writers || []).map(w => w.component));

  return Object.entries(pack.primary_components || {}).map(([id, card]) => {
    const reasons = [];
    let score = 0;
    if (named.has(id)) { score += 50; reasons.push("named in the log"); }
    if (id in recent) {
      score += Math.max(5, 40 - recent[id]);
      reasons.push(`changed ${recent[id]} day(s) ago`);
    }
    if (fieldWriters.has(id)) { score += 35; reasons.push(`writes ${pack.suspect_field}`); }
    if (card.type === "ApexTrigger") { score += 15; reasons.push("trigger -- runs on every save"); }
    if (card.type === "Flow") { score += 12; reasons.push("flow automation"); }
    if ((card.dml || []).length) { score += 8; reasons.push(`${card.dml.length} DML operation(s)`); }
    if ((card.callouts || []).length) { score += 8; reasons.push(`${card.callouts.length} callout(s)`); }
    if (card.is_test_class) score -= 40;
    if (card.is_managed || card.is_customer_authored === false) {
      score -= 25;
      reasons.push("managed package -- not editable");
    }
    return { id, card, score, reasons, ageDays: recent[id] };
  }).sort((a, b) => b.score - a.score);
}

function suspectRow(s, rank) {
  const c = s.card;
  const facts = [
    (c.soql || []).length ? `${c.soql.length} SOQL` : null,
    (c.dml || []).length ? `${c.dml.length} DML` : null,
    (c.callouts || []).length ? `${c.callouts.length} callout(s)` : null,
    (c.objects_referenced || []).length ? `objects: ${c.objects_referenced.slice(0, 5).join(", ")}` : null,
    c.loc ? `${c.loc} lines` : null,
  ].filter(Boolean);
  return `
    <div class="suspect">
      <div class="suspect-head">
        <span class="rank">#${rank}</span>
        <b>${escapeHtml(s.id)}</b>
        <span class="badge type">${escapeHtml(TYPE_LABEL[c.type] || c.type || "?")}</span>
        ${c.is_customer_authored === false || c.is_managed ? `<span class="badge managed">managed</span>` : ""}
        ${c.is_test_class ? `<span class="badge">test</span>` : ""}
        ${ageBadge(s.ageDays)}
      </div>
      ${s.reasons.length ? `<div class="why">${escapeHtml(s.reasons.join(" &middot; ").replace(/&middot;/g, "·"))}</div>` : ""}
      ${facts.length ? `<div class="muted">${escapeHtml(facts.join(" · "))}</div>` : ""}
      ${c.file ? `<div class="muted mono">${escapeHtml(c.file)}</div>` : ""}
      ${collapsibleJson("Full component card", c)}
    </div>`;
}

/** The transaction shape from a normalized log -- shared by the incident view
 *  and the standalone log library, since it needs no org knowledge at all. */
// One governor-limit tile per entry. The normalizer emits {used, max}; older
// fixtures and the demo use {used, limit} -- accept either so a key rename
// can never again render as "0/undefined".
function renderLimitGrid(limits) {
  return `<div class="limits">` + Object.keys(limits).map(k => {
    const v = limits[k];
    let used = null, cap = null, close = false, peak = null;
    if (v && typeof v === "object") {
      used = v.used ?? null;
      cap = v.max ?? v.limit ?? null;
      close = !!v.close_to_limit;
      peak = v.peak_used ?? null;
    }
    // Colour by the worst point in the log, not just the last checkpoint:
    // a log with several transactions can end on a tiny one.
    const worst = peak != null ? peak : used;
    const pct = (worst != null && cap) ? Math.round((worst / cap) * 100) : null;
    const cls = close ? "high" : pct === null ? "" : pct >= 90 ? "high" : pct >= 70 ? "medium" : "low";
    const figure = used == null ? escapeHtml(String(v))
      : cap == null ? String(used) : `${used}/${cap}`;
    const tips = [];
    if (close) tips.push("Salesforce flagged this as CLOSE TO LIMIT");
    if (peak != null) tips.push(`Peaked at ${peak}/${cap} at an earlier checkpoint -- this log likely holds more than one transaction`);
    return `<div class="limit ${cls}"${tips.length ? ` title="${escapeHtml(tips.join(". "))}"` : ""}>
      <span>${escapeHtml(k)}</span><b>${figure}</b>
      ${pct !== null ? `<i>${peak != null ? `peak ${peak} · ` : ""}${pct}%</i>` : ""}</div>`;
  }).join("") + `</div>`;
}

function renderNormalizedLog(n, { heading = true } = {}) {
  if (!n) return "";
  const exc = n.exceptions || [];
  const parts = [];

  if (exc.length) {
    parts.push(`<h3>${heading ? "Exception" + (exc.length > 1 ? `s (${exc.length})` : "") : ""}</h3>`);
    exc.slice(0, 5).forEach(e => {
      parts.push(`
        <div class="exception">
          <div class="exc-type">${escapeHtml(e.type || "Exception")}</div>
          <div class="exc-msg">${escapeHtml(e.message || "(no message)")}</div>
          ${(e.stack || []).length ? `<details><summary>Stack (${e.stack.length} frame(s))</summary>
            <pre>${escapeHtml(e.stack.join("\n"))}</pre></details>` : ""}
        </div>`);
    });
    if (exc.length > 5) parts.push(`<p class="muted">+ ${exc.length - 5} more exception(s) -- see the raw JSON.</p>`);
  } else {
    parts.push(`<p class="muted">No exception in this log. The transaction completed -- if a value is wrong,
      it was written deliberately by something, so work from the field writers below.</p>`);
  }

  const vf = n.validation_failures || [];
  if (vf.length) {
    parts.push(`<h3>Validation failures (${vf.length})</h3><ul class="tight">` +
      vf.slice(0, 10).map(v => `<li>${escapeHtml(typeof v === "string" ? v : JSON.stringify(v))}</li>`).join("") +
      `</ul>`);
  }

  const units = n.execution_units || [];
  if (units.length) {
    parts.push(`<h3>Execution units (${units.length})</h3><ul class="tight units">` +
      units.slice(0, 15).map(u =>
        `<li class="${u.had_exception ? "failed" : ""}">${"&nbsp;".repeat((u.depth || 0) * 2)}
          ${escapeHtml(u.label)}${u.had_exception ? ' <span class="badge high">threw</span>' : ""}</li>`).join("") +
      `</ul>`);
  }

  const soql = n.soql_summary || [], dml = n.dml_summary || [];
  if (soql.length || dml.length) {
    parts.push(`<h3>Database activity</h3><div class="grid2">`);
    if (soql.length) {
      parts.push(`<div><b>SOQL</b><table><thead><tr><th>Object</th><th>#</th></tr></thead><tbody>` +
        soql.slice(0, 10).map(s =>
          `<tr><td>${escapeHtml(s.object || s.signature || "?")}</td><td>${s.occurrences ?? 1}</td></tr>`).join("") +
        `</tbody></table></div>`);
    }
    if (dml.length) {
      parts.push(`<div><b>DML</b><table><thead><tr><th>Op</th><th>Object</th><th>#</th><th>Rows</th></tr></thead><tbody>` +
        dml.slice(0, 10).map(d =>
          `<tr><td>${escapeHtml(d.operation || "?")}</td><td>${escapeHtml(d.object || "?")}</td>
           <td>${d.occurrences ?? 1}</td><td>${d.total_rows ?? "-"}</td></tr>`).join("") +
        `</tbody></table></div>`);
    }
    parts.push(`</div>`);
  }

  const callouts = n.callouts || [];
  if (callouts.length) {
    parts.push(`<h3>Callouts (${callouts.length})</h3><ul class="tight">` +
      callouts.slice(0, 10).map(c =>
        `<li class="mono">${escapeHtml(typeof c === "string" ? c : (c.endpoint || JSON.stringify(c)))}</li>`).join("") +
      `</ul>`);
  }

  const limits = n.limits_final || {};
  if (Object.keys(limits).length) {
    parts.push(`<h3>Governor limits at the end of the transaction</h3>` + renderLimitGrid(limits));
    // Managed packages get their own per-namespace limits. Only surface the
    // ones that actually consumed something -- all-zero blocks are noise.
    const others = Object.entries(n.limits_by_namespace || {})
      .filter(([ns, lim]) => ns !== "(default)" && lim !== limits
        && Object.values(lim || {}).some(v => v && (v.used > 0 || v.peak_used > 0)));
    if (others.length) {
      parts.push(`<details><summary>Managed-package namespaces (${others.length})</summary>` +
        others.map(([ns, lim]) => `<h4 class="mono">${escapeHtml(ns)}</h4>${renderLimitGrid(lim)}`).join("") +
        `</details>`);
    }
  }

  const dbg = n.user_debug || [];
  if (dbg.length) {
    parts.push(`<details><summary>System.debug output (${dbg.length} line(s))</summary>
      <pre>${escapeHtml(dbg.slice(0, 200).map(d => typeof d === "string" ? d : (d.message || JSON.stringify(d))).join("\n"))}</pre>
      </details>`);
  }

  return parts.join("");
}

function renderFieldWriters(data, field) {
  if (!data || !data.writers || !data.writers.length) {
    return `<p class="muted">Nothing in this org's knowledgebase writes <b>${escapeHtml(field)}</b>.
      If the value is still wrong, it came from outside tracked automation -- a page layout edit,
      the API, a managed package, or a data load.</p>`;
  }
  const order = ["Apex", "Flow", "Process Builder", "Workflow/Approval field update"];
  const groups = {};
  data.writers.forEach(w => { (groups[w.mechanism || "Apex"] = groups[w.mechanism || "Apex"] || []).push(w); });
  const mechs = Object.keys(groups).sort((a, b) => (order.indexOf(a) + 1 || 99) - (order.indexOf(b) + 1 || 99));

  let html = mechs.map(mech => `
    <h3>${escapeHtml(mech)} <span class="muted">(${groups[mech].length})</span></h3>
    ` + groups[mech].map(w => `
      <div class="writer">
        <span class="badge ${escapeHtml(w.risk || "")}">${escapeHtml((w.risk || "?").toUpperCase())}</span>
        <b>${escapeHtml(w.component)}</b>
        ${w.object ? `<span class="muted"> on ${escapeHtml(w.object)}</span>` : ""}
        ${w.age_days != null ? ageBadge(w.age_days) : ""}
        ${w.reason ? `<div class="muted">${escapeHtml(w.reason)}</div>` : ""}
        <div class="muted mono">${escapeHtml(field)} = ${escapeHtml(w.example ?? "(value not statically resolvable)")}</div>
      </div>`).join("")).join("");

  const criteria = data.used_in_entry_criteria_of || [];
  if (criteria.length) {
    html += `<h3>Also gates entry criteria of <span class="muted">(${criteria.length})</span></h3>
      <p class="muted">These don't write the field, but they branch on it -- a wrong value here changes what runs.</p>
      <div>${criteria.map(c => `<span class="pill">${escapeHtml(typeof c === "string" ? c : c.component || JSON.stringify(c))}</span>`).join(" ")}</div>`;
  }
  if (typeof askAbout === "function") {
    html += `<div class="ask-about-bar">
      <button class="secondary" onclick="askAbout(${JSON.stringify(
        `${field} is getting the wrong value. Rank these writers by which most likely set it last, and tell me what to check.`
      ).replace(/"/g, "&quot;")})">Ask about this field</button>
    </div>`;
  }
  return html;
}

function renderComponentCard(id, card) {
  const rows = [
    ["Type", TYPE_LABEL[card.type] || card.type],
    ["File", card.file],
    ["Lines", card.loc],
    ["Manageability", card.is_customer_authored === false || card.is_managed
      ? `managed (${card.namespace || "packaged"}) -- not editable in this org` : "customer-authored"],
    ["Sharing", card.sharing],
    ["Test class", card.is_test_class ? "yes" : null],
    ["Objects referenced", (card.objects_referenced || []).join(", ")],
    ["SOQL", (card.soql || []).length || null],
    ["DML", (card.dml || []).length || null],
    ["Callouts", (card.callouts || []).length || null],
    ["Calls", (card.calls_to || []).slice(0, 12).join(", ")],
    ["Entry points", (card.entry_points || []).map(e => typeof e === "string" ? e : e.kind || JSON.stringify(e)).join(", ")],
    ["Writes fields", (card.field_writes || []).map(f => f.field || f).slice(0, 12).join(", ")],
    ["Static mutable state", (card.static_mutable_state || []).map(s => s.name || s).join(", ")],
  ].filter(([, v]) => v !== null && v !== undefined && v !== "" && v !== 0);

  return `<div class="detail-block">
    <h3>${escapeHtml(id)}</h3>
    <div class="kv">${rows.map(([k, v]) =>
      `<div class="k">${escapeHtml(k)}</div><div class="v">${escapeHtml(String(v))}</div>`).join("")}</div>
    ${collapsibleJson("Full component card (JSON)", card)}
  </div>`;
}

// =====================================================================
// 3. views
// =====================================================================

// ---------- connections ----------

async function loadOrgs() {
  ORGS = await apiJson("/api/orgs", {}, {}) || {};
  renderOrgPicker();
  // The dock can mount before this resolves (enterApp does not await it), so
  // its org chip would otherwise be stuck on whatever it saw first.
  if (typeof chatOrgChanged === "function") chatOrgChanged();
  renderOrgsTable();
  if (typeof renderHomeOrgs === "function") renderHomeOrgs();
  markInFlightOrgs();
}

// ---------- customer accounts ----------
//
// Orgs carry an optional `account` -- the customer they belong to -- so a
// customer's production org and its sandboxes stack under one heading
// (server side: app/accounts.py). `environment` and `my_domain` come from
// the server too; the fallbacks below mirror its rules for orgs that are not
// in ORGS yet (the Connect form) or an older server that does not send them.

const UNASSIGNED_KEY = "__unassigned__";
const ENV_META = {
  production: { label: "Production", short: "PROD" },
  sandbox: { label: "Sandbox", short: "SANDBOX" },
  developer: { label: "Developer Edition", short: "DEV" },
  scratch: { label: "Scratch org", short: "SCRATCH" },
  unknown: { label: "Other", short: "" },
};
const ENV_RANK = { production: 0, sandbox: 1, developer: 2, scratch: 3, unknown: 4 };

function accountKey(name) {
  const n = String(name ?? "").replace(/\s+/g, " ").trim();
  return n ? n.toLowerCase() : UNASSIGNED_KEY;
}

function hostOf(url) {
  let u = String(url || "").trim();
  if (!u) return "";
  if (!u.includes("://")) u = "https://" + u;
  try { return new URL(u).hostname.toLowerCase(); } catch (e) { return ""; }
}

function envFromUrl(url) {
  const h = hostOf(url);
  if (!h) return "unknown";
  if (h.includes(".scratch.")) return "scratch";
  if (h.includes(".develop.")) return "developer";
  if (h.includes(".sandbox.") || h.startsWith("test.") || /^cs\d+\./.test(h) || h.split(".")[0].includes("--")) return "sandbox";
  if (h.includes("salesforce.com") || h.includes("force.com")) return "production";
  return "unknown";
}

function myDomainOf(url) {
  const h = hostOf(url);
  if (!h || !/(\.my\.salesforce\.com|force\.com)$/.test(h)) return null;
  const first = h.split(".")[0];
  if (/^(na|cs|eu|ap|um|gs)\d+$/.test(first)) return null;
  return first.split("--")[0] || null;
}

function orgEnv(o) { return (o && o.environment) || envFromUrl(o && o.instance_url); }
function orgMyDomain(o) { return (o && o.my_domain) || myDomainOf(o && o.instance_url); }

function envBadge(o) {
  const e = orgEnv(o), m = ENV_META[e];
  if (!m || !m.short) return "";
  return `<span class="env-badge env-${e}" title="${escapeHtml(m.label)} org">${m.short}</span>`;
}

/** "1 Production · 3 Sandbox" for a group header. */
function envSummary(orgs) {
  const n = {};
  orgs.forEach(([, o]) => { const e = orgEnv(o); n[e] = (n[e] || 0) + 1; });
  return Object.keys(n).sort((a, b) => ENV_RANK[a] - ENV_RANK[b])
    .map(e => `${n[e]} ${e === "unknown" ? "other" : ENV_META[e].label.replace(" Edition", "").replace(" org", "")}`).join(" \u00b7 ");
}

function isAccountPinned(key) {
  const prefs = typeof homePrefs === "function" ? homePrefs() : {};
  return (prefs.pinned_accounts || []).includes(key);
}

function isAccountCollapsed(key) {
  const prefs = typeof homePrefs === "function" ? homePrefs() : {};
  return (prefs.collapsed_accounts || []).includes(key);
}

/** key -> the spelling to show. The server snaps new names to the one in
 *  use, so a mismatch only comes from older data; the commonest spelling
 *  wins, and between equals one with capitals beats all-lowercase. */
function accountSpellings(orgs = Object.values(ORGS)) {
  const tally = new Map();
  orgs.forEach(o => {
    const name = String(o.account ?? "").replace(/\s+/g, " ").trim();
    if (!name) return;
    const key = accountKey(name);
    if (!tally.has(key)) tally.set(key, new Map());
    tally.get(key).set(name, (tally.get(key).get(name) || 0) + 1);
  });
  const out = new Map();
  tally.forEach((counts, key) => {
    out.set(key, [...counts.entries()].sort(([a, n], [b, m]) =>
      (m - n) || ((b !== b.toLowerCase()) - (a !== a.toLowerCase())) || a.localeCompare(b))[0][0]);
  });
  return out;
}

function accountDisplayName(name) {
  const key = accountKey(name);
  return key === UNASSIGNED_KEY ? null : (accountSpellings().get(key) || String(name).trim());
}

/** Every distinct account name on the orgs this person can see. */
function accountNames() {
  return [...accountSpellings().values()].sort((a, b) => a.localeCompare(b));
}

/** Group [id, org] entries by account. Pinned accounts first, then A-Z,
 *  Unassigned last. Inside a group: pinned orgs, the active org, production
 *  before sandboxes, then id. */
function accountGroups(entries) {
  const pinnedOrg = typeof isPinned === "function" ? isPinned : () => false;
  const map = new Map();
  const spell = accountSpellings(Object.values(ORGS));
  entries.forEach(([id, o]) => {
    const key = accountKey(o.account);
    if (!map.has(key)) map.set(key, { key, name: key === UNASSIGNED_KEY ? null : (spell.get(key) || String(o.account).trim()), orgs: [] });
    map.get(key).orgs.push([id, o]);
  });
  const groups = [...map.values()];
  groups.forEach(g => g.orgs.sort(([a, oa], [b, ob]) =>
    (pinnedOrg(b) - pinnedOrg(a)) || ((b === CURRENT_ORG) - (a === CURRENT_ORG))
    || (ENV_RANK[orgEnv(oa)] - ENV_RANK[orgEnv(ob)]) || a.localeCompare(b)));
  return groups.sort((a, b) =>
    ((a.key === UNASSIGNED_KEY) - (b.key === UNASSIGNED_KEY))
    || (isAccountPinned(b.key) - isAccountPinned(a.key))
    || (a.name || "").localeCompare(b.name || ""));
}

/** Headings only earn their space once someone has started using accounts:
 *  one "Unassigned" heading over every org is noise. */
function showAccountHeaders(groups) {
  return groups.length > 1 || (groups.length === 1 && groups[0].key !== UNASSIGNED_KEY);
}

/** The table form of the org list. Split out of loadOrgs so pinning an org
 *  or switching the active one can redraw it without refetching. Grouped by
 *  account the same way as the cards, one header row per account. */
function renderOrgsTable() {
  const tbody = document.getElementById("orgsTable");
  const matches = typeof orgMatchesFilter === "function" ? orgMatchesFilter : () => true;
  const filtering = typeof orgFilterText === "function" && !!orgFilterText();
  const groups = accountGroups(Object.entries(ORGS).filter(([id, o]) => matches(id, o)))
    .filter(g => typeof accountInFocus !== "function" || accountInFocus(g.key));
  const headers = showAccountHeaders(accountGroups(Object.entries(ORGS)));
  const rows = [];
  groups.forEach(g => {
    if (headers) rows.push({ head: g });
    if (!headers || filtering || !isAccountCollapsed(g.key)
        || (typeof accountFocusKey === "function" && accountFocusKey())) g.orgs.forEach(e => rows.push({ org: e }));
  });
  fillTable(tbody, Object.keys(ORGS).length ? rows : [], 12, {
      title: "No orgs you can see yet",
      body: "Connect one above to build its knowledgebase, or ask a colleague to make theirs public. "
          + "Want to see what an investigation looks like first? The demo case uses made-up data.",
      actions: [
        { label: "Connect an org", onclick: "toggleConnect(true)", primary: true, role: "user" },
        { label: "Play the demo case", onclick: "startDemo()" },
      ],
    },
    row => {
      const tr = document.createElement("tr");
      if (row.head) {
        const g = row.head;
        tr.className = "acct-row" + (g.key === UNASSIGNED_KEY ? " unassigned" : "");
        tr.dataset.account = g.key;
        tr.innerHTML = `<td colspan="12">${typeof accountHeadHtml === "function"
          ? accountHeadHtml(g, { compact: true }) : escapeHtml(g.name || "Unassigned")}</td>`;
        return tr;
      }
      const [id, o] = row.org;
      const c = o.component_counts || {};
      tr.dataset.org = id;
      tr.innerHTML = `<td>${typeof pinButton === "function" ? pinButton(id) : ""}</td>
        <td><a class="link" onclick="setActiveOrg('${escapeHtml(id)}'); showView('dashboard')">${escapeHtml(id)}</a></td>
        <td>${escapeHtml(o.name)}</td><td>${envBadge(o) || "<span class='muted'>-</span>"}</td><td>${visibilityCell(id, o)}</td>
        <td>${o.owner ? escapeHtml(o.owner) : "<span class='muted'>(none)</span>"}</td>
        <td>${c.apex_classes ?? "-"}</td><td>${c.apex_triggers ?? "-"}</td>
        <td>${c.flows ?? "-"}</td><td>${c.lwc_components ?? "-"}</td>
        <td>${fmtWhen(o.last_extracted_at)}${changesHint(o)}</td>
        <td class="row-actions">${o.can_manage ? `<button class="secondary" onclick="event.stopPropagation(); refreshOrg('${escapeHtml(id)}')">Refresh</button>
          ${typeof moveOrgToAccount === "function" ? `<button class="link-btn" onclick="event.stopPropagation(); moveOrgToAccount('${escapeHtml(id)}')">Move</button>` : ""}` : ""}</td>`;
      return tr;
    });
  // A fetch someone else started should be visible here, not just in the
  // panel of whoever clicked the button -- otherwise a colleague sees an org
  // with stale counts and no clue that it is mid-refresh, and reaches for the
  // Refresh button that will now be rejected. (Drawn by markInFlightOrgs,
  // which loadOrgs calls after this.)
}

async function markInFlightOrgs() {
  for (const id of Object.keys(ORGS)) {
    const s = await apiJson(`/api/orgs/${encodeURIComponent(id)}/status`, {}, null);
    if (!s || ["done", "error", "unknown"].includes(s.status)) continue;
    const html = `<div class="muted">${escapeHtml(s.step_label || s.status)} &mdash; ${
      s.percent || 0}%</div>
      <div class="progress-track" style="height:5px; margin-top:4px;">
        <div class="progress-fill" style="width:${s.percent || 0}%"></div></div>`;
    const cell = [...document.querySelectorAll("#orgsTable tr")]
      .find(tr => tr.dataset.org === id)?.cells[10];
    if (cell) cell.innerHTML = html;
    // The same signal on the org's card, which is what most people look at.
    const slot = [...document.querySelectorAll(".org-card")]
      .find(c => c.dataset.org === id)?.querySelector(".org-card-inflight");
    if (slot) slot.innerHTML = html;
  }
}

function changesHint(o) {
  const ch = o.last_refresh_changes;
  if (!ch || ch.first_connection) return "";
  if (!ch.changed && !ch.added && !ch.removed) return `<div class="muted">no changes last refresh</div>`;
  const bits = [ch.changed ? `${ch.changed} changed` : null, ch.added ? `${ch.added} new` : null,
                ch.removed ? `${ch.removed} removed` : null].filter(Boolean);
  return `<div class="muted">${bits.join(", ")} last refresh</div>`;
}

const VIS_ICON = {
  private: '<svg viewBox="0 0 16 16" width="10" height="10" aria-hidden="true"><rect x="3" y="7" width="10" height="7" rx="1.5" fill="currentColor"/><path d="M5 7V5a3 3 0 0 1 6 0v2" fill="none" stroke="currentColor" stroke-width="1.8"/></svg>',
  public: '<svg viewBox="0 0 16 16" width="10" height="10" aria-hidden="true"><circle cx="8" cy="8" r="6.2" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M1.8 8h12.4M8 1.8c2 2 2 10.4 0 12.4M8 1.8c-2 2-2 10.4 0 12.4" fill="none" stroke="currentColor" stroke-width="1.3"/></svg>',
};

/** The private/public switch. "On" means public, because that is the state
 *  that changes something for other people -- the one worth a coloured track.
 *  A real <button role="switch">, so it is reachable by keyboard and reads
 *  as a switch to a screen reader. */
function visToggleHtml(isPublic, { onclick = "", id = "", title = "", disabled = false } = {}) {
  return `<button type="button" role="switch" class="vis-toggle${isPublic ? " on" : ""}"
      aria-checked="${isPublic}" ${id ? `id="${id}"` : ""} ${disabled ? "disabled" : ""}
      title="${escapeHtml(title)}" onclick="${onclick}">
      <span class="vis-track" aria-hidden="true"><span class="vis-knob">${isPublic ? VIS_ICON.public : VIS_ICON.private}</span></span>
      <span class="vis-label">${isPublic ? "Public" : "Private"}</span>
    </button>`;
}

// Owner/admin get a live switch to flip an org public <-> private; everyone
// else just sees the current state as a badge.
function visibilityCell(id, o) {
  const vis = o.visibility || "public";
  if (!o.can_manage) return `<span class="badge visibility-${vis}">${vis}</span>`;
  const safeId = escapeHtml(String(id).replace(/\\/g, "\\\\").replace(/'/g, "\\'"));
  return visToggleHtml(vis === "public", {
    onclick: `event.stopPropagation(); toggleOrgVisibility('${safeId}', this)`,
    title: vis === "public"
      ? "Public: everyone signed in can see this org. Click to make it private."
      : "Private: only you and admins can see this org. Click to make it public.",
  });
}

/** Paint a switch as on/off without waiting for the server. */
function setVisToggle(el, isPublic) {
  if (!el) return;
  el.classList.toggle("on", isPublic);
  el.setAttribute("aria-checked", String(isPublic));
  el.querySelector(".vis-label").textContent = isPublic ? "Public" : "Private";
  el.querySelector(".vis-knob").innerHTML = isPublic ? VIS_ICON.public : VIS_ICON.private;
}

async function toggleOrgVisibility(id, el) {
  const current = (ORGS[id] && ORGS[id].visibility) || "public";
  const next = current === "public" ? "private" : "public";
  // Going public exposes the org to every account on the server, so a
  // stray click should not do it silently. Going private narrows access and
  // needs no confirmation.
  if (next === "public" && !await confirmModal(`Make ${id} public?`,
      "Everyone signed in to this app will be able to see its knowledgebase, incidents and known issues, "
      + "and file incidents against it. Only you or an admin can still refresh it.", "Make public")) return;
  await setOrgVisibility(id, next, el);
}

async function setOrgVisibility(id, visibility, el) {
  const previous = ORGS[id] ? ORGS[id].visibility : null;
  if (el) { el.disabled = true; if (el.classList.contains("vis-toggle")) setVisToggle(el, visibility === "public"); }
  const res = await api(`/api/orgs/${encodeURIComponent(id)}/visibility`, {
    method: "PATCH", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ visibility }),
  });
  if (el) el.disabled = false;
  if (!res.ok) {
    toast("Could not change visibility: " + await errorText(res), "error");
    if (el && previous) {
      if (el.classList.contains("vis-toggle")) setVisToggle(el, previous === "public");
      else el.value = previous;
    }
    return;
  }
  toast(`${id} is now ${visibility}.`, "ok");
  await loadOrgs();
}

/** The Connect form's switch writes into the hidden #newVisibility input,
 *  which is what createOrg() reads. */
function toggleNewOrgVisibility(el) {
  const input = document.getElementById("newVisibility");
  const isPublic = input.value !== "public";
  input.value = isPublic ? "public" : "private";
  setVisToggle(el, isPublic);
  const hint = document.getElementById("newVisDesc");
  if (hint) hint.textContent = isPublic
    ? "Everyone signed in to this app can see it and file incidents against it."
    : "Only you and admins can see it.";
}

/** Re-fetch an org already on record. The only thing that can't be reused is
 *  the access token (Salesforce expires them), so that's the only thing we
 *  ask for -- previously this meant retyping the org id, name and instance
 *  URL into the "connect" form. */
async function refreshOrg(id) {
  const o = ORGS[id] || {};
  const answer = await modal({
    title: `Refresh ${id}`,
    body: `Re-fetches from <b>${escapeHtml(o.instance_url || "the stored instance URL")}</b>.
           Salesforce access tokens expire, so paste a current one. Everything else is reused,
           and only components whose content actually changed are re-indexed.`,
    fields: [{ name: "access_token", label: "Access Token", type: "password", placeholder: "00D..." }],
    submitLabel: "Refresh",
  });
  if (!answer) return;
  if (!answer.access_token.trim()) { toast("An access token is required.", "error"); return; }

  const statusEl = document.getElementById("createStatus");
  statusEl.textContent = `Refreshing ${id}...`;
  statusEl.className = "status-line";
  const res = await api(`/api/orgs/${encodeURIComponent(id)}/refresh`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ access_token: answer.access_token.trim() }),
  });
  if (!res.ok) {
    statusEl.textContent = "Refresh failed: " + await errorText(res);
    statusEl.className = "status-line error";
    return;
  }
  pollOrgStatus(id, { verb: "Refreshing" });
}

async function createOrg() {
  const org_id = document.getElementById("newOrgId").value.trim();
  const org_name = document.getElementById("newOrgName").value.trim();
  const instance_url = document.getElementById("newInstanceUrl").value.trim();
  const access_token = document.getElementById("newAccessToken").value.trim();
  const visibility = document.getElementById("newVisibility").value;
  const accountEl = document.getElementById("newAccount");
  const account = accountEl ? accountEl.value.replace(/\s+/g, " ").trim() : "";
  const statusEl = document.getElementById("createStatus");
  if (!org_id || !org_name || !instance_url || !access_token) {
    statusEl.textContent = "All fields are required."; statusEl.className = "status-line error"; return;
  }
  statusEl.textContent = "Queued..."; statusEl.className = "status-line";
  const res = await api("/api/orgs", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ org_id, org_name, instance_url, access_token, visibility, account: account || null }),
  });
  if (!res.ok) {
    statusEl.textContent = "Failed to queue: " + await errorText(res);
    statusEl.className = "status-line error"; return;
  }
  document.getElementById("newAccessToken").value = "";  // don't leave a token sitting in the DOM
  if (accountEl) { accountEl.value = ""; delete accountEl.dataset.touched; }
  track("connect");
  pollOrgStatus(org_id, { verb: "Connecting" });
}

// ---------- org fetch progress ----------
//
// A fetch of a real org runs for minutes. The old UI showed one status word
// on a single line, which for most of that time was indistinguishable from a
// hung request -- and the commonest reaction to a page that looks hung is to
// click the button again, which is exactly the wrong thing to do. So the
// fetch now gets a modal progress panel: a real percentage, the phase names
// with ticks against the finished ones, the counts as they come in, and
// elapsed time so it is visibly alive even during the long Apex phase.
//
// It is a modal deliberately. The one action that must not happen during a
// fetch is starting a second one, and covering the form is the simplest way
// to make that true.

// `hidden` is per-org and sticky: once someone sends a fetch to the
// background, the next poll must not pop the panel straight back up.
let PROGRESS = { orgId: null, timer: null, hidden: {} };

function openProgress(org_id, { verb = "Connecting" } = {}) {
  closeProgress();
  const back = document.createElement("div");
  back.className = "modal-backdrop progress-backdrop";
  back.id = "orgProgress";
  back.innerHTML = `
    <div class="modal progress-modal" role="dialog" aria-modal="true" aria-live="polite">
      <h3>${escapeHtml(verb)} ${escapeHtml(org_id)}</h3>
      <div class="progress-phase" id="pgPhase">Queued...</div>
      <div class="progress-track"><div class="progress-fill" id="pgFill" style="width:0%"></div></div>
      <div class="progress-meta">
        <span id="pgPercent">0%</span>
        <span id="pgElapsed"></span>
      </div>
      <ul class="progress-steps" id="pgSteps"></ul>
      <div class="progress-counts" id="pgCounts"></div>
      <p class="muted progress-note">This can take a few minutes on a large org. Nothing but the
        derived knowledgebase is stored &mdash; the access token is used for this fetch only.
        You can leave this open; it updates itself.</p>
      <div class="modal-actions">
        <button type="button" class="secondary" id="pgHide">Run in the background</button>
      </div>
    </div>`;
  document.body.appendChild(back);
  // Dismissing hides the panel but does NOT cancel the fetch, which keeps
  // running server-side -- so the button says what it does rather than
  // "Cancel", which would be a lie.
  document.getElementById("pgHide").onclick = () => {
    PROGRESS.hidden[org_id] = true;
    closeProgress();
    toast(`${org_id} is still being fetched. Its card on Home will update when it finishes.`, "info");
  };
  PROGRESS.orgId = org_id;
  delete PROGRESS.hidden[org_id];
}

function closeProgress() {
  const el = document.getElementById("orgProgress");
  if (el) el.remove();
  PROGRESS.orgId = null;
}

/** Paint one poll's worth of progress. Guarded on the panel still existing,
 *  because the user may have sent the fetch to the background -- polling
 *  continues either way, since the completion toast and the table refresh
 *  still have to happen. */
function renderProgress(s) {
  const fill = document.getElementById("pgFill");
  if (!fill) return;
  const pct = Math.max(0, Math.min(100, s.percent ?? 0));
  fill.style.width = `${pct}%`;
  fill.classList.toggle("indeterminate", s.status === "queued");
  document.getElementById("pgPercent").textContent = `${pct}%`;
  document.getElementById("pgPhase").textContent =
    s.step_label || String(s.status || "").replace(/_/g, " ") || "Working";

  const el = document.getElementById("pgElapsed");
  if (el) {
    el.textContent = s.elapsed_seconds != null
      ? `${Math.floor(s.elapsed_seconds / 60)}m ${String(Math.floor(s.elapsed_seconds % 60)).padStart(2, "0")}s`
      : "";
  }

  // Phase list with ticks. Showing the whole sequence, not just the current
  // phase, is what turns "still going" into "three phases left".
  const stepsHost = document.getElementById("pgSteps");
  if (stepsHost && (s.steps || []).length) {
    const current = s.step_index || 0;
    stepsHost.innerHTML = (s.steps || []).map((step, i) => {
      const n = i + 1;
      // The server now says each step's state outright; older payloads only
      // carry step_index, so fall back to deriving it.
      const state = s.status === "done" ? "done"
        : step.state || (n < current ? "done" : n === current ? "active" : "pending");
      const cls = state === "pending" ? "" : state;
      const mark = state === "done" ? "&#10003;" : state === "active" ? "&#9679;" : "&#9675;";
      // The fetch phase runs several streams at once; show each one's own
      // bar under it so "40% overall" reads as "classes 70%, flows 10%".
      const tracks = state === "active" && (s.tracks || []).length ? renderTracks(s.tracks) : "";
      return `<li class="${cls}${tracks ? " has-tracks" : ""}"><span class="progress-mark">${mark}</span>${
        escapeHtml(step.label)}${tracks}</li>`;
    }).join("");
  }

  const countsHost = document.getElementById("pgCounts");
  if (countsHost) {
    const labels = { objects: "objects", classes: "Apex classes", triggers: "triggers",
                     flows: "flows", lwc: "LWC bundles",
                     workflow_field_updates: "field updates", components: "components",
                     managed_skipped: "managed (listed, not fetched)" };
    const entries = Object.entries(s.counts || {}).filter(([, v]) => v != null);
    countsHost.innerHTML = entries.length
      ? entries.map(([k, v]) => `<span class="count-pill"><b>${v}</b> ${escapeHtml(labels[k] || k)}</span>`).join("")
      : "";
  }
}

/** Per-stream progress for the parallel fetch phase. `total` is null while a
 *  stream has not been sized yet (workflow field updates arrive in one go),
 *  which renders as a sweeping bar rather than a made-up number. */
function renderTracks(tracks) {
  return `<ul class="progress-tracks">${tracks.map(t => {
    const known = t.total != null && t.total > 0;
    const pct = t.state === "done" ? 100 : known ? Math.min(100, Math.round(100 * (t.done || 0) / t.total)) : 0;
    const figure = t.state === "done" ? "&#10003;"
      : known ? `${t.done || 0} / ${t.total}` : t.total === 0 ? "none" : "&hellip;";
    const cls = t.state === "done" ? "done" : t.state === "failed" ? "failed" : known ? "" : "indeterminate";
    return `<li class="progress-track-row ${cls}">
      <span class="progress-track-label">${escapeHtml(t.label || t.name)}</span>
      <span class="progress-track-mini"><span style="width:${pct}%"></span></span>
      <span class="progress-track-figure">${figure}</span></li>`;
  }).join("")}</ul>`;
}

async function pollOrgStatus(org_id, { verb = "Working", showPanel = true } = {}) {
  const statusEl = document.getElementById("createStatus");
  if (showPanel && PROGRESS.orgId !== org_id && !PROGRESS.hidden[org_id]) {
    openProgress(org_id, { verb });
  }

  const s = await apiJson(`/api/orgs/${encodeURIComponent(org_id)}/status`, {}, null);
  if (!s) { closeProgress(); return; }

  if (s.status === "error") {
    closeProgress();
    statusEl.textContent = "Error: " + s.detail;
    statusEl.className = "status-line error";
    // The detail can be a paragraph about proxies and instance URLs, so it
    // goes in a modal rather than a toast that vanishes in five seconds.
    modal({ title: `${org_id}: fetch failed`, body: escapeHtml(s.detail || "Unknown error."),
            fields: [], submitLabel: "Close" });
    return;
  }

  if (s.status === "done") {
    renderProgress(s);
    statusEl.textContent = summariseChanges(org_id, s.changes)
      + (s.warnings?.length ? ` (${s.warnings.length} warning(s) -- see server log)` : "");
    statusEl.className = "status-line ok";
    // Hold the completed bar on screen for a moment. Snapping it away the
    // instant it hits 100% robs the user of the confirmation they waited
    // minutes for.
    setTimeout(closeProgress, 900);
    toast(`${org_id} is up to date.`, "ok");
    await loadOrgs();
    if (org_id === CURRENT_ORG) loadDashboard();
    return;
  }

  renderProgress(s);
  statusEl.textContent = `${verb}... (${s.step_label || String(s.status).replace(/_/g, " ")})`;
  statusEl.className = "status-line";
  PROGRESS.timer = setTimeout(() => pollOrgStatus(org_id, { verb, showPanel }), 1500);
}

function summariseChanges(org_id, ch) {
  if (!ch) return "Done. Knowledgebase built.";
  if (ch.first_connection) return `Done. Indexed ${ch.total} component(s).`;
  if (!ch.changed && !ch.added && !ch.removed) {
    return `Done. Nothing changed since the last fetch (${ch.total} component(s) checked).`;
  }
  const named = (ch.changed_sample || []).slice(0, 3).map(k => k.split("/").pop()).join(", ");
  return `Done. ${ch.changed} changed, ${ch.added} new, ${ch.removed} removed`
    + (named ? ` -- e.g. ${named}` : "") + `.`;
}

// ---------- dashboard ----------

async function loadDashboard() {
  const el = document.getElementById("dashStats");
  if (!CURRENT_ORG) {
    document.getElementById("dashOrgTitle").textContent = "Org stats";
    el.innerHTML = emptyStateHtml({
      title: "No org selected",
      body: "The dashboard shows one org's knowledgebase: component counts, async jobs, integration "
          + "points and the risk rollups. Connect an org on Home, or pick one in the header.",
      actions: [{ label: "Go to Home", onclick: "showView('connections')", primary: true },
                { label: "Play the demo case", onclick: "startDemo()" }],
    });
    return;
  }
  const acct = ORGS[CURRENT_ORG] && accountDisplayName(ORGS[CURRENT_ORG].account);
  document.getElementById("dashOrgTitle").textContent = `Org stats -- ${acct ? `${acct} \u203a ` : ""}${CURRENT_ORG}`;
  setBusy(el);
  const s = await apiJson(`/api/orgs/${encodeURIComponent(CURRENT_ORG)}/stats`, {}, null);
  if (!s) { el.innerHTML = "<p class='muted'>No knowledgebase yet for this org.</p>"; return; }
  el.innerHTML = `
    <div><b>Apex classes:</b> ${s.counts.apex_classes} &nbsp; <b>Triggers:</b> ${s.counts.apex_triggers} &nbsp;
      <b>Test classes:</b> ${s.counts.test_classes}</div>
    <div><b>Flows:</b> ${s.counts.flows} &nbsp; <b>Process Builder:</b> ${s.counts.process_builder_processes ?? 0} &nbsp;
      <b>Workflow field updates:</b> ${s.counts.workflow_field_updates ?? 0} &nbsp; <b>LWC:</b> ${s.counts.lwc_components}</div>
    <div><b>Batchable / Queueable / Schedulable / @future:</b>
      ${s.async_job_classes.batchable.length} / ${s.async_job_classes.queueable.length} /
      ${s.async_job_classes.schedulable.length} / ${s.async_job_classes.future.length}</div>
    <div><b>Classes with callouts:</b> ${s.integration_points.classes_with_callouts.length}</div>
    <div><b>Flows without a fault path:</b> ${s.flows_without_fault_paths.length}</div>
    <div><b>Never-cleared static collections org-wide:</b> ${s.never_cleared_static_collections.length}</div>
    <div><b>Fields with a high-risk writer:</b> ${s.fields_with_high_risk_writes.map(f => `<span class="pill link" onclick="showFieldWriters('${escapeHtml(f)}')">${escapeHtml(f)}</span>`).join(" ") || "none"}</div>
    <div><b>Fields written by Flow/PB/Workflow automation:</b> ${(s.fields_written_by_declarative_automation || []).length}</div>
  `;
}

async function runSearch() {
  const q = document.getElementById("searchBox").value.trim();
  const el = document.getElementById("searchResults");
  if (!CURRENT_ORG) { el.innerHTML = `<p class="muted">Pick an org first.</p>`; return; }
  if (!q) { el.innerHTML = ""; return; }
  setBusy(el, "Searching...");
  const data = await apiJson(`/api/orgs/${encodeURIComponent(CURRENT_ORG)}/search?q=${encodeURIComponent(q)}`, {}, null);
  if (!data) { el.innerHTML = `<p class="muted">Search failed.</p>`; return; }
  const section = (title, items, onClick) => items.length
    ? `<p><b>${title}:</b> ${items.map(i => `<span class="pill link" onclick="${onClick}('${escapeHtml(i)}')">${escapeHtml(i)}</span>`).join(" ")}</p>`
    : "";
  const any = data.components.length || data.objects.length || data.fields.length;
  track("search");
  // Results REPLACE the previous ones -- the old version appended forever,
  // so a few searches left a wall of stale JSON.
  el.innerHTML = any
    ? section("Components", data.components, "showComponent")
      + section("Objects touched", data.objects, "showObjectTouch")
      + section("Fields", data.fields, "showFieldWriters")
      + `<div id="searchDetail"></div>`
    : `<p class="muted">Nothing matching "${escapeHtml(q)}" in customer-authored components.</p>`;
}

async function showComponent(id) {
  const card = await apiJson(`/api/orgs/${encodeURIComponent(CURRENT_ORG)}/components/${encodeURIComponent(id)}`, {}, null);
  const host = document.getElementById("searchDetail") || document.getElementById("searchResults");
  host.innerHTML = card ? renderComponentCard(id, card) : `<p class="muted">No card for ${escapeHtml(id)}.</p>`;
}

async function showObjectTouch(name) {
  const data = await apiJson(`/api/orgs/${encodeURIComponent(CURRENT_ORG)}/object-touch/${encodeURIComponent(name)}`, {}, {});
  const host = document.getElementById("searchDetail") || document.getElementById("searchResults");
  const groups = Object.entries(data || {});
  host.innerHTML = groups.length
    ? `<div class="detail-block"><h3>Everything that touches ${escapeHtml(name)}</h3>` +
      groups.map(([k, v]) => `<p><b>${escapeHtml(k)}</b> (${(v || []).length}): ` +
        (v || []).map(i => `<span class="pill link" onclick="showComponent('${escapeHtml(typeof i === "string" ? i : i.component || "")}')">${escapeHtml(typeof i === "string" ? i : i.component || JSON.stringify(i))}</span>`).join(" ") +
        `</p>`).join("") + collapsibleJson("Raw JSON", data) + `</div>`
    : `<p class="muted">Nothing in the knowledgebase touches ${escapeHtml(name)}.</p>`;
}

function showFieldWriters(name) {
  showView("dashboard");
  document.getElementById("fieldWriterBox").value = name;
  findFieldWriters();
}

async function findFieldWriters() {
  const field = document.getElementById("fieldWriterBox").value.trim();
  const el = document.getElementById("fieldWriterResults");
  if (!CURRENT_ORG) { el.innerHTML = `<p class="muted">Pick an org first.</p>`; return; }
  if (!field) { el.innerHTML = ""; return; }
  setBusy(el, "Looking up writers...");
  const data = await apiJson(`/api/orgs/${encodeURIComponent(CURRENT_ORG)}/field-writers/${encodeURIComponent(field)}`, {}, null);
  el.innerHTML = renderFieldWriters(data, field);
  if (data) track("writers");
}

// ---------- incidents ----------

async function fileIncident() {
  if (!CURRENT_ORG) { toast("Pick an org first.", "error"); return; }
  const statusEl = document.getElementById("incidentStatus");
  const label = document.getElementById("incLabel").value.trim();
  const field = document.getElementById("incField").value.trim();
  const fileInput = document.getElementById("incLogFile");
  if (!field && !fileInput.files.length) {
    statusEl.textContent = "Provide a log file, a field name, or both."; statusEl.className = "status-line error"; return;
  }
  const form = new FormData();
  if (label) form.append("label", label);
  if (field) form.append("field", field);
  if (fileInput.files.length) form.append("log_file", fileInput.files[0]);

  statusEl.textContent = "Filing..."; statusEl.className = "status-line";
  const res = await api(`/api/orgs/${encodeURIComponent(CURRENT_ORG)}/incidents`, { method: "POST", body: form });
  if (!res.ok) {
    statusEl.textContent = "Failed: " + await errorText(res); statusEl.className = "status-line error"; return;
  }
  const data = await res.json();
  const m = data.meta;
  track("incident");
  statusEl.textContent = m.recurrence
    ? `RECURRENCE -- seen ${m.prior_occurrences} time(s) before.`
    : (m.signature ? "NEW ISSUE filed." : "Filed (no signature -- no exception and no field given).");
  statusEl.className = "status-line ok";
  if (m.recurrence && m.prior_resolution) toast("This one has a resolution on file -- see the report below.", "ok", 8000);
  await loadIncidents();
  showIncidentDetail(m.incident_id);   // go straight to the report
}

async function loadIncidents() {
  if (!CURRENT_ORG) return;
  const tbody = document.getElementById("incidentsTable");
  const incidents = await apiJson(`/api/orgs/${encodeURIComponent(CURRENT_ORG)}/incidents`, {}, []) || [];
  fillTable(tbody, incidents, 4, {
    title: `No incidents filed for ${CURRENT_ORG} yet`,
    body: "An incident is a debug log, a suspect field, or both, checked against this org's knowledgebase. "
        + "The report ranks the likely culprits, and every one you file teaches Known Issues -- so the "
        + "second time the same failure appears, the answer is already there.",
    actions: [{ label: "File the first one", onclick: "document.getElementById('incLabel').focus()", primary: true, role: "user" },
              { label: "See an example", onclick: "startDemo(4)" }],
  }, m => {
    const tr = document.createElement("tr");
    tr.onclick = () => showIncidentDetail(m.incident_id);
    const badge = m.recurrence ? `<span class="badge recurrence">RECURRENCE</span>` : `<span class="badge new">NEW</span>`;
    tr.innerHTML = `<td>${fmtWhen(m.timestamp)}</td><td>${escapeHtml(m.incident_id)}</td>
      <td>${badge}</td><td>${escapeHtml(m.suspect_field || "")}</td>`;
    return tr;
  });
}

/** The RCA report. This used to be `<pre>${JSON.stringify(pack)}</pre>` --
 *  technically complete and practically unreadable. */
async function showIncidentDetail(incidentId) {
  const card = document.getElementById("incidentDetailCard");
  card.style.display = "block";
  setBusy("incidentDetail", "Building the report...");
  card.scrollIntoView({ behavior: "smooth" });

  const data = await apiJson(
    `/api/orgs/${encodeURIComponent(CURRENT_ORG)}/incidents/${encodeURIComponent(incidentId)}`, {}, null);
  if (!data) {
    document.getElementById("incidentDetail").innerHTML = `<p class="muted">Could not load that incident.</p>`;
    return;
  }
  const m = data.meta, pack = data.rca_context_pack || {};
  const n = pack.normalized_log || data.normalized_log || {};
  const parts = [];

  // Context handoff: the engineer is already looking at this incident, so the
  // question should not have to restate it. Pre-fills the composer rather than
  // sending, so they can edit first.
  if (typeof askAbout === "function") {
    parts.push(`<div class="ask-about-bar">
      <button class="secondary" onclick="askAbout(${JSON.stringify(
        `Walk me through incident ${m.incident_id}. What is the most likely root cause, and what should I check first?`
      ).replace(/"/g, "&quot;")})">Ask about this incident</button>
    </div>`);
  }

  // --- verdict banner: the first thing worth knowing ---
  if (m.recurrence) {
    parts.push(`<div class="banner recurrence">
      <div class="banner-title">Seen before -- ${m.prior_occurrences} prior occurrence(s)</div>
      ${m.prior_resolution
        ? `<div class="banner-body"><b>Resolution on file:</b> ${escapeHtml(m.prior_resolution)}</div>`
        : `<div class="banner-body">No resolution recorded yet. When you fix it, write it down below --
           that's what makes the next occurrence a two-minute job.</div>`}
      ${(m.prior_incident_ids || []).length
        ? `<div class="banner-body muted">Earlier: ${m.prior_incident_ids.slice(-5).map(escapeHtml).join(", ")}</div>` : ""}
    </div>`);
  } else if (m.signature) {
    parts.push(`<div class="banner new">
      <div class="banner-title">New issue</div>
      <div class="banner-body">First time this signature has been filed for ${escapeHtml(m.org_id)}.</div>
    </div>`);
  }

  parts.push(`<div class="meta-line muted">${escapeHtml(m.incident_id)} &middot; filed ${fmtWhen(m.timestamp)}
    ${m.source_log ? `&middot; from ${escapeHtml(m.source_log)}` : ""}
    ${m.signature ? `&middot; signature <span class="mono">${escapeHtml(m.signature)}</span>
      (${escapeHtml(m.signature_source || "")})` : ""}</div>`);

  // --- what failed ---
  parts.push(`<div class="section">${renderNormalizedLog(n)}</div>`);

  // --- suspect field ---
  if (m.suspect_field) {
    const writers = (pack.suspect_field_writers || []).map(w => ({ ...w }));
    parts.push(`<div class="section"><h3>Who writes ${escapeHtml(m.suspect_field)}</h3>
      ${renderFieldWriters({ writers }, m.suspect_field)}</div>`);
  }

  // --- prime suspects ---
  const suspects = rankSuspects(pack);
  if (suspects.length) {
    const shown = suspects.slice(0, 8);
    parts.push(`<div class="section"><h3>Prime suspects <span class="muted">(${suspects.length} component(s) in scope)</span></h3>
      <p class="muted">Components named in the log, plus one call-graph hop either side, ranked by how
        likely they are to be the cause.</p>
      ${shown.map((s, i) => suspectRow(s, i + 1)).join("")}
      ${suspects.length > shown.length
        ? `<details><summary>${suspects.length - shown.length} more in scope</summary>
           ${suspects.slice(8).map((s, i) => suspectRow(s, i + 9)).join("")}</details>` : ""}
    </div>`);
  }

  // --- recently changed ---
  const recent = pack.recently_changed_components || [];
  if (recent.length) {
    parts.push(`<div class="section"><h3>Changed in the last 14 days</h3>
      <p class="muted">A component that changed just before an incident started is the highest-value thing to read first.</p>
      <div>${recent.map(r => `<span class="pill link" onclick="showComponent('${escapeHtml(r.id)}')">
        ${escapeHtml(r.id)} <b>${r.age_days}d</b></span>`).join(" ")}</div></div>`);
  }

  // --- same-object neighbours ---
  const related = Object.entries(pack.related_by_object || {}).filter(([, v]) => v && Object.keys(v).length);
  if (related.length) {
    parts.push(`<div class="section"><h3>Other automation on the same objects</h3>
      <p class="muted">Not in the log, but touches the same object(s) -- the usual source of order-of-execution surprises.</p>
      ${related.map(([obj, v]) => `<details><summary>${escapeHtml(obj)}</summary>
        ${Object.entries(v).map(([k, items]) => `<p><b>${escapeHtml(k)}:</b> ` +
          (items || []).map(i => `<span class="pill">${escapeHtml(typeof i === "string" ? i : i.component || JSON.stringify(i))}</span>`).join(" ") + `</p>`).join("")}
        </details>`).join("")}</div>`);
  }

  // --- record the fix ---
  if (m.signature) {
    parts.push(`<div class="section" data-requires="user">
      <h3>Record the resolution</h3>
      <p class="muted">Saved against signature <span class="mono">${escapeHtml(m.signature)}</span>, so the next
        person who hits this sees your fix immediately.</p>
      <textarea id="resolutionText" rows="3" placeholder="What was actually wrong, and what fixed it?">${escapeHtml(m.prior_resolution || "")}</textarea>
      <button class="secondary" onclick="recordResolution('${escapeHtml(m.signature)}')">Save resolution</button>
      <div id="resolveStatus" class="status-line"></div>
    </div>`);
  }

  parts.push(collapsibleJson("Full RCA context pack (JSON) -- this is what the MCP tools hand to Claude", pack));

  document.getElementById("incidentDetail").innerHTML = parts.join("");
  applyRole();  // the resolution box is a write action
}

async function recordResolution(signature) {
  const resolution = document.getElementById("resolutionText").value.trim();
  const statusEl = document.getElementById("resolveStatus");
  if (!resolution) { statusEl.textContent = "Enter a resolution first."; statusEl.className = "status-line error"; return; }
  const res = await api(`/api/orgs/${encodeURIComponent(CURRENT_ORG)}/resolve`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ signature, resolution }),
  });
  statusEl.textContent = res.ok ? "Resolution saved." : "Failed: " + await errorText(res);
  statusEl.className = res.ok ? "status-line ok" : "status-line error";
  if (res.ok) { toast("Resolution saved to the known-issues library.", "ok"); track("fix"); loadKnownIssues(); }
}

// ---------- known issues ----------

let KNOWN_ISSUES = {};

async function loadKnownIssues() {
  const host = document.getElementById("knownList");
  if (!CURRENT_ORG) { host.innerHTML = `<p class="muted">Pick an org first.</p>`; return; }
  setBusy(host);
  KNOWN_ISSUES = await apiJson(`/api/orgs/${encodeURIComponent(CURRENT_ORG)}/known-issues`, {}, {}) || {};
  document.getElementById("knownOrgTitle").textContent = `Known issues -- ${CURRENT_ORG}`;
  renderKnownIssues();
}

function renderKnownIssues() {
  const host = document.getElementById("knownList");
  const q = document.getElementById("knownFilter").value.trim().toLowerCase();
  const onlyUnresolved = document.getElementById("knownUnresolved").checked;

  let entries = Object.entries(KNOWN_ISSUES);
  const total = entries.length;
  const unresolved = entries.filter(([, v]) => !v.resolution).length;
  document.getElementById("knownSummary").innerHTML = total
    ? `<b>${total}</b> distinct issue(s) &middot; <b>${total - unresolved}</b> with a recorded fix &middot;
       <b>${unresolved}</b> still undocumented`
    : "";

  if (onlyUnresolved) entries = entries.filter(([, v]) => !v.resolution);
  if (q) {
    entries = entries.filter(([sig, v]) =>
      sig.toLowerCase().includes(q) ||
      (v.type || "").toLowerCase().includes(q) ||
      (v.message_sample || "").toLowerCase().includes(q) ||
      (v.field || "").toLowerCase().includes(q) ||
      (v.resolution || "").toLowerCase().includes(q));
  }
  entries.sort((a, b) => (b[1].last_seen || "").localeCompare(a[1].last_seen || "")); // most recent first

  if (!entries.length) {
    host.innerHTML = total
      ? `<p class="muted">No known issue matches that filter.</p>`
      : emptyStateHtml({
          title: "No known issues for this org yet",
          body: "Every incident filed with an exception or a suspect field adds its signature here, and "
              + "any fix someone records shows up alongside it. It is the org's memory: the more incidents "
              + "are filed, the more often the answer is already on file.",
          actions: [{ label: "File an incident", onclick: "showView('incidents')", primary: true, role: "user" },
                    { label: "See how it pays off", onclick: "startDemo(8)" }],
        });
    return;
  }

  host.innerHTML = entries.map(([sig, v]) => `
    <div class="known ${v.resolution ? "resolved" : "unresolved"}">
      <div class="known-head">
        <span class="badge ${v.resolution ? "low" : "medium"}">${v.resolution ? "RESOLVED" : "NO FIX ON FILE"}</span>
        <b>${escapeHtml(v.kind === "field_report" ? `Field report: ${v.field}` : (v.type || "Exception"))}</b>
        <span class="badge recurrence">${v.occurrences}&times;</span>
        <span class="muted mono">${escapeHtml(sig)}</span>
      </div>
      ${v.message_sample ? `<div class="known-msg">${escapeHtml(v.message_sample)}</div>` : ""}
      <div class="muted">first seen ${fmtWhen(v.first_seen)} &middot; last seen ${fmtWhen(v.last_seen)}
        ${(v.incident_ids || []).length ? `&middot; ${v.incident_ids.length} incident(s)` : ""}</div>
      ${v.resolution
        ? `<div class="known-res"><b>Fix:</b> ${escapeHtml(v.resolution)}
             ${v.resolution_recorded_at ? `<span class="muted"> -- recorded ${fmtWhen(v.resolution_recorded_at)}</span>` : ""}</div>`
        : ""}
      <div class="known-actions" data-requires="user">
        <button class="secondary" onclick="editKnownResolution('${escapeHtml(sig)}')">
          ${v.resolution ? "Edit fix" : "Record a fix"}</button>
        ${(v.incident_ids || []).length
          ? `<button class="secondary" onclick="showView('incidents'); showIncidentDetail('${escapeHtml(v.incident_ids[v.incident_ids.length - 1])}')">
             Latest incident</button>` : ""}
      </div>
      ${(v.stack_sample || []).length ? `<details><summary>Stack sample</summary>
        <pre>${escapeHtml(v.stack_sample.join("\n"))}</pre></details>` : ""}
    </div>`).join("");
  applyRole();
}

async function editKnownResolution(signature) {
  const current = (KNOWN_ISSUES[signature] || {}).resolution || "";
  const answer = await modal({
    title: "Record the fix",
    body: `For signature <span class="mono">${escapeHtml(signature)}</span>. Everyone who hits this issue
           in ${escapeHtml(CURRENT_ORG)} sees what you write here.`,
    fields: [{ name: "resolution", label: "What was wrong, and what fixed it?", value: current }],
    submitLabel: "Save",
  });
  if (!answer) return;
  if (!answer.resolution.trim()) { toast("Nothing entered -- not saved.", "error"); return; }
  const res = await api(`/api/orgs/${encodeURIComponent(CURRENT_ORG)}/resolve`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ signature, resolution: answer.resolution.trim() }),
  });
  if (!res.ok) { toast("Could not save: " + await errorText(res), "error"); return; }
  toast("Fix recorded.", "ok");
  track("fix");
  loadKnownIssues();
}

// ---------- log normalizer + log library ----------
//
// Upload side: one file (dropped or chosen), an optional label, and optional
// org / account tags that are saved with the log when it is stored. The file
// is kept in the browser after a normalize-only run so "Save to library"
// can store it afterwards without choosing it again -- the server still
// never sees anything but a fresh upload it normalizes and discards.
//
// Library side: every log the caller can see is fetched once (status=all)
// and filtered here, so search and the account/org/owner/status filters are
// instant. The server applies the same visibility rules as the org list
// (app/log_library.py): a log tagged to a private org you cannot see never
// reaches this page at all.

let CURRENT_NORMALIZED = null;
let CURRENT_NORMALIZED_NAME = "normalized_log";

const LOGLIB = {
  logs: [],
  filter: { q: "", org_id: "", account: null, owner: "", status: "active" },
  selected: new Set(),
  openId: null,
  file: null,
  storeTouched: false,
  autoAccount: null,     // account value the org picker filled in, so it can be taken back out
  formReady: false,
  timer: null,
};

function fmtBytes(n) {
  if (n >= 1048576) return `${(n / 1048576).toFixed(1)} MB`;
  if (n >= 1024) return `${Math.round(n / 1024)} KB`;
  return `${n} B`;
}

/** A string as a JS literal that is safe inside a double-quoted HTML attribute. */
function jsStr(s) { return escapeHtml(JSON.stringify(String(s ?? ""))); }

// ---- upload form ----

function wireLogDrop() {
  const zone = document.getElementById("logDrop");
  const input = document.getElementById("logFile");
  if (!zone || !input || zone.dataset.wired) return;
  zone.dataset.wired = "1";
  zone.addEventListener("click", () => input.click());
  zone.addEventListener("keydown", e => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); }
  });
  zone.addEventListener("dragover", e => { e.preventDefault(); zone.classList.add("drag"); });
  zone.addEventListener("dragleave", e => { if (!zone.contains(e.relatedTarget)) zone.classList.remove("drag"); });
  zone.addEventListener("drop", e => {
    e.preventDefault(); zone.classList.remove("drag");
    const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
    if (f) setLogFile(f);
  });
  input.addEventListener("change", () => { if (input.files.length) setLogFile(input.files[0]); });
  const store = document.getElementById("logStore");
  if (store) store.addEventListener("change", () => { LOGLIB.storeTouched = true; renderLogTagHint(); });
}

function setLogFile(file) {
  LOGLIB.file = file;
  document.getElementById("logDropEmpty").style.display = file ? "none" : "";
  document.getElementById("logDropFile").style.display = file ? "" : "none";
  document.getElementById("logDrop").classList.toggle("has-file", !!file);
  if (file) {
    document.getElementById("logFileName").textContent = file.name;
    document.getElementById("logFileSize").textContent = fmtBytes(file.size);
    document.getElementById("logLabel").placeholder = file.name.replace(/\.[^.]+$/, "") || "e.g. Quote sync NPE";
  } else {
    document.getElementById("logFile").value = "";
    document.getElementById("logLabel").placeholder = "e.g. Quote sync NPE after upgrade";
  }
  document.getElementById("logNormalizeBtn").disabled = !file;
}

function clearLogFile() { setLogFile(null); }

/** Account names from orgs AND from logs already in the library. */
function logAccountNames() {
  const out = new Map();
  accountNames().forEach(n => out.set(accountKey(n), n));
  LOGLIB.logs.forEach(m => { if (m.account && !out.has(accountKey(m.account))) out.set(accountKey(m.account), m.account); });
  return [...out.values()].sort((a, b) => a.localeCompare(b));
}

/** Visible orgs as <option>s, grouped under their account. */
function orgOptionsHtml(selected, { blank = "No org" } = {}) {
  const groups = new Map();
  Object.entries(ORGS).forEach(([id, o]) => {
    const key = accountKey(o.account);
    if (!groups.has(key)) groups.set(key, { name: accountDisplayName(o.account), ids: [] });
    groups.get(key).ids.push(id);
  });
  const sorted = [...groups.values()].sort((a, b) =>
    (a.name === null) - (b.name === null) || String(a.name).localeCompare(String(b.name)));
  const opt = id => {
    const o = ORGS[id] || {};
    const env = ENV_META[orgEnv(o)];
    const suffix = env && env.short ? ` · ${env.label.replace(" org", "")}` : "";
    return `<option value="${escapeHtml(id)}" ${id === selected ? "selected" : ""}>${escapeHtml(id)}${
      o.name && o.name !== id ? ` — ${escapeHtml(o.name)}` : ""}${escapeHtml(suffix)}</option>`;
  };
  return `<option value="">${escapeHtml(blank)}</option>` + sorted.map(g =>
    `<optgroup label="${escapeHtml(g.name || "Unassigned")}">${g.ids.sort().map(opt).join("")}</optgroup>`).join("");
}

function initLogForm() {
  const sel = document.getElementById("logOrg");
  if (!sel) return;
  // First visit defaults the org to the one being worked on; after that the
  // person's own choice sticks across tab switches.
  const current = LOGLIB.formReady ? sel.value : (CURRENT_ORG && ORGS[CURRENT_ORG] ? CURRENT_ORG : "");
  sel.innerHTML = orgOptionsHtml(ORGS[current] ? current : "");
  document.getElementById("logAccountList").innerHTML =
    logAccountNames().map(n => `<option value="${escapeHtml(n)}"></option>`).join("");
  LOGLIB.formReady = true;
  logOrgChanged({ quiet: true });
}

function logOrgChanged({ quiet = false } = {}) {
  const orgId = document.getElementById("logOrg").value;
  const acctEl = document.getElementById("logAccount");
  const orgAccount = orgId && ORGS[orgId] ? accountDisplayName(ORGS[orgId].account) : null;
  if (orgAccount) {
    acctEl.value = orgAccount;
    acctEl.disabled = true;
    LOGLIB.autoAccount = orgAccount;
  } else {
    if (LOGLIB.autoAccount !== null && acctEl.value === LOGLIB.autoAccount) acctEl.value = "";
    acctEl.disabled = false;
    LOGLIB.autoAccount = null;
  }
  if (!quiet) logTagEdited();
  renderLogTagHint();
}

function logTagEdited() {
  // Tagging a log only means something if it is kept, so choosing a tag
  // ticks "Store" -- unless the person has already made that choice.
  const tagged = document.getElementById("logOrg").value || document.getElementById("logAccount").value.trim();
  const store = document.getElementById("logStore");
  if (!LOGLIB.storeTouched && tagged) store.checked = true;
  renderLogTagHint();
}

function renderLogTagHint() {
  const hint = document.getElementById("logTagHint");
  if (!hint) return;
  const orgId = document.getElementById("logOrg").value;
  const acct = document.getElementById("logAccount").value.trim();
  const store = document.getElementById("logStore").checked;
  const o = ORGS[orgId];
  const bits = [];
  if (o && o.account) bits.push(`The account comes from the org &mdash; <b>${escapeHtml(orgId)}</b> is under <b>${escapeHtml(accountDisplayName(o.account))}</b>.`);
  else if (o && acct) bits.push(`<b>${escapeHtml(orgId)}</b> has no account yet; the log will be tagged <b>${escapeHtml(acct)}</b>.`);
  if (o && o.visibility === "private") bits.push("The org is private, so this log will be visible only to people who can see the org.");
  if ((orgId || acct) && !store) bits.push("Tags are saved only when the log is stored.");
  hint.innerHTML = bits.join(" ");
}

function logTagFormData(form) {
  const orgId = document.getElementById("logOrg").value;
  const acct = document.getElementById("logAccount").value.trim();
  if (orgId) form.append("org_id", orgId);
  if (acct) form.append("account", acct);
}

async function normalizeLog({ forceStore = false } = {}) {
  const fileInput = document.getElementById("logFile");
  const statusEl = document.getElementById("logStatus");
  const file = LOGLIB.file || (fileInput.files && fileInput.files[0]);
  if (!file) {
    statusEl.textContent = "Choose a debug log file first."; statusEl.className = "status-line error"; return;
  }
  const label = document.getElementById("logLabel").value.trim();
  const store = forceStore || document.getElementById("logStore").checked;
  const form = new FormData();
  form.append("log_file", file);
  if (label) form.append("label", label);
  form.append("store", store ? "true" : "false");
  if (store) logTagFormData(form);

  const btn = document.getElementById("logNormalizeBtn");
  btn.disabled = true;
  statusEl.textContent = `${store ? "Normalizing and storing" : "Normalizing"} ${fmtBytes(file.size)}...`;
  statusEl.className = "status-line";
  if (!forceStore) setBusy("logResult", "Parsing the log...");
  let res;
  try {
    res = await api("/api/logs/normalize", { method: "POST", body: form });
  } finally {
    btn.disabled = !LOGLIB.file;
  }
  if (!res.ok) {
    statusEl.textContent = "Failed: " + await errorText(res);
    statusEl.className = "status-line error";
    if (!forceStore) document.getElementById("logResult").innerHTML = "";
    return;
  }
  const data = await res.json();

  CURRENT_NORMALIZED = data.normalized_log;
  track("normalize");
  CURRENT_NORMALIZED_NAME = data.log_id || label || (file.name.replace(/\.[^.]+$/, "")) || "normalized_log";
  const n = data.normalized_log;
  const excCount = (n.exceptions || []).length;
  const m = data.meta || {};
  const whereText = [m.account, m.org_id].filter(Boolean).join(" · ");
  statusEl.innerHTML = (data.stored
      ? `Stored as <span class="mono">${escapeHtml(data.log_id)}</span>${whereText ? ` under ${escapeHtml(whereText)}` : ""}. `
      : "Normalized. ")
    + `${excCount} exception(s), ${(n.execution_units || []).length} execution unit(s).`
    + (data.stored ? "" : " Nothing was stored.");
  statusEl.className = "status-line ok";
  const wantedTags = store && (document.getElementById("logOrg").value || document.getElementById("logAccount").value.trim());
  if (data.stored && (!("owner" in m) || (wantedTags && !m.org_id && !m.account))) {
    // An older server stores the log but drops owner/org/account without an error.
    statusEl.innerHTML += ` <span class="warn-text">The server ignored the owner and tags because it is running
      older code &mdash; restart the server, then use <b>Edit</b> on this log to tag it.</span>`;
    statusEl.className = "status-line";
  }
  // Only one rendering of a log on this page at a time -- a stale detail card
  // left open below made the page look like it had two of every section.
  if (!forceStore) {
    closeLogDetail();
    document.getElementById("logResult").innerHTML =
      renderNormalizedLog(n) + collapsibleJson("Normalized JSON", n);
  }
  renderUploadActions(data);
  if (data.stored) {
    toast(`Stored in the library${whereText ? " under " + whereText : ""}.`, "ok");
    await loadLogs({ highlight: data.log_id });
  }
}

function renderUploadActions(data) {
  const host = document.getElementById("logResultActions");
  host.style.display = "flex";
  const n = data.normalized_log || {};
  const exc = (n.exceptions || [])[0];
  const q = data.stored
    ? `Analyze stored normalized log ${data.log_id} (use get_normalized_log) and give me the most likely root cause and a suggested fix.`
    : exc ? `A debug log fails with ${exc.type}: ${exc.message}. What is the most likely root cause?` : "";
  host.innerHTML = `
    <button type="button" class="secondary" onclick="downloadCurrentNormalized()">Download normalized JSON</button>
    ${data.stored
      ? `<button type="button" class="secondary" onclick="showLogDetail(${jsStr(data.log_id)})">Open in library</button>`
      : `<button type="button" class="secondary" onclick="normalizeLog({ forceStore: true })" title="Stores it with the label and tags above">Save to library</button>`}
    ${q && typeof askAbout === "function" ? `<button type="button" class="secondary" onclick="askAbout(${jsStr(q)})">Ask the assistant</button>` : ""}`;
}

function downloadBlob(obj, filename) {
  const blob = new Blob([JSON.stringify(obj, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = filename; a.click();
  URL.revokeObjectURL(url);
}

function downloadCurrentNormalized() {
  if (CURRENT_NORMALIZED) downloadBlob(CURRENT_NORMALIZED, `${CURRENT_NORMALIZED_NAME}.normalized.json`);
}

// ---- library ----

async function loadLogs({ highlight = null } = {}) {
  wireLogDrop();
  const tbody = document.getElementById("logsTable");
  if (!LOGLIB.logs.length) tbody.innerHTML = `<tr class="empty-row"><td colspan="8"><p class="muted loading">Loading the library...</p></td></tr>`;
  const logs = await apiJson("/api/logs?status=all", {}, null);
  LOGLIB.logs = Array.isArray(logs) ? logs : [];
  const ids = new Set(LOGLIB.logs.map(m => m.log_id));
  [...LOGLIB.selected].forEach(id => { if (!ids.has(id)) LOGLIB.selected.delete(id); });
  initLogForm();
  renderLogLibrary();
  if (highlight) flashLogRow(highlight);
}

function scheduleLogFilter() {
  clearTimeout(LOGLIB.timer);
  LOGLIB.timer = setTimeout(() => {
    LOGLIB.filter.q = document.getElementById("logSearch").value;
    renderLogLibrary();
  }, 120);
}

function setLogFilter(key, value) {
  LOGLIB.filter[key] = value;
  renderLogLibrary();
}

function clearLogFilters() {
  LOGLIB.filter = { q: "", org_id: "", account: null, owner: "", status: "active" };
  document.getElementById("logSearch").value = "";
  renderLogLibrary();
}

function logHaystack(m) {
  return [m.label, m.log_id, m.source_log, m.org_id, m.org_name, m.account, m.owner, m.top_exception,
          ...(m.involved_components || [])].filter(Boolean).join(" ").toLowerCase();
}

function logPassesBase(m) {
  const f = LOGLIB.filter;
  if (f.status === "active" && m.archived) return false;
  if (f.status === "archived" && !m.archived) return false;
  if (f.owner === "me" && m.owner !== currentUsername()) return false;
  const terms = f.q.toLowerCase().split(/\s+/).filter(Boolean);
  if (terms.length) { const h = logHaystack(m); if (!terms.every(t => h.includes(t))) return false; }
  return true;
}

function filteredLogs() {
  const f = LOGLIB.filter;
  return LOGLIB.logs.filter(m => logPassesBase(m)
    && (f.account === null || accountKey(m.account) === f.account)
    && (!f.org_id || m.org_id === f.org_id));
}

function renderLogLibrary() {
  const f = LOGLIB.filter;
  const on = (id, cond) => { const el = document.getElementById(id); if (el) el.classList.toggle("on", cond); };
  on("logOwnerAll", f.owner !== "me"); on("logOwnerMe", f.owner === "me");
  on("logStatusActive", f.status === "active"); on("logStatusArchived", f.status === "archived"); on("logStatusAll", f.status === "all");

  // Account chips: counted over everything the other filters let through,
  // so each number answers "how many would I see if I clicked this".
  const base = LOGLIB.logs.filter(m => logPassesBase(m) && (!f.org_id || m.org_id === f.org_id));
  const counts = new Map();
  base.forEach(m => {
    const k = accountKey(m.account);
    if (!counts.has(k)) counts.set(k, { name: m.account || null, n: 0 });
    counts.get(k).n += 1;
  });
  const chips = [...counts.entries()].sort(([ka, a], [kb, b]) =>
    (ka === UNASSIGNED_KEY) - (kb === UNASSIGNED_KEY) || String(a.name).localeCompare(String(b.name)));
  if (f.account !== null && !counts.has(f.account)) {
    const prior = LOGLIB.logs.find(m => accountKey(m.account) === f.account);
    chips.push([f.account, { name: f.account === UNASSIGNED_KEY ? null : (prior && prior.account) || f.account, n: 0 }]);
  }
  const chipHost = document.getElementById("logAccountChips");
  chipHost.innerHTML = LOGLIB.logs.length ? `<span class="log-chips-label">Account</span>`
    + `<button type="button" class="chip ${f.account === null ? "acc" : ""}" onclick="setLogFilter('account', null)">All <span class="chip-n">${base.length}</span></button>`
    + chips.map(([k, g]) => `<button type="button" class="chip ${f.account === k ? "acc" : ""} ${k === UNASSIGNED_KEY ? "unassigned" : ""}"
        onclick="setLogFilter('account', ${jsStr(k)})">${escapeHtml(g.name || "Unassigned")} <span class="chip-n">${g.n}</span></button>`).join("")
    : "";

  // Org filter: orgs that appear on a visible log, within the chosen account.
  const orgSel = document.getElementById("logFilterOrg");
  const orgCounts = new Map();
  LOGLIB.logs.filter(m => logPassesBase(m) && (f.account === null || accountKey(m.account) === f.account))
    .forEach(m => { if (m.org_id) orgCounts.set(m.org_id, (orgCounts.get(m.org_id) || 0) + 1); });
  if (f.org_id && !orgCounts.has(f.org_id)) orgCounts.set(f.org_id, 0);
  orgSel.innerHTML = `<option value="">All orgs</option>` + [...orgCounts.entries()].sort(([a], [b]) => a.localeCompare(b))
    .map(([id, n]) => `<option value="${escapeHtml(id)}" ${id === f.org_id ? "selected" : ""}>${escapeHtml(id)} (${n})</option>`).join("");
  orgSel.style.display = orgCounts.size ? "" : "none";

  const rows = filteredLogs();
  // A selection only covers what is on screen: archiving "3 selected" must
  // never touch a log the current filter is hiding.
  const onScreen = new Set(rows.map(m => m.log_id));
  [...LOGLIB.selected].forEach(id => { if (!onScreen.has(id)) LOGLIB.selected.delete(id); });
  const total = LOGLIB.logs.filter(m => f.status === "all" || (f.status === "archived") === !!m.archived).length;
  document.getElementById("logCount").textContent = LOGLIB.logs.length
    ? (rows.length === total ? `${total}` : `${rows.length} of ${total}`) : "";

  const anyManage = LOGLIB.logs.some(m => m.can_manage);
  document.getElementById("logLibraryCard").classList.toggle("no-select", !anyManage);

  const tbody = document.getElementById("logsTable");
  const filtered = f.q || f.account !== null || f.org_id || f.owner || f.status !== "active";
  const archivedHit = f.status === "active" && LOGLIB.logs.some(m => m.archived && logHaystack(m).includes(f.q.toLowerCase()));
  const empty = !LOGLIB.logs.length ? {
    title: "No stored logs yet",
    body: "Normalizing works without keeping anything. Tick <b>Store in the library</b> when a log is worth coming "
        + "back to, and tag it with the org or account it came from &mdash; only the normalized JSON is kept, never the raw log.",
    actions: [{ label: "Normalize a log", onclick: "document.getElementById('logFile').click()", primary: true, role: "user" }],
  } : {
    title: f.q ? `No logs match “${f.q}”` : "No logs match these filters",
    body: archivedHit ? "Some archived logs match &mdash; switch to <b>Archived</b> or <b>All</b> to see them."
                      : "Try another account, org or search term.",
    actions: filtered ? [{ label: "Clear filters", onclick: "clearLogFilters()", primary: true }] : [],
  };
  fillTable(tbody, rows, 8, empty, logRow);
  renderLogBulkBar(rows);
}

function ownerHtml(m) {
  if (!m.owner) return `<span class="muted" title="Stored before owners were tracked -- only an admin can change it">&mdash;</span>`;
  const me = m.owner === currentUsername();
  return `<span class="owner-tag ${me ? "me" : ""}" title="Stored by ${escapeHtml(m.owner)}">${me ? "You" : escapeHtml(m.owner)}</span>`;
}

function logWhereHtml(m) {
  const acct = m.account
    ? `<button type="button" class="link-btn plain" onclick="event.stopPropagation(); setLogFilter('account', ${jsStr(accountKey(m.account))})" title="Show only ${escapeHtml(m.account)}">${escapeHtml(m.account)}</button>`
    : `<span class="muted">Unassigned</span>`;
  const org = m.org_id
    ? `<div class="log-org">${envBadge(ORGS[m.org_id] || { environment: m.org_environment })}<button type="button" class="link-btn plain mono"
         onclick="event.stopPropagation(); setLogFilter('org_id', ${jsStr(m.org_id)})" title="Show only ${escapeHtml(m.org_id)}">${escapeHtml(m.org_id)}</button></div>`
    : "";
  return acct + org;
}

function logRow(m) {
  const tr = document.createElement("tr");
  tr.dataset.logId = m.log_id;
  tr.className = [m.archived ? "archived" : "", LOGLIB.openId === m.log_id ? "open" : ""].join(" ").trim();
  tr.onclick = () => showLogDetail(m.log_id);
  const title = m.label || m.source_log || m.log_id;
  const idArg = jsStr(m.log_id);
  tr.innerHTML = `
    <td class="sel-col" onclick="event.stopPropagation()">
      <input type="checkbox" aria-label="Select ${escapeHtml(title)}" ${LOGLIB.selected.has(m.log_id) ? "checked" : ""}
        ${m.can_manage ? "" : `disabled title="Only the owner or an admin can change this log"`}
        onchange="toggleLogSelected(${idArg}, this.checked)"></td>
    <td class="nowrap">${fmtWhen(m.timestamp)}</td>
    <td class="log-title-cell" title="${escapeHtml(m.log_id)}"><div class="log-title">${escapeHtml(title)}${m.archived ? ` <span class="badge archived">Archived</span>` : ""}</div>
      ${m.source_log && m.source_log !== title ? `<div class="log-sub">${escapeHtml(m.source_log)}</div>`
        : title !== m.log_id ? `<div class="log-sub mono">${escapeHtml(m.log_id)}</div>` : ""}</td>
    <td>${logWhereHtml(m)}</td>
    <td>${ownerHtml(m)}</td>
    <td>${m.top_exception ? `<span class="exc-name">${escapeHtml(m.top_exception)}</span>` : `<span class="muted">none</span>`}</td>
    <td class="num">${m.exception_count ? `<span class="badge high">${m.exception_count}</span>` : `<span class="muted">0</span>`}</td>
    <td class="row-actions" onclick="event.stopPropagation()">
      <button type="button" class="link-btn" onclick="downloadStoredLog(${idArg})">Download</button>
      ${m.can_manage ? `
        <button type="button" class="link-btn" onclick="editLogTags(${idArg})">Edit</button>
        <button type="button" class="link-btn" onclick="setLogArchived(${idArg}, ${!m.archived})">${m.archived ? "Restore" : "Archive"}</button>
        <button type="button" class="link-btn danger" onclick="deleteLogs([${idArg}])">Delete</button>` : ""}
    </td>`;
  return tr;
}

function flashLogRow(id) {
  const tr = [...document.querySelectorAll("#logsTable tr")].find(r => r.dataset.logId === id);
  if (!tr) return;
  tr.classList.add("flash");
  tr.scrollIntoView({ behavior: "smooth", block: "nearest" });
  setTimeout(() => tr.classList.remove("flash"), 2200);
}

function toggleLogSelected(id, on) {
  if (on) LOGLIB.selected.add(id); else LOGLIB.selected.delete(id);
  renderLogBulkBar(filteredLogs());
}

function toggleAllLogs(on) {
  filteredLogs().filter(m => m.can_manage).forEach(m => { if (on) LOGLIB.selected.add(m.log_id); else LOGLIB.selected.delete(m.log_id); });
  renderLogLibrary();
}

function renderLogBulkBar(rows) {
  const bar = document.getElementById("logBulkBar");
  const manageable = rows.filter(m => m.can_manage);
  const all = document.getElementById("logSelectAll");
  const selectedHere = manageable.filter(m => LOGLIB.selected.has(m.log_id));
  if (all) {
    all.disabled = !manageable.length;
    all.checked = !!manageable.length && selectedHere.length === manageable.length;
    all.indeterminate = selectedHere.length > 0 && selectedHere.length < manageable.length;
  }
  const sel = LOGLIB.logs.filter(m => LOGLIB.selected.has(m.log_id));
  if (!sel.length) { bar.style.display = "none"; bar.innerHTML = ""; return; }
  const anyActive = sel.some(m => !m.archived), anyArchived = sel.some(m => m.archived);
  bar.style.display = "flex";
  bar.innerHTML = `<b>${sel.length} selected</b>
    ${anyActive ? `<button type="button" class="secondary" onclick="bulkArchive(true)">Archive</button>` : ""}
    ${anyArchived ? `<button type="button" class="secondary" onclick="bulkArchive(false)">Restore</button>` : ""}
    <button type="button" class="secondary" onclick="bulkRetag()">Tag&hellip;</button>
    <button type="button" class="secondary danger-outline" onclick="deleteLogs([...LOGLIB.selected])">Delete</button>
    <button type="button" class="link-btn" onclick="LOGLIB.selected.clear(); renderLogLibrary()">Clear selection</button>`;
}

async function patchLog(id, body) {
  const res = await api(`/api/logs/${encodeURIComponent(id)}`, {
    method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  if (!res.ok) return { ok: false, error: await errorText(res) };
  return { ok: true, meta: await res.json() };
}

async function setLogArchived(id, archived) {
  const r = await patchLog(id, { archived });
  if (!r.ok) { toast(`Could not ${archived ? "archive" : "restore"} it: ${r.error}`, "error"); return; }
  toast(archived ? "Archived -- find it under Archived." : "Restored to the active library.", "ok");
  await loadLogs();
  if (LOGLIB.openId === id) showLogDetail(id, { scroll: false });
}

async function bulkArchive(archived) {
  const ids = LOGLIB.logs.filter(m => LOGLIB.selected.has(m.log_id) && !!m.archived !== archived).map(m => m.log_id);
  let ok = 0; const errs = [];
  for (const id of ids) { const r = await patchLog(id, { archived }); if (r.ok) ok++; else errs.push(r.error); }
  toast(`${archived ? "Archived" : "Restored"} ${ok} log(s).` + (errs.length ? ` ${errs.length} failed: ${errs[0]}` : ""), errs.length ? "error" : "ok");
  LOGLIB.selected.clear();
  await loadLogs();
}

async function deleteLogs(ids) {
  ids = ids.filter(Boolean);
  if (!ids.length) return;
  const one = ids.length === 1 ? LOGLIB.logs.find(m => m.log_id === ids[0]) : null;
  const name = one ? (one.label || one.log_id) : `${ids.length} logs`;
  if (!await confirmModal(`Delete ${name}?`,
      `Permanently removes the normalized JSON and its tags from the library. This can't be undone &mdash;
       <b>archive</b> instead if it might be needed again.`, "Delete")) return;
  let ok = 0; const errs = [];
  for (const id of ids) {
    const res = await api(`/api/logs/${encodeURIComponent(id)}`, { method: "DELETE" });
    if (res.ok) { ok++; LOGLIB.selected.delete(id); if (LOGLIB.openId === id) closeLogDetail(); }
    else errs.push(await errorText(res));
  }
  toast(`Deleted ${ok} log(s).` + (errs.length ? ` ${errs.length} failed: ${errs[0]}` : ""), errs.length ? "error" : "ok");
  await loadLogs();
}

function tagFields(m = {}) {
  return [
    { name: "org_id", label: "Org", value: m.org_id || "", placeholder: "Org ID (leave blank for none)",
      options: Object.keys(ORGS).sort(), hint: "An org's own account is used when it has one." },
    { name: "account", label: "Customer account", value: m.account || "", placeholder: "e.g. Acme Corp",
      options: logAccountNames(), hint: "Clear a box to remove that tag." },
  ];
}

function cleanTag(s) { return String(s || "").replace(/\s+/g, " ").trim(); }

async function editLogTags(id) {
  const m = LOGLIB.logs.find(x => x.log_id === id);
  if (!m) return;
  const answer = await modal({
    title: "Edit log",
    body: `<span class="mono">${escapeHtml(id)}</span>`,
    fields: [{ name: "label", label: "Label", value: m.label || "", placeholder: m.source_log || "" }, ...tagFields(m)],
    submitLabel: "Save",
  });
  if (!answer) return;
  const body = {};
  const label = cleanTag(answer.label), org = cleanTag(answer.org_id), acct = cleanTag(answer.account);
  if (label !== (m.label || "")) body.label = label || null;
  if (org !== (m.org_id || "")) body.org_id = org || null;
  if (acct !== (m.account || "") || "org_id" in body) body.account = acct || null;
  if (!Object.keys(body).length) return;
  if (org && !ORGS[org]) { toast(`No org '${org}' that you can see.`, "error"); return; }
  const r = await patchLog(id, body);
  if (!r.ok) { toast("Could not save: " + r.error, "error"); return; }
  toast("Saved.", "ok");
  await loadLogs({ highlight: id });
  if (LOGLIB.openId === id) showLogDetail(id, { scroll: false });
}

async function bulkRetag() {
  const ids = [...LOGLIB.selected];
  if (!ids.length) return;
  const answer = await modal({
    title: `Tag ${ids.length} log(s)`,
    body: "Sets the same org and account on every selected log. Leave the org blank to tag an account only.",
    fields: tagFields(),
    submitLabel: "Apply",
  });
  if (!answer) return;
  const org = cleanTag(answer.org_id), acct = cleanTag(answer.account);
  if (org && !ORGS[org]) { toast(`No org '${org}' that you can see.`, "error"); return; }
  let ok = 0; const errs = [];
  for (const id of ids) { const r = await patchLog(id, { org_id: org || null, account: acct || null }); if (r.ok) ok++; else errs.push(r.error); }
  toast(`Tagged ${ok} log(s).` + (errs.length ? ` ${errs.length} failed: ${errs[0]}` : ""), errs.length ? "error" : "ok");
  LOGLIB.selected.clear();
  await loadLogs();
}

async function downloadStoredLog(id) {
  const data = await apiJson(`/api/logs/${encodeURIComponent(id)}`, {}, null);
  if (!data) { toast("Could not load that log.", "error"); return; }
  downloadBlob(data.normalized_log, `${id}.normalized.json`);
}

function closeLogDetail() {
  const card = document.getElementById("logDetailCard");
  if (card) card.style.display = "none";
  LOGLIB.openId = null;
  document.querySelectorAll("#logsTable tr.open").forEach(tr => tr.classList.remove("open"));
}

async function showLogDetail(logId, { scroll = true } = {}) {
  const card = document.getElementById("logDetailCard");
  card.style.display = "block";
  LOGLIB.openId = logId;
  document.querySelectorAll("#logsTable tr").forEach(tr => tr.classList.toggle("open", tr.dataset.logId === logId));
  // Opening a stored log replaces the upload result above rather than
  // stacking a second full copy of the same sections under it.
  const uploadResult = document.getElementById("logResult");
  if (uploadResult) uploadResult.innerHTML = "";
  const uploadActions = document.getElementById("logResultActions");
  if (uploadActions) uploadActions.style.display = "none";
  setBusy("logDetail");
  if (scroll) card.scrollIntoView({ behavior: "smooth" });
  const data = await apiJson(`/api/logs/${encodeURIComponent(logId)}`, {}, null);
  if (!data) {
    document.getElementById("logDetail").innerHTML = `<p class="muted">Could not load that log &mdash; it may have been deleted.</p>`;
    document.getElementById("logDetailActions").innerHTML = "";
    return;
  }
  const m = data.meta || {};
  window._logDetail = data.normalized_log;
  document.getElementById("logDetailTitle").textContent = m.label || m.source_log || logId;
  const facts = [
    ["Account", m.account ? escapeHtml(m.account) : `<span class="muted">Unassigned</span>`],
    ["Org", m.org_id ? `${envBadge(ORGS[m.org_id] || { environment: m.org_environment })}${ORGS[m.org_id]
        ? `<button type="button" class="link-btn plain mono" onclick="setActiveOrg(${jsStr(m.org_id)}); showView('dashboard')" title="Open this org's dashboard">${escapeHtml(m.org_id)}</button>`
        : `<span class="mono">${escapeHtml(m.org_id)}</span>`}` : `<span class="muted">None</span>`],
    ["Owner", ownerHtml(m)],
    ["Stored", escapeHtml(fmtWhen(m.timestamp))],
    ["Source file", m.source_log ? `<span class="mono">${escapeHtml(m.source_log)}</span>` : `<span class="muted">-</span>`],
  ];
  if (m.archived) facts.push(["Archived", `${escapeHtml(fmtWhen(m.archived_at))}${m.archived_by ? ` by ${escapeHtml(m.archived_by)}` : ""}`]);
  document.getElementById("logDetailMeta").innerHTML = `<span class="mono muted">${escapeHtml(logId)}</span>`
    + `<dl class="log-facts">${facts.map(([k, v]) => `<div><dt>${k}</dt><dd>${v}</dd></div>`).join("")}</dl>`;
  const idArg = jsStr(logId);
  const q = `Analyze stored normalized log ${logId} (use get_normalized_log) and give me the most likely root cause and a suggested fix.`;
  document.getElementById("logDetailActions").innerHTML = `
    <button type="button" class="secondary" onclick="downloadBlob(window._logDetail, ${jsStr(logId + ".normalized.json")})">Download normalized JSON</button>
    ${typeof askAbout === "function" ? `<button type="button" class="secondary" onclick="askAbout(${jsStr(q)})">Ask the assistant</button>` : ""}
    ${m.can_manage ? `
      <button type="button" class="secondary" onclick="editLogTags(${idArg})">Edit label &amp; tags</button>
      <button type="button" class="secondary" onclick="setLogArchived(${idArg}, ${!m.archived})">${m.archived ? "Restore" : "Archive"}</button>
      <button type="button" class="secondary danger-outline" onclick="deleteLogs([${idArg}])">Delete</button>`
      : m.can_manage === undefined || serverIsStale()
        ? `<span class="warn-text small">The server is running older code, so archive, delete and tags are unavailable &mdash; restart the server.</span>`
        : `<span class="muted small">Only ${m.owner ? `${escapeHtml(m.owner)} or an admin` : "an admin"} can archive or delete this log.</span>`}`;
  document.getElementById("logDetail").innerHTML =
    renderNormalizedLog(data.normalized_log) + collapsibleJson("Normalized JSON", data.normalized_log);
}

if (typeof document !== "undefined" && document.getElementById("logDrop")) wireLogDrop();

// =====================================================================
// 4. auth + boot
// =====================================================================

let CURRENT_USER = null;

async function doLogin() {
  const username = document.getElementById("loginUser").value.trim();
  const password = document.getElementById("loginPass").value;
  const statusEl = document.getElementById("loginStatus");
  if (!username || !password) { statusEl.textContent = "Enter username and password."; statusEl.className = "status-line error"; return; }
  statusEl.textContent = "Signing in..."; statusEl.className = "status-line";
  const res = await api("/api/auth/login", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (!res.ok) {
    statusEl.textContent = await errorText(res); statusEl.className = "status-line error"; return;
  }
  CURRENT_USER = await res.json();
  SESSION_DEAD = false;
  statusEl.textContent = "";
  document.getElementById("loginPass").value = "";
  enterApp();
}

async function doLogout() {
  await api("/api/auth/logout", { method: "POST" });
  CURRENT_USER = null;
  SESSION_DEAD = false;
  document.getElementById("appRoot").style.display = "none";
  const s = document.getElementById("loginStatus");
  s.textContent = "Signed out."; s.className = "status-line";
  showSignup(false);
  document.getElementById("loginOverlay").style.display = "flex";
}

// ---------- self-registration ----------
//
// Open registration is safe here only because the app is reachable only over
// the Conga VPN, so everyone who can load this form is already inside the
// company. What keeps it safe beyond that is server-side: the role is
// whitelisted in app/auth.py (never trusted from this form) and a new account
// lands in the lower LLM quota tier until an admin verifies it.

/** Whether the server is accepting registrations, fetched once so the link is
 *  not offered on a server that would refuse it. */
async function loadSignupConfig() {
  const cfg = await apiJson("/api/auth/signup-config", {}, null);
  if (!cfg || !cfg.enabled) return;
  document.getElementById("signupPrompt").style.display = "";
  document.getElementById("suUserHint").textContent = cfg.username_rule || "";
}

function showSignup(on) {
  document.getElementById("signInPane").style.display = on ? "none" : "";
  document.getElementById("signUpPane").style.display = on ? "" : "none";
  const status = document.getElementById(on ? "signupStatus" : "loginStatus");
  status.textContent = ""; status.className = "status-line";
  const focus = document.getElementById(on ? "suUser" : "loginUser");
  if (focus) focus.focus();
}

async function doSignup() {
  const username = document.getElementById("suUser").value.trim().toLowerCase();
  const password = document.getElementById("suPass").value;
  const confirm = document.getElementById("suPass2").value;
  const role = (document.querySelector('input[name="suRole"]:checked') || {}).value || "user";
  const statusEl = document.getElementById("signupStatus");
  const fail = msg => { statusEl.textContent = msg; statusEl.className = "status-line error"; };

  // Checked here purely so the common mistakes get an instant answer; the
  // server validates all of it again and is the only thing that decides.
  if (!username || !password) return fail("Choose a username and a password.");
  if (password !== confirm) return fail("The two passwords don't match.");
  if (password.length < 8) return fail("Password must be at least 8 characters.");

  statusEl.textContent = "Creating your account..."; statusEl.className = "status-line";
  const res = await api("/api/auth/signup", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password, role }),
  });
  if (!res.ok) return fail(await errorText(res));
  CURRENT_USER = await res.json();
  SESSION_DEAD = false;
  statusEl.textContent = "";
  document.getElementById("suPass").value = "";
  document.getElementById("suPass2").value = "";
  enterApp();
  // Said once, on the way in, because it explains a limit they would
  // otherwise only discover by hitting it mid-investigation.
  toast("Welcome. Your account starts with a smaller LLM allowance until an "
        + "admin verifies it -- see the Usage tab.", "ok", 9000);
}

async function changeOwnPassword() {
  const answer = await modal({
    title: "Change your password",
    body: "Minimum 8 characters. You stay signed in on this device.",
    fields: [
      { name: "current", label: "Current password", type: "password" },
      { name: "next", label: "New password", type: "password" },
      { name: "confirm", label: "Confirm new password", type: "password" },
    ],
    submitLabel: "Change password",
  });
  if (!answer) return;
  if (answer.next !== answer.confirm) { toast("The two new passwords don't match.", "error"); return; }
  const res = await api("/api/auth/password", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ current_password: answer.current, new_password: answer.next }),
  });
  toast(res.ok ? "Password changed." : "Failed: " + await errorText(res), res.ok ? "ok" : "error");
}

function applyRole() {
  if (!CURRENT_USER) return;
  const role = CURRENT_USER.role;
  document.getElementById("userInfo").innerHTML =
    `${escapeHtml(CURRENT_USER.username)}<span class="role-tag">${escapeHtml(role)}</span>`;
  document.getElementById("navAdmin").style.display = role === "admin" ? "" : "none";
  // Usage is for everyone now: the tab shows your own consumption and your own
  // quota, and only the cross-user reports inside it are admin-only. Somebody
  // who can see how much of their allowance is left does not have to ask.
  document.getElementById("navUsage").style.display = "";
  document.querySelectorAll("[data-admin-only]").forEach(el => {
    el.style.display = role === "admin" ? "" : "none";
  });
  // Hide write-only cards for readers (the server enforces this too).
  const canWrite = role === "user" || role === "admin";
  document.querySelectorAll('[data-requires="user"]').forEach(el => {
    el.style.display = canWrite ? "" : "none";
  });
}

// Must match APP_BUILD in app/main.py and the ?v= on index.html's assets.
const CLIENT_BUILD = 27;
let SERVER_BUILD = null;   // null = not checked yet, 0 = a server too old to report one

/** Static files are served fresh, but the server's Python is only loaded at
 *  start-up -- so an update can leave new UI talking to an old server that
 *  quietly ignores new fields. Say so, loudly, instead of letting features
 *  look broken. */
async function checkServerBuild() {
  try {
    const res = await fetch("/api/build");
    SERVER_BUILD = res.ok ? ((await res.json()).build || 0) : 0;
  } catch (e) {
    return;
  }
  const host = document.getElementById("buildBanner");
  if (!host) return;
  if (SERVER_BUILD < CLIENT_BUILD) {
    host.innerHTML = `<b>The server is running older code than this page</b> (server build ${SERVER_BUILD || "unknown"},
      page build ${CLIENT_BUILD}). Restart it (stop it, then run <span class="mono">start_server.ps1</span> again) &mdash;
      until then newer features such as log tags, owners, archive and delete will not work.`;
    host.style.display = "";
  } else if (SERVER_BUILD > CLIENT_BUILD) {
    host.innerHTML = `<b>This page is out of date.</b> The server was updated &mdash; press <b>Ctrl+F5</b> to reload.`;
    host.style.display = "";
  } else {
    host.style.display = "none";
  }
}

function serverIsStale() { return SERVER_BUILD !== null && SERVER_BUILD < CLIENT_BUILD; }

function enterApp() {
  document.getElementById("loginOverlay").style.display = "none";
  document.getElementById("appRoot").style.display = "";
  checkServerBuild();
  applyRole();
  loadOrgs();
  if (typeof initHome === "function") initHome();
  if (typeof initGuide === "function") initGuide();
  // The dock remembers whether it was open, per browser. Guarded because
  // chat.js is a separate script and a cached index.html could load without it.
  if (typeof initChatDock === "function") initChatDock();
}

// ---------- usage (admin) ----------
//
// Every figure here comes from data/usage/*.jsonl, written one record per
// chat turn. With one shared LLM key the provider's dashboard can only say
// what the SERVER spent; only this app knows which account asked.

async function loadUsageView() {
  // Everyone gets their own numbers; only an admin gets the rest, and asking
  // for the admin endpoints as a reader would just 403 into a scary toast.
  await renderMyUsage();
  if (!CURRENT_USER || CURRENT_USER.role !== "admin") return;
  await Promise.all([renderLlmStatusPanel(), populateUsageUserPicker()]);
  await loadUsage();
}

/** One account's own consumption and quota. The quota meter is the point:
 *  "how much have I used" is only ever asked in order to answer "how much is
 *  left", and the previous screen made the reader do that subtraction. */
async function renderMyUsage() {
  const quotaHost = document.getElementById("myQuota");
  const kpis = document.getElementById("myUsageKpis");
  setBusy(quotaHost);
  const r = await apiJson("/api/usage/me?days=30", {}, null);
  if (!r) { quotaHost.innerHTML = `<p class="status-line error">Could not load your usage.</p>`;
            kpis.innerHTML = ""; return; }
  quotaHost.innerHTML = renderQuotaPanel(r.quota);
  const t = r.totals || {};
  kpis.innerHTML = `
    <div class="kpi"><div class="kpi-value">${t.turns || 0}</div><div class="kpi-label">Your questions</div></div>
    <div class="kpi"><div class="kpi-value">${fmtCompact(t.total_tokens)}</div><div class="kpi-label">Tokens, 30 days</div></div>
    <div class="kpi"><div class="kpi-value">${t.tool_calls || 0}</div><div class="kpi-label">Tool calls</div></div>
    <div class="kpi"><div class="kpi-value">${t.avg_seconds_per_turn || 0}s</div><div class="kpi-label">Avg answer time</div></div>`;
  renderUsageTrend(r, "myUsageTrend");
}

/** The quota meter, shared by the Usage tab and the chat footer. */
function renderQuotaPanel(q) {
  if (!q) return "";
  if (q.unlimited) {
    return `<div class="key-status ok">No LLM limit on this account${
      q.tier === "admin" ? " (admins are never capped)" : ""}.</div>`;
  }
  const tierNote = q.tier === "unverified"
    ? `Your account is <b>unverified</b>, so it is on the lower allowance.
       An admin verifying it raises the limit.`
    : (q.source === "override"
        ? `An admin has set a limit specifically for your account.`
        : `Standard allowance for a verified account.`);
  return `<div class="quota-panel${q.exceeded ? " exceeded" : ""}">
      ${bar("Today", q.daily, `resets ${escapeHtml((q.daily_resets_at || "").slice(0, 10))} 00:00 UTC`)}
      ${bar(`Last ${q.window_days} days`, q.window, "frees up as older days drop out")}
      <p class="muted quota-note">${tierNote}</p>
    </div>`;

  function bar(label, part, note) {
    if (!part || part.limit === null) {
      return `<div class="quota-row"><div class="quota-label">${escapeHtml(label)}</div>
        <div class="quota-meter"><span class="muted">unlimited</span></div></div>`;
    }
    // Three bands rather than a gradient: below 75% there is nothing to think
    // about, 75-100% is the moment to ask for more, and over is over.
    const band = part.exceeded ? "over" : (part.pct >= 75 ? "warn" : "ok");
    return `<div class="quota-row">
      <div class="quota-label">${escapeHtml(label)}</div>
      <div class="quota-meter">
        <div class="quota-track"><div class="quota-fill ${band}" style="width:${Math.min(100, part.pct)}%"></div></div>
        <div class="quota-figures">${fmtCompact(part.used)} of ${fmtCompact(part.limit)} tokens
          &middot; ${part.exceeded ? "<b>none left</b>" : `${fmtCompact(part.remaining)} left`}
          <span class="muted">&middot; ${escapeHtml(note)}</span></div>
      </div></div>`;
  }
}

/** The shared connection, read-only. Deliberately a report and not a form:
 *  the values live in the server's environment, and the panel's job is to say
 *  what is loaded, whether it works, and which variables to edit. */
async function renderLlmStatusPanel() {
  const host = document.getElementById("llmStatusPanel");
  setBusy(host);
  const s = await apiJson("/api/llm", {}, null);
  if (!s) { host.innerHTML = `<p class="status-line error">Could not read the LLM connection.</p>`; return; }
  const names = { openrouter: "OpenRouter", azure: "Azure OpenAI" };
  const env = s.env_vars || {};

  let banner;
  if (s.configured) {
    banner = `<div class="key-status ok">
      Connected to <b>${escapeHtml(names[s.provider] || s.provider)}</b>${
        s.hint ? ` with key <code>${escapeHtml(s.hint)}</code>` : ""}.
      Every signed-in user chats through this &mdash; they have nothing to configure.
      ${s.verified_at ? `<div style="margin-top:4px;">Last successful call ${
        escapeHtml(fmtWhen(s.verified_at))}.</div>` : ""}
    </div>`;
  } else if (s.present_but_invalid) {
    banner = `<div class="key-status warn"><b>Configured but not usable</b> &mdash; nobody can chat.
      <div style="margin-top:4px;">${escapeHtml(s.config_error || "")}</div></div>`;
  } else {
    banner = `<div class="key-status warn"><b>Not configured.</b> Chat is unavailable for everyone.
      Everything else &mdash; knowledgebases, incidents, known issues, the log normalizer &mdash;
      works without it.</div>`;
  }

  host.innerHTML = `${banner}
    <table class="mini-table" style="margin-top:12px;">
      <tr><td>Provider</td><td><code>${escapeHtml(s.provider || "-")}</code></td></tr>
      <tr><td>Model</td><td><code>${escapeHtml(s.default_model || "(chosen per chat)")}</code></td></tr>
      ${s.endpoint ? `<tr><td>Endpoint</td><td class="mono" style="word-break:break-all; font-size:11px;">${
        escapeHtml(s.endpoint)}</td></tr>` : ""}
      <tr><td>Users may change model</td><td>${s.model_locked ? "no (locked)" : "yes"}</td></tr>
    </table>
    <details class="raw-json" style="margin-top:12px;">
      <summary>How to change the connection</summary>
      <pre>${escapeHtml(env.provider || "TS_LLM_PROVIDER")}=azure | openrouter
${escapeHtml(env.api_key || "TS_LLM_API_KEY")}=&lt;the key&gt;
${escapeHtml(env.endpoint || "TS_LLM_ENDPOINT")}=&lt;full Azure chat-completions URL, Azure only&gt;
${escapeHtml(env.default_model || "TS_LLM_DEFAULT_MODEL")}=&lt;optional&gt;
${escapeHtml(env.lock_model || "TS_LLM_LOCK_MODEL")}=1   # optional: users cannot change model</pre>
      <p class="muted">Put these in the service's EnvironmentFile (mode 0600, so the key is not
        world-readable via <code>systemctl show</code>) and restart. The startup log line reports
        whether the connection loaded.</p>
    </details>`;
}

async function populateUsageUserPicker() {
  const sel = document.getElementById("usageUser");
  const current = sel.value;
  const users = await apiJson("/api/admin/users", {}, {}) || {};
  sel.innerHTML = `<option value="">Everyone</option>` + Object.keys(users).sort()
    .map(u => `<option value="${escapeHtml(u)}">${escapeHtml(u)}</option>`).join("");
  sel.value = current;
}

async function loadUsage() {
  const days = document.getElementById("usageDays").value;
  const username = document.getElementById("usageUser").value;
  const kpis = document.getElementById("usageKpis");
  setBusy(kpis);
  const q = `days=${encodeURIComponent(days)}${username ? `&username=${encodeURIComponent(username)}` : ""}`;
  const r = await apiJson(`/api/admin/usage?${q}`, {}, null);
  if (!r) { kpis.innerHTML = `<p class="status-line error">Could not load usage.</p>`; return; }

  const t = r.totals || {};
  // `cost_available` is false when no provider in the window reported a cost,
  // which is the Azure case -- Azure bills the subscription, not the call. A
  // confident "$0.00" there would be read as "this is free", so it says so.
  const costCell = t.cost_available
    ? `<div class="kpi-value">${fmtMoney(t.cost)}</div><div class="kpi-label">Cost</div>`
    : `<div class="kpi-value">&mdash;</div><div class="kpi-label">Cost</div>
       <div class="kpi-note">not reported by this provider</div>`;

  kpis.innerHTML = `
    <div class="kpi"><div class="kpi-value">${t.turns || 0}</div><div class="kpi-label">Questions</div></div>
    <div class="kpi"><div class="kpi-value">${fmtCompact(t.total_tokens)}</div><div class="kpi-label">Tokens</div></div>
    <div class="kpi">${costCell}</div>
    <div class="kpi"><div class="kpi-value">${r.active_users || 0}</div><div class="kpi-label">Active users</div></div>
    <div class="kpi"><div class="kpi-value">${t.tool_calls || 0}</div><div class="kpi-label">Tool calls</div></div>
    <div class="kpi"><div class="kpi-value">${t.avg_seconds_per_turn || 0}s</div><div class="kpi-label">Avg answer time</div></div>
    ${t.failed_turns ? `<div class="kpi"><div class="kpi-value">${t.failed_turns}</div>
      <div class="kpi-label">Failed turns</div>
      <div class="kpi-note">counted because a failed attempt still explains a quiet period</div></div>` : ""}`;

  renderUsageTrend(r);
  renderUsageTable("usageByUser", r.by_user, "username", t.total_tokens, 8, true);
  renderUsageTable("usageByOrg", r.by_org, "org_id", t.total_tokens, 5);
  renderUsageModels(r.by_model);
}

/** A per-day bar chart, drawn with divs.
 *
 *  No charting library: this is the only chart in the app, and pulling in a
 *  dependency for it would be the biggest thing the page downloads. Days with
 *  no activity are drawn as a hairline rather than skipped, because a trend
 *  that silently omits quiet days makes a flat week look busy. */
function renderUsageTrend(r, hostId = "usageTrend") {
  const host = document.getElementById(hostId);
  if (!host) return;
  const days = r.by_day || [];
  if (!days.length) { host.innerHTML = ""; return; }
  const peak = Math.max(1, ...days.map(d => d.total_tokens || 0));
  const bars = days.map(d => {
    const v = d.total_tokens || 0;
    const h = v ? Math.max(2, Math.round((v / peak) * 100)) : 0;
    const title = `${d.date}: ${fmtCompact(v)} tokens, ${d.turns} question(s)`
      + (d.cost_available ? `, ${fmtMoney(d.cost)}` : "");
    return `<div class="usage-bar-wrap" title="${escapeHtml(title)}">
      <div class="usage-bar ${v ? "" : "empty"}" style="height:${h}%"></div></div>`;
  }).join("");
  host.innerHTML = `
    <div class="usage-chart">${bars}</div>
    <div class="usage-axis"><span>${escapeHtml(r.from)}</span><span>${escapeHtml(r.to)}</span></div>
    <div class="usage-legend">Tokens per day. Peak day: ${fmtCompact(peak)} tokens. Hover a bar
      for that day's figures.</div>`;
}

function renderUsageTable(tbodyId, rows, keyField, grandTotal, colspan, markSelf = false) {
  const tbody = document.getElementById(tbodyId);
  fillTable(tbody, rows || [], colspan, "No usage recorded in this period.", row => {
    const tr = document.createElement("tr");
    const share = grandTotal ? Math.round((row.total_tokens / grandTotal) * 100) : 0;
    const isSelf = markSelf && row[keyField] === currentUsername();
    if (isSelf) tr.className = "usage-row-self";
    const label = row[keyField] === "(no org)"
      ? `<span class="muted">no org selected</span>` : escapeHtml(row[keyField]);
    const cost = row.cost_available ? fmtMoney(row.cost) : `<span class="muted">&mdash;</span>`;
    const shareCell = `<td><div class="usage-bar-mini" style="width:${Math.max(2, share)}%"
        title="${share}% of tokens"></div></td>`;
    if (keyField === "username") {
      tr.innerHTML = `<td>${label}${isSelf ? " <span class='badge'>you</span>" : ""}</td>
        <td class="num">${row.turns}</td><td class="num">${fmtCompact(row.total_tokens)}</td>
        <td class="num">${cost}</td><td class="num">${row.tool_calls}</td>
        <td class="num">${row.avg_seconds_per_turn}s</td>
        <td class="num">${row.failed_turns || "-"}</td>${shareCell}`;
    } else {
      tr.innerHTML = `<td>${label}</td><td class="num">${row.turns}</td>
        <td class="num">${fmtCompact(row.total_tokens)}</td><td class="num">${cost}</td>${shareCell}`;
    }
    return tr;
  });
}

function renderUsageModels(rows) {
  const tbody = document.getElementById("usageByModel");
  fillTable(tbody, rows || [], 5, "No usage recorded in this period.", row => {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td class="mono">${escapeHtml(row.model)}</td><td class="num">${row.turns}</td>
      <td class="num">${fmtCompact(row.total_tokens)}</td>
      <td class="num">${row.cost_available ? fmtMoney(row.cost) : "<span class='muted'>&mdash;</span>"}</td>
      <td class="num">${row.avg_seconds_per_turn}s</td>`;
    return tr;
  });
}

/** The signed-in username, or null.
 *
 *  A function rather than a direct `CURRENT_USER.username` read at each call
 *  site: `CURRENT_USER` is a top-level `let`, which is a lexical binding and
 *  therefore invisible to the render tests' sandbox. Reading it through a
 *  declared function makes the dependency substitutable, and saves every
 *  caller from repeating the null guard. */
function currentUsername() {
  return CURRENT_USER ? CURRENT_USER.username : null;
}

function fmtCompact(n) {
  n = n || 0;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}k`;
  return String(n);
}

function fmtMoney(n) {
  n = n || 0;
  if (n === 0) return "$0.00";
  return n < 0.01 ? `$${n.toFixed(4)}` : `$${n.toFixed(2)}`;
}

// ---------- API tokens ----------

async function createToken() {
  const label = document.getElementById("tokenLabel").value.trim();
  const ttlRaw = document.getElementById("tokenTtl").value.trim();
  const body = {};
  if (label) body.label = label;
  if (ttlRaw) body.ttl_days = parseInt(ttlRaw, 10);
  const res = await api("/api/tokens", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const el = document.getElementById("tokenCreateResult");
  if (!res.ok) { el.innerHTML = `<p class="status-line error">${escapeHtml(await errorText(res))}</p>`; return; }
  const data = await res.json();
  track("token");
  el.innerHTML = `<div class="token-reveal">
      <b>Copy this token now (role: ${escapeHtml(data.role)}):</b><br>
      <span id="tokenValue">${escapeHtml(data.token)}</span>
      <div><button class="secondary" onclick="copyToken()">Copy to clipboard</button></div>
      <div class="muted">It is not stored in readable form and cannot be shown again -- only revoked.</div>
    </div>`;
  loadTokens();
}

async function copyToken() {
  const text = document.getElementById("tokenValue").textContent;
  try {
    await navigator.clipboard.writeText(text);
    toast("Token copied to the clipboard.", "ok");
  } catch (e) {
    toast("Clipboard blocked by the browser -- select the token and copy it manually.", "error");
  }
}

async function loadTokens() {
  const tbody = document.getElementById("tokensTable");
  const tokens = await apiJson("/api/tokens", {}, []) || [];
  const apiTokens = tokens.filter(t => t.kind !== "session");
  fillTable(tbody, apiTokens, 7, {
    title: "No API tokens yet",
    body: "A token lets Claude Desktop, or any MCP client, use these same tools as you -- with your role "
        + "and your org visibility. Create one above, then follow the setup steps.",
    actions: [{ label: "Show me how to connect Claude Desktop", onclick: "openMcpSetup()", primary: true }],
  }, t => {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${escapeHtml(t.label || "(none)")}</td><td>${escapeHtml(t.role)}</td>
      <td>${escapeHtml(t.username)}</td><td>${fmtWhen(t.created_at)}</td>
      <td>${t.expires_at ? fmtWhen(t.expires_at) : "never"}</td>
      <td>${t.last_used ? fmtWhen(t.last_used) : "never"}</td>
      <td><button class="secondary" onclick="revokeToken('${escapeHtml(t.id)}')">Revoke</button></td>`;
    return tr;
  });
}

async function revokeToken(id) {
  if (!await confirmModal("Revoke this token?",
      "Anything using it -- an MCP server, a script -- stops working immediately.", "Revoke")) return;
  const res = await api(`/api/tokens/${encodeURIComponent(id)}`, { method: "DELETE" });
  toast(res.ok ? "Token revoked." : "Could not revoke that token.", res.ok ? "ok" : "error");
  loadTokens();
}

// ---------- admin ----------

async function createUser() {
  const username = document.getElementById("admUser").value.trim();
  const password = document.getElementById("admPass").value;
  const role = document.getElementById("admRole").value;
  const statusEl = document.getElementById("admStatus");
  const res = await api("/api/admin/users", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password, role }),
  });
  if (!res.ok) { statusEl.textContent = await errorText(res); statusEl.className = "status-line error"; return; }
  statusEl.textContent = `Created ${username} (${role}).`; statusEl.className = "status-line ok";
  document.getElementById("admUser").value = "";
  document.getElementById("admPass").value = "";
  loadUsers();
}

async function loadUsers() {
  await Promise.all([renderUsersTable(), renderLimitsEditor()]);
}

async function renderUsersTable() {
  const tbody = document.getElementById("usersTable");
  const users = await apiJson("/api/admin/users", {}, {}) || {};
  fillTable(tbody, Object.entries(users), 8, "No users.", ([name, u]) => {
    const tr = document.createElement("tr");
    const isSelf = CURRENT_USER && name === CURRENT_USER.username;
    const q = u.quota || {};
    const roleSel = `<select onchange="setUserRole('${escapeHtml(name)}', this.value)">
      ${["reader", "user", "admin"].map(r => `<option value="${r}" ${u.role === r ? "selected" : ""}>${r}</option>`).join("")}
    </select>`;

    // Where the account came from. Self-registration is the one worth
    // spotting at a glance, so it is the only value that gets a badge.
    const source = u.created_by === "self"
      ? `<span class="tag tag-self">self-registered</span>`
      : (u.created_by === "bootstrap" ? `<span class="muted">bootstrap</span>`
        : (u.created_by ? `<span class="muted">${escapeHtml(u.created_by.replace(/^admin:/, "by "))}</span>`
          : `<span class="muted">&mdash;</span>`));

    // Admins cannot be capped, so offering to verify one is a control that
    // does nothing -- say so instead of showing a dead button.
    const verifyCell = u.role === "admin"
      ? `<span class="muted">n/a</span>`
      : (u.verified
          ? `<span class="tag tag-ok" title="${escapeHtml(
              (u.verified_by ? "by " + u.verified_by : "") + (u.verified_at ? " " + fmtWhen(u.verified_at) : ""))}">verified</span>`
          : `<span class="tag tag-warn">unverified</span>`);

    tr.innerHTML = `<td>${escapeHtml(name)}${isSelf ? ' <span class="muted">(you)</span>' : ""}</td>
      <td>${roleSel}</td>
      <td>${u.disabled ? "<span style='color:var(--conga-color-status-error)'>disabled</span>" : "active"}</td>
      <td>${source}</td>
      <td>${verifyCell}</td>
      <td>${quotaCell(q, u)}</td>
      <td>${fmtWhen(u.created_at)}</td>
      <td class="row-actions">
        ${u.role === "admin" ? "" : `<button class="secondary" onclick="setVerified('${escapeHtml(name)}', ${!u.verified})">${
          u.verified ? "Unverify" : "Verify"}</button>
        <button class="secondary" onclick="editUserLimits('${escapeHtml(name)}')">Limit</button>`}
        <button class="secondary" onclick="resetUserPassword('${escapeHtml(name)}')">Reset password</button>
        ${isSelf ? "" : `<button class="secondary" onclick="toggleDisabled('${escapeHtml(name)}', ${!u.disabled})">${u.disabled ? "Enable" : "Disable"}</button>
        <button class="secondary" onclick="deleteUser('${escapeHtml(name)}')">Delete</button>`}
      </td>`;
    return tr;
  });
}

/** 30-day consumption against the cap, so the decision to verify or raise a
 *  limit can be made from the same row rather than from another screen. */
function quotaCell(q, u) {
  if (!q || q.unlimited) return `<span class="muted">unlimited</span>`;
  const w = q.window || {};
  const band = w.exceeded ? "over" : (w.pct >= 75 ? "warn" : "ok");
  const override = u.limits ? ` <span class="tag tag-info" title="per-account override">custom</span>` : "";
  return `<div class="quota-cell">
    <div class="quota-track sm"><div class="quota-fill ${band}" style="width:${Math.min(100, w.pct || 0)}%"></div></div>
    <div class="quota-cell-text">${fmtCompact(w.used)} / ${fmtCompact(w.limit)}${override}</div>
  </div>`;
}

async function setVerified(name, verified) {
  if (!verified && !await confirmModal(`Un-verify ${name}?`,
      "Their LLM allowance drops back to the unverified tier immediately. "
      + "They keep their role and stay signed in.", "Un-verify")) return;
  const res = await api(`/api/admin/users/${encodeURIComponent(name)}/verified`, {
    method: "PATCH", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ verified }),
  });
  toast(res.ok ? `${name} ${verified ? "verified" : "un-verified"}.` : await errorText(res),
        res.ok ? "ok" : "error");
  loadUsers();
}

/** A per-account override. Blank means "follow the tier" rather than
 *  "unlimited", because an account pinned to a number would never pick up a
 *  later change to the tier default -- a surprise six months from now. */
async function editUserLimits(name) {
  const answer = await modal({
    title: `LLM limit for ${name}`,
    body: "Tokens. Leave both blank and submit to remove the override so this account "
        + "follows its tier again. Enter 'unlimited' in a field for no cap on that window.",
    fields: [
      { name: "daily", label: "Daily token limit", type: "text" },
      { name: "monthly", label: "30-day token limit", type: "text" },
    ],
    submitLabel: "Save limit",
  });
  if (!answer) return;

  const parse = (raw) => {
    const v = (raw || "").trim().toLowerCase();
    if (!v) return undefined;                       // not provided
    if (v === "unlimited" || v === "none") return null;
    const n = parseInt(v.replace(/[,_\s]/g, ""), 10);
    return Number.isFinite(n) ? n : NaN;
  };
  const daily = parse(answer.daily);
  const monthly = parse(answer.monthly);
  if (Number.isNaN(daily) || Number.isNaN(monthly)) {
    toast("Enter a whole number of tokens, 'unlimited', or leave it blank.", "error"); return;
  }

  const body = daily === undefined && monthly === undefined
    ? { clear: true }
    : { daily_tokens: daily === undefined ? null : daily,
        monthly_tokens: monthly === undefined ? null : monthly };
  const res = await api(`/api/admin/users/${encodeURIComponent(name)}/limits`, {
    method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  toast(res.ok ? (body.clear ? `${name} follows the tier default again.` : `Limit updated for ${name}.`)
              : await errorText(res), res.ok ? "ok" : "error");
  loadUsers();
}

/** The tier defaults, as a form. These live in data/auth/limits.json rather
 *  than in the environment so that raising a cap is an admin action, not a
 *  deployment. */
async function renderLimitsEditor() {
  const host = document.getElementById("limitsEditor");
  setBusy(host);
  const r = await apiJson("/api/admin/limits", {}, null);
  if (!r) { host.innerHTML = `<p class="status-line error">Could not load the quota policy.</p>`; return; }
  const cfg = r.config || {};
  const rowFor = (tier, label, note) => {
    const t = (cfg.tiers || {})[tier] || {};
    return `<tr>
      <td><b>${escapeHtml(label)}</b><div class="muted">${note}</div></td>
      <td><input id="lim_${tier}_daily" class="num-input" value="${t.daily_tokens ?? ""}"
            placeholder="unlimited"></td>
      <td><input id="lim_${tier}_monthly" class="num-input" value="${t.monthly_tokens ?? ""}"
            placeholder="unlimited"></td>
    </tr>`;
  };
  host.innerHTML = `
    <table class="mini-table limits-table">
      <thead><tr><th>Tier</th><th>Daily tokens</th><th>Rolling-window tokens</th></tr></thead>
      <tbody>
        ${rowFor("unverified", "Unverified", "Self-registered, not yet vouched for by an admin")}
        ${rowFor("verified", "Verified", "Created by an admin, or verified by one afterwards")}
      </tbody>
    </table>
    <div class="limits-window">
      <label for="limWindow">Rolling window</label>
      <input id="limWindow" class="num-input" value="${cfg.window_days ?? 30}"> days
      <span class="muted">1-90. Rolling rather than calendar-month, so an allowance does not
        arrive in a lump and run out mid-month.</span>
    </div>
    <button class="primary" onclick="saveLimits()">Save limits</button>
    <div id="limitsStatus" class="status-line">${cfg.updated_at
      ? `Last changed ${escapeHtml(fmtWhen(cfg.updated_at))}${
          cfg.updated_by ? ` by ${escapeHtml(cfg.updated_by)}` : ""}.`
      : "Currently on the built-in defaults."}</div>`;
}

async function saveLimits() {
  const statusEl = document.getElementById("limitsStatus");
  const read = (id) => {
    const v = (document.getElementById(id).value || "").trim().replace(/[,_\s]/g, "");
    if (!v) return null;                             // blank = unlimited
    const n = parseInt(v, 10);
    return Number.isFinite(n) ? n : NaN;
  };
  const tiers = {};
  for (const tier of ["unverified", "verified"]) {
    const daily = read(`lim_${tier}_daily`);
    const monthly = read(`lim_${tier}_monthly`);
    if (Number.isNaN(daily) || Number.isNaN(monthly)) {
      statusEl.textContent = "Limits must be whole numbers of tokens, or blank for unlimited.";
      statusEl.className = "status-line error"; return;
    }
    tiers[tier] = { daily_tokens: daily, monthly_tokens: monthly };
  }
  const windowDays = parseInt((document.getElementById("limWindow").value || "30").trim(), 10);
  const res = await api("/api/admin/limits", {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ tiers, window_days: Number.isFinite(windowDays) ? windowDays : 30 }),
  });
  if (!res.ok) { statusEl.textContent = await errorText(res); statusEl.className = "status-line error"; return; }
  toast("Quota limits saved. They apply to the next question anyone asks.", "ok");
  loadUsers();
}

async function setUserRole(name, role) {
  const res = await api(`/api/admin/users/${encodeURIComponent(name)}/role`, {
    method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ role }),
  });
  toast(res.ok ? `${name} is now ${role}.` : "Could not change that role.", res.ok ? "ok" : "error");
  loadUsers();
}

async function toggleDisabled(name, disabled) {
  const res = await api(`/api/admin/users/${encodeURIComponent(name)}/disabled`, {
    method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ disabled }),
  });
  toast(res.ok ? `${name} ${disabled ? "disabled" : "enabled"}.` : await errorText(res), res.ok ? "ok" : "error");
  loadUsers();
}

async function resetUserPassword(name) {
  const answer = await modal({
    title: `Reset password for ${name}`,
    body: "They can change it themselves after signing in.",
    fields: [{ name: "password", label: "New password (min 8 characters)", type: "text" }],
    submitLabel: "Reset",
  });
  if (!answer) return;
  const res = await api(`/api/admin/users/${encodeURIComponent(name)}/reset-password`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password: answer.password }),
  });
  toast(res.ok ? "Password reset." : await errorText(res), res.ok ? "ok" : "error");
}

async function deleteUser(name) {
  if (!await confirmModal(`Delete ${name}?`, "This also revokes every token they created.", "Delete")) return;
  const res = await api(`/api/admin/users/${encodeURIComponent(name)}`, { method: "DELETE" });
  toast(res.ok ? `${name} deleted.` : await errorText(res), res.ok ? "ok" : "error");
  loadUsers();
}

// ---------- boot ----------

async function boot() {
  const res = await fetch("/api/auth/me");   // raw: a 401 here is normal, not an expiry
  if (res.ok) {
    CURRENT_USER = await res.json();
    enterApp();
  } else {
    document.getElementById("loginOverlay").style.display = "flex";
    // Only asked for when the login screen is actually shown: a signed-in
    // user has no use for it, and it is the one unauthenticated GET here.
    loadSignupConfig();
  }
}
boot();

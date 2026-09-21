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

/** modal({title, body, fields:[{name,label,type,placeholder,value}], submitLabel})
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
                   placeholder="${escapeHtml(f.placeholder || "")}" value="${escapeHtml(f.value || "")}">
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
    if (first) first.focus(); else back.querySelector("[type=submit]").focus();
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

/** Fill a <tbody>, with a proper empty state instead of a blank void. */
function fillTable(tbody, rows, colspan, emptyMessage, rowFn) {
  tbody.innerHTML = "";
  if (!rows || !rows.length) {
    tbody.innerHTML = `<tr class="empty-row"><td colspan="${colspan}">${escapeHtml(emptyMessage)}</td></tr>`;
    return;
  }
  rows.forEach(r => tbody.appendChild(rowFn(r)));
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
function showView(name) {
  document.querySelectorAll("nav button").forEach(b => b.classList.toggle("active", b.dataset.view === name));
  document.querySelectorAll(".view").forEach(v => v.classList.toggle("active", v.id === `view-${name}`));
  if (name === "dashboard") loadDashboard();
  if (name === "incidents") loadIncidents();
  if (name === "known") loadKnownIssues();
  if (name === "logs") loadLogs();
  if (name === "tokens") loadTokens();
  if (name === "usage") loadUsageView();
  if (name === "admin") loadUsers();
}

function renderOrgPicker() {
  const el = document.getElementById("orgPicker");
  const ids = Object.keys(ORGS);
  if (!ids.length) { el.textContent = "No orgs connected yet"; return; }
  if (!CURRENT_ORG || !ORGS[CURRENT_ORG]) CURRENT_ORG = ids[0];
  el.innerHTML = "Active org: <select id='orgSelect'></select>";
  const sel = document.getElementById("orgSelect");
  ids.forEach(id => {
    const opt = document.createElement("option");
    opt.value = id; opt.textContent = `${id} (${ORGS[id].name})`;
    if (id === CURRENT_ORG) opt.selected = true;
    sel.appendChild(opt);
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
  const limitKeys = Object.keys(limits);
  if (limitKeys.length) {
    parts.push(`<h3>Governor limits at the end of the transaction</h3><div class="limits">` +
      limitKeys.map(k => {
        const v = limits[k];
        // Values look like {used, limit} or "12 out of 100" depending on the log.
        let used = null, cap = null;
        if (v && typeof v === "object") { used = v.used; cap = v.limit; }
        const pct = (used != null && cap) ? Math.round((used / cap) * 100) : null;
        const cls = pct === null ? "" : pct >= 90 ? "high" : pct >= 70 ? "medium" : "low";
        return `<div class="limit ${cls}"><span>${escapeHtml(k)}</span>
          <b>${used != null ? `${used}/${cap}` : escapeHtml(String(v))}</b>
          ${pct !== null ? `<i>${pct}%</i>` : ""}</div>`;
      }).join("") + `</div>`);
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
  const tbody = document.getElementById("orgsTable");
  fillTable(tbody, Object.entries(ORGS), 10,
    "No orgs you can see yet. Connect one above, or ask a colleague to make theirs public.",
    ([id, o]) => {
      const c = o.component_counts || {};
      const tr = document.createElement("tr");
      tr.innerHTML = `<td><a class="link" onclick="setActiveOrg('${escapeHtml(id)}'); showView('dashboard')">${escapeHtml(id)}</a></td>
        <td>${escapeHtml(o.name)}</td><td>${visibilityCell(id, o)}</td>
        <td>${o.owner ? escapeHtml(o.owner) : "<span class='muted'>(none)</span>"}</td>
        <td>${c.apex_classes ?? "-"}</td><td>${c.apex_triggers ?? "-"}</td>
        <td>${c.flows ?? "-"}</td><td>${c.lwc_components ?? "-"}</td>
        <td>${fmtWhen(o.last_extracted_at)}${changesHint(o)}</td>
        <td>${o.can_manage ? `<button class="secondary" onclick="event.stopPropagation(); refreshOrg('${escapeHtml(id)}')">Refresh</button>` : ""}</td>`;
      return tr;
    });
  // A fetch someone else started should be visible here, not just in the
  // panel of whoever clicked the button -- otherwise a colleague sees an org
  // with stale counts and no clue that it is mid-refresh, and reaches for the
  // Refresh button that will now be rejected.
  markInFlightOrgs();
}

async function markInFlightOrgs() {
  for (const id of Object.keys(ORGS)) {
    const s = await apiJson(`/api/orgs/${encodeURIComponent(id)}/status`, {}, null);
    if (!s || ["done", "error", "unknown"].includes(s.status)) continue;
    const cell = [...document.querySelectorAll("#orgsTable tr")]
      .find(tr => tr.querySelector("a.link")?.textContent === id)?.cells[8];
    if (!cell) continue;
    cell.innerHTML = `<div class="muted">${escapeHtml(s.step_label || s.status)} &mdash; ${
      s.percent || 0}%</div>
      <div class="progress-track" style="height:5px; margin-top:4px;">
        <div class="progress-fill" style="width:${s.percent || 0}%"></div></div>`;
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

// Owner/admin get a live dropdown to flip an org public <-> private;
// everyone else just sees the current state as a badge.
function visibilityCell(id, o) {
  const vis = o.visibility || "public";
  if (!o.can_manage) return `<span class="badge visibility-${vis}">${vis}</span>`;
  return `<select class="vis-select" onclick="event.stopPropagation()" onchange="setOrgVisibility('${escapeHtml(id)}', this.value, this)">
      <option value="private" ${vis === "private" ? "selected" : ""}>private</option>
      <option value="public" ${vis === "public" ? "selected" : ""}>public</option>
    </select>`;
}

async function setOrgVisibility(id, visibility, el) {
  const previous = ORGS[id] ? ORGS[id].visibility : null;
  if (el) el.disabled = true;
  const res = await api(`/api/orgs/${encodeURIComponent(id)}/visibility`, {
    method: "PATCH", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ visibility }),
  });
  if (el) el.disabled = false;
  if (!res.ok) {
    toast("Could not change visibility: " + await errorText(res), "error");
    if (el && previous) el.value = previous;
    return;
  }
  toast(`${id} is now ${visibility}.`, "ok");
  await loadOrgs();
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
  const statusEl = document.getElementById("createStatus");
  if (!org_id || !org_name || !instance_url || !access_token) {
    statusEl.textContent = "All fields are required."; statusEl.className = "status-line error"; return;
  }
  statusEl.textContent = "Queued..."; statusEl.className = "status-line";
  const res = await api("/api/orgs", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ org_id, org_name, instance_url, access_token, visibility }),
  });
  if (!res.ok) {
    statusEl.textContent = "Failed to queue: " + await errorText(res);
    statusEl.className = "status-line error"; return;
  }
  document.getElementById("newAccessToken").value = "";  // don't leave a token sitting in the DOM
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
    toast(`${org_id} is still being fetched. The Connections table will update when it finishes.`, "info");
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
      const cls = s.status === "done" || n < current ? "done" : n === current ? "active" : "";
      const mark = s.status === "done" || n < current ? "&#10003;" : n === current ? "&#9679;" : "&#9675;";
      return `<li class="${cls}"><span class="progress-mark">${mark}</span>${escapeHtml(step.label)}</li>`;
    }).join("");
  }

  const countsHost = document.getElementById("pgCounts");
  if (countsHost) {
    const labels = { objects: "objects", classes: "Apex classes", triggers: "triggers",
                     flows: "flows", lwc: "LWC bundles",
                     workflow_field_updates: "field updates", components: "components" };
    const entries = Object.entries(s.counts || {}).filter(([, v]) => v != null);
    countsHost.innerHTML = entries.length
      ? entries.map(([k, v]) => `<span class="count-pill"><b>${v}</b> ${escapeHtml(labels[k] || k)}</span>`).join("")
      : "";
  }
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
    el.innerHTML = `<p class="muted">Connect an org on the Connections tab first.</p>`;
    return;
  }
  document.getElementById("dashOrgTitle").textContent = `Org stats -- ${CURRENT_ORG}`;
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
  fillTable(tbody, incidents, 4, "No incidents filed for this org yet.", m => {
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
  if (res.ok) { toast("Resolution saved to the known-issues library.", "ok"); loadKnownIssues(); }
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
      : `<p class="muted">Nothing yet. Every incident you file with an exception or a suspect field
         adds its signature here, and any resolution you record shows up alongside it.</p>`;
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
  loadKnownIssues();
}

// ---------- log normalizer (org-independent) ----------

let CURRENT_NORMALIZED = null;
let CURRENT_NORMALIZED_NAME = "normalized_log";

async function normalizeLog() {
  const fileInput = document.getElementById("logFile");
  const statusEl = document.getElementById("logStatus");
  if (!fileInput.files.length) {
    statusEl.textContent = "Choose a debug log file first."; statusEl.className = "status-line error"; return;
  }
  const label = document.getElementById("logLabel").value.trim();
  const store = document.getElementById("logStore").checked;
  const form = new FormData();
  form.append("log_file", fileInput.files[0]);
  if (label) form.append("label", label);
  form.append("store", store ? "true" : "false");

  const sizeMb = (fileInput.files[0].size / 1048576).toFixed(1);
  statusEl.textContent = `Normalizing ${sizeMb} MB...`; statusEl.className = "status-line";
  setBusy("logResult", "Parsing the log...");
  const res = await api("/api/logs/normalize", { method: "POST", body: form });
  if (!res.ok) {
    statusEl.textContent = "Failed: " + await errorText(res);
    statusEl.className = "status-line error";
    document.getElementById("logResult").innerHTML = "";
    return;
  }
  const data = await res.json();

  CURRENT_NORMALIZED = data.normalized_log;
  CURRENT_NORMALIZED_NAME = data.log_id || label || (fileInput.files[0].name.replace(/\.[^.]+$/, "")) || "normalized_log";
  document.getElementById("logResultActions").style.display = "block";
  const n = data.normalized_log;
  const excCount = (n.exceptions || []).length;
  statusEl.textContent = "Normalized." + (data.stored ? ` Stored as ${data.log_id}.` : "")
    + ` ${excCount} exception(s), ${(n.execution_units || []).length} execution unit(s).`;
  statusEl.className = "status-line ok";
  document.getElementById("logResult").innerHTML =
    renderNormalizedLog(n) + collapsibleJson("Normalized JSON", n);
  if (data.stored) loadLogs();
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

async function loadLogs() {
  const tbody = document.getElementById("logsTable");
  const logs = await apiJson("/api/logs", {}, []) || [];
  fillTable(tbody, logs, 5, "No stored logs yet. Tick “Store in the library” above to keep one.", m => {
    const tr = document.createElement("tr");
    tr.onclick = () => showLogDetail(m.log_id);
    tr.innerHTML = `<td>${fmtWhen(m.timestamp)}</td><td>${escapeHtml(m.log_id)}</td>
      <td>${escapeHtml(m.top_exception || "-")}</td><td>${m.exception_count}</td>
      <td><button class="secondary" onclick="event.stopPropagation(); showLogDetail('${escapeHtml(m.log_id)}')">View</button></td>`;
    return tr;
  });
}

async function showLogDetail(logId) {
  const card = document.getElementById("logDetailCard");
  card.style.display = "block";
  setBusy("logDetail");
  card.scrollIntoView({ behavior: "smooth" });
  const data = await apiJson(`/api/logs/${encodeURIComponent(logId)}`, {}, null);
  if (!data) { document.getElementById("logDetail").innerHTML = `<p class="muted">Could not load that log.</p>`; return; }
  window._logDetail = data.normalized_log;
  document.getElementById("logDetailActions").innerHTML =
    `<button class="secondary" onclick="downloadBlob(window._logDetail, '${escapeHtml(logId)}.normalized.json')">Download normalized JSON</button>`;
  document.getElementById("logDetail").innerHTML =
    `<p class="muted mono">${escapeHtml(logId)}</p>` + renderNormalizedLog(data.normalized_log)
    + collapsibleJson("Normalized JSON", data.normalized_log);
}

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
  document.getElementById("loginOverlay").style.display = "flex";
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
  document.getElementById("navUsage").style.display = role === "admin" ? "" : "none";
  // Hide write-only cards for readers (the server enforces this too).
  const canWrite = role === "user" || role === "admin";
  document.querySelectorAll('[data-requires="user"]').forEach(el => {
    el.style.display = canWrite ? "" : "none";
  });
}

function enterApp() {
  document.getElementById("loginOverlay").style.display = "none";
  document.getElementById("appRoot").style.display = "";
  applyRole();
  loadOrgs();
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
  await Promise.all([renderLlmStatusPanel(), populateUsageUserPicker()]);
  await loadUsage();
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
function renderUsageTrend(r) {
  const host = document.getElementById("usageTrend");
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
  fillTable(tbody, apiTokens, 7, "No API tokens yet. Create one above to point the MCP server at this app.", t => {
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
  const tbody = document.getElementById("usersTable");
  const users = await apiJson("/api/admin/users", {}, {}) || {};
  fillTable(tbody, Object.entries(users), 5, "No users.", ([name, u]) => {
    const tr = document.createElement("tr");
    const isSelf = CURRENT_USER && name === CURRENT_USER.username;
    const roleSel = `<select onchange="setUserRole('${escapeHtml(name)}', this.value)">
      ${["reader", "user", "admin"].map(r => `<option value="${r}" ${u.role === r ? "selected" : ""}>${r}</option>`).join("")}
    </select>`;
    tr.innerHTML = `<td>${escapeHtml(name)}${isSelf ? ' <span class="muted">(you)</span>' : ""}</td>
      <td>${roleSel}</td>
      <td>${u.disabled ? "<span style='color:var(--conga-color-status-error)'>disabled</span>" : "active"}</td>
      <td>${fmtWhen(u.created_at)}</td>
      <td>
        <button class="secondary" onclick="resetUserPassword('${escapeHtml(name)}')">Reset password</button>
        ${isSelf ? "" : `<button class="secondary" onclick="toggleDisabled('${escapeHtml(name)}', ${!u.disabled})">${u.disabled ? "Enable" : "Disable"}</button>
        <button class="secondary" onclick="deleteUser('${escapeHtml(name)}')">Delete</button>`}
      </td>`;
    return tr;
  });
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
  }
}
boot();

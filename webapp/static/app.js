let CURRENT_ORG = null;
let ORGS = {};

// ---------- nav ----------
document.querySelectorAll("nav button").forEach(btn => {
  btn.addEventListener("click", () => showView(btn.dataset.view));
});
function showView(name) {
  document.querySelectorAll("nav button").forEach(b => b.classList.toggle("active", b.dataset.view === name));
  document.querySelectorAll(".view").forEach(v => v.classList.toggle("active", v.id === `view-${name}`));
  if (name === "dashboard") loadDashboard();
  if (name === "incidents") loadIncidents();
  if (name === "logs") loadLogs();
  if (name === "tokens") loadTokens();
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
  sel.addEventListener("change", () => { CURRENT_ORG = sel.value; loadDashboard(); loadIncidents(); });
}

// ---------- connections ----------
async function loadOrgs() {
  const res = await fetch("/api/orgs");
  ORGS = await res.json();
  renderOrgPicker();
  const tbody = document.getElementById("orgsTable");
  tbody.innerHTML = "";
  Object.entries(ORGS).forEach(([id, o]) => {
    const c = o.component_counts || {};
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${id}</td><td>${o.name}</td><td>${c.apex_classes ?? "-"}</td>
      <td>${c.apex_triggers ?? "-"}</td><td>${c.flows ?? "-"}</td><td>${c.lwc_components ?? "-"}</td>
      <td>${o.last_extracted_at ?? "-"}</td>`;
    tbody.appendChild(tr);
  });
}

async function createOrg() {
  const org_id = document.getElementById("newOrgId").value.trim();
  const org_name = document.getElementById("newOrgName").value.trim();
  const instance_url = document.getElementById("newInstanceUrl").value.trim();
  const access_token = document.getElementById("newAccessToken").value.trim();
  const statusEl = document.getElementById("createStatus");
  if (!org_id || !org_name || !instance_url || !access_token) {
    statusEl.textContent = "All fields are required."; statusEl.className = "status-line error"; return;
  }
  statusEl.textContent = "Queued..."; statusEl.className = "status-line";
  const res = await fetch("/api/orgs", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ org_id, org_name, instance_url, access_token }),
  });
  if (!res.ok) { statusEl.textContent = "Failed to queue: " + (await res.text()); statusEl.className = "status-line error"; return; }
  pollOrgStatus(org_id);
}

async function pollOrgStatus(org_id) {
  const statusEl = document.getElementById("createStatus");
  const res = await fetch(`/api/orgs/${org_id}/status`);
  const s = await res.json();
  if (s.status === "error") {
    statusEl.textContent = "Error: " + s.detail; statusEl.className = "status-line error"; return;
  }
  if (s.status === "done") {
    statusEl.textContent = "Done. Knowledgebase built." + (s.warnings?.length ? ` (${s.warnings.length} warning(s) -- see server log)` : "");
    statusEl.className = "status-line ok";
    await loadOrgs();
    return;
  }
  statusEl.textContent = `Working... (${s.status})`;
  setTimeout(() => pollOrgStatus(org_id), 1500);
}

// ---------- dashboard ----------
async function loadDashboard() {
  if (!CURRENT_ORG) return;
  document.getElementById("dashOrgTitle").textContent = `Org stats -- ${CURRENT_ORG}`;
  const res = await fetch(`/api/orgs/${CURRENT_ORG}/stats`);
  const el = document.getElementById("dashStats");
  if (!res.ok) { el.innerHTML = "<p class='muted'>No knowledgebase yet for this org.</p>"; return; }
  const s = await res.json();
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
    <div><b>Fields with a high-risk writer:</b> ${s.fields_with_high_risk_writes.map(f => `<span class="pill">${f}</span>`).join(" ") || "none"}</div>
    <div><b>Fields written by Flow/PB/Workflow automation:</b> ${(s.fields_written_by_declarative_automation || []).length}</div>
  `;
}

async function runSearch() {
  const q = document.getElementById("searchBox").value.trim();
  if (!q || !CURRENT_ORG) return;
  const res = await fetch(`/api/orgs/${CURRENT_ORG}/search?q=${encodeURIComponent(q)}`);
  const data = await res.json();
  const el = document.getElementById("searchResults");
  const section = (title, items, onClick) => items.length
    ? `<p><b>${title}:</b> ${items.map(i => `<span class="pill link" onclick="${onClick}('${i}')">${i}</span>`).join(" ")}</p>`
    : "";
  el.innerHTML = section("Components", data.components, "showComponent")
    + section("Objects touched", data.objects, "showObjectTouch")
    + section("Fields", data.fields, "showFieldWriters");
}

async function showComponent(id) {
  const res = await fetch(`/api/orgs/${CURRENT_ORG}/components/${encodeURIComponent(id)}`);
  const card = await res.json();
  document.getElementById("searchResults").innerHTML += `<pre>${JSON.stringify(card, null, 2)}</pre>`;
}
async function showObjectTouch(name) {
  const res = await fetch(`/api/orgs/${CURRENT_ORG}/object-touch/${encodeURIComponent(name)}`);
  const data = await res.json();
  document.getElementById("searchResults").innerHTML += `<pre>${JSON.stringify(data, null, 2)}</pre>`;
}
function showFieldWriters(name) {
  document.getElementById("fieldWriterBox").value = name;
  findFieldWriters();
}

async function findFieldWriters() {
  const field = document.getElementById("fieldWriterBox").value.trim();
  if (!field || !CURRENT_ORG) return;
  const res = await fetch(`/api/orgs/${CURRENT_ORG}/field-writers/${encodeURIComponent(field)}`);
  const data = await res.json();
  const el = document.getElementById("fieldWriterResults");
  if (!data.writers || !data.writers.length) {
    el.innerHTML = "<p class='muted'>No tracked writers for this field.</p>"; return;
  }
  const order = ["Apex", "Flow", "Process Builder", "Workflow/Approval field update"];
  const groups = {};
  data.writers.forEach(w => { (groups[w.mechanism || "Apex"] = groups[w.mechanism || "Apex"] || []).push(w); });
  const mechs = Object.keys(groups).sort((a, b) => (order.indexOf(a) + 1 || 99) - (order.indexOf(b) + 1 || 99));

  el.innerHTML = mechs.map(mech => `
    <h3 style="margin:14px 0 6px; font-size:14px;">${mech} <span class="muted">(${groups[mech].length})</span></h3>
    ` + groups[mech].map(w => `
      <div style="margin:8px 0; padding:10px; border:1px solid var(--border); border-radius:4px;">
        <span class="badge ${w.risk}">${w.risk.toUpperCase()}</span> <b>${w.component}</b>
        ${w.object ? `<span class="muted"> on ${w.object}</span>` : ""}
        ${w.last_changed ? `<span class="muted"> -- last changed ${w.last_changed} (${w.age_days}d ago)</span>` : ""}
        ${w.reason ? `<div class="muted">${w.reason}</div>` : ""}
        <div class="muted">value: ${field} = ${w.example ?? "(unavailable)"}</div>
      </div>`).join("")).join("");
}

// ---------- incidents ----------
async function fileIncident() {
  if (!CURRENT_ORG) return;
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
  const res = await fetch(`/api/orgs/${CURRENT_ORG}/incidents`, { method: "POST", body: form });
  if (!res.ok) { statusEl.textContent = "Failed: " + (await res.text()); statusEl.className = "status-line error"; return; }
  const data = await res.json();
  const m = data.meta;
  statusEl.textContent = m.recurrence
    ? `RECURRENCE -- seen ${m.prior_occurrences} time(s) before.` + (m.prior_resolution ? ` Resolution on file: ${m.prior_resolution}` : " No resolution on file yet.")
    : (m.signature ? "NEW ISSUE filed." : "Filed (no signature -- no exception and no field given).");
  statusEl.className = "status-line ok";
  loadIncidents();
}

async function loadIncidents() {
  if (!CURRENT_ORG) return;
  const res = await fetch(`/api/orgs/${CURRENT_ORG}/incidents`);
  const incidents = res.ok ? await res.json() : [];
  const tbody = document.getElementById("incidentsTable");
  tbody.innerHTML = "";
  incidents.forEach(m => {
    const tr = document.createElement("tr");
    tr.onclick = () => showIncidentDetail(m.incident_id);
    const badge = m.recurrence ? `<span class="badge recurrence">RECURRENCE</span>` : `<span class="badge new">NEW</span>`;
    tr.innerHTML = `<td>${m.timestamp}</td><td>${m.incident_id}</td><td>${badge}</td><td>${m.suspect_field || ""}</td>`;
    tbody.appendChild(tr);
  });
}

async function showIncidentDetail(incidentId) {
  const res = await fetch(`/api/orgs/${CURRENT_ORG}/incidents/${encodeURIComponent(incidentId)}`);
  const data = await res.json();
  const card = document.getElementById("incidentDetailCard");
  card.style.display = "block";
  const m = data.meta;
  let resolveBtn = "";
  if (m.signature) {
    resolveBtn = `
      <label>Record resolution for signature ${m.signature}</label>
      <textarea id="resolutionText" rows="2"></textarea>
      <button class="secondary" onclick="recordResolution('${m.signature}')">Save resolution</button>
      <div id="resolveStatus" class="status-line"></div>`;
  }
  document.getElementById("incidentDetail").innerHTML = `
    <p><b>${m.incident_id}</b> -- ${m.recurrence ? "RECURRENCE" : "NEW"} ${m.signature ? `(signature ${m.signature})` : ""}</p>
    <h3>RCA context pack</h3>
    <pre>${JSON.stringify(data.rca_context_pack, null, 2)}</pre>
    ${resolveBtn}
  `;
  card.scrollIntoView({ behavior: "smooth" });
}

async function recordResolution(signature) {
  const resolution = document.getElementById("resolutionText").value.trim();
  const statusEl = document.getElementById("resolveStatus");
  if (!resolution) { statusEl.textContent = "Enter a resolution first."; statusEl.className = "status-line error"; return; }
  const res = await fetch(`/api/orgs/${CURRENT_ORG}/resolve`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ signature, resolution }),
  });
  statusEl.textContent = res.ok ? "Resolution saved." : "Failed to save.";
  statusEl.className = res.ok ? "status-line ok" : "status-line error";
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

  statusEl.textContent = "Normalizing..."; statusEl.className = "status-line";
  const res = await fetch("/api/logs/normalize", { method: "POST", body: form });
  if (!res.ok) { statusEl.textContent = "Failed: " + (await res.text()); statusEl.className = "status-line error"; return; }
  const data = await res.json();

  CURRENT_NORMALIZED = data.normalized_log;
  CURRENT_NORMALIZED_NAME = data.log_id || label || (fileInput.files[0].name.replace(/\.[^.]+$/, "")) || "normalized_log";
  document.getElementById("logResultActions").style.display = "block";
  const n = data.normalized_log;
  const excCount = (n.exceptions || []).length;
  statusEl.textContent = "Normalized." + (data.stored ? ` Stored as ${data.log_id}.` : "")
    + ` ${excCount} exception(s), ${(n.execution_units || []).length} execution unit(s).`;
  statusEl.className = "status-line ok";
  document.getElementById("logResult").innerHTML = `<pre>${JSON.stringify(n, null, 2)}</pre>`;
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
  const res = await fetch("/api/logs");
  const logs = res.ok ? await res.json() : [];
  const tbody = document.getElementById("logsTable");
  tbody.innerHTML = "";
  logs.forEach(m => {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${m.timestamp}</td><td>${m.log_id}</td><td>${m.top_exception || "-"}</td>
      <td>${m.exception_count}</td>
      <td><button class="secondary" onclick="event.stopPropagation(); showLogDetail('${m.log_id}')">View</button></td>`;
    tbody.appendChild(tr);
  });
}

async function showLogDetail(logId) {
  const res = await fetch(`/api/logs/${encodeURIComponent(logId)}`);
  if (!res.ok) return;
  const data = await res.json();
  const card = document.getElementById("logDetailCard");
  card.style.display = "block";
  document.getElementById("logDetailActions").innerHTML =
    `<button class="secondary" onclick="downloadBlob(window._logDetail, '${logId}.normalized.json')">Download normalized JSON</button>`;
  window._logDetail = data.normalized_log;
  document.getElementById("logDetail").innerHTML =
    `<p class="muted">${logId}</p><pre>${JSON.stringify(data.normalized_log, null, 2)}</pre>`;
  card.scrollIntoView({ behavior: "smooth" });
}

// ---------- auth ----------
let CURRENT_USER = null;

async function doLogin() {
  const username = document.getElementById("loginUser").value.trim();
  const password = document.getElementById("loginPass").value;
  const statusEl = document.getElementById("loginStatus");
  if (!username || !password) { statusEl.textContent = "Enter username and password."; statusEl.className = "status-line error"; return; }
  statusEl.textContent = "Signing in..."; statusEl.className = "status-line";
  const res = await fetch("/api/auth/login", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (!res.ok) {
    const t = await res.json().catch(() => ({}));
    statusEl.textContent = t.detail || "Sign in failed."; statusEl.className = "status-line error"; return;
  }
  CURRENT_USER = await res.json();
  document.getElementById("loginPass").value = "";
  enterApp();
}

async function doLogout() {
  await fetch("/api/auth/logout", { method: "POST" });
  CURRENT_USER = null;
  document.getElementById("appRoot").style.display = "none";
  document.getElementById("loginOverlay").style.display = "flex";
}

function applyRole() {
  const role = CURRENT_USER.role;
  document.getElementById("userInfo").innerHTML =
    `${CURRENT_USER.username}<span class="role-tag">${role}</span>`;
  document.getElementById("navAdmin").style.display = role === "admin" ? "" : "none";
  // Hide write-only cards for readers (server enforces this too).
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
}

// ---------- API tokens ----------
async function createToken() {
  const label = document.getElementById("tokenLabel").value.trim();
  const ttlRaw = document.getElementById("tokenTtl").value.trim();
  const body = {};
  if (label) body.label = label;
  if (ttlRaw) body.ttl_days = parseInt(ttlRaw, 10);
  const res = await fetch("/api/tokens", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const el = document.getElementById("tokenCreateResult");
  if (!res.ok) { el.innerHTML = `<div class="status-line error">Failed to create token.</div>`; return; }
  const data = await res.json();
  el.innerHTML = `<div class="token-reveal"><b>Copy this token now (role: ${data.role}):</b><br>${data.token}
    <div class="muted" style="margin-top:6px;">It won't be shown again. Put it in the MCP server's
    <code>TS_DEBUG_HELPER_TOKEN</code> env variable.</div></div>`;
  document.getElementById("tokenLabel").value = "";
  document.getElementById("tokenTtl").value = "";
  loadTokens();
}

async function loadTokens() {
  const res = await fetch("/api/tokens");
  const tokens = res.ok ? await res.json() : [];
  const tbody = document.getElementById("tokensTable");
  tbody.innerHTML = "";
  tokens.forEach(t => {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${t.label || "(none)"}</td><td>${t.role}</td><td>${t.username}</td>
      <td>${t.created_at || "-"}</td><td>${t.expires_at || "never"}</td><td>${t.last_used || "never"}</td>
      <td><button class="secondary" onclick="revokeToken('${t.id}')">Revoke</button></td>`;
    tbody.appendChild(tr);
  });
}

async function revokeToken(id) {
  await fetch(`/api/tokens/${id}`, { method: "DELETE" });
  loadTokens();
}

// ---------- admin: users ----------
async function createUser() {
  const username = document.getElementById("admUser").value.trim();
  const password = document.getElementById("admPass").value;
  const role = document.getElementById("admRole").value;
  const statusEl = document.getElementById("admStatus");
  const res = await fetch("/api/admin/users", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password, role }),
  });
  if (!res.ok) {
    const t = await res.json().catch(() => ({}));
    statusEl.textContent = t.detail || "Failed."; statusEl.className = "status-line error"; return;
  }
  statusEl.textContent = `Created ${username} (${role}).`; statusEl.className = "status-line ok";
  document.getElementById("admUser").value = "";
  document.getElementById("admPass").value = "";
  loadUsers();
}

async function loadUsers() {
  const res = await fetch("/api/admin/users");
  if (!res.ok) return;
  const users = await res.json();
  const tbody = document.getElementById("usersTable");
  tbody.innerHTML = "";
  Object.entries(users).forEach(([name, u]) => {
    const isSelf = CURRENT_USER && name === CURRENT_USER.username;
    const roleSel = `<select onchange="setUserRole('${name}', this.value)">
      ${["reader", "user", "admin"].map(r => `<option value="${r}" ${u.role === r ? "selected" : ""}>${r}</option>`).join("")}
    </select>`;
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${name}${isSelf ? " <span class='muted'>(you)</span>" : ""}</td>
      <td>${roleSel}</td>
      <td>${u.disabled ? "<span style='color:var(--high)'>disabled</span>" : "active"}</td>
      <td>${u.created_at || "-"}</td>
      <td>
        <button class="secondary" onclick="resetUserPassword('${name}')">Reset password</button>
        ${isSelf ? "" : `<button class="secondary" onclick="toggleDisabled('${name}', ${!u.disabled})">${u.disabled ? "Enable" : "Disable"}</button>
        <button class="secondary" onclick="deleteUser('${name}')">Delete</button>`}
      </td>`;
    tbody.appendChild(tr);
  });
}

async function setUserRole(name, role) {
  await fetch(`/api/admin/users/${name}/role`, {
    method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ role }),
  });
  loadUsers();
}

async function toggleDisabled(name, disabled) {
  await fetch(`/api/admin/users/${name}/disabled`, {
    method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ disabled }),
  });
  loadUsers();
}

async function resetUserPassword(name) {
  const pw = prompt(`New password for ${name} (min 8 chars):`);
  if (!pw) return;
  const res = await fetch(`/api/admin/users/${name}/reset-password`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ password: pw }),
  });
  alert(res.ok ? "Password reset." : "Failed (min 8 chars?).");
}

async function deleteUser(name) {
  if (!confirm(`Delete user ${name}? This also revokes their tokens.`)) return;
  await fetch(`/api/admin/users/${name}`, { method: "DELETE" });
  loadUsers();
}

// ---------- boot ----------
async function boot() {
  const res = await fetch("/api/auth/me");
  if (res.ok) {
    CURRENT_USER = await res.json();
    enterApp();
  } else {
    document.getElementById("loginOverlay").style.display = "flex";
  }
}
boot();

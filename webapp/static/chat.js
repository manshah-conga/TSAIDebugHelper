/* TS Intelligent Debug Helper -- chat dock.
 *
 * One entry point, mountChat(container, ctx), so the same transcript,
 * composer and tool-row rendering can live in the narrow right-hand dock
 * today and a full-width page later without a rewrite.
 *
 * Layout of this file:
 *   1. state + helpers
 *   2. key settings (the four states: absent / unlocked / locked / cleared)
 *   3. model picker
 *   4. transcript rendering  -- including the tool rows, which are the whole
 *      trust surface: an engineer has to see WHICH evidence produced a claim
 *   5. the SSE turn loop
 *   6. sharing
 *   7. mount + public API
 *
 * Everything server-facing goes through api()/apiJson() from app.js so an
 * expired session bounces to the login screen in one place, including
 * mid-stream.
 */

const CHAT = {
  chatId: null,
  meta: null,
  messages: [],
  keyState: null,
  models: [],
  model: null,
  streaming: false,
  abort: null,
  pendingConfirm: null,
  el: {},
  open: false,
};

// =====================================================================
// 1. helpers
// =====================================================================

function chatEl(tag, cls, html) {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  if (html !== undefined) el.innerHTML = html;
  return el;
}

function fmtCost(n) {
  if (n === null || n === undefined) return "";
  if (n === 0) return "$0";
  return n < 0.01 ? `$${n.toFixed(4)}` : `$${n.toFixed(3)}`;
}

function fmtTokens(n) {
  if (!n) return "0";
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);
}

/** Tool results arrive as a JSON string. Re-indent them for reading, but fall
 *  back to the raw text rather than showing nothing: a truncated result is
 *  deliberately not valid JSON any more, and that is exactly the case where an
 *  engineer most wants to see what the model actually got. */
function prettyJson(text) {
  if (typeof text !== "string") {
    try { return JSON.stringify(text, null, 2); } catch (e) { return String(text); }
  }
  try { return JSON.stringify(JSON.parse(text), null, 2); } catch (e) { return text; }
}

/** Minimal, safe markdown: escape first, then re-introduce only the four
 *  things a model reliably emits. Never innerHTML raw model output -- it is
 *  the one place a prompt-injected response could reach the DOM. */
function renderMarkdown(text) {
  let s = escapeHtml(text || "");
  s = s.replace(/```([\s\S]*?)```/g, (m, code) => `<pre class="chat-code">${code.trim()}</pre>`);
  s = s.replace(/`([^`\n]+)`/g, '<code>$1</code>');
  s = s.replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>");
  s = s.replace(/^### (.+)$/gm, "<h4>$1</h4>");
  s = s.replace(/^[-*] (.+)$/gm, "<li>$1</li>");
  s = s.replace(/(<li>[\s\S]*?<\/li>)(?!\s*<li>)/g, "<ul>$1</ul>");
  s = s.replace(/\n{2,}/g, "</p><p>");
  s = s.replace(/\n/g, "<br>");
  return `<p>${s}</p>`;
}

// =====================================================================
// 2. key settings
// =====================================================================

/** The EFFECTIVE state, not a personal key record.
 *
 *  This used to describe one thing: "do you have a key, and is it unlocked".
 *  Since the LLM connection moved to the server, that question no longer
 *  determines whether chat works -- almost every user has no key of their own
 *  and can chat perfectly well on the shared connection. The endpoint now
 *  returns `ready` / `using` / `can_manage` and this code reads those, so a
 *  working chat can never sit behind a chip that says "Connect an LLM". */
async function loadKeyState() {
  CHAT.keyState = await apiJson("/api/chat/key", {}, null);
  if (CHAT.keyState && !CHAT.model) CHAT.model = CHAT.keyState.default_model;
  return CHAT.keyState;
}

function llmReady() {
  return !!(CHAT.keyState && CHAT.keyState.ready);
}

function canManageLlm() {
  return !!(CHAT.keyState && CHAT.keyState.can_manage);
}

const PROVIDER_NAME = { openrouter: "OpenRouter", azure: "Azure OpenAI" };

/** What a NON-admin sees. Informational only, with no controls at all.
 *
 *  A user has nothing to configure and nothing to fix, so offering them a
 *  provider button would be offering a door that is locked from the other
 *  side. What they do need, when chat is not working, is to be told that it
 *  is a server-side matter and that the person to ask is an admin -- so that
 *  is the whole dialog. */
async function openLlmInfo() {
  const s = (await loadKeyState()) || {};
  const shared = s.shared || {};
  const ready = !!s.ready;

  let body;
  if (ready) {
    body = `<div class="key-status ok">Chat is connected through this server's shared
        <b>${escapeHtml(PROVIDER_NAME[s.provider] || s.provider || "LLM")}</b> connection.
        There is nothing for you to set up.</div>
      ${s.default_model ? `<p class="muted">Model: <code>${escapeHtml(s.default_model)}</code>${
        s.model_locked ? " &mdash; fixed by this server's configuration." : ""}</p>` : ""}`;
  } else if (shared.present_but_invalid) {
    body = `<div class="key-status warn">This server's LLM connection is configured but not
        working, so chat is unavailable. An administrator needs to correct it on the server.</div>
      <p class="muted">Reported problem: ${escapeHtml(shared.config_error || "unknown")}</p>`;
  } else {
    body = `<div class="key-status warn">This server has no LLM connection configured yet, so
        chat is unavailable. Ask an administrator to set one up.</div>
      <p class="muted">Everything else in the app &mdash; org knowledgebases, incidents, known
        issues and the log normalizer &mdash; works without it.</p>`;
  }

  await new Promise(resolve => {
    const back = chatEl("div", "modal-backdrop");
    back.innerHTML = `
      <div class="modal" role="dialog" aria-modal="true" style="width:460px;">
        <h3>LLM connection</h3>
        ${body}
        <p class="muted" style="margin-top:12px;">The LLM connection is managed centrally by
          administrators, so every signed-in account shares it. Your usage is recorded against
          your own account.</p>
        <div class="modal-actions">
          <button type="button" class="secondary" data-a="usage">My usage</button>
          <button type="button" class="primary" data-cancel>Close</button>
        </div>
      </div>`;
    const close = () => { document.removeEventListener("keydown", onKey); back.remove(); resolve(); };
    const onKey = e => { if (e.key === "Escape") close(); };
    back.querySelector("[data-cancel]").onclick = close;
    back.querySelector("[data-a='usage']").onclick = () => { close(); showMyUsage(); };
    back.onclick = e => { if (e.target === back) close(); };
    document.addEventListener("keydown", onKey);
    document.body.appendChild(back);
  });
}

/** Everyone can see their own consumption. On a shared key, "is it me
 *  burning the budget?" should not require asking an admin. */
async function showMyUsage() {
  const data = await apiJson("/api/usage/me?days=30", {}, null);
  if (!data) { toast("Could not load your usage.", "error"); return; }
  const t = data.totals || {};
  await modal({
    title: "Your usage (last 30 days)",
    body: `<table class="mini-table">
        <tr><td>Questions asked</td><td><b>${t.turns || 0}</b></td></tr>
        <tr><td>Tokens</td><td><b>${fmtTokens(t.total_tokens)}</b></td></tr>
        <tr><td>Cost</td><td><b>${t.cost_available ? fmtCost(t.cost)
          : "<span class='muted'>not reported by this provider</span>"}</b></td></tr>
        <tr><td>Tool calls</td><td><b>${t.tool_calls || 0}</b></td></tr>
        <tr><td>Average answer time</td><td><b>${t.avg_seconds_per_turn || 0}s</b></td></tr>
      </table>`,
    fields: [], submitLabel: "Close",
  });
}

/** The ADMIN dialog. Two clearly separated halves, because they are governed
 *  differently and conflating them is what made the old single "LLM settings"
 *  screen misleading:
 *
 *   - The SHARED connection is read-only here by design. It comes from the
 *     server's environment, which means changing it requires server access
 *     rather than a session -- so this panel reports what is loaded and names
 *     the variables to edit, instead of pretending to be a form.
 *   - A PERSONAL key is still fully editable, because it is genuinely this
 *     admin's own credential, encrypted under their own password.
 */
async function openKeySettings() {
  if (!canManageLlm()) return openLlmInfo();

  const s = (await loadKeyState()) || {};
  const shared = s.shared || {};
  const personal = s.personal || {};
  const env = shared.env_vars || {};

  let sharedStatus, sharedClass;
  if (shared.configured) {
    sharedClass = "ok";
    sharedStatus = `Serving every signed-in user through
      <b>${escapeHtml(PROVIDER_NAME[shared.provider] || shared.provider)}</b>` +
      (shared.hint ? ` with key <code>${escapeHtml(shared.hint)}</code>` : "") + `.` +
      (shared.endpoint ? `<div class="mono" style="font-size:11px; word-break:break-all; margin-top:4px;">${
        escapeHtml(shared.endpoint)}</div>` : "") +
      (shared.default_model ? `<div style="margin-top:4px;">Model:
        <code>${escapeHtml(shared.default_model)}</code>${
        shared.model_locked ? " (locked for non-admins)" : ""}</div>` : "") +
      (shared.verified_at ? `<div style="margin-top:4px;">Last successful call
        ${escapeHtml(fmtWhen(shared.verified_at))}.</div>` : "");
  } else if (shared.present_but_invalid) {
    sharedClass = "warn";
    sharedStatus = `Configured but <b>not usable</b>, so nobody can chat.<div style="margin-top:4px;">${
      escapeHtml(shared.config_error || "")}</div>`;
  } else {
    sharedClass = "warn";
    sharedStatus = `<b>Not configured.</b> Nobody on this server can chat until it is set.`;
  }

  let personalStatus;
  if (!personal.configured && personal.cleared_reason === "password_reset_by_admin") {
    personalStatus = `Your personal key was cleared when your password was reset &mdash; only
      that password could decrypt it. Add one again below if you still want it.`;
  } else if (!personal.configured) {
    personalStatus = `None. Your chats run on the shared connection above, which is usually
      what you want.`;
  } else if (!personal.unlocked) {
    personalStatus = `Saved but <b>locked</b> (normal after a server restart). Your chats are
      running on the shared connection meanwhile &mdash; nothing is broken.`;
  } else {
    personalStatus = `Active: <b>${escapeHtml(PROVIDER_NAME[personal.provider] || personal.provider)}</b>,
      key <code>${escapeHtml(personal.hint || "")}</code>. <b>Your</b> turns use this instead of
      the shared connection; everyone else still uses the shared one.`;
  }

  const choice = await new Promise(resolve => {
    const back = chatEl("div", "modal-backdrop");
    back.innerHTML = `
      <div class="modal" role="dialog" aria-modal="true" style="width:560px;">
        <h3>LLM connection</h3>

        <div class="llm-section-title">Shared server connection <span class="badge">all users</span></div>
        <div class="key-status ${sharedClass}">${sharedStatus}</div>
        <details class="raw-json" style="margin-top:8px;">
          <summary>How to change it</summary>
          <p class="muted">This connection is read from the server's environment on purpose: a
            credential that any signed-in session could rewrite is a credential a stolen session
            could rewrite. Changing it means editing the server's configuration and restarting
            the service &mdash; deliberate friction, for a rare action.</p>
          <pre>${escapeHtml(env.provider || "TS_LLM_PROVIDER")}=azure | openrouter
${escapeHtml(env.api_key || "TS_LLM_API_KEY")}=<the key>
${escapeHtml(env.endpoint || "TS_LLM_ENDPOINT")}=<Azure chat-completions URL, Azure only>
${escapeHtml(env.default_model || "TS_LLM_DEFAULT_MODEL")}=<optional, new chats start here>
${escapeHtml(env.lock_model || "TS_LLM_LOCK_MODEL")}=1   # optional: users cannot change model</pre>
          <p class="muted">On systemd, put these in the unit's EnvironmentFile (mode 0600) and
            run <code>systemctl restart ts-debug-helper</code>. The startup log line reports
            whether the connection loaded.</p>
        </details>

        <div class="llm-section-title" style="margin-top:18px;">Your personal key
          <span class="badge">just you, optional</span></div>
        <div class="key-status">${personalStatus}</div>
        ${personal.configured && !personal.unlocked
          ? `<button type="button" class="key-action primary-action" data-a="unlock">
               Unlock with your password</button>` : ""}
        <button type="button" class="key-action" data-a="azure">
          <b>${personal.configured ? "Replace with" : "Use"} an Azure OpenAI key</b>
          <span>Your own deployment. Only your own turns use it.</span>
        </button>
        <button type="button" class="key-action" data-a="openrouter">
          <b>${personal.configured ? "Replace with" : "Use"} an OpenRouter key</b>
          <span>Your own account and model catalogue. Only your own turns use it.</span>
        </button>
        ${personal.configured ? `<button type="button" class="key-action danger" data-a="remove">
          <b>Remove your personal key</b>
          <span>Your chats fall back to the shared connection. Conversations are kept.</span>
        </button>` : ""}
        <p class="muted" style="margin-top:12px;">A personal key is encrypted with your own
          password before it is stored, so no other admin can read it out of the server's files
          &mdash; and a password reset by someone else destroys it.</p>
        <div class="modal-actions">
          <button type="button" class="secondary" data-a="usage">Usage report</button>
          <button type="button" class="secondary" data-cancel>Close</button>
        </div>
      </div>`;
    const close = v => { document.removeEventListener("keydown", onKey); back.remove(); resolve(v); };
    const onKey = e => { if (e.key === "Escape") close(null); };
    back.querySelector("[data-cancel]").onclick = () => close(null);
    back.onclick = e => { if (e.target === back) close(null); };
    back.querySelectorAll("[data-a]").forEach(b => { b.onclick = () => close(b.dataset.a); });
    document.addEventListener("keydown", onKey);
    document.body.appendChild(back);
  });

  if (!choice) return;
  if (choice === "usage") { showView("usage"); toggleChatDock(false); return; }
  if (choice === "remove") { await removeKey(); return; }
  if (choice === "unlock") { await unlockKey(); return; }

  const provider = choice;                 // "azure" | "openrouter"
  const isAzure = provider === "azure";
  const fields = [];
  if (isAzure) {
    fields.push({
      name: "endpoint", label: "Azure chat completions URL (includes the deployment and api-version)",
      type: "text", value: personal.endpoint || "",
      placeholder: "https://<resource>.openai.azure.com/openai/deployments/gpt-4o/chat/completions?api-version=2024-08-01-preview",
    });
  }
  fields.push({
    name: "api_key",
    label: isAzure ? "Azure OpenAI API key" : "OpenRouter API key",
    type: "password",
    placeholder: isAzure ? "your resource key" : "sk-or-v1-...",
  });
  fields.push({ name: "password", label: "Your password (encrypts the key)", type: "password" });

  const answer = await modal({
    title: personal.configured ? "Replace your personal key" : "Use your own key",
    body: (isAzure
        ? "Paste the <b>full chat completions URL</b> from the Azure portal, not just the " +
          "resource root &mdash; the deployment in its path is what selects the model. "
        : "Paste an OpenRouter API key. ") +
      "This affects <b>only your own</b> chats; everyone else keeps using the shared server " +
      "connection. The key is encrypted with your password before it is stored, so it cannot " +
      "be read from the server's files without you &mdash; and <b>another admin resetting your " +
      "password will destroy it</b>." +
      (isAzure ? "<br><br>Saving sends a one-token test call, which is the only way to confirm " +
                 "the deployment name and api-version are right." : ""),
    fields,
    submitLabel: "Test & save",
  });
  if (!answer) return;

  toast(isAzure ? "Testing that endpoint and key..." : "Checking that key with OpenRouter...", "info", 4000);
  const res = await api("/api/chat/key", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      api_key: answer.api_key, password: answer.password,
      provider, endpoint: answer.endpoint || null,
    }),
  });
  if (!res.ok) { toast(await errorText(res), "error", 10000); return; }
  CHAT.keyState = await res.json();
  CHAT.model = CHAT.keyState.default_model || CHAT.model;
  CHAT.models = [];
  toast(isAzure ? `Your turns now use ${CHAT.model}.` : "Saved. Your turns now use your own key.", "ok");
  await loadModels(true);
  renderDock();
}

async function unlockKey() {
  const answer = await modal({
    title: "Unlock your personal key",
    body: "Your own key is saved but locked. This happens after the server restarts, because " +
          "the material that decrypts it is only ever held in memory &mdash; never written to " +
          "disk. Your chats have been running on the shared server connection meanwhile.",
    fields: [{ name: "password", label: "Your password", type: "password" }],
    submitLabel: "Unlock",
  });
  if (!answer) return;
  const res = await api("/api/chat/unlock", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password: answer.password }),
  });
  if (!res.ok) { toast(await errorText(res), "error"); return; }
  CHAT.keyState = await res.json();
  CHAT.model = CHAT.keyState.default_model || CHAT.model;
  CHAT.models = [];
  toast("Your personal key is unlocked.", "ok");
  renderDock();
}

async function removeKey() {
  if (!await confirmModal("Remove your personal API key?",
      "Your chats fall back to the shared server connection, so chat keeps working. Your " +
      "conversations are kept.", "Remove")) return;
  const res = await api("/api/chat/key", { method: "DELETE" });
  if (res.ok) {
    CHAT.keyState = await res.json();
    CHAT.models = [];
    toast("Personal key removed. You are back on the shared connection.", "ok");
    renderDock();
  }
}

// =====================================================================
// 3. model picker
// =====================================================================

async function loadModels(force = false) {
  if (CHAT.models.length && !force) return CHAT.models;
  const data = await apiJson(`/api/chat/models${force ? "?refresh=true" : ""}`, {}, null);
  CHAT.models = (data && data.models) || [];
  if (!CHAT.model && CHAT.models.length) {
    const preferred = CHAT.models.find(m => /claude.*sonnet/i.test(m.id)) || CHAT.models[0];
    CHAT.model = preferred.id;
  }
  return CHAT.models;
}

/** Switching org from inside the dock.
 *
 *  The header has an org picker too, but reaching for it mid-conversation is
 *  the wrong motion -- and while the dock is open it is 400px to the right of
 *  where you are looking. Changing it here changes it everywhere, through the
 *  same setActiveOrg() the header uses, so the tabs behind stay in step. */
function openOrgPicker() {
  const ids = Object.keys(ORGS || {});
  const back = chatEl("div", "modal-backdrop");

  const rows = ids.map(id => `
    <button type="button" class="chat-row ${id === CURRENT_ORG ? "on" : ""}" data-org="${escapeHtml(id)}">
      <span class="chat-row-title">${escapeHtml(id)}</span>
      <span class="chat-row-meta">${escapeHtml((ORGS[id] || {}).name || "")}</span>
    </button>`).join("");

  back.innerHTML = `
    <div class="modal" role="dialog" aria-modal="true" style="width:460px;">
      <h3>Which org?</h3>
      <p class="muted">Org-scoped tools act on this one. Changing it here also changes the
        active org in the tabs behind the dock.</p>
      <div class="chat-list">
        <button type="button" class="chat-row ${!CURRENT_ORG ? "on" : ""}" data-org="">
          <span class="chat-row-title">No org</span>
          <span class="chat-row-meta">log normalizing only</span>
        </button>
        ${rows || '<p class="muted">No orgs connected yet -- connect one on the Connections tab.</p>'}
      </div>
      <div class="modal-actions"><button type="button" class="secondary" data-cancel>Cancel</button></div>
    </div>`;

  const close = () => { document.removeEventListener("keydown", onKey); back.remove(); };
  const onKey = e => { if (e.key === "Escape") close(); };
  back.querySelector("[data-cancel]").onclick = close;
  back.onclick = e => { if (e.target === back) close(); };
  back.querySelectorAll("[data-org]").forEach(b => {
    b.onclick = () => {
      const id = b.dataset.org || null;
      close();
      if (id && typeof setActiveOrg === "function") setActiveOrg(id);
      else { CURRENT_ORG = id; renderDock(); renderTranscript(); }
      toast(id ? `Asking about ${id}.` : "No org selected -- org lookups are unavailable.", "ok");
    };
  });
  document.addEventListener("keydown", onKey);
  document.body.appendChild(back);
}

/** Called by app.js whenever the active org changes anywhere, so the chip can
 *  never disagree with the rest of the app. */
function chatOrgChanged() {
  if (!CHAT.el.chips) return;
  renderDock();
  if (!CHAT.messages.length) renderTranscript();   // refresh the org-aware starters
}

async function openModelPicker() {
  // Readiness, not "do you personally hold an unlocked key" -- the shared
  // connection has a model catalogue of its own, and a user on it was
  // previously bounced into a settings dialog they could do nothing with.
  if (!llmReady()) { openKeySettings(); return; }
  if (CHAT.keyState && CHAT.keyState.model_locked) {
    toast("The model is fixed by this server's configuration.", "info");
    return;
  }
  await loadModels();
  if (!CHAT.models.length) { toast("Could not load the model list from the LLM provider.", "error"); return; }

  // Agentic score is the number that matters for this workload -- multi-round
  // tool calling over long JSON results. Banding it is more honest than
  // printing a bare figure: the practical question is "will this cope", and
  // below roughly 20 the answer is usually no.
  const band = a => a === null || a === undefined ? { cls: "", txt: "unrated" }
    : a >= 25 ? { cls: "low", txt: `agentic ${a}` }
    : a >= 15 ? { cls: "medium", txt: `agentic ${a}` }
    : { cls: "high", txt: `agentic ${a}` };

  const back = chatEl("div", "modal-backdrop");
  const rows = CHAT.models.map(m => {
    const b = band(m.agentic_index);
    return `
    <button type="button" class="model-row ${m.id === CHAT.model ? "on" : ""}" data-model="${escapeHtml(m.id)}">
      <span class="model-name">${escapeHtml(m.name)}</span>
      <span class="model-meta">
        <span class="badge ${b.cls}">${escapeHtml(b.txt)}</span>
        ${m.is_free ? '<span class="badge declarative">free</span>' : ""}
        ${m.context_length ? `${Math.round(m.context_length / 1000)}k ctx` : ""}
      </span>
      <span class="model-id mono">${escapeHtml(m.id)}${
        m.expires ? ` &middot; retires ${escapeHtml(m.expires)}` : ""}</span>
    </button>`;
  }).join("");

  back.innerHTML = `
    <div class="modal" role="dialog" aria-modal="true" style="width:560px;">
      <h3>Choose a model</h3>
      <p class="muted">Only models that support <b>tool calling</b> are listed, sorted by
        <b>agentic score</b> — how well a model handles multi-round tool use, which is what
        this app does. Below about 15 expect the recovery notices you have been seeing.</p>
      <input id="modelFilter" placeholder="Filter models..." style="margin-bottom:10px;">
      <div class="model-list">${rows}</div>
      <div class="modal-actions">
        <button type="button" class="secondary" data-cancel>Cancel</button>
        <button type="button" class="secondary" data-refresh>Refresh list</button>
      </div>
    </div>`;

  const close = () => { document.removeEventListener("keydown", onKey); back.remove(); };
  const onKey = e => { if (e.key === "Escape") close(); };
  back.querySelector("[data-cancel]").onclick = close;
  back.onclick = e => { if (e.target === back) close(); };
  back.querySelector("[data-refresh]").onclick = async () => { close(); await loadModels(true); openModelPicker(); };
  back.querySelector("#modelFilter").oninput = e => {
    const q = e.target.value.toLowerCase();
    back.querySelectorAll(".model-row").forEach(r => {
      r.style.display = r.textContent.toLowerCase().includes(q) ? "" : "none";
    });
  };
  back.querySelectorAll(".model-row").forEach(r => {
    r.onclick = async () => {
      CHAT.model = r.dataset.model;
      close();
      renderDock();
      await api("/api/chat/default-model", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model: CHAT.model }),
      });
      toast(`Model set to ${CHAT.model}.`, "ok");
    };
  });
  document.addEventListener("keydown", onKey);
  document.body.appendChild(back);
  back.querySelector("#modelFilter").focus();
}

// =====================================================================
// 4. transcript rendering
// =====================================================================

function renderTranscript() {
  const host = CHAT.el.transcript;
  if (!host) return;

  // Keep the scroll pinned to the bottom only if the reader was already there.
  // Yanking them back down mid-turn while they are reading a tool result is
  // maddening, and this re-renders on every streamed token.
  //
  // The element that scrolls is the wrapper, not the transcript itself: in
  // full screen the transcript is a centred column inside a full-width
  // scroller, and measuring the inner one would report a page that never
  // scrolls and so always "near bottom".
  const scroller = CHAT.el.scroll || host;
  const nearBottom = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 80;
  const openRows = new Set(
    [...host.querySelectorAll(".tool-row[open]")].map(r => r.dataset.callId));

  host.innerHTML = "";

  if (!CHAT.messages.length) {
    host.appendChild(renderEmptyState());
    return;
  }

  // The full result of a tool call lives in its `tool` message, not on the
  // call itself -- one copy, and it is exactly the text the model was given.
  // Index them so each row can find its own.
  CHAT.toolResults = {};
  CHAT.messages.forEach(m => {
    if (m.role === "tool" && m.tool_call_id) CHAT.toolResults[m.tool_call_id] = m.content;
  });

  CHAT.messages.forEach(m => {
    const node = renderMessage(m);
    if (node) host.appendChild(node);
  });
  openRows.forEach(id => {
    const row = host.querySelector(`.tool-row[data-call-id="${CSS.escape(id)}"]`);
    if (row) row.open = true;
  });
  if (nearBottom) scroller.scrollTop = scroller.scrollHeight;
  if (typeof updateJumpButton === "function") updateJumpButton();
}

function renderEmptyState() {
  const wrap = chatEl("div", "chat-empty");
  const org = CURRENT_ORG;
  const starters = org ? [
    "Which flows have no fault path, and which are riskiest?",
    "Show me recurring incidents from the last 30 days",
    "What automation fires when an Opportunity is saved?",
  ] : [
    "Normalize a debug log and tell me what failed",
    "What orgs can I see?",
  ];
  wrap.innerHTML = `
    <div class="chat-empty-title">${org ? `Ask about ${escapeHtml(org)}` : "Ask a question"}</div>
    <div class="chat-empty-sub">${org
      ? "Answers are grounded in this org's knowledgebase, not general Salesforce knowledge."
      : "Pick an org in the header to ask org-specific questions."}</div>
    <div class="chat-starters">
      ${starters.map(s => `<button type="button" class="starter">${escapeHtml(s)}</button>`).join("")}
    </div>`;
  wrap.querySelectorAll(".starter").forEach(b => {
    b.onclick = () => { CHAT.el.input.value = b.textContent; CHAT.el.input.focus(); };
  });
  return wrap;
}

function renderMessage(m) {
  if (m.role === "tool") return null;         // shown as rows on the assistant turn
  if (m.role === "system") return null;

  if (m.role === "user") {
    const el = chatEl("div", "chat-msg user");
    el.innerHTML = `<div class="chat-bubble">${renderMarkdown(m.content)}</div>`;
    return el;
  }

  const el = chatEl("div", "chat-msg assistant");

  if (m.reasoning) {
    const d = chatEl("details", "chat-reasoning");
    d.innerHTML = `<summary>Thinking</summary><div>${renderMarkdown(m.reasoning)}</div>`;
    el.appendChild(d);
  }

  (m.tool_calls || []).forEach(c => el.appendChild(renderToolRow(c)));

  if (m.content) {
    const body = chatEl("div", "chat-body");
    body.innerHTML = renderMarkdown(m.content);
    el.appendChild(body);
  }

  if (m.notice) {
    el.appendChild(chatEl("div", "chat-notice", escapeHtml(m.notice)));
  }

  if (m.usage) {
    const u = m.usage;
    el.appendChild(chatEl("div", "chat-usage",
      `${fmtTokens(u.prompt_tokens)} in &middot; ${fmtTokens(u.completion_tokens)} out` +
      (u.reasoning_tokens ? ` &middot; ${fmtTokens(u.reasoning_tokens)} reasoning` : "") +
      (u.cost !== null && u.cost !== undefined ? ` &middot; ${fmtCost(u.cost)}` : "")));
  }
  return el;
}

/** Tool rows are the trust surface of this whole feature. Collapsed by
 *  default so the transcript stays readable; expandable to the exact
 *  arguments and the raw result, so a claim can always be traced back to the
 *  evidence that produced it. */
function renderToolRow(c) {
  const state = c.pending ? "pending" : (c.ok === false ? "error" : (c.ok === true ? "ok" : "run"));
  const row = chatEl("details", `tool-row ${state}`);
  row.dataset.callId = c.id;

  const timing = c.ms !== null && c.ms !== undefined ? `${c.ms}ms` : (state === "run" ? "running" : "");

  // Prefer the copy streamed with this turn; fall back to the stored tool
  // message when an older conversation is reopened. Both are the same text the
  // model was given, so what is shown here is genuinely the evidence behind
  // the answer, not a paraphrase of it.
  const resultText = c.result !== undefined && c.result !== null
    ? c.result
    : (CHAT.toolResults || {})[c.id];

  let resultBlock;
  if (state === "run") {
    resultBlock = "";
  } else if (resultText !== undefined && resultText !== null) {
    resultBlock = `<div class="tool-label">Result</div><pre>${escapeHtml(prettyJson(resultText))}</pre>`;
  } else if (state === "pending") {
    resultBlock = '<p class="muted">Nothing has run yet &mdash; confirm below to run it.</p>';
  } else {
    resultBlock = '<p class="muted">This result was not recorded.</p>';
  }

  row.innerHTML = `
    <summary>
      <span class="tool-dot"></span>
      <span class="tool-name mono">${escapeHtml(c.name)}</span>
      <span class="tool-preview">${escapeHtml(c.preview || "")}</span>
      <span class="tool-ms">${escapeHtml(timing)}</span>
    </summary>
    <div class="tool-detail">
      <div class="tool-label">Arguments</div>
      <pre>${escapeHtml(JSON.stringify(c.args || {}, null, 2))}</pre>
      ${c.truncated
        ? '<p class="muted">This result was too large to send in full, so the model saw the ' +
          'portion below and was told it had been cut.</p>'
        : ""}
      ${resultBlock}
    </div>`;

  if (c.pending) row.appendChild(renderConfirmCard(c));
  return row;
}

/** Write tools never run on the model's say-so alone. Tool results include
 *  customer Apex source and raw-log text -- third-party content that can
 *  contain instructions aimed at the model -- and set_org_visibility can
 *  expose an org to every account in the app. */
function renderConfirmCard(c) {
  const card = chatEl("div", "tool-confirm");
  card.innerHTML = `
    <div class="tool-confirm-title">Confirm before this runs</div>
    <div class="tool-confirm-body">
      <b class="mono">${escapeHtml(c.name)}</b> changes stored data. Review the arguments above.
    </div>
    <div class="tool-confirm-actions">
      <button type="button" class="secondary" data-decline>Don't run it</button>
      <button type="button" class="primary" data-confirm>Run it</button>
    </div>`;
  card.querySelector("[data-confirm]").onclick = () => confirmPendingTool(c.id, true);
  card.querySelector("[data-decline]").onclick = () => confirmPendingTool(c.id, false);
  return card;
}

async function confirmPendingTool(callId, approve) {
  CHAT.pendingConfirm = null;
  if (!approve) {
    CHAT.messages.push({ role: "user", content: "Don't run that -- explain what you were going to do instead." });
    renderTranscript();
    await sendTurn("Don't run that -- explain what you were going to do instead.", []);
    return;
  }
  await sendTurn("Go ahead.", [callId]);
}

// =====================================================================
// 5. the turn loop (SSE)
// =====================================================================

async function ensureChat() {
  if (CHAT.chatId) return CHAT.chatId;
  const res = await api("/api/chats", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ org_id: CURRENT_ORG || null, model: CHAT.model }),
  });
  if (!res.ok) { toast(await errorText(res), "error"); return null; }
  CHAT.meta = await res.json();
  CHAT.chatId = CHAT.meta.chat_id;
  return CHAT.chatId;
}

async function sendMessage() {
  const text = (CHAT.el.input.value || "").trim();
  if (!text || CHAT.streaming) return;
  CHAT.el.input.value = "";
  CHAT.el.input.style.height = "";
  await sendTurn(text, []);
}

/** Reads the SSE stream by hand rather than with EventSource, because
 *  EventSource cannot POST and cannot carry the session cookie the way we
 *  need. fetch + a ReadableStream reader gives the same events with a body. */
async function sendTurn(text, confirmToolIds) {
  const chatId = await ensureChat();
  if (!chatId) return;

  // One readiness gate instead of two key-state checks. For the great
  // majority of users this passes without their ever having configured
  // anything -- which is the whole point of the shared server connection.
  if (!llmReady()) {
    await loadKeyState();                       // it may have been configured since page load
    renderDock();
    if (!llmReady()) { openKeySettings(); return; }
  }
  if (!CHAT.model) { await loadModels(); if (!CHAT.model) { openModelPicker(); return; } }

  if (!confirmToolIds.length) {
    CHAT.messages.push({ role: "user", content: text });
  }
  const assistant = { role: "assistant", content: "", tool_calls: [], reasoning: "" };
  CHAT.messages.push(assistant);
  CHAT.streaming = true;
  renderTranscript();
  renderComposer();

  const controller = new AbortController();
  CHAT.abort = controller;

  let res;
  try {
    res = await api(`/api/chats/${encodeURIComponent(chatId)}/messages`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        content: text, org_id: CURRENT_ORG || null, model: CHAT.model,
        confirm_tool_ids: confirmToolIds,
      }),
      signal: controller.signal,
    });
  } catch (e) {
    finishStream(assistant, e.name === "AbortError" ? null : "Lost connection to the server.");
    return;
  }
  if (!res.ok) { finishStream(assistant, await errorText(res)); return; }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const frames = buffer.split("\n\n");
      buffer = frames.pop();
      frames.forEach(frame => handleFrame(frame, assistant));
    }
  } catch (e) {
    if (e.name !== "AbortError") finishStream(assistant, "The stream was interrupted.");
    else finishStream(assistant, null);
    return;
  }
  finishStream(assistant, null);
}

function handleFrame(frame, assistant) {
  const lines = frame.split("\n");
  let event = "message", data = "";
  lines.forEach(l => {
    if (l.startsWith("event:")) event = l.slice(6).trim();
    else if (l.startsWith("data:")) data += l.slice(5).trim();
  });
  if (!data) return;
  let payload;
  try { payload = JSON.parse(data); } catch (e) { return; }

  switch (event) {
    case "token":
      assistant.content += payload.text;
      break;
    case "reasoning":
      assistant.reasoning += payload.text;
      break;
    case "tool_call":
      assistant.tool_calls.push({ id: payload.id, name: payload.name, args: payload.args, ok: null, ms: null });
      break;
    case "tool_result": {
      const call = assistant.tool_calls.find(c => c.id === payload.id);
      if (call) {
        call.ok = payload.ok; call.ms = payload.ms;
        call.preview = payload.preview; call.truncated = payload.truncated;
        call.result = payload.result;
      }
      break;
    }
    case "confirm_required": {
      const call = assistant.tool_calls.find(c => c.id === payload.id);
      if (call) { call.pending = true; }
      else assistant.tool_calls.push({ ...payload, pending: true, ok: null, ms: null });
      CHAT.pendingConfirm = payload.id;
      break;
    }
    case "usage":
      assistant.usage = payload;
      break;
    case "content_replace":
      // The server recovered tool calls that this model wrote as raw markup in
      // the message body. Swap the streamed text for the cleaned version so the
      // user never ends up reading <tool_call> XML as though it were an answer.
      assistant.content = payload.text || "";
      break;
    case "notice":
      // Not a failure -- the answer above is real. Kept visually distinct from
      // an error so a completed-but-capped turn does not read as a crash.
      assistant.notice = payload.message;
      break;
    case "error":
      assistant.content += (assistant.content ? "\n\n" : "") + `**${payload.message}**`;
      if (payload.code === "key_locked" || payload.code === "key_missing") {
        loadKeyState().then(renderDock);
      }
      // A refused turn is exactly the moment to show the meter, and the
      // server already sent the figures with the refusal -- so no second
      // request to find out what it just told us.
      if (payload.code === "quota_exceeded") renderChatQuota(payload.quota);
      break;
    case "done":
      if (CHAT.meta) { CHAT.meta.title = payload.title; CHAT.meta.total_cost = payload.total_cost; }
      break;
  }
  renderTranscript();
}

function finishStream(assistant, errorMessage) {
  CHAT.streaming = false;
  CHAT.abort = null;
  if (errorMessage) {
    assistant.content += (assistant.content ? "\n\n" : "") + `**${errorMessage}**`;
  }
  if (!assistant.content && !assistant.tool_calls.length) {
    CHAT.messages = CHAT.messages.filter(m => m !== assistant);
  }
  renderTranscript();
  renderComposer();
  // The turn that just ran is what moved the needle, so re-read it now rather
  // than leaving a stale figure over the composer.
  renderChatQuota();
  if (CHAT.mode === "full") renderChatRail();     // the title may have changed
}

function stopStream() {
  if (CHAT.abort) CHAT.abort.abort();
}

// =====================================================================
// 6. sharing
// =====================================================================

async function shareChat() {
  if (!CHAT.chatId) { toast("Ask something first -- there is nothing to share yet.", "info"); return; }

  const answer = await modal({
    title: "Share this transcript",
    body: "Creates a read-only link that works without an account. " +
          "<b>Tool arguments and results are hidden by default</b> -- they contain org internals " +
          "(component cards, field maps, incident packs) and whoever opens the link has no org " +
          "permissions to check them against. Type <code>include tools</code> below only if the " +
          "recipient is entitled to see the underlying org data.",
    fields: [{ name: "tools", label: "Include tool arguments and results? (type 'include tools' to opt in)", type: "text" }],
    submitLabel: "Create link",
  });
  if (!answer) return;
  const includeTools = (answer.tools || "").trim().toLowerCase() === "include tools";

  const res = await api(`/api/chats/${encodeURIComponent(CHAT.chatId)}/share`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ include_tools: includeTools }),
  });
  if (!res.ok) { toast(await errorText(res), "error"); return; }
  const data = await res.json();
  const url = `${window.location.origin}${data.url}`;

  await modal({
    title: "Share link created",
    body: `<div class="token-reveal">${escapeHtml(url)}</div>
      <p class="muted">${includeTools
        ? "This link <b>includes</b> tool arguments and results."
        : "Tool names are shown; their arguments and results are hidden."}
      Anyone with the link can read the transcript. Revoke it from the dock menu at any time.</p>`,
    fields: [], submitLabel: "Copy link",
  }).then(r => {
    if (r !== null) {
      navigator.clipboard.writeText(url)
        .then(() => toast("Link copied.", "ok"))
        .catch(() => toast("Copy failed -- select the link and copy it manually.", "error"));
    }
  });
  if (CHAT.meta) CHAT.meta.share_token = data.token;
  renderDock();
}

async function unshareChat() {
  if (!CHAT.chatId) return;
  if (!await confirmModal("Revoke the share link?",
      "The link stops working for everyone immediately. This cannot be undone -- sharing again makes a new link.",
      "Revoke")) return;
  const res = await api(`/api/chats/${encodeURIComponent(CHAT.chatId)}/share`, { method: "DELETE" });
  if (res.ok) {
    if (CHAT.meta) CHAT.meta.share_token = null;
    toast("Share link revoked.", "ok");
    renderDock();
  }
}

// =====================================================================
// 7. chat list / history
// =====================================================================

async function openChatList() {
  const chats = await apiJson("/api/chats", {}, []) || [];
  const back = chatEl("div", "modal-backdrop");
  const rows = chats.length ? chats.map(c => `
    <button type="button" class="chat-row ${c.chat_id === CHAT.chatId ? "on" : ""}" data-id="${escapeHtml(c.chat_id)}">
      <span class="chat-row-title">${escapeHtml(c.title || "Untitled")}</span>
      <span class="chat-row-meta">
        ${c.org_id ? `<span class="pill">${escapeHtml(c.org_id)}</span>` : ""}
        ${c.share_token ? '<span class="badge declarative">shared</span>' : ""}
        ${escapeHtml(fmtWhen(c.updated_at))}
      </span>
      <span class="chat-row-del" data-del="${escapeHtml(c.chat_id)}" title="Delete">&times;</span>
    </button>`).join("") : '<p class="muted">No conversations yet.</p>';

  back.innerHTML = `
    <div class="modal" role="dialog" aria-modal="true" style="width:520px;">
      <h3>Conversations</h3>
      <div class="chat-list">${rows}</div>
      <div class="modal-actions">
        <button type="button" class="secondary" data-cancel>Close</button>
        <button type="button" class="primary" data-new>New conversation</button>
      </div>
    </div>`;
  const close = () => { document.removeEventListener("keydown", onKey); back.remove(); };
  const onKey = e => { if (e.key === "Escape") close(); };
  back.querySelector("[data-cancel]").onclick = close;
  back.onclick = e => { if (e.target === back) close(); };
  back.querySelector("[data-new]").onclick = () => { close(); newChat(); };
  back.querySelectorAll(".chat-row").forEach(r => {
    r.onclick = async e => {
      if (e.target.dataset.del) {
        e.stopPropagation();
        if (!await confirmModal("Delete this conversation?", "This cannot be undone.", "Delete")) return;
        await api(`/api/chats/${encodeURIComponent(e.target.dataset.del)}`, { method: "DELETE" });
        close(); openChatList();
        return;
      }
      close();
      await loadChat(r.dataset.id);
    };
  });
  document.addEventListener("keydown", onKey);
  document.body.appendChild(back);
}

async function loadChat(chatId) {
  const data = await apiJson(`/api/chats/${encodeURIComponent(chatId)}`, {}, null);
  if (!data) { toast("Could not open that conversation.", "error"); return; }
  CHAT.chatId = chatId;
  CHAT.meta = data.meta;
  CHAT.messages = data.messages || [];
  if (data.meta.model) CHAT.model = data.meta.model;
  renderDock();
  renderTranscript();
}

function newChat() {
  CHAT.chatId = null;
  CHAT.meta = null;
  CHAT.messages = [];
  CHAT.pendingConfirm = null;
  renderDock();
  renderTranscript();
  if (CHAT.mode === "full" && typeof renderChatRail === "function") renderChatRail();
  if (CHAT.el.input) CHAT.el.input.focus();
}

// =====================================================================
// 8. mount + render
// =====================================================================

function renderComposer() {
  const btn = CHAT.el.send;
  if (!btn) return;
  btn.textContent = CHAT.streaming ? "Stop" : "Send";
  btn.className = CHAT.streaming ? "secondary" : "primary";
  btn.onclick = CHAT.streaming ? stopStream : sendMessage;
  if (CHAT.el.input) CHAT.el.input.disabled = false;
}

function renderDock() {
  const bar = CHAT.el.chips;
  if (!bar) return;
  const s = CHAT.keyState || {};

  // The LLM chip reports the EFFECTIVE state. It used to read "Connect an
  // LLM" whenever the signed-in user had no key of their own -- which is now
  // almost everybody, all of whom can chat fine on the shared connection. A
  // call to action nobody can act on is worse than no chip at all.
  let keyChip;
  if (!s.ready) {
    keyChip = `<button type="button" class="chip warn" data-act="key">${
      s.can_manage ? "LLM not configured" : "chat unavailable"}</button>`;
  } else if (s.using === "personal") {
    keyChip = `<button type="button" class="chip" data-act="key">your key</button>`;
  } else if (s.personal_key_locked) {
    // Admin only, and informational: chat is working on the shared
    // connection, their own key is merely waiting to be unlocked.
    keyChip = `<button type="button" class="chip" data-act="key">shared &middot; unlock yours</button>`;
  } else if (s.can_manage) {
    keyChip = `<button type="button" class="chip" data-act="key">shared LLM</button>`;
  } else {
    // A plain user gets no LLM chip at all. There is nothing to change, and
    // the information is still one click away from the header's own controls.
    keyChip = "";
  }

  const modelChip = s.model_locked
    ? `<span class="chip" title="Fixed by this server's configuration">${
        escapeHtml(CHAT.model || s.default_model || "model")}</span>`
    : `<button type="button" class="chip" data-act="model">${
        escapeHtml(CHAT.model || "pick a model")} &#9662;</button>`;

  bar.innerHTML = `
    <button type="button" class="chip ${CURRENT_ORG ? "acc" : "warn"}" data-act="org">
      ${CURRENT_ORG ? `org: ${escapeHtml(CURRENT_ORG)}` : "no org"} &#9662;</button>
    ${modelChip}
    ${keyChip}
    ${CHAT.meta && CHAT.meta.share_token ? '<span class="chip acc">shared</span>' : ""}`;

  bar.querySelectorAll("[data-act]").forEach(b => {
    b.onclick = () => {
      if (b.dataset.act === "key") openKeySettings();
      if (b.dataset.act === "model") openModelPicker();
      if (b.dataset.act === "org") openOrgPicker();
    };
  });

  if (CHAT.el.footer) {
    const totals = CHAT.meta && CHAT.meta.total_tokens
      ? `${fmtTokens(CHAT.meta.total_tokens)} tokens &middot; ${fmtCost(CHAT.meta.total_cost)}` : "";
    CHAT.el.footer.innerHTML = totals;
  }
}

/** The one entry point. `container` is any element and `mode` is "dock" or
 *  "full"; everything above this line is written against CHAT.el and neither
 *  knows nor cares which one it is rendering into.
 *
 *  Because the transcript re-renders from CHAT.messages rather than from the
 *  DOM, re-mounting into the other container mid-conversation -- even
 *  mid-stream -- loses nothing. That is what makes the expand/collapse
 *  button safe to press at any moment.
 */
function mountChat(container, mode = "dock") {
  const full = mode === "full";
  CHAT.mode = mode;
  container.innerHTML = `
    <div class="chat-head">
      <span class="chat-title">${full ? "Ask" : "Ask"}</span>
      <div class="chat-head-actions">
        ${full
          ? `<button type="button" class="icon-btn" data-act="collapse" title="Open in the side panel instead">&#8600;</button>`
          : `<button type="button" class="icon-btn" data-act="expand" title="Expand to full screen">&#8599;</button>`}
        <button type="button" class="icon-btn" data-act="history" title="Conversations">&#9776;</button>
        <button type="button" class="icon-btn" data-act="share" title="Share transcript">&#128279;</button>
        <button type="button" class="icon-btn" data-act="new" title="New conversation">+</button>
        <button type="button" class="icon-btn" data-act="close" title="${full ? "Back to the app" : "Close"}">&times;</button>
      </div>
    </div>
    <div class="chat-chips"></div>
    <div class="chat-scroll">
      <div class="chat-transcript"></div>
      <button type="button" class="chat-jump" style="display:none;">Jump to latest &#8595;</button>
    </div>
    <div class="chat-quota"></div>
    <div class="chat-footer"></div>
    <div class="chat-composer">
      <textarea class="chat-input" rows="${full ? 3 : 2}" placeholder="Ask about this org..."></textarea>
      <div class="chat-composer-actions">
        <span class="chat-hint">Enter to send &middot; Shift+Enter for a new line${
          full ? " &middot; Esc to go back" : ""}</span>
        <button type="button" class="primary chat-send">Send</button>
      </div>
    </div>`;

  CHAT.el = {
    chips: container.querySelector(".chat-chips"),
    scroll: container.querySelector(".chat-scroll"),
    transcript: container.querySelector(".chat-transcript"),
    jump: container.querySelector(".chat-jump"),
    quota: container.querySelector(".chat-quota"),
    footer: container.querySelector(".chat-footer"),
    input: container.querySelector(".chat-input"),
    send: container.querySelector(".chat-send"),
  };

  container.querySelectorAll("[data-act]").forEach(b => {
    b.onclick = () => {
      const act = b.dataset.act;
      if (act === "history") openChatList();
      if (act === "share") (CHAT.meta && CHAT.meta.share_token) ? unshareChat() : shareChat();
      if (act === "new") newChat();
      // enterChatFull, NOT openChatFull. openChatFull is the nav button's
      // entry point and honours the remembered preference -- and opening the
      // dock sets that preference to "dock", so routing this button through
      // it made expand re-open the dock and do nothing at all. A button that
      // says "expand" is an explicit instruction, not a preference to consult.
      if (act === "expand") enterChatFull();
      if (act === "collapse") collapseChatToDock();
      if (act === "close") full ? leaveChatFull() : toggleChatDock(false);
    };
  });

  // The composer grows further in full screen: three lines is cramped for the
  // paragraph-long questions this view invites, and the dock's 160px ceiling
  // was sized for a 400px rail.
  const maxH = full ? Math.round(window.innerHeight * 0.35) : 160;
  CHAT.el.input.addEventListener("keydown", e => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendMessage(); }
    // Ctrl/Cmd+Enter sends too. Muscle memory from every other chat box, and
    // it is the one that still works when Enter has been used for newlines.
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); sendMessage(); }
  });
  CHAT.el.input.addEventListener("input", e => {
    e.target.style.height = "auto";
    e.target.style.height = Math.min(e.target.scrollHeight, maxH) + "px";
  });

  // "Jump to latest" rather than dragging the reader back down. renderTranscript
  // already refuses to auto-scroll someone who has scrolled up; this is what
  // tells them there is more below, which matters far more on a tall screen
  // than in the dock.
  if (CHAT.el.scroll) {
    CHAT.el.scroll.addEventListener("scroll", updateJumpButton);
    CHAT.el.jump.onclick = () => {
      CHAT.el.scroll.scrollTop = CHAT.el.scroll.scrollHeight;
      updateJumpButton();
    };
  }

  renderComposer();
  renderChatQuota();
  // Paint from the state already in hand FIRST, then again once the key state
  // arrives. Both of these used to happen only inside the `.then`, which meant
  // the chips bar and the transcript stayed empty until a request came back:
  // re-mounting (switching between full screen and the dock, even mid-stream)
  // showed a blank conversation for a moment, and if the key-state request
  // failed they never rendered at all -- leaving the full-screen view with no
  // org indicator anywhere on screen, since the header's picker is not
  // visible in that mode.
  renderDock();
  renderTranscript();
  loadKeyState().then(() => { renderDock(); renderTranscript(); });
}

function updateJumpButton() {
  const s = CHAT.el.scroll, j = CHAT.el.jump;
  if (!s || !j) return;
  const away = s.scrollHeight - s.scrollTop - s.clientHeight > 120;
  j.style.display = away ? "" : "none";
}

/** The quota notice, shown only when it is about to matter.
 *
 *  Placed in the chat surface on purpose: this is where the limit gets hit,
 *  and a warning on a tab nobody has open is not a warning. Below 75% it says
 *  nothing at all -- a permanent meter over the composer would be noise for
 *  the ninety-odd percent of turns that are nowhere near a cap. */
async function renderChatQuota(known) {
  const host = CHAT.el.quota;
  if (!host) return;
  let q = known;
  if (!q) {
    const r = await apiJson("/api/usage/me?days=1", {}, null);
    q = r && r.quota;
  }
  if (!q || q.unlimited) { host.innerHTML = ""; return; }
  const worst = [q.daily, q.window]
    .filter(p => p && p.limit !== null)
    .sort((a, b) => (b.pct || 0) - (a.pct || 0))[0];
  if (!worst || (worst.pct || 0) < 75) { host.innerHTML = ""; return; }
  const which = worst === q.daily ? "today" : `the last ${q.window_days} days`;
  host.innerHTML = worst.exceeded
    ? `<div class="chat-quota-bar over">You have used your whole LLM allowance for ${which}.
         ${q.tier === "unverified" ? "An admin verifying your account raises it."
                                   : "An admin can raise it."}</div>`
    : `<div class="chat-quota-bar warn">${fmtTokens(worst.remaining)} tokens left of your
         allowance for ${which}.</div>`;
}

/** Deep link from anywhere in the app: opens the DOCK with a question
 *  pre-filled but NOT sent, so the engineer can edit it first.
 *
 *  Deliberately the dock and not full screen. These links come off a row in a
 *  table -- an incident, a field writer, a flow -- and the value of asking
 *  from there is that the thing being asked about stays on screen next to the
 *  answer. Full screen would cover it up. */
function askAbout(question) {
  if (CHAT.mode === "full") {
    // ...unless they are already in full screen, in which case yanking them
    // out of it would be the more surprising move.
    prefillChat(question);
    return;
  }
  toggleChatDock(true);
  prefillChat(question);
}

function prefillChat(question) {
  if (!CHAT.el.input) return;
  CHAT.el.input.value = question;
  CHAT.el.input.focus();
  CHAT.el.input.dispatchEvent(new Event("input"));
}

// ---------- the two modes ----------
//
// Chat is a view AND a dock, and which one you get depends on how you asked.
// "Ask" in the nav means "I am going to work in here for a while" and opens
// full screen; "explain this" next to a record means "tell me about that" and
// opens the dock beside it. The preference is remembered per browser, so
// whichever one someone settles into is what the nav button gives them next
// time.

const CHAT_MODE_KEY = "ts_chat_mode";

function preferredChatMode() {
  try { return localStorage.getItem(CHAT_MODE_KEY) || "full"; } catch (e) { return "full"; }
}

function rememberChatMode(mode) {
  try { localStorage.setItem(CHAT_MODE_KEY, mode); } catch (e) { /* private mode */ }
}

/** The nav's "Ask" button. Honours the remembered preference, so someone who
 *  prefers the dock is not thrown into full screen every time. */
function openChatFull() {
  if (preferredChatMode() === "dock") { toggleChatDock(true); return; }
  enterChatFull();
}

function enterChatFull() {
  rememberChatMode("full");
  if (CHAT.open) toggleChatDock(false);
  showView("chat");
}

/** Called by showView, so the mount happens whether chat was reached from the
 *  nav button or by any other route into the view. */
function mountChatFull() {
  const host = document.getElementById("chatFullInner");
  if (!host) return;
  if (CHAT.mode !== "full" || !host.querySelector(".chat-input")) {
    mountChat(host, "full");
  }
  renderChatRail();
  if (CHAT.el.input) CHAT.el.input.focus();
}

function collapseChatToDock() {
  rememberChatMode("dock");
  showView(LAST_VIEW || "connections");
  CHAT.mode = null;                 // force a re-mount into the dock
  CHAT.el = {};
  toggleChatDock(true);
}

/** Leave full screen without changing the preference -- this is "I'm done",
 *  not "I prefer the other one". */
function leaveChatFull() {
  showView(LAST_VIEW || "connections");
}

// ---------- the conversation rail ----------
//
// Collapsed by default, because the default should be one clean column of
// conversation. It pops back out on the arrow, and that choice is remembered.

const CHAT_RAIL_KEY = "ts_chat_rail";

function toggleChatRail(open) {
  const rail = document.getElementById("chatRail");
  const peek = document.getElementById("chatRailPeek");
  if (!rail) return;
  const collapsed = open === undefined ? !rail.classList.contains("collapsed") : !open;
  rail.classList.toggle("collapsed", collapsed);
  if (peek) peek.style.display = collapsed ? "" : "none";
  try { localStorage.setItem(CHAT_RAIL_KEY, collapsed ? "0" : "1"); } catch (e) { /* ignore */ }
  if (!collapsed) renderChatRail();
}

async function renderChatRail() {
  const list = document.getElementById("chatRailList");
  const rail = document.getElementById("chatRail");
  if (!list || !rail) return;
  const peek = document.getElementById("chatRailPeek");
  let wantOpen = false;
  try { wantOpen = localStorage.getItem(CHAT_RAIL_KEY) === "1"; } catch (e) { /* ignore */ }
  rail.classList.toggle("collapsed", !wantOpen);
  if (peek) peek.style.display = wantOpen ? "none" : "";
  if (!wantOpen) return;                        // nothing to fetch while hidden

  const chats = await apiJson("/api/chats", {}, []) || [];
  if (!chats.length) { list.innerHTML = `<p class="muted" style="padding:10px;">No conversations yet.</p>`; return; }
  list.innerHTML = chats.map(c => `
    <button type="button" class="rail-row ${c.chat_id === CHAT.chatId ? "on" : ""}"
            data-id="${escapeHtml(c.chat_id)}">
      <span class="rail-row-title">${escapeHtml(c.title || "Untitled")}</span>
      <span class="rail-row-meta">${c.org_id ? `<span class="pill">${escapeHtml(c.org_id)}</span>` : ""}
        ${escapeHtml(fmtWhen(c.updated_at))}</span>
    </button>`).join("")
    + `<button type="button" class="rail-new" onclick="newChat()">+ New conversation</button>`;
  list.querySelectorAll(".rail-row").forEach(r => {
    r.onclick = async () => { await loadChat(r.dataset.id); renderChatRail(); };
  });
}

// ---------- the dock ----------

function toggleChatDock(open) {
  const dock = document.getElementById("chatDock");
  const root = document.getElementById("appRoot");
  CHAT.open = open === undefined ? !CHAT.open : open;
  dock.classList.toggle("open", CHAT.open);
  root.classList.toggle("dock-open", CHAT.open);
  try { localStorage.setItem("ts_chat_dock", CHAT.open ? "1" : "0"); } catch (e) { /* private mode */ }
  if (CHAT.open) {
    rememberChatMode("dock");
    // Re-mount if the last mount was the full-screen one: CHAT.el would still
    // point at nodes inside a hidden view, so the composer would take text
    // nobody could see.
    if (CHAT.mode !== "dock" || !CHAT.el.input) {
      mountChat(dock.querySelector(".chat-dock-inner"), "dock");
    }
    if (CHAT.el.input) CHAT.el.input.focus();
  }
}

function initChatDock() {
  let wasOpen = false;
  try { wasOpen = localStorage.getItem("ts_chat_dock") === "1"; } catch (e) { /* ignore */ }
  if (wasOpen) toggleChatDock(true);
}

// Esc leaves full-screen chat; Cmd/Ctrl+K jumps into the composer from
// anywhere. Both are skipped while a modal is open or while the caret is in
// some other field, so neither steals a keystroke meant for something else.
document.addEventListener("keydown", e => {
  const inModal = !!document.querySelector(".modal-backdrop");
  if (e.key === "Escape" && !inModal && CHAT.mode === "full"
      && document.body.classList.contains("chat-fullscreen")) {
    leaveChatFull();
    return;
  }
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k" && !inModal) {
    e.preventDefault();
    if (CHAT.mode === "full" && document.body.classList.contains("chat-fullscreen")) {
      CHAT.el.input && CHAT.el.input.focus();
    } else {
      openChatFull();
    }
  }
});

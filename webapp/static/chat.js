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

async function loadKeyState() {
  CHAT.keyState = await apiJson("/api/chat/key", {}, null);
  if (CHAT.keyState && !CHAT.model) CHAT.model = CHAT.keyState.default_model;
  return CHAT.keyState;
}

/** One dialog listing every action, rather than a chain of modals.
 *
 *  The chained version hid things badly: when a key was locked (which is the
 *  normal state after any server restart) the only thing on offer was a
 *  password box, so there was no route to switching provider at all -- and
 *  "connect a different provider" was buried behind the submit button of a
 *  dialog that read as purely informational. Everything is a visible button
 *  here, and each state says what it means. */
async function openKeySettings() {
  const s = (await loadKeyState()) || {};
  const provName = { openrouter: "OpenRouter", azure: "Azure OpenAI" };

  let status, statusClass;
  if (!s.configured && s.cleared_reason === "password_reset_by_admin") {
    statusClass = "warn";
    status = "Your saved key was cleared when an admin reset your password. It could not be " +
             "recovered, because only your password could decrypt it. Add a key again below.";
  } else if (!s.configured) {
    statusClass = "";
    status = "No LLM is connected yet. Pick a provider to get started.";
  } else if (!s.unlocked) {
    statusClass = "warn";
    status = `Your <b>${escapeHtml(provName[s.provider] || s.provider)}</b> key is saved but ` +
             `<b>locked</b>. This is normal after the server restarts &mdash; the unlock ` +
             `material is only ever held in memory, never on disk. Enter your password to unlock.`;
  } else {
    statusClass = "ok";
    status = `Connected to <b>${escapeHtml(provName[s.provider] || s.provider)}</b> with key ` +
             `<code>${escapeHtml(s.hint || "")}</code>.` +
             (s.endpoint ? `<div class="mono" style="font-size:11px; word-break:break-all; margin-top:4px;">${
               escapeHtml(s.endpoint)}</div>` : "") +
             (s.verified_at ? `<div style="margin-top:4px;">Last verified ${
               escapeHtml(fmtWhen(s.verified_at))}.</div>` : "");
  }

  const choice = await new Promise(resolve => {
    const back = chatEl("div", "modal-backdrop");
    back.innerHTML = `
      <div class="modal" role="dialog" aria-modal="true" style="width:500px;">
        <h3>LLM settings</h3>
        <div class="key-status ${statusClass}">${status}</div>
        ${s.configured && !s.unlocked
          ? `<button type="button" class="key-action primary-action" data-a="unlock">
               Unlock with your password</button>` : ""}
        <button type="button" class="key-action" data-a="azure">
          <b>Connect Azure OpenAI</b>
          <span>Your own deployment and key. Data stays in your Azure tenant.</span>
        </button>
        <button type="button" class="key-action" data-a="openrouter">
          <b>Connect OpenRouter</b>
          <span>One key, a catalogue of models. Free tiers for testing.</span>
        </button>
        ${s.configured ? `<button type="button" class="key-action danger" data-a="remove">
          <b>Remove the stored key</b>
          <span>Chat stops working until you add one. Conversations are kept.</span>
        </button>` : ""}
        <p class="muted" style="margin-top:12px;">Your key is encrypted with your password before
          it is stored. Nobody &mdash; including an admin &mdash; can read it back out of the
          server's files.</p>
        <div class="modal-actions"><button type="button" class="secondary" data-cancel>Close</button></div>
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
  if (choice === "remove") { await removeKey(); return; }
  if (choice === "unlock") { await unlockKey(); return; }

  const provider = choice;                 // "azure" | "openrouter"
  const isAzure = provider === "azure";
  const fields = [];
  if (isAzure) {
    fields.push({
      name: "endpoint", label: "Azure chat completions URL (includes the deployment and api-version)",
      type: "text", value: s.endpoint || "",
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
    title: s.configured ? "Replace your API key" : "Connect an LLM",
    body: (isAzure
        ? "Paste the <b>full chat completions URL</b> from the Azure portal, not just the " +
          "resource root &mdash; the deployment in its path is what selects the model. "
        : "Paste an OpenRouter API key. ") +
      "The key is encrypted with your password before it is stored, so it cannot be read from " +
      "the server's files without you. <b>An admin resetting your password will destroy it</b> " +
      "&mdash; you would add it again." +
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
  toast(isAzure ? `Connected to ${CHAT.model}.` : "Connected. Pick a model to start.", "ok");
  await loadModels(true);
  renderDock();
}

async function unlockKey() {
  const answer = await modal({
    title: "Unlock your LLM key",
    body: "Your key is saved but locked. This happens after the server restarts, because the " +
          "material that decrypts it is only ever held in memory &mdash; never written to disk. " +
          "Enter your password to unlock it.",
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
  toast("Chat unlocked.", "ok");
  renderDock();
}

async function removeKey() {
  if (!await confirmModal("Remove your stored API key?",
      "Chat stops working until you add a key again. Your conversations are kept.", "Remove")) return;
  const res = await api("/api/chat/key", { method: "DELETE" });
  if (res.ok) { CHAT.keyState = await res.json(); toast("Key removed.", "ok"); renderDock(); }
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
  if (!CHAT.keyState || !CHAT.keyState.unlocked) { openKeySettings(); return; }
  await loadModels();
  if (!CHAT.models.length) { toast("Could not load the model list from OpenRouter.", "error"); return; }

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
  const nearBottom = host.scrollHeight - host.scrollTop - host.clientHeight < 80;
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
  if (nearBottom) host.scrollTop = host.scrollHeight;
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

  if (!CHAT.keyState || !CHAT.keyState.configured) { openKeySettings(); return; }
  if (!CHAT.keyState.unlocked) { openKeySettings(); return; }
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

  let keyChip;
  if (!s.configured) keyChip = `<button type="button" class="chip warn" data-act="key">Connect an LLM</button>`;
  else if (!s.unlocked) keyChip = `<button type="button" class="chip warn" data-act="key">Locked &mdash; unlock</button>`;
  else keyChip = `<button type="button" class="chip" data-act="key">${escapeHtml(s.hint || "key")}</button>`;

  bar.innerHTML = `
    <button type="button" class="chip ${CURRENT_ORG ? "acc" : "warn"}" data-act="org">
      ${CURRENT_ORG ? `org: ${escapeHtml(CURRENT_ORG)}` : "no org"} &#9662;</button>
    <button type="button" class="chip" data-act="model">${escapeHtml(CHAT.model || "pick a model")} &#9662;</button>
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

/** The one entry point. `container` is any element; the dock passes a narrow
 *  aside, and a future full-page view can pass a wide one with no other
 *  change to this file. */
function mountChat(container) {
  container.innerHTML = `
    <div class="chat-head">
      <span class="chat-title">Ask</span>
      <div class="chat-head-actions">
        <button type="button" class="icon-btn" data-act="history" title="Conversations">&#9776;</button>
        <button type="button" class="icon-btn" data-act="share" title="Share transcript">&#8599;</button>
        <button type="button" class="icon-btn" data-act="new" title="New conversation">+</button>
        <button type="button" class="icon-btn" data-act="close" title="Close">&times;</button>
      </div>
    </div>
    <div class="chat-chips"></div>
    <div class="chat-transcript"></div>
    <div class="chat-footer"></div>
    <div class="chat-composer">
      <textarea class="chat-input" rows="2" placeholder="Ask about this org..."></textarea>
      <div class="chat-composer-actions">
        <span class="chat-hint">Enter to send &middot; Shift+Enter for a new line</span>
        <button type="button" class="primary chat-send">Send</button>
      </div>
    </div>`;

  CHAT.el = {
    chips: container.querySelector(".chat-chips"),
    transcript: container.querySelector(".chat-transcript"),
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
      if (act === "close") toggleChatDock(false);
    };
  });

  CHAT.el.input.addEventListener("keydown", e => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendMessage(); }
  });
  CHAT.el.input.addEventListener("input", e => {
    e.target.style.height = "auto";
    e.target.style.height = Math.min(e.target.scrollHeight, 160) + "px";
  });

  renderComposer();
  loadKeyState().then(() => { renderDock(); renderTranscript(); });
}

/** Deep link from anywhere in the app: opens the dock with a question
 *  pre-filled but NOT sent, so the engineer can edit it first. */
function askAbout(question) {
  toggleChatDock(true);
  if (CHAT.el.input) {
    CHAT.el.input.value = question;
    CHAT.el.input.focus();
    CHAT.el.input.dispatchEvent(new Event("input"));
  }
}

function toggleChatDock(open) {
  const dock = document.getElementById("chatDock");
  const root = document.getElementById("appRoot");
  CHAT.open = open === undefined ? !CHAT.open : open;
  dock.classList.toggle("open", CHAT.open);
  root.classList.toggle("dock-open", CHAT.open);
  try { localStorage.setItem("ts_chat_dock", CHAT.open ? "1" : "0"); } catch (e) { /* private mode */ }
  if (CHAT.open && !CHAT.el.input) mountChat(dock.querySelector(".chat-dock-inner"));
  if (CHAT.open && CHAT.el.input) CHAT.el.input.focus();
}

function initChatDock() {
  let wasOpen = false;
  try { wasOpen = localStorage.getItem("ts_chat_dock") === "1"; } catch (e) { /* ignore */ }
  if (wasOpen) toggleChatDock(true);
}

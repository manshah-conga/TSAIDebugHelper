/* Chat mode checks -- full screen vs the side dock, against the REAL index.html.
 *
 * test_ui_render.js runs app.js against a hand-written DOM stub, which is right
 * for the pure render functions but cannot catch the failure that matters here:
 * an id that exists in the script but not in the page, or a mount that binds
 * CHAT.el to nodes in the wrong container. So this one parses static/index.html
 * with jsdom and drives the real elements.
 *
 * Why the assertions are a string
 * -------------------------------
 * app.js and chat.js declare their state with top-level `let`/`const`
 * (CURRENT_USER, CHAT). Those bindings live in the script's own lexical scope,
 * not on `window` -- and each separate `window.eval()` in jsdom is its own
 * script, so a binding declared in one call is gone by the next. Driving the
 * page from outside would therefore be testing `window.CHAT === undefined`
 * rather than the app. Concatenating the two scripts and the checks into ONE
 * evaluation puts them all in the same scope, which is also how the browser
 * actually runs them.
 *
 * jsdom is a dev-only dependency: no npm package is needed to RUN this app,
 * and requirements.txt is Python. Install it to run this file:
 *
 *     npm install jsdom
 *     node tests/test_ui_chat_modes.js
 *
 * It skips cleanly when jsdom is absent, so a checkout without it still runs
 * the rest of the suite.
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

/* ---- the full-screen layout contract --------------------------------------
 * jsdom does no layout, so these read the declarations rather than measuring
 * pixels. They exist because the same visible symptom -- a narrow box in the
 * middle of a wide monitor -- was shipped twice from two unrelated causes, and
 * the second one (auto margins on a flex item suppressing cross-axis stretch)
 * is exactly the kind of thing that gets silently reintroduced by someone
 * tidying up a rule.
 */
function checkFullscreenCss(check) {
  const css = read("style.css");
  const rule = (css.match(/body\.chat-fullscreen main \{([^}]*)\}/) || [, ""])[1];
  check("the full-screen 'main' rule exists", !!rule.trim());
  check("...and clears the app's max-width cap", /max-width:\s*none/.test(rule), rule);
  check("...and clears the auto margins that stop a flex item stretching",
    /margin:\s*0\s*(;|$)/m.test(rule) || /margin:\s*0\s+0/.test(rule), rule);
  check("...and asks for the full width outright", /width:\s*100%/.test(rule), rule);
  check("the base 'main' rule is the one being overridden (auto margins)",
    /(^|\n)main \{[^}]*margin:\s*0 auto/.test(css));

  // Everything aligns to one variable, so a future change to the measure
  // cannot leave one row out of step with the others.
  const measure = (css.match(/--chat-measure:\s*([^;]+);/) || [, ""])[1].trim();
  check("a single --chat-measure drives the column", !!measure);
  check("...and it is full width by default", measure === "100%", measure);
  // Comments stripped first: the header comment above describes the old
  // hard-coded form on purpose, and matching prose would fail forever.
  const declarations = css.replace(/\/\*[\s\S]*?\*\//g, "");
  check("no hard-coded half-measure paddings are left",
    !/calc\(50%\s*-\s*\d+px\)/.test(declarations));
  for (const sel of [".chat-full-inner .chat-transcript",
                     ".chat-full-inner .chat-composer > *"]) {
    const body = (css.match(new RegExp(sel.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "\\s*\\{([^}]*)\\}")) || [, ""])[1];
    check(`${sel} derives its width from the variable`,
      /max-width:\s*var\(--chat-measure\)/.test(body), body);
  }
  const chipsRule = (css.match(/\.chat-full-inner \.chat-chips,[\s\S]*?\{([^}]*)\}/) || [, ""])[1];
  check("the chips/quota/footer rows align to the same derived padding",
    /var\(--chat-pad\)/.test(chipsRule), chipsRule);

  // Desktop-first: no viewport meta, so a phone renders at ~980px and zooms
  // out instead of switching the whole app to phone-width layout.
  check("index.html stays desktop-first (no viewport meta tag)",
    !/name=["']viewport["']/.test(read("index.html")));
}

const dom = new JSDOM(read("index.html"), { runScripts: "outside-only", url: "http://localhost/" });
const { window } = dom;

// Every fetch is stubbed: these checks are about DOM wiring, not data, and a
// real request would make them depend on a running server.
const routes = {
  "/api/auth/me": { status: 401 },
  "/api/chats": { json: [
    { chat_id: "c1", title: "Why did the quote recalc fail", org_id: "acme", updated_at: "2026-09-21T10:00:00Z" },
    { chat_id: "c2", title: "Flows with no fault path", updated_at: "2026-09-20T10:00:00Z" },
  ] },
  "/api/usage/me": { json: { totals: {}, by_day: [], quota: {
    tier: "unverified", source: "unverified", window_days: 30, unlimited: false, exceeded: false,
    daily: { used: 190000, limit: 200000, remaining: 10000, pct: 95, exceeded: false },
    window: { used: 190000, limit: 2000000, remaining: 1810000, pct: 9.5, exceeded: false },
    daily_resets_at: "2026-09-23T00:00:00Z",
  } } },
  "/api/llm": { json: { configured: true, provider: "azure", ready: true, can_manage: false } },
  "/api/llm/state": { json: { ready: true, using: "shared", can_manage: false } },
};
window.fetch = async (url) => {
  const key = Object.keys(routes).find(k => String(url).startsWith(k));
  const r = routes[key] || { json: {} };
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

const CHECKS = `
const check = window.__check, log = window.__log;
const $ = id => document.getElementById(id);
const ls = window.localStorage;

log("\\n-- sign in / sign up on one card --");
check("the page has a sign-up pane", !!$("signUpPane"));
check("it is hidden until asked for", $("signUpPane").style.display === "none");
check("the link to it is hidden until the server says registration is open",
  $("signupPrompt").style.display === "none");
showSignup(true);
check("choosing 'create one' swaps the panes in place",
  $("signUpPane").style.display === "" && $("signInPane").style.display === "none");
check("writer is preselected",
  document.querySelector('input[name="suRole"]:checked').value === "user");
check("reader is the only other option",
  [...document.querySelectorAll('input[name="suRole"]')].map(i => i.value).join(",") === "user,reader");
showSignup(false);
check("and it swaps back", $("signInPane").style.display === "");

log("\\n-- who sees what --");
const adminCards = () => [...document.querySelectorAll("[data-admin-only]")];
CURRENT_USER = { username: "dana", role: "user" };
applyRole();
check("a writer sees the Usage tab (their own numbers)", $("navUsage").style.display === "");
check("a writer does not see the Admin tab", $("navAdmin").style.display === "none");
check("...and there are cross-user cards to hide", adminCards().length >= 4);
check("the cross-user usage cards are hidden from a writer",
  adminCards().every(el => el.style.display === "none"),
  adminCards().map(el => el.style.display).join("|"));
CURRENT_USER = { username: "admin", role: "admin" };
applyRole();
check("an admin sees them", adminCards().every(el => el.style.display === ""));
check("an admin sees the Admin tab", $("navAdmin").style.display === "");

log("\\n-- full-screen chat --");
CURRENT_USER = { username: "dana", role: "user" };
enterChatFull();
check("the chat view becomes the active one", $("view-chat").classList.contains("active"));
check("the body is put into full-screen mode so the page stops scrolling",
  document.body.classList.contains("chat-fullscreen"));
check("the composer is mounted inside the full-screen host",
  !!$("chatFullInner").querySelector(".chat-input"));
check("CHAT.el points at the full-screen nodes",
  CHAT.el.input === $("chatFullInner").querySelector(".chat-input"));
check("the mode is recorded", CHAT.mode === "full");
check("the org chip is present, since the header's picker is out of view",
  !!$("chatFullInner").querySelector('[data-act="org"]'));
check("a collapse control is offered", !!$("chatFullInner").querySelector('[data-act="collapse"]'));
check("the full-screen composer starts taller than the dock's",
  $("chatFullInner").querySelector(".chat-input").getAttribute("rows") === "3");
check("Esc is advertised in the hint",
  $("chatFullInner").querySelector(".chat-hint").innerHTML.includes("Esc"));
check("the scroller and the transcript are separate elements",
  !!CHAT.el.scroll && CHAT.el.scroll !== CHAT.el.transcript);
check("a jump-to-latest control exists and starts hidden",
  !!CHAT.el.jump && CHAT.el.jump.style.display === "none");
check("the mode preference is remembered", ls.getItem("ts_chat_mode") === "full");

log("\\n-- the conversation rail --");
check("the rail starts collapsed", $("chatRail").classList.contains("collapsed"));
check("a peek control is there to bring it back", $("chatRailPeek").style.display !== "none");
toggleChatRail();
check("it pops out on demand", !$("chatRail").classList.contains("collapsed"));
check("the peek control gets out of the way", $("chatRailPeek").style.display === "none");
check("the choice is remembered", ls.getItem("ts_chat_rail") === "1");
toggleChatRail();
check("and it collapses again", $("chatRail").classList.contains("collapsed"));
check("collapsing is remembered too", ls.getItem("ts_chat_rail") === "0");

log("\\n-- switching between the two modes --");
CHAT.messages = [{ role: "user", content: "a question already asked" }];
renderTranscript();
collapseChatToDock();
check("collapsing leaves full screen", !document.body.classList.contains("chat-fullscreen"));
check("the dock opens", $("chatDock").classList.contains("open"));
check("the composer is re-mounted inside the dock",
  CHAT.el.input === $("chatDock").querySelector(".chat-input"));
check("the mode is updated", CHAT.mode === "dock");
check("the conversation survives the move", CHAT.messages.length === 1);
check("the transcript is rebuilt from state, not carried over as DOM",
  $("chatDock").querySelector(".chat-transcript").innerHTML.includes("a question already asked"),
  $("chatDock").querySelector(".chat-transcript").innerHTML.slice(0, 120));
check("the dock offers a way back to full screen",
  !!$("chatDock").querySelector('[data-act="expand"]'));
check("the preference now says dock", ls.getItem("ts_chat_mode") === "dock");
openChatFull();
check("...so the nav's Ask button honours it rather than forcing full screen",
  !document.body.classList.contains("chat-fullscreen"));

// Clicking it, not merely finding it. The first version of this check only
// asserted the button existed -- and it existed while doing nothing, because
// it was routed through the entry point that consults the remembered
// preference, which opening the dock had just set to "dock".
$("chatDock").querySelector('[data-act="expand"]').click();
check("clicking expand actually reaches full screen",
  document.body.classList.contains("chat-fullscreen")
  && $("view-chat").classList.contains("active"));
check("...and re-mounts the composer into the full-screen host",
  CHAT.mode === "full" && CHAT.el.input === $("chatFullInner").querySelector(".chat-input"));
check("...and closes the dock behind it", !$("chatDock").classList.contains("open"));
check("...and updates the preference, so Ask goes there next time",
  ls.getItem("ts_chat_mode") === "full");
check("the conversation is still there after expanding", CHAT.messages.length === 1);

// And back the other way, by click, for symmetry.
$("chatFullInner").querySelector('[data-act="collapse"]').click();
check("clicking collapse returns to the dock",
  !document.body.classList.contains("chat-fullscreen")
  && $("chatDock").classList.contains("open") && CHAT.mode === "dock");
$("chatDock").querySelector('[data-act="expand"]').click();
check("expand still works on a second round trip",
  document.body.classList.contains("chat-fullscreen") && CHAT.mode === "full");

log("\\n-- deep links open the dock, not full screen --");
enterChatFull();
toggleChatDock(false);
CHAT.mode = null;
CHAT.el = {};
askAbout("Which flows write Increment_Adjustment__c?");
check("askAbout opens the dock, so the record stays on screen",
  $("chatDock").classList.contains("open"));
check("the question is pre-filled but not sent",
  (CHAT.el.input.value || "").startsWith("Which flows write"));
check("nothing was sent", CHAT.streaming === false);

log("\\n-- ask from another tab after using full screen (2026-10-08 regression) --");
// Full-screen chat was used, then the engineer went to the Log Normalizer.
// CHAT.mode stays "full", and askAbout used to pre-fill the hidden composer.
enterChatFull();
showView("logs");
check("left full screen for another tab", CHAT.mode === "full"
  && !document.body.classList.contains("chat-fullscreen"));
askAbout("Analyze stored normalized log L1");
check("Ask the assistant opens the dock from another tab", $("chatDock").classList.contains("open"));
check("...with the question in the VISIBLE composer",
  CHAT.mode === "dock" && $("chatDock").contains(CHAT.el.input)
  && CHAT.el.input.value.startsWith("Analyze stored normalized log"));
toggleChatDock(false);
enterChatFull();
askAbout("Asked while in full screen");
check("in full screen it still pre-fills in place",
  CHAT.mode === "full" && CHAT.el.input.value === "Asked while in full screen"
  && !$("chatDock").classList.contains("open"));
`;

window.eval(read("app.js") + "\n;\n" + read("chat.js") + "\n;\n" + CHECKS);

console.log("\n-- the full-screen layout contract --");
checkFullscreenCss(window.__check);

console.log();
if (failures.length) {
  console.log(`${failures.length} FAILURE(S):`);
  failures.forEach(f => console.log("  - " + f));
  process.exit(1);
}
console.log("All chat mode checks passed.");

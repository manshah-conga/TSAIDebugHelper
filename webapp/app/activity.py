"""
Per-action activity ledger: who did what, through which channel, how often.

Why this exists alongside app/usage.py
---------------------------------------
usage.py answers "who spent LLM tokens". That misses most of the people who
get value from this app, because most of the value never touches the in-app
LLM:

  * an engineer driving the tools from Claude Desktop / Claude Code / Copilot
    over MCP spends their own client's model, not ours -- zero rows in the
    usage ledger, yet they may be the heaviest user of the knowledgebase;
  * the log normalizer is pure parsing -- no model at all;
  * lookups from the web UI (field writers, component detail, search) are
    deterministic reads.

So this ledger records ACTIONS, not tokens: one line per meaningful thing a
person did, whatever the channel.

One choke point: the HTTP API
-----------------------------
Every channel ends in this app's own HTTP API. The web UI calls it directly;
the in-app chat, the remote MCP endpoint and the local stdio MCP server all
run the tool bodies in mcp_server.py, which are thin httpx proxies over the
same routes. So a single ASGI middleware sees every action from every
channel -- including stdio MCP running on someone else's laptop, which this
process could not otherwise observe.

The tool bodies label their requests with three headers (see
mcp_server._telemetry_headers):

    X-TS-Channel   chat | mcp-remote | mcp-stdio
    X-TS-Tool      the MCP tool name
    X-TS-Client    the MCP client (Claude Desktop, claude-code, ...)

The headers are labels for analytics, not security: authorization is decided
by the token, as before. A caller can mislabel its own channel; it cannot act
as anyone else. A request with no channel header is "web" if it carries a
browser session token and "api" if it carries an API token (a script).

What is recorded, and what is deliberately not
----------------------------------------------
Recorded: time, username, role, channel, action name, category, org id, tool
name, MCP client name, HTTP status, duration, and a few numeric facts about
the action (a log's size and line count, how many exceptions it held).

Never recorded: request bodies, query strings, search text, log content,
field or component names, chat text. Those can carry customer data, and an
analytics file is the last place customer data should accumulate. A report
that says "kb.search x 41" is enough to steer the product; "searched for
<customer's field name>" is a liability.

Noise control
-------------
The UI polls some routes every 1.5s and loads list endpoints on every tab
switch. Recording those would bury the signal, so every route is classified
in CATALOG as:

    always  -- a deliberate act (connect an org, look up field writers)
    nonweb  -- a list/read that is a page load in the browser but a
               deliberate tool call over MCP/chat, so recorded only off-web
    never   -- polls, config reads, the analytics screens themselves

Tab switches, palette commands, tours and copy buttons never reach a route,
so the browser reports them through POST /api/activity/events (see
ingest_ui_events) against a fixed allowlist.

Storage mirrors usage.py: one append-only JSONL file per UTC day under
data/activity/, aggregated on read.
"""
import csv
import datetime
import glob
import io
import json
import os
import re
import time

from . import storage

MAX_DAYS = 400
DEFAULT_DAYS = 30

ENABLED = os.environ.get("TS_ACTIVITY_ENABLED", "1").strip().lower() not in ("0", "false", "no")

CHANNELS = {
    "web": "Web UI",
    "chat": "In-app AI chat",
    "mcp-remote": "MCP (remote /mcp)",
    "mcp-stdio": "MCP (local stdio)",
    "api": "API token (script, or an older MCP copy)",
}
MCP_CHANNELS = ("mcp-remote", "mcp-stdio")
_LABELLED_CHANNELS = ("chat", "mcp-remote", "mcp-stdio")

# (method, route template) -> (action, category, human label, mode)
CATALOG = {
    # account
    ("POST", "/api/auth/signup"): ("auth.signup", "Account", "Signed up", "always"),
    ("POST", "/api/auth/login"): ("auth.login", "Account", "Signed in", "always"),
    ("POST", "/api/auth/logout"): ("auth.logout", "Account", "Signed out", "always"),
    ("POST", "/api/auth/password"): ("auth.password_change", "Account", "Changed password", "always"),
    ("GET", "/api/auth/signup-config"): ("auth.signup_config", "Account", "", "never"),
    ("GET", "/api/auth/me"): ("auth.me", "Account", "", "never"),
    ("GET", "/api/build"): ("app.build", "Account", "", "never"),
    # admin
    ("GET", "/api/admin/users"): ("admin.users_list", "Admin", "Listed users", "nonweb"),
    ("POST", "/api/admin/users"): ("admin.user_create", "Admin", "Created a user", "always"),
    ("PATCH", "/api/admin/users/{username}/verified"): ("admin.user_verify", "Admin", "Verified a user", "always"),
    ("PATCH", "/api/admin/users/{username}/limits"): ("admin.user_limits", "Admin", "Set a user's limits", "always"),
    ("GET", "/api/admin/limits"): ("admin.limits_view", "Admin", "", "never"),
    ("PUT", "/api/admin/limits"): ("admin.limits_update", "Admin", "Changed default limits", "always"),
    ("PATCH", "/api/admin/users/{username}/role"): ("admin.user_role", "Admin", "Changed a role", "always"),
    ("PATCH", "/api/admin/users/{username}/disabled"): ("admin.user_disable", "Admin", "Enabled/disabled a user", "always"),
    ("POST", "/api/admin/users/{username}/reset-password"): ("admin.password_reset", "Admin", "Reset a password", "always"),
    ("DELETE", "/api/admin/users/{username}"): ("admin.user_delete", "Admin", "Deleted a user", "always"),
    ("GET", "/api/admin/usage"): ("admin.usage_view", "Admin", "", "never"),
    ("GET", "/api/admin/activity"): ("admin.activity_view", "Admin", "", "never"),
    ("GET", "/api/admin/activity/export"): ("admin.activity_export", "Admin", "Exported activity", "always"),
    ("POST", "/api/chat/key"): ("admin.llm_key_store", "Admin", "Stored an LLM key", "always"),
    ("DELETE", "/api/chat/key"): ("admin.llm_key_delete", "Admin", "Removed an LLM key", "always"),
    # tokens
    ("GET", "/api/tokens"): ("tokens.list", "Tokens", "Listed API tokens", "nonweb"),
    ("POST", "/api/tokens"): ("tokens.create", "Tokens", "Created an API token", "always"),
    ("DELETE", "/api/tokens/{token_id}"): ("tokens.revoke", "Tokens", "Revoked an API token", "always"),
    # orgs
    ("POST", "/api/orgs"): ("org.connect", "Orgs", "Connected an org", "always"),
    ("GET", "/api/orgs"): ("org.list", "Orgs", "Listed orgs", "nonweb"),
    ("GET", "/api/orgs/{org_id}/status"): ("org.status", "Orgs", "Checked fetch status", "nonweb"),
    ("GET", "/api/orgs/{org_id}/visibility"): ("org.visibility_view", "Orgs", "", "never"),
    ("PATCH", "/api/orgs/{org_id}/visibility"): ("org.visibility", "Orgs", "Changed org visibility", "always"),
    ("PATCH", "/api/orgs/{org_id}/account"): ("org.set_account", "Orgs", "Assigned org to account", "always"),
    ("GET", "/api/accounts"): ("account.list", "Orgs", "Listed accounts", "nonweb"),
    ("GET", "/api/accounts/suggest"): ("account.suggest", "Orgs", "", "never"),
    ("POST", "/api/accounts/rename"): ("account.rename", "Orgs", "Renamed an account", "always"),
    ("POST", "/api/orgs/{org_id}/refresh"): ("org.refresh", "Orgs", "Refreshed an org", "always"),
    # knowledgebase lookups
    ("GET", "/api/orgs/{org_id}/stats"): ("kb.stats", "Knowledgebase", "Org stats", "nonweb"),
    ("GET", "/api/orgs/{org_id}/components/{component_id}"): ("kb.component", "Knowledgebase", "Opened a component", "always"),
    ("GET", "/api/orgs/{org_id}/object-touch/{object_name}"): ("kb.object_touch", "Knowledgebase", "Object touch map", "always"),
    ("GET", "/api/orgs/{org_id}/field-writers/{field_name}"): ("kb.field_writers", "Knowledgebase", "Field writers", "always"),
    ("GET", "/api/orgs/{org_id}/search"): ("kb.search", "Knowledgebase", "Knowledgebase search", "always"),
    ("GET", "/api/orgs/{org_id}/inbound/{component_id}"): ("kb.inbound", "Knowledgebase", "Inbound references", "always"),
    ("GET", "/api/orgs/{org_id}/entry-points/{object_name}"): ("kb.entry_points", "Knowledgebase", "Entry points", "always"),
    # incidents / known issues
    ("POST", "/api/orgs/{org_id}/incidents"): ("incident.file", "Incidents", "Filed an incident", "always"),
    ("GET", "/api/orgs/{org_id}/incidents"): ("incident.list", "Incidents", "Listed incidents", "nonweb"),
    ("GET", "/api/orgs/{org_id}/incidents/{incident_id}"): ("incident.view", "Incidents", "Opened an incident", "always"),
    ("POST", "/api/orgs/{org_id}/resolve"): ("incident.resolve", "Incidents", "Recorded a resolution", "always"),
    ("GET", "/api/orgs/{org_id}/known-issues"): ("known.list", "Incidents", "Listed known issues", "nonweb"),
    ("GET", "/api/triage/known"): ("triage.known_lookup", "Incidents", "Triage: known-issue lookup", "always"),
    # logs
    ("POST", "/api/logs/normalize"): ("log.parse", "Logs", "Parsed a debug log", "always"),
    ("GET", "/api/logs"): ("log.list", "Logs", "Listed stored logs", "nonweb"),
    ("GET", "/api/logs/facets"): ("log.facets", "Logs", "", "never"),
    ("PATCH", "/api/logs/{log_id}"): ("log.update", "Logs", "Edited/archived a log", "always"),
    ("DELETE", "/api/logs/{log_id}"): ("log.delete", "Logs", "Deleted a log", "always"),
    ("GET", "/api/logs/{log_id}"): ("log.view", "Logs", "Opened a stored log", "always"),
    ("GET", "/api/logs/{log_id}/download"): ("log.download", "Logs", "Downloaded a log", "always"),
    # chat
    ("GET", "/api/chat/key"): ("chat.key_state", "Chat", "", "never"),
    ("GET", "/api/llm"): ("chat.llm_state", "Chat", "", "never"),
    ("POST", "/api/chat/unlock"): ("chat.unlock", "Chat", "Unlocked a personal key", "always"),
    ("POST", "/api/chat/default-model"): ("chat.set_model", "Chat", "Changed default model", "always"),
    ("GET", "/api/chat/models"): ("chat.models", "Chat", "", "never"),
    ("GET", "/api/chats"): ("chat.list", "Chat", "", "never"),
    ("POST", "/api/chats"): ("chat.new", "Chat", "Started a conversation", "always"),
    ("GET", "/api/chats/{chat_id}"): ("chat.open", "Chat", "Opened a conversation", "always"),
    ("DELETE", "/api/chats/{chat_id}"): ("chat.delete", "Chat", "Deleted a conversation", "always"),
    ("POST", "/api/chats/{chat_id}/messages"): ("chat.ask", "Chat", "Asked the AI", "always"),
    # sharing
    ("POST", "/api/chats/{chat_id}/share"): ("share.create", "Sharing", "Shared a conversation", "always"),
    ("DELETE", "/api/chats/{chat_id}/share"): ("share.revoke", "Sharing", "Unshared a conversation", "always"),
    ("GET", "/api/shared/{token}"): ("share.view", "Sharing", "Shared link opened", "always"),
    # self-service reads that are the analytics/onboarding screens themselves
    ("GET", "/api/usage/me"): ("usage.me", "Account", "", "never"),
    ("GET", "/api/me/guide"): ("guide.get", "Account", "", "never"),
    ("POST", "/api/me/guide"): ("guide.post", "Account", "", "never"),
    ("GET", "/api/home"): ("home.view", "Account", "", "never"),
    ("GET", "/api/activity/me"): ("activity.me", "Account", "", "never"),
    ("POST", "/api/activity/events"): ("activity.ui", "UI", "", "never"),
}

# Actions an unauthenticated caller can meaningfully perform. Anything else
# with no identity is dropped: a 401 is not an action anyone took.
ANONYMOUS_OK = {"share.view"}

# Browser-only interactions (never reach a route). Fixed allowlist so the
# beacon cannot be used to write arbitrary strings into the ledger.
UI_ACTIONS = {
    "ui.view": ("Navigation", "Opened a tab"),
    "ui.palette_open": ("Navigation", "Opened the command palette"),
    "ui.palette_run": ("Navigation", "Ran a palette command"),
    "ui.tour_start": ("Help", "Started a tour"),
    "ui.tour_done": ("Help", "Finished a tour"),
    "ui.help_open": ("Help", "Opened the help drawer"),
    "ui.demo_open": ("Help", "Opened the demo case"),
    "ui.triage_submit": ("Incidents", "Used the triage bar"),
    "ui.chat_dock": ("Chat", "Opened the chat dock"),
    "ui.copy": ("Productivity", "Copied to clipboard"),
    "ui.export": ("Productivity", "Exported/downloaded from the UI"),
    "ui.log_upload": ("Logs", "Chose a log to parse"),
    "ui.feedback": ("Chat", "Rated an answer"),
}
_UI_META_KEYS = ("view", "command", "tour", "target", "kind", "value")
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_.:\-/ ]{1,60}$")
UI_BATCH_MAX = 50
# Per-account ceiling on browser-reported events per UTC hour. Normal use is
# tens; this only stops a stuck loop (or a curious user) filling the ledger.
UI_HOURLY_MAX = int(os.environ.get("TS_ACTIVITY_UI_HOURLY_MAX", "600") or 600)
_UI_BUDGET = {}

# Categories that measure outcomes rather than navigation, in the order the
# adoption ladder presents them.
LADDER = [
    ("active", "Active", lambda a: True),
    ("kb", "Looked up org metadata", lambda a: a.startswith("kb.")),
    ("log", "Parsed a debug log", lambda a: a == "log.parse"),
    ("incident", "Filed an incident", lambda a: a == "incident.file"),
    ("resolve", "Recorded a resolution", lambda a: a == "incident.resolve"),
]


def _now():
    return datetime.datetime.utcnow()


def _today():
    return _now().date()


def _day_path(day):
    return os.path.join(storage.activity_root(), f"{day.isoformat()}.jsonl")


def _days_in_range(days):
    days = max(1, min(int(days or DEFAULT_DAYS), MAX_DAYS))
    today = _today()
    return [today - datetime.timedelta(days=i) for i in range(days - 1, -1, -1)]


def _clean_label(value, limit=60):
    """Header-borne labels are caller-controlled: keep them short and to a
    safe character set so a report can render them without surprises."""
    if not value:
        return None
    value = str(value).strip()[:limit]
    value = re.sub(r"[^A-Za-z0-9_.:\-/ ()@+]", "", value)
    return value or None


# ---------------------------------------------------------------- writing

def record(event):
    """Append one event. Never raises: losing an analytics line must never
    turn a user's successful action into an error."""
    if not ENABLED:
        return
    try:
        event.setdefault("at", _now().strftime("%Y-%m-%dT%H:%M:%SZ"))
        day = datetime.date.fromisoformat(event["at"][:10])
        storage.append_jsonl(_day_path(day), event)
    except Exception:  # noqa: BLE001
        pass


def note(request, **meta):
    """Attach facts to the event the middleware will write for this request.

    Routes call this for things only they know: a parsed log's size, whether
    it was stored. `username=` identifies the actor on routes that
    authenticate themselves (login, signup, logout) rather than through a
    role dependency. Values must be scalars; anything else is dropped."""
    try:
        state = request.scope.setdefault("state", {})
        bag = state.setdefault("activity_meta", {})
        for k, v in meta.items():
            if v is None or isinstance(v, (bool, int, float)) or (isinstance(v, str) and len(v) <= 80):
                bag[k] = v
    except Exception:  # noqa: BLE001
        pass


def resolve_channel(headers, ident):
    label = (headers.get("x-ts-channel") or "").strip().lower()
    if label in _LABELLED_CHANNELS:
        return label
    if ident and ident.get("kind") == "session":
        return "web"
    return "api"


def _match_route(routes, scope):
    try:
        from starlette.routing import Match
    except Exception:  # noqa: BLE001
        return None
    for route in routes:
        path = getattr(route, "path", None)
        if not path or not hasattr(route, "matches"):
            continue
        try:
            match, child = route.matches(scope)
        except Exception:  # noqa: BLE001
            continue
        if match == Match.FULL:
            return route, child.get("path_params") or {}
    return None


def build_event(scope, method, template, path_params, status, ms):
    """Turn one finished request into an event, or None if it is noise."""
    headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
    state = scope.get("state") or {}
    ident = state.get("activity_ident")
    meta = dict(state.get("activity_meta") or {})
    username = (ident or {}).get("username") or meta.pop("username", None)
    role = (ident or {}).get("role") or meta.pop("role", None)
    meta.pop("username", None)
    meta.pop("role", None)
    label = (headers.get("x-ts-channel") or "").strip().lower()
    if ident:
        channel = resolve_channel(headers, ident)
    elif label in _LABELLED_CHANNELS:
        channel = label
    else:
        # Routes that authenticate themselves (login, signup) and anonymous
        # shared-link views are browser traffic.
        channel = "web"

    entry = CATALOG.get((method, template))
    if entry:
        action, category, _label, mode = entry
    else:
        action, category, mode = f"{method.lower()} {template}", "Other", (
            "always" if method in ("POST", "PUT", "PATCH", "DELETE") else "nonweb")
    if mode == "never":
        return None
    if mode == "nonweb" and channel == "web":
        return None
    if not username and action not in ANONYMOUS_OK:
        return None
    # A 401 on a login is a failed guess, not an action by the account named.
    if action == "auth.login" and status and status >= 400:
        return None

    ev = {
        "kind": "action",
        "username": username,
        "role": role,
        "channel": channel,
        "action": action,
        "category": category,
        "org_id": path_params.get("org_id") or meta.pop("org_id", None),
        "tool": _clean_label(headers.get("x-ts-tool")),
        "client": _clean_label(headers.get("x-ts-client")),
        "status": status,
        "ok": bool(status and status < 400),
        "ms": ms,
    }
    if ident and ident.get("kind"):
        ev["token_kind"] = ident.get("kind")
    if meta:
        ev["meta"] = meta
    return ev


class ActivityMiddleware:
    """Pure ASGI, so a streaming chat answer is timed to its last byte and the
    route template can be matched after the router has run."""

    def __init__(self, app, router=None):
        self.app = app
        self.router = router

    async def __call__(self, scope, receive, send):
        if (not ENABLED or scope.get("type") != "http"
                or not scope.get("path", "").startswith("/api/")):
            await self.app(scope, receive, send)
            return
        start = time.perf_counter()
        status = [None]

        async def _send(message):
            if message.get("type") == "http.response.start":
                status[0] = message.get("status")
            await send(message)

        try:
            await self.app(scope, receive, _send)
        except Exception:
            status[0] = status[0] or 500
            raise
        finally:
            try:
                await self._finish(scope, status[0], start)
            except Exception:  # noqa: BLE001
                pass

    async def _finish(self, scope, status, start):
        ms = int((time.perf_counter() - start) * 1000)
        routes = getattr(self.router, "routes", None) or []
        found = _match_route(routes, scope)
        if not found:
            return
        route, params = found
        ev = build_event(scope, scope.get("method", "GET").upper(), route.path, params, status, ms)
        if ev is None:
            return
        try:
            import anyio
            await anyio.to_thread.run_sync(record, ev)
        except Exception:  # noqa: BLE001
            record(ev)


def record_mcp_connect(username, role, client_name, client_version=None, protocol=None):
    """An MCP client's initialize handshake on the remote endpoint -- the
    closest thing stateless HTTP has to "a session started"."""
    meta = {}
    if client_version:
        meta["client_version"] = _clean_label(client_version, 30)
    if protocol:
        meta["protocol"] = _clean_label(protocol, 30)
    ev = {"kind": "action", "username": username, "role": role, "channel": "mcp-remote",
          "action": "mcp.connect", "category": "MCP", "org_id": None, "tool": None,
          "client": _clean_label(client_name), "status": 200, "ok": True, "ms": None}
    if meta:
        ev["meta"] = meta
    record(ev)


def ingest_ui_events(ident, events):
    """Browser-reported interactions. Unknown actions are dropped silently:
    an old cached app.js sending a retired name must not error the page."""
    accepted = 0
    now = _now()
    bucket = (ident["username"], now.strftime("%Y%m%d%H"))
    if len(_UI_BUDGET) > 5000:
        _UI_BUDGET.clear()
    for raw in (events or [])[:UI_BATCH_MAX]:
        if _UI_BUDGET.get(bucket, 0) >= UI_HOURLY_MAX:
            break
        if not isinstance(raw, dict):
            continue
        action = raw.get("action")
        if action not in UI_ACTIONS:
            continue
        category, _label = UI_ACTIONS[action]
        meta = {}
        for k in _UI_META_KEYS:
            v = (raw.get("meta") or {}).get(k) if isinstance(raw.get("meta"), dict) else None
            if isinstance(v, str) and _SAFE_TOKEN.match(v):
                meta[k] = v
            elif isinstance(v, (int, float)) and not isinstance(v, bool):
                meta[k] = v
        at = now
        ts = raw.get("at")
        if isinstance(ts, (int, float)):
            # Client clock, bounded: a batch is at most a few minutes old.
            try:
                cand = datetime.datetime.utcfromtimestamp(ts / 1000.0)
                if abs((now - cand).total_seconds()) < 900:
                    at = cand
            except (OverflowError, OSError, ValueError):
                pass
        ev = {"kind": "ui", "at": at.strftime("%Y-%m-%dT%H:%M:%SZ"),
              "username": ident["username"], "role": ident.get("role"),
              "channel": "web", "action": action, "category": category,
              "org_id": _clean_label((raw.get("meta") or {}).get("org_id")) if isinstance(raw.get("meta"), dict) else None,
              "tool": None, "client": None, "status": None, "ok": True, "ms": None}
        if meta:
            ev["meta"] = meta
        record(ev)
        _UI_BUDGET[bucket] = _UI_BUDGET.get(bucket, 0) + 1
        accepted += 1
    return accepted


# ---------------------------------------------------------------- reading

def load_records(days=DEFAULT_DAYS, username=None):
    out = []
    for day in _days_in_range(days):
        for rec in storage.read_jsonl(_day_path(day)):
            if username and rec.get("username") != username:
                continue
            out.append(rec)
    return out


def _pct(values, p):
    if not values:
        return None
    values = sorted(values)
    k = max(0, min(len(values) - 1, int(round((p / 100.0) * (len(values) - 1)))))
    return values[k]


def _label_for(action):
    for (_m, _t), (a, _c, label, _mode) in CATALOG.items():
        if a == action and label:
            return label
    if action in UI_ACTIONS:
        return UI_ACTIONS[action][1]
    if action == "mcp.connect":
        return "MCP client connected"
    return action


def _segment(channels, llm_turns):
    uses_llm = "chat" in channels or llm_turns > 0
    uses_mcp = any(c in channels for c in MCP_CHANNELS)
    if uses_llm and uses_mcp:
        return "AI chat + MCP"
    if uses_mcp:
        return "MCP only (no in-app LLM)"
    if uses_llm:
        return "In-app AI chat"
    if "api" in channels:
        return "API scripts"
    return "Web UI only (no AI)"


def report(days=DEFAULT_DAYS, username=None, llm_records=None, registry=None):
    """Everything the Activity screen shows, in one pass over the window.

    `llm_records` (from usage.load_records) lets the per-user table show LLM
    turns next to non-LLM actions -- the comparison that answers "who gets
    value without touching the model?"."""
    records = load_records(days, username=username)
    day_list = _days_in_range(days)
    llm_records = llm_records or []
    registry = registry or {}

    actions = [r for r in records if r.get("kind", "action") == "action"]
    ui = [r for r in records if r.get("kind") == "ui"]

    # --- per day, per channel
    by_day = {d.isoformat(): {"date": d.isoformat(), "events": 0, "users": set(),
                               "channels": {c: 0 for c in CHANNELS}} for d in day_list}
    for r in actions:
        dk = (r.get("at") or "")[:10]
        if dk in by_day:
            b = by_day[dk]
            b["events"] += 1
            if r.get("username"):
                b["users"].add(r["username"])
            ch = r.get("channel") or "web"
            b["channels"][ch] = b["channels"].get(ch, 0) + 1
    for r in ui:
        dk = (r.get("at") or "")[:10]
        if dk in by_day and r.get("username"):
            by_day[dk]["users"].add(r["username"])
    day_rows = []
    for k in sorted(by_day):
        b = by_day[k]
        day_rows.append({"date": k, "events": b["events"], "active_users": len(b["users"]),
                         "channels": b["channels"]})

    # --- channels
    chan = {}
    for r in actions:
        ch = r.get("channel") or "web"
        c = chan.setdefault(ch, {"channel": ch, "label": CHANNELS.get(ch, ch), "events": 0,
                                  "users": set(), "errors": 0})
        c["events"] += 1
        if r.get("username"):
            c["users"].add(r["username"])
        if not r.get("ok", True):
            c["errors"] += 1
    channel_rows = sorted(({**v, "users": len(v["users"])} for v in chan.values()),
                          key=lambda x: x["events"], reverse=True)

    # --- features (actions + ui)
    feat = {}
    for r in actions + ui:
        a = r.get("action") or "?"
        f = feat.setdefault(a, {"action": a, "label": _label_for(a), "category": r.get("category") or "Other",
                                "kind": r.get("kind", "action"), "count": 0, "users": set(),
                                "errors": 0, "ms": [], "channels": {}})
        f["count"] += 1
        if r.get("username"):
            f["users"].add(r["username"])
        if not r.get("ok", True):
            f["errors"] += 1
        if isinstance(r.get("ms"), (int, float)):
            f["ms"].append(r["ms"])
        ch = r.get("channel") or "web"
        f["channels"][ch] = f["channels"].get(ch, 0) + 1
    feature_rows = []
    for f in feat.values():
        feature_rows.append({"action": f["action"], "label": f["label"], "category": f["category"],
                             "kind": f["kind"], "count": f["count"], "users": len(f["users"]),
                             "errors": f["errors"], "p50_ms": _pct(f["ms"], 50), "p95_ms": _pct(f["ms"], 95),
                             "channels": f["channels"]})
    feature_rows.sort(key=lambda x: x["count"], reverse=True)

    cats = {}
    for f in feature_rows:
        c = cats.setdefault(f["category"], {"category": f["category"], "count": 0, "users": set()})
        c["count"] += f["count"]
    for r in actions + ui:
        if r.get("username"):
            cats.setdefault(r.get("category") or "Other", {"category": r.get("category") or "Other",
                                                            "count": 0, "users": set()})["users"].add(r["username"])
    category_rows = sorted(({**v, "users": len(v["users"])} for v in cats.values()),
                           key=lambda x: x["count"], reverse=True)

    # --- MCP / chat tools
    tools = {}
    for r in actions:
        t = r.get("tool")
        if not t:
            continue
        x = tools.setdefault(t, {"tool": t, "calls": 0, "users": set(), "errors": 0, "ms": [], "channels": {}})
        x["calls"] += 1
        if r.get("username"):
            x["users"].add(r["username"])
        if not r.get("ok", True):
            x["errors"] += 1
        if isinstance(r.get("ms"), (int, float)):
            x["ms"].append(r["ms"])
        ch = r.get("channel") or "?"
        x["channels"][ch] = x["channels"].get(ch, 0) + 1
    tool_rows = sorted(({"tool": x["tool"], "calls": x["calls"], "users": len(x["users"]),
                         "errors": x["errors"], "p50_ms": _pct(x["ms"], 50), "p95_ms": _pct(x["ms"], 95),
                         "channels": x["channels"]} for x in tools.values()),
                       key=lambda x: x["calls"], reverse=True)

    # --- MCP clients
    clients = {}
    for r in actions:
        if r.get("channel") not in MCP_CHANNELS:
            continue
        name = r.get("client") or "(unidentified client)"
        c = clients.setdefault(name, {"client": name, "calls": 0, "connects": 0, "users": set(),
                                      "channels": set(), "last_seen": None})
        if r.get("action") == "mcp.connect":
            c["connects"] += 1
        else:
            c["calls"] += 1
        if r.get("username"):
            c["users"].add(r["username"])
        c["channels"].add(r.get("channel"))
        if not c["last_seen"] or (r.get("at") or "") > c["last_seen"]:
            c["last_seen"] = r.get("at")
    client_rows = sorted(({**v, "users": len(v["users"]), "channels": sorted(v["channels"])}
                          for v in clients.values()), key=lambda x: x["calls"], reverse=True)

    # --- log parser
    parses = [r for r in actions if r.get("action") == "log.parse"
              or (r.get("action") == "incident.file" and (r.get("meta") or {}).get("has_log"))]
    ok_parses = [r for r in parses if r.get("ok", True)]
    kb_sizes = [(r.get("meta") or {}).get("size_kb") for r in ok_parses]
    kb_sizes = [x for x in kb_sizes if isinstance(x, (int, float))]
    parse_ms = [(r.get("meta") or {}).get("parse_ms") for r in ok_parses]
    parse_ms = [x for x in parse_ms if isinstance(x, (int, float))]
    by_ch = {}
    for r in parses:
        ch = r.get("channel") or "web"
        by_ch[ch] = by_ch.get(ch, 0) + 1
    log_parser = {
        "parses": len(parses),
        "failed": len(parses) - len(ok_parses),
        "users": len({r.get("username") for r in parses if r.get("username")}),
        "stored": sum(1 for r in ok_parses if (r.get("meta") or {}).get("stored")),
        "via_incident": sum(1 for r in parses if r.get("action") == "incident.file"),
        "total_mb": round(sum(kb_sizes) / 1024.0, 2),
        "avg_kb": round(sum(kb_sizes) / len(kb_sizes), 1) if kb_sizes else None,
        "max_kb": max(kb_sizes) if kb_sizes else None,
        "p50_parse_ms": _pct(parse_ms, 50),
        "p95_parse_ms": _pct(parse_ms, 95),
        "exceptions_found": sum(int((r.get("meta") or {}).get("exceptions") or 0) for r in ok_parses),
        "logs_with_exceptions": sum(1 for r in ok_parses if (r.get("meta") or {}).get("exceptions")),
        "by_channel": by_ch,
    }

    # --- LLM joins
    llm_by_user = {}
    for rec in llm_records:
        u = rec.get("username") or "(unknown)"
        x = llm_by_user.setdefault(u, {"turns": 0, "tokens": 0})
        x["turns"] += 1
        x["tokens"] += int(rec.get("total_tokens") or 0)

    # --- per user
    users = {}
    for r in actions + ui:
        u = r.get("username")
        if not u:
            continue
        x = users.setdefault(u, {"username": u, "role": r.get("role"), "events": 0, "ui_events": 0,
                                 "days": set(), "channels": set(), "actions": {}, "errors": 0,
                                 "mcp_calls": 0, "chat_tool_calls": 0, "logs_parsed": 0,
                                 "incidents_filed": 0, "resolutions": 0, "kb_lookups": 0,
                                 "last_seen": None, "first_seen": None, "clients": set(), "orgs": set()})
        if r.get("kind") == "ui":
            x["ui_events"] += 1
        else:
            x["events"] += 1
            x["channels"].add(r.get("channel") or "web")
            a = r.get("action") or "?"
            x["actions"][a] = x["actions"].get(a, 0) + 1
            if r.get("channel") in MCP_CHANNELS and a != "mcp.connect":
                x["mcp_calls"] += 1
            if r.get("channel") == "chat":
                x["chat_tool_calls"] += 1
            if a == "log.parse" or (a == "incident.file" and (r.get("meta") or {}).get("has_log")):
                x["logs_parsed"] += 1
            if a == "incident.file":
                x["incidents_filed"] += 1
            if a == "incident.resolve":
                x["resolutions"] += 1
            if a.startswith("kb."):
                x["kb_lookups"] += 1
            if not r.get("ok", True):
                x["errors"] += 1
            if r.get("client"):
                x["clients"].add(r["client"])
        if r.get("org_id"):
            x["orgs"].add(r["org_id"])
        at = r.get("at") or ""
        x["days"].add(at[:10])
        if not x["last_seen"] or at > x["last_seen"]:
            x["last_seen"] = at
        if not x["first_seen"] or at < x["first_seen"]:
            x["first_seen"] = at
        if r.get("role"):
            x["role"] = r["role"]
    for u in llm_by_user:
        if u in users or u == "(unknown)":
            continue
        users[u] = {"username": u, "role": None, "events": 0, "ui_events": 0, "days": set(),
                    "channels": set(), "actions": {}, "errors": 0, "mcp_calls": 0, "chat_tool_calls": 0,
                    "logs_parsed": 0, "incidents_filed": 0, "resolutions": 0, "kb_lookups": 0,
                    "last_seen": None, "first_seen": None, "clients": set(), "orgs": set()}
    user_rows = []
    for u, x in users.items():
        llm = llm_by_user.get(u, {"turns": 0, "tokens": 0})
        top = max(x["actions"].items(), key=lambda kv: kv[1])[0] if x["actions"] else None
        user_rows.append({
            "username": u, "role": x["role"], "events": x["events"], "ui_events": x["ui_events"],
            "active_days": len(x["days"]), "channels": sorted(x["channels"]),
            "segment": _segment(x["channels"], llm["turns"]),
            "llm_turns": llm["turns"], "llm_tokens": llm["tokens"],
            "mcp_calls": x["mcp_calls"], "chat_tool_calls": x["chat_tool_calls"],
            "kb_lookups": x["kb_lookups"], "logs_parsed": x["logs_parsed"],
            "incidents_filed": x["incidents_filed"], "resolutions": x["resolutions"],
            "errors": x["errors"], "top_action": top, "top_action_label": _label_for(top) if top else None,
            "clients": sorted(x["clients"]), "orgs": len(x["orgs"]),
            "first_seen": x["first_seen"], "last_seen": x["last_seen"],
        })
    user_rows.sort(key=lambda x: (x["events"] + x["llm_turns"]), reverse=True)

    segments = {}
    for row in user_rows:
        s = segments.setdefault(row["segment"], {"segment": row["segment"], "users": 0, "events": 0})
        s["users"] += 1
        s["events"] += row["events"]
    segment_rows = sorted(segments.values(), key=lambda x: x["users"], reverse=True)

    # --- adoption ladder
    per_user_actions = {}
    for r in actions:
        if r.get("username") and r.get("ok", True):
            per_user_actions.setdefault(r["username"], set()).add(r.get("action") or "")
    for r in ui:
        if r.get("username"):
            per_user_actions.setdefault(r["username"], set())
    ladder = []
    for key, label, pred in LADDER:
        n = sum(1 for acts in per_user_actions.values() if key == "active" or any(pred(a) for a in acts))
        ladder.append({"step": key, "label": label, "users": n})

    # --- per org
    orgs = {}
    for r in actions:
        o = r.get("org_id")
        if not o:
            continue
        x = orgs.setdefault(o, {"org_id": o, "account": (registry.get(o) or {}).get("account"),
                                "org_name": (registry.get(o) or {}).get("org_name"),
                                "events": 0, "users": set(), "channels": {}, "actions": {}, "last_seen": None})
        x["events"] += 1
        if r.get("username"):
            x["users"].add(r["username"])
        ch = r.get("channel") or "web"
        x["channels"][ch] = x["channels"].get(ch, 0) + 1
        a = r.get("action") or "?"
        x["actions"][a] = x["actions"].get(a, 0) + 1
        if not x["last_seen"] or (r.get("at") or "") > x["last_seen"]:
            x["last_seen"] = r.get("at")
    org_rows = []
    for x in orgs.values():
        top = max(x["actions"].items(), key=lambda kv: kv[1])[0] if x["actions"] else None
        org_rows.append({"org_id": x["org_id"], "org_name": x["org_name"], "account": x["account"],
                         "events": x["events"], "users": len(x["users"]), "channels": x["channels"],
                         "top_action": top, "top_action_label": _label_for(top) if top else None,
                         "incidents": x["actions"].get("incident.file", 0),
                         "last_seen": x["last_seen"]})
    org_rows.sort(key=lambda x: x["events"], reverse=True)

    accounts = {}
    for row in org_rows:
        a = row["account"] or "(unassigned)"
        x = accounts.setdefault(a, {"account": a, "events": 0, "orgs": 0, "incidents": 0})
        x["events"] += row["events"]
        x["orgs"] += 1
        x["incidents"] += row["incidents"]
    account_rows = sorted(accounts.values(), key=lambda x: x["events"], reverse=True)

    # --- hour of day (UTC)
    hours = [0] * 24
    for r in actions:
        try:
            hours[int((r.get("at") or "")[11:13])] += 1
        except (ValueError, IndexError):
            pass

    # --- errors
    errs = {}
    for r in actions:
        if r.get("ok", True):
            continue
        k = (r.get("action"), r.get("status"))
        x = errs.setdefault(k, {"action": r.get("action"), "label": _label_for(r.get("action")),
                                "status": r.get("status"), "count": 0, "users": set(), "channels": {}})
        x["count"] += 1
        if r.get("username"):
            x["users"].add(r["username"])
        ch = r.get("channel") or "web"
        x["channels"][ch] = x["channels"].get(ch, 0) + 1
    error_rows = sorted(({**v, "users": len(v["users"])} for v in errs.values()),
                        key=lambda x: x["count"], reverse=True)

    recent = sorted(actions + ui, key=lambda r: r.get("at") or "", reverse=True)[:100]
    recent = [{"at": r.get("at"), "username": r.get("username"), "channel": r.get("channel"),
               "action": r.get("action"), "label": _label_for(r.get("action")),
               "category": r.get("category"), "org_id": r.get("org_id"), "tool": r.get("tool"),
               "client": r.get("client"), "ok": r.get("ok", True), "status": r.get("status"),
               "ms": r.get("ms"), "kind": r.get("kind", "action")} for r in recent]

    active_users = {r.get("username") for r in actions + ui if r.get("username")}
    return {
        "days": len(day_list),
        "from": day_list[0].isoformat(),
        "to": day_list[-1].isoformat(),
        "scope": username or "all users",
        "channel_labels": CHANNELS,
        "totals": {
            "actions": len(actions),
            "ui_events": len(ui),
            "active_users": len(active_users),
            "active_days": len({(r.get("at") or "")[:10] for r in actions + ui}),
            "errors": sum(1 for r in actions if not r.get("ok", True)),
            "mcp_calls": sum(1 for r in actions if r.get("channel") in MCP_CHANNELS and r.get("action") != "mcp.connect"),
            "chat_tool_calls": sum(1 for r in actions if r.get("channel") == "chat"),
            "llm_turns": len(llm_records),
            "logs_parsed": log_parser["parses"],
            "incidents_filed": sum(1 for r in actions if r.get("action") == "incident.file" and r.get("ok", True)),
            "resolutions": sum(1 for r in actions if r.get("action") == "incident.resolve" and r.get("ok", True)),
            "kb_lookups": sum(1 for r in actions if (r.get("action") or "").startswith("kb.")),
            "non_llm_users": sum(1 for row in user_rows if row["llm_turns"] == 0 and "chat" not in row["channels"]
                                 and row["events"] > 0),
        },
        "by_day": day_rows,
        "by_channel": channel_rows,
        "by_category": category_rows,
        "features": feature_rows,
        "tools": tool_rows,
        "mcp_clients": client_rows,
        "log_parser": log_parser,
        "by_user": user_rows,
        "segments": segment_rows,
        "ladder": ladder,
        "by_org": org_rows,
        "by_account": account_rows,
        "by_hour": hours,
        "errors": error_rows,
        "recent": recent,
    }


def my_report(username, days=DEFAULT_DAYS, llm_records=None, registry=None):
    """A user's own view. Everything in it is already scoped to them; the
    cross-user sections (segments, the ladder, other people) are removed so
    the payload cannot be read as a leaderboard."""
    r = report(days=days, username=username, llm_records=llm_records, registry=registry)
    for k in ("segments", "ladder", "by_account", "by_hour"):
        r.pop(k, None)
    me = next((u for u in r["by_user"] if u["username"] == username), None)
    r["me"] = me
    r.pop("by_user", None)
    r["recent"] = r["recent"][:50]
    return r


EXPORT_FIELDS = ["at", "kind", "username", "role", "channel", "action", "category", "org_id",
                 "tool", "client", "status", "ok", "ms", "token_kind", "meta"]


def export_csv(days=DEFAULT_DAYS, username=None):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=EXPORT_FIELDS, extrasaction="ignore")
    w.writeheader()
    for rec in load_records(days, username=username):
        row = dict(rec)
        if isinstance(row.get("meta"), dict):
            row["meta"] = json.dumps(row["meta"], separators=(",", ":"))
        # Spreadsheet formula injection: a client name like "=HYPERLINK(...)"
        # arrives in a header the caller controls, and this file is made to be
        # opened in Excel.
        for k, v in row.items():
            if isinstance(v, str) and v[:1] in ("=", "+", "-", "@"):
                row[k] = "'" + v
        w.writerow(row)
    return buf.getvalue()


# ---------------------------------------------------------------- housekeeping

def prune(keep_days=None):
    if keep_days is None:
        try:
            keep_days = int(os.environ.get("TS_ACTIVITY_RETENTION_DAYS", "365"))
        except ValueError:
            keep_days = 365
    cutoff = _today() - datetime.timedelta(days=keep_days)
    removed = []
    for path in glob.glob(os.path.join(storage.activity_root(), "*.jsonl")):
        stem = os.path.splitext(os.path.basename(path))[0]
        try:
            day = datetime.date.fromisoformat(stem)
        except ValueError:
            continue
        if day < cutoff:
            try:
                os.remove(path)
                removed.append(stem)
            except OSError:
                pass
    return removed


def forget_user(username):
    """Account deletion: rewrite each day file without that user."""
    touched = 0
    for path in glob.glob(os.path.join(storage.activity_root(), "*.jsonl")):
        with storage.locked(path):
            records = storage.read_jsonl(path)
            keep = [r for r in records if r.get("username") != username]
            if len(keep) == len(records):
                continue
            tmp = path + ".rewrite"
            with open(tmp, "w", encoding="utf-8") as f:
                for rec in keep:
                    f.write(json.dumps(rec, default=str, separators=(",", ":")) + "\n")
            os.replace(tmp, path)
            touched += 1
    return touched

"""
Onboarding, the home page summary, and "have we seen this before?" matching.

Three things live here because they all answer the same question -- what
should a person see when they open the app -- and none of them belongs to an
existing module.

1. Guide state (per user, data/guide/<username>.json)
   What the account has already done and seen: the getting-started checklist,
   which tabs have been visited, which tours finished, the last "what's new"
   entry read, plus a few UI preferences (pinned orgs, cards vs table, whether
   the Connect card is open). Stored server-side rather than in localStorage
   so it follows the person across browsers -- someone who finished the tour
   on their laptop should not be offered it again on the jump box.

   The checklist ticks itself. Most items are recorded by the browser when
   the action succeeds (`EVENTS`), but the two that can happen outside the
   browser -- connecting an org and creating an API token, both reachable
   over MCP -- are derived from the real stores every time, so they are right
   however the person did them.

   "Until the person understands it" is defined as: the checklist is complete
   or they dismissed it. Until then the nav shows dots on unvisited tabs and
   the home page shows the checklist. After that, the only way back in is the
   help drawer, which is always there.

2. Home summary
   Per visible org: incident count, known issues with and without a fix, and
   when the last incident was filed -- the numbers the org cards need. For
   readers, the latest recorded fixes. For admins, a small health strip.

3. Known-issue matching
   Takes free text -- an exception message pasted from a case -- and finds
   known issues across every org the caller can see. Deliberately fuzzy:
   the text in a case has record ids, line numbers and quoted values that the
   stored sample will not share, so it is normalised the same way incident
   signatures are before comparing.
"""
import os
import re
import glob

from . import storage
from . import auth
from . import org_access
from . import llm_config
from . import usage as usage_ledger
from .common_now import iso_now


# ---------- guide state ----------

# Events the browser may record. A whitelist, so the endpoint cannot be used
# to write arbitrary keys into the document.
EVENTS = {"search", "writers", "normalize", "incident", "fix", "ask", "connect", "token"}
TOURS = {"demo", "screen", "home", "triage", "orgs", "connect", "dashboard-search",
         "dashboard-writers", "incidents", "known", "logs", "tokens", "usage", "ask", "admin",
         "mcp", "known-feed"}
VIEWS = {"connections", "dashboard", "incidents", "known", "logs", "tokens", "usage",
         "admin", "chat"}
PREF_KEYS = {
    "pinned_orgs": list,
    "org_view": str,          # "cards" | "table"
    "connect_open": bool,     # explicit choice; absent = automatic
    "mcp_card_dismissed": bool,
    "collapsed_accounts": list,   # account keys (casefolded) folded on Home
    "pinned_accounts": list,      # account keys kept at the top
}
MAX_PINNED = 50

# (id, label, hint, roles, how it is satisfied)
CHECKLIST = [
    ("tour", "Play the 90-second demo case",
     "A guided walk through one real-shaped investigation, on demo data.",
     {"reader", "user", "admin"}, ("tour", "demo")),
    ("connect", "Connect an org",
     "Builds that org's knowledgebase from its metadata. Only derived JSON is kept.",
     {"user", "admin"}, ("derived", "owns_org")),
    ("search", "Search an org's knowledgebase",
     "Find a component, object or field by partial name.",
     {"reader", "user", "admin"}, ("event", "search")),
    ("writers", "Find who writes a field",
     "Every Apex, Flow, Process Builder and Workflow writer of one field.",
     {"reader", "user", "admin"}, ("event", "writers")),
    ("normalize", "Normalize a debug log",
     "Turns a raw log into the compact shape the RCA works from.",
     {"user", "admin"}, ("event", "normalize")),
    ("incident", "File an incident",
     "A log and/or a suspect field, ranked against the org's knowledgebase.",
     {"user", "admin"}, ("event", "incident")),
    ("fix", "Record a fix",
     "What makes the next occurrence of the same issue a two-minute job.",
     {"user", "admin"}, ("event", "fix")),
    ("known", "Browse Known Issues",
     "Every failure signature seen in an org, with its fix if one is on file.",
     {"reader"}, ("tab", "known")),
    ("ask", "Ask the assistant a question",
     "Answers are grounded in the knowledgebase tools, not general knowledge.",
     {"reader", "user", "admin"}, ("event", "ask")),
    ("token", "Create an API token",
     "Lets Claude Desktop (or any MCP client) use these tools as you.",
     {"reader", "user", "admin"}, ("derived", "has_token")),
    ("admin", "Review accounts and quotas",
     "Verify self-signups and set LLM limits on the Admin tab.",
     {"admin"}, ("tab", "admin")),
]


def _facts(username):
    """The two checklist items that are true of the stores, not of clicks."""
    registry = storage.load_registry()
    owns_org = any(org_access.owner_of(e) == username for e in registry.values())
    has_token = any(t.get("kind") != "session" for t in auth.list_tokens(username))
    return {"owns_org": owns_org, "has_token": has_token}


def _checklist(username, role, state):
    facts = _facts(username)
    events = state.get("events") or {}
    tours = set(state.get("tours_done") or [])
    tabs = set(state.get("tabs_seen") or [])
    items = []
    for cid, label, hint, roles, (kind, key) in CHECKLIST:
        if role not in roles:
            continue
        if kind == "event":
            done = key in events
        elif kind == "tour":
            done = key in tours
        elif kind == "tab":
            done = key in tabs
        else:
            done = bool(facts.get(key)) or (cid in events)
        items.append({"id": cid, "label": label, "hint": hint, "done": done})
    return items


def guide_payload(ident):
    username, role = ident["username"], ident["role"]
    state = storage.load_guide(username)
    items = _checklist(username, role, state)
    done = sum(1 for i in items if i["done"])
    complete = done == len(items)
    # Stamp completion once, so "complete" survives an item later becoming
    # false again (a token revoked, an org deleted) -- the person still knows
    # how the app works.
    if complete and not state.get("checklist_completed_at"):
        storage.mutate_guide(username, lambda s: s.setdefault("checklist_completed_at", iso_now()))
        state["checklist_completed_at"] = iso_now()
    learning = not (state.get("checklist_completed_at") or state.get("checklist_dismissed"))
    return {
        "username": username,
        "role": role,
        "visits": state.get("visits", 0),
        "welcome_seen": bool(state.get("welcome_seen")),
        "tours_done": sorted(state.get("tours_done") or []),
        "tabs_seen": sorted(state.get("tabs_seen") or []),
        "seen_version": state.get("seen_version"),
        "checklist": items,
        "checklist_done": done,
        "checklist_total": len(items),
        "checklist_dismissed": bool(state.get("checklist_dismissed")),
        "checklist_completed_at": state.get("checklist_completed_at"),
        # The single switch the UI keys every "you are new here" affordance
        # off: nav dots, the checklist card, the tour nudge.
        "learning": learning,
        "prefs": state.get("prefs") or {},
    }


def _clean_prefs(prefs):
    out = {}
    for k, v in (prefs or {}).items():
        want = PREF_KEYS.get(k)
        if want is None:
            continue
        if v is None:
            out[k] = None               # explicit reset
            continue
        if want is list:
            if not isinstance(v, list):
                raise ValueError(f"{k} must be a list")
            out[k] = [str(x)[:128] for x in v][:MAX_PINNED]
        elif want is bool:
            out[k] = bool(v)
        elif want is str:
            if k == "org_view" and v not in ("cards", "table"):
                raise ValueError("org_view must be 'cards' or 'table'")
            out[k] = str(v)[:32]
    return out


def update_guide(ident, change):
    """Apply one update from the browser. Every field is optional; unknown
    event, tour and view names are rejected rather than stored."""
    username = ident["username"]
    event = change.get("event")
    tour = change.get("tour_done")
    tab = change.get("tab_seen")
    if event is not None and event not in EVENTS:
        raise ValueError(f"unknown event '{event}'")
    if tour is not None and tour not in TOURS:
        raise ValueError(f"unknown tour '{tour}'")
    if tab is not None and tab not in VIEWS:
        raise ValueError(f"unknown view '{tab}'")
    prefs = _clean_prefs(change.get("prefs")) if change.get("prefs") is not None else None
    seen_version = change.get("seen_version")
    now = iso_now()

    def _apply(s):
        s.setdefault("created_at", now)
        if change.get("visit"):
            s["visits"] = int(s.get("visits", 0)) + 1
            s["last_visit_at"] = now
        if event:
            s.setdefault("events", {}).setdefault(event, now)
        if tour:
            done = s.setdefault("tours_done", [])
            if tour not in done:
                done.append(tour)
        if tab:
            seen = s.setdefault("tabs_seen", [])
            if tab not in seen:
                seen.append(tab)
        if change.get("welcome_seen"):
            s["welcome_seen"] = True
        if "checklist_dismissed" in change and change["checklist_dismissed"] is not None:
            s["checklist_dismissed"] = bool(change["checklist_dismissed"])
        if seen_version:
            s["seen_version"] = str(seen_version)[:32]
        if change.get("reset"):
            # "Start over" from the help drawer: forget what was seen, keep
            # the preferences (pins are not onboarding).
            for k in ("tours_done", "tabs_seen", "welcome_seen", "checklist_dismissed",
                      "checklist_completed_at"):
                s.pop(k, None)
        if prefs is not None:
            p = s.setdefault("prefs", {})
            for k, v in prefs.items():
                if v is None:
                    p.pop(k, None)
                else:
                    p[k] = v

    storage.mutate_guide(username, _apply)
    return guide_payload(ident)


# ---------- home summary ----------

def _incident_stats(org_id):
    dirs = [p for p in glob.glob(os.path.join(storage.incidents_dir(org_id), "*")) if os.path.isdir(p)]
    last = max((os.path.basename(p) for p in dirs), default=None)
    known = storage.load_known_issues(org_id)
    unresolved = sum(1 for v in known.values() if not v.get("resolution"))
    return {
        "incidents": len(dirs),
        # Incident ids start with a compact UTC timestamp (20260729T173311Z_...)
        "last_incident_at": last.split("_", 1)[0] if last else None,
        "known": len(known),
        "unresolved": unresolved,
    }


def home_payload(ident):
    visible = org_access.visible_orgs(ident)
    orgs = {oid: _incident_stats(oid) for oid in visible}

    fixes = []
    for oid in visible:
        for sig, v in storage.load_known_issues(oid).items():
            if v.get("resolution"):
                fixes.append({
                    "org_id": oid, "signature": sig, "kind": v.get("kind"),
                    "type": v.get("type"), "field": v.get("field"),
                    "message_sample": (v.get("message_sample") or "")[:240],
                    "resolution": v["resolution"][:400],
                    "resolution_recorded_at": v.get("resolution_recorded_at"),
                    "occurrences": v.get("occurrences", 1),
                })
    fixes.sort(key=lambda f: f.get("resolution_recorded_at") or "", reverse=True)

    has_token = any(t.get("kind") != "session" for t in auth.list_tokens(ident["username"]))
    out = {"orgs": orgs, "recent_fixes": fixes[:6], "has_api_token": has_token}

    if ident["role"] == "admin":
        users = auth.list_users()
        unverified = sorted(u for u, d in users.items()
                            if d.get("created_by") == "self" and not d.get("verified")
                            and not d.get("disabled"))
        try:
            week = usage_ledger.report(days=7)
            top = [{"username": r["username"], "total_tokens": r.get("total_tokens", 0),
                    "turns": r.get("turns", 0)} for r in week.get("by_user", [])[:3]]
            week_tokens = (week.get("totals") or {}).get("total_tokens", 0)
        except Exception:                       # a report is decoration here
            top, week_tokens = [], 0
        llm = llm_config.public_state()
        out["admin"] = {
            "llm": {"configured": llm.get("configured"), "provider": llm.get("provider"),
                    "default_model": llm.get("default_model"),
                    "problem": llm.get("config_error") or llm.get("verify_error")},
            "unverified": unverified,
            "users": len(users),
            "top_users": top,
            "week_tokens": week_tokens,
        }
    return out


# ---------- known-issue matching ----------

_SFID_RE = re.compile(r"\b[a-zA-Z0-9]{15}(?:[a-zA-Z0-9]{3})?\b")
_LITERAL_RE = re.compile(r"'[^']*'|\"[^\"]*\"|:\w+|\b\d+\b")
_WORD_RE = re.compile(r"[a-z_][a-z0-9_.]{2,}")
_EXC_TYPE_RE = re.compile(r"\b([A-Za-z]+\.)?[A-Za-z]*(Exception|Error)\b")
_STOP = {"the", "and", "for", "with", "this", "that", "was", "are", "not", "from",
         "line", "column", "class", "trigger", "attempt", "error", "exception", "system"}


def _norm(text):
    t = _SFID_RE.sub(lambda m: "?" if any(c.isdigit() for c in m.group(0)) else m.group(0), text or "")
    return re.sub(r"\s+", " ", _LITERAL_RE.sub("?", t)).strip().lower()


def _words(text):
    return {w for w in _WORD_RE.findall(text) if w not in _STOP}


def match_known(ident, query, limit=8):
    q_raw = (query or "").strip()
    if not q_raw:
        return []
    q = _norm(q_raw)
    qw = _words(q)
    q_types = {m.group(0).lower() for m in _EXC_TYPE_RE.finditer(q_raw)}
    hits = []
    for oid in org_access.visible_orgs(ident):
        for sig, v in storage.load_known_issues(oid).items():
            if v.get("kind") == "field_report":
                text = f"field report {v.get('field') or ''}"
            else:
                text = f"{v.get('type') or ''} {v.get('message_sample') or ''}"
            e = _norm(text)
            msg = _norm(v.get("message_sample") or "")
            score = 0
            if q_raw == sig:
                score = 100
            elif msg and len(msg) >= 12 and (msg in q or (len(q) >= 12 and q in msg)):
                score = 90
            elif v.get("field") and v["field"].lower() in q:
                score = 80
            else:
                ew = _words(e)
                shared = qw & ew
                if ew and len(shared) >= 2:
                    score = int(70 * len(shared) / len(ew))
            etype = (v.get("type") or "").lower()
            if score and etype and any(t in etype or etype.endswith(t) for t in q_types):
                score += 15
            if score < 40:
                continue
            ids = v.get("incident_ids") or []
            hits.append({
                "org_id": oid, "signature": sig, "score": min(score, 100),
                "kind": v.get("kind"), "type": v.get("type"), "field": v.get("field"),
                "message_sample": (v.get("message_sample") or "")[:300],
                "resolution": v.get("resolution"),
                "occurrences": v.get("occurrences", 1),
                "last_seen": v.get("last_seen"),
                "latest_incident": ids[-1] if ids else None,
            })
    hits.sort(key=lambda h: (h["score"], bool(h["resolution"]), h.get("last_seen") or ""), reverse=True)
    return hits[:limit]

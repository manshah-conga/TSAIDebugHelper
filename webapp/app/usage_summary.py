"""
Executive summary for the Usage tab: the handful of numbers a lead or
manager asks for first -- how many questions, how many tokens, how many orgs,
how many people, how many incidents -- for one period, next to the period
before it.

Why a module of its own rather than another key on usage.report /
activity.report
-------------------------------------------------------------------------
The two ledgers answer different questions (usage.py: who spent LLM tokens;
activity.py: who did what, on which channel) and each screen already has its
own payload. The summary is the one place they are read *together*, plus the
org registry and the account list, and it needs the previous period as well
-- which neither report reads. Computing it here, in one pass over both
ledgers, keeps the headline numbers internally consistent: "questions" and
"active users" come from the same records the trend chart is drawn from, so
the tiles and the chart can never disagree.

Scope
-----
An admin sees everybody (optionally narrowed to one account). Anybody else
sees the same layout scoped to themselves and to the orgs they can see --
the summary is never a leaderboard for a non-admin, so the people table is
admin-only.

Nothing new is recorded. Everything is derived on read from data the two
ledgers and the registry already hold; no customer content is involved.
"""
import datetime
import glob
import os

from . import activity, storage
from . import usage as usage_ledger

MAX_DAYS = 400
DEFAULT_DAYS = 30


def _today():
    return datetime.datetime.utcnow().date()


def _window(days, end):
    return [end - datetime.timedelta(days=i) for i in range(days - 1, -1, -1)]


def _read(day_path_fn, day_list, username=None):
    out = []
    for day in day_list:
        for rec in storage.read_jsonl(day_path_fn(day)):
            if username and rec.get("username") != username:
                continue
            out.append(rec)
    return out


def _int(v):
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _float(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _in_window(ts, day_list):
    """True when an ISO timestamp falls on one of the window's UTC days."""
    if not ts or not day_list:
        return False
    d = str(ts)[:10]
    return day_list[0].isoformat() <= d <= day_list[-1].isoformat()


def _metrics(acts, llm, day_list, registry, users=None):
    """Every headline figure for one window. `acts` and `llm` are already
    narrowed to the window and the scope."""
    actions = [r for r in acts if r.get("kind", "action") == "action"]
    ok_actions = [r for r in actions if r.get("ok", True)]

    def count(pred, rows=ok_actions):
        return sum(1 for r in rows if pred(r))

    active_users = {r.get("username") for r in acts if r.get("username")}
    active_users |= {r.get("username") for r in llm if r.get("username")}
    active_days = {(r.get("at") or "")[:10] for r in acts + llm if r.get("at")}

    orgs_worked = {r.get("org_id") for r in actions if r.get("org_id")}
    orgs_worked |= {r.get("org_id") for r in llm if r.get("org_id")}
    accounts_worked = {(registry.get(o) or {}).get("account") for o in orgs_worked}
    accounts_worked.discard(None)
    accounts_worked.discard("")

    mcp = [r for r in actions if r.get("channel") in activity.MCP_CHANNELS
           and r.get("action") != "mcp.connect"]
    logs = [r for r in ok_actions if r.get("action") == "log.parse"
            or (r.get("action") == "incident.file" and (r.get("meta") or {}).get("has_log"))]

    questions = len(llm)
    failed_q = sum(1 for r in llm if not r.get("ok", True))
    tokens = sum(_int(r.get("total_tokens")) for r in llm)
    cost_turns = sum(1 for r in llm if r.get("cost_reported"))
    duration = sum(_int(r.get("duration_ms")) for r in llm)

    new_users = (sum(1 for u in (users or {}).values() if _in_window(u.get("created_at"), day_list))
                 if users is not None else None)

    return {
        "questions": questions,
        "failed_questions": failed_q,
        "answer_rate": round((questions - failed_q) / questions * 100, 1) if questions else None,
        "tokens": tokens,
        "prompt_tokens": sum(_int(r.get("prompt_tokens")) for r in llm),
        "completion_tokens": sum(_int(r.get("completion_tokens")) for r in llm),
        "avg_tokens_per_question": round(tokens / questions) if questions else 0,
        "avg_answer_seconds": round(duration / questions / 1000, 1) if questions else 0,
        "chat_tool_calls": sum(_int(r.get("tool_calls")) for r in llm),
        # False = no provider in the window reported a cost (the Azure case).
        # "Cannot say" must not render as "$0.00".
        "cost": round(sum(_float(r.get("cost")) for r in llm), 4),
        "cost_available": cost_turns > 0,
        "active_users": len(active_users),
        "active_days": len(active_days),
        "actions": len(actions),
        "errors": sum(1 for r in actions if not r.get("ok", True)),
        "mcp_calls": len(mcp),
        "mcp_users": len({r.get("username") for r in mcp if r.get("username")}),
        "kb_lookups": count(lambda r: (r.get("action") or "").startswith("kb.")),
        "logs_parsed": len(logs),
        "exceptions_found": sum(_int((r.get("meta") or {}).get("exceptions")) for r in logs),
        "incidents_filed": count(lambda r: r.get("action") == "incident.file"),
        "resolutions": count(lambda r: r.get("action") == "incident.resolve"),
        "orgs_worked_on": len(orgs_worked),
        "accounts_worked_on": len(accounts_worked),
        "orgs_connected": sum(1 for e in registry.values()
                              if _in_window(e.get("first_onboarded_at"), day_list)),
        "org_refreshes": count(lambda r: r.get("action") == "org.refresh"),
        "new_users": new_users,
    }


def summary(days=DEFAULT_DAYS, username=None, registry=None, users=None, include_people=True):
    """The executive summary for the last `days` UTC days (today inclusive)
    and the same-length period immediately before it.

    `registry` is the set of orgs in scope (all of them for an admin, the
    visible ones for anyone else). `users` is the account list, or None when
    the caller may not see other accounts."""
    days = max(1, min(int(days or DEFAULT_DAYS), MAX_DAYS))
    registry = registry if registry is not None else storage.load_registry()
    end = _today()
    cur = _window(days, end)
    prev = _window(days, cur[0] - datetime.timedelta(days=1))

    acts = _read(activity._day_path, cur, username)
    llm = _read(usage_ledger._day_path, cur, username)
    p_acts = _read(activity._day_path, prev, username)
    p_llm = _read(usage_ledger._day_path, prev, username)

    # Narrowed to one account, "orgs connected" means orgs that account
    # connected (the registry's owner), and "new accounts" means nothing.
    owned = ({k: v for k, v in registry.items() if v.get("owner") == username}
             if username else registry)
    people = None if username else users
    current = _metrics(acts, llm, cur, registry, people)
    previous = _metrics(p_acts, p_llm, prev, registry, people)
    for m, win in ((current, cur), (previous, prev)):
        m["orgs_connected"] = sum(1 for e in owned.values()
                                  if _in_window(e.get("first_onboarded_at"), win))

    # A prior period from before the ledgers started recording is "no data",
    # not "zero" -- otherwise the first month always reads as +100% on all.
    earliest = _earliest_ledger_day()
    previous_has_data = bool(earliest and earliest <= prev[-1].isoformat())

    # --- inventory: point-in-time, not per period
    counts = {}
    for e in registry.values():
        for k, v in (e.get("component_counts") or {}).items():
            if isinstance(v, (int, float)):
                counts[k] = counts.get(k, 0) + int(v)
    accounts_total = {e.get("account") for e in registry.values() if e.get("account")}
    inventory = {
        "orgs_total": len(registry),
        "accounts_total": len(accounts_total),
        "orgs_unassigned": sum(1 for e in registry.values() if not e.get("account")),
        "components_indexed": sum(counts.values()),
        "users_total": (sum(1 for u in users.values() if not u.get("disabled")) if users is not None else None),
        "users_verified": (sum(1 for u in users.values() if u.get("verified") and not u.get("disabled"))
                           if users is not None else None),
    }

    # --- per day: questions, tokens, actions, active people
    by_day = {d.isoformat(): {"date": d.isoformat(), "questions": 0, "tokens": 0, "actions": 0,
                              "users": set()} for d in cur}
    for r in llm:
        b = by_day.get((r.get("at") or "")[:10])
        if b is not None:
            b["questions"] += 1
            b["tokens"] += _int(r.get("total_tokens"))
            if r.get("username"):
                b["users"].add(r["username"])
    for r in acts:
        b = by_day.get((r.get("at") or "")[:10])
        if b is not None:
            if r.get("kind", "action") == "action":
                b["actions"] += 1
            if r.get("username"):
                b["users"].add(r["username"])
    trend = [{**v, "active_users": len(v.pop("users"))} for _, v in sorted(by_day.items())]

    # --- top orgs: where the effort went
    orgs = {}

    def org_row(o):
        e = registry.get(o) or {}
        return orgs.setdefault(o, {"org_id": o, "org_name": e.get("name"), "account": e.get("account"),
                                   "questions": 0, "tokens": 0, "actions": 0, "incidents": 0,
                                   "users": set()})
    for r in llm:
        if r.get("org_id"):
            x = org_row(r["org_id"])
            x["questions"] += 1
            x["tokens"] += _int(r.get("total_tokens"))
            if r.get("username"):
                x["users"].add(r["username"])
    for r in acts:
        if r.get("org_id") and r.get("kind", "action") == "action":
            x = org_row(r["org_id"])
            x["actions"] += 1
            if r.get("action") == "incident.file" and r.get("ok", True):
                x["incidents"] += 1
            if r.get("username"):
                x["users"].add(r["username"])
    top_orgs = sorted(({**v, "users": len(v["users"])} for v in orgs.values()),
                      key=lambda x: (x["questions"] + x["actions"], x["tokens"]), reverse=True)[:8]

    # --- top accounts (customer), rolled up from orgs
    accts = {}
    for v in orgs.values():
        a = v["account"] or "(unassigned)"
        x = accts.setdefault(a, {"account": a, "orgs": 0, "questions": 0, "tokens": 0,
                                 "actions": 0, "incidents": 0})
        x["orgs"] += 1
        for k in ("questions", "tokens", "actions", "incidents"):
            x[k] += v[k]
    top_accounts = sorted(accts.values(), key=lambda x: (x["questions"] + x["actions"]), reverse=True)[:8]

    # --- people (admin only)
    top_users = None
    if include_people:
        people = {}

        def person(u):
            return people.setdefault(u, {"username": u, "questions": 0, "tokens": 0, "actions": 0,
                                         "incidents": 0, "logs": 0, "channels": set(), "days": set()})
        for r in llm:
            if r.get("username"):
                p = person(r["username"])
                p["questions"] += 1
                p["tokens"] += _int(r.get("total_tokens"))
                p["days"].add((r.get("at") or "")[:10])
        for r in acts:
            if not r.get("username"):
                continue
            p = person(r["username"])
            p["days"].add((r.get("at") or "")[:10])
            if r.get("kind", "action") != "action":
                continue
            p["actions"] += 1
            p["channels"].add(r.get("channel") or "web")
            a = r.get("action")
            if a == "incident.file" and r.get("ok", True):
                p["incidents"] += 1
            if r.get("ok", True) and (a == "log.parse" or (a == "incident.file" and (r.get("meta") or {}).get("has_log"))):
                p["logs"] += 1
        top_users = sorted(({**v, "channels": sorted(v["channels"]), "active_days": len(v.pop("days"))}
                            for v in people.values()),
                           key=lambda x: (x["questions"] + x["actions"], x["tokens"]), reverse=True)[:10]

    # --- channel mix + what people actually do
    channels = {}
    feats = {}
    for r in acts:
        if r.get("kind", "action") != "action":
            continue
        ch = r.get("channel") or "web"
        channels[ch] = channels.get(ch, 0) + 1
        a = r.get("action") or "?"
        if a.startswith("auth.") or a == "mcp.connect":
            continue   # sign-ins are not what anybody came to do
        feats[a] = feats.get(a, 0) + 1
    top_features = [{"action": a, "label": activity._label_for(a), "count": n}
                    for a, n in sorted(feats.items(), key=lambda kv: kv[1], reverse=True)[:6]]

    models = {}
    for r in llm:
        m = r.get("model") or "(unknown)"
        x = models.setdefault(m, {"model": m, "questions": 0, "tokens": 0})
        x["questions"] += 1
        x["tokens"] += _int(r.get("total_tokens"))
    top_models = sorted(models.values(), key=lambda x: x["tokens"], reverse=True)[:5]

    return {
        "days": days,
        "from": cur[0].isoformat(),
        "to": cur[-1].isoformat(),
        "previous_from": prev[0].isoformat(),
        "previous_to": prev[-1].isoformat(),
        "previous_has_data": previous_has_data,
        "scope": username or "all users",
        "current": current,
        "previous": previous,
        "inventory": inventory,
        "trend": trend,
        "channels": channels,
        "channel_labels": activity.CHANNELS,
        "top_orgs": top_orgs,
        "top_accounts": top_accounts,
        "top_users": top_users,
        "top_features": top_features,
        "top_models": top_models,
        "generated_at": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _earliest_ledger_day():
    days = []
    for root in (storage.activity_root(), storage.usage_root()):
        for path in glob.glob(os.path.join(root, "*.jsonl")):
            stem = os.path.splitext(os.path.basename(path))[0]
            if len(stem) == 10 and stem[4] == "-":
                days.append(stem)
    return min(days) if days else None

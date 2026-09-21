"""
Per-user LLM usage: what each account spent, on which org, with which model.

Why this exists
---------------
The moment one shared API key serves everybody, the provider's own dashboard
stops being able to answer the only question that matters operationally --
*who* spent this. Every call arrives from the same key. Attribution has to
happen here, at the point of use, or it does not happen at all.

Storage shape, and why it is a log rather than a document
---------------------------------------------------------
One append-only JSONL file per UTC day:

    data/usage/2026-09-21.jsonl

A single running-totals document would have been smaller to read, and would
have been the wrong choice. Totals require read-modify-write, and this app is
multi-user: several turns finish at the same instant, each wanting to add its
own numbers to the same counters. That is exactly the pattern that loses
data. An append has no read step -- two writers cannot erase each other's
records, only sit next to them.

Per day, rather than one big log, keeps every query bounded: "last 30 days"
opens 30 small files and never touches the rest, and retention is a matter of
deleting old files.

Aggregation happens on read. At this app's scale -- support engineers, tens
of turns a day -- that is a handful of milliseconds over a few thousand short
lines, and it buys the ability to slice by user, day, org or model without
having pre-computed each combination.

Honesty about cost
------------------
`cost` comes from the provider's own usage block, which OpenRouter returns
and **Azure OpenAI does not** -- Azure bills against the Azure subscription,
not per call, so there is no per-request figure to report. Every total here
therefore carries `cost_available`, and the UI says "not reported by Azure"
rather than showing a confident $0.00 that an admin might take for real.
Token counts are reported by both, so they are the number to plan against on
Azure.

A usage record is written even for a turn that failed, with `ok: false` and
the error code. A failed turn can still have consumed tokens, and a turn that
was *attempted* is a fact worth having when someone asks why their quota
went.
"""
import datetime
import glob
import json
import os

from . import storage

# Guardrail on ad-hoc queries. A request for a decade of history would open
# thousands of files inside a request thread; nothing in the UI needs more
# than a year, and an admin who does can read the files directly.
MAX_DAYS = 400
DEFAULT_DAYS = 30


def _today():
    return datetime.datetime.utcnow().date()


def _day_path(day):
    return os.path.join(storage.usage_root(), f"{day.isoformat()}.jsonl")


def record_turn(username, *, chat_id=None, org_id=None, model=None, provider=None,
                source=None, usage=None, tool_calls=0, tool_rounds=0,
                duration_ms=None, ok=True, error_code=None):
    """Write one usage record. Never raises.

    Called at the end of every chat turn, from inside the SSE generator. That
    placement is why the whole body is wrapped: a failure to record usage
    must not truncate a stream the user is reading, and losing one accounting
    record is a far smaller problem than losing the answer it was measuring.
    """
    try:
        usage = usage or {}
        now = datetime.datetime.utcnow()
        record = {
            "at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "username": username,
            "chat_id": chat_id,
            "org_id": org_id,
            "model": model,
            "provider": provider,
            # "shared" (the server connection) or "personal" (an admin's own
            # key). Kept because it decides whose bill a cost lands on, and
            # an admin reading the report needs to know which figures are
            # theirs personally and which are the team's.
            "source": source,
            "prompt_tokens": _int(usage.get("prompt_tokens")),
            "completion_tokens": _int(usage.get("completion_tokens")),
            "reasoning_tokens": _int(usage.get("reasoning_tokens")),
            "total_tokens": _int(usage.get("total_tokens")),
            "cost": _float(usage.get("cost")),
            # Whether the provider reported a cost at all, as opposed to
            # reporting zero. Azure never does; conflating the two would show
            # an admin a $0 spend that reads as "free".
            "cost_reported": usage.get("cost") is not None,
            "tool_calls": _int(tool_calls),
            "tool_rounds": _int(tool_rounds),
            "duration_ms": _int(duration_ms),
            "ok": bool(ok),
            "error_code": error_code,
        }
        storage.append_jsonl(_day_path(now.date()), record)
        return record
    except Exception as e:                            # noqa: BLE001
        print(f"[TS Debug Helper] failed to record usage for {username}: "
              f"{e.__class__.__name__}: {e}", flush=True)
        return None


def _int(v):
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _float(v):
    try:
        return float(v or 0.0)
    except (TypeError, ValueError):
        return 0.0


# =====================================================================
# reading
# =====================================================================

def _days_in_range(days):
    days = max(1, min(int(days or DEFAULT_DAYS), MAX_DAYS))
    today = _today()
    return [today - datetime.timedelta(days=i) for i in range(days - 1, -1, -1)]


def load_records(days=DEFAULT_DAYS, username=None, org_id=None):
    """Every record in the window, optionally narrowed to one user or org.

    Filtering here rather than in each aggregator means a per-user drilldown
    reads the same code path as the overall report, so the two can never
    disagree about what counts.
    """
    out = []
    for day in _days_in_range(days):
        for rec in storage.read_jsonl(_day_path(day)):
            if username and rec.get("username") != username:
                continue
            if org_id and rec.get("org_id") != org_id:
                continue
            out.append(rec)
    return out


def _blank():
    return {"turns": 0, "failed_turns": 0, "prompt_tokens": 0, "completion_tokens": 0,
            "reasoning_tokens": 0, "total_tokens": 0, "cost": 0.0, "cost_reported_turns": 0,
            "tool_calls": 0, "tool_rounds": 0, "duration_ms": 0}


def _add(bucket, rec):
    bucket["turns"] += 1
    if not rec.get("ok", True):
        bucket["failed_turns"] += 1
    for field in ("prompt_tokens", "completion_tokens", "reasoning_tokens",
                  "total_tokens", "tool_calls", "tool_rounds", "duration_ms"):
        bucket[field] += _int(rec.get(field))
    bucket["cost"] += _float(rec.get("cost"))
    if rec.get("cost_reported"):
        bucket["cost_reported_turns"] += 1


def _finish(bucket):
    """Derived fields the UI would otherwise compute three times over."""
    bucket["cost"] = round(bucket["cost"], 6)
    bucket["avg_tokens_per_turn"] = (
        round(bucket["total_tokens"] / bucket["turns"]) if bucket["turns"] else 0)
    bucket["avg_seconds_per_turn"] = (
        round(bucket["duration_ms"] / bucket["turns"] / 1000, 1) if bucket["turns"] else 0)
    # False means "no provider in this bucket reported a cost", which is the
    # Azure case. It is the difference between "spent nothing" and "cannot
    # say", and the UI must not render them the same way.
    bucket["cost_available"] = bucket["cost_reported_turns"] > 0
    return bucket


def report(days=DEFAULT_DAYS, username=None):
    """The whole usage picture for one window, in one response.

    Assembled as a single payload rather than four endpoints because the
    admin screen shows all of it at once, and four requests over the same
    files would read them four times and could straddle a midnight rollover
    mid-render.
    """
    records = load_records(days, username=username)
    day_list = _days_in_range(days)

    totals = _blank()
    by_user, by_org, by_model, by_day = {}, {}, {}, {}

    # Pre-seed every day in the window, including the empty ones. A trend
    # chart that silently omits quiet days compresses the gaps and makes a
    # flat week look like steady activity.
    for day in day_list:
        by_day[day.isoformat()] = _blank()

    for rec in records:
        _add(totals, rec)
        user = rec.get("username") or "(unknown)"
        by_user.setdefault(user, _blank())
        _add(by_user[user], rec)

        # A turn with no org is real and common -- normalizing a standalone
        # log needs no org connection at all -- so it gets its own bucket
        # instead of being dropped from the breakdown.
        org = rec.get("org_id") or "(no org)"
        by_org.setdefault(org, _blank())
        _add(by_org[org], rec)

        model = rec.get("model") or "(unknown)"
        by_model.setdefault(model, _blank())
        _add(by_model[model], rec)

        day_key = (rec.get("at") or "")[:10]
        if day_key in by_day:
            _add(by_day[day_key], rec)

    def rows(mapping, key_name, sort_key="total_tokens"):
        out = [dict(_finish(v), **{key_name: k}) for k, v in mapping.items()]
        return sorted(out, key=lambda r: r[sort_key], reverse=True)

    return {
        "days": len(day_list),
        "from": day_list[0].isoformat(),
        "to": day_list[-1].isoformat(),
        "scope": username or "all users",
        "totals": _finish(totals),
        "by_user": rows(by_user, "username"),
        "by_org": rows(by_org, "org_id"),
        "by_model": rows(by_model, "model"),
        # Chronological, not ranked -- it is a time series.
        "by_day": [dict(_finish(v), date=k) for k, v in sorted(by_day.items())],
        "active_users": len(by_user),
    }


def my_summary(username, days=DEFAULT_DAYS):
    """A user's own totals. Not admin-gated: seeing your own consumption is
    reasonable, and it answers "is it me?" without an admin in the loop."""
    r = report(days=days, username=username)
    return {"days": r["days"], "from": r["from"], "to": r["to"],
            "totals": r["totals"], "by_day": r["by_day"], "by_model": r["by_model"]}


def prune(keep_days=365):
    """Delete day files outside the retention window. Not wired to a
    scheduler -- there is no scheduler in this app -- but it is here so
    retention is a one-line call from a cron job rather than a shell
    incantation an operator has to get right."""
    cutoff = _today() - datetime.timedelta(days=keep_days)
    removed = []
    for path in glob.glob(os.path.join(storage.usage_root(), "*.jsonl")):
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
    """Account deletion. The records are rewritten without that user rather
    than left behind, so deleting an account does not leave their name in the
    admin usage report forever.

    Each day file is rewritten under its own lock, which is safe against a
    concurrent append: the appender waits, then adds its line to the rewritten
    file. The only lost case is a turn finishing for an account being deleted
    in the same instant, which is not a case worth more machinery.
    """
    touched = 0
    for path in glob.glob(os.path.join(storage.usage_root(), "*.jsonl")):
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

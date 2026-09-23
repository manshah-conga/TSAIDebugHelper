"""
LLM quota policy: how much an account may spend, and whether it may spend
any more right now.

Why this is a separate module
-----------------------------
`app/usage.py` is accounting -- it records what happened and adds it up, and
it holds no opinion about whether any of it was allowed. Policy belongs
somewhere else, or every future change to a cap has to be made inside the
ledger and risks changing what the ledger reports. This module reads the
ledger and the user record, and answers one question: may this turn start?

Why the cap is counted in tokens, not dollars
---------------------------------------------
Azure OpenAI does not report a per-call cost -- it bills against the Azure
subscription, so `usage.py` records `cost_reported: false` and every total
carries `cost_available`. A dollar cap would therefore never trip on the
provider this app actually runs on. Tokens are reported by both providers,
so tokens are the only unit a cap can be enforced in honestly.

Why the limits live in a document, not in the environment
---------------------------------------------------------
`TS_LIMIT_*` variables would mean an operator with shell access and a
service restart every time a cap needs to move. These caps will move -- they
are a first guess at what a support engineer consumes in a day. So the
numbers live in `data/auth/limits.json`, editable by an admin from the UI,
and the constants below are only the fallback used to write that file on
first run.

The tiers
---------
Two tiers plus an exemption, resolved in this order (first match wins):

1. a per-user override on the user record (`users[u]["limits"]`), set by an
   admin for one account;
2. `verified` -- an admin has vouched for the account, or an admin created
   it in the first place;
3. `unverified` -- it signed itself up and nobody has looked at it yet;
4. admins are exempt entirely. An admin who is out of quota cannot raise
   their own quota, which is a deadlock with no way out of it.

`None` as a limit value means unlimited, at any level. An override may set
just one of the two fields; the other still inherits its tier.

Windows, and what a cap actually promises
-----------------------------------------
Two windows, because they fail differently: a **daily** cap contains a
runaway loop or a bad afternoon, and a **rolling 30-day** cap contains
steady over-use that no single day would catch. Rolling rather than
calendar-month, so quota does not arrive in a lump on the 1st and run out
on the 12th.

The check happens before a turn starts, and that is the only promise it can
make. Token counts are not knowable in advance, so a turn already in flight
runs to completion: an account can finish slightly over its cap, by at most
one turn. The alternative -- killing a stream mid-answer on a token
threshold -- destroys work the user is already reading to save a few
thousand tokens.
"""
import datetime

from . import storage, usage

# Fallbacks, used only to seed the stored document on first read. Sized from
# this install's own ledger, where a turn ran ~10k tokens (median) and ~14k
# at p90: the unverified tier is about twenty turns a day, and the verified
# tier about a hundred. They are a starting point, not a finding -- which is
# exactly why they are editable from the UI rather than hardcoded here.
DEFAULT_CONFIG = {
    "tiers": {
        "unverified": {"daily_tokens": 200_000, "monthly_tokens": 2_000_000},
        "verified": {"daily_tokens": 1_000_000, "monthly_tokens": 10_000_000},
    },
    "window_days": 30,
}

TIERS = ("unverified", "verified")
FIELDS = ("daily_tokens", "monthly_tokens")
MIN_WINDOW_DAYS = 1
MAX_WINDOW_DAYS = 90


def _now():
    return datetime.datetime.utcnow()


# =====================================================================
# configuration
# =====================================================================

def load_config():
    """The stored policy, with every missing piece filled from the defaults.

    Always returns a complete document, so no caller has to cope with a
    half-written file or a config saved by an older version that did not
    have one of these fields yet.
    """
    stored = storage.load_limits() or {}
    cfg = {
        "tiers": {},
        "window_days": _clamp_window(stored.get("window_days")),
        "updated_at": stored.get("updated_at"),
        "updated_by": stored.get("updated_by"),
    }
    stored_tiers = stored.get("tiers") or {}
    for tier in TIERS:
        src = stored_tiers.get(tier) or {}
        fallback = DEFAULT_CONFIG["tiers"][tier]
        cfg["tiers"][tier] = {
            field: _clean_limit(src[field]) if field in src else fallback[field]
            for field in FIELDS
        }
    return cfg


def _clamp_window(value):
    try:
        days = int(value)
    except (TypeError, ValueError):
        return DEFAULT_CONFIG["window_days"]
    return max(MIN_WINDOW_DAYS, min(days, MAX_WINDOW_DAYS))


def _clean_limit(value):
    """A limit is a positive integer, or None for unlimited.

    Zero is rejected rather than read as "unlimited" or as "no tokens at
    all": an admin typing 0 into a cap field means one of those two things
    and there is no way to tell which, so the UI has to ask for an explicit
    choice instead of silently picking the dangerous reading.
    """
    if value is None or value == "":
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ValueError("a limit must be a whole number of tokens, or blank for unlimited")
    if n <= 0:
        raise ValueError("a limit must be greater than zero (leave it blank for unlimited)")
    return n


def save_config(patch, updated_by=None):
    """Apply an admin's edit to the stored policy.

    A patch, not a replacement: the UI sends only the fields it changed, so
    two admins editing different tiers in the same minute do not overwrite
    each other with a stale copy of the whole document.
    """
    patch = patch or {}
    tiers_patch = patch.get("tiers") or {}
    for tier in tiers_patch:
        if tier not in TIERS:
            raise ValueError(f"unknown tier '{tier}' (expected one of {list(TIERS)})")
    # Validate everything before taking the lock, so a bad value cannot leave
    # the document half-updated.
    cleaned = {
        tier: {field: _clean_limit(vals[field]) for field in FIELDS if field in (vals or {})}
        for tier, vals in tiers_patch.items()
    }
    window = _clamp_window(patch["window_days"]) if "window_days" in patch else None
    stamp = _now().strftime("%Y-%m-%dT%H:%M:%SZ")

    def _apply(doc):
        doc.setdefault("tiers", {})
        for tier, vals in cleaned.items():
            doc["tiers"].setdefault(tier, {})
            doc["tiers"][tier].update(vals)
        if window is not None:
            doc["window_days"] = window
        doc["updated_at"] = stamp
        doc["updated_by"] = updated_by

    storage.mutate_limits(_apply)
    return load_config()


def clean_override(override):
    """Validate a per-user override before it is written to a user record.

    Returns None for "no override" so the caller can store exactly that and
    let the account fall back to its tier, rather than storing an empty dict
    that later reads as an override of nothing.
    """
    if not override:
        return None
    unknown = [k for k in override if k not in FIELDS]
    if unknown:
        raise ValueError(f"unknown limit field(s) {unknown} (expected {list(FIELDS)})")
    cleaned = {field: _clean_limit(override[field]) for field in override if field in FIELDS}
    return cleaned or None


# =====================================================================
# resolution
# =====================================================================

def tier_for(user_record):
    """Which tier an account sits in.

    An account created by an admin counts as verified without anyone having
    to click anything: the admin creating it *is* the vouching step, and
    making them then verify their own new user is ceremony that teaches
    people to click past it. Only self-signup starts unverified.
    """
    record = user_record or {}
    if record.get("verified"):
        return "verified"
    created_by = record.get("created_by")
    if created_by and created_by != "self":
        return "verified"
    # No provenance at all means the account predates self-signup, so it was
    # necessarily created by an admin.
    if created_by is None and "verified" not in record:
        return "verified"
    return "unverified"


def effective_limits(user_record, config=None):
    """The caps that actually apply to one account, and where they came from.

    `source` is part of the answer, not decoration: an admin looking at a
    user who is out of quota needs to know whether to raise that account's
    override or the whole tier, and a user told they are capped deserves to
    know whether it is because nobody has verified them yet.
    """
    record = user_record or {}
    config = config or load_config()
    if record.get("role") == "admin":
        return {"daily_tokens": None, "monthly_tokens": None, "tier": "admin",
                "source": "admin", "window_days": config["window_days"]}

    tier = tier_for(record)
    limits = dict(config["tiers"][tier])
    override = record.get("limits") or {}
    source = tier
    for field in FIELDS:
        if field in override:
            limits[field] = _safe_limit(override[field])
            source = "override"
    limits.update({"tier": tier, "source": source, "window_days": config["window_days"]})
    return limits


def _safe_limit(value):
    """Reading, unlike writing, must not raise. A malformed value that got
    into the store somehow is treated as unlimited rather than locking the
    account out of chat with no way for the user to explain why."""
    try:
        return _clean_limit(value)
    except ValueError:
        return None


# =====================================================================
# consumption
# =====================================================================

def _tokens_since(username, days):
    """Tokens this account has spent in the last `days` UTC days, inclusive
    of today. Counts failed turns too -- a turn that burned tokens and then
    errored still cost the budget, and excluding them would make a retry
    loop look free."""
    return sum(int(rec.get("total_tokens") or 0)
               for rec in usage.load_records(days=days, username=username))


def quota_status(username, user_record, config=None):
    """Everything the UI and the enforcement point both need, in one read.

    Assembled together because the two windows have to be measured against
    the same ledger state: reading them separately could straddle a midnight
    rollover and report a daily figure from one day against a window total
    from another.
    """
    config = config or load_config()
    limits = effective_limits(user_record, config)
    window_days = limits["window_days"]

    if limits["daily_tokens"] is None and limits["monthly_tokens"] is None:
        used_today = used_window = None
    else:
        used_today = _tokens_since(username, 1)
        used_window = _tokens_since(username, window_days)

    def part(used, limit):
        if limit is None:
            return {"used": used, "limit": None, "remaining": None,
                    "pct": None, "exceeded": False}
        remaining = max(0, limit - (used or 0))
        return {"used": used or 0, "limit": limit, "remaining": remaining,
                "pct": round(min(100.0, (used or 0) * 100.0 / limit), 1),
                "exceeded": (used or 0) >= limit}

    daily = part(used_today, limits["daily_tokens"])
    window = part(used_window, limits["monthly_tokens"])
    now = _now()
    return {
        "username": username,
        "tier": limits["tier"],
        "source": limits["source"],
        "window_days": window_days,
        "unlimited": limits["daily_tokens"] is None and limits["monthly_tokens"] is None,
        "daily": daily,
        "window": window,
        # When the daily figure rolls over. The rolling window has no single
        # reset moment -- it frees up gradually as old days fall out of it --
        # so the UI says so rather than inventing a date.
        "daily_resets_at": (now.date() + datetime.timedelta(days=1)).isoformat() + "T00:00:00Z",
        "exceeded": daily["exceeded"] or window["exceeded"],
    }


def check_turn_allowed(username, user_record, config=None):
    """(allowed, reason) for one about-to-start turn.

    Returns a reason string written for the person who hit the cap, naming
    which window ran out, by how much, and what makes it go away -- because
    "quota exceeded" with no numbers and no remedy generates a support
    ticket every single time.
    """
    status = quota_status(username, user_record, config)
    if not status["exceeded"]:
        return True, None, status

    if status["daily"]["exceeded"]:
        which = (f"today's limit of {status['daily']['limit']:,} tokens "
                 f"(used {status['daily']['used']:,}). It resets at "
                 f"{status['daily_resets_at'][:10]} 00:00 UTC")
    else:
        which = (f"the {status['window_days']}-day limit of "
                 f"{status['window']['limit']:,} tokens "
                 f"(used {status['window']['used']:,}). It frees up as older "
                 f"days fall out of the window")

    remedy = ""
    if status["tier"] == "unverified":
        remedy = (" Your account is still unverified -- an admin verifying it "
                  "raises the limit.")
    elif status["source"] != "override":
        remedy = " An admin can raise the limit for your account."

    return False, f"This turn would exceed {which}.{remedy}", status

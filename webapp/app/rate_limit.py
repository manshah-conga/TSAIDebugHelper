"""
A small in-process rate limiter for the app's unauthenticated endpoints.

Why this exists now
-------------------
Until self-registration, every write in this app was behind a token, so the
worst an unauthenticated caller could do was guess passwords at whatever
rate the network allowed -- bad, but bounded by nothing here. Opening
`POST /api/auth/signup` adds an endpoint that *creates persistent state* with
no credential at all. Unlimited, one script fills users.json.

So both public endpoints are limited: signup by how many accounts one source
may create, and login by how many passwords one source may try. Login needed
this regardless of signup; it simply never had it.

What it is, and what it is not
------------------------------
A fixed-window counter held in memory, keyed by client address and bucket
name. That is the right size of mechanism for this app: one uvicorn process
behind nginx on a VPN-only host, where the threat is an accident or a bored
colleague rather than a distributed attack. Its limits are worth stating
plainly:

* **Per process.** Two workers mean two independent allowances. There is one
  worker today (see deploy/ts-debug-helper.service), and a shared counter
  would mean a lock on a hot path for no real gain.
* **Memory only.** A restart forgets every counter. An attacker who can
  restart the server has far better options available.
* **Keyed by address**, so everyone behind one NAT shares an allowance. On a
  corporate VPN that is a real possibility, which is why the limits below
  are set to catch scripts rather than to ration humans: twenty logins a
  minute is a jammed key, not a person signing in.

`X-Forwarded-For` is trusted only because nginx sets it in front of this app
(see deploy/nginx-ts-debug-helper.conf). If this ever gets exposed directly,
that header becomes attacker-controlled and this limiter becomes trivially
bypassable -- hence TS_TRUST_FORWARDED_FOR to turn it off.
"""
import os
import threading
import time

from fastapi import HTTPException, Request

# (max_events, window_seconds) per bucket.
BUCKETS = {
    # Enough for a person fumbling a password, nowhere near enough to
    # enumerate one.
    "login": (20, 60),
    # Registration is limited in two places, for two different reasons, and
    # collapsing them into one counter gets the trade wrong in both
    # directions.
    #
    # `signup` counts accounts actually CREATED. It is the tight one, because
    # creating accounts is what fills the user store; ten in an hour from one
    # address covers a team signing up together in a training session.
    #
    # `signup_attempt` counts every call, including the ones rejected for a
    # short password or a taken username. It has to be much looser: a person
    # feeling their way through a form generates several failures, and
    # charging those against the creation allowance would lock someone out
    # for an hour for mistyping. Its job is only to stop a tight loop.
    "signup": (10, 3600),
    "signup_attempt": (30, 600),
}

_LOCK = threading.Lock()
_HITS = {}                      # (bucket, key) -> [window_start, count]
# Bound the dictionary. Without this, every distinct source address is
# remembered forever, which is a slow leak an attacker can drive on purpose
# by varying the forwarded address.
MAX_TRACKED = 10_000


def _trust_forwarded():
    raw = (os.environ.get("TS_TRUST_FORWARDED_FOR") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def client_key(request: Request):
    if _trust_forwarded():
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd:
            # Left-most entry is the original client; the rest are proxies.
            return fwd.split(",")[0].strip()
    return (request.client.host if request.client else "unknown") or "unknown"


def check(bucket, key, now=None):
    """(allowed, retry_after_seconds). Counts the event when it is allowed."""
    limit, window = BUCKETS[bucket]
    now = now if now is not None else time.monotonic()
    slot = (bucket, key)
    with _LOCK:
        entry = _HITS.get(slot)
        if entry is None or now - entry[0] >= window:
            if len(_HITS) >= MAX_TRACKED:
                _evict(now)
            _HITS[slot] = [now, 1]
            return True, 0
        if entry[1] < limit:
            entry[1] += 1
            return True, 0
        return False, max(1, int(window - (now - entry[0])))


def _evict(now):
    """Drop expired entries; if none have expired, drop the oldest half.

    Called with the lock held. The second case matters: a burst of unique
    keys inside one window would otherwise leave nothing to evict and the
    dictionary would grow past its bound anyway.
    """
    stale = [slot for slot, (start, _) in _HITS.items()
             if now - start >= BUCKETS[slot[0]][1]]
    if not stale:
        stale = sorted(_HITS, key=lambda s: _HITS[s][0])[: len(_HITS) // 2]
    for slot in stale:
        _HITS.pop(slot, None)


def enforce(bucket, request: Request):
    """Raise 429 with a Retry-After header, or return quietly.

    429 rather than a vague 400 so a client -- including the app's own UI --
    can tell "you are going too fast" from "your password is wrong", and
    can say when to try again instead of inviting another immediate retry.
    """
    allowed, retry_after = check(bucket, client_key(request))
    if not allowed:
        raise HTTPException(
            429,
            detail=f"Too many attempts. Try again in {retry_after} seconds.",
            headers={"Retry-After": str(retry_after)},
        )


def reset():
    """Clear every counter. For tests, which otherwise leak allowance from
    one case into the next."""
    with _LOCK:
        _HITS.clear()

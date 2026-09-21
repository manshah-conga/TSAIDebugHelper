"""
Local auth layer: username/password accounts with three roles, plus
long-lived API tokens for the MCP server.

Design notes
------------
- Roles are ordered: reader < user < admin. A route asking for `user`
  accepts `user` and `admin`; a route asking for `reader` accepts anyone
  authenticated.
    * reader -- read-only: view orgs, stats, field-writers, incidents,
      search, stored logs.
    * user   -- everything reader can do PLUS write actions: connect orgs,
      file incidents, record resolutions, normalize/store logs.
    * admin  -- everything, PLUS user management (create users, set roles,
      enable/disable, reset passwords) and managing every token.
- Passwords are stored only as PBKDF2-HMAC-SHA256 hashes with a per-user
  random salt (stdlib only -- no bcrypt/argon dependency to install).
- API tokens are shown to the user exactly once at creation; only the
  token's SHA-256 hash is persisted, so a leak of tokens.json does not
  expose usable tokens. A token inherits the role of the user who made it.
- Web login uses the same token mechanism with kind="session" delivered as
  an HttpOnly cookie; API/MCP callers send kind="api" as a Bearer header.

Everything here reads/writes through storage.py so all disk I/O stays in
one auditable place.
"""
import os
import hashlib
import hmac
import secrets
import datetime

from fastapi import Request, HTTPException, Depends

from . import storage

ROLES = ["reader", "user", "admin"]
ROLE_RANK = {r: i for i, r in enumerate(ROLES)}

SESSION_COOKIE = "ts_session"
SESSION_TTL_DAYS = 14
PBKDF2_ROUNDS = 200_000


def _now():
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------- password hashing ----------

def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ROUNDS)
    return {"salt": salt, "hash": dk.hex(), "rounds": PBKDF2_ROUNDS}


def verify_password(password, stored):
    if not stored:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                             stored["salt"].encode("utf-8"), stored.get("rounds", PBKDF2_ROUNDS))
    return hmac.compare_digest(dk.hex(), stored["hash"])


# ---------- user management ----------

def list_users():
    users = storage.load_users()
    # never leak password material
    return {
        u: {"role": d["role"], "disabled": d.get("disabled", False), "created_at": d.get("created_at")}
        for u, d in users.items()
    }


def get_user(username):
    return storage.load_users().get(username)


def create_user(username, password, role):
    username = (username or "").strip()
    if not username:
        raise ValueError("username is required")
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    if not password or len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    # The existence check and the insert have to happen under one lock, or
    # two admins creating the same username at the same moment both pass the
    # check and the second silently overwrites the first one's password.
    clash = [False]

    def _insert(users):
        if username in users:
            clash[0] = True
            return
        users[username] = {"password": hash_password(password), "role": role,
                           "disabled": False, "created_at": _now()}

    storage.mutate_users(_insert)
    if clash[0]:
        raise ValueError(f"user '{username}' already exists")
    return {"username": username, "role": role}


def _update_user(username, change):
    """Apply one field change to one account under an exclusive lock.

    Every mutator below used to load the whole users document, change one
    nested value and write it all back. Concurrently -- an admin flipping a
    role while another disables a different account -- one of those two
    changes disappeared. Funnelling them through here means a change to user
    A can never erase a change to user B.
    """
    missing = [False]

    def _apply(users):
        if username not in users:
            missing[0] = True
            return
        change(users[username])

    storage.mutate_users(_apply)
    if missing[0]:
        raise ValueError(f"no such user '{username}'")


def set_role(username, role):
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    _update_user(username, lambda u: u.update({"role": role}))


def set_disabled(username, disabled):
    _update_user(username, lambda u: u.update({"disabled": bool(disabled)}))


def reset_password(username, new_password):
    if not new_password or len(new_password) < 8:
        raise ValueError("password must be at least 8 characters")
    hashed = hash_password(new_password)
    _update_user(username, lambda u: u.update({"password": hashed}))


def delete_user(username):
    missing = [False]

    def _drop(users):
        if username not in users:
            missing[0] = True
            return
        del users[username]

    storage.mutate_users(_drop)
    if missing[0]:
        raise ValueError(f"no such user '{username}'")

    # Revoke that user's tokens too. Separate lock, separate document -- and
    # under it, so a login happening right now for someone else does not get
    # wiped out by this cleanup.
    def _revoke(tokens):
        for tid in [t for t, d in tokens.items() if d.get("username") == username]:
            del tokens[tid]

    storage.mutate_tokens(_revoke)


def authenticate(username, password):
    user = get_user(username)
    if not user or user.get("disabled"):
        return None
    if not verify_password(password, user.get("password")):
        return None
    return {"username": username, "role": user["role"]}


# ---------- tokens (sessions + API) ----------

def _hash_token(raw):
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_token(username, role, kind="api", label=None, ttl_days=None):
    """Returns (token_id, raw_token). raw_token is shown once and never
    stored -- only its hash is kept."""
    raw = secrets.token_urlsafe(32)
    token_id = secrets.token_hex(8)
    expires_at = None
    if ttl_days:
        expires_at = (datetime.datetime.utcnow() + datetime.timedelta(days=ttl_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    entry = {
        "id": token_id, "hash": _hash_token(raw), "username": username, "role": role,
        "kind": kind, "label": label, "created_at": _now(), "expires_at": expires_at,
        "last_used": None,
    }
    # Two people signing in at the same instant each mint a session token. An
    # unlocked read-modify-write here meant one of those two sessions was
    # never persisted -- and the user it belonged to got bounced back to the
    # login screen on their very next request, with nothing to explain it.
    storage.mutate_tokens(lambda tokens: tokens.update({token_id: entry}))
    return token_id, raw


def verify_token(raw):
    if not raw:
        return None
    h = _hash_token(raw)
    tokens = storage.load_tokens()
    now = datetime.datetime.utcnow()
    for tid, d in tokens.items():
        if hmac.compare_digest(d.get("hash", ""), h):
            if d.get("expires_at"):
                try:
                    if now > datetime.datetime.strptime(d["expires_at"], "%Y-%m-%dT%H:%M:%SZ"):
                        return None
                except ValueError:
                    pass
            # confirm the user still exists / is enabled and pick up role changes
            user = get_user(d["username"])
            if not user or user.get("disabled"):
                return None
            # Stamp "last used" -- but this is purely informational, so make it
            # (a) throttled: skip if we already stamped within the last minute,
            # to avoid rewriting tokens.json on every single polled request, and
            # (b) best-effort: a failed write must never turn a valid request
            # into a 500. Auth succeeds regardless of whether the stamp lands.
            #
            # This was the most damaging race in the app. Every authenticated
            # request could rewrite the ENTIRE tokens document from a snapshot
            # read moments earlier, so a stamp landing at the same time as
            # someone's login deleted that brand-new session token -- and the
            # UI polls status every 1.5 seconds, so the collision window was
            # open more or less continuously. Now only the one field is
            # touched, inside the lock.
            if _should_stamp(d.get("last_used"), now):
                try:
                    _stamp_last_used(tid)
                except Exception:
                    pass
            return {"username": d["username"], "role": user["role"], "token_id": tid, "kind": d.get("kind")}
    return None


def _stamp_last_used(token_id):
    stamp = _now()

    def _apply(tokens):
        entry = tokens.get(token_id)
        if entry is not None:
            entry["last_used"] = stamp

    storage.mutate_tokens(_apply)


def _should_stamp(last_used, now):
    if not last_used:
        return True
    try:
        prev = datetime.datetime.strptime(last_used, "%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError):
        return True
    return (now - prev).total_seconds() >= 60


def list_tokens(username=None):
    tokens = storage.load_tokens()
    out = []
    for tid, d in tokens.items():
        if username and d.get("username") != username:
            continue
        out.append({k: d.get(k) for k in ("id", "username", "role", "kind", "label",
                                          "created_at", "expires_at", "last_used")})
    return sorted(out, key=lambda x: x.get("created_at") or "", reverse=True)


def revoke_token(token_id, username=None):
    removed = [False]

    def _apply(tokens):
        entry = tokens.get(token_id)
        if entry is None:
            return
        if username is not None and entry.get("username") != username:
            return
        del tokens[token_id]
        removed[0] = True

    storage.mutate_tokens(_apply)
    return removed[0]


# ---------- bootstrap ----------

def bootstrap_admin():
    """Ensure at least one admin exists. On a fresh install this creates an
    'admin' account; the password comes from TS_ADMIN_PASSWORD if set,
    otherwise a random one is generated and printed to the server console
    once (so a local operator can log in and then change it)."""
    password = os.environ.get("TS_ADMIN_PASSWORD") or secrets.token_urlsafe(12)
    created = [False]

    def _apply(users):
        if any(d.get("role") == "admin" and not d.get("disabled") for d in users.values()):
            return
        if "admin" in users:
            return
        users["admin"] = {"password": hash_password(password), "role": "admin",
                          "disabled": False, "created_at": _now()}
        created[0] = True

    storage.mutate_users(_apply)
    if not created[0]:
        return
    generated = not os.environ.get("TS_ADMIN_PASSWORD")
    print("=" * 68, flush=True)
    print("[TS Debug Helper] Created initial admin account.", flush=True)
    print("    username: admin", flush=True)
    if generated:
        print(f"    password: {password}", flush=True)
        print("    (randomly generated -- log in and change it, or set", flush=True)
        print("     TS_ADMIN_PASSWORD before first run to choose your own)", flush=True)
    else:
        print("    password: (from TS_ADMIN_PASSWORD)", flush=True)
    print("=" * 68, flush=True)


# ---------- FastAPI dependencies ----------

def _identity_from_request(request: Request):
    # 1) Bearer token (API / MCP)
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        ident = verify_token(auth_header[7:].strip())
        if ident:
            return ident
    # 2) session cookie (web UI)
    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie:
        ident = verify_token(cookie)
        if ident:
            return ident
    return None


def current_identity(request: Request):
    return _identity_from_request(request)


def raw_token_from_request(request: Request):
    """The caller's raw token string, not just their identity.

    The chat agent needs this because the MCP tools it runs loop back into
    this app's own HTTP API and must present a real Bearer token there. A
    session cookie IS a token of kind="session", so handing it straight back
    makes the loopback act as the same user with the same role and the same
    org visibility -- no second credential to mint, expire, or leak."""
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        raw = auth_header[7:].strip()
        if raw and verify_token(raw):
            return raw
    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie and verify_token(cookie):
        return cookie
    return ""


def require(min_role):
    """FastAPI dependency factory: require an authenticated identity of at
    least `min_role`."""
    min_rank = ROLE_RANK[min_role]

    def _dep(request: Request):
        ident = _identity_from_request(request)
        if not ident:
            raise HTTPException(401, "Authentication required.")
        if ROLE_RANK.get(ident["role"], -1) < min_rank:
            raise HTTPException(403, f"This action requires the '{min_role}' role or higher.")
        return ident

    return _dep


require_reader = require("reader")
require_user = require("user")
require_admin = require("admin")

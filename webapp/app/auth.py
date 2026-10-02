"""
Local auth layer: username/password accounts with three roles, plus
long-lived API tokens for the MCP server.

Design notes
------------
- Accounts arrive two ways: an admin creates one, or a visitor signs
  themselves up. Both land in the same store; `created_by` records which,
  and `verified` records whether an admin has since vouched for a
  self-signup. Neither field affects what the account may *do* -- the role
  decides that -- but together they decide its LLM quota tier, which is why
  self-signup can be open without handing a stranger the whole token
  budget. See app/limits.py.
- Self-signup is only safe because the app is reachable only over the Conga
  VPN, so "anyone who can reach the form" is already "anyone inside the
  company". Two things still have to hold here: the role a signup may
  request is whitelisted server-side (a posted role is an attacker's field,
  not the user's), and the endpoint is rate limited (it is the one
  unauthenticated write in the app).
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
import re
import hashlib
import hmac
import secrets
import datetime

from fastapi import Request, HTTPException, Depends

from . import storage
from . import limits

ROLES = ["reader", "user", "admin"]
ROLE_RANK = {r: i for i, r in enumerate(ROLES)}

# What a visitor may ask to be. `admin` is absent on purpose and the check is
# server-side: the role arrives in the request body, so trusting it would make
# the signup form a public admin-promotion endpoint.
SIGNUP_ROLES = ["user", "reader"]
SIGNUP_DEFAULT_ROLE = "user"

# Usernames appear in file paths (data/chats/<username>/...), in the org
# ownership field, and in the usage ledger. Restricting the charset keeps all
# three unambiguous and keeps path traversal out of the picture entirely.
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,31}$")
USERNAME_RULE = ("3-32 characters, lowercase letters, digits, dot, underscore "
                 "or hyphen, starting with a letter or digit")
# Names that would either collide with a real account or read as one in the
# UI. `admin` is here because the bootstrap account owns it; the rest are
# reserved so nobody signs up as something that looks like the system talking.
RESERVED_USERNAMES = {
    "admin", "administrator", "root", "system", "superuser", "su",
    "api", "mcp", "me", "self", "anonymous", "null", "none", "support",
    "ts-debug-helper", "conga",
}

MIN_PASSWORD_LENGTH = 8

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
        u: {"role": d["role"], "disabled": d.get("disabled", False),
            "created_at": d.get("created_at"),
            # Provenance and vouching. Both are shown in the admin table
            # because together they are what an admin needs in order to
            # decide whether to verify an account, and they are the only
            # visible difference between a colleague who signed up and an
            # account somebody was given.
            "created_by": d.get("created_by"),
            "verified": bool(d.get("verified")),
            "verified_by": d.get("verified_by"),
            "verified_at": d.get("verified_at"),
            "limits": d.get("limits") or None}
        for u, d in users.items()
    }


def get_user(username):
    return storage.load_users().get(username)


def normalize_username(username):
    """Lowercase, trimmed, and validated against the charset.

    Lowercasing at the door rather than comparing case-insensitively later
    is what stops `Dana` and `dana` becoming two accounts that look like one
    in every list -- and, because the username is also a directory name
    under data/chats, two accounts that collide on a case-insensitive
    filesystem but not on a case-sensitive one.
    """
    username = (username or "").strip().lower()
    if not username:
        raise ValueError("username is required")
    if not USERNAME_RE.match(username):
        raise ValueError(f"username must be {USERNAME_RULE}")
    if username in RESERVED_USERNAMES:
        raise ValueError(f"'{username}' is a reserved username; please choose another")
    return username


def validate_password(password):
    if not password or len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    return password


def create_user(username, password, role, created_by=None, verified=None):
    """Create one account.

    `created_by` is `"self"` for a signup, `"admin:<who>"` for an account an
    admin made, and absent only on accounts that predate the field.
    `verified` defaults to True for anything an admin created, because the
    admin creating it is the vouching step -- making them then verify their
    own new account is a click that teaches people to click past it.
    """
    username = normalize_username(username)
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    validate_password(password)
    if verified is None:
        verified = created_by != "self"
    # The existence check and the insert have to happen under one lock, or
    # two admins creating the same username at the same moment both pass the
    # check and the second silently overwrites the first one's password.
    clash = [False]

    def _insert(users):
        if username in users:
            clash[0] = True
            return
        users[username] = {"password": hash_password(password), "role": role,
                           "disabled": False, "created_at": _now(),
                           "created_by": created_by, "verified": bool(verified)}

    storage.mutate_users(_insert)
    if clash[0]:
        raise ValueError(f"user '{username}' already exists")
    return {"username": username, "role": role, "verified": bool(verified)}


def signup_user(username, password, role=None):
    """Self-service registration.

    The whole security value of this function is the role whitelist, and it
    is here rather than in the route so that no future caller can bypass it
    by reaching for `create_user` directly. An unrecognised role is rejected
    outright rather than quietly downgraded to the default: silently
    granting something other than what was asked for hides a UI bug from
    whoever has to debug it.
    """
    if not signup_enabled():
        raise PermissionError("Self-registration is disabled on this server.")
    role = (role or SIGNUP_DEFAULT_ROLE).strip().lower()
    if role not in SIGNUP_ROLES:
        raise ValueError(f"role must be one of {SIGNUP_ROLES}")
    return create_user(username, password, role, created_by="self", verified=False)


def signup_enabled():
    """Off by setting TS_SIGNUP_ENABLED to 0/false/no. Default on, since the
    app is VPN-only and open registration is the point of this feature -- but
    an operator needs a way to close it without a code change if it is ever
    abused."""
    raw = (os.environ.get("TS_SIGNUP_ENABLED") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


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


def set_verified(username, verified, by=None):
    """Vouch for (or un-vouch) an account.

    Verification is deliberately not a role change and grants no new
    permission: it moves the account from the unverified quota tier to the
    verified one and nothing else. Keeping the two concepts apart means an
    admin can raise somebody's token budget without also widening what they
    can reach, which is the common case.

    Un-verifying is allowed, and is the lever to pull when an account is
    burning budget: it lowers the tier immediately without disabling the
    person mid-investigation.
    """
    verified = bool(verified)
    stamp = _now() if verified else None
    _update_user(username, lambda u: u.update({
        "verified": verified,
        "verified_by": by if verified else None,
        "verified_at": stamp,
    }))
    return {"username": username, "verified": verified}


def set_user_limits(username, override):
    """Set or clear one account's quota override.

    `None` clears it, which is not the same as setting both fields to
    unlimited: cleared means the account follows its tier, so a later change
    to the tier default reaches it. A stored override would shield it from
    that forever, which is a surprise an admin does not need six months
    later.
    """
    cleaned = limits.clean_override(override)

    def _apply(u):
        if cleaned is None:
            u.pop("limits", None)
        else:
            u["limits"] = cleaned

    _update_user(username, _apply)
    return {"username": username, "limits": cleaned}


def reset_password(username, new_password):
    validate_password(new_password)
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
                          "disabled": False, "created_at": _now(),
                          "created_by": "bootstrap", "verified": True}
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
    ident = _resolve_identity(request)
    if ident:
        # Left on the request for app/activity.py's middleware, which runs
        # outside the router and would otherwise have to verify the token a
        # second time just to learn who acted.
        try:
            request.state.activity_ident = ident
        except Exception:  # noqa: BLE001
            pass
    return ident


def _resolve_identity(request: Request):
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

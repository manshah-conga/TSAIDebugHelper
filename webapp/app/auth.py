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
    users = storage.load_users()
    if username in users:
        raise ValueError(f"user '{username}' already exists")
    users[username] = {"password": hash_password(password), "role": role,
                       "disabled": False, "created_at": _now()}
    storage.save_users(users)
    return {"username": username, "role": role}


def set_role(username, role):
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    users = storage.load_users()
    if username not in users:
        raise ValueError(f"no such user '{username}'")
    users[username]["role"] = role
    storage.save_users(users)


def set_disabled(username, disabled):
    users = storage.load_users()
    if username not in users:
        raise ValueError(f"no such user '{username}'")
    users[username]["disabled"] = bool(disabled)
    storage.save_users(users)


def reset_password(username, new_password):
    if not new_password or len(new_password) < 8:
        raise ValueError("password must be at least 8 characters")
    users = storage.load_users()
    if username not in users:
        raise ValueError(f"no such user '{username}'")
    users[username]["password"] = hash_password(new_password)
    storage.save_users(users)


def delete_user(username):
    users = storage.load_users()
    if username not in users:
        raise ValueError(f"no such user '{username}'")
    del users[username]
    storage.save_users(users)
    # revoke that user's tokens too
    tokens = storage.load_tokens()
    for tid in [t for t, d in tokens.items() if d.get("username") == username]:
        del tokens[tid]
    storage.save_tokens(tokens)


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
    tokens = storage.load_tokens()
    expires_at = None
    if ttl_days:
        expires_at = (datetime.datetime.utcnow() + datetime.timedelta(days=ttl_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    tokens[token_id] = {
        "id": token_id, "hash": _hash_token(raw), "username": username, "role": role,
        "kind": kind, "label": label, "created_at": _now(), "expires_at": expires_at,
        "last_used": None,
    }
    storage.save_tokens(tokens)
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
            d["last_used"] = _now()
            storage.save_tokens(tokens)
            return {"username": d["username"], "role": user["role"], "token_id": tid, "kind": d.get("kind")}
    return None


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
    tokens = storage.load_tokens()
    if token_id not in tokens:
        return False
    if username is not None and tokens[token_id].get("username") != username:
        return False
    del tokens[token_id]
    storage.save_tokens(tokens)
    return True


# ---------- bootstrap ----------

def bootstrap_admin():
    """Ensure at least one admin exists. On a fresh install this creates an
    'admin' account; the password comes from TS_ADMIN_PASSWORD if set,
    otherwise a random one is generated and printed to the server console
    once (so a local operator can log in and then change it)."""
    users = storage.load_users()
    if any(d.get("role") == "admin" and not d.get("disabled") for d in users.values()):
        return
    if "admin" in users:
        return
    password = os.environ.get("TS_ADMIN_PASSWORD") or secrets.token_urlsafe(12)
    users["admin"] = {"password": hash_password(password), "role": "admin",
                      "disabled": False, "created_at": _now()}
    storage.save_users(users)
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

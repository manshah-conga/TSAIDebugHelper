"""
Per-user LLM API keys, encrypted at rest with a key derived from the user's
own password.

Its role changed -- read this first
-----------------------------------
This used to be the ONLY way to get an LLM into the app, and every user had
to walk through it. It is now an **admin-only override** sitting behind the
shared, server-configured connection in app/llm_config.py.

The shared connection is what ordinary users get: configured once in the
server's environment, available to everyone the moment they sign in, no key
to paste and no password to re-enter after a restart. An admin who wants
their own turns billed to their own provider account can still store a
personal key here, and it wins for their sessions only.

Nothing below changed mechanically. The scheme, the consequences, and the
tests are all as they were -- what changed is who can reach it
(`require_admin` on the routes) and what happens when there is no personal
key: instead of "chat is unavailable", the caller falls through to the
shared connection. See `require_creds` at the bottom for that order.

Why not a server master key
---------------------------
The obvious design is one AES key in the environment and every user's API
key encrypted under it. That makes `data/` plus the env file a single
compromise instead of two, on a box where they sit side by side. Nothing in
this app needs a user's key while that user is absent -- chat is inherently
interactive -- so we can afford the stronger option.

The scheme
----------
Two layers, so a password change rewraps 32 bytes instead of needing the
plaintext API key back:

    KEK  = PBKDF2-HMAC-SHA256(password, kek_salt, 400k)   # never stored
    DEK  = random 32 bytes                                # stored wrapped
    wrapped_dek = AESGCM(KEK).encrypt(dek_nonce, DEK, aad=username)
    ciphertext  = AESGCM(DEK).encrypt(key_nonce, api_key, aad=username)

Only `kek_salt`, `wrapped_dek`, `dek_nonce`, `ciphertext` and `key_nonce`
touch disk. An attacker holding a full copy of data/ -- a backup, a stolen
disk, a mis-scoped rsync -- has nothing usable, because the KEK exists only
in the user's head and, briefly, in this process's RAM.

`username` is the AEAD associated data on both layers, so a record moved
between users fails to authenticate rather than decrypting to someone else's
key.

The consequences, which are real and are surfaced in the UI
-----------------------------------------------------------
* The plaintext key lives only in KEYRING, an in-memory dict keyed by
  session token id. A server restart empties it: every user's chat is locked
  until they re-enter their password. Everything else in the app keeps
  working on the existing session cookie.
* KEYRING is per *process*. With more than one uvicorn worker a user would be
  unlocked on one worker and locked on another, at random. Deploy
  single-worker (the shipped systemd unit does) or read the C2-b note in the
  design doc before scaling out.
* A self-service password change can rewrap, because both passwords are in
  hand at that moment.
* An ADMIN password reset cannot. The key is unrecoverable by construction
  and the record is deleted -- see `note_password_reset_by_admin`.
"""
import base64
import datetime
import hashlib
import os
import secrets
import threading

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag

from . import storage
from . import llm_config

KEK_ROUNDS = 400_000
ALG = "AESGCM-256"
KDF = "pbkdf2-sha256"

LLM_KEYS_PATH = os.path.join(storage.AUTH_ROOT, "llm_keys.json")

SUPPORTED_PROVIDERS = ("openrouter", "azure")
DEFAULT_PROVIDER = "openrouter"


class KeyLocked(Exception):
    """A key is on file but this session has not unwrapped it."""


class KeyMissing(Exception):
    """No key on file for this user."""


class BadPassword(Exception):
    """The supplied password did not unwrap the key."""


def _now():
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


# ---------- record persistence ----------

def _load_all():
    return storage.read_json(LLM_KEYS_PATH, {}) or {}


def _mutate_all(mutator):
    """Every write in this module changes ONE user's record inside a document
    that holds all of them, so a plain load/modify/save loses a concurrent
    change to a different user. Routed through storage.mutate_json, which
    holds an exclusive lock across the whole read-modify-write."""
    return storage.mutate_json(LLM_KEYS_PATH, mutator, {})


def get_record(username):
    return _load_all().get(username)


# ---------- the in-memory keyring ----------
#
# token_id -> plaintext API key. Never persisted, never logged, never returned
# over the API. A lock because BackgroundTasks and the SSE turn handler can
# touch it from different threads.

_KEYRING = {}
_KEYRING_LOCK = threading.Lock()


def put_in_keyring(token_id, username, api_key):
    """Entries carry their owner's username.

    That is deliberate, not incidental. The first version of `evict_user`
    looked owners up in tokens.json, which meant eviction silently did nothing
    whenever that lookup missed -- a revoked or admin-cleared key would keep
    working in every already-open session until the process restarted.
    Recording the owner alongside the key makes eviction self-contained and
    impossible to no-op.
    """
    if not token_id:
        return
    with _KEYRING_LOCK:
        _KEYRING[token_id] = (username, api_key)


def key_for_session(token_id):
    """The plaintext key for this session, or None if locked/absent."""
    if not token_id:
        return None
    with _KEYRING_LOCK:
        entry = _KEYRING.get(token_id)
    return entry[1] if entry else None


def evict(token_id):
    with _KEYRING_LOCK:
        _KEYRING.pop(token_id, None)


def evict_user(username):
    """Drop every session's unwrapped copy for one user. Called whenever the
    stored key stops being the right one: replaced, removed, or destroyed by
    an admin password reset."""
    with _KEYRING_LOCK:
        for tid in [t for t, (u, _k) in _KEYRING.items() if u == username]:
            _KEYRING.pop(tid, None)


# ---------- derivation and wrapping ----------

def _derive_kek(password: str, salt: bytes, rounds=None) -> bytes:
    """Note `rounds=None` rather than `rounds=KEK_ROUNDS`. A default argument
    is bound once, at import; the module global is read per call. If those two
    ever disagree -- because the constant was raised, or a test lowered it --
    a key would be wrapped at one cost factor and unwrapped at another, and
    every unlock would fail with a bogus 'wrong password'. Read it live."""
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt,
                               rounds if rounds is not None else KEK_ROUNDS, dklen=32)


def _unwrap_dek(record, password):
    salt = _b64d(record["kek_salt"])
    kek = _derive_kek(password, salt, record.get("rounds", KEK_ROUNDS))
    aad = record["username"].encode("utf-8")
    try:
        return AESGCM(kek).decrypt(_b64d(record["dek_nonce"]), _b64d(record["wrapped_dek"]), aad)
    except InvalidTag:
        raise BadPassword("That password does not unlock the stored key.")


def _decrypt_key(record, dek):
    aad = record["username"].encode("utf-8")
    try:
        raw = AESGCM(dek).decrypt(_b64d(record["key_nonce"]), _b64d(record["ciphertext"]), aad)
    except InvalidTag:
        # Key material is internally inconsistent -- treat like a bad password
        # rather than a 500, since the remedy for the user is the same.
        raise BadPassword("The stored key could not be decrypted.")
    return raw.decode("utf-8")


def _hint(api_key):
    """Enough to recognise the key, not enough to use it: prefix + last 4."""
    api_key = api_key or ""
    if len(api_key) <= 8:
        return "*" * len(api_key)
    head = api_key[:10] if api_key.startswith("sk-") else api_key[:4]
    return f"{head}…{api_key[-4:]}"


# ---------- public operations ----------

def store_key(username, password, api_key, provider=DEFAULT_PROVIDER, token_id=None,
              endpoint=None):
    """Wrap and persist a new key, replacing any existing one, and unlock it
    for the calling session. `password` is verified by the caller against the
    account first; here it is only key material."""
    api_key = (api_key or "").strip()
    if not api_key:
        raise ValueError("An API key is required.")
    provider = (provider or DEFAULT_PROVIDER).strip().lower()
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError(f"provider must be one of {list(SUPPORTED_PROVIDERS)}")

    rounds = KEK_ROUNDS
    kek_salt = secrets.token_bytes(16)
    kek = _derive_kek(password, kek_salt, rounds)
    dek = AESGCM.generate_key(bit_length=256)
    dek_nonce = secrets.token_bytes(12)
    key_nonce = secrets.token_bytes(12)
    aad = username.encode("utf-8")

    record = {
        "username": username,
        "alg": ALG,
        "kdf": KDF,
        "rounds": rounds,
        "kek_salt": _b64e(kek_salt),
        "wrapped_dek": _b64e(AESGCM(kek).encrypt(dek_nonce, dek, aad)),
        "dek_nonce": _b64e(dek_nonce),
        "ciphertext": _b64e(AESGCM(dek).encrypt(key_nonce, api_key.encode("utf-8"), aad)),
        "key_nonce": _b64e(key_nonce),
        "provider": provider,
        # Not a credential, so not encrypted -- it is a URL the user pasted and
        # the UI has to show it back so they can check the deployment name. It
        # does name internal infrastructure, so it stays inside data/auth/ with
        # everything else rather than anywhere public.
        "endpoint": (endpoint or "").strip() or None,
        "hint": _hint(api_key),
        "created_at": _now(),
        "verified_at": None,
        "default_model": None,
    }
    def _apply(records):
        previous = records.get(username) or {}
        # Keep the user's model choice across a key replacement.
        record["default_model"] = previous.get("default_model")
        records[username] = record

    _mutate_all(_apply)

    evict_user(username)
    put_in_keyring(token_id, username, api_key)
    return public_state(username, token_id)


def unlock(username, password, token_id):
    """Re-derive the KEK and put the plaintext key back in this session's
    keyring. Raises KeyMissing or BadPassword."""
    record = get_record(username)
    if not record:
        raise KeyMissing("No API key is stored for this account.")
    dek = _unwrap_dek(record, password)
    api_key = _decrypt_key(record, dek)
    put_in_keyring(token_id, username, api_key)
    return api_key


def unlock_quietly(username, password, token_id):
    """Login-path helper: unlock if there is a key and the password works,
    and never raise. Login must succeed whether or not a key exists."""
    try:
        unlock(username, password, token_id)
        return True
    except Exception:
        return False


def rewrap_for_new_password(username, old_password, new_password):
    """Self-service password change. Both passwords are in hand, so the DEK is
    unwrapped under the old KEK and re-wrapped under the new one -- the API
    key's own ciphertext is never touched. No-op when no key is stored."""
    record = get_record(username)
    if not record:
        return False
    dek = _unwrap_dek(record, old_password)

    rounds = KEK_ROUNDS
    kek_salt = secrets.token_bytes(16)
    kek = _derive_kek(new_password, kek_salt, rounds)
    dek_nonce = secrets.token_bytes(12)
    wrapped = _b64e(AESGCM(kek).encrypt(dek_nonce, dek, username.encode("utf-8")))

    def _apply(records):
        # Re-read inside the lock: the record may have been replaced between
        # the unwrap above and here, and rewrapping a DEK onto a record that
        # no longer holds the matching ciphertext would corrupt it.
        current = records.get(username)
        if not current or not current.get("ciphertext"):
            return
        current["kek_salt"] = _b64e(kek_salt)
        current["dek_nonce"] = _b64e(dek_nonce)
        current["wrapped_dek"] = wrapped
        current["rounds"] = rounds

    _mutate_all(_apply)
    return True


def note_password_reset_by_admin(username):
    """An admin does not know the old password, so the DEK cannot be unwrapped
    and the stored key is gone for good. Delete the record and leave a marker
    so the UI can explain what happened instead of showing an inscrutable
    'locked' state forever."""
    existed = [False]

    def _apply(records):
        record = records.pop(username, None)
        if record is None:
            return
        existed[0] = True
        records[username] = {
            "username": username,
            "cleared": True,
            "key_lost_at": _now(),
            "reason": "password_reset_by_admin",
            "provider": record.get("provider"),
            "default_model": record.get("default_model"),
        }

    _mutate_all(_apply)
    evict_user(username)
    return existed[0]


def remove_key(username, token_id=None):
    existed = [False]

    def _apply(records):
        if username not in records:
            return
        existed[0] = True
        default_model = (records[username] or {}).get("default_model")
        del records[username]
        if default_model:
            records[username] = {"username": username, "cleared": True,
                                 "reason": "removed_by_user", "default_model": default_model}

    _mutate_all(_apply)
    evict_user(username)
    return existed[0]


def forget_user(username):
    """Called when an account is deleted.

    Written as a named function rather than a lambda for a reason worth
    recording: `lambda records: records.pop(username, None) and None` looks
    equivalent and is not. `mutate_json` treats a non-None return as a
    REPLACEMENT document, and `{} and None` evaluates to `{}` -- so popping a
    record that happened to be an empty dict would replace the entire key
    store with one, destroying every other user's wrapped key. A mutator must
    either change its argument in place and return None, or return a
    deliberate replacement; never both by accident.
    """
    def _drop(records):
        records.pop(username, None)

    _mutate_all(_drop)
    evict_user(username)


def set_default_model(username, model):
    def _apply(records):
        record = records.get(username) or {"username": username, "cleared": True}
        record["default_model"] = model or None
        records[username] = record

    _mutate_all(_apply)


def mark_verified(username):
    """Stamp a successful provider call. Best-effort: a failed write must never
    turn a working chat turn into an error.

    A user on the shared connection has no record here to stamp, so the stamp
    lands on the shared connection's own state instead -- otherwise "last
    verified" would be permanently blank for everyone except the admins who
    store a personal key, which is precisely backwards."""
    try:
        if not has_usable_key(get_record(username)):
            llm_config.mark_verified(_now())
            return

        def _apply(records):
            if username in records:
                records[username]["verified_at"] = _now()

        _mutate_all(_apply)
    except Exception:
        pass


def has_usable_key(record):
    return bool(record) and not record.get("cleared") and bool(record.get("ciphertext"))


def public_state(username, token_id):
    """Everything the UI is allowed to know about this account's PERSONAL
    key. Never the key itself.

    Note what this does not describe: the shared server connection. That has
    its own reporter (`llm_config.public_state`), and keeping the two apart
    matters -- conflating them would leave an ordinary user looking at a
    screen that says "not configured" while their chat works perfectly,
    because it is running on the shared connection. `effective_state` below
    is the one the UI actually renders.
    """
    record = get_record(username)
    usable = has_usable_key(record)
    return {
        "configured": usable,
        "unlocked": bool(usable and key_for_session(token_id)),
        "provider": (record or {}).get("provider") or DEFAULT_PROVIDER,
        "endpoint": (record or {}).get("endpoint") if usable else None,
        "hint": (record or {}).get("hint") if usable else None,
        "created_at": (record or {}).get("created_at") if usable else None,
        "verified_at": (record or {}).get("verified_at") if usable else None,
        "default_model": (record or {}).get("default_model"),
        "cleared_reason": (record or {}).get("reason") if record and record.get("cleared") else None,
        "key_lost_at": (record or {}).get("key_lost_at") if record and record.get("cleared") else None,
    }


def effective_state(ident):
    """What the UI renders: which connection this session will actually use,
    and whether chat is ready.

    The shape is deliberately flat, with `ready` decided here rather than in
    JavaScript. The dock previously inferred readiness from `configured &&
    unlocked`, which is now wrong for the great majority of users -- they
    have no personal key at all and are nonetheless entirely able to chat.
    """
    username = ident["username"]
    is_admin = ident.get("role") == "admin"
    personal = public_state(username, ident.get("token_id"))
    shared = llm_config.public_state()

    # A personal key only counts when it is present AND unlocked AND the
    # holder is still an admin. The role check is what stops a demoted admin
    # from quietly continuing on their own key after the privilege was taken
    # away.
    personal_active = bool(personal["configured"] and personal["unlocked"] and is_admin)
    using = "personal" if personal_active else ("shared" if shared["configured"] else None)

    if using == "personal":
        provider = personal["provider"]
        default_model = personal["default_model"] or shared["default_model"]
        model_locked = False             # an admin on their own key picks freely
    else:
        provider = shared["provider"]
        default_model = shared["default_model"] or personal["default_model"]
        model_locked = shared["model_locked"] and not is_admin

    return {
        "ready": using is not None,
        "using": using,
        "provider": provider,
        "default_model": default_model,
        "model_locked": model_locked,
        # An admin has a personal key on file but has not unlocked it this
        # session: chat still works on the shared connection, so this is a
        # nudge rather than a blocker.
        "personal_key_locked": bool(personal["configured"] and not personal["unlocked"] and is_admin),
        "can_manage": is_admin,
        "shared": shared,
        # Only admins are shown the personal-key panel, so only admins are
        # sent its state. There is nothing secret in it, but a field a user
        # can never act on is a field that only raises questions.
        "personal": personal if is_admin else None,
    }


def require_key(ident):
    """The plaintext key for the identity making this request, or a typed
    exception the route can turn into a specific, actionable message."""
    return require_creds(ident)["api_key"]


def require_creds(ident):
    """Key plus provider and endpoint -- everything a call needs, in the one
    shape llm.py takes, so callers never assemble it themselves and cannot
    forget the endpoint on the Azure path.

    Resolution order, and the reasoning for it:

    1. **This admin's own unlocked key.** An explicit act by someone with the
       privilege to perform it, so it wins. Restricted to admins because the
       route that stores one is; checking the role here too means a demotion
       takes effect immediately rather than at their next sign-in.
    2. **The shared server connection.** The default for everybody. No paste,
       no unlock, works straight after a restart.
    3. **Nothing** -- raise, with a message that says whose problem it is.
       A user cannot fix an unset server environment variable, so telling
       them to "add an API key" would send them looking for a screen that,
       for them, does not exist.

    An admin whose personal key is merely *locked* falls through to the
    shared connection rather than erroring. Their chat keeps working; the UI
    tells them separately that their own key is available to unlock.
    """
    username = ident["username"]
    record = get_record(username)

    if has_usable_key(record) and ident.get("role") == "admin":
        api_key = key_for_session(ident.get("token_id"))
        if api_key:
            return {"provider": record.get("provider") or DEFAULT_PROVIDER,
                    "api_key": api_key,
                    "endpoint": record.get("endpoint") or "",
                    "source": "personal"}

    shared = llm_config.creds()
    if shared:
        return dict(shared, source="shared")

    state = llm_config.public_state()
    if state["present_but_invalid"]:
        raise KeyMissing(
            "The server's LLM connection is configured but not usable: "
            f"{state['config_error']} An administrator needs to correct it on the server.")
    raise KeyMissing(
        "This server has no LLM connection configured yet, so chat is unavailable. "
        "Ask an administrator to set it up -- everything else in the app works without it.")

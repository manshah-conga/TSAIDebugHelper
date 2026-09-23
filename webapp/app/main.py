"""
TS Intelligent Debug Helper -- web app.

Endpoints group into three areas: org connections (create/list/refresh,
backed by a background fetch from Salesforce), lookups against an org's
knowledgebase (stats, field-writers, object-touch, component detail), and
incidents (file one from an uploaded log and/or a suspect field, list,
detail, resolve).

Run with:  python -m uvicorn app.main:app --reload --port 8000
           (from inside webapp/ -- see README section 1; add --host 0.0.0.0
            to make it reachable from other machines)
"""
import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, BackgroundTasks, UploadFile, File, Form, HTTPException, Depends, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import storage
from . import auth
from . import limits as limits_policy
from . import rate_limit
from . import org_access
from . import secrets_store
from . import chat_store
from . import chat as chat_agent
from . import env_file
from . import llm
from . import llm_config
from . import usage as usage_ledger
from .onboarding import run_onboarding, JOBS, progress_payload, job_in_flight
from .log_normalizer import parse_log_text
from .rca import assemble_context, lookup_field_writers
from .incidents import file_incident
from .common_now import iso_now
from .mcp_http import MCPTransportMiddleware, mcp_lifespan


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Process startup/shutdown. Creates the bootstrap admin, then holds the
    MCP Streamable HTTP session manager open for the life of the process --
    POST /mcp returns a 500 if that manager was never started."""
    auth.bootstrap_admin()
    # Say plainly at boot which config file was used and whether chat will
    # work. An operator who mistyped a variable -- or who created the file
    # somewhere the app does not look -- finds out here, at startup, rather
    # than from a user reporting broken chat an hour later.
    for line in env_file.startup_report():
        print(line, flush=True)
    print(llm_config.startup_report(), flush=True)
    async with mcp_lifespan(app):
        yield


app = FastAPI(title="TS Intelligent Debug Helper", lifespan=lifespan)

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")

# Role dependencies (see app/auth.py). Reads require reader+, write actions
# require user+, user/token administration requires admin.
Dep_reader = [Depends(auth.require_reader)]
Dep_user = [Depends(auth.require_user)]
Dep_admin = [Depends(auth.require_admin)]


def _org_view_dep(request: Request, ident=Depends(auth.require_reader)):
    """Per-org visibility gate for any route with an `{org_id}` path param
    (see app/org_access.py). Raises 404 -- not 403 -- for a private org you
    are not allowed to see, so the route never confirms it exists."""
    org_access.assert_can_view(request.path_params.get("org_id"), ident)
    return ident


# Read an org's knowledgebase/incidents: must be able to SEE the org.
Dep_org_view = [Depends(_org_view_dep)]
# Write against an org (file an incident, record a resolution): must be able
# to see it AND hold the `user` role. Visibility is checked first on purpose,
# so a reader is told "no such org" rather than "wrong role" for an org that
# is not theirs to know about.
Dep_org_write = [Depends(_org_view_dep), Depends(auth.require_user)]


# ---------- remote MCP endpoint ----------
# POST /mcp serves the MCP tools over Streamable HTTP, so a client needs only
# a URL and a token. The tools themselves live in mcp_server.py and are shared
# verbatim with the local stdio entry point; app/mcp_http.py supplies the
# transport and pulls the caller's API token off the request. It runs as
# middleware, above the router, so the bare /mcp path is not redirected.
app.add_middleware(MCPTransportMiddleware)


# ---------- authentication ----------

class LoginRequest(BaseModel):
    username: str
    password: str


class SignupRequest(BaseModel):
    username: str
    password: str
    role: Optional[str] = None


@app.get("/api/auth/signup-config")
def signup_config():
    """What the login screen needs to know before anyone is signed in:
    whether to offer a "create account" link at all, and which roles it may
    offer. Served unauthenticated because it is consumed by the one screen
    that by definition has no session yet, and it discloses nothing beyond
    the shape of the form."""
    return {"enabled": auth.signup_enabled(),
            "roles": auth.SIGNUP_ROLES,
            "default_role": auth.SIGNUP_DEFAULT_ROLE,
            "username_rule": auth.USERNAME_RULE,
            "min_password_length": auth.MIN_PASSWORD_LENGTH}


@app.post("/api/auth/signup")
def signup(req: SignupRequest, request: Request, response: Response):
    """Self-service registration, then straight in.

    Signing in immediately is deliberate: the alternative is to bounce a
    brand-new account to the login form to retype the password it just
    chose, which reads as a failure. There is nothing to unlock here --
    `secrets_store.unlock_quietly` is skipped because a new account cannot
    have a password-wrapped LLM key yet.
    """
    # Order matters. The "is registration even open" check is free and has no
    # side effect, so it goes first -- otherwise a closed endpoint still
    # spends the caller's allowance and answers 429 instead of saying it is
    # closed. Then the loose attempt limiter, and only once the request looks
    # like a real registration does it count against the tight
    # accounts-created allowance.
    if not auth.signup_enabled():
        raise HTTPException(403, "Self-registration is disabled on this server.")
    rate_limit.enforce("signup_attempt", request)
    try:
        auth.normalize_username(req.username)
        auth.validate_password(req.password)
    except ValueError as e:
        raise HTTPException(400, str(e))
    rate_limit.enforce("signup", request)
    try:
        created = auth.signup_user(req.username, req.password, req.role)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    tid, raw = auth.create_token(created["username"], created["role"], kind="session",
                                 label="web signup", ttl_days=auth.SESSION_TTL_DAYS)
    response.set_cookie(
        auth.SESSION_COOKIE, raw, httponly=True, samesite="lax",
        max_age=auth.SESSION_TTL_DAYS * 86400,
    )
    quota = limits_policy.quota_status(created["username"], auth.get_user(created["username"]))
    return {"username": created["username"], "role": created["role"],
            "llm_unlocked": False, "quota": quota}


@app.post("/api/auth/login")
def login(req: LoginRequest, request: Request, response: Response):
    rate_limit.enforce("login", request)
    ident = auth.authenticate(req.username, req.password)
    if not ident:
        raise HTTPException(401, "Invalid username or password (or the account is disabled).")
    tid, raw = auth.create_token(ident["username"], ident["role"], kind="session",
                                 label="web login", ttl_days=auth.SESSION_TTL_DAYS)
    response.set_cookie(
        auth.SESSION_COOKIE, raw, httponly=True, samesite="lax",
        max_age=auth.SESSION_TTL_DAYS * 86400,
    )
    # This is the one moment the plaintext password is in hand, so it is the
    # only moment a password-wrapped LLM key can be unwrapped without asking
    # again. Deliberately silent: login must succeed whether or not a key
    # exists, and a user with no key must not be told anything about it.
    unlocked = secrets_store.unlock_quietly(ident["username"], req.password, tid)
    quota = limits_policy.quota_status(ident["username"], auth.get_user(ident["username"]))
    return {"username": ident["username"], "role": ident["role"],
            "llm_unlocked": unlocked, "quota": quota}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response):
    cookie = request.cookies.get(auth.SESSION_COOKIE)
    if cookie:
        ident = auth.verify_token(cookie)
        if ident and ident.get("token_id"):
            secrets_store.evict(ident["token_id"])
            auth.revoke_token(ident["token_id"])
    response.delete_cookie(auth.SESSION_COOKIE)
    return {"ok": True}


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


@app.post("/api/auth/password")
def change_own_password(req: ChangePasswordRequest, ident=Depends(auth.require_reader)):
    """Change your OWN password. Previously only an admin could reset one,
    which left every account stuck on whatever temporary password an admin
    typed. Requires the current password, so a walk-up on an unlocked browser
    cannot lock the real owner out."""
    if not auth.authenticate(ident["username"], req.current_password):
        raise HTTPException(403, "Current password is incorrect.")
    try:
        auth.reset_password(ident["username"], req.new_password)
    except ValueError as e:
        raise HTTPException(400, str(e))
    # Both passwords are in hand here, which is exactly what makes a
    # self-service change survivable for the stored LLM key: re-wrap the DEK
    # under the new password. Best-effort -- a failure here must not leave the
    # account with a password that changed and a UI that says it did not.
    rewrapped = None
    try:
        rewrapped = secrets_store.rewrap_for_new_password(
            ident["username"], req.current_password, req.new_password)
    except Exception:
        rewrapped = False
    return {"ok": True, "llm_key_rewrapped": rewrapped}


@app.get("/api/auth/me")
def whoami(request: Request):
    ident = auth.current_identity(request)
    if not ident:
        raise HTTPException(401, "Not authenticated.")
    return {"username": ident["username"], "role": ident["role"]}


# ---------- admin: user management ----------

class NewUserRequest(BaseModel):
    username: str
    password: str
    role: str


class RoleRequest(BaseModel):
    role: str


class PasswordRequest(BaseModel):
    password: str


class DisabledRequest(BaseModel):
    disabled: bool


class VerifiedRequest(BaseModel):
    verified: bool


class UserLimitsRequest(BaseModel):
    # Absent means "leave that field alone"; explicit null means unlimited.
    # `clear` is the third state -- drop the override entirely so the account
    # follows its tier again -- which neither of the other two can express.
    daily_tokens: Optional[int] = None
    monthly_tokens: Optional[int] = None
    clear: bool = False


class TierLimitsRequest(BaseModel):
    tiers: Optional[dict] = None
    window_days: Optional[int] = None


@app.get("/api/admin/users", dependencies=Dep_admin)
def admin_list_users():
    """Every account, with its quota position alongside it.

    The quota figures are joined in here rather than fetched per row by the
    UI: the admin screen shows a table, and a per-row request would be one
    ledger read per user on every render.
    """
    users = auth.list_users()
    config = limits_policy.load_config()
    raw = storage.load_users()
    for username, row in users.items():
        row["quota"] = limits_policy.quota_status(username, raw.get(username), config)
    return users


@app.post("/api/admin/users", dependencies=Dep_admin)
def admin_create_user(req: NewUserRequest, request: Request):
    ident = auth.current_identity(request)
    try:
        return auth.create_user(req.username, req.password, req.role,
                                created_by=f"admin:{ident['username']}")
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.patch("/api/admin/users/{username}/verified", dependencies=Dep_admin)
def admin_set_verified(username: str, req: VerifiedRequest, request: Request):
    """Vouch for an account, which moves it to the verified quota tier.

    Grants no permission of its own -- see auth.set_verified for why that
    separation is deliberate.
    """
    ident = auth.current_identity(request)
    try:
        result = auth.set_verified(username, req.verified, by=ident["username"])
    except ValueError as e:
        raise HTTPException(400, str(e))
    result["quota"] = limits_policy.quota_status(username, auth.get_user(username))
    return result


@app.patch("/api/admin/users/{username}/limits", dependencies=Dep_admin)
def admin_set_user_limits(username: str, req: UserLimitsRequest):
    """Set or clear one account's quota override."""
    override = None
    if not req.clear:
        override = {}
        if req.daily_tokens is not None:
            override["daily_tokens"] = req.daily_tokens
        if req.monthly_tokens is not None:
            override["monthly_tokens"] = req.monthly_tokens
    try:
        result = auth.set_user_limits(username, override)
    except ValueError as e:
        raise HTTPException(400, str(e))
    result["quota"] = limits_policy.quota_status(username, auth.get_user(username))
    return result


@app.get("/api/admin/limits", dependencies=Dep_admin)
def admin_get_limits():
    """The tier defaults, editable from the admin screen.

    These are configuration rather than code precisely so that moving a cap
    does not need a deploy -- the first numbers are a guess at what a support
    engineer consumes, and guesses need adjusting by whoever is watching the
    usage report.
    """
    return {"config": limits_policy.load_config(),
            "fallback": limits_policy.DEFAULT_CONFIG,
            "tiers": list(limits_policy.TIERS)}


@app.put("/api/admin/limits", dependencies=Dep_admin)
def admin_put_limits(req: TierLimitsRequest, request: Request):
    ident = auth.current_identity(request)
    # Built by hand rather than with `model_dump(exclude_unset=True)`: inside
    # the tier dicts an explicit null means "unlimited", so the distinction
    # between absent and null has to survive, and it does that reliably here
    # regardless of which pydantic major version is installed.
    patch = {}
    if req.tiers is not None:
        patch["tiers"] = req.tiers
    if req.window_days is not None:
        patch["window_days"] = req.window_days
    try:
        config = limits_policy.save_config(patch, updated_by=ident["username"])
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"config": config}


@app.patch("/api/admin/users/{username}/role", dependencies=Dep_admin)
def admin_set_role(username: str, req: RoleRequest):
    try:
        auth.set_role(username, req.role)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"username": username, "role": req.role}


@app.patch("/api/admin/users/{username}/disabled", dependencies=Dep_admin)
def admin_set_disabled(username: str, req: DisabledRequest, request: Request):
    ident = auth.current_identity(request)
    if username == ident["username"] and req.disabled:
        raise HTTPException(400, "You cannot disable your own account.")
    try:
        auth.set_disabled(username, req.disabled)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"username": username, "disabled": req.disabled}


@app.post("/api/admin/users/{username}/reset-password", dependencies=Dep_admin)
def admin_reset_password(username: str, req: PasswordRequest):
    """Note the LLM-key consequence. An admin does not know the old password,
    so the key-encryption key cannot be re-derived and the user's stored LLM
    key is unrecoverable by construction. Clearing the record here is what
    lets the UI say so plainly, instead of leaving that user staring at a
    'locked' chat that no password will ever open."""
    try:
        auth.reset_password(username, req.password)
    except ValueError as e:
        raise HTTPException(400, str(e))
    key_cleared = secrets_store.note_password_reset_by_admin(username)
    return {"username": username, "ok": True, "llm_key_cleared": key_cleared}


@app.delete("/api/admin/users/{username}", dependencies=Dep_admin)
def admin_delete_user(username: str, request: Request):
    ident = auth.current_identity(request)
    if username == ident["username"]:
        raise HTTPException(400, "You cannot delete your own account.")
    try:
        auth.delete_user(username)
    except ValueError as e:
        raise HTTPException(400, str(e))
    secrets_store.forget_user(username)
    chat_store.forget_user(username)
    # Otherwise a deleted account's name lives on in the admin usage report
    # forever, which is both untidy and a small privacy problem.
    usage_ledger.forget_user(username)
    return {"ok": True}


# ---------- API tokens (for the MCP server) ----------

class NewTokenRequest(BaseModel):
    label: Optional[str] = None
    ttl_days: Optional[int] = None


@app.get("/api/tokens")
def list_tokens(request: Request, _ident=Depends(auth.require_reader)):
    # non-admins see only their own tokens; admins see all
    if _ident["role"] == "admin":
        return auth.list_tokens()
    return auth.list_tokens(username=_ident["username"])


@app.post("/api/tokens")
def create_token(req: NewTokenRequest, request: Request, _ident=Depends(auth.require_reader)):
    """Create a personal API token for the MCP server. The token inherits
    YOUR role, and is shown exactly once -- copy it now, it can't be
    retrieved later (only revoked)."""
    _tid, raw = auth.create_token(_ident["username"], _ident["role"], kind="api",
                                  label=req.label, ttl_days=req.ttl_days)
    return {"id": _tid, "token": raw, "role": _ident["role"],
            "note": "Copy this token now -- it will not be shown again."}


@app.delete("/api/tokens/{token_id}")
def revoke_token(token_id: str, request: Request, _ident=Depends(auth.require_reader)):
    username = None if _ident["role"] == "admin" else _ident["username"]
    if not auth.revoke_token(token_id, username=username):
        raise HTTPException(404, "No such token (or not yours to revoke).")
    secrets_store.evict(token_id)   # that session's unwrapped LLM key dies with it
    return {"ok": True}


# ---------- org connections ----------

class NewOrgRequest(BaseModel):
    org_id: str
    org_name: str
    instance_url: str
    access_token: str
    # "private" (default) -- only you and admins can see this org.
    # "public"            -- every signed-in account can see it.
    # On a re-connect of an org you already own, omitting this keeps the
    # visibility it already has.
    visibility: Optional[str] = None


class VisibilityRequest(BaseModel):
    visibility: str


@app.post("/api/orgs", dependencies=Dep_user)
def create_org(req: NewOrgRequest, background_tasks: BackgroundTasks,
               ident=Depends(auth.require_user)):
    # Refuse a second fetch of the same org while one is already running.
    # Two concurrent onboardings write the same knowledgebase files and the
    # same content-hash manifest, so each one's changed/added/removed report
    # is computed against a baseline the other already moved -- the result is
    # not corrupt, but it is confidently wrong, which is worse. This is the
    # one multi-user case locking cannot make safe, so it is refused instead.
    if job_in_flight(req.org_id):
        job = JOBS.get(req.org_id, {})
        raise HTTPException(
            409, f"A fetch of '{req.org_id}' is already running "
                 f"({job.get('step_label') or job.get('status')}). Wait for it to finish "
                 f"-- it may have been started by someone else.")

    registry = storage.load_registry()
    existing_entry = registry.get(req.org_id)

    if existing_entry is not None:
        # Re-connecting an org that already exists is a refresh of someone's
        # knowledgebase -- only its owner (or an admin) may do that.
        #
        # The org id is a GLOBAL name chosen by whoever connects first, so a
        # second person picking the same id is a name collision, not an
        # access attempt. Answering it the way a read is answered -- 404,
        # "No org 'X' (or you do not have access to it)" -- told someone
        # filling in the Connect form that the thing they are trying to
        # create does not exist, which is both confusing and unactionable.
        # Worse, it is what every non-admin saw for any id already in the
        # registry, including the pre-ownership orgs that are now
        # admin-only, so a new user's first attempt could hit it.
        #
        # So a create says the id is taken and to pick another. That does
        # reveal that *something* holds the id, which the 404 deliberately
        # hid -- an accepted trade, and a narrow one: no name, no owner, no
        # data, only "you cannot have this name". There is no way to run a
        # create endpoint on a shared namespace without telling the caller
        # their chosen name is unavailable, and the alternative (a silent
        # refusal, or letting them overwrite someone's org) is worse than
        # the disclosure.
        if not org_access.can_manage(existing_entry, ident):
            if not org_access.can_view(existing_entry, ident):
                raise HTTPException(
                    409,
                    f"The Org ID '{req.org_id}' is already in use by an org you do not have "
                    f"access to. Choose a different Org ID, or ask an admin -- if this is an "
                    f"org you should be able to see, they can share it with you.")
            existing_owner = org_access.owner_of(existing_entry)
            who = (f"'{existing_owner}'" if existing_owner
                   else "nobody yet (it predates org ownership, so only an admin can claim it)")
            raise HTTPException(
                409,
                f"The Org ID '{req.org_id}' already belongs to an org owned by {who}. Only its "
                f"owner or an admin can re-connect it. Choose a different Org ID to connect a "
                f"new org, or ask them to refresh it.")
        owner = org_access.owner_of(existing_entry) or ident["username"]
        default_visibility = org_access.visibility_of(existing_entry)
    else:
        owner = ident["username"]
        default_visibility = org_access.DEFAULT_VISIBILITY

    try:
        visibility = org_access.normalize_visibility(req.visibility, default=default_visibility)
    except ValueError as e:
        raise HTTPException(400, str(e))

    existing = storage.read_json(os.path.join(storage.kb_dir(req.org_id), "file_hashes.json"), {})
    JOBS[req.org_id] = {"status": "queued", "detail": "", "warnings": [], "owner": owner}
    background_tasks.add_task(
        run_onboarding, req.org_id, req.org_name, req.instance_url, req.access_token, existing,
        owner, visibility,
    )
    return {"org_id": req.org_id, "status": "queued", "owner": owner, "visibility": visibility}


@app.get("/api/orgs", dependencies=Dep_reader)
def list_orgs(ident=Depends(auth.require_reader)):
    """Only the orgs this account may see: everything public, plus your own
    private orgs (plus everyone's, if you are an admin). Each entry is
    decorated with `owner`, `visibility` and `can_manage`."""
    return org_access.visible_orgs(ident)


@app.get("/api/orgs/{org_id}/status", dependencies=Dep_reader)
def org_status(org_id: str, ident=Depends(auth.require_reader)):
    """Live progress for an in-flight fetch.

    The payload carries a percentage, the current phase's human label, the
    full phase list, per-phase counts and elapsed seconds -- everything the
    progress bar needs. A large org takes minutes, and a single status word
    was indistinguishable from a hang for most of that time."""
    job = JOBS.get(org_id)
    registry = storage.load_registry()
    if org_id in registry:
        org_access.assert_can_view(org_id, ident, registry)
    elif job is not None:
        # Still in flight -- no registry entry exists yet, so fall back to
        # whoever queued the job.
        if ident["role"] != "admin" and job.get("owner") != ident["username"]:
            raise HTTPException(404, f"No org '{org_id}' (or you do not have access to it).")
    return progress_payload(job)


@app.get("/api/orgs/{org_id}/visibility", dependencies=Dep_reader)
def get_visibility(org_id: str, ident=Depends(auth.require_reader)):
    entry = org_access.assert_can_view(org_id, ident)
    return {"org_id": org_id, "visibility": org_access.visibility_of(entry),
            "owner": org_access.owner_of(entry),
            "can_manage": org_access.can_manage(entry, ident)}


@app.patch("/api/orgs/{org_id}/visibility", dependencies=Dep_reader)
def set_visibility(org_id: str, req: VisibilityRequest, ident=Depends(auth.require_reader)):
    """Make an org public (visible to everyone signed in) or private (visible
    only to its owner and admins). Owner or admin only."""
    try:
        return org_access.set_visibility(org_id, req.visibility, ident)
    except ValueError as e:
        raise HTTPException(400, str(e))


class RefreshRequest(BaseModel):
    access_token: str
    # Optional overrides, for the rare case an org moved instance or was
    # renamed. Omitted -> reuse what is already on record.
    instance_url: Optional[str] = None
    org_name: Optional[str] = None


@app.post("/api/orgs/{org_id}/refresh", dependencies=Dep_user)
def refresh_org(org_id: str, req: RefreshRequest, background_tasks: BackgroundTasks,
                ident=Depends(auth.require_user)):
    """Re-fetch an org you already connected. Salesforce access tokens expire,
    so a fresh one is the only thing you have to supply -- the org's name,
    instance URL, owner and visibility all come from the registry, and each
    component's content hash decides what actually counts as changed. Owner or
    admin only, same as any other management action on an org."""
    if job_in_flight(org_id):
        job = JOBS.get(org_id, {})
        raise HTTPException(
            409, f"A fetch of '{org_id}' is already running "
                 f"({job.get('step_label') or job.get('status')}). Wait for it to finish "
                 f"-- it may have been started by someone else.")
    entry = org_access.assert_can_manage(org_id, ident)
    instance_url = (req.instance_url or entry.get("instance_url") or "").strip()
    if not instance_url:
        raise HTTPException(
            400, f"Org '{org_id}' has no instance URL on record -- pass instance_url with the refresh.")
    org_name = req.org_name or entry.get("name") or org_id

    existing = storage.read_json(os.path.join(storage.kb_dir(org_id), "file_hashes.json"), {})
    JOBS[org_id] = {"status": "queued", "detail": "", "warnings": [],
                    "owner": org_access.owner_of(entry) or ident["username"]}
    background_tasks.add_task(
        run_onboarding, org_id, org_name, instance_url, req.access_token, existing,
        org_access.owner_of(entry) or ident["username"], org_access.visibility_of(entry),
    )
    return {"org_id": org_id, "status": "queued", "instance_url": instance_url,
            "reused_instance_url": req.instance_url is None}


# ---------- knowledgebase lookups ----------

@app.get("/api/orgs/{org_id}/stats", dependencies=Dep_org_view)
def get_stats(org_id: str):
    kb = storage.load_kb(org_id)
    if not kb["org_index"]:
        raise HTTPException(404, f"No knowledgebase for org '{org_id}' yet.")
    return kb["org_stats"]


@app.get("/api/orgs/{org_id}/components/{component_id}", dependencies=Dep_org_view)
def get_component(org_id: str, component_id: str):
    kb = storage.load_kb(org_id)
    card = kb["org_index"].get(component_id)
    if not card:
        raise HTTPException(404, f"No component '{component_id}' in org '{org_id}'.")
    return card


@app.get("/api/orgs/{org_id}/object-touch/{object_name}", dependencies=Dep_org_view)
def get_object_touch(org_id: str, object_name: str):
    kb = storage.load_kb(org_id)
    return kb["object_touch_map"].get(object_name, {})


@app.get("/api/orgs/{org_id}/field-writers/{field_name}", dependencies=Dep_org_view)
def get_field_writers(org_id: str, field_name: str):
    kb = storage.load_kb(org_id)
    return lookup_field_writers(field_name, kb["org_index"], kb["field_touch_map"], kb["file_hashes"])


@app.get("/api/orgs/{org_id}/search", dependencies=Dep_org_view)
def search_components(org_id: str, q: str, customer_authored_only: bool = True):
    """Freeform lookup used by the 'ask a question' box in the UI: matches
    the query against component ids, objects touched, and field names, so
    a person (or an AI over MCP) can start from a vague description
    instead of an exact identifier. Defaults to customer-authored components
    only (§4.3) -- managed-package internals are usually noise in an
    incident; pass customer_authored_only=false to include them."""
    kb = storage.load_kb(org_id)
    q_lower = q.lower()
    index = kb["org_index"]

    def keep(cid):
        if not customer_authored_only:
            return True
        return index.get(cid, {}).get("is_customer_authored", True)

    return {
        "components": [cid for cid in index if q_lower in cid.lower() and keep(cid)][:25],
        "objects": [o for o in kb["object_touch_map"] if q_lower in o.lower()][:25],
        "fields": [f for f in kb["field_touch_map"] if q_lower in f.lower()][:25],
        "customer_authored_only": customer_authored_only,
    }


@app.get("/api/orgs/{org_id}/inbound/{component_id}", dependencies=Dep_org_view)
def get_inbound(org_id: str, component_id: str):
    """Reverse-call index (§7.1): everything that invokes this component --
    flows via actionCall, classes via method call, flows via subflow."""
    kb = storage.load_kb(org_id)
    return kb["inbound_index"].get(component_id, {"called_by": []})


@app.get("/api/orgs/{org_id}/entry-points/{object_name}", dependencies=Dep_org_view)
def get_entry_points(org_id: str, object_name: str):
    """Per-object automation entry points (§7.3): the flows / triggers /
    process builder / workflow field updates that fire when a record of this
    object is saved, plus any self-referential automation on it."""
    kb = storage.load_kb(org_id)
    return kb["entry_points_index"].get(object_name, {})


# ---------- incidents ----------

@app.post("/api/orgs/{org_id}/incidents", dependencies=Dep_org_write)
async def create_incident(
    org_id: str,
    label: Optional[str] = Form(None),
    field: Optional[str] = Form(None),
    log_file: Optional[UploadFile] = File(None),
):
    kb = storage.load_kb(org_id)
    if not kb["org_index"]:
        raise HTTPException(404, f"No knowledgebase for org '{org_id}' yet -- create the org connection first.")
    if not field and not log_file:
        raise HTTPException(400, "Provide a log file, a suspect field name, or both.")

    log_filename = None
    if log_file is not None:
        raw_bytes = await log_file.read()  # held in memory only; never written to disk
        raw_text = raw_bytes.decode("utf-8", errors="replace")
        log_filename = log_file.filename
        normalized = parse_log_text(raw_text, index_ids=set(kb["org_index"].keys()))
        del raw_bytes, raw_text  # explicit: the raw log does not outlive this request
    else:
        normalized = {
            "header": None, "execution_units": [], "exceptions": [], "soql_summary": [],
            "dml_summary": [], "callouts": [], "user_debug": [], "validation_failures": [],
            "flow_events": [], "limits_final": {}, "involved_components": [],
        }

    context_pack = assemble_context(normalized, kb["org_index"], kb["call_graph"],
                                     kb["object_touch_map"], kb["file_hashes"])

    field_result = None
    if field:
        field_result = lookup_field_writers(field, kb["org_index"], kb["field_touch_map"], kb["file_hashes"])
        context_pack["suspect_field"] = field
        context_pack["suspect_field_writers"] = field_result["writers"]
        for w in field_result["writers"]:
            if w["component"] in kb["org_index"]:
                context_pack["primary_components"][w["component"]] = kb["org_index"][w["component"]]

    now = iso_now()
    timestamp_slug = now.replace(":", "").replace("-", "")
    default_label = (os.path.splitext(log_filename)[0] if log_filename else field) or "incident"
    incident_id = f"{timestamp_slug}_{label or default_label}"

    # Signature matching and the recurrence bump have to happen inside one
    # lock. Two engineers filing against the same org read the same index,
    # each bumped its counter, and the second write discarded the first --
    # losing exactly the recurrence history this feature exists to build.
    outcome = {}

    def _file(known):
        signature, recurrence, prior, sig_source = file_incident(
            known, incident_id, normalized, field)
        outcome.update({"signature": signature, "recurrence": recurrence,
                        "prior": prior, "sig_source": sig_source})

    storage.mutate_known_issues(org_id, _file)
    signature = outcome["signature"]
    recurrence = outcome["recurrence"]
    prior = outcome["prior"]
    sig_source = outcome["sig_source"]

    meta = {
        "incident_id": incident_id, "org_id": org_id, "timestamp": now,
        "source_log": log_filename, "suspect_field": field, "signature": signature,
        "signature_source": sig_source, "recurrence": recurrence,
        "prior_occurrences": prior["occurrences"] if prior else 0,
        "prior_incident_ids": prior["incident_ids"] if prior else [],
        "prior_resolution": prior.get("resolution") if prior else None,
    }
    storage.save_incident(org_id, incident_id, normalized, context_pack, meta)

    return {"meta": meta, "field_writers": field_result["writers"] if field_result else None}


@app.get("/api/orgs/{org_id}/incidents", dependencies=Dep_org_view)
def get_incidents(org_id: str):
    return storage.list_incidents(org_id)


@app.get("/api/orgs/{org_id}/incidents/{incident_id}", dependencies=Dep_org_view)
def get_incident(org_id: str, incident_id: str):
    result = storage.load_incident(org_id, incident_id)
    if not result:
        raise HTTPException(404, f"No incident '{incident_id}' for org '{org_id}'.")
    return result


class ResolveRequest(BaseModel):
    signature: str
    resolution: str


@app.post("/api/orgs/{org_id}/resolve", dependencies=Dep_org_write)
def resolve_incident(org_id: str, req: ResolveRequest):
    missing = [False]
    result = {}

    def _resolve(known):
        entry = known.get(req.signature)
        if entry is None:
            missing[0] = True
            return
        entry["resolution"] = req.resolution
        entry["resolution_recorded_at"] = iso_now()
        result.update(entry)

    storage.mutate_known_issues(org_id, _resolve)
    if missing[0]:
        raise HTTPException(404, f"No known issue with signature '{req.signature}' for org '{org_id}'.")
    return result


@app.get("/api/orgs/{org_id}/known-issues", dependencies=Dep_org_view)
def get_known_issues(org_id: str):
    return storage.load_known_issues(org_id)


# ---------- standalone log normalization (no org / no code / no metadata) ----------

def _slugify(text, fallback="log"):
    keep = "".join(c if c.isalnum() or c in "-_" else "_" for c in (text or "")).strip("_")
    return keep or fallback


@app.post("/api/logs/normalize", dependencies=Dep_user)
async def normalize_log(
    log_file: UploadFile = File(...),
    label: Optional[str] = Form(None),
    store: bool = Form(False),
):
    """Upload a raw Salesforce debug log and get back its normalized JSON.
    This path is completely org-independent: no org connection, no Apex/Flow
    source, no metadata is required or used -- it is pure log condensation, so
    it works even for an org this app has never connected to. The raw log is
    read into memory, normalized, and discarded before the response returns;
    if `store` is true the derived JSON (never the raw log) is kept in the
    normalized-log library for future reference."""
    raw_bytes = await log_file.read()  # in memory only
    raw_text = raw_bytes.decode("utf-8", errors="replace")
    normalized = parse_log_text(raw_text)  # no index_ids -> no org needed
    del raw_bytes, raw_text  # the raw log does not outlive this request

    now = iso_now()
    result = {"normalized_log": normalized, "stored": False, "log_id": None, "meta": None}

    if store:
        timestamp_slug = now.replace(":", "").replace("-", "")
        base = label or (os.path.splitext(log_file.filename)[0] if log_file.filename else "log")
        log_id = f"{timestamp_slug}_{_slugify(base)}"
        n_exc = len(normalized.get("exceptions", []))
        meta = {
            "log_id": log_id,
            "label": label,
            "source_log": log_file.filename,
            "timestamp": now,
            "exception_count": n_exc,
            "top_exception": (normalized["exceptions"][0]["type"] if n_exc else None),
            "execution_unit_count": len(normalized.get("execution_units", [])),
            "involved_components": normalized.get("involved_components", []),
        }
        storage.save_normalized_log(log_id, normalized, meta)
        result.update({"stored": True, "log_id": log_id, "meta": meta})

    return result


@app.get("/api/logs", dependencies=Dep_reader)
def list_logs():
    return storage.list_normalized_logs()


@app.get("/api/logs/{log_id}", dependencies=Dep_reader)
def get_log(log_id: str):
    result = storage.load_normalized_log(log_id)
    if not result:
        raise HTTPException(404, f"No stored normalized log '{log_id}'.")
    return result


@app.get("/api/logs/{log_id}/download", dependencies=Dep_reader)
def download_log(log_id: str):
    result = storage.load_normalized_log(log_id)
    if not result:
        raise HTTPException(404, f"No stored normalized log '{log_id}'.")
    return JSONResponse(
        content=result["normalized_log"],
        headers={"Content-Disposition": f'attachment; filename="{log_id}.normalized.json"'},
    )


# ---------- LLM connection ----------
#
# Two layers, and the split is the whole point of this section.
#
# 1. The SHARED connection (app/llm_config.py) comes from the server's
#    environment and serves every signed-in user. Nothing to paste, nothing
#    to unlock, works immediately after a restart. It cannot be changed
#    through the API by anyone -- changing it means editing the server's
#    config and restarting, which is a privilege no session can be tricked
#    into exercising.
#
# 2. A PERSONAL key (app/secrets_store.py) is an ADMIN-ONLY override,
#    encrypted at rest under that admin's own password. Every route that
#    creates, unlocks or removes one now requires the `admin` role, which is
#    what "only admins can change the LLM connection" means in practice: an
#    ordinary user has no endpoint to call and no control to click.
#
# GET /api/chat/key stays open to any signed-in account, because a user
# whose chat is not working has to be able to find out why. It returns the
# effective state -- which connection is in use and whether chat is ready --
# and never any key material.

class StoreKeyRequest(BaseModel):
    api_key: str
    password: str
    provider: Optional[str] = None
    # Azure only: the full chat-completions URL including ?api-version=...
    # The deployment in its path is what selects the model, so there is no
    # separate model name to send.
    endpoint: Optional[str] = None


class UnlockRequest(BaseModel):
    password: str


class DefaultModelRequest(BaseModel):
    model: Optional[str] = None


@app.get("/api/chat/key", dependencies=Dep_reader)
def get_key_state(ident=Depends(auth.require_reader)):
    """Which connection this session will use, and whether chat is ready.

    Open to every role on purpose. The shape is `effective_state`, not the
    raw personal-key record: a user with no personal key -- which is now
    almost everyone -- can chat perfectly well on the shared connection, and
    a screen reading "not configured" beside a working chat box is worse than
    no screen at all."""
    return secrets_store.effective_state(ident)


@app.get("/api/llm", dependencies=Dep_reader)
def llm_connection(ident=Depends(auth.require_reader)):
    """The shared server connection on its own.

    Admins get the operational detail -- masked key hint, the Azure endpoint,
    and the names of the environment variables to edit -- because they are
    the people who will be asked to fix it. Everyone else gets provider,
    model and readiness, which is enough to understand their own situation
    and nothing that names internal infrastructure."""
    return llm_config.admin_state() if ident["role"] == "admin" else llm_config.public_state()


@app.post("/api/chat/key", dependencies=Dep_admin)
async def store_key(req: StoreKeyRequest, ident=Depends(auth.require_admin)):
    """Store a PERSONAL key for this admin. Admin-only -- see the section
    comment above.

    This is an override, not the way chat is provisioned. Ordinary users are
    served by the shared server connection and never reach this route; an
    admin uses it when they want their own turns billed to their own provider
    account.

    The password is re-asked here rather than reused from the session on
    purpose: it is the encryption material, and requiring it confirms the
    person at the keyboard is the account owner and not a walk-up on an
    unlocked browser."""
    if not auth.authenticate(ident["username"], req.password):
        raise HTTPException(403, "That password is incorrect.")
    api_key = (req.api_key or "").strip()
    if not api_key:
        raise HTTPException(400, "Paste an API key.")

    provider = (req.provider or secrets_store.DEFAULT_PROVIDER).strip().lower()
    endpoint = (req.endpoint or "").strip()
    if provider == llm.PROVIDER_AZURE:
        # Validate the URL shape before spending a call on it: the two usual
        # mistakes (resource root instead of the deployment path, missing
        # api-version) are recognisable without touching the network.
        try:
            endpoint = llm.validate_azure_endpoint(endpoint)
        except ValueError as e:
            raise HTTPException(400, str(e))

    # A live call, not just a format check -- on Azure this is also the only
    # way to prove the deployment name and api-version are right.
    try:
        await llm.verify_creds(llm.creds(provider, api_key, endpoint))
    except llm.LLMError as e:
        raise HTTPException(400, f"That did not work: {e}")
    except ValueError as e:
        raise HTTPException(400, str(e))

    try:
        secrets_store.store_key(ident["username"], req.password, api_key,
                                provider=provider, token_id=ident.get("token_id"),
                                endpoint=endpoint or None)
    except ValueError as e:
        raise HTTPException(400, str(e))

    # On Azure the deployment IS the model, so there is nothing for the user to
    # pick -- set it for them rather than leaving the picker empty.
    if provider == llm.PROVIDER_AZURE:
        secrets_store.set_default_model(ident["username"], llm.azure_deployment(endpoint))
    secrets_store.mark_verified(ident["username"])
    return secrets_store.effective_state(ident)


@app.delete("/api/chat/key", dependencies=Dep_admin)
def delete_key(ident=Depends(auth.require_admin)):
    """Drop this admin's personal key. Chat does not stop -- their sessions
    fall back to the shared server connection, which is the point of having
    it."""
    secrets_store.remove_key(ident["username"], ident.get("token_id"))
    return secrets_store.effective_state(ident)


@app.post("/api/chat/unlock", dependencies=Dep_admin)
def unlock_key(req: UnlockRequest, ident=Depends(auth.require_admin)):
    """Post-restart path for an admin's personal key. The wrapped key
    survived; the in-memory copy did not, so the password re-derives it.

    This is no longer on anyone's critical path. Before the shared
    connection existed, every user hit this after every restart or chat was
    dead; now a locked personal key just means the admin's turns run on the
    shared connection until they unlock it."""
    try:
        secrets_store.unlock(ident["username"], req.password, ident.get("token_id"))
    except secrets_store.KeyMissing as e:
        raise HTTPException(404, str(e))
    except secrets_store.BadPassword as e:
        raise HTTPException(403, str(e))
    return secrets_store.effective_state(ident)


@app.post("/api/chat/default-model", dependencies=Dep_reader)
def set_default_model(req: DefaultModelRequest, ident=Depends(auth.require_reader)):
    """Remembering which model to start new chats on.

    Choosing a model is not changing the connection, so it stays open to
    every role -- unless the operator set TS_LLM_LOCK_MODEL, which is there
    for a deployment that wants one approved model and no experimentation."""
    state = secrets_store.effective_state(ident)
    if state["model_locked"]:
        raise HTTPException(
            403, "The model is fixed by this server's configuration. Ask an administrator "
                 "if you need a different one.")
    secrets_store.set_default_model(ident["username"], req.model)
    return secrets_store.effective_state(ident)


@app.get("/api/chat/models", dependencies=Dep_reader)
async def get_models(ident=Depends(auth.require_reader), refresh: bool = False):
    """The model picker's source. Filtered to tool-capable models only --
    most free models on OpenRouter cannot call tools, and a chat using one
    silently never touches the org knowledgebase, it just improvises.

    Note that this now works for an ordinary user with no key of their own:
    `require_creds` resolves to the shared server connection, and the
    catalogue it returns is the shared connection's catalogue."""
    try:
        creds = secrets_store.require_creds(ident)
    except secrets_store.KeyLocked as e:
        raise HTTPException(409, str(e))
    except secrets_store.KeyMissing as e:
        raise HTTPException(404, str(e))
    try:
        models = await llm.list_models_for(creds, force=refresh)
    except llm.LLMError as e:
        raise HTTPException(502, str(e))
    if creds["provider"] == llm.PROVIDER_AZURE:
        note = ("This is the server's Azure deployment. The deployment decides the model, "
                "so changing it means changing the server's configuration.")
    else:
        note = "Only models that support tool calling are listed, best agentic score first."
    return {"models": models, "note": note, "provider": creds["provider"],
            "source": creds.get("source"), "locked": secrets_store.effective_state(ident)["model_locked"]}


# ---------- usage ----------
#
# One shared key means the provider's own dashboard can no longer say WHO
# spent what -- every call arrives from the same credential. Attribution
# happens in app/usage.py, at the point of use, and these two routes are how
# it is read back.


@app.get("/api/admin/usage", dependencies=Dep_admin)
def admin_usage(days: int = usage_ledger.DEFAULT_DAYS, username: Optional[str] = None):
    """The whole usage picture for a window: totals, per user, per day, per
    org, per model. Optionally narrowed to one account for a drilldown.

    Returned as one payload rather than four endpoints because the screen
    shows all of it at once -- and four requests over the same day files
    could straddle a midnight rollover and disagree with each other."""
    return usage_ledger.report(days=days, username=username)


@app.get("/api/usage/me", dependencies=Dep_reader)
def my_usage(days: int = usage_ledger.DEFAULT_DAYS, ident=Depends(auth.require_reader)):
    """Your own consumption. Not admin-gated: on a shared key, "is it me
    burning the budget?" is a fair question to be able to answer without
    asking an admin to look it up."""
    summary = usage_ledger.my_summary(ident["username"], days=days)
    # Their own quota position travels with their own usage, so the screen
    # that answers "what have I spent" also answers "how much is left" --
    # which is the question actually being asked.
    summary["quota"] = limits_policy.quota_status(ident["username"],
                                                  auth.get_user(ident["username"]))
    return summary


# ---------- chat ----------

class NewChatRequest(BaseModel):
    org_id: Optional[str] = None
    title: Optional[str] = None
    model: Optional[str] = None


class SendMessageRequest(BaseModel):
    content: str
    org_id: Optional[str] = None
    model: Optional[str] = None
    confirm_tool_ids: Optional[list] = None


class ShareRequest(BaseModel):
    include_tools: bool = False


@app.get("/api/chats", dependencies=Dep_reader)
def list_chats(ident=Depends(auth.require_reader)):
    return chat_store.list_chats(ident["username"])


def _resolve_model(ident, requested, stored, required=True):
    """Which model this turn actually runs on.

    Two bugs live here if it is done inline, and both did.

    First, the fallback must come from `effective_state`, not from the
    caller's personal key record. A user with no personal key -- now the
    normal case -- has no `default_model` of their own, so reading the
    personal record meant the server's own TS_LLM_DEFAULT_MODEL never reached
    a turn and the request failed with "Pick a model first". The browser
    masked it by always sending a model explicitly; an API or MCP client got
    a flat 400.

    Second, TS_LLM_LOCK_MODEL has to be enforced *here*. Refusing the
    "remember my model" route alone is theatre: a client that puts `model` in
    the turn body, or in the body of `POST /api/chats`, bypasses the pin
    entirely and spends the shared key on whatever it likes. When the model
    is locked the request's own value is ignored outright rather than
    rejected, so an older client that keeps sending one still works -- it
    just does not get to choose.

    `required=False` for creating a conversation: the UI's flow is to open a
    chat and then pick a model, so a null there is legitimate. A turn, by
    contrast, cannot run without one.
    """
    state = secrets_store.effective_state(ident)
    if state["model_locked"]:
        if not state["default_model"]:
            raise HTTPException(
                500, "This server pins the model (TS_LLM_LOCK_MODEL) but has not said which "
                     "one (TS_LLM_DEFAULT_MODEL is unset). An administrator needs to set it.")
        return state["default_model"]
    model = requested or stored or state["default_model"]
    if not model and required:
        raise HTTPException(400, "Pick a model first.")
    return model


@app.post("/api/chats", dependencies=Dep_user)
def create_chat(req: NewChatRequest, ident=Depends(auth.require_user)):
    if req.org_id:
        org_access.assert_can_view(req.org_id, ident)
    # Resolved rather than taken verbatim, so a pinned model cannot be
    # sidestepped by naming a different one at creation time.
    return chat_store.create_chat(ident["username"], org_id=req.org_id,
                                  title=req.title,
                                  model=_resolve_model(ident, req.model, None, required=False))


@app.get("/api/chats/{chat_id}", dependencies=Dep_reader)
def get_chat(chat_id: str, ident=Depends(auth.require_reader)):
    meta = chat_store.load_meta(ident["username"], chat_id)
    if not meta:
        raise HTTPException(404, "No such conversation.")
    return {"meta": meta, "messages": chat_store.load_messages(ident["username"], chat_id)}


@app.delete("/api/chats/{chat_id}", dependencies=Dep_reader)
def delete_chat(chat_id: str, ident=Depends(auth.require_reader)):
    if not chat_store.delete_chat(ident["username"], chat_id):
        raise HTTPException(404, "No such conversation.")
    return {"ok": True}


@app.post("/api/chats/{chat_id}/messages", dependencies=Dep_user)
async def send_message(chat_id: str, req: SendMessageRequest, request: Request,
                       ident=Depends(auth.require_user)):
    """Run one turn, streamed as Server-Sent Events.

    Note the manual visibility check. The generic `_org_view_dep` gate reads
    `org_id` out of the PATH, and this route carries it in the body, so the
    gate would never fire -- it has to be called explicitly or an org the user
    cannot see would be reachable through chat.
    """
    meta = chat_store.load_meta(ident["username"], chat_id)
    if not meta:
        raise HTTPException(404, "No such conversation.")
    if not (req.content or "").strip():
        raise HTTPException(400, "Type a question first.")

    org_id = req.org_id or meta.get("org_id")
    org_label = None
    if org_id:
        entry = org_access.assert_can_view(org_id, ident)
        org_label = entry.get("name")

    model = _resolve_model(ident, req.model, meta.get("model"))

    # The MCP tools loop back into this app's HTTP API and need a real Bearer
    # token to do it. The caller's own session token is exactly right: same
    # user, same role, same org visibility, nothing new to mint or expire.
    ident = dict(ident)
    ident["_raw_token"] = auth.raw_token_from_request(request)

    stream = chat_agent.run_turn(ident, chat_id, req.content.strip(), org_id, org_label,
                                 model, confirmed_tool_ids=req.confirm_tool_ids)
    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            # nginx buffers proxied responses by default, which would hold the
            # whole turn back and deliver it in one lump at the end.
            "X-Accel-Buffering": "no",
        },
    )


# ---------- shared transcripts ----------

@app.post("/api/chats/{chat_id}/share", dependencies=Dep_reader)
def share_chat(chat_id: str, req: ShareRequest, ident=Depends(auth.require_reader)):
    """Mint (or re-fetch) a public link to a transcript.

    `include_tools` defaults to false and should stay false for anything
    leaving Conga: tool results carry org internals -- component cards, field
    maps, incident packs -- and an anonymous viewer has no org visibility to
    evaluate them against."""
    result = chat_store.create_share(ident["username"], chat_id,
                                     include_tools=req.include_tools)
    if not result:
        raise HTTPException(404, "No such conversation.")
    return {"token": result["token"], "url": f"/shared/{result['token']}",
            "include_tools": result["include_tools"]}


@app.delete("/api/chats/{chat_id}/share", dependencies=Dep_reader)
def unshare_chat(chat_id: str, ident=Depends(auth.require_reader)):
    meta = chat_store.load_meta(ident["username"], chat_id)
    if not meta:
        raise HTTPException(404, "No such conversation.")
    if meta.get("share_token"):
        chat_store.revoke_share(meta["share_token"])
    return {"ok": True}


@app.get("/api/shared/{token}")
def read_shared(token: str):
    """Public, unauthenticated, read-only. A revoked token is indistinguishable
    from one that never existed."""
    result = chat_store.resolve_share(token)
    if not result:
        raise HTTPException(404, "This shared conversation is not available.")
    return result


@app.get("/shared/{token}")
def shared_page(token: str):
    return FileResponse(os.path.join(STATIC_DIR, "shared.html"))


# ---------- frontend ----------

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))

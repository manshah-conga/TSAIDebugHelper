"""
TS Intelligent Debug Helper -- web app.

Endpoints group into three areas: org connections (create/list/refresh,
backed by a background fetch from Salesforce), lookups against an org's
knowledgebase (stats, field-writers, object-touch, component detail), and
incidents (file one from an uploaded log and/or a suspect field, list,
detail, resolve).

Run with:  uvicorn app.main:app --reload --port 8000
"""
import os
from typing import Optional

from fastapi import FastAPI, BackgroundTasks, UploadFile, File, Form, HTTPException, Depends, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import storage
from . import auth
from .onboarding import run_onboarding, JOBS
from .log_normalizer import parse_log_text
from .rca import assemble_context, lookup_field_writers
from .incidents import file_incident
from .common_now import iso_now

app = FastAPI(title="TS Intelligent Debug Helper")

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")

# Role dependencies (see app/auth.py). Reads require reader+, write actions
# require user+, user/token administration requires admin.
Dep_reader = [Depends(auth.require_reader)]
Dep_user = [Depends(auth.require_user)]
Dep_admin = [Depends(auth.require_admin)]


@app.on_event("startup")
def _startup():
    auth.bootstrap_admin()


# ---------- authentication ----------

class LoginRequest(BaseModel):
    username: str
    password: str


@app.post("/api/auth/login")
def login(req: LoginRequest, response: Response):
    ident = auth.authenticate(req.username, req.password)
    if not ident:
        raise HTTPException(401, "Invalid username or password (or the account is disabled).")
    _tid, raw = auth.create_token(ident["username"], ident["role"], kind="session",
                                  label="web login", ttl_days=auth.SESSION_TTL_DAYS)
    response.set_cookie(
        auth.SESSION_COOKIE, raw, httponly=True, samesite="lax",
        max_age=auth.SESSION_TTL_DAYS * 86400,
    )
    return {"username": ident["username"], "role": ident["role"]}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response):
    cookie = request.cookies.get(auth.SESSION_COOKIE)
    if cookie:
        ident = auth.verify_token(cookie)
        if ident and ident.get("token_id"):
            auth.revoke_token(ident["token_id"])
    response.delete_cookie(auth.SESSION_COOKIE)
    return {"ok": True}


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


@app.get("/api/admin/users", dependencies=Dep_admin)
def admin_list_users():
    return auth.list_users()


@app.post("/api/admin/users", dependencies=Dep_admin)
def admin_create_user(req: NewUserRequest):
    try:
        return auth.create_user(req.username, req.password, req.role)
    except ValueError as e:
        raise HTTPException(400, str(e))


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
    try:
        auth.reset_password(username, req.password)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"username": username, "ok": True}


@app.delete("/api/admin/users/{username}", dependencies=Dep_admin)
def admin_delete_user(username: str, request: Request):
    ident = auth.current_identity(request)
    if username == ident["username"]:
        raise HTTPException(400, "You cannot delete your own account.")
    try:
        auth.delete_user(username)
    except ValueError as e:
        raise HTTPException(400, str(e))
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
    return {"ok": True}


# ---------- org connections ----------

class NewOrgRequest(BaseModel):
    org_id: str
    org_name: str
    instance_url: str
    access_token: str


@app.post("/api/orgs", dependencies=Dep_user)
def create_org(req: NewOrgRequest, background_tasks: BackgroundTasks):
    existing = storage.read_json(os.path.join(storage.kb_dir(req.org_id), "file_hashes.json"), {})
    JOBS[req.org_id] = {"status": "queued", "detail": "", "warnings": []}
    background_tasks.add_task(
        run_onboarding, req.org_id, req.org_name, req.instance_url, req.access_token, existing,
    )
    return {"org_id": req.org_id, "status": "queued"}


@app.get("/api/orgs", dependencies=Dep_reader)
def list_orgs():
    return storage.load_registry()


@app.get("/api/orgs/{org_id}/status", dependencies=Dep_reader)
def org_status(org_id: str):
    return JOBS.get(org_id, {"status": "unknown", "detail": "", "warnings": []})


@app.post("/api/orgs/{org_id}/refresh", dependencies=Dep_user)
def refresh_org(org_id: str, background_tasks: BackgroundTasks):
    registry = storage.load_registry()
    if org_id not in registry:
        raise HTTPException(404, f"Org '{org_id}' not found. Create it first with POST /api/orgs.")
    raise HTTPException(
        400,
        "Refresh needs a fresh Access Token (tokens expire) -- POST /api/orgs again with the "
        "same org_id, org_name, instance_url and a current access_token; changed-vs-unchanged "
        "is computed automatically from each component's content hash.",
    )


# ---------- knowledgebase lookups ----------

@app.get("/api/orgs/{org_id}/stats", dependencies=Dep_reader)
def get_stats(org_id: str):
    kb = storage.load_kb(org_id)
    if not kb["org_index"]:
        raise HTTPException(404, f"No knowledgebase for org '{org_id}' yet.")
    return kb["org_stats"]


@app.get("/api/orgs/{org_id}/components/{component_id}", dependencies=Dep_reader)
def get_component(org_id: str, component_id: str):
    kb = storage.load_kb(org_id)
    card = kb["org_index"].get(component_id)
    if not card:
        raise HTTPException(404, f"No component '{component_id}' in org '{org_id}'.")
    return card


@app.get("/api/orgs/{org_id}/object-touch/{object_name}", dependencies=Dep_reader)
def get_object_touch(org_id: str, object_name: str):
    kb = storage.load_kb(org_id)
    return kb["object_touch_map"].get(object_name, {})


@app.get("/api/orgs/{org_id}/field-writers/{field_name}", dependencies=Dep_reader)
def get_field_writers(org_id: str, field_name: str):
    kb = storage.load_kb(org_id)
    return lookup_field_writers(field_name, kb["org_index"], kb["field_touch_map"], kb["file_hashes"])


@app.get("/api/orgs/{org_id}/search", dependencies=Dep_reader)
def search_components(org_id: str, q: str):
    """Freeform lookup used by the 'ask a question' box in the UI: matches
    the query against component ids, objects touched, and field names, so
    a person (or an AI over MCP) can start from a vague description
    instead of an exact identifier."""
    kb = storage.load_kb(org_id)
    q_lower = q.lower()
    matches = {
        "components": [cid for cid in kb["org_index"] if q_lower in cid.lower()][:25],
        "objects": [o for o in kb["object_touch_map"] if q_lower in o.lower()][:25],
        "fields": [f for f in kb["field_touch_map"] if q_lower in f.lower()][:25],
    }
    return matches


# ---------- incidents ----------

@app.post("/api/orgs/{org_id}/incidents", dependencies=Dep_user)
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

    known = storage.load_known_issues(org_id)
    now = iso_now()
    timestamp_slug = now.replace(":", "").replace("-", "")
    default_label = (os.path.splitext(log_filename)[0] if log_filename else field) or "incident"
    incident_id = f"{timestamp_slug}_{label or default_label}"

    signature, recurrence, prior, sig_source = file_incident(known, incident_id, normalized, field)
    if signature:
        storage.save_known_issues(org_id, known)

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


@app.get("/api/orgs/{org_id}/incidents", dependencies=Dep_reader)
def get_incidents(org_id: str):
    return storage.list_incidents(org_id)


@app.get("/api/orgs/{org_id}/incidents/{incident_id}", dependencies=Dep_reader)
def get_incident(org_id: str, incident_id: str):
    result = storage.load_incident(org_id, incident_id)
    if not result:
        raise HTTPException(404, f"No incident '{incident_id}' for org '{org_id}'.")
    return result


class ResolveRequest(BaseModel):
    signature: str
    resolution: str


@app.post("/api/orgs/{org_id}/resolve", dependencies=Dep_user)
def resolve_incident(org_id: str, req: ResolveRequest):
    known = storage.load_known_issues(org_id)
    if req.signature not in known:
        raise HTTPException(404, f"No known issue with signature '{req.signature}' for org '{org_id}'.")
    known[req.signature]["resolution"] = req.resolution
    known[req.signature]["resolution_recorded_at"] = iso_now()
    storage.save_known_issues(org_id, known)
    return known[req.signature]


@app.get("/api/orgs/{org_id}/known-issues", dependencies=Dep_reader)
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


# ---------- frontend ----------

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))

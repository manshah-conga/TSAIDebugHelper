"""
TS Intelligent Debug Helper -- MCP server (stdio + remote streamable HTTP).

This does NOT talk to Salesforce or the filesystem directly. It is a thin
proxy over the running FastAPI web app's HTTP API (see app/main.py), so an
AI agent (Claude Desktop, Cowork, Claude Code, Copilot Studio, etc.) can
drive org onboarding, knowledgebase lookups, and incident filing/resolution
through the exact same code path -- and the exact same "only normalized JSON
is ever persisted" guarantee -- as the web UI.

There are two ways to reach these tools, and they share every tool
definition below.

1. REMOTE (preferred; nothing to install on the client)
   The web app mounts this same FastMCP instance at POST /mcp as a
   Streamable HTTP endpoint -- see app/mcp_http.py. A client just needs the
   URL and an API token sent as a request header:

       https://<host>/mcp     header:  Authorization: Bearer <api token>

   The token travels per request, so one endpoint serves many people, each
   acting as themselves with their own role and org visibility. Nothing is
   read from the environment in this mode.

2. LOCAL stdio (legacy; needs Python on each client machine)
   Run this script as a subprocess of the MCP client. Because there is no
   request to carry a header, the token comes from the environment instead:
    {
      "mcpServers": {
        "ts-debug-helper": {
          "command": "python3",
          "args": ["/absolute/path/to/webapp/mcp_server.py"],
          "env": {
            "TS_DEBUG_HELPER_URL": "http://127.0.0.1:8000",
            "TS_DEBUG_HELPER_TOKEN": "<paste an API token from the web UI>"
          }
        }
      }
    }

Either way the token inherits the role of the user who created it -- a
reader token can only call the read tools, a user/admin token can also
create org connections and file incidents.

Every tool here is a network call to the web app, not a direct file read --
if the web app is not running, every tool will return an error saying so.
"""
import os
from contextvars import ContextVar
from typing import Optional

import httpx
from mcp.server.fastmcp import FastMCP

# Importing the package loads webapp/.env (see app/env_file.py), so running
# this as a standalone stdio server picks up the same TS_DEBUG_HELPER_URL /
# TS_DEBUG_HELPER_TOKEN an operator put in the file. It has to happen before
# the two reads below, which are import-time. The load is silent by design:
# stdio MCP speaks JSON-RPC on stdout, and a banner printed there corrupts
# the protocol.
try:
    import app as _app                # noqa: F401 - imported for its side effect
except Exception:                     # noqa: BLE001 - never block the server
    pass

BASE_URL = os.environ.get("TS_DEBUG_HELPER_URL", "http://127.0.0.1:8000").rstrip("/")
API_TOKEN = os.environ.get("TS_DEBUG_HELPER_TOKEN", "").strip()

# Set per request by app/mcp_http.py from the caller's Authorization header
# when these tools are served over Streamable HTTP. Empty in stdio mode, where
# API_TOKEN from the environment is used instead. A ContextVar (not a global)
# so concurrent callers on the shared remote endpoint never see each other's
# token: each request runs in its own context.
CURRENT_TOKEN: ContextVar[str] = ContextVar("ts_api_token", default="")

# stateless_http: every request is self-contained, which is what lets the
# per-request token above be the whole of the auth story -- there is no
# server-side session holding an identity between calls. It also keeps the
# endpoint working behind load balancers and reverse proxies that do not
# pin a client to one worker.
mcp = FastMCP("ts-debug-helper", stateless_http=True)


def _token() -> str:
    """The API token for the call in flight: the caller's request header when
    served over HTTP, else the environment variable in stdio mode."""
    return CURRENT_TOKEN.get() or API_TOKEN


def _auth_headers():
    tok = _token()
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=BASE_URL, timeout=30.0, headers=_auth_headers())


def _auth_error(status: int):
    if status == 401:
        return {"error": "Authentication failed (401). The API token is missing, expired, or "
                         "revoked. Over HTTP, send a valid token as 'Authorization: Bearer "
                         "<token>'; in stdio mode, set TS_DEBUG_HELPER_TOKEN in the MCP server "
                         "env. Tokens are created in the web UI under 'API Tokens'."}
    if status == 403:
        return {"error": "Not permitted (403). Your API token's role is too low for this action -- "
                         "e.g. connecting an org or filing an incident needs a 'user' or 'admin' token, "
                         "not a 'reader' token."}
    return None


_UNREACHABLE = None  # set lazily below


def _conn_error():
    return {"error": f"Cannot reach the TS Intelligent Debug Helper web app at {BASE_URL}. "
                     f"Is it running (uvicorn app.main:app --port 8000)?"}


async def _get(path: str, params: Optional[dict] = None):
    async with _client() as c:
        try:
            r = await c.get(path, params=params)
        except httpx.ConnectError:
            return _conn_error()
        if r.status_code >= 400:
            return _auth_error(r.status_code) or {"error": f"{r.status_code}: {r.text}"}
        return r.json()


async def _post_json(path: str, body: dict):
    async with _client() as c:
        try:
            r = await c.post(path, json=body)
        except httpx.ConnectError:
            return _conn_error()
        if r.status_code >= 400:
            return _auth_error(r.status_code) or {"error": f"{r.status_code}: {r.text}"}
        return r.json()


async def _post_form(path: str, data: dict, files: Optional[dict] = None):
    async with _client() as c:
        try:
            r = await c.post(path, data=data, files=files or {})
        except httpx.ConnectError:
            return _conn_error()
        if r.status_code >= 400:
            return _auth_error(r.status_code) or {"error": f"{r.status_code}: {r.text}"}
        return r.json()


# ---------- org connections ----------

@mcp.tool()
async def create_org_connection(org_id: str, org_name: str, instance_url: str,
                                access_token: str, visibility: str = "private") -> dict:
    """Connect a new Salesforce org: fetch its Apex classes/triggers, flows,
    LWC components and custom objects via the Tooling API, then build and
    store the derived knowledgebase for it. Only the derived JSON is ever
    stored -- the access token and raw fetched source are never persisted.
    This queues background work; poll get_org_connection_status(org_id) or
    call list_orgs() until it reports status 'done'.

    `visibility` controls who else can see the org in this app:
      "private" (default) -- only the account that owns the API token you are
                             using, plus admins.
      "public"            -- every signed-in account.
    Either way, only the owner or an admin can re-connect/refresh the org or
    change its visibility later (see set_org_visibility). Re-connecting an
    org you already own keeps its current visibility unless you pass a new
    one explicitly."""
    return await _post_json("/api/orgs", {
        "org_id": org_id, "org_name": org_name,
        "instance_url": instance_url, "access_token": access_token,
        "visibility": visibility,
    })


@mcp.tool()
async def set_org_visibility(org_id: str, visibility: str) -> dict:
    """Make a connected org "public" (visible to every signed-in account) or
    "private" (visible only to its owner and admins). Only the org's owner or
    an admin can do this."""
    async with _client() as c:
        try:
            r = await c.patch(f"/api/orgs/{org_id}/visibility", json={"visibility": visibility})
        except httpx.ConnectError:
            return _conn_error()
        if r.status_code >= 400:
            return _auth_error(r.status_code) or {"error": f"{r.status_code}: {r.text}"}
        return r.json()


@mcp.tool()
async def refresh_org(org_id: str, access_token: str) -> dict:
    """Re-fetch an org that is already connected, to pick up customization
    changes. Only a fresh Salesforce access token is needed -- the org's name,
    instance URL, owner and visibility all come from what is already on record,
    and each component's content hash decides what counts as changed. Owner or
    admin only. Queues background work: poll get_org_connection_status(org_id)
    until it reports 'done', and its `changes` block then tells you how many
    components changed / were added / were removed since the last fetch."""
    return await _post_json(f"/api/orgs/{org_id}/refresh", {"access_token": access_token})


@mcp.tool()
async def get_org_connection_status(org_id: str) -> dict:
    """Check the progress of an org connection/fetch that was queued via
    create_org_connection. Status values: queued, connecting,
    fetching_objects, fetching_classes, fetching_triggers, fetching_flows,
    fetching_lwc, fetching_workflow, extracting, indexing, saving, done, error."""
    return await _get(f"/api/orgs/{org_id}/status")


@mcp.tool()
async def list_orgs() -> dict:
    """List the orgs your API token's account is allowed to see -- every
    public org, plus your own private ones (plus everyone's, for an admin
    token) -- with component counts, `owner`, `visibility`, `can_manage` and
    the last-refreshed timestamp for each. Use this to discover valid org_id
    values for the other tools. An org that does not appear here will report
    "no such org" from every other tool, whether it is private to someone
    else or genuinely absent."""
    return await _get("/api/orgs")


# ---------- knowledgebase lookups ----------

@mcp.tool()
async def get_org_stats(org_id: str) -> dict:
    """Get org-wide customization stats: counts of classes/triggers/flows/
    LWC, async job classes (batch/queueable/schedulable/future), classes
    with external callouts, flows missing a fault path, and org-wide risk
    rollups (never-cleared static collections, fields with a high-risk
    writer). Good first call when starting an investigation on an org."""
    return await _get(f"/api/orgs/{org_id}/stats")


@mcp.tool()
async def get_component(org_id: str, component_id: str) -> dict:
    """Get the full stored knowledgebase card for one component (an Apex
    class/trigger, a flow, or an LWC bundle) -- its structure, objects/
    fields touched, calls made, and (for Apex) any detected static mutable
    state or risky field writes. Use search_knowledgebase or
    find_field_writers first if you don't already know the exact id."""
    return await _get(f"/api/orgs/{org_id}/components/{component_id}")


@mcp.tool()
async def get_object_touch(org_id: str, object_name: str) -> dict:
    """List every component that reads or writes a given Salesforce object
    (standard or custom, e.g. 'Quote' or 'Apttus_Config2__LineItem__c'),
    useful for scoping which customizations could plausibly be involved in
    an incident on that object."""
    return await _get(f"/api/orgs/{org_id}/object-touch/{object_name}")


@mcp.tool()
async def find_field_writers(org_id: str, field_api_name: str) -> dict:
    """Find every writer of a specific field (e.g. 'Increment_Adjustment__c')
    across ALL update mechanisms -- not just Apex. Each writer carries a
    `mechanism` ('Apex', 'Flow', 'Process Builder', or 'Workflow/Approval
    field update'), a risk level (high/medium/low for Apex from static
    analysis; 'declarative' for Flow/PB/Workflow), the target object, and a
    plain-language reason. This is the key tool for 'field X had the wrong
    value but there was no exception' reports, since it works from the static
    knowledgebase alone with no debug log needed.

    Coverage notes so you don't over-claim: Apex writers come from parsing
    class/trigger source. Flow/Process Builder writers come from
    recordUpdate/recordCreate/assignment elements in the flow metadata (a
    declarative writer may carry a `confidence` of 'medium' when it writes via
    a record variable rather than $Record directly). Workflow and Approval
    field updates both come from WorkflowFieldUpdate metadata. If the field is
    a formula or roll-up summary it is not 'written' by anything -- its value
    derives from other data. When reporting, group writers by mechanism and
    note that active-vs-inactive state of a flow/rule is not captured here, so
    confirm the writer is active before concluding it caused a given change."""
    return await _get(f"/api/orgs/{org_id}/field-writers/{field_api_name}")


@mcp.tool()
async def get_inbound_references(org_id: str, component_id: str) -> dict:
    """Reverse-call lookup (schema v3): everything in the org that INVOKES a
    given class/flow -- flows via actionCall (with the resolved Apex method),
    classes via method call, flows via subflow. Answers 'what else calls
    this?' without scanning every card. The complement of get_component's
    outbound `calls_to`."""
    return await _get(f"/api/orgs/{org_id}/inbound/{component_id}")


@mcp.tool()
async def get_entry_points(org_id: str, object_name: str) -> dict:
    """Per-object automation entry points (schema v3): the before/after-save
    flows, apex triggers, process builder, and workflow/approval field
    updates that fire when a record of this object is saved, plus any
    self_referential_automation (after-save automation that updates its own
    object -- the recursion pattern behind loop-guard bugs). The closest the
    knowledgebase gets to 'what runs when this object is saved'."""
    return await _get(f"/api/orgs/{org_id}/entry-points/{object_name}")


@mcp.tool()
async def search_knowledgebase(org_id: str, query: str, customer_authored_only: bool = True) -> dict:
    """Freeform search over an org's knowledgebase: matches the query
    (case-insensitive substring) against component ids, object names, and
    field names. Use this when you have a vague description instead of an
    exact identifier -- e.g. searching 'pricing' or 'adjustment'. Defaults to
    customer-authored components only (managed-package internals are usually
    noise); pass customer_authored_only=false to include managed results."""
    return await _get(f"/api/orgs/{org_id}/search",
                      params={"q": query, "customer_authored_only": str(customer_authored_only).lower()})


# ---------- incidents ----------

@mcp.tool()
async def file_incident(
    org_id: str,
    label: Optional[str] = None,
    suspect_field: Optional[str] = None,
    log_text: Optional[str] = None,
) -> dict:
    """File a new incident against an org's knowledgebase and get back an
    RCA context pack (relevant components, object/field touch info,
    recently-changed components, and -- if suspect_field is given --
    ranked field writers) plus whether this matches a previously-seen
    signature (recurrence) and any resolution already on file for it.
    Provide log_text (the raw contents of a Salesforce debug log) and/or
    suspect_field (a custom field API name reported as wrong with no
    exception). Only the normalized/derived form of the log is ever
    stored -- log_text itself is not persisted to disk."""
    if not suspect_field and not log_text:
        return {"error": "Provide log_text, suspect_field, or both."}
    data = {}
    if label:
        data["label"] = label
    if suspect_field:
        data["field"] = suspect_field
    files = None
    if log_text:
        files = {"log_file": ("incident.log", log_text.encode("utf-8"), "text/plain")}
    return await _post_form(f"/api/orgs/{org_id}/incidents", data, files)


@mcp.tool()
async def list_incidents(org_id: str) -> dict:
    """List all incidents filed so far for an org, newest metadata only
    (timestamp, incident id, recurrence flag, suspect field). Use
    get_incident for the full RCA context pack of one incident."""
    return await _get(f"/api/orgs/{org_id}/incidents")


@mcp.tool()
async def get_incident(org_id: str, incident_id: str) -> dict:
    """Get the full stored record for one incident: its normalized log
    (if any), the assembled RCA context pack, and its filing metadata
    (signature, recurrence, prior occurrences/resolution)."""
    return await _get(f"/api/orgs/{org_id}/incidents/{incident_id}")


@mcp.tool()
async def record_resolution(org_id: str, signature: str, resolution: str) -> dict:
    """Record the root cause and/or fix for a known-issue signature (as
    returned in an incident's metadata) so future recurrences of the same
    signature immediately surface this resolution instead of requiring a
    fresh investigation."""
    return await _post_json(f"/api/orgs/{org_id}/resolve", {"signature": signature, "resolution": resolution})


@mcp.tool()
async def list_known_issues(org_id: str) -> dict:
    """List every known-issue signature recorded for an org so far, each
    with its occurrence count, first/last-seen incident ids, and recorded
    resolution (if any)."""
    return await _get(f"/api/orgs/{org_id}/known-issues")


# ---------- standalone log normalization + log-only RCA ----------

@mcp.tool()
async def normalize_log(log_text: str, label: Optional[str] = None, store: bool = False) -> dict:
    """Normalize a raw Salesforce debug log with NO org, code, or metadata
    required. Pass the raw log contents as `log_text`; get back the compact
    normalized JSON -- execution units (with nesting depth and which threw),
    deduplicated exceptions with type/message/stack, collapsed SOQL and DML
    summaries, callouts, flow events, validation failures, final governor
    limits, and any component names the log itself mentions. The raw log is
    processed in memory and never stored; if `store` is true the derived
    JSON (never the raw log) is kept in the library and a `log_id` is
    returned.

    After calling this, analyze the returned normalized log to produce an RCA
    and a suggested resolution FROM THE LOG ALONE -- you do not have (and do
    not need) the affected org's Apex/Flow source or metadata. Base the RCA on
    what the log shows: the exception type and message, the failing frame at
    the top of the stack, which execution unit / trigger was active, the
    governor-limit usage (look for a limit at or near its max), the SOQL/DML
    volumes (signs of queries or DML inside a loop), and the ordering of
    events. State clearly which parts are evidenced by the log versus
    inferred, and if confirming the root cause would require the org's code,
    say specifically what to look at (e.g. 'inspect method X named in the top
    stack frame')."""
    files = {"log_file": ("incident.log", log_text.encode("utf-8"), "text/plain")}
    data = {"store": "true" if store else "false"}
    if label:
        data["label"] = label
    return await _post_form("/api/logs/normalize", data, files)


@mcp.tool()
async def list_normalized_logs() -> dict:
    """List every standalone normalized log kept in the library (org-
    independent), newest first, with each log's id, label, top exception
    type, exception count, and the component names it mentions. Use
    get_normalized_log(log_id) to pull the full normalized JSON for one."""
    return await _get("/api/logs")


@mcp.tool()
async def get_normalized_log(log_id: str) -> dict:
    """Get the full normalized JSON for one stored log (plus its metadata).
    Use this to analyze a previously-normalized log and produce an RCA and
    suggested resolution from the log alone -- see normalize_log for what the
    log-only analysis should cover and how to caveat it."""
    return await _get(f"/api/logs/{log_id}")


if __name__ == "__main__":
    mcp.run(transport="stdio")

# TS Intelligent Debug Helper -- Web App

A local web app that fetches a Salesforce org's customizations (Apex classes/triggers,
flows, LWC components, custom objects) straight from the org via the Tooling API,
builds a compact knowledgebase from them, and uses that knowledgebase to run RCA
against future incidents -- either an uploaded debug log, a "this field came out
wrong and there was no exception" report, or both. Every org gets its own
knowledgebase; every incident is kept for future recurrence matching.

**Hard rule this app is built around: only derived/normalized JSON is ever written
to disk.** Raw Apex/Flow/LWC source fetched from Salesforce, the Salesforce access
token, and raw uploaded debug logs are held in memory only for the duration of the
request that needs them, then discarded. See "Data storage guarantee" below.

## 1. Running it locally

```bash
cd webapp
pip install -r requirements.txt
python -m uvicorn app.main:app --port 8000
```

Use `python -m uvicorn ...` rather than bare `uvicorn ...` -- on Windows, pip's
`Scripts` folder (where the standalone `uvicorn.exe` lands) is often not on `PATH`,
which produces `'uvicorn' is not recognized as an internal or external command`.
Running it as a module through `python` sidesteps that, since it only needs `python`
itself on PATH. If `python` isn't recognized either, use the Windows launcher
instead: `py -m pip install -r requirements.txt` then `py -m uvicorn app.main:app --port 8000`.

Then open `http://127.0.0.1:8000` in a browser. That's the whole app -- one process,
one port, no database to stand up. Data is written under `webapp/data/` as flat JSON
files (created automatically on first use).

## 1a. Signing in, roles, and users

The app now requires a login. On first startup with no accounts, an initial **admin**
is created automatically -- the password is taken from the `TS_ADMIN_PASSWORD`
environment variable if you set one before starting, otherwise a random password is
generated and printed to the server console **once**. Watch the terminal for a block
like:

```
[TS Debug Helper] Created initial admin account.
    username: admin
    password: q7Xr...   (randomly generated -- log in and change it)
```

Sign in at `http://127.0.0.1:8000` with that account, then create the users you need
from the **Admin** tab.

There are three roles:

- **reader** -- read-only. View orgs, stats, field-writers, incidents, search, and
  stored normalized logs. Cannot connect orgs, file incidents, record resolutions,
  or normalize/store logs.
- **user** -- everything a reader can do, plus all write actions (connect orgs, file
  incidents, record resolutions, normalize and store logs).
- **admin** -- everything, plus user management (create users, change roles,
  enable/disable, reset passwords, delete) and visibility of every API token.

Only an admin sees the **Admin** tab. Role checks are enforced on the server, not
just hidden in the UI -- a reader's browser (or a reader-role API token) gets a 403
on any write, regardless of what the UI shows.

Passwords are stored only as salted PBKDF2-SHA256 hashes (stdlib, no extra
dependency); the cleartext is never written to disk.

This is meant to run on your own machine or a trusted internal host. There is no
transport encryption built in -- if you expose it beyond localhost, put it behind a
reverse proxy that terminates HTTPS.

## 2. Connecting an org

On the **Connections** tab, you need:

- **Org ID**: a short slug you choose (e.g. `acme_prod`) -- used in every API/MCP
  call afterwards.
- **Org Name**: a display label.
- **Instance URL**: e.g. `https://yourorg.my.salesforce.com`.
- **Access Token**: a valid Salesforce session/access token for that org.

This app does not implement an OAuth login flow -- you obtain the token yourself,
for example:

- **Workbench** (workbench.developerforce.com): log in to your org through it, then
  copy the session ID it's using and the instance URL shown in its address bar.
- **A Connected App** you already have set up for JWT bearer or client-credentials
  flow: exchange your own client id/secret/certificate for a token however you
  normally do, then paste the resulting `access_token` and `instance_url` here.
- Any existing authenticated session/tool that can hand you a bearer token for the
  REST/Tooling API.

The token is sent once, used to fetch metadata, and is never written to disk (see
below). Tokens expire -- to refresh an org's knowledgebase, POST to `/api/orgs`
again with the same `org_id` and a current token; each component's content hash
determines what actually changed, so a refresh is cheap even for a large org.

Fetch progress is polled from the UI automatically; component counts and last-
refreshed time show up on the Connections tab once it's done.

## 3. Working an incident

On the **Org Dashboard** tab:
- **Org stats** gives an at-a-glance summary: async job counts, integration points,
  flows missing a fault path, and the org-wide risk rollups -- which static
  collections are never cleared, and which custom fields have a high-risk writer.
- **Search the knowledgebase** does a substring match across component ids, objects,
  and fields -- a starting point when you only have a vague description.
- **Find who writes a field** is the tool for "field X had the wrong value and there
  was no exception" -- it returns every writer of that field across **all update
  mechanisms**, grouped by mechanism: **Apex** (classes/triggers, tagged high/medium/
  low risk with a plain-language reason, e.g. "value comes from an uncleared static
  Map, can leak a stale value across re-entrant calls"), **Flow** and **Process
  Builder** (from recordUpdate/recordCreate/assignment elements), and **Workflow Rule
  / Approval Process field updates**. Declarative writers are tagged `declarative`
  and show the target object and the value written. This closes the gap where only
  Apex was searched -- a field silently set by a flow or workflow now shows up too.
  Two caveats to keep in mind when reading results: a flow write can carry a "match
  confidence: medium" when it writes through a record variable rather than `$Record`
  directly, and active-vs-inactive state of a flow/rule is not captured, so confirm a
  declarative writer is actually active before concluding it caused a given change.
  (Formula and roll-up-summary fields are not "written" by anything -- their value
  derives from other data.)

On the **Incidents** tab, file a new incident with a debug log, a suspect field, or
both. You get back immediately:
- Whether this matches a previously-seen signature (**recurrence**) and, if so, how
  many times it's happened and any resolution already on file.
- An **RCA context pack**: the components most likely involved (from the log's
  execution units / exceptions, or from the field's writers), the objects/fields
  they touch, and which of those components changed most recently -- everything an
  AI (or a person) needs to reason about root cause without being handed the whole
  org.

Once you know the fix, record it against the incident's signature so the next
recurrence surfaces the resolution immediately instead of starting from scratch.

## 3a. Normalizing a log on its own (no org, no code, no metadata)

The **Log Normalizer** tab is a completely org-independent path: upload a raw
Salesforce debug log and get back its compact normalized JSON, with no org
connection and no Apex/Flow/LWC source or metadata required or used. This works
even for an org this app has never connected to. Use it to:

- **Download** the normalized JSON (via the button after normalizing, or from any
  stored log's detail view).
- **Store** it in the normalized-log library (tick the checkbox before normalizing)
  so it's kept for future reference and can be pulled up again later. As everywhere
  else, only the derived JSON is stored -- the raw log is used in memory and
  discarded.

The normalized form keeps the RCA-relevant signal and drops the noise: execution
units (with nesting depth and which one threw), deduplicated exceptions with
type/message/stack, collapsed SOQL and DML summaries (counts and row totals),
callouts, flow events, validation failures, the final governor-limit usage, and any
component names the log itself mentions.

**Getting an RCA from the log alone (via Claude + MCP).** Because the normalized log
is self-contained, Claude can reason about root cause and resolution from it without
the affected org's code or metadata. Over the MCP server (section 6) ask Claude
something like *"normalize this log and tell me the likely root cause and fix"* (it
calls `normalize_log`), or *"look at stored log X and suggest an RCA"* (it calls
`get_normalized_log`). Claude bases the RCA on what the log evidences -- the
exception and its top stack frame, which trigger/execution unit was active, governor
limits at or near their max, SOQL/DML volumes that suggest work inside a loop, and
event ordering -- and flags which parts are evidenced versus inferred, plus what to
check in the org's code if confirming the cause requires it. If you *do* have the
org connected, the org-scoped incident flow (section 3) additionally pulls in the
relevant components; the log-only path is for when you don't have, or don't want to
use, the org's code and metadata.

## 4. Data storage guarantee

All disk I/O lives in one file: `app/storage.py`. Every write there is one of:
`org_index.json`, `object_touch_map.json`, `call_graph.json`, `field_touch_map.json`,
`org_stats.json`, `file_hashes.json` (the knowledgebase, entirely derived from
fetched source by the extractors -- including the Flow field-write facts and the
Workflow/Approval field-update facts), `registry.json` (org metadata + component
counts), per-incident `normalized_log.json` / `rca_context_pack.json` /
`meta.json` (derived from an uploaded log, never the log itself), and -- for the
standalone Log Normalizer -- `normalized_logs/<log_id>/normalized_log.json` +
`meta.json` (again, derived from the log, never the raw log).

Concretely: `sf_client.py` fetches raw Apex/Flow/LWC content and raw Workflow
field-update metadata into memory; `onboarding.py` runs it through the extractors
and discards the raw content before calling `storage.save_kb`; the `/incidents` and `/logs/normalize` endpoints in
`main.py` read an uploaded log into memory, normalize it, and explicitly delete the
raw bytes/text before the request returns. The access token is used to construct
request headers and is never part of anything written to disk.

This was validated end-to-end against a mock Salesforce server (`tests/`) with a
grep-based check that no raw class/trigger source, no raw log line, and no token
ever landed under `data/`. One real leak was found and fixed during that validation:
the very first line of a raw debug log (a verbatim log-level directive like
`59.0 APEX_CODE,FINE;APEX_PROFILING,NONE`) was being carried through unparsed; it's
now parsed into a structured `{api_version, log_levels}` object instead.

Two things persisted "as data" are still worth being aware of, since they're
short verbatim substrings from the raw input rather than pure aggregates: normalized
logs keep a short, truncated example string per distinct SOQL/DML/user-debug
pattern (e.g. `"raw_example": "SELECT Id, Line_Number__c FROM Mock_Line__c ..."`,
capped at 150-300 characters) so a person reading the context pack has a concrete
instance to look at, and field-writer cards keep the single line of Apex that
performs a flagged write (e.g. `"example": "lineAdjustmentCache.get(item.Line_Number__c)"`)
so the risk reason is checkable. Neither is the original file/log in anything close
to full -- a debug log can be tens of MB; what's stored per pattern is at most a few
hundred characters -- but if your policy requires zero verbatim substrings of any
length, that's the place to tighten further (drop `raw_example`/`example` or hash
them instead).

## 5. Coverage and known limitations

- **Apex classes/triggers**: fetched via the Tooling API's standard
  `SELECT Id, Name, Body FROM ApexClass`/`ApexTrigger` query -- this is a
  well-documented, reliable capability and was exercised against a mock server that
  mirrors it exactly.
- **Custom objects**: via the standard REST API's global describe (`/sobjects/`) --
  also standard and reliable.
- **Flows**: fetched via `FlowDefinition` (to find each flow's active version) then
  the Tooling API's generic sobject metadata JSON representation
  (`/tooling/sobjects/Flow/<versionId>` -> a `Metadata` blob). This is a less
  commonly exercised corner of the Tooling API. The field names the extractor reads
  (`start.object`, `start.triggerType`, `decisions`, `recordUpdates`, `actionCalls`,
  `subflows`, `faultConnector`, ...) match Salesforce's documented Flow metadata
  schema and were validated against a mock server built to that same schema, but
  this project has not yet had a live Salesforce token to confirm the shape against
  a real org. Every field access in `extractors/flow.py` is defensive (`.get()`-based),
  so a real-world shape mismatch degrades to a thinner flow card rather than
  aborting the whole org fetch.
- **LWC components**: fetched via `LightningComponentBundle`/`LightningComponentResource`
  (base64-decoded `Source`), same caveat as Flows -- schema-correct and
  mock-validated, not yet live-validated.
- **Flow / Process Builder field writes**: `extractors/flow.py` now names the fields
  a flow writes (not just element counts), from `recordUpdates`/`recordCreates`
  `inputAssignments` and from `assignments` targeting `$Record.<Field>` or a record
  variable that is later persisted. Process Builder is detected via
  `processType` (`Workflow`/`InvocableProcess`) and reported as its own mechanism.
  Same live-verification caveat as the rest of Flow parsing; assignment-through-a-
  variable writes are tagged `confidence: medium`. PB stores some record updates as
  `actionCalls` rather than `recordUpdates`; those specific action-parameter field
  writes are not yet parsed, so treat PB coverage as best-effort.
- **Workflow Rule & Approval Process field updates**: `sf_client.fetch_workflow_field_updates`
  queries `WorkflowFieldUpdate` via the Tooling API (both mechanisms share this
  metadata type), and `extractors/workflow.py` turns each into a writer card (object,
  field, operation, value). This previously wasn't indexed at all. Which specific
  workflow rule or approval step *fires* each field update is not resolved yet (the
  field update itself is indexed, not its owning rule's active/entry criteria), so
  confirm the owning rule is active when attributing a change. Not yet live-verified;
  the bulk `Metadata` query has a per-id fallback if an org rejects it.
- **Static-mutable-state / risky-field-write detection**: this is the same
  regex-based Apex analysis validated earlier in this project against two real,
  large customer orgs (including the actual `Increment_Adjustment__c` bug that
  motivated it), just re-run in-memory instead of against files on disk.
- **Refresh** currently requires POSTing to `/api/orgs` again with a fresh token
  (tokens expire) rather than a one-click "refresh" button; `POST /api/orgs/{id}/refresh`
  exists but returns a 400 explaining this.
- **Auth** is username/password with reader/user/admin roles and API tokens for MCP
  (see section 1a). It's application-level authorization, not transport security --
  there's no built-in HTTPS, so terminate TLS at a reverse proxy if you expose this
  beyond localhost. Sessions and API tokens are stored as hashes under `data/auth/`.

**If/when you have a real Instance URL + Access Token to test against**: connect the
org, check `GET /api/orgs/{org_id}/status` for any warnings (each Flow/LWC that
failed to fetch is recorded there instead of aborting the whole run), and compare a
couple of `get_component` results against the actual class/flow to confirm the
shapes line up. Report back anything that looks thin or wrong -- per the point
above, it isolates to `extractors/flow.py` or `sf_client.py`'s Flow/LWC methods.

## 6. Extending via MCP

`mcp_server.py` is a local **stdio** MCP server that proxies every tool call to this
same running web app over HTTP -- it has no direct file or Salesforce access of its
own, so it inherits the same storage guarantee. It exposes: `create_org_connection`,
`get_org_connection_status`, `list_orgs`, `get_org_stats`, `get_component`,
`get_object_touch`, `find_field_writers`, `search_knowledgebase`, `file_incident`,
`list_incidents`, `get_incident`, `record_resolution`, `list_known_issues`, and --
for the org-independent log path -- `normalize_log`, `list_normalized_logs`,
`get_normalized_log`. The three log tools let Claude normalize a raw log, keep it,
and pull it back up, then reason about RCA and resolution from the normalized log
alone (no org code or metadata) -- their tool descriptions tell Claude exactly what
log evidence to base the RCA on and how to caveat it.

**The MCP server authenticates with an API token.** Since the web app now requires a
login, create a token in the UI on the **API Tokens** tab (any role can create one;
the token inherits your role -- a reader token can only call the read tools, a
user/admin token can also connect orgs and file incidents). Copy the token when it's
shown -- it's displayed once -- and put it in the MCP server's `TS_DEBUG_HELPER_TOKEN`
environment variable. If it's missing or wrong, every tool returns a clear 401/403
message telling you what to fix.

To use it, start the web app first, then point an MCP client at the script:

```bash
uvicorn app.main:app --port 8000     # in one terminal
python3 mcp_server.py                # the MCP server itself is launched by your MCP client, not run standalone
```

For Claude Desktop, add to `claude_desktop_config.json`:

```json
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
```

Set `TS_DEBUG_HELPER_URL` if the web app runs on a different host/port, and
`TS_DEBUG_HELPER_TOKEN` to an API token from the **API Tokens** tab. Every tool call
is a plain HTTP request to that URL with the token as a Bearer header -- if the web
app isn't running, or the token is missing/expired/too low-privilege, each tool
returns a clear error saying exactly what to fix instead of failing silently.

## 7. Folder layout

```
webapp/
  app/
    extractors/        apex.py, flow.py, lwc.py, workflow.py, common.py -- in-memory metadata parsers
    sf_client.py        Salesforce Tooling/REST API client (Instance URL + token)
    onboarding.py        orchestrates fetch -> extract -> index -> save, tracks job status
    index_builder.py     builds org_index / object_touch_map / call_graph / field_touch_map / org_stats
    log_normalizer.py     condenses a raw debug log into normalized JSON
    rca.py                assembles an RCA context pack; finds field writers
    incidents.py         exception/field signature matching for recurrence detection
    auth.py               users, roles, password hashing, API tokens, role dependencies
    storage.py           the ONLY module that touches disk
    common_now.py        iso_now() helper
    main.py               FastAPI app / routes (incl. auth, admin, token endpoints)
  static/                 index.html, app.js, style.css -- the web UI (incl. login, admin, tokens)
  tests/
    mock_salesforce.py    mock Tooling/REST API used to validate the whole flow without a live org
  mcp_server.py           local stdio MCP server proxying the web app (sends an API token)
  requirements.txt
  data/                   created at runtime -- org knowledgebases + incidents + auth (flat JSON)
    auth/                 users.json + tokens.json (salted hashes only, never cleartext)
```

## 8. What's validated vs. not

Validated end-to-end in this project (against a mock Salesforce server standing in
for the real Tooling API): org connect -> fetch -> extract -> index -> save; org
stats and risk rollups; field-writer lookup; filing an incident from a log (with
exception-signature recurrence detection); filing a field-only incident (no
exception, field-signature detection); recording and retrieving a resolution; and
the "nothing but derived JSON reaches disk" guarantee, via a grep-based check of
everything under `data/` after a full run.

Not yet validated: a real Salesforce org's actual Tooling API responses for Flow and
LWC metadata (see section 5), and the MCP server against a live MCP client (it was
verified to import and register its tools cleanly, and its HTTP calls were
sanity-checked against the running web app's tool implementations, but not driven by
an actual Claude Desktop/Cowork MCP session in this environment).

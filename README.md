# TS Intelligent Debug Helper

Tooling to summarize a Salesforce org's customizations (Apex, triggers, Flows,
Process Builder, Workflow field updates, LWC) into a compact knowledgebase, and to
normalize Salesforce debug logs, so that root-cause analysis and resolutions for
incidents can be derived with minimal token usage.

Two ways to use it live in this repo:

- **`webapp/`** — a local FastAPI web app: connect a Salesforce org via Instance URL
  + access token, build/refresh its knowledgebase, normalize logs, file incidents
  with recurrence detection, and find every writer of a field across Apex / Flow /
  Process Builder / Workflow. Includes username-password auth (reader/user/admin
  roles), API tokens, and a local stdio MCP server so Claude can drive it. See
  [`webapp/README.md`](webapp/README.md) for setup, usage, the data-storage
  guarantee, and MCP configuration.

- **`scripts/`** — the original standalone CLI versions of the same extraction /
  normalization / RCA pipeline (run per-folder against a metadata backup).

## Data handling

Only derived / normalized JSON is ever written to disk — raw Apex/Flow/LWC source,
Salesforce access tokens, and raw debug logs are held in memory only for the request
that needs them, then discarded.

**Nothing under `webapp/data/` (org knowledgebases, incidents, normalized-log
library, and `auth/` user + token data) is tracked in git** — see `.gitignore`. This
repo contains source code only, never customer org data or user credentials.

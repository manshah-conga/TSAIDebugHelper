"""
Salesforce Tooling API client: given an Instance URL + an already-obtained
Access Token (no OAuth flow implemented here -- the user pastes a token,
e.g. from a Connected App client-credentials/JWT flow they run
themselves, or a session id from Workbench), fetches exactly the metadata
the extractors need and nothing else.

Everything this returns is held in memory by the caller (see
onboarding.py) just long enough to run it through the extractors and
discard it -- this module has no disk I/O of its own.

Coverage / honesty about what's verified:
  - ApexClass / ApexTrigger (Body field via Tooling API SOQL): this is a
    standard, well-documented Tooling API capability and should work
    against any real org as written.
  - CustomObject / CustomField (for the known-objects list): via the
    standard REST API's global describe + per-object describe, also
    standard and reliable.
  - Flow (Metadata via /tooling/sobjects/Flow/<id>) and
    LightningComponentBundle/-Resource: these use less commonly
    exercised corners of the Tooling API. The request shapes below match
    Salesforce's documented schema, but this session had no live token to
    verify them against a real org. Each is wrapped so a failure here
    produces a warning and an empty result for that metadata type,
    rather than aborting the whole fetch.
"""
import base64
import urllib.request
import httpx

API_VERSION = "60.0"

# Records requested per query page (see _headers). Small pages return fast and
# keep memory bounded even for orgs with thousands of large Apex classes.
QUERY_BATCH_SIZE = 200

# Per-request read timeout, in seconds. Generous because even a single small
# page of Apex classes carries full source bodies, and a busy org can be slow
# to produce the first page.
READ_TIMEOUT = 180


class SalesforceAuthError(Exception):
    pass


def _system_https_proxy(target_url):
    """On Windows (and elsewhere), a corporate network sometimes routes
    outbound HTTPS through a proxy that's configured system-wide (what
    browsers pick up automatically) rather than via HTTP_PROXY/HTTPS_PROXY
    environment variables, which is all httpx checks on its own by default.
    urllib.request.getproxies() additionally reads that OS-level
    configuration (the Windows registry Internet Settings on Windows), so we
    use it explicitly.

    Critically, we only apply it if urllib.request.proxy_bypass() says this
    specific target host should NOT be exempted -- i.e. we honour the same
    NO_PROXY / bypass-list semantics curl and browsers do. Skipping this
    check would force every request through the proxy unconditionally,
    breaking local/internal targets that are meant to bypass it."""
    try:
        host = httpx.URL(target_url).host
        if not host or urllib.request.proxy_bypass(host):
            return None
        proxies = urllib.request.getproxies()
    except Exception:
        return None
    return proxies.get("https") or proxies.get("http") or None


def make_async_client(target_url=None, timeout=READ_TIMEOUT):
    # local_address="0.0.0.0" forces the underlying socket to bind IPv4 before
    # connecting, which in turn forces httpx/httpcore to only try IPv4
    # addresses for the target host. This is the standard workaround for a
    # very common "curl works instantly, Python hangs until it times out"
    # symptom: DNS returns both an A (IPv4) and AAAA (IPv6) record, curl (and
    # browsers) race both and use whichever answers first (RFC 6555 "Happy
    # Eyeballs"), but httpx/httpcore does not -- it tries the addresses in
    # the order the OS resolver returned them, and if that's IPv6 first on a
    # network where IPv6 is technically enabled but not actually routed
    # (common on corporate networks), every request hangs for the full
    # timeout on a route that was never going to work, when the IPv4 route
    # right behind it would have connected immediately.
    transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")
    proxy = _system_https_proxy(target_url) if target_url else None
    return httpx.AsyncClient(proxy=proxy, timeout=timeout, transport=transport)


class SalesforceClient:
    def __init__(self, instance_url, access_token, api_version=API_VERSION):
        self.instance_url = instance_url.rstrip("/")
        self.access_token = access_token
        self.api_version = api_version
        self.warnings = []

    def _headers(self, query=False):
        h = {"Authorization": f"Bearer {self.access_token}", "Content-Type": "application/json"}
        if query:
            # Ask Salesforce for small pages. Without this, a query like
            # `SELECT Id, Name, Body FROM ApexClass` against a real org returns
            # the FULL source of every Apex class in one page (default up to
            # 2000 records), which Salesforce buffers entirely before sending
            # any response -- for a large managed-package org that's tens of MB
            # and can take well over a minute just to produce the first byte,
            # which shows up as a ReadTimeout while waiting for response
            # headers. A small batch returns fast; nextRecordsUrl pages through
            # the rest, one quick request at a time.
            h["Sforce-Query-Options"] = f"batchSize={QUERY_BATCH_SIZE}"
        return h

    async def _get(self, client, path, query=False, timeout=READ_TIMEOUT):
        url = path if path.startswith("http") else f"{self.instance_url}{path}"
        resp = await client.get(url, headers=self._headers(query=query), timeout=timeout)
        if resp.status_code == 401:
            raise SalesforceAuthError("Access token was rejected (401). It may be expired or the "
                                       "Instance URL doesn't match the org that issued it.")
        resp.raise_for_status()
        return resp.json()

    async def verify_connection(self):
        """A cheap call to confirm the URL + token actually work before
        committing to a full fetch."""
        async with make_async_client(self.instance_url) as client:
            return await self._get(client, f"/services/data/v{self.api_version}/limits")

    async def _tooling_query_all(self, client, soql):
        path = f"/services/data/v{self.api_version}/tooling/query/?q={httpx.QueryParams({'q': soql})['q']}"
        records = []
        while path:
            data = await self._get(client, path, query=True)
            records.extend(data.get("records", []))
            path = data.get("nextRecordsUrl")
        return records

    async def _rest_query_all(self, client, soql):
        path = f"/services/data/v{self.api_version}/query/?q={httpx.QueryParams({'q': soql})['q']}"
        records = []
        while path:
            data = await self._get(client, path, query=True)
            records.extend(data.get("records", []))
            path = data.get("nextRecordsUrl")
        return records

    async def fetch_apex_classes(self, client):
        """-> {ClassName: {"body": str, "file": "ClassName.cls"}}"""
        records = await self._tooling_query_all(client, "SELECT Id, Name, Body FROM ApexClass")
        return {r["Name"]: {"body": r.get("Body") or "", "file": f"{r['Name']}.cls"} for r in records if r.get("Body")}

    async def fetch_apex_triggers(self, client):
        records = await self._tooling_query_all(client, "SELECT Id, Name, Body, TableEnumOrId FROM ApexTrigger")
        return {r["Name"]: {"body": r.get("Body") or "", "file": f"{r['Name']}.trigger"} for r in records if r.get("Body")}

    async def fetch_custom_objects(self, client):
        """-> set of custom object / custom metadata / platform event API names,
        used the same way objects/*.object filenames were used from a local
        backup: to disambiguate real object references from stray identifiers."""
        try:
            data = await self._get(client, f"/services/data/v{self.api_version}/sobjects/")
            return {s["name"] for s in data.get("sobjects", []) if s.get("custom")}
        except Exception as e:
            self.warnings.append(f"custom object list: {e}")
            return set()

    async def fetch_flows(self, client):
        """-> {FlowApiName: {"metadata": dict, "api_version": str}}
        Best-effort: gets the active Flow version per FlowDefinition, then
        the Metadata JSON for each via the Tooling API's generic sobject
        metadata representation."""
        out = {}
        try:
            defs = await self._tooling_query_all(
                client,
                "SELECT Id, DeveloperName, ActiveVersionId, ActiveVersion.VersionNumber, ActiveVersion.ApiVersion "
                "FROM FlowDefinition WHERE ActiveVersionId != null",
            )
        except Exception as e:
            self.warnings.append(f"flow list: {e}")
            return out

        for d in defs:
            version_id = d.get("ActiveVersionId")
            name = d.get("DeveloperName")
            if not version_id or not name:
                continue
            try:
                rec = await self._get(client, f"/services/data/v{self.api_version}/tooling/sobjects/Flow/{version_id}")
                metadata = rec.get("Metadata", {})
                av = (d.get("ActiveVersion") or {}) or {}
                out[name] = {"metadata": metadata, "api_version": av.get("ApiVersion")}
            except Exception as e:
                self.warnings.append(f"flow '{name}': {e}")
        return out

    async def fetch_workflow_field_updates(self, client):
        """-> {FullName: metadata_dict}
        Workflow Rule AND Approval Process field updates both flow through
        the WorkflowFieldUpdate metadata type, so this one fetch covers both.
        Best-effort: tries a bulk Metadata query first; if that's rejected
        (the Metadata compound field can't always be queried in bulk), falls
        back to listing FullNames then fetching each one's metadata via the
        generic Tooling sobject GET."""
        out = {}
        try:
            rows = await self._tooling_query_all(client, "SELECT Id, FullName, Metadata FROM WorkflowFieldUpdate")
            for r in rows:
                if r.get("FullName") and r.get("Metadata") is not None:
                    out[r["FullName"]] = r["Metadata"]
            if out:
                return out
        except Exception as e:
            self.warnings.append(f"workflow field update bulk query: {e}")

        try:
            rows = await self._tooling_query_all(client, "SELECT Id, FullName FROM WorkflowFieldUpdate")
        except Exception as e:
            self.warnings.append(f"workflow field update list: {e}")
            return out
        for r in rows:
            fid, full = r.get("Id"), r.get("FullName")
            if not fid or not full:
                continue
            try:
                rec = await self._get(client, f"/services/data/v{self.api_version}/tooling/sobjects/WorkflowFieldUpdate/{fid}")
                out[full] = rec.get("Metadata", {})
            except Exception as e:
                self.warnings.append(f"workflow field update '{full}': {e}")
        return out

    async def fetch_lwc(self, client):
        """-> {ComponentName: {filename: content}}"""
        out = {}
        try:
            bundles = await self._tooling_query_all(client, "SELECT Id, DeveloperName FROM LightningComponentBundle")
        except Exception as e:
            self.warnings.append(f"lwc bundle list: {e}")
            return out

        for b in bundles:
            bundle_id, name = b.get("Id"), b.get("DeveloperName")
            if not bundle_id or not name:
                continue
            try:
                resources = await self._tooling_query_all(
                    client,
                    f"SELECT FilePath, Source FROM LightningComponentResource "
                    f"WHERE LightningComponentBundleId = '{bundle_id}'",
                )
                files = {}
                for r in resources:
                    fp = r.get("FilePath") or ""
                    fn = fp.rsplit("/", 1)[-1]
                    src = r.get("Source") or ""
                    try:
                        # Source is base64-encoded for some file types/API versions;
                        # fall back to the raw string if it isn't.
                        files[fn] = base64.b64decode(src).decode("utf-8")
                    except Exception:
                        files[fn] = src
                out[name] = files
            except Exception as e:
                self.warnings.append(f"lwc '{name}': {e}")
        return out

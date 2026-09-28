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
import asyncio
import base64
import os
import random
import time
import urllib.request
import httpx

API_VERSION = "60.0"


def _env_int(name, default, lo, hi):
    try:
        return max(lo, min(hi, int(os.environ.get(name, default))))
    except (TypeError, ValueError):
        return default


# How many Salesforce requests one org fetch keeps in flight at once.
# Salesforce allows 25 concurrent API requests that run longer than 20s per
# org (production and sandbox alike; fewer on Developer Edition / trials),
# and the org's own integrations share that budget -- so the default stays
# well under it. Raise with TS_SF_FETCH_CONCURRENCY on a quiet sandbox.
FETCH_CONCURRENCY = _env_int("TS_SF_FETCH_CONCURRENCY", 8, 1, 20)

# Apex bodies are fetched by explicit Id list, a chunk per request, so the
# chunks can run side by side (a single SOQL cursor can only be walked one
# page at a time). A chunk closes at ID_CHUNK ids or CHUNK_CHAR_BUDGET
# characters of source (from LengthWithoutComments), whichever comes first,
# so one chunk full of 10k-line classes does not become the long pole.
ID_CHUNK = _env_int("TS_SF_ID_CHUNK", 100, 10, 200)
CHUNK_CHAR_BUDGET = _env_int("TS_SF_CHUNK_CHARS", 1_500_000, 100_000, 20_000_000)
LWC_BUNDLE_CHUNK = _env_int("TS_SF_LWC_CHUNK", 25, 1, 100)

# Flow metadata is one GET per flow. The Tooling Composite API carries up to
# 25 GETs in one round trip (and one API call against the daily limit); 10
# keeps each composite response to a reasonable size for big flows. Set 1 to
# disable composite and fetch each flow on its own.
COMPOSITE_SIZE = _env_int("TS_SF_COMPOSITE_SIZE", 10, 1, 25)

# Managed-package Apex/trigger/LWC source is hidden in a subscriber org (the
# Body comes back as "(hidden)"), so fetching it is pure cost. Listed by name
# (so call edges into the package still resolve) but not fetched. Set 1 to
# fetch it anyway. Flows are declarative automation, not code, and are still
# fetched unless TS_SF_SKIP_MANAGED_FLOWS=1.
FETCH_MANAGED_CODE = os.environ.get("TS_SF_FETCH_MANAGED_CODE", "0").strip().lower() in ("1", "true", "yes")
SKIP_MANAGED_FLOWS = os.environ.get("TS_SF_SKIP_MANAGED_FLOWS", "0").strip().lower() in ("1", "true", "yes")

# Transient failures are far more likely with requests in parallel (a busy
# sandbox answering 503, a read timeout on one chunk). One chunk failing used
# to be one warning and a silently incomplete knowledgebase; retry first.
MAX_ATTEMPTS = _env_int("TS_SF_MAX_ATTEMPTS", 4, 1, 8)

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


def _retryable_status(resp):
    if resp.status_code in (408, 429, 500, 502, 503, 504):
        return True
    # Salesforce reports the concurrent-request cap as a 403.
    if resp.status_code == 403:
        body = resp.text[:500].lower()
        return "concurrent" in body and "limit" in body
    return False


def _in_clause(ids):
    return ",".join("'" + i.replace("'", "") + "'" for i in ids)


class AdaptiveLimiter:
    """Caps requests in flight and adapts the cap to what the org tolerates
    (additive increase, multiplicative decrease -- the TCP congestion rule).

    Starts at the configured maximum. A throttling signal -- 429, 503/502/504,
    Salesforce's concurrent-request 403, or a timeout -- halves the cap
    (at most once per `cooldown` seconds, since a burst of in-flight requests
    tends to fail together and should count as one event). After
    `increase_after` consecutive successes the cap creeps back up by one, never
    past the maximum. So a busy production org with integrations already
    near the concurrency limit gets backed off automatically instead of
    having its own traffic starved, and a quiet sandbox runs at full speed.
    """

    def __init__(self, max_limit, min_limit=1, increase_after=None, cooldown=2.0):
        self.max = max(1, int(max_limit))
        self.min = max(1, min(int(min_limit), self.max))
        self.limit = self.max
        self.in_flight = 0
        self.increase_after = increase_after or max(5, 2 * self.max)
        self.cooldown = cooldown
        self._streak = 0
        self._last_cut = -1e9
        self._cond = None
        self.throttle_events = 0
        self.lowest = self.limit
        self.peak_in_flight = 0

    def _condition(self):
        if self._cond is None:
            self._cond = asyncio.Condition()
        return self._cond

    async def __aenter__(self):
        cond = self._condition()
        async with cond:
            await cond.wait_for(lambda: self.in_flight < self.limit)
            self.in_flight += 1
            self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        return self

    async def __aexit__(self, *exc):
        cond = self._condition()
        async with cond:
            self.in_flight -= 1
            cond.notify_all()
        return False

    def on_success(self):
        self._streak += 1
        if self._streak >= self.increase_after and self.limit < self.max:
            self.limit += 1
            self._streak = 0
            # waiters are woken by the next __aexit__'s notify_all

    def on_throttle(self):
        self._streak = 0
        now = time.monotonic()
        if now - self._last_cut < self.cooldown:
            return
        self._last_cut = now
        self.throttle_events += 1
        self.limit = max(self.min, self.limit // 2)
        self.lowest = min(self.lowest, self.limit)

    def stats(self):
        return {"max": self.max, "final": self.limit, "lowest": self.lowest,
                "throttle_events": self.throttle_events, "peak_in_flight": self.peak_in_flight}


_THROTTLE_STATUSES = (429, 502, 503, 504)


class CompositeUnsupported(Exception):
    pass


class SalesforceClient:
    def __init__(self, instance_url, access_token, api_version=API_VERSION,
                 concurrency=None):
        self.instance_url = instance_url.rstrip("/")
        self.access_token = access_token
        self.api_version = api_version
        self.warnings = []
        self.concurrency = concurrency or FETCH_CONCURRENCY
        self.limiter = AdaptiveLimiter(self.concurrency)
        self.requests_made = 0
        self.retries = 0
        self.composite_ok = COMPOSITE_SIZE > 1

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

    async def _request(self, client, method, path, query=False, json_body=None, timeout=READ_TIMEOUT):
        """One request, bounded by the adaptive limiter and retried with
        jittered backoff on timeouts, dropped connections, 5xx and the
        concurrent-request 403. Throttling signals shrink the limiter; a run
        of successes grows it back. A 401 is never retried."""
        url = path if path.startswith("http") else f"{self.instance_url}{path}"
        attempt = 0
        while True:
            attempt += 1
            try:
                async with self.limiter:
                    self.requests_made += 1
                    resp = await client.request(method, url, headers=self._headers(query=query),
                                                json=json_body, timeout=timeout)
            except (httpx.TimeoutException, httpx.RemoteProtocolError, httpx.ReadError,
                    httpx.ConnectError) as e:
                if isinstance(e, httpx.TimeoutException):
                    self.limiter.on_throttle()
                if attempt >= MAX_ATTEMPTS:
                    raise
                self.retries += 1
                await asyncio.sleep(min(30, 1.5 * 2 ** (attempt - 1)) + random.random())
                continue
            if resp.status_code == 401:
                raise SalesforceAuthError("Access token was rejected (401). It may be expired or the "
                                           "Instance URL doesn't match the org that issued it.")
            if _retryable_status(resp):
                if resp.status_code in _THROTTLE_STATUSES or resp.status_code == 403:
                    self.limiter.on_throttle()
                if attempt < MAX_ATTEMPTS:
                    self.retries += 1
                    retry_after = resp.headers.get("Retry-After")
                    delay = float(retry_after) if (retry_after or "").isdigit() else 1.5 * 2 ** (attempt - 1)
                    await asyncio.sleep(min(30, delay) + random.random())
                    continue
            resp.raise_for_status()
            self.limiter.on_success()
            return resp.json()

    async def _get(self, client, path, query=False, timeout=READ_TIMEOUT):
        return await self._request(client, "GET", path, query=query, timeout=timeout)

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
        """-> {ClassName: {"body", "file", "namespace_prefix", "api_version"}}"""
        records = await self._tooling_query_all(
            client, "SELECT Id, Name, NamespacePrefix, ApiVersion, Body FROM ApexClass")
        return {r["Name"]: {"body": r.get("Body") or "", "file": f"{r['Name']}.cls",
                            "namespace_prefix": r.get("NamespacePrefix"), "api_version": r.get("ApiVersion")}
                for r in records if r.get("Body")}

    async def fetch_apex_triggers(self, client):
        records = await self._tooling_query_all(
            client, "SELECT Id, Name, NamespacePrefix, ApiVersion, Body, TableEnumOrId FROM ApexTrigger")
        return {r["Name"]: {"body": r.get("Body") or "", "file": f"{r['Name']}.trigger",
                            "namespace_prefix": r.get("NamespacePrefix"), "api_version": r.get("ApiVersion")}
                for r in records if r.get("Body")}

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
                "SELECT Id, DeveloperName, NamespacePrefix, ActiveVersionId, "
                "ActiveVersion.VersionNumber, ActiveVersion.ApiVersion, ActiveVersion.Status "
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
                # Only the ACTIVE version is fetched; that's exactly what runs, so
                # is_active_version is True and status Active. Sibling (obsolete/
                # draft) versions are not enumerated -- honestly reflected as [].
                version_info = {
                    "version_number": av.get("VersionNumber"),
                    "status": av.get("Status") or "Active",
                    "is_active_version": True,
                    # sibling versions not enumerated (only the active version is
                    # fetched) -- left unset so the card records it honestly.
                }
                out[name] = {"metadata": metadata, "api_version": av.get("ApiVersion"),
                             "version_info": version_info, "namespace_prefix": d.get("NamespacePrefix")}
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

    # ------------------------------------------------------------------
    # Parallel-fetch primitives (used by onboarding.run_onboarding).
    #
    # The sequential fetchers above walk one SOQL cursor page by page and, for
    # flows and LWC, make one request per component in a row. On a large org
    # that is thousands of round trips end to end -- the 80-minute connects.
    # These split the work into independent requests that onboarding runs
    # side by side under the one semaphore in _get.
    # ------------------------------------------------------------------

    async def list_apex(self, client, sobject):
        """Cheap listing -- Id/Name/namespace/size, no Body -- for ApexClass or
        ApexTrigger. Falls back to a listing without LengthWithoutComments if
        the org rejects that field."""
        base = "Id, Name, NamespacePrefix" + (", TableEnumOrId" if sobject == "ApexTrigger" else "")
        try:
            return await self._tooling_query_all(
                client, f"SELECT {base}, LengthWithoutComments FROM {sobject}")
        except httpx.HTTPStatusError as e:
            if e.response is not None and e.response.status_code == 400:
                self.warnings.append(f"{sobject} listing without size hints: {e.response.text[:160]}")
                return await self._tooling_query_all(client, f"SELECT {base} FROM {sobject}")
            raise

    @staticmethod
    def chunk_ids(listing, max_ids=None, char_budget=None):
        """Split a listing into Id chunks bounded by count AND by source size,
        largest classes first so the long poles start early."""
        max_ids = max_ids or ID_CHUNK
        char_budget = char_budget or CHUNK_CHAR_BUDGET

        def size(r):
            n = r.get("LengthWithoutComments")
            return n if isinstance(n, int) and n > 0 else 4000

        rows = sorted((r for r in listing if r.get("Id")), key=size, reverse=True)
        chunks, cur, cur_size = [], [], 0
        for r in rows:
            s = size(r)
            if cur and (len(cur) >= max_ids or cur_size + s > char_budget):
                chunks.append(cur)
                cur, cur_size = [], 0
            cur.append(r["Id"])
            cur_size += s
        if cur:
            chunks.append(cur)
        return chunks

    async def fetch_apex_chunk(self, client, sobject, ids):
        """Bodies for one chunk of ApexClass / ApexTrigger ids."""
        extra = ", TableEnumOrId" if sobject == "ApexTrigger" else ""
        return await self._tooling_query_all(
            client, f"SELECT Id, Name, NamespacePrefix, ApiVersion, Body{extra} FROM {sobject} "
                    f"WHERE Id IN ({_in_clause(ids)})")

    async def fetch_org_namespace(self, client):
        """The org's OWN namespace prefix, if it has one. Components carrying
        it are the customer's code (a namespaced dev/packaging org), not a
        managed package, and must still be fetched."""
        try:
            rows = await self._rest_query_all(client, "SELECT NamespacePrefix FROM Organization")
            return (rows[0].get("NamespacePrefix") or None) if rows else None
        except SalesforceAuthError:
            raise
        except Exception as e:
            self.warnings.append(f"org namespace lookup: {e}")
            return None

    async def fetch_flow_batch(self, client, defs):
        """Metadata for several flows in ONE Tooling Composite request.
        -> list of (definition, info-or-None); None means that one subrequest
        failed and the caller should fetch it on its own. Raises
        CompositeUnsupported if the org rejects the composite endpoint itself."""
        v = self.api_version
        body = {"allOrNone": False, "compositeRequest": [
            {"method": "GET", "referenceId": f"f{i}",
             "url": f"/services/data/v{v}/tooling/sobjects/Flow/{d['ActiveVersionId']}"}
            for i, d in enumerate(defs)]}
        try:
            data = await self._request(client, "POST", f"/services/data/v{v}/tooling/composite", json_body=body)
        except httpx.HTTPStatusError as e:
            if e.response is not None and e.response.status_code in (400, 404, 405, 501):
                raise CompositeUnsupported(f"{e.response.status_code}: {e.response.text[:160]}")
            raise
        by_ref = {r.get("referenceId"): r for r in (data or {}).get("compositeResponse", [])}
        out = []
        for i, d in enumerate(defs):
            r = by_ref.get(f"f{i}") or {}
            if r.get("httpStatusCode") == 200 and isinstance(r.get("body"), dict):
                out.append((d, self._flow_info(d, r["body"])))
            else:
                out.append((d, None))
        return out

    async def list_flow_definitions(self, client):
        return await self._tooling_query_all(
            client,
            "SELECT Id, DeveloperName, NamespacePrefix, ActiveVersionId, "
            "ActiveVersion.VersionNumber, ActiveVersion.ApiVersion, ActiveVersion.Status "
            "FROM FlowDefinition WHERE ActiveVersionId != null")

    async def fetch_flow_one(self, client, d):
        """(name, info) for one FlowDefinition row -- the same shape
        fetch_flows produces per flow."""
        rec = await self._get(client, f"/services/data/v{self.api_version}/tooling/sobjects/Flow/{d['ActiveVersionId']}")
        return d["DeveloperName"], self._flow_info(d, rec)

    @staticmethod
    def _flow_info(d, rec):
        av = (d.get("ActiveVersion") or {}) or {}
        return {
            "metadata": rec.get("Metadata", {}), "api_version": av.get("ApiVersion"),
            "version_info": {"version_number": av.get("VersionNumber"),
                             "status": av.get("Status") or "Active", "is_active_version": True},
            "namespace_prefix": d.get("NamespacePrefix"),
        }

    async def list_lwc_bundles(self, client):
        return await self._tooling_query_all(
            client, "SELECT Id, DeveloperName, NamespacePrefix FROM LightningComponentBundle")

    async def fetch_lwc_chunk(self, client, bundles):
        """{ComponentName: {filename: content}} for a chunk of bundles in ONE
        query (was one query per bundle). If the IN query is rejected, falls
        back to per-bundle queries for this chunk only."""
        id_to_name = {b["Id"]: b["DeveloperName"] for b in bundles if b.get("Id") and b.get("DeveloperName")}
        out = {name: {} for name in id_to_name.values()}
        try:
            rows = await self._tooling_query_all(
                client, "SELECT LightningComponentBundleId, FilePath, Source FROM LightningComponentResource "
                        f"WHERE LightningComponentBundleId IN ({_in_clause(list(id_to_name))})")
        except httpx.HTTPStatusError as e:
            if e.response is None or e.response.status_code != 400:
                raise
            rows = []
            for bid in id_to_name:
                for r in await self._tooling_query_all(
                        client, "SELECT FilePath, Source FROM LightningComponentResource "
                                f"WHERE LightningComponentBundleId = '{bid}'"):
                    r["LightningComponentBundleId"] = bid
                    rows.append(r)
        for r in rows:
            name = id_to_name.get(r.get("LightningComponentBundleId"))
            if not name:
                continue
            fn = (r.get("FilePath") or "").rsplit("/", 1)[-1]
            src = r.get("Source") or ""
            try:
                out[name][fn] = base64.b64decode(src, validate=True).decode("utf-8")
            except Exception:
                out[name][fn] = src
        return out

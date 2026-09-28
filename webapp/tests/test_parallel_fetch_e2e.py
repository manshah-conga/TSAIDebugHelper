"""
End-to-end checks for the parallel, streaming org fetch and for async-dispatch
edges (who enqueues / executes / schedules an Apex job).

1. Connect the mock org through the real API and check the Equiniti case:
   FinaliseCartBatch.finish() does System.enqueueJob(new
   C2C_ActivateCPIOrderQueuable(...), delayInMinutes) -- the queueable must
   come back from the inbound lookup AND carry `invoked_by` on its own card.
2. Check the fetch really was chunked (Apex bodies fetched by Id IN (...),
   LWC resources a chunk of bundles per query) and progress tracks finished.
3. Benchmark: pad the mock org with synthetic classes, add per-request
   latency, and compare concurrency 1 vs the default. Parallel must be
   markedly faster and must produce the same knowledgebase.

Run:  python tests/test_parallel_fetch_e2e.py
"""
import os
import sys
import time
import socket
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="ts-parallel-e2e-")
os.environ["TS_ADMIN_PASSWORD"] = "adminpassword123"
for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(var, None)
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
os.environ["TS_SKIP_ENV_FILE"] = "1"

from app import storage  # noqa: E402

storage.DATA_ROOT = _TMP
storage.ORGS_ROOT = os.path.join(_TMP, "orgs")
storage.REGISTRY_PATH = os.path.join(_TMP, "registry.json")
storage.LOGS_ROOT = os.path.join(_TMP, "normalized_logs")
storage.AUTH_ROOT = os.path.join(_TMP, "auth")
storage.USERS_PATH = os.path.join(storage.AUTH_ROOT, "users.json")
storage.TOKENS_PATH = os.path.join(storage.AUTH_ROOT, "tokens.json")

import logging  # noqa: E402
logging.getLogger("httpx").setLevel(logging.WARNING)

import uvicorn  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app import sf_client  # noqa: E402
from app import chunk_parse  # noqa: E402
from tests import mock_salesforce  # noqa: E402

FAILURES = []


def check(label, condition, extra=""):
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"  {extra}" if extra and not condition else ""))
    if not condition:
        FAILURES.append(label)


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _start_mock():
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(mock_salesforce.app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return port
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("mock Salesforce did not start")


def _wait_done(client, org_id, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = client.get(f"/api/orgs/{org_id}/status").json()
        if s.get("status") in ("done", "error", "unknown"):
            return s
        time.sleep(0.2)
    return {"status": "timeout"}


def _connect(c, org_id, instance_url):
    # TestClient runs the background fetch before post() returns, so the
    # clock has to start before the request, not after it.
    t0 = time.time()
    r = c.post("/api/orgs", json={
        "org_id": org_id, "org_name": org_id, "instance_url": instance_url,
        "access_token": mock_salesforce.EXPECTED_TOKEN, "visibility": "private"})
    assert r.status_code == 200, r.text[:200]
    s = _wait_done(c, org_id)
    return s, time.time() - t0


def main():
    port = _start_mock()
    url = f"http://127.0.0.1:{port}"
    print(f"\nmock Salesforce on {url}")

    with TestClient(app) as c:
        c.post("/api/auth/login", json={"username": "admin", "password": "adminpassword123"})

        print("\n-- queueable started from a batch finish() (the Equiniti case) --")
        mock_salesforce.REQUEST_LOG.clear()
        s, _ = _connect(c, "par1", url)
        check("fetch completed", s.get("status") == "done", str(s)[:300])

        inbound = c.get("/api/orgs/par1/inbound/C2C_ActivateCPIOrderQueuable").json()
        rows = inbound.get("called_by", [])
        hit = next((r for r in rows if r.get("id") == "FinaliseCartBatch"), None)
        check("inbound lookup finds the batch that enqueues the queueable", hit is not None, str(rows))
        if hit:
            check("the edge is labelled System.enqueueJob", hit.get("via") == "System.enqueueJob", str(hit))
            check("the calling method is named", hit.get("method") == "finish", str(hit))
            check("the delay is resolved from the local variable", hit.get("delay_minutes") == 2, str(hit))
            check("a line number is given", isinstance(hit.get("line"), int), str(hit))

        q = c.get("/api/orgs/par1/components/C2C_ActivateCPIOrderQueuable").json()
        ep = next((e for e in q.get("entry_points", []) if e.get("kind") == "Queueable"), {})
        check("Queueable is detected when it is not first in `implements`", bool(ep), str(q.get("entry_points")))
        check("the queueable's own card says who starts it",
              any(i.get("id") == "FinaliseCartBatch" for i in ep.get("invoked_by", [])), str(ep))

        b = c.get("/api/orgs/par1/components/FinaliseCartBatch").json()
        check("generic `implements Database.Batchable<Id>` now parses",
              "Database.Batchable<Id>" in b.get("implements", []), str(b.get("implements")))
        disp = b.get("async_dispatches", [])
        check("the batch card lists its dispatch",
              any(d.get("target") == "C2C_ActivateCPIOrderQueuable" for d in disp), str(disp))
        check("method visibility is recovered", any(m.get("visibility") == "public" for m in b.get("methods", [])))

        stats = c.get("/api/orgs/par1/stats").json()
        inv = (stats.get("async_invocations") or {}).get("C2C_ActivateCPIOrderQueuable")
        check("org stats carry an async invocation map", bool(inv), str(stats.get("async_invocations"))[:200])

        print("\n-- the fetch was chunked, not per-record --")
        log = [q.upper() for q in mock_salesforce.REQUEST_LOG]
        check("Apex bodies fetched by Id IN (...)", any("FROM APEXCLASS WHERE ID IN" in q for q in log), str(log))
        check("LWC resources fetched per chunk of bundles",
              any("LIGHTNINGCOMPONENTBUNDLEID IN" in q for q in log), str(log))
        tracks = {t["name"]: t for t in s.get("tracks", [])}
        check("every progress stream finished",
              all(t.get("state") == "done" for t in tracks.values()) and len(tracks) == 5, str(s.get("tracks")))
        check("fetch stats are reported", (s.get("fetch_stats") or {}).get("requests", 0) > 0, str(s.get("fetch_stats")))

        print("\n-- managed-package code is listed, not fetched --")
        log = [q.upper() for q in mock_salesforce.REQUEST_LOG]
        body_q = " ".join(q for q in log if "BODY" in q.split(" FROM ")[0])
        check("managed class bodies are never requested",
              "01P003" not in body_q and "01P006" not in body_q, body_q[:300])
        check("managed trigger bodies are never requested", "01Q002" not in body_q, body_q[:300])
        m = c.get("/api/orgs/par1/components/PricingCallbackBase").json()
        check("a managed class still has a card that says why it is thin",
              m.get("source_fetched") is False and m.get("is_managed") is True, str(m)[:200])
        ep = c.get("/api/orgs/par1/entry-points/Account").json()
        check("a managed trigger still shows up in its object's entry points",
              any(t.get("id") == "ProductConfigurationTrigger" for t in ep.get("apex_triggers", [])), str(ep))
        lw = c.get("/api/orgs/par1/components/cartGrid").json()
        check("a managed LWC bundle gets a stub card", lw.get("source_fetched") is False, str(lw)[:200])
        check("its resources are never requested", not any("0RB002" in q for q in log))
        check("the skip is reported", (s.get("fetch_stats") or {}).get("managed_skipped", {}).get("classes") == 3,
              str((s.get("fetch_stats") or {}).get("managed_skipped")))

        print("\n-- the org's own namespace is customer code --")
        mock_salesforce.ORG_NAMESPACE = "myns"
        s_ns, _ = _connect(c, "nsorg", url)
        mock_salesforce.ORG_NAMESPACE = None
        own = c.get("/api/orgs/nsorg/components/OwnNsHelper").json()
        check("a class in the org's own namespace is fetched and parsed",
              own.get("source_fetched") is not False and own.get("calls_to"), str(own)[:200])
        check("and counted as customer-authored",
              own.get("is_customer_authored") is True and own.get("is_managed") is False, str(own)[:200])
        check("its call into a managed class is still an edge",
              any(c2.get("target") == "PricingCallbackBase" and c2.get("kind") == "local_class"
                  for c2 in own.get("calls_to", [])), str(own.get("calls_to")))

        print("\n-- flows are fetched through Composite, 10 per request --")
        mock_salesforce.EXTRA_FLOWS[:] = [
            {"Id": f"3000{i:02d}X", "DeveloperName": f"Extra_Flow_{i}", "NamespacePrefix": None,
             "ActiveVersionId": "BROKEN" if i == 7 else f"3010{i:02d}X",
             "ActiveVersion": {"VersionNumber": 1, "ApiVersion": 64.0, "Status": "Active"}}
            for i in range(24)]
        mock_salesforce.STATS.update(composite_calls=0, flow_gets=0)
        s_f, _ = _connect(c, "flows", url)
        st = dict(mock_salesforce.STATS)
        check("25 flows took 3 composite calls", st["composite_calls"] == 3, str(st))
        check("only the failed subrequest was retried on its own", st["flow_gets"] == 1, str(st))
        check("every flow made it into the knowledgebase", s_f.get("counts", {}).get("flows") == 25,
              str(s_f.get("counts")))

        mock_salesforce.COMPOSITE_SUPPORTED = False
        mock_salesforce.STATS.update(composite_calls=0, flow_gets=0)
        s_f2, _ = _connect(c, "flows2", url)
        mock_salesforce.COMPOSITE_SUPPORTED = True
        check("an org without Composite falls back to one GET per flow",
              s_f2.get("counts", {}).get("flows") == 25 and mock_salesforce.STATS["flow_gets"] == 25,
              f"{s_f2.get('counts')} {mock_salesforce.STATS}")
        check("and says so once", sum("composite" in w for w in s_f2.get("warnings", [])) == 1,
              str(s_f2.get("warnings")))
        mock_salesforce.EXTRA_FLOWS[:] = []

        print("\n-- adaptive concurrency backs off when the org says 'too many' --")
        mock_salesforce.EXTRA_CLASSES[:] = [
            {"Id": f"01pY{i:04d}", "Name": f"Busy{i}", "NamespacePrefix": None, "ApiVersion": 60.0,
             "Body": f"public class Busy{i} {{ }}"} for i in range(200)]
        mock_salesforce.LATENCY = 0.05
        mock_salesforce.CONCURRENCY_CAP = 3
        mock_salesforce.STATS.update(throttled=0)
        old_chunk = sf_client.ID_CHUNK
        sf_client.ID_CHUNK = 10
        try:
            s_a, _ = _connect(c, "busy", url)
        finally:
            sf_client.ID_CHUNK = old_chunk
            mock_salesforce.CONCURRENCY_CAP = None
            mock_salesforce.LATENCY = 0.0
            mock_salesforce.EXTRA_CLASSES[:] = []
        lim = (s_a.get("fetch_stats") or {}).get("concurrency_adaptive") or {}
        check("the fetch still completes with nothing missing",
              s_a.get("status") == "done" and s_a.get("counts", {}).get("classes") == 207
              and not [w for w in s_a.get("warnings", []) if "chunk" in w],
              f"{s_a.get('counts')} {s_a.get('warnings')}")
        check("the limiter cut itself back", lim.get("lowest", 99) <= 4 and lim.get("throttle_events", 0) >= 1,
              str(lim))
        check("the org actually throttled us (so the test means something)",
              mock_salesforce.STATS["throttled"] > 0, str(mock_salesforce.STATS))

        print("\n-- Apex parsing in a process pool --")
        old_w, old_min = chunk_parse.PARSE_WORKERS, chunk_parse.POOL_MIN_CLASSES
        chunk_parse.PARSE_WORKERS, chunk_parse.POOL_MIN_CLASSES = 2, 1
        try:
            s_p, _ = _connect(c, "pooled", url)
        finally:
            chunk_parse.PARSE_WORKERS, chunk_parse.POOL_MIN_CLASSES = old_w, old_min
        check("parsing ran in worker processes",
              (s_p.get("fetch_stats") or {}).get("parse_mode") == "processes x2", str(s_p.get("fetch_stats")))
        a = c.get("/api/orgs/par1/stats").json().get("counts")
        b = c.get("/api/orgs/pooled/stats").json().get("counts")
        check("and produced the same knowledgebase as threads", a == b, f"{a} vs {b}")
        inb = c.get("/api/orgs/pooled/inbound/C2C_ActivateCPIOrderQueuable").json().get("called_by", [])
        check("including cross-class edges", any(r.get("id") == "FinaliseCartBatch" for r in inb), str(inb))

        print("\n-- resilience --")
        old_attempts = sf_client.MAX_ATTEMPTS
        mock_salesforce.FAIL_NEXT_BODY_QUERIES = 2
        s, _ = _connect(c, "flaky", url)
        check("a 503 on a chunk is retried, not turned into a gap",
              s.get("status") == "done" and (s.get("fetch_stats") or {}).get("retries", 0) >= 2
              and s.get("counts", {}).get("classes") == 7, str(s)[:300])
        mock_salesforce.FAIL_NEXT_BODY_QUERIES = 0

        mock_salesforce.REJECT_BODY_QUERIES = True
        s, t = _connect(c, "revoked", url)
        mock_salesforce.REJECT_BODY_QUERIES = False
        check("a token rejected mid-fetch fails the whole fetch cleanly",
              s.get("status") == "error" and "401" in (s.get("detail") or ""), str(s)[:300])
        check("and does so promptly", t < 10, f"{t:.1f}s")
        sf_client.MAX_ATTEMPTS = old_attempts

        print("\n-- benchmark: serial vs parallel against a slow org --")
        mock_salesforce.EXTRA_CLASSES[:] = [
            {"Id": f"01pX{i:04d}", "Name": f"Synthetic{i}", "NamespacePrefix": None, "ApiVersion": 60.0,
             "Body": f"public class Synthetic{i} {{ public void run(){{ System.enqueueJob(new Synthetic{(i+1)%400}()); }} }}"}
            for i in range(400)]
        mock_salesforce.LATENCY = 0.15
        old_chunk, old_conc = sf_client.ID_CHUNK, sf_client.FETCH_CONCURRENCY
        sf_client.ID_CHUNK = 20            # 21 class chunks
        try:
            sf_client.FETCH_CONCURRENCY = 1
            s1, t_serial = _connect(c, "serial", url)
            sf_client.FETCH_CONCURRENCY = 8
            s8, t_par = _connect(c, "parallel", url)
        finally:
            sf_client.ID_CHUNK, sf_client.FETCH_CONCURRENCY = old_chunk, old_conc
            mock_salesforce.LATENCY = 0.0
            mock_salesforce.EXTRA_CLASSES[:] = []
        print(f"     concurrency 1: {t_serial:.1f}s   concurrency 8: {t_par:.1f}s   "
              f"speed-up x{t_serial / max(t_par, 0.01):.1f}")
        check("both runs completed", s1.get("status") == s8.get("status") == "done")
        check("parallel is at least 3x faster", t_serial / max(t_par, 0.01) >= 3,
              f"{t_serial:.1f}s vs {t_par:.1f}s")
        a = c.get("/api/orgs/serial/stats").json().get("counts")
        b = c.get("/api/orgs/parallel/stats").json().get("counts")
        check("and builds the same knowledgebase", a == b, f"{a} vs {b}")
        inb = c.get("/api/orgs/parallel/inbound/Synthetic7").json().get("called_by", [])
        check("cross-chunk edges survive chunking", any(r.get("id") == "Synthetic6" for r in inb), str(inb))

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): " + ", ".join(FAILURES))
        return 1
    print("All parallel-fetch / async-dispatch checks passed.")
    return 0


def test_parallel_fetch_e2e():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())

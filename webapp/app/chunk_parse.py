"""
Pure, per-chunk parsers used by the streaming org fetch (onboarding.py), and
the process pool that runs the Apex ones on large orgs.

Kept in their own module, importing nothing but the extractors, so a worker
process spawned to parse Apex does not drag in storage, the web app, or the
HTTP client -- and so the same functions run unchanged in a thread or a
process.

Why a process pool
------------------
The Apex extractor is regex over source text: pure CPU, and it holds the GIL.
Worker THREADS keep the event loop responsive but still parse one chunk at a
time overall, so on a 20k-class org the parse -- not the network -- becomes
the long pole once fetching is parallel. Separate processes parse chunks
truly in parallel.

Processes are started with `spawn` on every platform (the Windows default;
on Linux `fork` from a process running an event loop and a thread pool can
deadlock the child). Spawning costs a second or so per worker, which is why
small orgs keep using threads (`POOL_MIN_CLASSES`).

The class-name set and object model -- needed by every chunk and the largest
arguments -- are sent once per worker through the pool initializer, not once
per chunk.
"""
import asyncio
import concurrent.futures
import hashlib
import multiprocessing
import os

from .common_now import iso_now
from .extractors import apex as apex_extractor
from .extractors import flow as flow_extractor
from .extractors import lwc as lwc_extractor
from .extractors import workflow as workflow_extractor

# Marker used as the "content" of a component whose source is not fetched
# (managed-package code). It is exactly what Salesforce returns as the Body
# of a managed class in a subscriber org, so an org fetched before managed
# code was skipped hashes identically and a refresh does not report every
# managed class as changed.
HIDDEN_BODY = "(hidden)"


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def default_workers():
    # One core is left for the web app itself. Measured on a 2-CPU box: 1,500
    # 280-line classes took 36s in threads and 20s in 2 worker processes.
    # A 1-CPU box still gets one worker: not for speed, but so the parse
    # watchdog below has a process it can kill.
    n = os.cpu_count() or 1
    return min(4, n - 1) if n >= 2 else 1


# 0 = always threads (and no watchdog). Unset = min(4, CPUs - 1), at least 1.
PARSE_WORKERS = _env_int("TS_PARSE_WORKERS", default_workers())
# Below this many classes to parse, a full pool's start-up cost outweighs the
# win -- such orgs get a single "guard" worker instead (see ApexParser).
POOL_MIN_CLASSES = _env_int("TS_PARSE_POOL_MIN_CLASSES", 800)

# Parse watchdog. The extractor is regex, and a pathological pattern/input
# pair can run for hours inside one C call that Python cannot interrupt --
# and `re` holds the GIL throughout, so in a worker THREAD it freezes the
# whole web app, not just the fetch. Only a process can be stopped, so the
# watchdog needs the pool: a chunk that overruns CHUNK_TIMEOUT has its worker
# killed, then its classes are re-parsed one at a time under CLASS_TIMEOUT so
# the one that hangs is found, stubbed and reported while the rest parse
# normally. Normal chunks (100 classes) take well under a second.
# 0 disables it.
CHUNK_TIMEOUT = _env_float("TS_PARSE_CHUNK_TIMEOUT", 120)
CLASS_TIMEOUT = _env_float("TS_PARSE_CLASS_TIMEOUT", 30)

# Test hook: with TS_PARSE_TEST_HANG=1 in the environment (inherited by the
# spawned workers), a class whose body contains this marker "hangs" in the
# parser, so tests can exercise the watchdog without a real pathological
# regex. Inert unless that variable is set.
TEST_HANG_MARKER = "__TS_PARSE_TEST_HANG__"


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def content_hash_entry(key, content, existing_hashes):
    h = sha(content)
    prev = existing_hashes.get(key)
    now = iso_now()
    if prev and prev.get("hash") == h:
        return prev
    return {"hash": h, "first_seen": (prev or {}).get("first_seen", now), "last_changed": now}


def apply_org_namespace(card, org_namespace):
    """A component in the org's OWN namespace is the customer's code, not a
    managed package's."""
    if org_namespace and card.get("namespace") == org_namespace:
        card["is_managed"] = False
        card["is_customer_authored"] = True
        card["in_org_namespace"] = True
    return card


# ---------- Apex ----------

def _apex_key(kind, name):
    folder, ext = ("classes", "cls") if kind == "class" else ("triggers", "trigger")
    return f"{folder}/{name}.{ext}", f"{name}.{ext}"


def parse_apex_chunk(kind, records, known_objects, all_class_names, existing_hashes, org_namespace=None):
    """kind: 'class' | 'trigger'. -> (cards, file_hashes, warnings)"""
    cards, hashes, warnings = {}, {}, []
    parse = apex_extractor.parse_class if kind == "class" else apex_extractor.parse_trigger
    test_hang = os.environ.get("TS_PARSE_TEST_HANG") == "1"
    for r in records:
        name, body = r.get("Name"), r.get("Body") or ""
        if not name or not body:
            continue
        if test_hang and TEST_HANG_MARKER in body:
            import time
            while True:
                time.sleep(3600)
        try:
            card = parse(name, body, known_objects, all_class_names,
                         namespace_prefix=r.get("NamespacePrefix"), api_version=r.get("ApiVersion"))
            key, fname = _apex_key(kind, name)
            card["file"] = fname
            cards[name] = apply_org_namespace(card, org_namespace)
            hashes[key] = content_hash_entry(key, body, existing_hashes)
        except Exception as e:
            warnings.append(f"{kind} '{name}': {e}")
    return cards, hashes, warnings


def timeout_apex_stub(kind, r, seconds, existing_hashes, org_namespace=None):
    """Card for a class/trigger whose source WAS fetched but whose analysis
    was stopped by the watchdog. Keeps the component findable (get_component
    says why it is empty instead of "no such component"), flags it with
    analysis_status='timeout', and keeps the content hash so a refresh after
    the extractor is fixed re-parses it like any other component."""
    from .schema import envelope, namespace_fields
    name, body = r["Name"], r.get("Body") or ""
    key, fname = _apex_key(kind, name)
    card = {
        "id": name, "type": "ApexClass" if kind == "class" else "ApexTrigger",
        **envelope(r.get("ApiVersion")), **namespace_fields(r.get("NamespacePrefix"), name),
        "analysis_status": "timeout",
        "source_note": f"source was fetched ({body.count(chr(10)) + 1} lines) but analysis did not finish "
                       f"within {seconds:g}s and was stopped, so its calls, SOQL, DML and field writes "
                       f"are NOT in the knowledgebase -- open the source in the org. This is an extractor "
                       f"bug; report the component name.",
        "file": fname, "loc": body.count("\n") + 1, "entry_points": [], "calls_to": [],
        "objects_referenced": [], "soql": [], "dml": [], "field_writes": [], "exceptions_caught": [],
        "exceptions_thrown": [], "callouts": [], "static_mutable_state": [], "async_dispatches": [],
    }
    if kind == "trigger":
        # The declaration regex is linear and only needs the head of the file.
        m = apex_extractor.TRIGGER_DECL_RE.search(apex_extractor.strip_comments(body[:5000]))
        card["object"] = m.group(2) if m else None
        card["events"] = [e.strip() for e in m.group(3).split(",")] if m else []
        if card["object"]:
            card["entry_points"].append({"kind": "Trigger",
                                         "detail": f"{card['object']} ({', '.join(card['events'])})"})
    else:
        card["methods"] = []
    return apply_org_namespace(card, org_namespace), {key: content_hash_entry(key, body, existing_hashes)}


def managed_apex_stubs(kind, rows, existing_hashes):
    """Cards for managed-package classes/triggers that are listed but not
    fetched. They keep the name in the knowledgebase -- so get_component on
    `Apttus_Config2.PricingCallback` says "managed, source hidden" instead of
    "no such component", and a managed trigger still appears in the object's
    entry points -- without spending a request on source Salesforce would not
    show us anyway."""
    from .schema import envelope, namespace_fields
    cards, hashes = {}, {}
    for r in rows:
        name = r.get("Name")
        if not name:
            continue
        key, fname = _apex_key(kind, name)
        card = {
            "id": name, "type": "ApexClass" if kind == "class" else "ApexTrigger",
            **envelope(None), **namespace_fields(r.get("NamespacePrefix"), name),
            "source_fetched": False,
            "source_note": "managed-package component: source is hidden in a subscriber org, so it "
                           "was listed but not fetched. Its internal calls, SOQL and field writes are "
                           "not in the knowledgebase.",
            "file": fname, "entry_points": [], "calls_to": [], "objects_referenced": [],
            "soql": [], "dml": [], "field_writes": [], "exceptions_caught": [],
            "static_mutable_state": [], "async_dispatches": [],
        }
        if kind == "trigger":
            table = r.get("TableEnumOrId")
            # standard objects come back by name, custom ones as a 01I... id
            obj = table if table and not str(table).startswith("01I") else None
            card["object"] = obj
            card["events"] = []
            if obj:
                card["entry_points"].append({"kind": "Trigger", "detail": f"{obj} (managed; events not visible)"})
        cards[name] = card
        hashes[key] = content_hash_entry(key, HIDDEN_BODY, existing_hashes)
    return cards, hashes


# ---------- Flow / LWC / Workflow (cheap; always threads) ----------

def parse_flow_batch(items, existing_hashes, org_namespace=None):
    """items: [(name, info)] -> (cards, hashes, warnings)"""
    cards, hashes, warnings = {}, {}, []
    for name, info in items:
        try:
            card = flow_extractor.parse_flow(
                name, info["metadata"], info.get("api_version"),
                version_info=info.get("version_info"), namespace_prefix=info.get("namespace_prefix"))
            card["file"] = f"{name}.flow"
            cards[name] = apply_org_namespace(card, org_namespace)
            key = f"flows/{name}.flow"
            hashes[key] = content_hash_entry(key, str(info["metadata"]), existing_hashes)
        except Exception as e:
            warnings.append(f"flow '{name}': {e}")
    return cards, hashes, warnings


def parse_lwc_chunk(bundles, existing_hashes):
    cards, hashes, warnings = {}, {}, []
    for name, files in bundles.items():
        try:
            cards[name] = lwc_extractor.parse_lwc(name, files)
            hashes[f"lwc/{name}"] = content_hash_entry(f"lwc/{name}", "".join(files.values()), existing_hashes)
        except Exception as e:
            warnings.append(f"lwc '{name}': {e}")
    return cards, hashes, warnings


def managed_lwc_stubs(rows, existing_hashes):
    from .schema import namespace_fields
    cards, hashes = {}, {}
    for b in rows:
        name = b.get("DeveloperName")
        if not name:
            continue
        cards[name] = {"id": name, "type": "LWC", **namespace_fields(b.get("NamespacePrefix"), name),
                       "source_fetched": False, "apex_methods_imported": [], "wire_adapters": [],
                       "child_components": [], "exposed_targets": [], "is_exposed": False,
                       "source_note": "managed-package component: listed, source not fetched."}
        hashes[f"lwc/{name}"] = content_hash_entry(f"lwc/{name}", HIDDEN_BODY, existing_hashes)
    return cards, hashes


def parse_workflow(workflow_raw, existing_hashes):
    cards, hashes, warnings = {}, {}, []
    for full_name, metadata in workflow_raw.items():
        try:
            card = workflow_extractor.parse_workflow_field_update(full_name, metadata)
            if card:
                cards[full_name] = card
                hashes[f"workflow/{full_name}"] = content_hash_entry(
                    f"workflow/{full_name}", str(metadata), existing_hashes)
        except Exception as e:
            warnings.append(f"workflow field update '{full_name}': {e}")
    return cards, hashes, warnings


# ---------- process pool ----------

_WORKER_CTX = {}


def _worker_init(known_objects, all_class_names, org_namespace):
    _WORKER_CTX["known_objects"] = known_objects
    _WORKER_CTX["all_class_names"] = all_class_names
    _WORKER_CTX["org_namespace"] = org_namespace


def _worker_parse(kind, records, existing_hashes):
    return parse_apex_chunk(kind, records, _WORKER_CTX["known_objects"], _WORKER_CTX["all_class_names"],
                            existing_hashes, _WORKER_CTX.get("org_namespace"))


def _worker_ping():
    return os.getpid()


class _PoolGone(Exception):
    """The pool was recycled and could not be restarted."""


def _kill_pool(pool):
    """Stop a pool NOW, including a worker stuck inside a C call.
    ProcessPoolExecutor has no public way to kill a busy worker, so this
    reaches for `_processes` (present on every CPython 3.x); shutdown alone
    would wait for -- or orphan -- the hung child."""
    if pool is None:
        return
    for proc in list((getattr(pool, "_processes", None) or {}).values()):
        try:
            proc.kill()
        except Exception:
            pass
    try:
        pool.shutdown(wait=False, cancel_futures=True)
    except Exception:
        pass


class ApexParser:
    """Parses Apex chunks in a process pool, under a watchdog, and falls
    back to threads -- with a warning -- if the pool cannot start or dies.
    Parsing is never lost to a pool problem; it just runs slower.

    Pool size: `workers` for an org with at least `min_classes` classes to
    parse; a single guard worker for a smaller org while the watchdog is on
    (a thread cannot be stopped, and a hung regex in one freezes the app);
    threads only when workers == 0, the watchdog is off on a small org, or
    the pool fails.

    Watchdog: a chunk that runs past `chunk_timeout` has its worker killed
    and the pool restarted; its classes are then re-parsed one at a time
    under `class_timeout`. The one(s) that overrun get a stub card
    (analysis_status='timeout') and a warning naming them; the rest parse
    normally, and the fetch finishes. Only as many chunks as there are
    workers are handed to the pool at once, so the timer measures parsing,
    never time spent queued behind other chunks.

    Use as `async with ApexParser(...) as p: await p.parse(kind, records, hashes)`.
    """

    def __init__(self, known_objects, all_class_names, n_to_parse, org_namespace=None,
                 workers=None, min_classes=None, chunk_timeout=None, class_timeout=None,
                 on_event=None):
        self.known_objects = known_objects
        self.all_class_names = all_class_names
        self.org_namespace = org_namespace
        self.workers = PARSE_WORKERS if workers is None else workers
        self.min_classes = POOL_MIN_CLASSES if min_classes is None else min_classes
        self.chunk_timeout = CHUNK_TIMEOUT if chunk_timeout is None else chunk_timeout
        self.class_timeout = CLASS_TIMEOUT if class_timeout is None else class_timeout
        self.n_to_parse = n_to_parse
        # optional callback(str) for live progress text ("isolating ...")
        self.on_event = on_event
        self.pool = None
        self.pool_size = 0
        self.mode = "threads"
        self.warnings = []
        self.timed_out = []       # "class Foo" / "trigger Bar" stopped by the watchdog
        self._gen = 0             # bumped on every pool recycle
        self._lock = None
        self._slots = None

    # ---- pool lifecycle ----

    def _new_pool(self, size):
        ctx = multiprocessing.get_context("spawn")
        return concurrent.futures.ProcessPoolExecutor(
            max_workers=size, mp_context=ctx, initializer=_worker_init,
            initargs=(self.known_objects, self.all_class_names, self.org_namespace))

    async def _warm(self, pool, size):
        """Start every worker and prove it imports cleanly, rather than
        discovering a broken pool on the first chunk."""
        try:
            loop = asyncio.get_running_loop()
            await asyncio.gather(*[loop.run_in_executor(pool, _worker_ping) for _ in range(size)])
        except BaseException:
            _kill_pool(pool)
            raise

    async def _start_pool(self, size):
        pool = self._new_pool(size)
        await self._warm(pool, size)
        return pool

    async def __aenter__(self):
        self._lock = asyncio.Lock()
        big = self.n_to_parse >= self.min_classes
        guarded = self.chunk_timeout > 0
        self._ready = None
        if self.workers > 0 and self.n_to_parse > 0 and (big or guarded):
            size = self.workers if big else 1
            try:
                self.pool = self._new_pool(size)
                self.pool_size = size
                self.mode = f"processes x{size}"
                # Workers spawn (~1s each on Windows) in the background while
                # the first chunks are still downloading; the first parse()
                # waits for this, so a watchdog timer never includes start-up.
                self._ready = asyncio.ensure_future(self._warm(self.pool, size))
            except Exception as e:
                self._drop_pool(f"parse process pool unavailable, using threads: {type(e).__name__}: {e}")
        if self.n_to_parse > 0 and (self.pool is None or not guarded):
            self.mode += " (no watchdog)"
        self._slots = asyncio.Semaphore(max(1, self.pool_size))
        return self

    async def _await_ready(self):
        ready = self._ready
        if ready is None:
            return
        try:
            await asyncio.shield(ready)       # several chunks may wait on the same start-up
        except Exception as e:
            if self.pool is not None:         # first waiter to see the failure drops the pool
                self._drop_pool(f"parse process pool unavailable, using threads: {type(e).__name__}: {e}")
                if self.chunk_timeout > 0:
                    self.mode += " (no watchdog)"
        self._ready = None

    async def __aexit__(self, *exc):
        # Kill rather than shut down: by now every chunk has returned or been
        # abandoned (a cancelled fetch), and a worker still busy in a hung
        # regex would otherwise outlive the fetch.
        ready, self._ready = self._ready, None
        if ready is not None and not ready.done():
            ready.cancel()
        _kill_pool(self.pool)
        self.pool = None
        if ready is not None:
            try:
                await ready                   # retrieve it, so no "exception never retrieved"
            except BaseException:
                pass
        return False

    def _drop_pool(self, why):
        self.warnings.append(why)
        _kill_pool(self.pool)
        self.pool = None
        self.mode = "threads (pool fallback)"

    async def _recycle(self, gen):
        """Kill the pool generation `gen` (if it is still the live one) and
        start a fresh one. Chunks that were running on the killed pool see
        BrokenProcessPool, notice the generation moved on, and resubmit."""
        async with self._lock:
            if gen != self._gen:
                return
            self._gen += 1
            old, self.pool = self.pool, None
            _kill_pool(old)
            try:
                self.pool = await self._start_pool(self.pool_size)
            except Exception as e:
                self._drop_pool(f"parse process pool could not be restarted after a timeout: "
                                f"{type(e).__name__}: {e}")

    async def _run_in_pool(self, kind, records, subset, timeout):
        """-> the chunk result, or None if it overran `timeout` (its worker
        has then been killed and the pool restarted). Raises _PoolGone if
        there is no pool to run on."""
        loop = asyncio.get_running_loop()
        while True:
            async with self._lock:          # waits out a restart in progress
                pool, gen = self.pool, self._gen
            if pool is None:
                raise _PoolGone()
            try:
                fut = loop.run_in_executor(pool, _worker_parse, kind, records, subset)
                if timeout and timeout > 0:
                    return await asyncio.wait_for(fut, timeout)
                return await fut
            except asyncio.TimeoutError:
                await self._recycle(gen)
                return None
            except (concurrent.futures.process.BrokenProcessPool, RuntimeError):
                # RuntimeError: "cannot schedule new futures after shutdown"
                if gen != self._gen:
                    continue                # we killed it for another chunk -- resubmit
                raise

    # ---- parsing ----

    def _event(self, msg):
        if self.on_event:
            try:
                self.on_event(msg)
            except Exception:
                pass

    async def parse(self, kind, records, existing_hashes):
        # Only the hash entries this chunk can touch cross the process
        # boundary, not the whole org's manifest.
        subset = {}
        for r in records:
            if r.get("Name"):
                key = _apex_key(kind, r["Name"])[0]
                if key in existing_hashes:
                    subset[key] = existing_hashes[key]
        await self._await_ready()
        if self.pool is not None:
            async with self._slots:
                try:
                    res = await self._run_in_pool(kind, records, subset, self.chunk_timeout)
                    if res is not None:
                        return res
                    return await self._isolate(kind, records, subset)
                except _PoolGone:
                    pass
                except concurrent.futures.process.BrokenProcessPool as e:
                    self._drop_pool(f"parse process pool died, continuing in threads: {e}")
                except Exception as e:
                    # e.g. a record that will not pickle -- parse this one chunk
                    # in a thread, keep the pool for the rest
                    self.warnings.append(f"{kind} chunk parsed in a thread after pool error: "
                                         f"{type(e).__name__}: {e}")
        return await asyncio.to_thread(parse_apex_chunk, kind, records, self.known_objects,
                                       self.all_class_names, subset, self.org_namespace)

    async def _isolate(self, kind, records, subset):
        """A chunk overran: parse its components one at a time so a single
        bad one costs only itself."""
        todo = [r for r in records if r.get("Name") and r.get("Body")]
        self.warnings.append(
            f"a {kind} chunk of {len(todo)} did not finish parsing within {self.chunk_timeout:g}s; "
            f"its worker was stopped and each {kind} in it re-parsed on its own "
            f"(limit {self.class_timeout:g}s each) to find the one that hangs")
        self._event(f"A {kind} chunk is taking too long to analyse -- checking its {len(todo)} "
                    f"{kind}(es) one at a time to find the one that hangs")
        cards, hashes, warns = {}, {}, []
        pool_lost = False
        for r in todo:
            name = r["Name"]
            key = _apex_key(kind, name)[0]
            one = {key: subset[key]} if key in subset else {}
            res = None
            if not pool_lost:
                try:
                    res = await self._run_in_pool(kind, [r], one, self.class_timeout)
                except (_PoolGone, concurrent.futures.process.BrokenProcessPool):
                    pool_lost = True
            if res is None:
                # Timed out -- or the pool is gone and a thread is not safe
                # here: this chunk is known to contain source that hangs.
                why = (f"analysis stopped after {self.class_timeout:g}s (the extractor hung on this "
                       f"source)" if not pool_lost else
                       "not analysed: the parse pool was lost while isolating a hung component")
                card, h = timeout_apex_stub(kind, r, self.class_timeout, one, self.org_namespace)
                if pool_lost:
                    card["analysis_status"] = "skipped"
                cards[name] = card
                hashes.update(h)
                warns.append(f"{kind} '{name}': {why}; it is in the knowledgebase as a stub "
                             f"(analysis_status='{card['analysis_status']}') with no calls/SOQL/DML")
                self.timed_out.append(f"{kind} {name}")
                if not pool_lost:
                    self._event(f"Stopped analysing {kind} {name} after {self.class_timeout:g}s "
                                f"(extractor hang) -- continuing with the rest")
            else:
                c, h, w = res
                cards.update(c)
                hashes.update(h)
                warns.extend(w)
        return cards, hashes, warns

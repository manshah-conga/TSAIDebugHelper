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


def default_workers():
    # One core is left for the web app itself. Measured on a 2-CPU box: 1,500
    # 280-line classes took 36s in threads and 20s in 2 worker processes.
    n = os.cpu_count() or 1
    return min(4, n - 1) if n >= 2 else 0


# 0 = always threads. Unset = min(4, CPUs - 1); threads on a 1-CPU box.
PARSE_WORKERS = _env_int("TS_PARSE_WORKERS", default_workers())
# Below this many classes to parse, a pool's start-up cost outweighs the win.
POOL_MIN_CLASSES = _env_int("TS_PARSE_POOL_MIN_CLASSES", 800)


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
    for r in records:
        name, body = r.get("Name"), r.get("Body") or ""
        if not name or not body:
            continue
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


class ApexParser:
    """Parses Apex chunks in a process pool when the org is big enough to
    be worth it, in a worker thread otherwise -- and falls back to threads,
    with a warning, if the pool cannot start or a worker dies. Parsing is
    never lost to a pool problem; it just runs slower.

    Use as `async with ApexParser(...) as p: await p.parse(kind, records, hashes)`.
    """

    def __init__(self, known_objects, all_class_names, n_to_parse, org_namespace=None,
                 workers=None, min_classes=None):
        self.known_objects = known_objects
        self.all_class_names = all_class_names
        self.org_namespace = org_namespace
        self.workers = PARSE_WORKERS if workers is None else workers
        self.min_classes = POOL_MIN_CLASSES if min_classes is None else min_classes
        self.n_to_parse = n_to_parse
        self.pool = None
        self.mode = "threads"
        self.warnings = []

    async def __aenter__(self):
        if self.workers > 0 and self.n_to_parse >= self.min_classes:
            try:
                ctx = multiprocessing.get_context("spawn")
                self.pool = concurrent.futures.ProcessPoolExecutor(
                    max_workers=self.workers, mp_context=ctx, initializer=_worker_init,
                    initargs=(self.known_objects, self.all_class_names, self.org_namespace))
                # Start the workers now (and prove they import cleanly) rather
                # than discovering a broken pool on the first chunk.
                loop = asyncio.get_running_loop()
                await asyncio.gather(*[loop.run_in_executor(self.pool, _worker_ping)
                                       for _ in range(self.workers)])
                self.mode = f"processes x{self.workers}"
            except Exception as e:
                self._drop_pool(f"parse process pool unavailable, using threads: {type(e).__name__}: {e}")
        return self

    async def __aexit__(self, *exc):
        if self.pool is not None:
            self.pool.shutdown(wait=False, cancel_futures=True)
            self.pool = None
        return False

    def _drop_pool(self, why):
        self.warnings.append(why)
        if self.pool is not None:
            try:
                self.pool.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
        self.pool = None
        self.mode = "threads (pool fallback)"

    async def parse(self, kind, records, existing_hashes):
        # Only the hash entries this chunk can touch cross the process
        # boundary, not the whole org's manifest.
        subset = {}
        for r in records:
            if r.get("Name"):
                key = _apex_key(kind, r["Name"])[0]
                if key in existing_hashes:
                    subset[key] = existing_hashes[key]
        if self.pool is not None:
            try:
                loop = asyncio.get_running_loop()
                return await loop.run_in_executor(self.pool, _worker_parse, kind, records, subset)
            except concurrent.futures.process.BrokenProcessPool as e:
                self._drop_pool(f"parse process pool died, continuing in threads: {e}")
            except Exception as e:
                # e.g. a record that will not pickle -- parse this one chunk
                # in a thread, keep the pool for the rest
                self.warnings.append(f"{kind} chunk parsed in a thread after pool error: "
                                     f"{type(e).__name__}: {e}")
        return await asyncio.to_thread(parse_apex_chunk, kind, records, self.known_objects,
                                       self.all_class_names, subset, self.org_namespace)

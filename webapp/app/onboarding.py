"""
Orchestrates one org connection end to end: fetch metadata from
Salesforce (in memory), run it through the extractors (in memory), build
the knowledgebase, hash each component's fetched content for
recently-changed tracking, and persist ONLY the derived JSON. The
fetched Apex/Flow/LWC source itself is never written to disk and is
dropped as soon as the chunk that carried it has been parsed.

Runs as a FastAPI BackgroundTask; progress is tracked in an in-memory
`JOBS` dict so the UI/API can poll status instead of blocking on what can
be a slow, multi-thousand-record fetch for a large org.

Parallel, streaming fetch
-------------------------
The fetch used to be strictly sequential: walk the ApexClass cursor page by
page, then triggers, then one GET per flow in a row, then one query per LWC
bundle in a row, and only when every byte had arrived, parse. On a large org
that is thousands of round trips back to back -- 80+ minutes of wall clock
that is almost entirely waiting on the network.

Now:

1. **List** (in parallel): the object model, ApexClass / ApexTrigger ids +
   sizes (no Body -- cheap), FlowDefinitions, LWC bundles.
2. **Fetch + parse** (in parallel, one shared cap on requests in flight --
   `sf_client.FETCH_CONCURRENCY`): Apex bodies in Id-list chunks sized by
   source length, each flow's metadata, LWC resources a chunk of bundles per
   query, workflow field updates. Each chunk is parsed in a worker thread
   the moment it lands and its source dropped, so parsing overlaps the
   network instead of following it, and peak memory is a few chunks rather
   than the whole org's source.
3. **Index + save**, as before -- this is the only step that needs every
   card at once.

Parsing a chunk needs only the full list of class NAMES (to classify call
targets) and the object model, both of which the listing step provides, so
no chunk waits on any other.

Progress reporting
------------------
Phases still declare a share of the bar (`STEPS`), but the long middle
phase is now several streams at once, so it reports per-stream `tracks`
(done / total for classes, triggers, flows, LWC, workflow) and the percentage
moves with their weighted sum. `progress_payload` is what the status route
returns and the UI's progress bar renders.
"""
import asyncio
import time
import traceback

from . import storage
from . import chunk_parse
from . import sf_client as _sf
from .sf_client import SalesforceClient, SalesforceAuthError, make_async_client
from .extractors import apex as apex_extractor
from .extractors import flow as flow_extractor
from .extractors import lwc as lwc_extractor
from .extractors import workflow as workflow_extractor
from .index_builder import build_index

JOBS = {}  # org_id -> {"status": str, "detail": str, "warnings": [...], ...}

# Phases in order, with the share of the bar each one owns and the label the
# UI shows.
STEPS = [
    ("connecting",  3, "Verifying the connection"),
    ("listing",     5, "Listing the org's components"),
    ("fetching",   80, "Fetching and analysing components (in parallel)"),
    ("indexing",    8, "Building the knowledgebase index"),
    ("saving",      4, "Saving"),
]
STEP_ORDER = [name for name, _w, _l in STEPS]
STEP_LABELS = {name: label for name, _w, label in STEPS}
# The terminal states are not phases, but they still need a label -- a panel
# that reaches 100% and then shows a bare "done" (or nothing at all) undercuts
# the confirmation the user waited minutes for.
STEP_LABELS["done"] = "Complete"
STEP_LABELS["error"] = "Failed"
STEP_LABELS["queued"] = "Queued"
STEP_WEIGHTS = {name: weight for name, weight, _l in STEPS}
_TOTAL_WEIGHT = sum(STEP_WEIGHTS.values())

# Streams inside the "fetching" phase and their share of it. Apex bodies are
# still the bulk of the bytes.
TRACKS = [
    ("classes",  56, "Apex classes"),
    ("triggers",  8, "Apex triggers"),
    ("flows",    20, "Flows / Process Builder"),
    ("lwc",      12, "Lightning components"),
    ("workflow",  4, "Workflow field updates"),
]
TRACK_LABELS = {n: l for n, _w, l in TRACKS}
_TRACK_WEIGHT = {n: w for n, w, _l in TRACKS}

# Percentage at which each phase BEGINS. A phase that is under way reports
# its start value, so the bar only ever moves forward.
_STEP_START = {}
_acc = 0
for _name, _weight, _label in STEPS:
    _STEP_START[_name] = round(_acc * 100 / _TOTAL_WEIGHT)
    _acc += _weight


def _job(org_id, status, detail="", counts=None):
    """Publish one progress update.

    Everything except `status` and `detail` is carried forward from the
    previous update, which is what lets a phase add a count ("412 classes")
    without having to restate the owner, the warnings, the tracks or the
    start time.
    """
    prev = JOBS.get(org_id, {})
    merged_counts = dict(prev.get("counts") or {})
    if counts:
        merged_counts.update(counts)
    tracks = prev.get("tracks") or {}
    JOBS[org_id] = {
        "status": status,
        "detail": detail,
        "warnings": prev.get("warnings", []),
        # who queued this job -- so /status on an org that has no
        # registry entry yet is still gated to that person + admins
        "owner": prev.get("owner"),
        "started_at": prev.get("started_at") or time.time(),
        "counts": merged_counts,
        "tracks": tracks,
        **({"fetch_stats": prev["fetch_stats"]} if prev.get("fetch_stats") else {}),
        "step_label": STEP_LABELS.get(status),
        "step_index": (STEP_ORDER.index(status) + 1) if status in STEP_ORDER else None,
        "step_count": len(STEP_ORDER),
        "percent": _percent(status, tracks),
    }
    return JOBS[org_id]


def _track(org_id, name, done=None, add=0, total=None, state=None):
    """Update one stream of the parallel phase and re-derive the percentage.
    Only ever called on the event loop (worker threads hand results back
    first), so there is no second writer to race."""
    job = JOBS.get(org_id)
    if job is None:
        return
    t = job.setdefault("tracks", {}).setdefault(
        name, {"label": TRACK_LABELS.get(name, name), "done": 0, "total": None, "state": "pending"})
    if total is not None:
        t["total"] = total
    if done is not None:
        t["done"] = done
    t["done"] += add
    if state:
        t["state"] = state
    elif t["state"] == "pending" and (t["done"] or t["total"] is not None):
        t["state"] = "active"
    if t["total"] is not None and t["done"] >= t["total"] and t["state"] != "failed":
        t["state"] = "done"
    job["percent"] = _percent(job.get("status"), job["tracks"])


def _tracks_fraction(tracks):
    if not tracks:
        return 0.0
    num = den = 0.0
    for name, w in _TRACK_WEIGHT.items():
        t = tracks.get(name)
        den += w
        if not t:
            continue
        if t.get("state") in ("done", "failed"):
            frac = 1.0
        elif t.get("total"):
            frac = min(1.0, t.get("done", 0) / t["total"])
        else:
            frac = 0.0
        num += w * frac
    return num / den if den else 0.0


def _percent(status, tracks=None):
    """Where the bar sits for a given status.

    `queued` is deliberately 0 and not None: a bar that renders nothing until
    the first phase lands reads as a broken page during the second or two
    before the background task is picked up.
    """
    if status == "done":
        return 100
    if status in ("error", "queued", "unknown"):
        return 0
    start = _STEP_START.get(status, 0)
    if status == "fetching" and tracks:
        span = STEP_WEIGHTS["fetching"] * 100 / _TOTAL_WEIGHT
        # never quite reach the next phase's start from inside this one
        return min(_STEP_START["indexing"] - 1, start + int(span * _tracks_fraction(tracks)))
    return start


def progress_payload(job):
    """The status shape the API returns and the UI renders. Strips `owner`
    (internal gating only) and adds elapsed seconds, computed here so every
    poll gets a fresh value without the job having to tick a timer."""
    if not job:
        return {"status": "unknown", "detail": "", "warnings": [], "percent": 0,
                "step_label": None, "step_index": None, "step_count": len(STEP_ORDER),
                "counts": {}, "tracks": [], "elapsed_seconds": None, "steps": _step_manifest()}
    out = {k: v for k, v in job.items() if k not in ("owner", "started_at", "tracks")}
    out.setdefault("percent", _percent(job.get("status"), job.get("tracks")))
    out.setdefault("counts", {})
    started = job.get("started_at")
    out["elapsed_seconds"] = round(time.time() - started, 1) if started else None
    # The full step list rides along so the UI can show every phase with a
    # tick against the finished ones, rather than just a bare percentage.
    out["steps"] = _step_manifest(job.get("status"))
    tracks = job.get("tracks") or {}
    out["tracks"] = [{"name": n, **tracks[n]} for n, _w, _l in TRACKS if n in tracks]
    return out


def _step_manifest(status=None):
    cur = STEP_ORDER.index(status) if status in STEP_ORDER else None
    out = []
    for i, (name, _w, label) in enumerate(STEPS):
        if status == "done":
            state = "done"
        elif cur is None:
            state = "pending"
        else:
            state = "done" if i < cur else "active" if i == cur else "pending"
        out.append({"name": name, "label": label, "starts_at": _STEP_START[name], "state": state})
    return out


# Chunk parsers live in chunk_parse.py (importable by a parse worker process
# without pulling in the web app). Old names kept for callers of this module.
_parse_apex_chunk = chunk_parse.parse_apex_chunk
_parse_lwc_chunk = chunk_parse.parse_lwc_chunk
_parse_workflow = chunk_parse.parse_workflow
_content_hash_entry = chunk_parse.content_hash_entry
_sha = chunk_parse.sha


def _err_text(e):
    msg = str(e).strip()
    return f"{type(e).__name__}: {msg}" if msg else type(e).__name__


async def _gather_or_cancel(coros):
    """Run coroutines concurrently. If one raises (only auth errors and
    listing failures are allowed to escape a task), cancel the rest and
    re-raise -- a rejected token must stop every request, not leave dozens of
    doomed ones running. (asyncio.TaskGroup does this, but needs 3.11.)"""
    tasks = [asyncio.ensure_future(c) for c in coros]
    try:
        return await asyncio.gather(*tasks)
    except BaseException:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def run_onboarding(org_id, org_name, instance_url, access_token, existing_hashes=None,
                         owner=None, visibility=None):
    """`owner` / `visibility` carry the per-org access settings (see
    app/org_access.py) through to the registry write at the end. On a
    re-connect of an existing org they are passed as the values already on
    record, so a refresh never silently changes who can see the org."""
    existing_hashes = existing_hashes or {}
    client_sf = SalesforceClient(instance_url, access_token)
    _job(org_id, "connecting")
    fetch_started = time.time()

    apex_cards, flow_cards, lwc_cards, workflow_cards = {}, {}, {}, {}
    file_hashes, warnings = {}, []

    def _count_apex():
        n_trig = sum(1 for c in apex_cards.values() if c.get("type") == "ApexTrigger")
        JOBS[org_id]["counts"]["classes"] = len(apex_cards) - n_trig
        JOBS[org_id]["counts"]["triggers"] = n_trig

    try:
        async with make_async_client(instance_url) as http_client:
            await client_sf.verify_connection()

            # ---- 1. listing, all at once ----
            _job(org_id, "listing")

            async def _soft(coro, label, default):
                try:
                    return await coro
                except SalesforceAuthError:
                    raise
                except Exception as e:
                    warnings.append(f"{label}: {_err_text(e)}")
                    return default

            (known_objects, class_list, trigger_list, flow_defs, lwc_bundles,
             org_ns) = await _gather_or_cancel([
                client_sf.fetch_custom_objects(http_client),
                # hard failures: no Apex listing means no knowledgebase worth saving
                client_sf.list_apex(http_client, "ApexClass"),
                client_sf.list_apex(http_client, "ApexTrigger"),
                _soft(client_sf.list_flow_definitions(http_client), "flow list", []),
                _soft(client_sf.list_lwc_bundles(http_client), "lwc bundle list", []),
                client_sf.fetch_org_namespace(http_client),
            ])
            # Every class NAME, managed or not -- a call into a managed class
            # is still a real edge and must classify as one.
            all_class_names = {r["Name"] for r in class_list if r.get("Name")}

            def _is_managed(row):
                ns = row.get("NamespacePrefix")
                return bool(ns) and ns != org_ns

            skip_code = not _sf.FETCH_MANAGED_CODE
            managed_classes = [r for r in class_list if skip_code and _is_managed(r)]
            managed_triggers = [r for r in trigger_list if skip_code and _is_managed(r)]
            lwc_bundles = [b for b in lwc_bundles if b.get("Id") and b.get("DeveloperName")]
            managed_lwc = [b for b in lwc_bundles if skip_code and _is_managed(b)]
            fetch_classes = [r for r in class_list if not (skip_code and _is_managed(r))]
            fetch_triggers = [r for r in trigger_list if not (skip_code and _is_managed(r))]
            fetch_lwc = [b for b in lwc_bundles if not (skip_code and _is_managed(b))]

            flow_defs = [d for d in flow_defs if d.get("ActiveVersionId") and d.get("DeveloperName")]
            skipped_flows = 0
            if _sf.SKIP_MANAGED_FLOWS:
                kept = [d for d in flow_defs if not _is_managed(d)]
                skipped_flows, flow_defs = len(flow_defs) - len(kept), kept

            # Managed components: a stub card each, straight from the listing.
            for kind, rows in (("class", managed_classes), ("trigger", managed_triggers)):
                cards, hashes = chunk_parse.managed_apex_stubs(kind, rows, existing_hashes)
                apex_cards.update(cards)
                file_hashes.update(hashes)
            cards, hashes = chunk_parse.managed_lwc_stubs(managed_lwc, existing_hashes)
            lwc_cards.update(cards)
            file_hashes.update(hashes)

            class_chunks = client_sf.chunk_ids(fetch_classes)
            trigger_chunks = client_sf.chunk_ids(fetch_triggers)
            step = _sf.LWC_BUNDLE_CHUNK
            lwc_chunks = [fetch_lwc[i:i + step] for i in range(0, len(fetch_lwc), step)]
            cstep = max(1, _sf.COMPOSITE_SIZE)
            flow_batches = [flow_defs[i:i + cstep] for i in range(0, len(flow_defs), cstep)]
            skipped = {"classes": len(managed_classes), "triggers": len(managed_triggers),
                       "lwc": len(managed_lwc), "flows": skipped_flows}

            _job(org_id, "fetching", counts={"objects": len(known_objects),
                                             "managed_skipped": sum(skipped.values())})
            _count_apex()
            _track(org_id, "classes", total=len(fetch_classes))
            _track(org_id, "triggers", total=len(fetch_triggers))
            _track(org_id, "flows", total=len(flow_defs))
            _track(org_id, "lwc", total=len(fetch_lwc))
            _track(org_id, "workflow", state="active")

            def _parse_event(msg):
                # surfaced live as the job's detail line while the watchdog works
                if org_id in JOBS:
                    JOBS[org_id]["detail"] = msg

            parser = chunk_parse.ApexParser(known_objects, all_class_names,
                                            len(fetch_classes) + len(fetch_triggers), org_namespace=org_ns,
                                            on_event=_parse_event)
            await parser.__aenter__()

            # ---- 2. fetch + parse, streaming ----
            async def apex_chunk(kind, ids, idx):
                sobject, track = ("ApexClass", "classes") if kind == "class" else ("ApexTrigger", "triggers")
                try:
                    recs = await client_sf.fetch_apex_chunk(http_client, sobject, ids)
                except SalesforceAuthError:
                    raise
                except Exception as e:
                    warnings.append(f"{kind} chunk {idx} ({len(ids)} ids) failed after retries: {_err_text(e)}")
                    _track(org_id, track, add=len(ids))
                    return
                cards, hashes, warns = await parser.parse(kind, recs, existing_hashes)
                del recs   # drop the source as soon as it is parsed
                apex_cards.update(cards)
                file_hashes.update(hashes)
                warnings.extend(warns)
                _track(org_id, track, add=len(ids))
                _count_apex()

            async def _flow_single(d):
                try:
                    return (await client_sf.fetch_flow_one(http_client, d))[1]
                except SalesforceAuthError:
                    raise
                except Exception as e:
                    warnings.append(f"flow '{d['DeveloperName']}': {_err_text(e)}")
                    return None

            async def flow_batch(defs):
                results = None
                if client_sf.composite_ok and len(defs) > 1:
                    try:
                        results = await client_sf.fetch_flow_batch(http_client, defs)
                    except _sf.CompositeUnsupported as e:
                        # the org (or API version) will not take composite at
                        # all: stop trying it for the rest of this fetch
                        if client_sf.composite_ok:
                            client_sf.composite_ok = False
                            warnings.append(f"flow composite requests unavailable, fetching flows "
                                            f"one by one: {e}")
                    except SalesforceAuthError:
                        raise
                    except Exception as e:
                        warnings.append(f"flow composite batch of {len(defs)} failed, retrying singly: "
                                        f"{_err_text(e)}")
                if results is None:
                    results = [(d, None) for d in defs]
                # anything the batch did not return is fetched on its own
                retry = [d for d, info in results if info is None]
                if retry:
                    singles = await asyncio.gather(*[_flow_single(d) for d in retry])
                    fixed = {d["DeveloperName"]: info for d, info in zip(retry, singles)}
                    results = [(d, info if info is not None else fixed.get(d["DeveloperName"]))
                               for d, info in results]
                items = [(d["DeveloperName"], info) for d, info in results if info is not None]
                cards, hashes, warns = await asyncio.to_thread(
                    chunk_parse.parse_flow_batch, items, existing_hashes, org_ns)
                flow_cards.update(cards)
                file_hashes.update(hashes)
                warnings.extend(warns)
                _track(org_id, "flows", add=len(defs))
                JOBS[org_id]["counts"]["flows"] = len(flow_cards)

            async def lwc_chunk(bundles, idx):
                try:
                    raw = await client_sf.fetch_lwc_chunk(http_client, bundles)
                except SalesforceAuthError:
                    raise
                except Exception as e:
                    warnings.append(f"lwc chunk {idx} ({len(bundles)} bundles): {_err_text(e)}")
                    _track(org_id, "lwc", add=len(bundles))
                    return
                cards, hashes, warns = await asyncio.to_thread(_parse_lwc_chunk, raw, existing_hashes)
                lwc_cards.update(cards)
                file_hashes.update(hashes)
                warnings.extend(warns)
                _track(org_id, "lwc", add=len(bundles))
                JOBS[org_id]["counts"]["lwc"] = len(lwc_cards)

            async def workflow_all():
                raw = await client_sf.fetch_workflow_field_updates(http_client)
                cards, hashes, warns = await asyncio.to_thread(_parse_workflow, raw, existing_hashes)
                workflow_cards.update(cards)
                file_hashes.update(hashes)
                warnings.extend(warns)
                _track(org_id, "workflow", total=len(raw), done=len(raw), state="done")
                JOBS[org_id]["counts"]["workflow_field_updates"] = len(workflow_cards)

            # Interleave the streams so no one kind queues behind another --
            # flows and LWC make progress from the first second, not after
            # every class chunk has gone through. (All tasks start at once;
            # the semaphore in SalesforceClient._get is what bounds requests
            # in flight, and it wakes waiters in creation order.)
            streams = [
                [apex_chunk("class", ids, i) for i, ids in enumerate(class_chunks)],
                [apex_chunk("trigger", ids, i) for i, ids in enumerate(trigger_chunks)],
                [flow_batch(b) for b in flow_batches],
                [lwc_chunk(b, i) for i, b in enumerate(lwc_chunks)],
                [workflow_all()],
            ]
            jobs = []
            while any(streams):
                for s in streams:
                    if s:
                        jobs.append(s.pop(0))
            try:
                await _gather_or_cancel(jobs)
            finally:
                await parser.__aexit__(None, None, None)
                warnings.extend(parser.warnings)
            for name in ("classes", "triggers", "flows", "lwc"):
                _track(org_id, name, state="done")

    except SalesforceAuthError as e:
        _job(org_id, "error", str(e) or repr(e))
        return
    except Exception as e:
        # Some exceptions (notably httpx connect/read timeouts) have an empty
        # str() -- always fall back to at least the exception class name so
        # the UI is never left showing a bare "Connection/fetch failed:" with
        # nothing after it. The full traceback goes to the server console
        # either way, for exact diagnosis.
        msg = str(e).strip()
        detail = f"{type(e).__name__}: {msg}" if msg else (
            f"{type(e).__name__} with no message -- usually means the request to "
            f"{instance_url} could not connect or timed out (wrong Instance URL, "
            f"org unreachable from this machine, or a corporate firewall/proxy "
            f"blocking the connection). Full traceback is in the server console."
        )
        print(f"[TS Debug Helper] org '{org_id}' onboarding failed:", flush=True)
        traceback.print_exc()
        _job(org_id, "error", f"Connection/fetch failed: {detail}")
        return

    warnings.extend(client_sf.warnings)
    JOBS[org_id]["warnings"].extend(warnings)
    fetch_stats = {
        "seconds": round(time.time() - fetch_started, 1),
        "requests": client_sf.requests_made, "retries": client_sf.retries,
        "concurrency": client_sf.concurrency,
        "concurrency_adaptive": client_sf.limiter.stats(),
        "flow_composite": client_sf.composite_ok and _sf.COMPOSITE_SIZE > 1,
        "parse_mode": parser.mode,
        # components the parse watchdog stopped (stub cards, analysis_status='timeout')
        "parse_timeouts": parser.timed_out,
        "managed_skipped": skipped,
        "org_namespace": org_ns,
    }
    JOBS[org_id]["fetch_stats"] = fetch_stats
    lim = fetch_stats["concurrency_adaptive"]
    print(f"[TS Debug Helper] org '{org_id}' fetched+parsed in {fetch_stats['seconds']}s "
          f"({fetch_stats['requests']} requests, {fetch_stats['retries']} retries, "
          f"concurrency {lim['max']} -> lowest {lim['lowest']} / {lim['throttle_events']} throttle "
          f"event(s), parse: {parser.mode}, managed not fetched: {sum(skipped.values())})", flush=True)
    extracted = {"apex_cards": apex_cards, "flow_cards": flow_cards, "lwc_cards": lwc_cards,
                 "workflow_cards": workflow_cards}
    coverage = _coverage(extracted, JOBS[org_id]["warnings"])

    _job(org_id, "indexing", counts={
        "components": len(apex_cards) + len(flow_cards) + len(lwc_cards) + len(workflow_cards),
    })
    index_result = await asyncio.to_thread(
        build_index, apex_cards, flow_cards, lwc_cards, workflow_cards, coverage)

    _job(org_id, "saving")
    # Writing the knowledgebase is a dozen json.dump calls over documents
    # that reach tens of megabytes on a large org -- also blocking, also off
    # the loop.
    await asyncio.to_thread(storage.save_kb, org_id, index_result, file_hashes)

    # What actually moved since the last fetch. Purely derived from the
    # content hashes we already keep, so a refresh can report "3 classes and
    # 1 flow changed" instead of just "done".
    changes = diff_hashes(existing_hashes, file_hashes)

    from .common_now import iso_now
    from . import org_access

    # Under a lock, and touching only this org's entry. Two engineers
    # finishing fetches of two different orgs within the same second used to
    # mean one of the two orgs was simply absent from the connected list
    # afterwards -- the second write replaced the whole document with a
    # snapshot taken before the first one landed.
    def _write_entry(registry):
        prior = registry.get(org_id, {})
        registry[org_id] = {
            "name": org_name,
            "instance_url": instance_url,
            # Access settings: keep whatever is already on record unless the
            # caller explicitly supplied new values.
            "owner": owner or prior.get("owner"),
            "visibility": org_access.normalize_visibility(
                visibility, default=prior.get("visibility") or org_access.DEFAULT_VISIBILITY),
            "first_onboarded_at": prior.get("first_onboarded_at", iso_now()),
            "last_extracted_at": iso_now(),
            "component_counts": index_result["org_stats"]["counts"],
            "warnings": list(JOBS[org_id]["warnings"]),
            "last_refresh_changes": changes,
            "last_fetch_stats": fetch_stats,
        }

    await asyncio.to_thread(storage.mutate_registry, _write_entry)

    _job(org_id, "done", detail=_done_detail(changes))
    JOBS[org_id]["changes"] = changes


def _done_detail(changes):
    """A one-line summary the progress panel can show on completion, so the
    end state says what happened rather than just 'done'."""
    if not changes:
        return "Knowledgebase built."
    if changes.get("first_connection"):
        return f"Indexed {changes.get('total', 0)} component(s)."
    moved = changes.get("changed", 0) + changes.get("added", 0) + changes.get("removed", 0)
    if not moved:
        return f"Nothing changed since the last fetch ({changes.get('total', 0)} checked)."
    return (f"{changes.get('changed', 0)} changed, {changes.get('added', 0)} new, "
            f"{changes.get('removed', 0)} removed.")


def job_in_flight(org_id):
    """True while a fetch for this org is running.

    Two people refreshing the same org at once is the one concurrency case
    the locking above cannot make safe: both fetches write the same
    knowledgebase files and the same `file_hashes` manifest, so the
    changed/added/removed report comes out of a comparison against a baseline
    the other job already moved. The routes use this to refuse the second
    request outright, which is both correct and kinder than letting it run
    and produce a quietly wrong answer.
    """
    job = JOBS.get(org_id)
    return bool(job) and job.get("status") not in ("done", "error", None)


def _extract_all(classes, triggers, flows_raw, lwc_raw, workflow_raw,
                 known_objects, existing_hashes):
    """Run every extractor. Pure and synchronous, so it can be handed to a
    worker thread.

    Warnings are collected into a local list and returned rather than
    appended straight onto the live JOBS entry, because this body no longer
    runs on the event loop -- writing into a dict that a polling request may
    be reading at the same moment is the kind of thing that works until it
    does not.
    """
    warnings = []
    all_class_names = set(classes.keys())
    apex_cards, flow_cards, lwc_cards, workflow_cards = {}, {}, {}, {}
    file_hashes = {}

    for name, info in classes.items():
        try:
            apex_cards[name] = apex_extractor.parse_class(
                name, info["body"], known_objects, all_class_names,
                namespace_prefix=info.get("namespace_prefix"), api_version=info.get("api_version"))
            apex_cards[name]["file"] = info["file"]
            file_hashes[f"classes/{info['file']}"] = _content_hash_entry(
                f"classes/{info['file']}", info["body"], existing_hashes)
        except Exception as e:
            warnings.append(f"class '{name}': {e}")

    for name, info in triggers.items():
        try:
            apex_cards[name] = apex_extractor.parse_trigger(
                name, info["body"], known_objects, all_class_names,
                namespace_prefix=info.get("namespace_prefix"), api_version=info.get("api_version"))
            apex_cards[name]["file"] = info["file"]
            file_hashes[f"triggers/{info['file']}"] = _content_hash_entry(
                f"triggers/{info['file']}", info["body"], existing_hashes)
        except Exception as e:
            warnings.append(f"trigger '{name}': {e}")

    for name, info in flows_raw.items():
        try:
            flow_cards[name] = flow_extractor.parse_flow(
                name, info["metadata"], info.get("api_version"),
                version_info=info.get("version_info"), namespace_prefix=info.get("namespace_prefix"))
            flow_cards[name]["file"] = f"{name}.flow"
            file_hashes[f"flows/{name}.flow"] = _content_hash_entry(
                f"flows/{name}.flow", str(info["metadata"]), existing_hashes)
        except Exception as e:
            warnings.append(f"flow '{name}': {e}")

    for name, files in lwc_raw.items():
        try:
            lwc_cards[name] = lwc_extractor.parse_lwc(name, files)
            combined = "".join(files.values())
            file_hashes[f"lwc/{name}"] = _content_hash_entry(f"lwc/{name}", combined, existing_hashes)
        except Exception as e:
            warnings.append(f"lwc '{name}': {e}")

    for full_name, metadata in workflow_raw.items():
        try:
            card = workflow_extractor.parse_workflow_field_update(full_name, metadata)
            if card:
                workflow_cards[full_name] = card
                file_hashes[f"workflow/{full_name}"] = _content_hash_entry(
                    f"workflow/{full_name}", str(metadata), existing_hashes)
        except Exception as e:
            warnings.append(f"workflow field update '{full_name}': {e}")

    return {"apex_cards": apex_cards, "flow_cards": flow_cards, "lwc_cards": lwc_cards,
            "workflow_cards": workflow_cards, "file_hashes": file_hashes,
            "warnings": warnings}


def _coverage(extracted, warnings):
    """Coverage block (§4.2): distinguishes a genuine zero from 'not
    collected'."""
    warns = " ".join(warnings).lower()

    def _cov(count, keyword=None):
        status = "partial" if (keyword and keyword in warns) else "ok"
        return {"attempted": True, "status": status, "count": count}

    apex_cards = extracted["apex_cards"]
    flow_cards = extracted["flow_cards"]
    n_classes = sum(1 for c in apex_cards.values() if c.get("type") == "ApexClass")
    n_triggers = sum(1 for c in apex_cards.values() if c.get("type") == "ApexTrigger")
    n_pb = sum(1 for c in flow_cards.values() if c.get("mechanism") == "Process Builder")
    n_flows_only = len(flow_cards) - n_pb
    return {
        "apex_classes": _cov(n_classes, "class"),
        "apex_triggers": _cov(n_triggers, "trigger"),
        "flows": _cov(n_flows_only, "flow"),
        "process_builder": _cov(n_pb),
        "lwc_components": _cov(len(extracted["lwc_cards"]), "lwc"),
        "workflow_field_updates": _cov(len(extracted["workflow_cards"]), "workflow"),
        "validation_rules": {"attempted": False, "status": "not_supported", "count": None},
        "record_types": {"attempted": False, "status": "not_supported", "count": None},
        "approval_processes": {"attempted": False, "status": "not_supported", "count": None},
    }


def diff_hashes(before, after, sample=8):
    """Compare two file_hashes manifests. Keys look like 'classes/Foo.cls',
    'flows/My_Flow.flow', 'lwc/myCmp' -- the prefix gives us a per-kind
    breakdown for free. `before` empty means this is a first connection, not
    a refresh, so everything counts as new rather than 'changed'."""
    before, after = before or {}, after or {}
    added = sorted(k for k in after if k not in before)
    removed = sorted(k for k in before if k not in after)
    changed = sorted(k for k in after if k in before
                     and after[k].get("hash") != before[k].get("hash"))

    def by_kind(keys):
        out = {}
        for k in keys:
            out[k.split("/", 1)[0]] = out.get(k.split("/", 1)[0], 0) + 1
        return out

    return {
        "first_connection": not before,
        "added": len(added), "changed": len(changed), "removed": len(removed),
        "unchanged": len(after) - len(added) - len(changed),
        "total": len(after),
        "added_by_kind": by_kind(added), "changed_by_kind": by_kind(changed),
        # A short sample so the UI can name names without carrying a manifest
        # of thousands of components around.
        "changed_sample": changed[:sample], "added_sample": added[:sample],
        "removed_sample": removed[:sample],
    }

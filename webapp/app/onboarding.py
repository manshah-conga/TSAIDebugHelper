"""
Orchestrates one org connection end to end: fetch metadata from
Salesforce (in memory), run it through the extractors (in memory), build
the knowledgebase, hash each component's fetched content for
recently-changed tracking, and persist ONLY the derived JSON. The
fetched Apex/Flow/LWC source itself is never written to disk and is
dropped as soon as this function returns.

Runs as a FastAPI BackgroundTask; progress is tracked in an in-memory
`JOBS` dict so the UI/API can poll status instead of blocking on what can
be a slow, multi-thousand-record fetch for a large org.

Progress reporting
------------------
A fetch of a large org runs for minutes. The status used to be a single word
("fetching_classes"), which told a waiting engineer nothing about whether
they were 10% or 90% through -- long enough to look indistinguishable from a
hang. Each phase now declares its share of the total (see `STEPS`), so the
job publishes a real percentage, a human label, per-phase counts as they
arrive, and elapsed time. `progress_payload` is what the status route
returns and the UI's progress bar renders.

The weights are measured, not even: fetching Apex bodies dominates a real
org, so a bar that gave every phase an equal slice would sit at 30% for
minutes and then sprint through the last five steps.

Running off the event loop
--------------------------
The extraction phase is pure CPU -- regex-parsing thousands of Apex classes
and Flow metadata documents. Inside an `async def` background task that
blocks the event loop, which in this app means every other user's requests
stop dead, including live chat streams, for as long as the parse takes. It
now runs in a worker thread via `asyncio.to_thread`, so one engineer
connecting a big org no longer freezes the app for everybody else.
"""
import asyncio
import hashlib
import time
import traceback

from . import storage
from .sf_client import SalesforceClient, SalesforceAuthError, make_async_client
from .extractors import apex as apex_extractor
from .extractors import flow as flow_extractor
from .extractors import lwc as lwc_extractor
from .extractors import workflow as workflow_extractor
from .index_builder import build_index

JOBS = {}  # org_id -> {"status": str, "detail": str, "warnings": [...], ...}

# Phases in order, with the share of the bar each one owns and the label the
# UI shows. The shares are weighted by how long each phase actually takes on
# a real org -- Apex bodies are by far the largest payload, LWC and workflow
# are quick.
STEPS = [
    ("connecting",        4,  "Verifying the connection"),
    ("fetching_objects",  6,  "Reading the object model"),
    ("fetching_classes", 32,  "Fetching Apex classes"),
    ("fetching_triggers", 8,  "Fetching Apex triggers"),
    ("fetching_flows",   18,  "Fetching Flows and Process Builder"),
    ("fetching_lwc",      7,  "Fetching Lightning components"),
    ("fetching_workflow", 5,  "Fetching Workflow field updates"),
    ("extracting",       12,  "Analysing customization"),
    ("indexing",          5,  "Building the knowledgebase index"),
    ("saving",            3,  "Saving"),
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

# Percentage at which each phase BEGINS. A phase that is under way reports
# its start value, so the bar only ever moves forward.
_STEP_START = {}
_acc = 0
for _name, _weight, _label in STEPS:
    _STEP_START[_name] = round(_acc * 100 / _TOTAL_WEIGHT)
    _acc += _weight


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _job(org_id, status, detail="", counts=None):
    """Publish one progress update.

    Everything except `status` and `detail` is carried forward from the
    previous update, which is what lets a phase add a count ("412 classes")
    without having to restate the owner, the warnings or the start time.
    """
    prev = JOBS.get(org_id, {})
    merged_counts = dict(prev.get("counts") or {})
    if counts:
        merged_counts.update(counts)
    JOBS[org_id] = {
        "status": status,
        "detail": detail,
        "warnings": prev.get("warnings", []),
        # who queued this job -- so /status on an org that has no
        # registry entry yet is still gated to that person + admins
        "owner": prev.get("owner"),
        "started_at": prev.get("started_at") or time.time(),
        "counts": merged_counts,
        "step_label": STEP_LABELS.get(status),
        "step_index": (STEP_ORDER.index(status) + 1) if status in STEP_ORDER else None,
        "step_count": len(STEP_ORDER),
        "percent": _percent(status),
    }
    return JOBS[org_id]


def _percent(status):
    """Where the bar sits for a given status.

    `queued` is deliberately 0 and not None: a bar that renders nothing until
    the first phase lands reads as a broken page during the second or two
    before the background task is picked up.
    """
    if status == "done":
        return 100
    if status in ("error", "queued", "unknown"):
        return 0
    return _STEP_START.get(status, 0)


def progress_payload(job):
    """The status shape the API returns and the UI renders. Strips `owner`
    (internal gating only) and adds elapsed seconds, computed here so every
    poll gets a fresh value without the job having to tick a timer."""
    if not job:
        return {"status": "unknown", "detail": "", "warnings": [], "percent": 0,
                "step_label": None, "step_index": None, "step_count": len(STEP_ORDER),
                "counts": {}, "elapsed_seconds": None, "steps": _step_manifest()}
    out = {k: v for k, v in job.items() if k not in ("owner", "started_at")}
    out.setdefault("percent", _percent(job.get("status")))
    out.setdefault("counts", {})
    started = job.get("started_at")
    out["elapsed_seconds"] = round(time.time() - started, 1) if started else None
    # The full step list rides along so the UI can show every phase with a
    # tick against the finished ones, rather than just a bare percentage.
    out["steps"] = _step_manifest()
    return out


def _step_manifest():
    return [{"name": name, "label": label, "starts_at": _STEP_START[name]}
            for name, _w, label in STEPS]


async def run_onboarding(org_id, org_name, instance_url, access_token, existing_hashes=None,
                         owner=None, visibility=None):
    """`owner` / `visibility` carry the per-org access settings (see
    app/org_access.py) through to the registry write at the end. On a
    re-connect of an existing org they are passed as the values already on
    record, so a refresh never silently changes who can see the org."""
    existing_hashes = existing_hashes or {}
    client_sf = SalesforceClient(instance_url, access_token)
    _job(org_id, "connecting")

    try:
        async with make_async_client(instance_url) as http_client:
            await client_sf.verify_connection()

            # Each phase reports its count on the way OUT, so the UI's
            # progress panel fills in with real numbers as the fetch walks
            # through the org instead of only at the end.
            _job(org_id, "fetching_objects")
            known_objects = await client_sf.fetch_custom_objects(http_client)

            _job(org_id, "fetching_classes", counts={"objects": len(known_objects)})
            classes = await client_sf.fetch_apex_classes(http_client)

            _job(org_id, "fetching_triggers", counts={"classes": len(classes)})
            triggers = await client_sf.fetch_apex_triggers(http_client)

            _job(org_id, "fetching_flows", counts={"triggers": len(triggers)})
            flows_raw = await client_sf.fetch_flows(http_client)

            _job(org_id, "fetching_lwc", counts={"flows": len(flows_raw)})
            lwc_raw = await client_sf.fetch_lwc(http_client)

            _job(org_id, "fetching_workflow", counts={"lwc": len(lwc_raw)})
            workflow_raw = await client_sf.fetch_workflow_field_updates(http_client)

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

    _job(org_id, "extracting", counts={"workflow_field_updates": len(workflow_raw)})

    # Parsing is pure CPU over what can be thousands of Apex bodies, and this
    # coroutine runs on the app's single event loop. Left inline it blocks
    # every other request in the process for the whole parse -- other users'
    # page loads, and any chat stream mid-answer. `to_thread` hands it to a
    # worker so the loop stays responsive. The extractors touch no shared
    # mutable state, so a thread is safe; they are also mostly `re`, which
    # releases the GIL rarely, but the loop only needs to run between
    # bytecode boundaries to keep serving.
    extracted = await asyncio.to_thread(
        _extract_all, classes, triggers, flows_raw, lwc_raw, workflow_raw,
        known_objects, existing_hashes)
    JOBS[org_id]["warnings"].extend(extracted["warnings"])
    coverage = _coverage(extracted, JOBS[org_id]["warnings"])
    file_hashes = extracted["file_hashes"]

    _job(org_id, "indexing", counts={
        "components": len(extracted["apex_cards"]) + len(extracted["flow_cards"])
                      + len(extracted["lwc_cards"]) + len(extracted["workflow_cards"]),
    })
    index_result = await asyncio.to_thread(
        build_index, extracted["apex_cards"], extracted["flow_cards"],
        extracted["lwc_cards"], extracted["workflow_cards"], coverage)

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


def _content_hash_entry(key, content, existing_hashes):
    from .common_now import iso_now
    h = _sha(content)
    prev = existing_hashes.get(key)
    now = iso_now()
    if prev and prev.get("hash") == h:
        return prev
    return {"hash": h, "first_seen": (prev or {}).get("first_seen", now), "last_changed": now}

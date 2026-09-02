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
"""
import hashlib
import traceback

from . import storage
from .sf_client import SalesforceClient, SalesforceAuthError, make_async_client
from .extractors import apex as apex_extractor
from .extractors import flow as flow_extractor
from .extractors import lwc as lwc_extractor
from .extractors import workflow as workflow_extractor
from .index_builder import build_index

JOBS = {}  # org_id -> {"status": str, "detail": str, "warnings": [...]}


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _job(org_id, status, detail=""):
    prev = JOBS.get(org_id, {})
    JOBS[org_id] = {"status": status, "detail": detail,
                    "warnings": prev.get("warnings", []),
                    # who queued this job -- so /status on an org that has no
                    # registry entry yet is still gated to that person + admins
                    "owner": prev.get("owner")}


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

            _job(org_id, "fetching_objects")
            known_objects = await client_sf.fetch_custom_objects(http_client)

            _job(org_id, "fetching_classes")
            classes = await client_sf.fetch_apex_classes(http_client)

            _job(org_id, "fetching_triggers")
            triggers = await client_sf.fetch_apex_triggers(http_client)

            _job(org_id, "fetching_flows")
            flows_raw = await client_sf.fetch_flows(http_client)

            _job(org_id, "fetching_lwc")
            lwc_raw = await client_sf.fetch_lwc(http_client)

            _job(org_id, "fetching_workflow")
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

    _job(org_id, "extracting")
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
            JOBS[org_id]["warnings"].append(f"class '{name}': {e}")

    for name, info in triggers.items():
        try:
            apex_cards[name] = apex_extractor.parse_trigger(
                name, info["body"], known_objects, all_class_names,
                namespace_prefix=info.get("namespace_prefix"), api_version=info.get("api_version"))
            apex_cards[name]["file"] = info["file"]
            file_hashes[f"triggers/{info['file']}"] = _content_hash_entry(
                f"triggers/{info['file']}", info["body"], existing_hashes)
        except Exception as e:
            JOBS[org_id]["warnings"].append(f"trigger '{name}': {e}")

    for name, info in flows_raw.items():
        try:
            flow_cards[name] = flow_extractor.parse_flow(
                name, info["metadata"], info.get("api_version"),
                version_info=info.get("version_info"), namespace_prefix=info.get("namespace_prefix"))
            flow_cards[name]["file"] = f"{name}.flow"
            file_hashes[f"flows/{name}.flow"] = _content_hash_entry(
                f"flows/{name}.flow", str(info["metadata"]), existing_hashes)
        except Exception as e:
            JOBS[org_id]["warnings"].append(f"flow '{name}': {e}")

    for name, files in lwc_raw.items():
        try:
            lwc_cards[name] = lwc_extractor.parse_lwc(name, files)
            combined = "".join(files.values())
            file_hashes[f"lwc/{name}"] = _content_hash_entry(f"lwc/{name}", combined, existing_hashes)
        except Exception as e:
            JOBS[org_id]["warnings"].append(f"lwc '{name}': {e}")

    for full_name, metadata in workflow_raw.items():
        try:
            card = workflow_extractor.parse_workflow_field_update(full_name, metadata)
            if card:
                workflow_cards[full_name] = card
                file_hashes[f"workflow/{full_name}"] = _content_hash_entry(
                    f"workflow/{full_name}", str(metadata), existing_hashes)
        except Exception as e:
            JOBS[org_id]["warnings"].append(f"workflow field update '{full_name}': {e}")

    # Coverage block (§4.2): distinguishes a genuine zero from "not collected".
    warns = " ".join(JOBS[org_id]["warnings"]).lower()

    def _cov(count, keyword=None):
        status = "partial" if (keyword and keyword in warns) else "ok"
        return {"attempted": True, "status": status, "count": count}

    n_classes = sum(1 for c in apex_cards.values() if c.get("type") == "ApexClass")
    n_triggers = sum(1 for c in apex_cards.values() if c.get("type") == "ApexTrigger")
    n_pb = sum(1 for c in flow_cards.values() if c.get("mechanism") == "Process Builder")
    n_flows_only = len(flow_cards) - n_pb
    coverage = {
        "apex_classes": _cov(n_classes, "class"),
        "apex_triggers": _cov(n_triggers, "trigger"),
        "flows": _cov(n_flows_only, "flow"),
        "process_builder": _cov(n_pb),
        "lwc_components": _cov(len(lwc_cards), "lwc"),
        "workflow_field_updates": _cov(len(workflow_cards), "workflow"),
        "validation_rules": {"attempted": False, "status": "not_supported", "count": None},
        "record_types": {"attempted": False, "status": "not_supported", "count": None},
        "approval_processes": {"attempted": False, "status": "not_supported", "count": None},
    }

    _job(org_id, "indexing")
    index_result = build_index(apex_cards, flow_cards, lwc_cards, workflow_cards, coverage=coverage)

    _job(org_id, "saving")
    storage.save_kb(org_id, index_result, file_hashes)

    # What actually moved since the last fetch. Purely derived from the
    # content hashes we already keep, so a refresh can report "3 classes and
    # 1 flow changed" instead of just "done".
    changes = diff_hashes(existing_hashes, file_hashes)

    registry = storage.load_registry()
    from .common_now import iso_now
    from . import org_access
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
    storage.save_registry(registry)

    _job(org_id, "done")
    JOBS[org_id]["changes"] = changes


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

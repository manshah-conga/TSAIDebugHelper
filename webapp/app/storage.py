"""
All disk I/O for the web app lives here, deliberately, so it's one place
to audit for the "only normalized JSON, never raw data" rule. Every write
in this module is a JSON document derived by the extractors/index
builder/log normalizer -- nothing here ever accepts raw Apex source, raw
Flow metadata, raw LWC file text, or a raw debug log body.

Concurrency
-----------
This app is multi-user: several engineers hit it from different machines at
once, and FastAPI runs every `def` route in a thread pool, so two requests
really do execute in parallel. Almost every store here is a whole-file JSON
document that gets read, modified and written back -- registry.json,
users.json, tokens.json, llm_keys.json, a chat's messages.json. That pattern
loses data under concurrency: two writers each read version N, each apply
their own change, and the second write erases the first one's.

`write_json` being atomic (temp file + os.replace) never fixed that. Atomic
means a reader never sees a half-written file; it says nothing about two
writers racing. The fix is `mutate_json`, which holds an exclusive lock
across the whole read-modify-write, and every caller that changes part of a
shared document now goes through it.

The lock is both inter-thread (a `threading.RLock` per path, for this
process's thread pool) and inter-process (`fcntl.flock` / `msvcrt.locking`
on a sidecar `.lock` file, so a second uvicorn worker -- or the stdio MCP
server running alongside -- cannot interleave either).

Acquisition is re-entrant per thread. Note what that is and is not for:
`write_json` itself takes no lock, so `mutate_json` calling it internally
was never the risk. Re-entrancy matters for a caller that holds `locked()`
over a whole read-rewrite sequence of its own (`usage.forget_user` does) and
calls something inside that locks the same path again.

Two rules for anything added here:

* Anything that computes a new value from the old one goes through
  `mutate_json`. `write_json`, `save_users`, `save_registry` and friends are
  correct ONLY for replacing a document outright.
* A mutator passed to `mutate_json` changes its argument IN PLACE. Its return
  value is ignored, which is what makes `lambda d: d.pop(k, None)` safe
  rather than a whole-document wipe. See `mutate_json` for the bug that rule
  exists to prevent.
"""
import copy
import os
import json
import glob
import time
import tempfile
import threading
from contextlib import contextmanager

try:                                    # POSIX
    import fcntl
    _HAVE_FCNTL = True
except ImportError:                     # Windows
    fcntl = None
    _HAVE_FCNTL = False

try:                                    # Windows
    import msvcrt
    _HAVE_MSVCRT = True
except ImportError:
    msvcrt = None
    _HAVE_MSVCRT = False

DATA_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
ORGS_ROOT = os.path.join(DATA_ROOT, "orgs")
REGISTRY_PATH = os.path.join(DATA_ROOT, "registry.json")
# Standalone (org-independent) normalized-log library. A log lands here when a
# user just wants to normalize + keep a log for reference without tying it to a
# connected org's knowledgebase. As everywhere else, only the derived
# normalized JSON is stored -- never the raw log text.
LOGS_ROOT = os.path.join(DATA_ROOT, "normalized_logs")
# Auth store: local user accounts (with salted password hashes) and API
# tokens (stored as hashes, never in the clear). This is app-access data, not
# anything fetched from Salesforce, so it does not affect the "only normalized
# JSON, never raw org data" guarantee.
AUTH_ROOT = os.path.join(DATA_ROOT, "auth")
USERS_PATH = os.path.join(AUTH_ROOT, "users.json")
TOKENS_PATH = os.path.join(AUTH_ROOT, "tokens.json")
# LLM quota policy: the per-tier token allowances an admin edits from the UI.
# Deliberately a stored document rather than environment variables, because
# changing a cap must not require an operator with shell access and a
# service restart -- see app/limits.py.
# Per-user LLM usage ledger. Append-only JSONL, one file per UTC day -- see
# app/usage.py for why that shape was chosen over a single JSON document.
USAGE_ROOT = os.path.join(DATA_ROOT, "usage")


def limits_path():
    """Late-bound, like `usage_root`: no module constant to keep in sync.

    The older stores each have a module-level constant that every test has to
    remember to repoint at its scratch directory (see the list in
    `tests/test_org_visibility.py`). Deriving this one at call time means a
    test that moves `DATA_ROOT` gets the quota store moved with it for free,
    and nothing can write policy into the real data directory by accident."""
    return os.path.join(DATA_ROOT, "auth", "limits.json")


def load_limits():
    return read_json(limits_path(), {})


def mutate_limits(mutator):
    """One admin raising a tier default while another sets a per-user
    override are two writes to the same document. Same lock discipline as
    every other shared store here."""
    return mutate_json(limits_path(), mutator, {})


def usage_root():
    """Derived at call time, not import time.

    The tests point `DATA_ROOT` at a scratch directory *after* importing this
    module, so anything precomputed from it at import would still reference
    the real data directory -- which is how a test run ends up writing into
    production data. The module-level constants above are kept for
    compatibility; new code should call the accessors."""
    return os.path.join(DATA_ROOT, "usage")


def activity_root():
    """Per-day action ledger (app/activity.py). Late-bound like `usage_root`
    so a test that repoints DATA_ROOT after import is honoured."""
    return os.path.join(DATA_ROOT, "activity")


def guide_path(username):
    """Per-user onboarding state and UI preferences (see app/guide.py).

    One small document per account rather than a field on users.json: it is
    written on ordinary clicks -- a tab visited, an org pinned -- and
    users.json holds the password hashes, which should be rewritten as rarely
    as possible. Late-bound for the same reason as `usage_root`. Usernames are
    already restricted to a path-safe charset by auth.USERNAME_RE."""
    return os.path.join(DATA_ROOT, "guide", f"{username}.json")


def load_guide(username):
    return read_json(guide_path(username), {})


def mutate_guide(username, mutator):
    return mutate_json(guide_path(username), mutator, {})


def forget_guide(username):
    try:
        os.remove(guide_path(username))
    except FileNotFoundError:
        pass


def locks_root():
    """Locks live in one subdirectory of the data root, so they never show up
    in a glob over orgs/ or chats/. Same late-binding reason as
    `usage_root`."""
    return os.path.join(DATA_ROOT, ".locks")


def _ensure_dirs():
    os.makedirs(ORGS_ROOT, exist_ok=True)


# =====================================================================
# file locking
# =====================================================================

_THREAD_LOCKS = {}
_THREAD_LOCKS_GUARD = threading.Lock()
# Re-entrancy bookkeeping: the OS-level lock must be taken exactly once per
# outermost acquisition, even though the thread-level RLock happily nests.
_DEPTH = threading.local()


def _lock_path(path):
    """One lock file per guarded document, named by a flattened form of its
    path so two different stores never share a lock (which would serialise
    unrelated writes) and the same store always finds the same one."""
    root = locks_root()
    os.makedirs(root, exist_ok=True)
    rel = os.path.relpath(os.path.abspath(path), DATA_ROOT)
    flat = rel.replace(os.sep, "__").replace("/", "__").replace(":", "_").lstrip(".")
    return os.path.join(root, flat + ".lock")


def _thread_lock(path):
    key = os.path.abspath(path)
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _THREAD_LOCKS[key] = lock
        return lock


def _depths():
    d = getattr(_DEPTH, "counts", None)
    if d is None:
        d = {}
        _DEPTH.counts = d
    return d


@contextmanager
def locked(path, timeout=30.0):
    """Exclusive lock over one JSON document, across threads AND processes.

    Re-entrant within a thread: nesting only increments a counter, so a
    caller that holds this over a whole read-rewrite sequence of its own
    (`usage.forget_user`) and calls something that locks the same path again
    does not deadlock on itself.

    A lock that cannot be taken within `timeout` is a bug elsewhere -- a
    crashed process holding a stale flock, most likely -- and blocking
    forever would hang a worker thread permanently. Time out and proceed
    unlocked instead: a lost update is bad, a wedged server is worse, and
    the console line says which happened.
    """
    key = os.path.abspath(path)
    depths = _depths()
    tlock = _thread_lock(key)
    # Timed, not indefinite. Nothing in the app currently nests locks over two
    # different paths, so there is no lock-order inversion to deadlock on --
    # but an untimed acquire means the first one anyone adds hangs a worker
    # thread permanently and silently. This turns that into a loud line in the
    # log and a served request.
    if not tlock.acquire(timeout=timeout):
        print(f"[TS Debug Helper] timed out waiting {timeout:.0f}s for the in-process lock on "
              f"{os.path.basename(path)}. This should not happen -- suspect a nested lock over "
              f"two different files.", flush=True)
        raise TimeoutError(f"could not acquire the lock on {path} within {timeout}s")
    nested = depths.get(key, 0) > 0
    depths[key] = depths.get(key, 0) + 1
    handle = None
    try:
        if not nested:
            handle = _acquire_os_lock(key, timeout)
        yield
    finally:
        if handle is not None:
            _release_os_lock(handle)
        depths[key] = depths.get(key, 1) - 1
        if depths[key] <= 0:
            depths.pop(key, None)
        tlock.release()


def _acquire_os_lock(path, timeout):
    if not (_HAVE_FCNTL or _HAVE_MSVCRT):
        return None                                  # no OS primitive available
    try:
        handle = open(_lock_path(path), "a+b")
    except OSError:
        return None                                  # read-only data dir; thread lock still holds
    deadline = time.monotonic() + timeout
    while True:
        try:
            if _HAVE_FCNTL:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return handle
        except OSError:
            if time.monotonic() >= deadline:
                print(f"[TS Debug Helper] timed out waiting {timeout:.0f}s for a lock on "
                      f"{os.path.basename(path)}; proceeding without the cross-process lock. "
                      f"A previous process may have died holding it.", flush=True)
                try:
                    handle.close()
                except OSError:
                    pass
                return None
            time.sleep(0.02)


def _release_os_lock(handle):
    try:
        if _HAVE_FCNTL:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        else:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    finally:
        try:
            handle.close()
        except OSError:
            pass


def mutate_json(path, mutator, default=None):
    """Read-modify-write one JSON document under an exclusive lock.

    This is the only safe way to change part of a shared document. `mutator`
    receives the current value and **mutates it in place**; whatever it leaves
    behind is written back before the lock is released, so a concurrent writer
    sees the finished version rather than the one this caller started from.

    **The mutator's return value is ignored, deliberately.** An earlier
    version treated any non-None return as a replacement document, which made
    a whole class of one-line mutators quietly catastrophic:

        mutate_json(path, lambda d: d.pop(key, None), {})

    reads as "remove this entry", and under that rule it replaced the entire
    document with whatever was popped -- so deleting one user's record wiped
    every other user's. Ignoring the return value makes the dangerous version
    behave identically to the safe one, which is better than documenting the
    difference and hoping.

    A caller that genuinely wants to replace the document clears and refills
    the value it was handed (`d.clear(); d.update(new)`), which says so
    unambiguously at the call site.

    Returns the value that was written, so a caller can use the post-mutation
    state without a second read.
    """
    with locked(path):
        current = read_json(path, None)
        if current is None:
            # A fresh copy, never the caller's `default` object -- mutating a
            # literal passed in at the call site would be harmless, but
            # mutating a shared module-level default would not.
            current = copy.deepcopy(default) if default is not None else {}
        mutator(current)
        write_json(path, current)
        return current


def load_users():
    return read_json(USERS_PATH, {})


def save_users(users):
    write_json(USERS_PATH, users)


def mutate_users(mutator):
    """Change one entry in users.json without losing a concurrent change to
    another entry. Two admins creating users at the same moment used to mean
    one of those accounts silently never existed."""
    return mutate_json(USERS_PATH, mutator, {})


def load_tokens():
    return read_json(TOKENS_PATH, {})


def save_tokens(tokens):
    write_json(TOKENS_PATH, tokens)


def mutate_tokens(mutator):
    """tokens.json is the hottest shared document in the app: every
    authenticated request may stamp `last_used` on its own token, and every
    login adds one. A read-modify-write without this lock drops tokens --
    which logs people out at random."""
    return mutate_json(TOKENS_PATH, mutator, {})


def read_json(path, default=None):
    """A lone read needs no lock: `write_json` swaps the file in atomically,
    so a reader sees either the old document or the new one, never a torn
    one. A read that is the first half of a read-modify-write is a different
    matter entirely -- that one must go through `mutate_json`."""
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        # A truncated document means a process died mid-write in some older
        # version, or something outside this app edited it. Returning the
        # default keeps the app up; the alternative is a 500 on every
        # request that touches this store.
        print(f"[TS Debug Helper] {path} is not valid JSON; treating it as empty.", flush=True)
        return default


def write_json(path, obj):
    """Atomic write via a UNIQUE temp file in the same directory, then
    os.replace. The temp name must be unique per write: a fixed `path + '.tmp'`
    breaks under concurrent requests (two writers clobber the same temp and one
    os.replace then fails with PermissionError on Windows). os.replace is also
    retried briefly because Windows can transiently lock the destination
    (antivirus / search indexer) right as we swap it in.

    Atomic is not the same as serialised. This function guarantees no reader
    sees a partial file; it does NOT stop two writers from clobbering each
    other's changes. Anything that computes its new value from the old one
    must use `mutate_json`."""
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2)
        last_err = None
        for attempt in range(10):
            try:
                os.replace(tmp, path)
                return
            except PermissionError as e:  # transient Windows lock; back off and retry
                last_err = e
                time.sleep(0.05 * (attempt + 1))
        raise last_err
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def append_jsonl(path, record):
    """Append one record as a single line, under the same lock discipline as
    the JSON documents.

    An append-only log is the right shape for the usage ledger precisely
    because it has no read-modify-write step: nothing already on disk is
    revisited, so a concurrent writer cannot erase an earlier entry the way
    it can with a whole-document rewrite. The lock is still taken, because
    two interleaved partial line writes would corrupt both records.
    """
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    line = json.dumps(record, default=str, separators=(",", ":")) + "\n"
    with locked(path):
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()


def read_jsonl(path):
    """Every well-formed record in an append-only log. A malformed final line
    (a process killed mid-append) is skipped rather than failing the read --
    losing one usage record must never take out the whole usage report."""
    if not os.path.exists(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def org_dir(org_id):
    return os.path.join(ORGS_ROOT, org_id)


def kb_dir(org_id):
    return os.path.join(org_dir(org_id), "knowledge_base")


def incidents_dir(org_id):
    return os.path.join(org_dir(org_id), "incidents")


def load_registry():
    _ensure_dirs()
    return read_json(REGISTRY_PATH, {})


def save_registry(registry):
    write_json(REGISTRY_PATH, registry)


def mutate_registry(mutator):
    """The registry holds every org's name, owner and visibility in one
    document, so two people connecting or refreshing different orgs at the
    same time were racing each other. Whole-document writes are why one of
    those orgs would occasionally vanish from the list."""
    _ensure_dirs()
    return mutate_json(REGISTRY_PATH, mutator, {})


def load_kb(org_id):
    """Loads every knowledgebase JSON document for an org. Returns None
    for any file not yet written (e.g. mid-fetch)."""
    d = kb_dir(org_id)
    idx = os.path.join(d, "_indexes")
    return {
        "org_index": read_json(os.path.join(d, "org_index.json"), {}),
        "object_touch_map": read_json(os.path.join(d, "object_touch_map.json"), {}),
        "call_graph": read_json(os.path.join(d, "call_graph.json"), {"calls": {}, "called_by": {}}),
        "field_touch_map": read_json(os.path.join(d, "field_touch_map.json"), {}),
        "inbound_index": read_json(os.path.join(idx, "inbound.json"), {}),
        "entry_points_index": read_json(os.path.join(idx, "entry_points.json"), {}),
        "org_stats": read_json(os.path.join(d, "org_stats.json"), {}),
        "file_hashes": read_json(os.path.join(d, "file_hashes.json"), {}),
    }


def save_kb(org_id, index_result, file_hashes):
    d = kb_dir(org_id)
    idx = os.path.join(d, "_indexes")
    write_json(os.path.join(d, "org_index.json"), index_result["org_index"])
    write_json(os.path.join(d, "object_touch_map.json"), index_result["object_touch_map"])
    write_json(os.path.join(d, "call_graph.json"), index_result["call_graph"])
    write_json(os.path.join(d, "field_touch_map.json"), index_result["field_touch_map"])
    write_json(os.path.join(idx, "inbound.json"), index_result.get("inbound_index", {}))
    write_json(os.path.join(idx, "entry_points.json"), index_result.get("entry_points_index", {}))
    write_json(os.path.join(d, "org_stats.json"), index_result["org_stats"])
    write_json(os.path.join(d, "file_hashes.json"), file_hashes)


def load_known_issues(org_id):
    return read_json(os.path.join(incidents_dir(org_id), "known_issues_index.json"), {})


def save_known_issues(org_id, known):
    write_json(known_issues_path(org_id), known)


def known_issues_path(org_id):
    return os.path.join(incidents_dir(org_id), "known_issues_index.json")


def mutate_known_issues(org_id, mutator):
    """Two engineers filing incidents against the same org at the same moment
    both bump the recurrence count on this index. Unlocked, the second write
    discarded the first -- which is exactly the data this feature exists to
    accumulate."""
    return mutate_json(known_issues_path(org_id), mutator, {})


def save_incident(org_id, incident_id, normalized_log, context_pack, meta):
    d = os.path.join(incidents_dir(org_id), incident_id)
    write_json(os.path.join(d, "normalized_log.json"), normalized_log)
    write_json(os.path.join(d, "rca_context_pack.json"), context_pack)
    write_json(os.path.join(d, "meta.json"), meta)


def list_incidents(org_id):
    pattern = os.path.join(incidents_dir(org_id), "*")
    out = []
    for path in sorted(glob.glob(pattern), reverse=True):
        if os.path.isdir(path):
            meta = read_json(os.path.join(path, "meta.json"))
            if meta:
                out.append(meta)
    return out


def load_incident(org_id, incident_id):
    d = os.path.join(incidents_dir(org_id), incident_id)
    if not os.path.isdir(d):
        return None
    return {
        "meta": read_json(os.path.join(d, "meta.json")),
        "normalized_log": read_json(os.path.join(d, "normalized_log.json")),
        "rca_context_pack": read_json(os.path.join(d, "rca_context_pack.json")),
    }


# ---------- standalone normalized-log library (org-independent) ----------

def _log_dir(log_id):
    return os.path.join(LOGS_ROOT, log_id)


def save_normalized_log(log_id, normalized, meta):
    """Persist an org-independent normalized log. Only the derived JSON
    (`normalized`) and its metadata are written -- never the raw log."""
    d = _log_dir(log_id)
    write_json(os.path.join(d, "normalized_log.json"), normalized)
    write_json(os.path.join(d, "meta.json"), meta)


def list_normalized_logs():
    out = []
    for path in sorted(glob.glob(os.path.join(LOGS_ROOT, "*")), reverse=True):
        if os.path.isdir(path):
            meta = read_json(os.path.join(path, "meta.json"))
            if meta:
                out.append(meta)
    return out


def load_normalized_log(log_id):
    d = _log_dir(log_id)
    if not os.path.isdir(d):
        return None
    return {
        "meta": read_json(os.path.join(d, "meta.json")),
        "normalized_log": read_json(os.path.join(d, "normalized_log.json")),
    }


def read_log_meta(log_id):
    d = _log_dir(log_id)
    if not os.path.isdir(d):
        return None
    return read_json(os.path.join(d, "meta.json"))


def mutate_log_meta(log_id, mutator):
    """Archive / retag / relabel one stored log's meta.json under its lock,
    so an archive and a retag landing together both survive."""
    return mutate_json(os.path.join(_log_dir(log_id), "meta.json"), mutator, {})


def delete_normalized_log(log_id):
    """Remove one stored log (its normalized JSON + meta). Retries briefly
    because Windows can hold a transient lock on a file the indexer or an
    antivirus scanner is reading at that moment."""
    import shutil
    d = _log_dir(log_id)
    if not os.path.isdir(d):
        return False
    meta_path = os.path.join(d, "meta.json")
    with locked(meta_path):
        last_err = None
        for attempt in range(10):
            try:
                shutil.rmtree(d)
                return True
            except FileNotFoundError:
                return True
            except PermissionError as e:
                last_err = e
                time.sleep(0.05 * (attempt + 1))
        raise last_err

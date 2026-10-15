"""
Claude Desktop extension (.mcpb) download.

The package is built outside the app (desktop-extension/, `npx @anthropic-ai/mcpb
pack`) and dropped on the server by an operator. The app only serves it, so a
new extension build ships by copying one file -- no app restart, no rebuild.

Where the file is looked for:

  1. TS_MCPB_PATH, if set. Only that path -- an operator who set it wants to
     know when it is wrong, not to be silently served an older copy elsewhere.
  2. Otherwise the first that exists of:
       webapp/downloads/ts-debug-helper.mcpb          <- recommended on the VM
       <project>/desktop-extension/ts-debug-helper.mcpb   <- where `mcpb pack` writes it

The file is read fresh on each request (cheap: one stat, and the manifest is
re-read only when size/mtime change), so replacing it takes effect at once.
"""
import hashlib
import json
import os
import zipfile

FILENAME = "ts-debug-helper.mcpb"

_WEBAPP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PROJECT_DIR = os.path.dirname(_WEBAPP_DIR)

DEFAULT_CANDIDATES = [
    os.path.join(_WEBAPP_DIR, "downloads", FILENAME),
    os.path.join(_PROJECT_DIR, "desktop-extension", FILENAME),
]

_cache = {"key": None, "meta": None}


def configured_path():
    return (os.environ.get("TS_MCPB_PATH") or "").strip()


def candidate_paths():
    p = configured_path()
    return [os.path.abspath(os.path.expanduser(p))] if p else list(DEFAULT_CANDIDATES)


def resolve_path():
    """The file to serve, or None if none of the candidates is a regular file."""
    for p in candidate_paths():
        if os.path.isfile(p):
            return p
    return None


def _read_meta(path):
    """Version and checksum, taken from the package itself so the page can never
    advertise a version the file isn't. A file that is not a valid .mcpb (a zip
    with manifest.json) is reported as invalid rather than served."""
    meta = {"valid": False, "version": None, "display_name": None, "sha256": None, "error": None}
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    meta["sha256"] = h.hexdigest()
    try:
        with zipfile.ZipFile(path) as z:
            manifest = json.loads(z.read("manifest.json").decode("utf-8"))
        meta["version"] = manifest.get("version")
        meta["display_name"] = manifest.get("display_name") or manifest.get("name")
        meta["valid"] = True
    except KeyError:
        meta["error"] = "The file has no manifest.json -- it is not a packed .mcpb."
    except zipfile.BadZipFile:
        meta["error"] = "The file is not a zip archive -- it is not a packed .mcpb."
    except Exception as e:  # malformed JSON, unreadable entry, ...
        meta["error"] = f"Could not read the package manifest: {type(e).__name__}."
    return meta


def info(include_paths=False):
    """Payload for GET /api/desktop-extension. Paths are only included for
    admins: they are what an admin needs to fix a missing file, and nobody
    else needs to see the server's directory layout."""
    path = resolve_path()
    out = {"available": False, "filename": FILENAME}
    if include_paths:
        out["looked_in"] = candidate_paths()
        out["env_var"] = "TS_MCPB_PATH"
        out["configured"] = bool(configured_path())
    if not path:
        return out
    st = os.stat(path)
    key = (path, st.st_size, st.st_mtime_ns)
    if _cache["key"] != key:
        _cache["meta"] = _read_meta(path)
        _cache["key"] = key
    meta = _cache["meta"]
    out.update({
        "available": meta["valid"],
        "version": meta["version"],
        "display_name": meta["display_name"],
        "size": st.st_size,
        "modified": st.st_mtime,
        "sha256": meta["sha256"],
    })
    if meta["error"]:
        out["error"] = meta["error"]
    if include_paths:
        out["path"] = path
    return out


def download_path():
    """Path to stream, or None if there is nothing valid to serve."""
    meta = info()
    return resolve_path() if meta.get("available") else None

"""
Load configuration from a local env file into `os.environ`.

Why this exists
---------------
The shared LLM connection is read from environment variables (see
app/llm_config.py), and the documented way to supply them on the VM was
systemd's `EnvironmentFile=`. That is a **systemd directive**: systemd reads
the file and hands the values to the process. Nothing in this app read it, so
on Windows -- where the app is started by `start_server.bat` and there is no
systemd -- creating the file had no effect whatsoever, silently. The app
reported "no shared LLM connection configured" and the file sat there looking
correct.

So the app now loads it itself. The same file works under systemd (which
still exports it first, and wins -- see below), under the Windows launchers,
under a bare `python -m uvicorn`, and in the stdio MCP server.

Where it looks
--------------
If `$TS_ENV_FILE` is set, that path is the only one considered -- naming a
file and getting a different one loaded would be worse than getting nothing.
Otherwise, the first of these that exists wins:

    webapp/.env                        -- the conventional name
    webapp/etc/ts-debug-helper.env     -- matches the filename the systemd
                                          unit uses, so dev and prod
                                          configuration look the same

Real environment variables always win
-------------------------------------
A value already present in `os.environ` is never overwritten. That ordering
is deliberate and matters in three places:

  * under systemd, `EnvironmentFile=` has already exported everything, so
    this loader finds the variables set and changes nothing -- the file is
    read once, by systemd, exactly as before;
  * an operator can override one value for a single run
    (`set TS_LLM_LOCK_MODEL=0 && start_server.bat`) without editing a file;
  * the test suites set `TS_LLM_*` in `os.environ` before importing the app,
    and a developer's own `.env` must not be able to break them.

Format
------
`KEY=VALUE`, one per line. Blank lines and lines starting with `#` are
ignored. A leading `export ` is allowed, so a file copied from a shell script
works. Values may be wrapped in single or double quotes, which are stripped.

The value is split on the FIRST `=` only. That is not a detail: an Azure
endpoint is
`.../chat/completions?api-version=2024-08-01-preview`, which contains its own
`=` and would be truncated by a naive split.

An unquoted trailing comment is stripped only when the `#` is preceded by
whitespace, so `TS_LLM_LOCK_MODEL=1   # optional` sets `1` while a value that
legitimately contains `#` is left alone.

This file holds a live API key, so it must never be committed. The repo's
.gitignore covers `*.env` and `webapp/etc/` -- a bare `.env` rule did not,
which is how a real key nearly went in.
"""
import os

# Every variable this app understands. A key in the file that is not on this
# list is loaded anyway -- refusing would be unhelpful -- but it IS reported,
# because a typo like `TS_LLM_APIKEY=` is otherwise indistinguishable from a
# working configuration: the app would just say "not configured" and leave the
# operator staring at a file that looks right.
KNOWN_VARS = {
    "TS_LLM_PROVIDER", "TS_LLM_API_KEY", "TS_LLM_ENDPOINT",
    "TS_LLM_DEFAULT_MODEL", "TS_LLM_LOCK_MODEL",
    "TS_ADMIN_PASSWORD",
    "TS_CHAT_MAX_TOOL_ROUNDS", "TS_CHAT_MAX_TOOL_RESULT_BYTES", "TS_CHAT_TURN_SECONDS",
    "TS_OPENROUTER_URL", "TS_PUBLIC_URL",
    "TS_MCP_ALLOW_QUERY_TOKEN", "TS_MCP_PATH",
    "TS_DEBUG_HELPER_URL", "TS_DEBUG_HELPER_TOKEN",
}

SECRET_VARS = {"TS_LLM_API_KEY", "TS_ADMIN_PASSWORD", "TS_DEBUG_HELPER_TOKEN"}

WEBAPP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CANDIDATES = (
    os.path.join(WEBAPP_DIR, ".env"),
    os.path.join(WEBAPP_DIR, "etc", "ts-debug-helper.env"),
)

# Set by `load()` so the startup banner can say which file was used -- or that
# none was found, which is the answer to "why did it not pick up my file".
LOADED_FROM = None
_REPORT = []


def skipped_by_request():
    """`TS_SKIP_ENV_FILE=1` disables the file entirely.

    The test suites set it before importing the app. Without it, a developer
    who has configured a real `.env` would have their own Azure connection
    quietly present in every test run -- so a test's behaviour would depend
    on a file that is not in the repository, which is the kind of thing that
    passes locally and fails in CI (or the reverse, which is worse)."""
    return (os.environ.get("TS_SKIP_ENV_FILE") or "").strip().lower() in ("1", "true", "yes", "on")


def explicit_path():
    return (os.environ.get("TS_ENV_FILE") or "").strip()


def candidate_paths():
    """An explicit `TS_ENV_FILE` is the ONLY candidate when it is set.

    It deliberately does not fall back to the conventional locations. If an
    operator names a file and it is not there, silently loading a different
    one is the worst outcome available: the app comes up looking configured,
    on settings the operator did not choose and cannot see. Better to load
    nothing and say which path was missing."""
    explicit = explicit_path()
    return (explicit,) if explicit else CANDIDATES


def parse(text):
    """Parse env-file text into an ordered list of (key, value, line_number).

    Returns a list rather than a dict so a duplicated key can be reported
    instead of silently resolving to whichever came last.
    """
    out = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        # First `=` only: an Azure endpoint carries `?api-version=...`.
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        out.append((key, _clean_value(value), lineno))
    return out


def _clean_value(value):
    value = value.strip()
    quoted = len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"')
    if quoted:
        return value[1:-1]
    # Strip a trailing comment only when the `#` follows whitespace. The
    # README's own examples carry `# optional` notes, and a value that
    # genuinely contains `#` must survive.
    for i in range(1, len(value)):
        if value[i] == "#" and value[i - 1].isspace():
            value = value[:i]
            break
    return value.strip()


def load(verbose=True):
    """Read the first env file found and fill in anything not already set.

    Never raises: a malformed or unreadable config file must not stop the app
    from starting, because everything except chat works without it.
    """
    global LOADED_FROM
    _REPORT.clear()

    if skipped_by_request():
        LOADED_FROM = None
        _REPORT.append("TS_SKIP_ENV_FILE is set, so no configuration file was read")
        return {}

    path = next((p for p in candidate_paths() if p and os.path.isfile(p)), None)
    if path is None:
        LOADED_FROM = None
        if explicit_path():
            _REPORT.append(f"TS_ENV_FILE points at {explicit_path()}, which does not exist; "
                           f"no configuration file was read")
        return {}

    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            entries = parse(f.read())
    except OSError as e:
        LOADED_FROM = None
        _REPORT.append(f"could not read {path}: {e}")
        return {}

    applied, skipped, unknown, seen = {}, [], [], set()
    for key, value, lineno in entries:
        if key in seen:
            _REPORT.append(f"{os.path.basename(path)} line {lineno}: "
                           f"{key} appears more than once; the first value is used")
            continue
        seen.add(key)
        if key not in KNOWN_VARS:
            unknown.append(f"{key} (line {lineno})")
        if key in os.environ:
            # Already set in the real environment -- systemd, or an explicit
            # override for this run. Leave it.
            skipped.append(key)
            continue
        os.environ[key] = value
        applied[key] = value

    LOADED_FROM = path
    if unknown:
        _REPORT.append("not a setting this app reads -- check the spelling: "
                       + ", ".join(unknown))
    if skipped:
        _REPORT.append("already set in the environment, so the file's value was ignored: "
                       + ", ".join(sorted(skipped)))
    if verbose:
        for line in startup_report():
            print(line, flush=True)
    return applied


def startup_report():
    """Lines for the boot banner. Values of secrets are never printed."""
    lines = []
    if LOADED_FROM:
        lines.append(f"[TS Debug Helper] Loaded configuration from {LOADED_FROM}")
    else:
        looked = [p for p in candidate_paths() if p]
        lines.append("[TS Debug Helper] No configuration file found. Looked for: "
                     + ", ".join(looked))
        lines.append("[TS Debug Helper]   (that is fine if the variables are already set in "
                     "the environment, e.g. by systemd's EnvironmentFile)")
    for note in _REPORT:
        lines.append(f"[TS Debug Helper]   note: {note}")
    return lines


def describe_loaded():
    """Which settings are in effect and where from, for diagnostics. Secret
    values are reduced to whether they are present."""
    out = {}
    for key in sorted(KNOWN_VARS):
        if key not in os.environ:
            continue
        out[key] = "(set)" if key in SECRET_VARS else os.environ[key]
    return {"file": LOADED_FROM, "settings": out, "notes": list(_REPORT)}

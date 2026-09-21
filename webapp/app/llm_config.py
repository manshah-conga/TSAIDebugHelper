"""
The shared, app-wide LLM connection -- one credential that serves every user.

Why this exists
---------------
The original design gave every user their own API key, encrypted under their
own password (app/secrets_store.py). That is cryptographically the stronger
arrangement, and it was the wrong product: a support engineer who wants to
ask about a customer org had to first obtain an OpenRouter or Azure key,
paste it in, and re-enter their password after every server restart before
chat would work at all. Three obstacles between signing in and asking a
question.

So the connection moved to where it belongs -- the server, configured once by
whoever operates it:

    TS_LLM_PROVIDER        "azure" or "openrouter"
    TS_LLM_API_KEY         the key itself
    TS_LLM_ENDPOINT        Azure only: the full chat-completions URL,
                           including the deployment path and ?api-version=
    TS_LLM_DEFAULT_MODEL   optional; the model new chats start on
                           (on Azure the deployment already decides this)
    TS_LLM_LOCK_MODEL      optional; "1" stops non-admins changing model

Every signed-in user gets this connection automatically. Nothing to paste,
nothing to unlock, and it survives a restart because the key comes from the
process environment rather than from anything encrypted at rest.

Why environment variables and not a settings screen
---------------------------------------------------
Because a key that any signed-in session can change is a key that a stolen
session can change. Reading it from the environment means changing the LLM
connection requires access to the server itself -- which is the same
privilege boundary as "can restart the service", and is not something an
in-app role can be tricked into crossing. The trade-off is that rotating the
key needs a config edit plus a restart. That is a deliberate, and rare,
piece of friction.

The UI shows admins exactly what is configured (provider, endpoint, a masked
hint, whether a live call has succeeded) and is explicit that changing it
happens on the server. Non-admins see no LLM settings at all.

What survives of the per-user path
----------------------------------
All of it, in app/secrets_store.py -- the code is intact and still tested. It
is now an ADMIN-ONLY override: an admin who wants their own turns billed to
their own account can store a personal key, and it takes precedence for
their sessions only. Ordinary users have no route to it, in the UI or the
API. See `secrets_store.require_creds` for the resolution order.

Reading a key from the environment is not a secret store. The value is
visible to anything that can read /proc/<pid>/environ, which is the process
owner and root. That is the same audience that can read data/ and restart
the service, so it adds no new reader -- but it is why the systemd unit
keeps it in a 0600 EnvironmentFile rather than inline in the unit, where it
would be world-readable via `systemctl show`.
"""
import os
import threading

from . import llm

ENV_PROVIDER = "TS_LLM_PROVIDER"
ENV_API_KEY = "TS_LLM_API_KEY"
ENV_ENDPOINT = "TS_LLM_ENDPOINT"
ENV_DEFAULT_MODEL = "TS_LLM_DEFAULT_MODEL"
ENV_LOCK_MODEL = "TS_LLM_LOCK_MODEL"

# Set once at startup by `describe()`; a validation failure is recorded rather
# than raised so a bad value degrades chat instead of refusing to boot the
# whole app (org connections, incidents and the knowledgebase do not need an
# LLM at all).
_STATE = {"checked": False, "error": None, "verified_at": None, "verify_error": None}
_LOCK = threading.Lock()


def _env(name):
    return (os.environ.get(name) or "").strip()


def _hint(api_key):
    """Enough to recognise which key is loaded, not enough to use it."""
    api_key = api_key or ""
    if len(api_key) <= 8:
        return "*" * len(api_key)
    head = api_key[:10] if api_key.startswith("sk-") else api_key[:4]
    return f"{head}…{api_key[-4:]}"


def configured():
    """True when the environment carries a usable shared connection."""
    return bool(_env(ENV_API_KEY)) and not _validation_error()


def _validation_error():
    """The reason the configured values are unusable, or None.

    Checked on every call rather than cached, because the answer depends only
    on the environment and the cost is two string comparisons -- and caching
    it would make the state stick across a test that patches the environment.
    """
    api_key = _env(ENV_API_KEY)
    if not api_key:
        return None                      # not configured at all is not an error
    provider = (_env(ENV_PROVIDER) or llm.PROVIDER_OPENROUTER).lower()
    if provider not in (llm.PROVIDER_OPENROUTER, llm.PROVIDER_AZURE):
        return (f"{ENV_PROVIDER}={provider!r} is not recognised. Use "
                f"'{llm.PROVIDER_AZURE}' or '{llm.PROVIDER_OPENROUTER}'.")
    if provider == llm.PROVIDER_AZURE:
        try:
            llm.validate_azure_endpoint(_env(ENV_ENDPOINT))
        except ValueError as e:
            return f"{ENV_ENDPOINT} is not usable: {e}"
    return None


def provider():
    return (_env(ENV_PROVIDER) or llm.PROVIDER_OPENROUTER).lower()


def endpoint():
    return _env(ENV_ENDPOINT)


def default_model():
    """The model a new chat starts on.

    On Azure the deployment in the endpoint path IS the model, so an explicit
    TS_LLM_DEFAULT_MODEL is redundant there and the deployment name wins --
    otherwise a stale value in the environment would show a model in the UI
    that no request could ever actually reach.
    """
    if provider() == llm.PROVIDER_AZURE and endpoint():
        return llm.azure_deployment(endpoint())
    return _env(ENV_DEFAULT_MODEL) or None


def model_locked():
    return _env(ENV_LOCK_MODEL).lower() in ("1", "true", "yes", "on")


def creds():
    """The credential bundle for the shared connection, or None."""
    if not configured():
        return None
    return llm.creds(provider(), _env(ENV_API_KEY), endpoint())


def mark_verified(at):
    with _LOCK:
        _STATE["verified_at"] = at
        _STATE["verify_error"] = None


def mark_verify_failed(message):
    with _LOCK:
        _STATE["verify_error"] = str(message)[:500]


def public_state():
    """What the UI may know about the shared connection. Never the key.

    Deliberately available to every signed-in account, not just admins: a
    user whose chat is not working needs to be told *why* -- "the server has
    no LLM configured, ask an admin" -- and a provider name with a masked
    hint is not a secret. The key, and the endpoint URL (which names internal
    Azure infrastructure), are admin-only; see `admin_state`.
    """
    err = _validation_error()
    api_key = _env(ENV_API_KEY)
    return {
        "source": "server",
        "configured": bool(api_key) and not err,
        "provider": provider(),
        "default_model": default_model(),
        "model_locked": model_locked(),
        "verified_at": _STATE["verified_at"],
        # Present only when something is actually wrong, so the UI can show
        # the real reason rather than a generic "chat unavailable".
        "config_error": err,
        "verify_error": _STATE["verify_error"],
        "present_but_invalid": bool(api_key) and bool(err),
    }


def admin_state():
    """`public_state` plus the operational detail an admin needs to confirm
    which key and which deployment are loaded, and the env var names to edit."""
    state = dict(public_state())
    api_key = _env(ENV_API_KEY)
    state.update({
        "hint": _hint(api_key) if api_key else None,
        "endpoint": endpoint() or None,
        "env_vars": {
            "provider": ENV_PROVIDER,
            "api_key": ENV_API_KEY,
            "endpoint": ENV_ENDPOINT,
            "default_model": ENV_DEFAULT_MODEL,
            "lock_model": ENV_LOCK_MODEL,
        },
    })
    return state


def startup_report():
    """Printed once at boot. An operator who mistyped an environment variable
    finds out here, in the journal at startup, rather than from a user
    reporting that chat is broken an hour later."""
    api_key = _env(ENV_API_KEY)
    if not api_key:
        return ("[TS Debug Helper] No shared LLM connection configured "
                f"({ENV_API_KEY} is unset). Chat will be unavailable until an "
                f"operator sets it; everything else works. See webapp/README.md.")
    err = _validation_error()
    if err:
        return f"[TS Debug Helper] Shared LLM connection is NOT usable: {err}"
    detail = f"provider={provider()}"
    if provider() == llm.PROVIDER_AZURE:
        detail += f", deployment={llm.azure_deployment(endpoint())}"
    elif default_model():
        detail += f", default model={default_model()}"
    return (f"[TS Debug Helper] Shared LLM connection loaded ({detail}, "
            f"key {_hint(api_key)}). Available to every signed-in user.")

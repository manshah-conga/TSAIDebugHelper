"""
"Run it" on a write-tool confirm card looped (2026-10-07).

The click replayed the turn with the parked call id in confirmed_tool_ids and
relied on the model re-issuing the SAME call with the SAME id. Providers mint
a fresh id on every completion, so the id never matched: file_incident was
parked again, three times, and never ran. Now the parked call itself runs.

Run:  python tests/test_chat_confirm.py
"""
import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["TS_SKIP_ENV_FILE"] = "1"

from app import storage  # noqa: E402
_TMP = tempfile.mkdtemp(prefix="ts-confirm-test-")
storage.DATA_ROOT = _TMP
from app import chat_store  # noqa: E402
chat_store.CHATS_ROOT = os.path.join(_TMP, "chats")
from app import chat, llm, limits, secrets_store, usage as usage_ledger  # noqa: E402

FAILURES = []


def check(name, condition, detail=""):
    print(f"  {'ok  ' if condition else 'FAIL'} {name}  {'' if condition else detail}")
    if not condition:
        FAILURES.append(name)


ARGS = {"org_id": "00D000000000001", "label": "toomanyqueueable", "log_id": "L1"}
STATE = {"n": 0, "wires": []}
EXECUTED = []


async def fake_stream(creds, model, messages, tools=None, **kw):
    """Every completion proposes file_incident with a NEW id, exactly as a
    real provider does -- until it has seen a real result, then it answers."""
    STATE["wires"].append(messages)
    STATE["n"] += 1
    last_tool = next((m for m in reversed(messages) if m.get("role") == "tool"), None)
    if last_tool and "INC-1" in (last_tool.get("content") or ""):
        yield llm.Delta(content="Filed as INC-1.")
        return
    yield llm.Delta(tool_calls=[{"index": 0, "id": f"call_{STATE['n']}",
                                 "function": {"name": "file_incident",
                                              "arguments": json.dumps(ARGS)}}])


async def fake_call_tool(name, args, token=None):
    EXECUTED.append((name, args))
    return {"incident_id": "INC-1", "ok": True}


async def fake_schemas(with_org):
    return []


chat.llm.stream_chat = fake_stream
chat.call_tool = fake_call_tool
chat.tool_schemas = fake_schemas
secrets_store.require_creds = lambda ident: {"source": "shared"}
secrets_store.mark_verified = lambda u: None
limits.check_turn_allowed = lambda u, user: (True, None, None)
usage_ledger.record_turn = lambda *a, **k: None

IDENT = {"username": "alice", "role": "admin", "_raw_token": "t"}


async def turn(chat_id, text, confirm=None):
    frames = []
    async for f in chat.run_turn(IDENT, chat_id, text, "00D000000000001", None, "m",
                                 confirmed_tool_ids=confirm):
        frames.append(f)
    return frames


def events(frames, name):
    return [json.loads(f.split("data: ", 1)[1]) for f in frames if f.startswith(f"event: {name}\n")]


def test_confirm_runs_parked_call():
    meta = chat_store.create_chat("alice", org_id="00D000000000001")
    cid = meta["chat_id"]
    f1 = asyncio.run(turn(cid, "File an incident for this log."))
    parked = events(f1, "confirm_required")
    check("first turn parks the write", len(parked) == 1 and not EXECUTED)

    f2 = asyncio.run(turn(cid, "Go ahead.", [parked[0]["id"]]))
    check("confirm executes the parked call exactly once", len(EXECUTED) == 1, str(EXECUTED))
    check("no second confirmation asked", not events(f2, "confirm_required"))
    check("model answers from the real result",
          "Filed as INC-1." in "".join(e["text"] for e in events(f2, "token")))

    msgs = chat_store.load_messages("alice", cid)
    tool = [m for m in msgs if m.get("role") == "tool" and m.get("tool_call_id") == parked[0]["id"]]
    check("parked tool message now holds the real result",
          tool and "INC-1" in tool[0]["content"] and "awaiting" not in tool[0]["content"])
    call = [c for m in msgs for c in (m.get("tool_calls") or []) if c["id"] == parked[0]["id"]]
    check("display record no longer pending", call and not call[0].get("pending") and call[0]["ok"])
    check("'Go ahead.' not stored as a user message",
          not any(m.get("role") == "user" and m.get("content") == "Go ahead." for m in msgs))


def test_unconfirmed_call_marked_not_run():
    EXECUTED.clear()
    meta = chat_store.create_chat("alice", org_id="00D000000000001")
    cid = meta["chat_id"]
    asyncio.run(turn(cid, "File an incident for this log."))
    # User ignores the card / declines and types something else.
    STATE["n"] = 100
    asyncio.run(turn(cid, "Don't run that -- explain what you were going to do instead."))
    msgs = chat_store.load_messages("alice", cid)
    first_tool = next(m for m in msgs if m.get("role") == "tool")
    check("declined call was not executed", not EXECUTED)
    check("declined call is marked not_run for the model", "not_run" in first_tool["content"])


if __name__ == "__main__":
    for t in (test_confirm_runs_parked_call, test_unconfirmed_call_marked_not_run):
        print(t.__name__)
        t()
    print("FAILED: " + ", ".join(FAILURES) if FAILURES else "all passed")
    sys.exit(1 if FAILURES else 0)

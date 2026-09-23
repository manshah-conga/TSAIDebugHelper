"""
Regression evals for the in-app chat agent.

Drives the REAL chat endpoint (POST /api/chats, POST /api/chats/{id}/messages)
exactly as the browser does, so it tests the model + system prompt + tool
docstrings together. Each case is graded on the final answer text AND the
tool calls the model made -- getting the right name by luck without opening
the component still fails `must_call`.

Usage
-----
    python evals/run_evals.py --model openai/gpt-4o
    python evals/run_evals.py --model openai/gpt-4o --preamble prefix
    python evals/run_evals.py --model openai/gpt-4o --preamble turn --only status_internal_review_via_variable

    Env (or flags):  TS_DEBUG_HELPER_URL   default http://127.0.0.1:8000
                     TS_DEBUG_HELPER_TOKEN an API token from the web UI (role user or admin)

--preamble tests prompt changes WITHOUT editing the system prompt:
    none    question only (baseline)
    prefix  prompts/field_value_protocol.md + the question, in ONE user message
    turn    protocol sent as its own first turn, then the question as turn 2

Run each mode against the same model and compare the summaries. A report is
written to evals/results/<timestamp>_<model>_<mode>.json. Chats are deleted
afterwards unless --keep-chats is passed (keep them to read the transcript in
the UI).

Exit code is 1 if any case fails, so this can gate a deploy.
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import httpx

HERE = os.path.dirname(os.path.abspath(__file__))
WEBAPP = os.path.dirname(HERE)
PROTOCOL_PATH = os.path.join(WEBAPP, "prompts", "field_value_protocol.md")

LOG_REQUEST = re.compile(r"debug\s*log|provide (a|the) log|share (a|the) log", re.I)


# ---------- grading (pure; unit-tested in tests/test_evals_grader.py) ----------

def grade(case, answer, tool_calls):
    """Return (passed, failures, warnings). `tool_calls` is a list of
    {"name": str, "args": dict}."""
    exp = case.get("expect", {})
    text = (answer or "").lower()
    failures, warnings = [], []

    missing = [s for s in exp.get("must_mention_all", []) if s.lower() not in text]
    for s in missing:
        failures.append(f"answer does not name expected '{s}'")

    for s in exp.get("should_mention", []):
        if s.lower() not in text:
            warnings.append(f"answer does not cite element '{s}'")

    for req in exp.get("must_call", []):
        needle = (req.get("args_contains") or "").lower()
        hit = any(c["name"] == req["name"]
                  and (not needle or needle in json.dumps(c.get("args", {})).lower())
                  for c in tool_calls)
        if not hit:
            failures.append(f"never called {req['name']}"
                            + (f" with '{req['args_contains']}'" if needle else ""))

    # A decoy is only a failure when it is offered INSTEAD of the right answer.
    # Mentioning it as a near-miss alongside the expected component is good.
    for d in exp.get("decoys", []):
        if d.lower() in text and missing:
            failures.append(f"named decoy '{d}' instead of the expected component")
        elif d.lower() in text:
            warnings.append(f"mentions decoy '{d}' (alongside the right answer)")

    if exp.get("forbid_log_request_without_answer") and missing and LOG_REQUEST.search(answer or ""):
        failures.append("asked for a debug log instead of resolving from static metadata")

    return (not failures), failures, warnings


# ---------- transport ----------

def _parse_sse(lines):
    """Yield (event, data) from an iterator of SSE text lines."""
    event, data = None, []
    for line in lines:
        if line == "":
            if event:
                try:
                    yield event, json.loads("\n".join(data)) if data else {}
                except json.JSONDecodeError:
                    yield event, {"raw": "\n".join(data)}
            event, data = None, []
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    if event:
        yield event, json.loads("\n".join(data)) if data else {}


def send_turn(client, chat_id, content, org_id, model, timeout):
    """Run one turn; return dict(answer, tool_calls, errors, confirm_required, usage, ms)."""
    started = time.monotonic()
    answer, tool_calls, errors, confirm, usage = "", [], [], None, None
    body = {"content": content, "org_id": org_id}
    if model:
        body["model"] = model
    with client.stream("POST", f"/api/chats/{chat_id}/messages", json=body, timeout=timeout) as r:
        if r.status_code != 200:
            r.read()
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
        for ev, data in _parse_sse(r.iter_lines()):
            if ev == "token":
                answer += data.get("text", "")
            elif ev == "content_replace":
                answer = data.get("text", "")
            elif ev == "tool_call":
                tool_calls.append({"name": data.get("name"), "args": data.get("args") or {}})
            elif ev == "error":
                errors.append(data)
            elif ev == "confirm_required":
                confirm = data
            elif ev == "usage":
                usage = data
    return {"answer": answer.strip(), "tool_calls": tool_calls, "errors": errors,
            "confirm_required": confirm, "usage": usage,
            "ms": int((time.monotonic() - started) * 1000)}


def build_messages(question, mode, protocol):
    if mode == "none":
        return [question]
    if mode == "prefix":
        return [f"{protocol}\n\n---\n\nQuestion: {question}"]
    if mode == "turn":
        return [protocol + "\n\nAcknowledge with 'Ready.' only and wait for my question.", question]
    raise ValueError(mode)


def run_case(client, case, model, mode, protocol, keep, timeout):
    chat = client.post("/api/chats", json={"org_id": case["org_id"], "model": model,
                                            "title": f"[eval] {case['id']} ({mode})"})
    chat.raise_for_status()
    chat_id = chat.json()["chat_id"]
    try:
        turns = [send_turn(client, chat_id, m, case["org_id"], model, timeout)
                 for m in build_messages(case["question"], mode, protocol)]
    finally:
        if not keep:
            client.delete(f"/api/chats/{chat_id}")
    last = turns[-1]
    all_calls = [c for t in turns for c in t["tool_calls"]]
    passed, failures, warnings = grade(case, last["answer"], all_calls)
    if last["errors"]:
        passed = False
        failures.append(f"stream error: {last['errors'][0]}")
    return {"id": case["id"], "passed": passed, "failures": failures, "warnings": warnings,
            "tool_calls": [c["name"] + (f"({c['args'].get('component_id') or c['args'].get('field_api_name') or ''})")
                           for c in all_calls],
            "answer": last["answer"], "ms": sum(t["ms"] for t in turns),
            "total_tokens": sum((t["usage"] or {}).get("total_tokens", 0) or 0 for t in turns),
            "chat_id": chat_id if keep else None}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cases", default=os.path.join(HERE, "field_value_cases.json"))
    ap.add_argument("--base-url", default=os.environ.get("TS_DEBUG_HELPER_URL", "http://127.0.0.1:8000"))
    ap.add_argument("--token", default=os.environ.get("TS_DEBUG_HELPER_TOKEN", ""))
    ap.add_argument("--model", default=None, help="model id as shown in the chat model picker; default = user's default")
    ap.add_argument("--preamble", choices=["none", "prefix", "turn"], default="none")
    ap.add_argument("--only", action="append", help="case id to run (repeatable)")
    ap.add_argument("--repeat", type=int, default=1, help="run each case N times (models are nondeterministic)")
    ap.add_argument("--keep-chats", action="store_true")
    ap.add_argument("--timeout", type=float, default=300.0)
    a = ap.parse_args(argv)

    if not a.token:
        sys.exit("Set TS_DEBUG_HELPER_TOKEN or pass --token (an API token from the web UI).")
    cases = json.load(open(a.cases, encoding="utf-8"))["cases"]
    if a.only:
        cases = [c for c in cases if c["id"] in a.only]
    protocol = open(PROTOCOL_PATH, encoding="utf-8").read().strip() if a.preamble != "none" else ""

    results = []
    with httpx.Client(base_url=a.base_url.rstrip("/"),
                      headers={"Authorization": f"Bearer {a.token}"}, timeout=60) as client:
        for case in cases:
            for i in range(a.repeat):
                r = run_case(client, case, a.model, a.preamble, protocol, a.keep_chats, a.timeout)
                r["run"] = i + 1
                results.append(r)
                mark = "PASS" if r["passed"] else "FAIL"
                print(f"[{mark}] {r['id']} (run {i+1}, {r['ms']/1000:.1f}s, {r['total_tokens']} tok)")
                print(f"       tools: {' -> '.join(r['tool_calls']) or '(none)'}")
                for f in r["failures"]:
                    print(f"       x {f}")
                for w in r["warnings"]:
                    print(f"       ~ {w}")

    n_pass = sum(r["passed"] for r in results)
    print(f"\n{n_pass}/{len(results)} passed  (model={a.model or 'default'}, preamble={a.preamble})")

    os.makedirs(os.path.join(HERE, "results"), exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_model = re.sub(r"[^A-Za-z0-9._-]+", "_", a.model or "default")
    out = os.path.join(HERE, "results", f"{stamp}_{safe_model}_{a.preamble}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"model": a.model, "preamble": a.preamble, "base_url": a.base_url,
                   "passed": n_pass, "total": len(results), "results": results}, f, indent=2)
    print(f"report: {out}")
    return 0 if n_pass == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())

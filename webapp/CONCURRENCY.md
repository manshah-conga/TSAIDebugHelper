# Multi-user concurrency: what breaks, what was fixed, what to watch

This app is now used the way it was always meant to be: several engineers
signed in at once, from different machines, sharing one LLM connection and one
set of connected orgs. That changes the failure modes. Most of what follows
was not a latent theoretical race — it was actively losing data, quietly,
under load that this app sees on a normal Tuesday.

The single most important thing on this page: **atomic is not the same as
serialised.** `storage.write_json` has always written via a temp file and
`os.replace`, so no reader ever sees half a document. That says nothing about
two *writers*. Nearly every store in this app is one JSON document that gets
read, modified in one small place, and written back whole. Two requests doing
that concurrently means the second one writes a version computed from a
snapshot taken before the first one landed, and the first one's change is gone.

## How bad it actually was

Measured, not estimated. Eight threads, fifty increments each, through the
exact read-modify-write pattern the app used everywhere:

| | Expected | Actual |
|---|---|---|
| Without a lock | 400 | **56** |
| With the lock | 400 | 400 |
| With the lock, 4 separate processes × 200 | 800 | 800 |

86% of writes lost. That is the shape of every issue marked **Critical** below.
The test that produced it is `test_locked_mutation_is_serialised` in
`tests/test_chat_secrets.py`.

Two properties of this app made it worse than it looks. FastAPI runs every
`def` route in a thread pool, so plain-looking synchronous handlers really do
execute in parallel. And the UI polls org status every 1.5 seconds per open
tab, so the highest-traffic write path — stamping `last_used` on a token — was
firing more or less continuously.

---

## Fixed in this pass

### 1. Critical — token loss logged people out at random

`auth.verify_token` stamped `last_used` by loading the whole of
`tokens.json`, setting one field, and writing everything back. Any login
completing during that window had its brand-new session token erased. The
user's very next request then 401'd and bounced them to the login screen with
nothing to explain it, and it looked like an intermittent session bug rather
than a lost write.

**Fix.** `_stamp_last_used` touches only that one token's field, inside the
lock (`app/auth.py`). Every other token operation — minting, revoking,
cascade-revoking on user deletion — goes through `storage.mutate_tokens` too.

### 2. Critical — connected orgs disappearing from the list

`registry.json` holds every org's name, owner and visibility in one document.
Two engineers finishing fetches of two *different* orgs within the same second
each wrote the whole registry from their own snapshot, so one of the two orgs
was simply absent afterwards — knowledgebase intact on disk, invisible in the
UI.

**Fix.** `onboarding.run_onboarding` writes through `storage.mutate_registry`
and touches only its own org's entry.

### 3. Critical — a visibility change silently reverting

`org_access.set_visibility` checked permission, then wrote. An org fetch
finishing in between rewrote the registry and dropped the change. The owner
was told the org was now public; it stayed private. Confidentiality-shaped,
not cosmetic — and it fails in the other direction just as easily.

**Fix.** The permission check and the write now happen inside one lock, so the
entry cannot move underneath them.

### 4. Critical — recurrence history being erased

`known_issues_index.json` is the point of filing incidents: the second time an
issue appears, the answer is already on file. Two engineers filing against the
same org both read the index, both bumped the occurrence count, and the second
write discarded the first — losing precisely the accumulating history the
feature exists to build.

**Fix.** Signature matching and the recurrence bump run together under
`storage.mutate_known_issues`, in `main.create_incident` and
`main.resolve_incident`.

### 5. Critical — one big org fetch froze the app for everyone

This one is not a lock problem. `run_onboarding` is an `async def` background
task, and its extraction phase is pure CPU: regex-parsing thousands of Apex
bodies and Flow metadata documents. Sitting on the event loop, it blocked
*every* other request in the process for the duration — other users' page
loads, status polls, and any chat stream mid-answer. On a large org that is
minutes of total unavailability, triggered by one person clicking Connect.

**Fix.** Extraction, index building and the knowledgebase write all run via
`asyncio.to_thread`. The extractors touch no shared mutable state, so a worker
thread is safe; warnings are returned rather than appended onto the live job
dict, so nothing is mutated while a polling request may be reading it.

### 6. High — two people fetching the same org

The one case locking cannot make correct. Both jobs write the same
knowledgebase files and the same content-hash manifest, so each computes its
changed/added/removed report against a baseline the other already moved. The
result is not corrupt, it is *confidently wrong*, which is worse.

**Fix.** Refused rather than serialised. `onboarding.job_in_flight` gates both
`POST /api/orgs` and `POST /api/orgs/{id}/refresh` with a 409 that names the
phase the running fetch is on, so the second person can see it is someone
else's fetch and not a stuck button. The orgs table also shows a live progress
bar for in-flight orgs, so a colleague can see a refresh is under way before
reaching for the button.

### 7. High — a chat losing a turn

Same conversation open in two browser tabs — not contrived, it is how people
work. Whichever turn finished second rewrote `messages.json` from the version
it had read, deleting the other turn and the running token/cost totals with it.

**Fix.** `chat_store.append_messages` appends and rolls up totals through
`storage.mutate_json`.

### 8. Medium — user administration overwriting itself

Two admins working the user list at once: one flips a role, the other disables
a different account, and one change vanishes. Worse, `create_user` checked for
an existing username and then inserted, so two admins creating the same name
both passed the check and the second silently overwrote the first's password.

**Fix.** Every mutator in `app/auth.py` funnels through `_update_user`, which
applies one field change inside the lock. `create_user` does its existence
check inside the same lock as its insert.

### 9. Medium — LLM key records clobbering each other

`llm_keys.json` holds one record per user in one document, so storing a key,
setting a default model, or an admin password reset could each erase another
user's record. `rewrap_for_new_password` was the sharp edge: it unwrapped the
DEK outside the lock and wrote it back afterwards, so a key replaced in
between would be left with a wrapper that did not match its own ciphertext —
unrecoverable.

**Fix.** All of it goes through `_mutate_all`, and the rewrap re-reads inside
the lock and declines if the record changed.

### 10. Medium — share links that 404 for no reason

`_shares.json` is one document for the whole app. Two people sharing different
conversations at once: one link worked, the other did not.

**Fix.** `create_share` and `revoke_share` mutate under the lock.

### 11. Low — usage accounting under load

New in this pass, and worth saying why it is shaped the way it is. Running
totals in a document would have been smaller to read and would have had
exactly the bug above — several turns finishing at once, all adding to the
same counters. The ledger is therefore **append-only**, one JSONL file per UTC
day: an append has no read step, so two writers cannot erase each other, only
sit next to each other. Verified with twenty concurrent writers in
`test_usage_concurrent_appends`.

### 12. Low — a corrupt document taking out every request

`read_json` raised `JSONDecodeError` on a truncated file, so one bad document
meant a 500 on every request that touched it.

**Fix.** It logs and returns the default, keeping the app serving.

### 13. Critical — the locking API itself had a whole-store-wipe footgun

Found in review of the fixes above, which is the honest place to record it.
`mutate_json` originally treated any non-None mutator return as a
*replacement document*. That makes the most natural one-line mutator a
disaster:

```python
mutate_json(path, lambda d: d.pop(key, None), {})   # "remove this entry"
```

`dict.pop` returns the removed value, so this replaced the entire document
with whatever was popped. `secrets_store.forget_user` was written that way
(as `... and None`, which is worse — `{} and None` is `{}`, not `None`), so
deleting an account whose key record happened to be empty would have
destroyed **every other user's wrapped API key**.

**Fix.** The mutator's return value is now ignored outright. The dangerous
one-liner and the careful named function behave identically, which is better
than documenting the difference and hoping. A caller that really means to
replace the document does `d.clear(); d.update(new)`, which says so at the
call site. `mutate_json` also materialises a fresh copy of `default` rather
than handing the caller's own object to the mutator. Pinned by
`test_mutator_cannot_replace_the_whole_store`.

### 14. High — `append_messages` was locked and then immediately undone

Also from review. `chat.run_turn` called the carefully-locked
`append_messages`, then wrote the whole meta document back with `save_meta`
to stamp `model` and `org_id` — rolling the token and cost totals straight
back off whenever a second turn landed in between.

**Fix.** A new `chat_store.update_meta` changes named fields under the lock,
and `run_turn`, `create_share` and `revoke_share` all use it. `save_meta`
keeps a docstring saying it is correct only for a deliberate whole-document
replacement. `chat_store.forget_user` also went through `mutate_json` for
`_shares.json`.

### 15. Medium — `TS_LLM_LOCK_MODEL` was decorative

Not a race, but found in the same review and worth recording with it. The
model pin was enforced only on the "remember my model" route. A client that
put `model` in the turn body — or in the body of `POST /api/chats` — chose
whatever it liked, and the shared key paid for it.

**Fix.** A single `_resolve_model` in `main.py` is used by both routes and
ignores a requested model when the pin is set. It also fixes a related
functional bug: the fallback read the caller's *personal* key record for a
default model, so the server's own `TS_LLM_DEFAULT_MODEL` never reached a
turn and any client that did not name a model got a flat 400.

### 16. Low — `locked()` could hang a worker thread forever

The cross-process half had a 30-second timeout; the in-process
`threading.RLock` acquire did not. No current code path nests locks over two
different files, so there is nothing to deadlock on today — but the first
inversion anyone added would have hung a worker silently and permanently.

**Fix.** The thread lock is acquired with the same timeout and raises
`TimeoutError` with a console line naming the file, so the cause is visible
rather than presenting as a hung server.

---

## How the lock works

`app/storage.py`, two layers, both needed:

- a `threading.RLock` per path, for this process's request thread pool;
- `fcntl.flock` (POSIX) / `msvcrt.locking` (Windows) on a sidecar file in
  `data/.locks/`, for other processes — a second uvicorn worker, or the stdio
  MCP server running alongside the web app against the same `data/`.

Acquisition is re-entrant per thread, so a `mutate_json` body that calls
`write_json` internally does not deadlock on itself. A lock that cannot be
taken within 30 seconds proceeds *without* the cross-process lock and says so
on the console — a lost update is bad, but a permanently wedged worker thread
is worse, and the usual cause is a crashed process holding a stale flock.

`mutate_json(path, mutator, default)` is the API. **Any code that computes a
new value from the old one must use it.** `write_json` remains correct only for
writing a document you are replacing outright.

---

## Still open, and deliberately so

### Deploy single-worker

Two pieces of state live in process memory and are not shared between workers:

- `onboarding.JOBS` — in-flight fetch progress. With two workers, a status
  poll landing on the other worker reports `unknown`, and the duplicate-fetch
  guard in issue 6 does not see the other worker's job.
- the unwrapped-key ring in `secrets_store` — an admin with a personal key
  would appear unlocked on one worker and locked on the other, at random.

The shipped systemd unit pins `--workers 1` with this written next to it.
Everything on *disk* is now multi-process safe, so scaling out is a matter of
moving these two pieces of state — Redis, or a small SQLite table — not of
fixing corruption.

### Job progress does not survive a restart

`JOBS` is memory-only. Restart mid-fetch and the status route reports
`unknown` forever, even though the registry and knowledgebase are consistent
(the fetch simply never finished). The remedy today is to fetch again. Worth
persisting if fetches get long enough that a restart during one becomes
routine.

### No per-user rate limiting

One shared LLM key means one user can consume the whole budget. Nothing stops
them. The usage report makes it *visible* after the fact, per user and per
day, which is the prerequisite for a quota — but a quota is not implemented.
If it becomes necessary, the ledger already holds everything a check would
need.

### Two writes that predate this work remain unguarded by role

Not concurrency, but noticed while auditing the routes and worth flagging: a
`reader` account can flip an org **it owns** to public
(`PATCH /api/orgs/{id}/visibility`) and mint an anonymous public transcript
link (`POST /api/chats/{id}/share`). Both are gated on ownership rather than
role, which matches the documented design in `app/org_access.py` — but they
are publish actions available to a role named "read-only". Unchanged here
because changing it is a product decision, not a bug fix.

### Locks are per-file, not per-transaction

Filing an incident takes the known-issues lock, then separately writes the
incident directory. A crash between the two leaves a bumped recurrence count
with no incident folder behind it. Cross-document atomicity needs a real
transaction, which means a database; at this scale the exposure is a stale
counter, not lost analysis.

---

## Verifying

```bash
cd webapp

# Concurrency: 8 threads x 50 increments through mutate_json, and 20
# concurrent usage writers.
python tests/test_chat_secrets.py

# The HTTP guards: duplicate-fetch 409, progress payload, role gating.
python tests/test_llm_and_usage_api.py

python tests/test_org_visibility.py
python tests/test_refresh_e2e.py
python tests/test_refresh_and_password.py
node  tests/test_ui_render.js
```

Run the Python files one per process. Each builds a `TestClient`, and the MCP
session manager refuses to start twice in one interpreter — so `pytest tests/`
in a single process fails on the second and third files for that reason alone,
not because anything is wrong.

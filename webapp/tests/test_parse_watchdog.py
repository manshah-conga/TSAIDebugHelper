"""
Parse watchdog (chunk_parse.ApexParser): a chunk whose parse hangs must not
hang the fetch. The hang is simulated with chunk_parse.TEST_HANG_MARKER
(honoured only when TS_PARSE_TEST_HANG=1, which the spawned workers inherit).

Run:  python -m tests.test_parse_watchdog
"""
import asyncio
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["TS_PARSE_TEST_HANG"] = "1"

from app import chunk_parse  # noqa: E402

M = chunk_parse.TEST_HANG_MARKER


def cls(name, body=None):
    return {"Name": name, "NamespacePrefix": None, "ApiVersion": 60.0,
            "Body": body or f"public class {name} {{ public void run() {{ Other.go(); }} }}"}


def trig(name, body):
    return {"Name": name, "NamespacePrefix": None, "ApiVersion": 60.0, "Body": body}


NAMES = {"Good1", "Good2", "Good3", "Hangs", "Other", "Good4"}


def run(coro):
    return asyncio.run(coro)


async def parse_chunks(chunks, kind="class", **kw):
    kw.setdefault("chunk_timeout", 3)
    kw.setdefault("class_timeout", 2)
    kw.setdefault("workers", 1)
    kw.setdefault("min_classes", 1)
    p = chunk_parse.ApexParser({}, NAMES, sum(len(c) for c in chunks), **kw)
    events = []
    p.on_event = events.append
    async with p:
        results = await asyncio.gather(*[p.parse(kind, c, {}) for c in chunks])
    cards, warns = {}, []
    for c, _h, w in results:
        cards.update(c)
        warns.extend(w)
    return p, cards, warns, events


class Watchdog(unittest.TestCase):
    def test_hung_class_is_isolated_and_stubbed(self):
        chunk = [cls("Good1"), cls("Hangs", f"public class Hangs {{ /*{M}*/ }}"), cls("Good2")]
        t = time.perf_counter()
        p, cards, warns, events = run(parse_chunks([chunk]))
        elapsed = time.perf_counter() - t
        self.assertLess(elapsed, 25, "watchdog should cut the hang off")
        self.assertEqual(set(cards), {"Good1", "Hangs", "Good2"})
        self.assertEqual(cards["Hangs"]["analysis_status"], "timeout")
        self.assertNotIn("analysis_status", cards["Good1"])
        # the good classes still got full analysis, not stubs
        self.assertTrue(any(c.get("target") == "Other" or "Other" in str(c) for c in cards["Good1"]["calls_to"]))
        self.assertEqual(p.timed_out, ["class Hangs"])
        self.assertTrue(any("Hangs" in w and "stub" in w for w in warns), warns)
        self.assertTrue(any("did not finish parsing" in w for w in p.warnings), p.warnings)
        self.assertTrue(any("Hangs" in e for e in events), events)
        self.assertEqual(p.mode, "processes x1")

    def test_other_chunks_survive_a_recycle(self):
        # two workers, three chunks in flight; killing the pool for the hung
        # one must not lose or stub the others
        chunks = [[cls("Good1"), cls("Good2")],
                  [cls("Hangs", f"public class Hangs {{ /*{M}*/ }}")],
                  [cls("Good3"), cls("Good4")]]
        p, cards, warns, _ = run(parse_chunks(chunks, workers=2))
        self.assertEqual(set(cards), {"Good1", "Good2", "Hangs", "Good3", "Good4"})
        stubs = {n for n, c in cards.items() if c.get("analysis_status")}
        self.assertEqual(stubs, {"Hangs"})
        self.assertFalse([w for w in p.warnings if "threads" in w], p.warnings)

    def test_hung_trigger_keeps_its_object(self):
        chunk = [trig("AccTrig", f"trigger AccTrig on Account (before insert, after update) {{ /*{M}*/ }}")]
        p, cards, _, _ = run(parse_chunks([chunk], kind="trigger"))
        c = cards["AccTrig"]
        self.assertEqual((c["analysis_status"], c["object"]), ("timeout", "Account"))
        self.assertEqual(c["events"], ["before insert", "after update"])
        self.assertEqual(c["entry_points"][0]["kind"], "Trigger")

    def test_normal_chunks_match_direct_parse(self):
        chunk = [cls("Good1"), cls("Good2")]
        _, cards, _, _ = run(parse_chunks([chunk]))
        direct, _, _ = chunk_parse.parse_apex_chunk("class", chunk, {}, NAMES, {})
        strip = lambda d: {k: {f: v for f, v in c.items() if f != "extracted_at"} for k, c in d.items()}
        self.assertEqual(strip(cards), strip(direct))

    def test_small_org_gets_a_guard_worker(self):
        p = chunk_parse.ApexParser({}, NAMES, 5, workers=4, min_classes=800, chunk_timeout=60)

        async def go():
            async with p:
                return p.mode
        self.assertEqual(run(go()), "processes x1")

    def test_pool_that_cannot_start_falls_back_to_threads(self):
        # a lambda cannot be pickled into the spawned worker, so the
        # background start-up fails; parsing must still happen, in threads
        p = chunk_parse.ApexParser({"Unpicklable": lambda: 1}, NAMES, 2, workers=1, min_classes=1)

        async def go():
            async with p:
                return await p.parse("class", [cls("Good1"), cls("Good2")], {})
        cards, _, _ = run(go())
        self.assertEqual(set(cards), {"Good1", "Good2"})
        self.assertEqual(p.mode, "threads (pool fallback) (no watchdog)")
        self.assertTrue(any("pool unavailable" in w for w in p.warnings), p.warnings)

    def test_watchdog_off_keeps_threads_for_small_org(self):
        p = chunk_parse.ApexParser({}, NAMES, 5, workers=4, min_classes=800, chunk_timeout=0)

        async def go():
            async with p:
                return p.mode
        self.assertEqual(run(go()), "threads (no watchdog)")


if __name__ == "__main__":
    unittest.main(verbosity=2)

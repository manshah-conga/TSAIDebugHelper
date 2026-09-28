"""
Regression: "Governor limits at the end of the transaction" rendered
"0/undefined" for every limit (Workday_Agreement_StatusUpdate, 2026-09-28).

Two causes:
  1. the normalizer emitted {used, max} while the UI read {used, limit};
  2. every "Number of X" line in the log went into one dict regardless of its
     LIMIT_USAGE_FOR_NS|<namespace>| header, so a managed package's all-zero
     block (written after the org's own block) overwrote the real usage.
It also dropped "Maximum CPU time" / "Maximum heap size" and any line ending
in "******* CLOSE TO LIMIT" -- the lines that matter most.
"""
import unittest

from app.log_normalizer import parse_log_text


def block(ns, soql, cpu, close=False):
    tail = " ******* CLOSE TO LIMIT" if close else ""
    return [
        f"21:20:56.9 (9000000)|LIMIT_USAGE_FOR_NS|{ns}|",
        f"  Number of SOQL queries: {soql} out of 100{tail}",
        "  Number of query rows: 40 out of 50000" if soql else "  Number of query rows: 0 out of 50000",
        "  Number of DML statements: 3 out of 150" if soql else "  Number of DML statements: 0 out of 150",
        f"  Maximum CPU time: {cpu} out of 10000",
        "  Maximum heap size: 0 out of 6000000",
        "  Number of callouts: 0 out of 100",
    ]


def log(*blocks):
    lines = ["50.0 APEX_CODE,FINEST;APEX_PROFILING,INFO",
             "21:20:56.0 (100)|EXECUTION_STARTED",
             "21:20:56.9 (8999999)|CUMULATIVE_LIMIT_USAGE"]
    for b in blocks:
        lines += b
    lines += ["21:20:56.9 (9000001)|CUMULATIVE_LIMIT_USAGE_END", "21:20:57.0 (9100000)|EXECUTION_FINISHED"]
    return "\n".join(lines)


class LimitParsing(unittest.TestCase):
    def test_managed_zero_block_does_not_overwrite_default(self):
        out = parse_log_text(log(block("(default)", 42, 3100), block("Apttus", 0, 0)))
        lim = out["limits_final"]
        self.assertEqual(lim["SOQL queries"], {"used": 42, "max": 100})
        self.assertEqual(lim["DML statements"]["used"], 3)
        self.assertEqual(out["limits_by_namespace"]["Apttus"]["SOQL queries"]["used"], 0)

    def test_cap_is_always_present(self):
        out = parse_log_text(log(block("(default)", 5, 10)))
        for name, v in out["limits_final"].items():
            self.assertIsInstance(v.get("max"), int, name)

    def test_cpu_and_heap_are_captured(self):
        lim = parse_log_text(log(block("(default)", 5, 9876)))["limits_final"]
        self.assertEqual(lim["CPU time"], {"used": 9876, "max": 10000})
        self.assertIn("heap size", lim)

    def test_close_to_limit_suffix_is_parsed_and_flagged(self):
        lim = parse_log_text(log(block("(default)", 97, 10, close=True)))["limits_final"]
        self.assertEqual(lim["SOQL queries"]["used"], 97)
        self.assertTrue(lim["SOQL queries"]["close_to_limit"])

    def test_last_checkpoint_per_namespace_wins(self):
        out = parse_log_text(log(block("(default)", 2, 10), block("(default)", 30, 500)))
        self.assertEqual(out["limits_final"]["SOQL queries"]["used"], 30)

    def test_usage_drop_between_checkpoints_keeps_the_peak(self):
        # A log with two transactions: the busy one, then a tiny trailing one.
        # The final figure must stay honest (0) but the peak must survive.
        out = parse_log_text(log(block("(default)", 45, 8200), block("(default)", 0, 0)))
        soql = out["limits_final"]["SOQL queries"]
        self.assertEqual(soql["used"], 0)
        self.assertEqual(soql["peak_used"], 45)
        self.assertEqual(out["limits_final"]["CPU time"]["peak_used"], 8200)

    def test_monotonic_checkpoints_carry_no_peak(self):
        out = parse_log_text(log(block("(default)", 2, 10), block("(default)", 30, 500)))
        self.assertNotIn("peak_used", out["limits_final"]["SOQL queries"])

    def test_crlf_log(self):
        out = parse_log_text(log(block("(default)", 7, 10)).replace("\n", "\r\n"))
        self.assertEqual(out["limits_final"]["SOQL queries"]["used"], 7)

    def test_no_limit_block(self):
        out = parse_log_text("50.0 APEX_CODE,FINEST;APEX_PROFILING,NONE\n21:20:56.0 (100)|EXECUTION_STARTED")
        self.assertEqual(out["limits_final"], {})
        self.assertEqual(out["limits_by_namespace"], {})


if __name__ == "__main__":
    unittest.main()

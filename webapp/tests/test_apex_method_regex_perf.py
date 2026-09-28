"""
Regression: METHOD_DECL_RE used to backtrack catastrophically (cubic) on a
class whose tail is a long block of `//` comments -- strip_comments leaves
bare newlines with no `(`, and the old overlapping `[...\\s]+?\\s+` split every
whitespace run every possible way. ContactTriggerUtilityTest (one tiny method
+ commented-out code) hung an org fetch at 2791/3057 classes with no error.
"""
import time
import unittest

from app.extractors import apex
from app.extractors.common import strip_comments

HEAD = ("@isTest\nprivate class ContactTriggerUtilityTest {\n    @isTest\n"
        "    static void testNothing() {\n"
        "        ContactTriggerUtility ut = new ContactTriggerUtility();\n    }\n")


class MethodRegexPerf(unittest.TestCase):
    def _time(self, tail, n):
        code = strip_comments(HEAD + tail * n + "}")
        t = time.perf_counter()
        methods = apex.find_methods(code, "ContactTriggerUtilityTest")
        return time.perf_counter() - t, methods

    def test_comment_tail_is_linear(self):
        # old regex: ~24s at n=400; must now be well under a second at 5000
        elapsed, methods = self._time("    // ContactTriggerUtility.run(x);\n", 5000)
        self.assertLess(elapsed, 1.0)
        self.assertEqual([m["name"] for m in methods], ["testNothing"])

    def test_paren_free_word_run_is_linear(self):
        # a huge SOQL select list: tokens + whitespace, no `(`
        elapsed, _ = self._time("        Name, Foo__c, Bar__r.Baz__c,\n", 5000)
        self.assertLess(elapsed, 1.0)

    def test_declarations_still_parse(self):
        code = strip_comments(
            "public with sharing class A {\n"
            "  @AuraEnabled(cacheable=true)\n"
            "  public static List<Map<String, Object>> getRows(Id recId, String x) { return null; }\n"
            "  global virtual override Map<Id, List<SObject>> doIt ( ) { }\n"
            "  private A() { }\n"
            "  public virtual static void weird() {}\n"
            "  public void execute(QueueableContext ctx) { System.enqueueJob(new B()); }\n"
            "  public class Inner { public Inner(Integer a) {} }\n"
            "}")
        by = {m["name"]: m for m in apex.find_methods(code, "A")}
        self.assertEqual(set(by), {"getRows", "doIt", "A", "weird", "execute", "Inner"})
        self.assertEqual(by["getRows"]["annotations"], ["AuraEnabled"])
        self.assertTrue(by["getRows"]["is_static"])
        self.assertEqual(by["doIt"]["visibility"], "global")
        self.assertIsNone(by["A"]["returns"])
        self.assertTrue(by["weird"]["is_static"])
        self.assertEqual(by["execute"]["signature"], "execute(QueueableContext ctx)")


if __name__ == "__main__":
    unittest.main()

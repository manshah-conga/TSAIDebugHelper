"""Offline tests for evals/run_evals.grade -- no server or model needed.
The failing transcript is GPT-4o's real answer from 2026-09-23."""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "evals"))
from run_evals import grade, _parse_sse, build_messages  # noqa: E402

CASES = json.load(open(os.path.join(os.path.dirname(HERE), "evals", "field_value_cases.json")))["cases"]
CASE1 = next(c for c in CASES if c["id"] == "status_internal_review_via_variable")
FLOW = "CLM_Document_Version_Detail_AS_AgreementStageAutomation"
DECOY = "CLM_Agreement_SCR_SelfServicePathUpdateAgreementStageStatus"

GPT4O_ANSWER = (
    "The value 'Internal Review' is not explicitly listed as an example in the writers for "
    "Apttus__Status__c. ... There are no direct matches for the value 'Internal Review' in the "
    "knowledgebase for this org. To proceed: 1. If you have a debug log from when the value was "
    "set to 'Internal Review', I can analyze it to pinpoint the source.")
GPT4O_CALLS = [{"name": "find_field_writers", "args": {"field_api_name": "Apttus__Status__c"}},
               {"name": "search_knowledgebase", "args": {"query": "Internal Review"}}]

GOOD_ANSWER = (f"{FLOW} sets it: element Assign_Stage_Status_Default assigns 'Internal Review' "
               f"on the non-self-service path. Near-miss: {DECOY} writes 'Internal Review Complete'.")
GOOD_CALLS = [{"name": "find_field_writers", "args": {"field_api_name": "Apttus__Status__c"}},
              {"name": "get_component", "args": {"component_id": FLOW}}]


def test_gpt4o_transcript_fails_for_the_right_reasons():
    ok, failures, _ = grade(CASE1, GPT4O_ANSWER, GPT4O_CALLS)
    assert not ok
    joined = " | ".join(failures)
    assert "does not name expected" in joined
    assert "never called get_component" in joined
    assert "asked for a debug log" in joined


def test_correct_answer_passes_and_decoy_as_near_miss_is_only_a_warning():
    ok, failures, warnings = grade(CASE1, GOOD_ANSWER, GOOD_CALLS)
    assert ok, failures
    assert any("decoy" in w for w in warnings)


def test_decoy_instead_of_answer_fails():
    ok, failures, _ = grade(CASE1, f"It is {DECOY}.", GOOD_CALLS)
    assert not ok and any("decoy" in f for f in failures)


def test_right_name_without_opening_component_fails():
    ok, failures, _ = grade(CASE1, GOOD_ANSWER, GOOD_CALLS[:1])
    assert not ok and any("get_component" in f for f in failures)


def test_sse_parser_and_preamble_modes():
    lines = ["event: token", 'data: {"text": "hi"}', "", "event: done", "data: {}", ""]
    assert list(_parse_sse(lines)) == [("token", {"text": "hi"}), ("done", {})]
    assert build_messages("Q", "none", "P") == ["Q"]
    assert len(build_messages("Q", "prefix", "P")) == 1
    assert build_messages("Q", "turn", "P")[1] == "Q"

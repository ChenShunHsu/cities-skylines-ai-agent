#!/usr/bin/env python3

import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import qwen_crosscheck as crosscheck


class FakeModel:
    def __init__(self, answer):
        self.answer = answer
        self.prompts = []

    def ask(self, system, user, schema, kind="extract"):
        self.prompts.append((system, user, schema, kind))
        return self.answer, {"seconds": 0.0, "promptTokens": 1, "completionTokens": 1}


SNAPSHOT = {
    "cityHealth": {"population": 168, "cash": {"amountDisplay": 16566.25}},
    "problems": {"countsByProblem": {"NoWorkers": 1, "Water": 3}},
    "blocks": [{"blockId": 7, "zone": "R"}, {"blockId": 9, "zone": "I"}],
}


class ResolveTests(unittest.TestCase):
    def test_path_language(self):
        self.assertEqual(168, crosscheck.resolve(SNAPSHOT, "cityHealth.population"))
        self.assertEqual([7, 9], crosscheck.resolve(SNAPSHOT, "blocks[*].blockId"))
        self.assertEqual("I", crosscheck.resolve(SNAPSHOT, "blocks[blockId=9].zone"))
        self.assertEqual(7, crosscheck.resolve(SNAPSHOT, "blocks[0].blockId"))
        self.assertEqual([7], crosscheck.resolve(SNAPSHOT, "blocks[:1][*].blockId"))
        self.assertEqual(2, crosscheck.resolve(SNAPSHOT, "len(blocks)"))
        self.assertEqual(16, crosscheck.resolve(SNAPSHOT, "sum(blocks[*].blockId)"))
        self.assertEqual(["NoWorkers", "Water"], crosscheck.resolve(SNAPSHOT, "keys(problems.countsByProblem)"))

    def test_trim_keeps_focus_and_caps_lists(self):
        view = crosscheck.trim({"a": {"b": list(range(20))}, "c": 1}, ["a.b"], list_limit=3)
        self.assertEqual({"a.b": [0, 1, 2]}, view)


class EqualTests(unittest.TestCase):
    def test_numbers_strings_and_unordered_lists(self):
        self.assertTrue(crosscheck.equal(16566.25, "16,566.25"))
        self.assertTrue(crosscheck.equal(3, 3.0))
        self.assertTrue(crosscheck.equal(["Water", "NoWorkers"], ["noworkers", "water"]))
        self.assertFalse(crosscheck.equal(0.256, 0.26))
        self.assertFalse(crosscheck.equal([1, 2], [1, 2, 3]))

    def test_keys_listed_with_their_counts_match_the_keys(self):
        self.assertTrue(crosscheck.equal(["Garbage", "Water"], ['"Water": 8', '"Garbage": 42']))
        self.assertFalse(crosscheck.equal(["Garbage", "Water"], ['"Water": 8', '"Fire": 42']))


class CompareArgumentsTests(unittest.TestCase):
    PROPERTIES = {"limit": {"default": 500}, "service": {"default": ""}, "budget": {},
                  "checkpointName": {}, "expectedOutcome": {}}

    def test_added_defaults_are_benign_but_new_filters_are_semantic(self):
        diff = crosscheck.compare_arguments({}, {"limit": 500, "service": "Water"}, self.PROPERTIES)
        self.assertEqual(["limit"], diff["benignDefaults"])
        self.assertEqual("service", diff["semantic"][0]["key"])

    def test_controller_owned_and_free_text_never_count_as_disagreement(self):
        diff = crosscheck.compare_arguments(
            {"budget": 50, "checkpointName": "<controller>", "expectedOutcome": "x"},
            {"budget": 50, "checkpointName": "CityHall", "expectedOutcome": "y"},
            self.PROPERTIES, controller_owned={"checkpointName"}, free_text={"expectedOutcome"})
        self.assertNotIn("semantic", diff)
        self.assertEqual([{"checkpointName": "CityHall"}], diff["inventedControllerValues"])

    def test_missing_required_value_is_semantic(self):
        diff = crosscheck.compare_arguments({"budget": 50}, {}, self.PROPERTIES)
        self.assertEqual("budget", diff["semantic"][0]["key"])


class RunnerTests(unittest.TestCase):
    def test_extract_compares_every_answer_with_the_resolved_path(self):
        model = FakeModel({"population": 168, "problems": ["Water", "NoWorkers"], "cash": 16566.0})
        task = {"tool": "observe_city", "result": SNAPSHOT, "questions": [
            {"key": "population", "ask": "population", "path": "cityHealth.population", "type": "integer"},
            {"key": "problems", "ask": "problems", "path": "keys(problems.countsByProblem)", "type": "strings"},
            {"key": "cash", "ask": "cash", "path": "cityHealth.cash.amountDisplay"}]}
        outcome = crosscheck.run_extract(task, model, call_tool=None)
        self.assertEqual(2, outcome["matched"])
        self.assertFalse(next(c for c in outcome["checks"] if c["key"] == "cash")["match"])

    def test_extract_refuses_mutating_tools(self):
        with self.assertRaises(ValueError):
            crosscheck.run_extract({"tool": "zone_block", "result": {}, "questions": []}, FakeModel({}))

    def test_toolcall_executes_only_matching_read_only_calls(self):
        calls = []

        def call_tool(name, arguments):
            calls.append((name, arguments))
            return {"ok": True}

        model = FakeModel({"toolName": "inspect_water_service_coverage", "argumentsJson": '{"limit": 500}'})
        task = {"instruction": "list buildings missing water", "execute": True,
                "allowedTools": ["inspect_water_service_coverage"],
                "expected": {"toolName": "inspect_water_service_coverage", "arguments": {}}}
        outcome = crosscheck.run_toolcall(task, model, call_tool=call_tool)
        self.assertTrue(outcome["match"])
        self.assertEqual([("inspect_water_service_coverage", {"limit": 500})], calls)

        model = FakeModel({"toolName": "set_service_budget", "argumentsJson":
                           '{"checkpointName": "x", "service": "Water", "budget": 50, '
                           '"reason": "sustained-deficit", "expectedOutcome": "y"}'})
        task = {"instruction": "water budget 50", "execute": True, "allowedTools": ["set_service_budget"],
                "controllerOwned": ["checkpointName"], "freeText": ["expectedOutcome"],
                "expected": {"toolName": "set_service_budget", "arguments": {
                    "checkpointName": "c", "service": "Water", "budget": 50, "reason": "sustained-deficit"}}}
        outcome = crosscheck.run_toolcall(task, model, call_tool=call_tool)
        self.assertTrue(outcome["match"])
        self.assertFalse(outcome["executed"])
        self.assertEqual(1, len(calls))

    def test_select_and_journal_stats(self):
        model = FakeModel({"candidateId": "b", "reason": "r"})
        outcome = crosscheck.run_select({"instruction": "pick", "candidates": [
            {"candidateId": "a"}, {"candidateId": "b"}], "expectedCandidateId": "a"}, model)
        self.assertFalse(outcome["match"])
        self.assertEqual("select", model.prompts[-1][3])
        with tempfile.TemporaryDirectory() as folder:
            journal = Path(folder) / "j.jsonl"
            crosscheck.append_journal(journal, "m", outcome)
            crosscheck.append_journal(journal, "m", {**outcome, "matched": 1})
            self.assertEqual({"select": {"tasks": 2, "matched": 1, "total": 2}}, crosscheck.journal_stats(journal))


if __name__ == "__main__":
    unittest.main()

"""Offline contract and decision regressions; no live game or model required."""

import copy
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from city_assessment import assess_city
import mayor_tools


def snapshot():
    return {
        "ok": True,
        "city": {"instanceId": "test-city", "name": "Fixture"},
        "snapshot": {"gameTime": "2050-01-02T00:00:00", "frame": 2},
        "summary": {"ok": True, "population": 1000,
                    "demand": {"residential": 70, "commercial": 20, "workplace": 10}},
        "cityHealth": {"ok": True, "cash": {"amount": 30000000, "periodNet": 10000},
                       "wellbeing": {"happiness": 75}, "employment": {"unemployment": 5},
                       "services": {name: {"used": 0, "capacity": 0} for name in ("healthcare", "elementaryEducation", "highSchoolEducation", "universityEducation")},
                       "utilities": {name: {"used": 40, "capacity": 100} for name in ("electricity", "water", "sewage")}},
        "problems": {"ok": True, "total": 0, "countsByProblem": {}, "problems": []},
        "roadAnomalies": {"ok": True, "total": 0},
        "buildingAnomalies": {"ok": True, "total": 0},
        "traffic": {"ok": True, "severeSegments": 0},
    }


class CityAssessmentTests(unittest.TestCase):
    def test_growth_before_single_deficit_tax_tinkering(self):
        state = snapshot()
        state["cityHealth"]["cash"]["periodNet"] = -25502
        result = assess_city(state)
        self.assertEqual("growth-residential", result["nextStep"]["id"])
        self.assertEqual(300000, result["metrics"]["cashDisplay"])
        self.assertEqual(-255.02, result["metrics"]["periodNetDisplay"])
        self.assertFalse(result["metrics"]["sustainedDeficit"])
        self.assertFalse(result["mutationAuthorized"])

    def test_fire_outranks_cash_and_demand_even_without_returned_details(self):
        state = snapshot()
        state["problems"].update(total=7, countsByProblem={"Fire": 2}, returned=0)
        state["cityHealth"]["cash"]["amount"] = 100
        result = assess_city(state)
        self.assertEqual("resident-problems", result["nextStep"]["id"])
        self.assertEqual(2, result["metrics"]["problemCounts"]["Fire"])

    def test_zero_capacity_is_bootstrap_not_healthy(self):
        state = snapshot()
        state["summary"]["population"] = 0
        state["cityHealth"]["utilities"]["water"] = {"used": 0, "capacity": 0}
        result = assess_city(state)
        self.assertEqual("bootstrap", result["phase"])
        self.assertEqual("bootstrap-neighborhood", result["nextStep"]["id"])
        self.assertEqual("not-established", result["metrics"]["utilities"]["water"]["state"])

    def test_empty_city_keeps_complete_bootstrap_goal_over_generic_stub_signals(self):
        state = snapshot()
        state['summary']['population'] = 0
        state['roadAnomalies'].update(total=3, counts={'shortRoadStub':2,'deadEndNearRoad':1})
        result = assess_city(state)
        self.assertEqual('bootstrap-neighborhood', result['nextStep']['id'])
        self.assertIn('inspect_road_result', result['nextStep']['inspectTools'])
        self.assertTrue(any(o['id']=='roadAnomalies' for o in result['objectives']))
        self.assertFalse(result['mutationAuthorized'])
        state['problems'].update(total=1,countsByProblem={'Fire':1})
        self.assertEqual('resident-problems',assess_city(state)['nextStep']['id'])

    def test_missing_and_failed_nested_endpoints_are_unknown(self):
        for state in ({}, {"ok": False}, {**snapshot(), "cityHealth": {"ok": False, "cash": {"amount": 9999999}}}):
            with self.subTest(state=state):
                result = assess_city(state)
                self.assertEqual("missing-evidence", result["nextStep"]["id"])
                self.assertIsNone(result["metrics"]["cashDisplay"])
                self.assertEqual("unknown", result["dimensions"]["finance"])

    def test_invalid_nonfinite_and_boolean_values_are_not_metrics(self):
        for value in (float("nan"), float("inf"), True, "1000", None):
            state = snapshot()
            state["summary"]["population"] = value
            result = assess_city(state)
            self.assertIsNone(result["metrics"]["population"])
            json.dumps(result, allow_nan=False)
        for value in (-1, float("nan"), True):
            with self.assertRaises(ValueError):
                assess_city(snapshot(), reserve_display=value)

    def test_invalid_problem_count_map_is_missing_evidence(self):
        for value in (None, [], {"Fire": True}, {"Fire": -1}, {"Fire": float("nan")}):
            state = snapshot()
            state["problems"]["countsByProblem"] = value
            result = assess_city(state)
            self.assertIn("problems.countsByProblem", result["missingEvidence"])
            self.assertEqual("missing-evidence", result["nextStep"]["id"])
            json.dumps(result, allow_nan=False)

    def test_same_city_later_game_time_required_for_trends(self):
        previous = snapshot()
        previous["cityHealth"]["cash"]["periodNet"] = -200
        current = copy.deepcopy(previous)
        current["summary"]["population"] = 1010
        current["summary"]["demand"]["residential"] = 60
        current["snapshot"]["gameTime"] = "2050-01-03T00:00:00"
        result = assess_city(current, previous)
        self.assertEqual(10, result["metrics"]["populationDelta"])
        self.assertEqual(10, result["metrics"]["populationDeltaPerGameDay"])
        self.assertEqual(-10, result["metrics"]["demandDelta"]["residential"])
        self.assertEqual(
            -10, result["metrics"]["demandDeltaPerGameDay"]["residential"]
        )
        self.assertEqual(1, result["comparison"]["elapsedGameDays"])
        self.assertTrue(result["metrics"]["sustainedDeficit"])
        for bad_time in ("2050-01-02T00:00:00", "2049-01-01T00:00:00", "bad"):
            current["snapshot"]["gameTime"] = bad_time
            self.assertFalse(assess_city(current, previous)["comparison"]["comparable"])
        current["snapshot"]["gameTime"] = "2050-01-03T00:00:00"
        current["city"]["instanceId"] = "other-save"
        self.assertIsNone(assess_city(current, previous)["metrics"]["populationDelta"])

    def test_assessment_does_not_modify_input_or_double_convert_money(self):
        state = snapshot()
        state["cityHealth"]["cash"]["amountDisplay"] = 300000
        original = copy.deepcopy(state)
        first = assess_city(state)
        self.assertEqual(original, state)
        first["baseline"]["city"]["instanceId"] = "changed"
        self.assertEqual(original, state)
        self.assertEqual(300000, first["metrics"]["cashDisplay"])

    def test_snapshot_capture_wrapper_and_baseline_round_trip(self):
        previous = snapshot()
        baseline = assess_city({"mayorSnapshot": previous})["baseline"]
        current = snapshot()
        current["snapshot"]["gameTime"] = "2050-01-03T00:00:00"
        self.assertTrue(assess_city(current, baseline)["comparison"]["comparable"])

    def test_repeated_bottleneck_requests_evidence_instead_of_endless_wait(self):
        state = snapshot()
        state["problems"].update(total=2, countsByProblem={"Fire": 2})
        first = assess_city(state)
        state["snapshot"]["gameTime"] = "2050-01-03T00:00:00"
        second = assess_city(state, first["baseline"])
        state["snapshot"]["gameTime"] = "2050-01-04T00:00:00"
        third = assess_city(state, second["baseline"])
        self.assertEqual("resolve-blocker", third["nextStep"]["kind"])
        state["problems"]["total"] = 1
        state["snapshot"]["gameTime"] = "2050-01-05T00:00:00"
        self.assertEqual(1, assess_city(state, third["baseline"])["nextStep"]["unchangedObservations"])

    def test_wait_is_valid_and_is_never_permission_to_resume(self):
        state = snapshot()
        state["summary"]["demand"] = dict.fromkeys(("residential", "commercial", "workplace"), 0)
        result = assess_city(state)
        self.assertEqual("wait", result["nextStep"]["id"])
        self.assertFalse(result["mutationAuthorized"])
        self.assertIn("unverified", result["dimensions"]["basicSupply"])

    def test_services_do_not_misclassify_landfill_as_missing_incinerator(self):
        state = snapshot()
        state["cityHealth"]["services"].update({"healthcare": {"used": 10, "capacity": 0}, "deathcare": {"used": 10, "capacity": 0}})
        state["cityHealth"]["utilities"]["garbage"] = {"used": 10, "capacity": 0}
        result = assess_city(state)
        self.assertEqual("service-healthcare", result["nextStep"]["id"])
        self.assertNotIn("service-deathcare", [item["id"] for item in result["objectives"]])

    def test_full_landfill_storage_becomes_an_objective_without_incinerators(self):
        # A landfill-only city reports incineration capacity 0; storage fill is the signal.
        state = snapshot()
        state["cityHealth"]["utilities"]["garbage"] = {
            "used": 112832, "capacity": 0, "storage": {"amount": 9300, "capacity": 10000}}
        state["cityHealth"]["services"]["deathcare"] = {
            "used": 50, "capacity": 0, "storage": {"amount": 50, "capacity": 6000}}
        ids = [item["id"] for item in assess_city(state)["objectives"]]
        self.assertIn("landfill-storage", ids)
        self.assertNotIn("cemetery-storage", ids)
        landfill = next(item for item in assess_city(state)["objectives"] if item["id"] == "landfill-storage")
        self.assertEqual(75, landfill["priority"])

        state["cityHealth"]["utilities"]["garbage"]["storage"]["amount"] = 9900
        self.assertEqual("landfill-storage", assess_city(state)["nextStep"]["id"])
        del state["cityHealth"]["utilities"]["garbage"]["storage"]
        self.assertNotIn("landfill-storage", [item["id"] for item in assess_city(state)["objectives"]])

    def test_mostly_full_landfills_become_an_objective_despite_combined_headroom(self):
        # Two fresh landfills can hide seven full ones: only the fresh ones still collect.
        state = snapshot()
        state["cityHealth"]["utilities"]["garbage"] = {"used": 147344, "capacity": 0, "storage": {
            "amount": 56_000_000, "capacity": 72_000_000, "facilities": 9, "fullFacilities": 7}}
        landfill = next(item for item in assess_city(state)["objectives"] if item["id"] == "landfill-storage")
        self.assertEqual(80, landfill["priority"])
        self.assertIn("7 of 9 landfills are full", landfill["reason"])
        self.assertIn("preview_bulldoze_impact", landfill["inspectTools"])

        state["cityHealth"]["utilities"]["garbage"]["storage"]["fullFacilities"] = 9
        landfill = next(item for item in assess_city(state)["objectives"] if item["id"] == "landfill-storage")
        self.assertEqual(90, landfill["priority"])

        state["cityHealth"]["utilities"]["garbage"]["storage"]["fullFacilities"] = 2
        self.assertNotIn("landfill-storage", [item["id"] for item in assess_city(state)["objectives"]])

    def test_road_access_failures_point_at_overlong_segments(self):
        state = snapshot()
        state["problems"].update(total=2, countsByProblem={"RoadAccessFailed": 2})
        state["roadAnomalies"] = {"ok": True, "total": 3, "counts": {"overlongRoadSegment": 2, "deadEndRoad": 1},
                                  "anomalies": [{"type": "deadEndRoad", "nodeId": 4},
                                                {"type": "overlongRoadSegment", "segmentId": 27},
                                                {"type": "overlongRoadSegment", "segmentId": 18}]}
        access = next(item for item in assess_city(state)["objectives"] if item["id"] == "road-access")
        self.assertEqual(90, access["priority"])
        self.assertIn("split_road_segment", access["reason"])
        self.assertIn("Overlong road segments: 2, including [27, 18]", access["reason"])

        state["problems"].update(total=1, countsByProblem={"Fire": 1})
        self.assertNotIn("road-access", [item["id"] for item in assess_city(state)["objectives"]])

    def test_locked_service_need_does_not_outrank_actionable_employment(self):
        state = snapshot()
        state["progression"] = {"capabilities": {"education": False, "healthcare": True}}
        state["cityHealth"]["services"]["elementaryEducation"] = {"used": 5, "capacity": 0}
        state["cityHealth"]["employment"]["unemployment"] = 100
        result = assess_city(state)
        self.assertEqual("employment", result["nextStep"]["id"])
        locked = next(item for item in result["objectives"] if item["id"] == "service-elementaryEducation")
        self.assertEqual("locked", locked["blockedBy"])
        self.assertEqual(["inspect_toolbar_unlocks"], locked["inspectTools"])

        state["progression"]["capabilities"]["education"] = True
        self.assertEqual("service-elementaryEducation", assess_city(state)["nextStep"]["id"])
        del state["progression"]
        self.assertEqual("service-elementaryEducation", assess_city(state)["nextStep"]["id"])

    def test_locked_university_does_not_outrank_growth(self):
        # Education unlocks with elementary schools; universities need a later milestone.
        state = snapshot()
        state["progression"] = {"capabilities": {"education": True, "highSchool": True, "university": False}}
        state["cityHealth"]["services"]["universityEducation"] = {"used": 3147, "capacity": 0}
        result = assess_city(state)
        self.assertEqual("growth-residential", result["nextStep"]["id"])
        locked = next(item for item in result["objectives"] if item["id"] == "service-universityEducation")
        self.assertEqual("locked", locked["blockedBy"])

        state["progression"]["capabilities"]["university"] = True
        self.assertEqual("service-universityEducation", assess_city(state)["nextStep"]["id"])
        del state["progression"]["capabilities"]["university"]  # older Bridge: shared education flag
        self.assertEqual("service-universityEducation", assess_city(state)["nextStep"]["id"])

    def test_complete_network_only_problems_do_not_outrank_bootstrap(self):
        state = snapshot()
        state["summary"]["population"] = 0
        node = {"entityType": "netNode", "problems": "RoadNotConnected"}
        state["problems"].update(total=2, returned=2, countsByProblem={"RoadNotConnected": 2},
                                 problems=[{**node, "id": 1}, {**node, "id": 2}])
        result = assess_city(state)
        self.assertEqual("bootstrap-neighborhood", result["nextStep"]["id"])
        self.assertIn("network-problems", [item["id"] for item in result["objectives"]])
        state["progression"] = {"capabilities": {"electricity": False, "water": False}}
        state["cityHealth"]["utilities"]["water"] = {"used": 0, "capacity": 0}
        water = next(item for item in assess_city(state)["objectives"] if item["id"] == "water")
        self.assertEqual("locked", water["blockedBy"])
        # A truncated list could hide a building problem, so it stays urgent.
        state["problems"].update(total=3)
        self.assertEqual("resident-problems", assess_city(state)["nextStep"]["id"])

    def test_planning_hints_explain_early_deficit_and_utility_oversupply(self):
        state = snapshot()
        state["summary"]["population"] = 55
        state["progression"] = {"capabilities": {"education": False, "police": False, "bus": False}}
        state["cityHealth"]["cash"]["periodNet"] = -72548
        state["cityHealth"]["utilities"]["water"] = {"used": 752, "capacity": 120000}
        hints = {item["id"]: item for item in assess_city(state)["planningHints"]}
        self.assertIn("Little Hamlet", hints["next-milestone"]["text"])
        self.assertIn("early-deficit", hints)
        self.assertEqual(50, hints["water-budget-oversupply"]["suggestedBudget"])
        self.assertNotIn("electricity-budget-oversupply", hints)  # 40% used is not oversupply
        self.assertLessEqual(len(hints), 4)

    def test_planning_hints_stay_silent_without_deficit_or_known_progression(self):
        state = snapshot()
        hints = assess_city(state)["planningHints"]
        self.assertEqual([], hints)

    def test_live_tool_uses_only_get_and_does_not_touch_memory(self):
        with patch.object(mayor_tools, "_request", return_value=snapshot()) as request, patch.object(mayor_tools, "_save_memory") as save:
            result = mayor_tools.call_tool("assess_city", {})
            request.assert_called_once_with("GET", "/state/mayor-snapshot", query={"detailLimit": 0})
            save.assert_not_called()
            self.assertFalse(result["offline"])

    def test_profile_and_on_demand_definitions_keep_all_tools_available(self):
        full = mayor_tools._select_definitions()
        compact = mayor_tools._select_definitions("mayor")
        self.assertEqual(6, len(compact))
        self.assertLess(len(compact), len(full))
        self.assertEqual("build_network", mayor_tools._select_definitions(names=["build_network"])[0]["function"]["name"])
        with self.assertRaises(ValueError):
            mayor_tools._select_definitions(names=["nonexistent"])

    def test_every_recommended_inspection_tool_exists(self):
        state = snapshot()
        state["cityHealth"]["cash"]["amount"] = 0
        state["cityHealth"]["wellbeing"]["happiness"] = 1
        state["cityHealth"]["employment"]["unemployment"] = 50
        state["traffic"]["severeSegments"] = 3
        state["problems"]["total"] = 4
        state["roadAnomalies"]["total"] = 2
        state["buildingAnomalies"]["total"] = 2
        names = {item["function"]["name"] for item in mayor_tools._definitions()}
        for objective in assess_city(state)["objectives"]:
            self.assertTrue(set(objective["inspectTools"]) <= names)

    def test_cli_handles_missing_file_with_json_error(self):
        completed = subprocess.run([sys.executable, str(Path(__file__).with_name("city_assessment.py")), "--snapshot", "missing-fixture.json"], capture_output=True, text=True)
        self.assertEqual(1, completed.returncode)
        self.assertFalse(json.loads(completed.stdout)["ok"])

    def test_invalid_counts_do_not_report_healthy_residents(self):
        state = snapshot()
        state["problems"]["countsByProblem"] = {"ok": False}
        result = assess_city(state)
        self.assertEqual("unknown", result["dimensions"]["residents"])
        self.assertIn("problems.countsByProblem", result["missingEvidence"])

    def test_negative_counts_and_capacity_are_unknown_but_negative_cash_is_valid(self):
        state = snapshot()
        state["cityHealth"]["cash"]["amount"] = -100
        state["problems"]["total"] = -1
        state["traffic"]["severeSegments"] = -1
        state["cityHealth"]["utilities"]["water"]["used"] = -1
        result = assess_city(state)
        self.assertEqual(-1, result["metrics"]["cashDisplay"])
        for dimension in ("residents", "traffic", "basicSupply"):
            self.assertEqual("unknown", result["dimensions"][dimension])

    def test_service_telemetry_failure_requires_evidence(self):
        state = snapshot()
        state["cityHealth"]["services"]["healthcare"] = {"ok": False, "used": 0, "capacity": 100}
        result = assess_city(state)
        self.assertIn("cityHealth.services.healthcare.used", result["missingEvidence"])
        self.assertEqual("missing-evidence", result["nextStep"]["id"])

    def test_invalid_identity_and_time_cannot_make_comparable_baselines(self):
        for identity in (None, True, 123, [], {}, " "):
            state = snapshot()
            state["city"]["instanceId"] = identity
            result = assess_city(state, state)
            self.assertIn("city.instanceId", result["missingEvidence"])
            self.assertFalse(result["comparison"]["comparable"])
        state = snapshot()
        state["snapshot"]["gameTime"] = "invalid"
        self.assertIn("snapshot.gameTime", assess_city(state)["missingEvidence"])

    def test_failed_capture_wrapper_does_not_expose_stale_healthy_payload(self):
        result = assess_city({"ok": False, "mayorSnapshot": snapshot()})
        self.assertIsNone(result["metrics"]["cashDisplay"])
        self.assertEqual("missing-evidence", result["nextStep"]["id"])

    def test_entire_result_remains_strict_json_with_invalid_metadata(self):
        state = snapshot()
        state["snapshot"]["frame"] = float("nan")
        state["city"]["extra"] = float("inf")
        state["summary"]["population"] = 10 ** 400
        result = assess_city(state)
        self.assertIsNone(result["metrics"]["population"])
        json.dumps(result, allow_nan=False)

    def test_paused_reads_preserve_but_do_not_increment_blocker_history(self):
        state = snapshot()
        state["problems"].update(total=2, countsByProblem={"Fire": 2})
        first = assess_city(state)
        state["snapshot"]["gameTime"] = "2050-01-03T00:00:00"
        second = assess_city(state, first["baseline"])
        for _ in range(4):
            second = assess_city(state, second["baseline"])
            self.assertEqual(2, second["nextStep"]["unchangedObservations"])
        state["snapshot"]["gameTime"] = "2050-01-04T00:00:00"
        third = assess_city(state, second["baseline"])
        self.assertEqual("resolve-blocker", third["nextStep"]["kind"])


if __name__ == "__main__":
    unittest.main()

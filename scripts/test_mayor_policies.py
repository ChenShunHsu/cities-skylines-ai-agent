#!/usr/bin/env python3

import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from mayor import policies


def snapshot(*, game_time="2027-02-02T00:00:00", demand=None, utilities=None, cash=None):
    return {
        "ok": True,
        "city": {"instanceId": "fixture-city"},
        "snapshot": {"gameTime": game_time},
        "summary": {"demand": demand or {}},
        "cityHealth": {
            "utilities": utilities or {},
            "cash": cash or {},
        },
    }


class MayorPolicyTests(unittest.TestCase):
    def test_derives_metrics_with_provenance_and_comparable_rates(self):
        previous = snapshot(
            game_time="2027-02-01T00:00:00",
            demand={"residential": 80, "commercial": 10, "workplace": 20},
        )
        current = snapshot(
            demand={"residential": 90, "commercial": 8, "workplace": 25},
            utilities={
                "electricity": {"used": 90, "capacity": 100},
                "water": {"used": 40, "capacity": 100},
                "sewage": {"used": 30, "capacity": 100},
            },
            cash={"amount": 2_000_000, "periodNet": -50_000},
        )
        zones = {
            "capacityByZone": [
                {"zone": "ResidentialLow", "occupiedCells": 45, "vacantCells": 5},
                {"zone": "Industrial", "occupiedCells": 10, "vacantCells": 10},
            ]
        }
        result = policies.derive_metrics(current, previous, zones=zones)
        electricity = result["utilities"]["electricity"]
        self.assertEqual("known", electricity["utilization"]["status"])
        self.assertEqual(0.9, electricity["utilization"]["value"])
        self.assertEqual(10, electricity["headroom"]["value"])
        self.assertEqual("fixture-city", electricity["utilization"]["instanceId"])
        self.assertEqual("mayor-policies-v1", electricity["utilization"]["policyVersion"])
        self.assertEqual(10, result["demandTrendPerGameDay"]["residential"]["value"])
        self.assertEqual(0.9, result["frontageSaturation"]["residential"]["value"])
        self.assertEqual(10_000, result["cashAboveReserve"]["value"])
        self.assertEqual(20, result["deficitRunway"]["value"])
        self.assertFalse(result["mutationAuthorized"])

    def test_unknown_values_fail_closed_with_reason_codes(self):
        result = policies.derive_metrics(snapshot())
        self.assertEqual(
            "missing_or_invalid_utility_telemetry",
            result["utilities"]["electricity"]["utilization"]["reasonCode"],
        )
        self.assertEqual(
            "snapshots_not_comparable",
            result["demandTrendPerGameDay"]["residential"]["reasonCode"],
        )
        self.assertEqual(
            "capacity_by_zone_unavailable",
            result["frontageSaturation"]["residential"]["reasonCode"],
        )
        evaluation = policies.evaluate_policies(result)
        self.assertFalse(evaluation["alerts"])
        self.assertEqual(3, len(evaluation["unknownMetrics"]))

    def test_capacity_policy_emits_only_fixed_read_only_candidates(self):
        current = snapshot(
            utilities={
                "electricity": {"used": 95, "capacity": 100},
                "water": {"used": 20, "capacity": 100},
                "sewage": {"used": 20, "capacity": 100},
            }
        )
        evaluation = policies.evaluate_policies(policies.derive_metrics(current))
        self.assertEqual(1, len(evaluation["alerts"]))
        alert = evaluation["alerts"][0]
        self.assertEqual("electricity_capacity_pressure", alert["policyId"])
        self.assertLess(alert["exitWhen"]["value"], alert["enterWhen"]["value"])
        packet = policies.compact_decision_packet(
            "mission-1", "collect_delivery_evidence", evaluation, remaining_steps=4
        )
        self.assertEqual(
            ["inspect-electricity-capacity", "inspect-electricity-delivery"],
            [item["candidateId"] for item in packet["candidates"]],
        )
        self.assertFalse(evaluation["mutationAuthorized"])
        self.assertNotIn("instanceId", json.dumps(packet["candidates"]))
        self.assertNotIn("position", json.dumps(packet["candidates"]))

    def test_hysteresis_keeps_active_alert_until_exit_threshold(self):
        current = snapshot(
            utilities={
                "electricity": {"used": 85, "capacity": 100},
                "water": {"used": 20, "capacity": 100},
                "sewage": {"used": 20, "capacity": 100},
            }
        )
        metrics = policies.derive_metrics(current)
        self.assertFalse(policies.evaluate_policies(metrics)["alerts"])
        active = policies.evaluate_policies(
            metrics, active_policy_ids={"electricity_capacity_pressure"}
        )
        self.assertEqual("continued", active["alerts"][0]["transition"])

    def test_policy_config_rejects_expression_and_missing_hysteresis(self):
        invalid = {
            "policyVersion": "mayor-policies-v1",
            "policies": [{
                "id": "electricity_capacity_pressure",
                "metric": "utilities.electricity.utilization",
                "enterWhen": {"operator": "gte", "value": 0.9, "expression": "exec()"},
                "exitWhen": {"operator": "lte", "value": 0.95},
                "consecutiveSamples": 1,
                "cooldownGameDays": 1,
                "severity": "high",
                "candidateActionClass": "read_only_inspection",
                "recheckCondition": "later",
            }],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.json"
            path.write_text(json.dumps(invalid), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "enterWhen"):
                policies.load_policy_config(path)


if __name__ == "__main__":
    unittest.main()

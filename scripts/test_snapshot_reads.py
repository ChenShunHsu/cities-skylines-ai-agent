"""The full mayor snapshot is read once a wake, not once or twice per change.

/state/mayor-snapshot reruns every anomaly scan on the game thread (live: 3.8 s with
detailLimit=0); a wake of 15-27 changes read it 40-65 times, and one wake stopped when such a
read timed out. These tests drive the real request path (mayor_tools._request) against a fake
Bridge and count what reaches it.
"""

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from loop import city as city_module
from loop import land, protocol, services, utilities, wake
from loop.city import mt

IDENTITY = {"instanceId": "fake-city", "name": "Fake", "map": "Fake Map"}
LATE = ("ResidentialHigh", "Office", "HealthCare", "Education", "PublicTransportBus", "PublicTransportMetro",
        "PublicTransportTrain")


def unlocks(milestone: str = "Milestone4", late: bool = False) -> dict:
    def entries(*names):
        return [{"name": name, "value": index, "unlocked": late or name not in LATE} for index, name in enumerate(names)]
    return {"ok": True, "currentMilestone": milestone,
            "nextMilestone": {"name": milestone, "passed": False, "requirements": []} if milestone else None,
            "zones": entries("ResidentialLow", "ResidentialHigh", "CommercialLow", "Industrial", "Office"),
            "services": entries("Residential", "Electricity", "Water", "HealthCare", "Education"),
            "subServices": entries("PublicTransportBus", "PublicTransportMetro", "PublicTransportTrain",
                                   "PublicTransportTaxi")}


def capabilities(answer: dict) -> dict:
    """The snapshot's capability flags for the same unlock state (MayorSnapshotCommands)."""
    unlocked = {item["name"] for group in ("zones", "services", "subServices") for item in answer[group]
                if item["unlocked"]}
    return {"highDensityResidential": "ResidentialHigh" in unlocked, "office": "Office" in unlocked,
            "healthcare": "HealthCare" in unlocked, "education": "Education" in unlocked,
            "bus": "PublicTransportBus" in unlocked, "metro": "PublicTransportMetro" in unlocked,
            "train": "PublicTransportTrain" in unlocked, "electricity": True, "water": True}


HEALTH = {"ok": True, "cash": {"amount": 5211241, "periodNet": -1300}, "population": 13359,
          "utilities": {"electricity": {"used": 180336, "capacity": 518720, "utilization": 0.348},
                        "water": {"used": 240368, "capacity": 300000, "utilization": 0.801},
                        "sewage": {"used": 240368, "capacity": 360000, "utilization": 0.668}},
          "services": {"healthcare": {"used": 112, "capacity": 300, "utilization": 0.373}}}
SUMMARY = {"ok": True, "gameTime": "2044-10-12T07:16:17", "population": 13359,
           "demand": {"residential": 70, "commercial": 50, "workplace": 42}}


class FakeBridge:
    """Answers the requests a wake sends; counts each one."""

    def __init__(self, unlock_answer: dict | None = None, health: dict | None = None):
        self.unlocks = unlock_answer or unlocks()
        self.health = health or HEALTH
        self.sent: list[tuple[str, str]] = []

    def snapshot(self) -> dict:
        return {"ok": True, "snapshot": {"gameTime": SUMMARY["gameTime"]}, "city": dict(IDENTITY),
                "progression": {"currentMilestone": self.unlocks["currentMilestone"],
                                "nextMilestone": self.unlocks["nextMilestone"],
                                "capabilities": capabilities(self.unlocks)},
                "summary": copy.deepcopy(SUMMARY), "cityHealth": copy.deepcopy(self.health),
                "problems": {"countsByProblem": {}}}

    def __call__(self, method, path, query, body):
        self.sent.append((method, path))
        answers = {
            "/state/mayor-snapshot": self.snapshot,
            "/state/unlocks": lambda: copy.deepcopy(self.unlocks),
            "/state/city-health": lambda: copy.deepcopy(self.health),
            "/state/summary": lambda: copy.deepcopy(SUMMARY),
            "/state/budgets": lambda: {"budgets": []},
            "/state/regression-report": lambda: {"ok": True, "delta": {"cash": 0}},
            "/commands/create-checkpoint": lambda: {"ok": True, "saveName": body["name"]},
            "/commands/capture-regression-baseline": lambda: {"ok": True, "name": body["name"], "baseline": {"cash": 1}},
            "/commands/set-simulation-speed": lambda: {"ok": True},
            "/commands/bulldoze": lambda: {"ok": True},
            "/commands/set-zone": lambda: {"ok": True, "changedCells": 4},
            "/commands/set-tax-rate": lambda: {"ok": True},
        }
        return answers[path]()

    def count(self, path: str) -> int:
        return sum(1 for _, sent in self.sent if sent == path)


class BridgeCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        mt.forget_city()
        self.addCleanup(mt.forget_city)
        protocol.new_batch()
        self.addCleanup(protocol.new_batch)
        for patch in (mock.patch.object(mt, "MEMORY_PATH", self.folder / "mayor-state.json"),
                      mock.patch.object(mt, "REGRESSION_BASELINES_PATH", self.folder / "baselines.json"),
                      mock.patch.object(mt, "_checkpoint_exists", return_value=True),
                      mock.patch.object(mt, "_wait_checkpoint", return_value=True),
                      mock.patch.object(protocol, "wait_save")):
            patch.start()
            self.addCleanup(patch.stop)

    def bridge(self, *args, **kwargs) -> FakeBridge:
        fake = FakeBridge(*args, **kwargs)
        patch = mock.patch.object(mt, "_send", side_effect=fake)
        patch.start()
        self.addCleanup(patch.stop)
        return fake


class WakeSnapshotReadTests(BridgeCase):
    def run_wake(self, changes: int) -> FakeBridge:
        mt.MEMORY_PATH.unlink(missing_ok=True)  # no fiscal cooldown left by an earlier wake
        fake = self.bridge()
        city = city_module.current(self.folder)
        wake.wake(city, 0.0)
        outcomes = []
        for index in range(1, changes + 1):
            tool, arguments = (("zone_block", {"blockId": index, "zone": "ResidentialHigh"}) if index % 2
                               else ("bulldoze_entity", {"entityType": "building", "id": index}))
            outcomes.append(protocol.run(f"change-{index}", tool, arguments))
        outcomes.append(protocol.run("tax", "set_tax_rate", {
            "service": "Residential", "rate": 10, "reason": "sustained-deficit", "expectedOutcome": "a smaller deficit"}))
        # what the loop's own steps read between the changes
        services.cash()
        utilities.cash_display()
        with mock.patch.object(utilities, "trim_budgets", return_value=[]), \
                mock.patch.object(utilities, "tax_rate", return_value=9):
            refused = utilities.balance("label")  # a second fiscal change: the cooldown refuses it
        self.assertTrue(all(outcome["ok"] for outcome in outcomes), outcomes)
        self.assertIn("cooldown", refused[0]["error"])
        return fake

    def test_a_wake_reads_the_snapshot_twice_however_many_changes_it_makes(self):
        for changes in (1, 20):
            with self.subTest(changes=changes):
                fake = self.run_wake(changes)
                # once for the city, once for the wake's reading. Before, this same wake read it
                # 13 times for 1 change and 51 for 20: 2 per change (baseline identity,
                # progression), more per fiscal change (memory), and the cash and tax readings
                self.assertEqual(2, fake.count("/state/mayor-snapshot"))
                self.assertEqual(changes + 2, fake.count("/commands/capture-regression-baseline"))
                self.assertEqual(changes + 2, fake.count("/state/unlocks"))  # each change still checks
                mt.forget_city()
                protocol.new_batch()

    def test_records_are_keyed_with_the_city_the_wake_read(self):
        self.bridge()
        city_module.current(self.folder)
        protocol.run("one", "bulldoze_entity", {"entityType": "building", "id": 1})
        stored = mt.mayor_regression.load_baseline(mt.REGRESSION_BASELINES_PATH, "one-baseline")
        self.assertEqual(IDENTITY, stored["cityIdentity"])
        state, scope = mt._load_city_scoped_memory()
        self.assertEqual(IDENTITY, scope["liveCityIdentity"])

    def test_a_process_that_has_not_read_the_city_reads_it_live_every_time(self):
        fake = self.bridge()  # a planner calling tools, not a wake
        mt.call_tool("capture_regression_baseline", {"name": "a"})
        mt.call_tool("capture_regression_baseline", {"name": "b"})
        mt._load_city_scoped_memory()
        self.assertEqual(3, fake.count("/state/mayor-snapshot"))

    def test_a_snapshot_without_a_city_is_not_remembered(self):
        self.assertIsNone(mt.remember_city({"ok": True, "city": {"instanceId": ""}}))
        fake = self.bridge()
        mt.call_tool("capture_regression_baseline", {"name": "a"})
        self.assertEqual(1, fake.count("/state/mayor-snapshot"))

    def test_restart_recovery_still_compares_against_the_live_city(self):
        mt.remember_city({"city": dict(IDENTITY)})
        fake = self.bridge()
        mt.mayor_regression.save_baseline(mt.REGRESSION_BASELINES_PATH, "lost", IDENTITY, {"cash": 1})
        lost = mt.BridgeRequestError({"ok": False, "error": "Regression baseline was not found"})
        answer = fake.__call__

        def send(method, path, query, body):
            if path == "/state/regression-report":
                fake.sent.append((method, path))
                raise lost
            return answer(method, path, query, body)
        with mock.patch.object(mt, "_send", side_effect=send), \
                mock.patch.object(mt.mayor_regression, "compare", return_value={"ok": True}):
            result = mt.call_tool("verify_regression", {"name": "lost"})
        self.assertEqual("persistentRestartRecovery", result["baselineSource"])
        self.assertEqual(1, fake.count("/state/mayor-snapshot"))


class ProgressionCheckTests(BridgeCase):
    def test_the_light_reads_give_the_snapshot_verdict(self):
        cases = {"normal": (unlocks(), HEALTH),
                 "late unlocks without a milestone": (unlocks(milestone="", late=True), HEALTH),
                 "empty rich city": (unlocks(), {**HEALTH, "population": 0, "cash": {"amount": 50_000_000}})}
        for case, (answer, health) in cases.items():
            with self.subTest(case=case):
                fake = self.bridge(answer, health)
                light = mt._validate_progression_mode()
                self.assertEqual(mt._assess_progression_mode(fake.snapshot()), light)
                self.assertEqual(0, fake.count("/state/mayor-snapshot"))

    def test_a_city_with_late_unlocks_and_no_milestone_is_refused(self):
        fake = self.bridge(unlocks(milestone="", late=True))
        with self.assertRaisesRegex(RuntimeError, "City mutation blocked because normal milestone progression "
                                                  "could not be verified: No current milestone is reported"):
            mt.call_tool("bulldoze_entity", {"entityType": "building", "id": 1, "checkpointName": "c"})
        self.assertEqual(0, fake.count("/commands/bulldoze"))

    def test_an_empty_city_with_a_fortune_is_refused(self):
        fake = self.bridge(health={**HEALTH, "population": 0, "cash": {"amount": 50_000_000, "periodNet": 0}})
        with self.assertRaisesRegex(RuntimeError, "An empty city has at least 500,000 in display cash"):
            mt.call_tool("zone_block", {"blockId": 3, "zone": "ResidentialLow", "checkpointName": "c"})
        self.assertEqual(0, fake.count("/commands/set-zone"))

    def test_a_normal_city_passes(self):
        self.bridge()
        mt._require_normal_progression()
        report = mt.call_tool("validate_progression_mode", {})
        self.assertTrue(report["normalProgression"])
        self.assertEqual("Milestone4", report["currentMilestone"])


class SameValuesTests(BridgeCase):
    """The loop reads the same numbers it read from the snapshot: the Bridge embeds this very
    /state/city-health answer as the snapshot's cityHealth, and /state/summary as its summary."""

    def test_cash_and_utilities_match_the_snapshot(self):
        fake = self.bridge()
        snapshot = fake.snapshot()
        self.assertEqual(float(snapshot["cityHealth"]["cash"]["amount"]), services.cash())
        self.assertEqual(float(snapshot["cityHealth"]["cash"]["amount"]) / mt.MONEY_INTERNAL_SCALE,
                         utilities.cash_display())
        self.assertEqual(snapshot["cityHealth"]["utilities"], mt.city_health()["utilities"])
        self.assertEqual(snapshot["cityHealth"]["services"], mt.city_health()["services"])
        self.assertEqual(0, fake.count("/state/mayor-snapshot"))

    def test_the_tax_reading_uses_the_same_deficit_and_demand(self):
        self.bridge()
        rates = {"Residential": 10, "Commercial": 9}
        with mock.patch.object(utilities, "trim_budgets", return_value=[]), \
                mock.patch.object(utilities, "tax_rate", side_effect=lambda service: rates[service]), \
                mock.patch.object(utilities.protocol, "run", return_value={"ok": True, "regressionOk": True}) as run:
            utilities.balance("a")
        # a deficit (-1300) while homes are wanted (70 >= TAX_DEMAND): the lower tax goes up a point
        self.assertEqual({"service": "Commercial", "rate": 10}, {k: run.call_args.args[2][k] for k in ("service", "rate")})

    def test_land_is_priced_against_the_same_cash(self):
        fake = self.bridge()
        areas = {"areaCount": 9, "maxAreaCount": 9, "areas": []}
        answer = fake.__call__
        with mock.patch.object(mt, "_send", side_effect=lambda m, p, q, b: areas if p == "/state/map-areas"
                               else answer(m, p, q, b)):
            land.buy(city_module.City(dict(IDENTITY), self.folder), "a", {"wake": 10}, (0.0, 0.0))
        self.assertEqual(0, fake.count("/state/mayor-snapshot"))


if __name__ == "__main__":
    unittest.main()

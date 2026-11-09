#!/usr/bin/env python3

import sys
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import mayor_tools


class GrowthPacingTests(unittest.TestCase):
    def test_vacant_supply_leads_high_demand(self):
        result = mayor_tools._growth_pacing(
            {"residential": 87, "commercial": 15, "workplace": 13},
            {
                "capacityByZone": [
                    {
                        "zone": "ResidentialLow",
                        "validUnsharedCells": 56,
                        "occupiedCells": 32,
                        "vacantCells": 24,
                    },
                    {
                        "zone": "Industrial",
                        "validUnsharedCells": 24,
                        "occupiedCells": 24,
                        "vacantCells": 0,
                    },
                ]
            },
        )
        residential = result["categories"]["residential"]
        # Demand 87 targets 144 vacant cells; 24 vacant leaves a five-block gap capped at 4.
        self.assertEqual("considerSmallBlocks", residential["decision"])
        self.assertEqual(4, residential["maximumAdditionalBlocksThisWake"])
        self.assertFalse(result["highDemandAloneAuthorizesExpansion"])

    def test_caps_blocks_per_wake_when_capacity_is_nearly_full(self):
        result = mayor_tools._growth_pacing(
            {"residential": 90, "commercial": 20, "workplace": 20},
            {
                "capacityByZone": [
                    {
                        "zone": "ResidentialLow",
                        "validUnsharedCells": 56,
                        "occupiedCells": 52,
                        "vacantCells": 4,
                    }
                ]
            },
        )
        residential = result["categories"]["residential"]
        self.assertEqual("considerSmallBlocks", residential["decision"])
        self.assertEqual(144, residential["targetVacantCells"])
        self.assertEqual(4, residential["maximumAdditionalBlocksThisWake"])
        self.assertFalse(result["mutationAuthorized"])

    def test_waits_when_vacant_supply_meets_the_demand_tier(self):
        result = mayor_tools._growth_pacing(
            {"residential": 60, "commercial": 0, "workplace": 0},
            {"capacityByZone": [{"zone": "ResidentialLow", "validUnsharedCells": 130,
                                 "occupiedCells": 30, "vacantCells": 100}]},
        )
        residential = result["categories"]["residential"]
        self.assertEqual("waitForAbsorption", residential["decision"])
        self.assertEqual(0, residential["maximumAdditionalBlocksThisWake"])

    def test_missing_bridge_capacity_fails_closed(self):
        result = mayor_tools._growth_pacing(
            {"residential": 100}, {"zonedCells": 20}
        )
        self.assertFalse(result["available"])
        self.assertEqual("capacityByZoneUnavailable", result["reason"])
        self.assertFalse(result["mutationAuthorized"])


if __name__ == "__main__":
    unittest.main()

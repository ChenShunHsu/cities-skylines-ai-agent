import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import mayor_tools  # noqa: E402
from mayor import traffic_remedies  # noqa: E402

HOTSPOT = {"segmentIds": [1, 2, 3], "center": {"x": 0, "z": 0}, "prefabs": {"Basic Road": 3},
           "busiestSegmentId": 2, "segmentCount": 3, "meanDensity": 99,
           "junctionTrials": [{"nodeId": 9, "kind": "removeTrafficLights"}], "upgradeCandidates": []}


def routes(by_ai, exits, pairs=(), destinations=(), origins=()):
    return {"vehiclesPlanningToUse": sum(by_ai.values()), "byVehicleAI": by_ai, "startNodeId": 10, "endNodeId": 11,
            "exitingToSegments": list(exits), "originDestinationPairs": list(pairs),
            "destinationCells": list(destinations), "originCells": list(origins)}


def kinds(diagnosis, hotspot=HOTSPOT):
    return [item["kind"] for item in traffic_remedies.remedy_kinds(diagnosis, hotspot)]


class TrafficRemedyTests(unittest.TestCase):
    def test_saturated_exit_puts_the_fix_downstream(self):
        diagnosis = traffic_remedies.diagnose(HOTSPOT, routes(
            {"CargoTruckAI": 60, "PassengerCarAI": 40},
            [{"segmentId": 7, "vehicles": 70, "density": 100, "prefab": "HighwayRamp"},
             {"segmentId": 0, "vehicles": 30}],
            destinations=[{"center": {"x": 3200, "z": -8576}}, {"center": {"x": 384, "z": 1920}}]))
        self.assertTrue(diagnosis["downstreamSaturated"])
        self.assertEqual({"x": 384, "z": 1920}, diagnosis["localAnchor"])  # not the outside connection
        self.assertEqual(["relieveDownstream", "cargoRail", "junctionControl"], kinds(diagnosis))

    def test_local_car_traffic_gets_transit_and_through_traffic_a_bypass(self):
        far_pair = {"origin": {"x": -2000, "z": 0}, "destination": {"x": 2000, "z": 0}, "vehicles": 50}
        near_pair = {"origin": {"x": 100, "z": 0}, "destination": {"x": -2000, "z": 0}, "vehicles": 50}
        diagnosis = traffic_remedies.diagnose(HOTSPOT, routes(
            {"PassengerCarAI": 80, "GarbageTruckAI": 20},
            [{"segmentId": 7, "vehicles": 40, "density": 60}], pairs=[far_pair, near_pair]))
        self.assertFalse(diagnosis["downstreamSaturated"])
        self.assertEqual(0.5, diagnosis["throughShare"])
        self.assertEqual(["transit", "bypass", "junctionControl"], kinds(diagnosis))

    def test_a_ramp_at_one_end_does_not_rule_out_local_remedies(self):
        hotspot = {**HOTSPOT, "prefabs": {"Basic Road": 8, "HighwayRamp": 1}, "junctionTrials": []}
        diagnosis = traffic_remedies.diagnose(hotspot, routes(
            {"PassengerCarAI": 90}, [{"segmentId": 7, "vehicles": 30, "density": 30}]))
        self.assertEqual(0.111, diagnosis["highwayShare"])
        self.assertEqual(["transit"], kinds(diagnosis, hotspot))

    def test_highway_hotspot_without_a_downstream_cause_needs_an_interchange(self):
        hotspot = {**HOTSPOT, "prefabs": {"HighwayRamp": 3}, "junctionTrials": []}
        diagnosis = traffic_remedies.diagnose(hotspot, routes(
            {"PassengerCarAI": 90}, [{"segmentId": 7, "vehicles": 90, "density": 30}]))
        self.assertEqual(["interchange"], kinds(diagnosis, hotspot))  # transit is for local roads


class DownstreamCandidateTests(unittest.TestCase):
    def test_far_junction_and_onward_upgradeable_road(self):
        roads = {
            7: {"id": 7, "prefab": "HighwayRamp", "startNodeId": 11, "endNodeId": 20},
            8: {"id": 8, "prefab": "Basic Road", "startNodeId": 20, "endNodeId": 21},
            9: {"id": 9, "prefab": "Basic Road", "startNodeId": 21, "endNodeId": 22},
            10: {"id": 10, "prefab": "Basic Road", "startNodeId": 22, "endNodeId": 23},
            11: {"id": 11, "prefab": "Basic Road", "startNodeId": 22, "endNodeId": 24},
        }
        junction = {"nodeId": 20, "trafficLights": True, "canToggleTrafficLights": True, "segments": []}
        with mock.patch.object(mayor_tools, "_request", return_value=junction):
            result = mayor_tools._downstream_candidates({"segmentId": 7}, {"startNodeId": 10, "endNodeId": 11}, roads)
        self.assertEqual(20, result["farNodeId"])
        self.assertEqual("removeTrafficLights", result["junctionTrial"]["kind"])
        # the road continues through node 21 and stops at junction 22
        self.assertEqual([8, 9], [item["segmentId"] for item in result["upgradeCandidates"]])


if __name__ == "__main__":
    unittest.main()

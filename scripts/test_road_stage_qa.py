"""An intermediate construction pass must never become completed outside access."""
import unittest
from unittest import mock

import mayor_tools as tools


class RoadStageTests(unittest.TestCase):
    def inspect(self, mode="two-way", terminal=20, dead_ends=True, *, safe=True,
                found=True, node=99, count=5, near_miss=False, terminal_degree=1,
                anchor_degree=2, bidirectional=True):
        segment = {"startNodeId": 10, "endNodeId": 20,
                   "start": {"x": 0, "y": 0, "z": 0}, "end": {"x": 64, "y": 0, "z": 0},
                   "flags": "Created", "problems": "", "actualLaneCount": 2, "prefabLaneCount": 2,
                   "prefabLanes": [], "isBidirectional": bidirectional, "isGroundRoad": True,
                   "terrainProfile": {
                       "source": "nativeGenerateBezierAndLiveTerrain",
                       "verificationStage": "inspection",
                       "clearanceModel": "legacyNodeInsetProxyNotCalibratedSurface",
                       "required": True, "safe": safe, "sampleCount": 9,
                       "minimumRoadAboveTerrain": 0.1,
                       "maximumRoadAboveTerrain": 0.2,
                       "maxRoadToTerrainDelta": 0.2,
                       "maxLocalTerrainGrade": 0.01,
                       "maximumRoadToTerrainDelta": 1.0,
                       "minimumRoadAboveTerrainAllowed": 0.02,
                       "maximumLocalTerrainGrade": 0.25,
                       "originalNodeTransition": False}}
        anomaly = {"type": "deadEndNearRoad" if near_miss else "deadEndRoad",
                   "nodeId": 20, "ownSegmentId": 42}
        reachability = {"safeForTwoWayFreight": False,
                        "inboundFromOutside": {"found": found, "outsideNodeId": node, "segmentCount": count},
                        "outboundToOutside": {"found": False}}
        with mock.patch.object(tools, "_request", side_effect=[segment,
                {"connectCount": anchor_degree}, {"connectCount": terminal_degree},
                {"anomalies": [anomaly]}, reachability, {"ok": True}]):
            return tools._inspect_road_result(42, dead_ends, terminal, mode)

    def test_default_still_requires_complete_two_way_access(self):
        result = self.inspect()
        self.assertFalse(result["ok"])
        self.assertFalse(result["fullOutsideAccessVerified"])

    def test_explicit_intermediate_pass_is_not_full_access(self):
        result = self.inspect("inbound")
        self.assertTrue(result["ok"])
        self.assertFalse(result["checks"]["twoWayOutsideReachable"])
        self.assertFalse(result["fullOutsideAccessVerified"])
        self.assertEqual("intermediateOutsideAccess", result["verificationScope"])

    def test_intermediate_requires_matching_direction_and_real_path(self):
        self.assertFalse(self.inspect("outbound")["ok"])
        for changes in ({"found": False}, {"node": 0}, {"count": 0}):
            with self.subTest(changes=changes):
                self.assertFalse(self.inspect("inbound", **changes)["ok"])

    def test_intermediate_keeps_terrain_anomaly_and_topology_gates(self):
        for changes in ({"safe": False}, {"near_miss": True}, {"terminal_degree": 2},
                        {"anchor_degree": 1}, {"bidirectional": False}):
            with self.subTest(changes=changes):
                self.assertFalse(self.inspect("inbound", **changes)["ok"])

    def test_intermediate_requires_explicit_matching_terminal_and_inspection(self):
        for changes in ({"terminal": 0}, {"terminal": 123}, {"dead_ends": False}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.inspect("inbound", **changes)

    def test_public_tool_passes_the_explicit_scope(self):
        with mock.patch.object(tools, "_inspect_road_result", return_value={"ok": True}) as inspect:
            tools.call_tool("inspect_road_result", {"segmentId": 42, "allowTerminalDeadEnd": True,
                            "terminalNodeId": 20, "outsideAccessRequirement": "inbound"})
        inspect.assert_called_once_with(42, True, 20, "inbound")


if __name__ == "__main__":
    unittest.main()

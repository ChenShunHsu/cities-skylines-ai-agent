#!/usr/bin/env python3

import sys
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import mayor_tools
from mayor import traffic


def road(segment_id, start, end, prefab="Basic Road"):
    return {"id": segment_id, "startNodeId": start, "endNodeId": end, "prefab": prefab}


def busy(segment_id, density=100, prefab="Basic Road", length=90.0):
    return {"id": segment_id, "density": density, "prefab": prefab, "length": length, "trafficBuffer": density * 10,
            "position": {"x": float(segment_id), "z": 0.0}}


ROADS = [
    road(1, 10, 11), road(2, 11, 12), road(3, 11, 13),  # node 11 is a three-way junction
    road(4, 12, 14), road(5, 20, 21, "Gravel Road"), road(6, 30, 31),
]


class HotspotTests(unittest.TestCase):
    def test_congested_segments_sharing_nodes_form_one_hotspot(self):
        hotspots = traffic.congestion_hotspots(
            [busy(1), busy(2, 95), busy(4), busy(5, prefab="Gravel Road"), busy(99)], ROADS)
        self.assertEqual([[1, 2, 4], [5]], [hotspot["segmentIds"] for hotspot in hotspots])
        first = hotspots[0]
        self.assertEqual([11], first["junctionNodeIds"])
        self.assertEqual(270.0, first["lengthMeters"])
        self.assertEqual([1, 2, 4], [item["segmentId"] for item in first["upgradeCandidates"]])
        self.assertEqual("Small 4 Lane Road", first["upgradeCandidates"][0]["roadPrefab"])
        self.assertEqual([], hotspots[1]["upgradeCandidates"])  # gravel has no same-width upgrade

    def test_missing_watched_segment_reads_as_free_flowing(self):
        sample = traffic.traffic_sample({"segments": [busy(1, 80)], "totalTrafficFlow": 75,
                                         "maximumTrafficFlow": 100}, [1, 2])
        self.assertEqual({"meanDensity": 40.0, "flowRatio": 0.75}, sample)

    def test_verdict_keeps_only_a_clear_win(self):
        before = {"meanDensity": 90.0, "flowRatio": 0.80}
        self.assertTrue(traffic.trial_verdict(before, {"meanDensity": 70.0, "flowRatio": 0.80}, 5, 0.01)["keep"])
        self.assertEqual("watchedDensityDidNotImprove", traffic.trial_verdict(
            before, {"meanDensity": 87.0, "flowRatio": 0.82}, 5, 0.01)["reason"])
        self.assertEqual("citywideFlowFell", traffic.trial_verdict(
            before, {"meanDensity": 40.0, "flowRatio": 0.75}, 5, 0.01)["reason"])


class AssessTrafficTests(unittest.TestCase):
    def test_hotspots_list_toggleable_junctions_and_route_evidence(self):
        junctions = {
            11: {"nodeId": 11, "canToggleTrafficLights": True, "trafficLights": True,
                 "segments": [{"segmentId": 1}, {"segmentId": 2}, {"segmentId": 3}]},
        }

        def request(method, path, query=None, body=None):
            if path == "/state/traffic":
                return {"segments": [busy(1), busy(2), busy(4)], "totalTrafficFlow": 70,
                        "maximumTrafficFlow": 100, "roadSegments": 6}
            if path == "/state/networks":
                return {"segments": ROADS}
            if path == "/state/junction":
                return junctions[query["id"]]
            raise AssertionError(path)

        with mock.patch.object(mayor_tools, "_request", side_effect=request):
            result = mayor_tools.call_tool("assess_traffic_congestion", {})
        self.assertEqual("congested", result["status"])
        self.assertEqual(0.7, result["flowRatio"])
        hotspot = result["hotspots"][0]
        self.assertEqual([{"nodeId": 11, "trafficLights": True, "congestedRoadEnds": 2,
                           "kind": "removeTrafficLights",
                           "trial": {"tool": "trial_junction_control", "nodeId": 11, "trafficLights": False}}],
                         hotspot["junctionTrials"])
        self.assertEqual({"tool": "inspect_segment_routes", "segmentId": 1}, hotspot["routeEvidence"])

    def test_unsignalled_junction_proposes_stop_signs_on_its_quiet_ends(self):
        junction = {"nodeId": 11, "canToggleTrafficLights": True, "trafficLights": False, "canUseStopSigns": True,
                    "segments": [{"segmentId": 1}, {"segmentId": 2}, {"segmentId": 3}]}
        candidate = mayor_tools._junction_trial_candidate(junction, {1, 2})
        self.assertEqual(("stopSignsOnQuietEnds", [3]), (candidate["kind"], candidate["trial"]["stopSignSegmentIds"]))
        # one queued end only: nothing to give priority to, so the last resort is lights
        candidate = mayor_tools._junction_trial_candidate(junction, {1})
        self.assertEqual("addTrafficLights", candidate["kind"])

    def test_small_hotspots_and_free_flow_read_clear(self):
        def request(method, path, query=None, body=None):
            if path == "/state/traffic":
                return {"segments": [busy(6)], "totalTrafficFlow": 95, "maximumTrafficFlow": 100}
            return {"segments": ROADS}

        with mock.patch.object(mayor_tools, "_request", side_effect=request):
            result = mayor_tools.call_tool("assess_traffic_congestion", {})
        self.assertEqual(("clear", []), (result["status"], result["hotspots"]))


class TrialJunctionTests(unittest.TestCase):
    def run_trial(self, after_density, paused=True, lights=True, change=None):
        state = {"lights": lights, "phase": "before", "stops": set()}
        calls = []

        def request(method, path, query=None, body=None):
            calls.append((path, body))
            if path == "/state/junction":
                return {"canToggleTrafficLights": True, "canUseStopSigns": not state["lights"],
                        "trafficLights": state["lights"],
                        "segments": [{"segmentId": item, "stopSign": item in state["stops"]} for item in (1, 2, 3)]}
            if path == "/state/summary":
                return {"simulation": {"paused": paused}}
            if path == "/commands/set-junction-control":
                if body["control"] == "trafficLights":
                    state["lights"] = body["enabled"]
                elif body["enabled"]:
                    state["stops"].add(body["segmentId"])
                else:
                    state["stops"].discard(body["segmentId"])
                state["phase"] = "after"
                return {"ok": True}
            if path == "/state/traffic":
                density = 90 if state["phase"] == "before" else after_density
                return {"segments": [busy(1, density), busy(2, density)], "totalTrafficFlow": 80,
                        "maximumTrafficFlow": 100}
            if path == "/commands/set-simulation-speed":
                return {"ok": True}
            raise AssertionError(path)

        with mock.patch.object(mayor_tools, "_request", side_effect=request), \
                mock.patch.object(mayor_tools, "_require_normal_progression"), \
                mock.patch.object(mayor_tools, "_require_checkpoint", side_effect=lambda args: args), \
                mock.patch.object(mayor_tools.time, "sleep"):
            result = mayor_tools.call_tool("trial_junction_control", {
                "checkpointName": "c", "nodeId": 11, "samples": 2, **(change or {"trafficLights": not lights})})
        return result, calls

    def test_improvement_is_kept(self):
        result, calls = self.run_trial(after_density=60)
        self.assertTrue(result["ok"])
        self.assertFalse(result["reverted"])
        self.assertFalse(result["controlsAfter"]["trafficLights"])
        changes = [body["enabled"] for path, body in calls if path == "/commands/set-junction-control"]
        self.assertEqual([False], changes)

    def test_no_improvement_switches_back_and_restores_the_pause(self):
        result, calls = self.run_trial(after_density=88)
        self.assertTrue(result["ok"])
        self.assertTrue(result["reverted"])
        self.assertTrue(result["controlsAfter"]["trafficLights"])
        changes = [body["enabled"] for path, body in calls if path == "/commands/set-junction-control"]
        self.assertEqual([False, True], changes)
        speeds = [body for path, body in calls if path == "/commands/set-simulation-speed"]
        self.assertEqual([{"paused": False, "speed": 3}, {"paused": True}], speeds)

    def test_running_simulation_is_left_running(self):
        _, calls = self.run_trial(after_density=60, paused=False)
        speeds = [body for path, body in calls if path == "/commands/set-simulation-speed"]
        self.assertEqual([{"paused": False, "speed": 3}], speeds)

    def test_stop_signs_without_improvement_are_removed_again(self):
        result, calls = self.run_trial(after_density=90, lights=False, change={"stopSignSegmentIds": [3]})
        self.assertTrue(result["ok"])
        self.assertTrue(result["reverted"])
        self.assertEqual([3], result["controlsTried"]["stopSignSegmentIds"])
        self.assertEqual([], result["controlsAfter"]["stopSignSegmentIds"])
        changes = [(body["segmentId"], body["enabled"]) for path, body in calls
                   if path == "/commands/set-junction-control"]
        self.assertEqual([(3, True), (3, False)], changes)

    def test_refuses_a_change_that_is_already_in_place(self):
        with mock.patch.object(mayor_tools, "_request", return_value={
                "canToggleTrafficLights": True, "trafficLights": False, "segments": []}), \
                mock.patch.object(mayor_tools, "_require_normal_progression"), \
                mock.patch.object(mayor_tools, "_require_checkpoint", side_effect=lambda args: args):
            with self.assertRaises(ValueError):
                mayor_tools.call_tool("trial_junction_control",
                                      {"checkpointName": "c", "nodeId": 11, "trafficLights": False})
            with self.assertRaises(ValueError):
                mayor_tools.call_tool("trial_junction_control", {"checkpointName": "c", "nodeId": 11})


class WidenRoadChainTests(unittest.TestCase):
    def run_widen(self, failing, bulldoze_safe=True, invalid=()):
        calls = []

        def request(method, path, query=None, body=None):
            calls.append((path, body))
            if path == "/state/network-segment-details":
                segment_id = query["id"]
                prefab = "Basic Road" if segment_id < 100 else "Small 4 Lane Road"
                return {"prefab": prefab, "start": {"x": 0.0, "z": float(segment_id)},
                        "end": {"x": 90.0, "z": float(segment_id)}}
            if path == "/commands/upgrade-road":
                if body.get("dryRun"):
                    return {"ok": True, "valid": body["segmentId"] not in invalid}
                return {"ok": True, "newSegmentId": body["segmentId"] + 100}
            if path == "/state/zoning-support":
                return {"safe": not failing, "checkedGrowables": 9,
                        "failing": [{"id": item, "repairableCells": 0} for item in failing]}
            if path == "/commands/preview-bulldoze-impact":
                return {"safe": bulldoze_safe, "risks": [] if bulldoze_safe else ["lastActiveFacilityOfService"]}
            if path == "/commands/bulldoze":
                return {"ok": True}
            raise AssertionError(path)

        with mock.patch.object(mayor_tools, "_request", side_effect=request),                 mock.patch.object(mayor_tools, "_require_normal_progression"),                 mock.patch.object(mayor_tools, "_require_checkpoint", side_effect=lambda args: args):
            result = mayor_tools.call_tool("widen_road_chain", {"checkpointName": "c", "segmentIds": [1, 2, 3]})
        return result, calls

    def test_whole_chain_is_upgraded_before_zoning_is_judged(self):
        result, calls = self.run_widen(failing=[], invalid=(2,))
        self.assertTrue(result["ok"])
        self.assertEqual([101, 103], result["upgradedSegmentIds"])
        self.assertEqual([2], [item["segmentId"] for item in result["skipped"]])
        upgrades = [index for index, (path, body) in enumerate(calls)
                    if path == "/commands/upgrade-road" and not body.get("dryRun")]
        first_zoning = next(index for index, (path, _) in enumerate(calls) if path == "/state/zoning-support")
        self.assertLess(max(upgrades), first_zoning)
        self.assertEqual("Small 4 Lane Road", next(body for path, body in calls
                                                   if path == "/commands/upgrade-road")["roadPrefab"])

    def test_unsupported_building_is_cleared_when_safe(self):
        result, calls = self.run_widen(failing=[55])
        self.assertTrue(result["ok"])
        self.assertEqual([{"id": 55, "ok": True}], result["cleared"])
        self.assertIn(("/commands/bulldoze", {"entityType": "building", "id": 55}), calls)

    def test_a_game_exception_after_the_upgrade_is_judged_again_from_the_zoning_that_stands(self):
        # the first zoning read throws (as the game did once a chain stood widened); read again it is
        # safe, or it still finds a building the game would demolish
        for second_safe, expected in ((True, True), (False, False)):
            reads = []

            def request(method, path, query=None, body=None):
                if path == "/state/network-segment-details":
                    return {"prefab": "Basic Road", "start": {"x": 0.0, "z": 0.0}, "end": {"x": 90.0, "z": 0.0}}
                if path == "/commands/upgrade-road":
                    return {"ok": True, "valid": True} if body.get("dryRun") else {"ok": True, "newSegmentId": 101}
                if path == "/state/zoning-support":
                    reads.append(1)
                    if len(reads) == 1:
                        raise mayor_tools.BridgeRequestError(
                            {"ok": False, "error": "IndexOutOfRangeException: Array index is out of range. "
                                                   "(in TerrainModify.ApplyQuad)"})
                    return {"safe": second_safe, "checkedGrowables": 9,
                            "failing": [] if second_safe else [{"id": 55, "repairableCells": 0}]}
                raise AssertionError(path)
            with mock.patch.object(mayor_tools, "_request", side_effect=request), \
                    mock.patch.object(mayor_tools, "_require_normal_progression"), \
                    mock.patch.object(mayor_tools, "_require_checkpoint", side_effect=lambda args: args):
                result = mayor_tools.call_tool("widen_road_chain", {"checkpointName": "c", "segmentIds": [1],
                                                                    "clearUnsupported": False})
            self.assertEqual(expected, result["ok"])
            self.assertIn("ApplyQuad", result["postError"])
            self.assertEqual([101], result["upgradedSegmentIds"])

    def test_unsafe_clearance_fails_for_a_checkpoint_restore(self):
        result, calls = self.run_widen(failing=[55], bulldoze_safe=False)
        self.assertFalse(result["ok"])
        self.assertNotIn("/commands/bulldoze", [path for path, _ in calls])


if __name__ == "__main__":
    unittest.main()

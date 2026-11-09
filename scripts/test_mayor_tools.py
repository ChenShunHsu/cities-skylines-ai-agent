#!/usr/bin/env python3

import copy
import io
import json
import math
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).with_name("mayor_tools.py")
SPEC = importlib.util.spec_from_file_location("mayor_tools_under_test", SCRIPT)
MAYOR_TOOLS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MAYOR_TOOLS)


class MayorToolsTests(unittest.TestCase):
    def test_noise_names_the_facility_whose_radius_reaches_the_home(self):
        problems = {"problems": [{"id": 5, "problemNames": ["Noise"], "position": {"x": 0, "z": 0}},
                                 {"id": 6, "problemNames": ["Water"], "position": {"x": 9, "z": 9}}]}
        influence = {"facilities": [
            {"buildingId": 1, "prefab": "Wind Turbine", "position": {"x": 100, "z": 0},
             "noiseInfluence": {"radius": 150}},
            {"buildingId": 2, "prefab": "Wind Turbine", "position": {"x": 400, "z": 0},
             "noiseInfluence": {"radius": 150}}]}
        roads = {"segments": [{"prefab": "Basic Road", "start": {"x": -50, "z": 10}, "end": {"x": 50, "z": 10}}]}

        def request(method, path, query=None, body=None):
            return {"/state/problems": problems, "/state/facility-influence": influence,
                    "/state/growables": {"growables": []}, "/state/networks": roads,
                    "/state/service-coverage": {"summary": {"average": 44}}}[path]

        with mock.patch.object(MAYOR_TOOLS, "_request", side_effect=request):
            result = MAYOR_TOOLS.call_tool("diagnose_noise", {})
        self.assertEqual(1, result["noisy"])
        self.assertEqual({"facility": 1}, result["bySource"])  # a plain street is not named
        self.assertEqual([1], [s["buildingId"] for s in result["homes"][0]["sources"]])
        self.assertEqual(44, result["homes"][0]["noise"])

    def test_a_city_mostly_dark_is_short_of_power_not_cut_off(self):
        coverage = {"summary": {"requiringElectricity": 10, "missingElectricity": 8},
                    "buildings": [{"id": index, "position": {"x": 0, "z": 0}} for index in range(8)]}

        def request(method, path, query=None, body=None):
            if path == "/state/electricity-service-coverage":
                return coverage
            if path == "/state/city-health":
                return {"utilities": {"electricity": {"used": 7200, "capacity": 8000, "utilization": 0.9}}}
            raise AssertionError(path)  # no neighbourhood is sampled: none would show powered ground

        with mock.patch.object(MAYOR_TOOLS, "_request", side_effect=request):
            result = MAYOR_TOOLS.call_tool("diagnose_utility_shortfall", {"service": "electricity", "limit": 4})
        self.assertEqual(["supplyShort"], result["verdict"])
        self.assertEqual(4, len(result["coveredButShort"]))

    def test_a_dark_town_its_grid_barely_feeds_is_unreached_not_short(self):
        # live: turbines 200-280m from every home at 8% use; a line was needed, not a plant
        coverage = {"summary": {"requiringElectricity": 10, "missingElectricity": 8},
                    "buildings": [{"id": index, "position": {"x": 0, "z": 0}} for index in range(8)]}

        def request(method, path, query=None, body=None):
            if path == "/state/electricity-service-coverage":
                return coverage
            if path == "/state/city-health":
                return {"utilities": {"electricity": {"used": 620, "capacity": 8000, "utilization": 0.077}}}
            raise AssertionError(path)

        with mock.patch.object(MAYOR_TOOLS, "_request", side_effect=request), \
                mock.patch.object(MAYOR_TOOLS, "_inspect_electricity_connection_area",
                                  return_value={"summary": {"poweredSamples": 0}}):
            result = MAYOR_TOOLS.call_tool("diagnose_utility_shortfall", {"service": "electricity", "limit": 4})
        self.assertEqual(["notCovered"], result["verdict"])

    def test_water_shortfall_tells_a_dry_pipe_from_a_missing_one(self):
        coverage = {"summary": {"missingWater": 2}, "buildings": [
            {"id": 1, "hasWater": False, "hasSewage": True, "position": {"x": 0, "z": 40}},
            {"id": 2, "hasWater": False, "hasSewage": False, "position": {"x": 0, "z": 400}}]}
        pipes = {"segments": [{"start": {"x": -100, "z": 0}, "end": {"x": 100, "z": 0},
                               "startNodeFlags": "Created, Sewage", "endNodeFlags": "Created, Sewage"}]}

        def request(method, path, query=None, body=None):
            if path == "/state/water-service-coverage":
                return coverage
            if path == "/state/networks":
                return pipes
            return {"cityHealth": {"utilities": {"water": {"used": 10, "capacity": 100}}}}

        with mock.patch.object(MAYOR_TOOLS, "_request", side_effect=request):
            result = MAYOR_TOOLS.call_tool("diagnose_utility_shortfall", {"service": "water"})
        self.assertEqual(["supplyShort", "notCovered"], result["verdict"])
        self.assertEqual([1], [item["id"] for item in result["coveredButShort"]])
        self.assertEqual({"water": False, "sewage": True}, result["coveredButShort"][0]["pipeCarries"])
        self.assertEqual([2], [item["id"] for item in result["notCovered"]])

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patcher = mock.patch.object(MAYOR_TOOLS, "ROAD_FAILURE_PATH", Path(directory.name) / "road-failures.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        regression_patcher = mock.patch.object(
            MAYOR_TOOLS,
            "REGRESSION_BASELINES_PATH",
            Path(directory.name) / "regression-baselines.json",
        )
        regression_patcher.start()
        self.addCleanup(regression_patcher.stop)

    @staticmethod
    def regression_snapshot(**overrides):
        value = {
            "cash": 100000,
            "periodNet": -100,
            "population": 20,
            "problemEntities": 1,
            "roadSegments": 10,
            "trafficDensity": 5.5,
            "progression": {
                "currentMilestone": "Milestone1",
                "capabilities": {"water": True},
            },
            "electricity": {"used": 10, "capacity": 100},
            "water": {"used": 20, "capacity": 100},
            "sewage": {"used": 20, "capacity": 100},
        }
        value.update(overrides)
        return value

    def inspection_terrain_profile(self, **overrides):
        profile = {
            "source": "nativeGenerateBezierAndLiveTerrain",
            "verificationStage": "inspection",
            "clearanceModel": "legacyNodeInsetProxyNotCalibratedSurface",
            "required": True,
            "sampleCount": 9,
            "maxRoadToTerrainDelta": 0.7,
            "minimumRoadAboveTerrain": 0.02,
            "maximumRoadAboveTerrain": 0.2,
            "maxLocalTerrainGrade": 0.01,
            "maximumRoadToTerrainDelta": 1.0,
            "minimumRoadAboveTerrainAllowed": 0.02,
            "maximumLocalTerrainGrade": 0.25,
            "originalNodeTransition": False,
            "safe": True,
        }
        profile.update(overrides)
        return profile

    def test_invalid_arguments_are_rejected_before_bridge_access(self):
        cases = [
            ("observe_city", {"detailLimit": -1}),
            ("observe_city", {"detailLimit": 100000}),
            ("create_checkpoint", {"name": ""}),
            ("propose_power_plant_candidates", {"buildingPrefab": "Coal Power Plant", "serviceAreaAnchor": {"x": 1}}),
            ("propose_power_plant_candidates", {"buildingPrefab": "Coal Power Plant", "serviceAreaAnchor": {"x": True, "z": 1}}),
            ("propose_power_plant_candidates", {"buildingPrefab": "Coal Power Plant", "serviceAreaAnchor": {"x": float("nan"), "z": 1}}),
            ("propose_power_plant_candidates", {"buildingPrefab": "Coal Power Plant", "serviceAreaAnchor": {"x": 1, "z": 1, "typo": 0}}),
            ("propose_power_plant_candidates", {"buildingPrefab": "Coal Power Plant", "serviceAreaAnchor": {"x": 1, "z": 1}, "avoidAreas": [{}] * 65}),
        ]
        for name, arguments in cases:
            with self.subTest(name=name, arguments=arguments), mock.patch.object(MAYOR_TOOLS, "_request") as request:
                with self.assertRaises(ValueError):
                    MAYOR_TOOLS.call_tool(name, arguments)
                request.assert_not_called()

    def test_segment_routes_maps_segment_id_and_rejects_missing_id(self):
        with mock.patch.object(MAYOR_TOOLS, "_request", return_value={"ok": True}) as request:
            MAYOR_TOOLS.call_tool("inspect_segment_routes", {"segmentId": 4137, "limit": 5})
        request.assert_called_once_with("GET", "/state/segment-routes", query={"id": 4137, "limit": 5})
        with mock.patch.object(MAYOR_TOOLS, "_request") as request, self.assertRaises(ValueError):
            MAYOR_TOOLS.call_tool("inspect_segment_routes", {})
        request.assert_not_called()

    def test_junction_control_requires_checkpoint_and_matching_segment(self):
        with mock.patch.object(MAYOR_TOOLS, "_request", return_value={"ok": True}) as request:
            MAYOR_TOOLS.call_tool("inspect_junction", {"nodeId": 21763})
        request.assert_called_once_with("GET", "/state/junction", query={"id": 21763})

        cases = [
            {"control": "stopSign", "enabled": True},
            {"control": "trafficLights", "segmentId": 4137, "enabled": False},
        ]
        for case in cases:
            with self.subTest(case=case), mock.patch.object(MAYOR_TOOLS, "_require_normal_progression"),                     mock.patch.object(MAYOR_TOOLS, "_checkpoint_exists", return_value=True),                     mock.patch.object(MAYOR_TOOLS, "_request") as request, self.assertRaises(ValueError):
                MAYOR_TOOLS.call_tool("set_junction_control", {"checkpointName": "pre", "nodeId": 21763, **case})
            request.assert_not_called()

        with mock.patch.object(MAYOR_TOOLS, "_require_normal_progression"),                 mock.patch.object(MAYOR_TOOLS, "_checkpoint_exists", return_value=True),                 mock.patch.object(MAYOR_TOOLS, "_request", return_value={"ok": True}) as request:
            MAYOR_TOOLS.call_tool("set_junction_control", {"checkpointName": "pre", "nodeId": 21763,
                                                           "control": "stopSign", "segmentId": 4137, "enabled": True})
        request.assert_called_once_with("POST", "/commands/set-junction-control", body={
            "nodeId": 21763, "control": "stopSign", "segmentId": 4137, "enabled": True})
        with mock.patch.object(MAYOR_TOOLS, "_request") as request, self.assertRaises(ValueError):
            MAYOR_TOOLS.call_tool("set_junction_control", {"nodeId": 21763, "control": "trafficLights", "enabled": False})
        request.assert_not_called()

    def test_invalid_city_identity_does_not_overwrite_memory(self):
        cases = [{"city": {"instanceId": value}} for value in (None, True, 123, {}, " ")]
        cases.append({"ok": False, "city": {"instanceId": "stale-city"}})
        for snapshot in cases:
            with self.subTest(snapshot=snapshot), mock.patch.object(MAYOR_TOOLS, "_request", return_value=snapshot), mock.patch.object(MAYOR_TOOLS, "_save_memory") as save:
                with self.assertRaises(RuntimeError):
                    MAYOR_TOOLS.call_tool("load_mayor_memory", {})
                save.assert_not_called()

    def test_cli_accepts_windows_bom_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            arguments = Path(directory) / "arguments.json"
            arguments.write_text('{"candidates": []}', encoding="utf-8-sig")
            code, payload = self.run_tool("call", "score_candidates", "--arguments-file", str(arguments))
            self.assertEqual(0, code)
            self.assertEqual([], payload["ranked"])

    def test_cli_missing_arguments_file_returns_json_error(self):
        code, payload = self.run_tool("call", "observe_city", "--arguments-file", "missing-arguments.json")
        self.assertEqual(1, code)
        self.assertFalse(payload["ok"])

    def test_cli_reports_failed_tool_and_timeout_with_nonzero_status(self):
        for outcome in ({"ok": False, "error": "Rejected by Bridge"}, TimeoutError("Bridge timed out")):
            with self.subTest(outcome=outcome), mock.patch.object(sys, "argv", [str(SCRIPT), "call", "observe_city"]), mock.patch("sys.stdout", new_callable=io.StringIO) as output:
                options = {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome}
                with mock.patch.object(MAYOR_TOOLS, "call_tool", **options):
                    self.assertEqual(1, MAYOR_TOOLS.main())
                self.assertFalse(json.loads(output.getvalue())["ok"])

    def test_nonfinite_memory_write_preserves_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mayor-state.json"
            original = '{"version":1}'
            path.write_text(original, encoding="utf-8-sig")
            with mock.patch.object(MAYOR_TOOLS, "MEMORY_PATH", path):
                self.assertEqual(1, MAYOR_TOOLS._load_memory()["version"])
                with self.assertRaises(ValueError):
                    MAYOR_TOOLS._save_memory({"lastSnapshot": {"cash": float("nan")}})
            self.assertEqual(original, path.read_text(encoding="utf-8-sig"))
            self.assertEqual([path], list(Path(directory).iterdir()))

    def test_active_surfaces_do_not_embed_known_qa_city_identity(self):
        forbidden = (
            "Black Woods",
            "Grand River",
            "Green Plains",
            "Agent QA",
            "第一次玩",
        )
        roots = (
            MAYOR_TOOLS.ROOT / "src",
            MAYOR_TOOLS.ROOT / "scripts",
            MAYOR_TOOLS.ROOT / "references",
        )
        files = [MAYOR_TOOLS.ROOT / "SKILL.md"]
        for root in roots:
            files.extend(path for path in root.rglob("*") if path.is_file())
        matches = []
        for path in files:
            if path == Path(__file__) or any(
                part in {"bin", "obj", ".runtime", "__pycache__"} for part in path.parts
            ):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for value in forbidden:
                if value.casefold() in text.casefold():
                    matches.append(f"{path.relative_to(MAYOR_TOOLS.ROOT)}: {value}")
        self.assertEqual([], matches)

    def test_fixed_layout_legacy_mutation_scripts_are_not_shipped(self):
        retired = (
            "develop-starter-city.ps1",
            "develop-city-with-infrastructure.ps1",
            "repair-service-overlap.ps1",
        )
        self.assertEqual(
            [],
            [name for name in retired if (MAYOR_TOOLS.ROOT / "scripts" / name).exists()],
        )

    def test_network_telemetry_reports_original_node_transition_threshold(self):
        game_state = (MAYOR_TOOLS.ROOT / "src" / "GameState.cs").read_text(encoding="utf-8")
        road_commands = (MAYOR_TOOLS.ROOT / "src" / "RoadCommands.cs").read_text(encoding="utf-8")
        self.assertIn("IsOriginalNodeTransition", game_state)
        self.assertIn('\\\"originalNodeTransition\\\"', game_state)
        self.assertIn("ExistingNodeTransitionMaximumDeviation", game_state)
        self.assertIn("NetNode.Flags.Original", road_commands)

    def test_original_node_transition_clearance_follows_the_original_node(self):
        # Maps place some original nodes (highways) flush with the ground, so a short
        # transition leaving one cannot reach the generic 0.02m clearance.
        game_state = (MAYOR_TOOLS.ROOT / "src" / "GameState.cs").read_text(encoding="utf-8")
        road_commands = (MAYOR_TOOLS.ROOT / "src" / "RoadCommands.cs").read_text(encoding="utf-8")
        fitting = (MAYOR_TOOLS.ROOT / "src" / "RoadTerrainFitting.cs").read_text(encoding="utf-8")
        self.assertIn("originalNodeClearance - OriginalNodeClearanceTolerance", road_commands)
        self.assertIn("Mathf.Max(ExistingNodeTransitionMinimumClearance, allowed)", road_commands)
        self.assertIn("minimumClearance >= transitionMinimumClearance", road_commands)
        # Definition plus the preview, immediate, and settled gates.
        self.assertEqual(4, road_commands.count(" TransitionMinimumClearance("))
        self.assertIn("RoadCommands.TransitionMinimumClearance(", game_state)
        self.assertIn("TransitionMinimumClearance(manager, startNode, endNode)", fitting)

    def test_overlong_road_segments_are_refused_reported_and_splittable(self):
        # PathManager.FindPathPosition only scans segments with an end node near the queried
        # building, so the middle of a long segment is unreachable for service vehicles.
        road_commands = (MAYOR_TOOLS.ROOT / "src" / "RoadCommands.cs").read_text(encoding="utf-8")
        split = (MAYOR_TOOLS.ROOT / "src" / "RoadSplitCommands.cs").read_text(encoding="utf-8")
        game_state = (MAYOR_TOOLS.ROOT / "src" / "GameState.cs").read_text(encoding="utf-8")
        api = (MAYOR_TOOLS.ROOT / "src" / "ApiServer.cs").read_text(encoding="utf-8")
        self.assertIn("internal const float MaximumRoadSegmentLength = 160f;", split)
        self.assertIn("if (IsOverlongRoadSegment(info, flatLength))", road_commands)
        self.assertIn("RoadCommands.IsOverlongRoadSegment(segment.Info, length)", game_state)
        self.assertIn('Begin("overlongRoadSegment")', game_state)
        self.assertIn('"SplitSegment", BindingFlags.Static | BindingFlags.NonPublic', split)
        # Fewest cuts, each on the 8m zone-cell grid, so buildings beside a cut keep their cells.
        self.assertIn("Mathf.CeilToInt(flatLength / MaximumRoadSegmentLength)", split)
        self.assertIn("Mathf.RoundToInt((float)cells * piece / pieces) * ZoneCellSize", split)
        self.assertIn("RoadCommands.SplitRoadSegment(body)", api)

    def test_split_road_segment_checks_zoning_across_every_new_piece(self):
        responses = {
            "/commands/split-road": [{"ok": True, "newSegmentIds": [8, 9, 10]}],
            "/state/network-segment-details": [
                {"start": {"x": 0, "z": 0}, "end": {"x": 100, "z": 0}},
                {"start": {"x": 100, "z": 0}, "end": {"x": 200, "z": 0}},
                {"start": {"x": 200, "z": 0}, "end": {"x": 300, "z": 0}},
            ],
            "/state/zoning-support": [{"safe": True, "checkedGrowables": 12, "failing": []}],
        }
        calls = []

        def request(method, path, **kwargs):
            calls.append((path, kwargs))
            return responses[path].pop(0)

        with mock.patch.object(MAYOR_TOOLS, "_require_normal_progression"), mock.patch.object(
            MAYOR_TOOLS, "_require_checkpoint", side_effect=lambda args: args
        ), mock.patch.object(MAYOR_TOOLS, "_request", side_effect=request):
            result = MAYOR_TOOLS.call_tool("split_road_segment", {"checkpointName": "c", "segmentId": 3})
        self.assertTrue(result["ok"])
        self.assertEqual(("/commands/split-road", 3), (calls[0][0], calls[0][1]["body"]["segmentId"]))
        support_query = next(kwargs["query"] for path, kwargs in calls if path == "/state/zoning-support")
        self.assertEqual({"x": 150.0, "z": 0.0, "radius": 246.0, "limit": 64}, support_query)
        with self.assertRaises(ValueError):
            MAYOR_TOOLS.call_tool("split_road_segment", {"segmentId": 3})

    def _connect_water_facility(self, facility_problems="WaterNotConnected", pair_safe=True, after_problems="",
                                arguments=None):
        facilities = [
            {"id": 11, "prefab": "Water Intake", "position": {"x": -500, "z": 100}, "angleDegrees": 270,
             "problems": facility_problems},
            {"id": 12, "prefab": "Water Outlet", "position": {"x": 300, "z": -600}, "angleDegrees": 315,
             "problems": ""},
        ]
        after = [dict(facilities[0], problems=after_problems), facilities[1]]
        responses = {
            "/state/facilities": [{"facilities": facilities}, {"facilities": after}],
            "/state/networks": [{"segments": [
                {"service": "Water", "start": {"x": -480.0, "z": 170.0}, "end": {"x": -450.0, "z": 220.0}},
                {"service": "Water", "start": {"x": -300, "z": 300}, "end": {"x": -250, "z": 512}},
            ]}],
            "/commands/preview-network": [{"ok": True, "valid": True, "constructionCost": 1200, "length": 70.2}],
            "/commands/build-network": [{"ok": True, "segmentId": 77}],
        }
        calls = []

        def request(method, path, **kwargs):
            calls.append((path, kwargs))
            return responses[path].pop(0)

        pair = {"safe": pair_safe, "risks": [] if pair_safe else ["outletNotDownstream"], "previews": {
            "intake": {"networkPaths": [{"service": "Water", "nodes": [{"x": -485.0, "z": 100.0}]}]}}}
        with mock.patch.object(MAYOR_TOOLS, "_require_normal_progression"), mock.patch.object(
            MAYOR_TOOLS, "_require_checkpoint", side_effect=lambda args: args
        ), mock.patch.object(MAYOR_TOOLS, "_inspect_water_pair", return_value=pair) as inspect_pair, \
                mock.patch.object(MAYOR_TOOLS, "_request", side_effect=request):
            result = MAYOR_TOOLS.call_tool("connect_water_facility", arguments or {
                "checkpointName": "c", "buildingId": 11, "pairBuildingId": 12})
        return result, calls, inspect_pair

    def test_connect_water_facility_branches_from_the_native_connection_to_the_nearest_pipe(self):
        result, calls, inspect_pair = self._connect_water_facility()
        self.assertTrue(result["ok"])
        self.assertTrue(result["connected"])
        self.assertEqual("intake", result["role"])
        pair_arguments = inspect_pair.call_args.args[0]
        self.assertEqual((11, 12), (pair_arguments["intake"]["buildingId"], pair_arguments["outlet"]["buildingId"]))
        build = next(kwargs["body"] for path, kwargs in calls if path == "/commands/build-network")
        self.assertEqual(({"x": -485.0, "z": 100.0}, {"x": -480.0, "z": 170.0}), (build["start"], build["end"]))
        self.assertEqual("Water Pipe", build["roadPrefab"])

    def test_a_facility_far_from_the_pipes_is_joined_in_pieces_and_a_broken_branch_comes_out(self):
        start, end = {"x": 0.0, "z": 0.0}, {"x": 330.0, "z": 0.0}
        pieces = MAYOR_TOOLS._branch_pieces(start, end, 330.0)
        self.assertEqual(3, len(pieces))
        self.assertEqual((start, end), (pieces[0][0], pieces[-1][1]))
        self.assertEqual([10.0, -10.0], [a["z"] for a, _ in pieces[1:]])  # joints shifted either side
        for a, b in pieces:
            self.assertLessEqual(math.dist((a["x"], a["z"]), (b["x"], b["z"])),
                                 MAYOR_TOOLS.WATER_FACILITY_BRANCH_MAX_LENGTH)
        near = {"x": 120.0, "z": 0.0}
        self.assertEqual([(start, near)], MAYOR_TOOLS._branch_pieces(start, near, 120.0))

        facilities = [{"id": 11, "prefab": "Water Tower", "position": {"x": 0, "z": 0}, "problems": "WaterNotConnected"},
                      {"id": 12, "prefab": "Water Outlet", "position": {"x": 900, "z": 900}, "problems": ""}]
        pair = {"safe": True, "previews": {"intake": {"networkPaths": [
            {"service": "Water", "nodes": [{"x": 0.0, "z": 0.0}]}]}}}
        for builds, removed in (([{"ok": True, "segmentId": 1}, {"ok": True, "segmentId": 2},
                                  {"ok": True, "segmentId": 3}], []),
                                ([{"ok": True, "segmentId": 1}, {"ok": False, "error": "x"}], [1])):
            responses = {"/state/facilities": [{"facilities": facilities},
                                               {"facilities": [dict(facilities[0], problems=""), facilities[1]]}],
                         "/state/networks": [{"segments": [{"service": "Water", "start": {"x": 330.0, "z": 0.0},
                                                            "end": {"x": 500.0, "z": 0.0}}]}],
                         "/commands/preview-network": [{"ok": True, "valid": True, "constructionCost": 100}] * 3,
                         "/commands/build-network": list(builds), "/commands/bulldoze": [{"ok": True}] * 3}
            calls = []

            def request(method, path, **kwargs):
                calls.append((path, kwargs.get("body")))
                return responses[path].pop(0)
            with mock.patch.object(MAYOR_TOOLS, "_require_normal_progression"), mock.patch.object(
                MAYOR_TOOLS, "_require_checkpoint", side_effect=lambda args: args
            ), mock.patch.object(MAYOR_TOOLS, "_inspect_water_pair", return_value=pair), \
                    mock.patch.object(MAYOR_TOOLS, "_request", side_effect=request):
                result = MAYOR_TOOLS.call_tool("connect_water_facility", {
                    "checkpointName": "c", "buildingId": 11, "pairBuildingId": 12})
            self.assertEqual(3, sum(1 for path, _ in calls if path == "/commands/preview-network"))
            self.assertEqual(not removed, result["ok"])
            self.assertEqual(removed, [body["id"] for path, body in calls if path == "/commands/bulldoze"])

    def test_connect_water_facility_dry_run_and_refusals_do_not_build(self):
        result, calls, _ = self._connect_water_facility(arguments={
            "buildingId": 11, "pairBuildingId": 12, "dryRun": True})
        self.assertTrue(result["ok"])
        self.assertNotIn("/commands/build-network", [path for path, _ in calls])
        result, calls, _ = self._connect_water_facility(pair_safe=False)
        self.assertFalse(result["ok"])
        self.assertNotIn("/commands/build-network", [path for path, _ in calls])
        with self.assertRaises(RuntimeError):
            self._connect_water_facility(facility_problems="")
        result, _, _ = self._connect_water_facility(after_problems="WaterNotConnected")
        self.assertFalse(result["ok"])
        self.assertIn("restore the checkpoint", result["error"])

    def test_zone_block_listing_covers_a_grown_city(self):
        # A grown city passed 1,000 zone blocks and the old cap silently hid the rest.
        game_state = (MAYOR_TOOLS.ROOT / "src" / "GameState.cs").read_text(encoding="utf-8")
        self.assertIn("private const int MaximumZoneBlockListing = 5000;", game_state)
        self.assertIn("if (limit > MaximumZoneBlockListing)", game_state)

    def test_progression_reports_what_the_next_milestone_needs(self):
        unlocks = (MAYOR_TOOLS.ROOT / "src" / "UnlockStateCommands.cs").read_text(encoding="utf-8")
        snapshot = (MAYOR_TOOLS.ROOT / "src" / "MayorSnapshotCommands.cs").read_text(encoding="utf-8")
        self.assertIn("next.GetLocalizedProgress(out subProgress)", unlocks)
        self.assertIn("UnlockStateCommands.NextMilestoneJson(unlocks)", snapshot)
        need = {"name": "Milestone8", "requirements": [{"current": 16000, "target": 18000, "passed": False}]}
        result = MAYOR_TOOLS._assess_progression_mode({"progression": {"currentMilestone": "Milestone8", "nextMilestone": need},
                                                       "cityHealth": {"population": 16000}})
        self.assertEqual(need, result["nextMilestone"])

    def test_road_rollback_refunds_authoritative_charged_cost(self):
        road_commands = (MAYOR_TOOLS.ROOT / "src" / "RoadCommands.cs").read_text(encoding="utf-8")
        self.assertIn("public int ChargedCost;", road_commands)
        self.assertIn("ChargedCost = chargedCost", road_commands)
        self.assertIn("long refund = chargedCost > 0 ? chargedCost : missing;", road_commands)
        self.assertNotIn("if (missing <= 0)", road_commands)

    def test_short_collinear_road_extensions_are_guarded_before_mutation(self):
        road_commands = (MAYOR_TOOLS.ROOT / "src" / "RoadCommands.cs").read_text(encoding="utf-8")
        self.assertIn("WouldMergeShortCollinearExtension", road_commands)
        self.assertIn("shortCollinearMergeRisk", road_commands)
        self.assertIn("native short-segment merging", road_commands)
        guard = road_commands.index("if (!dryRun && shortCollinearMergeRisk)")
        create = road_commands.index("NetTool.CreateNode(", guard)
        self.assertLess(guard, create)

    def run_tool(self, *arguments, memory_path=None):
        environment = dict(os.environ)
        if memory_path is not None:
            environment["CS1_MAYOR_STATE_PATH"] = str(memory_path)
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), *arguments],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=environment,
        )
        return completed.returncode, json.loads(completed.stdout)

    def test_definitions_are_unique(self):
        code, payload = self.run_tool("definitions")
        self.assertEqual(0, code)
        names = [item["function"]["name"] for item in payload["tools"]]
        self.assertEqual(len(names), len(set(names)))
        self.assertIn("observe_city", names)
        self.assertIn("score_candidates", names)
        self.assertIn("save_mayor_memory", names)
        self.assertIn("capture_regression_baseline", names)
        self.assertIn("verify_regression", names)
        self.assertIn("set_simulation_speed", names)
        self.assertIn("validate_progression_mode", names)
        self.assertIn("inspect_toolbar_unlocks", names)
        self.assertIn("inspect_road_result", names)
        self.assertIn("propose_outside_road_connection", names)
        self.assertIn("inspect_water_pair", names)
        self.assertIn("propose_water_pair_candidates", names)
        self.assertIn("propose_water_service_connection", names)
        self.assertIn("preview_water_system_plan", names)
        self.assertIn("build_water_pipe", names)
        self.assertIn("build_water_service_pipe", names)
        self.assertIn("place_water_facility", names)
        self.assertIn("inspect_water_network_topology", names)
        self.assertIn("inspect_water_service_coverage", names)
        self.assertIn("inspect_electricity_service_coverage", names)
        self.assertIn("inspect_electricity_connection_area", names)
        self.assertIn("propose_power_line_connection", names)
        self.assertIn("propose_minimal_power_grid", names)
        self.assertIn("inspect_power_grid_efficiency", names)
        self.assertIn("inspect_wind_resource_map", names)
        self.assertIn("preview_power_plant_candidate", names)
        self.assertIn("propose_power_plant_candidates", names)
        self.assertIn("place_power_plant", names)
        self.assertIn("inspect_power_plant_operation", names)
        self.assertIn("inspect_road_layers_3d", names)
        self.assertIn("inspect_grade_separated_crossings", names)
        self.assertIn("inspect_building_layers_3d", names)
        self.assertIn("inspect_building_road_clearance_3d", names)
        self.assertIn("inspect_planning_area", names)
        self.assertIn("propose_development_plan", names)
        self.assertIn("inspect_facility_influence", names)
        self.assertIn("preview_building_candidate", names)
        self.assertIn("preview_bulldoze_impact", names)
        self.assertIn("bulldoze_entity", names)
        self.assertIn("preview_operational_change", names)
        self.assertIn("set_building_active", names)
        self.assertIn("preview_transport_plan", names)
        self.assertIn("preview_district_specialization", names)
        self.assertIn("set_district_specialization", names)
        self.assertIn("preview_road_upgrade", names)
        self.assertIn("upgrade_road", names)
        self.assertIn("preview_road_reversal", names)
        self.assertIn("reverse_road", names)

    def test_regression_baseline_survives_bridge_process_restart(self):
        before = self.regression_snapshot()
        after = self.regression_snapshot(population=21, cash=99900, problemEntities=0)
        identity = {
            "ok": True,
            "city": {"instanceId": "city-1", "name": "Fixture", "map": "Map"},
        }

        def capture_request(method, path, query=None, body=None):
            if path == "/commands/capture-regression-baseline":
                return {"ok": True, "name": body["name"], "baseline": before}
            if path == "/state/mayor-snapshot":
                return identity
            raise AssertionError((method, path, query, body))

        with mock.patch.object(MAYOR_TOOLS, "_request", side_effect=capture_request):
            captured = MAYOR_TOOLS.call_tool(
                "capture_regression_baseline", {"name": "restart-proof"}
            )
        self.assertTrue(captured["persistence"]["stored"])

        def verify_request(method, path, query=None, body=None):
            if path == "/state/regression-report":
                raise MAYOR_TOOLS.BridgeRequestError({
                    "ok": False,
                    "error": "Regression baseline was not found: restart-proof",
                })
            if path == "/state/mayor-snapshot":
                return identity
            if path == "/commands/capture-regression-baseline":
                self.assertTrue(body["name"].startswith("__recovery_"))
                return {"ok": True, "name": body["name"], "baseline": after}
            raise AssertionError((method, path, query, body))

        with mock.patch.object(MAYOR_TOOLS, "_request", side_effect=verify_request):
            report = MAYOR_TOOLS.call_tool(
                "verify_regression", {"name": "restart-proof"}
            )
        self.assertTrue(report["ok"])
        self.assertEqual("persistentRestartRecovery", report["baselineSource"])
        self.assertEqual(1, report["delta"]["population"])
        self.assertEqual(-100, report["delta"]["cash"])
        self.assertEqual(-1, report["delta"]["problemEntities"])

    def test_persisted_regression_baseline_is_city_scoped(self):
        before = self.regression_snapshot()
        MAYOR_TOOLS.mayor_regression.save_baseline(
            MAYOR_TOOLS.REGRESSION_BASELINES_PATH,
            "wrong-city",
            {"instanceId": "city-1", "name": "One", "map": "Map"},
            before,
        )

        def request(method, path, query=None, body=None):
            if path == "/state/regression-report":
                return {"ok": False, "error": "Regression baseline was not found: wrong-city"}
            if path == "/state/mayor-snapshot":
                return {
                    "ok": True,
                    "city": {"instanceId": "city-2", "name": "Two", "map": "Map"},
                }
            raise AssertionError((method, path, query, body))

        with mock.patch.object(MAYOR_TOOLS, "_request", side_effect=request):
            report = MAYOR_TOOLS.call_tool("verify_regression", {"name": "wrong-city"})
        self.assertFalse(report["ok"])
        self.assertIn("different city instance", report["error"])

    def test_growth_pacing_waits_when_high_demand_has_vacant_zoned_capacity(self):
        result = MAYOR_TOOLS._growth_pacing(
            {"residential": 55, "commercial": 15, "workplace": 13},
            {
                "capacityByZone": [
                    {
                        "zone": "ResidentialLow",
                        "validUnsharedCells": 128,
                        "occupiedCells": 32,
                        "vacantCells": 96,
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
        self.assertEqual("waitForAbsorption", residential["decision"])
        self.assertEqual(0, residential["maximumAdditionalBlocksThisWake"])
        self.assertFalse(result["highDemandAloneAuthorizesExpansion"])

    def test_growth_pacing_ignores_vacant_cells_stranded_behind_buildings(self):
        result = MAYOR_TOOLS._growth_pacing(
            {"residential": 55, "commercial": 15, "workplace": 13},
            {
                "capacityByZone": [
                    {
                        "zone": "ResidentialLow",
                        "validUnsharedCells": 128,
                        "occupiedCells": 32,
                        "vacantCells": 96,
                        "developableVacantCells": 54,
                    }
                ]
            },
        )
        residential = result["categories"]["residential"]
        self.assertEqual("considerSmallBlocks", residential["decision"])
        self.assertEqual(96, residential["vacantCells"])
        self.assertEqual(54, residential["developableVacantCells"])
        self.assertEqual(42, residential["strandedVacantCells"])
        self.assertEqual(2, residential["maximumAdditionalBlocksThisWake"])

    def test_growth_pacing_keeps_one_block_ahead_of_moderate_demand(self):
        result = MAYOR_TOOLS._growth_pacing(
            {"residential": 46, "commercial": 25, "workplace": 13},
            {
                "capacityByZone": [
                    {
                        "zone": "ResidentialLow",
                        "validUnsharedCells": 80,
                        "occupiedCells": 66,
                        "vacantCells": 14,
                        "developableVacantCells": 14,
                    }
                ]
            },
        )
        residential = result["categories"]["residential"]
        self.assertEqual("considerSmallBlocks", residential["decision"])
        self.assertEqual(24, residential["targetVacantCells"])
        self.assertEqual(1, residential["maximumAdditionalBlocksThisWake"])
        self.assertEqual("hold", result["categories"]["commercial"]["decision"])

    def test_growth_pacing_caps_blocks_per_wake_when_capacity_is_nearly_full(self):
        result = MAYOR_TOOLS._growth_pacing(
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

    def test_growth_pacing_reports_missing_bridge_capacity_without_guessing(self):
        result = MAYOR_TOOLS._growth_pacing({"residential": 100}, {"zonedCells": 20})
        self.assertFalse(result["available"])
        self.assertEqual("capacityByZoneUnavailable", result["reason"])
        self.assertFalse(result["mutationAuthorized"])

    def test_growth_capacity_reports_frontage_rejection_reasons(self):
        demand = {"residential": 10, "commercial": 5, "workplace": 80}
        zones = {"capacityByZone": []}
        actionable = {"ok": True, "totalMatching": 0, "returned": 0, "blocks": []}
        diagnostic = {
            "ok": True,
            "totalMatching": 3,
            "returned": 3,
            "blocks": [
                {
                    "blockId": 11,
                    "roadSegmentId": 101,
                    "roadName": "Short frontage",
                    "center": {"x": 1, "y": 2, "z": 3},
                    "rows": 4,
                    "validCells": 16,
                    "developmentFrontageSafe": False,
                    "frontageReason": "nearConnectedRoadEnd",
                    "junctionClearanceSafe": False,
                },
                {
                    "blockId": 12,
                    "roadSegmentId": 102,
                    "developmentFrontageSafe": False,
                    "frontageReason": "nearConnectedRoadEnd",
                    "junctionClearanceSafe": False,
                },
                {
                    "blockId": 13,
                    "roadSegmentId": 103,
                    "developmentFrontageSafe": False,
                    "frontageReason": "touchesHighway",
                    "junctionClearanceSafe": True,
                },
            ],
        }
        anomalies = {"ok": True, "total": 0, "returned": 0, "anomalies": []}

        def request(method, path, query=None, body=None):
            if path == "/state/demand":
                return demand
            if path == "/state/zones":
                return zones
            if path == "/state/zone-anomalies":
                return anomalies
            if path == "/state/zone-blocks":
                if query["requireDevelopmentFrontage"] == "true":
                    return actionable
                self.assertEqual(200, query["limit"])
                return diagnostic
            raise AssertionError((method, path, query, body))

        with mock.patch.object(MAYOR_TOOLS, "_request", side_effect=request):
            result = MAYOR_TOOLS.call_tool("inspect_growth_capacity", {"limit": 2})

        self.assertEqual(actionable, result["actionableBlocks"])
        frontage = result["frontageDiagnostics"]
        self.assertTrue(frontage["diagnosticComplete"])
        self.assertEqual(3, frontage["totalClearUnzonedBlocks"])
        self.assertEqual(
            {"nearConnectedRoadEnd": 2, "touchesHighway": 1},
            frontage["blockedCountsByReason"],
        )
        self.assertEqual([11, 12], [item["blockId"] for item in frontage["blockedSamples"]])

    def test_tool_clusters_cover_all_definitions_and_keep_core_compatible(self):
        definitions = MAYOR_TOOLS._definitions()
        catalog = MAYOR_TOOLS._cluster_catalog()
        all_names = {item["function"]["name"] for item in definitions}
        covered = {
            name
            for cluster in catalog["clusters"]
            for name in cluster["tools"]
        }
        self.assertEqual(all_names, covered)
        self.assertEqual(
            MAYOR_TOOLS.MAYOR_PROFILE,
            frozenset(
                next(
                    cluster["tools"]
                    for cluster in catalog["clusters"]
                    if cluster["name"] == "core"
                )
            ),
        )

    def test_cluster_definitions_load_only_requested_domain(self):
        code, payload = self.run_tool("definitions", "--cluster", "roads")
        self.assertEqual(0, code)
        names = {item["function"]["name"] for item in payload["tools"]}
        self.assertIn("preview_network", names)
        self.assertIn("inspect_road_result", names)
        self.assertIn("build_network", names)
        self.assertNotIn("inspect_water_pair", names)
        self.assertNotIn("assess_city", names)

    def test_cluster_catalog_reports_schema_size_reduction_without_full_schemas(self):
        code, payload = self.run_tool("clusters")
        self.assertEqual(0, code)
        self.assertTrue(payload["ok"])
        self.assertEqual(99, payload["allToolCount"])
        roads = next(cluster for cluster in payload["clusters"] if cluster["name"] == "roads")
        self.assertGreater(roads["characterReductionVsAll"], 0.5)
        self.assertNotIn("parameters", roads)
        self.assertEqual(roads["toolCount"], len(roads["tools"]))

    def test_unknown_tool_cluster_fails_closed(self):
        code, payload = self.run_tool("definitions", "--cluster", "not-a-cluster")
        self.assertEqual(1, code)
        self.assertFalse(payload["ok"])
        self.assertEqual("Unknown tool cluster: not-a-cluster", payload["error"])

    def test_bridge_http_error_preserves_structured_recovery(self):
        payload = {
            "ok": False,
            "error": "mutation failed",
            "recovery": {
                "localRollbackAttempted": True,
                "localRollbackSucceeded": False,
                "checkpointRestoreRequired": True,
                "entityType": "netSegment",
                "entityId": 42,
            },
        }
        http_error = urllib.error.HTTPError(
            "http://127.0.0.1:32123/commands/build-road",
            409,
            "Conflict",
            {},
            io.BytesIO(json.dumps(payload).encode("utf-8")),
        )
        with mock.patch.object(MAYOR_TOOLS.urllib.request, "urlopen", side_effect=http_error):
            with self.assertRaises(MAYOR_TOOLS.BridgeRequestError) as raised:
                MAYOR_TOOLS._request("POST", "/commands/build-road", body={})
        self.assertEqual(payload, raised.exception.detail)
        http_error.close()

    def test_road_result_accepts_only_the_declared_terminal_dead_end(self):
        segment = {
            "startNodeId": 10,
            "endNodeId": 20,
            "start": {"x": 0, "y": 0, "z": 0},
            "end": {"x": 100, "y": 1, "z": 0},
            "flags": "Created",
            "problems": "",
            "actualLaneCount": 2,
            "prefabLaneCount": 2,
            "prefabLanes": [
                {"laneType": "Vehicle", "finalDirection": "Forward"},
                {"laneType": "Vehicle", "finalDirection": "Backward"},
            ],
            "directionality": "Bidirectional",
            "isBidirectional": True,
            "isOneWay": False,
            "isGroundRoad": True,
            "terrainProfile": self.inspection_terrain_profile(),
        }
        start_node = {"connectCount": 2}
        end_node = {"connectCount": 1}
        anomalies = {
            "anomalies": [
                {
                    "type": "deadEndRoad",
                    "nodeId": 20,
                    "ownSegmentId": 42,
                }
            ]
        }
        with mock.patch.object(
            MAYOR_TOOLS,
            "_request",
            side_effect=[
                segment,
                start_node,
                end_node,
                anomalies,
                {"safeForTwoWayFreight": True},
                {"ok": True},
            ],
        ):
            result = MAYOR_TOOLS._inspect_road_result(42, True, 20)
        self.assertTrue(result["ok"])
        self.assertEqual([], result["focusedAnomalies"])
        self.assertEqual(1, len(result["acceptedTerminalAnomalies"]))

    def test_road_result_reports_a_hillside_cut_without_blocking(self):
        segment = {
            "startNodeId": 10, "endNodeId": 20,
            "start": {"x": 0, "y": 0, "z": 0}, "end": {"x": 100, "y": 1, "z": 0},
            "flags": "Created", "problems": "", "actualLaneCount": 2, "prefabLaneCount": 2,
            "prefabLanes": [], "isGroundRoad": True, "terrainProfile": self.inspection_terrain_profile(),
        }
        anomalies = {"anomalies": [{"type": "roadTerrainCliff", "segmentId": 42, "maxRoadToTerrainDelta": 20.4},
                                   {"type": "roadBelowLocalGrade", "segmentId": 42}]}
        with mock.patch.object(MAYOR_TOOLS, "_request", side_effect=[
                segment, {"connectCount": 2}, {"connectCount": 2}, anomalies,
                {"safeForTwoWayFreight": True}, {"ok": True}]):
            result = MAYOR_TOOLS._inspect_road_result(42, True, 20)
        self.assertTrue(result["ok"])
        self.assertEqual([], result["focusedAnomalies"])
        self.assertEqual(["roadTerrainCliff", "roadBelowLocalGrade"], [item["type"] for item in result["terrainNotes"]])

    def test_road_result_keeps_near_miss_at_declared_terminal_blocking(self):
        segment = {
            "startNodeId": 10,
            "endNodeId": 20,
            "start": {"x": 0, "y": 0, "z": 0},
            "end": {"x": 100, "y": 1, "z": 0},
            "flags": "Created",
            "problems": "",
            "actualLaneCount": 2,
            "prefabLaneCount": 2,
            "prefabLanes": [],
            "isGroundRoad": True,
            "terrainProfile": self.inspection_terrain_profile(),
        }
        anomalies = {
            "anomalies": [
                {
                    "type": "deadEndNearRoad",
                    "nodeId": 20,
                    "ownSegmentId": 42,
                }
            ]
        }
        with mock.patch.object(
            MAYOR_TOOLS,
            "_request",
            side_effect=[
                segment,
                {"connectCount": 2},
                {"connectCount": 1},
                anomalies,
                {"safeForTwoWayFreight": True},
                {"ok": True},
            ],
        ):
            result = MAYOR_TOOLS._inspect_road_result(42, True, 20)
        self.assertFalse(result["ok"])
        self.assertEqual("deadEndNearRoad", result["focusedAnomalies"][0]["type"])
        self.assertEqual([], result["acceptedTerminalAnomalies"])

    def test_road_result_rejects_missing_or_unsafe_actual_terrain_profile(self):
        segment = {
            "startNodeId": 10,
            "endNodeId": 20,
            "start": {"x": 0, "y": 0, "z": 0},
            "end": {"x": 100, "y": 0, "z": 0},
            "flags": "Created",
            "problems": "",
            "actualLaneCount": 2,
            "prefabLaneCount": 2,
            "prefabLanes": [],
            "isGroundRoad": True,
            "terrainProfile": self.inspection_terrain_profile(
                maxRoadToTerrainDelta=1.2,
                minimumRoadAboveTerrain=-0.2,
                safe=False,
            ),
        }
        with mock.patch.object(
            MAYOR_TOOLS,
            "_request",
            side_effect=[
                segment,
                {"connectCount": 1},
                {"connectCount": 1},
                {"anomalies": []},
                {"safeForTwoWayFreight": True},
                {"ok": True},
            ],
        ):
            result = MAYOR_TOOLS._inspect_road_result(42, True)
        self.assertFalse(result["ok"])
        self.assertFalse(result["checks"]["actualTerrainProfileSafe"])
        missing = {**segment, "terrainProfile": None}
        with mock.patch.object(
            MAYOR_TOOLS,
            "_request",
            side_effect=[
                missing,
                {"connectCount": 1},
                {"connectCount": 1},
                {"anomalies": []},
                {"safeForTwoWayFreight": True},
                {"ok": True},
            ],
        ):
            missing_result = MAYOR_TOOLS._inspect_road_result(42, True)
        self.assertFalse(missing_result["ok"])
        self.assertFalse(missing_result["checks"]["actualTerrainProfileReported"])

    def test_road_result_rejects_small_but_negative_ground_clearance(self):
        segment = {
            "startNodeId": 10,
            "endNodeId": 20,
            "start": {"x": 0, "y": 0, "z": 0},
            "end": {"x": 100, "y": 0, "z": 0},
            "flags": "Created",
            "problems": "",
            "actualLaneCount": 2,
            "prefabLaneCount": 2,
            "prefabLanes": [],
            "isGroundRoad": True,
            "terrainProfile": self.inspection_terrain_profile(
                maxRoadToTerrainDelta=0.2,
                minimumRoadAboveTerrain=-0.2,
                safe=False,
            ),
        }
        with mock.patch.object(
            MAYOR_TOOLS,
            "_request",
            side_effect=[
                segment,
                {"connectCount": 1},
                {"connectCount": 1},
                {"anomalies": []},
                {"safeForTwoWayFreight": True},
                {"ok": True},
            ],
        ):
            result = MAYOR_TOOLS._inspect_road_result(42, True)
        self.assertFalse(result["ok"])
        self.assertFalse(result["checks"]["actualTerrainProfileSafe"])

    def test_inspection_terrain_schema_fails_closed(self):
        baseline = self.inspection_terrain_profile()
        for field, value in (
            ("source", "legacy"),
            ("verificationStage", "immediate"),
            ("clearanceModel", "terrainSeededControlCurve"),
            ("sampleCount", True),
            ("safe", 1),
        ):
            with self.subTest(field=field, value=value), self.assertRaises(RuntimeError):
                MAYOR_TOOLS._validated_native_road_terrain_profile(
                    {**baseline, field: value}, "inspection", require_ground=True)

    def test_water_pair_rejects_close_outlet(self):
        preview = {
            "valid": True,
            "gamePlacement": {"productionRate": 100},
            "waterPlacement": {
                "location": {"x": 0, "y": 100, "z": 0},
                "closestWater": {"found": True},
            },
        }
        outlet_preview = {
            "valid": True,
            "gamePlacement": {"productionRate": 100},
            "waterPlacement": {
                "location": {"x": 32, "y": 100, "z": 0},
                "closestWater": {"found": True},
            },
        }
        map_analysis = {
            "cells": [
                {
                    "position": {"x": 0, "z": 0},
                    "water": {"present": True, "velocity": {"x": 1, "z": 0}},
                },
                {
                    "position": {"x": 32, "z": 0},
                    "water": {"present": True, "velocity": {"x": 1, "z": 0}},
                },
            ]
        }
        coverage = {
            "samples": [
                {"position": {"x": 0, "z": 0}, "waterPollution": 0},
                {"position": {"x": 32, "z": 0}, "waterPollution": 0},
            ]
        }
        with mock.patch.object(
            MAYOR_TOOLS,
            "_request",
            side_effect=[preview, outlet_preview, map_analysis, coverage],
        ):
            result = MAYOR_TOOLS._inspect_water_pair(
                {
                    "intake": {
                        "buildingPrefab": "Water Intake",
                        "position": {"x": 0, "z": 0},
                        "angleDegrees": 180,
                    },
                    "outlet": {
                        "buildingPrefab": "Water Outlet",
                        "position": {"x": 32, "z": 0},
                        "angleDegrees": 180,
                    },
                    "checkUnlockedReach": False,
                }
            )
        self.assertFalse(result["safe"])
        self.assertIn("insufficientSeparation", result["risks"])

    def test_water_pair_never_overrides_dead_local_water(self):
        intake_preview = {
            "valid": True,
            "gamePlacement": {"productionRate": 100},
            "waterPlacement": {
                "location": {"x": 0, "y": 100, "z": 0},
                "closestWater": {"found": True},
            },
        }
        outlet_preview = {
            "valid": True,
            "gamePlacement": {"productionRate": 100},
            "waterPlacement": {
                "location": {"x": 300, "y": 100, "z": 0},
                "closestWater": {"found": True},
            },
        }
        map_analysis = {
            "cells": [
                {
                    "position": {"x": 0, "z": 0},
                    "water": {"present": True, "velocity": {"x": 0, "z": 0}},
                },
                {
                    "position": {"x": 300, "z": 0},
                    "water": {"present": True, "velocity": {"x": 1, "z": 0}},
                },
            ]
        }
        coverage = {
            "samples": [
                {"position": {"x": 0, "z": 0}, "waterPollution": 0},
                {"position": {"x": 300, "z": 0}, "waterPollution": 0},
            ]
        }
        with mock.patch.object(
            MAYOR_TOOLS,
            "_request",
            side_effect=[intake_preview, outlet_preview, map_analysis, coverage],
        ):
            result = MAYOR_TOOLS._inspect_water_pair(
                {
                    "intake": {
                        "buildingPrefab": "Water Intake",
                        "position": {"x": 0, "z": 0},
                        "angleDegrees": 180,
                    },
                    "outlet": {
                        "buildingPrefab": "Water Outlet",
                        "position": {"x": 300, "z": 0},
                        "angleDegrees": 180,
                    },
                    "checkUnlockedReach": False,
                }
            )
        self.assertFalse(result["safe"])
        self.assertIn("intakeDeadOrWeakWater", result["risks"])

    def test_unlocked_reach_requires_extreme_placement(self):
        water_cells = [
            {
                "position": {"x": x, "z": 0},
                "unlocked": True,
                "water": {"present": True, "velocity": {"x": 1, "z": 0}},
            }
            for x in range(0, 321, 64)
        ]
        land_cells = [
            {
                "position": {"x": x, "z": 64},
                "unlocked": True,
                "buildable": True,
                "water": {"present": False},
            }
            for x in range(0, 321, 64)
        ]
        with mock.patch.object(
            MAYOR_TOOLS,
            "_request",
            side_effect=[
                {"areas": [{"x": 2, "z": 2, "unlocked": True}]},
                {"cells": water_cells + land_cells},
            ],
        ):
            result = MAYOR_TOOLS._inspect_unlocked_water_reach(
                {"x": 192, "z": 0},
                {"x": 256, "z": 0},
                {
                    "unlockedReachResolution": 31,
                    "maxUpstreamGap": 64,
                    "maxDownstreamGap": 64,
                },
            )
        self.assertFalse(result["safe"])
        self.assertIn("intakeNotNearUnlockedUpstreamExtreme", result["risks"])
        self.assertNotIn("outletNotNearUnlockedDownstreamExtreme", result["risks"])
        self.assertEqual(6, result["feasibleShorelineSampleCount"])

    def test_water_pair_candidate_failure_reports_full_search_diagnostics(self):
        water_cell = {
            "position": {"x": 0, "z": 0},
            "water": {"present": True, "velocity": {"x": 1, "z": 0}},
        }
        candidates = [
            {
                "buildingPrefab": "Water Intake",
                "position": {"x": index * 32, "z": 0},
                "angleDegrees": 0,
                "waterLocation": {"x": index * 32, "z": 0},
            }
            for index in range(3)
        ]
        outlets = [
            {
                "buildingPrefab": "Water Outlet",
                "position": {"x": 512 + index * 32, "z": 0},
                "angleDegrees": 0,
                "waterLocation": {"x": 512 + index * 32, "z": 0},
            }
            for index in range(3)
        ]
        failed_pair = {
            "safe": False,
            "risks": ["intakeNotNearUnlockedUpstreamExtreme"],
            "warnings": [],
            "geometry": {"separation": 512},
            "flow": {"agreement": 1},
            "pollution": {"intake": 0, "outlet": 0},
            "unlockedReach": {
                "safe": False,
                "risks": ["intakeNotNearUnlockedUpstreamExtreme"],
                "endpointsConnected": True,
                "intakeUpstreamGap": 192,
                "outletDownstreamGap": 0,
                "thresholds": {"maxUpstreamGap": 128},
            },
        }
        with (
            mock.patch.object(
                MAYOR_TOOLS,
                "_sample_unlocked_map",
                return_value=([water_cell], {"resolution": 48}),
            ),
            mock.patch.object(
                MAYOR_TOOLS,
                "_feasible_shoreline_samples",
                return_value=[{"water": water_cell, "land": water_cell}],
            ),
            mock.patch.object(
                MAYOR_TOOLS,
                "_preview_shore_facility_candidates",
                side_effect=[candidates, outlets],
            ),
            mock.patch.object(
                MAYOR_TOOLS, "_inspect_water_pair", return_value=failed_pair
            ),
        ):
            result = MAYOR_TOOLS._propose_water_pair_candidates({})

        self.assertFalse(result["safe"])
        self.assertEqual({"intake": 3, "outlet": 3}, result["candidateCounts"])
        self.assertEqual(9, result["totalAttemptCount"])
        self.assertEqual(8, result["returnedAttemptCount"])
        self.assertTrue(result["searchExhausted"])
        self.assertEqual(
            9, result["rejectionCounts"]["intakeNotNearUnlockedUpstreamExtreme"]
        )
        self.assertEqual(
            192,
            result["bestFailedAttempt"]["evidence"]["unlockedReach"][
                "intakeUpstreamGap"
            ],
        )

    def test_unlocked_reach_uses_native_valid_facility_extremes(self):
        water_cells = [
            {
                "position": {"x": x, "z": 0},
                "unlocked": True,
                "water": {"present": True, "velocity": {"x": 1, "z": 0}},
            }
            for x in range(0, 321, 64)
        ]
        land_cells = [
            {
                "position": {"x": x, "z": 64},
                "unlocked": True,
                "buildable": True,
                "water": {"present": False},
            }
            for x in range(0, 321, 64)
        ]
        intake_reference = {
            "waterLocation": {"x": 128, "z": 0},
        }
        outlet_reference = {
            "waterLocation": {"x": 320, "z": 0},
        }
        with (
            mock.patch.object(
                MAYOR_TOOLS,
                "_sample_unlocked_map",
                return_value=(water_cells + land_cells, {"resolution": 31}),
            ),
            mock.patch.object(
                MAYOR_TOOLS,
                "_preview_shore_facility_candidates",
                side_effect=[[intake_reference], [outlet_reference]],
            ),
        ):
            result = MAYOR_TOOLS._inspect_unlocked_water_reach(
                {"x": 192, "z": 0},
                {"x": 256, "z": 0},
                {
                    "unlockedReachResolution": 31,
                    "maxUpstreamGap": 64,
                    "maxDownstreamGap": 64,
                    "_intakePrefab": "Water Intake",
                    "_outletPrefab": "Water Outlet",
                },
            )

        self.assertTrue(result["safe"])
        self.assertEqual(64, result["intakeUpstreamGap"])
        self.assertEqual(64, result["outletDownstreamGap"])
        self.assertEqual(
            "nativeValidFacilityCandidates", result["extremeBasis"]["source"]
        )

    def test_curved_connected_reach_downgrades_local_flow_disagreement(self):
        intake_preview = {
            "valid": True,
            "gamePlacement": {"productionRate": 100},
            "waterPlacement": {
                "location": {"x": 0, "y": 100, "z": 0},
                "closestWater": {"found": True},
            },
        }
        outlet_preview = {
            "valid": True,
            "gamePlacement": {"productionRate": 100},
            "waterPlacement": {
                "location": {"x": 300, "y": 100, "z": 0},
                "closestWater": {"found": True},
            },
        }
        map_analysis = {
            "cells": [
                {
                    "position": {"x": 0, "z": 0},
                    "water": {"present": True, "velocity": {"x": 1, "z": 0}},
                },
                {
                    "position": {"x": 300, "z": 0},
                    "water": {"present": True, "velocity": {"x": 0, "z": 1}},
                },
            ]
        }
        coverage = {
            "samples": [
                {"position": {"x": 0, "z": 0}, "waterPollution": 0},
                {"position": {"x": 300, "z": 0}, "waterPollution": 0},
            ]
        }
        with mock.patch.object(
            MAYOR_TOOLS,
            "_request",
            side_effect=[intake_preview, outlet_preview, map_analysis, coverage],
        ), mock.patch.object(
            MAYOR_TOOLS,
            "_inspect_unlocked_water_reach",
            return_value={"safe": True, "risks": [], "endpointsConnected": True},
        ):
            result = MAYOR_TOOLS._inspect_water_pair(
                {
                    "intake": {
                        "buildingPrefab": "Water Intake",
                        "position": {"x": 0, "z": 0},
                        "angleDegrees": 0,
                    },
                    "outlet": {
                        "buildingPrefab": "Water Outlet",
                        "position": {"x": 300, "z": 0},
                        "angleDegrees": 0,
                    },
                    "minDownstreamProjection": 128,
                }
            )
        self.assertTrue(result["safe"])
        self.assertNotIn("inconsistentFlowDirection", result["risks"])
        self.assertIn("localFlowDirectionsDifferOnCurvedReach", result["warnings"])

    def test_shore_candidate_selects_water_port_facing_away_from_land(self):
        shoreline = [
            {
                "land": {"position": {"x": 0, "z": 0}},
                "water": {"position": {"x": 64, "z": 0}},
            }
        ]
        previews = []
        for angle in range(0, 360, 45):
            if angle == 90:
                location = {"x": 24, "y": 10, "z": 0}
            elif angle == 45:
                location = {"x": 16, "y": 10, "z": 16}
            else:
                location = {"x": -24, "y": 10, "z": 0}
            previews.append(
                {
                    "valid": True,
                    "gamePlacement": {"productionRate": 100},
                    "waterPlacement": {
                        "location": location,
                        "closestWater": {"found": True, "x": location["x"], "z": location["z"]},
                    },
                    "safety": {"riskCount": 0},
                }
            )
        with mock.patch.object(MAYOR_TOOLS, "_request", side_effect=previews):
            result = MAYOR_TOOLS._preview_shore_facility_candidates(
                "Water Outlet",
                shoreline,
                1,
                0,
                upstream=False,
                limit=1,
            )
        self.assertEqual(1, len(result))
        self.assertEqual(90, result[0]["angleDegrees"])
        self.assertTrue(result[0]["orientation"]["facesWater"])
        self.assertEqual(1.0, result[0]["orientation"]["waterSideAlignment"])

    def test_wind_resource_map_ranks_native_samples_without_placement(self):
        payload = {
            "ok": True,
            "source": "WeatherManager.GetWindSpeed",
            "bounds": {"minX": -10, "minZ": -10, "maxX": 10, "maxZ": 10},
            "resolution": 3,
            "sampleCount": 3,
            "summary": {"minimum": 0.2, "maximum": 0.9, "average": 0.6},
            "cells": [
                {"position": {"x": 0, "z": 0}, "windSpeed": 0.7},
                {"position": {"x": 1, "z": 0}, "windSpeed": 0.9},
                {"position": {"x": 2, "z": 0}, "windSpeed": 0.2},
            ],
        }
        with mock.patch.object(MAYOR_TOOLS, "_request", return_value=payload) as request:
            result = MAYOR_TOOLS._inspect_wind_resource_map(
                {
                    "center": {"x": 0, "z": 0},
                    "radius": 10,
                    "resolution": 3,
                    "maxResults": 2,
                }
            )
        self.assertEqual("WeatherManager.GetWindSpeed", result["source"])
        self.assertEqual(0.9, result["highestWindSamples"][0]["windSpeed"])
        self.assertEqual(2, len(result["highestWindSamples"]))
        self.assertEqual("/state/wind-map", request.call_args.args[1])

    def test_power_line_proposer_connects_powered_area_to_target_blue_radius(self):
        connection_area = {
            "ok": True,
            "building": {
                "id": 42,
                "prefab": "Water Pumping Station",
                "position": {"x": 100, "y": 10, "z": 0},
                "electricityGridRadius": 38,
                "powered": False,
            },
            "bounds": {"minX": -100, "minZ": -100, "maxX": 200, "maxZ": 100},
            "summary": {
                "conductiveSamples": 2,
                "poweredSamples": 1,
                "targetAreaSamples": 1,
                "poweredTargetAreaSamples": 0,
            },
            "cells": [
                {
                    "position": {"x": 0, "y": 10, "z": 0},
                    "distanceToBuilding": 100,
                    "insideTargetGridRadius": False,
                    "conductive": True,
                    "powered": True,
                },
                {
                    "position": {"x": 72, "y": 10, "z": 0},
                    "distanceToBuilding": 28,
                    "insideTargetGridRadius": True,
                    "conductive": True,
                    "powered": False,
                },
            ],
        }
        preview = {
            "ok": True,
            "dryRun": True,
            "valid": True,
            "toolErrors": "None",
            "powerLineRoadClearance": {
                "connectedToExistingGrid": True,
                "safe": True,
            },
        }
        with mock.patch.object(
            MAYOR_TOOLS,
            "_request",
            side_effect=[connection_area, preview],
        ) as request:
            result = MAYOR_TOOLS._propose_power_line_connection(
                {
                    "buildingId": 42,
                    "powerLinePrefab": "Power Line",
                    "_buildingObstacles": [],
                }
            )
        self.assertTrue(result["safe"])
        self.assertTrue(result["actionRequired"])
        self.assertEqual({"x": 0.0, "z": 0.0}, result["candidate"]["start"])
        self.assertEqual({"x": 72.0, "z": 0.0}, result["candidate"]["end"])
        self.assertTrue(result["candidate"]["source"]["powered"])
        self.assertTrue(
            result["candidate"]["target"]["insideBuildingGridRadius"]
        )
        self.assertEqual(
            "/commands/preview-network",
            request.call_args_list[1].args[1],
        )

    def test_power_line_proposer_skips_mutation_when_target_is_powered(self):
        connection_area = {
            "ok": True,
            "building": {
                "id": 42,
                "prefab": "Water Pumping Station",
                "powered": True,
            },
            "cells": [],
        }
        with mock.patch.object(
            MAYOR_TOOLS,
            "_request",
            return_value=connection_area,
        ) as request:
            result = MAYOR_TOOLS._propose_power_line_connection(
                {"buildingId": 42}
            )
        self.assertTrue(result["safe"])
        self.assertFalse(result["actionRequired"])
        self.assertEqual("targetBuildingAlreadyPowered", result["reason"])
        request.assert_called_once()

    def test_power_line_proposer_rejects_building_footprint_before_native_preview(self):
        connection_area = {
            "ok": True,
            "building": {
                "id": 42,
                "position": {"x": 100, "z": 0},
                "electricityGridRadius": 64,
                "powered": False,
            },
            "cells": [
                {
                    "position": {"x": 0, "z": 0},
                    "powered": True,
                    "conductive": True,
                }
            ],
            "targetConnectionSamples": [
                {
                    "position": {"x": 100, "z": 0},
                    "distanceToBuilding": 32,
                    "insideTargetGridRadius": True,
                    "outsideBuildingFootprint": True,
                    "conductive": True,
                    "powered": False,
                },
                {
                    "position": {"x": 100, "z": 50},
                    "distanceToBuilding": 32,
                    "insideTargetGridRadius": True,
                    "outsideBuildingFootprint": True,
                    "conductive": True,
                    "powered": False,
                },
            ],
        }
        obstacle = {
            "buildingId": 99,
            "position": {"x": 50, "z": 0},
            "axisX": {"x": 1, "z": 0},
            "axisZ": {"x": 0, "z": 1},
            "widthMeters": 20,
            "lengthMeters": 20,
        }
        preview = {"ok": True, "valid": True, "toolErrors": "None"}
        with mock.patch.object(
            MAYOR_TOOLS,
            "_request",
            side_effect=[connection_area, preview],
        ) as request:
            result = MAYOR_TOOLS._propose_power_line_connection(
                {
                    "buildingId": 42,
                    "maxPreviewAttempts": 1,
                    "_buildingObstacles": [obstacle],
                }
            )

        self.assertTrue(result["safe"])
        self.assertEqual({"x": 100.0, "z": 50.0}, result["candidate"]["end"])
        self.assertEqual(1, result["previewAttempts"])
        self.assertEqual(1, result["rejectedCounts"]["buildingFootprintClearance"])
        self.assertEqual(2, len(request.call_args_list))

    def test_power_line_footprint_clearance_is_translation_rotation_invariant(self):
        building = {
            "position": {"x": 50, "z": 0},
            "axisX": {"x": 1, "z": 0},
            "axisZ": {"x": 0, "z": 1},
            "widthMeters": 20,
            "lengthMeters": 12,
        }
        crossing = ({"x": 0, "z": 0}, {"x": 100, "z": 0})
        clear = ({"x": 0, "z": 30}, {"x": 100, "z": 30})
        self.assertTrue(
            MAYOR_TOOLS.segment_intersects_building_footprint(
                *crossing, building, clearance=4
            )
        )
        self.assertFalse(
            MAYOR_TOOLS.segment_intersects_building_footprint(
                *clear, building, clearance=4
            )
        )

        def transform(point):
            return {"x": 1000 - point["z"], "z": -500 + point["x"]}

        transformed = {
            **building,
            "position": transform(building["position"]),
            "axisX": {"x": 0, "z": 1},
            "axisZ": {"x": -1, "z": 0},
        }
        self.assertTrue(
            MAYOR_TOOLS.segment_intersects_building_footprint(
                transform(crossing[0]), transform(crossing[1]), transformed, clearance=4
            )
        )
        self.assertFalse(
            MAYOR_TOOLS.segment_intersects_building_footprint(
                transform(clear[0]), transform(clear[1]), transformed, clearance=4
            )
        )

    def test_power_line_build_fails_closed_before_mutation_on_building_footprint(self):
        obstacle = {
            "buildingId": 99,
            "position": {"x": 50, "z": 0},
            "axisX": {"x": 1, "z": 0},
            "axisZ": {"x": 0, "z": 1},
            "widthMeters": 20,
            "lengthMeters": 20,
        }
        with mock.patch.object(
            MAYOR_TOOLS, "_read_live_power_line_obstacles", return_value=[obstacle]
        ), mock.patch.object(MAYOR_TOOLS, "_request") as request:
            with self.assertRaisesRegex(RuntimeError, "building footprint"):
                MAYOR_TOOLS._build_verified_network(
                    {
                        "roadPrefab": "Power Line",
                        "start": {"x": 0, "z": 0},
                        "end": {"x": 100, "z": 0},
                    },
                    "checkpoint-test",
                )
        request.assert_not_called()

    def test_power_line_obstacle_inventory_fails_closed_when_truncated(self):
        facilities = {
            "ok": True,
            "total": 5001,
            "returned": 5000,
            "facilities": [],
        }
        growables = {
            "ok": True,
            "total": 0,
            "returned": 0,
            "growables": [],
        }
        with mock.patch.object(
            MAYOR_TOOLS, "_request", side_effect=[facilities, growables]
        ):
            with self.assertRaisesRegex(RuntimeError, "inventory is truncated"):
                MAYOR_TOOLS._read_live_power_line_obstacles()

    def test_minimal_power_grid_uses_residential_area_as_shorter_bridge(self):
        def area(building_id, x, z, radius, powered):
            samples = []
            for angle, dx, dz in (
                (0, 1, 0),
                (90, 0, 1),
                (180, -1, 0),
                (270, 0, -1),
            ):
                samples.append(
                    {
                        "angleDegrees": angle,
                        "radialFraction": 0.95,
                        "position": {
                            "x": x + dx * radius * 0.95,
                            "z": z + dz * radius * 0.95,
                        },
                    }
                )
            return {
                "ok": True,
                "building": {
                    "id": building_id,
                    "position": {"x": x, "z": z},
                    "electricityGridRadius": radius,
                    "powered": powered,
                },
                "targetConnectionSamples": samples,
            }

        areas = {
            1: area(1, 0, 0, 20, True),
            2: area(2, 80, 0, 20, False),
            3: area(3, 0, 80, 20, False),
            4: area(4, 0, 160, 20, False),
        }

        def request(method, path, query=None, body=None):
            if path == "/state/electricity-connection-area":
                return areas[int(query["buildingId"])]
            if path == "/commands/preview-network":
                return {"ok": True, "valid": True, "toolErrors": "None"}
            raise AssertionError(path)

        with mock.patch.object(MAYOR_TOOLS, "_request", side_effect=request):
            result = MAYOR_TOOLS._propose_minimal_power_grid(
                {
                    "sourceBuildingId": 1,
                    "buildingIds": [2, 3, 4],
                    "maxPreviewAttemptsPerEdge": 8,
                    "_buildingObstacles": [],
                }
            )

        topology = {
            frozenset(
                (segment["fromBuildingId"], segment["toBuildingId"])
            )
            for segment in result["segments"]
        }
        self.assertTrue(result["safe"])
        self.assertEqual(
            {
                frozenset((1, 2)),
                frozenset((1, 3)),
                frozenset((3, 4)),
            },
            topology,
        )
        self.assertEqual([1, 2, 3], [item["buildOrder"] for item in result["segments"]])
        self.assertGreater(result["estimatedSavingsAgainstDirectStar"], 0)

    def test_minimal_power_grid_rejects_building_corridor_before_native_preview(self):
        def area(building_id, x, powered):
            return {
                "ok": True,
                "building": {
                    "id": building_id,
                    "position": {"x": x, "z": 0},
                    "electricityGridRadius": 20,
                    "powered": powered,
                },
                "targetConnectionSamples": [
                    {
                        "angleDegrees": 0,
                        "radialFraction": 0.95,
                        "position": {"x": x, "z": 0},
                        "outsideBuildingFootprint": True,
                        "conductive": True,
                    }
                ],
            }

        areas = {1: area(1, 0, True), 2: area(2, 100, False)}
        obstacle = {
            "buildingId": 99,
            "position": {"x": 50, "z": 0},
            "axisX": {"x": 1, "z": 0},
            "axisZ": {"x": 0, "z": 1},
            "widthMeters": 20,
            "lengthMeters": 20,
        }

        def request(method, path, query=None, body=None):
            if path == "/state/electricity-connection-area":
                return areas[int(query["buildingId"])]
            raise AssertionError(f"unexpected native preview: {path}")

        with mock.patch.object(MAYOR_TOOLS, "_request", side_effect=request) as mocked:
            result = MAYOR_TOOLS._propose_minimal_power_grid(
                {
                    "sourceBuildingId": 1,
                    "buildingIds": [2],
                    "_buildingObstacles": [obstacle],
                }
            )

        self.assertFalse(result["safe"])
        self.assertEqual([], result["segments"])
        self.assertEqual([2], result["disconnectedBuildingIds"])
        self.assertEqual(
            1,
            result["rejectedEdges"][0]["rejectedCounts"][
                "buildingFootprintClearance"
            ],
        )
        self.assertEqual(2, mocked.call_count)

    def test_minimal_power_grid_treats_overlapping_blue_areas_as_zero_length(self):
        def area(building_id, x, radius, powered):
            return {
                "ok": True,
                "building": {
                    "id": building_id,
                    "position": {"x": x, "z": 0},
                    "electricityGridRadius": radius,
                    "powered": powered,
                },
                "targetConnectionSamples": [
                    {
                        "angleDegrees": 0,
                        "position": {"x": x + radius * 0.95, "z": 0},
                    },
                    {
                        "angleDegrees": 180,
                        "position": {"x": x - radius * 0.95, "z": 0},
                    },
                ],
            }

        areas = {
            10: area(10, 0, 30, True),
            20: area(20, 40, 30, False),
            30: area(30, 100, 20, False),
        }

        def request(method, path, query=None, body=None):
            if path == "/state/electricity-connection-area":
                return areas[int(query["buildingId"])]
            if path == "/commands/preview-network":
                return {"ok": True, "valid": True, "toolErrors": "None"}
            raise AssertionError(path)

        with mock.patch.object(MAYOR_TOOLS, "_request", side_effect=request):
            result = MAYOR_TOOLS._propose_minimal_power_grid(
                {
                    "sourceBuildingId": 10,
                    "buildingIds": [20, 30],
                    "_buildingObstacles": [],
                }
            )

        self.assertTrue(result["safe"])
        self.assertEqual(1, len(result["zeroLengthConnections"]))
        self.assertEqual(1, len(result["segments"]))
        self.assertEqual(20, result["segments"][0]["fromBuildingId"])
        self.assertEqual(30, result["segments"][0]["toBuildingId"])

    def test_power_preview_pairs_sample_beyond_clustered_shortest_candidates(self):
        pairs = [
            (float(index), {"position": {"x": index, "z": 0}}, {"position": {"x": 0, "z": index}})
            for index in range(1000)
        ]
        selected = MAYOR_TOOLS._diverse_preview_pairs(pairs, 100)
        selected_distances = [item[0] for item in selected]
        self.assertEqual(100, len(selected))
        self.assertEqual(list(range(25)), selected_distances[:25])
        self.assertGreater(selected_distances[-2], 900)
        self.assertEqual(999, selected_distances[-1])

    def test_power_endpoint_pairs_keep_pylons_outside_inner_building_ring(self):
        def area(x):
            return {
                "targetConnectionSamples": [
                    {"radialFraction": 0.25, "position": {"x": x, "z": 0}},
                    {"radialFraction": 0.75, "position": {"x": x, "z": 10}},
                    {"radialFraction": 0.95, "position": {"x": x, "z": 20}},
                ]
            }

        pairs = MAYOR_TOOLS._electricity_region_endpoint_pairs(area(0), area(100), 200)
        self.assertTrue(pairs)
        self.assertTrue(
            all(
                first["radialFraction"] >= 0.75
                and second["radialFraction"] >= 0.75
                for _, first, second in pairs
            )
        )

    def test_power_endpoint_pairs_prefer_native_footprint_clearance_flags(self):
        first = {
            "targetConnectionSamples": [
                {"radialFraction": 0.5, "outsideBuildingFootprint": True, "position": {"x": 20, "z": 0}},
                {"radialFraction": 0.95, "outsideBuildingFootprint": False, "position": {"x": 40, "z": 0}},
            ]
        }
        second = {
            "targetConnectionSamples": [
                {"radialFraction": 0.75, "outsideBuildingFootprint": True, "position": {"x": 100, "z": 0}}
            ]
        }
        pairs = MAYOR_TOOLS._electricity_region_endpoint_pairs(first, second, 200)
        self.assertEqual(1, len(pairs))
        self.assertEqual(0.5, pairs[0][1]["radialFraction"])

    def test_power_endpoint_pairs_reject_nonconductive_blue_area_holes(self):
        first = {
            "targetConnectionSamples": [
                {"conductive": True, "outsideBuildingFootprint": True, "position": {"x": 0, "z": 0}}
            ]
        }
        second = {
            "targetConnectionSamples": [
                {"conductive": False, "outsideBuildingFootprint": True, "position": {"x": 20, "z": 0}},
                {"conductive": True, "outsideBuildingFootprint": True, "position": {"x": 30, "z": 0}},
            ]
        }
        pairs = MAYOR_TOOLS._electricity_region_endpoint_pairs(first, second, 200)
        self.assertEqual(1, len(pairs))
        self.assertEqual(30, pairs[0][2]["position"]["x"])

    def test_power_endpoint_pairs_block_when_entire_blue_area_is_nonconductive(self):
        first = {
            "targetConnectionSamples": [
                {"conductive": True, "outsideBuildingFootprint": True, "position": {"x": 0, "z": 0}}
            ]
        }
        second = {
            "targetConnectionSamples": [
                {"conductive": False, "outsideBuildingFootprint": True, "position": {"x": 20, "z": 0}},
                {"conductive": False, "outsideBuildingFootprint": True, "position": {"x": 30, "z": 0}},
            ]
        }
        pairs = MAYOR_TOOLS._electricity_region_endpoint_pairs(first, second, 200)
        self.assertEqual([], pairs)

    def test_service_budget_sends_schema_default_subservice(self):
        with mock.patch.object(MAYOR_TOOLS, "_require_normal_progression"), mock.patch.object(
            MAYOR_TOOLS, "_require_fiscal_cooldown_clear", return_value={}
        ), mock.patch.object(MAYOR_TOOLS, "_require_checkpoint", side_effect=lambda args: args), mock.patch.object(
            MAYOR_TOOLS, "_request", return_value={"ok": False}
        ) as request:
            MAYOR_TOOLS.call_tool("set_service_budget", {
                "checkpointName": "c", "service": "Water", "budget": 50,
                "reason": "sustained-deficit", "expectedOutcome": "cost falls"})
        self.assertEqual("None", request.call_args.kwargs["body"]["subService"])

    def test_service_budget_day_and_night_is_one_fiscal_action(self):
        with mock.patch.object(MAYOR_TOOLS, "_require_normal_progression"), mock.patch.object(
            MAYOR_TOOLS, "_require_fiscal_cooldown_clear", return_value={}
        ), mock.patch.object(MAYOR_TOOLS, "_require_checkpoint", side_effect=lambda args: args), mock.patch.object(
            MAYOR_TOOLS, "_request", return_value={"ok": True}
        ) as request, mock.patch.object(MAYOR_TOOLS, "_record_fiscal_adjustment") as record:
            result = MAYOR_TOOLS.call_tool("set_service_budget", {
                "checkpointName": "c", "service": "Water", "budget": 50, "dayAndNight": True,
                "reason": "sustained-deficit", "expectedOutcome": "cost falls"})
        self.assertTrue(result["ok"])
        self.assertEqual([False, True], [call.kwargs["body"]["night"] for call in request.call_args_list])
        self.assertEqual(1, record.call_count)

    def _upgrade_road(self, support_responses, repair_supported=True):
        responses = {
            "/commands/upgrade-road": [{"ok": True, "newSegmentId": 77}],
            "/state/network-segment-details": [{"start": {"x": 0, "z": 0}, "end": {"x": 100, "z": 0}}],
            "/state/zoning-support": list(support_responses),
            "/commands/repair-building-zoning": [{"buildingId": 5, "changedCells": 1, "supportedAfter": repair_supported}],
        }
        calls = []

        def request(method, path, **kwargs):
            calls.append((path, kwargs))
            return responses[path].pop(0)

        with mock.patch.object(MAYOR_TOOLS, "_require_normal_progression"), mock.patch.object(
            MAYOR_TOOLS, "_require_checkpoint", side_effect=lambda args: args
        ), mock.patch.object(MAYOR_TOOLS, "_request", side_effect=request):
            result = MAYOR_TOOLS.call_tool("upgrade_road", {
                "checkpointName": "c", "segmentId": 3, "roadPrefab": "Small 4 Lane Road"})
        return result, calls

    def test_upgrade_road_restores_zoning_under_buildings_it_disturbed(self):
        failing = {"safe": False, "checkedGrowables": 9,
                   "failing": [{"id": 5, "repairableCells": 1}, {"id": 6, "repairableCells": 0}]}
        fixed = {"safe": True, "checkedGrowables": 9, "failing": []}
        result, calls = self._upgrade_road([failing, fixed])
        self.assertTrue(result["ok"])
        repairs = [kwargs["body"] for path, kwargs in calls if path == "/commands/repair-building-zoning"]
        self.assertEqual([{"buildingId": 5, "confirm": True}], repairs)  # nothing repairable on 6
        support_query = next(kwargs["query"] for path, kwargs in calls if path == "/state/zoning-support")
        self.assertEqual({"x": 50.0, "z": 0.0, "radius": 146.0, "limit": 64}, support_query)
        self.assertEqual([5, 6], result["zoningSupport"]["failingBeforeRepair"])
        self.assertEqual([], result["zoningSupport"]["failingAfterRepair"])

    def test_upgrade_road_fails_while_a_growable_stays_unsupported(self):
        failing = {"safe": False, "checkedGrowables": 9, "failing": [{"id": 5, "repairableCells": 1}]}
        result, _ = self._upgrade_road([failing, failing], repair_supported=False)
        self.assertFalse(result["ok"])
        self.assertIn("Restore the checkpoint", result["error"])
        self.assertEqual([5], result["zoningSupport"]["failingAfterRepair"])

    def test_upgrade_road_without_disturbed_zoning_does_not_repair(self):
        result, calls = self._upgrade_road([{"safe": True, "checkedGrowables": 4, "failing": []}])
        self.assertTrue(result["ok"])
        self.assertNotIn("/commands/repair-building-zoning", [path for path, _ in calls])

    def _purchase_map_area(self, cash, can_unlock=True, unlocks=True):
        areas = lambda owned, count: {"areaCount": count, "maxAreaCount": 9, "areas": [
            {"index": 17, "canUnlock": can_unlock and not owned, "unlocked": owned, "price": 350000}]}
        responses = {"/state/map-areas": [areas(False, 1), areas(unlocks, 2 if unlocks else 1)],
                     "/state/city-health": [{"cash": {"amount": cash}}],
                     "/commands/purchase-map-area": [{"ok": unlocks}]}
        calls = []

        def request(method, path, **kwargs):
            calls.append(path)
            return responses[path].pop(0)

        with mock.patch.object(MAYOR_TOOLS, "_require_normal_progression"), mock.patch.object(
            MAYOR_TOOLS, "_require_checkpoint", side_effect=lambda args: args
        ), mock.patch.object(MAYOR_TOOLS, "_request", side_effect=request):
            result = MAYOR_TOOLS.call_tool("purchase_map_area", {"checkpointName": "c", "tileIndex": 17})
        return result, calls

    def test_purchase_map_area_buys_one_offered_tile_and_verifies_it(self):
        result, calls = self._purchase_map_area(cash=4_000_000)
        self.assertTrue(result["ok"])
        self.assertEqual(3500.0, result["priceDisplay"])
        self.assertIn("/commands/purchase-map-area", calls)
        failed, _ = self._purchase_map_area(cash=4_000_000, unlocks=False)
        self.assertFalse(failed["ok"])

    def test_purchase_map_area_refuses_unoffered_tiles_and_reserve_breaches(self):
        with self.assertRaisesRegex(RuntimeError, "not purchasable"):
            self._purchase_map_area(cash=4_000_000, can_unlock=False)
        with self.assertRaisesRegex(RuntimeError, "below the 10000 reserve"):
            self._purchase_map_area(cash=1_200_000)

    def test_service_branch_bends_a_collinear_first_joint(self):
        segments = [
            {"start": {"x": 0, "z": 0}, "end": {"x": 100, "z": 0}, "targetBuildingId": 0, "name": "b"},
            {"start": {"x": 100, "z": 0}, "end": {"x": 200, "z": 0}, "targetBuildingId": 7, "name": "b"},
        ]
        bent = MAYOR_TOOLS._bend_first_service_joint(segments, 6.0, 160)
        self.assertEqual(bent[0]["end"], bent[1]["start"])
        self.assertEqual({"x": 0, "z": 0}, bent[0]["start"])
        self.assertEqual({"x": 200, "z": 0}, bent[1]["end"])
        self.assertGreater(abs(bent[0]["end"]["z"]), 5)
        single = MAYOR_TOOLS._bend_first_service_joint(segments[:1], -6.0, 160)
        self.assertEqual(2, len(single))
        self.assertEqual(0, single[0]["targetBuildingId"])
        self.assertIsNone(MAYOR_TOOLS._bend_first_service_joint(segments, 6.0, 99))
        targeted = [{**segments[0], "targetBuildingId": 5}, segments[1]]
        dogleg = MAYOR_TOOLS._bend_first_service_joint(targeted, 6.0, 160)
        self.assertEqual(3, len(dogleg))
        self.assertEqual({"x": 100, "z": 0}, dogleg[1]["end"])  # the target position is kept
        self.assertEqual(5, dogleg[1]["targetBuildingId"])

    def test_water_plan_splits_long_branches_into_update_safe_segments(self):
        arguments = {
            "intake": {"position": {"x": 0, "z": 0}},
            "outlet": {"position": {"x": 1000, "z": 0}},
            "segments": [
                {"role": "intakeBranch", "start": {"x": 0, "z": 0}, "end": {"x": 480, "z": 0}, "name": "intake"},
                {"role": "outletBranch", "start": {"x": 1000, "z": 0}, "end": {"x": 480, "z": 0}, "name": "outlet"},
                {"role": "sharedTrunk", "start": {"x": 480, "z": 0}, "end": {"x": 480, "z": 320}, "name": "city"},
            ],
            "maxWaterPipeSegmentLength": 160,
        }
        normalized = MAYOR_TOOLS._normalize_water_pipe_segments(arguments)
        # Exact multiples need one extra piece because joints are offset.
        self.assertEqual(11, len(normalized["segments"]))
        for first, second in zip(normalized["segments"], normalized["segments"][1:]):
            if first["logicalSegmentIndex"] != second["logicalSegmentIndex"]:
                continue
            a = (first["end"]["x"] - first["start"]["x"], first["end"]["z"] - first["start"]["z"])
            b = (second["end"]["x"] - second["start"]["x"], second["end"]["z"] - second["start"]["z"])
            cosine = (a[0] * b[0] + a[1] * b[1]) / (math.hypot(*a) * math.hypot(*b))
            # A collinear joint would invoke native MoveMiddleNode on the next piece.
            self.assertLess(cosine, math.cos(math.radians(3)))
        self.assertEqual(1, sum(item["role"] == "intakeBranch" for item in normalized["segments"]))
        self.assertEqual(1, sum(item["role"] == "outletBranch" for item in normalized["segments"]))
        self.assertTrue(
            all(
                MAYOR_TOOLS._point_distance(item["start"], item["end"]) <= 160.01
                for item in normalized["segments"]
            )
        )

    def test_minimal_power_grid_returns_no_lines_when_targets_already_powered(self):
        areas = {
            building_id: {
                "ok": True,
                "building": {
                    "id": building_id,
                    "position": {"x": building_id * 10, "z": 0},
                    "electricityGridRadius": 20,
                    "powered": True,
                },
                "targetConnectionSamples": [],
            }
            for building_id in (1, 2, 3)
        }
        with mock.patch.object(
            MAYOR_TOOLS,
            "_request",
            side_effect=lambda method, path, query=None, body=None: areas[
                int(query["buildingId"])
            ],
        ) as request:
            result = MAYOR_TOOLS._propose_minimal_power_grid(
                {
                    "sourceBuildingId": 1,
                    "buildingIds": [2, 3],
                    "_buildingObstacles": [],
                }
            )
        self.assertTrue(result["safe"])
        self.assertFalse(result["actionRequired"])
        self.assertEqual([], result["segments"])
        self.assertEqual(3, request.call_count)

    def test_minimal_power_grid_does_not_duplicate_existing_powered_connection(self):
        def area(building_id, x, powered):
            return {
                "ok": True,
                "building": {
                    "id": building_id,
                    "position": {"x": x, "z": 0},
                    "electricityGridRadius": 20,
                    "powered": powered,
                },
                "targetConnectionSamples": [
                    {
                        "angleDegrees": 0,
                        "position": {"x": x + 19, "z": 0},
                    },
                    {
                        "angleDegrees": 180,
                        "position": {"x": x - 19, "z": 0},
                    },
                ],
            }

        areas = {
            1: area(1, 0, True),
            2: area(2, 80, True),
            3: area(3, 160, False),
        }

        def request(method, path, query=None, body=None):
            if path == "/state/electricity-connection-area":
                return areas[int(query["buildingId"])]
            if path == "/commands/preview-network":
                return {"ok": True, "valid": True, "toolErrors": "None"}
            raise AssertionError(path)

        with mock.patch.object(MAYOR_TOOLS, "_request", side_effect=request):
            result = MAYOR_TOOLS._propose_minimal_power_grid(
                {
                    "sourceBuildingId": 1,
                    "buildingIds": [2, 3],
                    "_buildingObstacles": [],
                }
            )

        self.assertTrue(result["safe"])
        self.assertEqual(
            [
                {
                    "fromBuildingId": 1,
                    "toBuildingId": 2,
                    "reason": "targetAlreadyPoweredByLiveGrid",
                }
            ],
            result["existingGridConnections"],
        )
        self.assertEqual(1, len(result["segments"]))
        self.assertEqual(
            {2, 3},
            {
                result["segments"][0]["fromBuildingId"],
                result["segments"][0]["toBuildingId"],
            },
        )

    def test_connection_area_returns_compact_powered_samples_and_dense_target_ring(self):
        target_ring = [
            {
                "position": {"x": 24, "z": 0},
                "distanceToBuilding": 24,
                "insideTargetGridRadius": True,
                "conductive": True,
                "powered": False,
            }
        ]
        payload = {
            "ok": True,
            "building": {
                "id": 42,
                "position": {"x": 0, "z": 0},
                "electricityGridRadius": 32,
                "powered": False,
            },
            "semantics": {
                "conductive": "ElectricityManager.CheckConductivity",
                "powered": "ElectricityManager.CheckElectricity",
            },
            "bounds": {"minX": -100, "minZ": -100, "maxX": 100, "maxZ": 100},
            "resolution": 8,
            "summary": {
                "targetAreaSamples": 0,
                "targetConnectionSamples": 128,
            },
            "targetConnectionSamples": target_ring,
            "cells": [
                {"position": {"x": 80, "z": 0}, "powered": True},
                {"position": {"x": 40, "z": 0}, "powered": True},
                {"position": {"x": -100, "z": 0}, "powered": False},
            ],
        }
        with mock.patch.object(MAYOR_TOOLS, "_request", return_value=payload):
            result = MAYOR_TOOLS._inspect_electricity_connection_area(
                {"buildingId": 42, "maxResults": 1}
            )
        self.assertNotIn("cells", result)
        self.assertEqual(target_ring, result["targetConnectionSamples"])
        self.assertEqual(
            40,
            result["nearestPoweredSamples"][0]["position"]["x"],
        )

    def test_power_grid_efficiency_ranks_longest_lines_for_checkpoint_experiments(self):
        networks = {
            "ok": True,
            "segments": [
                {
                    "id": 10,
                    "name": "short",
                    "prefab": "Power Line",
                    "startNodeId": 1,
                    "endNodeId": 2,
                    "start": {"x": 0, "z": 0},
                    "end": {"x": 30, "z": 40},
                },
                {
                    "id": 20,
                    "name": "long",
                    "prefab": "Power Line",
                    "startNodeId": 2,
                    "endNodeId": 3,
                    "start": {"x": 0, "z": 0},
                    "end": {"x": 300, "z": 400},
                },
            ],
        }
        coverage = {
            "ok": True,
            "summary": {
                "requiringElectricity": 2,
                "covered": 2,
                "missingElectricity": 0,
            },
        }
        impact = {
            "ok": True,
            "safe": False,
            "risks": ["networkCutSegment"],
        }
        with mock.patch.object(
            MAYOR_TOOLS,
            "_request",
            side_effect=[networks, coverage, impact],
        ):
            result = MAYOR_TOOLS._inspect_power_grid_efficiency(
                {"maxCandidates": 1}
            )
        self.assertTrue(result["planningOnly"])
        self.assertFalse(result["safeToRemoveWithoutExperiment"])
        self.assertEqual(550, result["baseline"]["totalPowerLineLength"])
        self.assertEqual(20, result["candidates"][0]["id"])
        self.assertEqual(
            "checkpointExperimentRequired",
            result["candidates"][0]["classification"],
        )

    def test_existing_unconnected_intake_can_complete_pair_transaction(self):
        existing_intake = {
            "id": 42,
            "prefab": "Water Intake",
            "position": {"x": 0, "z": 0},
            "angleDegrees": 0,
            "flags": "Created, Completed",
            "productionRate": 100,
            "waterSourceId": 0,
            "active": False,
        }
        intake_preview = {
            "valid": False,
            "gamePlacement": {"productionRate": 100},
            "waterPlacement": {
                "location": {"x": 0, "y": 100, "z": 0},
                "closestWater": {"found": True},
            },
        }
        outlet_preview = {
            "valid": True,
            "gamePlacement": {"productionRate": 100},
            "waterPlacement": {
                "location": {"x": 300, "y": 100, "z": 0},
                "closestWater": {"found": True},
            },
        }
        analysis = {
            "cells": [
                {
                    "position": {"x": 0, "z": 0},
                    "water": {"present": True, "velocity": {"x": 1, "z": 0}},
                },
                {
                    "position": {"x": 300, "z": 0},
                    "water": {"present": True, "velocity": {"x": 1, "z": 0}},
                },
            ]
        }
        coverage = {
            "samples": [
                {"position": {"x": 0, "z": 0}, "waterPollution": 0},
                {"position": {"x": 300, "z": 0}, "waterPollution": 0},
            ]
        }
        with mock.patch.object(
            MAYOR_TOOLS,
            "_request",
            side_effect=[
                {"facilities": [existing_intake]},
                intake_preview,
                outlet_preview,
                analysis,
                coverage,
            ],
        ):
            result = MAYOR_TOOLS._inspect_water_pair(
                {
                    "intake": {
                        "buildingPrefab": "Water Intake",
                        "buildingId": 42,
                        "position": {"x": 0, "z": 0},
                        "angleDegrees": 0,
                    },
                    "outlet": {
                        "buildingPrefab": "Water Outlet",
                        "position": {"x": 300, "z": 0},
                        "angleDegrees": 0,
                    },
                    "checkUnlockedReach": False,
                }
            )
        self.assertTrue(result["safe"])
        self.assertTrue(result["existingFacilities"]["intake"]["verified"])
        self.assertNotIn("intakePlacementInvalid", result["risks"])

    def test_water_pipe_plan_requires_separate_branches_and_shared_trunk(self):
        result = MAYOR_TOOLS._inspect_water_pipe_topology(
            {
                "intake": {
                    "buildingPrefab": "Water Pumping Station",
                    "position": {"x": 0, "z": 0},
                    "angleDegrees": 0,
                },
                "outlet": {
                    "buildingPrefab": "Water Drain Pipe",
                    "position": {"x": 300, "z": 0},
                    "angleDegrees": 0,
                },
                "pipePrefab": "Water Pipe",
                "_facilityPipeConnections": {
                    "intake": [{"x": 20, "z": 0}],
                    "outlet": [{"x": 280, "z": 0}],
                },
                "segments": [
                    {
                        "role": "intakeBranch",
                        "start": {"x": 20, "z": 0},
                        "end": {"x": 150, "z": 0},
                    },
                    {
                        "role": "outletBranch",
                        "start": {"x": 280, "z": 0},
                        "end": {"x": 150, "z": 0},
                    },
                    {
                        "role": "sharedTrunk",
                        "start": {"x": 150, "z": 0},
                        "end": {"x": 150, "z": 100},
                    },
                ],
                "serviceAreaAnchor": {"x": 150, "z": 100},
            }
        )
        self.assertTrue(result["safe"])
        self.assertEqual(3, result["maximumNodeDegree"])
        self.assertTrue(result["usedExactFacilityPipeConnections"]["intake"])

    def test_water_pipe_plan_rejects_direct_facility_connection(self):
        result = MAYOR_TOOLS._inspect_water_pipe_topology(
            {
                "intake": {
                    "buildingPrefab": "Water Pumping Station",
                    "position": {"x": 0, "z": 0},
                    "angleDegrees": 0,
                },
                "outlet": {
                    "buildingPrefab": "Water Drain Pipe",
                    "position": {"x": 300, "z": 0},
                    "angleDegrees": 0,
                },
                "pipePrefab": "Water Pipe",
                "segments": [
                    {
                        "role": "intakeBranch",
                        "start": {"x": 0, "z": 0},
                        "end": {"x": 150, "z": 0},
                    },
                    {
                        "role": "outletBranch",
                        "start": {"x": 300, "z": 0},
                        "end": {"x": 150, "z": 0},
                    },
                    {
                        "role": "sharedTrunk",
                        "start": {"x": 0, "z": 0},
                        "end": {"x": 300, "z": 0},
                    },
                ],
                "serviceAreaAnchor": {"x": 150, "z": 0},
            }
        )
        self.assertFalse(result["safe"])
        self.assertIn("directFacilityToFacilityPipePlanned", result["risks"])

    def test_water_plan_reuses_verified_preceding_segments(self):
        arguments = {
            "intake": {"buildingPrefab": "Water Intake", "position": {"x": 0, "z": 0}, "angleDegrees": 0},
            "outlet": {"buildingPrefab": "Water Outlet", "position": {"x": 300, "z": 0}, "angleDegrees": 0},
            "pipePrefab": "Water Pipe",
            "segments": [
                {"role": "intakeBranch", "start": {"x": 20, "z": 0}, "end": {"x": 150, "z": 0}},
                {"role": "outletBranch", "start": {"x": 280, "z": 0}, "end": {"x": 150, "z": 0}},
                {"role": "sharedTrunk", "start": {"x": 150, "z": 0}, "end": {"x": 150, "z": 100}},
            ],
            "serviceAreaAnchor": {"x": 150, "z": 100},
        }
        existing = {
            "segments": [
                {
                    "id": 55,
                    "service": "Water",
                    "prefab": "Water Pipe",
                    "start": {"x": 20, "z": 0},
                    "end": {"x": 150, "z": 0},
                }
            ]
        }
        native_preview = {
            "ok": True,
            "valid": True,
            "toolErrors": "None",
            "constructionCost": 100,
            "length": 130,
        }
        with mock.patch.object(
            MAYOR_TOOLS,
            "_inspect_water_pair",
            return_value={"safe": True, "risks": [], "previews": {}},
        ), mock.patch.object(
            MAYOR_TOOLS,
            "_inspect_water_pipe_topology",
            return_value={"safe": True, "risks": []},
        ), mock.patch.object(
            MAYOR_TOOLS,
            "_is_water_network_prefab",
            return_value=True,
        ), mock.patch.object(
            MAYOR_TOOLS,
            "_request",
            side_effect=[existing, native_preview, native_preview],
        ):
            result = MAYOR_TOOLS._inspect_water_system_plan(
                arguments,
                completed_segment_indexes={0},
            )

        self.assertTrue(result["safe"])
        self.assertTrue(result["segmentPreviews"][0]["alreadyBuilt"])
        self.assertEqual(55, result["segmentPreviews"][0]["segmentId"])
        self.assertEqual(200, result["totalConstructionCost"])

    def test_existing_water_topology_requires_serviced_area_branch(self):
        facilities = {
            "facilities": [
                {"id": 10, "netNodeId": 1},
                {"id": 20, "netNodeId": 2},
            ]
        }
        path_only = {
            "segments": [
                {"id": 101, "startNodeId": 1, "endNodeId": 3},
                {"id": 102, "startNodeId": 3, "endNodeId": 2},
                {"id": 103, "startNodeId": 2, "endNodeId": 4},
            ]
        }
        with mock.patch.object(
            MAYOR_TOOLS,
            "_request",
            side_effect=[facilities, path_only],
        ):
            result = MAYOR_TOOLS._inspect_water_network_topology(
                {"intakeBuildingId": 10, "outletBuildingId": 20}
            )
        self.assertFalse(result["safe"])
        self.assertIn("waterComponentLacksServicedAreaBranch", result["risks"])

        shared_trunk = {
            "segments": [
                {"id": 101, "startNodeId": 1, "endNodeId": 3},
                {"id": 102, "startNodeId": 3, "endNodeId": 2},
                {"id": 103, "startNodeId": 3, "endNodeId": 4},
            ]
        }
        with mock.patch.object(
            MAYOR_TOOLS,
            "_request",
            side_effect=[facilities, shared_trunk],
        ):
            result = MAYOR_TOOLS._inspect_water_network_topology(
                {"intakeBuildingId": 10, "outletBuildingId": 20}
            )
        self.assertTrue(result["safe"])
        self.assertEqual(3, result["component"]["servicedAreaBranches"][0]["nodeId"])

    def test_existing_water_topology_fails_closed_when_network_is_truncated(self):
        facilities = {
            "total": 2,
            "returned": 2,
            "facilities": [
                {"id": 10, "netNodeId": 1},
                {"id": 20, "netNodeId": 2},
            ],
        }
        networks = {"total": 5001, "returned": 5000, "segments": []}
        with mock.patch.object(
            MAYOR_TOOLS, "_request", side_effect=[facilities, networks]
        ):
            result = MAYOR_TOOLS._inspect_water_network_topology(
                {"intakeBuildingId": 10, "outletBuildingId": 20}
            )
        self.assertFalse(result["safe"])
        self.assertEqual(["waterNetworkInventoryTruncated"], result["risks"])

    def test_water_service_branch_generation_is_translation_rotation_invariant(self):
        targets = {
            10: {"position": {"x": 120, "z": 0}},
            20: {"position": {"x": 120, "z": 40}},
        }
        segments = MAYOR_TOOLS._generate_water_service_branch_segments(
            {"x": 0, "z": 0}, targets, 80
        )

        def transform(point):
            return {"x": 1000 - point["z"], "z": -500 + point["x"]}

        transformed_targets = {
            building_id: {"position": transform(target["position"])}
            for building_id, target in targets.items()
        }
        transformed = MAYOR_TOOLS._generate_water_service_branch_segments(
            transform({"x": 0, "z": 0}), transformed_targets, 80
        )
        self.assertEqual(
            [round(MAYOR_TOOLS._point_distance(item["start"], item["end"]), 6) for item in segments],
            [round(MAYOR_TOOLS._point_distance(item["start"], item["end"]), 6) for item in transformed],
        )
        self.assertEqual(
            [item["targetBuildingId"] for item in segments],
            [item["targetBuildingId"] for item in transformed],
        )

    def test_water_service_proposer_uses_verified_component_node_and_native_preview(self):
        topology = {
            "ok": True,
            "safe": True,
            "risks": [],
            "intakeNetNodeId": 1,
            "outletNetNodeId": 2,
        }
        coverage = {
            "ok": True,
            "summary": {"requiringWater": 2},
            "returned": 2,
            "buildings": [
                {
                    "id": 10,
                    "position": {"x": 100, "z": 0},
                    "hasWater": False,
                    "hasSewage": False,
                },
                {
                    "id": 20,
                    "position": {"x": 120, "z": 20},
                    "hasWater": False,
                    "hasSewage": False,
                },
            ],
        }
        networks = {
            "ok": True,
            "total": 2,
            "returned": 2,
            "segments": [
                {
                    "id": 101,
                    "service": "Water",
                    "prefab": "Water Pipe",
                    "startNodeId": 1,
                    "endNodeId": 3,
                    "start": {"x": 0, "z": 0},
                    "end": {"x": 0, "z": 50},
                },
                {
                    "id": 102,
                    "service": "Water",
                    "prefab": "Water Pipe",
                    "startNodeId": 3,
                    "endNodeId": 2,
                    "start": {"x": 0, "z": 50},
                    "end": {"x": -50, "z": 50},
                },
            ],
        }
        preview = {
            "ok": True,
            "valid": True,
            "toolErrors": "None",
            "constructionCost": 100,
        }
        with mock.patch.object(
            MAYOR_TOOLS, "_inspect_water_network_topology", return_value=topology
        ), mock.patch.object(
            MAYOR_TOOLS, "_is_water_network_prefab", return_value=True
        ), mock.patch.object(
            MAYOR_TOOLS, "_request", side_effect=[coverage, networks, preview, preview]
        ) as request:
            result = MAYOR_TOOLS._propose_water_service_connection(
                {
                    "intakeBuildingId": 1000,
                    "outletBuildingId": 2000,
                    "targetBuildingIds": [10, 20],
                    "pipePrefab": "Water Pipe",
                    "maxWaterPipeSegmentLength": 160,
                }
            )

        self.assertTrue(result["safe"])
        self.assertTrue(result["actionRequired"])
        self.assertEqual(1, result["sourceNode"]["id"])
        self.assertEqual({10, 20}, {item["targetBuildingId"] for item in result["segments"] if item["targetBuildingId"]})
        self.assertEqual(4, request.call_count)

    def test_water_service_plan_rejects_changed_plan_identity(self):
        arguments = {
            "planId": "water-service-stale",
            "intakeBuildingId": 1000,
            "outletBuildingId": 2000,
            "targetBuildingIds": [10],
            "sourceNodeId": 1,
            "pipePrefab": "Water Pipe",
            "segments": [
                {
                    "start": {"x": 0, "z": 0},
                    "end": {"x": 100, "z": 0},
                    "targetBuildingId": 10,
                    "name": "Water Service Branch",
                }
            ],
        }
        topology = {
            "ok": True,
            "safe": True,
            "risks": [],
            "intakeNetNodeId": 1,
            "outletNetNodeId": 2,
        }
        coverage = {
            "summary": {"requiringWater": 1},
            "returned": 1,
            "buildings": [
                {
                    "id": 10,
                    "position": {"x": 100, "z": 0},
                    "hasWater": False,
                    "hasSewage": False,
                }
            ],
        }
        networks = {
            "total": 1,
            "returned": 1,
            "segments": [
                {
                    "id": 1,
                    "service": "Water",
                    "prefab": "Water Pipe",
                    "startNodeId": 1,
                    "endNodeId": 2,
                    "start": {"x": 0, "z": 0},
                    "end": {"x": -50, "z": 0},
                }
            ],
        }
        with mock.patch.object(
            MAYOR_TOOLS, "_inspect_water_network_topology", return_value=topology
        ), mock.patch.object(
            MAYOR_TOOLS, "_is_water_network_prefab", return_value=True
        ), mock.patch.object(
            MAYOR_TOOLS,
            "_request",
            side_effect=[
                coverage,
                networks,
                {"ok": True, "valid": True, "toolErrors": "None"},
            ],
        ):
            result = MAYOR_TOOLS._inspect_water_service_branch_plan(arguments)

        self.assertFalse(result["safe"])
        self.assertIn("waterServiceBranchPlanIdMismatch", result["risks"])

    def test_wind_power_preview_rejects_low_output_and_residential_noise(self):
        placement = {
            "ok": True,
            "valid": True,
            "electricityProfile": {
                "aiType": "WindTurbineAI",
                "renewable": True,
                "locationDependentProduction": True,
                "productionRatePercent": 40,
                "maximumElectricityProduction": 8,
                "estimatedElectricityProduction": 3,
                "estimatedOutputRatio": 0.375,
                "fuelBased": False,
                "resourceType": "None",
                "noiseAccumulation": 75,
                "noiseRadius": 75,
                "pollutionAccumulation": 0,
                "pollutionRadius": 0,
            },
            "environmental": {
                "residentialBuildingsInsideNoiseRadius": 2,
                "residentialBuildingsInsideGroundPollutionRadius": 0,
            },
            "roadAccess": {"accessible": False, "nearestSegmentId": 0},
        }
        with mock.patch.object(
            MAYOR_TOOLS,
            "_request",
            side_effect=[placement, {"ok": True, "used": 50, "capacity": 100}],
        ):
            result = MAYOR_TOOLS._preview_power_plant_candidate(
                {
                    "buildingPrefab": "Wind Turbine",
                    "position": {"x": 10, "z": 20},
                }
            )
        self.assertFalse(result["safe"])
        self.assertIn("locationDependentOutputBelowMinimum", result["risks"])
        self.assertIn("excessResidentialNoiseExposure", result["risks"])

    def test_wind_power_placement_drains_preview_guide_before_mutation(self):
        preview = {
            "ok": True,
            "safe": True,
            "risks": [],
            "classification": {"locationDependentProduction": True},
        }
        mutation = {"ok": True, "buildingId": 42}
        with mock.patch.object(
            MAYOR_TOOLS, "_require_normal_progression"
        ), mock.patch.object(
            MAYOR_TOOLS, "_preview_power_plant_candidate", return_value=preview
        ), mock.patch.object(
            MAYOR_TOOLS,
            "_require_checkpoint",
            side_effect=lambda arguments: arguments.pop("checkpointName"),
        ), mock.patch.object(
            MAYOR_TOOLS,
            "_request",
            side_effect=[{"ok": True}, mutation],
        ) as request:
            result = MAYOR_TOOLS.call_tool(
                "place_power_plant",
                {
                    "checkpointName": "AgentCheckpoint-wind",
                    "buildingPrefab": "Wind Turbine",
                    "position": {"x": 10, "z": 20},
                },
            )

        self.assertTrue(result["ok"])
        self.assertEqual(("GET", "/state/summary"), request.call_args_list[0].args[:2])
        self.assertTrue(request.call_args_list[1].kwargs["body"]["nativePlacementVerified"])

    def test_water_facility_placement_marks_native_preview_verified(self):
        pair = {"ok": True, "safe": True, "risks": []}
        mutation = {"ok": True, "buildingId": 42, "nativePlacementVerified": True}
        arguments = {
            "checkpointName": "AgentCheckpoint-water",
            "role": "intake",
            "intake": {
                "buildingPrefab": "Water Intake",
                "position": {"x": 10, "z": 20},
                "angleDegrees": 0,
            },
            "outlet": {
                "buildingPrefab": "Water Outlet",
                "position": {"x": 300, "z": 20},
                "angleDegrees": 180,
            },
        }
        with mock.patch.object(
            MAYOR_TOOLS, "_require_normal_progression"
        ), mock.patch.object(
            MAYOR_TOOLS, "_inspect_water_pair", return_value=pair
        ), mock.patch.object(
            MAYOR_TOOLS, "_require_checkpoint"
        ), mock.patch.object(
            MAYOR_TOOLS, "_request", return_value=mutation
        ) as request:
            result = MAYOR_TOOLS.call_tool("place_water_facility", arguments)

        self.assertTrue(result["ok"])
        self.assertTrue(request.call_args.kwargs["body"]["nativePlacementVerified"])

    def test_water_facility_placement_identifies_the_placed_partner(self):
        arguments = {
            "checkpointName": "AgentCheckpoint-water",
            "role": "outlet",
            "intake": {"buildingPrefab": "Water Intake", "position": {"x": 10, "z": 20}, "angleDegrees": 0},
            "outlet": {"buildingPrefab": "Water Outlet", "position": {"x": 300, "z": 20}, "angleDegrees": 180},
        }
        facilities = {"facilities": [
            {"id": 7, "prefab": "Water Intake", "position": {"x": 400, "z": 20}},
            {"id": 9, "prefab": "Water Intake", "position": {"x": 10.5, "z": 20.4}},
        ]}
        with mock.patch.object(
            MAYOR_TOOLS, "_require_normal_progression"
        ), mock.patch.object(
            MAYOR_TOOLS, "_inspect_water_pair", return_value={"ok": True, "safe": True, "risks": []}
        ) as inspect, mock.patch.object(
            MAYOR_TOOLS, "_require_checkpoint"
        ), mock.patch.object(
            MAYOR_TOOLS, "_request", side_effect=[facilities, {"ok": True, "buildingId": 42}]
        ):
            result = MAYOR_TOOLS.call_tool("place_water_facility", arguments)

        self.assertTrue(result["ok"])
        checked = inspect.call_args.args[0]
        self.assertEqual(9, checked["intake"]["buildingId"])
        self.assertNotIn("buildingId", checked["outlet"])

    def test_power_candidate_proposer_avoids_planned_residential_area(self):
        cells = [
            {
                "position": {"x": 0, "z": 0},
                "unlocked": True,
                "buildable": True,
                "slope": 0.01,
                "water": {"present": False},
            },
            {
                "position": {"x": 200, "z": 0},
                "unlocked": True,
                "buildable": True,
                "slope": 0.01,
                "water": {"present": False},
            },
        ]

        def safe_preview(arguments):
            return {
                "ok": True,
                "safe": True,
                "risks": [],
                "warnings": [],
                "production": {"estimated": 8},
                "environmental": {
                    "noiseRadius": 75,
                    "pollutionRadius": 0,
                },
                "fuel": {"resourceType": "None"},
                "placement": {
                    "terrain": {"heightRange": 1},
                    "roadAccess": {"accessible": False},
                },
            }

        with mock.patch.object(
            MAYOR_TOOLS,
            "_sample_unlocked_map",
            return_value=(cells, {"tileCount": 1, "resolutionPerTile": 32}),
        ), mock.patch.object(
            MAYOR_TOOLS,
            "_preview_power_plant_candidate",
            side_effect=safe_preview,
        ), mock.patch.object(
            MAYOR_TOOLS,
            "_residential_positions",
            return_value={},
        ), mock.patch.object(
            MAYOR_TOOLS,
            "_power_plant_requires_road_candidates",
            return_value=(False, "notRequiredByNativeBuildingAI"),
        ):
            result = MAYOR_TOOLS._propose_power_plant_candidates(
                {
                    "buildingPrefab": "Wind Turbine",
                    "serviceAreaAnchor": {"x": 0, "z": 0},
                    "searchRadius": 300,
                    "maxPreviewAttempts": 8,
                    "avoidAreas": [
                        {
                            "name": "future-homes",
                            "center": {"x": 0, "z": 0},
                            "radius": 50,
                        }
                    ],
                }
            )
        self.assertTrue(result["safe"])
        self.assertEqual(200, result["candidates"][0]["position"]["x"])
        self.assertIn(
            "plannedUseExposure:future-homes",
            result["rejectedCounts"],
        )

    def test_power_candidate_search_passes_spots_whose_reach_holds_a_home_unpreviewed(self):
        cells = [{"position": {"x": x, "z": 0}, "buildable": True, "slope": 0, "_angles": (0,)}
                 for x in (0, 20, 40, 60, 400)]
        previewed = []

        def preview(arguments):
            previewed.append(arguments["position"]["x"])
            near_home = arguments["position"]["x"] < 300
            return {"risks": ["excessResidentialNoiseExposure"] if near_home else [],
                    "production": {"estimated": 8}, "environmental": {"noiseRadius": 150, "pollutionRadius": 100},
                    "placement": {"terrain": {}, "roadAccess": {"accessible": True}}, "fuel": {}, "warnings": []}

        homes = {(int(30 // MAYOR_TOOLS.HOME_GRID), 0): [(30.0, 10.0)]}
        with mock.patch.object(MAYOR_TOOLS, "_sample_unlocked_map",
                               return_value=(cells, {"tileCount": 1, "resolutionPerTile": 32})), \
                mock.patch.object(MAYOR_TOOLS, "_preview_power_plant_candidate", side_effect=preview), \
                mock.patch.object(MAYOR_TOOLS, "_residential_positions", return_value=homes) as read, \
                mock.patch.object(MAYOR_TOOLS, "_power_plant_requires_road_candidates",
                                  return_value=(False, "notRequiredByNativeBuildingAI")):
            result = MAYOR_TOOLS._propose_power_plant_candidates({
                "buildingPrefab": "Coal Power Plant", "serviceAreaAnchor": {"x": 0, "z": 0},
                "searchRadius": 500, "maxPreviewAttempts": 2})
        self.assertTrue(result["safe"])
        self.assertEqual([0, 400], previewed)  # 20, 40 and 60 hold the home within 150m
        self.assertEqual(3, result["rejectedCounts"]["homesWithinReach"])
        self.assertEqual(1, read.call_count)

    def test_power_candidate_search_passes_spots_a_building_stands_on_unpreviewed(self):
        cells = [{"position": {"x": x, "z": 0}, "buildable": True, "slope": 0, "_angles": (0,)}
                 for x in (0, 30, 60, 200)]
        previewed = []

        def preview(arguments):
            previewed.append(arguments["position"]["x"])
            free = arguments["position"]["x"] > 100
            return {"risks": [] if free else ["nativePlacementUnsafe"], "production": {"estimated": 8},
                    "environmental": {"noiseRadius": 0, "pollutionRadius": 0},
                    "placement": {"footprint": {"width": 48, "length": 40}, "roadAccess": {"accessible": True}},
                    "fuel": {}, "warnings": []}

        standing = {(0, 0): [(45.0, 5.0)]}
        with mock.patch.object(MAYOR_TOOLS, "_sample_unlocked_map",
                               return_value=(cells, {"tileCount": 1, "resolutionPerTile": 32})), \
                mock.patch.object(MAYOR_TOOLS, "_preview_power_plant_candidate", side_effect=preview), \
                mock.patch.object(MAYOR_TOOLS, "_residential_positions", return_value=standing) as read, \
                mock.patch.object(MAYOR_TOOLS, "_power_plant_requires_road_candidates",
                                  return_value=(False, "notRequiredByNativeBuildingAI")):
            result = MAYOR_TOOLS._propose_power_plant_candidates({
                "buildingPrefab": "Advanced Wind Turbine", "serviceAreaAnchor": {"x": 0, "z": 0},
                "searchRadius": 500, "maxPreviewAttempts": 2})
        self.assertTrue(result["safe"])
        self.assertEqual([0, 200], previewed)  # the building at 45 lies within 31m of 30 and of 60
        self.assertEqual(2, result["rejectedCounts"]["buildingWithinLot"])
        self.assertEqual([mock.call(homes_only=False)], read.call_args_list)

    def test_homes_are_found_within_a_radius_across_index_cells(self):
        index = {}
        for x, z in ((100.0, 0.0), (-500.0, 40.0)):
            index.setdefault((int(x // MAYOR_TOOLS.HOME_GRID), int(z // MAYOR_TOOLS.HOME_GRID)), []).append((x, z))
        self.assertTrue(MAYOR_TOOLS._home_within(index, {"x": 0, "z": 0}, 150))
        self.assertFalse(MAYOR_TOOLS._home_within(index, {"x": 0, "z": 0}, 90))
        self.assertTrue(MAYOR_TOOLS._home_within(index, {"x": -380, "z": 0}, 130))

    def test_power_candidate_proposer_derives_roadside_positions_and_angles(self):
        roads = {
            "segments": [
                {
                    "id": 77,
                    "prefab": "Basic Road",
                    "start": {"x": 0, "z": 0},
                    "end": {"x": 100, "z": 100},
                    "isUnderground": False,
                }
            ]
        }
        with mock.patch.object(MAYOR_TOOLS, "_request", return_value=roads):
            candidates = MAYOR_TOOLS._roadside_power_candidate_cells(
                {"x": 50, "z": 50},
                200,
            )
        self.assertTrue(candidates)
        cell = candidates[0][1]
        self.assertEqual("roadside", cell["_source"])
        self.assertEqual(77, cell["_roadSegmentId"])
        self.assertNotIn(cell["_angles"][0], (0, 90, 180, 270))
        # the lot's front, local +Z at (-sin a, cos a), faces the road from the cell
        a = math.radians(cell["_angles"][0])
        front = (-math.sin(a), math.cos(a))
        x, z = cell["position"]["x"], cell["position"]["z"]  # the road runs from (0, 0) to (100, 100)
        toward = ((x + z) / 2 - x, (x + z) / 2 - z)
        self.assertGreater(front[0] * toward[0] + front[1] * toward[1], 0)

    def test_fuel_power_candidate_search_uses_native_road_requirement(self):
        preview = {
            "roadAccess": {"requiredByAI": True},
            "electricityProfile": {"fuelBased": True},
        }
        terrain = [(0, {"position": {"x": 10, "z": 20}})]
        with mock.patch.object(MAYOR_TOOLS, "_request", return_value=preview) as request:
            required, source = MAYOR_TOOLS._power_plant_requires_road_candidates(
                {"buildingPrefab": "Oil Power Plant"},
                terrain,
            )
        self.assertTrue(required)
        self.assertEqual("nativeBuildingAI", source)
        self.assertEqual("/commands/preview-placement", request.call_args.args[1])

    def test_fixed_power_preview_does_not_use_native_location_rate(self):
        source = (SCRIPT.parent.parent / "src" / "PlacementCommands.cs").read_text(encoding="utf-8")
        self.assertIn("powerPlantAI != null && !locationDependentElectricity", source)
        self.assertIn("? 100", source)
        self.assertIn("BuildElectricityProfile(info, effectiveProductionRate)", source)
        self.assertIn("effectiveProductionRate", source)
        mutation_source = (SCRIPT.parent.parent / "src" / "BuildingCommands.cs").read_text(encoding="utf-8")
        self.assertIn("powerPlant is WindTurbineAI && productionRate <= 0", mutation_source)
        self.assertIn("powerPlant.m_electricityProduction <= 0", mutation_source)
        road_source = (SCRIPT.parent.parent / "src" / "RoadCommands.cs").read_text(encoding="utf-8")
        self.assertIn("HasPowerPlantConnectionAreaNear", road_source)
        self.assertNotIn("HasActivePowerPlantNear", road_source)
        placement_source = (SCRIPT.parent.parent / "src" / "PlacementCommands.cs").read_text(encoding="utf-8")
        self.assertIn("RequiresRoadAccess(info)", placement_source)
        self.assertIn("GetClosestLanePosition", placement_source)
        self.assertIn("distanceFromSidewalkToVehicleLane", placement_source)
        self.assertIn("createdPlayerBuildingAI.CheckRoadAccess", mutation_source)
        self.assertIn("createdBuilding.m_accessSegment", mutation_source)
        self.assertIn("MaximumWaterPipeSegmentLength", road_source)
        self.assertIn("RefreshWaterNetworkState", road_source)
        service_source = (SCRIPT.parent.parent / "src" / "ServiceStateCommands.cs").read_text(encoding="utf-8")
        self.assertIn("IsZonedGrowableService", service_source)

    def test_road_access_preview_uses_native_search_window(self):
        # Native PlayerBuildingAI.FindRoadAccess only considers segments with an end node
        # near the access point; the middle of a long segment is never an access road.
        source = (SCRIPT.parent.parent / "src" / "PlacementCommands.cs").read_text(encoding="utf-8")
        self.assertIn("new Bounds(roadAccessPosition, new Vector3(40f, 40f, 40f))", source)
        self.assertIn("IsNativeAccessCandidate(manager, segment, searchBounds)", source)
        self.assertIn("segment.m_bounds.Intersects(bounds)", source)
        self.assertIn("bounds.min.x - 64f - node.x", source)

    def test_fuel_power_preview_requires_two_way_outside_freight(self):
        placement = {
            "ok": True,
            "valid": True,
            "electricityProfile": {
                "aiType": "PowerPlantAI",
                "renewable": False,
                "locationDependentProduction": False,
                "productionRatePercent": 100,
                "maximumElectricityProduction": 40,
                "estimatedElectricityProduction": 40,
                "estimatedOutputRatio": 1,
                "fuelBased": True,
                "resourceType": "Coal",
                "resourceCapacity": 32000,
                "resourceConsumption": 16,
                "noiseAccumulation": 50,
                "noiseRadius": 100,
                "pollutionAccumulation": 50,
                "pollutionRadius": 50,
            },
            "environmental": {
                "residentialBuildingsInsideNoiseRadius": 0,
                "residentialBuildingsInsideGroundPollutionRadius": 0,
            },
            "roadAccess": {"accessible": True, "nearestSegmentId": 77},
        }
        reachability = {
            "ok": True,
            "safeForTwoWayFreight": False,
            "inboundFromOutside": {"found": True, "distance": 1000},
            "outboundToOutside": {"found": False, "distance": 0},
            "limitations": {
                "turnRestrictionsAndDistrictHeavyTrafficPoliciesNotEvaluated": True
            },
        }
        with mock.patch.object(
            MAYOR_TOOLS,
            "_request",
            side_effect=[
                placement,
                reachability,
                {"ok": True, "used": 50, "capacity": 100},
            ],
        ):
            result = MAYOR_TOOLS._preview_power_plant_candidate(
                {
                    "buildingPrefab": "Coal Power Plant",
                    "position": {"x": 10, "z": 20},
                }
            )
        self.assertFalse(result["safe"])
        self.assertIn(
            "fuelDeliveryCannotReachAndReturnFromOutside",
            result["risks"],
        )

    def test_power_operation_reports_low_fuel_without_delivery(self):
        facility = {
            "id": 152,
            "prefab": "Coal Power Plant",
            "angleDegrees": 0,
            "position": {"x": 10, "y": 5, "z": 20},
            "customBuffer1": 1000,
            "productionRate": 100,
            "problems": "",
        }
        placement = {
            "ok": True,
            "valid": True,
            "electricityProfile": {
                "aiType": "PowerPlantAI",
                "renewable": False,
                "locationDependentProduction": False,
                "productionRatePercent": 100,
                "maximumElectricityProduction": 40,
                "estimatedElectricityProduction": 40,
                "estimatedOutputRatio": 1,
                "fuelBased": True,
                "resourceType": "Coal",
                "resourceCapacity": 32000,
                "resourceConsumption": 16,
                "noiseAccumulation": 50,
                "noiseRadius": 100,
                "pollutionAccumulation": 50,
                "pollutionRadius": 50,
            },
            "environmental": {
                "residentialBuildingsInsideNoiseRadius": 0,
                "residentialBuildingsInsideGroundPollutionRadius": 0,
            },
            "roadAccess": {"accessible": True, "nearestSegmentId": 77},
        }
        reachability = {
            "safeForTwoWayFreight": True,
            "inboundFromOutside": {
                "found": True,
                "distance": 1000,
                "maximumTrafficDensity": 50,
            },
            "outboundToOutside": {
                "found": True,
                "distance": 1000,
                "maximumTrafficDensity": 50,
            },
            "limitations": {},
        }
        with mock.patch.object(
            MAYOR_TOOLS,
            "_request",
            side_effect=[
                {"facilities": [facility]},
                placement,
                reachability,
                {"ok": True, "used": 50, "capacity": 100},
                {"ok": True, "inbound": 0, "outbound": 0, "withPath": 0},
                {"ok": True, "summary": {"missingElectricity": 0}},
            ],
        ):
            result = MAYOR_TOOLS._inspect_power_plant_operation(
                {"buildingId": 152}
            )
        self.assertFalse(result["safe"])
        self.assertIn("fuelBufferBelowMinimum", result["risks"])
        self.assertIn("lowFuelWithoutInboundDelivery", result["risks"])

    def test_generic_service_placement_rejects_specialized_utilities(self):
        arguments = {
            "checkpointName": "safe",
            "buildingPrefab": "Coal Power Plant",
            "position": {"x": 10, "z": 20},
        }
        with mock.patch.object(
            MAYOR_TOOLS,
            "_require_normal_progression",
        ), mock.patch.object(
            MAYOR_TOOLS,
            "_request",
            return_value={
                "ok": True,
                "valid": True,
                "waterPlacement": None,
                "electricityProfile": {"fuelBased": True},
            },
        ):
            with self.assertRaisesRegex(RuntimeError, "place_power_plant"):
                MAYOR_TOOLS.call_tool("place_service", dict(arguments))

        arguments["buildingPrefab"] = "Water Pumping Station"
        with mock.patch.object(
            MAYOR_TOOLS,
            "_require_normal_progression",
        ), mock.patch.object(
            MAYOR_TOOLS,
            "_request",
            return_value={
                "ok": True,
                "valid": True,
                "waterPlacement": {"waterIntake": 100},
                "electricityProfile": None,
            },
        ):
            with self.assertRaisesRegex(RuntimeError, "place_water_facility"):
                MAYOR_TOOLS.call_tool("place_service", dict(arguments))

    def test_road_result_preserves_xyz_and_directionality(self):
        segment = {
            "id": 9,
            "flags": "Created",
            "problems": "",
            "startNodeId": 1,
            "endNodeId": 2,
            "start": {"x": 0, "y": 80, "z": 0},
            "middle": {"x": 32, "y": 76, "z": 0},
            "end": {"x": 64, "y": 80, "z": 0},
            "isUnderground": True,
            "isTunnel": True,
            "directionality": "Bidirectional",
            "isBidirectional": True,
            "isOneWay": False,
            "actualLaneCount": 2,
            "prefabLaneCount": 2,
            "prefabLanes": [
                {"laneType": "Vehicle", "finalDirection": "Forward"},
                {"laneType": "Vehicle", "finalDirection": "Backward"},
            ],
            "isGroundRoad": False,
            "terrainProfile": self.inspection_terrain_profile(
                required=False,
                sampleCount=0,
            ),
        }
        node = {"connectCount": 1}
        with mock.patch.object(
            MAYOR_TOOLS,
            "_request",
            side_effect=[
                segment,
                node,
                node,
                {"anomalies": []},
                {"safeForTwoWayFreight": True},
                {"connections": []},
            ],
        ):
            result = MAYOR_TOOLS._inspect_road_result(9, True)
        self.assertTrue(result["ok"])
        self.assertEqual(76, result["geometry"]["middle"]["y"])
        self.assertTrue(result["geometry"]["isTunnel"])
        self.assertTrue(result["isBidirectional"])
        self.assertFalse(result["requiresAdditionalTelemetry"])

    def test_offline_spatial_model_distinguishes_vertical_crossing(self):
        def road(segment_id, prefab, start, middle, end, start_node, end_node):
            return {
                "id": segment_id,
                "prefab": prefab,
                "service": "Road",
                "startNodeId": start_node,
                "endNodeId": end_node,
                "start": start,
                "middle": middle,
                "end": end,
            }

        snapshot = {
            "networks": {
                "segments": [
                    road(
                        1,
                        "Basic Road",
                        {"x": -10, "y": 10, "z": 0},
                        {"x": 0, "y": 10, "z": 0},
                        {"x": 10, "y": 10, "z": 0},
                        1,
                        2,
                    ),
                    road(
                        2,
                        "Basic Road Tunnel",
                        {"x": 0, "y": 0, "z": -10},
                        {"x": 0, "y": 0, "z": 0},
                        {"x": 0, "y": 0, "z": 10},
                        3,
                        4,
                    ),
                ]
            },
            "roadTemplates": {
                "Basic Road": {
                    "prefabLanes": [
                        {"laneType": "Vehicle", "finalDirection": "Forward"},
                        {"laneType": "Vehicle", "finalDirection": "Backward"},
                    ]
                },
                "Basic Road Tunnel": {
                    "prefabLanes": [
                        {"laneType": "Vehicle", "finalDirection": "Forward"},
                        {"laneType": "Vehicle", "finalDirection": "Backward"},
                    ]
                },
            },
            "tileAnalyses": [
                {
                    "bounds": {"minX": -10, "minZ": -10, "maxX": 10, "maxZ": 10},
                    "map": {
                        "cells": [
                            {"row": 0, "column": 0, "height": 10},
                            {"row": 0, "column": 1, "height": 10},
                            {"row": 1, "column": 0, "height": 10},
                            {"row": 1, "column": 1, "height": 10},
                        ]
                    },
                }
            ],
        }
        roads = MAYOR_TOOLS.build_layered_roads(snapshot, sample_count=5)
        crossings = MAYOR_TOOLS.inspect_crossings(roads, grade_separation_threshold=4)
        self.assertEqual("Ground", roads[0]["structureType"])
        self.assertEqual("Tunnel", roads[1]["structureType"])
        self.assertEqual(-10, roads[1]["terrainRelativeBounds"]["min"])
        self.assertEqual(1, len(crossings))
        self.assertEqual("AGradeSeparatedAboveB", crossings[0]["relation"])
        self.assertEqual(10, crossings[0]["verticalClearance"])

    def test_offline_spatial_model_prefers_native_bezier_controls(self):
        snapshot = {
            "networks": {
                "segments": [
                    {
                        "id": 7,
                        "prefab": "Basic Road",
                        "service": "Road",
                        "startNodeId": 1,
                        "endNodeId": 2,
                        "start": {"x": 0, "y": 0, "z": 0},
                        "middle": {"x": 5, "y": 999, "z": 0},
                        "end": {"x": 10, "y": 0, "z": 0},
                        "curve": {
                            "source": "nativeGenerateBezier",
                            "a": {"x": 0, "y": 0, "z": 0},
                            "b": {"x": 0, "y": 6, "z": 0},
                            "c": {"x": 10, "y": 6, "z": 0},
                            "d": {"x": 10, "y": 0, "z": 0},
                        },
                    }
                ]
            },
            "roadTemplates": {},
            "tileAnalyses": [],
        }
        roads = MAYOR_TOOLS.build_layered_roads(snapshot, sample_count=3)
        self.assertEqual("nativeGenerateBezier", roads[0]["curveSource"])
        self.assertEqual(5, roads[0]["samples"][1]["x"])
        self.assertEqual(4.5, roads[0]["samples"][1]["y"])

    def test_offline_building_model_distinguishes_tunnel_below_building(self):
        snapshot = {
            "facilities": {
                "facilities": [
                    {
                        "id": 12,
                        "prefab": "House",
                        "displayName": "House",
                        "service": "Residential",
                        "subService": "ResidentialLow",
                        "width": 2,
                        "length": 2,
                        "angleDegrees": 0,
                        "position": {"x": 0, "y": 10, "z": 0},
                        "spatial3d": {
                            "source": "nativeBuildingInfo",
                            "widthMeters": 16,
                            "lengthMeters": 16,
                            "modelHeight": 8,
                            "collisionHeight": 8,
                            "verticalBounds": {"minY": 10, "maxY": 18},
                            "axisX": {"x": 1, "z": 0},
                            "axisZ": {"x": 0, "z": 1},
                            "footprintCorners": [
                                {"x": -8, "z": -8},
                                {"x": -8, "z": 8},
                                {"x": 8, "z": 8},
                                {"x": 8, "z": -8},
                            ],
                        },
                    }
                ]
            },
            "growables": {"growables": []},
            "networks": {
                "segments": [
                    {
                        "id": 7,
                        "prefab": "Basic Road Tunnel",
                        "service": "Road",
                        "startNodeId": 1,
                        "endNodeId": 2,
                        "start": {"x": -20, "y": 0, "z": 0},
                        "middle": {"x": 0, "y": 0, "z": 0},
                        "end": {"x": 20, "y": 0, "z": 0},
                    }
                ]
            },
            "roadTemplates": {},
            "tileAnalyses": [],
        }
        buildings = MAYOR_TOOLS.build_layered_buildings(snapshot)
        roads = MAYOR_TOOLS.build_layered_roads(snapshot, sample_count=5)
        results = MAYOR_TOOLS.inspect_building_road_clearance(buildings, roads, road_half_height=2)
        self.assertEqual(1, len(buildings))
        self.assertTrue(buildings[0]["heightKnown"])
        self.assertEqual("RoadBelowBuilding", results[0]["relation"])
        self.assertEqual(8, results[0]["verticalClearance"])

    def test_planning_area_ranks_exact_low_pollution_terrain_sample(self):
        snapshot = self._planning_snapshot()
        context = MAYOR_TOOLS.inspect_planning_area(
            snapshot,
            center={"x": 0, "z": 0},
            radius=100,
            intended_use="Residential",
            sample_count=5,
            candidate_limit=4,
        )
        self.assertTrue(context["planningOnly"])
        self.assertEqual(4, context["terrain"]["sampleCount"])
        self.assertEqual(1, context["roads"]["count"])
        self.assertEqual(1, context["utilities"]["networks"]["Water"]["count"])
        self.assertEqual(1, context["utilities"]["networks"]["Electricity"]["count"])
        self.assertEqual({"Residential": 1}, context["buildings"]["countsByService"])
        self.assertFalse(context["candidateSites"][0]["hardBlocked"])
        self.assertEqual(0, context["candidateSites"][0]["resources"]["pollution"])
        self.assertIn(
            "Candidate sites are exact captured terrain samples, not approved construction coordinates.",
            context["limitations"],
        )

    def test_development_plan_stays_non_executable_until_live_previews(self):
        context = MAYOR_TOOLS.inspect_planning_area(
            self._planning_snapshot(),
            center={"x": 0, "z": 0},
            radius=100,
            intended_use="Mixed",
            sample_count=5,
            candidate_limit=4,
        )
        plans = MAYOR_TOOLS.propose_development_plans(context, max_plans=2)
        self.assertEqual(2, len(plans))
        self.assertEqual("Residential", plans[0]["primaryUse"])
        self.assertEqual("ResidentialLow", plans[0]["targetZone"])
        self.assertFalse(plans[0]["executionReady"])
        self.assertEqual(
            "inspectExactEmptyRoadGeneratedBlocksBeforeZoning",
            plans[0]["components"]["zoning"]["strategy"],
        )
        self.assertGreater(
            plans[0]["components"]["zoning"]["demandWeightedMix"]["ResidentialLow"],
            plans[0]["components"]["zoning"]["demandWeightedMix"]["Industrial"],
        )
        self.assertEqual(8, plans[0]["components"]["utilities"]["nearestNetworks"]["Water"]["distance"])
        self.assertIn("preview_network for every proposed road or utility segment", plans[0]["requiredValidation"])

    def test_development_plan_does_not_reuse_local_road_without_exact_frontage(self):
        context = MAYOR_TOOLS.inspect_planning_area(
            self._planning_snapshot(),
            center={"x": 0, "z": 0},
            radius=100,
            intended_use="Residential",
            sample_count=5,
            candidate_limit=4,
        )
        context["actionableZoneBlocks"] = {
            "evidenceStatus": "liveExact",
            "totalMatching": 0,
            "returned": 0,
            "blocks": [],
        }
        plans = MAYOR_TOOLS.propose_development_plans(
            context, intended_use="Residential", max_plans=1
        )
        self.assertEqual(
            "previewLocalStreetForDevelopmentFrontage",
            plans[0]["components"]["roadAccess"]["strategy"],
        )
        self.assertEqual(
            "zoneOnlyAfterVerifiedLocalStreetCreatesExactBlocks",
            plans[0]["components"]["zoning"]["strategy"],
        )
        self.assertIsNone(plans[0]["components"]["roadAccess"]["verifiedBlockId"])

    def test_development_plan_reuses_only_matching_exact_frontage_block(self):
        context = MAYOR_TOOLS.inspect_planning_area(
            self._planning_snapshot(),
            center={"x": 0, "z": 0},
            radius=100,
            intended_use="Residential",
            sample_count=5,
            candidate_limit=4,
        )
        segment_id = context["candidateSites"][0]["nearestRoad"]["segmentId"]
        context["actionableZoneBlocks"] = {
            "evidenceStatus": "liveExact",
            "totalMatching": 1,
            "returned": 1,
            "blocks": [
                {
                    "blockId": 77,
                    "roadSegmentId": segment_id,
                    "developmentFrontageSafe": True,
                    "junctionClearanceSafe": True,
                    "center": context["candidateSites"][0]["position"],
                    "rows": 8,
                }
            ],
        }
        plans = MAYOR_TOOLS.propose_development_plans(
            context, intended_use="Residential", max_plans=1
        )
        self.assertEqual(
            "reuseExistingRoadFrontage",
            plans[0]["components"]["roadAccess"]["strategy"],
        )
        self.assertEqual(77, plans[0]["components"]["roadAccess"]["verifiedBlockId"])
        self.assertEqual(
            "revalidateReturnedExactBlockBeforeZoning",
            plans[0]["components"]["zoning"]["strategy"],
        )

    def test_development_plan_prioritizes_and_deduplicates_late_exact_frontage(self):
        context = MAYOR_TOOLS.inspect_planning_area(
            self._planning_snapshot(),
            center={"x": 0, "z": 0},
            radius=100,
            intended_use="Residential",
            sample_count=5,
            candidate_limit=4,
        )
        late_site = copy.deepcopy(context["candidateSites"][-1])
        duplicate_site = copy.deepcopy(late_site)
        duplicate_site["suitabilityScore"] = late_site["suitabilityScore"] - 1
        context["candidateSites"].append(duplicate_site)
        segment_id = late_site["nearestRoad"]["segmentId"]
        context["actionableZoneBlocks"] = {
            "evidenceStatus": "liveExact",
            "totalMatching": 1,
            "returned": 1,
            "blocks": [
                {
                    "blockId": 88,
                    "roadSegmentId": segment_id,
                    "developmentFrontageSafe": True,
                    "junctionClearanceSafe": True,
                    "center": late_site["position"],
                    "rows": 8,
                }
            ],
        }

        plans = MAYOR_TOOLS.propose_development_plans(
            context, intended_use="Residential", max_plans=2
        )

        self.assertEqual(2, len(plans))
        self.assertEqual(88, plans[0]["components"]["roadAccess"]["verifiedBlockId"])
        self.assertEqual(
            "reuseExistingRoadFrontage",
            plans[0]["components"]["roadAccess"]["strategy"],
        )
        self.assertEqual(
            1,
            sum(
                plan["components"]["roadAccess"]["verifiedBlockId"] == 88
                for plan in plans
            ),
        )

    def test_development_plan_does_not_associate_distant_block_on_same_road(self):
        context = MAYOR_TOOLS.inspect_planning_area(
            self._planning_snapshot(),
            center={"x": 0, "z": 0},
            radius=100,
            intended_use="Residential",
            sample_count=5,
            candidate_limit=4,
        )
        site = context["candidateSites"][0]
        segment_id = site["nearestRoad"]["segmentId"]
        context["actionableZoneBlocks"] = {
            "evidenceStatus": "liveExact",
            "totalMatching": 1,
            "returned": 1,
            "blocks": [
                {
                    "blockId": 78,
                    "roadSegmentId": segment_id,
                    "developmentFrontageSafe": True,
                    "junctionClearanceSafe": True,
                    "center": {
                        "x": site["position"]["x"] + 200,
                        "z": site["position"]["z"],
                    },
                    "rows": 8,
                }
            ],
        }
        plans = MAYOR_TOOLS.propose_development_plans(
            context, intended_use="Residential", max_plans=1
        )
        self.assertEqual(
            "previewLocalStreetForDevelopmentFrontage",
            plans[0]["components"]["roadAccess"]["strategy"],
        )
        self.assertIsNone(plans[0]["components"]["roadAccess"]["verifiedBlockId"])

    def test_industrial_planning_blocks_residential_pollution_proximity(self):
        context = MAYOR_TOOLS.inspect_planning_area(
            self._planning_snapshot(),
            center={"x": 0, "z": 0},
            radius=100,
            intended_use="Industrial",
            sample_count=5,
            candidate_limit=4,
        )
        near = [
            site
            for site in context["candidateSites"]
            if site["nearestResidentialBuildingClearance"] < 64
        ]
        self.assertTrue(near)
        self.assertTrue(all(site["hardBlocked"] for site in near))
        self.assertTrue(
            all("residentialPollutionSeparation" in site["blockers"] for site in near)
        )
        self.assertEqual(
            len(near),
            context["candidateSiteDiagnostics"]["rejectionCounts"][
                "residentialPollutionSeparation"
            ],
        )
        plans = MAYOR_TOOLS.propose_development_plans(
            context, intended_use="Industrial", max_plans=4
        )
        self.assertTrue(plans)
        self.assertTrue(
            all(plan["evidence"]["nearestResidentialBuildingClearance"] >= 64 for plan in plans)
        )

    def test_residential_planning_blocks_industrial_pollution_proximity(self):
        snapshot = self._planning_snapshot()
        snapshot["growables"]["growables"].append(
            {
                "id": 13,
                "prefab": "Factory",
                "displayName": "Factory",
                "service": "Industrial",
                "subService": "IndustrialGeneric",
                "active": True,
                "width": 2,
                "length": 2,
                "angleDegrees": 0,
                "position": {"x": 0, "y": 10, "z": 0},
            }
        )
        context = MAYOR_TOOLS.inspect_planning_area(
            snapshot,
            center={"x": 0, "z": 0},
            radius=100,
            intended_use="Residential",
            sample_count=5,
            candidate_limit=4,
        )
        near = [
            site
            for site in context["candidateSites"]
            if site["nearestIndustrialBuildingClearance"] < 64
        ]
        self.assertTrue(near)
        self.assertTrue(all(site["hardBlocked"] for site in near))
        self.assertTrue(
            all("industrialPollutionSeparation" in site["blockers"] for site in near)
        )

    def test_residential_exact_block_industrial_separation_is_transform_invariant(self):
        snapshot = self._planning_snapshot()
        snapshot["growables"]["growables"].append(
            {
                "id": 13,
                "prefab": "Factory",
                "displayName": "Factory",
                "service": "Industrial",
                "subService": "IndustrialGeneric",
                "active": True,
                "width": 2,
                "length": 2,
                "angleDegrees": 0,
                "position": {"x": 0, "y": 10, "z": 0},
            }
        )
        block = {
            "blockId": 80,
            "roadSegmentId": 7,
            "developmentFrontageSafe": True,
            "junctionClearanceSafe": True,
            "center": {"x": 100, "z": 0},
            "rows": 4,
        }
        baseline = MAYOR_TOOLS.annotate_actionable_zone_block_clearance(
            [block], snapshot
        )[0]

        transformed_snapshot = self._planning_snapshot()
        transformed_factory = dict(snapshot["growables"]["growables"][-1])
        transformed_factory["position"] = {"x": 1000, "y": 10, "z": -500}
        transformed_factory["angleDegrees"] = 90
        transformed_snapshot["growables"]["growables"].append(transformed_factory)
        transformed_block = {
            **block,
            "center": {"x": 1000, "z": -400},
        }
        transformed = MAYOR_TOOLS.annotate_actionable_zone_block_clearance(
            [transformed_block], transformed_snapshot
        )[0]

        self.assertFalse(baseline["planningEvidence"]["residentialReuseEligible"])
        self.assertEqual(
            baseline["planningEvidence"]["nearestIndustrialBuildingClearanceLowerBound"],
            transformed["planningEvidence"]["nearestIndustrialBuildingClearanceLowerBound"],
        )
        context = MAYOR_TOOLS.inspect_planning_area(
            self._planning_snapshot(),
            center={"x": 0, "z": 0},
            radius=100,
            intended_use="Residential",
            sample_count=5,
            candidate_limit=4,
        )
        site = context["candidateSites"][0]
        site["position"] = dict(block["center"])
        site["nearestRoad"]["segmentId"] = 7
        site["nearestIndustrialBuildingClearance"] = 100
        site["hardBlocked"] = False
        site["blockers"] = []
        context["actionableZoneBlocks"] = {
            "evidenceStatus": "liveExact",
            "totalMatching": 1,
            "returned": 1,
            "blocks": [baseline],
        }
        plans = MAYOR_TOOLS.propose_development_plans(
            context, intended_use="Residential", max_plans=1
        )
        exact = plans[0]["evidence"]["exactFrontage"]
        self.assertIsNone(exact["blockId"])
        self.assertEqual(80, exact["rejectedBlockId"])
        self.assertEqual("industrialPollutionSeparation", exact["rejectionReason"])

    def test_industrial_plan_rejects_exact_block_with_unsafe_blockwide_clearance(self):
        snapshot = self._planning_snapshot()
        context = MAYOR_TOOLS.inspect_planning_area(
            snapshot,
            center={"x": 0, "z": 0},
            radius=100,
            intended_use="Residential",
            sample_count=5,
            candidate_limit=4,
        )
        site = context["candidateSites"][0]
        segment_id = site["nearestRoad"]["segmentId"]
        blocks = MAYOR_TOOLS.annotate_actionable_zone_block_clearance(
            [
                {
                    "blockId": 79,
                    "roadSegmentId": segment_id,
                    "developmentFrontageSafe": True,
                    "junctionClearanceSafe": True,
                    "center": {
                        "x": site["position"]["x"],
                        "z": site["position"]["z"] + 40,
                    },
                    "rows": 8,
                }
            ],
            snapshot,
        )
        self.assertFalse(blocks[0]["planningEvidence"]["industrialReuseEligible"])
        context["actionableZoneBlocks"] = {
            "evidenceStatus": "liveExact",
            "totalMatching": 1,
            "returned": 1,
            "blocks": blocks,
        }
        plans = MAYOR_TOOLS.propose_development_plans(
            context, intended_use="Industrial", max_plans=1
        )
        self.assertEqual(
            "previewLocalStreetForDevelopmentFrontage",
            plans[0]["components"]["roadAccess"]["strategy"],
        )
        exact = plans[0]["evidence"]["exactFrontage"]
        self.assertIsNone(exact["blockId"])
        self.assertEqual(79, exact["rejectedBlockId"])
        self.assertEqual("residentialPollutionSeparation", exact["rejectionReason"])

    def test_industrial_residential_separation_is_translation_rotation_invariant(self):
        baseline_snapshot = self._planning_snapshot()
        transformed_snapshot = self._planning_snapshot()

        def transform(point):
            x = float(point["x"])
            z = float(point["z"])
            return {**point, "x": 1000 - z, "z": -500 + x}

        for collection_name, key in (("facilities", "facilities"), ("growables", "growables")):
            for building in transformed_snapshot[collection_name][key]:
                building["position"] = transform(building["position"])
                building["angleDegrees"] = float(building.get("angleDegrees", 0)) + 90
        for segment in transformed_snapshot["networks"]["segments"]:
            for point_name in ("start", "middle", "end"):
                segment[point_name] = transform(segment[point_name])
        for tile in transformed_snapshot["tileAnalyses"]:
            for cell in tile["map"]["cells"]:
                cell["position"] = transform(cell["position"])
            for sample in tile["utilities"]["samples"]:
                sample["position"] = transform(sample["position"])

        baseline = MAYOR_TOOLS.inspect_planning_area(
            baseline_snapshot,
            center={"x": 0, "z": 0},
            radius=100,
            intended_use="Industrial",
            sample_count=5,
            candidate_limit=4,
        )
        transformed = MAYOR_TOOLS.inspect_planning_area(
            transformed_snapshot,
            center={"x": 1000, "z": -500},
            radius=100,
            intended_use="Industrial",
            sample_count=5,
            candidate_limit=4,
        )
        baseline_signature = sorted(
            (
                site["nearestResidentialBuildingClearance"],
                "residentialPollutionSeparation" in site["blockers"],
            )
            for site in baseline["candidateSites"]
        )
        transformed_signature = sorted(
            (
                site["nearestResidentialBuildingClearance"],
                "residentialPollutionSeparation" in site["blockers"],
            )
            for site in transformed["candidateSites"]
        )
        self.assertEqual(baseline_signature, transformed_signature)

    def test_local_development_road_candidates_reuse_native_node_toward_anchor(self):
        snapshot = self._planning_snapshot()
        snapshot["tileAnalyses"][0]["bounds"] = {
            "minX": -300,
            "minZ": -300,
            "maxX": 300,
            "maxZ": 300,
        }
        plan = {
            "planId": "industrial-safe",
            "primaryUse": "Industrial",
            "planningAnchor": {"x": -50, "y": 10, "z": -50},
            "components": {
                "roadAccess": {
                    "strategy": "previewLocalStreetForDevelopmentFrontage",
                    "segmentId": 7,
                }
            },
        }
        result = MAYOR_TOOLS.generate_local_development_road_candidates(
            snapshot, [plan], road_prefab="Basic Road", limit=4
        )
        self.assertEqual(2, result["generated"])
        self.assertEqual(2, result["staticallyEligible"])
        first = result["candidates"][0]
        self.assertEqual(1, first["sourceNodeId"])
        self.assertEqual("industrial-safe:7:1:128m", first["candidateId"])
        self.assertEqual({"x": -100.0, "z": 0.0}, first["start"])
        self.assertEqual({"x": -9.49, "z": -90.51}, first["end"])
        self.assertEqual(
            "existingSourceRoadEndpointTowardPlanningAnchor", first["derivation"]
        )
        self.assertGreaterEqual(first["minimumIncidentAngleDegrees"], 30)

    def test_local_development_road_candidates_block_collinear_node_branch(self):
        snapshot = self._planning_snapshot()
        snapshot["tileAnalyses"][0]["bounds"] = {
            "minX": -300,
            "minZ": -300,
            "maxX": 300,
            "maxZ": 300,
        }
        plan = {
            "planId": "collinear",
            "primaryUse": "Residential",
            "planningAnchor": {"x": 50, "y": 10, "z": 0},
            "components": {
                "roadAccess": {
                    "strategy": "previewLocalStreetForDevelopmentFrontage",
                    "segmentId": 7,
                }
            },
        }
        result = MAYOR_TOOLS.generate_local_development_road_candidates(
            snapshot, [plan], limit=4
        )
        self.assertTrue(result["candidates"])
        self.assertTrue(
            all(
                "junctionBranchAngle" in candidate["staticBlockers"]
                for candidate in result["candidates"]
            )
        )

    def test_local_development_road_candidates_are_translation_rotation_invariant(self):
        baseline_snapshot = self._planning_snapshot()
        baseline_snapshot["tileAnalyses"][0]["bounds"] = {
            "minX": -300,
            "minZ": -300,
            "maxX": 300,
            "maxZ": 300,
        }
        transformed_snapshot = copy.deepcopy(baseline_snapshot)

        def transform(point):
            x = float(point["x"])
            z = float(point["z"])
            return {**point, "x": 1000 - z, "z": -500 + x}

        for collection_name, key in (("facilities", "facilities"), ("growables", "growables")):
            for building in transformed_snapshot[collection_name][key]:
                building["position"] = transform(building["position"])
                building["angleDegrees"] = float(building.get("angleDegrees", 0)) + 90
        for segment in transformed_snapshot["networks"]["segments"]:
            for point_name in ("start", "middle", "end"):
                segment[point_name] = transform(segment[point_name])
        for tile in transformed_snapshot["tileAnalyses"]:
            for cell in tile["map"]["cells"]:
                cell["position"] = transform(cell["position"])
            for sample in tile["utilities"]["samples"]:
                sample["position"] = transform(sample["position"])
            tile["bounds"] = {
                "minX": 700,
                "minZ": -800,
                "maxX": 1300,
                "maxZ": -200,
            }
        plan = {
            "planId": "baseline",
            "primaryUse": "Industrial",
            "planningAnchor": {"x": -50, "y": 10, "z": -50},
            "components": {
                "roadAccess": {
                    "strategy": "previewLocalStreetForDevelopmentFrontage",
                    "segmentId": 7,
                }
            },
        }
        transformed_plan = copy.deepcopy(plan)
        transformed_plan["planningAnchor"] = transform(plan["planningAnchor"])
        baseline = MAYOR_TOOLS.generate_local_development_road_candidates(
            baseline_snapshot, [plan], limit=4
        )
        transformed = MAYOR_TOOLS.generate_local_development_road_candidates(
            transformed_snapshot, [transformed_plan], limit=4
        )
        self.assertEqual(baseline["generated"], transformed["generated"])
        for original, rotated in zip(baseline["candidates"], transformed["candidates"]):
            self.assertEqual(transform(original["start"]), rotated["start"])
            self.assertEqual(transform(original["end"]), rotated["end"])
            self.assertEqual(original["staticBlockers"], rotated["staticBlockers"])

    def test_local_industrial_road_endpoint_near_residential_is_blocked(self):
        snapshot = self._planning_snapshot()
        snapshot["tileAnalyses"][0]["bounds"] = {
            "minX": -300,
            "minZ": -300,
            "maxX": 300,
            "maxZ": 300,
        }
        plan = {
            "planId": "industrial-unsafe",
            "primaryUse": "Industrial",
            "planningAnchor": {"x": 0, "y": 10, "z": 50},
            "components": {
                "roadAccess": {
                    "strategy": "previewLocalStreetForDevelopmentFrontage",
                    "segmentId": 7,
                }
            },
        }
        result = MAYOR_TOOLS.generate_local_development_road_candidates(
            snapshot, [plan], limit=4
        )
        self.assertTrue(result["candidates"])
        self.assertTrue(
            all(
                "industrialEndpointResidentialSeparation" in candidate["staticBlockers"]
                for candidate in result["candidates"]
            )
        )

    def test_local_road_preview_keeps_static_rejections_out_of_native_budget(self):
        candidates = [
            {
                "planId": f"plan-{index}",
                "roadPrefab": "Basic Road",
                "start": {"x": 0, "z": index * 40},
                "end": {"x": 64, "z": index * 40},
                "staticEligible": eligible,
                "staticBlockers": [] if eligible else ["endpointInWater"],
            }
            for index, eligible in enumerate((False, True, True, True))
        ]
        generated = {
            "generated": 4,
            "staticallyEligible": 3,
            "rejectionCounts": {"endpointInWater": 1},
            "candidates": candidates,
            "limits": {"candidateLimit": 8},
        }

        def request(method, path, query=None, body=None):
            if path == "/prefabs/roads":
                return {"roads": [{"name": "Basic Road", "unlocked": True}]}
            if path == "/commands/preview-network":
                return {"ok": True, "valid": True, "toolErrors": "None"}
            raise AssertionError(path)

        with mock.patch.object(
            MAYOR_TOOLS,
            "generate_local_development_road_candidates",
            return_value=generated,
        ), mock.patch.object(
            MAYOR_TOOLS,
            "_live_city_identity_or_none",
            return_value={"instanceId": "test"},
        ), mock.patch.object(
            MAYOR_TOOLS, "_request", side_effect=request
        ) as bridge, mock.patch.object(
            MAYOR_TOOLS, "_road_preview_survives_settlement", return_value=True
        ):
            result = MAYOR_TOOLS._preview_local_development_road_candidates(
                {}, [], {"roadPreviewLimit": 2}
            )

        self.assertEqual(3, result["staticallyEligible"])
        self.assertEqual(2, result["nativePreviewCalls"])
        self.assertEqual(2, result["acceptedCount"])
        self.assertEqual(1, result["nativePreviewCandidatesDeferred"])
        self.assertEqual("nativePreviewBudget", result["evaluated"][-1]["deferredReason"])
        self.assertEqual(3, bridge.call_count)

    def test_local_road_preview_rejects_native_valid_candidate_below_cash_reserve(self):
        generated = {
            "generated": 2,
            "staticallyEligible": 2,
            "rejectionCounts": {},
            "candidates": [
                {
                    "planId": "plan-1",
                    "candidateId": f"plan-1:7:1:{length}m",
                    "roadPrefab": "Basic Road",
                    "start": {"x": 0, "z": 0},
                    "end": {"x": length, "z": 0},
                    "staticEligible": True,
                    "staticBlockers": [],
                }
                for length in (64, 96)
            ],
            "limits": {"candidateLimit": 4},
        }
        costs = iter((3_200_000, 4_800_000))

        def request(method, path, query=None, body=None):
            if path == "/prefabs/roads":
                return {"roads": [{"name": "Basic Road", "unlocked": True}]}
            if path == "/commands/preview-network":
                return {
                    "ok": True,
                    "valid": True,
                    "toolErrors": "None",
                    "constructionCost": next(costs),
                }
            raise AssertionError(path)

        snapshot = {
            "mayorSnapshot": {"cityHealth": {"cash": {"amount": 4_966_766}}}
        }
        with mock.patch.object(
            MAYOR_TOOLS,
            "generate_local_development_road_candidates",
            return_value=generated,
        ), mock.patch.object(
            MAYOR_TOOLS,
            "_live_city_identity_or_none",
            return_value={"instanceId": "test"},
        ), mock.patch.object(
            MAYOR_TOOLS, "_request", side_effect=request
        ), mock.patch.object(
            MAYOR_TOOLS, "_road_preview_survives_settlement", return_value=True
        ):
            result = MAYOR_TOOLS._preview_local_development_road_candidates(
                snapshot, [], {"roadPreviewLimit": 2, "reserveDisplay": 10_000}
            )

        self.assertEqual(1, result["acceptedCount"])
        self.assertEqual("plan-1:7:1:64m", result["accepted"][0]["candidateId"])
        rejected = result["evaluated"][1]
        self.assertFalse(rejected["accepted"])
        self.assertEqual(["cashReservePolicy"], rejected["rejectionReasons"])
        self.assertEqual(1667.66, round(rejected["cashPolicy"]["projectedCashDisplay"], 2))
        self.assertFalse(rejected["cashPolicy"]["passed"])
        self.assertEqual(1, result["rejectionCounts"]["cashReservePolicy"])

    def test_explicit_offline_development_plan_never_calls_native_preview(self):
        with mock.patch.object(MAYOR_TOOLS, "_request") as bridge:
            result = MAYOR_TOOLS._preview_local_development_road_candidates(
                {}, [], {"snapshotPath": "snapshot.json", "roadPreviewLimit": 12}
            )
        self.assertFalse(result["available"])
        self.assertFalse(result["mutationAuthorized"])
        self.assertEqual(0, result["nativePreviewCalls"])
        bridge.assert_not_called()

    def test_zero_local_road_preview_budget_is_deferred_not_locked(self):
        generated = {
            "generated": 1,
            "staticallyEligible": 1,
            "rejectionCounts": {},
            "candidates": [
                {
                    "planId": "plan-1",
                    "roadPrefab": "Basic Road",
                    "start": {"x": 0, "z": 0},
                    "end": {"x": 64, "z": 0},
                    "staticEligible": True,
                    "staticBlockers": [],
                }
            ],
            "limits": {"candidateLimit": 4},
        }
        with mock.patch.object(
            MAYOR_TOOLS,
            "generate_local_development_road_candidates",
            return_value=generated,
        ), mock.patch.object(
            MAYOR_TOOLS,
            "_live_city_identity_or_none",
            return_value={"instanceId": "test"},
        ), mock.patch.object(MAYOR_TOOLS, "_request") as bridge:
            result = MAYOR_TOOLS._preview_local_development_road_candidates(
                {}, [], {"roadPreviewLimit": 0}
            )
        self.assertEqual(0, result["nativePreviewCalls"])
        self.assertEqual(1, result["nativePreviewCandidatesDeferred"])
        self.assertEqual("nativePreviewBudget", result["evaluated"][0]["deferredReason"])
        self.assertNotIn("roadPrefabLockedOrMissing", result["rejectionCounts"])
        bridge.assert_not_called()

    def test_implicit_offline_development_plan_skips_preview_without_bridge(self):
        with mock.patch.object(
            MAYOR_TOOLS, "_live_city_identity_or_none", return_value=None
        ), mock.patch.object(MAYOR_TOOLS, "_request") as bridge:
            result = MAYOR_TOOLS._preview_local_development_road_candidates(
                {}, [], {"roadPreviewLimit": 12}
            )
        self.assertFalse(result["available"])
        self.assertEqual("nativePreviewUnavailableWithoutLiveBridge", result["reason"])
        self.assertEqual(0, result["nativePreviewCalls"])
        bridge.assert_not_called()

    def test_network_preview_contract_exposes_bounded_new_endpoint_lifts(self):
        definitions = MAYOR_TOOLS._definitions()
        preview = next(
            item["function"]
            for item in definitions
            if item["function"]["name"] == "preview_network"
        )
        properties = preview["parameters"]["properties"]
        for name in ("startTerrainFitLift", "endTerrainFitLift"):
            self.assertEqual(0, properties[name]["minimum"])
            self.assertEqual(0.3, properties[name]["maximum"])
        MAYOR_TOOLS._validate_arguments(
            "preview_network",
            {
                "roadPrefab": "Basic Road",
                "start": {"x": 0, "z": 0},
                "end": {"x": 0, "z": 160},
                "endTerrainFitLift": 0.05,
            },
        )

    def test_development_plan_does_not_treat_highway_as_zoneable_frontage(self):
        snapshot = self._planning_snapshot()
        snapshot["networks"]["segments"][0]["prefab"] = "Highway"
        context = MAYOR_TOOLS.inspect_planning_area(
            snapshot,
            center={"x": 0, "z": 0},
            radius=100,
            intended_use="Residential",
            sample_count=5,
            candidate_limit=4,
        )
        plans = MAYOR_TOOLS.propose_development_plans(
            context,
            intended_use="Residential",
            max_plans=1,
        )
        self.assertFalse(context["candidateSites"][0]["nearestRoad"]["supportsZoning"])
        self.assertIn("nearestRoadDoesNotGenerateZoning", context["candidateSites"][0]["cautions"])
        self.assertEqual(
            "previewConnectorFromHighwayPairToLocalRoad",
            plans[0]["components"]["roadAccess"]["strategy"],
        )

    def test_development_plan_requires_local_street_after_gateway(self):
        snapshot = self._planning_snapshot()
        snapshot["networks"]["segments"][0]["name"] = "Test Gateway"
        context = MAYOR_TOOLS.inspect_planning_area(
            snapshot,
            center={"x": 0, "z": 0},
            radius=100,
            intended_use="Residential",
            sample_count=5,
            candidate_limit=4,
        )
        plans = MAYOR_TOOLS.propose_development_plans(
            context,
            intended_use="Residential",
            max_plans=1,
        )
        nearest = context["candidateSites"][0]["nearestRoad"]
        self.assertEqual("Gateway", nearest["planningRole"])
        self.assertFalse(nearest["supportsZoning"])
        self.assertIn(
            "gatewayRoadIsNotDevelopmentFrontage",
            context["candidateSites"][0]["cautions"],
        )
        self.assertEqual(
            "previewLocalStreetFromGateway",
            plans[0]["components"]["roadAccess"]["strategy"],
        )
        self.assertEqual(
            "zoneOnlyAfterVerifiedLocalStreetCreatesExactBlocks",
            plans[0]["components"]["zoning"]["strategy"],
        )

    @staticmethod
    def _planning_snapshot():
        cells = []
        utilities = []
        positions = [
            (-50, -50, 0),
            (50, -50, 200),
            (-50, 50, 0),
            (50, 50, 200),
        ]
        for index, (x, z, pollution) in enumerate(positions):
            row = index // 2
            column = index % 2
            cells.append(
                {
                    "row": row,
                    "column": column,
                    "position": {"x": x, "z": z},
                    "height": 10,
                    "slope": 0.05,
                    "unlocked": True,
                    "buildable": True,
                    "water": {"present": False},
                    "resources": {
                        "ore": 0,
                        "oil": 0,
                        "forest": 0,
                        "fertility": 0,
                        "pollution": pollution,
                    },
                }
            )
            utilities.append(
                {
                    "row": row,
                    "column": column,
                    "position": {"x": x, "z": z},
                    "unlocked": True,
                    "electricity": True,
                    "water": True,
                    "sewage": True,
                }
            )
        building = {
            "id": 12,
            "prefab": "House",
            "displayName": "House",
            "service": "Residential",
            "subService": "ResidentialLow",
            "active": True,
            "width": 2,
            "length": 2,
            "angleDegrees": 0,
            "position": {"x": 0, "y": 10, "z": 90},
        }
        return {
            "capturedAtUtc": "2026-07-29T00:00:00Z",
            "mayorSnapshot": {
                "city": {"instanceId": "planning-test", "name": "Test"},
                "summary": {"demand": {"residential": 70, "commercial": 40, "workplace": 20}},
                "progression": {
                    "capabilities": {
                        "lowDensityResidential": True,
                        "commercial": True,
                        "industry": True,
                    }
                },
            },
            "zones": {"ok": True, "zonedCells": 100},
            "facilities": {"facilities": []},
            "growables": {"growables": [building]},
            "networks": {
                "segments": [
                    {
                        "id": 7,
                        "prefab": "Basic Road",
                        "service": "Road",
                        "startNodeId": 1,
                        "endNodeId": 2,
                        "start": {"x": -100, "y": 10, "z": 0},
                        "middle": {"x": 0, "y": 10, "z": 0},
                        "end": {"x": 100, "y": 10, "z": 0},
                    },
                    {
                        "id": 8,
                        "prefab": "Water Pipe",
                        "service": "Water",
                        "startNodeId": 3,
                        "endNodeId": 4,
                        "start": {"x": -100, "y": 8, "z": -42},
                        "middle": {"x": 0, "y": 8, "z": -42},
                        "end": {"x": 100, "y": 8, "z": -42},
                    },
                    {
                        "id": 9,
                        "prefab": "Power Line",
                        "service": "Electricity",
                        "startNodeId": 5,
                        "endNodeId": 6,
                        "start": {"x": -100, "y": 20, "z": 42},
                        "middle": {"x": 0, "y": 20, "z": 42},
                        "end": {"x": 100, "y": 20, "z": 42},
                    },
                ]
            },
            "roadTemplates": {
                "Basic Road": {
                    "prefabLanes": [
                        {"laneType": "Vehicle", "finalDirection": "Forward"},
                        {"laneType": "Vehicle", "finalDirection": "Backward"},
                    ]
                }
            },
            "tileAnalyses": [
                {
                    "bounds": {"minX": -50, "minZ": -50, "maxX": 50, "maxZ": 50},
                    "map": {"cells": cells},
                    "utilities": {"samples": utilities},
                }
            ],
        }

    def test_candidate_scoring_rejects_hard_blocks(self):
        candidates = {
            "candidates": [
                {
                    "id": "safe-growth",
                    "urgency": 70,
                    "impact": 80,
                    "confidence": 90,
                    "alignment": 90,
                    "cost": 30,
                    "risk": 20,
                    "repetitionPenalty": 0,
                },
                {
                    "id": "locked-hospital",
                    "hardBlocked": True,
                    "urgency": 100,
                    "impact": 100,
                    "confidence": 100,
                    "alignment": 100,
                    "cost": 0,
                    "risk": 0,
                    "repetitionPenalty": 0,
                },
            ]
        }
        code, payload = self.run_tool(
            "call",
            "score_candidates",
            "--arguments",
            json.dumps(candidates),
        )
        self.assertEqual(0, code)
        self.assertEqual("safe-growth", payload["ranked"][0]["id"])
        self.assertFalse(payload["ranked"][1]["eligible"])

    def test_memory_round_trip_is_atomic_and_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            memory_path = Path(directory) / "mayor-state.json"
            state = {
                "cityVision": "Test city",
                "phase": "early-growth",
                "activeGoals": [],
                "recentActions": [
                    {"cycle": str(index), "action": "wait", "result": "verified"}
                    for index in range(120)
                ],
                "blockedGoals": [],
                "cooldowns": {},
                "lastSnapshot": {"population": 123},
            }
            snapshot = {
                "city": {
                    "instanceId": "current-city",
                    "name": "Test",
                    "map": "Test Map",
                }
            }
            with mock.patch.object(MAYOR_TOOLS, "MEMORY_PATH", memory_path), mock.patch.object(
                MAYOR_TOOLS,
                "_request",
                return_value=snapshot,
            ):
                saved = MAYOR_TOOLS.call_tool("save_mayor_memory", {"state": state})
            self.assertTrue(saved["ok"])
            self.assertEqual(100, len(saved["state"]["recentActions"]))

            with mock.patch.object(MAYOR_TOOLS, "MEMORY_PATH", memory_path), mock.patch.object(
                MAYOR_TOOLS,
                "_request",
                return_value=snapshot,
            ):
                loaded = MAYOR_TOOLS.call_tool("load_mayor_memory", {})
            self.assertEqual("Test city", loaded["state"]["cityVision"])
            self.assertEqual(123, loaded["state"]["lastSnapshot"]["population"])
            self.assertFalse(loaded["scope"]["resetForNewCity"])

    def test_memory_resets_coordinates_and_entity_ids_when_city_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            memory_path = Path(directory) / "mayor-state.json"
            memory_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "cityIdentity": {
                            "instanceId": "old-city",
                            "name": "Old",
                            "map": "Old Map",
                        },
                        "activeGoals": [{"position": {"x": 748, "z": 858}}],
                        "recentActions": [{"entityId": 22497}],
                    }
                ),
                encoding="utf-8",
            )
            snapshot = {
                "city": {
                    "instanceId": "new-city",
                    "name": "New",
                    "map": "New Map",
                }
            }
            with mock.patch.object(MAYOR_TOOLS, "MEMORY_PATH", memory_path), mock.patch.object(
                MAYOR_TOOLS,
                "_request",
                return_value=snapshot,
            ):
                loaded = MAYOR_TOOLS.call_tool("load_mayor_memory", {})
            self.assertTrue(loaded["scope"]["resetForNewCity"])
            self.assertEqual([], loaded["state"]["activeGoals"])
            self.assertEqual([], loaded["state"]["recentActions"])
            self.assertEqual("new-city", loaded["state"]["cityIdentity"]["instanceId"])
            persisted = json.loads(memory_path.read_text(encoding="utf-8"))
            self.assertNotIn("748", json.dumps(persisted))
            self.assertNotIn("22497", json.dumps(persisted))

    def test_memory_rejects_saving_state_from_another_city(self):
        snapshot = {
            "city": {
                "instanceId": "live-city",
                "name": "Live",
                "map": "Live Map",
            }
        }
        with mock.patch.object(MAYOR_TOOLS, "_request", return_value=snapshot):
            with self.assertRaisesRegex(RuntimeError, "different city instance"):
                MAYOR_TOOLS.call_tool(
                    "save_mayor_memory",
                    {
                        "state": {
                            "cityIdentity": {
                                "instanceId": "old-city",
                                "name": "Old",
                                "map": "Old Map",
                            }
                        }
                    },
                )

    def test_offline_snapshot_captures_current_city_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot_path = Path(directory) / "fresh.json"
            snapshot_path.write_text(
                json.dumps(
                    {
                        "mayorSnapshot": {
                            "city": {
                                "instanceId": "new-city",
                                "name": "New",
                                "map": "New Map",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(
                MAYOR_TOOLS,
                "_live_city_identity_or_none",
                return_value={"instanceId": "new-city", "name": "New", "map": "New Map"},
            ), mock.patch.object(
                MAYOR_TOOLS,
                "_capture_live_spatial_snapshot",
                return_value=snapshot_path,
            ) as capture:
                path, snapshot = MAYOR_TOOLS._offline_snapshot({})

            capture.assert_called_once_with()
            self.assertEqual(snapshot_path, path)
            self.assertEqual("new-city", snapshot["mayorSnapshot"]["city"]["instanceId"])

    def test_offline_snapshot_rejects_explicit_snapshot_from_another_live_city(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot_path = Path(directory) / "old.json"
            snapshot_path.write_text(
                json.dumps(
                    {
                        "mayorSnapshot": {
                            "city": {
                                "instanceId": "old-city",
                                "name": "Old",
                                "map": "Old Map",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(
                MAYOR_TOOLS,
                "_live_city_identity_or_none",
                return_value={"instanceId": "new-city", "name": "New", "map": "New Map"},
            ):
                with self.assertRaisesRegex(RuntimeError, "different city instance"):
                    MAYOR_TOOLS._offline_snapshot({"snapshotPath": str(snapshot_path)})

    def test_ground_road_build_runs_later_update_verification(self):
        settled_profile = {
                "source": "nativeGenerateBezierAndLiveTerrain",
                "verificationStage": "settled",
                "clearanceModel": "legacyNodeInsetProxyNotCalibratedSurface",
                "required": True,
                "sampleCount": 9,
                "maxRoadToTerrainDelta": 0.1,
                "minimumRoadAboveTerrain": 0.1,
                "maximumRoadAboveTerrain": 0.2,
                "maxLocalTerrainGrade": 0.01,
                "maximumRoadToTerrainDelta": 1.0,
                "minimumRoadAboveTerrainAllowed": 0.02,
                "maximumLocalTerrainGrade": 0.25,
                "originalNodeTransition": False,
                "safe": True,
        }
        build = {
            "ok": True,
            "segmentId": 42,
            "verificationPending": True,
            "actualTerrainProfile": {
                **settled_profile,
                "verificationStage": "immediate",
            },
        }
        settled = {
            "ok": True,
            "segmentId": 42,
            "settledVerification": True,
            "terrainProfile": settled_profile,
        }
        with mock.patch.object(
            MAYOR_TOOLS,
            "_require_normal_progression",
        ), mock.patch.object(
            MAYOR_TOOLS,
            "_is_water_network_prefab",
            return_value=False,
        ), mock.patch.object(
            MAYOR_TOOLS,
            "_require_checkpoint",
            side_effect=lambda arguments: {
                key: value
                for key, value in arguments.items()
                if key != "checkpointName"
            },
        ), mock.patch.object(
            MAYOR_TOOLS,
            "_request",
            side_effect=[{"city": {"instanceId": "test-city"}}, build, {"ok": True}, settled],
        ) as request:
            result = MAYOR_TOOLS.call_tool(
                "build_network",
                {
                    "checkpointName": "AgentCheckpoint-test",
                    "roadPrefab": "Basic Road",
                    "start": {"x": 0, "z": 0},
                    "end": {"x": 32, "z": 0},
                },
            )

        self.assertTrue(result["ok"])
        self.assertFalse(result["verificationPending"])
        self.assertTrue(result["settledVerification"]["settledVerification"])
        self.assertEqual("settled",result["settledTerrainProfile"]["verificationStage"])
        self.assertEqual("/commands/build-network", request.call_args_list[1].args[1])
        self.assertEqual("/state/summary", request.call_args_list[2].args[1])
        self.assertEqual("/commands/finalize-road-build", request.call_args_list[3].args[1])
        self.assertEqual({"segmentId": 42}, request.call_args_list[3].kwargs["body"])

    def _unsafe_immediate_build(self, pending):
        return {
            "ok": True,
            "segmentId": 42,
            "verificationPending": pending,
            "actualTerrainProfile": {
                "source": "nativeGenerateBezierAndLiveTerrain",
                "verificationStage": "immediate",
                "clearanceModel": "legacyNodeInsetProxyNotCalibratedSurface",
                "required": True,
                "sampleCount": 9,
                "maxRoadToTerrainDelta": 0.25,
                "minimumRoadAboveTerrain": -0.134,
                "maximumRoadAboveTerrain": 0.25,
                "maxLocalTerrainGrade": 0.026,
                "maximumRoadToTerrainDelta": 1.0,
                "minimumRoadAboveTerrainAllowed": 0.02,
                "maximumLocalTerrainGrade": 0.25,
                "originalNodeTransition": False,
                "safe": False,
            },
        }

    def _build_with_responses(self, responses):
        with mock.patch.object(
            MAYOR_TOOLS, "_require_normal_progression",
        ), mock.patch.object(
            MAYOR_TOOLS, "_is_water_network_prefab", return_value=False,
        ), mock.patch.object(
            MAYOR_TOOLS,
            "_require_checkpoint",
            side_effect=lambda arguments: {
                key: value for key, value in arguments.items() if key != "checkpointName"
            },
        ), mock.patch.object(MAYOR_TOOLS, "_request", side_effect=responses) as request:
            try:
                return MAYOR_TOOLS.call_tool("build_network", {
                    "checkpointName": "AgentCheckpoint-test",
                    "roadPrefab": "Basic Road",
                    "start": {"x": 0, "z": 0},
                    "end": {"x": 44, "z": 0},
                }), request
            except MAYOR_TOOLS.BridgeRequestError as error:
                return error, request

    def test_unsafe_immediate_ground_road_defers_to_bridge_settled_rollback(self):
        # A pending ground road's immediate profile is provisional. Its settled
        # verification must still run so the Bridge can roll it back locally.
        rollback = MAYOR_TOOLS.BridgeRequestError({
            "ok": False,
            "error": "Settled road verification failed (terrainProfileSafe=False); local rollback succeeded.",
            "recovery": {"localRollbackAttempted": True, "localRollbackSucceeded": True,
                         "checkpointRestoreRequired": False},
        })
        error, request = self._build_with_responses(
            [{"city": {"instanceId": "test-city"}}, self._unsafe_immediate_build(True), {"ok": True}, rollback])

        self.assertIsInstance(error, MAYOR_TOOLS.BridgeRequestError)
        self.assertEqual("/commands/finalize-road-build", request.call_args_list[3].args[1])
        self.assertTrue(error.detail["recovery"]["localRollbackSucceeded"])
        history = MAYOR_TOOLS.load_failures(MAYOR_TOOLS.ROAD_FAILURE_PATH, "test-city")
        self.assertTrue(all(MAYOR_TOOLS.recovery_verified(entry) for entry in history["failures"]))

    def test_flush_original_node_transition_accepts_its_negative_allowance(self):
        # A ramp leaving a highway node that sits flush with the terrain: the Bridge
        # allows the transition as low as that node, slightly below zero.
        profile = dict(self._unsafe_immediate_build(True)["actualTerrainProfile"],
                       minimumRoadAboveTerrain=0.004, maximumRoadAboveTerrain=0.2,
                       maxRoadToTerrainDelta=0.2, maximumRoadToTerrainDelta=3.0,
                       minimumRoadAboveTerrainAllowed=-0.005, originalNodeTransition=True, safe=True)
        validate = MAYOR_TOOLS._validated_native_road_terrain_profile
        self.assertIs(profile, validate(profile, "immediate", True))
        below_zero = dict(profile, minimumRoadAboveTerrain=-0.003)
        self.assertIs(below_zero, validate(below_zero, "immediate", True))

        for change in ({"originalNodeTransition": False},
                       {"minimumRoadAboveTerrainAllowed": -0.6, "minimumRoadAboveTerrain": -0.55},
                       {"maxLocalTerrainGrade": -0.01}):
            with self.subTest(change=change), self.assertRaisesRegex(RuntimeError, "invalid terrain measurements"):
                validate(dict(profile, **change), "immediate", True)

    def test_unsafe_immediate_profile_without_pending_settlement_fails_closed(self):
        error, request = self._build_with_responses(
            [{"city": {"instanceId": "test-city"}}, self._unsafe_immediate_build(False)])

        self.assertIsInstance(error, MAYOR_TOOLS.BridgeRequestError)
        self.assertTrue(error.detail["recovery"]["checkpointRestoreRequired"])
        self.assertEqual(2, request.call_count)

    def test_ground_road_finalize_keeps_cash_for_later_rollback(self):
        source = (SCRIPT.parent.parent / "src" / "RoadCommands.cs").read_text(encoding="utf-8")
        self.assertIn("public static CommandResult FinalizeRoadBuild", source)
        self.assertIn("CashBeforeConstruction = cashBeforeConstruction", source)
        self.assertIn("pending.CashBeforeConstruction", source)
        self.assertIn("GameThreadHelpers.TryReleaseSegment", source)
        self.assertIn("RefundConstructionCost", source)
        self.assertIn('\\"verificationStage\\":\\"settled\\"', source)
        self.assertIn('\\"source\\":\\"nativeGenerateBezierAndLiveTerrain\\"', source)
        self.assertIn('\\"clearanceModel\\":\\"legacyNodeInsetProxyNotCalibratedSurface\\"', source)

    def test_ground_road_terrain_check_measures_native_vertical_inset(self):
        source = (SCRIPT.parent.parent / "src" / "RoadCommands.cs").read_text(encoding="utf-8")
        state_source = (SCRIPT.parent.parent / "src" / "GameState.cs").read_text(encoding="utf-8")
        self.assertIn("GetRoadSurfaceOffset(manager, segmentId, segment)", source)
        self.assertIn("m_position.y - curve.a.y", source)
        self.assertIn("m_position.y - curve.d.y", source)
        self.assertIn("point.y + roadSurfaceOffset - terrainY", source)
        self.assertNotIn("segment.m_mesh.bounds.max.y", source)
        self.assertIn('\\"isGroundRoad\\"', state_source)

    def test_ground_road_preview_does_not_subtract_uncalibrated_native_offset(self):
        road_source = (SCRIPT.parent.parent / "src" / "RoadCommands.cs").read_text(encoding="utf-8")
        evidence_source = (SCRIPT.parent.parent / "src" / "RoadGeometryEvidence.cs").read_text(encoding="utf-8")
        self.assertIn("float intendedRoadY = sample.y;", road_source)
        self.assertNotIn("sample.y - GroundRoadNativeCurveYOffset", road_source)
        self.assertNotIn("GroundRoadNativeCurveYOffset + GroundRoadTargetTerrainClearance", road_source)
        self.assertIn("start.y += GroundRoadTargetTerrainClearance;", road_source)
        self.assertIn("Precise(point.y - terrain)", evidence_source)
        self.assertIn('\\"clearanceModel\\":\\"terrainSeededControlCurve\\"', evidence_source)
        self.assertNotIn("point.y - GroundRoadNativeCurveYOffset - terrain", evidence_source)

    def test_ground_road_endpoint_fitting_keeps_the_first_safe_lift(self):
        source = (SCRIPT.parent.parent / "src" / "RoadTerrainFitting.cs").read_text(encoding="utf-8")
        safe_branch = source.index("if (safe)")
        self.assertLess(safe_branch, source.index("return;", safe_branch))
        self.assertNotIn("bestSafeClearance", source)

    def test_ground_road_finalize_rechecks_and_recovers_adjacent_segments(self):
        source = (SCRIPT.parent.parent / "src" / "RoadCommands.cs").read_text(encoding="utf-8")
        self.assertIn("GetAdjacentRoadSegments(net, segmentId, segment)", source)
        self.assertIn("InspectRoadTerrainRegression", source)
        self.assertIn("adjacentTerrainRecovered", source)
        self.assertIn("adjacentRoadSegmentsChecked", source)

    def test_ground_road_endpoint_identity_uses_horizontal_distance(self):
        source = (SCRIPT.parent.parent / "src" / "RoadCommands.cs").read_text(encoding="utf-8")
        self.assertIn("HorizontalDistanceSquared(actualStart, requestedStart)", source)
        self.assertIn("HorizontalDistanceSquared(actualEnd, requestedEnd)", source)

    def test_road_preview_blocks_the_exact_native_move_middle_node_trigger(self):
        source = (SCRIPT.parent.parent / "src" / "RoadCommands.cs").read_text(encoding="utf-8")
        guard = source.index("bool nativeMoveMiddleNodeRisk = WouldInvokeNativeMoveMiddleNode")
        mutation = source.index("buildErrors = NetTool.CreateNode(", guard)
        self.assertLess(guard, mutation)
        self.assertIn("NetNode.Flags.End | NetNode.Flags.Moveable", source)
        self.assertIn("NetNode.Flags.Untouchable", source)
        self.assertIn("node.Info.IsCombatible(requestedInfo)", source)
        self.assertIn("NetSegment.IsStraight(", source)
        self.assertIn("< -0.999f", source)

    def test_transition_terrain_threshold_is_shared_with_live_details(self):
        road_source = (SCRIPT.parent.parent / "src" / "RoadCommands.cs").read_text(encoding="utf-8")
        state_source = (SCRIPT.parent.parent / "src" / "GameState.cs").read_text(encoding="utf-8")
        self.assertIn("IsOriginalNodeTransition", road_source)
        self.assertIn("minimumTerrainClearance >= GroundRoadMinimumTerrainClearance", road_source)
        self.assertIn("RoadCommands.IsOriginalNodeTransition", state_source)
        self.assertIn("RoadCommands.IsReportedTerrainProfileAcceptable", state_source)

    def test_zone_block_forwards_exact_growth_block_without_radius_guessing(self):
        with mock.patch.object(
            MAYOR_TOOLS,
            "_require_normal_progression",
        ), mock.patch.object(
            MAYOR_TOOLS,
            "_require_checkpoint",
            return_value={"zone": "ResidentialLow", "blockId": 1895},
        ), mock.patch.object(
            MAYOR_TOOLS,
            "_request",
            return_value={"ok": True, "requestedBlockId": 1895, "changedCells": 8},
        ) as request:
            result = MAYOR_TOOLS.call_tool(
                "zone_block",
                {
                    "checkpointName": "AgentCheckpoint-zone",
                    "zone": "ResidentialLow",
                    "blockId": 1895,
                },
            )

        self.assertTrue(result["ok"])
        self.assertEqual(
            {"zone": "ResidentialLow", "blockId": 1895, "preserveOccupied": True},
            request.call_args.kwargs["body"],
        )

    def test_zone_block_reports_failure_when_exact_block_changes_no_cells(self):
        with mock.patch.object(MAYOR_TOOLS, "_require_normal_progression"), mock.patch.object(
            MAYOR_TOOLS, "_require_checkpoint",
            return_value={"zone": "ResidentialLow", "blockId": 3007},
        ), mock.patch.object(
            MAYOR_TOOLS, "_request",
            return_value={"ok": True, "dryRun": False, "requestedBlockId": 3007,
                          "touchedBlocks": 1, "skippedOccupiedBlocks": 1, "changedCells": 0},
        ):
            result = MAYOR_TOOLS.call_tool("zone_block", {
                "checkpointName": "AgentCheckpoint-zone", "zone": "ResidentialLow", "blockId": 3007})
        self.assertFalse(result["ok"])
        self.assertEqual(1, result["skippedOccupiedBlocks"])

    def test_a_block_a_neighbours_zoning_already_covered_is_zoned_not_failed(self):
        unchanged = {"ok": True, "dryRun": False, "requestedBlockId": 15493, "touchedBlocks": 1,
                     "skippedOccupiedBlocks": 0, "changedCells": 0}
        for listed, zoned in (({"blockId": 15493, "zone": "ResidentialHigh", "validCells": 2, "zonedCells": 2}, True),
                              ({"blockId": 15493, "zone": "ResidentialHigh", "validCells": 4, "zonedCells": 2}, False),
                              ({"blockId": 15493, "zone": "CommercialHigh", "validCells": 2, "zonedCells": 2}, False)):
            def request(method, path, **kwargs):
                return unchanged if path == "/commands/set-zone" else {"blocks": [listed]}
            with mock.patch.object(MAYOR_TOOLS, "_require_normal_progression"), mock.patch.object(
                MAYOR_TOOLS, "_require_checkpoint", return_value={"zone": "ResidentialHigh", "blockId": 15493},
            ), mock.patch.object(MAYOR_TOOLS, "_request", side_effect=request):
                result = MAYOR_TOOLS.call_tool("zone_block", {
                    "checkpointName": "c", "zone": "ResidentialHigh", "blockId": 15493})
            self.assertEqual(zoned, result["ok"])
            self.assertEqual(zoned, bool(result.get("alreadyZoned")))

    def test_spatial_capture_tolerates_console_code_page_output(self):
        completed = mock.Mock(returncode=1, stdout=None, stderr="capture failed")
        with mock.patch.object(MAYOR_TOOLS.subprocess, "run", return_value=completed) as run:
            with self.assertRaisesRegex(RuntimeError, "capture failed"):
                MAYOR_TOOLS._capture_live_spatial_snapshot()
        self.assertEqual("replace", run.call_args.kwargs["errors"])

    def test_building_placement_charges_native_construction_cost(self):
        source = (SCRIPT.parent.parent / "src" / "BuildingCommands.cs").read_text(encoding="utf-8")
        self.assertIn("PeekResource(EconomyManager.Resource.Construction, constructionCost)", source)
        self.assertIn("FetchResource(EconomyManager.Resource.Construction, constructionCost, info.m_class)", source)
        # The charge happens only after creation and road-access checks succeed.
        self.assertLess(source.index("CheckRoadAccess(buildingId"), source.index("FetchResource(EconomyManager.Resource.Construction"))

    def test_corner_clearance_ignores_gentle_two_road_continuations(self):
        helpers = (SCRIPT.parent.parent / "src" / "ZoneHelpers.cs").read_text(encoding="utf-8")
        self.assertIn("MaximumContinuationDeflectionDegrees = 45f", helpers)
        self.assertIn("connectedRoads == 2 &&", helpers)
        # Junctions with three or more roads keep the full 64m clearance.
        self.assertIn("ConnectedRoadEndClearance = 64f", helpers)

    def test_exact_zone_block_uses_native_occupancy_not_broad_radius(self):
        zone_commands = (SCRIPT.parent.parent / "src" / "ZoneCommands.cs").read_text(encoding="utf-8")
        self.assertIn("? ExactBlockHasProtectedBuilding(block)", zone_commands)
        self.assertIn("(block.m_occupied1 | block.m_occupied2) & valid", zone_commands)
        self.assertIn("Requested zone block was skipped by a safety gate", zone_commands)
        self.assertIn("info.m_buildingAI is PowerPoleAI", zone_commands)

    def test_zone_cluster_ignores_empty_blocks_rejected_by_frontage_gate(self):
        zone_commands = (SCRIPT.parent.parent / "src" / "ZoneCommands.cs").read_text(encoding="utf-8")
        game_state = (SCRIPT.parent.parent / "src" / "GameState.cs").read_text(encoding="utf-8")
        zone_helpers = (SCRIPT.parent.parent / "src" / "ZoneHelpers.cs").read_text(encoding="utf-8")
        self.assertIn("requestedBlockId != 0 && i != requestedBlockId", zone_commands)
        self.assertIn("!ZoneHelpers.HasAnyZonedCells(block)", game_state)
        self.assertIn("public static bool HasAnyZonedCells", zone_helpers)

    def test_fiscal_cooldown_blocks_four_wake_window(self):
        now = MAYOR_TOOLS.datetime(2026, 7, 26, 12, 0, tzinfo=MAYOR_TOOLS.timezone.utc)
        state = {
            "cooldowns": {
                "fiscalAdjustmentUntil": "2026-07-26T13:00:00+00:00"
            }
        }
        self.assertEqual(60, MAYOR_TOOLS._fiscal_cooldown_remaining(state, now))
        self.assertEqual(
            0,
            MAYOR_TOOLS._fiscal_cooldown_remaining(
                state,
                MAYOR_TOOLS.datetime(2026, 7, 26, 13, 0, tzinfo=MAYOR_TOOLS.timezone.utc),
            ),
        )

    def test_fiscal_tools_require_exception_reason_and_outcome(self):
        code, payload = self.run_tool(
            "call",
            "set_tax_rate",
            "--arguments",
            json.dumps(
                {
                    "checkpointName": "before-tax",
                    "service": "Residential",
                    "rate": 11,
                }
            ),
        )
        self.assertEqual(1, code)
        self.assertIn("reason", payload["error"])

    def test_progression_guard_rejects_cheat_like_empty_city(self):
        assessment = MAYOR_TOOLS._assess_progression_mode(
            {
                "progression": {
                    "currentMilestone": "",
                    "capabilities": {
                        "highDensityResidential": True,
                        "office": True,
                        "healthcare": True,
                        "education": True,
                        "bus": True,
                        "metro": True,
                        "train": True,
                    },
                },
                "cityHealth": {
                    "cash": {"amount": 71_500_000},
                    "population": 0,
                },
            }
        )
        self.assertFalse(assessment["normalProgression"])
        self.assertEqual(2, len(assessment["reasons"]))
        self.assertEqual(715_000, assessment["displayCash"])

    def test_progression_guard_accepts_normal_new_map(self):
        assessment = MAYOR_TOOLS._assess_progression_mode(
            {
                "progression": {
                    "currentMilestone": "Milestone1",
                    "capabilities": {
                        "highDensityResidential": False,
                        "office": False,
                        "metro": False,
                        "train": False,
                    },
                },
                "cityHealth": {
                    "cash": {"amount": 7_000_000},
                    "population": 0,
                },
            }
        )
        self.assertTrue(assessment["normalProgression"])
        self.assertEqual(70_000, assessment["displayCash"])

    def test_toolbar_unlock_tool_reads_actual_ui_state(self):
        payload = {
            "ok": True,
            "toolbarFound": True,
            "services": [
                {
                    "service": "Electricity",
                    "simulationUnlocked": True,
                    "toolUnlocked": True,
                    "uiUsable": True,
                    "consistent": True,
                }
            ],
        }
        with mock.patch.object(
            MAYOR_TOOLS,
            "_request",
            return_value=payload,
        ) as request:
            result = MAYOR_TOOLS.call_tool("inspect_toolbar_unlocks", {})
        self.assertTrue(result["services"][0]["uiUsable"])
        request.assert_called_once_with("GET", "/state/toolbar-unlocks")

    def test_snapshot_money_is_annotated_in_display_units(self):
        snapshot = {
            "cityHealth": {
                "cash": {
                    "amount": 7_000_000,
                    "periodNet": -12_300,
                }
            }
        }
        annotated = MAYOR_TOOLS._annotate_display_money(snapshot)
        cash = annotated["cityHealth"]["cash"]
        self.assertEqual(70_000, cash["amountDisplay"])
        self.assertEqual(-123, cash["periodNetDisplay"])
        self.assertEqual(100, cash["internalUnitsPerDisplayUnit"])

    def test_checkpoint_returns_game_generated_name(self):
        generated = "AgentCheckpoint-20260726-065609-short-name"
        with mock.patch.object(
            MAYOR_TOOLS,
            "_request",
            return_value={"ok": True, "saveName": generated},
        ), mock.patch.object(MAYOR_TOOLS, "_wait_checkpoint", return_value=True) as wait:
            result = MAYOR_TOOLS.call_tool(
                "create_checkpoint",
                {"name": "short-name"},
            )
        self.assertTrue(result["ok"])
        self.assertEqual(generated, result["checkpointName"])
        wait.assert_called_once_with(generated)

    def test_checkpoint_waits_until_async_save_finishes(self):
        record = {
            "name": "AgentCheckpoint-safe",
            "exists": True,
            "length": 1234,
            "lastWriteTimeUtc": "2026-07-29T15:20:00",
        }
        with mock.patch.object(
            MAYOR_TOOLS,
            "_request",
            side_effect=[
                {"ok": True, "isSaving": True, "save": record},
                {"ok": True, "isSaving": False, "save": record},
                {"ok": True, "isSaving": False, "save": record},
                {"ok": True, "isSaving": False, "save": record},
                {"ok": True, "isSaving": False, "save": record},
            ],
        ) as request, mock.patch.object(MAYOR_TOOLS.time, "sleep"):
            self.assertTrue(MAYOR_TOOLS._wait_checkpoint("AgentCheckpoint-safe", 1))
        self.assertEqual(5, request.call_count)
        # the save's own status, answered off the game thread, never the listing of every save
        self.assertEqual({("GET", "/state/save-status")}, {call.args for call in request.call_args_list})
        self.assertEqual({"name": "AgentCheckpoint-safe"}, request.call_args.kwargs["query"])

    def test_checkpoint_resets_stability_when_file_changes(self):
        first = {
            "name": "AgentCheckpoint-safe",
            "exists": True,
            "length": 1000,
            "lastWriteTimeUtc": "2026-07-29T15:20:00",
        }
        final = {
            "name": "AgentCheckpoint-safe",
            "exists": True,
            "length": 2000,
            "lastWriteTimeUtc": "2026-07-29T15:20:01",
        }
        responses = [
            {"ok": True, "isSaving": False, "save": first},
            {"ok": True, "isSaving": False, "save": first},
            {"ok": True, "isSaving": False, "save": final},
            {"ok": True, "isSaving": False, "save": final},
            {"ok": True, "isSaving": False, "save": final},
            {"ok": True, "isSaving": False, "save": final},
        ]
        with mock.patch.object(
            MAYOR_TOOLS,
            "_request",
            side_effect=responses,
        ) as request, mock.patch.object(MAYOR_TOOLS.time, "sleep"):
            self.assertTrue(MAYOR_TOOLS._wait_checkpoint("AgentCheckpoint-safe", 1))
        self.assertEqual(6, request.call_count)

    def test_checkpoint_wait_falls_back_to_the_listing_on_an_older_bridge(self):
        saves = [{"name": "AgentCheckpoint-safe", "length": 1234, "lastWriteTimeUtc": "2026-07-29T15:20:00"}]

        def request(method, path, **kwargs):
            if path == "/state/save-status":
                raise MAYOR_TOOLS.BridgeRequestError({"ok": False, "error": "Not found"})
            return {"ok": True, "isSaving": False, "saves": saves}

        with mock.patch.object(MAYOR_TOOLS, "_request", side_effect=request),                 mock.patch.object(MAYOR_TOOLS.time, "sleep"):
            self.assertTrue(MAYOR_TOOLS._wait_checkpoint("AgentCheckpoint-safe", 1))
            self.assertTrue(MAYOR_TOOLS._checkpoint_exists("AgentCheckpoint-safe"))
            self.assertFalse(MAYOR_TOOLS._checkpoint_exists("AgentCheckpoint-gone"))

    def test_checkpoint_exists_asks_for_that_save_only(self):
        with mock.patch.object(MAYOR_TOOLS, "_request", side_effect=[
                {"ok": True, "isSaving": False, "save": {"name": "a", "exists": True}},
                {"ok": True, "isSaving": False, "save": {"name": "b", "exists": False}}]) as request:
            self.assertTrue(MAYOR_TOOLS._checkpoint_exists("a"))
            self.assertFalse(MAYOR_TOOLS._checkpoint_exists("b"))
        self.assertEqual([("GET", "/state/save-status")] * 2, [call.args for call in request.call_args_list])

    def test_a_bridge_error_other_than_a_missing_endpoint_is_not_hidden(self):
        busy = MAYOR_TOOLS.BridgeRequestError({"ok": False, "error": "name must be a save name, not a path."})
        with mock.patch.object(MAYOR_TOOLS, "_request", side_effect=busy), self.assertRaises(MAYOR_TOOLS.BridgeRequestError):
            MAYOR_TOOLS._checkpoint_exists("../x")

    def test_building_preview_maps_existing_id_to_ignore_id(self):
        with mock.patch.object(
            MAYOR_TOOLS,
            "_request",
            return_value={"ok": True, "valid": True},
        ) as request:
            result = MAYOR_TOOLS.call_tool(
                "preview_building_candidate",
                {
                    "buildingPrefab": "Medical Clinic",
                    "buildingId": 42,
                    "position": {"x": 100, "z": 200},
                },
            )
        self.assertTrue(result["valid"])
        body = request.call_args.kwargs["body"]
        self.assertEqual(42, body["ignoreBuildingId"])
        self.assertNotIn("buildingId", body)

    def test_mutation_previews_force_dry_run(self):
        with mock.patch.object(
            MAYOR_TOOLS,
            "_request",
            return_value={"ok": True, "safe": True},
        ) as request:
            MAYOR_TOOLS.call_tool(
                "preview_operational_change",
                {"id": 10, "active": False},
            )
        self.assertTrue(request.call_args.kwargs["body"]["dryRun"])

        with mock.patch.object(
            MAYOR_TOOLS,
            "_request",
            return_value={"ok": True, "valid": True},
        ) as request:
            MAYOR_TOOLS.call_tool(
                "preview_transport_plan",
                {
                    "transportType": "Bus",
                    "stops": [{"x": 0, "z": 0}, {"x": 64, "z": 0}],
                },
            )
        self.assertTrue(request.call_args.kwargs["body"]["dryRun"])





class GroundwaterTests(unittest.TestCase):
    PUMP = {"buildingPrefab": "Water Tower", "position": {"x": 0, "z": 0}, "angleDegrees": 0}
    OUTLET = {"buildingPrefab": "Water Outlet", "position": {"x": 900, "z": 0}, "angleDegrees": 0}

    def request(self, pump_environment=None, surface_intake=False, outlet_valid=True):
        prefabs = {"buildings": [
            {"name": "Water Tower", "aiProfile": {"fields": {"waterIntake": 3750, "useGroundWater": True}}},
            {"name": "Water Intake", "aiProfile": {"fields": {"waterIntake": 7500, "useGroundWater": False}}},
            {"name": "Water Outlet", "aiProfile": {"fields": {"sewageOutlet": 7500}}}]}
        facilities = {"facilities": [{"id": 5, "prefab": "Water Intake"}] if surface_intake else []}

        def request(method, path, query=None, body=None):
            if path == "/commands/preview-placement":
                if body["buildingPrefab"] == "Water Tower":
                    return {"valid": True, "aiProfile": {"fields": {"useGroundWater": True}},
                            "environmental": pump_environment or {"groundWaterIntakePollution": 0,
                                                                  "currentGroundPollution": 0},
                            "networkPaths": [{"service": "Water", "nodes": [{"x": 0, "z": 8}]}]}
                return {"valid": outlet_valid, "aiProfile": {"fields": {}}, "gamePlacement": {"productionRate": 100},
                        "waterPlacement": {"closestWater": {"found": True}}}
            if path == "/prefabs/buildings":
                return prefabs
            if path == "/state/facilities":
                return facilities
            raise AssertionError(path)
        return request

    def inspect(self, polluters=(), **kwargs):
        with mock.patch.object(MAYOR_TOOLS, "_request", side_effect=self.request(**kwargs)), \
                mock.patch.object(MAYOR_TOOLS, "_groundwater_polluters", return_value=list(polluters)):
            return MAYOR_TOOLS._inspect_water_pair({"intake": self.PUMP, "outlet": self.OUTLET})

    def test_a_groundwater_pump_is_judged_by_its_ground_not_the_river(self):
        result = self.inspect()
        self.assertTrue(result["safe"], result["risks"])
        self.assertTrue(result["groundwater"])
        self.assertEqual([{"x": 0.0, "z": 8.0}], MAYOR_TOOLS._preview_water_pipe_connections(result["previews"]["intake"]))

    def test_polluted_ground_industry_near_a_bad_outlet_or_a_river_intake_make_it_unsafe(self):
        self.assertIn("groundwaterPolluted", self.inspect(
            pump_environment={"groundWaterIntakePollution": 12, "currentGroundPollution": 0})["risks"])
        self.assertIn("groundwaterNearPolluter", self.inspect(polluters=[(200, 0)])["risks"])
        self.assertTrue(self.inspect(polluters=[(400, 0)])["safe"])
        self.assertIn("outletPlacementInvalid", self.inspect(outlet_valid=False)["risks"])
        self.assertIn("surfaceIntakeNeedsRiverPair", self.inspect(surface_intake=True)["risks"])

    def test_pump_sites_are_clean_quiet_and_off_the_town_nearest_first(self):
        def request(method, path, query=None, body=None):
            if path == "/prefabs/buildings":
                return {"buildings": [{"name": "Water Tower", "unlocked": True, "constructionCost": 350000,
                                       "aiProfile": {"fields": {"useGroundWater": True}}}]}
            if path == "/commands/preview-placement":
                x = body["position"]["x"]
                noisy = int(x < 0)  # homes west of the town
                return {"valid": True, "position": {**body["position"], "y": 60.0},
                        "environmental": {"residentialBuildingsInsideNoiseRadius": noisy},
                        "networkPaths": [{"service": "Water", "nodes": [body["position"]]}]}
            raise AssertionError(path)
        with mock.patch.object(MAYOR_TOOLS, "_request", side_effect=request), \
                mock.patch.object(MAYOR_TOOLS, "_groundwater_polluters", return_value=[(0, -400)]):
            result = MAYOR_TOOLS.call_tool("propose_groundwater_pump_sites", {
                "serviceAreaAnchor": {"x": 0, "z": 0}, "minDistance": 200, "searchRadius": 300,
                "spacing": 100, "maxResults": 8})
        distances = [math.dist((0, 0), (site["position"]["x"], site["position"]["z"])) for site in result["candidates"]]
        self.assertEqual(sorted(distances), distances)  # nearest first
        self.assertTrue(result["safe"])
        for site in result["candidates"]:
            self.assertGreaterEqual(math.dist((0, 0), (site["position"]["x"], site["position"]["z"])), 200)
            self.assertGreaterEqual(site["position"]["x"], 0)  # the noisy side is refused
            self.assertEqual({"x", "z"}, set(site["position"]))  # a site is passed on as a facility position
        self.assertGreater(result["rejected"]["noisyHomes"], 0)
        self.assertGreater(result["rejected"]["nearPolluter"], 0)



class FiscalGameTimeTests(unittest.TestCase):
    def test_a_game_month_since_the_change_ends_the_cooldown_and_less_does_not(self):
        from datetime import datetime
        state = {"cooldowns": {"fiscalAdjustmentGameTime": "2062-06-01T00:00:00"}}
        self.assertTrue(MAYOR_TOOLS._fiscal_game_days_passed(state, datetime(2062, 7, 2)))
        self.assertFalse(MAYOR_TOOLS._fiscal_game_days_passed(state, datetime(2062, 6, 20)))
        self.assertFalse(MAYOR_TOOLS._fiscal_game_days_passed({"cooldowns": {}}, datetime(2062, 7, 2)))
        self.assertFalse(MAYOR_TOOLS._fiscal_game_days_passed(state, None))  # no clock read: real time rules



class BypassRelocateTests(unittest.TestCase):
    def preview(self, errors, under):
        reply = {"toolErrors": errors, "terrainProfile": {"safe": True},
                 "buildingsUnderRoad": [{"id": item} for item in under]}
        with mock.patch.object(MAYOR_TOOLS, "_request", return_value=reply):
            return MAYOR_TOOLS._bypass_piece_preview("Basic Road", {"start": {}, "end": {}}, {1: {}}, {5})

    def test_a_water_tower_in_the_way_is_cleared_with_its_collision_and_a_school_blocks(self):
        piece = self.preview("ObjectCollision", [1, 5])
        self.assertEqual((True, [1], [5], []), (piece["buildable"], piece["clearGrowables"],
                                                piece["relocateBuildings"], piece["blockingBuildings"]))
        self.assertFalse(self.preview("ObjectCollision", [5, 6])["buildable"])
        self.assertFalse(self.preview("ObjectCollision", [])["buildable"])  # another network in the way
        self.assertTrue(self.preview("ObjectCollision", [1])["buildable"])  # the homes it clears collide
        self.assertFalse(self.preview("TooShort", [5])["buildable"])


class BypassEntryTests(unittest.TestCase):
    def test_entries_inside_the_hotspot_are_followed_upstream_and_highways_left_to_links(self):
        feeders = {
            2: [{"segmentId": 3, "vehicles": 400, "prefab": "Small 4 Lane Road"}, {"segmentId": 9, "vehicles": 5}],
            3: [{"segmentId": 4, "vehicles": 380, "prefab": "Basic Road"}],
            6: [{"segmentId": 7, "vehicles": 90, "prefab": "Highway"}],
        }
        routes = {"enteringFromSegments": [
            {"segmentId": 6, "vehicles": 100, "prefab": "Small 4 Lane Road"},
            {"segmentId": 2, "vehicles": 700, "prefab": "Small 4 Lane Road"},
            {"segmentId": 8, "vehicles": 2, "prefab": "Basic Road"}]}
        reply = lambda method, path, query=None, **_: {"enteringFromSegments": feeders.get(query["id"], [])}
        with mock.patch.object(MAYOR_TOOLS, "_request", side_effect=reply):
            entries, highway_fed = MAYOR_TOOLS._bypass_entries(routes, {1, 2, 3, 6})
        # the 700 entering by hotspot segments 2 and 3 come from road 4, which carries 380 of them
        self.assertEqual([(4, 380), (8, 2)], [(item["segmentId"], item["vehicles"]) for item in entries])
        self.assertEqual(100, highway_fed)

if __name__ == "__main__":
    unittest.main()

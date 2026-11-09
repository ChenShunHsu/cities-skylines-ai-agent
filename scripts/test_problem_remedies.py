#!/usr/bin/env python3

import sys
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import mayor_tools
from mayor import problems


def row(building_id, names, x, z, major=False):
    return {"entityType": "building", "id": building_id, "problemNames": names, "isMajor": major,
            "isFatal": False, "position": {"x": x, "y": 0, "z": z}}


FACILITIES = [
    {"id": 1, "service": "HealthCare", "prefab": "Cemetery", "flags": "Created, Active", "problems": "",
     "position": {"x": 0, "z": 0}},
    {"id": 2, "service": "HealthCare", "prefab": "Cemetery", "flags": "Created, CapacityFull, Active",
     "problems": "LandfillFull", "position": {"x": -2000, "z": 2000}},
    {"id": 3, "service": "HealthCare", "prefab": "medicalclinicEU", "flags": "Created, Active", "problems": "",
     "position": {"x": -2000, "z": 2000}},
]


class ProblemRemedyTests(unittest.TestCase):
    def test_every_problem_gets_a_remedy_or_is_unmapped(self):
        entries = problems.problem_remedies([
            row(10, ["Death", "MajorProblem"], -2050, 2000, major=True),
            row(11, ["Death"], -1900, 2050),
            row(12, ["Death"], 30, 20),
            row(13, ["Abandoned", "FatalProblem"], 5, 5),
            row(14, ["SomethingNew"], 5, 5),
            row(15, ["NoPlaceforGoods"], 600, 1900),
            row(16, ["LineNotConnected"], 700, 1900),
            row(17, ["StructureDamaged", "FatalProblem"], 40, 40),
        ], FACILITIES)
        by_name = {entry["problem"]: entry for entry in entries}
        self.assertEqual({"Death", "Abandoned", "SomethingNew", "NoPlaceforGoods", "LineNotConnected",
                          "StructureDamaged"}, set(by_name))
        self.assertEqual("transitLine", by_name["LineNotConnected"]["remedy"]["kind"])
        self.assertEqual({"kind": "economy", "lever": "commercialOutlet"}, by_name["NoPlaceforGoods"]["remedy"])  # severity flags are not problems
        self.assertEqual("unmapped", by_name["SomethingNew"]["remedy"]["kind"])
        self.assertEqual("bulldoze", by_name["Abandoned"]["remedy"]["kind"])
        self.assertEqual("bulldoze", by_name["StructureDamaged"]["remedy"]["kind"])  # a ruin's flag
        death = by_name["Death"]
        self.assertEqual((3, 1), (death["count"], death["major"]))
        self.assertEqual([2], death["facilities"]["full"])
        self.assertEqual(["Cemetery"], death["facilities"]["prefabs"])  # the clinic is not deathcare
        far, near = death["clusters"]
        self.assertEqual([10, 11], far["buildingIds"])
        # the full cemetery next to the west cluster does not count as serving it
        self.assertEqual(1, far["nearestWorkingFacility"]["id"])
        self.assertGreater(far["nearestWorkingFacility"]["distance"], 2500)
        self.assertEqual([12], near["buildingIds"])

    def test_clusters_link_chains_of_nearby_buildings(self):
        groups = problems.clusters([{"id": i, "x": i * 200.0, "z": 0.0} for i in range(4)] +
                                   [{"id": 9, "x": 5000.0, "z": 0.0}], 250)
        self.assertEqual([[0, 1, 2, 3], [9]], [group["buildingIds"] for group in groups])


# every Problem1 flag of the base game (Notification.Problem1, read from Assembly-CSharp), without
# the severity marks and the flags only DLC buildings raise; plus the building flags the Bridge reports
BASE_GAME_PROBLEMS = (
    "Garbage", "Electricity", "Water", "Fire", "DirtyWater", "Crime", "Pollution", "TurnedOff",
    "TooFewServices", "LandValueLow", "ElectricityNotConnected", "NoFuel", "RoadNotConnected",
    "WaterNotConnected", "Sewage", "Death", "LandfillFull", "LineNotConnected", "NoCustomers",
    "NoResources", "NoGoods", "NoPlaceforGoods", "NoWorkers", "NoEducatedWorkers", "Noise", "Emptying",
    "TaxesTooHigh", "EmptyingFinished", "Flood", "DepotNotConnected", "TrackNotConnected",
    "StructureDamaged", "Abandoned", "BurnedDown", "Collapsed", "Flooded", "RoadAccessFailed")


class CoverageTests(unittest.TestCase):
    def test_every_base_game_problem_has_a_remedy(self):
        self.assertEqual([], [name for name in BASE_GAME_PROBLEMS if name not in problems.REMEDIES])

    def test_service_use_no_building_flags_is_reported_from_the_homes_it_affects(self):
        homes = [{"id": i, "position": {"x": x, "z": 0}} for i, x in enumerate((0, 100, 1500, 1600))]
        clinic = [{"id": 90, "prefab": "Medical Clinic", "service": "HealthCare", "position": {"x": 50, "z": 0}}]
        short = {"healthcare": {"used": 111, "capacity": 0}, "elementaryEducation": {"used": 168, "capacity": 0},
                 "highSchoolEducation": {"used": 400, "capacity": 0}}
        rows = problems.shortfall_rows(short, homes, [], {"healthcare", "elementary"})
        names = [(row["id"], row["problemNames"][0]) for row in rows]
        self.assertEqual([0, 1, 2, 3], [i for i, name in names if name == "Sick"])  # no clinic: every home
        self.assertNotIn("HighSchoolShort", {name for _, name in names})  # no high school unlocked
        # a clinic within reach of the first two homes: the two beyond report it
        rows = problems.shortfall_rows(short, homes, clinic, {"healthcare"})
        self.assertEqual([2, 3], [row["id"] for row in rows])
        # every home within reach yet the beds run short: all of them report it
        rows = problems.shortfall_rows({"healthcare": {"used": 300, "capacity": 100}}, homes[:2], clinic,
                                       {"healthcare"})
        self.assertEqual([0, 1], [row["id"] for row in rows])
        self.assertEqual([], problems.shortfall_rows({"healthcare": {"used": 110, "capacity": 100}}, homes,
                                                     [], {"healthcare"}))  # under SHORTFALL_MIN
        entries = problems.problem_remedies(problems.shortfall_rows(short, homes, [], {"elementary"}), [])
        self.assertEqual(("ElementaryShort", "elementary"),
                         (entries[0]["problem"], entries[0]["remedy"]["family"]))
        self.assertIsNone(entries[0]["clusters"][0]["nearestWorkingFacility"])


class ServiceSiteTests(unittest.TestCase):
    def test_sites_rank_by_displacement_and_never_cover_service_buildings(self):
        growables = [
            {"id": 50, "service": "Residential", "prefab": "house", "abandoned": False},
            {"id": 51, "service": "Commercial", "prefab": "shop", "abandoned": False},
            {"id": 52, "service": "Residential", "prefab": "house", "abandoned": True},
        ]
        roads = [{"id": 7, "prefab": "Basic Road", "start": {"x": 0, "z": 0}, "end": {"x": 96, "z": 0}},
                 {"id": 8, "prefab": "Highway", "start": {"x": 0, "z": 50}, "end": {"x": 96, "z": 50}}]

        def request(method, path, query=None, body=None):
            if path == "/state/growables":
                return {"buildings": growables}
            if path == "/state/networks":
                return {"segments": roads}
            if path == "/commands/preview-placement":
                if "requireRoadAccess" not in body:
                    return {"footprint": {"width": 80, "length": 64}}
                north = body["position"]["z"] > 0
                if body["angleDegrees"] not in (90.0, 270.0):
                    return {"safety": {"risks": ["requiredRoadAccessMissing"]}}
                if north:
                    return {"safety": {"risks": ["buildingOverlap"]},
                            "buildingOverlaps": [{"id": 50}, {"id": 52}]}
                return {"safety": {"risks": ["buildingOverlap"]}, "buildingOverlaps": [{"id": 51}, {"id": 99}]}
            raise AssertionError(path)

        with mock.patch.object(mayor_tools, "_request", side_effect=request):
            result = mayor_tools.call_tool("propose_service_sites", {
                "buildingPrefab": "Cemetery", "near": {"x": 48, "z": 0}})
        self.assertEqual(1, len(result["sites"]))  # the south side covers service building 99
        site = result["sites"][0]
        self.assertEqual((2, 7), (site["displacementCost"], site["roadSegmentId"]))
        self.assertEqual({"x": 48.0, "z": 40.5}, site["position"])
        self.assertEqual([50, 52], [item["id"] for item in site["coveredGrowables"]])

    def test_polluting_building_keeps_clear_of_homes(self):
        growables = [{"id": 60, "service": "Residential", "prefab": "house", "abandoned": False,
                      "position": {"x": 48, "z": 300}}]
        roads = [{"id": 7, "prefab": "Basic Road", "start": {"x": 0, "z": 0}, "end": {"x": 96, "z": 0}}]

        def request(method, path, query=None, body=None):
            if path == "/state/growables":
                return {"buildings": growables}
            if path == "/state/networks":
                return {"segments": roads}
            if path == "/commands/preview-placement":
                if "requireRoadAccess" not in body:
                    return {"footprint": {"width": 80, "length": 64},
                            "environmental": {"groundPollutionAccumulation": 100, "groundPollutionRadius": 150}}
                return {"safety": {"risks": []}}
            raise AssertionError(path)

        with mock.patch.object(mayor_tools, "_request", side_effect=request), \
                mock.patch.object(mayor_tools, "_land_use_context", return_value={"wells": []}):
            result = mayor_tools.call_tool("propose_service_sites", {
                "buildingPrefab": "Landfill Site", "near": {"x": 48, "z": 0}})
        self.assertEqual(300, result["residentialClearance"])
        # the north side is 259.5m from the home, the south side 340.5m
        self.assertEqual([{"x": 48.0, "z": -40.5}], [site["position"] for site in result["sites"]])

    def test_an_oblong_lot_turned_across_the_road_sits_past_its_long_side(self):
        roads = [{"id": 7, "prefab": "Basic Road", "start": {"x": 0, "z": 0}, "end": {"x": 96, "z": 0}}]

        def request(method, path, query=None, body=None):
            if path == "/state/growables":
                return {"buildings": []}
            if path == "/state/networks":
                return {"segments": roads}
            if path == "/commands/preview-placement":
                if "requireRoadAccess" not in body:
                    return {"footprint": {"width": 80, "length": 64}}
                if body["angleDegrees"] not in (90.0, 270.0):
                    return {"safety": {"risks": ["requiredRoadAccessMissing"]}}
                # facing the road, the 80m side lies across it and needs 52m from the road's centre
                if abs(body["position"]["z"]) < 52:
                    return {"safety": {"risks": ["roadOverlap"]}, "roadOverlaps": [{"id": 7}]}
                return {"safety": {"risks": []}}
            raise AssertionError(path)

        with mock.patch.object(mayor_tools, "_request", side_effect=request):
            result = mayor_tools.call_tool("propose_service_sites", {
                "buildingPrefab": "Landfill Site", "near": {"x": 48, "z": 0}})
        self.assertEqual([52.0, 52.0], sorted(abs(site["position"]["z"]) for site in result["sites"]))


class ClearAbandonedTests(unittest.TestCase):
    def run_clear(self, arguments, refuse=(), vanish=()):
        state = {"growables": [
            {"id": 1, "abandoned": True, "flags": "Created, Abandoned"},
            {"id": 2, "abandoned": False, "flags": "Created, BurnedDown"},
            {"id": 3, "abandoned": False, "flags": "Created, Active"},
            {"id": 4, "abandoned": True, "flags": "Created, Abandoned"},
        ]}
        calls = []

        def request(method, path, query=None, body=None):
            calls.append((path, body))
            if path == "/state/growables":
                return {"buildings": list(state["growables"])}
            if path == "/commands/preview-bulldoze-impact":
                if body["id"] in vanish:
                    raise mayor_tools.BridgeRequestError('{"ok": false, "error": "Building was not found: 1"}')
                safe = body["id"] != 4
                return {"safe": safe, "risks": [] if safe else ["lastActiveFacilityOfService"]}
            if path == "/commands/bulldoze":
                if body["id"] in refuse:
                    raise mayor_tools.BridgeRequestError("release refused")
                state["growables"] = [item for item in state["growables"] if item["id"] != body["id"]]
                return {"ok": True}
            raise AssertionError(path)

        with mock.patch.object(mayor_tools, "_request", side_effect=request), \
                mock.patch.object(mayor_tools, "_require_normal_progression"), \
                mock.patch.object(mayor_tools, "_require_checkpoint", side_effect=lambda args: args):
            return mayor_tools.call_tool("clear_abandoned_buildings", {"checkpointName": "c", **arguments}), calls

    def test_clears_abandoned_and_ruined_buildings_whose_preview_is_safe(self):
        result, _ = self.run_clear({})
        self.assertTrue(result["ok"])
        self.assertEqual((3, [1, 2]), (result["found"], result["cleared"]))
        self.assertEqual([4], [item["id"] for item in result["skipped"]])
        self.assertEqual([], result["stillPresent"])

    def test_refused_release_is_reported_and_dry_run_changes_nothing(self):
        result, _ = self.run_clear({}, refuse=(2,))
        self.assertFalse(result["ok"])
        self.assertEqual([2], [item["id"] for item in result["refused"]])
        result, _ = self.run_clear({}, vanish=(1,))
        self.assertTrue(result["ok"])
        self.assertEqual(([2], [1]), (result["cleared"], result["alreadyGone"]))
        result, calls = self.run_clear({"dryRun": True})
        self.assertEqual([1, 2], result["cleared"])
        self.assertNotIn("/commands/bulldoze", [path for path, _ in calls])


class RebuildFullFacilityTests(unittest.TestCase):
    def run_rebuild(self, building_id=2, lot_valid=True):
        calls = []

        def request(method, path, query=None, body=None):
            calls.append((path, body))
            if path == "/state/facilities":
                return {"facilities": [dict(item, angleDegrees=90) for item in FACILITIES]}
            if path == "/commands/preview-placement":
                return {"valid": lot_valid, "safety": {"hardSafe": lot_valid, "risks": [] if lot_valid else ["roadOverlap"]}}
            if path == "/commands/bulldoze":
                return {"ok": True}
            if path == "/commands/place-building":
                return {"ok": True, "buildingId": 77, "constructionCost": 800000}
            raise AssertionError(path)

        with mock.patch.object(mayor_tools, "_request", side_effect=request), \
                mock.patch.object(mayor_tools, "_require_normal_progression"), \
                mock.patch.object(mayor_tools, "_require_checkpoint", side_effect=lambda args: args):
            return mayor_tools.call_tool("rebuild_full_facility", {"checkpointName": "c", "buildingId": building_id}), calls

    def test_full_cemetery_is_rebuilt_on_its_lot_after_the_lot_previews_valid(self):
        result, calls = self.run_rebuild()
        self.assertTrue(result["ok"])
        self.assertEqual(77, result["newBuildingId"])
        paths = [path for path, _ in calls]
        self.assertLess(paths.index("/commands/preview-placement"), paths.index("/commands/bulldoze"))
        preview = next(body for path, body in calls if path == "/commands/preview-placement")
        self.assertEqual((2, 90), (preview["ignoreBuildingId"], preview["angleDegrees"]))

    def test_unbuildable_lot_keeps_the_full_facility(self):
        result, calls = self.run_rebuild(lot_valid=False)
        self.assertFalse(result["ok"])
        self.assertNotIn("/commands/bulldoze", [path for path, _ in calls])

    def test_only_full_landfills_and_cemeteries(self):
        with self.assertRaises(RuntimeError):
            self.run_rebuild(building_id=1)  # not full
        with self.assertRaises(ValueError):
            self.run_rebuild(building_id=3)  # a clinic


class WellSiteTests(unittest.TestCase):
    def test_a_polluting_building_keeps_off_a_groundwater_pump(self):
        roads = [{"id": 7, "prefab": "Basic Road", "start": {"x": 0, "z": 0}, "end": {"x": 96, "z": 0}}]

        def request(method, path, query=None, body=None):
            if path == "/state/growables":
                return {"buildings": []}
            if path == "/state/networks":
                return {"segments": roads}
            if path == "/commands/preview-placement":
                if "requireRoadAccess" not in body:
                    return {"footprint": {"width": 64, "length": 64},
                            "environmental": {"groundPollutionAccumulation": 100, "groundPollutionRadius": 150}}
                return {"safety": {"risks": []}}
            raise AssertionError(path)

        with mock.patch.object(mayor_tools, "_request", side_effect=request), \
                mock.patch.object(mayor_tools, "_land_use_context", return_value={"wells": [(48.0, 300.0)]}):
            result = mayor_tools.call_tool("propose_service_sites", {
                "buildingPrefab": "Landfill Site", "near": {"x": 48, "z": 0}})
        # the north side is 259.5m from the pump, the south side 340.5m
        self.assertEqual([{"x": 48.0, "z": -40.5}], [site["position"] for site in result["sites"]])

    def test_the_land_use_context_names_groundwater_pumps_as_wells(self):
        facilities = [{"id": 1, "prefab": "Water Tower", "position": {"x": 10, "z": 20}},
                      {"id": 2, "prefab": "Water Intake", "position": {"x": 90, "z": 90}}]
        prefabs = [{"name": "Water Tower", "aiProfile": {"fields": {"waterIntake": 3750, "useGroundWater": True}}},
                   {"name": "Water Intake", "aiProfile": {"fields": {"waterIntake": 7500, "useGroundWater": False}}}]

        def request(method, path, query=None, body=None):
            return {"/state/growables": {"buildings": []}, "/state/facilities": {"facilities": facilities},
                    "/prefabs/buildings": {"buildings": prefabs}, "/state/networks": {"segments": []},
                    "/state/zone-blocks": {"blocks": []}}[path]

        with mock.patch.object(mayor_tools, "_request", side_effect=request):
            self.assertEqual([(10.0, 20.0)], mayor_tools._land_use_context()["wells"])


if __name__ == "__main__":
    unittest.main()

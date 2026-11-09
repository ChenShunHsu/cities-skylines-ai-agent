import json
import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from loop import city as city_module  # noqa: E402
from loop import cycle, district, growth, industry, metro, play, protocol, ramps, roads, services, traffic, utilities, wake  # noqa: E402


# no test reads the live game: the towns here have no industry and no homes unless a test says so
_ISOLATION = [mock.patch.object(growth, "_industry", return_value=[]),
              mock.patch.object(growth, "_town", return_value=None)]


def setUpModule():
    for patcher in _ISOLATION:
        patcher.start()


def tearDownModule():
    for patcher in _ISOLATION:
        patcher.stop()


class CityProfileTests(unittest.TestCase):
    def test_profile_is_private_per_city_and_keeps_planner_edits(self):
        with tempfile.TemporaryDirectory() as root:
            city = city_module.City({"instanceId": "abc", "name": "Test", "map": "Map"}, Path(root))
            self.assertEqual(Path(root) / "abc", city.dir)
            city.profile["plan"].append({"minX": 0, "maxX": 1, "minZ": 0, "maxZ": 1, "kind": "workplace"})
            city.save_profile()
            again = city_module.City({"instanceId": "abc", "name": "Renamed", "map": "Map"}, Path(root))
            self.assertEqual("workplace", again.plan[0]["kind"])
            self.assertEqual("Renamed", again.profile["identity"]["name"])
            self.assertEqual(4000.0, again.profile["settings"]["reserve"])
            again.save_state("growth", {"a": 1})
            self.assertEqual({"a": 1}, again.load_state("growth", {}))

    def test_the_city_folder_is_ignored_by_git(self):
        ignore = (city_module.REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn(".runtime/", ignore)


class GrowthTests(unittest.TestCase):
    def test_absorbed_blocks_double_what_was_zoned(self):
        category = {"demand": 60, "developableVacantCells": 31}
        self.assertEqual(4, growth.absorbed_allowance(category, {"vacancy": 30, "zonedBlocks": 2}))
        self.assertEqual(12, growth.absorbed_allowance(category, {"vacancy": 30, "zonedBlocks": 9}))
        # vacancy built up: the blocks were not absorbed
        self.assertIsNone(growth.absorbed_allowance({"demand": 60, "developableVacantCells": 60},
                                                    {"vacancy": 30, "zonedBlocks": 2}))
        self.assertIsNone(growth.absorbed_allowance({"demand": 20, "developableVacantCells": 31},
                                                    {"vacancy": 30, "zonedBlocks": 2}))

    def test_stranded_vacancy_gets_blocks_after_waiting(self):
        category = {"demand": 45, "developableVacantCells": 41, "maximumAdditionalBlocksThisWake": 0}
        record: dict = {}
        results = []
        for _ in range(3):
            allowance, record = growth.stale_allowance(category, record)
            results.append(allowance)
        self.assertEqual([None, None, 3], results)

    def test_allowance_is_the_largest_reason(self):
        state = {"absorption": {"workplace": {"vacancy": 30, "zonedBlocks": 6}}, "stale": {}}
        categories = {use: {"demand": 10, "developableVacantCells": 0, "maximumAdditionalBlocksThisWake": 1}
                      for use in growth.USES}
        categories["workplace"] = {"demand": 80, "developableVacantCells": 30, "maximumAdditionalBlocksThisWake": 3}
        self.assertEqual({"residential": 1, "commercial": 1, "workplace": 12}, growth.allowances(categories, state))


class ExpansionTests(unittest.TestCase):
    def area(self, x, clear=True):
        return {"center": {"x": x, "z": 0}, "areaSquareMeters": 90000, "fishbone": {"collectorHeadingDegrees": 90,
                "branchLength": 96}, "access": [{"clear": clear, "crosses": [] if clear else ["water"],
                "distance": 0, "from": {"x": x, "z": 0}, "to": {"x": x, "z": 0}}]}

    def test_land_needing_a_crossing_is_reported_once_then_waits(self):
        with tempfile.TemporaryDirectory() as root:
            city = city_module.City({"instanceId": "t"}, Path(root))
            state = {"wake": 1}
            with mock.patch.object(growth.mt, "call_tool", return_value={"areas": [self.area(0, clear=False)]}):
                first = growth.expand(city, "residential", state, "a")
                state["wake"] = 2
                second = growth.expand(city, "residential", state, "b")
            self.assertEqual("needsCrossing", first["stage"])
            self.assertEqual(("waitingForPlanner", None), (second["stage"], second["ok"]))

    def test_a_way_in_whose_collector_cannot_start_is_not_used_again(self):
        with tempfile.TemporaryDirectory() as root:
            city = city_module.City({"instanceId": "t"}, Path(root))
            state = {"wake": 1}
            areas = {"areas": [self.area(0), self.area(5000)]}
            stuck = {"collector": [{"ok": False}], "branches": [], "collectorNodes": 1, "ended": "preview"}
            with mock.patch.object(growth.mt, "call_tool", return_value=areas), \
                    mock.patch.object(growth.roads, "grow_fishbone", return_value=stuck) as grow:
                growth.expand(city, "residential", state, "a")
                growth.expand(city, "residential", state, "b")
            self.assertEqual([(0, 0), (5000, 0)], [call.args[2] for call in grow.call_args_list])


class FailedWayTests(unittest.TestCase):
    def test_a_way_steered_off_its_line_is_not_taken_again_after_its_start_failed(self):
        way = {"nodeId": 1, "clear": True, "crosses": [], "blocked": 0, "distance": 170,
               "from": {"x": 0, "z": 0}, "to": {"x": 170, "z": 0}}
        area = {"center": {"x": 900, "z": 0}, "areaSquareMeters": 90000,
                "fishbone": {"collectorHeadingDegrees": 90, "branchLength": 96}, "access": [way]}
        stuck = {"collector": [{"ok": False, "stage": "preview"}], "branches": [], "collectorNodes": 1,
                 "ended": "preview"}
        with tempfile.TemporaryDirectory() as root:
            city = city_module.City({"instanceId": "t"}, Path(root))
            state = {"wake": 1}
            # the road was steered and landed 300m off the way's end
            with mock.patch.object(growth.mt, "call_tool", return_value={"areas": [area]}), \
                    mock.patch.object(growth, "open_access", return_value={"ok": True, "end": (170, 300)}) as opened, \
                    mock.patch.object(growth.roads, "grow_fishbone", return_value=stuck):
                growth.expand(city, "residential", state, "a")
                state["wake"] = 2
                second = growth.expand(city, "residential", state, "b")
                state["wake"] = 3
                growth.expand(city, "residential", state, "c")
        self.assertEqual(1, opened.call_count)  # the way is not opened again
        self.assertNotEqual(True, second.get("ok"))
        self.assertEqual(3, len(state["failedStarts"]))  # landing and both ends, once


class ZoningTests(unittest.TestCase):
    def test_paved_frontage_is_zoned_high_density_only_once_unlocked(self):
        proposal = {"candidates": [{"blockId": 1, "roadPrefab": "Basic Road"},
                                   {"blockId": 2, "roadPrefab": "Gravel Road"}]}
        done = {"ok": True, "regressionOk": True, "result": {}}
        for high, expected in ((True, ["CommercialHigh", "CommercialLow"]), (False, ["CommercialLow", "CommercialLow"])):
            with tempfile.TemporaryDirectory() as root:
                city = city_module.City({"instanceId": "t"}, Path(root))
                with mock.patch.object(growth.mt, "call_tool", return_value=proposal), \
                        mock.patch.object(growth.protocol, "run", return_value=done):
                    zoned = growth.zone_blocks(city, "commercial", 2, "a", [], high)
            self.assertEqual(expected, [item["zone"] for item in zoned])


class AccessTests(unittest.TestCase):
    def way(self, node, x):
        return {"nodeId": node, "clear": True, "crosses": [], "distance": 40,
                "from": {"x": x, "z": 0}, "to": {"x": x + 40, "z": 0}}

    def test_the_next_way_in_is_tried_when_one_is_refused(self):
        area = {"center": {"x": 0, "z": 0}, "areaSquareMeters": 90000,
                "fishbone": {"collectorHeadingDegrees": 90, "branchLength": 96},
                "access": [self.way(1, 0), self.way(2, 500)]}
        refused = {"ok": False, "stage": "build", "error": "the settled road failed its checks"}
        built = {"collector": [{"ok": True}], "branches": [], "collectorNodes": 2, "ended": None}
        with tempfile.TemporaryDirectory() as root:
            city = city_module.City({"instanceId": "t"}, Path(root))
            with mock.patch.object(growth.mt, "call_tool", return_value={"areas": [area]}), \
                    mock.patch.object(growth.roads, "build", side_effect=[
                        refused, {"ok": True, "segmentId": 3, "end": {"x": 540, "z": 0}}]), \
                    mock.patch.object(growth.roads, "grow_fishbone", return_value=built) as grow:
                result = growth.expand(city, "residential", {"wake": 1}, "a")
        self.assertTrue(result["ok"])
        self.assertEqual((540, 0), grow.call_args.args[2])

    def test_ways_over_nothing_are_tried_before_ways_over_homes_in_any_area(self):
        near = {"center": {"x": 0, "z": 0}, "areaSquareMeters": 90000,
                "fishbone": {"collectorHeadingDegrees": 90, "branchLength": 96},
                "access": [{**self.way(1, 0), "blocked": 2}]}
        far = {**near, "center": {"x": 900, "z": 0}, "access": [{**self.way(2, 900), "blocked": 0}]}
        built = {"collector": [{"ok": True}], "branches": [], "collectorNodes": 2, "ended": None}
        with tempfile.TemporaryDirectory() as root:
            city = city_module.City({"instanceId": "t"}, Path(root))
            with mock.patch.object(growth.mt, "call_tool", return_value={"areas": [near, far]}), \
                    mock.patch.object(growth.roads, "build",
                                      return_value={"ok": True, "segmentId": 3, "end": {"x": 940, "z": 0}}) as build, \
                    mock.patch.object(growth.roads, "grow_fishbone", return_value=built):
                result = growth.expand(city, "residential", {"wake": 1}, "a")
        self.assertTrue(result["ok"])
        self.assertEqual((900, 0), build.call_args_list[0].args[2])  # the far way, over nothing, first

    def test_a_piece_the_ground_refuses_ends_the_way_when_no_arc_through_its_ends_previews(self):
        refused = {"ok": False, "stage": "preview", "tried": [{"terrainSafe": False}]}
        with mock.patch.object(growth.roads, "build", side_effect=[refused]) as build, \
                mock.patch.object(growth.roads, "preview", return_value=(False, {}, {"valid": False})) as preview:
            opened = growth.open_access(None, "a", self.way(1, 0))
        self.assertFalse(opened["ok"])
        self.assertEqual(1, build.call_count)  # nothing built off the way's line unless all of it previews
        self.assertEqual((40.0, 0.0), build.call_args.args[3])
        # four arcs (two bows, two sides), each refused at its first piece after every lift
        self.assertEqual(4 * len(growth.roads.LIFTS), preview.call_count)

    def test_a_way_the_ground_refuses_straight_is_laid_as_an_arc_through_its_own_ends(self):
        way = {"nodeId": 1, "clear": True, "crosses": [], "distance": 160,
               "from": {"x": 0, "z": 0}, "to": {"x": 160, "z": 0}}
        refused = {"ok": False, "stage": "preview"}
        pieces = []

        def build(label, prefab, start, end, city=None):
            if not label.startswith("expand-a-r"):  # the straight pieces
                return refused
            pieces.append((start, end))
            return {"ok": True, "segmentId": len(pieces), "end": {"x": end[0], "z": end[1]}}
        with mock.patch.object(growth.roads, "build", side_effect=build), \
                mock.patch.object(growth.roads, "preview", return_value=(True, {}, {})), \
                mock.patch.object(growth, "_remove", return_value=[]):
            opened = growth.open_access(None, "a", way)
        self.assertTrue(opened["ok"])
        self.assertEqual((0.15, 1), (opened["curve"]["bulge"], opened["curve"]["side"]))
        self.assertEqual((160, 0), tuple(round(v, 6) for v in opened["end"]))  # the way's own end
        self.assertEqual((0, 0), pieces[0][0])
        self.assertTrue(all(end[1] > 0 for _, end in pieces[:-1]))  # one side: a bow, not a zigzag

    def test_a_way_in_clears_no_homes_and_a_refused_one_leaves_no_stub(self):
        way = {"nodeId": 1, "clear": True, "crosses": [], "distance": 160,
               "from": {"x": 0, "z": 0}, "to": {"x": 160, "z": 0}}
        refused = {"ok": False, "stage": "build", "error": "collided"}
        with mock.patch.object(growth.roads, "build", side_effect=[
                    {"ok": True, "segmentId": 3, "end": {"x": 80, "z": 0}}, refused]) as build, \
                mock.patch.object(growth.roads, "build_through") as through, \
                mock.patch.object(growth, "_remove", return_value=[3]) as remove:
            opened = growth.open_access(None, "a", way)
        through.assert_not_called()  # nothing in the way is bulldozed for a way in
        self.assertEqual([3], remove.call_args.args[1])
        self.assertEqual((False, [3]), (opened["ok"], opened["removed"]))
        self.assertEqual(2, build.call_count)

    def test_homes_in_the_way_are_cleared_but_services_stop_the_road(self):
        homes = {"obstacles": [{"id": 7, "service": "Residential"}]}
        clinic = {"obstacles": [{"id": 8, "service": "HealthCare"}]}
        for info, cleared in ((homes, [7]), (clinic, [])):
            with mock.patch.object(roads, "preview", return_value=(False, {}, info)), \
                    mock.patch.object(roads, "clear_buildings", return_value={"ok": True, "cleared": [7]}) as clear, \
                    mock.patch.object(roads, "build", return_value={"ok": bool(cleared)}):
                outcome = roads.build_through("a", "Basic Road", (0, 0), (40, 0))
            self.assertEqual(bool(cleared), outcome["ok"])
            self.assertEqual([[7]] if cleared else [], [call.args[1] for call in clear.call_args_list])


class FishboneTests(unittest.TestCase):
    def test_a_step_keeps_its_line_or_bends_and_ground_that_refuses_both_ends_the_collector(self):
        refused = {"ok": False, "stage": "preview"}
        with tempfile.TemporaryDirectory() as root:
            city = city_module.City({"instanceId": "t"}, Path(root))
            with mock.patch.object(roads, "build", return_value=refused) as build, \
                    mock.patch.object(roads, "build_leg", return_value={"ok": True}):
                result = roads.grow_fishbone(city, "f", (0.0, 0.0), 0.0, 1)
        self.assertEqual(1, result["collectorNodes"])
        self.assertEqual("preview", result["ended"])
        # the planned line (whole, then its first half), then a bend each way: no other heading or length
        headings = [round(math.degrees(math.atan2(x, -z)), 6) for x, z in
                    (call.args[3] for call in build.call_args_list)]
        self.assertEqual([roads.WIGGLE] * 2 + [roads.BEND] * 2 + [-roads.BEND] * 2, headings)

    def test_a_collector_bends_to_one_side_only_and_no_further_than_max_bend(self):
        def build(label, prefab, start, end, city=None, keep_failed=False):
            heading = math.degrees(math.atan2(end[0] - start[0], -(end[1] - start[1])))
            if heading < 14.0:  # the ground holds the road only once it has turned
                return {"ok": False, "stage": "preview"}
            return {"ok": True, "segmentId": 1, "end": {"x": end[0], "z": end[1]}}
        with tempfile.TemporaryDirectory() as root:
            city = city_module.City({"instanceId": "t"}, Path(root))
            with mock.patch.object(roads, "build", side_effect=build), \
                    mock.patch.object(roads, "build_leg", return_value={"ok": True}):
                result = roads.grow_fishbone(city, "f", (0.0, 0.0), 0.0, 2)
            state = city.load_state("fishbone-f", {})
        self.assertEqual([1, None], [item.get("bend") for item in result["collector"]])
        self.assertEqual(1, state["bend"])
        self.assertAlmostEqual(roads.BEND, state["base"])  # the curve's heading is kept, then wiggled about
        self.assertAlmostEqual(roads.BEND + roads.WIGGLE, state["collector"][2]["heading"])
        # a long refusal: bends keep to the first side until MAX_BEND, then the collector ends
        state = {"collector": [{"node": [0, 0], "heading": 0.0}, {"node": [0, -88], "heading": roads.MAX_BEND}],
                 "branches": [], "base": roads.MAX_BEND, "bend": 1}
        with tempfile.TemporaryDirectory() as root:
            city = city_module.City({"instanceId": "t"}, Path(root))
            city.save_state("fishbone-g", state)
            with mock.patch.object(roads, "build", return_value={"ok": False, "stage": "preview"}) as refused, \
                    mock.patch.object(roads, "build_leg", return_value={"ok": True}):
                result = roads.grow_fishbone(city, "g", (0.0, 0.0), 0.0, 1)
        self.assertEqual("preview", result["ended"])
        self.assertEqual(2, refused.call_count)  # its line only: no bend past 90 degrees, none back

    def test_an_arc_is_built_only_whole_and_a_refused_one_leaves_nothing(self):
        pieces, removed = [], []

        def build(label, prefab, start, end, city=None):
            pieces.append(label)
            if label == "x-r1" and len(pieces) == 2:  # the first arc's second piece fails its checks
                return {"ok": False, "stage": "checks", "removed": True}
            return {"ok": True, "segmentId": len(pieces), "end": {"x": end[0], "z": end[1]}}
        with mock.patch.object(roads, "passable", return_value=True), \
                mock.patch.object(roads, "build", side_effect=build), \
                mock.patch.object(roads.protocol, "run",
                                  side_effect=lambda label, tool, args, *a: removed.append(args["id"]) or {"ok": True}):
            outcome = roads.lay_arc("x", "Basic Road", (0.0, 0.0), (160.0, 0.0))
        self.assertTrue(outcome["ok"])
        self.assertEqual([1], removed)  # the failed arc's first piece, then the next arc whole
        self.assertEqual({"bulge": 0.15, "side": -1}, {k: outcome["curve"][k] for k in ("bulge", "side")})
        self.assertEqual((160.0, 0.0), (outcome["end"]["x"], outcome["end"]["z"]))

    def test_every_arc_tried_is_logged_with_the_piece_that_refused_it(self):
        with tempfile.TemporaryDirectory() as root:
            city = city_module.City({"instanceId": "t"}, Path(root))
            # the first piece of every arc previews, the second never does
            with mock.patch.object(roads, "passable", side_effect=lambda prefab, a, b, label: a == (0.0, 0.0)),                     mock.patch.object(roads, "build") as build:
                outcome = roads.lay_arc("x", "Basic Road", (0.0, 0.0), (160.0, 0.0), city)
            entries = [json.loads(line) for line in (city.dir / "road-arcs.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertFalse(outcome["ok"])
        build.assert_not_called()
        self.assertEqual(1, len(entries))
        self.assertEqual([1, 1, 1, 1], [item["refusedAt"] for item in entries[0]["tried"]])
        self.assertEqual([(0.15, 1), (0.15, -1), (0.3, 1), (0.3, -1)],
                         [(item["bulge"], item["side"]) for item in entries[0]["tried"]])

    def test_a_layout_edge_the_ground_refuses_straight_is_laid_as_an_arc_between_its_nodes(self):
        graph = {"layout": "grid", "entry": "0,0", "nodes": {"0,0": [0.0, 0.0], "1,0": [0.0, -88.0]},
                 "edges": [["0,0", "1,0"]]}
        with tempfile.TemporaryDirectory() as root:
            city = city_module.City({"instanceId": "t"}, Path(root))
            with mock.patch.object(roads, "follow", return_value={"ok": False, "stage": "preview"}), \
                    mock.patch.object(roads, "lay_arc", return_value={
                        "ok": True, "segmentId": 5, "end": {"x": 0.0, "z": -88.0}, "curve": {}}) as arc:
                report = district.grow(city, "d", graph, 4, "d")
        self.assertEqual(["0,0|1,0"], report["built"])
        self.assertEqual(["0,0|1,0"], report["curved"])
        self.assertEqual(((0.0, 0.0), (0.0, -88.0)), arc.call_args.args[2:4])  # through the layout's own nodes

    def test_an_arc_bows_to_one_side_in_equal_pieces_and_ends_on_the_way_end(self):
        for start, end in (((0.0, 0.0), (300.0, 0.0)), ((50.0, 20.0), (-90.0, 260.0))):
            for bulge in roads.ARC_BULGES:
                for side in (1, -1):
                    points = roads.arc_points(start, end, bulge, side)
                    ends = [start] + points
                    self.assertEqual(end, points[-1])
                    lengths = [math.dist(a, b) for a, b in zip(ends, ends[1:])]
                    self.assertLessEqual(max(lengths), roads.SPACING + 1e-6)
                    self.assertAlmostEqual(min(lengths), max(lengths), places=6)
                    chord = math.dist(start, end)
                    ux, uz = (end[0] - start[0]) / chord, (end[1] - start[1]) / chord
                    offsets = [(p[0] - start[0]) * -uz + (p[1] - start[1]) * ux for p in points[:-1]]
                    self.assertTrue(all(side * offset > 0 for offset in offsets))  # every joint on one side
                    self.assertAlmostEqual(bulge * chord, max(abs(o) for o in offsets), delta=bulge * chord * 0.05)

    def test_ground_bulging_under_a_whole_step_takes_two_half_pieces(self):
        refused = {"ok": False, "stage": "preview"}
        singles = len(roads.step_ends((0.0, 0.0), 0.0))
        half = {"ok": True, "segmentId": 9, "end": {"x": 0, "z": -44}}
        whole = {"ok": True, "segmentId": 10, "end": {"x": 0, "z": -88}}
        with tempfile.TemporaryDirectory() as root:
            city = city_module.City({"instanceId": "t"}, Path(root))
            with mock.patch.object(roads, "build", side_effect=[refused] * singles + [half, whole]), \
                    mock.patch.object(roads, "build_leg", return_value={"ok": True}):
                result = roads.grow_fishbone(city, "f", (0.0, 0.0), 0.0, 1)
            node = city.load_state("fishbone-f", {})["collector"][-1]["node"]
        self.assertEqual(2, result["collectorNodes"])
        self.assertEqual([0, -88], node)


class NewCityTests(unittest.TestCase):
    stubs = [{"nodeId": 1, "point": (0.0, 0.0), "inward": (-1.0, 0.0)},
             {"nodeId": 2, "point": (0.0, 30.0), "inward": (-1.0, 0.0)},
             {"nodeId": 3, "point": (2000.0, 0.0), "inward": (1.0, 0.0)}]

    def test_a_stub_town_roads_already_run_on_from_is_still_a_stub(self):
        def segment(sid, prefab, a, b, na, nb):
            return {"id": sid, "prefab": prefab, "startNodeId": na, "endNodeId": nb,
                    "start": {"x": a[0], "z": a[1]}, "end": {"x": b[0], "z": b[1]}}
        network = {"segments": [segment(1, "Highway", (0, -100), (0, 0), 10, 11),
                                {**segment(2, "Highway", (0, -200), (0, -100), 12, 10), "startOutside": True},
                                segment(3, "Basic Road", (0, 0), (0, 60), 11, 13)]}
        with mock.patch.object(growth.mt, "_request", return_value=network):
            stubs = growth.highway_stubs()
        self.assertEqual([11], [stub["nodeId"] for stub in stubs])
        self.assertEqual((0.0, 1.0), stubs[0]["inward"])

    def test_exit_and_entrance_stubs_pair_and_a_lone_stub_is_left_out(self):
        pairs = growth.stub_pairs(self.stubs)
        self.assertEqual([[1, 2]], [[stub["nodeId"] for stub in pair] for pair in pairs])

    def outside(self, builds, local=([], set(), set())):
        removed = []
        with mock.patch.object(growth, "_local_roads", return_value=local), \
                mock.patch.object(growth, "highway_stubs", return_value=self.stubs), \
                mock.patch.object(roads, "preview", return_value=(True, {}, {})), \
                mock.patch.object(roads, "can_step", return_value=True), \
                mock.patch.object(roads, "build", side_effect=builds), \
                mock.patch.object(growth, "_remove", side_effect=lambda label, ids: removed.extend(ids) or ids):
            state: dict = {}
            result = growth.ensure_outside(None, "a", state)
        return result, state, removed

    def test_an_attempt_that_does_not_settle_is_removed_and_the_next_spot_tried(self):
        road = lambda segment, ok=True: {"ok": ok, "segmentId": segment, "end": {"x": -60.0, "z": 15.0}}  # noqa: E731
        # an attempt runs each stub on into the map and joins the two ends: three roads
        result, state, removed = self.outside([road(10), road(11), road(12, ok=False),
                                               road(14), road(15), road(16)])
        self.assertTrue(result["ok"])
        self.assertEqual([10, 11, 12], removed)  # the failed attempt's roads, all of them
        self.assertEqual({"complete": True, "roads": [14, 15, 16]},
                         {k: state["outside"][k] for k in ("complete", "roads")})

    def test_a_road_left_on_one_stub_is_cleared_and_a_connected_town_is_left_alone(self):
        leftover = [{"id": 7, "startNodeId": 1, "endNodeId": 50, "startNodeDegree": 2, "endNodeDegree": 1,
                     "start": {"x": 0.0, "z": 0.0}, "end": {"x": -40.0, "z": 0.0}}]
        road = lambda segment: {"ok": True, "segmentId": segment, "end": {"x": -60.0, "z": 15.0}}  # noqa: E731
        _, _, removed = self.outside([road(12), road(13), road(14), road(15)], (leftover, {1, 2}, {7}))
        self.assertEqual(7, removed[0])
        town = leftover + [{"id": 8, "startNodeId": 50, "endNodeId": 51, "startNodeDegree": 2, "endNodeDegree": 1,
                            "start": {"x": -40.0, "z": 0.0}, "end": {"x": -80.0, "z": 0.0}}]
        result, state, removed = self.outside([], (town, {1, 2}, {7, 8}))
        self.assertEqual((None, {"complete": True}, []), (result, state["outside"], removed))

    def test_roads_already_run_on_from_both_stubs_are_joined_not_removed(self):
        run_on = [{"id": 7, "startNodeId": 1, "endNodeId": 50, "startNodeDegree": 2, "endNodeDegree": 1,
                   "start": {"x": 0.0, "z": 0.0}, "end": {"x": -40.0, "z": 0.0}},
                  {"id": 8, "startNodeId": 60, "endNodeId": 2, "startNodeDegree": 1, "endNodeDegree": 2,
                   "start": {"x": -40.0, "z": 30.0}, "end": {"x": 0.0, "z": 30.0}}]
        road = lambda segment: {"ok": True, "segmentId": segment, "end": {"x": -80.0, "z": 15.0}}  # noqa: E731
        result, state, removed = self.outside([road(12), road(13)], (run_on, {1, 2}, set()))
        self.assertTrue(result["ok"])
        self.assertEqual([], removed)
        self.assertEqual([7, 8, 12, 13], state["outside"]["roads"])
        self.assertEqual(2, result["roads"] - 2)  # two new roads: one from each end to the junction

    def test_once_the_attempts_are_spent_nothing_more_is_built(self):
        state = {"outside": {"failedAttempts": growth.OUTSIDE_FAILURES, "gatePolicy": growth.GATE_POLICY}}
        with mock.patch.object(growth, "_local_roads", return_value=([], set(), set())), \
                mock.patch.object(roads, "build") as build:
            result = growth.ensure_outside(None, "a", state)
        self.assertEqual((False, "attemptsSpent"), (result["ok"], result["reason"]))
        build.assert_not_called()

    def test_attempts_spent_under_other_road_gates_count_for_nothing(self):
        road = lambda segment: {"ok": True, "segmentId": segment, "end": {"x": -60.0, "z": 15.0}}  # noqa: E731
        with mock.patch.object(growth, "_local_roads", return_value=([], set(), set())), \
                mock.patch.object(growth, "highway_stubs", return_value=self.stubs), \
                mock.patch.object(roads, "preview", return_value=(True, {}, {})), \
                mock.patch.object(roads, "can_step", return_value=True), \
                mock.patch.object(roads, "build", side_effect=[road(1), road(2), road(3), road(4)]):
            state = {"outside": {"failedAttempts": growth.OUTSIDE_FAILURES, "gatePolicy": growth.GATE_POLICY - 1}}
            result = growth.ensure_outside(None, "a", state)
        self.assertTrue(result["ok"])

    def test_nothing_is_built_unless_every_road_of_an_attempt_previews(self):
        # the last road of each attempt (to the junction) is refused: no attempt starts
        refused_last = lambda prefab, a, b, label, lift=0.0: (abs(b[1] - 15.0) > 1e-6 or a[0] > -1, {}, {})  # noqa: E731
        with mock.patch.object(growth, "_local_roads", return_value=([], set(), set())), \
                mock.patch.object(growth, "highway_stubs", return_value=self.stubs[:2]), \
                mock.patch.object(growth, "_continuation",
                                  return_value=([((0.0, 0.0), (-60.0, 0.0)), ((0.0, 30.0), (-60.0, 30.0)),
                                                 ((-60.0, 0.0), (-80.0, 15.0)), ((-60.0, 30.0), (-80.0, 15.0))],
                                                (-80.0, 15.0), -90.0)), \
                mock.patch.object(growth, "_junction_spots", return_value=([], -90.0)), \
                mock.patch.object(roads, "preview", side_effect=refused_last), \
                mock.patch.object(roads, "can_step", return_value=True), \
                mock.patch.object(roads, "build") as build:
            result = growth.ensure_outside(None, "a", {})
        self.assertFalse(result["ok"])
        build.assert_not_called()

    def test_the_first_collector_grows_from_the_junction(self):
        area = {"center": {"x": 0, "z": 0}, "areaSquareMeters": 900000, "access": [], "fishbone": None}
        built = {"collector": [{"ok": True}], "branches": [], "collectorNodes": 2, "ended": None}
        state = {"wake": 1, "outside": {"complete": True, "junction": [800.0, 690.0], "heading": -90.0}}
        with tempfile.TemporaryDirectory() as root:
            city = city_module.City({"instanceId": "t"}, Path(root))
            with mock.patch.object(growth.mt, "call_tool", return_value={"areas": [area]}), \
                    mock.patch.object(growth.roads, "grow_fishbone", return_value=built) as grow:
                result = growth.expand(city, "residential", state, "a")
        self.assertTrue(result["ok"])
        self.assertEqual(((800.0, 690.0), -90.0), (grow.call_args.args[2], grow.call_args.args[3]))

    def test_land_the_plan_asks_for_is_reached_over_the_water_as_a_bridgehead(self):
        dry = {"center": {"x": -500, "z": -500}, "areaSquareMeters": 300000, "fishbone": {"collectorHeadingDegrees": 0.0}, "points": [[0, 0]],
               "access": [{"clear": True, "crosses": [], "distance": 30, "from": {}, "to": {}}]}
        far = {"center": {"x": 50, "z": 512}, "areaSquareMeters": 600000, "fishbone": {"collectorHeadingDegrees": 0.0}, "points": [[50, 512]],
               "access": [{"clear": True, "crosses": ["water"], "distance": 700, "from": {}, "to": {}}]}
        state = {"wake": 5, "expansions": ["expansion-1"],
                 "expansion": {"use": "residential", "ended": "done", "start": [0, 0], "collectorNodes": 6}}
        built = {"built": ["0,0|1,0"], "failed": [], "ended": False, "nodesBuilt": 2, "edgesLeft": 11}
        crossing = {"ok": True, "landing": [341.0, 63.0], "headingDegrees": 142.0, "roads": [1, 2], "cost": 9480,
                    "span": 416, "approach": 240}
        with tempfile.TemporaryDirectory() as root:
            city = city_module.City({"instanceId": "t"}, Path(root))
            city.profile["plan"] = [{"minX": 0, "maxX": 900, "minZ": 0, "maxZ": 900, "kind": "residential",
                                     "crossTo": True}]
            with mock.patch.object(growth.mt, "call_tool", side_effect=lambda name, *a: {
                        "areas": [dry, far]} if name == "propose_expansion_areas" else {"cash": {"amount": 4000000}}), \
                    mock.patch.object(growth.bridge, "cross", return_value=crossing) as cross, \
                    mock.patch.object(growth, "open_access") as dry_way, \
                    mock.patch.object(growth.district, "grow", return_value=built) as lay:
                result = growth.expand(city, "residential", state, "a")
        self.assertEqual((50, 512), cross.call_args.args[1])
        dry_way.assert_not_called()
        self.assertEqual(("bridgehead", (341.0, 63.0)), (result["layout"], tuple(state["expansion"]["start"])))
        self.assertEqual("bridgehead", lay.call_args.args[2]["layout"])

    def test_a_crossing_waiting_for_money_leaves_the_town_growing_a_little_on_this_side(self):
        dry = {"center": {"x": -500, "z": -500}, "areaSquareMeters": 300000, "points": [[-500, -500]],
               "fishbone": {"collectorHeadingDegrees": 0.0, "branchLength": 96.0},
               "access": [{"clear": True, "crosses": [], "distance": 30, "from": {}, "to": {}}]}
        far = {"center": {"x": 50, "z": 512}, "areaSquareMeters": 600000, "points": [[50, 512]],
               "fishbone": {"collectorHeadingDegrees": 0.0, "branchLength": 96.0},
               "access": [{"clear": True, "crosses": ["water"], "distance": 700, "from": {}, "to": {}}]}
        state = {"wake": 5, "expansions": ["expansion-1"],
                 "expansion": {"use": "residential", "ended": "done", "start": [0, 0], "collectorNodes": 6}}
        grown = {"collector": [{"ok": True}], "branches": [], "collectorNodes": 2, "ended": None}
        with tempfile.TemporaryDirectory() as root:
            city = city_module.City({"instanceId": "t"}, Path(root))
            city.profile["plan"] = [{"minX": 0, "maxX": 900, "minZ": 0, "maxZ": 900, "crossTo": True}]
            with mock.patch.object(growth.mt, "call_tool", side_effect=lambda name, *a: {
                        "areas": [dry, far]} if name == "propose_expansion_areas" else {"cash": {"amount": 900000}}), \
                    mock.patch.object(growth.bridge, "cross", return_value={"ok": None, "stage": "waitingForCash"}), \
                    mock.patch.object(growth, "open_access", return_value={"ok": True, "end": (-480.0, -480.0)}), \
                    mock.patch.object(growth.roads, "grow_fishbone", return_value=grown) as fishbone:
                result = growth.expand(city, "residential", state, "a", small=True)
        self.assertTrue(result["ok"])
        self.assertEqual(1, fishbone.call_args.args[4])  # one collector step while cash is short

    def test_land_that_suits_another_layout_is_built_from_that_layout(self):
        area = {"center": {"x": 0, "z": 0}, "areaSquareMeters": 300000, "access": [], "fishbone": None,
                "points": [[0, 0]], "layouts": [{"layout": "ring", "reason": "sloping land"}]}
        state = {"wake": 1, "outside": {"complete": True, "junction": [800.0, 690.0], "heading": -90.0}}
        built = {"built": ["e|a"], "failed": [], "ended": False, "nodesBuilt": 2, "edgesLeft": 9}
        with tempfile.TemporaryDirectory() as root:
            city = city_module.City({"instanceId": "t"}, Path(root))
            with mock.patch.object(growth.mt, "call_tool", return_value={"areas": [area]}), \
                    mock.patch.object(growth.layouts, "graph", return_value={"layout": "ring"}) as graph, \
                    mock.patch.object(growth.district, "grow", return_value=built) as lay, \
                    mock.patch.object(growth.roads, "grow_fishbone") as fishbone:
                first = growth.expand(city, "residential", state, "a")
                later = growth.expand(city, "residential", state, "b")
        self.assertEqual(("ring", True, "ring"), (first["layout"], first["ok"], later["layout"]))
        self.assertEqual("ring", graph.call_args.args[0])
        self.assertIsNone(lay.call_args_list[1].args[2])  # a later wake continues the district's own graph
        fishbone.assert_not_called()


class RoadBuildTests(unittest.TestCase):
    def test_a_road_ending_on_an_existing_node_is_not_tried_with_lifts(self):
        refused = (False, {}, {"valid": False, "reusedEnd": True, "settledMinAbove": -0.6})
        with mock.patch.object(roads, "preview", return_value=refused) as preview:
            outcome = roads.build("a", "Basic Road", (0, 0), (40, 0))
        self.assertEqual(("preview", 1), (outcome["stage"], preview.call_count))
        self.assertEqual(-0.6, outcome["tried"][0]["settledMinAbove"])


class DryFacilityTests(unittest.TestCase):
    @staticmethod
    def standing(intake_source, problems=""):
        return {"intake": {"buildingPrefab": "Water Intake", "buildingId": 1, "position": {"x": 0, "z": 0},
                           "angleDegrees": 0, "waterSource": intake_source, "active": True, "problems": problems},
                "outlet": {"buildingPrefab": "Water Outlet", "buildingId": 2, "position": {"x": 9, "z": 9},
                           "angleDegrees": 0, "waterSource": 0, "active": True, "problems": ""}}

    def repair(self, standing, wakes=2):
        summary = {"missingWater": 7, "missingSewage": 7}
        with tempfile.TemporaryDirectory() as root:
            city = city_module.City({"instanceId": "t"}, Path(root))
            with mock.patch.object(utilities, "_water_facilities", return_value=standing), \
                    mock.patch.object(utilities, "water_pair", return_value=(None, None)), \
                    mock.patch.object(utilities.mt, "call_tool", return_value={"summary": summary}), \
                    mock.patch.object(utilities.protocol, "run", return_value={"ok": True}) as run:
                for _ in range(wakes):
                    report = utilities.repair_water("a", city)
            return report, run, city.load_state("water-bootstrap", {})

    def test_an_intake_placed_this_wake_is_not_yet_judged(self):
        # it has not pumped, so holds no source: only a second wake without one shows it dry
        report, run, _ = self.repair(self.standing(0), wakes=1)
        self.assertNotIn("relocating", report)
        run.assert_not_called()

    def test_an_intake_the_river_left_is_moved_not_piped_around(self):
        report, run, state = self.repair(self.standing(0))
        self.assertEqual([{"role": "intake", "buildingId": 1, "removed": True}], report["relocating"])
        self.assertEqual(("bulldoze_entity", 1), (run.call_args.args[1], run.call_args.args[2]["id"]))
        self.assertEqual(["intake"], state["relocate"])  # the next wake designs a partner for the outlet

    def test_a_pumping_intake_an_unpowered_one_and_any_outlet_stay(self):
        # homes beyond the pipes are missing water: that is for pipes, not for moving the river works
        for standing in (self.standing(4), self.standing(0, "Electricity")):
            report, run, _ = self.repair(standing)
            self.assertNotIn("relocating", report)
            run.assert_not_called()


class ShortfallTests(unittest.TestCase):
    def test_three_dark_buildings_the_grid_reaches_mean_too_little_power(self):
        for covered, expected in (([{"id": 1}, {"id": 2}, {"id": 3}], True), ([{"id": 1}, {"id": 2}], False),
                                  ([{"id": 1}], False), ([], False)):
            with mock.patch.object(utilities.mt, "call_tool", return_value={"coveredButShort": covered}):
                self.assertEqual(expected, utilities.starved())

    def test_power_lines_go_only_to_buildings_no_powered_ground_reaches(self):
        coverage = {"summary": {}, "buildings": [{"id": 7}, {"id": 8}, {"id": 9}]}
        diagnosis = {"coveredButShort": [{"id": 7}, {"id": 8}, {"id": 9}], "notCovered": [], "verdict": ["supplyShort"]}
        with mock.patch.object(utilities.mt, "call_tool", side_effect=lambda name, args: diagnosis
                               if name == "diagnose_utility_shortfall" else coverage) as call, \
                mock.patch.object(utilities.protocol, "run") as run:
            report = utilities.repair_power("a")
        run.assert_not_called()  # the grid reaches all three: they wait for a plant, not a line
        self.assertNotIn("propose_power_line_connection", [c.args[0] for c in call.call_args_list])
        self.assertEqual(["supplyShort"], report["diagnosis"])

    def test_one_building_dark_while_the_grid_has_power_to_spare_gets_a_line(self):
        coverage = {"summary": {}, "buildings": [{"id": 46981, "position": {"x": 2400, "z": -2236}}]}
        for use, lined in ((0.44, True), (0.9, False)):
            diagnosis = {"coveredButShort": [{"id": 46981}], "notCovered": [], "verdict": ["supplyShort"],
                         "supply": {"electricity": {"utilization": use}}}
            asked = []

            def tool(name, args):
                if name == "diagnose_utility_shortfall":
                    return diagnosis
                if name == "propose_power_line_connection":
                    asked.append(args["buildingId"])
                    return {"actionRequired": True, "safe": True, "candidate": {
                        "powerLinePrefab": "Power Line", "start": {"x": 2319, "z": -2202}, "end": {"x": 2375, "z": -2220}}}
                if name == "propose_minimal_power_grid":
                    return {"segments": []}
                return coverage
            with mock.patch.object(utilities.mt, "call_tool", side_effect=tool), \
                    mock.patch.object(utilities.mt, "_request", return_value={"facilities": []}), \
                    mock.patch.object(utilities, "place_plant", return_value={"ok": False}), \
                    mock.patch.object(utilities.protocol, "run", return_value={"ok": True, "regressionOk": True}):
                utilities.repair_power("a")
            self.assertEqual([46981] if lined else [], asked)

    def test_water_repair_that_ran_out_of_dry_buildings_says_it_caught_up(self):
        dry = [{"id": 1, "position": {"x": 0, "z": 0}}]
        coverage = iter([{"summary": {"missingWater": 1, "missingSewage": 1}, "buildings": dry},
                         {"summary": {}, "buildings": dry}, {"summary": {}, "buildings": []},
                         {"summary": {"missingWater": 2, "missingSewage": 2}, "buildings": []}])

        def tool(name, args):
            if name == "diagnose_utility_shortfall":
                return {"coveredButShort": [], "notCovered": [{"id": 1}], "verdict": ["notCovered"]}
            if name == "propose_water_service_connection":
                return {"safe": True, "planId": "p", "segments": [], "targetBuildings": [{"id": 1}],
                        "sourceNode": {"id": 5}}
            return next(coverage)

        with tempfile.TemporaryDirectory() as root:
            city = city_module.City({"instanceId": "t"}, Path(root))
            with mock.patch.object(utilities, "_water_facilities", return_value={}),                     mock.patch.object(utilities, "water_pair", return_value=(1, 2)),                     mock.patch.object(utilities.mt, "call_tool", side_effect=tool),                     mock.patch.object(utilities.protocol, "run", return_value={"ok": True, "regressionOk": True}):
                report = utilities.repair_water("a", city)
        self.assertEqual(1, len(report["steps"]))
        self.assertTrue(report["caughtUp"])
        self.assertEqual(2, report["summary"]["missingWater"])  # homes grown since: the next wake's

    def test_dry_buildings_hidden_behind_polluted_served_ones_are_still_piped(self):
        served = [{"id": 9, "position": {"x": 0, "z": 0}}]  # a trace of pollution fills the listing
        coverage = iter([{"summary": {"missingWater": 1, "missingSewage": 1}, "buildings": served}] +
                        [{"summary": {}, "buildings": served}] * 3)
        targets = []

        def tool(name, args):
            if name == "diagnose_utility_shortfall":
                return {"coveredButShort": [], "notCovered": [{"id": 1}], "verdict": ["notCovered"]}
            if name == "propose_water_service_connection":
                targets.append(args["targetBuildingIds"])
                return {"safe": True, "planId": "p", "segments": [], "targetBuildings": [{"id": 1}],
                        "sourceNode": {"id": 5}}
            return next(coverage)

        with tempfile.TemporaryDirectory() as root:
            city = city_module.City({"instanceId": "t"}, Path(root))
            with mock.patch.object(utilities, "_water_facilities", return_value={}), \
                    mock.patch.object(utilities, "water_pair", return_value=(1, 2)), \
                    mock.patch.object(utilities.mt, "call_tool", side_effect=tool), \
                    mock.patch.object(utilities.protocol, "run", return_value={"ok": True, "regressionOk": True}):
                report = utilities.repair_water("a", city)
        self.assertEqual([[1]], targets)
        self.assertEqual(1, len(report["steps"]))
        self.assertTrue(report["caughtUp"])

    def test_a_short_water_supply_gets_its_trimmed_budget_back_before_any_pipe(self):
        summary = {"missingWater": 9, "missingSewage": 0}
        diagnosis = {"coveredButShort": [{"id": 1}, {"id": 2}], "notCovered": [], "verdict": ["supplyShort"]}

        def tool(name, args):
            return diagnosis if name == "diagnose_utility_shortfall" else {"summary": summary, "buildings": []}

        with tempfile.TemporaryDirectory() as root:
            city = city_module.City({"instanceId": "t"}, Path(root))
            with mock.patch.object(utilities, "_water_facilities", return_value={}), \
                    mock.patch.object(utilities, "water_pair", return_value=(1, 2)), \
                    mock.patch.object(utilities, "budget_of", return_value=50), \
                    mock.patch.object(utilities, "set_budget", return_value={"budget": "Water", "ok": True}) as budget, \
                    mock.patch.object(utilities.mt, "call_tool", side_effect=tool), \
                    mock.patch.object(utilities.protocol, "run") as run:
                report = utilities.repair_water("a", city)
        self.assertEqual(("Water", 100), (budget.call_args.args[1], budget.call_args.args[2]))
        run.assert_not_called()
        self.assertEqual([], report["steps"])


class RejoinTests(unittest.TestCase):
    @staticmethod
    def pipe(id, a, b, start, end):
        return {"id": id, "startNodeId": a, "endNodeId": b, "start": {"x": start[0], "z": start[1]},
                "end": {"x": end[0], "z": end[1]}}

    def test_a_new_intake_joins_the_pipes_laid_from_the_outlet_and_only_new_pipes_are_built(self):
        # outlet port (0,0) -> junction (100,0) -> town (100,150); the dead branch of the old intake
        # runs from the junction to (250,0); the new intake's port is at (400,40)
        live = [self.pipe(1, 1, 2, (0, 0), (100, 0)), self.pipe(2, 2, 3, (100, 0), (100, 150)),
                self.pipe(3, 2, 4, (100, 0), (250, 0))]
        with mock.patch.object(utilities.mt, "_request", return_value={"segments": live}):
            designs = utilities.rejoin_designs("intake", (400.0, 40.0), (0.0, 0.0))
        joint, segments, laid = designs[0]
        self.assertEqual((250, 0), joint)  # the old branch's end, nearest the new port
        self.assertEqual(3, laid)  # the outlet branch, the way to the joint and a third pipe
        self.assertEqual(["outletBranch", "sharedTrunk", "sharedTrunk"], [s["role"] for s in segments[:laid]])
        self.assertEqual("intakeBranch", segments[-1]["role"])
        self.assertEqual({"x": 400.0, "z": 40.0}, segments[-1]["end"])
        self.assertTrue(all(s["role"] == "sharedTrunk" for s in segments[laid:-1]))

    def test_a_port_near_a_joint_is_reached_by_its_branch_alone_and_never_by_a_stub(self):
        live = [self.pipe(1, 1, 2, (0, 0), (100, 0)), self.pipe(2, 2, 3, (100, 0), (100, 150)),
                self.pipe(3, 2, 4, (100, 0), (250, 0))]
        with mock.patch.object(utilities.mt, "_request", return_value={"segments": live}):
            designs = utilities.rejoin_designs("intake", (300.0, 20.0), (0.0, 0.0))
            near = utilities.rejoin_designs("intake", (255.0, 0.0), (0.0, 0.0))
        joint, segments, laid = designs[0]
        self.assertEqual([("intakeBranch", {"x": 250, "z": 0}, {"x": 300.0, "z": 20.0})],
                         [(s["role"], s["start"], s["end"]) for s in segments[laid:]])
        self.assertNotIn((250, 0), [design[0] for design in near])  # 5m from the port

    def test_a_pipe_already_ending_at_the_new_port_is_its_branch_and_nothing_is_laid(self):
        # the old intake's branch still reaches the port where the new one stands
        live = [self.pipe(1, 1, 2, (0, 0), (100, 0)), self.pipe(2, 2, 3, (100, 0), (100, 150)),
                self.pipe(3, 2, 4, (100, 0), (250, 0)), self.pipe(4, 4, 5, (250, 0), (300, 20))]
        with mock.patch.object(utilities.mt, "_request", return_value={"segments": live}):
            designs = utilities.rejoin_designs("intake", (300.5, 20.0), (0.0, 0.0))
        joint, segments, laid = designs[0]
        self.assertEqual((250, 0), joint)
        self.assertEqual(len(segments), laid)
        self.assertEqual(("intakeBranch", {"x": 250, "z": 0}, {"x": 300, "z": 20}),
                         (segments[-1]["role"], segments[-1]["start"], segments[-1]["end"]))
        self.assertEqual(["outletBranch", "sharedTrunk", "sharedTrunk"], [s["role"] for s in segments[:-1]])

    def test_no_rejoin_when_the_kept_facility_has_no_pipe(self):
        live = [self.pipe(1, 1, 2, (500, 500), (600, 500))]
        with mock.patch.object(utilities.mt, "_request", return_value={"segments": live}):
            self.assertEqual([], utilities.rejoin_designs("intake", (400.0, 40.0), (0.0, 0.0)))

    def test_abandoning_a_rejoin_removes_only_the_pipes_it_built(self):
        plan = {"segments": [{"start": {"x": 0, "z": 0}, "end": {"x": 100, "z": 0}},
                             {"start": {"x": 100, "z": 0}, "end": {"x": 200, "z": 0}}]}
        live = [self.pipe(1, 1, 2, (0, 0), (100, 0)), self.pipe(2, 2, 3, (100, 0), (200, 0))]
        with mock.patch.object(utilities.mt, "_request", return_value={"segments": live}):
            self.assertEqual([2], utilities._built_pipes(plan, 1))


class BudgetTests(unittest.TestCase):
    def trim(self, net, power_used=2560, power_upkeep=10712):
        health = {"cityHealth": {"cash": {"periodNet": net}, "utilities": {
            "electricity": {"used": power_used, "capacity": 8000}, "water": {"used": 2848, "capacity": 120000}}}}
        economy = {"services": [{"service": "Electricity", "expenses": power_upkeep},
                                {"service": "Water", "expenses": 57269}]}
        with mock.patch.object(utilities.mt, "call_tool",
                               side_effect=lambda name, *a: economy if name == "inspect_economy" else health), \
                mock.patch.object(utilities.mt, "city_health", return_value=health["cityHealth"]), \
                mock.patch.object(utilities, "budget_of", return_value=100), \
                mock.patch.object(utilities, "set_budget", side_effect=lambda label, service, value, *a: (service, value)):
            return utilities.trim_budgets("a")

    def test_idle_water_is_turned_down_while_money_runs_out_and_busy_power_is_not(self):
        # water is 2% used: the game's floor of 50%; power at 32% would run near the headroom trimmed
        self.assertEqual([("Water", 50)], self.trim(-25000))

    def test_an_idle_grid_costing_the_most_is_trimmed_first_with_a_wide_margin(self):
        # power at 20.5% of its capacity, the costliest upkeep: 60% keeps its use under 35% of it
        self.assertEqual([("Electricity", 60)], self.trim(-25000, power_used=1640, power_upkeep=90000))

    def balance(self, net, demand, rates):
        snapshot = {"cityHealth": {"cash": {"periodNet": net}}, "summary": {"demand": {"residential": demand}}}
        with mock.patch.object(utilities, "trim_budgets", return_value=[]), \
                mock.patch.object(utilities, "lower_raised_budgets", return_value=[]), \
                mock.patch.object(utilities.mt, "call_tool", side_effect=AssertionError("no snapshot read")), \
                mock.patch.object(utilities.mt, "city_health", return_value=snapshot["cityHealth"]), \
                mock.patch.object(utilities.mt, "_request",
                                  side_effect=lambda method, path, **_: {"/state/summary": snapshot["summary"]}[path]), \
                mock.patch.object(utilities, "tax_rate", side_effect=lambda service: rates[service]), \
                mock.patch.object(utilities.protocol, "run", return_value={"ok": True, "regressionOk": True}) as run:
            actions = utilities.balance("a")
        return actions, [call.args[2] for call in run.call_args_list]

    def test_a_deficit_with_homes_wanted_raises_the_lower_tax_a_point(self):
        actions, calls = self.balance(-1300, 80, {"Residential": 10, "Commercial": 9})
        self.assertEqual([("Commercial", 10)], [(call["service"], call["rate"]) for call in calls])

    def test_taxes_stay_once_homes_are_no_longer_wanted_or_at_the_ceiling(self):
        self.assertEqual([], self.balance(-1300, 40, {"Residential": 9, "Commercial": 9})[1])
        self.assertEqual([], self.balance(-1300, 80, {"Residential": 12, "Commercial": 12})[1])
        self.assertEqual([], self.balance(500, 80, {"Residential": 9, "Commercial": 9})[1])

    def test_budgets_stay_while_the_city_makes_money(self):
        self.assertEqual([], self.trim(1000))

    def test_a_plant_is_sought_when_the_cooldown_keeps_a_trimmed_budget_down(self):
        observed = {"cityHealth": {"utilities": {"electricity": {"capacity": 8000, "utilization": 1.25}}}}
        calls = []

        def call_tool(name, *args):
            calls.append(name)
            return {"options": []} if name == "compare_service_options" else observed

        with mock.patch.object(utilities.mt, "call_tool", side_effect=call_tool), \
                mock.patch.object(utilities.mt, "city_health", return_value=observed["cityHealth"]), \
                mock.patch.object(utilities, "budget_of", return_value=70), \
                mock.patch.object(utilities, "town_center", return_value=(0.0, 0.0)), \
                mock.patch.object(utilities, "set_budget", return_value={"ok": None, "stage": "cooldown"}), \
                mock.patch.object(utilities, "cash_display", return_value=100000.0), \
                mock.patch.object(utilities, "industry_anchors", return_value=[]), \
                mock.patch.object(utilities, "variable_yield", return_value=0.6):
            utilities.add_power("a", ["electricity"])
        self.assertIn("compare_service_options", calls)


class OpenDistrictTests(unittest.TestCase):
    def test_industry_is_not_laid_on_a_residential_district(self):
        state = {"wake": 3, "expansions": ["expansion-1"],
                 "expansion": {"name": "expansion-1", "use": "residential", "start": [0, 0], "ended": None,
                               "collectorNodes": 6}}
        with tempfile.TemporaryDirectory() as root:
            city = city_module.City({"instanceId": "t"}, Path(root))
            with mock.patch.object(growth.mt, "call_tool", return_value={"areas": []}) as call:
                growth.expand(city, "workplace", state, "a")
        self.assertEqual("otherUseWanted", state["expansion"]["ended"])
        self.assertEqual("workplace", call.call_args[0][1]["use"])  # new land is sought for industry

    def grow(self, zoned_ok, hold_land=None):
        categories = {use: {"demand": 100, "developableVacantCells": 0} for use in growth.USES}
        with tempfile.TemporaryDirectory() as root:
            city = city_module.City({"instanceId": "t"}, Path(root))
            city.save_state("growth", {"absorption": {}, "stale": {}, "outside": {"complete": True},
                                       "expansion": {"name": "expansion-1", "use": "residential", "ended": None}})
            with mock.patch.object(growth.mt, "call_tool", return_value={"growthPacing": {"categories": categories}}), \
                mock.patch.object(growth, "allowances", return_value={"residential": 4, "commercial": 0, "workplace": 0}), \
                mock.patch.object(growth, "zone_blocks", side_effect=lambda city, use, n, *a: [{"ok": True}] * n if zoned_ok else []), \
                mock.patch.object(growth, "expand", return_value={"ok": True}) as expand:
                growth.grow(city, "a", hold_land=hold_land)
        return expand.called

    def test_offices_take_the_job_blocks_industry_has_no_land_for_once_unlocked(self):
        asked = []

        def zone(city, use, allowance, *args):
            asked.append((use, allowance))
            return [{"ok": True}] * (1 if use == "workplace" else allowance)  # one industrial block left
        for offices, expected in ((True, [("workplace", 4), ("office", 3)]), (False, [("workplace", 4)])):
            asked.clear()
            with mock.patch.object(growth, "zone_blocks", side_effect=zone):
                zoned = growth.zone_workplace(None, 4, "a", [], offices)
            self.assertEqual(expected, asked)
            self.assertEqual(4 if offices else 1, len(zoned))
        self.assertEqual("Office", growth.LOW_ZONE["office"])
        # the tool takes the use: its argument schema is checked before any call reaches the game
        growth.mt._validate_arguments("propose_zoning_blocks", {"use": "office", "limit": 7})

    def test_an_open_district_waits_while_blocks_are_still_being_zoned(self):
        self.assertFalse(self.grow(zoned_ok=True))

    def test_a_vacancy_that_wobbles_but_is_not_built_on_is_stale(self):
        record, allowances = {}, []
        for vacancy in (77, 86, 77, 80):
            category = {"maximumAdditionalBlocksThisWake": 0, "demand": 48, "developableVacantCells": vacancy}
            allowance, record = growth.stale_allowance(category, record)
            allowances.append(allowance)
        self.assertEqual([None, None, 3, None], allowances)
        # a pacing alternating between one block and none still waits
        record, allowances = {}, []
        for vacancy, base in ((77, 1), (81, 0), (81, 0), (81, 1)):
            category = {"maximumAdditionalBlocksThisWake": base, "demand": 50, "developableVacantCells": vacancy}
            allowance, record = growth.stale_allowance(category, record)
            allowances.append(allowance)
        self.assertEqual([None, None, 3, None], allowances)
        # a vacancy being built on is not stale
        record, allowances = {}, []
        for vacancy in (77, 70, 62, 55):
            category = {"maximumAdditionalBlocksThisWake": 0, "demand": 48, "developableVacantCells": vacancy}
            allowance, record = growth.stale_allowance(category, record)
            allowances.append(allowance)
        self.assertEqual([None, None, None, None], allowances)

    def test_new_land_opens_once_the_blocks_fall_short_of_the_allowance(self):
        def run(found):
            categories = {use: {"demand": 60, "developableVacantCells": 0} for use in growth.USES}
            with tempfile.TemporaryDirectory() as root:
                city = city_module.City({"instanceId": "t"}, Path(root))
                city.save_state("growth", {"absorption": {}, "stale": {}, "outside": {"complete": True}})
                with mock.patch.object(growth.mt, "call_tool", return_value={"growthPacing": {"categories": categories}}), \
                        mock.patch.object(growth, "allowances",
                                          return_value={"residential": 4, "commercial": 0, "workplace": 0}), \
                        mock.patch.object(growth, "zone_blocks",
                                          side_effect=lambda city, use, n, *a: [{"ok": True}] * min(n, found)), \
                        mock.patch.object(growth, "expand", return_value={"ok": True}) as expand:
                    report = growth.grow(city, "a")
            return expand.called, report
        self.assertEqual((True, ["residential"], []), (run(2)[0], run(2)[1]["short"], run(2)[1]["exhausted"]))
        self.assertFalse(run(4)[0])  # every block allowed was found: no new land yet

    def test_an_open_district_goes_on_once_the_blocks_run_out(self):
        self.assertTrue(self.grow(zoned_ok=False))

    def test_no_new_land_while_homes_lack_water_or_power(self):
        self.assertFalse(self.grow(zoned_ok=False, hold_land="waitingForUtilities"))

    def test_no_new_land_while_cash_is_short(self):
        self.assertFalse(self.grow(zoned_ok=False, hold_land="waitingForCash"))


class ContinuationTests(unittest.TestCase):
    def test_each_stub_runs_on_and_a_cross_road_joins_the_ends(self):
        pair = [{"nodeId": 1, "point": (0.0, 0.0), "inward": (0.0, 1.0)},
                {"nodeId": 2, "point": (30.0, 0.0), "inward": (0.0, 1.0)}]
        legs, junction, heading = growth._continuation(pair, 60.0)
        self.assertEqual([(0.0, 0.0), (30.0, 0.0)], [legs[0][0], legs[1][0]])
        self.assertEqual((legs[1][1], legs[0][1]), legs[2])  # the cross road, from one end to the other
        self.assertEqual(legs[0][1], junction)  # the town grows on from the first end
        self.assertGreater(abs(legs[0][1][0] - legs[1][1][0]), 30.0)  # bent apart, not toward each other
        self.assertTrue(growth.roads_clear_of_each_other(legs))
        self.assertAlmostEqual(180.0, abs(heading), places=3)  # into the map, +z

    def test_roads_of_one_attempt_must_not_crowd_each_other(self):
        # two runs ending 17m apart (closer than two road widths), and two roads meeting at 10 degrees
        self.assertFalse(growth.roads_clear_of_each_other([((0.0, 0.0), (6.0, 60.0)), ((30.0, 0.0), (23.0, 60.0))]))
        self.assertFalse(growth.roads_clear_of_each_other([((0.0, 0.0), (0.0, 60.0)), ((10.0, 57.0), (0.0, 0.0))]))
        self.assertTrue(growth.roads_clear_of_each_other([((0.0, 0.0), (0.0, 60.0)), ((40.0, 60.0), (0.0, 60.0))]))


class DistrictTests(unittest.TestCase):
    graph = {"layout": "grid", "entry": "e", "nodes": {"e": [0, 0], "a": [0, 88], "b": [88, 88], "c": [88, 0]},
             "edges": [["e", "a"], ["e", "c"], ["a", "b"], ["c", "b"]]}

    def run_district(self, results, budget=10, wakes=1):
        calls = []

        def follow(label, prefab, start, path, city=None):
            calls.append((tuple(start), [tuple(p) for p in path]))
            outcome = results(label, path)
            return {**outcome, "end": {"x": path[-1][0], "z": path[-1][1]}} if outcome["ok"] else outcome

        with tempfile.TemporaryDirectory() as root:
            city = city_module.City({"instanceId": "t"}, Path(root))
            # no arc either: an edge refused straight would otherwise preview arcs against the live game
            with mock.patch.object(district.roads, "follow", side_effect=follow),                     mock.patch.object(district.roads, "lay_arc", return_value={"ok": False, "stage": "preview"}):
                reports = [district.grow(city, "d", self.graph, budget, "w") for _ in range(wakes)]
        return reports, calls

    def test_edges_are_laid_in_order_and_a_block_closes_on_the_built_node(self):
        reports, calls = self.run_district(lambda label, path: {"ok": True})
        self.assertEqual(["e|a", "e|c", "a|b", "c|b"], reports[0]["built"])
        self.assertTrue(reports[0]["ended"])
        self.assertEqual((88.0, 88.0), calls[3][1][-1])  # c to b ends where a to b put b

    def test_an_edge_from_a_failed_node_is_skipped_and_the_budget_spreads_work(self):
        reports, calls = self.run_district(lambda label, path: {"ok": not label.endswith("-e-a"), "stage": "preview"},
                                           budget=1, wakes=3)
        laid = [edge for report in reports for edge in report["built"]]
        self.assertNotIn("a|b", laid)  # a was never reached
        self.assertIn("e|c", laid)
        self.assertTrue(all(len(report["built"]) + len(report["failed"]) <= 1 for report in reports))

    def test_a_long_edge_is_laid_in_two_bent_halves(self):
        path = district._halves((0.0, 0.0), (160.0, 0.0))
        self.assertEqual(2, len(path))
        self.assertNotEqual(0.0, path[0][1])


class OverpassExpansionTests(unittest.TestCase):
    def test_land_beyond_a_railway_is_reached_over_an_overpass(self):
        area = {"center": {"x": 500, "z": 0}, "areaSquareMeters": 90000,
                "fishbone": {"collectorHeadingDegrees": 90, "branchLength": 96},
                "access": [{"clear": False, "crosses": ["Train Track"], "distance": 120,
                            "from": {"x": 0, "z": 0}, "to": {"x": 120, "z": 0}}]}
        pieces = [{"start": {"x": 0, "z": 0}, "end": {"x": 50, "z": 0}, "startHeightOffset": 0, "endHeightOffset": 10},
                  {"start": {"x": 50, "z": 0}, "end": {"x": 100, "z": 0}, "startHeightOffset": 10, "endHeightOffset": 10},
                  {"start": {"x": 100, "z": 0}, "end": {"x": 150, "z": 0}, "startHeightOffset": 10, "endHeightOffset": 0}]

        def call_tool(name, arguments):
            if name == "propose_expansion_areas":
                return {"areas": [area]}
            if name == "propose_network_crossing":
                return {"roadPrefab": "Basic Road Elevated", "candidates": [{"valid": True, "pieces": pieces}]}
            raise AssertionError(name)

        built = {"collector": [{"ok": True}], "branches": [], "collectorNodes": 3, "ended": None}
        with tempfile.TemporaryDirectory() as root:
            city = city_module.City({"instanceId": "t"}, Path(root))
            with mock.patch.object(growth.mt, "call_tool", side_effect=call_tool), \
                    mock.patch.object(growth.roads, "build_raised",
                                      side_effect=lambda label, prefab, piece: {"ok": True, "end": piece["end"]}) as raised, \
                    mock.patch.object(growth.roads, "grow_fishbone", return_value=built) as grow:
                result = growth.expand(city, "residential", {"wake": 1}, "a")
        self.assertEqual(3, raised.call_count)
        self.assertEqual((150, 0), grow.call_args.args[2])  # the fishbone starts where the overpass lands
        self.assertTrue(result["ok"])

    def test_an_overpass_is_tried_from_the_next_way_and_a_failed_one_comes_down(self):
        ways = [{"nodeId": node, "clear": False, "crosses": ["Highway"], "distance": 120,
                 "from": {"x": 0, "z": z}, "to": {"x": 120, "z": z}} for node, z in ((1, 0), (2, 100))]
        area = {"center": {"x": 500, "z": 0}, "areaSquareMeters": 90000,
                "fishbone": {"collectorHeadingDegrees": 90, "branchLength": 96}, "access": ways}
        pieces = [{"start": {"x": 0, "z": 100}, "end": {"x": 50 * i, "z": 100}, "startHeightOffset": 0,
                   "endHeightOffset": 0} for i in (1, 2, 3)]
        asked = []

        def call_tool(name, arguments):
            if name == "propose_expansion_areas":
                return {"areas": [area]}
            asked.append(arguments["start"]["z"])
            valid = arguments["start"]["z"] == 100  # the first way's overpass collides
            return {"roadPrefab": "Basic Road Elevated", "candidates": [{"valid": valid, "pieces": pieces}]}

        built = {"collector": [{"ok": True}], "branches": [], "collectorNodes": 3, "ended": None}
        for fails, expected in ((False, True), (True, False)):
            outcomes = iter([{"ok": True, "segmentId": 7, "end": pieces[0]["end"]},
                             {"ok": not fails, "segmentId": 8, "end": pieces[1]["end"]},
                             {"ok": True, "segmentId": 9, "end": pieces[2]["end"]}])
            asked.clear()
            with tempfile.TemporaryDirectory() as root:
                city = city_module.City({"instanceId": "t"}, Path(root))
                with mock.patch.object(growth.mt, "call_tool", side_effect=call_tool), \
                        mock.patch.object(growth.roads, "build_raised", side_effect=lambda *a: next(outcomes)), \
                        mock.patch.object(growth, "_remove", side_effect=lambda label, ids: list(ids)) as remove, \
                        mock.patch.object(growth.roads, "grow_fishbone", return_value=built):
                    result = growth.expand(city, "residential", {"wake": 1}, "a")
            self.assertEqual([0, 100], asked)
            self.assertEqual(expected, bool(result["ok"]))
            if fails:
                self.assertEqual("overpass1", result["stage"])
                # the ramp up comes down again, and the piece that stood but failed its checks
                self.assertEqual([7, 8], remove.call_args.args[1])

    def run_ways(self, outcomes, removes=None):
        ways = [{"nodeId": node, "clear": False, "crosses": ["Highway"], "distance": 120,
                 "from": {"x": 0, "z": z}, "to": {"x": 120, "z": z}} for node, z in ((1, 0), (2, 100))]
        area = {"center": {"x": 500, "z": 0}, "areaSquareMeters": 90000,
                "fishbone": {"collectorHeadingDegrees": 90, "branchLength": 96}, "access": ways}
        piece = {"start": {"x": 0, "z": 0}, "end": {"x": 50, "z": 0}, "startHeightOffset": 0, "endHeightOffset": 0}

        def call_tool(name, arguments):
            if name == "propose_expansion_areas":
                return {"areas": [area]}
            return {"roadPrefab": "Basic Road Elevated", "candidates": [{"valid": True, "pieces": [piece, piece]}]}

        built = {"collector": [{"ok": True}], "branches": [], "collectorNodes": 3, "ended": None}
        outcomes = iter(outcomes)
        with tempfile.TemporaryDirectory() as root:
            city = city_module.City({"instanceId": "t"}, Path(root))
            with mock.patch.object(growth.mt, "call_tool", side_effect=call_tool), \
                    mock.patch.object(growth.roads, "build_raised", side_effect=lambda *a: next(outcomes)) as raised, \
                    mock.patch.object(growth, "_remove", side_effect=removes or (lambda label, ids: list(ids))), \
                    mock.patch.object(growth.roads, "grow_fishbone", return_value=built):
                return growth.expand(city, "residential", {"wake": 1}, "a"), raised

    def test_an_overpass_refused_part_way_comes_down_and_the_next_way_is_built(self):
        refused = {"ok": False, "stage": "preview", "errors": "SlopeTooSteep"}
        up = {"ok": True, "segmentId": 7, "end": {"x": 50, "z": 0}}
        result, raised = self.run_ways([up, refused, {**up, "segmentId": 8}, {**up, "segmentId": 9}])
        self.assertTrue(result["ok"], result)
        self.assertEqual(4, raised.call_count)

    def test_every_way_refused_stops_with_why_each_failed(self):
        refused = {"ok": False, "stage": "preview", "errors": "SlopeTooSteep"}
        result, _ = self.run_ways([refused, refused])
        self.assertEqual((False, "overpass0"), (result["ok"], result["stage"]))
        self.assertEqual(["SlopeTooSteep", "SlopeTooSteep"], [item["errors"] for item in result["overpasses"]])
        self.assertEqual(["preview", "preview"], [item["pieceStage"] for item in result["overpasses"]])

    def test_a_game_thread_too_busy_for_every_way_waits_for_the_next_wake(self):
        busy = {"ok": False, "stage": "preview", "error": "Timed out waiting for the game thread; the command was not run."}
        result, _ = self.run_ways([busy, busy])
        self.assertEqual((None, "busy"), (result["ok"], result["stage"]))

    def test_a_ramp_that_will_not_come_down_stops_before_the_next_way(self):
        up = {"ok": True, "segmentId": 7, "end": {"x": 50, "z": 0}}
        result, raised = self.run_ways([up, {"ok": False, "stage": "build", "error": "x"}],
                                       removes=lambda label, ids: [])
        self.assertEqual((False, [7]), (result["ok"], result["left"]))
        self.assertEqual(2, raised.call_count)


class BusyGameThreadTests(unittest.TestCase):
    TIMEOUT = {"ok": False, "error": "Timed out waiting for the game thread; the command was not run.",
               "recovery": {"noConstructionAttempted": True}}

    def test_a_road_the_game_thread_was_too_busy_for_is_busy_not_built_and_failed(self):
        with mock.patch.object(roads, "preview", return_value=(True, {"roadPrefab": "Basic Road"}, {})), \
                mock.patch.object(roads.protocol, "checkpoint", return_value="c"), \
                mock.patch.object(roads.mt, "call_tool", side_effect=roads.mt.BridgeRequestError(self.TIMEOUT)):
            outcome = roads.build("e", "Basic Road", (0.0, 0.0), (80.0, 0.0))
        self.assertEqual((False, "busy"), (outcome["ok"], outcome["stage"]))

    def test_a_busy_edge_stays_open_and_the_district_is_not_failed(self):
        graph = {"layout": "grid", "entry": "0,0", "nodes": {"0,0": [0.0, 0.0], "1,0": [0.0, -88.0], "2,0": [0.0, -176.0]},
                 "edges": [["0,0", "1,0"], ["1,0", "2,0"]]}
        with tempfile.TemporaryDirectory() as root:
            city = city_module.City({"instanceId": "t"}, Path(root))
            with mock.patch.object(roads, "follow", return_value={"ok": False, "stage": "busy"}) as follow:
                report = district.grow(city, "d", graph, 4, "d")
            state = city.load_state("district-d", {})
        self.assertEqual(1, follow.call_count)  # no more roads this wake
        self.assertEqual({}, state["edges"])  # the edge is tried again next wake
        self.assertEqual([{"edge": "0,0|1,0", "stage": "busy"}], report["failed"])
        current = {"use": "residential", "name": "d", "layout": "grid", "layoutReason": "", "ended": None}
        with mock.patch.object(growth.district, "grow", return_value={**report, "built": [], "ended": False,
                                                                       "nodesBuilt": 1, "edgesLeft": 2}):
            result = growth._grow_district(city, current, None)
        self.assertIsNone(result["ok"])  # not a failure that stops the loop


class RemovedEdgeTests(unittest.TestCase):
    def test_an_edge_taken_out_after_failing_its_checks_does_not_fail_the_district(self):
        current = {"use": "residential", "name": "d", "layout": "ring", "layoutReason": "", "ended": None}
        for failed, expected in (({"edge": "a|b", "stage": "checks", "removed": True}, True),
                                 ({"edge": "a|b", "stage": "checks"}, False)):
            report = {"built": ["e|a"], "failed": [failed], "ended": False, "nodesBuilt": 3, "edgesLeft": 2}
            with mock.patch.object(growth.district, "grow", return_value=report):
                result = growth._grow_district(None, dict(current), None)
            self.assertEqual(expected, result["ok"])


class IneffectiveTests(unittest.TestCase):
    def test_a_need_whose_every_building_was_judged_ineffective_starts_over(self):
        options = [{"name": "Landfill Site"}, {"name": "Combustion Plant"}]
        state = {"ineffective": {"garbage:fillsUp": ["Combustion Plant", "Landfill Site"],
                                 "garbage:coverage": ["Landfill Site"]}}
        with mock.patch.object(services.mt, "call_tool", return_value={"options": options}),                 mock.patch.object(services, "cash", return_value=1000000.0):
            self.assertEqual(options, services.choose_prefabs("garbage", "fillsUp", state))
            self.assertEqual([], state["ineffective"]["garbage:fillsUp"])
            # one building still left: the judgement stands
            self.assertEqual([{"name": "Combustion Plant"}], services.choose_prefabs("garbage", "coverage", state))
            self.assertEqual(["Landfill Site"], state["ineffective"]["garbage:coverage"])


class RidershipTests(unittest.TestCase):
    def line(self, line_id, riders, buses, budget=100, stops=10, name=None):
        return {"id": line_id, "name": name or f"Relief {line_id}", "transportType": "Bus", "stops": stops,
                "vehicles": buses, "budget": budget, "weeklyPassengers": {"total": riders}}

    def review(self, lines, state, broken=()):
        runs = []

        def run(label, tool, arguments, settle=0.0):
            runs.append((tool, arguments))
            return {"ok": True, "regressionOk": True}

        def request(method, path, query=None, body=None):
            if path == "/state/transport-line-anomalies":
                return {"anomalies": list(broken)}
            return {"lines": lines}
        with mock.patch.object(traffic.mt, "_request", side_effect=request), \
                mock.patch.object(traffic.protocol, "run", side_effect=run):
            return traffic.review_lines(state, "t"), runs

    def test_an_underused_relief_line_is_halved_then_deleted(self):
        state = {"wake": 20, "actions": [{"kind": "transit", "wake": 15, "detail": {"lineId": 9}}]}
        lines = [self.line(1, 2, 25), self.line(2, 260, 21), self.line(9, 0, 10), self.line(4, 5, 0)]
        changes, runs = self.review(lines, state)
        # 1 is long run and carries 0.08 a bus; 2 carries 12.4; 9 is five wakes old; 4 has no bus out yet
        self.assertEqual([("set_transport_line", {"lineId": 1, "budget": 50})], runs)
        self.assertEqual(20, state["lines"]["1"]["halvedAt"])
        lines[0]["budget"] = 50
        state["wake"] = 23
        self.assertEqual([("set_transport_line", {"lineId": 9, "budget": 50})], self.review(lines, state)[1])
        lines[2]["budget"] = 50
        state["wake"] = 27
        self.assertEqual([], self.review(lines, state)[1])  # 1 halved seven wakes ago: it waits
        state["wake"] = 28
        changes, runs = self.review(lines, state)
        self.assertEqual([("delete_transport_line", {"lineId": 1})], runs)
        self.assertEqual(["deleted"], [change["change"] for change in changes])
        self.assertNotIn("1", state["lines"])

    def test_an_empty_line_goes_and_the_players_lines_stay(self):
        state = {"wake": 1, "actions": []}
        lines = [self.line(5, 0, 0, stops=0, name="Bus Line 0"), self.line(6, 0, 30, name="Downtown")]
        changes, runs = self.review(lines, state)
        self.assertEqual([("delete_transport_line", {"lineId": 5})], runs)
        self.assertEqual({"6"}, set(state["lines"]))  # a line gone is forgotten

    def test_a_relief_line_that_detours_is_deleted_at_once(self):
        stops = [{"position": {"x": x, "z": 0.0}} for x in (0.0, 500.0, 1000.0)]  # 1,000m along its stops
        state = {"wake": 1, "actions": [{"kind": "transit", "wake": 1, "detail": {"lineId": 1}}]}
        far = [{"position": {"x": x, "z": 5000.0}} for x in (0.0, 500.0, 1000.0)]  # served by no other line
        lines = [{**self.line(1, 150, 10), "stopNodes": stops, "totalLength": 5000.0},   # 2.5: new, still goes
                 {**self.line(2, 150, 10), "stopNodes": far, "totalLength": 3000.0},     # 1.5 stays
                 {**self.line(3, 150, 10), "stopNodes": [], "totalLength": 0.0},         # no path yet
                 {**self.line(4, 0, 10, name="Downtown"), "stopNodes": stops, "totalLength": 9000.0}]
        changes, runs = self.review(lines, state)
        self.assertEqual([("delete_transport_line", {"lineId": 1})], runs)
        self.assertEqual(2.5, changes[0]["detour"])
        self.assertIsNone(traffic.detour(lines[2]))

    def test_a_relief_line_the_game_reports_not_connected_is_deleted_at_once(self):
        state = {"wake": 1, "actions": [{"kind": "transit", "wake": 1, "detail": {"lineId": 1}}]}
        broken = [{"type": "lineNotConnected", "lineId": 1}, {"type": "noVehicles", "lineId": 2},
                  {"type": "lineNotConnected", "lineId": 3}]
        lines = [self.line(1, 0, 0), self.line(2, 150, 10), self.line(3, 0, 0, name="Downtown")]
        changes, runs = self.review(lines, state, broken)
        self.assertEqual([("delete_transport_line", {"lineId": 1})], runs)  # the player's line stays
        self.assertTrue(changes[0]["notConnected"])

    def test_a_relief_line_whose_stops_a_busier_line_serves_is_deleted(self):
        def at(*xs):
            return [{"position": {"x": x, "z": 0.0}} for x in xs]
        state = {"wake": 1, "actions": []}
        lines = [{**self.line(1, 300, 10), "stopNodes": at(0, 400, 800, 1200)},
                 {**self.line(2, 120, 10), "stopNodes": at(10, 410, 790, 1210, 1600)},  # 4 of 5 served by 1
                 {**self.line(3, 400, 10), "stopNodes": at(30, 405, 3000, 4000)}]      # 2 of 4: stays
        changes, runs = self.review(lines, state)
        self.assertEqual([("delete_transport_line", {"lineId": 2})], runs)
        self.assertIsNone(traffic.duplicate_of(lines[0], lines))  # 1 is busier than any line serving it

    def test_no_new_line_past_the_cap_or_where_other_lines_stop(self):
        relief = [{**self.line(i, 400, 10), "stopNodes": [{"position": {"x": 0.0, "z": 0.0}}]} for i in range(1, 4)]
        proposals = {"proposals": [{"stops": [{"x": 5.0, "z": 0.0}, {"x": 20.0, "z": 0.0}], "preview": {"ok": True}}]}
        with mock.patch.object(traffic, "_bus_lines", return_value=relief), \
                mock.patch.object(traffic.mt, "call_tool", return_value=proposals), \
                mock.patch.object(traffic.protocol, "run", side_effect=AssertionError("no line")):
            self.assertIsNone(traffic.transit("t", {"center": {"x": 0, "z": 0}}))  # its stops are served
        many = [self.line(i, 400, 10) for i in range(traffic.MAX_RELIEF_LINES)]
        with mock.patch.object(traffic, "_bus_lines", return_value=many), \
                mock.patch.object(traffic.mt, "call_tool", side_effect=AssertionError("no proposal")):
            self.assertIsNone(traffic.transit("t", {"center": {"x": 0, "z": 0}}))

    def test_changes_a_wake_are_capped(self):
        state = {"wake": 50, "actions": []}
        changes, runs = self.review([self.line(i, 0, 10) for i in range(1, 7)], state)
        self.assertEqual(traffic.MAX_LINE_CHANGES, len(runs))

    def test_no_relief_line_is_added_while_they_carry_too_few(self):
        self.assertTrue(traffic.relief_underused([self.line(1, 30, 20), self.line(2, 2, 25)]))
        self.assertTrue(traffic.relief_underused([self.line(1, 80, 20), self.line(2, 30, 25)]))  # 2.4 a bus
        self.assertFalse(traffic.relief_underused([self.line(1, 300, 20), self.line(2, 200, 25)]))
        self.assertFalse(traffic.relief_underused([self.line(1, 0, 20, name="Downtown")]))
        self.assertFalse(traffic.relief_underused([]))
        with mock.patch.object(traffic, "relief_underused", return_value=True), \
                mock.patch.object(traffic.mt, "call_tool", side_effect=AssertionError("no proposal")):
            self.assertIsNone(traffic.transit("t", {"center": {"x": 0, "z": 0}}))


class BusyPhaseTests(unittest.TestCase):
    def test_a_phase_the_busy_game_thread_did_not_run_is_reported_busy(self):
        busy = cycle.mt.BridgeRequestError({"ok": False, "error": "Timed out waiting for the game thread; "
                                                                   "the command was not run."})
        self.assertEqual({"ok": True}, cycle.unless_busy(lambda a, b: {"ok": a == b}, 1, 1))
        report = cycle.unless_busy(mock.Mock(side_effect=busy))
        self.assertEqual((None, True), (report["ok"], report["busy"]))
        other = cycle.mt.BridgeRequestError({"ok": False, "error": "Timed out; the outcome is unknown."})
        with self.assertRaises(cycle.mt.BridgeRequestError):
            cycle.unless_busy(mock.Mock(side_effect=other))  # it may have run: the wake fails
        with self.assertRaises(KeyError):
            cycle.unless_busy(mock.Mock(side_effect=KeyError("x")))

    def test_every_phase_after_the_growth_is_guarded(self):
        import inspect
        source = inspect.getsource(cycle.main)
        for call in ("unless_busy(clear_ruins", "unless_busy(clear_orphans", "unless_busy(clear_stacks", "unless_busy(services.solve, city, label, high, offices, burned, ruined)",
                     "unless_busy(utilities.balance", "unless_busy(traffic.watch",
                     "unless_busy(services.solve, city, label + \"-after-traffic\", high, offices)"):
            self.assertIn(call, source)


class OrphanRetryTests(unittest.TestCase):
    def test_an_orphan_whose_bulldoze_failed_is_tried_again_and_the_error_kept(self):
        orphans = [{"id": 1, "service": "Residential", "roadDistance": 38.9}]
        for outcomes, removed in (([{"ok": False, "error": "timeout"}, {"ok": True}], True),
                                  ([{"ok": False, "error": "timeout"}, {"ok": False, "error": "refused"}], False)):
            with mock.patch.object(cycle, "orphaned_buildings", return_value=[dict(orphans[0])]), \
                    mock.patch.object(cycle.protocol, "run", side_effect=list(outcomes)) as run:
                result = cycle.clear_orphans("a")
            self.assertEqual(2, run.call_count)
            self.assertEqual(removed, result["found"][0]["removed"])
            self.assertEqual(None if removed else "refused", result["found"][0].get("error"))


class TransitTests(unittest.TestCase):
    def setUp(self):
        for name, value in (("review_lines", []), ("relief_underused", False), ("_bus_lines", [])):
            patch = mock.patch.object(traffic, name, return_value=value)
            patch.start()
            self.addCleanup(patch.stop)

    HOTSPOT = {"center": {"x": 0, "z": 0}}
    STOPS = [{"x": 0, "z": 0}, {"x": 300, "z": 0}, {"x": 600, "z": 0}]
    WANTS_DEPOT = {"ok": True, "valid": False, "depot": {"required": True, "buildingId": 0, "safe": False},
                   "stopChecks": [{"compatibleNetwork": True, "separated": True}] * 3}

    def run_transit(self, previews, depots, placed=None):
        proposal = {"stops": self.STOPS, "carTrips": 9, "preview": {"ok": True, "valid": False}}
        previews = iter(previews)
        with mock.patch.object(traffic.mt, "call_tool", return_value={"proposals": [proposal]}), \
                mock.patch.object(traffic, "_line_preview", side_effect=lambda stops: next(previews)), \
                mock.patch.object(traffic, "_has_depot", return_value=depots), \
                mock.patch.object(traffic.services, "place_near",
                                  return_value=placed or {"ok": True, "buildingId": 77}) as place, \
                mock.patch.object(traffic.protocol, "run",
                                  return_value={"ok": True, "regressionOk": True, "result": {"lineId": 5}}) as run:
            return traffic.transit("a", self.HOTSPOT), place, run

    def test_a_line_wanting_only_a_depot_gets_one_by_its_middle_stop_then_runs(self):
        result, place, run = self.run_transit([self.WANTS_DEPOT, {"ok": True, "valid": True}], depots=False)
        self.assertEqual(("Bus Depot", {"x": 300, "z": 0}), place.call_args.args[1:3])
        self.assertEqual("create_transport_line", run.call_args.args[1])
        self.assertEqual((True, 5, 77), (result["ok"], result["lineId"], result["depot"]))

    def test_no_second_depot_and_none_for_a_line_whose_stops_do_not_fit(self):
        _, place, run = self.run_transit([self.WANTS_DEPOT], depots=True)
        place.assert_not_called()
        run.assert_not_called()
        bad_stop = {**self.WANTS_DEPOT, "stopChecks": [{"compatibleNetwork": False, "separated": True}]}
        result, place, run = self.run_transit([bad_stop], depots=False)
        self.assertIsNone(result)
        place.assert_not_called()

    def test_a_depot_with_no_site_ends_the_try(self):
        result, _, run = self.run_transit([self.WANTS_DEPOT], depots=False, placed={"ok": False, "stage": "noSite"})
        self.assertEqual(("depot", "noSite"), (result["stage"], result["depot"]["stage"]))
        run.assert_not_called()


class HighwayExitTests(unittest.TestCase):
    @staticmethod
    def segment(sid, a, b, start, end, prefab="Highway", direction="StartToEnd"):
        return {"id": sid, "prefab": prefab, "service": "Road", "startNodeId": a, "endNodeId": b,
                "directionality": direction, "start": {"x": start[0], "z": start[1]},
                "end": {"x": end[0], "z": end[1]},
                "middle": {"x": (start[0] + end[0]) / 2, "z": (start[1] + end[1]) / 2}}

    def roads(self):
        # a southbound carriageway at x 0 and a northbound one at x 30, dead ends 100m ahead either side
        return [self.segment(1, 1, 2, (0, 400), (0, 300)), self.segment(2, 2, 3, (0, 300), (0, 200)),
                self.segment(3, 4, 5, (30, 200), (30, 300)), self.segment(4, 5, 6, (30, 300), (30, 400)),
                self.segment(5, 10, 11, (-120, 200), (-70, 230), prefab="Basic Road", direction="Both"),
                self.segment(6, 12, 13, (120, 200), (70, 230), prefab="Basic Road", direction="Both"),
                self.segment(7, 14, 15, (-120, 400), (-70, 380), prefab="Basic Road", direction="Both")]

    def test_an_exit_leaves_a_plain_node_forward_onto_the_outer_side(self):
        found = ramps.candidates(self.roads(), [(0.0, 250.0)])
        southbound = next(item for item in found if item["node"] == 2)
        # the dead ends ahead on the west: the east one lies across the other carriageway, the one
        # at z 380 behind
        self.assertEqual([(-120.0, 200.0), (-70.0, 230.0)], southbound["ends"])
        self.assertEqual((0.0, -1.0), southbound["travel"])
        self.assertFalse(ramps.candidates(self.roads(), [(5000.0, 0.0)]))  # no node near the traffic

    def test_the_town_piece_goes_first_and_a_refused_piece_is_taken_down(self):
        hotspot = {"diagnosis": {"topDestinations": [{"center": {"x": 0, "z": 250}}]}}
        built = iter([{1}, {1}])
        calls = []
        with mock.patch.object(ramps.mt, "_request", return_value={"segments": self.roads()}), \
                mock.patch.object(ramps, "_preview", return_value=True), \
                mock.patch.object(ramps, "_ramp_segments", side_effect=lambda: next(built, {1}) if calls else set()), \
                mock.patch.object(ramps.protocol, "checkpoint", return_value="cp"), \
                mock.patch.object(ramps.mt, "call_tool",
                                  side_effect=lambda name, body: calls.append(body) or {"ok": len(calls) == 1}), \
                mock.patch.object(ramps.protocol, "run", return_value={"ok": True}) as run:
            done = ramps.build_exit("a", hotspot, 0)
        self.assertEqual((-120.0, 200.0), (calls[0]["end"]["x"], calls[0]["end"]["z"]))  # town piece first
        self.assertEqual((0.0, 300.0), (calls[1]["start"]["x"], calls[1]["start"]["z"]))
        self.assertEqual((False, "piece1", True), (done["ok"], done["stage"], done["undone"]))
        self.assertEqual("bulldoze_entity", run.call_args.args[1])
        self.assertIsNone(ramps.build_exit("a", hotspot, ramps.MAX_RAMPS))

    def test_a_highway_fed_hotspot_gets_an_exit_ramp_among_its_kinds(self):
        remedies = {"widen": {"kind": "widen"}}
        self.assertIn("highwayExit", traffic.kinds_for({"diagnosis": {"highwayShare": 1.0}}, dict(remedies), {}))
        self.assertNotIn("highwayExit", traffic.kinds_for({"diagnosis": {"highwayShare": 0.2}}, dict(remedies), {}))
        outside = traffic.kinds_for({"diagnosis": {"highwayShare": 0.0, "externalShare": 0.61}}, dict(remedies), {})
        self.assertEqual(["highwayExit", "outsideLink"], outside[-2:])  # the exit before the link is widened


class BypassWorthTests(unittest.TestCase):
    def test_a_bypass_serving_few_vehicles_for_the_buildings_it_clears_is_not_built(self):
        hotspot = {"segmentIds": [1, 2, 3]}
        poor = {"buildable": True, "servesVehicles": 4, "clearGrowables": list(range(9)), "pieces": [],
                "from": {"x": 0, "z": 0}, "length": 228.1, "detour": 1.22}
        good = {**poor, "servesVehicles": 40, "clearGrowables": [1, 2]}
        for candidate, built in ((poor, False), (good, True)):
            proposal = {"hotspots": [{"segmentIds": [1, 2, 3], "candidates": [candidate]}]}
            with mock.patch.object(traffic.mt, "call_tool", return_value=proposal), \
                    mock.patch.object(traffic.roads, "clear_buildings",
                                      return_value={"ok": True, "cleared": candidate["clearGrowables"]}) as clear:
                result = traffic.bypass(None, "a", hotspot)
            self.assertEqual(built, result is not None)
            self.assertEqual(built, clear.called)


class TrafficTests(unittest.TestCase):
    def test_kinds_that_keep_failing_go_last(self):
        stats = {"junctionControl": {"worked": 0, "failed": 4}, "widen": {"worked": 1, "failed": 1}}
        self.assertEqual(["widen", "bypass", "junctionControl"],
                         traffic.order(["junctionControl", "widen", "bypass"], stats))

    def test_judge_by_density_or_disappearance(self):
        state = {"wake": 5, "kindStats": {}, "actions": [
            {"key": "a", "kind": "widen", "wake": 2, "meanDensity": 99, "segments": 10, "detail": {}, "verdict": None},
            {"key": "b", "kind": "bypass", "wake": 2, "meanDensity": 99, "segments": 10, "detail": {}, "verdict": None},
            {"key": "c", "kind": "widen", "wake": 4, "meanDensity": 99, "segments": 10, "detail": {}, "verdict": None}]}
        hotspots = [{"meanDensity": 98, "segmentCount": 10}]
        tracked = [{"key": "a"}]
        verdicts = traffic.judge(state, hotspots, tracked, "t")
        self.assertEqual([("a", "failed"), ("b", "worked")], [(v["key"], v["verdict"]) for v in verdicts])
        self.assertIsNone(state["actions"][2]["verdict"])  # too recent to judge
        self.assertEqual({"widen": {"worked": 0, "failed": 1}, "bypass": {"worked": 1, "failed": 0}},
                         state["kindStats"])


def _road(sid, a, b, pa, pb, prefab="Small 4 Lane Road", **extra):
    return {"id": sid, "prefab": prefab, "service": "Road", "startNodeId": a, "endNodeId": b,
            "start": {"x": pa[0], "z": pa[1]}, "end": {"x": pb[0], "z": pb[1]},
            "middle": {"x": (pa[0] + pb[0]) / 2, "z": (pa[1] + pb[1]) / 2}, "isOneWay": False, **extra}


class OutsideLinkTests(unittest.TestCase):
    def setUp(self):
        for name, value in (("review_lines", []), ("relief_underused", False)):
            patch = mock.patch.object(traffic, name, return_value=value)
            patch.start()
            self.addCleanup(patch.stop)

    """Two ramp stubs (nodes 1 and 2) joined at junction 3, a trunk on to node 5 by the hotspot,
    and a side road the route does not use."""
    HIGHWAYS = [_road(90, 100, 1, (0, -100), (0, 0), "Highway", isOneWay=True, endOutside=False),
                _road(91, 2, 101, (40, 0), (40, -100), "Highway", isOneWay=True, endOutside=True)]
    LOCAL = [_road(10, 1, 3, (0, 0), (20, 60), "Basic Road"), _road(11, 2, 3, (40, 0), (20, 60)),
             _road(20, 3, 4, (20, 60), (20, 140)), _road(21, 4, 5, (20, 140), (20, 220)),
             _road(30, 3, 6, (20, 60), (-100, 60))]
    CENTER = {"x": 20, "z": 230}

    def route(self, local=None):
        return traffic.outside_route(self.HIGHWAYS + (local or self.LOCAL), self.CENTER)

    @staticmethod
    def homes(*ids):
        return [{"id": i, "service": "Residential"} for i in ids]

    def select(self, previews, local=None):
        seen = []

        def preview(segment):
            seen.append(segment["id"])
            return {"valid": True, "under": [], "powerLines": 0, **previews.get(segment["id"], {})}
        return traffic.select_outside_link(self.route(local), preview), seen

    def test_the_route_runs_from_each_highway_link_to_the_hotspot_nearest_the_highway_first(self):
        route = self.route()
        self.assertEqual([10, 11, 20, 21], [s["id"] for s in route])
        self.assertEqual([1, 2, 3, 4], [s["fromNode"] for s in route])
        self.assertEqual([0, 0], [s["routeStart"] for s in route[:2]])

    def test_a_way_in_ends_where_the_clearing_would_pass_its_cap_and_the_rest_is_taken(self):
        selection, seen = self.select({10: {"under": self.homes(501)},
                                       11: {"under": self.homes(501) + [{"id": 502, "service": "Commercial"}]},
                                       20: {"under": self.homes(503, 504, 505)}})
        self.assertEqual([10, 11], selection["segments"])
        self.assertEqual(([501, 502], 1), (selection["clear"], selection["homes"]))
        self.assertEqual({"segmentId": 20, "wouldClear": 5, "homes": 4}, selection["stoppedAt"][0])
        self.assertEqual([10, 11, 20], seen)  # 21 lies beyond the segment that was not widened

    def test_a_service_building_a_power_line_or_a_failed_preview_refuse_a_segment(self):
        selection, seen = self.select({10: {"under": [{"id": 7, "service": "Education"}]},
                                       11: {"powerLines": 1}})
        self.assertEqual([], selection["segments"])
        self.assertEqual([{"segmentId": 10, "blocking": [7]}, {"segmentId": 11, "powerLines": 1}],
                         selection["stoppedAt"])
        self.assertEqual([10, 11], seen)
        selection, _ = self.select({10: {"valid": False, "error": "Road prefab is still locked"}, 11: {"valid": False}})
        self.assertEqual([], selection["segments"])
        self.assertIn("locked", selection["stoppedAt"][0]["preview"])

    def test_wide_and_one_way_segments_are_passed_over_and_the_way_goes_on(self):
        local = [_road(10, 1, 3, (0, 0), (20, 60), "Large Road"), _road(11, 2, 3, (40, 0), (20, 60), isOneWay=True),
                 *self.LOCAL[2:]]
        selection, seen = self.select({}, local)
        self.assertEqual([20, 21], selection["segments"])
        self.assertEqual(["wide", "oneWay"], [item["reason"] for item in selection["passed"]])
        self.assertEqual([20, 21], seen)

    def test_no_highway_link_gives_no_route(self):
        self.assertEqual([], traffic.outside_route(self.LOCAL, self.CENTER))

    def run_link(self, under=(), after=None, locked=("Large Road",)):
        networks = iter([self.HIGHWAYS + self.LOCAL, after if after is not None else self.HIGHWAYS + [
            {**s, "id": s["id"] + 1000, "prefab": "Medium Road"} if s["id"] in (10, 11, 20, 21) else s
            for s in self.LOCAL]])
        events = []

        def request(method, path, query=None, body=None):
            if path == "/state/networks":
                return {"segments": next(networks)}
            self.assertEqual(("POST", "/commands/upgrade-road", True), (method, path, body["dryRun"]))
            if body["roadPrefab"] in locked:
                raise traffic.mt.BridgeRequestError("Road prefab is still locked: " + body["roadPrefab"])
            return {"ok": True, "valid": True, "cost": 48000}

        def preview(name, body):
            self.assertEqual("preview_network", name)
            return {"buildingsUnderRoad": [{"id": i, "service": "Commercial", "prefab": "Shop"} for i in under]
                    if body["start"]["z"] == 0 else [], "powerLinesCrossed": []}

        def clear(label, ids):
            events.append(("clear", list(ids)))
            return {"ok": True, "cleared": list(ids)}

        def run(label, tool, arguments):
            events.append((tool, arguments))
            return {"ok": True, "regressionOk": True, "result": {"ok": True}}
        with mock.patch.object(traffic.mt, "_request", side_effect=request), \
                mock.patch.object(traffic.mt, "call_tool", side_effect=preview), \
                mock.patch.object(traffic.roads, "clear_buildings", side_effect=clear), \
                mock.patch.object(traffic.protocol, "run", side_effect=run):
            return traffic.outside_link("t", {"center": self.CENTER}), events

    def test_the_link_clears_first_then_widens_with_the_widest_unlocked_road(self):
        result, events = self.run_link(under=(601,))
        self.assertEqual("Medium Road", result["prefab"])  # Large Road is locked here
        self.assertEqual([("clear", [601]),
                          ("widen_road_chain", {"segmentIds": [10, 11, 20, 21], "roadPrefab": "Medium Road"})], events)
        self.assertEqual((True, [1010, 1011, 1020, 1021], []), (result["ok"], result["upgraded"], result["cutOff"]))

    def test_a_road_cut_off_from_the_outside_afterwards_fails_the_link(self):
        cut = self.HIGHWAYS[:1] + [{**s, "prefab": "Medium Road"} for s in self.LOCAL]  # the exit ramp is gone
        result, _ = self.run_link(after=cut)
        self.assertFalse(result["ok"])
        self.assertTrue(result["cutOff"])

    def test_nothing_to_widen_reports_why_and_touches_nothing(self):
        result, events = self.run_link(locked=("Large Road", "Medium Road"))
        self.assertEqual((None, "noUpgrade"), (result["ok"], result["reason"]))
        self.assertEqual([], events)

    def test_outside_link_comes_last_and_only_for_outside_heavy_hotspots(self):
        stats = {"bypass": {"worked": 0, "failed": 5}}
        remedies = {"transit": {}, "bypass": {}, "cargoRail": {}}
        heavy = {"diagnosis": {"externalShare": 0.505}}
        self.assertEqual(["transit", "bypass", "highwayExit", "outsideLink"], traffic.kinds_for(heavy, dict(remedies), stats))
        self.assertEqual(["transit", "bypass"], traffic.kinds_for({"diagnosis": {"externalShare": 0.3}},
                                                                   dict(remedies), stats))

    def watch(self, link):
        hotspot = {"segmentIds": [1, 2, 3], "meanDensity": 99.0, "segmentCount": 3, "center": self.CENTER,
                   "diagnosis": {"externalShare": 0.5}, "remedies": [{"kind": "transit", "reason": "cars"}]}
        tried = [{"key": "hotspot-1", "kind": kind, "wake": 9, "meanDensity": 99, "segments": 3,
                  "detail": {"target": target}, "verdict": "failed"} for kind, target in
                 (("transit", "line"), ("bypass", "connector"))]
        saved = {}
        city = mock.Mock()
        city.load_state.return_value = {"wake": 10, "hotspots": [{"segmentIds": [1, 2, 3], "seen": 2,
                                                                   "key": "hotspot-1"}], "actions": tried,
                                        "tried": {}, "kindStats": {}, "spent": {}, "escalatedAt": {}, "widened": {}}
        city.save_state.side_effect = lambda name, value: saved.update({name: value})
        proposal = {"status": "congested", "flowRatio": 0.7, "hotspots": [hotspot]}
        with mock.patch.object(traffic.mt, "call_tool", return_value=proposal), \
                mock.patch.object(traffic, "outside_link", return_value=link) as call:
            summary = traffic.watch(city, "t")
        return summary, saved["traffic"], call

    def test_a_spent_outside_heavy_hotspot_widens_its_link_before_the_planner_and_is_judged_later(self):
        link = {"ok": True, "prefab": "Large Road", "upgraded": [7, 8], "segments": [1, 2], "clear": []}
        summary, state, call = self.watch(link)
        self.assertTrue(call.called)
        self.assertEqual("outsideLink", summary["action"]["kind"])
        self.assertNotIn("escalation", summary)
        action = state["actions"][-1]
        self.assertEqual(("outsideLink", None, "link"), (action["kind"], action["verdict"], action["detail"]["target"]))
        self.assertEqual([7, 8], state["outsideLinked"]["hotspot-1"]["upgraded"])
        state["wake"] += traffic.JUDGE_AFTER
        verdicts = traffic.judge(state, [], [], "t")  # the hotspot is gone
        self.assertEqual([{"key": "hotspot-1", "kind": "outsideLink", "verdict": "worked"}], verdicts)

    def test_an_outside_link_with_nothing_to_widen_goes_on_to_the_planner(self):
        summary, state, _ = self.watch({"ok": None, "reason": "nothingSelectable", "prefab": "Large Road",
                                        "stoppedAt": [{"segmentId": 20, "wouldClear": 12, "homes": 11}]})
        self.assertNotIn("action", summary)
        self.assertEqual("hotspot-1", summary["escalation"]["key"])
        self.assertEqual("nothingSelectable", summary["outsideLinkNone"]["reason"])
        self.assertNotIn("hotspot-1", state.get("outsideLinked", {}))


class ServiceTests(unittest.TestCase):
    def setUp(self):
        # no capacity reading unless a test gives one: services.idle_load holds nothing back
        health = mock.patch.object(services.mt, "city_health", return_value={})
        health.start()
        self.addCleanup(health.stop)

    def test_a_placement_that_left_its_cluster_is_ineffective_for_its_need(self):
        state = {"wake": 6, "placed": [
            {"problem": "Crime", "family": "police", "need": "coverage", "prefab": "Small",
             "center": {"x": 0, "z": 0}, "count": 3, "wake": 2},
            {"problem": "Death", "family": "deathcare", "need": "capacity", "prefab": "Cemetery",
             "center": {"x": 0, "z": 0}, "count": 4, "wake": 2}]}
        assessment = {"problems": [{"problem": "Crime", "clusters": [{"center": {"x": 50, "z": 0}, "count": 3}]}]}
        verdicts = services.judge_placements(state, assessment)
        self.assertEqual(["ineffective", "worked"], [v["verdict"] for v in verdicts])
        self.assertEqual({"police:coverage": ["Small"], "deathcare:capacity": []}, state["ineffective"])

    def test_buildings_abandoned_of_an_economy_problem_count_toward_it(self):
        problems = [{"problem": "NoCustomers", "count": 20, "remedy": {"kind": "economy", "lever": "slowCommercial"}},
                    {"problem": "Garbage", "count": 5, "remedy": {"kind": "service", "family": "garbage"}}]
        counted = services.with_ruins(problems, {"NoCustomers": 6, "NoResources": 8, "Garbage": 2, "Fire": 1})
        by_name = {entry["problem"]: entry for entry in counted}
        self.assertEqual(26, by_name["NoCustomers"]["count"])
        self.assertEqual(5, by_name["Garbage"]["count"])  # a service problem is counted by its clusters
        self.assertEqual((8, "industrySupply"), (by_name["NoResources"]["count"],
                                                 by_name["NoResources"]["remedy"]["lever"]))
        self.assertNotIn("Fire", by_name)
        self.assertEqual(problems, services.with_ruins(problems, None))

    def test_shops_short_of_customers_for_four_wakes_get_land_rezoned_though_not_rising(self):
        assessment = {"problems": [{"problem": "NoCustomers", "count": 20, "buildingIds": [5],
                                    "remedy": {"kind": "economy", "lever": "slowCommercial"}}]}
        rezoned = [{"blockId": 1, "zone": "ResidentialHigh", "ok": True}]
        with tempfile.TemporaryDirectory() as folder:
            city = city_module.City({"instanceId": "test"}, Path(folder))
            with mock.patch.object(services.mt, "call_tool", return_value=assessment), \
                    mock.patch.object(services.growth, "rezone_idle_shops", return_value=rezoned) as rezone:
                reports = [services.solve(city, "t") for _ in range(4)]
        self.assertEqual(1, rezone.call_count)  # the fourth wake: four wakes at 20
        self.assertEqual([], reports[-1]["escalations"])

    def test_streak_counts_consecutive_wakes_only(self):
        state = {"wake": 1}
        lengths = []
        for wake in (1, 2, 4, 5, 6):
            state["wake"] = wake
            lengths.append(services.streak(state, "LineNotConnected"))
        self.assertEqual([1, 2, 1, 2, 3], lengths)

    def test_a_persisting_problem_goes_to_the_planner_only_after_it_stays(self):
        assessment = {"problems": [{"problem": "Flooded", "count": 3, "remedy": {"kind": "persist", "lever": "waterRecedes"}},
                                   {"problem": "Emptying", "count": 1, "remedy": {"kind": "status"}}]}
        with tempfile.TemporaryDirectory() as folder:
            city = city_module.City({"instanceId": "test"}, Path(folder))
            escalated = []
            with mock.patch.object(services.mt, "call_tool", return_value=assessment):
                for _ in range(services.PERSIST):
                    escalated.append(services.solve(city, "t")["escalations"])
        self.assertEqual([[]] * (services.PERSIST - 1), escalated[:-1])
        self.assertEqual([{"persists": "Flooded", "wakes": services.PERSIST, "count": 3, "lever": "waterRecedes"}],
                         escalated[-1])

    def test_two_clusters_of_one_family_get_one_facility_a_wake(self):
        clusters = [{"center": {"x": 0, "z": 0}, "count": 45, "nearestWorkingFacility": None},
                    {"center": {"x": 900, "z": 0}, "count": 10, "nearestWorkingFacility": None}]
        assessment = {"problems": [{"problem": "Garbage", "count": 55, "remedy": {"kind": "service", "family": "garbage"},
                                    "clusters": clusters}]}
        landfill = [{"name": "Landfill Site", "pollutionRadius": 150, "affordable": True}]
        placed = {"prefab": "Landfill Site", "ok": True, "regressionOk": True, "position": {"x": 0, "z": 0}}
        with tempfile.TemporaryDirectory() as folder:
            city = city_module.City({"instanceId": "test"}, Path(folder))
            with mock.patch.object(services.mt, "call_tool", return_value=assessment), \
                    mock.patch.object(services, "choose_prefabs", return_value=landfill), \
                    mock.patch.object(services, "place_near", return_value=placed) as place:
                report = services.solve(city, "t")
        self.assertEqual(1, place.call_count)
        self.assertEqual(1, len(report["actions"]))

    def test_one_building_persisting_within_reach_builds_nothing_but_three_do(self):
        near = {"id": 9445, "distance": 300}
        placed = {"prefab": "Combustion Plant", "ok": True, "regressionOk": True, "position": {"x": 0, "z": 0}}
        plant = [{"name": "Combustion Plant", "pollutionRadius": 150, "affordable": True}]
        for count, built in ((1, False), (3, True)):
            clusters = [{"center": {"x": 0, "z": 0}, "count": count, "nearestWorkingFacility": near}]
            assessment = {"problems": [{"problem": "Garbage", "count": count,
                                        "remedy": {"kind": "service", "family": "garbage"}, "clusters": clusters}]}
            with tempfile.TemporaryDirectory() as folder:
                city = city_module.City({"instanceId": "test"}, Path(folder))
                with mock.patch.object(services.mt, "call_tool", return_value=assessment), \
                        mock.patch.object(services, "choose_prefabs", return_value=plant), \
                        mock.patch.object(services, "place_near", return_value=placed) as place:
                    for _ in range(services.PERSIST + 1):
                        services.solve(city, "t")
            self.assertEqual(built, place.called, count)

    def test_a_fire_station_goes_where_most_buildings_lie_beyond_every_station(self):
        station = {"facilities": [{"position": {"x": 0, "z": 0}}]}
        near = [{"id": i, "position": {"x": 100 + i, "z": 0}} for i in range(60)]
        far = [{"id": 100 + i, "position": {"x": 2000 + i, "z": 2000}} for i in range(services.FIRE_COVERAGE_MIN)]
        few = [{"id": 200 + i, "position": {"x": -3000, "z": 0}} for i in range(5)]

        def request(method, path, query=None, **kw):
            return station if path == "/state/facilities" else {"growables": near + far + few}
        options = {"options": []}
        with mock.patch.object(services.mt, "_request", side_effect=request), \
                mock.patch.object(services.mt, "call_tool", return_value=options):
            gap = services.fire_gap()
        self.assertEqual(services.FIRE_COVERAGE_MIN, gap["count"])
        self.assertAlmostEqual(2000 + (services.FIRE_COVERAGE_MIN - 1) / 2, gap["center"]["x"])
        with mock.patch.object(services.mt, "_request",
                               side_effect=lambda method, path, query=None, **kw: station if path == "/state/facilities"
                               else {"growables": near + few}), \
                mock.patch.object(services.mt, "call_tool", return_value=options):
            self.assertIsNone(services.fire_gap())  # five beyond reach get no station
        # a station's own radius counts: a 3000m one covers the far buildings too
        wide = {"facilities": [{"prefab": "Big Station", "position": {"x": 0, "z": 0}}]}
        with mock.patch.object(services.mt, "_request",
                               side_effect=lambda method, path, query=None, **kw: wide if path == "/state/facilities"
                               else {"growables": near + far + few}), \
                mock.patch.object(services.mt, "call_tool",
                                  return_value={"options": [{"name": "Big Station", "roles": {"radius": 3000.0}}]}):
            self.assertIsNone(services.fire_gap())

    def test_a_fire_coverage_gap_gets_one_station_a_wake(self):
        gap = {"center": {"x": 2000, "z": 2000}, "count": 300}
        placed = {"prefab": "firehouse_EU", "ok": True, "regressionOk": True, "position": gap["center"]}
        with tempfile.TemporaryDirectory() as folder:
            city = city_module.City({"instanceId": "test"}, Path(folder))
            burned = {"problems": [{"problem": "BurnedDown", "count": 3, "remedy": {"kind": "bulldoze"}}]}
            with mock.patch.object(services.mt, "call_tool", return_value=burned), \
                    mock.patch.object(services, "fire_gap", return_value=gap), \
                    mock.patch.object(services, "choose_prefabs",
                                      return_value=[{"name": "firehouse_EU", "affordable": True}]), \
                    mock.patch.object(services, "place_near", return_value=placed) as place:
                report = services.solve(city, "t")
        self.assertEqual(1, place.call_count)
        self.assertEqual(("firehouse_EU", gap["center"]), place.call_args.args[1:3])
        self.assertEqual(["FireCoverage"], [action.get("problem") for action in report["actions"]])

    def test_a_rising_economy_problem_is_reported_once_then_only_every_so_many_wakes(self):
        state = {"wake": 1}
        rising = [{"economyRising": "NoResources", "counts": [5, 7, 11, 13]}]
        reports = []
        for wake in range(1, services.SERVICE_REPORT_EVERY + 2):
            state["wake"] = wake
            reports.append(services.fresh_reports(state, list(rising)))
        self.assertEqual([1, services.SERVICE_REPORT_EVERY + 1],
                         [index + 1 for index, items in enumerate(reports) if items])
        # another problem is its own report
        self.assertEqual(1, len(services.fresh_reports(state, [{"economyRising": "NoWorkers", "counts": [1, 2, 3, 9]}])))

    def test_an_unresolved_cluster_is_reported_once_then_only_every_so_many_wakes(self):
        clusters = [{"center": {"x": 0, "z": 0}, "count": 4, "nearestWorkingFacility": {"id": 1, "distance": 1356}}]
        assessment = {"problems": [{"problem": "Garbage", "count": 4, "remedy": {"kind": "service", "family": "garbage"},
                                    "clusters": clusters}]}
        plant = [{"name": "Landfill Site", "pollutionRadius": 150, "affordable": True}]
        reports = []
        with tempfile.TemporaryDirectory() as folder:
            city = city_module.City({"instanceId": "test"}, Path(folder))
            with mock.patch.object(services.mt, "call_tool", return_value=assessment), \
                    mock.patch.object(services, "choose_prefabs", return_value=plant), \
                    mock.patch.object(services, "place_near", return_value={"ok": False, "stage": "noSite"}) as place, \
                    mock.patch.object(services, "open_land", return_value={"ok": False, "stage": "noLand"}), \
                    mock.patch.object(services, "raise_budget", return_value={"ok": True}):
                for _ in range(services.SERVICE_REPORT_EVERY + 2):
                    reports.append(services.solve(city, "t")["escalations"])
        reported = [index for index, escalations in enumerate(reports) if escalations]
        self.assertEqual(1, len(reported))  # not again every wake
        self.assertGreater(place.call_count, 2)  # the site search is tried again between reports

    def test_a_site_with_a_growable_that_may_not_be_cleared_is_passed_for_the_next(self):
        sites = {"sites": [
            {"position": {"x": 0, "z": 0}, "angleDegrees": 0, "coveredGrowables": [{"id": 1}, {"id": 2}]},
            {"position": {"x": 200, "z": 0}, "angleDegrees": 0, "coveredGrowables": [{"id": 3}]}]}

        def tool(name, args):
            if name == "propose_service_sites":
                return sites
            return {"safe": args["id"] != 2}  # the first site's second building may not go
        for unsafe_everywhere in (False, True):
            runs = []
            with mock.patch.object(services.mt, "call_tool",
                                   side_effect=(lambda name, args: {"safe": False} if name != "propose_service_sites"
                                                else sites) if unsafe_everywhere else tool), \
                    mock.patch.object(services.protocol, "run",
                                      side_effect=lambda label, t, args, *a: runs.append((t, args)) or
                                      {"ok": True, "regressionOk": True, "result": {"buildingId": 9}}):
                result = services.place_near("s", "Elementary_School_EU", {"x": 0, "z": 0})
            if unsafe_everywhere:
                self.assertEqual((None, "sitesTaken", []), (result["ok"], result["stage"], runs))
            else:
                # nothing on the first site is cleared, the second site is used
                self.assertEqual([("bulldoze_entity", {"entityType": "building", "id": 3}), "place_service"],
                                 [runs[0], runs[1][0]])
                self.assertEqual(({"x": 200, "z": 0}, [3]), (result["position"], result["cleared"]))

    def school_need(self, nearest=None):
        clusters = [{"center": {"x": 0, "z": 0}, "count": 102, "nearestWorkingFacility": nearest}]
        return {"problems": [{"problem": "HighSchoolShort", "count": 102,
                              "remedy": {"kind": "service", "family": "highSchool"}, "clusters": clusters}]}

    def test_the_first_building_of_a_family_may_take_half_the_cash(self):
        options = {"options": [{"name": "highschool_EU", "constructionCost": 2400000, "affordable": True}]}
        placed = {"prefab": "highschool_EU", "ok": True, "regressionOk": True, "position": {"x": 0, "z": 0}}
        with tempfile.TemporaryDirectory() as folder:
            city = city_module.City({"instanceId": "test"}, Path(folder))
            with mock.patch.object(services.mt, "call_tool", side_effect=lambda tool, args: options
                                   if tool == "compare_service_options" else self.school_need()) as tools, \
                    mock.patch.object(services, "cash", return_value=8030772.0), \
                    mock.patch.object(services, "place_near", return_value=placed):
                report = services.solve(city, "t")
        budgets = [args["budget"] for tool, args in (call.args for call in tools.call_args_list)
                   if tool == "compare_service_options"]
        self.assertEqual([8030772.0 * services.FIRST_SPEND_SHARE], budgets)
        self.assertTrue(report["actions"][0]["ok"])

    def test_a_building_the_city_cannot_afford_yet_is_saved_for_not_funded_by_budget(self):
        options = {"options": [{"name": "highschool_EU", "constructionCost": 2400000, "affordable": False}]}
        with tempfile.TemporaryDirectory() as folder:
            city = city_module.City({"instanceId": "test"}, Path(folder))
            with mock.patch.object(services.mt, "call_tool", side_effect=lambda tool, args: options
                                   if tool == "compare_service_options" else self.school_need()), \
                    mock.patch.object(services, "cash", return_value=3000000.0), \
                    mock.patch.object(services, "place_near") as place, \
                    mock.patch.object(services, "raise_budget") as budget:
                reports = [services.solve(city, "t") for _ in range(services.SAVING_WAKES)]
        place.assert_not_called()
        budget.assert_not_called()
        self.assertEqual(("saving", None, "highschool_EU"), tuple(reports[0]["actions"][0][key]
                                                                  for key in ("stage", "ok", "prefab")))
        self.assertEqual([], reports[-2]["escalations"])
        self.assertEqual("serviceUnaffordable", next(iter(reports[-1]["escalations"][0])))
        self.assertFalse(play.failed(reports[0]["actions"][0]))

    def test_no_site_raises_a_budget_only_for_a_family_with_buildings_to_fund(self):
        options = {"options": [{"name": "highschool_EU", "constructionCost": 2400000, "affordable": True}]}
        nearest = {"id": 7, "distance": 2000}  # beyond any school reach
        for found, raised in ((None, 0), (nearest, 1)):
            with tempfile.TemporaryDirectory() as folder:
                city = city_module.City({"instanceId": "test"}, Path(folder))
                with mock.patch.object(services.mt, "call_tool", side_effect=lambda tool, args: options
                                       if tool == "compare_service_options" else self.school_need(found)), \
                        mock.patch.object(services, "cash", return_value=8030772.0), \
                        mock.patch.object(services, "place_near", return_value={"ok": False, "stage": "noSite"}), \
                        mock.patch.object(services, "raise_budget", return_value={"ok": True}) as budget:
                    report = services.solve(city, "t")
            self.assertEqual(raised, budget.call_count)
            self.assertFalse(play.failed(report["actions"][0]))

    def test_a_road_is_laid_into_open_land_until_its_end_clears_the_homes(self):
        homes = {"growables": [{"service": "Residential", "position": {"x": 0, "z": 0}}]}
        area = {"center": {"x": 0, "z": -1000}, "access": [
            {"clear": True, "crosses": [], "from": {"x": 0, "z": -100}, "to": {"x": 0, "z": -150}}]}
        steps = iter([{"ok": True, "end": {"x": 0, "z": -150 - 88 * n}} for n in range(1, 9)])
        with mock.patch.object(services.mt, "_request", return_value=homes), \
                mock.patch.object(services.mt, "call_tool", return_value={"areas": [area]}), \
                mock.patch.object(services.growth, "open_access", return_value={"ok": True, "end": (0, -150)}), \
                mock.patch.object(services.mt, "_land_use_context", return_value={"wells": []}), \
                mock.patch.object(services.roads, "build", side_effect=lambda *a, **k: next(steps)) as build:
            land = services.open_land(None, "t", 300)
        self.assertTrue(land["ok"])
        self.assertGreaterEqual(-land["end"]["z"], 300 + services.LOT_DEPTH)  # 150 + 3 pieces of 88m
        self.assertEqual(3, build.call_count)

    def test_open_land_runs_on_until_it_clears_a_groundwater_pump_too(self):
        homes = {"growables": [{"service": "Residential", "position": {"x": 0, "z": 0}}]}
        area = {"center": {"x": 0, "z": -1000}, "access": [
            {"clear": True, "crosses": [], "from": {"x": 0, "z": -100}, "to": {"x": 0, "z": -150}}]}
        steps = iter([{"ok": True, "end": {"x": 0, "z": -150 - 88 * n}} for n in range(1, 9)])
        with mock.patch.object(services.mt, "_request", return_value=homes), \
                mock.patch.object(services.mt, "call_tool", return_value={"areas": [area]}), \
                mock.patch.object(services.growth, "open_access", return_value={"ok": True, "end": (0, -150)}), \
                mock.patch.object(services.mt, "_land_use_context", return_value={"wells": [(0.0, -300.0)]}), \
                mock.patch.object(services.roads, "build", side_effect=lambda *a, **k: next(steps)) as build:
            land = services.open_land(None, "t", 300)
        self.assertTrue(land["ok"])
        self.assertGreaterEqual(-land["end"]["z"] - 300, services.mt.GROUNDWATER_CLEARANCE + services.LOT_DEPTH)
        self.assertEqual(6, build.call_count)  # 150 + 6 pieces of 88m


class ModelCrossCheckTests(unittest.TestCase):
    def outcome(self, model_problems):
        return {"checks": [{"key": "population", "expected": 9, "model": 9, "match": True},
                           {"key": "problems", "expected": ["Water"], "model": model_problems,
                            "match": model_problems == ["Water"]}]}

    def read(self, answers):
        crosscheck = mock.Mock()
        crosscheck.run_extract.side_effect = [self.outcome(answer) for answer in answers]
        with tempfile.TemporaryDirectory() as folder:
            city = city_module.City({"instanceId": "test"}, Path(folder))
            with mock.patch.object(wake.mt, "call_tool", return_value={}), \
                    mock.patch.object(wake, "_model", return_value=(crosscheck, object())):
                return wake.wake(city, 0, use_model=True)["modelMismatches"], crosscheck.run_extract.call_count

    def test_a_reading_the_model_misses_once_is_asked_again(self):
        self.assertEqual(([], 1), self.read([["Water"]]))
        self.assertEqual(([], 2), self.read([[], ["Water"]]))  # a slip, right when asked again
        misses, asked = self.read([[], []])
        self.assertEqual((["problems"], 2), ([miss["key"] for miss in misses], asked))

    def test_the_second_reading_is_a_different_prompt_over_the_missed_field_only(self):
        crosscheck = mock.Mock()
        crosscheck.run_extract.side_effect = [self.outcome([]), self.outcome(["Water"])]
        with tempfile.TemporaryDirectory() as folder:
            city = city_module.City({"instanceId": "test"}, Path(folder))
            with mock.patch.object(wake.mt, "call_tool", return_value={}), \
                    mock.patch.object(wake, "_model", return_value=(crosscheck, object())):
                wake.wake(city, 0, use_model=True)
        second = crosscheck.run_extract.call_args_list[1].args[0]
        self.assertEqual(["problems"], [q["key"] for q in second["questions"]])
        self.assertEqual(["problems.countsByProblem"], second["focus"])


class PowerRepairTests(unittest.TestCase):
    def test_buildings_no_line_joins_do_not_use_up_the_lines(self):
        buildings = [{"id": i, "position": {"x": 900.0 + i, "z": 400.0}} for i in (1, 2, 3, 4)]
        coverage = {"summary": {"missingElectricity": 4}, "buildings": buildings}
        line = {"powerLinePrefab": "Power Line", "start": {"x": 0, "z": 0}, "end": {"x": 1, "z": 0}}

        def call(tool, arguments):
            if tool == "inspect_electricity_service_coverage":
                return coverage
            if tool == "propose_power_line_connection":
                found = arguments["buildingId"] == 4
                return {"actionRequired": True, "safe": found, "candidate": line if found else None}
            raise AssertionError(tool)
        shortfall = {"notCovered": [{"id": i} for i in (1, 2, 3, 4)], "verdict": ["notCovered"]}
        with mock.patch.object(utilities.mt, "call_tool", side_effect=call), \
                mock.patch.object(utilities, "shortfall", return_value=shortfall), \
                mock.patch.object(utilities.protocol, "run", return_value={"ok": True, "regressionOk": True}), \
                mock.patch.object(utilities, "power_tree", return_value=[]) as tree:
            report = utilities.repair_power("t")
        self.assertEqual([4], [step["building"] for step in report["steps"]])
        self.assertEqual([1, 2, 3], tree.call_args.args[1])
        self.assertEqual((902.0, 400.0), tree.call_args.args[2])  # the tree starts near them
        self.assertEqual([1, 2, 3, 4], report["unreached"])
        self.assertIsNone(report["island"])  # a line went through: no plant of their own

    def test_buildings_no_line_reaches_get_a_plant_among_them(self):
        far = [{"id": i, "position": {"x": 800.0, "z": -700.0 - 10 * i}} for i in (1, 2)]
        coverage = {"summary": {"missingElectricity": 2}, "buildings": far}

        def call(tool, arguments):
            if tool == "inspect_electricity_service_coverage":
                return coverage
            if tool == "propose_power_line_connection":
                return {"actionRequired": True, "safe": False, "candidate": None}
            raise AssertionError(tool)
        with mock.patch.object(utilities.mt, "call_tool", side_effect=call), \
                mock.patch.object(utilities, "shortfall", return_value={"notCovered": far}), \
                mock.patch.object(utilities, "power_tree", return_value=[]), \
                mock.patch.object(utilities, "place_plant", return_value={"ok": True}) as place:
            report = utilities.repair_power("t")
        self.assertEqual((800.0, -715.0), place.call_args.args[1])
        self.assertEqual({"ok": True}, report["island"])


class WaterBootstrapTests(unittest.TestCase):
    intake, outlet, town = (760.0, -340.0), (500.0, -900.0), (900.0, 750.0)

    def test_the_three_pipes_at_the_junction_part_widely(self):
        found = utilities.junctions(self.intake, self.outlet, self.town)
        self.assertTrue(found)
        for j in found[:5]:
            ways = [utilities._bearing(j, p) for p in (self.intake, self.outlet, self.town)]
            spread = min(abs((p - q + 180) % 360 - 180) for i, p in enumerate(ways) for q in ways[i + 1:])
            self.assertGreaterEqual(spread, utilities.JUNCTION_SPREAD)
        avoided = utilities.junctions(self.intake, self.outlet, self.town, [found[0]])
        self.assertNotIn(found[0], avoided)

    def test_a_plan_has_one_branch_per_facility_and_no_straight_joints(self):
        j = utilities.junctions(self.intake, self.outlet, self.town)[0]
        segments = utilities.water_plan(self.intake, self.outlet, self.town, j)
        roles = [s["role"] for s in segments]
        self.assertEqual((1, 1), (roles.count("intakeBranch"), roles.count("outletBranch")))
        point = lambda p: (p["x"], p["z"])  # noqa: E731
        self.assertTrue(all(math.dist(point(s["start"]), point(s["end"])) <= utilities.PIPE_PIECE + 1 for s in segments))
        degree: dict = {}
        for s in segments:
            for end in (point(s["start"]), point(s["end"])):
                degree[end] = degree.get(end, 0) + 1
        self.assertEqual(3, max(degree.values()))  # the junction
        for s in segments:
            for t in segments:
                if point(s["end"]) == point(t["start"]) and degree[point(t["start"])] == 2:
                    turn = abs((utilities._bearing(point(t["start"]), point(t["end"]))
                                - utilities._bearing(point(s["start"]), point(s["end"])) + 180) % 360 - 180)
                    self.assertGreater(turn, 5.0)


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        protocol.new_batch()
        self.addCleanup(protocol.new_batch)

    def _tools(self, created_ok=True):
        calls = []

        def call_tool(name, arguments):
            calls.append((name, dict(arguments)))
            if name == "create_checkpoint":
                return {"ok": created_ok, "checkpointName": f"AgentCheckpoint-{len(calls)}-{arguments['name']}"}
            if name == "verify_regression":
                return {"ok": True, "delta": {}}
            return {"ok": True}
        return calls, call_tool

    def test_one_save_serves_every_change_of_a_batch_each_with_its_own_baseline(self):
        calls, call_tool = self._tools()
        with mock.patch.object(protocol.mt, "call_tool", side_effect=call_tool), \
                mock.patch.object(protocol, "wait_save"):
            first = protocol.run("zone-a", "zone_block", {"blockId": 1})
            second = protocol.run("zone-b", "zone_block", {"blockId": 2})
            protocol.new_batch()
            third = protocol.run("svc", "bulldoze_entity", {"entityType": "building", "id": 3})
        names = [name for name, _ in calls]
        self.assertEqual(2, names.count("create_checkpoint"))
        self.assertEqual(["zone-a-baseline", "zone-b-baseline", "svc-baseline"],
                         [args["name"] for name, args in calls if name == "capture_regression_baseline"])
        self.assertEqual(["zone-a-baseline", "zone-b-baseline", "svc-baseline"],
                         [args["name"] for name, args in calls if name == "verify_regression"])
        used = [args["checkpointName"] for name, args in calls if name in ("zone_block", "bulldoze_entity")]
        self.assertEqual(used[0], used[1])  # the second change reuses the batch's save
        self.assertNotEqual(used[1], used[2])  # a new batch saves again
        self.assertTrue(used[0].endswith("pre-zone-a") and used[2].endswith("pre-svc"))
        self.assertTrue(all(outcome["ok"] for outcome in (first, second, third)))

    def test_a_batch_save_is_taken_again_once_it_is_old(self):
        calls, call_tool = self._tools()
        clock = iter([0.0, 10.0, 10.0 + protocol.CHECKPOINT_MAX_AGE_SECONDS + 1, 2000.0])
        with mock.patch.object(protocol.mt, "call_tool", side_effect=call_tool), \
                mock.patch.object(protocol, "wait_save"), \
                mock.patch.object(protocol.time, "monotonic", side_effect=lambda: next(clock)):
            names = [protocol.checkpoint(label) for label in ("a", "b", "c")]
        self.assertEqual(names[0], names[1])
        self.assertNotEqual(names[1], names[2])

    def test_a_save_the_bridge_could_not_verify_is_not_reused(self):
        calls, call_tool = self._tools(created_ok=False)
        with mock.patch.object(protocol.mt, "call_tool", side_effect=call_tool), \
                mock.patch.object(protocol, "wait_save"):
            protocol.checkpoint("a")
            protocol.checkpoint("b")
        self.assertEqual(2, [name for name, _ in calls].count("create_checkpoint"))

    def test_wait_save_polls_the_save_status_until_the_save_is_written(self):
        statuses = iter([{"isSaving": True}, {"isSaving": True}, {"isSaving": False}])
        with mock.patch.object(protocol.mt, "_save_status", side_effect=lambda: next(statuses)) as status, \
                mock.patch.object(protocol.mt, "_request") as request, \
                mock.patch.object(protocol.time, "sleep") as sleep:
            protocol.wait_save()
        self.assertEqual(3, status.call_count)
        request.assert_not_called()  # never the game-thread listing of every save
        sleep.assert_called_with(protocol.SAVE_POLL_SECONDS)

    def test_wait_save_reads_the_listing_from_an_older_bridge_and_gives_up_in_time(self):
        clock = iter(range(0, 1000, 60))
        with mock.patch.object(protocol.mt, "_save_status", return_value=None), \
                mock.patch.object(protocol.mt, "_request", return_value={"isSaving": True}) as request, \
                mock.patch.object(protocol.time, "monotonic", side_effect=lambda: next(clock)), \
                mock.patch.object(protocol.time, "sleep"), \
                self.assertRaises(RuntimeError):
            protocol.wait_save(180)
        self.assertEqual(("GET", "/state/saves"), request.call_args.args)

    def _save_folder(self, root: Path, checkpoints: int) -> Path:
        import os
        folder = Path(root) / "Saves"
        folder.mkdir()
        names = [f"AgentCheckpoint-202611{index:04d}-pre-x" for index in range(checkpoints)]
        for index, name in enumerate(names + ["Player stage save", "AgentAutoPaused-1", "Player_old"]):
            path = folder / f"{name}.crp"
            path.write_bytes(b"x")
            # checkpoint i was written at minute i; the player's saves are older than every one
            os.utime(path, (1_000_000 + index * 60, 1_000_000 + index * 60) if index < checkpoints else (10, 10))
        return folder

    def test_old_checkpoints_are_pruned_a_few_a_wake_oldest_first(self):
        with tempfile.TemporaryDirectory() as root:
            folder = self._save_folder(root, 30)
            with mock.patch.object(protocol.mt, "_save_status",
                                   return_value={"isSaving": False, "directory": str(folder)}), \
                    mock.patch.object(protocol.time, "sleep") as sleep:
                outcome = protocol.prune_checkpoints(keep=20, limit=5)
            left = sorted(path.stem for path in folder.glob("*.crp"))
        self.assertEqual((5, 25, []), (outcome["pruned"], outcome["remaining"], outcome["failed"]))
        self.assertEqual(4, sleep.call_count)  # spaced, so the game's file watcher sees each deletion
        gone = {f"AgentCheckpoint-202611{index:04d}-pre-x" for index in range(5)}
        self.assertFalse(gone & set(left))
        # never anything but an agent checkpoint, however old
        self.assertTrue({"Player stage save", "AgentAutoPaused-1", "Player_old"} <= set(left))
        self.assertEqual(28, len(left))

    def test_pruning_keeps_the_newest_and_the_save_continue_loads(self):
        with tempfile.TemporaryDirectory() as root:
            folder = self._save_folder(root, 24)
            (folder.parent / "continue_game.json").write_text(
                json.dumps({"title": "AgentCheckpoint-2026110000-pre-x"}), encoding="utf-8")
            with mock.patch.object(protocol.mt, "_save_status",
                                   return_value={"isSaving": False, "directory": str(folder)}), \
                    mock.patch.object(protocol.time, "sleep"):
                outcome = protocol.prune_checkpoints(keep=20, limit=50)
            left = {path.stem for path in folder.glob("AgentCheckpoint-*.crp")}
        self.assertEqual(3, outcome["pruned"])
        self.assertEqual({f"AgentCheckpoint-202611{index:04d}-pre-x" for index in [0] + list(range(4, 24))}, left)

    def test_nothing_is_pruned_during_a_save_or_without_a_save_status(self):
        with tempfile.TemporaryDirectory() as root:
            folder = self._save_folder(root, 30)
            for status in ({"isSaving": True, "directory": str(folder)}, None,
                           protocol.mt.BridgeRequestError({"ok": False, "error": "busy"})):
                kwargs = {"side_effect": status} if isinstance(status, Exception) else {"return_value": status}
                with mock.patch.object(protocol.mt, "_save_status", **kwargs):
                    self.assertEqual(0, protocol.prune_checkpoints(keep=20)["pruned"])
            self.assertEqual(33, len(list(folder.glob("*.crp"))))

    def test_a_checkpoint_the_game_holds_open_is_left_for_later(self):
        with tempfile.TemporaryDirectory() as root:
            folder = self._save_folder(root, 22)
            real_unlink = Path.unlink

            def unlink(path, *args, **kwargs):
                if path.stem.endswith("0000-pre-x"):
                    raise PermissionError("in use")
                return real_unlink(path, *args, **kwargs)

            with mock.patch.object(protocol.mt, "_save_status",
                                   return_value={"isSaving": False, "directory": str(folder)}), \
                    mock.patch.object(protocol.time, "sleep"), mock.patch.object(Path, "unlink", unlink):
                outcome = protocol.prune_checkpoints(keep=20)
        self.assertEqual(1, outcome["pruned"])
        self.assertEqual(["AgentCheckpoint-2026110000-pre-x"], [item["name"] for item in outcome["failed"]])

    def test_a_checkpoint_the_game_cannot_take_is_a_failed_action(self):
        busy = protocol.mt.BridgeRequestError("Timed out waiting for the game thread")
        with mock.patch.object(protocol, "checkpoint", side_effect=[busy, busy]) as checkpoint, \
                mock.patch.object(protocol.time, "sleep"), \
                mock.patch.object(protocol.mt, "call_tool") as call_tool:
            outcome = protocol.run("a", "zone_block", {"blockId": 1})
        self.assertFalse(outcome["ok"])
        self.assertTrue(outcome["error"].startswith("checkpoint"))
        self.assertEqual(2, checkpoint.call_count)
        call_tool.assert_not_called()  # nothing was changed without a checkpoint

    def test_a_refused_mutation_is_a_failed_action_not_a_crash(self):
        def call_tool(name, arguments):
            if name == "zone_block":
                raise protocol.mt.BridgeRequestError("skipped by a safety gate")
            return {"checkpointName": "cp", "ok": True}
        with mock.patch.object(protocol.mt, "call_tool", side_effect=call_tool), \
                mock.patch.object(protocol, "wait_save"):
            outcome = protocol.run("zone-x", "zone_block", {"zone": "Industrial", "blockId": 1})
        self.assertFalse(outcome["ok"])
        self.assertIn("safety gate", outcome["error"])
        self.assertTrue(outcome["regressionOk"])


class HomeLandTests(unittest.TestCase):
    def test_a_residential_district_does_not_start_inside_industrys_reach(self):
        industry = [(0.0, 0.0)]
        inside = {"to": {"x": 150, "z": 0}}
        beyond = {"to": {"x": 400, "z": 0}}
        self.assertFalse(growth._homes_may_grow(inside, industry))
        self.assertTrue(growth._homes_may_grow(beyond, industry))
        self.assertTrue(growth._homes_may_grow(inside, []))


class FailedRoadTests(unittest.TestCase):
    def test_a_road_that_fails_its_checks_is_taken_out_and_the_next_angle_tried(self):
        previews = iter([(True, {"start": {}}, {}), (True, {"start": {}}, {})])
        built = iter([{"ok": True, "segmentId": 11}, {"ok": True, "segmentId": 12}])
        checks = iter([{"ok": False, "focusedAnomalies": [{"type": "roadBelowTerrain"}]}, {"ok": True}])

        def tool(name, args):
            if name == "build_network":
                return next(built)
            return next(checks)

        with mock.patch.object(roads, "preview", side_effect=lambda *a, **k: next(previews)), \
                mock.patch.object(roads.protocol, "checkpoint", return_value="c"), \
                mock.patch.object(roads.protocol, "verify", return_value={"ok": True}), \
                mock.patch.object(roads.protocol, "run", return_value={"ok": True}) as run, \
                mock.patch.object(roads.mt, "_request", return_value={"startNodeId": 1, "endNodeId": 2,
                                                                     "end": {"x": 0, "z": 0}}), \
                mock.patch.object(roads.mt, "call_tool", side_effect=tool):
            first = roads.build("c1", "Basic Road", (0, 0), (88, 0))
            second = roads.build("c1", "Basic Road", (0, 0), (80, 30))
        self.assertEqual(("checks", True, ["roadBelowTerrain"]), (first["stage"], first["removed"], first["anomalies"]))
        self.assertEqual(("bulldoze_entity", 11), (run.call_args.args[1], run.call_args.args[2]["id"]))
        self.assertTrue(roads.retryable(first))
        self.assertTrue(second["ok"])

    def test_a_piece_of_a_larger_change_is_kept_for_the_caller_to_remove(self):
        with mock.patch.object(roads, "preview", return_value=(True, {"start": {}}, {})), \
                mock.patch.object(roads.protocol, "checkpoint", return_value="c"), \
                mock.patch.object(roads.protocol, "verify", return_value={"ok": True}), \
                mock.patch.object(roads.protocol, "run") as run, \
                mock.patch.object(roads.mt, "_request", return_value={"startNodeId": 1, "endNodeId": 2,
                                                                     "end": {"x": 0, "z": 0}}), \
                mock.patch.object(roads.mt, "call_tool", side_effect=lambda name, args: {"ok": True, "segmentId": 11}
                                  if name == "build_network" else {"ok": False}):
            leg = roads.build("outside-0", "Basic Road", (0, 0), (88, 0), keep_failed=True)
        run.assert_not_called()  # a dead-end leg of a highway link stands until the link is judged
        self.assertEqual((False, 11, "checks", False), (leg["ok"], leg["segmentId"], leg["stage"], leg["removed"]))
        self.assertFalse(roads.retryable(leg))


class OrphanTests(unittest.TestCase):
    def test_a_building_no_local_road_reaches_is_an_orphan_and_a_highway_does_not_serve_it(self):
        roads = {"segments": [
            {"prefab": "Basic Road", "start": {"x": 0, "z": 0}, "end": {"x": 100, "z": 0}},
            {"prefab": "Highway", "start": {"x": 0, "z": 200}, "end": {"x": 100, "z": 200}}]}
        growables = {"growables": [
            {"id": 1, "service": "Commercial", "position": {"x": 50, "z": 20}, "spatial3d": {"lengthMeters": 16}},
            {"id": 2, "service": "Commercial", "position": {"x": 50, "z": 190}, "spatial3d": {"lengthMeters": 16}}]}
        with mock.patch.object(cycle.mt, "_request",
                               side_effect=lambda method, path, **kw: roads if path == "/state/networks" else growables):
            found = cycle.orphaned_buildings()
        self.assertEqual([2], [item["id"] for item in found])  # 190m from the road, 10m from a highway

    def test_a_small_building_behind_the_front_row_is_no_orphan(self):
        roads = {"segments": [{"prefab": "Basic Road", "start": {"x": 0, "z": 0}, "end": {"x": 100, "z": 0}}]}
        growables = {"growables": [
            {"id": 1, "service": "Residential", "position": {"x": 50, "z": 38.9}, "spatial3d": {"lengthMeters": 8}},
            {"id": 2, "service": "Residential", "position": {"x": 50, "z": 71.0}, "spatial3d": {"lengthMeters": 8}}]}
        with mock.patch.object(cycle.mt, "_request",
                               side_effect=lambda method, path, **kw: roads if path == "/state/networks" else growables):
            found = cycle.orphaned_buildings()
        self.assertEqual([2], [item["id"] for item in found])  # 38.9m: a blockhouse that grew there (live)


class SpeedRetryTests(unittest.TestCase):
    def test_a_speed_change_the_game_thread_did_not_run_is_sent_again(self):
        busy = wake.mt.BridgeRequestError({"ok": False, "error": "Timed out waiting for the game thread; the command was not run."})
        with mock.patch.object(wake.mt, "call_tool", side_effect=[busy, {"ok": True, "paused": True}]) as call, \
                mock.patch.object(wake.time, "sleep"):
            self.assertEqual({"ok": True, "paused": True}, wake.set_speed({"paused": True}))
        self.assertEqual(2, call.call_count)
        other = wake.mt.BridgeRequestError({"ok": False, "error": "Unknown speed"})
        with mock.patch.object(wake.mt, "call_tool", side_effect=other), mock.patch.object(wake.time, "sleep"):
            with self.assertRaises(wake.mt.BridgeRequestError):
                wake.set_speed({"paused": True})  # anything else is not retried
        with mock.patch.object(wake.mt, "call_tool", side_effect=[busy] * wake.SPEED_TRIES), \
                mock.patch.object(wake.time, "sleep"):
            with self.assertRaises(wake.mt.BridgeRequestError):
                wake.set_speed({"paused": True})  # a thread busy every time still ends the wake


class SaveStuckTests(unittest.TestCase):
    def test_a_save_that_never_finishes_stops_the_loop_at_once(self):
        self.assertEqual("saveStuck", play.save_stuck('RuntimeError: save did not finish\n')["stop"])
        self.assertIsNone(play.save_stuck("Timed out waiting for the game thread"))
        self.assertIsNone(play.save_stuck(None))


class RetryTests(unittest.TestCase):
    def test_a_retried_wake_runs_no_clock(self):
        self.assertEqual("60", play.wake_command("60", 0, True)[3])
        self.assertEqual(["0", "0"], [play.wake_command("60", attempt, True)[3] for attempt in (1, 2)])
        self.assertEqual("--model", play.wake_command("60", 1, True)[-1])


class ModelOffByDefaultTests(unittest.TestCase):
    SNAPSHOT = {"snapshot": {"gameTime": "2030-01-01"}, "cityHealth": {"population": 9},
                "problems": {"countsByProblem": {"Water": 2}},
                "progression": {"currentMilestone": "M1", "capabilities": {"a": True, "b": False}}}

    def read(self, **options):
        crosscheck = mock.Mock()
        crosscheck.run_extract.return_value = {"checks": [
            {"key": key, "expected": None, "model": None, "match": True} for key, *_ in wake.OBSERVE]}
        with tempfile.TemporaryDirectory() as folder:
            city = city_module.City({"instanceId": "test"}, Path(folder))
            with mock.patch.object(wake.mt, "call_tool", return_value=self.SNAPSHOT), \
                    mock.patch.object(wake, "_model", return_value=(crosscheck, object())) as model:
                return wake.wake(city, 0, **options), model.call_count, (Path(folder) / "model-journal.jsonl").exists()

    def test_a_wake_reads_without_the_model_unless_asked(self):
        report, asked, journal = self.read()
        self.assertEqual((0, False), (asked, journal))
        self.assertEqual(("off", False, []), (report["model"], report["modelChecked"], report["modelMismatches"]))
        self.assertEqual((9, ["Water"], "M1", ["a"]),
                         (report["population"], report["problems"], report["milestone"], report["unlocked"]))
        on, asked, _ = self.read(use_model=True)
        self.assertEqual(("on", True, 1), (on["model"], on["modelChecked"], asked))

    def test_the_cycle_asks_for_the_model_only_with_the_flag(self):
        self.assertFalse(cycle.model_requested(["cycle", "60"]))  # what an older play.py passes
        self.assertFalse(cycle.model_requested(["cycle", "60", "--no-model"]))
        self.assertTrue(cycle.model_requested(["cycle", "60", "--model"]))
        self.assertFalse(cycle.model_requested(["cycle", "60", "--model", "--no-model"]))

    def test_play_passes_no_model_flag_by_default(self):
        self.assertEqual([], [arg for arg in play.wake_command("60", 0) if "model" in arg])
        self.assertEqual([], [arg for arg in play.wake_command("60", 0, False) if "model" in arg])


class StackTests(unittest.TestCase):
    def test_all_but_the_newest_building_on_one_lot_are_stacked(self):
        def item(i, x, angle=10.0):
            return {"id": i, "position": {"x": x, "z": 5.0}, "angleDegrees": angle}
        growables = [item(3, 1.0), item(9, 1.0), item(7, 1.0), item(4, 50.0), item(5, 1.0, angle=100.0)]
        self.assertEqual([3, 7], cycle.stacked_buildings(growables))

    def test_a_failed_regression_after_unstacking_stops_the_loop(self):
        report = StopRuleTests.report(StopRuleTests())
        report["stacks"] = {"found": 3, "cleared": 3, "regressionOk": False}
        self.assertIn("unstackFailed", [next(iter(r)) for r in play.stop_reasons(report, {"a"}, 4000)
                                        if isinstance(r, dict)])


class RuinKindTests(unittest.TestCase):
    def test_ruins_are_counted_by_kind_and_by_the_problems_left_on_them(self):
        growables = [{"id": 1, "service": "Industrial", "subService": "IndustrialGeneric"},
                     {"id": 2, "service": "Industrial", "subService": "IndustrialGeneric"},
                     {"id": 3, "service": "Residential", "subService": "ResidentialLow"},
                     {"id": 4, "service": "Commercial", "subService": "CommercialLow"}]
        problems = {"problems": [{"id": 1, "problemNames": ["Abandoned", "NoResources", "MajorProblem"]},
                                 {"id": 2, "problemNames": ["Abandoned", "NoResources", "NoWorkers"]},
                                 {"id": 3, "problemNames": ["Abandoned", "Pollution"]},
                                 {"id": 4, "problemNames": ["NoCustomers"]}]}  # not being cleared
        with mock.patch.object(cycle.mt, "_request", return_value=problems):
            seen = cycle.ruin_kinds(growables, [1, 2, 3])
        self.assertEqual({"Industrial/IndustrialGeneric": 2, "Residential/ResidentialLow": 1}, seen["kinds"])
        self.assertEqual({"NoResources": 2, "NoWorkers": 1, "Pollution": 1}, seen["causes"])


class BurnWaveTests(unittest.TestCase):
    def test_the_densest_knot_of_burned_ruins_is_found(self):
        knot = cycle.burn_knot([(0, 0), (100, 0), (0, 100), (2000, 0)])
        self.assertEqual(3, knot["count"])
        self.assertEqual({"x": 33.3, "z": 33.3}, knot["center"])
        self.assertIsNone(cycle.burn_knot([]))

    def test_a_burn_wave_places_fire_capacity_at_its_ruins_and_a_small_fire_does_not(self):
        assessment = {"problems": []}
        option = {"name": "Fire Station", "affordable": True}
        for count, placed in ((12, True), (4, False)):
            city = mock.Mock()
            city.load_state.return_value = {"clusters": {}, "counts": {}, "noSite": {}, "wake": 0}
            with mock.patch.object(services.mt, "call_tool", return_value=assessment), \
                    mock.patch.object(services, "judge_placements", return_value=[]), \
                    mock.patch.object(services, "choose_prefabs", return_value=[option]), \
                    mock.patch.object(services, "best_park", return_value=None), \
                    mock.patch.object(services, "place_near", return_value={"ok": True, "prefab": "Fire Station"}) as near:
                solved = services.solve(city, "a", burned={"count": count, "center": {"x": 5.0, "z": 6.0}})
            self.assertEqual(placed, near.called)
            if placed:
                self.assertEqual("fix-fire-wave-a", near.call_args_list[0].args[0])  # before any other need
                self.assertEqual({"x": 5.0, "z": 6.0}, near.call_args.args[2])
                self.assertIn("BurnWave", [action.get("problem") for action in solved["actions"]])


class IndustryDistrictTests(unittest.TestCase):
    @staticmethod
    def block(x, z):
        return {"zone": "Industrial", "zonedCells": 16, "center": {"x": x, "z": z}}

    @staticmethod
    def cell(x, z, **resources):
        return {"position": {"x": x, "z": z}, "resources": {"forest": 0, "fertility": 0, "ore": 0, **resources}}

    def test_the_circle_with_most_blocks_on_one_renewable_resource_wins_and_ore_never(self):
        forest = [self.block(x, 0) for x in range(0, 200, 20)]  # ten blocks on forest
        farm = [self.block(1000 + x, 0) for x in range(0, 160, 20)]  # eight on fertile soil
        ore = [self.block(3000 + x, 0) for x in range(0, 300, 20)]  # fifteen on ore
        cells = [self.cell(100, 0, forest=200), self.cell(1080, 0, fertility=150), self.cell(3150, 0, ore=255)]
        sites = industry.candidates(forest + farm + ore, cells)
        self.assertEqual([("Forestry", 10), ("Farming", 8)], [(s["specialization"], s["blocks"]) for s in sites])

    def test_too_few_resource_blocks_make_no_site(self):
        blocks = [self.block(x, 0) for x in range(0, 100, 20)]  # five
        self.assertEqual([], industry.candidates(blocks, [self.cell(50, 0, forest=255)]))

    def _city(self, state):
        city = mock.Mock()
        city.load_state.return_value = state
        return city

    def test_a_district_is_painted_and_specialised_on_the_best_free_site(self):
        city = self._city({})
        sites = [{"center": {"x": 1.0, "z": 2.0}, "resource": "forest", "specialization": "Forestry", "blocks": 30}]
        runs = [{"ok": True, "regressionOk": True, "result": {"districtId": 7}},
                {"ok": True, "regressionOk": True, "result": {}}]
        with mock.patch.object(industry, "_industrial_blocks", return_value=[self.block(0, 0)]), \
                mock.patch.object(industry, "_cells", return_value=[]), \
                mock.patch.object(industry, "candidates", return_value=sites), \
                mock.patch.object(industry, "_free", return_value=True), \
                mock.patch.object(industry.protocol, "run", side_effect=runs) as run:
            done = industry.supply(city, "a", 10, 20)
        self.assertTrue(done["ok"])
        self.assertEqual(("create_district", {"center": {"x": 1.0, "z": 2.0}, "radius": industry.RADIUS}),
                         run.call_args_list[0].args[1:])
        self.assertEqual({"districtId": 7, "specialization": "Forestry", "requireLocalResource": True,
                          "minResourceIntensity": industry.MIN_INTENSITY}, run.call_args_list[1].args[2])
        saved = city.save_state.call_args.args[1]["districts"]
        self.assertEqual([(10, 7, 20)], [(d["wake"], d["districtId"], d["noResources"]) for d in saved])

    def test_the_last_district_settles_before_another_and_a_full_set_goes_to_the_planner(self):
        recent = self._city({"districts": [{"wake": 8, "districtId": 3}]})
        with mock.patch.object(industry.protocol, "run") as run:
            waiting = industry.supply(recent, "a", 10, 20)
        self.assertEqual((None, "settling"), (waiting["ok"], waiting["stage"]))
        run.assert_not_called()
        full = self._city({"districts": [{"wake": 1, "districtId": i} for i in range(industry.MAX_DISTRICTS)]})
        self.assertIsNone(industry.supply(full, "a", 30, 20))

    def test_goods_short_on_four_wakes_without_rising_get_a_district_and_no_escalation(self):
        city = mock.Mock()
        city.load_state.return_value = {"clusters": {}, "counts": {"NoResources": [42, 63, 63]}, "noSite": {},
                                        "wake": 0}
        assessment = {"problems": [{"problem": "NoResources", "count": 61,
                                    "remedy": {"kind": "economy", "lever": "industrySupply"}}]}
        with mock.patch.object(services.mt, "call_tool", return_value=assessment), \
                mock.patch.object(services, "judge_placements", return_value=[]), \
                mock.patch.object(services, "best_park", return_value=None), \
                mock.patch.object(services.industry, "supply", return_value={"ok": True, "districtId": 4}) as supply:
            solved = services.solve(city, "a")
        supply.assert_called_once()
        self.assertEqual([], solved["escalations"])
        self.assertIn({"problem": "NoResources", "specialised": {"ok": True, "districtId": 4}, "ok": True,
                       "stage": None, "regressionOk": None}, solved["actions"])

    def test_no_free_site_goes_to_the_planner(self):
        sites = [{"center": {"x": 1.0, "z": 2.0}, "resource": "forest", "specialization": "Forestry", "blocks": 30}]
        with mock.patch.object(industry, "_industrial_blocks", return_value=[self.block(0, 0)]), \
                mock.patch.object(industry, "_cells", return_value=[]), \
                mock.patch.object(industry, "candidates", return_value=sites), \
                mock.patch.object(industry, "_free", return_value=False), \
                mock.patch.object(industry.protocol, "run") as run:
            self.assertIsNone(industry.supply(self._city({}), "a", 10, 20))
        run.assert_not_called()


class MetroTests(unittest.TestCase):
    HOTSPOT = {"diagnosis": {"localAnchor": {"x": 0.0, "z": 0.0}, "topDestinations": [
        {"center": {"x": 300.0, "z": 0.0}}, {"center": {"x": 1500.0, "z": 0.0}}, {"center": {"x": 5000.0, "z": 0.0}}]}}

    def test_the_farthest_destination_within_reach_pairs_with_the_anchor_unless_both_are_served(self):
        self.assertEqual(((0.0, 0.0), (1500.0, 0.0)), metro.pair(self.HOTSPOT, []))
        self.assertIsNone(metro.pair(self.HOTSPOT, [(100.0, 0.0), (1400.0, 50.0)]))
        self.assertEqual(((0.0, 0.0), (1500.0, 0.0)), metro.pair(self.HOTSPOT, [(100.0, 0.0)]))
        self.assertIsNone(metro.pair({"diagnosis": {"localAnchor": {"x": 0, "z": 0}, "topDestinations": [
            {"center": {"x": 300.0, "z": 0.0}}]}}, []))  # too near to be worth a line

    def test_pieces_join_end_to_end_and_never_run_straight_on(self):
        route = metro.pieces((0.0, 0.0), (400.0, 0.0))
        self.assertEqual(5, len(route))
        self.assertEqual((0.0, 0.0), route[0][0])
        self.assertEqual((400.0, 0.0), route[-1][1])
        for (_, end), (start, _) in zip(route, route[1:]):
            self.assertEqual(end, start)
        inner = [end for _, end in route[:-1]]
        self.assertEqual([metro.ZIGZAG, -metro.ZIGZAG, metro.ZIGZAG, -metro.ZIGZAG], [z for _, z in inner])

    def test_a_hotspot_of_local_cars_gets_a_metro_after_the_cheaper_kinds_once_unlocked(self):
        remedies = {"transit": {"kind": "transit"}, "widen": {"kind": "widen"}}
        self.assertNotIn("metro", traffic.kinds_for({}, dict(remedies), {}))
        kinds = traffic.kinds_for({}, dict(remedies), {}, metro_ready=True)
        self.assertEqual("metro", kinds[-1])
        self.assertNotIn("metro", traffic.kinds_for({}, {"widen": {"kind": "widen"}}, {}, metro_ready=True))

    def build(self, runs, placements, track_ok=True, tool=None):
        place = mock.patch.object(metro.services, "place_near", side_effect=placements)
        with place, mock.patch.object(metro, "stations", return_value=[]), \
                mock.patch.object(metro, "_metro_segments", side_effect=[set(), {1}, {1, 2}] + [{1, 2}] * 40), \
                mock.patch.object(metro, "_platform_node",
                                  side_effect=[(10, (0.0, 0.0), (40.0, 0.0)), (11, (210.0, 0.0), (170.0, 0.0))]), \
                mock.patch.object(metro, "_preview", side_effect=lambda s, e, a, b: b != metro.DEPTH), \
                mock.patch.object(metro.protocol, "checkpoint", return_value="cp"), \
                mock.patch.object(metro.protocol, "verify", return_value={"ok": True}), \
                mock.patch.object(metro.mt, "call_tool", side_effect=tool or (lambda *a: {"ok": track_ok, "error": "refused"})), \
                mock.patch.object(metro.protocol, "run", side_effect=runs) as run:
            done = metro.build_line("a", self.HOTSPOT, 0, 1e9)
        return done, [call.args[1] for call in run.call_args_list]

    STATION = {"ok": True, "buildingId": 7, "position": {"x": 0.0, "z": 0.0}}

    def test_a_line_is_opened_between_the_two_stations(self):
        line = {"ok": True, "regressionOk": True, "result": {"lineId": 216}}
        done, tools = self.build([line], [self.STATION, {**self.STATION, "buildingId": 8}])
        self.assertTrue(done["ok"])
        self.assertEqual(216, done["lineId"])
        self.assertEqual(["create_transport_line"], tools)

    def test_a_piece_that_collides_goes_deeper_and_the_next_starts_there(self):
        calls = []
        line = {"ok": True, "regressionOk": True, "result": {"lineId": 216}}
        done, _ = self.build([line], [self.STATION, {**self.STATION, "buildingId": 8}],
                             tool=lambda name, body: calls.append(body) or {"ok": True})
        heights = [(body["startHeightOffset"], body["endHeightOffset"]) for body in calls]
        # out of each platform straight on, two pieces between; each collides at DEPTH and goes deeper
        self.assertEqual([(0.0, -20.0), (-20.0, -20.0), (-20.0, -20.0), (-20.0, 0.0)], heights)
        self.assertEqual((40.0, 0.0), (calls[0]["end"]["x"], calls[0]["end"]["z"]))
        self.assertEqual(-20.0, done["deepest"])

    def test_a_refused_piece_removes_the_track_and_both_stations(self):
        undo = {"ok": True}
        done, tools = self.build([undo] * 10, [self.STATION, {**self.STATION, "buildingId": 8}], track_ok=False)
        self.assertEqual((False, "track", True), (done["ok"], done["stage"], done["undone"]))
        self.assertEqual(["bulldoze_entity"] * 3, tools)  # the piece laid, then the two stations

    def test_nothing_is_built_at_the_cap_or_short_of_cash(self):
        with mock.patch.object(metro, "stations", return_value=[]):
            self.assertIsNone(metro.build_line("a", self.HOTSPOT, metro.MAX_LINES, 1e9))
            self.assertIsNone(metro.build_line("a", self.HOTSPOT, 0, 1000.0))


class CapacityIdleTests(unittest.TestCase):
    def test_a_family_under_sixty_percent_used_is_no_shortage_and_a_backlog_is(self):
        health = {"utilities": {"garbage": {"utilization": 0.36}},
                  "services": {"deathcare": {"utilization": 13.6, "storage": {"utilization": 0.25}}}}
        self.assertEqual(0.36, services.idle_load("garbage", health))
        # deathcare is read by its cemeteries' storage, not bodies held against crematoria burning
        self.assertEqual(0.25, services.idle_load("deathcare", health))
        health["services"]["deathcare"]["storage"]["utilization"] = 0.9
        self.assertIsNone(services.idle_load("deathcare", health))
        self.assertIsNone(services.idle_load("fire", health))  # no reading: nothing is held back

    def test_a_persisting_garbage_cluster_builds_nothing_while_the_plants_sit_idle(self):
        city = mock.Mock()
        city.load_state.return_value = {"clusters": {"Garbage:0:0": 5}, "counts": {}, "noSite": {}, "wake": 0}
        cluster = {"count": 6, "center": {"x": 0.0, "z": 0.0}, "nearestWorkingFacility": {"distance": 300}}
        assessment = {"problems": [{"problem": "Garbage", "count": 6, "clusters": [cluster],
                                    "remedy": {"kind": "service", "family": "garbage"}}]}
        with mock.patch.object(services.mt, "call_tool", return_value=assessment), \
                mock.patch.object(services.mt, "city_health",
                                  return_value={"utilities": {"garbage": {"utilization": 0.36}}}), \
                mock.patch.object(services, "_key", return_value="Garbage:0:0"), \
                mock.patch.object(services, "judge_placements", return_value=[]), \
                mock.patch.object(services, "best_park", return_value=None), \
                mock.patch.object(services, "place_near") as near:
            solved = services.solve(city, "a")
        near.assert_not_called()
        self.assertIn("capacityIdle", [action.get("stage") for action in solved["actions"]])


class CashTrendTests(unittest.TestCase):
    def test_cash_falling_over_three_wakes_counts_as_a_deficit(self):
        with tempfile.TemporaryDirectory() as folder:
            city = city_module.City({"instanceId": "test"}, Path(folder))
            self.assertEqual([False, False, False, True],
                             [cycle.cash_falling(city, cash) for cash in (500, 480, 460, 440)])
            self.assertFalse(cycle.cash_falling(city, 470))  # 470 is not 5% under 480, three wakes back
        self.assertTrue(utilities.in_deficit({"cash": {"periodNet": 300}}, falling=True))
        self.assertFalse(utilities.in_deficit({"cash": {"periodNet": 300}}))
        self.assertTrue(utilities.in_deficit({"cash": {"periodNet": -1}}))


class RaisedBudgetTests(unittest.TestCase):
    def health(self, garbage, deathcare=0.2, net=-5):
        return {"cash": {"periodNet": net}, "utilities": {"garbage": {"utilization": garbage}},
                "services": {"healthcare": {"utilization": 0.3},
                             "deathcare": {"utilization": 13.6, "storage": {"utilization": deathcare}},
                             "police": {"utilization": 0.5}}}

    def lowered(self, health, budgets):
        with mock.patch.object(utilities.mt, "city_health", return_value=health), \
                mock.patch.object(utilities, "budget_of", side_effect=lambda service: budgets.get(service, 100)), \
                mock.patch.object(utilities, "set_budget", side_effect=lambda label, service, value, *a: (service, value)):
            return utilities.lower_raised_budgets("a")

    def test_an_idle_raised_budget_steps_back_toward_full_in_a_deficit(self):
        self.assertEqual([("Garbage", 100)], self.lowered(self.health(0.36), {"Garbage": 150}))
        self.assertEqual([("Garbage", 100)], self.lowered(self.health(0.36), {"Garbage": 105}))

    def test_a_busy_family_a_surplus_or_a_full_budget_keep_it(self):
        self.assertEqual([], self.lowered(self.health(0.8), {"Garbage": 150}))
        self.assertEqual([], self.lowered(self.health(0.36, net=5), {"Garbage": 150}))
        self.assertEqual([], self.lowered(self.health(0.36), {"Garbage": 100}))
        # healthcare also pays the hearses: cemeteries filling up keep its raise, a quarter full do not
        self.assertEqual([], self.lowered(self.health(0.9, deathcare=0.8), {"HealthCare": 130}))
        self.assertEqual([("HealthCare", 100)], self.lowered(self.health(0.9, deathcare=0.25), {"HealthCare": 140}))


class SurplusCommerceTests(unittest.TestCase):
    BLOCKS = [{"blockId": i, "zone": "CommercialHigh", "zonedCells": 16, "occupancyClear": True,
               "center": {"x": 100.0 * i, "z": 0.0}} for i in range(1, 12)]

    def rezone(self, categories, offices=True, allowed=("residential", "office")):
        runs = []
        with mock.patch.object(growth.mt, "_request", return_value={"blocks": self.BLOCKS}),                 mock.patch.object(growth, "_town", return_value=(0.0, 0.0)),                 mock.patch.object(growth.mt, "_land_use_context",
                                  return_value={"growables": [], "polluters": [], "corners": [], "through": []}),                 mock.patch.object(growth.land_use, "classify", return_value={"allowed": list(allowed)}),                 mock.patch.object(growth.protocol, "run",
                                  side_effect=lambda label, tool, args: runs.append(args) or {"ok": True, "regressionOk": True}):
            done = growth.rezone_surplus_commerce("a", categories, high_density=True, offices=offices)
        return done, runs

    def categories(self, shops=0, empty=615, work=59, homes=29):
        return {"commercial": {"demand": shops, "developableVacantCells": empty},
                "workplace": {"demand": work}, "residential": {"demand": homes}}

    def test_empty_shops_no_one_wants_become_offices_while_jobs_are_wanted_more(self):
        done, runs = self.rezone(self.categories())
        self.assertEqual(growth.SURPLUS_PER_WAKE, len(runs))
        self.assertEqual({"Office"}, {item["zone"] for item in done})
        self.assertEqual(1, runs[0]["blockId"])  # nearest the town first
        done, _ = self.rezone(self.categories(work=10, homes=40))
        self.assertEqual({"ResidentialHigh"}, {item["zone"] for item in done})
        done, _ = self.rezone(self.categories(), offices=False)
        self.assertEqual({"ResidentialHigh"}, {item["zone"] for item in done})

    def test_land_fit_only_for_shops_takes_offices_when_shops_are_in_surplus(self):
        done, runs = self.rezone(self.categories(work=10, homes=40), allowed=("commercial",))
        self.assertEqual({"Office"}, {item["zone"] for item in done})
        self.assertEqual([], self.rezone(self.categories(), offices=False, allowed=("commercial",))[1])

    def test_nothing_is_rezoned_while_shops_are_wanted_or_little_stands_empty(self):
        self.assertEqual([], self.rezone(self.categories(shops=5))[1])
        self.assertEqual([], self.rezone(self.categories(empty=100))[1])


class IdleShopTests(unittest.TestCase):
    def test_empty_commercial_land_by_shops_short_of_customers_becomes_homes(self):
        shops = {"growables": [{"id": 5, "service": "Commercial", "position": {"x": 0, "z": 0}}]}
        blocks = {"blocks": [
            {"blockId": 1, "zone": "CommercialLow", "zonedCells": 16, "occupancyClear": True,
             "roadPrefab": "Basic Road", "center": {"x": 100, "z": 0}},
            {"blockId": 2, "zone": "CommercialLow", "zonedCells": 16, "occupancyClear": True,
             "roadPrefab": "Basic Road", "center": {"x": 150, "z": 0}},
            {"blockId": 3, "zone": "CommercialHigh", "zonedCells": 16, "occupancyClear": False,
             "roadPrefab": "Basic Road", "center": {"x": 50, "z": 0}},  # built on: kept
            {"blockId": 4, "zone": "ResidentialLow", "zonedCells": 16, "occupancyClear": True,
             "roadPrefab": "Basic Road", "center": {"x": 60, "z": 0}},
            {"blockId": 6, "zone": "CommercialLow", "zonedCells": 16, "occupancyClear": True,
             "roadPrefab": "Basic Road", "center": {"x": 900, "z": 0}}]}  # beyond the shops' reach
        context = {"growables": [], "polluters": [], "corners": [], "through": set()}
        allowed = {1: ["commercial"], 2: ["residential", "commercial"]}
        with mock.patch.object(growth.mt, "_request", side_effect=lambda method, path, **kw:
                               shops if path == "/state/growables" else blocks), \
                mock.patch.object(growth.mt, "_land_use_context", return_value=context), \
                mock.patch.object(growth.land_use, "classify", side_effect=lambda block, *a, **k:
                                  {"allowed": allowed.get(block["blockId"], ["residential"])}), \
                mock.patch.object(growth.protocol, "run", return_value={"ok": True, "regressionOk": True}) as run:
            done = growth.rezone_idle_shops("a", [5], high_density=True)
        # block 1 may take no homes (industry near); block 2 does
        self.assertEqual([(2, "ResidentialHigh")], [(item["blockId"], item["zone"]) for item in done])
        self.assertEqual({"zone": "ResidentialHigh", "blockId": 2}, run.call_args.args[2])

    def test_buffer_land_where_homes_may_not_go_becomes_offices_once_unlocked(self):
        shops = {"growables": [{"id": 5, "service": "Commercial", "position": {"x": 0, "z": 0}}]}
        blocks = {"blocks": [{"blockId": 1, "zone": "CommercialHigh", "zonedCells": 16, "occupancyClear": True,
                              "roadPrefab": "Small 4 Lane Road", "center": {"x": 40, "z": 0}}]}
        context = {"growables": [], "polluters": [], "corners": [], "through": set()}
        for offices, expected in ((True, [(1, "Office")]), (False, [])):
            with mock.patch.object(growth.mt, "_request", side_effect=lambda method, path, **kw:
                                   shops if path == "/state/growables" else blocks), \
                    mock.patch.object(growth.mt, "_land_use_context", return_value=context), \
                    mock.patch.object(growth.land_use, "classify", return_value={"allowed": ["commercial", "office"]}), \
                    mock.patch.object(growth.protocol, "run", return_value={"ok": True, "regressionOk": True}):
                done = growth.rezone_idle_shops("a", [5], high_density=True, offices=offices)
            self.assertEqual(expected, [(item["blockId"], item["zone"]) for item in done])

    def test_no_shop_found_rezones_nothing(self):
        with mock.patch.object(growth.mt, "_request", return_value={"growables": []}), \
                mock.patch.object(growth.protocol, "run") as run:
            self.assertEqual([], growth.rezone_idle_shops("a", [5]))
        run.assert_not_called()


class StopRuleTests(unittest.TestCase):
    def report(self, **changes):
        report = {
            "wake": {"unlocked": ["a"], "cash": 10000, "modelMismatches": [], "elecUtil": 0.5, "waterUtil": 0.5},
            "growth": {"zoned": [{"ok": True, "regressionOk": True}], "densified": [], "expansion": None},
            "ruins": {"cleared": 0},
            "water": {"summary": {"missingWater": 0, "missingSewage": 0}},
            "power": {"summary": {"missingElectricity": 0}, "unpowered": []},
            "services": {"escalations": [], "actions": [{"ok": False, "stage": "atMaximum"}]},
            "traffic": {},
            "network": {"isolatedRoads": []},
        }
        for key, value in changes.items():
            report[key] = {**report[key], **value} if isinstance(value, dict) else value
        return report

    def test_a_quiet_wake_does_not_stop(self):
        self.assertEqual([], play.stop_reasons(self.report(), {"a"}, 4000))

    def test_planning_needs_stop_the_loop(self):
        reasons = play.stop_reasons(self.report(
            wake={"unlocked": ["a", "b"], "cash": 10000, "modelMismatches": [], "waterUtil": 0.9},
            growth={"zoned": [], "densified": [], "expansion": {"stage": "needsCrossing", "ok": False}},
            traffic={"escalation": {"key": "h"}}), {"a"}, 4000)
        kinds = [next(iter(reason)) if isinstance(reason, dict) else reason for reason in reasons]
        self.assertEqual(["unlocked", "landNeedsCrossing", "utilityPressure", "trafficNeedsPlanning"], kinds)

    def test_a_road_cut_off_from_the_outside_stops_the_loop(self):
        reasons = play.stop_reasons(self.report(network={"isolatedRoads": [5433]}), {"a"}, 4000)
        self.assertEqual([{"roadsNotConnected": [5433]}], reasons)
        self.assertEqual([], play.stop_reasons(self.report(network={"isolatedRoads": []}), {"a"}, 4000))

    def test_buildings_left_unpiped_stop_the_loop_only_when_the_pipes_stopped_holding(self):
        held = [{"step": step, "ok": True, "regressionOk": True} for step in range(utilities.MAX_PIPES)]
        missing = {"missingWater": 9, "missingSewage": 9}
        self.assertEqual([], play.stop_reasons(self.report(water={"summary": missing, "steps": held}), {"a"}, 4000))
        failed = held[:2] + [{"step": 2, "planSafe": False}]
        reasons = play.stop_reasons(self.report(water={"summary": missing, "steps": failed}), {"a"}, 4000)
        self.assertEqual(["waterUnrepaired"], [next(iter(reason)) for reason in reasons])

    def test_homes_grown_dry_after_every_dry_building_was_piped_do_not_stop_the_loop(self):
        held = [{"step": step, "ok": True, "regressionOk": True} for step in range(4)]
        missing = {"missingWater": 8, "missingSewage": 8}
        water = {"summary": missing, "steps": held, "caughtUp": True}
        self.assertEqual([], play.stop_reasons(self.report(water=water), {"a"}, 4000))
        for broken in ({**water, "caughtUp": False}, {**water, "steps": held[:3] + [{"step": 3, "ok": False}]}):
            reasons = play.stop_reasons(self.report(water=broken), {"a"}, 4000)
            self.assertEqual(["waterUnrepaired"], [next(iter(reason)) for reason in reasons])

    def test_homes_dry_the_wake_the_water_system_was_built_do_not_stop_the_loop(self):
        short = {"summary": {"missingWater": 2, "missingSewage": 2}, "steps": [],
                 "diagnosis": ["supplyShort"], "supply": {"ok": False, "stage": "waterSupplyShort"}}
        built = self.report(water=short)
        built["waterSystem"] = {"ok": True, "regressionOk": True}
        self.assertEqual([], play.stop_reasons(built, {"a"}, 4000))
        reasons = play.stop_reasons(self.report(water=short), {"a"}, 4000)
        self.assertEqual(["waterUnrepaired"], [next(iter(reason)) for reason in reasons])

    def test_water_pressure_met_by_a_restored_budget_does_not_stop_the_loop(self):
        pressed = {"unlocked": [], "cash": 10000, "modelMismatches": [], "waterUtil": 0.87, "sewageUtil": 0.87}
        for restored, stops in (([{"ok": True}], 0), ([{"ok": None, "stage": "cooldown"}], 2), ([], 2)):
            report = self.report(wake=pressed)
            report["waterBudget"] = restored
            self.assertEqual(stops, len(play.stop_reasons(report, {"a"}, 4000)))

    def test_homes_dry_the_wake_their_budget_went_back_to_full_do_not_stop_the_loop(self):
        missing = {"missingWater": 29, "missingSewage": 0}
        raised = {"summary": missing, "steps": [], "diagnosis": ["supplyShort"],
                  "supply": {"budget": "Water", "ok": True, "regressionOk": True, "value": 100}}
        self.assertEqual([], play.stop_reasons(self.report(water=raised), {"a"}, 4000))
        report = self.report(water={"summary": missing, "steps": [], "diagnosis": ["supplyShort"],
                                    "supply": {"ok": False, "stage": "waterSupplyShort"}})
        report["waterBudget"] = [{"budget": "Water", "ok": True, "value": 100}]
        self.assertEqual([], play.stop_reasons(report, {"a"}, 4000))
        report["waterBudget"] = [{"budget": "Water", "ok": None, "stage": "cooldown"}]
        self.assertEqual(["waterUnrepaired"], [next(iter(reason)) for reason in play.stop_reasons(report, {"a"}, 4000)])

    def test_a_trimmed_water_budget_goes_back_to_full_past_the_cooldown_when_use_nears_capacity(self):
        health = {"cityHealth": {"utilities": {"water": {"utilization": 0.866}, "sewage": {"utilization": 0.866}}}}
        for budget, expected in ((50, [100]), (30, [100]), (100, [])):
            with mock.patch.object(utilities.mt, "city_health", return_value=health["cityHealth"]), \
                    mock.patch.object(utilities, "budget_of", return_value=budget), \
                    mock.patch.object(utilities, "set_budget",
                                      side_effect=lambda *a: {"ok": True, "value": a[2], "reason": a[3]}):
                actions = utilities.restore_budgets("t")
            self.assertEqual(expected, [action["value"] for action in actions])
            # the reason that marks an undone trim, which the fiscal cooldown does not hold
            self.assertTrue(all(action["reason"] == "post-change-correction" for action in actions))

    def test_a_budget_put_back_to_full_passes_the_fiscal_cooldown(self):
        arguments = {"service": "Water", "budget": 100, "dayAndNight": True, "reason": "post-change-correction",
                     "expectedOutcome": "water capacity back to full", "checkpointName": "t"}
        with mock.patch.object(utilities.mt, "_require_normal_progression"), \
                mock.patch.object(utilities.mt, "_require_fiscal_cooldown_clear",
                                  side_effect=AssertionError("cooldown checked")), \
                mock.patch.object(utilities.mt, "_load_city_scoped_memory", return_value=({}, None)), \
                mock.patch.object(utilities.mt, "_require_checkpoint", side_effect=lambda a: dict(a)), \
                mock.patch.object(utilities.mt, "_request", return_value={"ok": True}), \
                mock.patch.object(utilities.mt, "_record_fiscal_adjustment") as recorded:
            result = utilities.mt.call_tool("set_service_budget", arguments)
        self.assertTrue(result["ok"])
        recorded.assert_not_called()  # an undone trim starts no new cooldown

    def test_buildings_short_of_supply_wait_a_wake_for_the_plant_this_wake_built(self):
        dark = {"summary": {"missingElectricity": 12}, "unpowered": [7], "unreached": []}
        report = self.report(power=dark)
        report["powerPlant"] = {"ok": True, "regressionOk": True}
        self.assertEqual([], play.stop_reasons(report, {"a"}, 4000))
        report["power"] = {**dark, "unreached": [3642]}
        self.assertEqual([{"unpowered": [7]}], play.stop_reasons(report, {"a"}, 4000))
        self.assertEqual([{"unpowered": [7]}], play.stop_reasons(self.report(power=dark), {"a"}, 4000))
        # a plant built among buildings no line reaches lets them wait a wake too
        report["power"] = {**dark, "unreached": [3642], "island": {"ok": True}}
        self.assertEqual([], play.stop_reasons(report, {"a"}, 4000))

    def test_a_gate_skip_is_not_a_failure(self):
        report = self.report(growth={"zoned": [{"ok": False, "gateSkipped": True, "regressionOk": True}],
                                     "densified": [], "expansion": None})
        self.assertEqual([], play.stop_reasons(report, {"a"}, 4000))


class GroundwaterSystemTests(unittest.TestCase):
    def test_a_pump_beside_the_town_still_gets_a_junction_its_pipes_part_at(self):
        pump, outlet, town = (1694.0, -976.0), (2277.0, -2277.0), (1838.0, -1120.0)
        found = utilities.junctions(pump, outlet, town)
        self.assertTrue(found)
        j = found[0]
        ways = [utilities._bearing(j, p) for p in (pump, outlet, town)]
        spread = min(abs((p - q + 180) % 360 - 180) for i, p in enumerate(ways) for q in ways[i + 1:])
        self.assertGreaterEqual(spread, utilities.JUNCTION_SPREAD)

    def test_no_safe_river_pair_falls_back_to_a_pump_and_the_nearest_outlet(self):
        pump = {"buildingPrefab": "Water Tower", "position": {"x": 1694.0, "z": -976.0}, "angleDegrees": 0}
        near = {"buildingPrefab": "Water Outlet", "position": {"x": 2277.0, "z": -2277.0}, "angleDegrees": 45}
        far = {"buildingPrefab": "Water Outlet", "position": {"x": 2150.0, "z": -2525.0}, "angleDegrees": 45}
        search = {"safe": False, "risks": ["noSafeWaterPairFound"], "attempts": [{"outlet": far}, {"outlet": near}]}

        def call(tool, arguments):
            if tool == "propose_groundwater_pump_sites":
                return {"candidates": [pump]}
            if tool == "inspect_water_pair":
                return {"safe": True}
            raise AssertionError(tool)
        with mock.patch.object(utilities.mt, "call_tool", side_effect=call) as calls:
            pair = utilities.groundwater_pair((1838.0, -1120.0), search, {})
        self.assertTrue(pair["safe"])
        self.assertEqual(near, pair["outlet"])
        self.assertEqual("Water Tower", pair["intake"]["buildingPrefab"])
        sites = next(c.args[1] for c in calls.call_args_list if c.args[0] == "propose_groundwater_pump_sites")
        self.assertEqual(utilities.GROUNDWATER_OFFSET, sites["minDistance"])

    def test_a_groundwater_pump_is_the_intake_and_never_dry(self):
        facilities = {"facilities": [{"id": 9, "prefab": "Water Tower", "position": {"x": 0, "z": 0},
                                      "flags": "Created, Completed", "waterSourceId": 0, "problems": ""}]}
        prefabs = {"buildings": [{"name": "Water Tower",
                                  "aiProfile": {"fields": {"waterIntake": 3750, "useGroundWater": True}}}]}
        with mock.patch.object(utilities.mt, "_request",
                               side_effect=lambda m, path, **k: facilities if path == "/state/facilities" else prefabs):
            standing = utilities._water_facilities()
            self.assertEqual(["intake"], list(standing))
            self.assertTrue(standing["intake"]["groundwater"])
            self.assertEqual([], utilities.dry_water_facilities())


class WaterCapacityTests(unittest.TestCase):
    TOWER = {"buildingPrefab": "Water Tower", "buildingId": 43203, "position": {"x": 0.0, "z": 0.0},
             "angleDegrees": 0, "waterSource": 0, "active": True, "groundwater": True, "problems": ""}
    OUTLET = {"buildingPrefab": "Water Outlet", "buildingId": 3395, "position": {"x": 900.0, "z": 0.0},
              "angleDegrees": 45, "waterSource": 1, "active": True, "groundwater": False, "problems": ""}

    def run_capacity(self, use=0.886, budget=100, intake=None, sites=(), connected=True, city=None, sewage=0.4,
                     outlets=()):
        intake = intake or self.TOWER
        health = {"cityHealth": {"utilities": {"water": {"utilization": use}, "sewage": {"utilization": sewage}}}}
        placed = []

        def call(tool, arguments):
            if tool == "propose_groundwater_pump_sites":
                found = sites(arguments["serviceAreaAnchor"]) if callable(sites) else sites
                return {"candidates": [{"buildingPrefab": "Water Tower", "position": point} for point in found]}
            if tool == "inspect_water_pair":  # the pipe port stands at the site, toward the trunk
                role = "outlet" if "buildingId" in arguments["intake"] else "intake"
                spot = arguments[role]["position"]
                return {"safe": True, "previews": {role: {"networkPaths": [
                    {"service": "Water", "nodes": [{"x": spot["x"], "z": spot["z"] - 10 * (spot["z"] > 0)}]}]}}}
            if tool == "propose_water_pair_candidates":
                # near the outlet the search lists its outlet sites; on the whole river, a safe pair
                # at one resolution and attempts at the other
                found = [{"buildingPrefab": "Water Outlet", "position": point, "angleDegrees": 90} for point in outlets]
                if "serviceAreaAnchor" in arguments:
                    return {"safe": False, "outletCandidates": found[-1:]}
                if arguments["resolution"] == utilities.WATER_SEARCH_RESOLUTIONS[0]:
                    return {"safe": bool(found), "outlet": found[0] if found else None}
                return {"safe": False, "attempts": [{"outlet": item} for item in found[1:]]}
            raise AssertionError(tool)

        def request(method, path, query=None, body=None):
            if path == "/state/city-health":
                return health["cityHealth"]
            if path == "/state/facilities":
                return {"facilities": [{"id": 43203, "prefab": "Water Tower", "position": {"x": 0, "z": 0}}]}
            if path == "/state/networks":  # the trunk runs east from the tower, a node every 200m
                return {"segments": [{"service": "Water", "start": {"x": 0, "z": 0}, "end": {"x": 200, "z": 0}},
                                     {"service": "Water", "start": {"x": 200, "z": 0}, "end": {"x": 400, "z": 0}}]}
            raise AssertionError(path)

        def run(label, tool, arguments, settle=0.0):
            placed.append((tool, arguments))
            if tool == "connect_water_facility":
                return {"ok": connected, "regressionOk": True, "result": {"connected": connected}}
            return {"ok": True, "regressionOk": True, "result": {}}

        with tempfile.TemporaryDirectory() as folder:
            city = city or city_module.City({"instanceId": "test"}, Path(folder))
            with mock.patch.object(utilities.mt, "call_tool", side_effect=call), \
                    mock.patch.object(utilities.mt, "_request", side_effect=request), \
                    mock.patch.object(utilities, "budget_of", return_value=budget), \
                    mock.patch.object(utilities, "_water_facilities",
                                      return_value={"intake": intake, "outlet": self.OUTLET}), \
                    mock.patch.object(utilities, "_placed_id", return_value=50001), \
                    mock.patch.object(utilities.protocol, "run", side_effect=run):
                results = [utilities.add_water_capacity(city, "t")
                           for _ in range(1 + utilities.PUMP_SETTLE_WAKES + 1)]
        return results, placed

    def test_a_pump_near_the_pipes_is_added_and_joined_when_water_runs_near_capacity(self):
        # one site 300m north of the pipes, one beside the trunk
        results, placed = self.run_capacity(sites=[{"x": 200, "z": 300}, {"x": 200, "z": 80}])
        first = results[0]
        self.assertTrue(first["ok"])
        self.assertEqual(("intake", 50001, 70.0), (first["role"], first["facility"], first["branch"]))  # from its port
        tools = [tool for tool, _ in placed]
        self.assertEqual("place_water_facility", tools[0])
        self.assertEqual({"x": 200, "z": 80}, placed[0][1]["intake"]["position"])
        self.assertEqual(3395, placed[0][1]["outlet"]["buildingId"])  # paired with the outlet standing
        self.assertEqual({"buildingId": 50001, "pairBuildingId": 3395}, placed[1][1])
        # the next wakes wait for its capacity to show, then the pressure is judged again
        self.assertEqual(["settling"] * utilities.PUMP_SETTLE_WAKES,
                         [result["stage"] for result in results[1:1 + utilities.PUMP_SETTLE_WAKES]])
        self.assertTrue(results[-1]["ok"])

    def test_the_pump_search_moves_on_while_the_pumps_standing_take_every_site(self):
        # around the tower the only site lies 30m from it; around the next anchor one lies by the trunk
        results, placed = self.run_capacity(
            sites=lambda anchor: [{"x": 0, "z": 30}] if anchor == {"x": 0.0, "z": 0.0} else [{"x": 200, "z": 80}])
        self.assertTrue(results[0]["ok"])
        self.assertEqual({"x": 200, "z": 80}, placed[0][1]["intake"]["position"])

    def test_a_pump_the_pipes_cannot_reach_is_removed(self):
        results, placed = self.run_capacity(sites=[{"x": 200, "z": 80}], connected=False)
        self.assertEqual("noPumpSite", results[0]["stage"])
        self.assertIn(("bulldoze_entity", {"entityType": "building", "id": 50001}), placed)

    def test_nothing_is_added_below_the_pressure_with_a_trimmed_budget_or_far_from_the_pipes(self):
        self.assertIsNone(self.run_capacity(use=0.6, sites=[{"x": 200, "z": 80}])[0][0])
        self.assertIsNone(self.run_capacity(budget=60, sites=[{"x": 200, "z": 80}])[0][0])
        far, placed = self.run_capacity(sites=[{"x": 200, "z": 500}])
        self.assertEqual(("noPumpSite", 0), (far[0]["stage"], far[0]["nearPipes"]))  # 500m: beyond any branch
        self.assertEqual([], placed)

    def test_an_outlet_near_the_pipes_is_added_when_sewage_runs_near_capacity(self):
        # one shore site 300m off the pipes, one by the trunk node at (200, 0)
        results, placed = self.run_capacity(use=0.5, sewage=0.864, outlets=[{"x": 200, "z": 300}, {"x": 230, "z": 60}])
        first = results[0]
        self.assertTrue(first["ok"])
        self.assertEqual("outlet", first["role"])
        tool, arguments = placed[0]
        self.assertEqual(("place_water_facility", "outlet"), (tool, arguments["role"]))
        self.assertEqual({"x": 230, "z": 60}, arguments["outlet"]["position"])
        self.assertEqual(43203, arguments["intake"]["buildingId"])  # paired with the pump standing
        self.assertEqual({"buildingId": 50001, "pairBuildingId": 43203}, placed[1][1])

    def test_a_site_is_judged_by_its_pipe_port_and_an_unsafe_one_skipped(self):
        partner = {"buildingPrefab": "Water Tower", "buildingId": 43203, "position": {"x": 0, "z": 0}, "angleDegrees": 0}
        like = {"buildingPrefab": "Water Outlet", "buildingId": 3395, "position": {"x": 900, "z": 0}}
        # 156m from the trunk node at (200, 0), its port 138m: joined (live); one nearer is unsafe
        far_site = {"buildingPrefab": "Water Outlet", "position": {"x": 356.0, "z": 0.0}, "angleDegrees": 90}
        unsafe = {"buildingPrefab": "Water Outlet", "position": {"x": 300.0, "z": 0.0}, "angleDegrees": 90}

        def call(tool, arguments):
            if tool == "inspect_water_pair":
                spot = arguments["outlet"]["position"]
                if spot["x"] == 300.0:
                    return {"safe": False, "risks": ["outletUpstreamOfIntake"]}
                return {"safe": True, "previews": {"outlet": {"networkPaths": [
                    {"service": "Water", "nodes": [{"x": 338.0, "z": 0.0}]}]}}}
            raise AssertionError(tool)

        def request(method, path, query=None, body=None):
            if path == "/state/facilities":
                return {"facilities": [{"id": 3395, "prefab": "Water Outlet", "position": {"x": 900, "z": 0}}]}
            if path == "/state/networks":
                return {"segments": [{"service": "Water", "start": {"x": 0, "z": 0}, "end": {"x": 200, "z": 0}}]}
            raise AssertionError(path)
        with tempfile.TemporaryDirectory() as folder:
            city = city_module.City({"instanceId": "test"}, Path(folder))
            with mock.patch.object(utilities.mt, "call_tool", side_effect=call), \
                    mock.patch.object(utilities.mt, "_request", side_effect=request), \
                    mock.patch.object(utilities, "_placed_id", return_value=50002), \
                    mock.patch.object(utilities.protocol, "run",
                                      return_value={"ok": True, "regressionOk": True, "result": {"connected": True}}) as run:
                result = utilities._add_water_facility(city, "t", "outlet", like, partner, [unsafe, far_site], 0.864)
        self.assertTrue(result["ok"])
        self.assertEqual(138.0, result["branch"])
        self.assertEqual({"x": 356.0, "z": 0.0}, run.call_args_list[0].args[2]["outlet"]["position"])

    def test_water_goes_first_when_both_run_near_capacity(self):
        results, placed = self.run_capacity(use=0.9, sewage=0.9, sites=[{"x": 200, "z": 80}],
                                            outlets=[{"x": 230, "z": 60}])
        self.assertEqual("intake", results[0]["role"])
        self.assertEqual("intake", placed[0][1]["role"])

    def test_a_city_on_river_water_leaves_another_intake_to_the_planner(self):
        river = {**self.TOWER, "buildingPrefab": "Water Intake", "groundwater": False}
        results, placed = self.run_capacity(intake=river, sites=[{"x": 200, "z": 80}])
        self.assertEqual("riverIntakeForPlanner", results[0]["stage"])
        self.assertEqual([], placed)

    def test_water_pressure_met_by_a_new_pump_does_not_stop_the_loop(self):
        pressed = {"unlocked": [], "cash": 10000, "modelMismatches": [], "waterUtil": 0.886}
        report = StopRuleTests.report(StopRuleTests(), wake=pressed)
        self.assertEqual(1, len(play.stop_reasons(report, {"a"}, 4000)))
        report["waterCapacity"] = {"ok": True, "regressionOk": True, "pump": 50001}
        self.assertEqual([], play.stop_reasons(report, {"a"}, 4000))
        report["waterCapacity"] = {"ok": True, "regressionOk": False, "pump": 50001}
        self.assertEqual(["waterCapacityFailed"], [next(iter(r)) for r in play.stop_reasons(report, {"a"}, 4000)])

    def test_sewage_pressure_while_a_new_pump_settles_waits_unless_near_the_limit(self):
        for use, stops in ((0.878, False), (0.96, True)):
            pressed = {"unlocked": [], "cash": 10000, "modelMismatches": [], "sewageUtil": use}
            report = StopRuleTests.report(StopRuleTests(), wake=pressed)
            report["waterCapacity"] = {"ok": None, "stage": "settling", "facility": 29961}
            reasons = play.stop_reasons(report, {"a"}, 4000)
            self.assertEqual([{"utilityPressure": {"sewageUtil": use}}] if stops else [], reasons)
        # power is no water facility's to settle
        pressed = {"unlocked": [], "cash": 10000, "modelMismatches": [], "elecUtil": 0.9}
        report = StopRuleTests.report(StopRuleTests(), wake=pressed)
        report["waterCapacity"] = {"ok": None, "stage": "settling"}
        self.assertEqual(["utilityPressure"], [next(iter(r)) for r in play.stop_reasons(report, {"a"}, 4000)])


class PowerSizingTests(unittest.TestCase):
    WIND = {"name": "Wind Turbine", "aiType": "WindTurbineAI", "roles": {"power": 500.0}, "constructionCost": 600000,
            "maintenanceCost": 50000, "pollutionRadius": 0, "affordable": True}
    ADVANCED = {"name": "Advanced Wind Turbine", "aiType": "WindTurbineAI", "roles": {"power": 1250.0},
                "constructionCost": 1200000, "maintenanceCost": 125000, "pollutionRadius": 0, "affordable": True}
    COAL = {"name": "Coal Power Plant", "aiType": "PowerPlantAI", "roles": {"power": 2500.0}, "constructionCost": 1900000,
            "maintenanceCost": 350000, "pollutionRadius": 100, "affordable": True}

    def test_the_turbines_yield_is_measured_from_the_grid(self):
        turbines = [{"prefab": "Wind Turbine", "productionRate": 100}] * 13 + [{"prefab": "Wind Turbine", "productionRate": 0}]
        with mock.patch.object(utilities.mt, "_request", return_value={"facilities": turbines}):
            self.assertAlmostEqual(45440 / (13 * 8000), utilities.variable_yield([self.WIND], 45440), places=3)
        with mock.patch.object(utilities.mt, "_request", return_value={"facilities": []}):
            self.assertEqual(utilities.WIND_YIELD_DEFAULT, utilities.variable_yield([self.WIND], 45440))

    def place(self, sites, wind=0.437, wells=(), budget=None, unsited=None):
        searched = []

        def call(tool, arguments):
            if tool == "compare_service_options":
                return {"options": [self.WIND, self.ADVANCED, self.COAL]}
            if tool == "propose_power_plant_candidates":
                searched.append(arguments["buildingPrefab"])
                found = sites.get(arguments["buildingPrefab"], [])
                return {"safe": bool(found), "candidates": [{"position": point} for point in found]}
            raise AssertionError(tool)
        with mock.patch.object(utilities.mt, "call_tool", side_effect=call), \
                mock.patch.object(utilities.mt, "city_health",
                                  return_value={"utilities": {"electricity": {"capacity": 45440}}}), \
                mock.patch.object(utilities, "variable_yield", return_value=wind), \
                mock.patch.object(utilities.mt, "_land_use_context", return_value={"wells": list(wells)}), \
                mock.patch.object(utilities.protocol, "run", return_value={"ok": True, "regressionOk": True}) as run:
            result = utilities.place_plant("t", (0.0, 0.0), unsited, budget)
        return result, searched, run

    def test_a_steady_plant_outranks_turbines_that_give_under_half_their_rating(self):
        result, _, run = self.place({"Coal Power Plant": [{"x": 900, "z": 0}], "Wind Turbine": [{"x": 50, "z": 0}]})
        self.assertEqual(("Coal Power Plant", 40000), (result["prefab"], result["expectedKw"]))
        self.assertEqual("Coal Power Plant", run.call_args.args[2]["buildingPrefab"])

    def test_of_two_equal_turbines_the_larger_goes_first(self):
        result, _, _ = self.place({"Wind Turbine": [{"x": 50, "z": 0}], "Advanced Wind Turbine": [{"x": 60, "z": 0}]})
        self.assertEqual("Advanced Wind Turbine", result["prefab"])
        self.assertEqual(round(1250 * 16 * 0.437), result["expectedKw"])

    def test_an_option_with_no_site_is_not_searched_again_this_wake(self):
        unsited = set()
        first, searched, _ = self.place({"Advanced Wind Turbine": [{"x": 60, "z": 0}]}, unsited=unsited)
        self.assertEqual("Advanced Wind Turbine", first["prefab"])
        self.assertEqual({"Coal Power Plant"}, unsited)
        _, searched, _ = self.place({"Advanced Wind Turbine": [{"x": 60, "z": 0}]}, unsited=unsited)
        self.assertNotIn("Coal Power Plant", searched)

    def test_a_plant_that_pollutes_the_ground_keeps_off_the_groundwater_pumps(self):
        sites = {"Coal Power Plant": [{"x": 200, "z": 0}, {"x": 900, "z": 0}]}
        result, _, run = self.place(sites, wells=[(0.0, 0.0)])
        self.assertEqual({"x": 900, "z": 0}, run.call_args.args[2]["position"])
        result, _, _ = self.place({"Coal Power Plant": [{"x": 200, "z": 0}]}, wells=[(0.0, 0.0)])
        self.assertFalse(result["ok"])

    def test_a_plant_beyond_the_wakes_budget_is_not_built(self):
        result, searched, _ = self.place({"Coal Power Plant": [{"x": 900, "z": 0}],
                                          "Advanced Wind Turbine": [{"x": 60, "z": 0}]}, budget=15000)
        self.assertEqual("Advanced Wind Turbine", result["prefab"])  # 19,000 coal is over the 15,000 left
        self.assertNotIn("Coal Power Plant", searched)

    def test_a_site_search_the_busy_game_thread_did_not_answer_moves_on(self):
        busy = utilities.mt.BridgeRequestError(
            {"ok": False, "error": "Timed out waiting for the game thread; the command was not run."})
        option = {"name": "Wind Turbine", "aiType": "WindTurbineAI", "roles": {"power": 500.0},
                  "constructionCost": 600000, "maintenanceCost": 50000, "affordable": True, "pollutionRadius": 0}
        searches = []

        def tool(name, args):
            if name == "compare_service_options":
                return {"options": [option]}
            if name == "propose_power_plant_candidates":
                searches.append(args["maxPreviewAttempts"])
                if len(searches) == 1:
                    raise busy
                return {"safe": True, "candidates": [{"position": {"x": 1, "z": 1}, "angleDegrees": 0}]}
            raise AssertionError(name)
        with mock.patch.object(utilities.mt, "call_tool", side_effect=tool), \
                mock.patch.object(utilities.mt, "city_health",
                                  return_value={"utilities": {"electricity": {"capacity": 1000}}}), \
                mock.patch.object(utilities.mt, "_request", return_value={"facilities": []}), \
                mock.patch.object(utilities.protocol, "run", return_value={"ok": True, "regressionOk": True}):
            result = utilities.place_plant("t", (0.0, 0.0))
        self.assertTrue(result["ok"])
        self.assertEqual([True, False], [item.get("busy", False) for item in result["tried"]])
        self.assertEqual({utilities.PLANT_PREVIEWS}, set(searches))

    def test_plants_are_built_to_the_deficit_up_to_a_wakes_limit(self):
        observed = {"cityHealth": {"utilities": {"electricity": {"capacity": 45440, "used": 60752,
                                                                  "utilization": 1.337}}}}
        plant = {"ok": True, "regressionOk": True, "prefab": "Advanced Wind Turbine", "expectedKw": 8740, "cost": 12000.0}
        with mock.patch.object(utilities.mt, "city_health", side_effect=lambda: observed["cityHealth"]), \
                mock.patch.object(utilities, "budget_of", return_value=100), \
                mock.patch.object(utilities, "town_center", return_value=(0.0, 0.0)), \
                mock.patch.object(utilities, "cash_display", return_value=154679.0), \
                mock.patch.object(utilities, "industry_anchors", return_value=[]), \
                mock.patch.object(utilities, "place_plant", return_value=plant) as place:
            result = utilities.add_power("t", ["electricity"])
        self.assertEqual(utilities.MAX_PLANTS, place.call_count)
        self.assertEqual(utilities.MAX_PLANTS, result["plants"])
        need = 60752 / utilities.POWER_TARGET - 45440
        self.assertEqual(round(need - 3 * 8740), result["stillNeededKw"])
        # a smaller deficit takes only the plants it needs
        observed["cityHealth"]["utilities"]["electricity"].update(used=37000, utilization=0.86)
        with mock.patch.object(utilities.mt, "city_health", side_effect=lambda: observed["cityHealth"]), \
                mock.patch.object(utilities, "budget_of", return_value=100), \
                mock.patch.object(utilities, "town_center", return_value=(0.0, 0.0)), \
                mock.patch.object(utilities, "cash_display", return_value=154679.0), \
                mock.patch.object(utilities, "industry_anchors", return_value=[]), \
                mock.patch.object(utilities, "place_plant", return_value=plant) as place:
            utilities.add_power("t", ["electricity"])
        self.assertEqual(1, place.call_count)  # 37000/0.7 - 45440 = 7417 kW: one turbine

    def firm_power(self, steady_rated, firm=True, plants=None, sun=0.0):
        now = {"utilities": {"electricity": {"capacity": 519808, "used": 315920, "utilization": 0.608}}}
        plant = {"ok": True, "regressionOk": True, "prefab": "Coal Power Plant", "expectedKw": 40000, "cost": 19000.0,
                 "steady": True}
        with mock.patch.object(utilities.mt, "city_health", return_value=now), \
                mock.patch.object(utilities, "starved", return_value=False), \
                mock.patch.object(utilities, "grid_mix", return_value=(steady_rated, 664000.0)), \
                mock.patch.object(utilities, "sun_rating", return_value=sun), \
                mock.patch.object(utilities.mt, "call_tool", return_value={"options": []}), \
                mock.patch.object(utilities, "budget_of", return_value=100), \
                mock.patch.object(utilities, "town_center", return_value=(0.0, 0.0)), \
                mock.patch.object(utilities, "cash_display", return_value=1900000.0), \
                mock.patch.object(utilities, "industry_anchors", return_value=[]), \
                mock.patch.object(utilities, "place_plant",
                                  **({"side_effect": plants} if plants else {"return_value": plant})) as place:
            return utilities.add_power("t", ["electricity"], None, firm), place

    # the use at POWER_PRESSURE, against the steady plants and half of what the turbines give now
    LULL_SHORT = 315920 / 0.85 - 519808 * 0.5

    def test_after_an_unseen_outage_the_grid_is_built_to_carry_a_lull(self):
        result, place = self.firm_power(0.0)
        self.assertEqual(utilities.MAX_PLANTS, place.call_count)  # 111,767 short: three steady plants
        self.assertTrue(all(call.args[5] is True for call in place.call_args_list))
        self.assertEqual(0, result["firmNeededKw"])
        steady = 2 * (self.LULL_SHORT - 30000)  # each steady unit stands for two units of turbines
        result, place = self.firm_power(steady)
        self.assertEqual(1, place.call_count)  # 30,000 short: one plant of 40,000
        result, place = self.firm_power(2 * self.LULL_SHORT + 1)
        self.assertIsNone(result)  # it carries a lull: the easy grid is left alone
        result, place = self.firm_power(0.0, firm=False)
        self.assertIsNone(result)  # no outage seen: wind alone is not doubted

    def test_with_no_site_for_a_steady_plant_turbines_carry_the_lull_at_half(self):
        no_site = {"ok": False, "reason": "noSite", "tried": [], "expectedKw": 0, "cost": 0.0}
        turbine = {"ok": True, "regressionOk": True, "prefab": "Advanced Wind Turbine", "expectedKw": 20000,
                   "cost": 12000.0, "steady": False}
        result, place = self.firm_power(0.0, plants=[no_site] + [turbine] * 3)
        self.assertEqual(4, place.call_count)
        self.assertEqual([True, False, False, False],
                         [len(call.args) > 5 and call.args[5] is True for call in place.call_args_list])
        self.assertEqual(3, result["plants"])
        self.assertEqual(round(self.LULL_SHORT - 3 * 20000 * utilities.LULL_SHARE), result["firmNeededKw"])
        self.assertEqual([{"prefab": "Advanced Wind Turbine", "ok": True}] * 3, result["attempts"])

    def test_a_steady_search_never_offers_wind_or_sun(self):
        options = [{"name": name, "aiType": ai, "roles": {"power": power}, "constructionCost": 100,
                    "maintenanceCost": upkeep, "affordable": True, "pollutionRadius": 0}
                   for name, ai, power, upkeep in (("Wind Turbine", "WindTurbineAI", 500, 1),
                                                   ("Solar Power Plant", "SolarPowerPlantAI", 10000, 1),
                                                   ("Coal Power Plant", "PowerPlantAI", 2500, 350))]
        searched = []

        def tool(name, args):
            if name == "compare_service_options":
                return {"options": options}
            searched.append(args["buildingPrefab"])
            return {"safe": False}
        with mock.patch.object(utilities.mt, "call_tool", side_effect=tool), \
                mock.patch.object(utilities.mt, "city_health",
                                  return_value={"utilities": {"electricity": {"capacity": 1000}}}), \
                mock.patch.object(utilities.mt, "_request", return_value={"facilities": []}):
            utilities.place_plant("t", (0.0, 0.0), steady=True)
            self.assertEqual({"Coal Power Plant"}, set(searched))
            searched.clear()
            utilities.place_plant("t", (0.0, 0.0))
            self.assertEqual("Solar Power Plant", searched[0])

    def test_a_lull_is_sized_from_the_typical_yield_not_one_reading(self):
        options = {"options": []}
        with mock.patch.object(utilities.mt, "call_tool", return_value=options), \
                mock.patch.object(utilities, "grid_mix", return_value=(0.0, 1544000.0)), \
                mock.patch.object(utilities, "sun_rating", return_value=320000.0):
            # one reading in a mild lull (capacity 1,002,544) less the sun rating, halved again
            now = utilities.firm_shortfall(380064, 1002544)
            typical = utilities.firm_shortfall(380064, 1002544, 0.75)
        self.assertGreater(now, 100000)
        self.assertLess(typical, 0)  # 0.75 of 1,224 MW of turbines, halved, carries 447 MW: enough
        self.assertAlmostEqual(380064 / 0.85 - 0.75 * 1224000 * utilities.LULL_SHARE, typical, places=3)

    def test_the_typical_yield_is_the_median_of_recent_wakes(self):
        with tempfile.TemporaryDirectory() as root:
            city = city_module.City({"instanceId": "t"}, Path(root))
            samples = [0.9, 0.5, 0.7] + [0.6] * cycle.YIELD_WAKES
            seen = []
            for sample in samples:
                with mock.patch.object(cycle.utilities, "yield_sample", return_value=sample):
                    seen.append(cycle.typical_yield(city))
            self.assertEqual([0.9, 0.7, 0.7], seen[:3])
            self.assertEqual(0.6, seen[-1])  # the oldest readings have left the window
            self.assertEqual(cycle.YIELD_WAKES, len(city.load_state("power-outages", {})["yields"]))
            with mock.patch.object(cycle.utilities, "yield_sample", return_value=None):
                self.assertEqual(0.6, cycle.typical_yield(city))

    def test_an_outage_seen_once_is_remembered(self):
        with tempfile.TemporaryDirectory() as root:
            city = city_module.City({"instanceId": "t"}, Path(root))
            self.assertFalse(cycle.firm_wanted(city, {"problemCounts": {"Electricity": 5}}))
            self.assertTrue(cycle.firm_wanted(city, {"problemCounts": {"Electricity": 173}}))
            self.assertTrue(cycle.firm_wanted(city, {"problemCounts": {}}))

    def test_a_lull_search_with_no_site_waits_unless_the_grid_runs_short_again(self):
        with tempfile.TemporaryDirectory() as root:
            city = city_module.City({"instanceId": "t"}, Path(root))
            self.assertTrue(cycle.firm_wanted(city, {"problemCounts": {"Electricity": 173}}))
            cycle.firm_unsited(city, {"ok": None, "reason": "firmNoSite"})
            waits = [cycle.firm_wanted(city, {}) for _ in range(cycle.FIRM_RETRY)]
            self.assertEqual([False] * (cycle.FIRM_RETRY - 1) + [True], waits)
            cycle.firm_unsited(city, {"ok": None, "reason": "firmNoSite"})
            self.assertTrue(cycle.firm_wanted(city, {"problemCounts": {"Electricity": 40}}))  # a new outage
            cycle.firm_unsited(city, {"ok": True})
            self.assertTrue(cycle.firm_wanted(city, {}))

    def test_solar_plants_count_for_nothing_in_a_lull_and_are_not_built_for_one(self):
        result, place = self.firm_power(2 * self.LULL_SHORT + 1, sun=320000.0)
        self.assertIsNotNone(result)  # two solar plants standing do not carry the lull
        self.assertTrue(all(call.args[7] is False for call in place.call_args_list))  # no sun offered
        options = [{"name": name, "aiType": ai, "roles": {"power": power}, "constructionCost": 100,
                    "maintenanceCost": 1, "affordable": True, "pollutionRadius": 0}
                   for name, ai, power in (("Solar Power Plant", "SolarPowerPlantAI", 10000),
                                           ("Wind Turbine", "WindTurbineAI", 500))]
        searched = []

        def tool(name, args):
            if name == "compare_service_options":
                return {"options": options}
            searched.append(args["buildingPrefab"])
            return {"safe": False}
        with mock.patch.object(utilities.mt, "call_tool", side_effect=tool), \
                mock.patch.object(utilities.mt, "city_health",
                                  return_value={"utilities": {"electricity": {"capacity": 1000}}}), \
                mock.patch.object(utilities.mt, "_request", return_value={"facilities": []}):
            utilities.place_plant("t", (0.0, 0.0), sun=False)
        self.assertEqual({"Wind Turbine"}, set(searched))

    def test_a_lull_with_no_site_does_not_stop_the_loop_but_a_short_grid_does(self):
        no_site = {"ok": False, "reason": "noSite", "tried": [], "expectedKw": 0, "cost": 0.0}
        result, place = self.firm_power(0.0, plants=[no_site, no_site])
        self.assertEqual((None, "firmNoSite"), (result["ok"], result["reason"]))
        self.assertEqual(utilities.FIRM_SEARCHES, place.call_args_list[0].args[6])
        self.assertGreater(utilities.FIRM_SEARCHES[-1][0], max(radius for radius, _ in utilities.SITE_SEARCHES))
        short = {"utilities": {"electricity": {"capacity": 45440, "used": 60752, "utilization": 1.337}}}
        with mock.patch.object(utilities.mt, "city_health", return_value=short), \
                mock.patch.object(utilities, "starved", return_value=False), \
                mock.patch.object(utilities, "budget_of", return_value=100), \
                mock.patch.object(utilities, "town_center", return_value=(0.0, 0.0)), \
                mock.patch.object(utilities, "cash_display", return_value=1900000.0), \
                mock.patch.object(utilities, "industry_anchors", return_value=[]), \
                mock.patch.object(utilities, "place_plant", return_value=no_site) as place:
            result = utilities.add_power("t", ["electricity"])
        self.assertFalse(result["ok"])  # short now: the loop stops for a look
        self.assertEqual(utilities.SITE_SEARCHES, place.call_args.args[6])

    def test_a_lull_the_wake_read_sizes_the_plants_though_the_grid_now_reads_easy(self):
        now = {"utilities": {"electricity": {"capacity": 638736, "used": 361968, "utilization": 0.567}}}
        plant = {"ok": True, "regressionOk": True, "prefab": "Advanced Wind Turbine", "expectedKw": 8740, "cost": 12000.0}
        for peak, built in ((None, 0), (0.6, 0), (1.149, utilities.MAX_PLANTS)):
            with mock.patch.object(utilities.mt, "city_health", return_value=now), \
                    mock.patch.object(utilities, "starved", return_value=False), \
                    mock.patch.object(utilities, "budget_of", return_value=100), \
                    mock.patch.object(utilities, "town_center", return_value=(0.0, 0.0)), \
                    mock.patch.object(utilities, "cash_display", return_value=1113844.0), \
                    mock.patch.object(utilities, "industry_anchors", return_value=[]), \
                    mock.patch.object(utilities, "place_plant", return_value=plant) as place:
                result = utilities.add_power("t", ["electricity"], peak)
            self.assertEqual(built, place.call_count, peak)
        # the lull left 361968 / 1.149 kW: to POWER_TARGET takes far more than three turbines
        need = 361968 / utilities.POWER_TARGET - 361968 / 1.149
        self.assertEqual(round(need - 3 * 8740), result["stillNeededKw"])

    def test_a_plant_no_town_site_takes_is_sought_beside_the_industry(self):
        searched = []

        def call(tool, arguments):
            if tool == "compare_service_options":
                return {"options": [self.ADVANCED]}
            if tool == "propose_power_plant_candidates":
                anchor = (arguments["serviceAreaAnchor"]["x"], arguments["serviceAreaAnchor"]["z"])
                searched.append(anchor)
                found = anchor == (2565.8, -19.5)  # homes ring the town; the industrial district has room
                return {"safe": found, "candidates": [{"position": {"x": 2694.2, "z": -154.8}}] if found else []}
            raise AssertionError(tool)
        with mock.patch.object(utilities.mt, "call_tool", side_effect=call), \
                mock.patch.object(utilities.mt, "city_health",
                                  return_value={"utilities": {"electricity": {"capacity": 101360}}}), \
                mock.patch.object(utilities, "variable_yield", return_value=0.44), \
                mock.patch.object(utilities.mt, "_land_use_context", return_value={"wells": []}), \
                mock.patch.object(utilities.protocol, "run", return_value={"ok": True, "regressionOk": True}):
            result = utilities.place_plant("t", (2567.2, -869.9), None, None, [(2565.8, -19.5)])
        self.assertTrue(result["ok"])
        self.assertEqual({"x": 2694.2, "z": -154.8}, result["position"])
        self.assertEqual([(2567.2, -869.9), (2565.8, -19.5)], searched)  # the strictest search from both first

    def test_industrial_districts_are_the_largest_clusters_of_factories(self):
        factories = [{"id": i, "service": "Industrial", "position": {"x": 2500 + 40 * (i % 5), "z": -40 * (i // 5)}}
                     for i in range(15)] + \
                    [{"id": 100 + i, "service": "Industrial", "position": {"x": 1200 + 40 * i, "z": -1500}}
                     for i in range(4)] + \
                    [{"id": 200, "service": "Industrial", "position": {"x": 0, "z": 0}},
                     {"id": 300, "service": "Residential", "position": {"x": 2500, "z": 0}}]
        with mock.patch.object(utilities.mt, "_request", return_value={"buildings": factories}):
            anchors = utilities.industry_anchors()
        self.assertEqual(utilities.INDUSTRY_ANCHORS, len(anchors))
        self.assertEqual((2580.0, -40.0), anchors[0])
        self.assertEqual((1260.0, -1500.0), anchors[1])

    def test_a_building_a_line_was_laid_to_this_wake_waits_a_wake(self):
        dark = {"summary": {"missingElectricity": 1}, "unpowered": [46981], "unreached": [], "island": None}
        report = StopRuleTests.report(StopRuleTests(), power={**dark, "steps": [{"building": 46981, "ok": True}]})
        self.assertEqual([], play.stop_reasons(report, {"a"}, 4000))
        report = StopRuleTests.report(StopRuleTests(), power={**dark, "steps": []})
        self.assertEqual(["unpowered"], [next(iter(r)) for r in play.stop_reasons(report, {"a"}, 4000)])

    def test_buildings_beyond_the_grid_wait_while_lines_still_reach_out(self):
        dark = {"summary": {"missingElectricity": 170}, "unpowered": [388], "unreached": [388, 900, 1008, 1347],
                "island": None}
        stop = StopRuleTests.report(StopRuleTests(), power={**dark, "steps": [{"building": 287, "ok": True}]})
        stop["powerPlant"] = {"ok": True, "regressionOk": True}
        self.assertEqual([], play.stop_reasons(stop, {"a"}, 4000))
        stuck = StopRuleTests.report(StopRuleTests(), power={**dark, "steps": [{"building": 287, "ok": False}]})
        stuck["powerPlant"] = {"ok": True, "regressionOk": True}
        self.assertEqual(["unpowered"], [next(iter(r)) for r in play.stop_reasons(stuck, {"a"}, 4000)])


class RebuildFullTests(unittest.TestCase):
    TAKEN = {"ok": False, "regressionOk": True, "result": {"error": "The lot cannot take the rebuilt facility; "
                                                                     "the full one was kept"}}
    DONE = {"ok": True, "regressionOk": True, "result": {"newBuildingId": 7}}

    def rebuild(self, outcomes, overlaps, growables=(16362,), safe=True):
        runs = []

        def run(label, tool, arguments, settle=0.0):
            runs.append((tool, arguments))
            if tool == "bulldoze_entity":
                return {"ok": True, "regressionOk": True}
            return outcomes.pop(0)

        def request(method, path, query=None, body=None):
            if path == "/state/facilities":
                return {"facilities": [{"id": 44940, "prefab": "Landfill Site", "angleDegrees": 0,
                                        "position": {"x": 2370.29, "z": -1177.45}}]}
            if path == "/commands/preview-placement":
                assert body["ignoreBuildingId"] == 44940
                return {"valid": False, "buildingOverlaps": [{"id": item} for item in overlaps]}
            if path == "/state/growables":
                return {"buildings": [{"id": item} for item in growables]}
            raise AssertionError(path)
        with mock.patch.object(services.protocol, "run", side_effect=run), \
                mock.patch.object(services.mt, "_request", side_effect=request), \
                mock.patch.object(services.mt, "call_tool", return_value={"safe": safe}):
            return services.rebuild_full([44940]), runs

    def test_a_building_grown_onto_the_lot_is_cleared_and_the_facility_rebuilt(self):
        results, runs = self.rebuild([self.TAKEN, self.DONE], overlaps=[16362])
        self.assertEqual((True, [16362]), (results[0]["ok"], results[0]["cleared"]))
        self.assertEqual(["rebuild_full_facility", "bulldoze_entity", "rebuild_full_facility"], [t for t, _ in runs])

    def test_a_lot_a_service_building_stands_on_is_left_and_waits(self):
        results, runs = self.rebuild([self.TAKEN], overlaps=[999])  # 999 is no growable
        self.assertEqual(("lotTaken", None), (results[0]["stage"], results[0]["ok"]))
        self.assertEqual(["rebuild_full_facility"], [t for t, _ in runs])
        results, runs = self.rebuild([self.TAKEN], overlaps=[16362], safe=False)  # its clearing is unsafe
        self.assertEqual("lotTaken", results[0]["stage"])
        self.assertNotIn("bulldoze_entity", [t for t, _ in runs])
        report = StopRuleTests.report(StopRuleTests())
        report["services"] = {"escalations": [], "actions": [{"rebuildFull": [44940], "results": results}]}
        self.assertEqual([], play.stop_reasons(report, {"a"}, 4000))

    def test_a_rebuild_that_fails_after_the_old_one_went_stops_the_loop(self):
        gone = {"ok": False, "regressionOk": True,
                "result": {"error": "Rebuild failed after the full facility was removed: blocked"}}
        results, _ = self.rebuild([gone], overlaps=[])
        self.assertIs(False, results[0]["ok"])
        report = StopRuleTests.report(StopRuleTests())
        report["services"] = {"escalations": [], "actions": [{"rebuildFull": [44940], "results": results}]}
        self.assertEqual(["serviceActionFailed"], [next(iter(r)) for r in play.stop_reasons(report, {"a"}, 4000)])


class ThroughTests(unittest.TestCase):
    WAY = {"nodeId": 11945, "clear": True, "crosses": [], "blocked": 2, "distance": 115.3,
           "from": {"x": 0.0, "z": 0.0}, "to": {"x": 115.3, "z": 0.0}}

    def through(self, obstacles, builds=None):
        """obstacles: the preview's per piece; builds: outcomes of the pieces built."""
        previews = iter(obstacles)

        def preview(prefab, start, end, label, lift=0.0):
            found = next(previews)
            return (not found), {}, ({"valid": False, "obstacles": found} if found else {"valid": True})
        builds = iter(builds or [{"ok": True, "segmentId": 70 + n, "end": {"x": 60.0 * (n + 1), "z": 0.0}}
                                 for n in range(4)])
        with mock.patch.object(growth.roads, "preview", side_effect=preview), \
                mock.patch.object(growth.roads, "clear_buildings",
                                  side_effect=lambda label, ids: {"ok": True, "cleared": ids}) as clear, \
                mock.patch.object(growth.roads, "build", side_effect=lambda *a, **k: next(builds)) as build, \
                mock.patch.object(growth, "_remove", side_effect=lambda label, ids: ids) as remove:
            result = growth.open_through(None, "a", self.WAY)
        return result, clear, build, remove

    def test_a_row_of_factories_is_cleared_and_the_way_built(self):
        factories = [{"id": 23382, "service": "Industrial"}, {"id": 27831, "service": "Industrial"}]
        result, clear, build, _ = self.through([factories, factories[:1]])  # the way is cut into two pieces
        self.assertTrue(result["ok"])
        self.assertEqual([23382, 27831], clear.call_args.args[1])
        self.assertEqual(2, build.call_count)
        self.assertEqual((120.0, 0.0), result["end"])

    def test_homes_and_service_buildings_are_never_cleared(self):
        for blocker in ({"id": 9, "service": "Residential"}, {"id": 9, "service": "Garbage"}):
            result, clear, build, _ = self.through([[{"id": 8, "service": "Industrial"}], [blocker]])
            self.assertEqual("inTheWay", result["stage"])
            clear.assert_not_called()
            build.assert_not_called()

    def test_one_power_line_is_taken_down_and_the_grid_joined_again_behind_the_road(self):
        line = {"segmentId": 13363, "distance": 0.9}
        runs = []
        with mock.patch.object(growth.protocol, "run",
                               side_effect=lambda label, tool, args, *a: runs.append((tool, args)) or {"ok": True}), \
                mock.patch.object(growth.utilities, "repair_power", return_value={
                    "summary": {"missingElectricity": 0}, "steps": [{"building": 3395, "ok": True}]}) as repair:
            result, clear, build, _ = self.through([[line], [line]])
        self.assertTrue(result["ok"])
        self.assertEqual([("bulldoze_entity", {"entityType": "netSegment", "id": 13363})], runs)
        self.assertEqual(2, build.call_count)
        repair.assert_called_once()
        self.assertEqual({"missingElectricity": 0, "lines": 1}, result["rejoined"])
        clear.assert_not_called()

    def test_two_power_lines_in_the_way_are_left_standing(self):
        result, _, build, _ = self.through([[{"segmentId": 1, "distance": 3}], [{"segmentId": 2, "distance": 3}]])
        self.assertEqual("tooMany", result["stage"])
        build.assert_not_called()

    def test_more_than_a_few_workplaces_is_not_cleared(self):
        many = [{"id": n, "service": "Commercial"} for n in range(growth.THROUGH_MAX + 1)]
        result, clear, _, _ = self.through([many, []])
        self.assertEqual("tooMany", result["stage"])
        clear.assert_not_called()

    def test_a_piece_refused_after_clearing_leaves_no_stub(self):
        refused = {"ok": False, "stage": "preview", "tried": [{"terrainSafe": False}]}
        result, _, _, remove = self.through([[{"id": 8, "service": "Industrial"}], []],
                                            builds=[{"ok": True, "segmentId": 70, "end": {"x": 60.0, "z": 0.0}}, refused])
        self.assertFalse(result["ok"])
        self.assertEqual([70], remove.call_args.args[1])

    def test_the_way_through_is_tried_only_when_no_clear_way_takes(self):
        area = {"center": {"x": 400, "z": 0}, "areaSquareMeters": 2177103,
                "fishbone": {"collectorHeadingDegrees": 90, "branchLength": 96},
                "access": [{**self.WAY, "nodeId": 17222, "blocked": 4}, {**self.WAY, "blocked": 3}]}
        refused = {"ok": False, "stage": "preview", "reason": "buildings"}
        built = {"collector": [{"ok": True}], "branches": [], "collectorNodes": 2, "ended": None}
        with tempfile.TemporaryDirectory() as root:
            city = city_module.City({"instanceId": "t"}, Path(root))
            with mock.patch.object(growth.mt, "call_tool", return_value={"areas": [area], "candidates": []}), \
                    mock.patch.object(growth, "open_access", return_value=refused) as opened, \
                    mock.patch.object(growth, "open_through",
                                      return_value={"ok": True, "end": (115.3, 0.0), "cleared": [1, 2, 3]}) as through, \
                    mock.patch.object(growth.roads, "grow_fishbone", return_value=built):
                result = growth.expand(city, "workplace", {"wake": 1}, "a")
        self.assertTrue(result["ok"])
        self.assertEqual(2, opened.call_count)  # both ways tried without clearing first
        self.assertEqual(11945, through.call_args.args[2]["nodeId"])  # then the one with fewest in the way


if __name__ == "__main__":
    unittest.main()

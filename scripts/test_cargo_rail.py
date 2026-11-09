import math
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from loop import cargo, cargo_remote, play, traffic, wake  # noqa: E402
import mayor_tools  # noqa: E402


def _live_bridge(*args, **kwargs):
    raise AssertionError("a test reached the live Bridge")


_GUARD = mock.patch.object(mayor_tools.mayor_transport, "request", side_effect=_live_bridge)


def setUpModule():
    _GUARD.start()


def tearDownModule():
    _GUARD.stop()


def _segment(sid, a, b, pa, pb, prefab="Train Track", service="PublicTransport", sub="PublicTransportTrain", **extra):
    return {"id": sid, "prefab": prefab, "service": service, "subService": sub, "startNodeId": a, "endNodeId": b,
            "start": {"x": pa[0], "z": pa[1]}, "end": {"x": pb[0], "z": pb[1]},
            "middle": {"x": (pa[0] + pb[0]) / 2, "z": (pa[1] + pb[1]) / 2}, **extra}


def _world():
    """A railway along z = 0 (nodes every 50 m from x -600 to 600, its west end an outside
    connection), a local road along z = -100, and industry south of the road."""
    xs = list(range(-600, 601, 50))
    rail = [_segment(100 + i, 1 + i, 2 + i, (xs[i], 0), (xs[i + 1], 0),
                     **({"prefab": "Train Connection Track", "startOutside": True} if i == 0 else {}))
            for i in range(len(xs) - 1)]
    road = [_segment(300 + i, 400 + i, 401 + i, (xs[i], -100), (xs[i + 1], -100), "Basic Road", "Road", "None")
            for i in range(len(xs) - 1)]
    industry = [{"id": 600 + i, "service": "Industrial", "position": {"x": -60 + 30 * i, "z": -160},
                 "problems": "NoResources" if i % 2 else ""} for i in range(5)]
    return rail + road, industry


def _node_at(segments, point):
    for segment in segments:
        for end in ("start", "end"):
            if math.dist(cargo._xz(segment[end]), point) < 2.0:
                return segment[f"{end}NodeId"]
    return None


class FakeBridge:
    """Previews answered from the synthetic world: a lot covers the growables inside it; a rail
    piece joins the railway where it ends on a node."""

    def __init__(self, segments, growables, *, milestones=True, rail_refusals=()):
        self.segments, self.growables = segments, growables
        self.milestones, self.rail_refusals = milestones, rail_refusals
        self.lots, self.rails = [], []

    def lot(self, site):
        self.lots.append(site)
        frame = cargo.Frame(site["center"], site["angleDegrees"])
        under = [{"id": g["id"], "prefab": "x"} for g in self.growables if frame.inside(cargo._xz(g["position"]))]
        result = {"valid": not under, "gamePlacement": {"errors": "None", "position": cargo._point(site["center"]),
                                                        "angleDegrees": site["angleDegrees"]},
                  "safety": {"risks": ["buildingOverlap"] if under else []}, "roadOverlaps": [],
                  "roadAccess": {"accessible": True}, "buildingOverlaps": under,
                  "networkPaths": [{"prefab": "Cargo Connection", "nodes": [{"x": 0, "z": 0}, {"x": 1, "z": 1}]},
                                   {"prefab": cargo.STATION_TRACK, "nodes": [
                                       cargo._point(frame.world(*cargo.TRACK_LOCAL[0])),
                                       cargo._point(frame.world(*cargo.TRACK_LOCAL[1]))]}]}
        if self.milestones:
            result["createPassMilestones"] = [{"name": "Cargo Center Created", "passed": False}]
        return result

    def rail(self, prefab, start, end):
        self.rails.append((prefab, start, end))
        joined = _node_at(self.segments, end) is not None
        refused = any(math.dist(end, point) < 2.0 for point in self.rail_refusals)
        return {"groundRail": True, "valid": False, "networkUnlocked": prefab != cargo.TRACK,
                "validIfUnlocked": not refused, "toolErrors": "ObjectCollision" if refused else "None",
                "reusedStartNode": False, "reusedEndNode": joined, "buildingsUnderRoad": [], "powerLinesCrossed": []}


class FrameTests(unittest.TestCase):
    def test_the_lot_frame_puts_the_terminal_track_where_the_live_preview_did(self):
        # live: Cargo Center at (3300, 560) laid its track from (3212, 536) to (3388, 536) at 0 degrees
        # and from (3324, 472) to (3324, 648) at 90 degrees
        for angle, ends in ((0.0, [(3212, 536), (3388, 536)]), (90.0, [(3324, 472), (3324, 648)])):
            frame = cargo.Frame((3300.0, 560.0), angle)
            got = sorted(frame.world(*local) for local in cargo.TRACK_LOCAL)
            for point, expected in zip(got, sorted(ends)):
                self.assertAlmostEqual(point[0], expected[0], places=3)
                self.assertAlmostEqual(point[1], expected[1], places=3)

    def test_the_facing_angle_turns_the_lot_front_toward_the_given_side(self):
        for front in ((0.0, -1.0), (0.6, 0.8), (-1.0, 0.0)):
            frame = cargo.Frame((0.0, 0.0), cargo.angle_facing(front))
            self.assertAlmostEqual(frame.axis_z[0], front[0], places=4)
            self.assertAlmostEqual(frame.axis_z[1], front[1], places=4)

    def test_distance_from_a_piece_to_the_lot(self):
        frame = cargo.Frame((0.0, 0.0), 0.0)  # 128 m along x, 64 m along z
        self.assertEqual(0.0, frame.distance((-200.0, 0.0), (200.0, 0.0)))  # through it
        self.assertAlmostEqual(8.0, frame.distance((-200.0, 40.0), (200.0, 40.0)))
        self.assertAlmostEqual(5.0, frame.distance((67.0, 36.0), (100.0, 100.0)))  # off a corner


class NetworkTests(unittest.TestCase):
    def test_a_track_reaches_the_outside_only_through_its_own_component(self):
        segments, _ = _world()
        stray = _segment(900, 901, 902, (0, 500), (50, 500))
        net = cargo.rail_network(segments + [stray])
        self.assertIn(13, net["linked"])
        self.assertNotIn(901, net["linked"])
        self.assertEqual({1, 2}, net["outside"] & {1, 2, 3})
        self.assertNotIn(100, [s["id"] for s in cargo.main_line(net)])  # the connection track itself

    def test_a_piece_may_not_cross_a_road_or_track_at_grade_but_may_join_its_node(self):
        segments, _ = _world()
        self.assertEqual([306], cargo.crossings((-260.0, -50.0), (-260.0, -150.0), segments))
        self.assertEqual([], cargo.crossings((-280.0, -50.0), (-250.0, 0.0), segments, {8}))
        self.assertEqual([105], cargo.crossings((-350.0, -50.0), (-300.0, 50.0), segments))
        raised = [dict(segments[30], prefab="Basic Road Elevated"), dict(segments[30], prefab="Bus Line")]
        self.assertEqual([], cargo.crossings((-150.0, -50.0), (-150.0, -150.0), raised))

    def test_a_link_meets_the_railway_at_a_real_angle(self):
        segments, _ = _world()
        net = cargo.rail_network(segments)
        main = cargo.main_line(net)
        routes = cargo.link_routes((88.0, -40.0), (1.0, 0.0), (0.0, 1.0), net, main)
        self.assertTrue(routes)
        for route in routes:
            last = cargo._sub(route["points"][-1], route["points"][-2])
            self.assertGreaterEqual(cargo.merge_angle(last, route["nodeId"], net, main), cargo.MERGE_MIN)
            self.assertGreaterEqual(cargo._dot(cargo._sub(route["points"][-1], (88.0, -40.0)), (1.0, 0.0)),
                                    cargo.LINK_MIN_AHEAD)
        # nothing behind the end, nothing on the far side of the railway
        self.assertEqual([], cargo.link_routes((88.0, -40.0), (1.0, 0.0), (0.0, -1.0), net, main))


class SiteTests(unittest.TestCase):
    def test_a_lot_slides_from_the_railway_to_the_road_and_faces_it(self):
        segments, growables = _world()
        middle = next(s for s in segments if s["id"] == 112)  # x 0..50
        found = cargo.sites(middle, segments, growables)
        self.assertTrue(found)
        for site in found:
            self.assertEqual(-1, site["side"])  # no road north of the railway
            frame = cargo.Frame(site["center"], site["angleDegrees"])
            self.assertAlmostEqual(-1.0, frame.axis_z[1], places=6)  # front to the road
            self.assertEqual(64.0, site["offset"])  # the lot's front edge 4 m short of the road
        self.assertEqual({"railParallel"}, {site["fit"] for site in found})  # road parallel to the railway

    def test_no_road_within_reach_gives_no_lot(self):
        segments, growables = _world()
        rail = [s for s in segments if s["service"] == "PublicTransport"]
        self.assertEqual([], cargo.sites(rail[12], rail, growables))


class JudgeTests(unittest.TestCase):
    SITE = {"center": (0.0, -64.0), "angleDegrees": 180.0}

    def preview(self, **change):
        result = FakeBridge([], []).lot(self.SITE)
        result.update(change)
        return result

    def test_placement_refusals(self):
        growables = {1: {"service": "Commercial"}}
        cases = [({"createPassMilestones": None}, None),
                 ({"gamePlacement": {"errors": "None", "position": {"x": 5.0, "z": -64.0}, "angleDegrees": 180.0}},
                  "snapped"),
                 ({"roadOverlaps": [{"id": 3}]}, "roadOverlap"),
                 ({"roadAccess": {"accessible": False}}, "roadAccess"),
                 ({"terrain": {"heightRange": 9.7}}, "notLevel"),
                 ({"buildingOverlaps": [{"id": 2}], "safety": {"risks": ["buildingOverlap"]}}, "blocked"),
                 ({"safety": {"risks": ["terrainHeightRangeExceeds16m"]}}, ["terrainHeightRangeExceeds16m"])]
        for change, why in cases:
            preview = self.preview(**change)
            if why is None:
                del preview["createPassMilestones"]
                why = "bridgeOutdated"
            self.assertEqual(why, cargo.judge_placement(preview, self.SITE, growables)["why"], change)

    def test_growables_and_noise_do_not_refuse_a_lot(self):
        preview = self.preview(buildingOverlaps=[{"id": 1}],
                               safety={"risks": ["buildingOverlap", "residentialInsideNoiseRadius"]})
        judged = cargo.judge_placement(preview, self.SITE, {1: {"service": "Residential"}})
        self.assertTrue(judged["ok"])
        self.assertEqual(([1], True), (judged["clear"], judged["noisy"]))
        self.assertEqual([(88.0, -40.0), (-88.0, -40.0)], [tuple(round(v, 3) for v in end) for end in judged["ends"]])

    def test_rail_refusals(self):
        base = {"groundRail": True, "validIfUnlocked": True, "reusedEndNode": True, "buildingsUnderRoad": [],
                "powerLinesCrossed": []}
        growables = {5: {"service": "Industrial"}}
        self.assertEqual("bridgeOutdated", cargo.judge_rail({"valid": True}, growables)["why"])
        self.assertEqual("powerLine", cargo.judge_rail({**base, "powerLinesCrossed": [{"segmentId": 1}]}, growables)["why"])
        self.assertEqual("blocked", cargo.judge_rail({**base, "buildingsUnderRoad": [{"id": 6}]}, growables)["why"])
        self.assertEqual("native:ObjectCollision", cargo.judge_rail(
            {**base, "validIfUnlocked": False, "toolErrors": "ObjectCollision"}, growables)["why"])
        self.assertEqual("notJoined", cargo.judge_rail({**base, "reusedEndNode": False}, growables)["why"])
        self.assertEqual("kneeOnNode", cargo.judge_rail(base, growables, joins=False)["why"])
        self.assertEqual([5], cargo.judge_rail({**base, "buildingsUnderRoad": [{"id": 5}]}, growables)["clear"])
        # the terminal's own track: only what it runs over counts
        self.assertTrue(cargo.judge_rail({**base, "validIfUnlocked": False, "reusedEndNode": False}, growables,
                                         own_track=True)["ok"])


class PlanTests(unittest.TestCase):
    def plan(self, bridge, segments, growables, **kwargs):
        return cargo.plan(segments, growables, [], preview_lot=bridge.lot, preview_rail=bridge.rail, **kwargs)

    def test_a_lot_with_both_ends_joined_is_chosen_and_its_growables_listed(self):
        segments, industry = _world()
        shop = {"id": 501, "service": "Commercial", "position": {"x": 10, "z": -60}}
        growables = industry + [shop]
        bridge = FakeBridge(segments, growables)
        site = self.plan(bridge, segments, growables)
        self.assertTrue(site["ok"], site)
        self.assertEqual(2, len(site["links"]))
        self.assertEqual(0, site["homes"])
        net = cargo.rail_network(segments)
        for link in site["links"]:
            self.assertIn(link["nodeId"], net["linked"])
            self.assertEqual(site["ends"][link["end"]], link["points"][0])
        # every piece of the chosen links was previewed with Train Track before anything was built
        for link in site["links"]:
            for piece in zip(link["points"], link["points"][1:]):
                self.assertIn((cargo.TRACK, *piece), bridge.rails)
        self.assertIn(cargo.STATION_TRACK, [prefab for prefab, _, _ in bridge.rails])
        frame = cargo.Frame(site["center"], site["angleDegrees"])
        self.assertEqual([501] if frame.inside((10.0, -60.0)) else [], site["clear"])

    def test_too_many_homes_refuse_a_lot(self):
        segments, industry = _world()
        homes = [{"id": 700 + i, "service": "Residential", "position": {"x": x, "z": -60}}
                 for i, x in enumerate(range(-700, 701, 20))]
        bridge = FakeBridge(segments, industry + homes)
        site = self.plan(bridge, segments, industry + homes)
        self.assertFalse(site["ok"])
        self.assertIn("wouldClear", site["refused"])

    def test_an_end_whose_ways_are_all_refused_leaves_one_link(self):
        segments, industry = _world()
        east = [(float(x), 0.0) for x in range(-600, 601, 50)]
        bridge = FakeBridge(segments, industry, rail_refusals=[p for p in east if p[0] > 0])
        with mock.patch.object(cargo, "MAX_SEGMENTS", 1):
            site = self.plan(bridge, segments, industry)
        self.assertTrue(site["ok"], site)
        self.assertEqual(1, len(site["links"]))

    def test_an_old_bridge_or_a_busy_game_stops_the_plan_without_a_verdict(self):
        segments, industry = _world()
        old = FakeBridge(segments, industry, milestones=False)
        self.assertEqual({"ok": None, "why": "bridgeOutdated"}, self.plan(old, segments, industry))
        busy = mock.Mock(side_effect=mayor_tools.BridgeRequestError(
            {"error": "Timed out waiting for the game thread; the command was not run."}))
        self.assertEqual({"ok": None, "why": "busy"},
                         cargo.plan(segments, industry, [], preview_lot=busy, preview_rail=old.rail))

    def test_tried_sites_are_not_planned_again(self):
        segments, industry = _world()
        first = self.plan(FakeBridge(segments, industry), segments, industry)
        second = self.plan(FakeBridge(segments, industry), segments, industry, tried={first["key"]})
        self.assertNotEqual(first["key"], second.get("key"))


class BuildTests(unittest.TestCase):
    SITE = {"key": "k", "center": (0.0, -64.0), "angleDegrees": 180.0, "clear": [501],
            "ends": [(88.0, -40.0), (-88.0, -40.0)],
            "links": [{"end": 0, "points": [(88.0, -40.0), (120.0, -30.0), (150.0, 0.0)], "nodeId": 16},
                      {"end": 1, "points": [(-88.0, -40.0), (-150.0, 0.0)], "nodeId": 10}]}

    def run_build(self, *, fail_piece=None, unlocked=True, connected_ok=True, steep=()):
        before = [{"id": 101, "startNodeId": 1, "endNodeId": 2, "start": {"x": 0, "z": 0}, "end": {"x": 50, "z": 0},
                   "subService": "PublicTransportTrain", "prefab": cargo.TRACK}]
        terminal = [{"id": 900, "startNodeId": 901, "endNodeId": 902, "start": {"x": 88, "z": -40},
                     "end": {"x": -88, "z": -40}, "subService": "PublicTransportTrain", "prefab": cargo.STATION_TRACK}]
        state = {"rail": list(before), "segment": 950, "removed": []}
        events = []

        def rail_segments():
            return list(state["rail"])

        def request(method, path, query=None, body=None):
            if path == "/prefabs/networks":
                return {"networks": [{"name": cargo.TRACK, "unlocked": unlocked}]}
            if path == "/state/networks":
                return {"segments": list(state["rail"])}
            if path == "/state/network-segment-details":
                piece = next(s for s in state["rail"] if s["id"] == query["id"])
                return {"end": piece["end"]}
            raise AssertionError(path)

        def run(label, tool, arguments, settle=0.0):
            events.append((tool, dict(arguments)))
            if tool == "place_service":
                state["rail"] += terminal
                return {"ok": True, "regressionOk": True, "result": {"ok": True, "buildingId": 77}}
            if tool == "build_network":
                state["segment"] += 1
                sid = state["segment"]
                ok = fail_piece != len([e for e in events if e[0] == "build_network"])
                state["rail"].append({"id": sid, "startNodeId": 2000 + sid, "endNodeId": 3000 + sid,
                                      "start": arguments["start"], "end": arguments["end"],
                                      "subService": "PublicTransportTrain", "prefab": cargo.TRACK})
                return {"ok": ok, "regressionOk": True, "result": {"ok": ok, "segmentId": sid},
                        "error": None if ok else "native refused"}
            if tool == "bulldoze_entity":
                if arguments["entityType"] == "netSegment":
                    state["rail"] = [s for s in state["rail"] if s["id"] != arguments["id"]]
                elif arguments["id"] == 77:
                    state["rail"] = [s for s in state["rail"] if s["id"] != 900]  # its paths go with it
                return {"ok": True, "regressionOk": True, "result": {"ok": True}}
            raise AssertionError(tool)

        def preview_rail(prefab, start, end):
            if any(math.dist(end, point) < 1 for point in steep):  # the levelled lot left it too steep
                return {"groundRail": True, "valid": False, "validIfUnlocked": False, "reusedStartNode": True,
                        "reusedEndNode": False, "toolErrors": "SlopeTooSteep", "buildingsUnderRoad": [],
                        "powerLinesCrossed": []}
            return {"groundRail": True, "valid": True, "validIfUnlocked": True, "reusedStartNode": True,
                    "reusedEndNode": math.dist(end, (150.0, 0.0)) < 1 or math.dist(end, (-150.0, 0.0)) < 1,
                    "buildingsUnderRoad": [], "powerLinesCrossed": []}

        with mock.patch.object(cargo, "_rail_segments", side_effect=rail_segments), \
                mock.patch.object(cargo.mt, "_request", side_effect=request), \
                mock.patch.object(cargo.protocol, "run", side_effect=run), \
                mock.patch.object(cargo, "_preview_lot", return_value={"valid": True}), \
                mock.patch.object(cargo, "_preview_rail", side_effect=preview_rail), \
                mock.patch.object(cargo, "connected", return_value={"ok": connected_ok}), \
                mock.patch.object(cargo.roads, "clear_buildings",
                                  side_effect=lambda label, ids: events.append(("clear", ids)) or
                                  {"ok": True, "cleared": list(ids)}):
            return cargo.build("t", self.SITE), events, state

    def test_clears_places_and_lays_each_link_from_the_terminal_node(self):
        result, events, state = self.run_build()
        self.assertTrue(result["ok"], result)
        self.assertEqual(["clear", "place_service", "build_network", "build_network", "build_network"],
                         [e[0] for e in events])
        builds = [args for tool, args in events if tool == "build_network"]
        self.assertEqual({"x": 88.0, "z": -40.0}, builds[0]["start"])  # the terminal's own node
        self.assertEqual(builds[0]["end"], builds[1]["start"])  # the knee just laid
        self.assertEqual({"x": 150.0, "z": 0.0}, builds[1]["end"])
        self.assertEqual({"x": -88.0, "z": -40.0}, builds[2]["start"])
        self.assertTrue(all(args["roadPrefab"] == cargo.TRACK for args in builds))
        self.assertEqual([951, 952, 953], result["links"])

    def test_a_failed_piece_removes_the_links_the_terminal_and_its_track(self):
        result, events, state = self.run_build(fail_piece=2)
        self.assertFalse(result["ok"])
        self.assertEqual("link", result["stage"])
        removed = [(args["entityType"], args["id"]) for tool, args in events if tool == "bulldoze_entity"]
        self.assertEqual([("netSegment", 952), ("netSegment", 951), ("building", 77)], removed)
        self.assertTrue(result["undo"]["removedAll"])
        self.assertEqual([101], [s["id"] for s in state["rail"]])

    def test_one_end_refused_once_the_terminal_stands_still_joins_by_the_other(self):
        result, events, state = self.run_build(steep=[(120.0, -30.0)])
        self.assertTrue(result["ok"], result)
        self.assertEqual([951], result["links"])  # only the second end's one piece
        self.assertEqual([False, True], [item["ok"] for item in result["linked"]])
        self.assertEqual("native:SlopeTooSteep", result["linked"][0]["tried"][0]["why"])
        self.assertNotIn("bulldoze_entity", [e[0] for e in events])

    def test_both_ends_refused_undoes_the_terminal_and_lays_nothing(self):
        result, events, state = self.run_build(steep=[(120.0, -30.0), (-150.0, 0.0)])
        self.assertEqual((False, "linkPreview"), (result["ok"], result["stage"]))
        self.assertNotIn("build_network", [e[0] for e in events])
        self.assertIn(("bulldoze_entity", {"entityType": "building", "id": 77}), events)
        self.assertEqual([101], [s["id"] for s in state["rail"]])

    def test_train_track_still_locked_or_a_terminal_not_joined_undoes_the_terminal(self):
        for kwargs, stage in (({"unlocked": False}, "trackLocked"), ({"connected_ok": False}, "notConnected")):
            result, events, state = self.run_build(**kwargs)
            self.assertEqual((False, stage), (result["ok"], result["stage"]))
            self.assertIn(("bulldoze_entity", {"entityType": "building", "id": 77}), events)
            self.assertEqual([101], [s["id"] for s in state["rail"]])


class LayLinkTests(unittest.TestCase):
    OK = {"groundRail": True, "valid": True, "reusedStartNode": True, "buildingsUnderRoad": [],
          "powerLinesCrossed": []}

    def lay(self, previews, builds_ok=True):
        events, built = [], {"links": []}
        segment = [700]

        def preview(prefab, start, end):
            answer = previews.pop(0)
            return {**self.OK, **answer}

        def run(label, tool, arguments, settle=0.0):
            events.append((tool, arguments.get("id") or arguments.get("end")))
            if tool == "bulldoze_entity":
                return {"ok": True, "regressionOk": True}
            segment[0] += 1
            return {"ok": builds_ok, "regressionOk": True, "result": {"segmentId": segment[0]}}
        routes = [[(0.0, 0.0), (40.0, 10.0), (90.0, 40.0)], [(0.0, 0.0), (100.0, 30.0)]]
        with mock.patch.object(cargo, "_preview_rail", side_effect=preview), \
                mock.patch.object(cargo.protocol, "run", side_effect=run), \
                mock.patch.object(cargo.mt, "_request", side_effect=lambda *a, **k: {"end": {"x": 40.0, "z": 10.0}}):
            return cargo.lay_link("t", routes, built), events, built

    def test_a_way_refused_in_preview_is_never_laid_and_the_next_is(self):
        result, events, built = self.lay([{"reusedEndNode": False}, {"valid": False, "toolErrors": "SlopeTooSteep"},
                                          {"reusedEndNode": True}, {"reusedEndNode": True}])
        self.assertEqual((True, 1), (result["ok"], result["way"]))
        self.assertEqual([("build_network", {"x": 100.0, "z": 30.0})], events)
        self.assertEqual([701], built["links"])

    def test_a_knee_refused_after_it_was_laid_comes_up_before_the_next_way(self):
        result, events, built = self.lay([{"reusedEndNode": False}, {"reusedEndNode": True},  # all previewed
                                          {"reusedEndNode": False}, {"valid": False, "toolErrors": "SlopeTooSteep"},
                                          {"reusedEndNode": True}, {"reusedEndNode": True}])
        self.assertTrue(result["ok"])
        self.assertEqual(["build_network", "bulldoze_entity", "build_network"], [e[0] for e in events])
        self.assertEqual(701, events[1][1])
        self.assertEqual([702], built["links"])

    def test_a_piece_the_game_would_not_build_is_a_hard_failure_left_to_the_undo(self):
        result, events, built = self.lay([{"reusedEndNode": False}, {"reusedEndNode": True},
                                          {"reusedEndNode": False}], builds_ok=False)
        self.assertEqual((False, True), (result["ok"], result["hard"]))
        self.assertEqual([701], built["links"])

    def test_a_start_not_on_the_terminal_track_is_refused(self):
        result, events, built = self.lay([{"reusedStartNode": False}, {"reusedStartNode": False, "reusedEndNode": True}])
        self.assertFalse(result["ok"])
        self.assertEqual(["notFromTerminal", "notFromTerminal"], [item["why"] for item in result["tried"]])
        self.assertEqual([], events)

    def test_routes_start_from_the_node_actually_laid_planned_way_first(self):
        segments, _ = _world()
        site = {"center": (0.0, -64.0), "angleDegrees": 0.0, "ends": [(88.0, -40.0), (-88.0, -40.0)],
                "away": (0.0, -1.0)}
        link = {"end": 0, "points": [(88.0, -40.0), (150.0, 0.0)], "nodeId": _node_at(segments, (150.0, 0.0))}
        routes = cargo.build_routes(site, link, 99, (89.0, -41.0), segments)
        self.assertEqual([(89.0, -41.0), (150.0, 0.0)], routes[0])
        self.assertTrue(len(routes) > 1)
        self.assertTrue(all(route[0] == (89.0, -41.0) for route in routes))
        self.assertTrue(all(route[-1][1] == 0.0 and route[-1][0] > 89.0 for route in routes))  # ahead, on the railway
        self.assertLessEqual(len(routes), cargo.LINK_TRIES)
        self.assertEqual(1, len(cargo.build_routes({**site, "away": None}, link, 99, (89.0, -41.0), segments)))


class ConnectedTests(unittest.TestCase):
    def test_the_terminal_must_reach_the_outside_and_report_no_access_problem(self):
        segments, _ = _world()
        link = _segment(990, 950, 13, (0, -40), (0, 0))
        facilities = {"facilities": [{"id": 77, "problems": ""}]}

        def request(segs, problems=""):
            def answer(method, path, query=None, body=None):
                if path == "/state/networks":
                    return {"segments": segs}
                if (query or {}).get("service") != "PublicTransport":
                    return {"facilities": []}  # the unfiltered list holds city services only
                return {"facilities": [{"id": 77, "problems": problems}]}
            return answer
        with mock.patch.object(cargo.mt, "_request", side_effect=request(segments + [link])):
            self.assertTrue(cargo.connected(77, [950])["ok"])
        with mock.patch.object(cargo.mt, "_request", side_effect=request(segments)):
            self.assertFalse(cargo.connected(77, [950])["ok"])
        with mock.patch.object(cargo.mt, "_request", side_effect=request(segments + [link], "RoadNotConnected")):
            self.assertFalse(cargo.connected(77, [950])["ok"])
        self.assertTrue(facilities)


class UndoTests(unittest.TestCase):
    def test_a_tunnel_run_is_taken_down_from_its_ends_in_rounds(self):
        standing = {1, 2, 3, 4}  # a run 1-2-3-4: only a piece at an end comes down

        def run(label, tool, args, *rest):
            segment = args["id"]
            ends = {min(standing), max(standing)}
            if args["entityType"] == "netSegment" and segment in ends:
                standing.discard(segment)
                return {"ok": True}
            return {"ok": args["entityType"] == "building"}

        with mock.patch.object(cargo, "_rail_segments", side_effect=lambda: [{"id": s} for s in sorted(standing)]), \
                mock.patch.object(cargo.protocol, "run", side_effect=run):
            result = cargo.undo("t", {"links": [], "buildingId": 7}, set())
        self.assertEqual((True, []), (result["removedAll"], result["remaining"]))


class RemoteTerminalTests(unittest.TestCase):
    def test_the_industry_centre_weighs_starved_factories_around_the_densest(self):
        growables = [{"service": "Industrial", "position": {"x": x, "z": 0}, "problems": p}
                     for x, p in ((0, ""), (100, ""), (200, "NoResources"), (5000, ""))]
        self.assertEqual((140.0, 0.0), cargo_remote.industry_center(growables))
        self.assertIsNone(cargo_remote.industry_center([]))

    def test_a_surface_piece_clears_growables_but_no_service_building_or_power_line(self):
        def surface(reply):
            with mock.patch.object(cargo_remote.mt, "_request", return_value=reply):
                return cargo_remote._surface((0.0, 0.0), (40.0, 0.0), 0.0)
        factory = {"id": 4, "service": "Industrial"}
        self.assertEqual([factory], surface({"toolErrors": "ObjectCollision", "buildingsUnderRoad": [factory]}))
        self.assertEqual([], surface({"toolErrors": "None", "buildingsUnderRoad": []}))
        self.assertIsNone(surface({"toolErrors": "ObjectCollision", "buildingsUnderRoad": [
            factory, {"id": 5, "service": "Garbage"}]}))
        self.assertIsNone(surface({"toolErrors": "None", "buildingsUnderRoad": [], "powerLinesCrossed": [{"segmentId": 1}]}))
        self.assertIsNone(surface({"toolErrors": "ObjectCollision", "buildingsUnderRoad": []}))  # another network
        self.assertIsNone(surface({"toolErrors": "SlopeTooSteep", "buildingsUnderRoad": [factory]}))
        road = {"id": 9, "prefab": "Basic Road", "service": "Road", "startNodeId": 1, "endNodeId": 2,
                "start": {"x": 20.0, "z": -10.0}, "end": {"x": 20.0, "z": 10.0}}
        with mock.patch.object(cargo_remote.mt, "_request", return_value={"toolErrors": "None"}):
            self.assertIsNone(cargo_remote._surface((0.0, 0.0), (40.0, 0.0), 0.0, [road]))  # a level crossing
            # the ramp down is deep enough by the road 90% along it
            self.assertEqual([], cargo_remote._surface((0.0, 0.0), (22.2, 0.0), -12.0, [road]))

    def test_the_way_out_leaves_by_the_end_facing_the_railway_and_never_turns_back(self):
        away, toward = ((0.0, 0.0), (-1.0, 0.0)), ((100.0, 0.0), (1.0, 0.0))
        with mock.patch.object(cargo_remote, "_surface", return_value=[]):
            way, cleared = cargo_remote.way_out([away, toward], (2000.0, 0.0), (2100.0, 50.0), [])
            self.assertEqual(((100.0, 0.0), []), (way[0][0], cleared))
            self.assertIsNone(cargo_remote.way_out([away], (2000.0, 0.0), (2100.0, 50.0), []))

    def test_the_way_leads_out_ramps_down_tunnels_and_ramps_up_into_the_railway(self):
        pieces = cargo_remote.route((0.0, 0.0), (1.0, 0.0), (1000.0, 0.0), (1100.0, 80.0))
        kinds = [kind for _, _, kind in pieces]
        self.assertEqual(["lead", "down"], kinds[:2])
        self.assertEqual("up", kinds[-1])
        self.assertTrue(all(kind == "tunnel" for kind in kinds[2:-1]))
        self.assertEqual((40.0, 0.0), pieces[0][1])
        down = (pieces[1][1][0] - 40.0, pieces[1][1][1])  # turned off the lead, not straight on
        self.assertAlmostEqual(cargo_remote.MIN_BEND, abs(math.degrees(math.atan2(down[1], down[0]))), places=1)
        for (_, end, _), (start, _, _) in zip(pieces, pieces[1:]):
            self.assertEqual(end, start)
        self.assertEqual((1100.0, 80.0), pieces[-1][1])

    def test_the_approach_meets_a_plain_railway_node_at_an_angle_toward_the_terminal(self):
        segments = [{"id": i, "prefab": "Train Track", "subService": "PublicTransportTrain",
                     "startNodeId": i, "endNodeId": i + 1, "start": {"x": 1000.0, "z": 100.0 * i},
                     "end": {"x": 1000.0, "z": 100.0 * (i + 1)}} for i in range(1, 5)]
        net = {"degree": {1: 1, 2: 2, 3: 2, 4: 2, 5: 1}}
        node, at, point = cargo_remote.approach(net, segments, (0.0, 300.0))
        self.assertIn(node, (2, 3, 4))  # a plain node: the line's ends join nothing
        self.assertLess(point[0], 1000.0)  # on the terminal's side
        found = cargo_remote.approaches(net, segments, (0.0, 300.0))
        self.assertEqual(3 * 4, len(found))  # every plain node from either side and way along
        self.assertEqual(sorted(math.dist(item[2], (0.0, 300.0)) for item in found),
                         [math.dist(item[2], (0.0, 300.0)) for item in found])
        self.assertAlmostEqual(cargo_remote.RAMP, math.dist(point, at), places=1)
        along = abs(point[1] - at[1]) / cargo_remote.RAMP
        self.assertAlmostEqual(math.cos(math.radians(cargo_remote.APPROACH)), along, places=2)


class StepTests(unittest.TestCase):
    def city(self, state=None):
        saved = {}
        city = mock.Mock()
        city.load_state.return_value = state or {}
        city.save_state.side_effect = lambda name, value: saved.update({name: value})
        return city, saved

    @staticmethod
    def wake(count, cash=500000.0, unlocked=("train",)):
        return {"problemCounts": {"NoResources": count}, "cash": cash, "unlocked": list(unlocked)}

    def step(self, state, count, traffic_summary=None, plan=None, build=None, remote=None, **wake_kwargs):
        city, saved = self.city(state)
        reads = {"/state/networks": {"segments": []}, "/state/growables": {"buildings": []},
                 "/state/facilities": {"facilities": []}, "/commands/preview-bulldoze-impact": {"ok": True}}
        with mock.patch.object(cargo.mt, "_request", side_effect=lambda m, p, query=None, body=None: reads[p]), \
                mock.patch.object(cargo, "plan", return_value=plan or {"ok": False, "why": "noSite"}) as planned, \
                mock.patch.object(cargo_remote, "build", return_value=remote), \
                mock.patch.object(cargo, "build", return_value=build or {"ok": False, "stage": "link"}) as built:
            report = cargo.step(city, "t", self.wake(count, **wake_kwargs), traffic_summary or {})
        return report, saved["cargo-rail"], planned, built

    def test_nothing_happens_until_no_resources_persists(self):
        state = {"history": [9, 9]}
        report, saved, planned, _ = self.step(state, 4)
        self.assertEqual("noEvidence", report["reason"])
        self.assertFalse(planned.called)
        self.assertEqual([9, 9, 4], saved["history"])
        report, saved, planned, _ = self.step({"history": [8, 13]}, 9)
        self.assertTrue(planned.called)

    def test_a_cargo_hotspot_is_enough_evidence(self):
        traffic_summary = {"cargo": [{"key": "hotspot-1", "seen": 3, "cargoShare": 0.6, "cargoRail": True}]}
        _, _, planned, _ = self.step({}, 2, traffic_summary)
        self.assertTrue(planned.called)
        _, _, planned, _ = self.step({}, 2, {"cargo": [{"key": "h", "seen": 2, "cargoRail": True}]})
        self.assertFalse(planned.called)

    def test_train_locked_or_short_of_cash_waits(self):
        report, _, planned, _ = self.step({"history": [9, 9]}, 9, unlocked=())
        self.assertEqual("trainLocked", report["reason"])
        report, _, planned, _ = self.step({"history": [9, 9]}, 9, cash=1000.0)
        self.assertEqual("waitingForCash", report["reason"])
        self.assertFalse(planned.called)

    def test_a_built_terminal_is_recorded_once_and_judged_later(self):
        site = {"ok": True, "key": "k", "segmentId": 1, "offset": 64.0, "side": -1, "industryScore": 9,
                "center": (0.0, -64.0), "angleDegrees": 180.0, "links": [{}, {}]}
        build = {"ok": True, "buildingId": 77, "links": [951], "cleared": [501], "nodes": [901, 902]}
        traffic_summary = {"cargo": [{"key": "h", "seen": 1, "cargoShare": 0.5, "cargoRail": False}]}
        report, saved, _, _ = self.step({"history": [12, 12]}, 12, traffic_summary, plan=site, build=build)
        self.assertTrue(report["ok"])
        self.assertEqual((77, 12.0, 0.5), (saved["built"]["buildingId"], saved["built"]["baseline"],
                                           saved["built"]["truckShare"]))
        state = saved
        for count in (11, 9, 7, 6, 6, 5, 5):
            report, state, planned, built = self.step(state, count)
            self.assertFalse(planned.called or built.called)
            self.assertNotIn("judged", report)
        report, state, _, _ = self.step(state, 5)
        self.assertEqual("worked", report["judged"]["verdict"])
        report, state, _, _ = self.step(state, 5)
        self.assertNotIn("judged", report)  # judged once

    def test_failed_attempts_wait_and_are_capped(self):
        site = {"ok": True, "key": "k1", "segmentId": 1, "offset": 64.0, "side": -1, "industryScore": 9,
                "center": (0.0, -64.0), "angleDegrees": 180.0, "links": [{}]}
        report, state, _, built = self.step({"history": [9, 9]}, 9, plan=site)
        self.assertTrue(built.called)
        self.assertEqual("k1", state["attempts"][-1]["key"])
        report, state, planned, _ = self.step(state, 9)
        self.assertEqual("retryLater", report["reason"])
        self.assertFalse(planned.called)
        state["wake"] += cargo.RETRY_AFTER
        _, _, planned, _ = self.step(state, 9, plan=site)
        self.assertEqual({"k1"}, planned.call_args.args[3])  # the tried site is passed over
        spent = {**state, "attempts": [{"wake": 0, "key": f"k{i}", "counts": True} for i in range(cargo.MAX_ATTEMPTS)]}
        report, _, planned, _ = self.step(spent, 9)
        self.assertEqual("besideSpent", report["reason"])  # the tries by the industry are still to come
        self.assertFalse(planned.called)
        both = {**spent, "attempts": spent["attempts"] + [{"wake": 0, "key": "remote", "counts": True}] * 3}
        report, _, planned, _ = self.step(both, 9)
        self.assertEqual("attemptsSpent", report["reason"])

    def test_no_site_by_the_railway_tries_a_terminal_by_the_industry(self):
        remote = {"ok": True, "buildingId": 7, "links": [1, 2, 3], "railNode": 9, "pieces": 3, "length": 2400,
                  "cleared": []}
        report, state, _, built = self.step({"history": [9, 9]}, 9, remote=remote)
        self.assertFalse(built.called)
        self.assertEqual((True, "remote"), (report["ok"], report["site"]))
        self.assertTrue(state["built"]["remote"])
        self.assertEqual("remote", state["attempts"][-1]["key"])
        report, state, _, _ = self.step({"history": [9, 9]}, 9, remote={"ok": False, "stage": "site"})
        self.assertEqual([], [a for a in state["attempts"] if a.get("counts")])  # no lot found: no attempt

    def test_a_busy_game_thread_is_no_attempt(self):
        site = {"ok": True, "key": "k1", "segmentId": 1, "offset": 64.0, "side": -1, "industryScore": 9,
                "center": (0.0, -64.0), "angleDegrees": 180.0, "links": [{}]}
        _, state, _, _ = self.step({"history": [9, 9]}, 9, plan=site, build={"ok": False, "stage": "busy"})
        self.assertEqual([], state["attempts"])  # tried again the next wake

    def test_a_failed_read_while_planning_never_ends_the_wake(self):
        city, saved = self.city({"history": [9, 9]})
        error = mayor_tools.BridgeRequestError({"error": "Timed out waiting for the game thread; the command was not run."})
        with mock.patch.object(cargo.mt, "_request", side_effect=error):
            report = cargo.step(city, "t", self.wake(9), {})
        self.assertEqual((None, "busy"), (report["ok"], report["reason"]))
        self.assertEqual([], saved["cargo-rail"]["attempts"])


class WiringTests(unittest.TestCase):
    def test_a_terminal_left_half_built_stops_the_loop_but_a_clean_failure_does_not(self):
        def reasons(cargo_report):
            return [r for r in play.stop_reasons(self.report(cargo_report), {"train"}, 0.0) if "cargoRailNotUndone" in r]
        self.assertTrue(reasons({"ok": False, "stage": "link", "undo": {"removedAll": False, "remaining": [951]}}))
        self.assertFalse(reasons({"ok": False, "stage": "link", "undo": {"removedAll": True}}))
        self.assertFalse(reasons({"ok": False, "stage": "clearing"}))
        self.assertFalse(reasons({"ok": None, "reason": "noEvidence"}))

    @staticmethod
    def report(cargo_report):
        return {"wake": {"unlocked": ["train"], "cash": 1e6}, "growth": {"zoned": [], "densified": []},
                "ruins": {}, "water": {"summary": {"missingWater": 0, "missingSewage": 0}},
                "power": {"summary": {"missingElectricity": 0}}, "cargo": cargo_report}

    def test_traffic_reports_each_hotspot_truck_share_for_cargo(self):
        hotspot = {"segmentIds": [1, 2], "meanDensity": 40.0, "segmentCount": 2, "center": {"x": 0, "z": 0},
                   "diagnosis": {"cargoShare": 0.62, "externalShare": 0.1},
                   "remedies": [{"kind": "cargoRail", "reason": "trucks"}]}
        city = mock.Mock()
        city.load_state.return_value = {}
        with mock.patch.object(traffic.mt, "call_tool",
                               return_value={"status": "ok", "flowRatio": 0.8, "hotspots": [hotspot]}),                 mock.patch.object(traffic, "review_lines", return_value=[]):
            summary = traffic.watch(city, "t")
        self.assertEqual([{"key": "hotspot-1", "seen": 1, "cargoShare": 0.62, "cargoRail": True}], summary["cargo"])

    def test_a_wake_reads_the_count_of_each_problem(self):
        snapshot = {"snapshot": {"gameTime": "2053"}, "problems": {"countsByProblem": {"NoResources": 11}},
                    "progression": {"capabilities": {"train": True}}}
        with mock.patch.object(wake, "set_speed"), \
                mock.patch.object(wake.mt, "call_tool", return_value=snapshot):
            result = wake.wake(mock.Mock(), 0.0)
        self.assertEqual({"NoResources": 11}, result["problemCounts"])
        self.assertEqual(["train"], result["unlocked"])


if __name__ == "__main__":
    unittest.main()

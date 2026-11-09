import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mayor import expansion  # noqa: E402

SPACING = 1920 / 63


def cell(i, j, slope=0.0, water=False, unlocked=True):
    return {"position": {"x": expansion.MAP_EDGE + i * SPACING, "z": expansion.MAP_EDGE + j * SPACING},
            "slope": slope, "unlocked": unlocked, "water": {"present": water}}


def road(segment_id, a, b, pa, pb, prefab="Basic Road"):
    return {"id": segment_id, "prefab": prefab, "startNodeId": a, "endNodeId": b,
            "start": {"x": pa[0], "z": pa[1]}, "end": {"x": pb[0], "z": pb[1]}}


class ExpansionTests(unittest.TestCase):
    def test_free_samples_skip_steep_wet_locked_and_used_land(self):
        cells = [cell(10, 10), cell(11, 10, slope=0.3), cell(12, 10, water=True), cell(13, 10, unlocked=False),
                 cell(14, 10), cell(16, 10), cell(18, 10, slope=0.2)]  # 15 lies within 32m of the road at 14
        used = cells[4]["position"]
        segments = [road(1, 1, 2, (used["x"], used["z"] - 50), (used["x"], used["z"] + 50))]
        free = expansion.free_samples(cells, segments, [], SPACING)
        # ground a building may stand on (slope 0.2) is tried; steeper (0.3) is not
        self.assertEqual([cells[0], cells[5], cells[6]], free)

    def test_areas_join_neighbouring_samples_across_rounding(self):
        # positions on the grid fall on .5 multiples of the spacing from the origin; keys must not gap
        block = [cell(i, j) for i in range(20, 30) for j in range(40, 44)]
        lone = [cell(60, 60)]
        found = expansion.areas(block + lone, SPACING, minimum_samples=6)
        self.assertEqual(1, len(found))
        self.assertEqual(40, found[0]["samples"])
        self.assertAlmostEqual(0.0, found[0]["axisDegrees"], places=1)  # longer along x
        self.assertGreater(found[0]["length"], found[0]["width"])

    def test_access_reports_ground_barriers_but_not_elevated_ones(self):
        area = expansion.areas([cell(i, j) for i in range(40, 46) for j in range(40, 44)], SPACING)[0]
        target_x = expansion.MAP_EDGE + 40 * SPACING
        z = expansion.MAP_EDGE + 41 * SPACING
        segments = [road(1, 1, 2, (target_x - 300, z), (target_x - 200, z)),
                    road(2, 3, 4, (target_x - 100, z - 200), (target_x - 100, z + 200), prefab="Train Track"),
                    road(3, 5, 6, (target_x - 150, z - 200), (target_x - 150, z + 200), prefab="Train Track Elevated")]
        ways = expansion.access(area, segments, {}, SPACING)
        self.assertEqual(2, ways[0]["nodeId"])
        self.assertEqual(["Train Track"], ways[0]["crosses"])
        self.assertFalse(ways[0]["clear"])

    def test_a_node_that_ends_a_highway_is_not_a_way_in(self):
        area = expansion.areas([cell(i, j) for i in range(40, 46) for j in range(40, 44)], SPACING)[0]
        x, z = expansion.MAP_EDGE + 40 * SPACING, expansion.MAP_EDGE + 41 * SPACING
        segments = [road(1, 1, 2, (x - 60, z), (x - 300, z), prefab="Highway"),   # the highway's stub ends at 1
                    road(2, 1, 3, (x - 60, z), (x - 120, z + 40)),               # the town's first street
                    road(3, 3, 4, (x - 120, z + 40), (x - 200, z + 60))]
        ways = expansion.access(area, segments, {}, SPACING)
        self.assertNotIn(1, [way["nodeId"] for way in ways])
        self.assertEqual(3, ways[0]["nodeId"])

    def test_fishbone_runs_into_the_area(self):
        area = expansion.areas([cell(i, j) for i in range(40, 60) for j in range(40, 44)], SPACING)[0]
        entry = {"nodeId": 9, "to": {"x": expansion.MAP_EDGE + 40 * SPACING, "z": expansion.MAP_EDGE + 41 * SPACING}}
        layout = expansion.fishbone(area, entry)
        self.assertAlmostEqual(90.0, layout["collectorHeadingDegrees"], places=1)  # toward +x
        self.assertEqual(6, layout["collectorSegments"])  # about 580m of area ahead in 88m steps
        self.assertEqual(53.0, layout["branchLength"])  # half the 122m width less 8m


class AccessInTheWayTests(unittest.TestCase):
    def setUp(self):
        self.area = expansion.areas([cell(i, j) for i in range(40, 46) for j in range(40, 44)], SPACING)[0]
        self.x, self.z = expansion.MAP_EDGE + 40 * SPACING, expansion.MAP_EDGE + 41 * SPACING

    def test_a_way_over_a_home_gives_way_to_a_farther_one_over_nothing(self):
        x, z = self.x, self.z
        # node 2 ends a street 50m short of the land with a house between; node 4 ends one 120m off
        segments = [road(1, 1, 2, (x - 150, z), (x - 50, z)),
                    road(2, 3, 4, (x - 120, z + 300), (x - 120, z + 200))]
        home = {"id": 9, "position": {"x": x - 25, "z": z}, "width": 3, "length": 3, "angleDegrees": 0}
        ways = expansion.access(self.area, segments, {}, SPACING, candidates=1, buildings=[home])
        self.assertEqual([(4, 0)], [(way["nodeId"], way["blocked"]) for way in ways])
        nearest = expansion.access(self.area, segments, {}, SPACING, candidates=1)
        self.assertEqual([(2, 0)], [(way["nodeId"], way["blocked"]) for way in nearest])  # nothing in the way

    def test_a_house_beside_the_way_or_a_power_line_across_it_count(self):
        x, z = self.x, self.z
        beside = {"id": 9, "position": {"x": x - 25, "z": z + 30}, "width": 2, "length": 2, "angleDegrees": 0}
        self.assertEqual(0, expansion.in_the_way((x - 50, z), (x, z), [beside], []))
        turned = {"id": 9, "position": {"x": x - 25, "z": z + 22}, "width": 1, "length": 4, "angleDegrees": 0}
        self.assertEqual(1, expansion.in_the_way((x - 50, z), (x, z), [turned], []))  # its 32m length reaches
        line = road(7, 8, 9, (x - 25, z - 100), (x - 25, z + 100), prefab="Power Line")
        self.assertEqual(1, expansion.in_the_way((x - 50, z), (x, z), [], [line]))
        pylon = road(7, 8, 9, (x - 25, z - 5), (x - 25, z - 200), prefab="Power Line")
        self.assertEqual(1, expansion.in_the_way((x - 50, z), (x, z), [], [pylon]))
        # alongside, 11.7m off: the preview refused it (live), 12m being a road's half width and 4m
        alongside = road(7, 8, 9, (x - 200, z - 11.7), (x + 200, z - 11.7), prefab="Power Line")
        self.assertEqual(1, expansion.in_the_way((x - 50, z), (x, z), [], [alongside]))
        clear = road(7, 8, 9, (x - 200, z - 13), (x + 200, z - 13), prefab="Power Line")
        self.assertEqual(0, expansion.in_the_way((x - 50, z), (x, z), [], [clear]))

    def test_with_nothing_in_the_way_the_nearest_ways_are_kept(self):
        x, z = self.x, self.z
        segments = [road(1, 1, 2, (x - 150, z), (x - 50, z)), road(2, 3, 4, (x - 160, z + 10), (x - 60, z + 10)),
                    road(3, 5, 6, (x - 170, z + 20), (x - 70, z + 20))]
        ways = expansion.access(self.area, segments, {}, SPACING, candidates=2)
        self.assertEqual([2, 4], [way["nodeId"] for way in ways])


if __name__ == "__main__":
    unittest.main()

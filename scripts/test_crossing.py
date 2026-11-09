import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mayor import crossing  # noqa: E402

RAIL = {"id": 5, "prefab": "Train Track", "start": {"x": 0, "z": -500}, "end": {"x": 0, "z": 500}}


def point(item):
    return item["x"], item["z"]


class CrossingTests(unittest.TestCase):
    def test_crossed_lists_barriers_on_the_straight_way(self):
        other = {"id": 6, "prefab": "Basic Road", "start": {"x": 300, "z": -10}, "end": {"x": 300, "z": 10}}
        self.assertEqual([5], [s["id"] for s in crossing.crossed((-100, 0), (200, 0), [RAIL, other])])

    def test_overpass_climbs_crosses_level_and_lands_on_the_far_side(self):
        candidates = crossing.overpasses((-100, 0), (300, 0), RAIL)
        self.assertTrue(candidates)
        up, over, down = candidates[0]
        self.assertEqual((0.0, crossing.CLEARANCE), (up["startHeightOffset"], up["endHeightOffset"]))
        self.assertEqual((crossing.CLEARANCE, crossing.CLEARANCE), (over["startHeightOffset"], over["endHeightOffset"]))
        self.assertEqual(0.0, down["endHeightOffset"])
        self.assertLess(point(up["end"])[0], 0)  # the ramp up stays on the near side
        self.assertGreater(point(down["end"])[0], 0)  # the ramp down lands beyond the rail
        for piece in candidates[0]:
            length = math.dist(point(piece["start"]), point(piece["end"]))
            self.assertLessEqual(length, crossing.MAX_PIECE)
            rise = abs(piece["endHeightOffset"] - piece["startHeightOffset"])
            self.assertLessEqual(rise / length, crossing.MAX_GRADE + 1e-9)

    def test_a_start_next_to_the_barrier_ramps_up_alongside_it(self):
        candidates = crossing.overpasses((-20, 0), (300, 0), RAIL)
        up = candidates[0][0]
        self.assertGreater(abs(point(up["end"])[1]), 40)  # it climbs along the rail first
        self.assertLess(point(up["end"])[0], -11)

    def test_a_railway_beside_the_highway_is_crossed_in_the_same_span(self):
        highway = {"id": 7, "prefab": "Highway", "start": {"x": 30, "z": -500}, "end": {"x": 30, "z": 500}}
        road = {"id": 8, "prefab": "Basic Road", "start": {"x": 200, "z": -500}, "end": {"x": 200, "z": 500}}
        group = crossing.barrier_group((-100, 0), (300, 0), [road, highway, RAIL])
        self.assertEqual([5, 7], [segment["id"] for segment in group])  # the road 200m on is not beside
        candidates = crossing.overpasses((-100, 0), (300, 0), RAIL, group[1:])
        self.assertTrue(candidates)
        for up, over, down in candidates:
            self.assertLessEqual(point(up["end"])[0], -12)
            self.assertGreaterEqual(point(over["end"])[0], 30 + crossing.PAST_BARRIER - 0.5)  # level past both
            self.assertGreater(point(down["end"])[0], 30)
            self.assertLessEqual(math.dist(point(over["start"]), point(over["end"])), crossing.MAX_PIECE + 1e-6)
        # crossing the railway alone, the ramp came down onto the highway beside it
        alone = crossing.overpasses((-100, 0), (300, 0), RAIL)
        self.assertTrue(any(point(over["end"])[0] < 30 for _, over, _ in alone))

    def test_barriers_too_far_apart_for_one_level_piece_give_no_overpass(self):
        far = {"id": 9, "prefab": "Highway", "start": {"x": 79, "z": -500}, "end": {"x": 79, "z": 500}}
        self.assertEqual([], crossing.overpasses((-100, 0), (300, 0), RAIL, [far]))


if __name__ == "__main__":
    unittest.main()

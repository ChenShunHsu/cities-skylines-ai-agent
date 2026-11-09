import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mayor import bridge  # noqa: E402


def river(north: float, south: float):
    """Water between two lines of constant z."""
    return lambda point: south <= point[1] <= north


class CrossingTests(unittest.TestCase):
    def test_a_line_finds_the_water_and_the_land_beyond(self):
        found = bridge.line((0.0, 0.0), (0.0, 1.0), river(500.0, 200.0), 2000.0)
        self.assertEqual((200.0, 304.0), (found["approach"], found["span"]))

    def test_no_crossing_without_land_after_the_water(self):
        self.assertIsNone(bridge.line((0.0, 0.0), (0.0, 1.0), river(5000.0, 200.0), 2000.0))

    def test_the_narrowest_way_over_from_the_nearest_end_comes_first(self):
        found = bridge.crossings([(0.0, -400.0), (0.0, 0.0)], (0.0, 900.0), river(500.0, 200.0))
        self.assertEqual((0.0, 0.0), found[0]["start"])
        self.assertEqual((0.0, 1.0), tuple(round(v, 6) for v in found[0]["heading"]))  # straight across

    def test_a_crossing_is_ground_to_the_bank_then_a_bridge_landing_beyond(self):
        crossing = {"start": (0.0, 0.0), "heading": (0.0, 1.0), "approach": 200.0, "span": 304.0}
        laid = bridge.pieces(crossing)
        self.assertEqual(2, len(laid["ground"]))  # 160m to the setback, two pieces under 90m
        self.assertEqual(160.0, laid["ground"][-1]["end"]["z"])
        deck = laid["bridge"]
        self.assertTrue(all(abs(p["end"]["z"] - p["start"]["z"]) <= bridge.PIECE for p in deck))
        self.assertEqual((0.0, 0.0), (deck[0]["startHeightOffset"], deck[-1]["endHeightOffset"]))
        self.assertTrue(all(p["endHeightOffset"] == bridge.DECK for p in deck[:-1]))
        self.assertEqual(544.0, laid["landing"]["z"])  # 40m beyond the far bank
        self.assertEqual([False] + [True] * (len(deck) - 2) + [False], [p["overWater"] for p in deck])


if __name__ == "__main__":
    unittest.main()

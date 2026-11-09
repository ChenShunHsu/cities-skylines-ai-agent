import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mayor import layouts  # noqa: E402


def area(width, length, slope=0.0, spacing=32.0):
    """A rectangle of free samples from (0, -width/2) along +x."""
    points = [(x * spacing, z * spacing - width / 2) for x in range(int(length // spacing) + 1)
              for z in range(int(width // spacing) + 1)]
    return {"points": points, "length": length, "width": width, "meanSlope": slope,
            "areaSquareMeters": width * length, "center": {"x": length / 2, "z": 0.0}}


def turn(a, b, c):
    first = math.atan2(b[1] - a[1], b[0] - a[0])
    second = math.atan2(c[1] - b[1], c[0] - b[0])
    return abs((math.degrees(second - first) + 180) % 360 - 180)


class CandidateTests(unittest.TestCase):
    def best(self, *args, **kwargs):
        return layouts.candidates(*args, **kwargs)[0]["layout"]

    def test_the_shape_slope_and_use_of_the_land_choose_the_layout(self):
        self.assertEqual("corridor", self.best(area(160, 900), "residential"))

    def test_a_bridgehead_is_a_small_closed_grid_where_the_bridge_lands(self):
        built = layouts.graph("bridgehead", area(800, 800), (0.0, 0.0), 0.0)
        self.assertEqual(9, len(built["nodes"]))  # three streets deep, three wide
        self.assertEqual(12, len(built["edges"]))  # every block closed
        self.assertEqual("0,0", built["entry"])
        self.assertNotEqual("corridor", self.best(area(586, 2280), "residential"))  # long, but no strip
        self.assertEqual("grid", self.best(area(600, 800), "residential"))
        self.assertEqual("ring", self.best(area(500, 700, slope=0.07), "residential"))
        self.assertEqual("industrial", self.best(area(600, 800), "workplace"))
        self.assertEqual("fishbone", self.best(area(300, 500), "residential"))

    def test_the_city_plan_names_the_layout(self):
        ranked = layouts.candidates(area(600, 800), "residential", plan_layout="ring")
        self.assertEqual(("ring", "the city plan names this layout"), (ranked[0]["layout"], ranked[0]["reason"]))
        self.assertEqual(len(layouts.LAYOUTS), len(ranked))


class GraphTests(unittest.TestCase):
    def check(self, layout, land):
        result = layouts.graph(layout, land, (0.0, 0.0), 0.0)
        nodes = {name: tuple(point) for name, point in result["nodes"].items()}
        reached = {result["entry"]}
        neighbours: dict = {name: [] for name in nodes}
        for a, b in result["edges"]:
            self.assertIn(a, reached)  # every edge leaves a node built before it
            reached.add(b)
            self.assertLessEqual(math.dist(nodes[a], nodes[b]), 170.0)
            neighbours[a].append(b)
            neighbours[b].append(a)
        self.assertEqual(set(nodes), reached)
        for name, others in neighbours.items():
            if len(others) == 2:  # a joint between two roads must bend
                self.assertGreater(turn(nodes[others[0]], nodes[name], nodes[others[1]]), 5.0, (layout, name))
        return result

    def test_every_layout_builds_outward_from_the_entry_with_bent_joints(self):
        for layout in layouts.LAYOUTS:
            with self.subTest(layout=layout):
                result = self.check(layout, area(500, 700))
                self.assertGreater(len(result["edges"]), 3)

    def test_a_grid_closes_blocks_into_loops(self):
        result = self.check("grid", area(500, 700))
        self.assertGreater(len(result["edges"]), len(result["nodes"]) - 1)  # more edges than a tree

    def test_nodes_stay_on_the_free_land(self):
        land = area(160, 900)
        result = self.check("grid", land)
        for point in result["nodes"].values():
            self.assertTrue(any(math.dist(point, sample) <= layouts.INSIDE + 1 for sample in land["points"]))


if __name__ == "__main__":
    unittest.main()

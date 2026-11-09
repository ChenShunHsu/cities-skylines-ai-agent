import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mayor import bypass  # noqa: E402


def road(segment_id, a, b, pa, pb, prefab="Basic Road"):
    return {"id": segment_id, "prefab": prefab, "startNodeId": a, "endNodeId": b,
            "start": {"x": pa[0], "z": pa[1]}, "end": {"x": pb[0], "z": pb[1]}}


# a west street and an east street joined only by the hotspot (segment 5) along z=500
ROADS = [
    road(1, 1, 2, (0, 0), (0, 250)), road(2, 2, 3, (0, 250), (0, 500)),
    road(3, 4, 5, (200, 0), (200, 250)), road(4, 5, 6, (200, 250), (200, 500)),
    road(5, 3, 6, (0, 500), (200, 500)),
    road(6, 7, 8, (0, -500), (200, -500), prefab="Highway"),
]


class BypassTests(unittest.TestCase):
    def test_graph_drops_removed_segments_and_highways(self):
        graph, positions = bypass.local_graph(ROADS, {5})
        self.assertNotIn(7, graph)
        self.assertEqual([(2, 250.0)], graph[1])
        self.assertFalse(any(neighbour == 6 for neighbour, _ in graph[3]))

    def test_connector_opens_a_route_the_hotspot_blocked(self):
        graph, positions = bypass.local_graph(ROADS, {5})
        result = bypass.connectors(graph, positions, (0, 0), (200, 0), max_length=250, limit=3)
        self.assertIsNone(result["existingRoute"])
        best = result["candidates"][0]
        self.assertEqual((1, 4), (best["fromNodeId"], best["toNodeId"]))
        self.assertEqual(1.0, best["detour"])
        # each end node is used once, so the next candidate joins other nodes
        self.assertTrue(all({item["fromNodeId"], item["toNodeId"]}.isdisjoint({1, 4})
                            for item in result["candidates"][1:]))

    def test_no_connector_when_a_route_around_already_exists(self):
        graph, positions = bypass.local_graph(ROADS + [road(7, 1, 4, (0, 0), (200, 0))], {5})
        result = bypass.connectors(graph, positions, (0, 0), (200, 0))
        self.assertEqual(200.0, result["existingRoute"])
        self.assertEqual([], result["candidates"])

    def test_pieces_stay_under_100m_with_bent_joints(self):
        pieces = bypass.pieces({"x": 0, "z": 0}, {"x": 250, "z": 0})
        self.assertEqual(3, len(pieces))
        self.assertEqual({"x": 250, "z": 0}, pieces[-1]["end"])
        lengths = [math.dist((p["start"]["x"], p["start"]["z"]), (p["end"]["x"], p["end"]["z"])) for p in pieces]
        self.assertTrue(all(length < 100 for length in lengths))
        self.assertGreater(pieces[0]["end"]["z"], 0)
        self.assertLess(pieces[1]["end"]["z"], 0)


if __name__ == "__main__":
    unittest.main()

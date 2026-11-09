import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mayor import network  # noqa: E402


def segment(segment_id, start, end, service="Road", start_outside=False, end_outside=False):
    return {"id": segment_id, "service": service, "startNodeId": start, "endNodeId": end,
            "startOutside": start_outside, "endOutside": end_outside}


class NetworkTests(unittest.TestCase):
    def test_roads_joined_to_the_highway_reach_the_outside_and_a_lone_road_does_not(self):
        segments = [segment(1, 10, 11, start_outside=True),   # highway from the map edge
                    segment(2, 11, 12),                       # the town's first street
                    segment(3, 12, 13),
                    segment(4, 20, 21),                       # a road built on its own
                    segment(5, 13, 30, service="Water"),      # a pipe joins nothing
                    segment(6, 30, 20)]
        reached, isolated = network.road_components(segments)
        self.assertEqual({1, 2, 3}, reached)
        self.assertEqual({4, 6}, isolated)

    def test_without_an_outside_connection_nothing_is_reached(self):
        reached, isolated = network.road_components([segment(1, 1, 2), segment(2, 2, 3)])
        self.assertEqual((set(), {1, 2}), (reached, isolated))


if __name__ == "__main__":
    unittest.main()

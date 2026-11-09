#!/usr/bin/env python3

import sys
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import mayor_tools
from mayor import transit


def road(segment_id, a, b, pa, pb, prefab="Basic Road"):
    return {"id": segment_id, "prefab": prefab, "startNodeId": a, "endNodeId": b,
            "start": {"x": pa[0], "z": pa[1]}, "end": {"x": pb[0], "z": pb[1]}}


# a straight local street 0..800 along x, plus a highway shortcut that buses must not use
ROADS = [road(i, i, i + 1, (i * 200.0, 0.0), ((i + 1) * 200.0, 0.0)) for i in range(4)] + [
    road(9, 0, 4, (0.0, 0.0), (800.0, 0.0), prefab="Highway")]


class TransitPlannerTests(unittest.TestCase):
    def test_route_avoids_highways_and_spaces_stops_for_walking(self):
        graph = transit.road_graph(ROADS)
        positions = transit.node_positions(ROADS)
        path = transit.shortest_path(graph, 0, 4)
        self.assertEqual([0, 1, 2, 3, 4], path)
        self.assertEqual(800.0, transit.path_length(path, positions))
        stops = transit.place_stops(path, positions, 350)
        self.assertEqual([0.0, 400.0, 800.0], [stop["x"] for stop in stops])

    def test_corridors_merge_directions_and_drop_walkable_trips(self):
        pairs = transit.corridor_pairs([
            {"originDestinationPairs": [
                {"origin": {"x": 0, "z": 0}, "destination": {"x": 1000, "z": 0}, "vehicles": 5},
                {"origin": {"x": 1000, "z": 0}, "destination": {"x": 0, "z": 0}, "vehicles": 3},
                {"origin": {"x": 0, "z": 0}, "destination": {"x": 256, "z": 0}, "vehicles": 9},
            ]}], 900)
        self.assertEqual([8], [pair["carTrips"] for pair in pairs])
        # the city owns only the centre tile: a trip to x=2000 leaves it
        outside = transit.corridor_pairs([{"originDestinationPairs": [
            {"origin": {"x": 0, "z": 0}, "destination": {"x": 2000, "z": 0}, "vehicles": 5}]}], 900, {(2, 2)})
        self.assertEqual([], outside)

    def test_tool_reads_car_paths_only_and_previews_each_proposal(self):
        calls = []

        def request(method, path, query=None, body=None):
            calls.append((path, query, body))
            if path == "/state/segment-routes":
                if query.get("vehicleAI") == "PassengerCarAI":
                    return {"vehiclesPlanningToUse": 60, "originDestinationPairs": [
                        {"origin": {"x": 0, "z": 0}, "destination": {"x": 800, "z": 0}, "vehicles": 12}]}
                return {"vehiclesPlanningToUse": 100}
            if path == "/state/networks":
                return {"segments": ROADS}
            if path == "/state/map-areas":
                return {"areas": [{"x": 2, "z": 2, "unlocked": True}]}
            if path == "/commands/create-transport-line":
                return {"ok": True, "valid": True}
            if path == "/state/transport-lines":
                return {"lines": [{"id": 69, "name": "Spine", "transportType": "Bus", "vehicles": 16, "stops": 10,
                                   "weeklyPassengers": {"total": 181}}]}
            raise AssertionError(path)

        assessment = {"flowRatio": 0.8, "hotspots": [{"busiestSegmentId": 2}]}
        with mock.patch.object(mayor_tools, "_request", side_effect=request), \
                mock.patch.object(mayor_tools, "_assess_traffic_congestion", return_value=assessment):
            result = mayor_tools.call_tool("propose_transit_lines", {"minimumTripMeters": 500})
        self.assertEqual(0.6, result["hotspotCarShares"][0]["passengerCarShare"])
        proposal = result["proposals"][0]
        self.assertEqual((12, 800.0, 3), (proposal["carTrips"], proposal["routeLength"], len(proposal["stops"])))
        preview = next(body for path, _, body in calls if path == "/commands/create-transport-line")
        self.assertTrue(preview["dryRun"] and preview["completeLoop"])
        self.assertEqual(181, result["existingLines"][0]["weeklyPassengers"]["total"])


if __name__ == "__main__":
    unittest.main()

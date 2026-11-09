"""Transit corridors from congested car trips, as pure functions.

Vehicles plan the fastest route at trip start, so a car trip moves to transit only
when the line is faster door to door. The candidates worth a line are the car
origin-destination pairs that load a congested road and are too long to walk.
A line follows the road graph between the pair's cells with stops spaced for
walking, and returns along the same roads (one-way loops force riders to drive).
"""

from __future__ import annotations

import heapq
import math
from collections import defaultdict
from typing import Any, Iterable

# buses cannot stop on these, and a stop beside a highway is unreachable on foot
NO_STOP_PREFABS = ("Highway", "Ramp")


def road_graph(segments: Iterable[dict[str, Any]]) -> dict[int, list[tuple[int, float]]]:
    graph: dict[int, list[tuple[int, float]]] = defaultdict(list)
    for segment in segments:
        prefab = str(segment.get("prefab") or "")
        if any(part in prefab for part in NO_STOP_PREFABS):
            continue
        start, end = int(segment["startNodeId"]), int(segment["endNodeId"])
        length = math.dist((segment["start"]["x"], segment["start"]["z"]), (segment["end"]["x"], segment["end"]["z"]))
        graph[start].append((end, length))
        graph[end].append((start, length))
    return graph


def node_positions(segments: Iterable[dict[str, Any]]) -> dict[int, tuple[float, float]]:
    positions: dict[int, tuple[float, float]] = {}
    for segment in segments:
        positions[int(segment["startNodeId"])] = (segment["start"]["x"], segment["start"]["z"])
        positions[int(segment["endNodeId"])] = (segment["end"]["x"], segment["end"]["z"])
    return positions


def nearest_node(graph: dict[int, list[tuple[int, float]]], positions: dict[int, tuple[float, float]],
                 point: tuple[float, float]) -> int | None:
    candidates = [node for node in graph if node in positions]
    return min(candidates, key=lambda node: math.dist(positions[node], point), default=None)


def shortest_path(graph: dict[int, list[tuple[int, float]]], start: int, goal: int) -> list[int]:
    distances = {start: 0.0}
    previous: dict[int, int] = {}
    queue = [(0.0, start)]
    while queue:
        distance, node = heapq.heappop(queue)
        if node == goal:
            break
        if distance > distances.get(node, math.inf):
            continue
        for neighbour, length in graph.get(node, []):
            candidate = distance + length
            if candidate < distances.get(neighbour, math.inf):
                distances[neighbour] = candidate
                previous[neighbour] = node
                heapq.heappush(queue, (candidate, neighbour))
    if goal not in distances:
        return []
    path = [goal]
    while path[-1] != start:
        path.append(previous[path[-1]])
    return path[::-1]


def place_stops(path: list[int], positions: dict[int, tuple[float, float]], spacing: float) -> list[dict[str, float]]:
    """A stop at the first node, then at the first node at least spacing along the path
    from the previous stop, and at the last node; stops sit on nodes, so each is on a road."""
    if not path:
        return []
    stops = [positions[path[0]]]
    travelled = 0.0
    for previous, node in zip(path, path[1:]):
        travelled += math.dist(positions[previous], positions[node])
        if travelled >= spacing:
            stops.append(positions[node])
            travelled = 0.0
    if math.dist(stops[-1], positions[path[-1]]) >= spacing / 2:
        stops.append(positions[path[-1]])
    return [{"x": round(x, 2), "z": round(z, 2)} for x, z in stops]


def path_length(path: list[int], positions: dict[int, tuple[float, float]]) -> float:
    return sum(math.dist(positions[a], positions[b]) for a, b in zip(path, path[1:]))


TILE_SIZE = 1920.0
MAP_HALF = 4800.0


def tile_of(x: float, z: float) -> tuple[int, int]:
    return int((x + MAP_HALF) // TILE_SIZE), int((z + MAP_HALF) // TILE_SIZE)


def corridor_pairs(route_reports: Iterable[dict[str, Any]], minimum_trip: float,
                   owned_tiles: set[tuple[int, int]] | None = None) -> list[dict[str, Any]]:
    """Car origin-destination cell pairs across the reports, merged in both directions
    (a line serves both ways), longest-walk trips only, busiest first. With owned_tiles,
    a pair with an end outside the city (trips to an outside connection) is dropped:
    a city line cannot run there."""
    counts: dict[tuple[tuple[float, float], tuple[float, float]], int] = defaultdict(int)
    for report in route_reports:
        for pair in report.get("originDestinationPairs", []):
            origin = (pair["origin"]["x"], pair["origin"]["z"])
            destination = (pair["destination"]["x"], pair["destination"]["z"])
            if math.dist(origin, destination) < minimum_trip:
                continue
            if owned_tiles is not None and (tile_of(*origin) not in owned_tiles or
                                            tile_of(*destination) not in owned_tiles):
                continue
            key = tuple(sorted((origin, destination)))
            counts[key] += int(pair.get("vehicles", 0))
    return [{"a": {"x": a[0], "z": a[1]}, "b": {"x": b[0], "z": b[1]}, "carTrips": trips,
             "distance": round(math.dist(a, b), 1)}
            for (a, b), trips in sorted(counts.items(), key=lambda item: -item[1])]

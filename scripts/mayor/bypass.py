"""New connector roads that give a hotspot's traffic a way around it, as pure functions.

Vehicles take the fastest route at trip start, so a queue persists while no reasonable route
avoids it. With the hotspot's segments (and its saturated exit) removed from the local road
graph, a demand from where traffic enters to where it is going either still has a route or it
does not; a connector between an existing node reachable from the entry and one that reaches the
destination makes one. Candidates are ranked by the detour they leave against the straight-line
trip, then by length; each still needs the native road preview.
"""

from __future__ import annotations

import heapq
import math
from collections import defaultdict
from typing import Any, Iterable

LOCAL_EXCLUDED = ("Highway", "Ramp", "Pipe", "Power Line", "Train", "Metro", "Tram", "Pedestrian")


def local_graph(segments: Iterable[dict[str, Any]], removed: set[int]) -> tuple[dict[int, list[tuple[int, float]]],
                                                                                dict[int, tuple[float, float]]]:
    graph: dict[int, list[tuple[int, float]]] = defaultdict(list)
    positions: dict[int, tuple[float, float]] = {}
    for segment in segments:
        prefab = str(segment.get("prefab") or "")
        if int(segment["id"]) in removed or any(part in prefab for part in LOCAL_EXCLUDED):
            continue
        a, b = int(segment["startNodeId"]), int(segment["endNodeId"])
        pa = (float(segment["start"]["x"]), float(segment["start"]["z"]))
        pb = (float(segment["end"]["x"]), float(segment["end"]["z"]))
        positions[a], positions[b] = pa, pb
        length = math.dist(pa, pb)
        graph[a].append((b, length))
        graph[b].append((a, length))
    return graph, positions


def distances(graph: dict[int, list[tuple[int, float]]], source: int, limit: float) -> dict[int, float]:
    best = {source: 0.0}
    queue = [(0.0, source)]
    while queue:
        distance, node = heapq.heappop(queue)
        if distance > best.get(node, math.inf) or distance > limit:
            continue
        for neighbour, length in graph.get(node, []):
            candidate = distance + length
            if candidate < best.get(neighbour, math.inf):
                best[neighbour] = candidate
                heapq.heappush(queue, (candidate, neighbour))
    return best


def nearest(positions: dict[int, tuple[float, float]], graph: dict[int, Any], point: tuple[float, float]) -> int | None:
    return min((node for node in graph if node in positions), key=lambda node: math.dist(positions[node], point),
               default=None)


def connectors(graph: dict[int, list[tuple[int, float]]], positions: dict[int, tuple[float, float]],
               source: tuple[float, float], target: tuple[float, float], *, min_length: float = 30.0,
               max_length: float = 300.0, max_detour: float = 1.6, limit: int = 5) -> dict[str, Any]:
    """Connectors that open a route source -> target avoiding the removed segments.

    Each candidate joins node u (reachable from the source) to node v (which reaches the target)
    with one straight road; the route via it must stay within max_detour of the straight line and
    be shorter than any route that already avoids the hotspot."""
    start, goal = nearest(positions, graph, source), nearest(positions, graph, target)
    if start is None or goal is None:
        return {"existingRoute": None, "candidates": []}
    straight = max(math.dist(positions[start], positions[goal]), 1.0)
    bound = straight * max_detour
    forward, backward = distances(graph, start, bound), distances(graph, goal, bound)
    existing = forward.get(goal)
    buckets: dict[tuple[int, int], list[int]] = defaultdict(list)
    for node in backward:
        x, z = positions[node]
        buckets[(int(x // max_length), int(z // max_length))].append(node)
    found = []
    for u, du in forward.items():
        ux, uz = positions[u]
        cell = (int(ux // max_length), int(uz // max_length))
        for dx in (-1, 0, 1):
            for dz in (-1, 0, 1):
                for v in buckets.get((cell[0] + dx, cell[1] + dz), []):
                    if v == u or any(n == v for n, _ in graph[u]):
                        continue
                    length = math.dist(positions[u], positions[v])
                    if not min_length <= length <= max_length:
                        continue
                    route = du + length + backward[v]
                    if route > bound or (existing is not None and route >= existing):
                        continue
                    found.append({"fromNodeId": u, "toNodeId": v, "length": round(length, 1),
                                  "routeLength": round(route, 1), "detour": round(route / straight, 3),
                                  "from": {"x": round(ux, 2), "z": round(uz, 2)},
                                  "to": {"x": round(positions[v][0], 2), "z": round(positions[v][1], 2)}})
    found.sort(key=lambda item: (item["detour"], item["length"]))
    distinct, used = [], set()
    for item in found:
        if item["fromNodeId"] in used or item["toNodeId"] in used:
            continue  # one candidate per end node keeps the list varied
        used.update((item["fromNodeId"], item["toNodeId"]))
        distinct.append(item)
        if len(distinct) >= limit:
            break
    return {"existingRoute": None if existing is None else round(existing, 1),
            "straightLine": round(straight, 1), "candidates": distinct}


PIECE = 96.0  # the native road tool builds pieces under 100m
BEND_DEGREES = 3.0  # consecutive collinear pieces merge their middle node; bend each joint slightly


def pieces(start: dict[str, float], end: dict[str, float], piece: float = PIECE) -> list[dict[str, dict[str, float]]]:
    """A straight connector cut into pieces under 100m whose joints alternate a small sideways
    offset, so no two consecutive pieces are collinear."""
    sx, sz, ex, ez = start["x"], start["z"], end["x"], end["z"]
    length = math.dist((sx, sz), (ex, ez))
    count = max(1, math.ceil(length / piece))
    ux, uz = (ex - sx) / length, (ez - sz) / length
    offset = math.tan(math.radians(BEND_DEGREES)) * length / count / 2
    points = [(sx, sz)]
    for index in range(1, count):
        side = offset if index % 2 else -offset
        points.append((sx + ux * length * index / count - uz * side, sz + uz * length * index / count + ux * side))
    points.append((ex, ez))
    return [{"start": {"x": round(a[0], 2), "z": round(a[1], 2)}, "end": {"x": round(b[0], 2), "z": round(b[1], 2)}}
            for a, b in zip(points, points[1:])]

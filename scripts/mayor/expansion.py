"""Undeveloped land a city can grow into, and how a road reaches it, as pure functions.

Zoning needs road frontage, so when no unzoned block is left the city grows by building roads
into free land. Free land is an unlocked, dry sample no steeper than the game lets a building
stand (MAX_SLOPE) and clear of every network and
building. Connected free samples form an area. An area is reached from the nearest local road
node; the straight access is checked for what it would have to cross (a ground-level railway or
highway needs a grade separation, water needs a bridge) and for what stands in its way (buildings
and power lines a street may not run over). A fishbone layout follows the area's
long axis: a collector along it with dead-end branches on both sides.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Iterable

# trial: the game's own limit for a building's ground, in place of 0.08 kept
# since zoning on steeper ground failed once roads settled; whether roads hold there is judged live
MAX_SLOPE = 0.25
CLEARANCE = 32.0  # from network centrelines and building centres: a free sample is not already used
BARRIER_PARTS = ("Train Track", "Highway", "Metro Track", "Monorail")
IGNORED_PARTS = ("Pipe", "Path", "Power Line", "Bus Line", "Metro Line", "Train Line")
MAP_EDGE = -4800.0  # sample grids start at the map edge, so indices from it are whole numbers
ACCESS_SCAN = 40  # road nodes, nearest first, looked through for ways in that run over nothing
BUILDING_MARGIN = 6.0  # a road's half width past a lot (live: previews refused lots 0-6m from the line)
LINE_MARGIN = 12.0  # a Basic Road's half width and 4m: the Bridge's preview refuses a power line nearer


def grid_key(point: tuple[float, float], spacing: float) -> tuple[int, int]:
    return round((point[0] - MAP_EDGE) / spacing), round((point[1] - MAP_EDGE) / spacing)


def _segment_points(segment: dict[str, Any], spacing: float = 12.0) -> list[tuple[float, float]]:
    a, b = segment["start"], segment["end"]
    count = max(1, int(math.dist((a["x"], a["z"]), (b["x"], b["z"])) / spacing))
    return [(a["x"] + (b["x"] - a["x"]) * i / count, a["z"] + (b["z"] - a["z"]) * i / count) for i in range(count + 1)]


def free_samples(cells: Iterable[dict[str, Any]], segments: Iterable[dict[str, Any]],
                 buildings: Iterable[dict[str, Any]], spacing: float) -> list[dict[str, Any]]:
    """Map-analysis samples that are unlocked, dry, flat and not near any network or building."""
    used: dict[tuple[int, int], list[tuple[float, float]]] = defaultdict(list)

    def mark(point: tuple[float, float]) -> None:
        used[(int(point[0] // spacing), int(point[1] // spacing))].append(point)

    for segment in segments:
        if any(part in str(segment.get("prefab")) for part in IGNORED_PARTS):
            continue
        for point in _segment_points(segment):
            mark(point)
    for building in buildings:
        mark((building["position"]["x"], building["position"]["z"]))
    free = []
    for cell in cells:
        if not cell.get("unlocked") or (cell.get("water") or {}).get("present") or float(cell.get("slope") or 0) > MAX_SLOPE:
            continue
        x, z = cell["position"]["x"], cell["position"]["z"]
        gx, gz = int(x // spacing), int(z // spacing)
        near = any(math.dist((x, z), point) < CLEARANCE
                   for dx in (-1, 0, 1) for dz in (-1, 0, 1) for point in used.get((gx + dx, gz + dz), []))
        if not near:
            free.append(cell)
    return free


def areas(samples: list[dict[str, Any]], spacing: float, minimum_samples: int = 6) -> list[dict[str, Any]]:
    """Connected free samples (4-neighbourhood on the sample grid), largest first, each with its
    centroid, extent and long axis (principal direction of its samples)."""
    by_key = {grid_key((cell["position"]["x"], cell["position"]["z"]), spacing): cell for cell in samples}
    seen, groups = set(), []
    for key in by_key:
        if key in seen:
            continue
        stack, members = [key], []
        seen.add(key)
        while stack:
            current = stack.pop()
            members.append(by_key[current])
            for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                neighbour = (current[0] + dx, current[1] + dz)
                if neighbour in by_key and neighbour not in seen:
                    seen.add(neighbour)
                    stack.append(neighbour)
        if len(members) >= minimum_samples:
            groups.append(members)
    result = []
    for members in groups:
        xs = [cell["position"]["x"] for cell in members]
        zs = [cell["position"]["z"] for cell in members]
        cx, cz = sum(xs) / len(xs), sum(zs) / len(zs)
        sxx = sum((x - cx) ** 2 for x in xs) / len(xs)
        szz = sum((z - cz) ** 2 for z in zs) / len(zs)
        sxz = sum((x - cx) * (z - cz) for x, z in zip(xs, zs)) / len(xs)
        axis = 0.5 * math.atan2(2 * sxz, sxx - szz)  # radians from +x
        ux, uz = math.cos(axis), math.sin(axis)
        along = [(x - cx) * ux + (z - cz) * uz for x, z in zip(xs, zs)]
        across = [-(x - cx) * uz + (z - cz) * ux for x, z in zip(xs, zs)]
        result.append({
            "samples": len(members), "areaSquareMeters": round(len(members) * spacing * spacing),
            "center": {"x": round(cx, 1), "z": round(cz, 1)},
            "bounds": {"minX": min(xs), "maxX": max(xs), "minZ": min(zs), "maxZ": max(zs)},
            "axisDegrees": round(math.degrees(axis), 1),
            "length": round(max(along) - min(along) + spacing, 1),
            "width": round(max(across) - min(across) + spacing, 1),
            "points": [(cell["position"]["x"], cell["position"]["z"]) for cell in members],
            "meanSlope": round(sum(float(cell.get("slope") or 0.0) for cell in members) / len(members), 3),
        })
    result.sort(key=lambda area: -area["samples"])
    return result


def _crosses(a: tuple[float, float], b: tuple[float, float], c: tuple[float, float], d: tuple[float, float]) -> bool:
    def side(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
    return side(a, b, c) * side(a, b, d) < 0 and side(c, d, a) * side(c, d, b) < 0


def _on_lot(point: tuple[float, float], building: dict[str, Any], margin: float) -> bool:
    """Whether a point lies on a building's lot (width and length in 8m cells, turned by its angle)."""
    angle = math.radians(float(building.get("angleDegrees") or 0))
    dx, dz = point[0] - building["position"]["x"], point[1] - building["position"]["z"]
    across = dx * math.cos(angle) + dz * math.sin(angle)
    along = -dx * math.sin(angle) + dz * math.cos(angle)
    return abs(across) <= 4 * float(building.get("width") or 1) + margin \
        and abs(along) <= 4 * float(building.get("length") or 1) + margin


def _distance_to_segment(point: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
    length = math.dist(a, b)
    if length == 0:
        return math.dist(point, a)
    t = max(0.0, min(1.0, ((point[0] - a[0]) * (b[0] - a[0]) + (point[1] - a[1]) * (b[1] - a[1])) / length ** 2))
    return math.dist(point, (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))


def in_the_way(start: tuple[float, float], end: tuple[float, float], buildings: list[dict[str, Any]],
               lines: list[dict[str, Any]]) -> int:
    """Buildings and power lines a straight street from start to end would run over."""
    length = math.dist(start, end)
    steps = max(1, int(length / 4))
    points = [(start[0] + (end[0] - start[0]) * i / steps, start[1] + (end[1] - start[1]) * i / steps)
              for i in range(1, steps + 1)]
    near = [b for b in buildings if _distance_to_segment((b["position"]["x"], b["position"]["z"]), start, end) < 64]
    count = sum(1 for b in near if any(_on_lot(point, b, BUILDING_MARGIN) for point in points))
    for line in lines:
        a, b = (line["start"]["x"], line["start"]["z"]), (line["end"]["x"], line["end"]["z"])
        if _crosses(start, end, a, b) or min([_distance_to_segment(p, start, end) for p in (a, b)] +
                                             [_distance_to_segment(p, a, b) for p in (start, end)]) < LINE_MARGIN:
            count += 1
    return count


def access(area: dict[str, Any], segments: list[dict[str, Any]], cells_by_key: dict[tuple[int, int], dict[str, Any]],
           spacing: float, candidates: int = 3, buildings: Iterable[dict[str, Any]] = ()) -> list[dict[str, Any]]:
    """Ways in from the nearest local road nodes, each with what the straight approach crosses:
    ground-level railways and highways (elevated or underground ones pass) and water. A node that
    also ends a highway or railway is not a way in: a street there would join the highway itself.

    In a built-up town the nearest nodes reach the land only across back gardens (live: all six
    ways tried ran over homes or a power line while dead ends 110-200m off ran over nothing), so
    nodes are looked through until candidates ways run over nothing (blocked counts what they do);
    those come first."""
    buildings = [building for building in buildings if building.get("position")]
    lines = [segment for segment in segments if "Power Line" in str(segment.get("prefab"))]
    barrier_nodes = {int(segment[end]) for segment in segments
                     if any(part in str(segment.get("prefab")) for part in BARRIER_PARTS)
                     for end in ("startNodeId", "endNodeId")}
    nodes: dict[int, tuple[float, float]] = {}
    for segment in segments:
        prefab = str(segment.get("prefab"))
        if any(part in prefab for part in BARRIER_PARTS + IGNORED_PARTS) or "Road" not in prefab:
            continue
        if "Elevated" in prefab or "Tunnel" in prefab or "Bridge" in prefab:
            continue
        for end, point in (("startNodeId", "start"), ("endNodeId", "end")):
            if int(segment[end]) not in barrier_nodes:
                nodes[int(segment[end])] = (segment[point]["x"], segment[point]["z"])
    barriers = [segment for segment in segments
                if any(part in str(segment.get("prefab")) for part in BARRIER_PARTS)
                and not any(level in str(segment.get("prefab")) for level in ("Elevated", "Tunnel", "Bridge"))]
    options = []
    for node, position in nodes.items():
        target = min(area["points"], key=lambda point: math.dist(point, position))
        options.append((math.dist(position, target), node, position, target))
    options.sort()
    result = []
    for distance, node, position, target in options[:max(ACCESS_SCAN, candidates)]:
        crossed = sorted({str(segment.get("prefab")) for segment in barriers
                          if _crosses(position, target, (segment["start"]["x"], segment["start"]["z"]),
                                      (segment["end"]["x"], segment["end"]["z"]))})
        steps = max(1, int(distance / (spacing / 2)))
        water = any((cells_by_key.get(grid_key((position[0] + (target[0] - position[0]) * i / steps,
                                                position[1] + (target[1] - position[1]) * i / steps), spacing))
                     or {}).get("water", {}).get("present") for i in range(steps + 1))
        result.append({"nodeId": node, "from": {"x": round(position[0], 2), "z": round(position[1], 2)},
                       "to": {"x": round(target[0], 2), "z": round(target[1], 2)}, "distance": round(distance, 1),
                       "crosses": crossed + (["water"] if water else []), "clear": not crossed and not water,
                       "blocked": in_the_way(position, target, buildings, lines)})
        if sum(1 for way in result if not way["blocked"]) >= candidates:
            break
    # of the ways something stands in, the one with least in its way first
    result.sort(key=lambda item: (not item["clear"], item["blocked"], item["distance"]))
    return result[:candidates]


def fishbone(area: dict[str, Any], entry: dict[str, Any], segment_length: float = 88.0) -> dict[str, Any]:
    """A collector from the entry along the area's long axis (the axis direction that points into
    the area), with branches on both sides reaching half the area's width."""
    axis = math.radians(area["axisDegrees"])
    ux, uz = math.cos(axis), math.sin(axis)
    start = (entry["to"]["x"], entry["to"]["z"])
    center = (area["center"]["x"], area["center"]["z"])
    if (center[0] - start[0]) * ux + (center[1] - start[1]) * uz < 0:
        ux, uz = -ux, -uz
    reach = max(((point[0] - start[0]) * ux + (point[1] - start[1]) * uz for point in area["points"]), default=0.0)
    branch = max(48.0, min(160.0, area["width"] / 2 - 8.0))
    # the comb builder's heading: 0 is due south (-z), positive turns toward +x
    heading = math.degrees(math.atan2(ux, -uz))
    return {"collectorHeadingDegrees": round(heading, 1), "collectorSegments": max(1, int(reach // segment_length)),
            "branchLength": round(branch, 1), "start": entry["to"], "entryNodeId": entry["nodeId"]}

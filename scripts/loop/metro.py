"""A metro line along a hotspot's busiest trip, once its cars are mostly local (loop.traffic, kind
"metro"): trains under the town take the trips a bus in the same traffic cannot (live: a two-stop
line carried 35 riders a week after two wakes, as many as the busier relief buses).

Stations: the hotspot's local anchor and the farthest of its top destinations between MIN_SPACING
and MAX_LENGTH from it; a pair whose ends both have a metro station within STATION_REACH is served
already. Each is placed like a service building (services.place_near), which lays its own platform
track underground.

Track: from the platform node of one station nearest the other to the other's nearest, in pieces of
about PIECE metres at DEPTH below the ground. The game merges a piece laid straight on from the one
before (the Bridge refuses that, as rollback would not restore the merged segment), so the inner
points stand ZIGZAG metres either side of the line, more than the 8 metres within which a failed
alignment counts as tried again (road_attempts). Each piece is previewed first and goes deeper
(DEPTHS) where it would hit something at the depth before (live: piece 9 of the first line the loop
laid collided, as an older line or pipes lie at the same depth); its next piece starts at the depth
it ended at. The track leaves each platform straight on for LEAD metres before it turns (live: a
first piece turning off the platform at once collided at every depth).

The line: the two stations as its stops (a metro line needs no depot). Anything that fails removes
what was built. At most MAX_LINES lines, and only with the cost and RESERVE in hand.
"""

from __future__ import annotations

import math
from typing import Any

from loop import protocol, services
from loop.city import mt

STATION = "Metro Entrance"
TRACK = "Metro Track"
PIECE = 80.0
DEPTH = -12.0
DEPTHS = (DEPTH, -20.0, -28.0, -36.0)  # in turn where a piece collides (live: a crossing cleared at -28)
ZIGZAG = 10.0
MIN_SPACING = 600.0
MAX_LENGTH = 3000.0
STATION_REACH = 400.0
LEAD = 40.0
PLATFORM_REACH = 120.0  # a station's platform track ends this near the entrance (live: 60-110m)
MAX_LINES = 3
# in the game's cents, as services.cash reads it
STATION_COST = 1_500_000.0  # live constructionCost
PIECE_COST = 380_000.0  # a piece of about PIECE metres (live preview)
RESERVE = 10_000_000.0
Point = tuple[float, float]


def available() -> bool:
    """Metro is unlocked; a read that fails leaves it out of this wake."""
    try:
        unlocks = mt._request("GET", "/state/unlocks").get("subServices", [])
    except (mt.BridgeRequestError, RuntimeError):
        return False
    return any(item.get("name") == "PublicTransportMetro" and item.get("unlocked") for item in unlocks)


def _xz(point: dict[str, float]) -> Point:
    return float(point["x"]), float(point["z"])


def stations() -> list[dict[str, Any]]:
    return [item for item in mt._request("GET", "/state/facilities",
                                         query={"service": "PublicTransport", "limit": 1000}).get("facilities", [])
            if item.get("prefab") == STATION]


def pair(hotspot: dict[str, Any], standing: list[Point]) -> tuple[Point, Point] | None:
    """The local anchor and its farthest top destination within reach, or None (none, or served)."""
    diagnosis = hotspot.get("diagnosis") or {}
    anchor = diagnosis.get("localAnchor")
    if not anchor:
        return None
    a = _xz(anchor)
    ends = [_xz(cell["center"]) for cell in diagnosis.get("topDestinations") or []]
    ends = [b for b in ends if MIN_SPACING <= math.dist(a, b) <= MAX_LENGTH]
    if not ends:
        return None
    b = max(ends, key=lambda point: math.dist(a, point))

    def served(point: Point) -> bool:
        return any(math.dist(point, station) <= STATION_REACH for station in standing)
    return None if served(a) and served(b) else (a, b)


def pieces(a: Point, b: Point) -> list[tuple[Point, Point]]:
    """Track pieces from a to b of about PIECE metres, the inner points ZIGZAG either side."""
    length = math.dist(a, b)
    count = max(1, math.ceil(length / PIECE))
    ux, uz = (b[0] - a[0]) / length, (b[1] - a[1]) / length
    side = (-uz, ux)
    points = []
    for i in range(count + 1):
        x, z = a[0] + (b[0] - a[0]) * i / count, a[1] + (b[1] - a[1]) * i / count
        if 0 < i < count:
            offset = ZIGZAG if i % 2 else -ZIGZAG
            x, z = x + side[0] * offset, z + side[1] * offset
        points.append((round(x, 2), round(z, 2)))
    return list(zip(points, points[1:]))


def cost(a: Point, b: Point) -> float:
    return 2 * STATION_COST + math.ceil(math.dist(a, b) / PIECE) * PIECE_COST


def _platform_node(building: dict[str, Any], before: set[int], toward: Point) -> tuple[int, Point, Point] | None:
    """The new station's platform node nearest the other end, with the point LEAD metres straight
    out of the platform beyond it."""
    here = _xz(building["position"])
    nodes = []
    for segment in mt._request("GET", "/state/networks", query={"limit": 20000}).get("segments", []):
        if segment.get("subService") != "PublicTransportMetro" or int(segment["id"]) in before:
            continue
        ends = (("startNodeId", "start", "end"), ("endNodeId", "end", "start"))
        for key, end, other in ends:
            point, back = _xz(segment[end]), _xz(segment[other])
            if math.dist(point, here) <= PLATFORM_REACH:
                length = math.dist(point, back) or 1.0
                lead = (round(point[0] + (point[0] - back[0]) / length * LEAD, 2),
                        round(point[1] + (point[1] - back[1]) / length * LEAD, 2))
                nodes.append((int(segment[key]), point, lead))
    return min(nodes, key=lambda node: math.dist(node[1], toward)) if nodes else None


def _preview(start: Point, end: Point, start_depth: float, end_depth: float) -> bool:
    try:
        preview = mt._request("POST", "/commands/preview-network", body={
            "roadPrefab": TRACK, "start": {"x": start[0], "z": start[1]}, "end": {"x": end[0], "z": end[1]},
            "startHeightOffset": start_depth, "endHeightOffset": end_depth})
    except (mt.BridgeRequestError, RuntimeError):
        return False
    return bool(preview.get("valid"))


def depth_for(start: Point, end: Point, start_depth: float, last: bool) -> float | None:
    """The depth the piece can end at: the platform's own for the last piece, else the first of
    DEPTHS it previews clean at, or None."""
    for depth in ((0.0,) if last else DEPTHS):
        if _preview(start, end, start_depth, depth):
            return depth
    return None


def _metro_segments() -> set[int]:
    return {int(s["id"]) for s in mt._request("GET", "/state/networks", query={"limit": 20000}).get("segments", [])
            if s.get("subService") == "PublicTransportMetro"}


def _undo(label: str, built: dict[str, Any]) -> bool:
    removed = True
    for segment in reversed(built["track"]):
        removed &= bool(protocol.run(f"{label}-undo-{segment}", "bulldoze_entity",
                                     {"entityType": "netSegment", "id": segment})["ok"])
    for building in built["stations"]:
        removed &= bool(protocol.run(f"{label}-undo-{building}", "bulldoze_entity",
                                     {"entityType": "building", "id": building})["ok"])
    return removed


def build_line(label: str, hotspot: dict[str, Any], lines: int, cash: float) -> dict[str, Any] | None:
    """A metro line for the hotspot, or None when there is nothing to build (no pair, served, the
    cap reached, or short of cash)."""
    if lines >= MAX_LINES:
        return None
    ends = pair(hotspot, [_xz(item["position"]) for item in stations()])
    if ends is None or cash < cost(*ends) + RESERVE:
        return None
    label = f"metro-{label}"
    built: dict[str, Any] = {"stations": [], "track": []}

    def failed(stage: str, **detail: Any) -> dict[str, Any]:
        return {"ok": False, "stage": stage, "ends": ends, **detail, "undone": _undo(label, built)}

    before = _metro_segments()
    placed = []
    for index, point in enumerate(ends):
        result = services.place_near(f"{label}-station{index}", STATION, {"x": point[0], "z": point[1]})
        if not result.get("ok"):
            return failed("station", station=index, result={k: result.get(k) for k in ("stage", "error")})
        built["stations"].append(int(result["buildingId"]))
        placed.append(result)
    nodes = [_platform_node(placed[0], before, _xz(placed[1]["position"])),
             _platform_node(placed[1], before, _xz(placed[0]["position"]))]
    if not all(nodes):
        return failed("platform")
    checkpoint = protocol.checkpoint(f"{label}-track")
    route = [(nodes[0][1], nodes[0][2])] + pieces(nodes[0][2], nodes[1][2]) + [(nodes[1][2], nodes[1][1])]
    depth, depths = 0.0, []
    for index, (start, end) in enumerate(route):
        laid = before | set(built["track"])
        end_depth = depth_for(start, end, depth, index == len(route) - 1)
        if end_depth is None:
            return failed("track", piece=index, at=start, error="collides at every depth")
        try:
            outcome = mt.call_tool("build_network", {
                "checkpointName": checkpoint, "roadPrefab": TRACK, "name": f"{label}-{index}",
                "start": {"x": start[0], "z": start[1]}, "end": {"x": end[0], "z": end[1]},
                "startHeightOffset": depth, "endHeightOffset": end_depth})
        except (mt.BridgeRequestError, RuntimeError) as error:
            built["track"].extend(sorted(_metro_segments() - laid))
            return failed("track", piece=index, error=str(error)[:200])
        built["track"].extend(sorted(_metro_segments() - laid))
        if not outcome.get("ok"):
            return failed("track", piece=index, at=start, error=str(outcome.get("error"))[:200])
        depth = end_depth
        depths.append(end_depth)
    regression = protocol.verify(f"{label}-track")
    if regression.get("ok") is False:
        return failed("regression")
    stops = [{"x": item["position"]["x"], "z": item["position"]["z"]} for item in placed]
    line = protocol.run(f"{label}-line", "create_transport_line",
                        {"transportType": "Metro", "stops": stops, "completeLoop": True, "name": f"Metro {label}"})
    if not line["ok"]:
        return failed("line", error=str(line.get("error"))[:200])
    return {"ok": True, "regressionOk": line["regressionOk"], "lineId": (line.get("result") or {}).get("lineId"),
            "stations": built["stations"], "pieces": len(route), "length": round(math.dist(*ends)),
            "deepest": min(depths, default=0.0)}

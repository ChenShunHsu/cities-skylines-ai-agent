"""An exit ramp off the highway near where a highway-fed hotspot's traffic is going (loop.traffic,
kind "highwayExit"): a town with one link to the highway sends every arriving car to that link and
back across town (live: 48,000 people, one link at the far end; the carriageway queued at density
100 for 500 vehicles, and one exit ramp near the north of town took it to 55-85 in two wakes).

Where: a plain node of a carriageway (two highway segments, no junction within JUNCTION_GAP) within
NODE_REACH of the hotspot's local anchor or top destinations, and a dead end of a town road
STUB_MIN-STUB_MAX ahead of it on the carriageway's outer side (away from the other carriageway).
The ramp leaves in the direction of travel.

How: two pieces through a midpoint MID_ALONG metres out and MID_ASIDE to one side (straight on, the
town piece would merge with the dead-end road), each previewed clean (no building, power line or
tool error; a terrain cut of a few centimetres allowed, as a native highway node lies on the
ground). The town piece is built first so its new node settles before the highway piece meets it
(the reverse leaves the highway piece too steep). What fails is taken down. At most MAX_RAMPS.
"""

from __future__ import annotations

import math
from typing import Any

from loop import protocol
from loop.city import mt

PREFAB = "HighwayRamp"
HIGHWAYS = ("Highway", "Highway Elevated")
NODE_REACH = 900.0
JUNCTION_GAP = 150.0
STUB_MIN, STUB_MAX = 70.0, 160.0
MID_ALONG = (50.0, 40.0, 60.0)
MID_ASIDE = (-12.0, 12.0, -20.0, 20.0)
OPPOSITE_REACH = 60.0  # the other carriageway runs this close
MAX_NODES = 6
MAX_RAMPS = 4
Point = tuple[float, float]


def _xz(point: dict[str, Any]) -> Point:
    return float(point["x"]), float(point["z"])


def _travel(segment: dict[str, Any]) -> Point:
    a, b = _xz(segment["start"]), _xz(segment["end"])
    if segment.get("directionality") == "EndToStart":
        a, b = b, a
    length = math.dist(a, b) or 1.0
    return (b[0] - a[0]) / length, (b[1] - a[1]) / length


def _side(origin: Point, direction: Point, point: Point) -> float:
    """Positive on the right of direction (x east, z north), negative on the left."""
    return direction[1] * (point[0] - origin[0]) - direction[0] * (point[1] - origin[1])


def candidates(segments: list[dict[str, Any]], targets: list[Point]) -> list[dict[str, Any]]:
    """(highway node, travel, outer sign, dead ends ahead on the outer side) near the targets,
    nearest a target first."""
    by_node: dict[int, list[dict[str, Any]]] = {}
    where: dict[int, Point] = {}
    for segment in segments:
        for key, end in (("startNodeId", "start"), ("endNodeId", "end")):
            by_node.setdefault(int(segment[key]), []).append(segment)
            where[int(segment[key])] = _xz(segment[end])
    highway = [s for s in segments if s.get("prefab") in HIGHWAYS]
    junctions = [where[node] for node, touching in by_node.items() if len(touching) >= 3
                 and any(s.get("prefab") in HIGHWAYS or "Ramp" in str(s.get("prefab")) for s in touching)]
    ends = [where[node] for node, touching in by_node.items() if len(touching) == 1
            and touching[0].get("service") == "Road" and touching[0].get("prefab") not in HIGHWAYS
            and "Ramp" not in str(touching[0].get("prefab"))]
    found = []
    for node, touching in by_node.items():
        if len(touching) != 2 or not all(s.get("prefab") in HIGHWAYS for s in touching):
            continue
        here = where[node]
        reach = min((math.dist(here, target) for target in targets), default=math.inf)
        if reach > NODE_REACH or any(math.dist(here, j) < JUNCTION_GAP for j in junctions):
            continue
        travel = _travel(touching[0])
        others = [s for s in highway if s not in touching and math.dist(here, _xz(s["middle"] if "middle" in s
                                                                                  else s["start"])) < OPPOSITE_REACH]
        opposite = [_side(here, travel, _xz(s.get("middle") or s["start"])) for s in others
                    if travel[0] * _travel(s)[0] + travel[1] * _travel(s)[1] < -0.5]
        outer = -1.0 if opposite and sum(opposite) > 0 else 1.0 if opposite else 0.0
        ahead = [end for end in ends if STUB_MIN <= math.dist(here, end) <= STUB_MAX
                 and (end[0] - here[0]) * travel[0] + (end[1] - here[1]) * travel[1] > 0.3 * math.dist(here, end)
                 and (outer == 0.0 or _side(here, travel, end) * outer > 0)]
        if ahead:
            found.append({"node": node, "at": here, "travel": travel, "ends": ahead, "reach": reach})
    return sorted(found, key=lambda item: item["reach"])[:MAX_NODES]


def _preview(start: Point, end: Point) -> bool:
    try:
        preview = mt._request("POST", "/commands/preview-network", body={
            "roadPrefab": PREFAB, "start": {"x": start[0], "z": start[1]}, "end": {"x": end[0], "z": end[1]},
            "allowTerrainCut": True})
    except (mt.BridgeRequestError, RuntimeError):
        return False
    return bool((preview.get("valid") or preview.get("validWithTerrainCut"))
                and str(preview.get("toolErrors") or "None") == "None"
                and not preview.get("buildingsUnderRoad") and not preview.get("powerLinesCrossed")
                and not preview.get("shortCollinearMergeRisk") and not preview.get("nativeMoveMiddleNodeRisk"))


def plan(candidate: dict[str, Any]) -> tuple[Point, Point, Point] | None:
    """(highway node, midpoint, dead end) whose two pieces preview clean, or None."""
    here = candidate["at"]
    for end in candidate["ends"]:
        length = math.dist(here, end)
        ux, uz = (end[0] - here[0]) / length, (end[1] - here[1]) / length
        for along in MID_ALONG:
            for aside in MID_ASIDE:
                middle = (round(here[0] + ux * along - uz * aside, 2), round(here[1] + uz * along + ux * aside, 2))
                if _preview(here, middle) and _preview(middle, end):
                    return here, middle, end
    return None


def _ramp_segments() -> set[int]:
    return {int(s["id"]) for s in mt._request("GET", "/state/networks", query={"limit": 50000, "service": "Road"})
            .get("segments", []) if s.get("prefab") == PREFAB}


def build_exit(label: str, hotspot: dict[str, Any], ramps: int) -> dict[str, Any] | None:
    """An exit ramp for the hotspot's traffic, or None when there is nothing to build."""
    if ramps >= MAX_RAMPS:
        return None
    diagnosis = hotspot.get("diagnosis") or {}
    targets = [_xz(cell["center"]) for cell in diagnosis.get("topDestinations") or []]
    if diagnosis.get("localAnchor"):
        targets.append(_xz(diagnosis["localAnchor"]))
    segments = mt._request("GET", "/state/networks", query={"limit": 50000, "service": "Road"}).get("segments", [])
    route = None
    for candidate in candidates(segments, targets):
        route = plan(candidate)
        if route:
            break
    if route is None:
        return None
    label = f"exit-{label}"
    before = _ramp_segments()
    checkpoint = protocol.checkpoint(label)
    built: list[int] = []
    for index, (start, end) in enumerate(((route[1], route[2]), (route[0], route[1]))):
        try:
            outcome = mt.call_tool("build_network", {
                "checkpointName": checkpoint, "roadPrefab": PREFAB, "name": f"{label}-{index}",
                "allowTerrainCut": True, "start": {"x": start[0], "z": start[1]}, "end": {"x": end[0], "z": end[1]}})
        except (mt.BridgeRequestError, RuntimeError) as error:
            outcome = {"ok": False, "error": str(error)[:200]}
        built = sorted(_ramp_segments() - before)
        if not outcome.get("ok"):
            undone = all(protocol.run(f"{label}-undo-{segment}", "bulldoze_entity",
                                      {"entityType": "netSegment", "id": segment})["ok"] for segment in built)
            return {"ok": False, "stage": f"piece{index}", "error": outcome.get("error"), "undone": undone,
                    "node": route[0]}
    regression = protocol.verify(label)
    return {"ok": regression.get("ok") is not False, "regressionOk": regression.get("ok"), "segments": built,
            "node": route[0], "end": route[2]}

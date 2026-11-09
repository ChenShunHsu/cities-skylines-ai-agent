"""Bounded dead-end frontage extension candidates from live road topology.

The planner extends existing dead-end roads in their own direction so the
anchor becomes a gentle continuation and the new end stays a dead end. Both
keep the corner clearance gate satisfied for blocks on the new segment. It
proposes and natively previews candidates only; it never mutates the city.
No map name, city ID, entity ID or fixed coordinate is used as policy.
"""
import math
from collections import Counter

from road_corridor import line_in_bounds, segment_distance

# Must stay below the Bridge's 45-degree continuation limit so the anchor
# node does not become a corner that invalidates nearby frontage. A straight
# (0 degree) extension of a dead end triggers CS1's native MoveMiddleNode,
# which rewrites the existing segment, so every candidate bends slightly.
DEFAULT_DEFLECTIONS = (8.0, -8.0, 20.0, -20.0, 35.0, -35.0)
DEFAULT_LENGTHS = (96.0, 80.0, 64.0)
CELL_SIZE = 8.0
BLOCK_DEPTH_CELLS = 4
# Shared with the project's residential/industrial planning separation.
RESIDENTIAL_INDUSTRY_SEPARATION = 64.0


def _xz(value):
    return (float(value["x"]), float(value["z"]))


def _rotate(direction, degrees):
    radians = math.radians(degrees)
    c, s = math.cos(radians), math.sin(radians)
    return (direction[0] * c - direction[1] * s, direction[0] * s + direction[1] * c)


def _unit(vector):
    length = math.hypot(*vector)
    if not math.isfinite(length) or length < 1e-6:
        return None
    return (vector[0] / length, vector[1] / length)


def _road_polyline(segment):
    points = [_xz(segment["start"])]
    if isinstance(segment.get("middle"), dict):
        points.append(_xz(segment["middle"]))
    points.append(_xz(segment["end"]))
    return points


def _distance_to_polyline(a, b, polyline):
    return min(segment_distance(a, b, polyline[i], polyline[i + 1])
               for i in range(len(polyline) - 1))


def _point_segment_distance(p, a, b):
    vx, vz = b[0] - a[0], b[1] - a[1]
    denominator = vx * vx + vz * vz
    t = 0.0 if denominator == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * vx + (p[1] - a[1]) * vz) / denominator))
    return math.dist(p, (a[0] + t * vx, a[1] + t * vz))


def expected_frontage_cells(length):
    """Upper bound: both sides, four cells deep, whole 8m cells along the road."""
    return 2 * BLOCK_DEPTH_CELLS * max(0, int(length // CELL_SIZE) - 1)


def dead_end_anchors(segments, center=None, radius=None):
    """Return dead-end anchors with their continuation heading, from live segments."""
    anchors = []
    for segment in segments:
        if "highway" in str(segment.get("prefab", "")).casefold():
            continue
        for side in ("start", "end"):
            if segment.get(f"{side}NodeDegree") != 1 or segment.get(f"{side}Outside"):
                continue
            position = segment.get(side)
            direction = segment.get(f"{side}Direction")
            if not isinstance(position, dict) or not isinstance(direction, dict):
                continue
            # Native directions point from the node into its segment.
            heading = _unit((-float(direction["x"]), -float(direction["z"])))
            if heading is None:
                continue
            point = _xz(position)
            if center is not None and radius is not None and math.dist(point, center) > radius:
                continue
            anchors.append({"nodeId": segment[f"{side}NodeId"], "segmentId": segment["id"],
                            "roadPrefab": segment.get("prefab", ""), "position": point,
                            "heading": heading})
    anchors.sort(key=lambda item: (math.dist(item["position"], center) if center else 0.0, item["nodeId"]))
    return anchors


def plan_frontage_extensions(*, anchors, segments, bounds, preview, accepted, intended_zone,
                             conflicting_points=(), rejected=lambda candidate: False,
                             lengths=DEFAULT_LENGTHS, deflections=DEFAULT_DEFLECTIONS,
                             road_prefab=None, minimum_road_separation=40.0,
                             maximum_previews=24, limit=5):
    stream = Counter()
    rejections = Counter()
    candidates = []
    previews = 0
    polylines = {segment["id"]: _road_polyline(segment) for segment in segments
                 if isinstance(segment.get("start"), dict) and isinstance(segment.get("end"), dict)}
    statically_eligible = []
    for anchor in anchors:
        for deflection in deflections:
            heading = _rotate(anchor["heading"], deflection)
            for length in lengths:
                stream["generated"] += 1
                start = anchor["position"]
                end = (start[0] + heading[0] * length, start[1] + heading[1] * length)
                if not line_in_bounds(start, end, bounds, margin=8):
                    rejections["outsideUnlockedArea"] += 1
                    continue
                # Ignore the shared anchor point when measuring separation.
                probe = (start[0] + heading[0] * minimum_road_separation,
                         start[1] + heading[1] * minimum_road_separation)
                too_close = False
                for segment_id, polyline in polylines.items():
                    if segment_id == anchor["segmentId"]:
                        continue
                    if _distance_to_polyline(probe, end, polyline) < minimum_road_separation:
                        too_close = True
                        break
                if too_close:
                    rejections["nearOtherRoad"] += 1
                    continue
                # Residential and industry keep the same separation both ways.
                if any(_point_segment_distance(point, start, end) < RESIDENTIAL_INDUSTRY_SEPARATION
                       for point in conflicting_points):
                    rejections["nearConflictingUse"] += 1
                    continue
                candidate = {"roadPrefab": road_prefab or anchor["roadPrefab"],
                             "start": {"x": round(start[0], 3), "z": round(start[1], 3)},
                             "end": {"x": round(end[0], 3), "z": round(end[1], 3)},
                             "name": f"Agent {intended_zone.lower()} frontage extension"}
                if rejected(candidate):
                    rejections["previousFailure"] += 1
                    continue
                stream["staticallyEligible"] += 1
                statically_eligible.append((anchor, deflection, length, candidate))
    # Longer, straighter extensions first so the preview budget favors frontage.
    statically_eligible.sort(key=lambda item: (-item[2], abs(item[1]), item[0]["nodeId"]))
    for anchor, deflection, length, candidate in statically_eligible:
        if previews >= maximum_previews:
            rejections["previewBudget"] += 1
            continue
        previews += 1
        stream["previewed"] += 1
        result = preview(candidate)
        if not accepted(result):
            rejections["nativePreviewOrTerrain"] += 1
            continue
        stream["accepted"] += 1
        terrain = result.get("terrainProfile") or {}
        cost = result.get("constructionCost")
        cells = expected_frontage_cells(length)
        candidates.append({
            "candidate": candidate,
            "anchorNodeId": anchor["nodeId"],
            "anchorSegmentId": anchor["segmentId"],
            "lengthMeters": round(length, 3),
            "deflectionDegrees": deflection,
            "expectedFrontageCellsUpperBound": cells,
            "nativePreview": {
                "valid": result.get("valid"),
                "cost": cost,
                "minimumRoadAboveTerrain": terrain.get("minimumRoadAboveTerrain"),
                "maxRoadToTerrainDelta": terrain.get("maxRoadToTerrainDelta"),
            },
            "cellsPerCost": round(cells / cost, 4) if isinstance(cost, (int, float)) and cost > 0 else None,
        })
    candidates.sort(key=lambda item: (-(item["cellsPerCost"] or 0), -item["lengthMeters"],
                                      abs(item["deflectionDegrees"]), item["anchorNodeId"]))
    return {
        "ok": True,
        "planningOnly": True,
        "mutationAuthorized": False,
        "intendedZone": intended_zone,
        "anchorsExamined": len(anchors),
        "stream": {key: stream[key] for key in ("generated", "staticallyEligible", "previewed", "accepted")},
        "nativePreviewCalls": previews,
        "rejections": dict(rejections),
        "returned": min(limit, len(candidates)),
        "candidates": candidates[:limit],
        "limitations": [
            "Expected frontage cells are an upper bound; native zone validity, terrain and collisions decide the real block.",
            "A preview is not settled evidence; build_network repeats terrain checks and settled QA remains required.",
        ],
        "nextSteps": [
            "create_checkpoint", "capture_regression_baseline", "build_network",
            "inspect_road_result with allowTerminalDeadEnd and the new terminal node",
            "inspect_growth_capacity for the exact developmentFrontageSafe block",
        ],
    }

"""Offline layered road geometry derived from a captured CS1 Bridge snapshot."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Iterable


DEFAULT_SAMPLE_COUNT = 9
ZONE_CELL_SIZE_METERS = 8.0
ZONE_BLOCK_COLUMNS = 4
INDUSTRIAL_RESIDENTIAL_MIN_CLEARANCE_METERS = 64.0
INDUSTRIAL_RESIDENTIAL_CAUTION_CLEARANCE_METERS = 96.0
INDUSTRIAL_ROAD_ENDPOINT_MIN_RESIDENTIAL_CLEARANCE_METERS = 96.0
LOCAL_ROAD_MINIMUM_JUNCTION_ANGLE_DEGREES = 30.0


def load_snapshot(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def latest_snapshot(root: Path) -> Path:
    matches = sorted((root / "artifacts").glob("city-spatial-snapshot-*.json"))
    if not matches:
        raise FileNotFoundError("No offline city spatial snapshot was found.")
    return matches[-1]


def structure_type(prefab: str) -> str:
    name = prefab.lower()
    if "tunnel" in name:
        return "Tunnel"
    if "slope" in name:
        return "Slope"
    if "bridge" in name:
        return "Bridge"
    if "elevated" in name:
        return "Elevated"
    return "Ground"


def _quadratic_control(start: dict[str, float], middle: dict[str, float], end: dict[str, float]) -> dict[str, float]:
    return {
        axis: 2.0 * float(middle[axis]) - 0.5 * (float(start[axis]) + float(end[axis]))
        for axis in ("x", "y", "z")
    }


def sample_road(road: dict[str, Any], count: int = DEFAULT_SAMPLE_COUNT) -> list[dict[str, float]]:
    count = max(3, min(65, int(count)))
    curve = road.get("curve", {})
    use_native_cubic = all(axis in curve for axis in ("a", "b", "c", "d"))
    start = curve["a"] if use_native_cubic else road["start"]
    end = curve["d"] if use_native_cubic else road["end"]
    middle = road["middle"]
    control = None if use_native_cubic else _quadratic_control(start, middle, end)
    samples = []
    for index in range(count):
        t = index / (count - 1)
        inverse = 1.0 - t
        if use_native_cubic:
            point = {
                axis: inverse**3 * float(curve["a"][axis])
                + 3.0 * inverse * inverse * t * float(curve["b"][axis])
                + 3.0 * inverse * t * t * float(curve["c"][axis])
                + t**3 * float(curve["d"][axis])
                for axis in ("x", "y", "z")
            }
        else:
            point = {
                axis: inverse * inverse * float(start[axis])
                + 2.0 * inverse * t * control[axis]
                + t * t * float(end[axis])
                for axis in ("x", "y", "z")
            }
        point["t"] = t
        samples.append(point)
    return samples


class TerrainSampler:
    def __init__(self, snapshot: dict[str, Any]):
        self.tiles: list[dict[str, Any]] = []
        for tile in snapshot.get("tileAnalyses", []):
            cells = tile.get("map", {}).get("cells", [])
            bounds = tile.get("bounds", {})
            if not cells or not bounds:
                continue
            resolution = int(round(math.sqrt(len(cells))))
            by_grid = {
                (int(cell["row"]), int(cell["column"])): float(cell["height"])
                for cell in cells
            }
            self.tiles.append({"bounds": bounds, "resolution": resolution, "grid": by_grid})

    def height(self, x: float, z: float) -> float | None:
        for tile in self.tiles:
            bounds = tile["bounds"]
            if not (
                float(bounds["minX"]) <= x <= float(bounds["maxX"])
                and float(bounds["minZ"]) <= z <= float(bounds["maxZ"])
            ):
                continue
            resolution = tile["resolution"]
            column = (x - float(bounds["minX"])) / (float(bounds["maxX"]) - float(bounds["minX"])) * (resolution - 1)
            row = (z - float(bounds["minZ"])) / (float(bounds["maxZ"]) - float(bounds["minZ"])) * (resolution - 1)
            c0 = max(0, min(resolution - 1, int(math.floor(column))))
            c1 = max(0, min(resolution - 1, c0 + 1))
            r0 = max(0, min(resolution - 1, int(math.floor(row))))
            r1 = max(0, min(resolution - 1, r0 + 1))
            tx = column - c0
            tz = row - r0
            grid = tile["grid"]
            h00 = grid.get((r0, c0))
            h10 = grid.get((r0, c1))
            h01 = grid.get((r1, c0))
            h11 = grid.get((r1, c1))
            if None in (h00, h10, h01, h11):
                return None
            return (
                h00 * (1 - tx) * (1 - tz)
                + h10 * tx * (1 - tz)
                + h01 * (1 - tx) * tz
                + h11 * tx * tz
            )
        return None


def _directionality(template: dict[str, Any]) -> tuple[str, bool, bool]:
    directions = {
        lane.get("finalDirection")
        for lane in template.get("prefabLanes", [])
        if lane.get("laneType") == "Vehicle"
    }
    forward = "Forward" in directions or "Both" in directions
    backward = "Backward" in directions or "Both" in directions
    if forward and backward:
        return "Bidirectional", True, False
    if forward:
        return "StartToEnd", False, True
    if backward:
        return "EndToStart", False, True
    return "NoVehicleLanes", False, False


def _road_planning_role(
    road: dict[str, Any],
    highway_node_ids: set[int],
) -> str:
    prefab = str(road.get("prefab", "")).lower()
    name = str(road.get("name", "")).lower()
    if "highway" in prefab:
        return "Highway"
    if any(
        token in name
        for token in ("gateway", "interchange", "ramp", "motorway access", "highway access")
    ):
        return "Gateway"
    if int(road.get("startNodeId", 0)) in highway_node_ids or int(
        road.get("endNodeId", 0)
    ) in highway_node_ids:
        return "Gateway"
    if structure_type(str(road.get("prefab", ""))) != "Ground":
        return "GradeSeparated"
    if any(token in prefab for token in ("large road", "medium road")) or any(
        token in name for token in ("arterial", "avenue", "boulevard")
    ):
        return "Arterial"
    return "Local"


def build_road_record(
    road: dict[str, Any],
    templates: dict[str, Any],
    terrain: TerrainSampler,
    sample_count: int = DEFAULT_SAMPLE_COUNT,
    highway_node_ids: set[int] | None = None,
) -> dict[str, Any]:
    points = sample_road(road, sample_count)
    relative_heights = []
    annotated = []
    for point in points:
        terrain_y = terrain.height(float(point["x"]), float(point["z"]))
        relative = None if terrain_y is None else float(point["y"]) - terrain_y
        if relative is not None:
            relative_heights.append(relative)
        annotated.append(
            {
                "t": round(float(point["t"]), 4),
                "x": round(float(point["x"]), 3),
                "y": round(float(point["y"]), 3),
                "z": round(float(point["z"]), 3),
                "terrainY": None if terrain_y is None else round(terrain_y, 3),
                "relativeHeight": None if relative is None else round(relative, 3),
            }
        )
    template_directionality, template_bidirectional, template_one_way = _directionality(
        templates.get(road.get("prefab", ""), {})
    )
    directionality = str(road.get("directionality", "")).strip() or template_directionality
    bidirectional = bool(road.get("isBidirectional", template_bidirectional))
    one_way = bool(road.get("isOneWay", template_one_way))
    planning_role = _road_planning_role(road, highway_node_ids or set())
    supports_development_frontage = planning_role in {"Local", "Arterial"}

    def node_position(name: str) -> dict[str, float] | None:
        value = road.get(name, {})
        if not isinstance(value, dict) or any(
            isinstance(value.get(axis), bool)
            or not isinstance(value.get(axis), (int, float))
            or not math.isfinite(float(value[axis]))
            for axis in ("x", "y", "z")
        ):
            return None
        return {
            axis: round(float(value[axis]), 3)
            for axis in ("x", "y", "z")
        }

    def node_direction(name: str, fallback_start: str, fallback_end: str) -> dict[str, float]:
        value = road.get(name, {})
        dx = float(value.get("x", 0.0))
        dz = float(value.get("z", 0.0))
        length = math.hypot(dx, dz)
        if length <= 1e-9:
            start_value = road.get(fallback_start, {})
            end_value = road.get(fallback_end, {})
            dx = float(end_value.get("x", 0.0)) - float(start_value.get("x", 0.0))
            dz = float(end_value.get("z", 0.0)) - float(start_value.get("z", 0.0))
            length = math.hypot(dx, dz)
        if length <= 1e-9:
            return {"x": 0.0, "z": 0.0}
        return {"x": round(dx / length, 6), "z": round(dz / length, 6)}

    return {
        "segmentId": int(road["id"]),
        "prefab": road.get("prefab", ""),
        "name": road.get("name", ""),
        "planningRole": planning_role,
        "supportsDevelopmentFrontage": supports_development_frontage,
        "structureType": structure_type(road.get("prefab", "")),
        "directionality": directionality,
        "isBidirectional": bidirectional,
        "isOneWay": one_way,
        "startNodeId": int(road.get("startNodeId", 0)),
        "endNodeId": int(road.get("endNodeId", 0)),
        "startNodePosition": node_position("start"),
        "endNodePosition": node_position("end"),
        "startNodeDegree": int(road.get("startNodeDegree", 0)),
        "endNodeDegree": int(road.get("endNodeDegree", 0)),
        "startNodeDirection": node_direction("startDirection", "start", "end"),
        "endNodeDirection": node_direction("endDirection", "end", "start"),
        "curveSource": (
            "nativeGenerateBezier"
            if road.get("curve", {}).get("source") == "nativeGenerateBezier"
            else "quadraticThroughReportedStartMiddleEnd"
        ),
        "verticalBounds": {
            "minY": round(min(float(point["y"]) for point in points), 3),
            "maxY": round(max(float(point["y"]) for point in points), 3),
        },
        "terrainRelativeBounds": {
            "min": None if not relative_heights else round(min(relative_heights), 3),
            "max": None if not relative_heights else round(max(relative_heights), 3),
        },
        "samples": annotated,
    }


def build_layered_roads(
    snapshot: dict[str, Any],
    segment_ids: Iterable[int] | None = None,
    sample_count: int = DEFAULT_SAMPLE_COUNT,
) -> list[dict[str, Any]]:
    selected = None if segment_ids is None else {int(value) for value in segment_ids}
    terrain = TerrainSampler(snapshot)
    templates = snapshot.get("roadTemplates", {})
    roads = [
        road
        for road in snapshot.get("networks", {}).get("segments", [])
        if road.get("service") == "Road" and (selected is None or int(road["id"]) in selected)
    ]
    all_roads = [
        road
        for road in snapshot.get("networks", {}).get("segments", [])
        if road.get("service") == "Road"
    ]
    highway_node_ids = {
        int(road.get(node_key, 0))
        for road in all_roads
        if "highway" in str(road.get("prefab", "")).lower()
        for node_key in ("startNodeId", "endNodeId")
        if int(road.get(node_key, 0)) > 0
    }
    return [
        build_road_record(
            road,
            templates,
            terrain,
            sample_count,
            highway_node_ids,
        )
        for road in roads
    ]


def _fallback_building_spatial(building: dict[str, Any]) -> dict[str, Any]:
    position = building["position"]
    angle = math.radians(float(building.get("angleDegrees", 0.0)))
    half_width = float(building.get("width", 0)) * 4.0
    half_length = float(building.get("length", 0)) * 4.0
    # the game's lot frame (Building.CalculatePosition), as the Bridge's spatial3d reports it
    axis_x = {"x": math.cos(angle), "z": math.sin(angle)}
    axis_z = {"x": -math.sin(angle), "z": math.cos(angle)}
    corners = []
    for width_sign, length_sign in ((-1, -1), (-1, 1), (1, 1), (1, -1)):
        corners.append(
            {
                "x": float(position["x"])
                + axis_x["x"] * half_width * width_sign
                + axis_z["x"] * half_length * length_sign,
                "z": float(position["z"])
                + axis_x["z"] * half_width * width_sign
                + axis_z["z"] * half_length * length_sign,
            }
        )
    base_y = float(position.get("y", 0.0))
    return {
        "source": "legacyFootprintOnly",
        "widthMeters": half_width * 2.0,
        "lengthMeters": half_length * 2.0,
        "modelHeight": None,
        "collisionHeight": None,
        "verticalBounds": {"minY": base_y, "maxY": base_y},
        "axisX": axis_x,
        "axisZ": axis_z,
        "footprintCorners": corners,
    }


def build_building_record(building: dict[str, Any], source: str) -> dict[str, Any]:
    spatial = building.get("spatial3d") or _fallback_building_spatial(building)
    vertical = spatial["verticalBounds"]
    return {
        "buildingId": int(building["id"]),
        "prefab": building.get("prefab", ""),
        "displayName": building.get("displayName", ""),
        "service": building.get("service", ""),
        "subService": building.get("subService", ""),
        "flags": building.get("flags", ""),
        "active": bool(building.get("active", False)),
        "sourceCollection": source,
        "position": building.get("position", {}),
        "angleDegrees": float(building.get("angleDegrees", 0.0)),
        "spatialSource": spatial.get("source", ""),
        "heightKnown": spatial.get("source") == "nativeBuildingInfo",
        "widthMeters": float(spatial.get("widthMeters", 0.0)),
        "lengthMeters": float(spatial.get("lengthMeters", 0.0)),
        "modelHeight": spatial.get("modelHeight"),
        "collisionHeight": spatial.get("collisionHeight"),
        "verticalBounds": {
            "minY": round(float(vertical["minY"]), 3),
            "maxY": round(float(vertical["maxY"]), 3),
        },
        "axisX": spatial["axisX"],
        "axisZ": spatial["axisZ"],
        "footprintCorners": spatial["footprintCorners"],
    }


def build_layered_buildings(
    snapshot: dict[str, Any],
    building_ids: Iterable[int] | None = None,
    service: str = "",
    include_original: bool = True,
    include_road_structures: bool = True,
    include_network_helpers: bool = True,
) -> list[dict[str, Any]]:
    selected = None if building_ids is None else {int(value) for value in building_ids}
    service_filter = service.strip().lower()
    combined: dict[int, tuple[dict[str, Any], str]] = {}
    for collection_name, key in (("facilities", "facilities"), ("growables", "growables")):
        for building in snapshot.get(collection_name, {}).get(key, []):
            building_id = int(building["id"])
            if selected is not None and building_id not in selected:
                continue
            if service_filter and str(building.get("service", "")).lower() != service_filter:
                continue
            if not include_original and "Original" in str(building.get("flags", "")):
                continue
            if not include_road_structures and str(building.get("service", "")) == "Road":
                continue
            if not include_network_helpers and str(building.get("prefab", "")) in {
                "Water Pipe Junction",
                "Heating Pipe Junction",
            }:
                continue
            combined.setdefault(building_id, (building, collection_name))
    return [
        build_building_record(building, source)
        for _, (building, source) in sorted(combined.items())
    ]


def _distance_xz(first: dict[str, Any], second: dict[str, Any]) -> float:
    return math.hypot(
        float(first.get("x", 0.0)) - float(second.get("x", 0.0)),
        float(first.get("z", 0.0)) - float(second.get("z", 0.0)),
    )


def _distance_to_segment_xz(
    point: dict[str, Any],
    start: dict[str, Any],
    end: dict[str, Any],
) -> float:
    px = float(point.get("x", 0.0))
    pz = float(point.get("z", 0.0))
    ax = float(start.get("x", 0.0))
    az = float(start.get("z", 0.0))
    bx = float(end.get("x", 0.0))
    bz = float(end.get("z", 0.0))
    dx = bx - ax
    dz = bz - az
    length_squared = dx * dx + dz * dz
    if length_squared <= 1e-9:
        return math.hypot(px - ax, pz - az)
    t = max(0.0, min(1.0, ((px - ax) * dx + (pz - az) * dz) / length_squared))
    return math.hypot(px - (ax + t * dx), pz - (az + t * dz))


def _distance_to_polyline_xz(point: dict[str, Any], samples: list[dict[str, Any]]) -> float:
    if not samples:
        return math.inf
    if len(samples) == 1:
        return _distance_xz(point, samples[0])
    return min(
        _distance_to_segment_xz(point, samples[index - 1], samples[index])
        for index in range(1, len(samples))
    )


def _distance_to_polygon_xz(point: dict[str, Any], corners: list[dict[str, Any]]) -> float:
    if len(corners) < 3:
        return math.inf
    px = float(point.get("x", 0.0))
    pz = float(point.get("z", 0.0))
    inside = False
    previous = corners[-1]
    for current in corners:
        ax = float(previous.get("x", 0.0))
        az = float(previous.get("z", 0.0))
        bx = float(current.get("x", 0.0))
        bz = float(current.get("z", 0.0))
        if (az > pz) != (bz > pz):
            crossing_x = (bx - ax) * (pz - az) / (bz - az) + ax
            if px < crossing_x:
                inside = not inside
        previous = current
    if inside:
        return 0.0
    return min(
        _distance_to_segment_xz(point, corners[index - 1], corners[index])
        for index in range(len(corners))
    )


def _local_map_cells(snapshot: dict[str, Any], center: dict[str, Any], radius: float) -> list[dict[str, Any]]:
    by_position: dict[tuple[float, float], dict[str, Any]] = {}
    for tile_index, tile in enumerate(snapshot.get("tileAnalyses", [])):
        for cell in tile.get("map", {}).get("cells", []):
            position = cell.get("position", {})
            if _distance_xz(position, center) <= radius:
                item = dict(cell)
                item["_sourceTileIndex"] = tile_index
                item["_sourceTileBounds"] = tile.get("bounds", {})
                key = (round(float(position.get("x", 0.0)), 3), round(float(position.get("z", 0.0)), 3))
                previous = by_position.get(key)
                quality = (
                    int(bool(item.get("unlocked", False))),
                    int(bool(item.get("buildable", False))),
                )
                previous_quality = (
                    -1
                    if previous is None
                    else int(bool(previous.get("unlocked", False))),
                    -1
                    if previous is None
                    else int(bool(previous.get("buildable", False))),
                )
                if previous is None or quality > previous_quality:
                    by_position[key] = item
    return list(by_position.values())


def _local_utility_samples(
    snapshot: dict[str, Any],
    center: dict[str, Any],
    radius: float,
) -> list[dict[str, Any]]:
    by_position: dict[tuple[float, float], dict[str, Any]] = {}
    for tile in snapshot.get("tileAnalyses", []):
        for sample in tile.get("utilities", {}).get("samples", []):
            position = sample.get("position", {})
            if _distance_xz(position, center) <= radius:
                key = (round(float(position.get("x", 0.0)), 3), round(float(position.get("z", 0.0)), 3))
                previous = by_position.get(key)
                coverage = sum(bool(sample.get(name, False)) for name in ("electricity", "water", "sewage"))
                previous_coverage = (
                    -1
                    if previous is None
                    else sum(bool(previous.get(name, False)) for name in ("electricity", "water", "sewage"))
                )
                if previous is None or coverage > previous_coverage:
                    by_position[key] = sample
    return list(by_position.values())


def _nearest_utility_sample(
    samples: list[dict[str, Any]],
    point: dict[str, Any],
) -> dict[str, Any] | None:
    if not samples:
        return None
    return min(samples, key=lambda sample: _distance_xz(sample.get("position", {}), point))


def _count_by(items: Iterable[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        value = str(item.get(key, "") or "Unknown")
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _resource_summary(cells: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = {}
    for resource in ("ore", "oil", "forest", "fertility", "pollution"):
        values = [float(cell.get("resources", {}).get(resource, 0.0)) for cell in cells]
        result[resource] = {
            "average": 0.0 if not values else round(sum(values) / len(values), 3),
            "maximum": 0.0 if not values else round(max(values), 3),
        }
    return result


def _road_length(road: dict[str, Any]) -> float:
    samples = road.get("samples", [])
    return sum(
        math.sqrt(
            (float(samples[index]["x"]) - float(samples[index - 1]["x"])) ** 2
            + (float(samples[index]["y"]) - float(samples[index - 1]["y"])) ** 2
            + (float(samples[index]["z"]) - float(samples[index - 1]["z"])) ** 2
        )
        for index in range(1, len(samples))
    )


def _network_access_features(
    snapshot: dict[str, Any],
    center: dict[str, Any],
    radius: float,
    services: Iterable[str],
    sample_count: int,
    limit: int,
) -> dict[str, Any]:
    requested = set(services)
    by_service: dict[str, list[dict[str, Any]]] = {service: [] for service in requested}
    for segment in snapshot.get("networks", {}).get("segments", []):
        service = str(segment.get("service", ""))
        if service not in requested:
            continue
        samples = sample_road(segment, sample_count)
        distance = _distance_to_polyline_xz(center, samples)
        if distance > radius:
            continue
        by_service[service].append(
            {
                "segmentId": int(segment.get("id", 0)),
                "prefab": segment.get("prefab", ""),
                "distanceToCenter": round(distance, 3),
                "startNodeId": int(segment.get("startNodeId", 0)),
                "endNodeId": int(segment.get("endNodeId", 0)),
                "_samples": samples,
            }
        )
    result: dict[str, Any] = {}
    for service in sorted(requested):
        features = by_service.get(service, [])
        features.sort(key=lambda item: (item["distanceToCenter"], item["segmentId"]))
        result[service] = {
            "count": len(features),
            "nearest": [
                {key: value for key, value in feature.items() if key != "_samples"}
                for feature in features[:limit]
            ],
            "_features": features,
        }
    return result


def _candidate_suitability(
    cell: dict[str, Any],
    intended_use: str,
    nearest_road_distance: float,
    nearest_road_prefab: str,
    nearest_road_role: str,
    road_supports_frontage: bool,
    nearest_building_clearance: float,
    nearest_residential_clearance: float,
    nearest_industrial_clearance: float,
    utility_sample: dict[str, Any] | None,
) -> tuple[float, list[str], list[str]]:
    blockers: list[str] = []
    cautions: list[str] = []
    if not bool(cell.get("unlocked", False)):
        blockers.append("lockedTerrain")
    if not bool(cell.get("buildable", False)):
        blockers.append("notBuildable")
    if bool(cell.get("water", {}).get("present", False)):
        blockers.append("water")
    if nearest_building_clearance < 8.0:
        blockers.append("buildingFootprintOrClearance")
    if nearest_road_distance < 12.0:
        blockers.append("roadCenterlineClearance")
    if nearest_road_distance > 128.0:
        blockers.append("noNearbyRoad")

    slope = float(cell.get("slope", 0.0))
    pollution = float(cell.get("resources", {}).get("pollution", 0.0))
    score = 100.0 - min(40.0, slope / 0.35 * 30.0)
    if intended_use in {"Residential", "Mixed"}:
        score -= pollution * 0.28
        if pollution > 32:
            cautions.append("groundPollutionExposure")
        if nearest_industrial_clearance < INDUSTRIAL_RESIDENTIAL_MIN_CLEARANCE_METERS:
            blockers.append("industrialPollutionSeparation")
        elif nearest_industrial_clearance < INDUSTRIAL_RESIDENTIAL_CAUTION_CLEARANCE_METERS:
            cautions.append("nearIndustrialForPollution")
    elif intended_use in {"Commercial", "Service"}:
        score -= pollution * 0.1
    if intended_use in {"Industrial", "SpecializedIndustry"}:
        if nearest_residential_clearance < INDUSTRIAL_RESIDENTIAL_MIN_CLEARANCE_METERS:
            blockers.append("residentialPollutionSeparation")
        elif nearest_residential_clearance < INDUSTRIAL_RESIDENTIAL_CAUTION_CLEARANCE_METERS:
            cautions.append("nearResidentialForPollution")
    if intended_use == "SpecializedIndustry":
        resources = cell.get("resources", {})
        local_resource = max(
            float(resources.get("ore", 0.0)),
            float(resources.get("oil", 0.0)),
            float(resources.get("forest", 0.0)),
            float(resources.get("fertility", 0.0)),
        )
        score += min(25.0, local_resource / 255.0 * 25.0)
        if local_resource < 32:
            blockers.append("noMatchingResourceEvidence")

    score -= min(30.0, abs(nearest_road_distance - 40.0) / 4.0)
    if not road_supports_frontage and intended_use != "Road":
        if nearest_road_role == "Highway":
            cautions.append("nearestRoadDoesNotGenerateZoning")
        elif nearest_road_role == "Gateway":
            cautions.append("gatewayRoadIsNotDevelopmentFrontage")
        else:
            cautions.append("nearestRoadIsNotDevelopmentFrontage")
        score -= 20.0
    if utility_sample is None:
        cautions.append("utilityTelemetryMissing")
        score -= 12.0
    else:
        required = ("electricity", "water", "sewage")
        missing = [name for name in required if not bool(utility_sample.get(name, False))]
        if missing:
            cautions.extend("missing" + name.title() for name in missing)
            score -= 5.0 * len(missing)

    if blockers:
        score -= 100.0
    return round(max(0.0, min(100.0, score)), 3), blockers, sorted(set(cautions))


def _nearest_building_clearance_by_service(
    point: dict[str, Any],
    buildings: list[dict[str, Any]],
) -> dict[str, float]:
    clearances: dict[str, float] = {}
    for building in buildings:
        service = str(building.get("service", "")).strip() or "Unknown"
        clearance = _distance_to_polygon_xz(point, building.get("footprintCorners", []))
        clearances[service] = min(clearance, clearances.get(service, math.inf))
    return clearances


def _actionable_block_association_radius(block: dict[str, Any]) -> float:
    rows = max(1, min(8, int(block.get("rows", 8))))
    half_width = ZONE_BLOCK_COLUMNS * ZONE_CELL_SIZE_METERS * 0.5
    half_length = rows * ZONE_CELL_SIZE_METERS * 0.5
    return math.hypot(half_width, half_length) + ZONE_CELL_SIZE_METERS


def _site_matches_actionable_block(
    site: dict[str, Any],
    block: dict[str, Any],
    road_segment_id: Any,
) -> bool:
    center = block.get("center")
    if not isinstance(center, dict):
        return False
    return (
        block.get("roadSegmentId") == road_segment_id
        and block.get("developmentFrontageSafe") is True
        and _distance_xz(site.get("position", {}), center)
        <= _actionable_block_association_radius(block)
    )


def _actionable_frontage_for_site(
    site: dict[str, Any],
    blocks: list[dict[str, Any]],
    primary_use: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str | None]:
    road_segment_id = site.get("nearestRoad", {}).get("segmentId")
    candidate = next(
        (
            block
            for block in blocks
            if _site_matches_actionable_block(site, block, road_segment_id)
        ),
        None,
    )
    if (
        candidate is not None
        and primary_use in {"Industrial", "SpecializedIndustry"}
        and candidate.get("planningEvidence", {}).get("industrialReuseEligible") is False
    ):
        return candidate, None, "residentialPollutionSeparation"
    if (
        candidate is not None
        and primary_use == "Residential"
        and candidate.get("planningEvidence", {}).get("residentialReuseEligible") is False
    ):
        return candidate, None, "industrialPollutionSeparation"
    return candidate, candidate, None


def annotate_actionable_zone_block_clearance(
    blocks: list[dict[str, Any]],
    snapshot: dict[str, Any],
) -> list[dict[str, Any]]:
    """Attach conservative block-wide residential/industrial separation evidence."""
    residential_buildings = [
        building
        for building in build_layered_buildings(
            snapshot,
            service="Residential",
            include_original=False,
            include_road_structures=False,
            include_network_helpers=False,
        )
    ]
    industrial_buildings = [
        building
        for building in build_layered_buildings(
            snapshot,
            service="Industrial",
            include_original=False,
            include_road_structures=False,
            include_network_helpers=False,
        )
    ]
    annotated = []
    for block in blocks:
        item = dict(block)
        center = block.get("center")
        association_radius = _actionable_block_association_radius(block)
        residential_center_clearance = (
            math.inf
            if not isinstance(center, dict)
            else min(
                (
                    _distance_to_polygon_xz(center, building.get("footprintCorners", []))
                    for building in residential_buildings
                ),
                default=math.inf,
            )
        )
        industrial_center_clearance = (
            math.inf
            if not isinstance(center, dict)
            else min(
                (
                    _distance_to_polygon_xz(center, building.get("footprintCorners", []))
                    for building in industrial_buildings
                ),
                default=math.inf,
            )
        )
        residential_lower_bound = max(
            0.0, residential_center_clearance - association_radius
        )
        industrial_lower_bound = max(
            0.0, industrial_center_clearance - association_radius
        )
        item["planningEvidence"] = {
            "blockAssociationRadius": round(association_radius, 3),
            "nearestResidentialBuildingClearanceLowerBound": (
                None
                if math.isinf(residential_center_clearance)
                else round(residential_lower_bound, 3)
            ),
            "industrialReuseEligible": (
                math.isinf(residential_center_clearance)
                or residential_lower_bound
                >= INDUSTRIAL_RESIDENTIAL_MIN_CLEARANCE_METERS
            ),
            "nearestIndustrialBuildingClearanceLowerBound": (
                None
                if math.isinf(industrial_center_clearance)
                else round(industrial_lower_bound, 3)
            ),
            "residentialReuseEligible": (
                math.isinf(industrial_center_clearance)
                or industrial_lower_bound
                >= INDUSTRIAL_RESIDENTIAL_MIN_CLEARANCE_METERS
            ),
            "industrialResidentialMinimumClearance": (
                INDUSTRIAL_RESIDENTIAL_MIN_CLEARANCE_METERS
            ),
            "method": "blockCenterFootprintClearanceMinusAssociationRadius",
        }
        annotated.append(item)
    return annotated


def _site_blocked_for_primary_use(site: dict[str, Any], primary_use: str) -> bool:
    if site.get("hardBlocked", True):
        return True
    if primary_use in {"Industrial", "SpecializedIndustry"}:
        clearance = site.get("nearestResidentialBuildingClearance")
    elif primary_use == "Residential":
        clearance = site.get("nearestIndustrialBuildingClearance")
    else:
        return False
    return (
        clearance is not None
        and float(clearance) < INDUSTRIAL_RESIDENTIAL_MIN_CLEARANCE_METERS
    )


def generate_local_development_road_candidates(
    snapshot: dict[str, Any],
    plans: list[dict[str, Any]],
    road_prefab: str = "",
    lengths: Iterable[float] = (64.0, 96.0, 128.0, 160.0),
    limit: int = 12,
) -> dict[str, Any]:
    """Derive bounded local streets from reusable native nodes and plan anchors."""
    # A planning call can return up to ten plans and each plan currently emits
    # four length variants.  Keep generation bounded without letting static
    # rejections consume the separate native-preview budget.
    limit = max(0, min(40, int(limit)))
    roads = {
        road["segmentId"]: road
        for road in build_layered_roads(snapshot, sample_count=65)
    }
    incident_directions: dict[int, list[dict[str, Any]]] = {}
    for road in roads.values():
        for endpoint in ("start", "end"):
            node_id = int(road.get(f"{endpoint}NodeId", 0))
            direction = road.get(f"{endpoint}NodeDirection", {})
            if node_id <= 0 or math.hypot(
                float(direction.get("x", 0.0)), float(direction.get("z", 0.0))
            ) <= 1e-9:
                continue
            incident_directions.setdefault(node_id, []).append(
                {"segmentId": road["segmentId"], "direction": direction}
            )
    buildings = build_layered_buildings(
        snapshot,
        include_original=False,
        include_road_structures=False,
        include_network_helpers=False,
    )
    residential = [
        building for building in buildings if building.get("service") == "Residential"
    ]
    map_cells = [
        cell
        for tile in snapshot.get("tileAnalyses", [])
        for cell in tile.get("map", {}).get("cells", [])
    ]
    captured_bounds = [
        tile.get("bounds", {})
        for tile in snapshot.get("tileAnalyses", [])
        if tile.get("bounds")
    ]
    normalized_lengths = sorted(
        {
            round(float(length), 3)
            for length in lengths
            if not isinstance(length, bool)
            and isinstance(length, (int, float))
            and math.isfinite(length)
            and 32.0 <= float(length) <= 256.0
        }
    )
    candidates = []
    rejection_counts: dict[str, int] = {}
    for plan in plans:
        road_access = plan.get("components", {}).get("roadAccess", {})
        if road_access.get("strategy") not in {
            "previewLocalStreetForDevelopmentFrontage",
            "previewLocalStreetFromGateway",
        }:
            continue
        source = roads.get(road_access.get("segmentId"))
        if source is None:
            rejection_counts["sourceRoadMissing"] = rejection_counts.get("sourceRoadMissing", 0) + 1
            continue
        anchor = plan.get("planningAnchor", {})
        endpoints = []
        for endpoint in ("start", "end"):
            node_id = int(source.get(f"{endpoint}NodeId", 0))
            position = source.get(f"{endpoint}NodePosition", {})
            if not isinstance(position, dict):
                continue
            distance = _distance_xz(anchor, position)
            if node_id > 0 and math.isfinite(distance):
                endpoints.append(
                    {
                        "nodeId": node_id,
                        "nodeDegree": int(source.get(f"{endpoint}NodeDegree", 0)),
                        "position": position,
                        "anchorDistance": distance,
                    }
                )
        endpoints.sort(key=lambda item: (item["anchorDistance"], item["nodeId"]))
        if not endpoints:
            rejection_counts["sourceRoadEndpointMissing"] = (
                rejection_counts.get("sourceRoadEndpointMissing", 0) + 1
            )
            continue
        for endpoint in endpoints:
            start = endpoint["position"]
            anchor_distance = endpoint["anchorDistance"]
            if anchor_distance <= 1e-6:
                rejection_counts["anchorCoincidesWithSourceNode"] = (
                    rejection_counts.get("anchorCoincidesWithSourceNode", 0) + 1
                )
                continue
            direction = {
                "x": (float(anchor.get("x", 0.0)) - float(start["x"])) / anchor_distance,
                "z": (float(anchor.get("z", 0.0)) - float(start["z"])) / anchor_distance,
            }
            incident_angles = []
            for incident in incident_directions.get(endpoint["nodeId"], []):
                incident_direction = incident["direction"]
                dot = max(
                    -1.0,
                    min(
                        1.0,
                        direction["x"] * float(incident_direction.get("x", 0.0))
                        + direction["z"] * float(incident_direction.get("z", 0.0)),
                    ),
                )
                incident_angles.append(
                    {
                        "segmentId": incident["segmentId"],
                        "angleDegrees": round(math.degrees(math.acos(dot)), 3),
                    }
                )
            minimum_incident_angle = min(
                (
                    min(item["angleDegrees"], 180.0 - item["angleDegrees"])
                    for item in incident_angles
                ),
                default=180.0,
            )
            minimum_length = max(64.0, anchor_distance + 32.0)
            for length in normalized_lengths:
                if len(candidates) >= limit:
                    break
                if length + 1e-6 < minimum_length:
                    continue
                end = {
                    "x": float(start["x"]) + direction["x"] * length,
                    "z": float(start["z"]) + direction["z"] * length,
                }
                nearest_cell = min(
                    map_cells,
                    key=lambda cell: _distance_xz(end, cell.get("position", {})),
                    default=None,
                )
                blockers = []
                if minimum_incident_angle + 1e-6 < LOCAL_ROAD_MINIMUM_JUNCTION_ANGLE_DEGREES:
                    blockers.append("junctionBranchAngle")
                endpoint_in_capture = any(
                    float(bounds.get("minX", math.inf)) <= end["x"]
                    <= float(bounds.get("maxX", -math.inf))
                    and float(bounds.get("minZ", math.inf)) <= end["z"]
                    <= float(bounds.get("maxZ", -math.inf))
                    for bounds in captured_bounds
                )
                if not endpoint_in_capture:
                    blockers.append("endpointOutsideCapturedUnlockedTiles")
                if nearest_cell is None:
                    blockers.append("endpointTerrainEvidenceMissing")
                else:
                    if not nearest_cell.get("unlocked", False):
                        blockers.append("endpointLockedTerrain")
                    if not nearest_cell.get("buildable", False):
                        blockers.append("endpointNotBuildable")
                    if nearest_cell.get("water", {}).get("present", False):
                        blockers.append("endpointInWater")
                residential_clearance = min(
                    (
                        _distance_to_polygon_xz(end, building.get("footprintCorners", []))
                        for building in residential
                    ),
                    default=math.inf,
                )
                if (
                    plan.get("primaryUse") in {"Industrial", "SpecializedIndustry"}
                    and residential_clearance
                    < INDUSTRIAL_ROAD_ENDPOINT_MIN_RESIDENTIAL_CLEARANCE_METERS
                ):
                    blockers.append("industrialEndpointResidentialSeparation")
                candidate = {
                    "planId": plan.get("planId"),
                    "candidateId": (
                        f"{plan.get('planId')}:{source['segmentId']}:"
                        f"{endpoint['nodeId']}:{round(length, 3):g}m"
                    ),
                    "sourceRoadSegmentId": source["segmentId"],
                    "sourceRoadPrefab": source.get("prefab", ""),
                    "sourceNodeId": endpoint["nodeId"],
                    "sourceNodeDegree": endpoint["nodeDegree"],
                    "roadPrefab": road_prefab or source.get("prefab", ""),
                    "start": {axis: round(float(start[axis]), 3) for axis in ("x", "z")},
                    "end": {axis: round(end[axis], 3) for axis in ("x", "z")},
                    "length": round(length, 3),
                    "planningAnchor": anchor,
                    "anchorDistanceFromSourceNode": round(anchor_distance, 3),
                    "branchDirection": {
                        axis: round(direction[axis], 6) for axis in ("x", "z")
                    },
                    "incidentAngles": incident_angles,
                    "minimumIncidentAngleDegrees": round(minimum_incident_angle, 3),
                    "endpointResidentialBuildingClearance": (
                        None if math.isinf(residential_clearance) else round(residential_clearance, 3)
                    ),
                    "staticEligible": not blockers,
                    "staticBlockers": sorted(set(blockers)),
                    "derivation": "existingSourceRoadEndpointTowardPlanningAnchor",
                }
                candidates.append(candidate)
                for reason in candidate["staticBlockers"]:
                    rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
            if len(candidates) >= limit:
                break
        if len(candidates) >= limit:
            break
    return {
        "planningOnly": True,
        "generated": len(candidates),
        "staticallyEligible": sum(candidate["staticEligible"] for candidate in candidates),
        "rejectionCounts": dict(sorted(rejection_counts.items())),
        "candidates": candidates,
        "limits": {
            "candidateLimit": limit,
            "lengths": normalized_lengths,
            "industrialEndpointResidentialMinimumClearance": (
                INDUSTRIAL_ROAD_ENDPOINT_MIN_RESIDENTIAL_CLEARANCE_METERS
            ),
            "minimumJunctionAngleDegrees": LOCAL_ROAD_MINIMUM_JUNCTION_ANGLE_DEGREES,
        },
    }


def inspect_planning_area(
    snapshot: dict[str, Any],
    center: dict[str, Any],
    radius: float,
    intended_use: str = "Mixed",
    sample_count: int = DEFAULT_SAMPLE_COUNT,
    max_features: int = 20,
    candidate_limit: int = 12,
) -> dict[str, Any]:
    """Return a compact, planning-only neighborhood derived from exact snapshot evidence."""
    radius = max(32.0, float(radius))
    max_features = max(0, min(100, int(max_features)))
    candidate_limit = max(0, min(100, int(candidate_limit)))
    roads = build_layered_roads(snapshot, sample_count=sample_count)
    buildings = build_layered_buildings(
        snapshot,
        include_original=False,
        include_road_structures=False,
        include_network_helpers=False,
    )
    local_roads = [
        road
        for road in roads
        if _distance_to_polyline_xz(center, road.get("samples", [])) <= radius
    ]
    local_buildings = [
        building
        for building in buildings
        if _distance_xz(building.get("position", {}), center)
        <= radius + 0.5 * math.hypot(building["widthMeters"], building["lengthMeters"])
    ]
    cells = _local_map_cells(snapshot, center, radius)
    utility_samples = _local_utility_samples(snapshot, center, radius)
    utility_networks = _network_access_features(
        snapshot,
        center,
        radius,
        ("Water", "Electricity"),
        sample_count,
        max_features,
    )
    facility_items = [
        item
        for item in snapshot.get("facilities", {}).get("facilities", [])
        if _distance_xz(item.get("position", {}), center) <= radius
    ]
    growable_items = [
        item
        for item in snapshot.get("growables", {}).get("growables", [])
        if _distance_xz(item.get("position", {}), center) <= radius
    ]

    road_features = []
    for road in local_roads:
        distance = _distance_to_polyline_xz(center, road.get("samples", []))
        road_features.append(
            {
                "segmentId": road["segmentId"],
                "prefab": road["prefab"],
                "name": road["name"],
                "planningRole": road["planningRole"],
                "supportsDevelopmentFrontage": road["supportsDevelopmentFrontage"],
                "structureType": road["structureType"],
                "directionality": road["directionality"],
                "distanceToCenter": round(distance, 3),
                "approximateLength": round(_road_length(road), 3),
                "startNodeId": road["startNodeId"],
                "endNodeId": road["endNodeId"],
                "verticalBounds": road["verticalBounds"],
            }
        )
    road_features.sort(key=lambda item: (item["distanceToCenter"], item["segmentId"]))

    candidate_sites = []
    for cell in cells:
        point = cell.get("position", {})
        if local_roads:
            nearest_road = min(
                local_roads,
                key=lambda road: _distance_to_polyline_xz(point, road.get("samples", [])),
            )
            road_distance = _distance_to_polyline_xz(point, nearest_road.get("samples", []))
            road_id = nearest_road["segmentId"]
            road_prefab = nearest_road["prefab"]
            road_name = nearest_road["name"]
            road_role = nearest_road["planningRole"]
            road_supports_frontage = nearest_road["supportsDevelopmentFrontage"]
        else:
            road_distance = math.inf
            road_id = None
            road_prefab = ""
            road_name = ""
            road_role = "None"
            road_supports_frontage = False
        clearance_by_service = _nearest_building_clearance_by_service(point, local_buildings)
        building_clearance = min(clearance_by_service.values(), default=math.inf)
        residential_clearance = clearance_by_service.get("Residential", math.inf)
        industrial_clearance = clearance_by_service.get("Industrial", math.inf)
        utility = _nearest_utility_sample(utility_samples, point)
        nearest_networks = {}
        for service in ("Water", "Electricity"):
            features = utility_networks.get(service, {}).get("_features", [])
            if not features:
                nearest_networks[service] = None
                continue
            nearest = min(
                features,
                key=lambda feature: _distance_to_polyline_xz(point, feature["_samples"]),
            )
            nearest_networks[service] = {
                "segmentId": nearest["segmentId"],
                "prefab": nearest["prefab"],
                "distance": round(_distance_to_polyline_xz(point, nearest["_samples"]), 3),
            }
        score, blockers, cautions = _candidate_suitability(
            cell,
            intended_use,
            road_distance,
            road_prefab,
            road_role,
            road_supports_frontage,
            building_clearance,
            residential_clearance,
            industrial_clearance,
            utility,
        )
        candidate_sites.append(
            {
                "sourceSample": {
                    "tileIndex": int(cell.get("_sourceTileIndex", 0)),
                    "tileBounds": cell.get("_sourceTileBounds", {}),
                    "row": int(cell.get("row", 0)),
                    "column": int(cell.get("column", 0)),
                },
                "position": {
                    "x": round(float(point.get("x", 0.0)), 3),
                    "y": round(float(cell.get("height", 0.0)), 3),
                    "z": round(float(point.get("z", 0.0)), 3),
                },
                "slope": round(float(cell.get("slope", 0.0)), 4),
                "resources": cell.get("resources", {}),
                "nearestRoad": {
                    "segmentId": road_id,
                    "prefab": road_prefab,
                    "name": road_name,
                    "planningRole": road_role,
                    "supportsZoning": bool(road_prefab) and road_supports_frontage,
                    "distance": None if math.isinf(road_distance) else round(road_distance, 3),
                },
                "nearestBuildingClearance": (
                    None if math.isinf(building_clearance) else round(building_clearance, 3)
                ),
                "nearestBuildingClearanceByService": {
                    service: round(clearance, 3)
                    for service, clearance in sorted(clearance_by_service.items())
                    if not math.isinf(clearance)
                },
                "nearestResidentialBuildingClearance": (
                    None if math.isinf(residential_clearance) else round(residential_clearance, 3)
                ),
                "nearestIndustrialBuildingClearance": (
                    None if math.isinf(industrial_clearance) else round(industrial_clearance, 3)
                ),
                "utilities": (
                    None
                    if utility is None
                    else {
                        name: bool(utility.get(name, False))
                        for name in ("electricity", "water", "sewage")
                    }
                ),
                "nearestUtilityNetworks": nearest_networks,
                "suitabilityScore": score,
                "hardBlocked": bool(blockers),
                "blockers": blockers,
                "cautions": cautions,
            }
        )
    candidate_sites.sort(
        key=lambda item: (
            item["hardBlocked"],
            -float(item["suitabilityScore"]),
            float(item["slope"]),
            item["sourceSample"]["row"],
            item["sourceSample"]["column"],
        )
    )

    terrain_heights = [float(cell.get("height", 0.0)) for cell in cells]
    terrain_slopes = [float(cell.get("slope", 0.0)) for cell in cells]
    utility_summary = {
        name: (
            0.0
            if not utility_samples
            else round(
                sum(bool(sample.get(name, False)) for sample in utility_samples)
                / len(utility_samples),
                3,
            )
        )
        for name in ("electricity", "water", "sewage")
    }
    mayor = snapshot.get("mayorSnapshot", {})
    limitations = [
        "Candidate sites are exact captured terrain samples, not approved construction coordinates.",
        "Native placement, exact zoning blocks, costs, unlocks, and live pathfinding must be refreshed before mutation.",
        "Offline traffic density and origin-destination demand are not present in this snapshot schema.",
    ]
    if "zones" not in snapshot:
        limitations.append("The snapshot predates aggregate zoning capture.")
    rejection_counts: dict[str, int] = {}
    for site in candidate_sites:
        for reason in site["blockers"]:
            rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
    return {
        "planningOnly": True,
        "center": {"x": float(center["x"]), "z": float(center["z"])},
        "radius": radius,
        "intendedUse": intended_use,
        "city": mayor.get("city", {}),
        "demand": mayor.get("summary", {}).get("demand", {}),
        "capabilities": mayor.get("progression", {}).get("capabilities", {}),
        "terrain": {
            "sampleCount": len(cells),
            "buildableRatio": (
                0.0
                if not cells
                else round(sum(bool(cell.get("buildable", False)) for cell in cells) / len(cells), 3)
            ),
            "waterSampleCount": sum(bool(cell.get("water", {}).get("present", False)) for cell in cells),
            "heightRange": {
                "min": None if not terrain_heights else round(min(terrain_heights), 3),
                "max": None if not terrain_heights else round(max(terrain_heights), 3),
            },
            "slope": {
                "average": (
                    None if not terrain_slopes else round(sum(terrain_slopes) / len(terrain_slopes), 4)
                ),
                "maximum": None if not terrain_slopes else round(max(terrain_slopes), 4),
            },
            "resources": _resource_summary(cells),
        },
        "utilities": {
            "sampleCount": len(utility_samples),
            "coverageRatio": utility_summary,
            "networks": {
                service: {
                    "count": value["count"],
                    "nearest": value["nearest"],
                }
                for service, value in utility_networks.items()
            },
        },
        "roads": {
            "count": len(local_roads),
            "countsByStructure": _count_by(local_roads, "structureType"),
            "nearest": road_features[:max_features],
        },
        "buildings": {
            "count": len(local_buildings),
            "facilityCount": len(facility_items),
            "growableCount": len(growable_items),
            "countsByService": _count_by(local_buildings, "service"),
            "countsBySubService": _count_by(local_buildings, "subService"),
        },
        "zoning": snapshot.get("zones"),
        "candidateSites": candidate_sites[:candidate_limit],
        "candidateSiteDiagnostics": {
            "generatedTerrainSamples": len(candidate_sites),
            "staticallyEligibleSamples": sum(
                not site["hardBlocked"] for site in candidate_sites
            ),
            "rankedSamplesReturned": min(candidate_limit, len(candidate_sites)),
            "rejectionCounts": dict(sorted(rejection_counts.items())),
            "nativePreviewCalls": 0,
            "scope": "offlinePlanningOnly",
        },
        "limitations": limitations,
    }


def propose_development_plans(
    context: dict[str, Any],
    intended_use: str = "Mixed",
    max_plans: int = 3,
) -> list[dict[str, Any]]:
    """Turn a compact context into ranked, non-mutating alternative development plans."""
    max_plans = max(0, min(10, int(max_plans)))
    demand = context.get("demand", {})
    capabilities = context.get("capabilities", {})
    if intended_use == "Mixed":
        use_order = [
            ("Residential", float(demand.get("residential", 0.0))),
            ("Commercial", float(demand.get("commercial", 0.0))),
            ("Industrial", float(demand.get("workplace", 0.0))),
        ]
        use_order.sort(key=lambda item: -item[1])
        primary_use = use_order[0][0] if use_order and use_order[0][1] > 0 else "Mixed"
    else:
        primary_use = intended_use
    demand_total = sum(
        max(0.0, float(demand.get(name, 0.0)))
        for name in ("residential", "commercial", "workplace")
    )
    zone_mix = {
        "ResidentialLow": (
            0.0
            if demand_total <= 0
            else round(max(0.0, float(demand.get("residential", 0.0))) / demand_total, 3)
        ),
        "CommercialLow": (
            0.0
            if demand_total <= 0
            else round(max(0.0, float(demand.get("commercial", 0.0))) / demand_total, 3)
        ),
        "Industrial": (
            0.0
            if demand_total <= 0
            else round(max(0.0, float(demand.get("workplace", 0.0))) / demand_total, 3)
        ),
    }

    plans = []
    exact_frontage = context.get("actionableZoneBlocks")
    exact_frontage_status = (
        str(exact_frontage.get("evidenceStatus", "unknown"))
        if isinstance(exact_frontage, dict)
        else "unknown"
    )
    exact_frontage_blocks = (
        exact_frontage.get("blocks", [])
        if isinstance(exact_frontage, dict)
        else []
    )
    candidate_sites = list(context.get("candidateSites", []))
    # Exact live frontage is stronger evidence than a terrain-only suitability
    # score.  Stable-partition reusable sites ahead of generic alternatives so
    # max_plans cannot hide a safe existing block behind higher-scoring samples.
    candidate_sites.sort(
        key=lambda site: (
            _actionable_frontage_for_site(
                site, exact_frontage_blocks, primary_use
            )[1]
            is None
        )
    )
    used_frontage_block_ids: set[Any] = set()
    for site in candidate_sites:
        if len(plans) >= max_plans:
            break
        if _site_blocked_for_primary_use(site, primary_use):
            continue
        utilities = site.get("utilities") or {}
        missing_utilities = [
            name for name in ("electricity", "water", "sewage") if not utilities.get(name, False)
        ]
        road_distance = site.get("nearestRoad", {}).get("distance")
        road_supports_zoning = bool(site.get("nearestRoad", {}).get("supportsZoning", False))
        road_role = str(site.get("nearestRoad", {}).get("planningRole", ""))
        road_segment_id = site.get("nearestRoad", {}).get("segmentId")
        candidate_frontage, matching_frontage, frontage_rejection_reason = (
            _actionable_frontage_for_site(site, exact_frontage_blocks, primary_use)
        )
        if matching_frontage is not None:
            block_id = matching_frontage.get("blockId")
            if block_id is not None:
                if block_id in used_frontage_block_ids:
                    continue
                used_frontage_block_ids.add(block_id)
        if road_role == "Highway":
            road_strategy = "previewConnectorFromHighwayPairToLocalRoad"
        elif road_role == "Gateway":
            road_strategy = "previewLocalStreetFromGateway"
        elif matching_frontage is not None:
            road_strategy = "reuseExistingRoadFrontage"
        elif exact_frontage_status == "liveExact":
            road_strategy = "previewLocalStreetForDevelopmentFrontage"
        elif road_supports_zoning and road_distance is not None and road_distance <= 48.0:
            road_strategy = "inspectExistingRoadFrontageBeforeReuse"
        else:
            road_strategy = "previewShortRoadExtension"
        if primary_use == "Residential":
            zone = "ResidentialLow"
            unlocked = bool(capabilities.get("lowDensityResidential", False))
        elif primary_use == "Commercial":
            zone = "CommercialLow"
            unlocked = bool(capabilities.get("commercial", False))
        elif primary_use in {"Industrial", "SpecializedIndustry"}:
            zone = "Industrial"
            unlocked = bool(capabilities.get("industry", False))
        else:
            zone = None
            unlocked = True
        blockers = [] if unlocked else ["targetZoneLocked"]
        plans.append(
            {
                "rank": len(plans) + 1,
                "planId": "development-" + str(len(plans) + 1),
                "planningOnly": True,
                "executionReady": False,
                "planningAnchor": site["position"],
                "sourceSample": site["sourceSample"],
                "primaryUse": primary_use,
                "targetZone": zone,
                "suitabilityScore": site["suitabilityScore"],
                "hardBlocked": bool(blockers),
                "blockers": blockers,
                "evidence": {
                    "slope": site["slope"],
                    "pollution": site.get("resources", {}).get("pollution", 0),
                    "nearestRoad": site["nearestRoad"],
                    "nearestBuildingClearance": site["nearestBuildingClearance"],
                    "nearestBuildingClearanceByService": site.get(
                        "nearestBuildingClearanceByService", {}
                    ),
                    "nearestResidentialBuildingClearance": site.get(
                        "nearestResidentialBuildingClearance"
                    ),
                    "nearestIndustrialBuildingClearance": site.get(
                        "nearestIndustrialBuildingClearance"
                    ),
                    "utilities": utilities,
                    "nearestUtilityNetworks": site.get("nearestUtilityNetworks", {}),
                    "demand": demand,
                    "exactFrontage": {
                        "evidenceStatus": exact_frontage_status,
                        "blockId": (
                            matching_frontage.get("blockId")
                            if matching_frontage is not None
                            else None
                        ),
                        "roadSegmentId": road_segment_id,
                        "rejectedBlockId": (
                            candidate_frontage.get("blockId")
                            if candidate_frontage is not None
                            and matching_frontage is None
                            else None
                        ),
                        "rejectionReason": frontage_rejection_reason,
                        "planningEvidence": (
                            candidate_frontage.get("planningEvidence", {})
                            if candidate_frontage is not None
                            else {}
                        ),
                    },
                },
                "components": {
                    "roadAccess": {
                        "strategy": road_strategy,
                        "segmentId": road_segment_id,
                        "verifiedBlockId": (
                            matching_frontage.get("blockId")
                            if matching_frontage is not None
                            else None
                        ),
                    },
                    "zoning": {
                        "strategy": (
                            "revalidateReturnedExactBlockBeforeZoning"
                            if matching_frontage is not None
                            else (
                                "zoneOnlyAfterVerifiedLocalStreetCreatesExactBlocks"
                                if exact_frontage_status == "liveExact"
                                or not road_supports_zoning
                                else "inspectExactEmptyRoadGeneratedBlocksBeforeZoning"
                            )
                        ),
                        "zone": zone,
                        "demandWeightedMix": (
                            zone_mix
                            if intended_use == "Mixed"
                            else ({} if zone is None else {zone: 1.0})
                        ),
                    },
                    "utilities": {
                        "strategy": (
                            "reuseExistingCoverage"
                            if not missing_utilities
                            else "previewMinimalVerifiedExtensions"
                        ),
                        "missing": missing_utilities,
                        "nearestNetworks": site.get("nearestUtilityNetworks", {}),
                    },
                },
                "requiredValidation": [
                    "validate_progression_mode",
                    "refresh_unlocked_assets",
                    "inspect_growth_capacity",
                    "preview_building_candidate when a ploppable building is selected",
                    "preview_network for every proposed road or utility segment",
                    "create_checkpoint and capture_regression_baseline before each mutation",
                ],
                "reasonNotExecutionReady": (
                    "The offline planner ranks exact evidence-backed terrain samples, but live native "
                    "placement, exact zoning frontage, current cost, pathfinding, and utility topology "
                    "must be verified immediately before construction."
                ),
            }
        )
    return plans


def _segment_box_interval(
    start: dict[str, float],
    end: dict[str, float],
    building: dict[str, Any],
) -> tuple[float, float] | None:
    center = building["position"]
    axis_x = building["axisX"]
    axis_z = building["axisZ"]
    half_width = float(building["widthMeters"]) * 0.5
    half_length = float(building["lengthMeters"]) * 0.5

    def local(point: dict[str, float]) -> tuple[float, float]:
        dx = float(point["x"]) - float(center["x"])
        dz = float(point["z"]) - float(center["z"])
        return (
            dx * float(axis_x["x"]) + dz * float(axis_x["z"]),
            dx * float(axis_z["x"]) + dz * float(axis_z["z"]),
        )

    sx, sz = local(start)
    ex, ez = local(end)
    dx, dz = ex - sx, ez - sz
    t_min, t_max = 0.0, 1.0
    for p, q in (
        (-dx, sx + half_width),
        (dx, half_width - sx),
        (-dz, sz + half_length),
        (dz, half_length - sz),
    ):
        if abs(p) < 1e-9:
            if q < 0.0:
                return None
            continue
        t = q / p
        if p < 0.0:
            if t > t_max:
                return None
            t_min = max(t_min, t)
        else:
            if t < t_min:
                return None
            t_max = min(t_max, t)
    return t_min, t_max


def segment_intersects_building_footprint(
    start: dict[str, float],
    end: dict[str, float],
    building: dict[str, Any],
    clearance: float = 0.0,
) -> bool:
    """Return whether an XZ segment enters an oriented footprint plus clearance."""
    expanded = dict(building)
    margin = max(0.0, float(clearance)) * 2.0
    expanded["widthMeters"] = float(building["widthMeters"]) + margin
    expanded["lengthMeters"] = float(building["lengthMeters"]) + margin
    return _segment_box_interval(start, end, expanded) is not None


def inspect_building_road_clearance(
    buildings: list[dict[str, Any]],
    roads: list[dict[str, Any]],
    road_half_height: float = 2.0,
) -> list[dict[str, Any]]:
    found: dict[tuple[int, int], dict[str, Any]] = {}
    road_entries = []
    for road in roads:
        samples = road["samples"]
        road_entries.append(
            (
                road,
                samples,
                min(float(point["x"]) for point in samples),
                max(float(point["x"]) for point in samples),
                min(float(point["z"]) for point in samples),
                max(float(point["z"]) for point in samples),
            )
        )
    for building in buildings:
        if not building["heightKnown"]:
            continue
        corners = building["footprintCorners"]
        min_x = min(float(point["x"]) for point in corners)
        max_x = max(float(point["x"]) for point in corners)
        min_z = min(float(point["z"]) for point in corners)
        max_z = max(float(point["z"]) for point in corners)
        building_min_y = float(building["verticalBounds"]["minY"])
        building_max_y = float(building["verticalBounds"]["maxY"])
        for road, samples, road_min_x, road_max_x, road_min_z, road_max_z in road_entries:
            if road_max_x < min_x or road_min_x > max_x or road_max_z < min_z or road_min_z > max_z:
                continue
            pair = (int(building["buildingId"]), int(road["segmentId"]))
            for index in range(len(samples) - 1):
                start, end = samples[index], samples[index + 1]
                interval = _segment_box_interval(start, end, building)
                if interval is None:
                    continue
                y0 = float(start["y"]) + interval[0] * (float(end["y"]) - float(start["y"]))
                y1 = float(start["y"]) + interval[1] * (float(end["y"]) - float(start["y"]))
                road_min_y = min(y0, y1) - road_half_height
                road_max_y = max(y0, y1) + road_half_height
                if road_max_y < building_min_y:
                    relation = "RoadBelowBuilding"
                    clearance = building_min_y - road_max_y
                elif road_min_y > building_max_y:
                    relation = "RoadAboveBuilding"
                    clearance = road_min_y - building_max_y
                else:
                    relation = "VerticalConflict"
                    clearance = 0.0
                result = {
                    "buildingId": pair[0],
                    "buildingPrefab": building["prefab"],
                    "segmentId": pair[1],
                    "roadPrefab": road["prefab"],
                    "roadStructure": road["structureType"],
                    "relation": relation,
                    "verticalClearance": round(clearance, 3),
                    "buildingVerticalBounds": building["verticalBounds"],
                    "roadVerticalBoundsAtFootprint": {
                        "minY": round(road_min_y, 3),
                        "maxY": round(road_max_y, 3),
                    },
                }
                previous = found.get(pair)
                if previous is None or relation == "VerticalConflict" or clearance < float(previous["verticalClearance"]):
                    found[pair] = result
    return sorted(found.values(), key=lambda item: (item["relation"], item["buildingId"], item["segmentId"]))


def _intersection_2d(
    a0: dict[str, float],
    a1: dict[str, float],
    b0: dict[str, float],
    b1: dict[str, float],
) -> tuple[float, float, float, float] | None:
    adx = float(a1["x"]) - float(a0["x"])
    adz = float(a1["z"]) - float(a0["z"])
    bdx = float(b1["x"]) - float(b0["x"])
    bdz = float(b1["z"]) - float(b0["z"])
    denominator = adx * bdz - adz * bdx
    if abs(denominator) < 1e-8:
        return None
    dx = float(b0["x"]) - float(a0["x"])
    dz = float(b0["z"]) - float(a0["z"])
    ta = (dx * bdz - dz * bdx) / denominator
    tb = (dx * adz - dz * adx) / denominator
    epsilon = 1e-7
    if not (-epsilon <= ta <= 1.0 + epsilon and -epsilon <= tb <= 1.0 + epsilon):
        return None
    return (
        float(a0["x"]) + ta * adx,
        float(a0["z"]) + ta * adz,
        ta,
        tb,
    )


def inspect_crossings(
    roads: list[dict[str, Any]],
    grade_separation_threshold: float = 4.0,
    cell_size: float = 128.0,
) -> list[dict[str, Any]]:
    pieces: list[dict[str, Any]] = []
    buckets: dict[tuple[int, int], list[int]] = {}
    for road_index, road in enumerate(roads):
        samples = road["samples"]
        for piece_index in range(len(samples) - 1):
            start = samples[piece_index]
            end = samples[piece_index + 1]
            piece = {"road": road_index, "piece": piece_index, "start": start, "end": end}
            index = len(pieces)
            pieces.append(piece)
            min_x = min(float(start["x"]), float(end["x"]))
            max_x = max(float(start["x"]), float(end["x"]))
            min_z = min(float(start["z"]), float(end["z"]))
            max_z = max(float(start["z"]), float(end["z"]))
            for bx in range(math.floor(min_x / cell_size), math.floor(max_x / cell_size) + 1):
                for bz in range(math.floor(min_z / cell_size), math.floor(max_z / cell_size) + 1):
                    buckets.setdefault((bx, bz), []).append(index)

    tested: set[tuple[int, int]] = set()
    found: dict[tuple[int, int], dict[str, Any]] = {}
    for indexes in buckets.values():
        for offset, first_index in enumerate(indexes):
            for second_index in indexes[offset + 1 :]:
                key = (min(first_index, second_index), max(first_index, second_index))
                if key in tested:
                    continue
                tested.add(key)
                first = pieces[first_index]
                second = pieces[second_index]
                if first["road"] == second["road"]:
                    continue
                road_a = roads[first["road"]]
                road_b = roads[second["road"]]
                pair = tuple(sorted((int(road_a["segmentId"]), int(road_b["segmentId"]))))
                if {
                    int(road_a["startNodeId"]),
                    int(road_a["endNodeId"]),
                } & {
                    int(road_b["startNodeId"]),
                    int(road_b["endNodeId"]),
                }:
                    continue
                intersection = _intersection_2d(first["start"], first["end"], second["start"], second["end"])
                if intersection is None:
                    continue
                x, z, ta, tb = intersection
                y_a = float(first["start"]["y"]) + ta * (float(first["end"]["y"]) - float(first["start"]["y"]))
                y_b = float(second["start"]["y"]) + tb * (float(second["end"]["y"]) - float(second["start"]["y"]))
                clearance = abs(y_a - y_b)
                result = {
                    "segmentA": int(road_a["segmentId"]),
                    "segmentB": int(road_b["segmentId"]),
                    "structureA": road_a["structureType"],
                    "structureB": road_b["structureType"],
                    "position": {"x": round(x, 3), "z": round(z, 3)},
                    "heightA": round(y_a, 3),
                    "heightB": round(y_b, 3),
                    "verticalClearance": round(clearance, 3),
                    "relation": (
                        "AGradeSeparatedAboveB"
                        if clearance >= grade_separation_threshold and y_a > y_b
                        else (
                            "BGradeSeparatedAboveA"
                            if clearance >= grade_separation_threshold
                            else "UnconnectedSameGradeCrossing"
                        )
                    ),
                }
                previous = found.get(pair)
                if previous is None or clearance < float(previous["verticalClearance"]):
                    found[pair] = result
    return sorted(found.values(), key=lambda item: (item["relation"], item["segmentA"], item["segmentB"]))

"""Which uses suit a zoning block, from what surrounds it, as pure functions.

A block takes the use of its neighbourhood (contiguous districts share services and keep
industry's pollution and trucks away from homes), within three hard rules:

- homes keep INDUSTRY_HOME_GAP from industrial buildings, twice its pollution radius from a
  polluting facility (landfills, incinerators, fossil plants) and out of a noisy facility's noise
  radius (wind turbines, which pollute nothing yet keep homes awake)
- industry keeps the same gap from homes, unless it extends an industrial district, and
  WELL_CLEARANCE from a groundwater pump, whose water its ground pollution would foul
- offices (which pollute nothing) may take land homes may not for industry's nearness, outside an
  industrial district: the buffer the planning rules ask for between industry and homes
- shops go on arterial or through-road frontage (a road that runs on to junctions at both ends,
  not a dead-end branch), at the corner where a local street meets an arterial, in the
  buffer between industry and homes, or where a residential area has none within
  COMMERCIAL_SPACING

A planned area (written by the planner into the city profile) overrides the neighbourhood.
A block with no neighbours and no plan is residential when no industry is near, otherwise
industrial; an agent may plan such land instead.
"""

from __future__ import annotations

import math
from typing import Any, Iterable

INDUSTRY_POLLUTION_REACH = 240.0  # how far industry's ground pollution reached homes (live: 141-225m)
# homes and industry keep the reach their pollution was seen to travel: at 300m the land between
# them grew nothing but shops, and a town of 4,300 had no free block left for homes (live)
INDUSTRY_HOME_GAP = INDUSTRY_POLLUTION_REACH
WELL_CLEARANCE = 300.0  # industry's ground pollution off a groundwater pump, as off homes
NEIGHBOURHOOD = 250.0
COMMERCIAL_SPACING = 250.0
CORNER_RADIUS = 110.0
BUFFER_REACH = 350.0
ARTERIAL_PARTS = ("4 Lane", "Medium Road", "Large Road", "Avenue", "Six Lane", "Highway")
USES = ("residential", "commercial", "workplace")
SERVICE_USE = {"Residential": "residential", "Commercial": "commercial", "Industrial": "workplace",
               "Office": "workplace"}
ZONE_USE = {"ResidentialLow": "residential", "ResidentialHigh": "residential", "CommercialLow": "commercial",
            "CommercialHigh": "commercial", "Industrial": "workplace", "Office": "workplace"}


def zoned_land(blocks: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Zoned blocks as {"use", "position", "zone"}: land zoned this wake has nothing built on it
    yet, but homes and industry will grow there (live: homes and industry zoned 210m apart in one
    wake)."""
    return [{"use": ZONE_USE[block["zone"]], "position": block["center"], "zone": block["zone"]} for block in blocks
            if (block.get("zonedCells") or 0) > 0 and block.get("zone") in ZONE_USE]


def industry(buildings: Iterable[dict[str, Any]], zoned: Iterable[dict[str, Any]] = ()) -> list[tuple[float, float]]:
    """Where industry stands or is zoned: the workplaces whose pollution the distance rules keep
    homes from. Offices pollute nothing and are left out (live: once the buffer took offices,
    every free block lay within the gap of one and homes had no land left, at 10,800 people)."""
    return [(b["position"]["x"], b["position"]["z"]) for b in buildings if b.get("service") == "Industrial"] + \
        [(land["position"]["x"], land["position"]["z"]) for land in zoned if land.get("zone") == "Industrial"]


def is_arterial(prefab: str | None) -> bool:
    return any(part in str(prefab or "") for part in ARTERIAL_PARTS)


def through_segments(segments: Iterable[dict[str, Any]]) -> set[int]:
    """Road segments that are not on a dead-end chain: following plain two-way joins in both
    directions reaches a junction (three or more roads) each way. These carry traffic between
    places, as collectors do; a chain that ends at a dead end is a branch serving its own homes."""
    roads = {int(segment["id"]): segment for segment in segments}
    by_node: dict[int, list[int]] = {}
    for segment_id, segment in roads.items():
        for end in ("startNodeId", "endNodeId"):
            by_node.setdefault(int(segment[end]), []).append(segment_id)

    def reaches_junction(segment_id: int, node: int) -> bool:
        seen = {segment_id}
        for _ in range(200):
            others = [other for other in by_node.get(node, []) if other not in seen]
            degree = len(by_node.get(node, []))
            if degree >= 3:
                return True
            if degree <= 1 or not others:
                return False
            segment_id = others[0]
            seen.add(segment_id)
            segment = roads[segment_id]
            node = int(segment["endNodeId"]) if int(segment["startNodeId"]) == node else int(segment["startNodeId"])
        return True

    return {segment_id for segment_id, segment in roads.items()
            if reaches_junction(segment_id, int(segment["startNodeId"]))
            and reaches_junction(segment_id, int(segment["endNodeId"]))}


def neighbourhood(center: tuple[float, float], buildings: Iterable[dict[str, Any]],
                  radius: float = NEIGHBOURHOOD) -> dict[str, Any]:
    """Counts of growables by use within radius, and the nearest of each use at any distance."""
    counts = {use: 0 for use in USES}
    nearest = {use: math.inf for use in USES}
    for building in buildings:
        use = SERVICE_USE.get(str(building.get("service")))
        if use is None:
            continue
        distance = math.dist(center, (building["position"]["x"], building["position"]["z"]))
        nearest[use] = min(nearest[use], distance)
        if distance <= radius:
            counts[use] += 1
    return {"counts": counts, "nearest": nearest}


def in_area(center: tuple[float, float], area: dict[str, Any]) -> bool:
    return area["minX"] <= center[0] <= area["maxX"] and area["minZ"] <= center[1] <= area["maxZ"]


def ground_for(use: str, samples: list[dict[str, Any]], buildings: Iterable[dict[str, Any]],
               polluters: Iterable[dict[str, Any]] = (), plan: Iterable[dict[str, Any]] = (),
               zoned: Iterable[dict[str, Any]] = (),
               wells: Iterable[tuple[float, float]] = ()) -> list[dict[str, Any]]:
    """The free map samples a new district for the use may take under the hard distance rules:
    homes out of industry's and polluters' reach, new industry out of the homes' and the
    groundwater pumps' (wells) reach. Planned areas for the use keep their ground."""
    zoned = list(zoned)
    if use == "residential":
        avoid = industry(buildings, zoned)
    else:
        avoid = [(b["position"]["x"], b["position"]["z"]) for b in buildings if b.get("service") == "Residential"]
        avoid += [(land["position"]["x"], land["position"]["z"]) for land in zoned if land["use"] == "residential"]
    polluters = list(polluters)
    planned = [area for area in plan if area.get("kind") == use]
    if use not in ("residential", "workplace"):
        return samples
    taken = []
    for sample in samples:
        point = (sample["position"]["x"], sample["position"]["z"])
        if any(in_area(point, area) for area in planned):
            taken.append(sample)
            continue
        if any(math.dist(point, other) <= INDUSTRY_HOME_GAP for other in avoid):
            continue
        if use == "workplace" and any(math.dist(point, well) < WELL_CLEARANCE for well in wells):
            continue
        if use == "residential" and any(
                math.dist(point, (item["position"]["x"], item["position"]["z"]))
                < max(2 * float(item.get("pollutionRadius") or 0), float(item.get("noiseRadius") or 0))
                for item in polluters):
            continue
        taken.append(sample)
    return taken


def classify(block: dict[str, Any], buildings: list[dict[str, Any]], *,
             plan: Iterable[dict[str, Any]] = (), polluters: Iterable[dict[str, Any]] = (),
             arterial_corners: Iterable[tuple[float, float]] = (),
             commercial_blocks: Iterable[tuple[float, float]] = (),
             through_roads: set[int] | None = None, zoned: Iterable[dict[str, Any]] = (),
             wells: Iterable[tuple[float, float]] = ()) -> dict[str, Any]:
    """The uses allowed on the block, the preferred one, and why.

    buildings are growables (service, position); polluters are facilities with a ground pollution
    or noise radius ({"position", "pollutionRadius", "noiseRadius"}); arterial_corners are junctions where a local street
    meets an arterial; commercial_blocks are centres of blocks already zoned commercial; zoned is
    zoned_land, which keeps the distance rules but does not count as neighbours; wells are
    groundwater pumps."""
    center = (block["center"]["x"], block["center"]["z"])
    for area in plan:
        if area.get("kind") and in_area(center, area):  # an entry may only ask for land, not a use
            return {"allowed": [area["kind"]], "preferred": area["kind"], "reason": f"planned {area['kind']} area"}
    hood = neighbourhood(center, buildings)
    nearest = {use: min([hood["nearest"][use]] + [math.dist(center, (land["position"]["x"], land["position"]["z"]))
                                                  for land in zoned if land["use"] == use]) for use in USES}
    nearest_industry = min((math.dist(center, point) for point in industry(buildings, zoned)), default=math.inf)
    nearest_home = nearest["residential"]
    # a block reaches past its centre: half its length along the road and half its 4-cell depth
    # (live: a block centred 168m from a wind turbine grew homes 147m from it, inside its noise)
    reach = math.hypot(4.0 * float(block.get("rows") or 8), 16.0) if block.get("blockId") is not None else 0.0
    polluted = any(math.dist(center, (item["position"]["x"], item["position"]["z"])) - reach
                   < max(2 * float(item.get("pollutionRadius") or 0), float(item.get("noiseRadius") or 0))
                   for item in polluters)
    counts = hood["counts"]
    total = sum(counts.values())
    allowed = []
    if nearest_industry > INDUSTRY_HOME_GAP and not polluted:
        allowed.append("residential")
    factories = sum(1 for b in buildings if b.get("service") == "Industrial"
                    and math.dist(center, (b["position"]["x"], b["position"]["z"])) <= NEIGHBOURHOOD)
    industrial_district = total and factories / total >= 0.6
    # a district extends toward homes only while its pollution stays off them (live: industry
    # zoned 141-225m from homes at a district's end left 13 homes polluted)
    fouls_well = any(math.dist(center, well) - reach < WELL_CLEARANCE for well in wells)
    if (nearest_home > INDUSTRY_HOME_GAP or (industrial_district and nearest_home > INDUSTRY_POLLUTION_REACH)) \
            and not fouls_well:
        allowed.append("workplace")
    arterial = is_arterial(block.get("roadPrefab")) or (
        through_roads is not None and int(block.get("roadSegmentId") or 0) in through_roads
        and block.get("roadPrefab") != "Gravel Road")
    corner = any(math.dist(center, point) <= CORNER_RADIUS for point in arterial_corners)
    buffer = nearest_industry <= BUFFER_REACH and nearest_home <= BUFFER_REACH
    shop_gap = counts["residential"] > 0 and all(math.dist(center, point) > COMMERCIAL_SPACING
                                                  for point in commercial_blocks)
    if (arterial or corner or buffer or shop_gap) and not industrial_district:
        allowed.append("commercial")
    if nearest_industry <= INDUSTRY_HOME_GAP and "residential" not in allowed and not industrial_district:
        allowed.append("office")
    if total:
        majority = max(USES, key=lambda use: counts[use])
        preferred = majority if majority in allowed else (allowed[0] if allowed else None)
        reason = f"neighbourhood is mostly {majority}"
    else:
        preferred = "residential" if "residential" in allowed else ("workplace" if "workplace" in allowed else None)
        reason = "no neighbours: residential unless industry is near"
    if buffer and "commercial" in allowed and "residential" not in allowed:
        preferred, reason = "commercial", "buffer between industry and homes"
    return {"allowed": allowed, "preferred": preferred, "reason": reason, "arterial": arterial,
            "corner": corner, "buffer": buffer, "nearestIndustry": round(nearest_industry, 1),
            "nearestHome": round(nearest_home, 1)}


def town_center(buildings: Iterable[dict[str, Any]]) -> tuple[float, float] | None:
    """Median position of the homes: the city's residential core, robust to outlying districts."""
    xs, zs = [], []
    for building in buildings:
        if building.get("service") == "Residential":
            xs.append(building["position"]["x"])
            zs.append(building["position"]["z"])
    if not xs:
        return None
    xs.sort()
    zs.sort()
    return xs[len(xs) // 2], zs[len(zs) // 2]

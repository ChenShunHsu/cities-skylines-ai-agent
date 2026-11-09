"""Every reported problem mapped to a remedy, as pure functions.

A city problem is a notification on a building. Each problem name maps to the
kind of remedy that removes it: a service facility within reach (a full one is
rebuilt, a far one gets a new facility near the affected buildings), a utility
repair, a bulldoze, a road split, a transit line check, or an economy lever. Names without a mapping
are reported as unmapped so an agent decides instead of the problem being
silently ignored.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Iterable


# family: which facilities serve the problem; match: prefab name fragments of that family
SERVICE_FAMILIES = {
    "deathcare": {"service": "HealthCare", "match": ("cemetery", "crematory", "crematorium")},
    "healthcare": {"service": "HealthCare", "match": ("clinic", "hospital", "medical")},
    "garbage": {"service": "Garbage", "match": ("landfill", "incinerat", "combustion", "recycl")},
    "police": {"service": "PoliceDepartment", "match": ("police",)},
    "fire": {"service": "FireDepartment", "match": ("fire",)},
    "elementary": {"service": "Education", "match": ("elementary",)},
    "highSchool": {"service": "Education", "match": ("high school",)},
    "university": {"service": "Education", "match": ("university",)},
}

REMEDIES = {
    "Death": {"kind": "service", "family": "deathcare"},
    "Garbage": {"kind": "service", "family": "garbage"},
    "LandfillFull": {"kind": "service", "family": "garbage"},
    "Crime": {"kind": "service", "family": "police"},
    "Fire": {"kind": "service", "family": "fire"},
    # no building is flagged for these: shortfall_rows reports them from the city's service use
    "Sick": {"kind": "service", "family": "healthcare"},
    "ElementaryShort": {"kind": "service", "family": "elementary"},
    "HighSchoolShort": {"kind": "service", "family": "highSchool"},
    "UniversityShort": {"kind": "service", "family": "university"},
    "Water": {"kind": "utility", "utility": "water"},
    "Sewage": {"kind": "utility", "utility": "water"},
    "WaterNotConnected": {"kind": "utility", "utility": "water"},
    "Electricity": {"kind": "utility", "utility": "electricity"},
    "Abandoned": {"kind": "bulldoze"},
    "BurnedDown": {"kind": "bulldoze"},
    "Collapsed": {"kind": "bulldoze"},
    # flagged on a burned or collapsed ruin with them (live: 2 of each), cleared with the ruin
    "StructureDamaged": {"kind": "bulldoze"},
    "RoadAccessFailed": {"kind": "splitRoad"},
    "TurnedOff": {"kind": "reactivate"},
    # a new line's stops report this until pathfinding between them completes; one that persists
    # has a stop no vehicle can reach (inspect the line's paths)
    "LineNotConnected": {"kind": "transitLine"},
    "NoWorkers": {"kind": "economy", "lever": "growResidential"},
    "NoEducatedWorkers": {"kind": "economy", "lever": "educationCapacity"},
    "NoCustomers": {"kind": "economy", "lever": "slowCommercial"},
    "NoGoods": {"kind": "economy", "lever": "industrySupply"},
    # "Not enough buyers for products": more goods than nearby commerce takes, or trucks
    # cannot reach the shops; zone commerce near the industry or relieve its roads
    "NoPlaceforGoods": {"kind": "economy", "lever": "commercialOutlet"},
    "LandValueLow": {"kind": "economy", "lever": "serviceCoverage"},
    "Noise": {"kind": "economy", "lever": "separateNoise"},
    "Pollution": {"kind": "economy", "lever": "separatePollution"},
    "TooFewServices": {"kind": "economy", "lever": "serviceCoverage"},  # cannot level up
    "TaxesTooHigh": {"kind": "economy", "lever": "lowerTax"},
    "NoResources": {"kind": "economy", "lever": "industrySupply"},  # a specialised industry ran dry
    "NoFuel": {"kind": "economy", "lever": "fuelSupply"},  # a coal or oil plant gets no fuel
    "ElectricityNotConnected": {"kind": "utility", "utility": "electricity"},
    "DepotNotConnected": {"kind": "transitLine"},
    "TrackNotConnected": {"kind": "transitLine"},
    # a facility reporting what it is doing, not a fault
    "Emptying": {"kind": "status"},
    "EmptyingFinished": {"kind": "status"},
    # repaired elsewhere in the wake (roads joined, orphans cleared, water receding) or by time;
    # one that stays PERSIST wakes goes to the planner
    "RoadNotConnected": {"kind": "persist", "lever": "joinRoads"},
    "Flood": {"kind": "persist", "lever": "waterRecedes"},
    "Flooded": {"kind": "persist", "lever": "waterRecedes"},
    # homes drinking water an outlet pollutes upstream of the intake (sickness)
    "DirtyWater": {"kind": "persist", "lever": "moveIntakeUpstream"},
}

# service use the game flags on no building -> (problem reported, family serving it)
SHORTFALLS = {
    "healthcare": ("Sick", "healthcare"),
    "elementaryEducation": ("ElementaryShort", "elementary"),
    "highSchoolEducation": ("HighSchoolShort", "highSchool"),
    "universityEducation": ("UniversityShort", "university"),
}
SHORTFALL_MIN = 20  # people beyond the places for them before it counts
SHORTFALL_REACH = {"healthcare": 700.0, "elementary": 500.0, "highSchool": 1000.0, "university": 1500.0}

# notification flags that mark severity, not a problem of their own
SEVERITY_FLAGS = frozenset({"MajorProblem", "FatalProblem"})


def facility_family(facility: dict[str, Any]) -> str | None:
    name = str(facility.get("prefab", "")).lower()
    for family, spec in SERVICE_FAMILIES.items():
        if facility.get("service") == spec["service"] and any(part in name for part in spec["match"]):
            return family
    return None


def shortfall_rows(services: dict[str, Any], homes: list[dict[str, Any]], facilities: list[dict[str, Any]],
                   unlocked: Iterable[str]) -> list[dict[str, Any]]:
    """Problem rows for service use the game flags on no building (live: 111 sick and 168 pupils
    with no clinic or school, and not one notification): the homes beyond the family's reach
    report it, or every home when all are within reach and the places run short."""
    unlocked = set(unlocked)
    rows = []
    for key, (name, family) in SHORTFALLS.items():
        use = services.get(key) or {}
        short = float(use.get("used") or 0) - float(use.get("capacity") or 0)
        if family not in unlocked or short < SHORTFALL_MIN:
            continue
        serving = [(item["position"]["x"], item["position"]["z"]) for item in facilities
                   if facility_family(item) == family]
        reach = SHORTFALL_REACH[family]
        beyond = [home for home in homes if all(
            math.dist((home["position"]["x"], home["position"]["z"]), spot) > reach for spot in serving)]
        rows += [{"id": home["id"], "position": home["position"], "problemNames": [name]}
                 for home in beyond or homes]
    return rows


def facility_full(facility: dict[str, Any]) -> bool:
    # the problem appears a step before both capacity bits are set; either means it stopped collecting
    return "CapacityFull" in str(facility.get("flags", "")) or "LandfillFull" in str(facility.get("problems", ""))


def clusters(points: list[dict[str, Any]], link_meters: float) -> list[dict[str, Any]]:
    """Single-linkage groups: buildings within link_meters of another member join it."""
    parent = list(range(len(points)))

    def root(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    cell = max(1.0, link_meters)
    grid: dict[tuple[int, int], list[int]] = defaultdict(list)
    for index, point in enumerate(points):
        grid[(int(point["x"] // cell), int(point["z"] // cell))].append(index)
    for (gx, gz), members in grid.items():
        for dx in (-1, 0, 1):
            for dz in (-1, 0, 1):
                for other in grid.get((gx + dx, gz + dz), []):
                    for index in members:
                        if index < other and math.dist((points[index]["x"], points[index]["z"]),
                                                       (points[other]["x"], points[other]["z"])) <= link_meters:
                            parent[root(other)] = root(index)
    groups: dict[int, list[int]] = defaultdict(list)
    for index in range(len(points)):
        groups[root(index)].append(index)
    result = []
    for members in groups.values():
        xs = [points[index]["x"] for index in members]
        zs = [points[index]["z"] for index in members]
        result.append({"count": len(members), "center": {"x": round(sum(xs) / len(xs), 1),
                                                         "z": round(sum(zs) / len(zs), 1)},
                       "buildingIds": sorted(int(points[index]["id"]) for index in members)})
    result.sort(key=lambda group: -group["count"])
    return result


def problem_remedies(problem_rows: Iterable[dict[str, Any]], facilities: list[dict[str, Any]],
                     link_meters: float = 250.0, cluster_limit: int = 3) -> list[dict[str, Any]]:
    """One entry per reported problem name with its remedy and, for service problems,
    the affected clusters with their nearest facility of the serving family."""
    by_problem: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in problem_rows:
        position = row.get("position") or {}
        for name in row.get("problemNames") or []:
            if name in SEVERITY_FLAGS:
                continue
            by_problem[name].append({"id": row.get("id"), "x": float(position.get("x", 0)),
                                     "z": float(position.get("z", 0)), "major": bool(row.get("isMajor")),
                                     "fatal": bool(row.get("isFatal"))})
    families: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for facility in facilities:
        family = facility_family(facility)
        if family:
            families[family].append(facility)
    entries = []
    for name, rows in sorted(by_problem.items(), key=lambda item: -len(item[1])):
        remedy = dict(REMEDIES.get(name) or {"kind": "unmapped"})
        entry: dict[str, Any] = {"problem": name, "count": len(rows), "remedy": remedy,
                                 "major": sum(row["major"] for row in rows),
                                 "fatal": sum(row["fatal"] for row in rows)}
        if remedy["kind"] == "service":
            serving = families.get(remedy["family"], [])
            entry["facilities"] = {"count": len(serving),
                                   "full": sorted(int(item["id"]) for item in serving if facility_full(item)),
                                   "prefabs": sorted({str(item["prefab"]) for item in serving})}
            groups = clusters(rows, link_meters)[:cluster_limit]
            for group in groups:
                working = [item for item in serving if not facility_full(item)]
                nearest = min(working, key=lambda item: math.dist(
                    (group["center"]["x"], group["center"]["z"]),
                    (item["position"]["x"], item["position"]["z"])), default=None)
                group["nearestWorkingFacility"] = None if nearest is None else {
                    "id": int(nearest["id"]), "prefab": nearest["prefab"],
                    "distance": round(math.dist((group["center"]["x"], group["center"]["z"]),
                                                (nearest["position"]["x"], nearest["position"]["z"])), 1)}
            entry["clusters"] = groups
        else:
            entry["buildingIds"] = sorted(int(row["id"]) for row in rows)[:50]
        entries.append(entry)
    return entries

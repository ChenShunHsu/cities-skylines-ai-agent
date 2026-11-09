"""Compare the unlocked buildings that can provide one service, as pure functions.

Each option's AI tuning fields (the Bridge's aiProfile) are read by role instead of by
prefab, so a new or modded building of a known AI type is compared without changes:

- radius: how far the building serves (coverage)
- vehicles: trucks, hearses, ambulances or patrol cars it dispatches
- served: people it holds at once (patients, students, jail cells, corpses in transit)
- storage: what it keeps until full (landfill garbage, graves); full means rebuild or empty
- processing: what it removes each period (incinerated garbage, cremations, cures)
- power: electricity it produces

The need decides the ranking: "coverage" when buildings are beyond reach, "capacity" when
they are within reach but the problem persists, "fillsUp" when facilities of the family
keep filling, "power" for electricity. Ratios are per maintenance cost, since upkeep is
what a growing city pays every period.
"""

from __future__ import annotations

import math
from typing import Any, Iterable

# family -> (game service, AI types that provide it)
FAMILIES = {
    "garbage": ("Garbage", ("LandfillSiteAI",)),
    "deathcare": ("HealthCare", ("CemeteryAI",)),
    "healthcare": ("HealthCare", ("HospitalAI",)),
    "eldercare": ("HealthCare", ("EldercareAI",)),
    "childcare": ("HealthCare", ("ChildcareAI",)),
    "police": ("PoliceDepartment", ("PoliceStationAI",)),
    "fire": ("FireDepartment", ("FireStationAI",)),
    "education": ("Education", ("SchoolAI",)),
    "elementary": ("Education", ("SchoolAI",)),
    "highSchool": ("Education", ("SchoolAI",)),
    "university": ("Education", ("SchoolAI",)),
    "power": ("Electricity", ("PowerPlantAI", "WindTurbineAI", "SolarPowerPlantAI", "FusionPowerPlantAI")),
}

# a school takes only its own level's pupils: a high school seats no child of elementary age
LEVELS = {"elementary": "Level1", "highSchool": "Level2", "university": "Level3"}

ROLE_FIELDS = {
    "radius": ("collectRadius", "deathCareRadius", "healthCareRadius", "policeDepartmentRadius",
               "fireDepartmentRadius", "educationRadius"),
    "vehicles": ("garbageTruckCount", "hearseCount", "ambulanceCount", "policeCarCount", "fireTruckCount"),
    "served": ("patientCapacity", "studentCount", "jailCapacity", "corpseCapacity"),
    "storage": ("graveCount",),
    "processing": ("garbageConsumption", "burialRate", "curingRate"),
    "power": ("electricityProduction",),
}

NEEDS = ("coverage", "capacity", "fillsUp", "power")


def roles(fields: dict[str, Any]) -> dict[str, float]:
    values = {role: float(sum(float(fields.get(name) or 0) for name in names
                              if isinstance(fields.get(name), (int, float)) and not isinstance(fields.get(name), bool)))
              for role, names in ROLE_FIELDS.items()}
    # a landfill keeps all it collects; an incinerator's capacity is only a buffer before burning
    if float(fields.get("garbageConsumption") or 0) == 0:
        values["storage"] += float(fields.get("garbageCapacity") or 0)
    values["radius"] = max((float(fields.get(name) or 0) for name in ROLE_FIELDS["radius"]), default=0.0)
    return values


def describe(asset: dict[str, Any]) -> dict[str, Any]:
    profile = asset.get("aiProfile") or {}
    fields = profile.get("fields") or {}
    upkeep = float(asset.get("maintenanceCost") or 0)
    values = roles(fields)
    per_upkeep = {role: round(value / upkeep * 1000, 3) if upkeep > 0 else None
                  for role, value in values.items() if value}
    return {
        "name": asset.get("name"),
        "displayName": asset.get("displayName"),
        "aiType": profile.get("aiType"),
        "constructionCost": asset.get("constructionCost"),
        "maintenanceCost": asset.get("maintenanceCost"),
        "cells": [asset.get("width"), asset.get("length")],
        "roles": {role: value for role, value in values.items() if value},
        "perThousandUpkeep": per_upkeep,
        # graves and landfill garbage stay until the building is emptied or rebuilt
        "fillsUp": bool(values["storage"]),
        "pollutionRadius": fields.get("pollutionRadius") if fields.get("pollutionAccumulation") else 0,
        "noiseRadius": fields.get("noiseRadius") if fields.get("noiseAccumulation") else 0,
    }


def score(option: dict[str, Any], need: str) -> float:
    """Higher is better. Coverage weighs area served per upkeep; capacity weighs vehicles and
    people held at once per upkeep; fillsUp ranks buildings that never fill (they process)
    first; power is production per upkeep."""
    roles_ = option["roles"]
    upkeep = float(option["maintenanceCost"] or 0) or 1.0
    if need == "coverage":
        return roles_.get("radius", 0.0) ** 2 / upkeep
    if need == "capacity":
        return (roles_.get("vehicles", 0.0) + roles_.get("served", 0.0)) / upkeep
    if need == "fillsUp":
        processes = roles_.get("processing", 0.0) > 0 and not option["fillsUp"]  # a cemetery buries into graves
        return (1.0 if processes else 0.0) * 1e6 + roles_.get("processing", 0.0) / upkeep
    if need == "power":
        return roles_.get("power", 0.0) / upkeep
    raise ValueError(f"Unknown need {need}; expected one of {NEEDS}")


UNIT = {"coverage": "radius", "fillsUp": "processing", "power": "power"}


def units_per_building(option: dict[str, Any], need: str) -> float:
    roles_ = option["roles"]
    if need == "capacity":
        return roles_.get("vehicles", 0.0) + roles_.get("served", 0.0)
    return roles_.get(UNIT[need], 0.0)


def buildings_needed(option: dict[str, Any], need: str, deficit: float) -> int | None:
    """How many of this option meet the deficit: a coverage deficit is the radius of the area to
    serve (circles add by area), every other deficit is in the need's own units."""
    unit = units_per_building(option, need)
    if unit <= 0:
        return None
    ratio = (deficit / unit) ** 2 if need == "coverage" else deficit / unit
    return max(1, math.ceil(ratio - 1e-9))


def options_for(assets: Iterable[dict[str, Any]], family: str, need: str,
                budget: float | None = None, deficit: float | None = None,
                site_cost: float = 0.0) -> list[dict[str, Any]]:
    """Unlocked buildings of the family, best first for the need.

    Without a deficit the ranking is the need's ratio per upkeep. With one, each option is
    costed as the buildings it takes to meet the deficit: their upkeep plus site_cost for every
    building after the first, since each extra building needs another site in a built-up city
    (and may displace growables). With a budget, options whose construction cost exceeds it are
    kept but marked unaffordable and ranked last."""
    if family not in FAMILIES:
        raise ValueError(f"Unknown family {family}; expected one of {sorted(FAMILIES)}")
    if need not in NEEDS:
        raise ValueError(f"Unknown need {need}; expected one of {NEEDS}")
    service, ai_types = FAMILIES[family]
    described = [describe(asset) for asset in assets
                 if asset.get("unlocked", True) and asset.get("service") == service
                 and (asset.get("aiProfile") or {}).get("aiType") in ai_types
                 and (family not in LEVELS or asset.get("level") == LEVELS[family])]
    for option in described:
        option["score"] = round(score(option, need), 6)
        if deficit is not None:
            count = buildings_needed(option, need, deficit)
            option["buildingsNeeded"] = count
            option["totalUpkeep"] = None if count is None else count * float(option["maintenanceCost"] or 0)
            option["score"] = (-math.inf if count is None else
                               -(option["totalUpkeep"] + (count - 1) * site_cost))
        option["affordable"] = budget is None or float(option["constructionCost"] or 0) <= budget
    return sorted(described, key=lambda option: (not option["affordable"], -option["score"]))

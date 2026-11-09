"""Diagnose a congestion hotspot from its route evidence and rank the kinds of remedy, as pure functions.

Junction control and widening only help when the queue forms at the hotspot itself. The route
evidence of the hotspot's busiest segment (inspect_segment_routes) tells the other cases apart:

- the dominant exit is itself saturated: the queue spills back from downstream, so the fix is
  there (its junction, its road, or a second access to where the traffic is going)
- cargo trucks dominate: trucks serve industry and outside connections, so freight rail takes
  them off the road; buses do not
- passenger cars dominate on local roads: a transit line can carry those trips
- most trips pass through (origin and destination far away): a bypass route relieves it
- highways or ramps make up most of it: local remedies rarely reach it (a ramp at one end of a
  local hotspot does not; its cars can still take a bus or a bypass)

Shares are of vehicles planning to use the segment (remaining paths), not of all traffic.
"""

from __future__ import annotations

import math
from typing import Any

CARGO_AI = ("CargoTruckAI",)
PASSENGER_AI = ("PassengerCarAI",)
SERVICE_AI = ("GarbageTruckAI", "PoliceCarAI", "HearseAI", "AmbulanceAI", "FireTruckAI",
              "MaintenanceTruckAI", "SnowTruckAI", "ParkMaintenanceVehicleAI", "PostVanAI")
SATURATED = 90  # density of an exit that is itself queued
CARGO_SHARE = 0.4
PASSENGER_SHARE = 0.5
THROUGH_SHARE = 0.4
HIGHWAY_MAJORITY = 0.5
THROUGH_RADIUS = 1500.0  # an origin and destination both this far away pass through
OUTSIDE = 4800.0  # beyond the 5x5-tile map edge: outside connections


def _share(counts: dict[str, Any], kinds: tuple[str, ...]) -> float:
    total = sum(int(value or 0) for value in counts.values())
    return round(sum(int(counts.get(kind) or 0) for kind in kinds) / total, 3) if total else 0.0


def diagnose(hotspot: dict[str, Any], routes: dict[str, Any]) -> dict[str, Any]:
    by_ai = routes.get("byVehicleAI") or {}
    planning = int(routes.get("vehiclesPlanningToUse") or 0) or sum(int(v or 0) for v in by_ai.values())
    center = (hotspot["center"]["x"], hotspot["center"]["z"])
    members = set(hotspot.get("segmentIds") or [])
    exits = [item for item in routes.get("exitingToSegments") or [] if int(item.get("segmentId") or 0)]
    dominant_exit = max(exits, key=lambda item: int(item.get("vehicles") or 0), default=None)
    exit_share = round(int(dominant_exit["vehicles"]) / planning, 3) if dominant_exit and planning else 0.0
    pairs = routes.get("originDestinationPairs") or []
    paired = sum(int(pair.get("vehicles") or 0) for pair in pairs)

    def far(point: dict[str, Any]) -> bool:
        return math.dist(center, (point["x"], point["z"])) > THROUGH_RADIUS

    def outside(point: dict[str, Any]) -> bool:
        return abs(point["x"]) > OUTSIDE or abs(point["z"]) > OUTSIDE

    through = sum(int(pair.get("vehicles") or 0) for pair in pairs
                  if far(pair["origin"]) and far(pair["destination"]))
    external = sum(int(pair.get("vehicles") or 0) for pair in pairs
                   if outside(pair["origin"]) or outside(pair["destination"]))
    prefabs = hotspot.get("prefabs") or {}
    segments = sum(int(count) for count in prefabs.values())
    highway = sum(int(count) for name, count in prefabs.items() if "Highway" in name or "Ramp" in name)
    highway_share = round(highway / segments, 3) if segments else 0.0
    return {
        "vehicles": planning,
        "cargoShare": _share(by_ai, CARGO_AI),
        "passengerCarShare": _share(by_ai, PASSENGER_AI),
        "serviceShare": _share(by_ai, SERVICE_AI),
        "dominantExit": None if dominant_exit is None else {
            "segmentId": int(dominant_exit["segmentId"]), "prefab": dominant_exit.get("prefab"),
            "density": dominant_exit.get("density"), "share": exit_share,
            "insideHotspot": int(dominant_exit["segmentId"]) in members,
            "position": dominant_exit.get("position")},
        "downstreamSaturated": bool(dominant_exit and exit_share >= 0.5
                                    and int(dominant_exit.get("density") or 0) >= SATURATED),
        "throughShare": round(through / paired, 3) if paired else 0.0,
        "externalShare": round(external / paired, 3) if paired else 0.0,
        "highwayShare": highway_share,
        "highwayOrRamp": highway_share > HIGHWAY_MAJORITY,
        "topDestinations": (routes.get("destinationCells") or [])[:3],
        # where the stream ends inside the city (outside connections excluded), e.g. for a cargo station
        "localAnchor": next((cell["center"] for cell in (routes.get("destinationCells") or []) +
                             (routes.get("originCells") or []) if not outside(cell["center"])), None),
    }


def remedy_kinds(diagnosis: dict[str, Any], hotspot: dict[str, Any]) -> list[dict[str, Any]]:
    """Kinds of remedy in the order the evidence supports, each with the reason. Junction control
    and widening stay as the last local options when nothing points elsewhere."""
    kinds = []
    exit_ = diagnosis.get("dominantExit") or {}
    if diagnosis["downstreamSaturated"]:
        kinds.append({"kind": "relieveDownstream",
                      "reason": f"{exit_['share']:.0%} of vehicles leave by segment {exit_['segmentId']} "
                                f"({exit_.get('prefab')}) at density {exit_.get('density')}: the queue spills back"})
    if diagnosis["cargoShare"] >= CARGO_SHARE:
        kinds.append({"kind": "cargoRail",
                      "reason": f"cargo trucks are {diagnosis['cargoShare']:.0%}; a cargo station takes "
                                "freight off the road"})
    if diagnosis["passengerCarShare"] >= PASSENGER_SHARE and not diagnosis["highwayOrRamp"]:
        kinds.append({"kind": "transit",
                      "reason": f"passenger cars are {diagnosis['passengerCarShare']:.0%} on local roads"})
    if diagnosis["throughShare"] >= THROUGH_SHARE and not diagnosis["highwayOrRamp"]:
        kinds.append({"kind": "bypass",
                      "reason": f"{diagnosis['throughShare']:.0%} of paired trips pass through"})
    if hotspot.get("junctionTrials"):
        kinds.append({"kind": "junctionControl", "reason": f"{len(hotspot['junctionTrials'])} junction candidates"})
    if hotspot.get("upgradeCandidates"):
        kinds.append({"kind": "widen", "reason": f"{len(hotspot['upgradeCandidates'])} same-width upgrades"})
    if diagnosis["highwayOrRamp"] and not any(item["kind"] == "relieveDownstream" for item in kinds):
        kinds.append({"kind": "interchange",
                      "reason": "a highway or ramp is part of the hotspot; its layout needs a redesign"})
    return kinds

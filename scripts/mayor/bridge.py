"""River crossings for new land beyond water, as pure functions.

A road reaches land across a river as a player builds it: a ground road from the network to the
near bank, a bridge from a little behind that bank to a little beyond the far one (a ramp up, a
deck high over the water, a ramp down), and the new district grows from where it lands. The place
to cross is chosen over the network's road ends and a fan of headings toward the land: the least
road for the money, a metre of bridge weighing BRIDGE_WEIGHT metres of ground road.
"""

from __future__ import annotations

import math
from typing import Any, Callable, Iterable

MARCH = 8.0  # metres between samples along a crossing line
SETBACK = 40.0  # the bridge starts and ends at least this far back from the water, on land
# a steep bank is crossed raised: the bridge may start farther back (live: a bank sloping 0.3 refused
# the ground road 40m from the water; the game takes an elevated ramp over it)
SETBACKS = (40.0, 80.0, 120.0)
PIECE = 90.0  # the longest bridge piece
DECK = 12.0  # deck height above the water and ramp tops above the banks
BRIDGE_WEIGHT = 4.0  # a bridge metre costs about four ground-road metres (live previews)
MAX_SPAN = 1200.0
MAX_APPROACH = 900.0
FAR_LAND = 120.0  # land the far side must offer along the line before the district starts
FAN = (0.0, -15.0, 15.0, -30.0, 30.0, -45.0, 45.0)


def _unit(dx: float, dz: float) -> tuple[float, float]:
    length = math.hypot(dx, dz) or 1.0
    return dx / length, dz / length


def _turn(u: tuple[float, float], degrees: float) -> tuple[float, float]:
    c, s = math.cos(math.radians(degrees)), math.sin(math.radians(degrees))
    return u[0] * c - u[1] * s, u[0] * s + u[1] * c


def line(start: tuple[float, float], u: tuple[float, float], wet: Callable[[tuple[float, float]], bool],
         limit: float) -> dict[str, Any] | None:
    """Along one heading from start: the first water, the land beyond it, and how much of that land
    there is (None when the line meets no water, or no land after it within the limits)."""
    distance, entered, left = 0.0, None, None
    while distance <= limit:
        point = (start[0] + u[0] * distance, start[1] + u[1] * distance)
        if wet(point):
            if left is not None:
                return None  # a second river before enough land: not one crossing
            if entered is None:
                entered = distance
        elif entered is not None:
            if left is None:
                left = distance
            if distance - left >= FAR_LAND:
                return {"approach": entered, "span": left - entered, "landed": left}
        distance += MARCH
    return None


def crossings(starts: Iterable[tuple[float, float]], target: tuple[float, float],
              wet: Callable[[tuple[float, float]], bool], limit: int = 5) -> list[dict[str, Any]]:
    """Ways over the water toward the target, cheapest first: from which road end, along which
    heading, how much ground road to the bank and how much bridge."""
    found = []
    for start in starts:
        if wet(start):
            continue
        toward = _unit(target[0] - start[0], target[1] - start[1])
        for offset in FAN:
            u = _turn(toward, offset)
            crossing = line(start, u, wet, MAX_APPROACH + MAX_SPAN + FAR_LAND)
            if crossing is None or crossing["approach"] > MAX_APPROACH or crossing["span"] > MAX_SPAN:
                continue
            found.append({"start": start, "heading": u, "approach": crossing["approach"], "span": crossing["span"],
                          "cost": crossing["approach"] + BRIDGE_WEIGHT * crossing["span"]})
    found.sort(key=lambda item: item["cost"])
    return found[:limit]


def _along(start: tuple[float, float], u: tuple[float, float], distance: float) -> dict[str, float]:
    return {"x": round(start[0] + u[0] * distance, 2), "z": round(start[1] + u[1] * distance, 2)}


def pieces(crossing: dict[str, Any], deck: float = DECK, setback: float = SETBACK) -> dict[str, list[dict[str, Any]]]:
    """The roads of one crossing: ground pieces to `setback` before the water, then the bridge from
    there to SETBACK beyond it, every piece no longer than PIECE. Bridge pieces carry height
    offsets: 0 on land at both ends, `deck` everywhere between."""
    start, u = crossing["start"], crossing["heading"]
    ground_end = max(0.0, crossing["approach"] - setback)
    ground = []
    count = max(1, math.ceil(ground_end / PIECE)) if ground_end > 1.0 else 0
    for index in range(count):
        ground.append({"start": _along(start, u, ground_end * index / count),
                       "end": _along(start, u, ground_end * (index + 1) / count)})
    bridge_end = crossing["approach"] + crossing["span"] + SETBACK
    length = bridge_end - ground_end
    count = max(3, math.ceil(length / PIECE))
    bridge = []
    for index in range(count):
        bridge.append({"start": _along(start, u, ground_end + length * index / count),
                       "end": _along(start, u, ground_end + length * (index + 1) / count),
                       "startHeightOffset": 0.0 if index == 0 else deck,
                       "endHeightOffset": 0.0 if index == count - 1 else deck,
                       "overWater": 0 < index < count - 1})
    return {"ground": ground, "bridge": bridge, "landing": _along(start, u, bridge_end)}

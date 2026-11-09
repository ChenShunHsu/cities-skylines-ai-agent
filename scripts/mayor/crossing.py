"""Grade-separated crossings of a ground railway or road, as pure functions.

The Bridge builds a road between two points without cutting a junction into what it crosses, so a
road cannot cross a ground railway or road at grade; it goes over. An overpass is three pieces:
a ramp up to CLEARANCE (no steeper than MAX_GRADE), a level piece over the barrier, and a ramp
down on the far side. A start node close to the barrier has no room to climb straight at it, so
the ramp may first run alongside the barrier and the level piece then turns across it.

Barriers close behind one another on the way (barrier_group: a railway beside a highway) are
crossed by one level piece, which runs past the last of them before the ramp comes down: ramps
that came down after the first landed on the railway beside it (live: every overpass to 1.1km2 of
flat land across a highway and its railway collided).
"""

from __future__ import annotations

import math
from typing import Any, Iterable

CLEARANCE = 10.0
MAX_GRADE = 0.2
MAX_PIECE = 96.0
PAST_BARRIER = 20.0  # how far beyond the barrier's centreline the level piece runs
RAMP_LENGTHS = (50.0, 70.0, 90.0)
GROUP_REACH = 80.0  # barriers this far behind the first along the way are crossed in the same span
SPAN_STEP = 4.0


def side(point: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
    """Signed distance of point from the line a-b (positive on its left)."""
    ux, uz = b[0] - a[0], b[1] - a[1]
    length = math.hypot(ux, uz) or 1.0
    return (ux * (point[1] - a[1]) - uz * (point[0] - a[0])) / length


def crossed(start: tuple[float, float], end: tuple[float, float],
            segments: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Segments the straight line start-end crosses, nearest first."""
    return [item["segment"] for item in _crossings(start, end, segments)]


def barrier_group(start: tuple[float, float], end: tuple[float, float], segments: Iterable[dict[str, Any]],
                  reach: float = GROUP_REACH) -> list[dict[str, Any]]:
    """The first segment the way crosses and those it crosses within reach behind it."""
    found = _crossings(start, end, segments)
    if not found:
        return []
    length = math.dist(start, end)
    first = found[0]["fraction"]
    return [item["segment"] for item in found if (item["fraction"] - first) * length <= reach]


def _crossings(start: tuple[float, float], end: tuple[float, float],
               segments: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    found = []
    for segment in segments:
        a = (segment["start"]["x"], segment["start"]["z"])
        b = (segment["end"]["x"], segment["end"]["z"])
        s1, s2 = side(a, start, end), side(b, start, end)
        t1, t2 = side(start, a, b), side(end, a, b)
        if s1 * s2 < 0 and t1 * t2 < 0:
            fraction = t1 / (t1 - t2)
            found.append({"segment": segment, "fraction": fraction})
    found.sort(key=lambda item: item["fraction"])
    return found


def _ends(segment: dict[str, Any]) -> tuple[tuple[float, float], tuple[float, float]]:
    return (segment["start"]["x"], segment["start"]["z"]), (segment["end"]["x"], segment["end"]["z"])


def overpasses(start: tuple[float, float], target: tuple[float, float], barrier: dict[str, Any],
               beside: Iterable[dict[str, Any]] = ()) -> list[list[dict[str, Any]]]:
    """Candidate three-piece overpasses from start over the barrier, and the barriers beside it,
    toward target, shortest first. Each piece is {start, end, startHeightOffset, endHeightOffset}."""
    lines = [_ends(segment) for segment in [barrier, *beside]]
    near = [side(start, p, q) for p, q in lines]

    def on_near_side(point: tuple[float, float]) -> bool:
        return all(side(point, p, q) * s > 0 and abs(side(point, p, q)) >= 12.0 for (p, q), s in zip(lines, near))

    def past(point: tuple[float, float], margin: float) -> bool:
        return all(side(point, p, q) * s < 0 and abs(side(point, p, q)) >= margin for (p, q), s in zip(lines, near))

    a, b = lines[0]
    ux, uz = b[0] - a[0], b[1] - a[1]
    length = math.hypot(ux, uz) or 1.0
    ux, uz = ux / length, uz / length
    start_side = side(start, a, b)
    # the normal pointing from the start's side to the far side
    nx, nz = (uz, -ux) if start_side > 0 else (-uz, ux)
    tx, tz = target[0] - start[0], target[1] - start[1]
    toward = math.hypot(tx, tz) or 1.0
    directions = [(ux, uz), (-ux, -uz), (tx / toward, tz / toward), (nx, nz)]
    candidates = []
    for dx, dz in directions:
        for climb in RAMP_LENGTHS:
            top = (start[0] + dx * climb, start[1] + dz * climb)
            if not on_near_side(top):
                continue  # the ramp must stay on the near side, clear of every barrier
            span = abs(side(top, a, b)) + PAST_BARRIER
            over = (top[0] + nx * span, top[1] + nz * span)
            while not past(over, PAST_BARRIER - 0.5) and span <= MAX_PIECE:
                span += SPAN_STEP  # on past the barriers beside the first
                over = (top[0] + nx * span, top[1] + nz * span)
            if span > MAX_PIECE:
                continue
            for descent in RAMP_LENGTHS:
                gx, gz = target[0] - over[0], target[1] - over[1]
                remaining = math.hypot(gx, gz) or 1.0
                down = (over[0] + gx / remaining * descent, over[1] + gz / remaining * descent)
                if not past(down, 0.0):
                    continue  # the ramp down must land on the far side of them all
                pieces = [
                    {"start": start, "end": top, "startHeightOffset": 0.0, "endHeightOffset": CLEARANCE},
                    {"start": top, "end": over, "startHeightOffset": CLEARANCE, "endHeightOffset": CLEARANCE},
                    {"start": over, "end": down, "startHeightOffset": CLEARANCE, "endHeightOffset": 0.0},
                ]
                candidates.append(([{**piece, "start": {"x": round(piece["start"][0], 2), "z": round(piece["start"][1], 2)},
                                     "end": {"x": round(piece["end"][0], 2), "z": round(piece["end"][1], 2)}}
                                    for piece in pieces], climb + span + descent))
    candidates.sort(key=lambda item: item[1])
    return [pieces for pieces, _ in candidates]

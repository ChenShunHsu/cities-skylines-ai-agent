"""Road layouts for new land, as pure functions: which suit an area, and the road graph of each.

No single pattern suits every map. A layout is chosen per area from its shape, slope and use
(a planner may name one instead), and expressed as a road graph the loop builds edge by edge:

- fishbone: a collector along the long axis, dead-end branches both sides. Quick on open land.
- grid: a lattice of streets. Large, flat, compact land; blocks close into loops (two ways out).
- corridor: one road along a narrow strip (valley, riverbank) with short spurs.
- ring: an arc around the area's centre with spokes outward; keeps to the contours of a rise.
- industrial: a sparse lattice, lots twice as deep across: fewer junctions for freight.
- bridgehead: a small closed grid where a bridge lands, so the first district across the water is a
  whole neighbourhood (blocks for homes, shops and work) before anything spreads from it.

Every graph starts at the entry (the way in from the network); its nodes are kept inside the
area. Joints alternate ZIGZAG metres either side of the straight line: a road built straight on
from another makes the joint a middle node the game may move, which the Bridge refuses.
"""

from __future__ import annotations

import math
from typing import Any

SPACING = 88.0  # two 4-cell zones (32m each) and a road between parallel streets
INDUSTRIAL_SPACING = 160.0
ZIGZAG = 5.0
CORRIDOR_WIDTH = 400.0  # wider land holds more than one road along it
INSIDE = SPACING * 0.75  # a node counts as inside when a free sample lies this close
LAYOUTS = ("fishbone", "grid", "corridor", "ring", "industrial")  # ranked per area; a bridgehead follows a crossing
BRIDGEHEAD_DEPTH = 3  # streets back from where the bridge lands


def _unit(degrees: float) -> tuple[float, float]:
    return math.cos(math.radians(degrees)), math.sin(math.radians(degrees))


def candidates(area: dict[str, Any], use: str, plan_layout: str | None = None) -> list[dict[str, Any]]:
    """The layouts ranked for this area and use, each with a score and the reason."""
    length, width = float(area.get("length") or 0), float(area.get("width") or 0)
    slope = float(area.get("meanSlope") or 0.0)
    ratio = length / max(width, 1.0)
    size = float(area.get("areaSquareMeters") or 0)
    scores = {
        "fishbone": (0.5, "open land: fast to grow a few steps at a time"),
        "grid": (0.9 if width >= 350 and ratio < 2.5 and slope < 0.04 and size >= 200000 else 0.2,
                 f"compact flat land ({width:.0f}m wide, slope {slope:.2f}): blocks close into loops"),
        # a strip, not merely long land: live, a 586m-wide area 2.3km long ranked as a corridor
        "corridor": (0.95 if width < 220 or (ratio >= 3.5 and width < CORRIDOR_WIDTH) else 0.15,
                     f"a strip {width:.0f}m wide and {ratio:.1f}x as long: one road along it"),
        "ring": (0.8 if slope >= 0.05 and width >= 250 else 0.1,
                 f"sloping land (slope {slope:.2f}): an arc keeps nearer the contours than straight rows"),
        "industrial": (0.95 if use == "workplace" else 0.0, "industry: deep lots, few junctions for trucks"),
    }
    ranked = sorted(({"layout": name, "score": score, "reason": reason} for name, (score, reason) in scores.items()),
                    key=lambda item: -item["score"])
    if plan_layout in scores:
        ranked = [item for item in ranked if item["layout"] == plan_layout] + \
                 [item for item in ranked if item["layout"] != plan_layout]
        ranked[0] = {**ranked[0], "reason": "the city plan names this layout"}
    return ranked


def _inside(point: tuple[float, float], area: dict[str, Any]) -> bool:
    return any(math.dist(point, sample) <= INSIDE for sample in area["points"])


def _lattice(entry: tuple[float, float], heading: float, along: range, across: range, step_along: float,
             step_across: float, area: dict[str, Any]) -> dict[tuple[int, int], tuple[float, float]]:
    ux, uz = _unit(heading)
    vx, vz = -uz, ux
    nodes = {}
    for i in along:
        for j in across:
            # joints alternate either side of each row and column so none is straight
            a = i * step_along + (ZIGZAG if j % 2 else -ZIGZAG) * (i != 0 or j != 0)
            c = j * step_across + (ZIGZAG if i % 2 else -ZIGZAG) * (i != 0 or j != 0)
            point = (entry[0] + ux * a + vx * c, entry[1] + uz * a + vz * c)
            if (i, j) == (0, 0) or _inside(point, area):
                nodes[(i, j)] = (round(point[0], 2), round(point[1], 2))
    return nodes


def _reachable(nodes: dict, edges: list[tuple]) -> tuple[dict, list[tuple]]:
    """Keep the part of the graph connected to the entry node (0, 0)."""
    linked: dict = {key: [] for key in nodes}
    for a, b in edges:
        linked[a].append(b)
        linked[b].append(a)
    seen, stack = {(0, 0)}, [(0, 0)]
    while stack:
        for other in linked[stack.pop()]:
            if other not in seen:
                seen.add(other)
                stack.append(other)
    return {k: v for k, v in nodes.items() if k in seen}, [(a, b) for a, b in edges if a in seen and b in seen]


def graph(layout: str, area: dict[str, Any], entry: tuple[float, float], heading: float) -> dict[str, Any]:
    """The road graph of a layout: nodes {name: [x, z]} and edges [[a, b]] in build order, every
    edge leaving a node built before it. `heading` points into the area (degrees from +x)."""
    reach = max(float(area.get("length") or 0), float(area.get("width") or 0))
    steps = max(1, int(reach // SPACING) + 1)
    if layout in ("grid", "bridgehead"):
        depth = min(steps, BRIDGEHEAD_DEPTH) if layout == "bridgehead" else steps
        across = range(-1, 2) if layout == "bridgehead" else range(-steps, steps + 1)
        nodes = _lattice(entry, heading, range(0, depth), across, SPACING, SPACING, area)
        edges = [(a, b) for a in nodes for b in ((a[0] + 1, a[1]), (a[0], a[1] + 1)) if b in nodes]
    elif layout == "industrial":
        rows = max(1, int(reach // INDUSTRIAL_SPACING) + 1)
        nodes = _lattice(entry, heading, range(0, steps), range(-rows, rows + 1), SPACING, INDUSTRIAL_SPACING, area)
        # roads along the axis on every row, across only every other step: long, deep lots
        edges = [(a, b) for a in nodes for b in ((a[0] + 1, a[1]),) if b in nodes] + \
                [(a, b) for a in nodes for b in ((a[0], a[1] + 1),) if b in nodes and a[0] % 2 == 0]
    elif layout == "corridor":
        nodes = _lattice(entry, heading, range(0, steps), range(-1, 2), SPACING, 48.0, area)
        edges = [(a, b) for a in nodes for b in ((a[0] + 1, a[1]),) if b in nodes and a[1] == 0] + \
                [((i, 0), (i, j)) for (i, j) in nodes if j != 0 and (i, 0) in nodes and i % 2 == 1]
    elif layout == "ring":
        center = (area["center"]["x"], area["center"]["z"])
        radius = max(SPACING, math.dist(entry, center))
        start = math.atan2(entry[1] - center[1], entry[0] - center[0])
        count = max(3, int(2 * math.pi * radius / SPACING))
        nodes = {}
        for k in range(-count // 2, count // 2 + 1):
            for ring, scale in ((0, 1.0), (1, (radius + SPACING) / radius)):
                angle = start + k * SPACING / radius + (0.03 if k % 2 else -0.03) * (k != 0)
                point = (center[0] + math.cos(angle) * radius * scale, center[1] + math.sin(angle) * radius * scale)
                if (k, ring) == (0, 0) or _inside(point, area):
                    nodes[(k, ring)] = (round(point[0], 2), round(point[1], 2))
        nodes[(0, 0)] = (round(entry[0], 2), round(entry[1], 2))
        edges = [(a, b) for a in nodes for b in ((a[0] + 1, 0), (a[0] - 1, 0)) if a[1] == 0 and b in nodes
                 and abs(b[0]) > abs(a[0])] + [((k, 0), (k, 1)) for (k, r) in nodes if r == 1 and (k, 0) in nodes]
    else:  # fishbone
        nodes = _lattice(entry, heading, range(0, steps), range(-1, 2), SPACING, min(160.0, max(48.0, float(
            area.get("width") or 0) / 2 - 8)), area)
        edges = [(a, b) for a in nodes for b in ((a[0] + 1, a[1]),) if b in nodes and a[1] == 0] + \
                [((i, 0), (i, j)) for (i, j) in nodes if j != 0 and (i, 0) in nodes]
    nodes, edges = _reachable(nodes, edges)
    order = _build_order(edges)
    names = {key: f"{key[0]},{key[1]}" for key in nodes}
    return {"layout": layout, "entry": names[(0, 0)], "nodes": {names[k]: list(v) for k, v in nodes.items()},
            "edges": [[names[a], names[b]] for a, b in order]}


def _build_order(edges: list[tuple]) -> list[tuple]:
    """Breadth first from the entry, each edge oriented from the node already reached."""
    order, reached, remaining = [], {(0, 0)}, list(edges)
    while remaining:
        progressed = False
        for edge in list(remaining):
            a, b = edge
            if a in reached or b in reached:
                order.append((a, b) if a in reached else (b, a))
                reached |= {a, b}
                remaining.remove(edge)
                progressed = True
        if not progressed:
            break
    return order

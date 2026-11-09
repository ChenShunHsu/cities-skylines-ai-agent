"""Build a layout's road graph (mayor.layouts) a few edges a wake.

Each edge leaves a node already built, from where that node actually ended up, and keeps the
layout's line: edges longer than a piece are laid in two halves with a slight bend, and an edge
the ground refuses straight is laid as an arc through the same two nodes (roads.lay_arc), never
steered off its line. An edge to a node already built must end on it, closing a block. An edge that
cannot be laid is marked failed, and edges that needed its far node are skipped. Progress is kept in the city
state, so the next wake continues where this one stopped.
"""

from __future__ import annotations

import math
from typing import Any

from loop import roads
from loop.city import City

PREFAB = "Basic Road"
SPLIT = roads.MAX_PIECE  # longer edges are laid as two halves


def _halves(start: tuple[float, float], end: tuple[float, float]) -> list[tuple[float, float]]:
    if math.dist(start, end) <= SPLIT:
        return [end]
    middle = ((start[0] + end[0]) / 2, (start[1] + end[1]) / 2)
    length = math.dist(start, end)
    side = 0.05 * length  # about a 6 degree bend at the middle joint
    bent = (middle[0] - (end[1] - start[1]) / length * side, middle[1] + (end[0] - start[0]) / length * side)
    return [bent, end]


def _paths(start: tuple[float, float], target: tuple[float, float], free_end: bool) -> list[list[tuple[float, float]]]:
    """The way to the target, on the layout's line: an edge is never steered or shortened off it."""
    return [_halves(start, target)]


def grow(city: City, name: str, graph: dict[str, Any] | None, budget: int, label: str) -> dict[str, Any]:
    """Lay up to `budget` edges of the named district (created from `graph` the first time)."""
    state = city.load_state(f"district-{name}", {})
    if not state.get("graph"):
        state = {"graph": graph, "built": {graph["entry"]: graph["nodes"][graph["entry"]]}, "edges": {}}
    nodes, built, done = state["graph"]["nodes"], state["built"], state["edges"]
    report: dict[str, Any] = {"layout": state["graph"]["layout"], "built": [], "failed": []}
    for a, b in state["graph"]["edges"]:
        key = f"{a}|{b}"
        if key in done or a not in built:
            continue
        if len(report["built"]) + len(report["failed"]) >= budget:
            break
        start = tuple(built[a])
        target = tuple(built[b]) if b in built else tuple(nodes[b])
        outcome: dict[str, Any] = {"ok": False, "stage": "preview"}
        for path in _paths(start, target, b not in built):
            outcome = roads.follow(f"{label}-{a}-{b}", PREFAB, start, path, city)
            if outcome["ok"] or not roads.retryable(outcome):
                break
        if not outcome["ok"] and roads.retryable(outcome):
            curved = roads.lay_arc(f"{label}-{a}-{b}", PREFAB, start, target, city)
            if curved["ok"]:
                outcome = curved
                report.setdefault("curved", []).append(key)
        if outcome["ok"]:
            done[key] = "ok"
            built.setdefault(b, [outcome["end"]["x"], outcome["end"]["z"]])
            report["built"].append(key)
        elif outcome.get("stage") == "busy":
            # the game thread was too busy to build it: the edge stays open for the next wake, and
            # this wake lays no more
            report["failed"].append({"edge": key, "stage": "busy"})
            break
        else:
            done[key] = outcome.get("stage") or "failed"
            report["failed"].append({"edge": key, "stage": outcome.get("stage"),
                                     **({"removed": True} if outcome.get("removed") else {})})
        city.save_state(f"district-{name}", state)
    open_edges = [1 for a, b in state["graph"]["edges"] if f"{a}|{b}" not in done and a in built]
    report["ended"] = not open_edges
    report["nodesBuilt"] = len(built)
    report["edgesLeft"] = sum(1 for a, b in state["graph"]["edges"] if f"{a}|{b}" not in done)
    city.save_state(f"district-{name}", state)
    return report

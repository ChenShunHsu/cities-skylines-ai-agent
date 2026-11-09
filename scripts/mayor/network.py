"""Which roads reach the outside, as a pure function of /state/networks segments.

A building grows on any zoned cell beside a road, connected or not, and its residents then cannot
reach work, shops or services. Zoning and growth therefore consider only roads in a component of
the road graph (roads, highways and ramps) that contains an outside connection.
"""

from __future__ import annotations

from typing import Any, Iterable


def road_components(segments: Iterable[dict[str, Any]]) -> tuple[set[int], set[int]]:
    """(segments that reach an outside connection, road segments that do not)."""
    roads = [s for s in segments if s.get("service") == "Road"]
    parent: dict[int, int] = {}

    def find(node: int) -> int:
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for segment in roads:
        parent[find(int(segment["startNodeId"]))] = find(int(segment["endNodeId"]))
    outside = {find(int(s[f"{end}NodeId"])) for s in roads for end in ("start", "end") if s.get(f"{end}Outside")}
    reached = {int(s["id"]) for s in roads if find(int(s["startNodeId"])) in outside}
    return reached, {int(s["id"]) for s in roads} - reached

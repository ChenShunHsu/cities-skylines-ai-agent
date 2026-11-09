"""Congestion hotspots and measured junction-control trials, as pure functions.

Vehicles pick their route at trip start and never reroute around a queue, so a
congested road is rarely alone: its neighbours queue too. Grouping congested
segments that share nodes turns a list of busy roads into a few hotspots, each
with the junctions where a control change could help and the roads that could
be widened.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable


# Upgrades that keep the road 16m wide, so nearby buildings stay put; the
# upgrade still rebuilds zone blocks and needs the zoning check afterwards.
SAME_WIDTH_UPGRADES = {"Basic Road": "Small 4 Lane Road"}


def congestion_hotspots(traffic_segments: Iterable[dict[str, Any]],
                        road_segments: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group congested segments that share a node, largest hotspot first.

    traffic_segments are /state/traffic rows already filtered to congested ones;
    road_segments are /state/networks rows (ids and end nodes) for every road."""
    roads = {int(segment["id"]): segment for segment in road_segments}
    degree: dict[int, int] = defaultdict(int)
    for segment in roads.values():
        degree[int(segment["startNodeId"])] += 1
        degree[int(segment["endNodeId"])] += 1
    congested = {int(row["id"]): row for row in traffic_segments if int(row["id"]) in roads}
    parent = {segment_id: segment_id for segment_id in congested}

    def root(segment_id: int) -> int:
        while parent[segment_id] != segment_id:
            parent[segment_id] = parent[parent[segment_id]]
            segment_id = parent[segment_id]
        return segment_id

    by_node: dict[int, list[int]] = defaultdict(list)
    for segment_id in congested:
        road = roads[segment_id]
        by_node[int(road["startNodeId"])].append(segment_id)
        by_node[int(road["endNodeId"])].append(segment_id)
    for members in by_node.values():
        for other in members[1:]:
            parent[root(other)] = root(members[0])
    groups: dict[int, list[int]] = defaultdict(list)
    for segment_id in congested:
        groups[root(segment_id)].append(segment_id)

    hotspots = []
    for members in groups.values():
        members.sort()
        rows = [congested[segment_id] for segment_id in members]
        nodes = sorted({int(roads[segment_id][end]) for segment_id in members
                        for end in ("startNodeId", "endNodeId")})
        length = sum(float(row.get("length") or 0.0) for row in rows)
        busiest = max(rows, key=lambda row: (float(row.get("density") or 0), float(row.get("trafficBuffer") or 0)))
        prefabs: dict[str, int] = defaultdict(int)
        for row in rows:
            prefabs[str(row.get("prefab"))] += 1
        hotspots.append({
            "segmentIds": members,
            "segmentCount": len(members),
            "lengthMeters": round(length, 1),
            "meanDensity": round(sum(float(row.get("density") or 0) for row in rows) / len(rows), 1),
            "maxDensity": max(float(row.get("density") or 0) for row in rows),
            "prefabs": dict(sorted(prefabs.items())),
            "junctionNodeIds": [node for node in nodes if degree[node] >= 3],
            "busiestSegmentId": int(busiest["id"]),
            "center": busiest.get("position"),
            "upgradeCandidates": [
                {"segmentId": int(row["id"]), "from": row.get("prefab"),
                 "roadPrefab": SAME_WIDTH_UPGRADES[str(row.get("prefab"))]}
                for row in rows if str(row.get("prefab")) in SAME_WIDTH_UPGRADES
            ],
        })
    hotspots.sort(key=lambda hotspot: (-hotspot["segmentCount"], -hotspot["lengthMeters"]))
    return hotspots


def traffic_sample(traffic: dict[str, Any], watched: Iterable[int]) -> dict[str, Any]:
    """One /state/traffic reading reduced to the watched roads and citywide flow.

    A watched road missing from the rows (they are filtered by minimumDensity)
    reads as 0, so callers ask for minimumDensity 0 and a large enough limit."""
    density = {int(row["id"]): float(row.get("density") or 0) for row in traffic.get("segments", [])}
    watched = [int(segment_id) for segment_id in watched]
    maximum = float(traffic.get("maximumTrafficFlow") or 0)
    return {
        "meanDensity": sum(density.get(segment_id, 0.0) for segment_id in watched) / max(1, len(watched)),
        "flowRatio": float(traffic.get("totalTrafficFlow") or 0) / maximum if maximum > 0 else 0.0,
    }


def mean_samples(samples: list[dict[str, Any]]) -> dict[str, float]:
    count = max(1, len(samples))
    return {
        "meanDensity": round(sum(sample["meanDensity"] for sample in samples) / count, 2),
        "flowRatio": round(sum(sample["flowRatio"] for sample in samples) / count, 4),
        "samples": len(samples),
    }


def trial_verdict(before: dict[str, float], after: dict[str, float],
                  minimum_density_gain: float, maximum_flow_drop: float) -> dict[str, Any]:
    """Keep a change only on a clear win: traffic varies with the time of day, and
    a junction change that only looks neutral can still move the queue elsewhere."""
    density_gain = before["meanDensity"] - after["meanDensity"]
    flow_change = after["flowRatio"] - before["flowRatio"]
    if flow_change < -maximum_flow_drop:
        return {"keep": False, "reason": "citywideFlowFell", "densityGain": round(density_gain, 2),
                "flowChange": round(flow_change, 4)}
    if density_gain < minimum_density_gain:
        return {"keep": False, "reason": "watchedDensityDidNotImprove", "densityGain": round(density_gain, 2),
                "flowChange": round(flow_change, 4)}
    return {"keep": True, "reason": "improved", "densityGain": round(density_gain, 2),
            "flowChange": round(flow_change, 4)}

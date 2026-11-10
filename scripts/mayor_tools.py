#!/usr/bin/env python3
"""Typed tool adapter and persistent memory helpers for the CS1 mayor agent."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from city_assessment import assess_city
from mayor import gameplay_rules
from mayor import crossing as mayor_crossing
from mayor import expansion as mayor_expansion
from mayor import land_use as mayor_land_use
from mayor import layouts as mayor_layouts
from mayor import network as mayor_network
from mayor import memory as mayor_memory
from mayor import problems as mayor_problems
from mayor import regression as mayor_regression
from mayor import schema as mayor_schema
from mayor import service_options as mayor_service_options
from mayor import bypass as mayor_bypass
from mayor import traffic as mayor_traffic
from mayor import traffic_remedies as mayor_traffic_remedies
from mayor import transit as mayor_transit
from mayor import transport as mayor_transport
from road_attempts import (
    clear_successful_attempt,
    geometry_key,
    is_rejected,
    load_failures,
    record_failure,
    recovery_verified,
    rejected_keys,
)
from road_attempts import _write as write_road_state
from spatial_model import (
    annotate_actionable_zone_block_clearance,
    build_layered_buildings,
    build_layered_roads,
    generate_local_development_road_candidates,
    inspect_planning_area,
    inspect_building_road_clearance,
    inspect_crossings,
    latest_snapshot,
    load_snapshot,
    propose_development_plans,
    segment_intersects_building_footprint,
)


ROOT = Path(__file__).resolve().parents[1]
DEFINITIONS_PATH = ROOT / "references" / "mayor-tools.json"
CLUSTERS_PATH = ROOT / "references" / "mayor-tool-clusters.json"
BASE_URL = os.environ.get("CS1_AGENT_BASE_URL", "http://127.0.0.1:32123").rstrip("/")
DEFAULT_MEMORY_ROOT = Path(
    os.environ.get(
        "LOCALAPPDATA",
        str(Path.home() / ".local" / "share"),
    )
) / "Colossal Order" / "Cities_Skylines" / "SkylinesAgentBridge"
MEMORY_PATH = Path(
    os.environ.get("CS1_MAYOR_STATE_PATH", str(DEFAULT_MEMORY_ROOT / "mayor-state.json"))
)
FISCAL_COOLDOWN_MINUTES = 60
# the cooldown also ends once this much game time has passed since the change: a loop that plays a
# game month in three real minutes waited twenty months between budget changes while the city lost
# 40,000 a month (live); a player looks at the budget again about monthly
FISCAL_COOLDOWN_GAME_DAYS = 30
ROAD_FAILURE_PATH = ROOT / ".runtime" / "road-failures.json"
ROAD_CORRIDOR_HINT_PATH = ROOT / ".runtime" / "road-corridor-hint.json"
ROAD_SEARCH_PROGRESS_PATH = ROOT / ".runtime" / "road-search-progress.json"
MONEY_INTERNAL_SCALE = 100.0
MAP_TILE_SIZE = 1920.0  # one of the 5x5 purchasable map tiles
REGRESSION_BASELINES_PATH = Path(
    os.environ.get(
        "CS1_REGRESSION_BASELINES_PATH",
        str(DEFAULT_MEMORY_ROOT / "regression-baselines.json"),
    )
)
POWER_LINE_BUILDING_CLEARANCE_METERS = 4.0
ORIGINAL_NODE_TRANSITION_MINIMUM_CLEARANCE = -0.5  # Bridge ExistingNodeTransitionMinimumClearance
MAYOR_PROFILE = frozenset({
    "assess_city", "observe_city", "validate_progression_mode",
    "load_mayor_memory", "save_mayor_memory", "set_simulation_speed",
})


BridgeRequestError = mayor_transport.BridgeRequestError


def _default_memory() -> dict[str, Any]:
    return mayor_memory.default_memory(FISCAL_COOLDOWN_MINUTES)


def _read_only(method: str, path: str) -> bool:
    return method == "GET" or path.startswith("/commands/preview-")


UNRUN_RESENDS = 2
UNRUN_RESEND_SECONDS = 5.0
# the request never left this machine: Windows had no socket buffer or local port for it (10055,
# 10048). Each request leaves the game process a bound socket that Windows takes back within about
# two minutes, so hundreds of placement previews in a row run it short (live: 850 bound sockets,
# every retry of a wake's service site search failed); nothing ran, so any request is resent
NO_SOCKET = ("WinError 10055", "WinError 10048")
NO_SOCKET_RESENDS = 4
NO_SOCKET_SECONDS = 20.0


def _send(method: str, path: str, query: dict[str, Any] | None, body: dict[str, Any] | None) -> Any:
    """A request, sent again when the Bridge withdrew it because the game thread did not start it
    within its wait: nothing ran, so a change is sent again too (live: at 28,000 people a ruin
    clearing, a power repair and a road's settlement check each failed a whole wake this way). A
    read whose answer did not come in time is asked once more; a change then may have run."""
    resends = 0
    no_socket = 0
    while True:
        try:
            return _send_once(method, path, query, body)
        except BridgeRequestError as error:
            if "command was not run" not in str(error) or resends >= UNRUN_RESENDS:
                raise
        except RuntimeError as error:
            if not any(code in str(error) for code in NO_SOCKET) or no_socket >= NO_SOCKET_RESENDS:
                raise
            no_socket += 1
            time.sleep(NO_SOCKET_SECONDS)
            continue
        except TimeoutError:
            if not _read_only(method, path) or resends >= 1:
                raise
        resends += 1
        time.sleep(UNRUN_RESEND_SECONDS)


def _send_once(method: str, path: str, query: dict[str, Any] | None, body: dict[str, Any] | None) -> Any:
    try:
        return mayor_transport.request(BASE_URL, method, path, query=query, body=body)
    except BridgeRequestError as error:
        # a game method that hands work back to the thread the Bridge already runs on throws this
        # now and then (live: one placement preview among hundreds); a read is simply asked again
        if not _read_only(method, path) or "Already in the same thread" not in str(error.detail.get("error")):
            raise
        return mayor_transport.request(BASE_URL, method, path, query=query, body=body)


# Large reads a wake repeats between changes (live: /state/networks?limit=20000 is 2.3 MB and
# 0.19 s of the game thread, asked for by several steps of one wake), kept until the city may have
# changed. Every request goes through _request, so the cache cannot outlive a change: any request
# that is not read-only (a mutation, a dry run, a speed change; previews are read-only) empties it
# before and after it is sent. A read is kept only while the simulation is known to be paused
# (the last speed request this process sent paused it), only from READ_CACHE_SETTLE_SECONDS after
# the last change (work the game finishes after answering has landed by then), and for at most
# READ_CACHE_TTL_SECONDS (a player's own change while paused). Off unless a process turns it on:
# the loop's wake does (enable_read_cache); a planner calling tools reads live state every time.
CACHED_READS = frozenset({"/state/networks", "/state/growables", "/state/facilities", "/state/zone-blocks"})
READ_CACHE_TTL_SECONDS = 30.0
READ_CACHE_SETTLE_SECONDS = 3.0
_read_cache: dict[str, Any] = {"enabled": False, "paused": False, "changedAt": float("-inf"), "entries": {}}


def enable_read_cache(enabled: bool = True) -> None:
    _read_cache.update(enabled=enabled, paused=False, changedAt=float("-inf"))
    _read_cache["entries"].clear()


def _changed(path: str, body: dict[str, Any] | None) -> None:
    _read_cache["entries"].clear()
    _read_cache["changedAt"] = time.monotonic()
    if path == "/commands/set-simulation-speed":
        _read_cache["paused"] = isinstance(body, dict) and body.get("paused") is True


def _request(method: str, path: str, *, query: dict[str, Any] | None = None, body: dict[str, Any] | None = None) -> Any:
    if not _read_only(method, path):
        _changed(path, None)  # a speed change in flight leaves the pause unknown
        sent = False
        try:
            result = _send(method, path, query, body)
            sent = True
            return result
        finally:
            _changed(path, body if sent else None)
    if not (_read_cache["enabled"] and method == "GET" and path in CACHED_READS):
        return _send(method, path, query, body)
    key = path + "?" + json.dumps(query or {}, sort_keys=True, default=str)
    now = time.monotonic()
    entry = _read_cache["entries"].get(key)
    if entry is not None and now - entry[0] <= READ_CACHE_TTL_SECONDS:
        return json.loads(entry[1])  # a copy: callers may change what they read
    result = _send(method, path, query, body)
    if _read_cache["paused"] and now - _read_cache["changedAt"] >= READ_CACHE_SETTLE_SECONDS:
        _read_cache["entries"][key] = (now, json.dumps(result))
    return result


def _block_zoned_as(block_id: int, zone: str) -> bool:
    """Whether every valid cell of the block already carries the zone."""
    block = next((item for item in _request("GET", "/state/zone-blocks", query={"limit": 5000}).get("blocks", [])
                  if int(item.get("blockId") or -1) == block_id), None)
    return bool(block) and block.get("zone") == zone and int(block.get("validCells") or 0) > 0 \
        and int(block.get("zonedCells") or 0) >= int(block.get("validCells") or 0)


def _definitions() -> list[dict[str, Any]]:
    return mayor_schema.load_definitions(DEFINITIONS_PATH)


def _cluster_catalog() -> dict[str, Any]:
    return mayor_schema.load_cluster_catalog(CLUSTERS_PATH, _definitions())


def _tool_clusters() -> dict[str, frozenset[str]]:
    return {
        cluster["name"]: frozenset(cluster["tools"])
        for cluster in _cluster_catalog()["clusters"]
    }


def _select_definitions(
    profile: str = "all",
    names: list[str] | None = None,
    cluster: str | None = None,
) -> list[dict[str, Any]]:
    return mayor_schema.select_definitions(
        _definitions(), profile=profile, names=names, mayor_profile=MAYOR_PROFILE,
        cluster=cluster, tool_clusters=_tool_clusters(),
    )


def _cluster_summary() -> dict[str, Any]:
    definitions = _definitions()
    catalog = _cluster_catalog()
    tool_clusters = {
        cluster["name"]: frozenset(cluster["tools"])
        for cluster in catalog["clusters"]
    }
    compact_all = len(json.dumps(
        {"tools": definitions}, ensure_ascii=False, separators=(",", ":")
    ))
    clusters = []
    for cluster in catalog["clusters"]:
        selected = mayor_schema.select_definitions(
            definitions,
            profile="all",
            names=None,
            mayor_profile=MAYOR_PROFILE,
            cluster=cluster["name"],
            tool_clusters=tool_clusters,
        )
        compact_size = len(json.dumps(
            {"tools": selected}, ensure_ascii=False, separators=(",", ":")
        ))
        clusters.append({
            **cluster,
            "toolCount": len(selected),
            "compactDefinitionCharacters": compact_size,
            "characterReductionVsAll": round(1 - compact_size / compact_all, 3),
        })
    return {
        "ok": True,
        "schemaVersion": catalog["schemaVersion"],
        "allToolCount": len(definitions),
        "allCompactDefinitionCharacters": compact_all,
        "measurement": "compact JSON characters; not a tokenizer-specific token count",
        "clusters": clusters,
    }


def _schema_for(name: str) -> dict[str, Any]:
    return mayor_schema.schema_for(_definitions(), name)


def _validate_type(value: Any, expected: str, path: str) -> None:
    mayor_schema.validate_type(value, expected, path)


def _validate_arguments(name: str, arguments: dict[str, Any]) -> None:
    mayor_schema.validate_arguments(_definitions(), name, arguments)


def _validate_schema(value: Any, schema: dict, root: dict, path: str) -> None:
    mayor_schema.validate_schema(value, schema, root, path)


def _load_memory() -> dict[str, Any]:
    return mayor_memory.load_memory(MEMORY_PATH, _default_memory)


def _city_identity(snapshot: dict[str, Any]) -> dict[str, str]:
    return mayor_memory.city_identity(snapshot)


# The city this process plays, once it is known. Only /state/mayor-snapshot reports the instance
# id, and that read reruns every anomaly scan on the game thread (live: 3.8 s with detailLimit=0,
# 3.5 s of it the building anomalies, against 0.07 s for /state/city-health); regression
# baselines, mayor memory and road failure records each asked it again only to key their records,
# and a wake of 15-27 changes spent minutes of game thread on it (live: a wake stopped when one of
# those reads timed out). A process that cannot see the city change under it (one loop wake: a
# city is never loaded mid-wake) remembers the identity it read (remember_city); any other process
# reads it live every time, and a recovery that compares against the live city still does.
_remembered_city: dict[str, Any] = {"identity": None}


def remember_city(snapshot: dict[str, Any]) -> dict[str, str] | None:
    """Keep the identity of the city a snapshot reports for this process; None (and nothing kept)
    when the snapshot names no city, so the next record reads it live and refuses as before."""
    try:
        identity = _city_identity(snapshot)
    except RuntimeError:
        _remembered_city["identity"] = None
        return None
    _remembered_city["identity"] = identity
    return dict(identity)


def forget_city() -> None:
    _remembered_city["identity"] = None


def _current_city_identity(read: Callable[..., Any] | None = None) -> dict[str, str]:
    """The city's identity: remembered for this process, or read live (through read when given)."""
    if _remembered_city["identity"] is not None:
        return dict(_remembered_city["identity"])
    return _city_identity((read or _request)("GET", "/state/mayor-snapshot", query={"detailLimit": 0}))


def city_health() -> dict[str, Any]:
    """The snapshot's cityHealth section (cash, population, utilities, services) on its own: the
    snapshot embeds this same /state/city-health answer (MayorSnapshotCommands), but around it
    reruns every anomaly scan (live: 3.8 s of game thread against 0.07 s). Raw money, as the
    snapshot reports it."""
    return _request("GET", "/state/city-health")


def _load_city_scoped_memory() -> tuple[dict[str, Any], dict[str, Any]]:
    return mayor_memory.load_city_scoped_memory(
        _request, _load_memory, _save_memory, _default_memory, identity=_current_city_identity
    )


def _save_city_scoped_memory(state: dict[str, Any]) -> dict[str, Any]:
    return mayor_memory.save_city_scoped_memory(state, _request, _save_memory, identity=_current_city_identity)


def _save_memory(state: dict[str, Any]) -> dict[str, Any]:
    return mayor_memory.save_memory(MEMORY_PATH, state, _default_memory)


def _score_candidates(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    ranked = []
    for candidate in candidates:
        hard_blocked = bool(candidate.get("hardBlocked", False))
        score = (
            0.25 * float(candidate["urgency"])
            + 0.25 * float(candidate["impact"])
            + 0.15 * float(candidate["confidence"])
            + 0.20 * float(candidate["alignment"])
            - 0.08 * float(candidate["cost"])
            - 0.07 * float(candidate["risk"])
            - 0.10 * float(candidate["repetitionPenalty"])
        )
        item = dict(candidate)
        item["score"] = round(-1000.0 if hard_blocked else score, 3)
        item["eligible"] = not hard_blocked
        ranked.append(item)
    ranked.sort(key=lambda item: (-item["score"], item["id"]))
    return {"ok": True, "formulaVersion": 1, "ranked": ranked}


def _inspect_road_result(
    segment_id: int,
    include_dead_ends: bool,
    allowed_terminal_node_id: int = 0,
    outside_access_requirement: str = "two-way",
) -> dict[str, Any]:
    if outside_access_requirement not in {"two-way", "inbound", "outbound"}:
        raise ValueError("Invalid outside access requirement")
    intermediate = outside_access_requirement != "two-way"
    if intermediate and (not allowed_terminal_node_id or not include_dead_ends):
        raise ValueError("Intermediate access QA requires an explicit terminal and dead-end inspection")
    segment = _request("GET", "/state/network-segment-details", query={"id": segment_id})
    start_node = _request("GET", "/state/network-node-details", query={"id": segment["startNodeId"]})
    end_node = _request("GET", "/state/network-node-details", query={"id": segment["endNodeId"]})
    anomalies = _request(
        "GET",
        "/state/road-anomalies",
        query={"limit": 500, "includeDeadEnds": str(include_dead_ends).lower()},
    )
    reachability = _request(
        "GET",
        "/state/road-reachability",
        query={"id": segment_id},
    )
    external_connections = _request(
        "GET",
        "/state/external-connections",
        query={"limit": 50},
    )
    related = [
        item
        for item in anomalies.get("anomalies", [])
        if item.get("segmentId") == segment_id
        or item.get("id") == segment_id
        or item.get("ownSegmentId") == segment_id
        or item.get("nodeId") in (segment["startNodeId"], segment["endNodeId"])
    ]
    if allowed_terminal_node_id and allowed_terminal_node_id not in (
        segment["startNodeId"],
        segment["endNodeId"],
    ):
        raise ValueError("terminalNodeId must be one endpoint of segmentId")
    accepted_terminal_anomalies = [
        item
        for item in related
        if item.get("type") == "deadEndRoad"
        and item.get("nodeId") == allowed_terminal_node_id
    ]
    # the raw ground beside a road (a hillside cut, a bank) and the height of other roads say
    # nothing a player's drag would not: the game shapes the ground to the road. Only ground drawn
    # over the road (roadBelowTerrain) blocks; these are reported (live: four collector steps
    # along a 20m bank failed as cliffs and forked one node)
    terrain_notes = [item for item in related if item.get("type") in TERRAIN_NOTE_ANOMALIES]
    blocking_related = [item for item in related
                        if item not in accepted_terminal_anomalies and item not in terrain_notes]
    dx = float(segment["end"]["x"]) - float(segment["start"]["x"])
    dy = float(segment["end"]["y"]) - float(segment["start"]["y"])
    dz = float(segment["end"]["z"]) - float(segment["start"]["z"])
    flat_length = (dx * dx + dz * dz) ** 0.5
    grade = abs(dy) / flat_length if flat_length else 999.0
    vehicle_directions = {
        lane.get("finalDirection")
        for lane in segment.get("prefabLanes", [])
        if lane.get("laneType") == "Vehicle"
    }
    terrain_profile = segment.get("terrainProfile")
    terrain_profile_reported = isinstance(terrain_profile, dict)
    is_ground_road = segment.get("isGroundRoad")
    try:
        if (not terrain_profile_reported
                or not isinstance(is_ground_road, bool)
                or terrain_profile.get("required") is not is_ground_road):
            raise RuntimeError("Road kind and terrain requirement are inconsistent.")
        _validated_native_road_terrain_profile(
            terrain_profile, "inspection", require_ground=is_ground_road)
        terrain_profile_safe = True
    except RuntimeError:
        terrain_profile_safe = False
    checks = {
        "created": "Created" in str(segment.get("flags", "")),
        "noSegmentProblems": not bool(segment.get("problems")),
        "laneCountMatchesPrefab": segment.get("actualLaneCount") == segment.get("prefabLaneCount"),
        "reasonableLength": flat_length >= 4,
        "reasonableGrade": grade <= 0.25,
        "bothEndpointsConnected": start_node.get("connectCount", 0) > 0 and end_node.get("connectCount", 0) > 0,
        "actualTerrainProfileReported": terrain_profile_reported,
        "actualTerrainProfileSafe": terrain_profile_safe,
        "twoWayOutsideReachable": bool(reachability.get("safeForTwoWayFreight", False)),
        "noFocusedAnomalies": len(blocking_related) == 0,
    }
    acceptance_checks = dict(checks)
    if intermediate:
        terminal = start_node if allowed_terminal_node_id == segment["startNodeId"] else end_node
        anchor = end_node if allowed_terminal_node_id == segment["startNodeId"] else start_node
        path = reachability.get("inboundFromOutside" if outside_access_requirement == "inbound" else "outboundToOutside", {})
        # A construction stage can preserve one directed highway connection.
        # Keep the full two-way result visible; this is not a freight-ready entrance.
        acceptance_checks.pop("twoWayOutsideReachable")
        acceptance_checks["declaredTerminalIsDeadEnd"] = terminal.get("connectCount") == 1
        acceptance_checks["anchorRemainsConnected"] = anchor.get("connectCount", 0) >= 2
        acceptance_checks["bidirectionalIntermediateRoad"] = bool(segment.get("isBidirectional", False))
        acceptance_checks["requiredOutsidePath"] = (
            path.get("found") is True and path.get("outsideNodeId", 0) > 0
            and path.get("segmentCount", 0) > 0
        )
    accepted = all(acceptance_checks.values())
    return {
        "ok": accepted,
        "verificationScope": "intermediateOutsideAccess" if intermediate else "completeOutsideAccess",
        "outsideAccessRequirement": outside_access_requirement,
        "fullOutsideAccessVerified": all(checks.values()),
        "acceptanceChecks": acceptance_checks,
        "segment": segment,
        "startNode": start_node,
        "endNode": end_node,
        "geometry": {
            "start": segment["start"],
            "middle": segment.get("middle"),
            "end": segment["end"],
            "startDirection": segment.get("startDirection"),
            "endDirection": segment.get("endDirection"),
            "flatLength": round(flat_length, 3),
            "grade": round(grade, 4),
            "isUnderground": bool(segment.get("isUnderground", False)),
            "isTunnel": bool(segment.get("isTunnel", False)),
            "terrainProfile": terrain_profile,
        },
        "directionality": segment.get("directionality"),
        "isBidirectional": bool(segment.get("isBidirectional", False)),
        "isOneWay": bool(segment.get("isOneWay", False)),
        "vehicleDirections": sorted(direction for direction in vehicle_directions if direction),
        "checks": checks,
        "focusedAnomalies": blocking_related,
        "acceptedTerminalAnomalies": accepted_terminal_anomalies,
        "terrainNotes": terrain_notes,
        "roadReachability": reachability,
        "externalConnections": external_connections,
        "requiresAdditionalTelemetry": not accepted,
    }


def _annotate_display_money(snapshot: dict[str, Any]) -> dict[str, Any]:
    cash = snapshot.get("cityHealth", {}).get("cash")
    if isinstance(cash, dict):
        raw_amount = float(cash.get("amount", 0) or 0)
        raw_period_net = float(cash.get("periodNet", 0) or 0)
        cash["amountRaw"] = raw_amount
        cash["amountDisplay"] = raw_amount / MONEY_INTERNAL_SCALE
        cash["periodNetRaw"] = raw_period_net
        cash["periodNetDisplay"] = raw_period_net / MONEY_INTERNAL_SCALE
        cash["internalUnitsPerDisplayUnit"] = int(MONEY_INTERNAL_SCALE)
    return snapshot


def _save_status(name: str | None = None) -> dict[str, Any] | None:
    """Whether a save is being written and, given a name, that save's file: the Bridge answers it
    off the game thread, so it stays quick during a save. A full /state/saves listing ran on the
    game thread and took 3 s per answer during a save (live, ~1150 saves), and every mutation
    asked for it several times. None from a Bridge that predates the endpoint."""
    try:
        return _request("GET", "/state/save-status", query={"name": name} if name else None)
    except BridgeRequestError as error:
        if error.detail.get("error") == "Not found":
            return None
        raise


def _checkpoint_exists(name: str) -> bool:
    status = _save_status(name)
    if status is None:
        saves = _request("GET", "/state/saves")
        return name.lower() in json.dumps(saves, ensure_ascii=False).lower()
    return bool((status.get("save") or {}).get("exists"))


def _save_record(name: str) -> tuple[dict[str, Any] | None, bool]:
    """The named save's file record (None until it exists) and whether a save is in progress."""
    status = _save_status(name)
    if status is not None:
        record = status.get("save") or {}
        return (record if record.get("exists") else None), bool(status.get("isSaving", False))
    saves = _request("GET", "/state/saves")
    record = next(
        (
            item
            for item in saves.get("saves", [])
            if str(item.get("name", "")).lower() == name.lower()
        ),
        None,
    )
    return record, bool(saves.get("isSaving", False))


def _wait_checkpoint(name: str, timeout_seconds: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    stable_signature: tuple[Any, Any] | None = None
    stable_reads = 0
    while time.monotonic() < deadline:
        record, saving = _save_record(name)
        if record is not None and not saving:
            signature = (
                record.get("length"),
                record.get("lastWriteTimeUtc"),
            )
            if signature == stable_signature:
                stable_reads += 1
            else:
                stable_signature = signature
                stable_reads = 1
            # SavePanel can clear isSaving before the package stream is fully
            # settled. Four identical observations provide a mutation barrier.
            if stable_reads >= 4:
                return True
        else:
            stable_signature = None
            stable_reads = 0
        time.sleep(0.4)
    return False


ZONING_SUPPORT_MARGIN_METERS = 96.0


def _verify_rebuilt_zoning(result: dict[str, Any], segment_ids: list[int], action: str) -> dict[str, Any]:
    """Upgrading or splitting rebuilds the segments' zone blocks; cells near a changed node can come
    back unzoned or invalid, and native Building.CheckZoning then demolishes the growable on them.
    Restore the zoning under each such building, or fail so the caller restores the checkpoint."""
    if not result.get("ok") or not segment_ids:
        return result
    points = []
    for segment_id in segment_ids:
        segment = _request("GET", "/state/network-segment-details", query={"id": segment_id})
        points += [(segment["start"]["x"], segment["start"]["z"]), (segment["end"]["x"], segment["end"]["z"])]
    xs, zs = [x for x, _ in points], [z for _, z in points]
    center = {"x": (min(xs) + max(xs)) / 2, "z": (min(zs) + max(zs)) / 2}
    radius = max(math.dist((center["x"], center["z"]), point) for point in points) + ZONING_SUPPORT_MARGIN_METERS
    query = {**center, "radius": round(radius, 3), "limit": 64}
    before = _request("GET", "/state/zoning-support", query=query)
    repairs = []
    for building in before.get("failing", []):
        if building.get("repairableCells", 0) > 0:
            repair = _request("POST", "/commands/repair-building-zoning",
                              body={"buildingId": building["id"], "confirm": True})
            repairs.append({key: repair.get(key) for key in ("buildingId", "changedCells", "supportedAfter")})
    after = _request("GET", "/state/zoning-support", query=query) if repairs else before
    result["zoningSupport"] = {
        "checkedGrowables": after.get("checkedGrowables"),
        "failingBeforeRepair": [b["id"] for b in before.get("failing", [])],
        "repairs": repairs,
        "failingAfterRepair": [b["id"] for b in after.get("failing", [])],
        "safe": bool(after.get("safe")),
    }
    if not after.get("safe"):
        result["ok"] = False
        result["error"] = (f"{action} left growables without native zoning support; they would be demolished. "
                           "Restore the checkpoint before resuming the simulation.")
    return result


def _require_checkpoint(arguments: dict[str, Any]) -> dict[str, Any]:
    name = str(arguments.pop("checkpointName", "")).strip()
    if not name:
        raise ValueError("checkpointName is required for mutations")
    if not _checkpoint_exists(name):
        raise RuntimeError(f"Verified checkpoint was not found: {name}")
    return arguments


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _fiscal_cooldown_remaining(state: dict[str, Any], now: datetime | None = None) -> int:
    value = state.get("cooldowns", {}).get("fiscalAdjustmentUntil")
    if not value:
        return 0
    current = now or datetime.now(timezone.utc)
    remaining = (_parse_utc(str(value)) - current).total_seconds()
    return max(0, int((remaining + 59) // 60))


def _game_time() -> datetime | None:
    try:
        return datetime.fromisoformat(str(_request("GET", "/state/summary").get("gameTime"))[:19])
    except (BridgeRequestError, RuntimeError, ValueError):
        return None


def _fiscal_game_days_passed(state: dict[str, Any], game_now: datetime | None) -> bool:
    """True once FISCAL_COOLDOWN_GAME_DAYS of game time have passed since the last fiscal change."""
    since = state.get("cooldowns", {}).get("fiscalAdjustmentGameTime")
    if not since or game_now is None:
        return False
    try:
        return (game_now - datetime.fromisoformat(str(since)[:19])).days >= FISCAL_COOLDOWN_GAME_DAYS
    except ValueError:
        return False


def _require_fiscal_cooldown_clear() -> dict[str, Any]:
    state, _ = _load_city_scoped_memory()
    remaining = _fiscal_cooldown_remaining(state)
    if remaining and _fiscal_game_days_passed(state, _game_time()):
        remaining = 0
    if remaining:
        raise RuntimeError(
            f"Fiscal adjustment cooldown is active for about {remaining} more minute(s). "
            "Prefer verified physical growth or wait for new evidence."
        )
    return state


ALL_MILESTONES_POPULATION = 50_000  # the last milestone asks 65,000 people; a town may shrink a little after


def _assess_progression_mode(snapshot: dict[str, Any]) -> dict[str, Any]:
    progression = snapshot.get("progression", {})
    milestone = str(progression.get("currentMilestone", "")).strip()
    capabilities = progression.get("capabilities", {})
    late_capabilities = [
        name
        for name in (
            "highDensityResidential",
            "office",
            "healthcare",
            "education",
            "bus",
            "metro",
            "train",
        )
        if capabilities.get(name) is True
    ]
    health = snapshot.get("cityHealth", {})
    raw_cash = float(health.get("cash", {}).get("amount", 0) or 0)
    display_cash = raw_cash / MONEY_INTERNAL_SCALE
    population = int(health.get("population", 0) or 0)
    reasons = []
    # every milestone passed reads as no current milestone too: a town that size earned its unlocks
    # (live: the last milestone, 65,000 people, passed at 64,948 read, and every change was blocked)
    if not milestone and len(late_capabilities) >= 4 and population < ALL_MILESTONES_POPULATION:
        reasons.append(
            "No current milestone is reported while multiple late-game capabilities are unlocked."
        )
    if population == 0 and display_cash >= 500_000:
        reasons.append("An empty city has at least 500,000 in display cash.")
    return {
        "ok": not reasons,
        "normalProgression": not reasons,
        "currentMilestone": milestone,
        # currentMilestone is the next target (the first not yet passed); this says what it needs
        "nextMilestone": progression.get("nextMilestone"),
        "rawCash": raw_cash,
        "displayCash": display_cash,
        "internalUnitsPerDisplayUnit": int(MONEY_INTERNAL_SCALE),
        "population": population,
        "lateCapabilitiesUnlocked": late_capabilities,
        "reasons": reasons,
    }


# The snapshot capabilities the progression check weighs, as /state/unlocks lists them (the
# snapshot's own flags come from the same UnlockManager calls: MayorSnapshotCommands
# .BuildCapabilitiesJson).
PROGRESSION_UNLOCKS = {
    "highDensityResidential": ("zones", "ResidentialHigh"),
    "office": ("zones", "Office"),
    "healthcare": ("services", "HealthCare"),
    "education": ("services", "Education"),
    "bus": ("subServices", "PublicTransportBus"),
    "metro": ("subServices", "PublicTransportMetro"),
    "train": ("subServices", "PublicTransportTrain"),
}


def _progression_state() -> dict[str, Any]:
    """The snapshot fields the progression check reads (progression and cityHealth), from the two
    light reads that hold them. Nearly every mutating tool checks progression first, and the full
    snapshot reruns every anomaly scan on the game thread (live: 3.8 s with detailLimit=0, against
    0.04 s for /state/unlocks and 0.07 s for /state/city-health); a wake stopped when that read
    timed out before a zoning change."""
    unlocks = _request("GET", "/state/unlocks")
    health = _request("GET", "/state/city-health")
    def unlocked(group: str, name: str) -> bool:
        return any(isinstance(item, dict) and item.get("name") == name and item.get("unlocked") is True
                   for item in unlocks.get(group) or [])
    return {
        "progression": {
            "currentMilestone": unlocks.get("currentMilestone", ""),
            "nextMilestone": unlocks.get("nextMilestone"),
            "capabilities": {flag: unlocked(group, name) for flag, (group, name) in PROGRESSION_UNLOCKS.items()},
        },
        "cityHealth": health,
    }


def _validate_progression_mode() -> dict[str, Any]:
    return _assess_progression_mode(_progression_state())


def _require_normal_progression() -> None:
    assessment = _validate_progression_mode()
    if not assessment["normalProgression"]:
        raise RuntimeError(
            "City mutation blocked because normal milestone progression could not be verified: "
            + " ".join(assessment["reasons"])
        )


def _record_fiscal_adjustment(
    state: dict[str, Any],
    *,
    action: str,
    reason: str,
    expected_outcome: str,
) -> None:
    until = datetime.now(timezone.utc) + timedelta(minutes=FISCAL_COOLDOWN_MINUTES)
    state.setdefault("cooldowns", {})["fiscalAdjustmentUntil"] = until.isoformat()
    game_now = _game_time()
    if game_now is not None:
        state["cooldowns"]["fiscalAdjustmentGameTime"] = game_now.isoformat()
    state.setdefault("recentActions", []).append(
        {
            "cycle": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "result": "deferred",
            "evidence": f"{reason}: pending post-change verification of {expected_outcome}",
        }
    )
    _save_memory(state)


def _nearest_sample(cells: list[dict[str, Any]], point: dict[str, float], *, require_water: bool = False) -> dict[str, Any] | None:
    candidates = [
        cell
        for cell in cells
        if not require_water or cell.get("water", {}).get("present", False)
    ]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda cell: (
            float(cell.get("position", {}).get("x", 0)) - float(point["x"])
        ) ** 2
        + (
            float(cell.get("position", {}).get("z", 0)) - float(point["z"])
        ) ** 2,
    )


def _sample_unlocked_map(resolution: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    areas = _request("GET", "/state/map-areas")
    cells: list[dict[str, Any]] = []
    unlocked_tiles = [tile for tile in areas.get("areas", []) if tile.get("unlocked", False)]
    tile_size = 1920.0
    for tile in unlocked_tiles:
        center_x = (int(tile["x"]) - 2) * tile_size
        center_z = (int(tile["z"]) - 2) * tile_size
        analysis = _request(
            "GET",
            "/state/map-analysis",
            query={
                "minX": center_x - tile_size * 0.5,
                "minZ": center_z - tile_size * 0.5,
                "maxX": center_x + tile_size * 0.5,
                "maxZ": center_z + tile_size * 0.5,
                "resolution": resolution,
                "maxBuildSlope": 0.35,
            },
        )
        cells.extend(cell for cell in analysis.get("cells", []) if cell.get("unlocked", True))
    return cells, {"tileCount": len(unlocked_tiles), "resolutionPerTile": resolution}


def _road_preview_survives_settlement(preview: dict[str, Any], reserve: float) -> bool:
    if "length" in preview:
        length = preview["length"]
        if isinstance(length, bool) or not isinstance(length, (int, float)) or not math.isfinite(length) or length < 32.0:
            return False
    terrain = preview.get("terrainProfile")
    # This helper is only used by the live ground-road corridor planner. A
    # missing/non-required profile would otherwise bypass every terrain gate.
    if not isinstance(terrain, dict) or terrain.get("required") is not True:
        return False
    if (terrain.get("source") != "predictedControlBezierAndLiveTerrain"
            or terrain.get("predictsSettledSurface") is not False):
        return False
    terrain_required = True
    reserve_value = terrain.get("settlementTerrainReserveApplied")
    if reserve_value is not True:
        return False
    reserve_applied = True
    # Short transitions from a map-original node keep the settled minimum
    # only; the Bridge reports that explicitly instead of a clearance reserve.
    original_transition = terrain.get("originalNodeTransition") is True
    if terrain.get("settlementClearanceReserveApplied") is not (not original_transition):
        return False
    minimum_allowed = terrain.get("minimumRoadAboveTerrainAllowed")
    if terrain_required and (
        isinstance(minimum_allowed, bool)
        or not isinstance(minimum_allowed, (int, float))
        or not math.isfinite(minimum_allowed)
        or minimum_allowed < 0
    ):
        return False
    if terrain_required and terrain.get("safe") is not True:
        return False
    if terrain_required:
        numeric_fields = [
            "minimumRoadAboveTerrain",
            "maxRoadToTerrainDelta",
            "maximumRoadToTerrainDelta",
            "maxLocalTerrainGrade",
            "maximumLocalTerrainGrade",
        ]
        if reserve_applied:
            numeric_fields.extend(("previewMaximumRoadToTerrainDelta",
                                   "settlementTerrainReserve",
                                   "settlementClearanceReserve",
                                   "settledMinimumRoadAboveTerrainAllowed"))
        if any(
            isinstance(terrain.get(field), bool)
            or not isinstance(terrain.get(field), (int, float))
            or not math.isfinite(terrain[field])
            or (field != "minimumRoadAboveTerrain" and terrain[field] < 0)
            for field in numeric_fields
        ):
            return False
        if reserve_applied and abs(
            terrain["previewMaximumRoadToTerrainDelta"]
            - (terrain["maximumRoadToTerrainDelta"] - terrain["settlementTerrainReserve"])
        ) > 0.002:
            return False
        if reserve_applied and abs(
            minimum_allowed
            - (terrain["settledMinimumRoadAboveTerrainAllowed"]
               + terrain["settlementClearanceReserve"])
        ) > 0.002:
            return False
        if (terrain["settlementClearanceReserve"] <= 0) is not original_transition:
            return False
        if (not isinstance(terrain.get("originalNodeTransition"), bool)
                or terrain["maxLocalTerrainGrade"] > terrain["maximumLocalTerrainGrade"] + 0.002):
            return False
    # Native compact aggregates round to millimetres. Compare precise samples
    # against the same explicit threshold reported by the Bridge so the Python
    # route policy cannot silently impose a contradictory clearance model.
    diagnostic = _preview_terrain_diagnostic(preview)
    if not diagnostic.get("available"):
        return False
    sample_count = terrain.get("sampleCount")
    if (isinstance(sample_count, bool) or not isinstance(sample_count, int)
            or sample_count < 3 or sample_count != diagnostic["sampleCount"]):
        return False
    minimum = diagnostic["minimumClearanceSample"]["predictedClearance"]
    maximum = abs(diagnostic["maximumAbsoluteDeviationSample"]["predictedClearance"])
    if (abs(float(terrain["minimumRoadAboveTerrain"]) - minimum) > 0.002
            or abs(float(terrain["maxRoadToTerrainDelta"]) - maximum) > 0.002):
        return False
    requested_budget = float(terrain["maximumRoadToTerrainDelta"]) - reserve
    budget = (min(float(terrain["previewMaximumRoadToTerrainDelta"]), requested_budget)
              if reserve_applied else requested_budget)
    if budget < 0 or minimum < minimum_allowed or maximum > budget:
        return False
    return (
        preview.get("valid") is True
        and preview.get("nativeMoveMiddleNodeRisk") is False
        and preview.get("shortCollinearMergeRisk") is False
        and preview.get("toolErrors") == "None"
        and (
            float(terrain.get("minimumRoadAboveTerrain", -999)) >= minimum_allowed
            and float(terrain.get("maxRoadToTerrainDelta", 999))
            <= (
                min(float(terrain["previewMaximumRoadToTerrainDelta"]), requested_budget)
                if reserve_applied else requested_budget
            )
        )
    )


def _preview_terrain_diagnostic(preview: dict[str, Any]) -> dict[str, Any]:
    """Explain predicted terrain rejection without changing acceptance policy.

    The native aggregate exempts reused endpoints. Retain the controlling
    interior samples so a compact report cannot mistake an exempt endpoint
    for the reason a candidate failed. This is not settled-surface evidence.
    """
    geometry = preview.get("predictionGeometryEvidence")
    if not isinstance(geometry, dict):
        return {"available": False, "reason": "Invalid prediction geometry container"}
    samples = geometry.get("samples")
    curve = geometry.get("curve")
    reused = [preview.get("reusedStartNode"), preview.get("reusedEndNode")]
    if all(isinstance(value, bool) for value in reused):
        reused = [reused[0] or preview.get("planningStartNode") is True,
                  reused[1] or preview.get("planningEndNode") is True]
    if (geometry.get("source") != "predictedControlBezier"
            or geometry.get("clearanceModel") != "terrainSeededControlCurve"
            or geometry.get("predictsSettledSurface") is not False
            or not isinstance(curve, dict)
            or not isinstance(samples, list) or len(samples) < 3
            or any(not isinstance(value, bool) for value in reused)):
        return {"available": False,
                "reason": "Missing/unsupported clearance model, prediction samples, or endpoint reuse telemetry"}
    for side in ("a", "d"):
        endpoint = curve.get(side)
        if (not isinstance(endpoint, dict) or any(
            isinstance(endpoint.get(axis), bool)
            or not isinstance(endpoint.get(axis), (int, float))
            or not math.isfinite(endpoint[axis])
            for axis in ("x", "y", "z")
        )):
            return {"available": False, "reason": "Invalid prediction curve endpoint"}
    for index, sample in enumerate(samples):
        if not isinstance(sample, dict):
            return {"available": False, "reason": "Invalid prediction sample"}
        position = sample.get("position")
        if (not isinstance(position, dict) or any(
            isinstance(position.get(axis), bool)
            or not isinstance(position.get(axis), (int, float))
            or not math.isfinite(position[axis])
            for axis in ("x", "y", "z")
        )):
            return {"available": False, "reason": "Invalid prediction sample position"}
        value = sample.get("predictedClearance")
        terrain_y = sample.get("terrainY")
        parameter = sample.get("t")
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or isinstance(terrain_y, bool)
                or not isinstance(terrain_y, (int, float))
                or not math.isfinite(terrain_y)
                or isinstance(parameter, bool)
                or not isinstance(parameter, (int, float))
                or not math.isfinite(parameter)
                or parameter < 0 or parameter > 1
                or (index == 0 and abs(parameter) > 1e-6)
                or (index == len(samples) - 1 and abs(parameter - 1) > 1e-6)):
            return {"available": False, "reason": "Invalid predicted clearance"}
        if abs(value - (position["y"] - terrain_y)) > 0.002:
            return {"available": False, "reason": "Prediction sample clearance is inconsistent"}
    for sample, endpoint in ((samples[0], curve["a"]), (samples[-1], curve["d"])):
        if any(abs(sample["position"][axis] - endpoint[axis]) > 0.002
               for axis in ("x", "y", "z")):
            return {"available": False, "reason": "Prediction endpoints do not match samples"}
    counted = [(index, sample) for index, sample in enumerate(samples)
               if not (index == 0 and reused[0])
               and not (index == len(samples) - 1 and reused[1])]
    lowest = min(counted, key=lambda item: item[1]["predictedClearance"])
    largest = max(counted, key=lambda item: abs(item[1]["predictedClearance"]))

    def evidence(item):
        index, sample = item
        return {"sampleIndex": index, **{key: sample[key] for key in
                ("t", "position", "terrainY", "predictedClearance") if key in sample}}

    return {"available": True, "source": "predictedControlBezierAndLiveTerrain",
            "predictsSettledSurface": False, "sampleCount": len(samples),
            "countedSampleCount": len(counted),
            "excludedEndpointSamples": [evidence((index, samples[index]))
                for index, exclude in ((0, reused[0]), (len(samples) - 1, reused[1])) if exclude],
            "minimumClearanceSample": evidence(lowest),
            "maximumAbsoluteDeviationSample": evidence(largest),
            "reportedTerrainProfile": preview.get("terrainProfile", {}),
            "interpretation": "Controlling samples for the preview clearance/deviation aggregate only; native errors and other gates remain independent."}


def _compact_road_attempt(attempt: dict[str, Any]) -> dict[str, Any]:
    """Keep candidate coordinates and rejection gates, not every sampled curve."""
    result = dict(attempt)
    def brief(preview):
        result = {key: value for key, value in preview.items() if key in {
            "ok", "valid", "error", "reason", "excludedPreviousFailure", "terrainProfile",
            "nativeMoveMiddleNodeRisk", "shortCollinearMergeRisk", "toolErrors",
            "checkBuildPositionErrors", "createNodeTestErrors", "constructionCost",
            "length", "terrainFit", "reusedStartNode", "reusedEndNode",
        }}
        result["terrainDiagnostic"] = _preview_terrain_diagnostic(preview)
        return result
    if "preview" in result:
        result["preview"] = brief(result["preview"])
    if "previews" in result:
        result["previews"] = [brief(item) for item in result["previews"]]
    return result


def _summarize_terminal_attempts(steps):
    """Keep each blocked side visible even when the opposite side is viable."""
    groups = {}
    for item in steps:
        key = (item["terminalNodeId"], item["otherTerminalNodeId"])
        groups.setdefault(key, []).append(item)
    summaries = []
    for (terminal, other), items in groups.items():
        counts = {}
        for item in items:
            preview = item["preview"]
            reasons = []
            if not item["valid"]:
                if preview.get("excludedPreviousFailure"):
                    reasons.append("excludedPreviousFailure")
                if preview.get("reason"):
                    reasons.append(preview["reason"])
                if preview.get("toolErrors") not in (None, "", "None"):
                    reasons.append(preview["toolErrors"])
                if preview.get("terrainProfile", {}).get("safe") is False:
                    reasons.append("terrainProfileUnsafe")
                for gate in ("nativeMoveMiddleNodeRisk", "shortCollinearMergeRisk"):
                    if preview.get(gate):
                        reasons.append(gate)
                if not reasons:
                    reasons.append("previewOrSettlementPolicyRejected")
            for reason in reasons:
                counts[reason] = counts.get(reason, 0) + 1
        collision_free = [item for item in items
                          if item["preview"].get("toolErrors") == "None"]
        samples = sorted(collision_free, key=lambda item:
                         item["preview"].get("terrainProfile", {}).get("maxRoadToTerrainDelta", 999))[:2]
        summaries.append({"terminalNodeId": terminal, "otherTerminalNodeId": other,
                          "attemptCount": len(items),
                          "validCount": sum(bool(item["valid"]) for item in items),
                          "nativeCollisionFreeCount": len(collision_free),
                          "rejectionCounts": counts,
                          "diagnosticCandidates": [_compact_road_attempt(item) for item in samples],
                          "scope": "boundedCurrentStateSearchNotProofOfImpossibility"})
    return summaries


def _iterative_pair_remains_plannable(candidate, other, max_separation):
    """Preserve the next planner's existing terminal-pair eligibility gates.

    A shortest-distance turn can reverse the new terminal and strand the next
    wake even when its native placement preview succeeds. This is a planning
    constraint, not a prediction of settled road geometry.
    """
    dx = candidate["end"]["x"] - candidate["start"]["x"]
    dz = candidate["end"]["z"] - candidate["start"]["z"]
    length = math.hypot(dx, dz)
    separation = math.hypot(candidate["end"]["x"] - other["position"]["x"],
                            candidate["end"]["z"] - other["position"]["z"])
    if not math.isfinite(length) or length < 32 or not math.isfinite(separation):
        return False
    dot = (dx * other["direction"]["x"] + dz * other["direction"]["z"]) / length
    return dot >= 0.5 and separation <= max_separation


def _propose_outside_road_connection(arguments: dict[str, Any]) -> dict[str, Any]:
    from road_corridor import propose_live_corridor
    planner_path = ROOT / "scripts" / "road_corridor.py"
    adapter_path = Path(__file__).resolve()
    planner_source = planner_path.read_bytes()
    adapter_source = adapter_path.read_bytes()
    planner_implementation = {
        "schemaVersion": 2,
        "sourceSha256": hashlib.sha256(planner_source).hexdigest(),
        "acceptanceAdapterSha256": hashlib.sha256(adapter_source).hexdigest(),
        "combinedSha256": hashlib.sha256(planner_source + b"\0" + adapter_source).hexdigest(),
        "requiredClearanceModel": "terrainSeededControlCurve",
    }
    def read(method, path, **kwargs):
        stop = ROOT / ".runtime/automation/STOP_REQUESTED.json"
        if stop.exists():
            raise RuntimeError("Stop requested; corridor planning halted")
        return _request(method, path, **kwargs)
    identity = _current_city_identity(read)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    def progress(value):
        write_road_state(ROAD_SEARCH_PROGRESS_PATH, {
            "runId": run_id, "city": identity,
            "plannerImplementation": planner_implementation,
            "updatedAt": datetime.now(timezone.utc).isoformat(), **value})
    progress({"status": "starting", "nativePreviewCalls": 0})
    failures = _road_failures(identity)
    if any(not recovery_verified(entry) for entry in failures["failures"]):
        result = {
            "ok": True, "safe": False, "outcome": "blocked",
            "mutationAuthorized": False, "city": identity,
            "plannerImplementation": planner_implementation,
            "excludedFailureCount": len(failures["failures"]),
            "blocker": "A previous road failure has unverified recovery; recover before planning.",
            "rejectingLayer": "recoveryState",
            "terminationLayer": "recoveryPrerequisite",
            "code": "unverifiedRoadRecovery",
            "contributingLayers": {"recoveryState": 1},
            "provenRootCause": {"scope": "currentCityInstance",
                "layer": "recoveryState", "code": "unverifiedRoadRecovery"},
            "nextCheck": "Inspect the recorded checkpoint and verify rollback before any new road planning.",
        }
        progress({**{key: value for key, value in result.items()
                    if key not in ("city", "plannerImplementation")},
                  "status": "finished", "nativePreviewCalls": 0})
        return result
    def preview(candidate):
        if is_rejected(candidate, failures):
            return {"ok": True, "valid": False, "excludedPreviousFailure": True}
        try:
            return read("POST", "/commands/preview-network", body=candidate)
        except BridgeRequestError as error:
            if (any(key in candidate for key in ("planningStartY", "planningEndY"))
                    and "Future height must match a new terrain-seeded endpoint" in str(error)):
                return {"ok": True, "valid": False, "reason": "futureNodeHeightEligibility",
                        "error": str(error)}
            raise
    reserve = max(0.7, float(arguments.get("settlementTerrainReserve", 0.7)))
    hint = None
    if ROAD_CORRIDOR_HINT_PATH.exists():
        try:
            saved = json.loads(ROAD_CORRIDOR_HINT_PATH.read_text(encoding="utf-8-sig"))
            points = saved.get("traversalPoints", [])
            if (saved.get("instanceId") == identity["instanceId"]
                    and saved.get("roadPrefab") == arguments["roadPrefab"]
                    and isinstance(points, list) and 2 <= len(points) <= 9
                    and all(isinstance(p, dict) and all(isinstance(p.get(k), (int, float))
                        and not isinstance(p[k], bool) and math.isfinite(p[k]) for k in ("x", "z")) for p in points)):
                hint = points
        except (ValueError, TypeError, AttributeError):
            hint = None  # Corrupt hints cannot bypass live planning gates.
    result = propose_live_corridor(arguments, read, preview,
                                  lambda value: _road_preview_survives_settlement(value, reserve),
                                  preferred_route=hint, progress=progress)
    if result.get("safe"):
        write_road_state(ROAD_CORRIDOR_HINT_PATH, {"instanceId": identity["instanceId"],
            "roadPrefab": arguments["roadPrefab"], "traversalPoints": result["plan"]["wholeRoute"]["traversalPoints"],
            "constructionAuthorized": False})
    result["city"] = identity
    result["plannerImplementation"] = planner_implementation
    result["excludedFailureCount"] = len(failures["failures"])
    return result


def _sample_unlocked_water(resolution: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    cells, sampling = _sample_unlocked_map(resolution)
    return [
        cell for cell in cells if cell.get("water", {}).get("present", False)
    ], sampling


def _feasible_shoreline_samples(
    component: list[dict[str, Any]],
    all_cells: list[dict[str, Any]],
    resolution: int,
) -> list[dict[str, Any]]:
    """Pair moving water samples with adjacent unlocked buildable land."""
    if not component or not all_cells:
        return []
    step = 1920.0 / max(1, resolution - 1)
    maximum_distance = step * 1.55
    land_cells = [
        cell
        for cell in all_cells
        if cell.get("unlocked", True)
        and cell.get("buildable", False)
        and not cell.get("water", {}).get("present", False)
    ]
    shoreline: list[dict[str, Any]] = []
    for water_cell in component:
        water_position = water_cell["position"]
        adjacent = [
            land_cell
            for land_cell in land_cells
            if math.hypot(
                float(land_cell["position"]["x"]) - float(water_position["x"]),
                float(land_cell["position"]["z"]) - float(water_position["z"]),
            )
            <= maximum_distance
        ]
        if not adjacent:
            continue
        land_cell = min(
            adjacent,
            key=lambda cell: math.hypot(
                float(cell["position"]["x"]) - float(water_position["x"]),
                float(cell["position"]["z"]) - float(water_position["z"]),
            ),
        )
        shoreline.append({"water": water_cell, "land": land_cell})
    return shoreline


def _connected_water_component(
    cells: list[dict[str, Any]],
    intake_location: dict[str, float],
    outlet_location: dict[str, float],
    resolution: int,
) -> tuple[list[dict[str, Any]], bool]:
    if not cells:
        return [], False
    step = 1920.0 / max(1, resolution - 1)
    bucket_size = step * 1.1
    buckets: dict[tuple[int, int], list[int]] = {}
    for index, cell in enumerate(cells):
        position = cell["position"]
        key = (round(float(position["x"]) / bucket_size), round(float(position["z"]) / bucket_size))
        buckets.setdefault(key, []).append(index)

    intake_sample = _nearest_sample(cells, intake_location, require_water=True)
    outlet_sample = _nearest_sample(cells, outlet_location, require_water=True)
    if intake_sample is None or outlet_sample is None:
        return [], False
    intake_index = cells.index(intake_sample)
    outlet_index = cells.index(outlet_sample)
    queue = [intake_index]
    visited = {intake_index}
    maximum_neighbor_distance = step * 1.55
    while queue:
        current = queue.pop()
        position = cells[current]["position"]
        bx = round(float(position["x"]) / bucket_size)
        bz = round(float(position["z"]) / bucket_size)
        for offset_x in (-1, 0, 1):
            for offset_z in (-1, 0, 1):
                for neighbor in buckets.get((bx + offset_x, bz + offset_z), []):
                    if neighbor in visited:
                        continue
                    other = cells[neighbor]["position"]
                    distance = math.hypot(
                        float(other["x"]) - float(position["x"]),
                        float(other["z"]) - float(position["z"]),
                    )
                    if distance <= maximum_neighbor_distance:
                        visited.add(neighbor)
                        queue.append(neighbor)
    return [cells[index] for index in visited], outlet_index in visited


def _inspect_unlocked_water_reach(
    intake_location: dict[str, float],
    outlet_location: dict[str, float],
    arguments: dict[str, Any],
) -> dict[str, Any]:
    resolution = int(arguments.get("unlockedReachResolution", 48))
    all_cells, sampling = _sample_unlocked_map(resolution)
    water_cells = [
        cell for cell in all_cells if cell.get("water", {}).get("present", False)
    ]
    component, endpoints_connected = _connected_water_component(
        water_cells, intake_location, outlet_location, resolution
    )
    moving = []
    for cell in component:
        velocity = cell.get("water", {}).get("velocity", {})
        vx = float(velocity.get("x", 0))
        vz = float(velocity.get("z", 0))
        speed = math.hypot(vx, vz)
        if speed >= float(arguments.get("minFlowSpeed", 0.1)):
            moving.append((cell, vx, vz, speed))

    risks: list[str] = []
    if not endpoints_connected:
        risks.append("intakeAndOutletNotOnSameUnlockedWaterReach")
    if not moving:
        risks.append("noMovingWaterInUnlockedReach")
        return {
            "safe": False,
            "risks": risks,
            "sampling": sampling,
            "waterSampleCount": len(water_cells),
            "componentSampleCount": len(component),
        }

    mean_x = sum(vx / speed for _, vx, _, speed in moving)
    mean_z = sum(vz / speed for _, _, vz, speed in moving)
    mean_length = math.hypot(mean_x, mean_z)
    if mean_length <= 1e-6:
        risks.append("ambiguousUnlockedReachFlow")
        return {"safe": False, "risks": risks, "sampling": sampling}
    flow_x = mean_x / mean_length
    flow_z = mean_z / mean_length
    minimum_agreement = float(arguments.get("minUnlockedFlowAgreement", 0.5))
    aligned = [
        cell
        for cell, vx, vz, speed in moving
        if (vx * flow_x + vz * flow_z) / speed >= minimum_agreement
    ]
    shoreline = _feasible_shoreline_samples(aligned, all_cells, resolution)
    projections = [
        float(item["water"]["position"]["x"]) * flow_x
        + float(item["water"]["position"]["z"]) * flow_z
        for item in shoreline
    ]
    if not projections:
        risks.append("noFeasibleUnlockedShoreline")
        return {
            "safe": False,
            "risks": risks,
            "sampling": sampling,
            "waterSampleCount": len(water_cells),
            "componentSampleCount": len(component),
            "alignedMovingSampleCount": len(aligned),
            "feasibleShorelineSampleCount": 0,
            "endpointsConnected": endpoints_connected,
            "flowUnit": {"x": round(flow_x, 5), "z": round(flow_z, 5)},
        }
    intake_projection = float(intake_location["x"]) * flow_x + float(intake_location["z"]) * flow_z
    outlet_projection = float(outlet_location["x"]) * flow_x + float(outlet_location["z"]) * flow_z
    upstream_projection = min(projections)
    downstream_projection = max(projections)
    extreme_basis = {
        "source": "genericFeasibleShoreline",
        "upstreamProjection": round(upstream_projection, 3),
        "downstreamProjection": round(downstream_projection, 3),
    }
    constructible_reference = arguments.get("_constructibleReachReference") or {}
    if {
        "upstreamProjection",
        "downstreamProjection",
    }.issubset(constructible_reference):
        upstream_projection = float(constructible_reference["upstreamProjection"])
        downstream_projection = float(constructible_reference["downstreamProjection"])
        extreme_basis = {
            "source": "nativeValidFacilityCandidates",
            "upstreamProjection": round(upstream_projection, 3),
            "downstreamProjection": round(downstream_projection, 3),
        }
    elif arguments.get("_intakePrefab") and arguments.get("_outletPrefab"):
        intake_references = _preview_shore_facility_candidates(
            str(arguments["_intakePrefab"]),
            shoreline,
            flow_x,
            flow_z,
            upstream=True,
            limit=8,
            ignore_building_id=int(arguments.get("_intakeBuildingId", 0)),
        )
        outlet_references = _preview_shore_facility_candidates(
            str(arguments["_outletPrefab"]),
            shoreline,
            flow_x,
            flow_z,
            upstream=False,
            limit=8,
            ignore_building_id=int(arguments.get("_outletBuildingId", 0)),
        )
        if not intake_references:
            risks.append("noNativeValidUpstreamExtremeReference")
        if not outlet_references:
            risks.append("noNativeValidDownstreamExtremeReference")
        if intake_references and outlet_references:
            upstream_location = intake_references[0]["waterLocation"]
            downstream_location = outlet_references[0]["waterLocation"]
            upstream_projection = (
                float(upstream_location["x"]) * flow_x
                + float(upstream_location["z"]) * flow_z
            )
            downstream_projection = (
                float(downstream_location["x"]) * flow_x
                + float(downstream_location["z"]) * flow_z
            )
            extreme_basis = {
                "source": "nativeValidFacilityCandidates",
                "upstreamProjection": round(upstream_projection, 3),
                "downstreamProjection": round(downstream_projection, 3),
                "intakeCandidateCount": len(intake_references),
                "outletCandidateCount": len(outlet_references),
            }
    intake_upstream_gap = max(0.0, intake_projection - upstream_projection)
    outlet_downstream_gap = max(0.0, downstream_projection - outlet_projection)
    max_upstream_gap = float(arguments.get("maxUpstreamGap", 128))
    max_downstream_gap = float(arguments.get("maxDownstreamGap", 128))
    if intake_upstream_gap > max_upstream_gap:
        risks.append("intakeNotNearUnlockedUpstreamExtreme")
    if outlet_downstream_gap > max_downstream_gap:
        risks.append("outletNotNearUnlockedDownstreamExtreme")
    return {
        "safe": len(risks) == 0,
        "risks": risks,
        "sampling": sampling,
        "waterSampleCount": len(water_cells),
        "componentSampleCount": len(component),
        "alignedMovingSampleCount": len(aligned),
        "feasibleShorelineSampleCount": len(shoreline),
        "endpointsConnected": endpoints_connected,
        "flowUnit": {"x": round(flow_x, 5), "z": round(flow_z, 5)},
        "intakeUpstreamGap": round(intake_upstream_gap, 3),
        "outletDownstreamGap": round(outlet_downstream_gap, 3),
        "extremeBasis": extreme_basis,
        "thresholds": {
            "maxUpstreamGap": max_upstream_gap,
            "maxDownstreamGap": max_downstream_gap,
            "minFlowSpeed": float(arguments.get("minFlowSpeed", 0.1)),
            "minFlowAgreement": minimum_agreement,
        },
    }


def _with_placed_partner(arguments: dict[str, Any]) -> dict[str, Any]:
    """The pair with the other role's facility identified when it already stands at its
    candidate spot. The pair is placed one facility at a time, so when the second one is
    placed the first exists, and re-checking its spot without its id previews a building
    on top of itself and reports the pair unsafe."""
    partner_role = "outlet" if str(arguments["role"]) == "intake" else "intake"
    partner = arguments[partner_role]
    if int(partner.get("buildingId", 0)) > 0:
        return arguments
    facilities = _request("GET", "/state/facilities", query={"service": "Water", "limit": 1000})
    placed = next((item for item in facilities.get("facilities", [])
                   if str(item.get("prefab", "")) == str(partner["buildingPrefab"])
                   and math.dist((float(item["position"]["x"]), float(item["position"]["z"])),
                                 (float(partner["position"]["x"]), float(partner["position"]["z"]))) <= 2.0),
                  None)
    if placed is None:
        return arguments
    return {**arguments, partner_role: {**partner, "buildingId": int(placed["id"])}}


GROUNDWATER_CLEARANCE = mayor_land_use.WELL_CLEARANCE


def _uses_groundwater(preview: dict[str, Any]) -> bool:
    return bool(((preview.get("aiProfile") or {}).get("fields") or {}).get("useGroundWater"))


def _groundwater_polluters() -> list[tuple[float, float]]:
    """Where ground pollution comes from: industrial buildings, land zoned for industry and
    facilities that pollute the ground (a landfill)."""
    growables = next((value for value in _request("GET", "/state/growables", query={"limit": 50000}).values()
                      if isinstance(value, list)), [])
    blocks = _request("GET", "/state/zone-blocks", query={"limit": 5000}).get("blocks", [])
    points = mayor_land_use.industry([item for item in growables if item.get("position")],
                                     mayor_land_use.zoned_land(blocks))
    points += [(item["position"]["x"], item["position"]["z"]) for item in _land_use_context()["polluters"]
               if float(item.get("pollutionRadius") or 0) > 0]
    return points


def _inspect_groundwater_pair(arguments: dict[str, Any], previews: dict[str, dict[str, Any]],
                              existing_by_id: dict[int, dict[str, Any]]) -> dict[str, Any]:
    """A groundwater pump (a water tower) and a sewage outlet. The pump draws from the ground, so
    no river lies between them to keep the outlet downstream of: the pump is checked for clean
    ground water away from what pollutes the ground, and the outlet for a valid, productive
    shore. A city that already draws surface water has its outlets judged against those intakes
    by the river pair instead."""
    risks: list[str] = []
    pump, outlet = previews["intake"], previews["outlet"]
    pump_id = int(arguments["intake"].get("buildingId", 0))
    outlet_id = int(arguments["outlet"].get("buildingId", 0))
    pump_standing = pump_id in existing_by_id and "Created" in str(existing_by_id[pump_id].get("flags", ""))
    outlet_standing = outlet_id in existing_by_id and "Created" in str(existing_by_id[outlet_id].get("flags", ""))
    environment = pump.get("environmental") or {}
    if not pump_standing:
        if not pump.get("valid", False):
            risks.append("groundwaterPumpPlacementInvalid")
        if float(environment.get("groundWaterIntakePollution") or 0) > 0:
            risks.append("groundwaterPolluted")
        if float(environment.get("currentGroundPollution") or 0) > 0:
            risks.append("groundwaterSitePolluted")
        spot = (float(arguments["intake"]["position"]["x"]), float(arguments["intake"]["position"]["z"]))
        if any(math.dist(spot, point) < GROUNDWATER_CLEARANCE for point in _groundwater_polluters()):
            risks.append("groundwaterNearPolluter")
    outlet_water = outlet.get("waterPlacement") or {}
    if not outlet_standing:
        if not outlet.get("valid", False):
            risks.append("outletPlacementInvalid")
        if not outlet_water.get("closestWater", {}).get("found", False):
            risks.append("outletWaterNotFound")
        if int(outlet.get("gamePlacement", {}).get("productionRate", 0)) <= 0:
            risks.append("outletProductionZero")
    prefabs = {item["name"]: item for item in _request("GET", "/prefabs/buildings", query={"service": "Water"})
               .get("buildings", [])}
    surface = [item for item in _request("GET", "/state/facilities", query={"service": "Water", "limit": 1000})
               .get("facilities", [])
               if (((prefabs.get(item.get("prefab")) or {}).get("aiProfile") or {}).get("fields") or {}).get("waterIntake")
               and not (((prefabs.get(item.get("prefab")) or {}).get("aiProfile") or {}).get("fields") or {})
               .get("useGroundWater")]
    if surface:
        risks.append("surfaceIntakeNeedsRiverPair")
    return {"ok": True, "safe": not risks, "risks": sorted(set(risks)), "groundwater": True,
            "previews": previews,
            "evidence": {"groundWaterIntakePollution": environment.get("groundWaterIntakePollution"),
                         "currentGroundPollution": environment.get("currentGroundPollution"),
                         "polluterClearance": GROUNDWATER_CLEARANCE,
                         "surfaceIntakes": [int(item["id"]) for item in surface]}}


def _propose_groundwater_pump_sites(arguments: dict[str, Any]) -> dict[str, Any]:
    """Free land near a town for a groundwater pump, nearest first: native-valid on its own lot,
    clean ground water, no home inside its noise radius and GROUNDWATER_CLEARANCE from what
    pollutes the ground. For a town no river pair can serve (the unlocked shore too short to
    keep an outlet downstream of an intake)."""
    anchor = (float(arguments["serviceAreaAnchor"]["x"]), float(arguments["serviceAreaAnchor"]["z"]))
    radius = float(arguments.get("searchRadius", 640))
    nearest = float(arguments.get("minDistance", 0))
    spacing = float(arguments.get("spacing", 48))
    attempts_left = int(arguments.get("maxPreviewAttempts", 96))
    wanted = int(arguments.get("maxResults", 3))
    prefab = arguments.get("buildingPrefab")
    if not prefab:
        pumps = [item for item in _request("GET", "/prefabs/buildings", query={"service": "Water"})
                 .get("buildings", []) if item.get("unlocked")
                 and ((item.get("aiProfile") or {}).get("fields") or {}).get("useGroundWater")]
        if not pumps:
            return {"ok": True, "safe": False, "risks": ["noGroundwaterPumpUnlocked"], "candidates": []}
        prefab = min(pumps, key=lambda item: float(item.get("constructionCost") or 0))["name"]
    polluters = _groundwater_polluters()
    rings = int(radius // spacing)
    points = sorted({(anchor[0] + dx * spacing, anchor[1] + dz * spacing)
                     for dx in range(-rings, rings + 1) for dz in range(-rings, rings + 1)
                     if nearest <= math.hypot(dx, dz) * spacing <= radius},
                    key=lambda point: math.dist(point, anchor))
    candidates, rejected = [], {}
    for point in points:
        if len(candidates) >= wanted or attempts_left <= 0:
            break
        if any(math.dist(point, polluter) < GROUNDWATER_CLEARANCE for polluter in polluters):
            rejected["nearPolluter"] = rejected.get("nearPolluter", 0) + 1
            continue
        attempts_left -= 1
        preview = _request("POST", "/commands/preview-placement", body={
            "buildingPrefab": prefab, "position": {"x": round(point[0], 3), "z": round(point[1], 3)},
            "angleDegrees": 0, "requireRoadAccess": False})
        environment = preview.get("environmental") or {}
        reason = ("invalid" if not preview.get("valid") else
                  "overlap" if preview.get("buildingOverlaps") or preview.get("roadOverlaps") else
                  "polluted" if float(environment.get("groundWaterIntakePollution") or 0) > 0
                  or float(environment.get("currentGroundPollution") or 0) > 0 else
                  "noisyHomes" if int(environment.get("residentialBuildingsInsideNoiseRadius") or 0) > 0 else None)
        if reason:
            rejected[reason] = rejected.get(reason, 0) + 1
            continue
        candidates.append({"buildingPrefab": prefab, "angleDegrees": 0,
                           "position": {"x": preview["position"]["x"], "z": preview["position"]["z"]},
                           "waterIntake": (preview.get("waterPlacement") or {}).get("waterIntake"),
                           "pipePorts": _preview_water_pipe_connections(preview),
                           "distance": round(math.dist(point, anchor), 1)})
    return {"ok": True, "safe": bool(candidates), "risks": [] if candidates else ["noGroundwaterPumpSite"],
            "buildingPrefab": prefab, "candidates": candidates, "rejected": rejected,
            "searchRadius": radius, "previewAttempts": int(arguments.get("maxPreviewAttempts", 96)) - attempts_left}


def _inspect_water_pair(arguments: dict[str, Any]) -> dict[str, Any]:
    existing_by_id: dict[int, dict[str, Any]] = {}
    existing_ids = {
        int(arguments[role].get("buildingId", 0))
        for role in ("intake", "outlet")
        if int(arguments[role].get("buildingId", 0)) > 0
    }
    if existing_ids:
        facilities = _request("GET", "/state/facilities", query={"service": "Water", "limit": 1000})
        existing_by_id = {
            int(item["id"]): item
            for item in facilities.get("facilities", [])
            if int(item.get("id", 0)) in existing_ids
        }
    previews: dict[str, dict[str, Any]] = {}
    for role in ("intake", "outlet"):
        candidate = arguments[role]
        body = {
            "buildingPrefab": candidate["buildingPrefab"],
            "position": candidate["position"],
            "angleDegrees": candidate.get("angleDegrees", 0),
            "ignoreBuildingId": candidate.get("buildingId", 0),
            "requireRoadAccess": False,
            "maxRoadDistance": 512,
        }
        previews[role] = _request("POST", "/commands/preview-placement", body=body)

    if _uses_groundwater(previews["intake"]):
        return _inspect_groundwater_pair(arguments, previews, existing_by_id)
    intake_water = previews["intake"].get("waterPlacement") or {}
    outlet_water = previews["outlet"].get("waterPlacement") or {}
    intake_location = intake_water.get("location")
    outlet_location = outlet_water.get("location")
    if not intake_location or not outlet_location:
        return {
            "ok": True,
            "safe": False,
            "risks": ["missingWaterPlacementTelemetry"],
            "previews": previews,
        }

    padding = float(arguments.get("samplePadding", 64))
    min_x = min(float(intake_location["x"]), float(outlet_location["x"])) - padding
    max_x = max(float(intake_location["x"]), float(outlet_location["x"])) + padding
    min_z = min(float(intake_location["z"]), float(outlet_location["z"])) - padding
    max_z = max(float(intake_location["z"]), float(outlet_location["z"])) + padding
    analysis = _request(
        "GET",
        "/state/map-analysis",
        query={
            "minX": min_x,
            "minZ": min_z,
            "maxX": max_x,
            "maxZ": max_z,
            "resolution": arguments.get("resolution", 24),
            "maxBuildSlope": 0.35,
        },
    )
    water_cells = analysis.get("cells", [])
    intake_sample = _nearest_sample(water_cells, intake_location, require_water=True)
    outlet_sample = _nearest_sample(water_cells, outlet_location, require_water=True)

    coverage = _request(
        "GET",
        "/state/utility-coverage",
        query={
            "minX": min_x,
            "minZ": min_z,
            "maxX": max_x,
            "maxZ": max_z,
            "resolution": arguments.get("resolution", 24),
        },
    )
    coverage_samples = coverage.get("samples", [])
    intake_pollution_sample = _nearest_sample(coverage_samples, intake_location)
    outlet_pollution_sample = _nearest_sample(coverage_samples, outlet_location)

    dx = float(outlet_location["x"]) - float(intake_location["x"])
    dz = float(outlet_location["z"]) - float(intake_location["z"])
    separation = math.hypot(dx, dz)
    intake_velocity = (intake_sample or {}).get("water", {}).get("velocity", {})
    outlet_velocity = (outlet_sample or {}).get("water", {}).get("velocity", {})
    ivx = float(intake_velocity.get("x", 0))
    ivz = float(intake_velocity.get("z", 0))
    ovx = float(outlet_velocity.get("x", 0))
    ovz = float(outlet_velocity.get("z", 0))
    intake_speed = math.hypot(ivx, ivz)
    outlet_speed = math.hypot(ovx, ovz)
    average_vx = (ivx + ovx) * 0.5
    average_vz = (ivz + ovz) * 0.5
    average_speed = math.hypot(average_vx, average_vz)
    downstream_projection = 0.0
    cross_flow_distance = separation
    if average_speed > 1e-6:
        flow_x = average_vx / average_speed
        flow_z = average_vz / average_speed
        downstream_projection = dx * flow_x + dz * flow_z
        cross_flow_distance = abs(dx * flow_z - dz * flow_x)
    flow_agreement = 0.0
    if intake_speed > 1e-6 and outlet_speed > 1e-6:
        flow_agreement = (ivx * ovx + ivz * ovz) / (intake_speed * outlet_speed)

    min_separation = float(arguments.get("minSeparation", 256))
    min_downstream = float(arguments.get("minDownstreamProjection", 128))
    intake_pollution = int((intake_pollution_sample or {}).get("waterPollution", 0))
    outlet_pollution = int((outlet_pollution_sample or {}).get("waterPollution", 0))
    risks: list[str] = []
    intake_existing = existing_by_id.get(int(arguments["intake"].get("buildingId", 0)))
    outlet_existing = existing_by_id.get(int(arguments["outlet"].get("buildingId", 0)))
    def matches_existing(role: str, existing: dict[str, Any] | None) -> bool:
        if not existing:
            return False
        candidate = arguments[role]
        actual_position = existing.get("position") or {}
        intended_position = candidate.get("position") or {}
        position_matches = math.hypot(
            float(actual_position.get("x", 0)) - float(intended_position.get("x", 0)),
            float(actual_position.get("z", 0)) - float(intended_position.get("z", 0)),
        ) <= 1.0
        angle_delta = abs(
            (
                float(existing.get("angleDegrees", 0))
                - float(candidate.get("angleDegrees", 0))
                + 180
            )
            % 360
            - 180
        )
        return bool(
            str(existing.get("prefab", "")) == str(candidate.get("buildingPrefab", ""))
            and position_matches
            and angle_delta <= 1.0
            and "Created" in str(existing.get("flags", ""))
            and int(existing.get("productionRate", 0)) > 0
        )

    intake_existing_verified = matches_existing("intake", intake_existing)
    outlet_existing_verified = matches_existing("outlet", outlet_existing)
    if not previews["intake"].get("valid", False) and not intake_existing_verified:
        risks.append("intakePlacementInvalid")
    if not previews["outlet"].get("valid", False) and not outlet_existing_verified:
        risks.append("outletPlacementInvalid")
    if not intake_water.get("closestWater", {}).get("found", False) and not intake_existing_verified:
        risks.append("intakeWaterNotFound")
    if not outlet_water.get("closestWater", {}).get("found", False) and not outlet_existing_verified:
        risks.append("outletWaterNotFound")
    if int(previews["intake"].get("gamePlacement", {}).get("productionRate", 0)) <= 0 and not intake_existing_verified:
        risks.append("intakeProductionZero")
    if int(previews["outlet"].get("gamePlacement", {}).get("productionRate", 0)) <= 0 and not outlet_existing_verified:
        risks.append("outletProductionZero")
    if separation < min_separation:
        risks.append("insufficientSeparation")
    minimum_flow_speed = float(arguments.get("minFlowSpeed", 0.1))
    if intake_speed < minimum_flow_speed:
        risks.append("intakeDeadOrWeakWater")
    if outlet_speed < minimum_flow_speed:
        risks.append("outletDeadOrWeakWater")
    if average_speed < minimum_flow_speed:
        risks.append("weakOrAmbiguousFlow")
    if downstream_projection < min_downstream:
        risks.append("outletNotFarEnoughDownstream")
    if intake_pollution > 0:
        risks.append("intakeAlreadyPolluted")
    unlocked_reach = None
    warnings: list[str] = []
    if arguments.get("checkUnlockedReach", True):
        reach_arguments = {
            **arguments,
            "_intakePrefab": arguments["intake"]["buildingPrefab"],
            "_outletPrefab": arguments["outlet"]["buildingPrefab"],
            "_intakeBuildingId": arguments["intake"].get("buildingId", 0),
            "_outletBuildingId": arguments["outlet"].get("buildingId", 0),
        }
        unlocked_reach = _inspect_unlocked_water_reach(
            intake_location, outlet_location, reach_arguments
        )
        risks.extend(unlocked_reach.get("risks", []))
    if flow_agreement < 0.25:
        if (
            unlocked_reach
            and unlocked_reach.get("safe", False)
            and downstream_projection >= min_downstream
        ):
            warnings.append("localFlowDirectionsDifferOnCurvedReach")
        else:
            risks.append("inconsistentFlowDirection")

    return {
        "ok": True,
        "safe": len(risks) == 0,
        "risks": risks,
        "warnings": warnings,
        "thresholds": {
            "minSeparation": min_separation,
            "minDownstreamProjection": min_downstream,
            "minFlowSpeed": minimum_flow_speed,
        },
        "geometry": {
            "intakeWaterLocation": intake_location,
            "outletWaterLocation": outlet_location,
            "separation": round(separation, 3),
            "downstreamProjection": round(downstream_projection, 3),
            "crossFlowDistance": round(cross_flow_distance, 3),
        },
        "flow": {
            "intake": {"x": ivx, "z": ivz, "speed": round(intake_speed, 3)},
            "outlet": {"x": ovx, "z": ovz, "speed": round(outlet_speed, 3)},
            "agreement": round(flow_agreement, 3),
        },
        "pollution": {
            "intake": intake_pollution,
            "outlet": outlet_pollution,
        },
        "unlockedReach": unlocked_reach,
        "previews": previews,
        "existingFacilities": {
            "intake": {
                "verified": intake_existing_verified,
                "facility": intake_existing,
            },
            "outlet": {
                "verified": outlet_existing_verified,
                "facility": outlet_existing,
            },
        },
    }


PORT_IN_WATER = 8.0  # metres from a water facility's port to the nearest water (the game allows 20)


def _preview_shore_facility_candidates(
    prefab: str,
    shoreline: list[dict[str, Any]],
    flow_x: float,
    flow_z: float,
    *,
    upstream: bool,
    limit: int,
    ignore_building_id: int = 0,
    anchor: tuple[float, float] | None = None,
    reach_offset: float = 0.0,
) -> list[dict[str, Any]]:
    minimum_water_side_alignment = 0.7

    def projection(item: dict[str, Any]) -> float:
        return float(item["water"]["position"]["x"]) * flow_x + float(item["water"]["position"]["z"]) * flow_z

    if anchor is None:
        ranked = sorted(shoreline, key=projection, reverse=not upstream)
    else:
        # near a town: the shore nearest it on its own side of the town along the flow (intakes
        # upstream, outlets downstream by at least reach_offset), not the far ends of the river
        middle = anchor[0] * flow_x + anchor[1] * flow_z
        side = [item for item in shoreline
                if (projection(item) <= middle - reach_offset if upstream else projection(item) >= middle + reach_offset)]
        ranked = sorted(side, key=lambda item: math.dist(
            anchor, (float(item["land"]["position"]["x"]), float(item["land"]["position"]["z"]))))
    results: list[dict[str, Any]] = []
    seen: set[tuple[float, float, int]] = set()
    for shore in ranked[: max(limit * 4, 8)]:
        water = shore["water"]["position"]
        land = shore["land"]["position"]
        for fraction in (0.0, 0.25, 0.5, 0.75):
            position = {
                "x": float(land["x"]) + (float(water["x"]) - float(land["x"])) * fraction,
                "z": float(land["z"]) + (float(water["z"]) - float(land["z"])) * fraction,
            }
            shore_x = float(water["x"]) - float(land["x"])
            shore_z = float(water["z"]) - float(land["z"])
            shore_length = math.hypot(shore_x, shore_z)
            if shore_length <= 1e-6:
                continue
            shore_x /= shore_length
            shore_z /= shore_length
            aligned_previews: list[dict[str, Any]] = []
            for angle in range(0, 360, 45):
                key = (round(position["x"], 3), round(position["z"], 3), angle)
                if key in seen:
                    continue
                seen.add(key)
                preview = _request(
                    "POST",
                    "/commands/preview-placement",
                    body={
                        "buildingPrefab": prefab,
                        "position": position,
                        "angleDegrees": angle,
                        "ignoreBuildingId": ignore_building_id,
                        "requireRoadAccess": False,
                        "maxRoadDistance": 512,
                    },
                )
                water_placement = preview.get("waterPlacement") or {}
                water_location = water_placement.get("location") or {}
                water_side_x = float(water_location.get("x", position["x"])) - position["x"]
                water_side_z = float(water_location.get("z", position["z"])) - position["z"]
                water_side_length = math.hypot(water_side_x, water_side_z)
                alignment = (
                    (water_side_x * shore_x + water_side_z * shore_z)
                    / water_side_length
                    if water_side_length > 1e-6
                    else -1.0
                )
                closest = water_placement.get("closestWater", {})
                # the port must reach water well within the game's 20m: one at the edge of that
                # lost it as the river settled (live: an outlet 38m from its building
                # found none a few months later and the town's sewage backed up)
                port_gap = math.hypot(float(closest.get("x", 0)) - float(water_location.get("x", 0)),
                                      float(closest.get("z", 0)) - float(water_location.get("z", 0)))
                if (
                    preview.get("valid", False)
                    and int(preview.get("gamePlacement", {}).get("productionRate", 0)) > 0
                    and closest.get("found", False)
                    and port_gap <= PORT_IN_WATER
                    and int(preview.get("safety", {}).get("riskCount", 0)) == 0
                    and alignment >= minimum_water_side_alignment
                ):
                    aligned_previews.append(
                        {
                            "buildingPrefab": prefab,
                            "position": {
                                "x": round(position["x"], 3),
                                "z": round(position["z"], 3),
                            },
                            "angleDegrees": angle,
                            "waterLocation": water_location,
                            "orientation": {
                                "shoreToWater": {
                                    "x": round(shore_x, 6),
                                    "z": round(shore_z, 6),
                                },
                                "facilityToWaterPort": {
                                    "x": round(water_side_x, 3),
                                    "z": round(water_side_z, 3),
                                },
                                "waterSideAlignment": round(alignment, 6),
                                "minimumAlignment": minimum_water_side_alignment,
                                "facesWater": True,
                            },
                            "terrain": preview.get("terrain"),
                            "productionRate": int(
                                preview.get("gamePlacement", {}).get("productionRate", 0)
                            ),
                        }
                    )
            if aligned_previews:
                aligned_previews.sort(
                    key=lambda item: (
                        -float(item["orientation"]["waterSideAlignment"]),
                        -int(item["productionRate"]),
                    )
                )
                results.append(aligned_previews[0])
            if len(results) >= limit:
                return results
    return results


def _inspect_wind_resource_map(arguments: dict[str, Any]) -> dict[str, Any]:
    center = arguments["center"]
    radius = float(arguments.get("radius", 640))
    resolution = int(arguments.get("resolution", 32))
    payload = _request(
        "GET",
        "/state/wind-map",
        query={
            "minX": float(center["x"]) - radius,
            "minZ": float(center["z"]) - radius,
            "maxX": float(center["x"]) + radius,
            "maxZ": float(center["z"]) + radius,
            "resolution": resolution,
            "unlockedOnly": arguments.get("unlockedOnly", True),
        },
    )
    ranked = sorted(
        payload.get("cells", []),
        key=lambda item: float(item.get("windSpeed", 0)),
        reverse=True,
    )
    limit = int(arguments.get("maxResults", 12))
    return {
        "ok": True,
        "source": payload.get("source"),
        "bounds": payload.get("bounds"),
        "resolution": payload.get("resolution"),
        "sampleCount": payload.get("sampleCount", 0),
        "summary": payload.get("summary"),
        "highestWindSamples": ranked[:limit],
    }


def _read_electricity_connection_area(
    arguments: dict[str, Any],
) -> dict[str, Any]:
    return _request(
        "GET",
        "/state/electricity-connection-area",
        query={
            "buildingId": arguments["buildingId"],
            "padding": arguments.get("padding", 512),
            "resolution": arguments.get("resolution", 48),
        },
    )


def _inspect_electricity_connection_area(
    arguments: dict[str, Any],
) -> dict[str, Any]:
    area = _read_electricity_connection_area(arguments)
    building_position = (area.get("building") or {}).get("position") or {
        "x": 0,
        "z": 0,
    }
    powered = sorted(
        (cell for cell in area.get("cells", []) if cell.get("powered", False)),
        key=lambda cell: _point_distance(cell["position"], building_position),
    )
    limit = int(arguments.get("maxResults", 16))
    return {
        "ok": True,
        "building": area.get("building"),
        "semantics": area.get("semantics"),
        "bounds": area.get("bounds"),
        "resolution": area.get("resolution"),
        "summary": area.get("summary"),
        "targetConnectionSamples": area.get("targetConnectionSamples", []),
        "nearestPoweredSamples": powered[:limit],
    }


NOISE_ROAD_REACH = 48.0  # road traffic noise is felt by the buildings fronting the road
NOISE_INDUSTRY_REACH = 96.0  # and an industrial building's by the homes around it


def _diagnose_noise(arguments: dict[str, Any]) -> dict[str, Any]:
    """Homes the game marks noisy, each with the live noise where it stands and what makes it: a
    facility whose noise radius reaches it, industry beside it, or the road it fronts. Each source
    has its own remedy, so the sources are named rather than summed."""
    limit = int(arguments.get("limit", 16))
    rows = [row for row in _request("GET", "/state/problems", query={"limit": 50000}).get("problems", [])
            if "Noise" in (row.get("problemNames") or [])]
    facilities = [item for item in _request("GET", "/state/facility-influence", query={"limit": 500})
                  .get("facilities", []) if item.get("noiseInfluence")]
    growables = next((value for value in _request("GET", "/state/growables", query={"limit": 50000}).values()
                      if isinstance(value, list)), [])
    industry = [(g["position"]["x"], g["position"]["z"]) for g in growables
                if str(g.get("service")) in ("Industrial", "Office") and g.get("position")]
    roads = _request("GET", "/state/networks", query={"limit": 20000, "service": "Road"}).get("segments", [])
    homes = []
    tally: dict[str, int] = {}
    for row in rows[:limit]:
        position = row["position"]
        here = (position["x"], position["z"])
        sample = _request("GET", "/state/service-coverage", query={
            "service": "NoisePollution", "minX": here[0] - 8, "minZ": here[1] - 8,
            "maxX": here[0] + 8, "maxZ": here[1] + 8, "resolution": 2})
        sources = []
        for item in facilities:
            noise = item["noiseInfluence"]
            distance = _point_distance(item["position"], position)
            if distance <= float(noise.get("radius") or 0):
                sources.append({"kind": "facility", "buildingId": item["buildingId"], "prefab": item.get("prefab"),
                                "distance": round(distance, 1), "radius": noise.get("radius")})
        near_industry = sum(1 for point in industry if math.dist(point, here) <= NOISE_INDUSTRY_REACH)
        if near_industry:
            sources.append({"kind": "industry", "buildings": near_industry, "within": NOISE_INDUSTRY_REACH})
        # every home fronts a road: only an arterial or highway carries traffic loud enough to name
        fronting = sorted({str(s.get("prefab")) for s in roads if mayor_land_use.is_arterial(s.get("prefab"))
                           and _distance_to_segment(position, s["start"], s["end"]) <= NOISE_ROAD_REACH})
        if fronting:
            sources.append({"kind": "traffic", "roads": fronting})
        for source in sources:
            tally[source["kind"]] = tally.get(source["kind"], 0) + 1
        homes.append({"id": row["id"], "position": position,
                      "noise": (sample.get("summary") or {}).get("average"), "sources": sources})
    return {
        "ok": True,
        "noisy": len(rows),
        "checked": len(homes),
        "homes": homes,
        "bySource": tally,
        "remedies": {
            "facility": "the facility stands too near homes: move it beyond its noise radius, or zone no homes inside it",
            "industry": "homes beside industry: a commercial or park buffer between them, or the homes rezoned",
            "traffic": "homes on a busy road: traffic routed onto roads without homes, or the frontage rezoned commercial",
        },
    }


TERRAIN_NOTE_ANOMALIES = ("roadTerrainCliff", "roadBelowLocalGrade")
SHORT_GRID_UTILIZATION = 0.5  # a dark city is short of power only while its grid delivers this share
# WaterManager.CheckWaterImpl serves a building from the closest pipe within 95m
WATER_PIPE_REACH = 95.0


def _distance_to_segment(point: dict[str, Any], start: dict[str, Any], end: dict[str, Any]) -> float:
    ax, az, bx, bz = float(start["x"]), float(start["z"]), float(end["x"]), float(end["z"])
    px, pz = float(point["x"]), float(point["z"])
    length = (bx - ax) ** 2 + (bz - az) ** 2
    t = 0.0 if length == 0 else max(0.0, min(1.0, ((px - ax) * (bx - ax) + (pz - az) * (bz - az)) / length))
    return math.hypot(px - (ax + t * (bx - ax)), pz - (az + t * (bz - az)))


def _diagnose_utility_shortfall(arguments: dict[str, Any]) -> dict[str, Any]:
    """Why buildings lack power or water: each one checked is either within the network's reach
    but given nothing (the supply is short: more generation, capacity, or the budget back up) or
    beyond its reach (a line or pipe is missing). Both are checked every time."""
    service = str(arguments["service"])
    limit = int(arguments.get("limit", 12))
    utilities = _request("GET", "/state/city-health").get("utilities") or {}
    covered: list[dict[str, Any]] = []
    uncovered: list[dict[str, Any]] = []
    if service == "electricity":
        coverage = _request("GET", "/state/electricity-service-coverage", query={"limit": 50000})
        dark = coverage.get("buildings", [])
        summary = coverage.get("summary") or {}
        # most of the city dark is a grid with too little to give, not lines cut everywhere: a
        # deep shortage darkens whole neighbourhoods, leaving no powered ground to see beside them
        delivering = ((utilities.get("electricity") or {}).get("utilization") or 0) >= SHORT_GRID_UTILIZATION
        # ...but only while the grid hands out most of what it makes: one that delivers little to a
        # dark town reaches too few of its buildings (live: turbines 200-280m from every home at
        # 8% and 3% use read as short, and two more were built while one 75m line would do)
        city_dark = delivering and \
            (summary.get("missingElectricity") or 0) > (summary.get("requiringElectricity") or 0) / 2
        for building in dark[:limit]:
            item = {"id": building["id"], "position": building.get("position")}
            if city_dark:
                covered.append(item)
                continue
            # powered ground within the building's reach: the grid gets there and gives nothing
            area = _inspect_electricity_connection_area(
                {"buildingId": int(building["id"]), "padding": 64, "resolution": 16, "maxResults": 1})
            (covered if (area.get("summary") or {}).get("poweredSamples") else uncovered).append(item)
        supply = {"electricity": utilities.get("electricity")}
    else:
        coverage = _request("GET", "/state/water-service-coverage", query={"limit": 50000})
        dark = [b for b in coverage.get("buildings", []) if not b.get("hasWater") or not b.get("hasSewage")]
        pipes = _request("GET", "/state/networks", query={"service": "Water", "limit": 5000}).get("segments", [])
        for building in dark[:limit]:
            nearest = min(pipes, key=lambda s: _distance_to_segment(building["position"], s["start"], s["end"]),
                          default=None)
            distance = None if nearest is None else _distance_to_segment(
                building["position"], nearest["start"], nearest["end"])
            item = {"id": building["id"], "position": building.get("position"),
                    "pipeDistance": None if distance is None else round(distance, 1)}
            if distance is not None and distance <= WATER_PIPE_REACH:
                flags = f"{nearest.get('startNodeFlags', '')} {nearest.get('endNodeFlags', '')}"
                # the pipe's joints carry Water and Sewage only while the system delivers them
                item["pipeCarries"] = {"water": "Water" in flags, "sewage": "Sewage" in flags}
                covered.append(item)
            else:
                uncovered.append(item)
        supply = {"water": utilities.get("water"), "sewage": utilities.get("sewage")}
    verdict = (["supplyShort"] if covered else []) + (["notCovered"] if uncovered else [])
    return {
        "ok": True,
        "service": service,
        "summary": coverage.get("summary"),
        "missing": len(dark),
        "checked": len(covered) + len(uncovered),
        "supply": supply,
        "supplyNote": "the game counts only what it delivers as used: a short supply can read well below capacity",
        "coveredButShort": covered,
        "notCovered": uncovered,
        "verdict": verdict,
        "remedies": {
            "supplyShort": "add generation or capacity (or raise a trimmed budget): lines and pipes do not help",
            "notCovered": "extend the network with lines or pipes to the buildings listed",
        },
    }


def _pylon_safe_connection_samples(area: dict[str, Any]) -> list[dict[str, Any]]:
    samples = area.get("targetConnectionSamples", [])
    has_native_conductivity = any("conductive" in sample for sample in samples)
    if has_native_conductivity:
        samples = [sample for sample in samples if sample.get("conductive", False)]
        if not samples:
            return []
    has_native_footprint_flags = any(
        "outsideBuildingFootprint" in sample for sample in samples
    )
    if has_native_footprint_flags:
        filtered = [
            sample
            for sample in samples
            if bool(sample.get("outsideBuildingFootprint", False))
        ]
        return filtered
    else:
        filtered = [
            sample
            for sample in samples
            if "radialFraction" not in sample
            or float(sample.get("radialFraction", 0)) >= 0.75
        ]
    return filtered or samples


def _read_live_power_line_obstacles() -> list[dict[str, Any]]:
    facilities = _request(
        "GET", "/state/facilities", query={"limit": 50000, "includeMapObjects": True}
    )
    growables = _request("GET", "/state/growables", query={"limit": 50000})
    for label, payload, key in (
        ("facilities", facilities, "facilities"),
        ("growables", growables, "growables"),
    ):
        returned = int(payload.get("returned", len(payload.get(key, []))))
        total = int(payload.get("total", returned))
        if returned < total:
            raise RuntimeError(
                f"Power-line building obstacle inventory is truncated for {label} "
                f"({returned}/{total}); refusing to plan or build with incomplete clearance data."
            )
    snapshot = {
        "facilities": facilities,
        "growables": growables,
    }
    return build_layered_buildings(
        snapshot,
        include_original=True,
        include_road_structures=False,
        include_network_helpers=False,
    )


def _power_line_pair_clear_of_buildings(
    start: dict[str, Any],
    end: dict[str, Any],
    buildings: list[dict[str, Any]],
    clearance: float = POWER_LINE_BUILDING_CLEARANCE_METERS,
) -> bool:
    return not any(
        segment_intersects_building_footprint(start, end, building, clearance)
        for building in buildings
    )


def _propose_power_line_connection(arguments: dict[str, Any]) -> dict[str, Any]:
    area = _read_electricity_connection_area(arguments)
    building = area.get("building") or {}
    if building.get("powered", False):
        return {
            "ok": True,
            "safe": True,
            "actionRequired": False,
            "risks": [],
            "reason": "targetBuildingAlreadyPowered",
            "building": building,
        }

    cells = area.get("cells", [])
    powered_sources = [
        cell for cell in cells if cell.get("powered", False)
    ]
    native_target_samples = area.get("targetConnectionSamples", [])
    target_points = [
        cell
        for cell in _pylon_safe_connection_samples(area)
        if not cell.get("powered", False)
    ]
    if not target_points and not native_target_samples:
        target_points = [
            cell
            for cell in cells
            if cell.get("insideTargetGridRadius", False)
            and not cell.get("powered", False)
        ]
    if not powered_sources:
        return {
            "ok": True,
            "safe": False,
            "actionRequired": True,
            "risks": ["noPoweredElectricityAreaWithinSearchBounds"],
            "building": building,
            "connectionArea": {
                "bounds": area.get("bounds"),
                "summary": area.get("summary"),
            },
        }
    if not target_points:
        return {
            "ok": True,
            "safe": False,
            "actionRequired": True,
            "risks": ["targetBuildingHasNoSampledConnectionArea"],
            "building": building,
            "connectionArea": {
                "bounds": area.get("bounds"),
                "summary": area.get("summary"),
            },
        }

    maximum_distance = float(arguments.get("maximumLineLength", 1000))
    pair_limit = int(arguments.get("maxPreviewAttempts", 96))
    pairs: list[tuple[float, dict[str, Any], dict[str, Any]]] = []
    for source in powered_sources:
        source_position = source["position"]
        for target in target_points:
            target_position = target["position"]
            distance = _point_distance(source_position, target_position)
            if 8 <= distance <= maximum_distance:
                pairs.append((distance, source, target))
    pairs.sort(key=lambda item: item[0])

    attempts = 0
    rejected: dict[str, int] = {}
    obstacles = arguments.get("_buildingObstacles")
    if obstacles is None:
        obstacles = _read_live_power_line_obstacles()
    statically_eligible_pairs = []
    for pair in pairs:
        _, source, target = pair
        if _power_line_pair_clear_of_buildings(
            source["position"], target["position"], obstacles
        ):
            statically_eligible_pairs.append(pair)
        else:
            rejected["buildingFootprintClearance"] = (
                rejected.get("buildingFootprintClearance", 0) + 1
            )
    for distance, source, target in _diverse_preview_pairs(
        statically_eligible_pairs, pair_limit
    ):
        attempts += 1
        body = {
            "roadPrefab": arguments.get("powerLinePrefab", "Power Line"),
            "start": {
                "x": round(float(source["position"]["x"]), 3),
                "z": round(float(source["position"]["z"]), 3),
            },
            "end": {
                "x": round(float(target["position"]["x"]), 3),
                "z": round(float(target["position"]["z"]), 3),
            },
            "heightOffset": 0,
        }
        preview = _request("POST", "/commands/preview-network", body=body)
        if preview.get("valid", False):
            return {
                "ok": True,
                "safe": True,
                "actionRequired": True,
                "risks": [],
                "building": building,
                "connectionArea": {
                    "bounds": area.get("bounds"),
                    "summary": area.get("summary"),
                },
                "candidate": {
                    "powerLinePrefab": body["roadPrefab"],
                    "start": body["start"],
                    "end": body["end"],
                    "length": round(distance, 3),
                    "source": {
                        "powered": source.get("powered", False),
                        "conductive": source.get("conductive", False),
                    },
                    "target": {
                        "insideBuildingGridRadius": target.get(
                            "insideTargetGridRadius", False
                        ),
                        "distanceToBuilding": target.get("distanceToBuilding"),
                    },
                    "buildingFootprintClearance": {
                        "minimum": POWER_LINE_BUILDING_CLEARANCE_METERS,
                        "checkedBuildingCount": len(obstacles),
                        "safe": True,
                    },
                    "preview": preview,
                },
                "previewAttempts": attempts,
                "candidatePairCount": len(pairs),
                "staticallyEligiblePairCount": len(statically_eligible_pairs),
                "rejectedCounts": rejected,
            }
        errors = str(preview.get("toolErrors", "nativePreviewRejected"))
        rejected[errors] = rejected.get(errors, 0) + 1

    return {
        "ok": True,
        "safe": False,
        "actionRequired": True,
        "risks": ["noNativeValidPowerLineConnectionFound"],
        "building": building,
        "connectionArea": {
            "bounds": area.get("bounds"),
            "summary": area.get("summary"),
        },
        "previewAttempts": attempts,
        "candidatePairCount": len(pairs),
        "staticallyEligiblePairCount": len(statically_eligible_pairs),
        "rejectedCounts": rejected,
    }


def _electricity_region_endpoint_pairs(
    first_area: dict[str, Any],
    second_area: dict[str, Any],
    maximum_distance: float,
) -> list[tuple[float, dict[str, Any], dict[str, Any]]]:
    first_samples = _pylon_safe_connection_samples(first_area)
    second_samples = _pylon_safe_connection_samples(second_area)
    pairs: list[tuple[float, dict[str, Any], dict[str, Any]]] = []
    for first in first_samples:
        for second in second_samples:
            distance = _point_distance(first["position"], second["position"])
            if 8 <= distance <= maximum_distance:
                pairs.append((distance, first, second))
    pairs.sort(
        key=lambda item: (
            item[0],
            float(item[1].get("angleDegrees", 0)),
            float(item[2].get("angleDegrees", 0)),
        )
    )
    return pairs


def _diverse_preview_pairs(
    pairs: list[tuple[float, dict[str, Any], dict[str, Any]]],
    limit: int,
) -> list[tuple[float, dict[str, Any], dict[str, Any]]]:
    """Keep short candidates while sampling the whole ordered boundary space."""
    if limit <= 0:
        return []
    if len(pairs) <= limit:
        return pairs

    local_count = max(1, limit // 4)
    indexes = list(range(local_count))
    remaining = limit - len(indexes)
    if remaining == 1:
        indexes.append(len(pairs) - 1)
    elif remaining > 1:
        span = len(pairs) - 1 - local_count
        indexes.extend(
            local_count + round(slot * span / (remaining - 1))
            for slot in range(remaining)
        )
    return [pairs[index] for index in indexes]


def _propose_minimal_power_grid(arguments: dict[str, Any]) -> dict[str, Any]:
    source_id = int(arguments["sourceBuildingId"])
    building_ids = [source_id]
    for building_id in arguments["buildingIds"]:
        value = int(building_id)
        if value not in building_ids:
            building_ids.append(value)

    area_arguments = {
        "padding": arguments.get("padding", 512),
        "resolution": arguments.get("resolution", 48),
    }
    areas: dict[int, dict[str, Any]] = {}
    for building_id in building_ids:
        areas[building_id] = _read_electricity_connection_area(
            {"buildingId": building_id, **area_arguments}
        )

    targets = [building_id for building_id in building_ids if building_id != source_id]
    already_powered = [
        building_id
        for building_id in targets
        if bool((areas[building_id].get("building") or {}).get("powered", False))
    ]
    if len(already_powered) == len(targets):
        return {
            "ok": True,
            "safe": True,
            "actionRequired": False,
            "reason": "allTargetBuildingsAlreadyPowered",
            "sourceBuildingId": source_id,
            "buildingIds": building_ids,
            "alreadyPoweredBuildingIds": already_powered,
            "segments": [],
            "totalLineLength": 0,
        }

    parent = {building_id: building_id for building_id in building_ids}

    def find(building_id: int) -> int:
        while parent[building_id] != building_id:
            parent[building_id] = parent[parent[building_id]]
            building_id = parent[building_id]
        return building_id

    def union(first_id: int, second_id: int) -> None:
        first_root = find(first_id)
        second_root = find(second_id)
        if first_root != second_root:
            parent[second_root] = first_root

    # A target that is already powered is already connected to the live
    # generating component through existing lines or conductive building
    # coverage. Seed Kruskal with those connections so it never proposes a
    # duplicate line back to the source.
    existing_grid_connections: list[dict[str, Any]] = []
    for building_id in already_powered:
        union(source_id, building_id)
        existing_grid_connections.append(
            {
                "fromBuildingId": source_id,
                "toBuildingId": building_id,
                "reason": "targetAlreadyPoweredByLiveGrid",
            }
        )

    maximum_distance = float(arguments.get("maximumLineLength", 1920))
    obstacles = arguments.get("_buildingObstacles")
    if obstacles is None:
        obstacles = _read_live_power_line_obstacles()
    region_edges: list[dict[str, Any]] = []
    star_estimate = 0.0
    for first_index, first_id in enumerate(building_ids):
        first_building = areas[first_id].get("building") or {}
        first_position = first_building.get("position") or {"x": 0, "z": 0}
        first_radius = float(first_building.get("electricityGridRadius", 8) or 8)
        for second_id in building_ids[first_index + 1 :]:
            second_building = areas[second_id].get("building") or {}
            second_position = second_building.get("position") or {"x": 0, "z": 0}
            second_radius = float(
                second_building.get("electricityGridRadius", 8) or 8
            )
            center_distance = _point_distance(first_position, second_position)
            region_gap = max(0.0, center_distance - first_radius - second_radius)
            endpoint_pairs = _electricity_region_endpoint_pairs(
                areas[first_id],
                areas[second_id],
                maximum_distance,
            )
            clear_endpoint_pairs = []
            blocked_pair_count = 0
            for pair in endpoint_pairs:
                if _power_line_pair_clear_of_buildings(
                    pair[1]["position"], pair[2]["position"], obstacles
                ):
                    clear_endpoint_pairs.append(pair)
                else:
                    blocked_pair_count += 1
            endpoint_pairs = clear_endpoint_pairs
            sampled_gap = endpoint_pairs[0][0] if endpoint_pairs else region_gap
            region_edges.append(
                {
                    "firstBuildingId": first_id,
                    "secondBuildingId": second_id,
                    "centerDistance": center_distance,
                    "regionGap": region_gap,
                    "sampledGap": sampled_gap,
                    "overlaps": region_gap <= 0,
                    "endpointPairs": endpoint_pairs,
                    "buildingFootprintRejectedPairs": blocked_pair_count,
                }
            )
            direct_target_id = (
                second_id
                if first_id == source_id
                else first_id if second_id == source_id else None
            )
            if direct_target_id is not None and direct_target_id not in already_powered:
                star_estimate += region_gap

    region_edges.sort(
        key=lambda edge: (
            float(edge["regionGap"]),
            float(edge["sampledGap"]),
            int(edge["firstBuildingId"]),
            int(edge["secondBuildingId"]),
        )
    )
    prefab = arguments.get("powerLinePrefab", "Power Line")
    per_edge_limit = int(arguments.get("maxPreviewAttemptsPerEdge", 96))
    total_attempt_limit = int(arguments.get("maxTotalPreviewAttempts", 512))
    preview_attempts = 0
    zero_length_connections: list[dict[str, Any]] = []
    segments: list[dict[str, Any]] = []
    rejected_edges: list[dict[str, Any]] = []

    for edge in region_edges:
        first_id = int(edge["firstBuildingId"])
        second_id = int(edge["secondBuildingId"])
        if find(first_id) == find(second_id):
            continue
        if edge["overlaps"]:
            union(first_id, second_id)
            zero_length_connections.append(
                {
                    "fromBuildingId": first_id,
                    "toBuildingId": second_id,
                    "reason": "nativeElectricityGridRadiiOverlap",
                    "centerDistance": round(float(edge["centerDistance"]), 3),
                    "regionGap": 0,
                }
            )
            continue

        accepted = None
        attempts_for_edge = 0
        rejected: dict[str, int] = {}
        if int(edge.get("buildingFootprintRejectedPairs", 0)):
            rejected["buildingFootprintClearance"] = int(
                edge["buildingFootprintRejectedPairs"]
            )
        preview_pairs = _diverse_preview_pairs(
            edge["endpointPairs"],
            min(per_edge_limit, total_attempt_limit - preview_attempts),
        )
        for distance, first_sample, second_sample in preview_pairs:
            if (
                attempts_for_edge >= per_edge_limit
                or preview_attempts >= total_attempt_limit
            ):
                break
            attempts_for_edge += 1
            preview_attempts += 1
            body = {
                "roadPrefab": prefab,
                "start": {
                    "x": round(float(first_sample["position"]["x"]), 3),
                    "z": round(float(first_sample["position"]["z"]), 3),
                },
                "end": {
                    "x": round(float(second_sample["position"]["x"]), 3),
                    "z": round(float(second_sample["position"]["z"]), 3),
                },
                "heightOffset": 0,
            }
            preview = _request("POST", "/commands/preview-network", body=body)
            if preview.get("valid", False):
                accepted = {
                    "fromBuildingId": first_id,
                    "toBuildingId": second_id,
                    "powerLinePrefab": prefab,
                    "start": body["start"],
                    "end": body["end"],
                    "length": round(distance, 3),
                    "preview": preview,
                    "previewAttempts": attempts_for_edge,
                    "buildingFootprintClearance": {
                        "minimum": POWER_LINE_BUILDING_CLEARANCE_METERS,
                        "checkedBuildingCount": len(obstacles),
                        "safe": True,
                    },
                }
                break
            reason = str(preview.get("toolErrors", "nativePreviewRejected"))
            rejected[reason] = rejected.get(reason, 0) + 1

        if accepted is not None:
            union(first_id, second_id)
            segments.append(accepted)
        else:
            rejected_edges.append(
                {
                    "fromBuildingId": first_id,
                    "toBuildingId": second_id,
                    "regionGap": round(float(edge["regionGap"]), 3),
                    "candidatePairCount": len(edge["endpointPairs"]),
                    "previewAttempts": attempts_for_edge,
                    "rejectedCounts": rejected,
                }
            )

    source_root = find(source_id)
    disconnected = [
        building_id
        for building_id in building_ids
        if find(building_id) != source_root
    ]
    total_length = sum(float(segment["length"]) for segment in segments)

    # Kruskal chooses the undirected tree. Reorder its buildable segments from
    # the generation source outward so each accepted mutation extends the live
    # supplied component whenever the preceding segment has settled.
    ordered_segments: list[dict[str, Any]] = []
    reached = {source_id, *already_powered}
    pending = list(segments)

    def expand_reached_through_overlaps() -> None:
        changed = True
        while changed:
            changed = False
            for connection in zero_length_connections:
                first_id = int(connection["fromBuildingId"])
                second_id = int(connection["toBuildingId"])
                if first_id in reached and second_id not in reached:
                    reached.add(second_id)
                    changed = True
                elif second_id in reached and first_id not in reached:
                    reached.add(first_id)
                    changed = True

    expand_reached_through_overlaps()
    while pending:
        progressed = False
        for pending_segment in list(pending):
            first_id = int(pending_segment["fromBuildingId"])
            second_id = int(pending_segment["toBuildingId"])
            if first_id in reached or second_id in reached:
                segment = pending_segment
                if second_id in reached and first_id not in reached:
                    segment = {
                        **pending_segment,
                        "fromBuildingId": second_id,
                        "toBuildingId": first_id,
                        "start": pending_segment["end"],
                        "end": pending_segment["start"],
                    }
                segment["buildOrder"] = len(ordered_segments) + 1
                ordered_segments.append(segment)
                reached.add(int(segment["fromBuildingId"]))
                reached.add(int(segment["toBuildingId"]))
                pending.remove(pending_segment)
                expand_reached_through_overlaps()
                progressed = True
        if not progressed:
            break

    safe = not disconnected and not pending
    return {
        "ok": True,
        "safe": safe,
        "planningOnly": True,
        "actionRequired": bool(ordered_segments),
        "algorithm": "native-previewed-region-kruskal",
        "sourceBuildingId": source_id,
        "buildingIds": building_ids,
        "alreadyPoweredBuildingIds": already_powered,
        "existingGridConnections": existing_grid_connections,
        "zeroLengthConnections": zero_length_connections,
        "segments": ordered_segments,
        "totalLineLength": round(total_length, 3),
        "directStarRegionGapEstimate": round(star_estimate, 3),
        "estimatedSavingsAgainstDirectStar": round(
            max(0.0, star_estimate - total_length),
            3,
        ),
        "previewAttempts": preview_attempts,
        "buildingFootprintClearance": {
            "minimum": POWER_LINE_BUILDING_CLEARANCE_METERS,
            "checkedBuildingCount": len(obstacles),
            "safe": safe,
        },
        "disconnectedBuildingIds": disconnected,
        "rejectedEdges": rejected_edges,
        "risks": [] if safe else ["couldNotConnectEveryElectricityRegion"],
        "verification": [
            "createCheckpointBeforeEachSegment",
            "buildInReturnedOrder",
            "settleSimulationAfterEachSegment",
            "inspectElectricityServiceCoverage",
            "inspectEveryTargetConnectionArea",
            "verifyRegression",
            "saveOnlyAfterAllTargetsArePowered",
        ],
    }


def _inspect_power_grid_efficiency(arguments: dict[str, Any]) -> dict[str, Any]:
    networks = _request(
        "GET",
        "/state/networks",
        query={"service": "Electricity", "limit": 5000},
    )
    coverage = _request(
        "GET",
        "/state/electricity-service-coverage",
        query={
            "limit": 50000,
            "includeCovered": True,
            "includeNoDemand": False,
        },
    )
    segments: list[dict[str, Any]] = []
    total_length = 0.0
    for segment in networks.get("segments", []):
        start = segment.get("start") or {}
        end = segment.get("end") or {}
        length = _point_distance(start, end)
        total_length += length
        segments.append(
            {
                "id": int(segment.get("id", 0)),
                "name": segment.get("name", ""),
                "prefab": segment.get("prefab", ""),
                "startNodeId": segment.get("startNodeId"),
                "endNodeId": segment.get("endNodeId"),
                "start": start,
                "end": end,
                "length": round(length, 3),
            }
        )
    segments.sort(key=lambda item: float(item["length"]), reverse=True)
    limit = int(arguments.get("maxCandidates", 12))
    candidates = []
    for segment in segments[:limit]:
        impact = _request(
            "POST",
            "/commands/preview-bulldoze-impact",
            body={"entityType": "netSegment", "id": segment["id"]},
        )
        candidates.append(
            {
                **segment,
                "graphImpact": impact,
                "classification": "checkpointExperimentRequired",
                "reason": (
                    "Native network-cut topology does not model electricity "
                    "bridging through building conductivity areas."
                ),
                "verification": [
                    "createCheckpoint",
                    "captureRegressionBaseline",
                    "removeOnlyThisSegment",
                    "settleSimulation",
                    "inspectElectricityServiceCoverage",
                    "inspectTargetConnectionAreas",
                    "inspectProblems",
                    "verifyRegression",
                    "restoreCheckpointOnAnyFailure",
                ],
            }
        )
    return {
        "ok": True,
        "planningOnly": True,
        "safeToRemoveWithoutExperiment": False,
        "baseline": {
            "electricityServiceCoverage": coverage.get("summary"),
            "segmentCount": len(segments),
            "totalPowerLineLength": round(total_length, 3),
        },
        "candidates": candidates,
    }


def _propose_water_pair_candidates(arguments: dict[str, Any]) -> dict[str, Any]:
    resolution = int(arguments.get("resolution", 48))
    all_cells, sampling = _sample_unlocked_map(resolution)
    # map analysis reports the terrain under the water as "level" and the water surface as "depth";
    # a facility on a sheet of water a few centimetres deep loses it when the river settles (live:
    # an outlet sited on 0.25m of water at the start found none a month later)
    minimum_depth = float(arguments.get("minWaterDepth", 2.0))
    water_cells = [
        cell for cell in all_cells if cell.get("water", {}).get("present", False)
        and ("depth" not in cell["water"] or "level" not in cell["water"]
             or float(cell["water"]["depth"]) - float(cell["water"]["level"]) >= minimum_depth)
    ]
    moving = []
    minimum_speed = float(arguments.get("minFlowSpeed", 0.1))
    for cell in water_cells:
        velocity = cell.get("water", {}).get("velocity", {})
        vx = float(velocity.get("x", 0))
        vz = float(velocity.get("z", 0))
        speed = math.hypot(vx, vz)
        if speed >= minimum_speed:
            moving.append((cell, vx, vz, speed))
    if not moving:
        return {
            "ok": True,
            "safe": False,
            "risks": ["noMovingWaterInUnlockedArea"],
            "sampling": sampling,
        }
    mean_x = sum(vx / speed for _, vx, _, speed in moving)
    mean_z = sum(vz / speed for _, _, vz, speed in moving)
    mean_length = math.hypot(mean_x, mean_z)
    if mean_length <= 1e-6:
        return {
            "ok": True,
            "safe": False,
            "risks": ["ambiguousUnlockedWaterFlow"],
            "sampling": sampling,
        }
    flow_x = mean_x / mean_length
    flow_z = mean_z / mean_length
    agreement = float(arguments.get("minFlowAgreement", 0.35))
    aligned = [
        cell
        for cell, vx, vz, speed in moving
        if (vx * flow_x + vz * flow_z) / speed >= agreement
    ]
    shoreline = _feasible_shoreline_samples(aligned, all_cells, resolution)
    if not shoreline:
        return {
            "ok": True,
            "safe": False,
            "risks": ["noFeasibleUnlockedShoreline"],
            "sampling": sampling,
        }
    candidate_limit = int(arguments.get("maxCandidatesPerRole", 4))
    anchor_point = arguments.get("serviceAreaAnchor")
    anchor = (float(anchor_point["x"]), float(anchor_point["z"])) if anchor_point else None
    # half the required separation on each side of the town keeps the pair apart
    offset = float(arguments.get("minSeparation", 256)) / 2 if anchor else 0.0
    intake_candidates = _preview_shore_facility_candidates(
        arguments.get("intakePrefab", "Water Intake"),
        shoreline,
        flow_x,
        flow_z,
        upstream=True,
        limit=candidate_limit,
        anchor=anchor,
        reach_offset=offset,
    )
    outlet_candidates = _preview_shore_facility_candidates(
        arguments.get("outletPrefab", "Water Outlet"),
        shoreline,
        flow_x,
        flow_z,
        upstream=False,
        limit=candidate_limit,
        anchor=anchor,
        reach_offset=offset,
    )
    if not intake_candidates or not outlet_candidates:
        return {
            "ok": True,
            "safe": False,
            "risks": ["noNativeValidWaterFacilityPairCandidates"],
            "sampling": sampling,
            "candidateCounts": {
                "intake": len(intake_candidates),
                "outlet": len(outlet_candidates),
            },
            # each role's valid sites stand on their own: a second outlet beside a working system
            # needs no new intake
            "intakeCandidates": intake_candidates,
            "outletCandidates": outlet_candidates,
        }
    pair_arguments = {
        key: value
        for key, value in arguments.items()
        if key
        in {
            "minSeparation",
            "minDownstreamProjection",
            "samplePadding",
            "minFlowSpeed",
            "maxUpstreamGap",
            "maxDownstreamGap",
        }
    }
    pair_arguments["unlockedReachResolution"] = resolution
    if anchor:  # a pair near the town need not sit at the ends of the unlocked reach
        pair_arguments.setdefault("maxUpstreamGap", 1e6)
        pair_arguments.setdefault("maxDownstreamGap", 1e6)
    pair_arguments["_constructibleReachReference"] = {
        "upstreamProjection": (
            float(intake_candidates[0]["waterLocation"]["x"]) * flow_x
            + float(intake_candidates[0]["waterLocation"]["z"]) * flow_z
        ),
        "downstreamProjection": (
            float(outlet_candidates[0]["waterLocation"]["x"]) * flow_x
            + float(outlet_candidates[0]["waterLocation"]["z"]) * flow_z
        ),
    }
    attempts: list[dict[str, Any]] = []
    rejection_counts: dict[str, int] = {}
    for intake in intake_candidates:
        for outlet in outlet_candidates:
            pair = _inspect_water_pair(
                {
                    **pair_arguments,
                    "intake": {
                        "buildingPrefab": intake["buildingPrefab"],
                        "position": intake["position"],
                        "angleDegrees": intake["angleDegrees"],
                    },
                    "outlet": {
                        "buildingPrefab": outlet["buildingPrefab"],
                        "position": outlet["position"],
                        "angleDegrees": outlet["angleDegrees"],
                    },
                }
            )
            attempt = {
                "intake": intake,
                "outlet": outlet,
                "safe": pair.get("safe", False),
                "risks": pair.get("risks", []),
                "warnings": pair.get("warnings", []),
                "evidence": {
                    "geometry": pair.get("geometry"),
                    "flow": pair.get("flow"),
                    "pollution": pair.get("pollution"),
                    "unlockedReach": {
                        key: value
                        for key, value in (pair.get("unlockedReach") or {}).items()
                        if key
                        in {
                            "safe",
                            "risks",
                            "endpointsConnected",
                            "intakeUpstreamGap",
                            "outletDownstreamGap",
                            "extremeBasis",
                            "thresholds",
                        }
                    },
                },
            }
            attempts.append(attempt)
            for risk in attempt["risks"]:
                rejection_counts[str(risk)] = rejection_counts.get(str(risk), 0) + 1
            if pair.get("safe", False):
                return {
                    "ok": True,
                    "safe": True,
                    "risks": [],
                    "sampling": sampling,
                    "flowUnit": {"x": round(flow_x, 5), "z": round(flow_z, 5)},
                    "feasibleShorelineSampleCount": len(shoreline),
                    "intake": attempt["intake"],
                    "outlet": attempt["outlet"],
                    "verification": pair,
                    "attemptCount": len(attempts),
                    "candidateCounts": {
                        "intake": len(intake_candidates),
                        "outlet": len(outlet_candidates),
                    },
                    "intakeCandidates": intake_candidates,
                    "outletCandidates": outlet_candidates,
                    "rejectionCountsBeforeSuccess": rejection_counts,
                }
    ranked_failures = sorted(
        attempts,
        key=lambda item: (
            len(item["risks"]),
            float(
                (item.get("evidence", {}).get("unlockedReach") or {}).get(
                    "intakeUpstreamGap", float("inf")
                )
            ),
            float(
                (item.get("evidence", {}).get("unlockedReach") or {}).get(
                    "outletDownstreamGap", float("inf")
                )
            ),
        ),
    )
    return {
        "ok": True,
        "safe": False,
        "risks": ["noSafeWaterPairFound"],
        "sampling": sampling,
        "flowUnit": {"x": round(flow_x, 5), "z": round(flow_z, 5)},
        "feasibleShorelineSampleCount": len(shoreline),
        "candidateCounts": {
            "intake": len(intake_candidates),
            "outlet": len(outlet_candidates),
        },
        "intakeCandidates": intake_candidates,
        "outletCandidates": outlet_candidates,
        "totalAttemptCount": len(attempts),
        "returnedAttemptCount": min(len(attempts), 8),
        "searchExhausted": True,
        "rejectionCounts": rejection_counts,
        "bestFailedAttempt": ranked_failures[0] if ranked_failures else None,
        "attempts": attempts[:8],
    }


def _point_distance(first: dict[str, Any], second: dict[str, Any]) -> float:
    return math.hypot(
        float(first.get("x", 0)) - float(second.get("x", 0)),
        float(first.get("z", 0)) - float(second.get("z", 0)),
    )


def _point_key(point: dict[str, Any], tolerance: float) -> tuple[int, int]:
    return (
        round(float(point.get("x", 0)) / tolerance),
        round(float(point.get("z", 0)) / tolerance),
    )


def _water_plan_id(arguments: dict[str, Any]) -> str:
    canonical = {
        "intake": arguments["intake"],
        "outlet": arguments["outlet"],
        "pipePrefab": arguments["pipePrefab"],
        "segments": arguments["segments"],
        "serviceAreaAnchor": arguments["serviceAreaAnchor"],
    }
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "water-" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _split_straight_pipe(
    start: dict[str, Any], end: dict[str, Any], maximum_length: float
) -> list[tuple[dict[str, float], dict[str, float]]]:
    """Split a straight pipe run without collinear joints.

    Extending a dead-end pipe exactly in line invokes CS1's native
    MoveMiddleNode, which rewrites the previous piece. Interior split points
    therefore alternate a small lateral offset (5% of a piece) so every joint
    bends; the run's own endpoints never move.
    """
    sx, sz = float(start["x"]), float(start["z"])
    ex, ez = float(end["x"]), float(end["z"])
    length = math.hypot(ex - sx, ez - sz)
    # Leave room for the slightly longer offset pieces.
    piece_count = max(1, math.ceil(length / (maximum_length * 0.98)))
    if piece_count == 1:
        return [({"x": round(sx, 3), "z": round(sz, 3)}, {"x": round(ex, 3), "z": round(ez, 3)})]
    normal_x, normal_z = -(ez - sz) / length, (ex - sx) / length
    offset = 0.05 * length / piece_count
    points = []
    for index in range(piece_count + 1):
        t = index / piece_count
        side = 0.0 if index in (0, piece_count) else (offset if index % 2 else -offset)
        points.append({"x": round(sx + (ex - sx) * t + normal_x * side, 3),
                       "z": round(sz + (ez - sz) * t + normal_z * side, 3)})
    return list(zip(points[:-1], points[1:]))


def _normalize_water_pipe_segments(arguments: dict[str, Any]) -> dict[str, Any]:
    maximum_length = float(arguments.get("maxWaterPipeSegmentLength", 160))
    normalized: list[dict[str, Any]] = []
    for original_index, original in enumerate(arguments["segments"]):
        segment = dict(original)
        start = dict(segment["start"])
        end = dict(segment["end"])
        role = str(segment.get("role", ""))
        if role in {"intakeBranch", "outletBranch"}:
            facility = arguments["intake" if role == "intakeBranch" else "outlet"]["position"]
            if _point_distance(end, facility) < _point_distance(start, facility):
                start, end = end, start
        pieces = _split_straight_pipe(start, end, maximum_length)
        piece_count = len(pieces)
        for piece_index, (piece_start, piece_end) in enumerate(pieces):
            piece_role = role
            if role in {"intakeBranch", "outletBranch"} and piece_index > 0:
                piece_role = "sharedTrunk"
            base_name = str(segment.get("name", "")).strip()
            normalized.append(
                {
                    "role": piece_role,
                    "start": piece_start,
                    "end": piece_end,
                    "name": (
                        base_name
                        if piece_count == 1 or not base_name
                        else f"{base_name} {piece_index + 1}/{piece_count}"
                    ),
                    "logicalSegmentIndex": original_index,
                    "pieceIndex": piece_index,
                    "pieceCount": piece_count,
                }
            )
    result = dict(arguments)
    result["segments"] = normalized
    result["maxWaterPipeSegmentLength"] = maximum_length
    return result


def _inspect_water_pipe_topology(arguments: dict[str, Any]) -> dict[str, Any]:
    segments = arguments["segments"]
    tolerance = float(arguments.get("nodeTolerance", 2))
    maximum_facility_distance = float(arguments.get("maxFacilityPipeDistance", 64))
    anchor_tolerance = float(arguments.get("serviceAnchorTolerance", 16))
    intake_position = arguments["intake"]["position"]
    outlet_position = arguments["outlet"]["position"]
    service_anchor = arguments["serviceAreaAnchor"]
    risks: list[str] = []

    if len(segments) < 3:
        risks.append("waterPlanNeedsAtLeastThreeSegments")
    role_indexes: dict[str, list[int]] = {}
    graph: dict[tuple[int, int], set[tuple[int, int]]] = {}
    segment_keys: set[tuple[tuple[int, int], tuple[int, int]]] = set()
    for index, segment in enumerate(segments):
        role = str(segment.get("role", ""))
        role_indexes.setdefault(role, []).append(index)
        start_key = _point_key(segment["start"], tolerance)
        end_key = _point_key(segment["end"], tolerance)
        if start_key == end_key:
            risks.append("waterPlanContainsZeroLengthSegment")
            continue
        canonical_segment = tuple(sorted((start_key, end_key)))
        if canonical_segment in segment_keys:
            risks.append("waterPlanContainsDuplicateSegment")
        segment_keys.add(canonical_segment)
        graph.setdefault(start_key, set()).add(end_key)
        graph.setdefault(end_key, set()).add(start_key)

        directly_spans_facilities = (
            _point_distance(segment["start"], intake_position) <= maximum_facility_distance
            and _point_distance(segment["end"], outlet_position) <= maximum_facility_distance
        ) or (
            _point_distance(segment["end"], intake_position) <= maximum_facility_distance
            and _point_distance(segment["start"], outlet_position) <= maximum_facility_distance
        )
        if directly_spans_facilities:
            risks.append("directFacilityToFacilityPipePlanned")

    if len(role_indexes.get("intakeBranch", [])) != 1:
        risks.append("waterPlanNeedsExactlyOneIntakeBranch")
    if len(role_indexes.get("outletBranch", [])) != 1:
        risks.append("waterPlanNeedsExactlyOneOutletBranch")
    if not role_indexes.get("sharedTrunk"):
        risks.append("waterPlanNeedsSharedTrunk")

    branch_junctions: dict[str, tuple[int, int]] = {}
    exact_connections = arguments.get("_facilityPipeConnections", {})
    for role, facility_position, connection_role in (
        ("intakeBranch", intake_position, "intake"),
        ("outletBranch", outlet_position, "outlet"),
    ):
        indexes = role_indexes.get(role, [])
        if len(indexes) != 1:
            continue
        segment = segments[indexes[0]]
        connection_points = exact_connections.get(connection_role, [])
        distance_limit = tolerance if connection_points else maximum_facility_distance
        if not connection_points:
            connection_points = [facility_position]
        start_distance = min(
            _point_distance(segment["start"], point) for point in connection_points
        )
        end_distance = min(
            _point_distance(segment["end"], point) for point in connection_points
        )
        if min(start_distance, end_distance) > distance_limit:
            risks.append(role + "DoesNotReachFacility")
            continue
        branch_junctions[role] = _point_key(
            segment["end"] if start_distance <= end_distance else segment["start"],
            tolerance,
        )

    trunk_graph: dict[tuple[int, int], set[tuple[int, int]]] = {}
    for index in role_indexes.get("sharedTrunk", []):
        segment = segments[index]
        start_key = _point_key(segment["start"], tolerance)
        end_key = _point_key(segment["end"], tolerance)
        trunk_graph.setdefault(start_key, set()).add(end_key)
        trunk_graph.setdefault(end_key, set()).add(start_key)

    intake_junction = branch_junctions.get("intakeBranch")
    outlet_junction = branch_junctions.get("outletBranch")
    if intake_junction is not None and intake_junction not in trunk_graph:
        risks.append("intakeBranchDoesNotJoinSharedTrunk")
    if outlet_junction is not None and outlet_junction not in trunk_graph:
        risks.append("outletBranchDoesNotJoinSharedTrunk")
    if (
        intake_junction is not None
        and outlet_junction is not None
        and intake_junction in trunk_graph
        and outlet_junction in trunk_graph
    ):
        visited = {intake_junction}
        queue = [intake_junction]
        while queue:
            node = queue.pop()
            for neighbor in trunk_graph.get(node, set()):
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append(neighbor)
        if outlet_junction not in visited:
            risks.append("facilityBranchesJoinDifferentTrunks")

    anchor_key = _point_key(service_anchor, tolerance)
    anchor_distance = min(
        (
            _point_distance(
                {"x": key[0] * tolerance, "z": key[1] * tolerance},
                service_anchor,
            )
            for key in trunk_graph
        ),
        default=float("inf"),
    )
    if anchor_distance > anchor_tolerance:
        risks.append("sharedTrunkDoesNotReachServiceAreaAnchor")

    facility_path_nodes: list[tuple[int, int]] = []
    if intake_junction is not None and outlet_junction is not None:
        facility_path_nodes = [intake_junction, outlet_junction]
    degree_at_shared_junction = max((len(neighbors) for neighbors in graph.values()), default=0)
    if degree_at_shared_junction < 3:
        risks.append("waterPlanLacksThreeWaySharedJunction")

    return {
        "safe": len(set(risks)) == 0,
        "risks": sorted(set(risks)),
        "segmentCount": len(segments),
        "roles": {role: indexes for role, indexes in sorted(role_indexes.items())},
        "nodeCount": len(graph),
        "maximumNodeDegree": degree_at_shared_junction,
        "branchJunctions": {
            role: {"xKey": key[0], "zKey": key[1]}
            for role, key in branch_junctions.items()
        },
        "facilityPathJunctions": [
            {"xKey": key[0], "zKey": key[1]} for key in facility_path_nodes
        ],
        "serviceAreaAnchor": service_anchor,
        "serviceAreaAnchorKey": {"xKey": anchor_key[0], "zKey": anchor_key[1]},
        "serviceAreaAnchorDistance": (
            None if math.isinf(anchor_distance) else round(anchor_distance, 3)
        ),
        "thresholds": {
            "nodeTolerance": tolerance,
            "maxFacilityPipeDistance": maximum_facility_distance,
            "serviceAnchorTolerance": anchor_tolerance,
        },
        "usedExactFacilityPipeConnections": {
            role: bool(exact_connections.get(role, []))
            for role in ("intake", "outlet")
        },
    }


def _preview_water_pipe_connections(preview: dict[str, Any]) -> list[dict[str, Any]]:
    points = []
    for path in preview.get("networkPaths", []):
        if str(path.get("service", "")) != "Water":
            continue
        for node in path.get("nodes", []):
            if "x" in node and "z" in node:
                points.append({"x": float(node["x"]), "z": float(node["z"])})
    return points


WATER_FACILITY_BRANCH_MAX_LENGTH = 160.0  # one pipe piece
# a branch reaches this far in pieces of at most WATER_FACILITY_BRANCH_PIECE, joints shifted aside so
# the game keeps them (live: at 9,800 people every groundwater pump site lay 218-279m off the pipes)
WATER_FACILITY_BRANCH_REACH = 400.0
WATER_FACILITY_BRANCH_PIECE = 150.0
WATER_FACILITY_BRANCH_ZIGZAG = 10.0


def _branch_pieces(start: dict[str, float], end: dict[str, float], length: float) -> list[tuple[dict, dict]]:
    """start to end in the fewest pieces under WATER_FACILITY_BRANCH_PIECE, the joints alternately
    WATER_FACILITY_BRANCH_ZIGZAG either side of the line (a piece built straight on from another makes
    the joint a middle node the game may move)."""
    count = max(1, math.ceil(length / WATER_FACILITY_BRANCH_PIECE))
    if count == 1:
        return [(start, end)]
    dx, dz = end["x"] - start["x"], end["z"] - start["z"]
    nx, nz = -dz / length, dx / length
    points = [start]
    for index in range(1, count):
        shift = WATER_FACILITY_BRANCH_ZIGZAG if index % 2 else -WATER_FACILITY_BRANCH_ZIGZAG
        points.append({"x": round(start["x"] + dx * index / count + nx * shift, 2),
                       "z": round(start["z"] + dz * index / count + nz * shift, 2)})
    points.append(end)
    return list(zip(points, points[1:]))


def _water_facility_candidate(facility: dict[str, Any]) -> dict[str, Any]:
    return {
        "buildingPrefab": facility["prefab"],
        "buildingId": int(facility["id"]),
        "position": {"x": float(facility["position"]["x"]), "z": float(facility["position"]["z"])},
        "angleDegrees": float(facility.get("angleDegrees", 0)),
    }


TRAFFIC_READ_LIMIT = 500


def _assess_traffic_congestion(arguments: dict[str, Any]) -> dict[str, Any]:
    """Citywide flow plus congestion hotspots, each with its candidate corrections.

    Junction control is reversible, so its candidates go to trial_junction_control,
    which measures before and after and reverts a change that does not help. When
    that is spent, widen_road_chain upgrades the hotspot's roads as one chain."""
    threshold = int(arguments.get("minimumDensity", 90))
    flow_target = float(arguments.get("flowRatioTarget", 0.85))
    minimum_segments = int(arguments.get("minimumHotspotSegments", 3))
    limit = int(arguments.get("limit", 5))
    traffic = _request("GET", "/state/traffic", query={"limit": TRAFFIC_READ_LIMIT, "minimumDensity": threshold})
    roads = _request("GET", "/state/networks", query={"limit": 5000, "service": "Road"}).get("segments", [])
    maximum = float(traffic.get("maximumTrafficFlow") or 0)
    flow_ratio = float(traffic.get("totalTrafficFlow") or 0) / maximum if maximum > 0 else 0.0
    hotspots = [hotspot for hotspot in mayor_traffic.congestion_hotspots(traffic.get("segments", []), roads)
                if hotspot["segmentCount"] >= minimum_segments][:limit]
    for hotspot in hotspots:
        members = set(hotspot["segmentIds"])
        junctions = []
        for node_id in hotspot["junctionNodeIds"]:
            try:
                junction = _request("GET", "/state/junction", query={"id": node_id})
            except BridgeRequestError:
                continue  # a node with three road ends the game does not treat as a junction
            candidate = _junction_trial_candidate(junction, members)
            if candidate is not None:
                junctions.append({"nodeId": node_id, **candidate})
        # removing lights most often helps in CS1, then letting the queued road go first
        # by putting stop signs on the quiet ends; adding lights comes last
        order = {"removeTrafficLights": 0, "stopSignsOnQuietEnds": 1, "addTrafficLights": 2}
        junctions.sort(key=lambda item: (order[item["kind"]], -item["congestedRoadEnds"], item["nodeId"]))
        hotspot["junctionTrials"] = junctions
        hotspot["routeEvidence"] = {"tool": "inspect_segment_routes", "segmentId": hotspot["busiestSegmentId"]}
    return {
        "ok": True,
        "status": "congested" if flow_ratio < flow_target or hotspots else "clear",
        "flowRatio": round(flow_ratio, 4),
        "flowRatioTarget": flow_target,
        "congestedSegments": len(traffic.get("segments", [])),
        "roadSegments": traffic.get("roadSegments"),
        "minimumDensity": threshold,
        "hotspots": hotspots,
        "correctionPolicy": {
            "junctionTrials": "reversible: trial_junction_control keeps only a measured improvement",
            "upgradeCandidates": "after junction trials: widen_road_chain upgrades the chain, then repairs or clears zoning",
        },
    }


def _downstream_candidates(exit_: dict[str, Any], routes: dict[str, Any],
                           roads: dict[int, dict[str, Any]]) -> dict[str, Any]:
    """Where a saturated exit leads: the junction at its far end and the upgradeable roads that
    continue from it (followed through plain two-way joins, at most eight segments)."""
    segment = roads.get(int(exit_["segmentId"]))
    if segment is None:
        return {}
    near = {int(routes.get("startNodeId") or 0), int(routes.get("endNodeId") or 0)}
    node = int(segment["endNodeId"]) if int(segment["startNodeId"]) in near else int(segment["startNodeId"])
    by_node: dict[int, list[dict[str, Any]]] = {}
    for road in roads.values():
        for end in ("startNodeId", "endNodeId"):
            by_node.setdefault(int(road[end]), []).append(road)
    result: dict[str, Any] = {"exitSegmentId": int(segment["id"]), "farNodeId": node}
    try:
        junction = _request("GET", "/state/junction", query={"id": node})
        candidate = _junction_trial_candidate(junction, {int(segment["id"])})
        if candidate is not None:
            result["junctionTrial"] = {"nodeId": node, **candidate}
    except BridgeRequestError:
        pass  # not a junction the game controls
    chain, current, previous = [], node, int(segment["id"])
    for _ in range(8):
        onward = [road for road in by_node.get(current, []) if int(road["id"]) != previous]
        if len(onward) != 1:
            break  # a junction or a dead end ends the road
        road = onward[0]
        if str(road.get("prefab")) in mayor_traffic.SAME_WIDTH_UPGRADES:
            chain.append({"segmentId": int(road["id"]), "from": road.get("prefab"),
                          "roadPrefab": mayor_traffic.SAME_WIDTH_UPGRADES[str(road.get("prefab"))]})
        previous = int(road["id"])
        current = int(road["endNodeId"]) if int(road["startNodeId"]) == current else int(road["startNodeId"])
    if str(segment.get("prefab")) in mayor_traffic.SAME_WIDTH_UPGRADES:
        chain.insert(0, {"segmentId": int(segment["id"]), "from": segment.get("prefab"),
                         "roadPrefab": mayor_traffic.SAME_WIDTH_UPGRADES[str(segment.get("prefab"))]})
    if chain:
        result["upgradeCandidates"] = chain
    return result


# small utility buildings a bypass may clear and the services loop builds again elsewhere: the loop
# drops them on any free lot, and they stood in the way of most connectors (live: a water tower and a
# wind turbine; a high school and a police station also did, which stay)
BYPASS_RELOCATABLE = ("Water Tower", "Wind Turbine")
BYPASS_RELOCATE_WEIGHT = 3  # a relocated building costs as much as three homes cleared


def _bypass_piece_preview(prefab: str, piece: dict[str, Any], growables: dict[int, dict[str, Any]],
                          relocatable: set[int] = frozenset()) -> dict[str, Any]:
    """One connector piece previewed natively. Growables and relocatable buildings under it can be
    cleared first (their own bulldoze preview decides); the ObjectCollision they cause goes with
    them (live: a piece over six homes reported it, so no piece over a home was ever built). Any other building, a power line, unsafe terrain, another native tool error or a
    collinear merge risk blocks the piece."""
    try:
        preview = _request("POST", "/commands/preview-network",
                           body={"roadPrefab": prefab, "start": piece["start"], "end": piece["end"]})
    except BridgeRequestError as error:
        return {"buildable": False, "error": str(error)[:160]}
    risk = preview.get("nativeMoveMiddleNodeRisk")
    risky = bool(risk.get("risk") if isinstance(risk, dict) else risk)
    under = [int(item["id"]) for item in preview.get("buildingsUnderRoad") or []]
    clear = [item for item in under if item in growables]
    relocate = [item for item in under if item in relocatable]
    blocking = [item for item in under if item not in growables and item not in relocatable]
    errors = str(preview.get("toolErrors") or "None")
    clean = errors == "None" or (errors == "ObjectCollision" and (clear or relocate) and not blocking)
    return {"buildable": bool((preview.get("terrainProfile") or {}).get("safe") and clean
                              and not blocking and not preview.get("powerLinesCrossed") and not risky),
            "clearGrowables": clear, "relocateBuildings": relocate, "blockingBuildings": blocking,
            "toolErrors": errors,
            "powerLinesCrossed": len(preview.get("powerLinesCrossed") or []), "moveMiddleNodeRisk": risky,
            "terrainSafe": (preview.get("terrainProfile") or {}).get("safe"),
            "constructionCost": preview.get("constructionCost")}


BYPASS_UPSTREAM_DEPTH = 4


def _bypass_entries(routes: dict[str, Any], removed: set[int]) -> tuple[list[dict[str, Any]], int]:
    """Where the hotspot's traffic comes from, outside the hotspot: each road entering the busiest
    segment, followed upstream along its busiest feeder while it lies in the hotspot (live: the
    entries carrying 700 vehicles were hotspot segments, so only side streets carrying 1 or 2 were
    left and no hotspot got a bypass in 26 tries). The vehicles are the entry's, as many as the
    feeder carries at most. Entries reached from a highway or ramp are left out and counted: a
    town road does not bypass them (an outside link or interchange does)."""
    entries, highway_fed = [], 0
    for entry in sorted(routes.get("enteringFromSegments") or [], key=lambda item: -int(item.get("vehicles") or 0)):
        vehicles, here, seen = int(entry.get("vehicles") or 0), entry, set()
        for _ in range(BYPASS_UPSTREAM_DEPTH):
            segment = int(here.get("segmentId") or 0)
            if not segment or segment not in removed or segment in seen:
                break
            seen.add(segment)
            feeders = [item for item in _request("GET", "/state/segment-routes",
                                                 query={"id": segment, "limit": 20}).get("enteringFromSegments") or []
                       if int(item.get("segmentId") or 0) and int(item["segmentId"]) not in seen]
            if not feeders:
                break
            here = max(feeders, key=lambda item: int(item.get("vehicles") or 0))
        segment = int(here.get("segmentId") or 0)
        if not segment or segment in removed:
            continue
        if any(part in str(here.get("prefab")) for part in ("Highway", "Ramp")):
            highway_fed += vehicles
            continue
        if all(int(item["segmentId"]) != segment for item in entries):
            entries.append({**here, "vehicles": min(vehicles, int(here.get("vehicles") or vehicles))})
    return entries, highway_fed


def _propose_bypass_roads(arguments: dict[str, Any]) -> dict[str, Any]:
    """Connector roads that open a route around each congestion hotspot (see mayor.bypass).

    The demands are the hotspot's busiest segment's local entries (where traffic comes from just
    before it) paired with its in-city destination cells; the hotspot's segments and its dominant
    exit are removed from the local road graph. A demand that already has a short route around
    gets no connector. Each candidate is cut into pieces under 100m, and every piece is previewed
    natively (terrain, buildings under the road, power lines, collinear merges)."""
    prefab = str(arguments.get("roadPrefab", "Basic Road"))
    max_length = float(arguments.get("maxLength", 250))
    per_hotspot = int(arguments.get("candidateLimit", 3))
    assessment = _assess_traffic_congestion({"limit": int(arguments.get("limit", 3))})
    roads = _request("GET", "/state/networks", query={"limit": 5000, "service": "Road"}).get("segments", [])
    growables = {int(item["id"]): item for item in next(
        (value for value in _request("GET", "/state/growables", query={"limit": 50000}).values()
         if isinstance(value, list)), [])}
    relocatable = {int(item["id"]) for item in _request("GET", "/state/facilities", query={"limit": 50000})
                   .get("facilities", []) if item.get("prefab") in BYPASS_RELOCATABLE}
    results = []
    for hotspot in assessment["hotspots"]:
        routes = _request("GET", "/state/segment-routes", query={"id": hotspot["busiestSegmentId"], "limit": 20})
        diagnosis = mayor_traffic_remedies.diagnose(hotspot, routes)
        removed = set(hotspot["segmentIds"])
        if diagnosis["dominantExit"]:
            removed.add(diagnosis["dominantExit"]["segmentId"])
        graph, positions = mayor_bypass.local_graph(roads, removed)
        entries, highway_fed = _bypass_entries(routes, removed)
        destinations = [cell for cell in routes.get("destinationCells") or []
                        if abs(cell["center"]["x"]) <= mayor_traffic_remedies.OUTSIDE
                        and abs(cell["center"]["z"]) <= mayor_traffic_remedies.OUTSIDE][:3]
        demands, found = [], {}
        for entry in entries[:2]:
            for cell in destinations:
                source = (entry["position"]["x"], entry["position"]["z"])
                target = (cell["center"]["x"], cell["center"]["z"])
                if math.dist(source, target) < 300:
                    continue
                route = mayor_bypass.connectors(graph, positions, source, target, max_length=max_length,
                                                limit=per_hotspot)
                vehicles = min(int(entry.get("vehicles") or 0), int(cell.get("vehicles") or 0))
                demands.append({"entrySegmentId": int(entry["segmentId"]), "destination": cell["center"],
                                "vehicles": vehicles, "existingRoute": route["existingRoute"],
                                "straightLine": route.get("straightLine")})
                for candidate in route["candidates"]:
                    key = tuple(sorted((candidate["fromNodeId"], candidate["toNodeId"])))
                    if key in found:
                        found[key]["servesVehicles"] += vehicles
                    else:
                        found[key] = {**candidate, "servesVehicles": vehicles}
        ranked = sorted(found.values(), key=lambda item: (-item["servesVehicles"], item["detour"], item["length"]))
        previewed = []
        for candidate in ranked[:per_hotspot * 3]:
            candidate["pieces"] = mayor_bypass.pieces(candidate["from"], candidate["to"])
            previews = [_bypass_piece_preview(prefab, piece, growables, relocatable) for piece in candidate["pieces"]]
            candidate["previews"] = previews
            candidate["buildable"] = all(preview["buildable"] for preview in previews)
            clear = sorted({item for preview in previews for item in preview.get("clearGrowables", [])})
            candidate["clearGrowables"] = clear
            candidate["relocateBuildings"] = sorted({item for preview in previews
                                                     for item in preview.get("relocateBuildings", [])})
            # like a service site: a home costs 2, any other growable 1, an abandoned one nothing
            candidate["displacementCost"] = sum(0 if growables[item].get("abandoned") else
                                                2 if growables[item].get("service") == "Residential" else 1
                                                for item in clear) + \
                BYPASS_RELOCATE_WEIGHT * len(candidate["relocateBuildings"])
            candidate["constructionCost"] = sum(int(preview.get("constructionCost") or 0) for preview in previews)
            previewed.append(candidate)
        previewed.sort(key=lambda item: (not item["buildable"], -item["servesVehicles"], item["displacementCost"],
                                         item["detour"]))
        results.append({"segmentIds": hotspot["segmentIds"], "center": hotspot["center"],
                        "meanDensity": hotspot["meanDensity"], "removedSegmentIds": sorted(removed),
                        "demands": demands, "highwayFedVehicles": highway_fed,
                        "candidates": previewed[:per_hotspot]})
    return {"ok": True, "roadPrefab": prefab, "hotspots": results}


def _propose_traffic_remedies(arguments: dict[str, Any]) -> dict[str, Any]:
    """Each congestion hotspot diagnosed from its route evidence, with the kinds of remedy the
    evidence supports (see mayor.traffic_remedies) and concrete candidates for each kind.

    Junction control and widening only help when the queue forms at the hotspot itself; a
    saturated exit, a truck-dominated stream, car trips that a line could carry, or through
    traffic each call for a different action."""
    assessment = _assess_traffic_congestion({"limit": int(arguments.get("limit", 3))})
    roads = {int(segment["id"]): segment for segment in
             _request("GET", "/state/networks", query={"limit": 5000, "service": "Road"}).get("segments", [])}
    results = []
    for hotspot in assessment["hotspots"]:
        routes = _request("GET", "/state/segment-routes", query={"id": hotspot["busiestSegmentId"], "limit": 20})
        diagnosis = mayor_traffic_remedies.diagnose(hotspot, routes)
        remedies = []
        for item in mayor_traffic_remedies.remedy_kinds(diagnosis, hotspot):
            kind = item["kind"]
            if kind == "junctionControl":
                candidates: Any = hotspot["junctionTrials"]
            elif kind == "widen":
                candidates = hotspot["upgradeCandidates"]
            elif kind == "relieveDownstream":
                candidates = _downstream_candidates(diagnosis["dominantExit"], routes, roads)
            elif kind == "transit":
                candidates = {"tool": "propose_transit_lines"}
            elif kind == "cargoRail":
                candidates = {"buildingPrefab": "Cargo Center", "near": diagnosis["localAnchor"],
                              "requires": "a cargo station joined to the rail network"}
            else:
                candidates = None
            remedies.append({**item, "candidates": candidates})
        results.append({key: hotspot[key] for key in ("segmentIds", "segmentCount", "meanDensity", "center",
                                                      "busiestSegmentId", "prefabs")}
                       | {"diagnosis": diagnosis, "remedies": remedies})
    return {"ok": True, "status": assessment["status"], "flowRatio": assessment["flowRatio"], "hotspots": results}


def _junction_trial_candidate(junction: dict[str, Any], hotspot_segments: set[int]) -> dict[str, Any] | None:
    ends = junction.get("segments", [])
    congested = [end for end in ends if int(end.get("segmentId", 0)) in hotspot_segments]
    quiet = [int(end["segmentId"]) for end in ends
             if int(end.get("segmentId", 0)) not in hotspot_segments and not end.get("stopSign")]
    base = {"congestedRoadEnds": len(congested), "trafficLights": bool(junction.get("trafficLights"))}
    if junction.get("trafficLights") and junction.get("canToggleTrafficLights"):
        return {**base, "kind": "removeTrafficLights",
                "trial": {"tool": "trial_junction_control", "nodeId": junction.get("nodeId"), "trafficLights": False}}
    if junction.get("canUseStopSigns") and len(congested) >= 2 and quiet:
        return {**base, "kind": "stopSignsOnQuietEnds",
                "trial": {"tool": "trial_junction_control", "nodeId": junction.get("nodeId"),
                          "stopSignSegmentIds": sorted(quiet)}}
    if junction.get("canToggleTrafficLights"):
        return {**base, "kind": "addTrafficLights",
                "trial": {"tool": "trial_junction_control", "nodeId": junction.get("nodeId"), "trafficLights": True}}
    return None


def _traffic_samples(watched: list[int], samples: int, interval_seconds: float) -> dict[str, float]:
    readings = []
    for _ in range(samples):
        time.sleep(interval_seconds)
        traffic = _request("GET", "/state/traffic", query={"limit": TRAFFIC_READ_LIMIT, "minimumDensity": 0})
        readings.append(mayor_traffic.traffic_sample(traffic, watched))
    return mayor_traffic.mean_samples(readings)


def _junction_controls(junction: dict[str, Any]) -> dict[str, Any]:
    return {"trafficLights": bool(junction.get("trafficLights")),
            "stopSignSegmentIds": sorted(int(end["segmentId"]) for end in junction.get("segments", [])
                                         if end.get("stopSign"))}


def _trial_junction_control(arguments: dict[str, Any]) -> dict[str, Any]:
    """Change one junction's control, measure, and undo the change unless traffic clearly improved.

    The change is either traffic lights on or off, or stop signs on the listed road ends
    (the quiet ends, so the queued road goes first). The simulation runs during the
    trial; trips planned before the change keep their old lanes, so the after-reading
    waits settleSeconds before sampling."""
    node_id = int(arguments["nodeId"])
    if ("trafficLights" in arguments) == ("stopSignSegmentIds" in arguments):
        raise ValueError("Give exactly one change: trafficLights or stopSignSegmentIds")
    samples = int(arguments.get("samples", 6))
    interval = float(arguments.get("sampleSeconds", 20))
    settle = float(arguments.get("settleSeconds", 60))
    junction = _request("GET", "/state/junction", query={"id": node_id})
    before_controls = _junction_controls(junction)
    ends = {int(end["segmentId"]) for end in junction.get("segments", [])}
    checkpoint = arguments["checkpointName"]
    if "trafficLights" in arguments:
        wanted = bool(arguments["trafficLights"])
        if not junction.get("canToggleTrafficLights"):
            raise RuntimeError(f"Junction {node_id} does not allow toggling traffic lights")
        if before_controls["trafficLights"] == wanted:
            raise ValueError(f"Junction {node_id} already has trafficLights={wanted}")
        changes = [{"control": "trafficLights", "enabled": wanted}]
        undo = [{"control": "trafficLights", "enabled": not wanted}]
    else:
        targets = sorted({int(item) for item in arguments["stopSignSegmentIds"]})
        if not junction.get("canUseStopSigns"):
            raise RuntimeError(f"Junction {node_id} does not allow stop signs")
        if not set(targets) <= ends:
            raise ValueError(f"stopSignSegmentIds must be road ends at junction {node_id}")
        targets = [item for item in targets if item not in before_controls["stopSignSegmentIds"]]
        if not targets:
            raise ValueError(f"Junction {node_id} already has stop signs on those road ends")
        changes = [{"control": "stopSign", "segmentId": item, "enabled": True} for item in targets]
        undo = [{"control": "stopSign", "segmentId": item, "enabled": False} for item in targets]
    watched = sorted(ends | {int(item) for item in arguments.get("watchSegmentIds", [])})
    was_paused = bool((_request("GET", "/state/summary").get("simulation") or {}).get("paused"))
    _request("POST", "/commands/set-simulation-speed", body={"paused": False, "speed": 3})
    result: dict[str, Any] = {"ok": False, "nodeId": node_id, "controlsBefore": before_controls,
                              "changes": changes, "watchedSegmentIds": watched}
    try:
        result["before"] = _traffic_samples(watched, samples, interval)
        for change in changes:
            _request("POST", "/commands/set-junction-control",
                     body={"checkpointName": checkpoint, "nodeId": node_id, **change})
        result["controlsTried"] = _junction_controls(_request("GET", "/state/junction", query={"id": node_id}))
        time.sleep(settle)
        result["after"] = _traffic_samples(watched, samples, interval)
        verdict = mayor_traffic.trial_verdict(
            result["before"], result["after"],
            float(arguments.get("minimumDensityGain", 5)), float(arguments.get("maximumFlowDrop", 0.01)))
        result["verdict"] = verdict
        if not verdict["keep"]:
            for change in undo:
                _request("POST", "/commands/set-junction-control",
                         body={"checkpointName": checkpoint, "nodeId": node_id, **change})
        result["reverted"] = not verdict["keep"]
        result["controlsAfter"] = _junction_controls(_request("GET", "/state/junction", query={"id": node_id}))
        expected = result["controlsTried"] if verdict["keep"] else before_controls
        result["ok"] = result["controlsAfter"] == expected and result["controlsTried"] != before_controls
    finally:
        if was_paused:
            _request("POST", "/commands/set-simulation-speed", body={"paused": True})
    return result


def _widen_road_chain(arguments: dict[str, Any]) -> dict[str, Any]:
    """Upgrade a chain of road segments, then judge their zoning once.

    Each upgrade rebuilds that segment's zone blocks, and a node between an upgraded
    and an old segment can leave the cells beside it invalid; once both sides are
    upgraded the node recovers. So zoning is only checked after the whole chain:
    cells under a building's own footprint are restored, and with clearUnsupported a
    building still unsupported (the game would demolish it at random) is cleared
    when its bulldoze preview is safe, so its lot can regrow."""
    upgrades = mayor_traffic.SAME_WIDTH_UPGRADES
    upgraded, skipped = [], []
    for segment_id in [int(item) for item in arguments["segmentIds"]]:
        details = _request("GET", "/state/network-segment-details", query={"id": segment_id})
        prefab = arguments.get("roadPrefab") or upgrades.get(str(details.get("prefab")))
        if not prefab:
            skipped.append({"segmentId": segment_id, "reason": f"no same-width upgrade for {details.get('prefab')}"})
            continue
        try:
            preview = _request("POST", "/commands/upgrade-road",
                               body={"segmentId": segment_id, "roadPrefab": prefab, "dryRun": True})
            if preview.get("ok") is False or preview.get("valid") is False:
                skipped.append({"segmentId": segment_id, "reason": str(preview.get("error") or "preview invalid")[:160]})
                continue
            result = _request("POST", "/commands/upgrade-road",
                              body={"segmentId": segment_id, "roadPrefab": prefab, "confirm": True})
        except BridgeRequestError as error:
            skipped.append({"segmentId": segment_id, "reason": str(error)[:160]})
            continue
        if result.get("ok") and result.get("newSegmentId"):
            upgraded.append(int(result["newSegmentId"]))
        else:
            skipped.append({"segmentId": segment_id, "reason": str(result.get("error"))[:160]})
    outcome: dict[str, Any] = {"ok": bool(upgraded), "upgradedSegmentIds": upgraded, "skipped": skipped,
                               "cleared": []}
    if not upgraded:
        outcome["error"] = "No segment in the chain could be upgraded"
        return outcome
    try:
        _verify_rebuilt_zoning(outcome, upgraded, "Widening")
        failing = outcome["zoningSupport"]["failingAfterRepair"]
        if failing and arguments.get("clearUnsupported", True):
            for building_id in failing:
                try:
                    impact = _request("POST", "/commands/preview-bulldoze-impact",
                                      body={"entityType": "building", "id": building_id})
                    if not impact.get("safe"):
                        outcome["cleared"].append({"id": building_id, "ok": False, "risks": impact.get("risks")})
                        continue
                    cleared = _request("POST", "/commands/bulldoze",
                                       body={"entityType": "building", "id": building_id})
                    outcome["cleared"].append({"id": building_id, "ok": bool(cleared.get("ok"))})
                except BridgeRequestError as error:
                    outcome["cleared"].append({"id": building_id, "ok": False, "error": str(error)[:160]})
            if all(item["ok"] for item in outcome["cleared"]):
                outcome["ok"] = True
                outcome.pop("error", None)
                outcome["zoningSupport"]["safe"] = True
            elif any("error" in item for item in outcome["cleared"]):
                _judge_zoning_again(outcome, upgraded)
    except BridgeRequestError as error:
        outcome["postError"] = str(error)[:200]
        _judge_zoning_again(outcome, upgraded)
    return outcome


def _judge_zoning_again(outcome: dict[str, Any], upgraded: list[int]) -> None:
    """The game threw after the chain stood upgraded (live: TerrainModify.ApplyQuad once four
    segments were widened, and the widening read as failed though every segment stood upgraded and
    the network whole): the zoning is judged again from what stands now, not from the throw."""
    outcome["ok"] = True
    outcome.pop("error", None)
    try:
        _verify_rebuilt_zoning(outcome, upgraded, "Widening")
    except BridgeRequestError as error:
        outcome["ok"] = False
        outcome["error"] = f"The zoning could not be judged after the upgrade: {str(error)[:160]}"


def _unused_map_stub_nodes() -> set[int]:
    """Nodes only the map's own network uses (no town road joins them)."""
    segments = _request("GET", "/state/networks", query={"limit": 20000}).get("segments", [])
    original: set[int] = set()
    joined: set[int] = set()
    for segment in segments:
        nodes = {int(segment["startNodeId"]), int(segment["endNodeId"])}
        (original if "Original" in str(segment.get("flags")) else joined).update(nodes)
    return original - joined


def _shortfall_rows(facilities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rows for the sick and the pupils beyond the places for them (mayor.problems.shortfall_rows),
    for the families with an unlocked building to serve them."""
    services = _request("GET", "/state/city-health").get("services") or {}
    families = {family for _, family in mayor_problems.SHORTFALLS.values()}
    unlocked = set()
    for service in sorted({mayor_service_options.FAMILIES[family][0] for family in families}):
        assets = _request("GET", "/prefabs/buildings", query={"service": service}).get("buildings", [])
        unlocked |= {family for family in families if mayor_service_options.FAMILIES[family][0] == service
                     and mayor_service_options.options_for(assets, family, "capacity")}
    if not unlocked:
        return []
    homes = [item for item in next((value for value in _request("GET", "/state/growables", query={"limit": 50000})
                                    .values() if isinstance(value, list)), [])
             if item.get("service") == "Residential" and not item.get("abandoned") and item.get("position")]
    return mayor_problems.shortfall_rows(services, homes, facilities, unlocked)


def _assess_problem_remedies(arguments: dict[str, Any]) -> dict[str, Any]:
    """Every reported problem with the remedy that removes it (see mayor/problems.py)."""
    rows = _request("GET", "/state/problems", query={"limit": 50000}).get("problems", [])
    # the game marks every highway end of the map no town road has reached yet as not connected:
    # an exit the city does not use is no problem to remedy
    stubs = _unused_map_stub_nodes() if any(row.get("entityType") == "netNode" for row in rows) else set()
    ignored = [row["id"] for row in rows if row.get("entityType") == "netNode" and row.get("id") in stubs
               and row.get("problemNames") == ["RoadNotConnected"]]
    rows = [row for row in rows if not (row.get("entityType") == "netNode" and row.get("id") in ignored)]
    facilities = _request("GET", "/state/facilities", query={"limit": 50000}).get("facilities", [])
    rows = rows + _shortfall_rows(facilities)
    entries = mayor_problems.problem_remedies(
        rows, facilities, float(arguments.get("clusterLinkMeters", 250)), int(arguments.get("clusterLimit", 3)))
    return {"ok": True, "problemCount": len(entries), "unusedMapStubs": ignored,
            "unmapped": [entry["problem"] for entry in entries if entry["remedy"]["kind"] == "unmapped"],
            "problems": entries}


SERVICE_SITE_ROADS = ("Basic Road", "Gravel Road", "Small 4 Lane Road", "Small 4 Lane Road 1 Way")


def _compare_service_options(arguments: dict[str, Any]) -> dict[str, Any]:
    """Every unlocked building that provides one service family, ranked for the binding need.

    The basic building is not always the right one: a landfill that keeps filling is better
    replaced by an incinerator, and one fire station covers what three firehouses do for less
    upkeep. Options are compared from their native AI tuning fields (see mayor.service_options)."""
    family = str(arguments["family"])
    need = str(arguments["need"])
    service = mayor_service_options.FAMILIES[family][0]
    assets = _request("GET", "/prefabs/buildings", query={"service": service}).get("buildings", [])
    budget = arguments.get("budget")
    if budget is None:
        budget = float(_request("GET", "/state/city-health")["cash"]["amount"])
    ranked = mayor_service_options.options_for(
        assets, family, need, budget=budget, deficit=arguments.get("deficit"),
        site_cost=float(arguments.get("siteCost", 0)))
    return {"ok": True, "family": family, "need": need, "budget": budget,
            "deficit": arguments.get("deficit"), "options": ranked[:int(arguments.get("limit", 6))]}


def _land_use_context() -> dict[str, Any]:
    """What the land-use classifier reads besides the block: growables, polluting facilities with
    their native pollution radius, groundwater pumps (wells), junctions where a local street meets
    an arterial, and through-road segments."""
    growables = next((value for value in _request("GET", "/state/growables", query={"limit": 50000}).values()
                      if isinstance(value, list)), [])
    facilities = _request("GET", "/state/facilities", query={"limit": 50000}).get("facilities", [])
    prefabs = {item["name"]: item for item in _request("GET", "/prefabs/buildings").get("buildings", [])}
    polluters = []
    wells = []
    for facility in facilities:
        fields = ((prefabs.get(facility.get("prefab")) or {}).get("aiProfile") or {}).get("fields") or {}
        if fields.get("waterIntake") and fields.get("useGroundWater") and facility.get("position"):
            wells.append((float(facility["position"]["x"]), float(facility["position"]["z"])))
        polluting = fields.get("pollutionAccumulation") and fields.get("pollutionRadius")
        noisy = fields.get("noiseAccumulation") and fields.get("noiseRadius")
        if polluting or noisy:
            polluters.append({"id": facility["id"], "position": facility["position"],
                              "pollutionRadius": fields["pollutionRadius"] if polluting else 0,
                              "noiseRadius": fields["noiseRadius"] if noisy else 0})
    roads = _request("GET", "/state/networks", query={"limit": 5000, "service": "Road"}).get("segments", [])
    by_node: dict[int, list[str]] = {}
    positions: dict[int, tuple[float, float]] = {}
    for segment in roads:
        for end, point in (("startNodeId", "start"), ("endNodeId", "end")):
            by_node.setdefault(int(segment[end]), []).append(str(segment.get("prefab")))
            positions[int(segment[end])] = (segment[point]["x"], segment[point]["z"])
    corners = [positions[node] for node, prefabs_ in by_node.items()
               if len(prefabs_) >= 3 and any(mayor_land_use.is_arterial(p) for p in prefabs_)
               and not all(mayor_land_use.is_arterial(p) for p in prefabs_)]
    blocks = _request("GET", "/state/zone-blocks", query={"limit": 5000}).get("blocks", [])
    return {"growables": growables, "polluters": polluters, "wells": wells, "corners": corners,
            "through": mayor_land_use.through_segments(roads), "zoned": mayor_land_use.zoned_land(blocks)}


def _propose_network_crossing(arguments: dict[str, Any]) -> dict[str, Any]:
    """Three-piece overpasses from a start point over the first ground railway, highway or road on
    the way to a target (see mayor.crossing), each piece previewed natively."""
    start = (float(arguments["start"]["x"]), float(arguments["start"]["z"]))
    target = (float(arguments["target"]["x"]), float(arguments["target"]["z"]))
    prefab = str(arguments.get("roadPrefab", "Basic Road Elevated"))
    limit = int(arguments.get("limit", 3))
    segments = _request("GET", "/state/networks", query={"limit": 20000}).get("segments", [])
    ground = [segment for segment in segments
              if any(part in str(segment.get("prefab")) for part in ("Train Track", "Highway", "Road", "Metro Track"))
              and not any(level in str(segment.get("prefab")) for level in ("Elevated", "Tunnel", "Bridge", "Slope"))
              and math.dist(start, (segment["start"]["x"], segment["start"]["z"])) > 1.0
              and math.dist(start, (segment["end"]["x"], segment["end"]["z"])) > 1.0]
    barriers = mayor_crossing.crossed(start, target, ground)
    if not barriers:
        return {"ok": True, "barrier": None, "candidates": [], "note": "nothing to cross on the straight way"}
    barrier = barriers[0]
    beside = mayor_crossing.barrier_group(start, target, ground)[1:]
    candidates = []
    for pieces in mayor_crossing.overpasses(start, target, barrier, beside):
        previews = []
        for piece in pieces:
            try:
                preview = _request("POST", "/commands/preview-network", body={"roadPrefab": prefab, **piece})
                previews.append({"valid": bool(preview.get("valid")), "toolErrors": preview.get("toolErrors"),
                                 "grade": preview.get("grade"), "constructionCost": preview.get("constructionCost")})
            except BridgeRequestError as error:
                previews.append({"valid": False, "error": str(error)[:160]})
                break
        valid = len(previews) == len(pieces) and all(item["valid"] for item in previews)
        candidates.append({"pieces": pieces, "previews": previews, "valid": valid,
                           "constructionCost": sum(int(item.get("constructionCost") or 0) for item in previews)})
        if sum(1 for item in candidates if item["valid"]) >= limit:
            break
    candidates.sort(key=lambda item: not item["valid"])
    return {"ok": True, "roadPrefab": prefab,
            "barrier": {"segmentId": barrier["id"], "prefab": barrier.get("prefab")},
            "beside": [{"segmentId": item["id"], "prefab": item.get("prefab")} for item in beside],
            "candidates": [item for item in candidates if item["valid"]][:limit] or candidates[:1]}


def _propose_expansion_areas(arguments: dict[str, Any]) -> dict[str, Any]:
    """Free land in the unlocked tiles that the city can grow into (see mayor.expansion): each area
    with the use its surroundings allow, the ways in from local roads with what each would cross,
    and a fishbone layout along its long axis."""
    use = arguments.get("use")
    limit = int(arguments.get("limit", 5))
    resolution = int(arguments.get("resolution", 64))
    minimum_area = float(arguments.get("minimumAreaSquareMeters", 40000))
    spacing = MAP_TILE_SIZE / (resolution - 1)
    cells: list[dict[str, Any]] = []
    for tile in _request("GET", "/state/map-areas").get("areas", []):
        if not tile.get("unlocked"):
            continue
        min_x = mayor_expansion.MAP_EDGE + tile["x"] * MAP_TILE_SIZE
        min_z = mayor_expansion.MAP_EDGE + tile["z"] * MAP_TILE_SIZE
        cells += _request("GET", "/state/map-analysis", query={
            "minX": min_x, "maxX": min_x + MAP_TILE_SIZE, "minZ": min_z, "maxZ": min_z + MAP_TILE_SIZE,
            "resolution": resolution}).get("cells", [])
    segments = _request("GET", "/state/networks", query={"limit": 20000}).get("segments", [])
    context = _land_use_context()
    facilities = _request("GET", "/state/facilities", query={"limit": 50000}).get("facilities", [])
    free = mayor_expansion.free_samples(cells, segments, context["growables"] + facilities, spacing)
    if use:
        # an area judged by its centre lost all its land to one rule: the only large area, centred
        # in the industrial district, gave homes nothing for seven wakes (live)
        free = mayor_land_use.ground_for(use, free, context["growables"], context["polluters"],
                                         arguments.get("plan") or [], context["zoned"], context["wells"])
    by_key = {mayor_expansion.grid_key((cell["position"]["x"], cell["position"]["z"]), spacing): cell
              for cell in cells}
    results = []
    for area in mayor_expansion.areas(free, spacing):
        if area["areaSquareMeters"] < minimum_area:
            break
        verdict = mayor_land_use.classify({"center": area["center"], "roadPrefab": "Basic Road"},
                                          context["growables"], plan=arguments.get("plan") or [],
                                          polluters=context["polluters"], zoned=context["zoned"],
                                          wells=context["wells"])
        if use and use not in verdict["allowed"]:
            continue
        ways = mayor_expansion.access(area, segments, by_key, spacing,
                                      buildings=context["growables"] + facilities)
        layout = mayor_expansion.fishbone(area, ways[0]) if ways else None
        # a planned rectangle may name the layout of the land inside it
        plan_layout = next((str(item["layout"]) for item in arguments.get("plan") or [] if item.get("layout")
                            and item["minX"] <= area["center"]["x"] <= item["maxX"]
                            and item["minZ"] <= area["center"]["z"] <= item["maxZ"]), None)
        ranked = mayor_layouts.candidates(area, use or verdict["preferred"] or "residential", plan_layout)
        keep_points = arguments.get("includePoints") is True
        results.append({key: value for key, value in area.items() if keep_points or key != "points"}
                       | {"allowedUses": verdict["allowed"], "preferredUse": verdict["preferred"],
                          "useReason": verdict["reason"], "access": ways, "fishbone": layout, "layouts": ranked})
        if len(results) >= limit:
            break
    # land reachable without a grade separation or a bridge comes first, then land whose surroundings
    # prefer the use asked for, then size
    results.sort(key=lambda item: (not (item["access"] and item["access"][0]["clear"]),
                                   bool(use) and item["preferredUse"] != use, -item["areaSquareMeters"]))
    return {"ok": True, "use": use, "sampleSpacing": round(spacing, 2), "freeSamples": len(free), "areas": results}


PARTIAL_BLOCK_CELLS = 8  # free cells that make a block a building stands on in part worth zoning


def _propose_zoning_blocks(arguments: dict[str, Any]) -> dict[str, Any]:
    """Unzoned blocks that suit one use, nearest to developed land first (see mayor.land_use).

    The use of a block comes from its neighbourhood, the distance rules between homes, industry
    and polluting facilities, and road class; planned areas passed by the caller override it."""
    use = str(arguments["use"])
    plan = arguments.get("plan") or []
    limit = int(arguments.get("limit", 10))
    capacity = _request("GET", "/state/zone-blocks", query={"limit": 5000}).get("blocks", [])
    # buildings would grow beside a road that leads nowhere; zone only roads that reach the outside
    reached, _ = mayor_network.road_components(
        _request("GET", "/state/networks", query={"limit": 20000}).get("segments", []))
    # a block a school, park or utility stands on in part keeps its free cells for zoning (live: 79
    # unzoned blocks held 1,005 free cells beside them while no clear block was left for homes)
    blocks = [block for block in capacity if block.get("fullyUnzoned")
              and (block.get("occupancyClear") or mayor_land_use.vacant_cells(block) >= PARTIAL_BLOCK_CELLS)
              and block.get("developmentFrontageSafe")
              and int(block.get("roadSegmentId") or 0) in reached]
    context = _land_use_context()
    developed = [(item["position"]["x"], item["position"]["z"]) for item in context["growables"]]
    center = mayor_land_use.town_center(context["growables"])
    commercial = [(item["position"]["x"], item["position"]["z"]) for item in context["growables"]
                  if item.get("service") == "Commercial"]
    candidates = []
    for block in blocks:
        verdict = mayor_land_use.classify(block, context["growables"], plan=plan, polluters=context["polluters"],
                                          arterial_corners=context["corners"], commercial_blocks=commercial,
                                          through_roads=context["through"], zoned=context["zoned"],
                                          wells=context["wells"])
        if use not in verdict["allowed"]:
            continue
        point = (block["center"]["x"], block["center"]["z"])
        candidates.append({
            "blockId": block["blockId"], "center": block["center"], "roadPrefab": block.get("roadPrefab"),
            "validCells": block.get("validCells"), "preferred": verdict["preferred"] == use,
            **({} if block.get("occupancyClear") else {"partial": True, "vacantCells": mayor_land_use.vacant_cells(block)}),
            "reason": verdict["reason"],
            "distanceToDeveloped": round(min((math.dist(point, other) for other in developed), default=0.0), 1),
            "distanceToCenter": round(math.dist(point, center), 1) if center else None})
    # the use the neighbourhood prefers first, then contiguous growth: services and utilities reach it
    candidates.sort(key=lambda item: (not item["preferred"], item["distanceToDeveloped"],
                                      item["distanceToCenter"] or 0.0))
    return {"ok": True, "use": use, "townCenter": center, "candidateCount": len(candidates),
            "candidates": candidates[:limit]}


def _propose_service_sites(arguments: dict[str, Any]) -> dict[str, Any]:
    """Road-facing sites for one service building near a point, cheapest displacement first.

    Built-up districts rarely have a free lot of facility size, so a site may cover
    growables: abandoned ones cost nothing, homes cost 2 and other growables 1. A site
    never covers a service building or a road, and it must keep native road access.
    The caller bulldozes the covered growables (after their own preview) and then
    places the building with place_service at the returned position and angle.

    A building that emits ground pollution (a landfill or incinerator) keeps every home
    residentialClearance away, by default twice its native pollution radius: pollution
    spreads past that radius over time, and a landfill placed among the homes it was
    meant to serve made them sick (Pollution problems 87m away). It keeps GROUNDWATER_CLEARANCE
    from a groundwater pump too, whose water the pollution would foul."""
    prefab = str(arguments["buildingPrefab"])
    near = arguments["near"]
    radius = float(arguments.get("radius", 600))
    limit = int(arguments.get("limit", 5))
    probe = _request("POST", "/commands/preview-placement", body={
        "buildingPrefab": prefab, "position": near, "angleDegrees": 0})
    footprint = probe.get("footprint") or {}
    depth = float(footprint.get("length") or 32)
    width = float(footprint.get("width") or depth)
    # the angle that faces the road may turn either side of an oblong lot to it, and the lot sits
    # a little past the kerb (live: an 80x64m landfill overlapped its road at every angle 0.5m
    # from the kerb, and fitted with its 80m side across the road 4m out)
    reaches = list(dict.fromkeys((8 + depth / 2 + 0.5, 8 + width / 2 + 0.5, 8 + width / 2 + 4, 8 + depth / 2 + 4)))
    environmental = probe.get("environmental") or {}
    pollutes = float(environmental.get("groundPollutionAccumulation") or 0) > 0
    clearance = float(arguments.get("residentialClearance",
                                    2 * float(environmental.get("groundPollutionRadius") or 0) if pollutes else 0))
    growables = {int(item["id"]): item for item in next(
        (value for value in _request("GET", "/state/growables", query={"limit": 50000}).values()
         if isinstance(value, list)), [])}
    homes = [(int(item["id"]), (float(item["position"]["x"]), float(item["position"]["z"])))
             for item in growables.values()
             if item.get("service") == "Residential" and not item.get("abandoned") and item.get("position")]
    wells = _land_use_context()["wells"] if pollutes else []
    roads = [segment for segment in _request("GET", "/state/networks", query={"limit": 5000, "service": "Road"})
             .get("segments", []) if segment.get("prefab") in SERVICE_SITE_ROADS]
    sites = []
    for segment in roads:
        start, end = segment["start"], segment["end"]
        middle = ((start["x"] + end["x"]) / 2, (start["z"] + end["z"]) / 2)
        length = math.dist((start["x"], start["z"]), (end["x"], end["z"]))
        if length < 40 or math.dist(middle, (near["x"], near["z"])) > radius:
            continue
        ux, uz = (end["x"] - start["x"]) / length, (end["z"] - start["z"]) / length
        for side in (1, -1):
            nx, nz = -uz * side, ux * side
            facing = math.degrees(math.atan2(nz, nx))
            best = None
            for reach, angle in [(reach, angle) for reach in reaches for angle in
                                 sorted({round((facing + turn) % 360, 1) for turn in (0, 90, 180, 270)})]:
                if best is not None and best["reach"] != reach:
                    break
                position = {"x": round(middle[0] + nx * reach, 2), "z": round(middle[1] + nz * reach, 2)}
                preview = _request("POST", "/commands/preview-placement", body={
                    "buildingPrefab": prefab, "position": position, "angleDegrees": angle,
                    "requireRoadAccess": True})
                risks = set((preview.get("safety") or {}).get("risks") or [])
                if preview.get("roadOverlaps") or risks - {"buildingOverlap"}:
                    continue
                covered = [int(item["id"]) for item in preview.get("buildingOverlaps") or []]
                if any(item not in growables for item in covered):
                    continue
                if clearance and any(home not in covered and
                                     math.dist(spot, (position["x"], position["z"])) < clearance
                                     for home, spot in homes):
                    continue
                if any(math.dist(well, (position["x"], position["z"])) < GROUNDWATER_CLEARANCE for well in wells):
                    continue
                cost = sum(0 if growables[item].get("abandoned") else
                           2 if growables[item].get("service") == "Residential" else 1 for item in covered)
                site = {"position": position, "angleDegrees": angle, "reach": reach,
                        "roadSegmentId": int(segment["id"]),
                        "displacementCost": cost, "distance": round(math.dist(
                            (position["x"], position["z"]), (near["x"], near["z"])), 1),
                        "coveredGrowables": [{"id": item, "prefab": growables[item].get("prefab"),
                                              "abandoned": bool(growables[item].get("abandoned"))}
                                             for item in covered]}
                if best is None or (cost, site["distance"]) < (best["displacementCost"], best["distance"]):
                    best = site
            if best:
                sites.append(best)
    sites.sort(key=lambda site: (site["displacementCost"], site["distance"]))
    return {"ok": True, "buildingPrefab": prefab, "footprint": footprint, "near": near,
            "residentialClearance": clearance, "sites": sites[:limit]}


RUINED_FLAGS = ("BurnedDown", "Collapsed")


def _ruined_growables() -> list[dict[str, Any]]:
    growables = next((value for value in _request("GET", "/state/growables", query={"limit": 50000}).values()
                      if isinstance(value, list)), [])
    return [item for item in growables
            if item.get("abandoned") or any(flag in str(item.get("flags") or "") for flag in RUINED_FLAGS)]


def _clear_abandoned_buildings(arguments: dict[str, Any]) -> dict[str, Any]:
    """Bulldoze every abandoned, burned-down or collapsed growable so its lot regrows.

    Such buildings never recover on their own, and they spread their problems to the
    neighbourhood. Each one is cleared only when its bulldoze preview is safe; one the
    game refuses to release is reported, not retried, so the caller can look at it."""
    dry_run = bool(arguments.get("dryRun", False))
    ruined = _ruined_growables()[: int(arguments.get("limit", 50))]
    cleared, skipped, refused, gone = [], [], [], []
    for building in ruined:
        building_id = int(building["id"])
        try:
            impact = _request("POST", "/commands/preview-bulldoze-impact",
                              body={"entityType": "building", "id": building_id})
        except BridgeRequestError as error:
            if "not found" not in str(error).lower():
                raise
            gone.append(building_id)  # released since the listing (the game or another caller)
            continue
        if not impact.get("safe"):
            skipped.append({"id": building_id, "risks": impact.get("risks")})
            continue
        if dry_run:
            cleared.append(building_id)
            continue
        try:
            outcome = _request("POST", "/commands/bulldoze", body={"entityType": "building", "id": building_id})
        except BridgeRequestError as error:
            refused.append({"id": building_id, "error": str(error)[:200]})
            continue
        (cleared if outcome.get("ok") else refused).append(
            building_id if outcome.get("ok") else {"id": building_id, "error": str(outcome.get("error"))[:200]})
    remaining = [] if dry_run else [int(item["id"]) for item in _ruined_growables() if int(item["id"]) in set(cleared)]
    return {"ok": not refused and not remaining, "dryRun": dry_run, "found": len(ruined),
            "cleared": cleared, "alreadyGone": gone, "skipped": skipped, "refused": refused,
            "stillPresent": remaining}


def _rebuild_full_facility(arguments: dict[str, Any]) -> dict[str, Any]:
    """Replace a full landfill or cemetery with a new one of the same prefab on its own lot.

    A full one stops collecting, so its whole area goes unserved. Rebuilding discards
    what it stored and is far cheaper than more upkeep elsewhere. The rebuild is
    previewed on the same lot (ignoring the old building) before anything is
    demolished, so a lot that cannot take the new building keeps the old one."""
    building_id = int(arguments["buildingId"])
    facility = next((item for item in _request("GET", "/state/facilities", query={"limit": 50000})
                     .get("facilities", []) if int(item["id"]) == building_id), None)
    if facility is None:
        raise ValueError(f"Facility {building_id} was not found")
    if mayor_problems.facility_family(facility) not in ("deathcare", "garbage"):
        raise ValueError(f"Facility {building_id} ({facility.get('prefab')}) is not a landfill or cemetery")
    if not mayor_problems.facility_full(facility):
        raise RuntimeError(f"Facility {building_id} is not full")
    placement = {"buildingPrefab": facility["prefab"],
                 "position": {"x": facility["position"]["x"], "z": facility["position"]["z"]},
                 "angleDegrees": facility.get("angleDegrees", 0)}
    preview = _request("POST", "/commands/preview-placement",
                       body={**placement, "requireRoadAccess": True, "ignoreBuildingId": building_id})
    result: dict[str, Any] = {"ok": False, "oldBuildingId": building_id, "prefab": facility["prefab"],
                              "position": placement["position"], "angleDegrees": placement["angleDegrees"]}
    if not (preview.get("valid") and (preview.get("safety") or {}).get("hardSafe")):
        result["error"] = "The lot cannot take the rebuilt facility; the full one was kept"
        result["safety"] = preview.get("safety")
        return result
    removed = _request("POST", "/commands/bulldoze",
                       body={"entityType": "building", "id": building_id, "confirmRisks": True})
    if not removed.get("ok"):
        result["error"] = f"Bulldoze failed: {removed.get('error')}"
        return result
    placed = _request("POST", "/commands/place-building", body=placement)
    result.update({"ok": bool(placed.get("ok")), "newBuildingId": placed.get("buildingId"),
                   "constructionCost": placed.get("constructionCost")})
    if not placed.get("ok"):
        result["error"] = (f"Rebuild failed after the full facility was removed: {placed.get('error')}; "
                           "restore the checkpoint")
    return result


def _propose_transit_lines(arguments: dict[str, Any]) -> dict[str, Any]:
    """Bus lines for the car trips that load congested roads, each previewed natively.

    For each hotspot's busiest segment it reads the remaining car paths (PassengerCarAI
    only, so service vehicles do not count), keeps origin-destination cell pairs too
    far apart to walk, merges both directions, and routes the busiest pairs over the
    local road graph with stops spaced for walking. Ridership of existing lines is
    reported alongside, since a line that already serves a corridor needs vehicles
    or a faster route, not a twin."""
    hotspot_limit = int(arguments.get("hotspotLimit", 3))
    corridor_limit = int(arguments.get("corridorLimit", 3))
    minimum_trip = float(arguments.get("minimumTripMeters", 900))
    spacing = float(arguments.get("stopSpacing", 350))
    assessment = _assess_traffic_congestion({"limit": hotspot_limit})
    reports, shares = [], []
    for hotspot in assessment["hotspots"]:
        segment_id = hotspot["busiestSegmentId"]
        everyone = _request("GET", "/state/segment-routes", query={"id": segment_id, "limit": 5})
        cars = _request("GET", "/state/segment-routes",
                        query={"id": segment_id, "limit": 40, "vehicleAI": "PassengerCarAI"})
        reports.append(cars)
        total = int(everyone.get("vehiclesPlanningToUse") or 0)
        shares.append({"segmentId": segment_id, "vehicles": total,
                       "passengerCars": int(cars.get("vehiclesPlanningToUse") or 0),
                       "passengerCarShare": round(int(cars.get("vehiclesPlanningToUse") or 0) / total, 3) if total else 0.0})
    roads = _request("GET", "/state/networks", query={"limit": 5000, "service": "Road"}).get("segments", [])
    graph = mayor_transit.road_graph(roads)
    positions = mayor_transit.node_positions(roads)
    proposals = []
    owned = {(int(area["x"]), int(area["z"])) for area in _request("GET", "/state/map-areas").get("areas", [])
             if area.get("unlocked")}
    for corridor in mayor_transit.corridor_pairs(reports, minimum_trip, owned)[:corridor_limit]:
        start = mayor_transit.nearest_node(graph, positions, (corridor["a"]["x"], corridor["a"]["z"]))
        goal = mayor_transit.nearest_node(graph, positions, (corridor["b"]["x"], corridor["b"]["z"]))
        path = mayor_transit.shortest_path(graph, start, goal) if start is not None and goal is not None else []
        stops = mayor_transit.place_stops(path, positions, spacing)
        proposal = {**corridor, "routeLength": round(mayor_transit.path_length(path, positions), 1),
                    "stops": stops}
        if len(stops) >= 2:
            preview = _request("POST", "/commands/create-transport-line", body={
                "transportType": "Bus", "stops": stops, "completeLoop": True, "dryRun": True})
            proposal["preview"] = {key: preview.get(key) for key in ("ok", "valid", "error", "errors", "risks")
                                   if key in preview}
        proposals.append(proposal)
    lines = _request("GET", "/state/transport-lines", query={"limit": 50}).get("lines", [])
    return {"ok": True, "flowRatio": assessment["flowRatio"], "hotspotCarShares": shares,
            "proposals": proposals,
            "existingLines": [{"id": line["id"], "name": line.get("name"), "type": line.get("transportType"),
                               "vehicles": line.get("vehicles"), "stops": line.get("stops"),
                               "weeklyPassengers": line.get("weeklyPassengers")}
                              for line in lines if line.get("stops")]}


def _connect_water_facility(arguments: dict[str, Any]) -> dict[str, Any]:
    """Connect one placed intake or outlet to the live water network with a single pipe.

    The rest of the network is already built and verified, so instead of a whole-system plan
    the gates are: the facility forms a safe pair with an already connected counterpart, the
    branch starts at the facility's native pipe connection and ends on the nearest live pipe
    node, it passes native preview, and the facility reports connected afterwards."""
    dry_run = bool(arguments.get("dryRun", False))
    pipe_prefab = str(arguments.get("pipePrefab") or "Water Pipe")
    facilities = {
        int(item["id"]): item
        for item in _request("GET", "/state/facilities", query={"service": "Water", "limit": 1000}).get("facilities", [])
    }
    facility = facilities.get(int(arguments["buildingId"]))
    counterpart = facilities.get(int(arguments["pairBuildingId"]))
    if facility is None or counterpart is None:
        raise ValueError("buildingId and pairBuildingId must be existing water facilities")
    if "WaterNotConnected" not in str(facility.get("problems", "")):
        raise RuntimeError(f"Water facility {facility['id']} is already connected to the pipe network")
    if "WaterNotConnected" in str(counterpart.get("problems", "")):
        raise RuntimeError(f"Pair facility {counterpart['id']} is not connected to the pipe network")
    roles: dict[str, dict[str, Any]] = {}
    for item in (facility, counterpart):
        name = str(item["prefab"]).lower()
        role = "intake" if "intake" in name or "pumping" in name or "tower" in name else "outlet" if "outlet" in name or "drain" in name or "treatment" in name else ""
        if not role or role in roles:
            raise ValueError("buildingId and pairBuildingId must be one intake and one outlet")
        roles[role] = item
    role = "intake" if roles["intake"] is facility else "outlet"
    pair = _inspect_water_pair({key: _water_facility_candidate(item) for key, item in roles.items()})
    result: dict[str, Any] = {
        "ok": False,
        "dryRun": dry_run,
        "buildingId": int(facility["id"]),
        "role": role,
        "pairBuildingId": int(counterpart["id"]),
        "waterPair": {"safe": bool(pair.get("safe")), "risks": pair.get("risks", [])},
    }
    if not pair.get("safe"):
        result["error"] = "The facility does not form a safe water pair with pairBuildingId."
        return result
    connections = _preview_water_pipe_connections(pair.get("previews", {}).get(role, {}))
    if not connections:
        result["error"] = "The native preview reports no pipe connection point for this facility."
        return result
    live = _request("GET", "/state/networks", query={"service": "Water", "limit": 5000}).get("segments", [])
    nodes = [
        {"x": float(segment[end]["x"]), "z": float(segment[end]["z"])}
        for segment in live
        if str(segment.get("service", "")) == "Water"
        for end in ("start", "end")
    ]
    if not nodes:
        result["error"] = "There is no live water network to connect to."
        return result
    start, end, length = min(
        ((point, node, _point_distance(point, node)) for point in connections for node in nodes),
        key=lambda item: item[2],
    )
    result["branch"] = {"start": start, "end": end, "length": round(length, 3)}
    if length <= 2.0:
        result["error"] = "A live pipe node already sits on the connection point; the game should report it connected."
        return result
    if length > WATER_FACILITY_BRANCH_REACH:
        result["error"] = f"The nearest live pipe node is {length:.0f}m away; at most {WATER_FACILITY_BRANCH_REACH:.0f}m."
        return result
    name = arguments.get("name", "Facility branch")
    bodies = [{"roadPrefab": pipe_prefab, "start": a, "end": b, "name": name}
              for a, b in _branch_pieces(start, end, length)]
    previews = [_request("POST", "/commands/preview-network", body=body) for body in bodies]
    result["preview"] = {key: previews[0].get(key) for key in ("valid", "toolErrors", "constructionCost", "length")}
    if len(bodies) > 1:
        result["preview"] = {"valid": all(item.get("valid") for item in previews), "pieces": len(bodies),
                             "toolErrors": [item.get("toolErrors") for item in previews if not item.get("valid")],
                             "constructionCost": sum(int(item.get("constructionCost") or 0) for item in previews)}
    if not all(item.get("ok") and item.get("valid") for item in previews):
        result["error"] = "The branch pipe fails native preview."
        return result
    if dry_run:
        result["ok"] = True
        return result
    built: list[int] = []
    for body in bodies:
        mutation = _request("POST", "/commands/build-network", body=body)
        if not mutation.get("ok"):
            break
        built.append(int(mutation.get("segmentId") or 0))
    if len(built) < len(bodies):
        # a branch that stops partway joins nothing: its pieces come out again
        for segment in reversed([item for item in built if item]):
            _request("POST", "/commands/bulldoze", body={"entityType": "netSegment", "id": segment})
    result["mutation"] = {key: mutation.get(key) for key in ("ok", "segmentId", "error")}
    if len(bodies) > 1:
        result["mutation"]["segmentIds"] = built
    after = {
        int(item["id"]): item
        for item in _request("GET", "/state/facilities", query={"service": "Water", "limit": 1000}).get("facilities", [])
    }.get(int(facility["id"]), {})
    result["connected"] = "WaterNotConnected" not in str(after.get("problems", ""))
    result["ok"] = bool(mutation.get("ok")) and result["connected"]
    if not result["ok"]:
        result["error"] = "The facility still reports WaterNotConnected; restore the checkpoint."
    return result


def _matching_existing_water_segment(
    live_segments: list[dict[str, Any]],
    planned_segment: dict[str, Any],
    pipe_prefab: str,
    tolerance: float = 2.0,
) -> dict[str, Any] | None:
    for live in live_segments:
        if str(live.get("service", "")) != "Water":
            continue
        if str(live.get("prefab", "")) != pipe_prefab:
            continue
        live_start = live.get("start") or {}
        live_end = live.get("end") or {}
        planned_start = planned_segment["start"]
        planned_end = planned_segment["end"]
        same_direction = (
            _point_distance(live_start, planned_start) <= tolerance
            and _point_distance(live_end, planned_end) <= tolerance
        )
        reverse_direction = (
            _point_distance(live_start, planned_end) <= tolerance
            and _point_distance(live_end, planned_start) <= tolerance
        )
        if same_direction or reverse_direction:
            return live
    return None


def _inspect_water_system_plan(
    arguments: dict[str, Any],
    completed_segment_indexes: set[int] | None = None,
) -> dict[str, Any]:
    arguments = _normalize_water_pipe_segments(arguments)
    pair_arguments = {
        key: arguments[key]
        for key in (
            "intake",
            "outlet",
            "minSeparation",
            "minDownstreamProjection",
            "samplePadding",
            "resolution",
            "checkUnlockedReach",
            "unlockedReachResolution",
            "minFlowSpeed",
            "minUnlockedFlowAgreement",
            "maxUpstreamGap",
            "maxDownstreamGap",
        )
        if key in arguments
    }
    pair = _inspect_water_pair(pair_arguments)
    topology_arguments = dict(arguments)
    topology_arguments["_facilityPipeConnections"] = {
        role: _preview_water_pipe_connections(pair.get("previews", {}).get(role, {}))
        for role in ("intake", "outlet")
    }
    topology = _inspect_water_pipe_topology(topology_arguments)
    pipe_prefab_is_water = _is_water_network_prefab(str(arguments["pipePrefab"]))
    completed_segment_indexes = completed_segment_indexes or set()
    live_water_segments: list[dict[str, Any]] = []
    if completed_segment_indexes:
        live_water_segments = _request(
            "GET",
            "/state/networks",
            query={"service": "Water", "limit": 5000},
        ).get("segments", [])
    previews = []
    preview_risks: list[str] = []
    for index, segment in enumerate(arguments["segments"]):
        if index in completed_segment_indexes:
            existing = _matching_existing_water_segment(
                live_water_segments,
                segment,
                str(arguments["pipePrefab"]),
            )
            valid = existing is not None
            if not valid:
                preview_risks.append("completedSegmentMissing:" + str(index))
            previews.append(
                {
                    "index": index,
                    "role": segment["role"],
                    "valid": valid,
                    "alreadyBuilt": valid,
                    "segmentId": None if existing is None else existing.get("id"),
                    "toolErrors": None if valid else "ExistingWaterSegmentNotFound",
                    "constructionCost": 0 if valid else None,
                    "length": (
                        None
                        if existing is None
                        else round(_point_distance(existing["start"], existing["end"]), 3)
                    ),
                }
            )
            continue
        preview = _request(
            "POST",
            "/commands/preview-network",
            body={
                "roadPrefab": arguments["pipePrefab"],
                "start": segment["start"],
                "end": segment["end"],
                "name": segment.get("name", ""),
            },
        )
        valid = bool(preview.get("ok", False)) and bool(preview.get("valid", False))
        if not valid:
            preview_risks.append("segmentPreviewFailed:" + str(index))
        previews.append(
            {
                "index": index,
                "role": segment["role"],
                "valid": valid,
                "alreadyBuilt": False,
                "toolErrors": preview.get("toolErrors"),
                "constructionCost": preview.get("constructionCost"),
                "length": preview.get("length"),
            }
        )

    risks = (
        ["unsafeWaterPair:" + risk for risk in pair.get("risks", [])]
        + ["unsafePipeTopology:" + risk for risk in topology.get("risks", [])]
        + ([] if pipe_prefab_is_water else ["pipePrefabIsNotWaterNetwork"])
        + preview_risks
    )
    total_cost = sum(
        int(item.get("constructionCost") or 0)
        for item in previews
        if item.get("constructionCost") is not None
    )
    return {
        "ok": True,
        "safe": len(risks) == 0,
        "planId": _water_plan_id(arguments),
        "risks": risks,
        "pipePrefab": arguments["pipePrefab"],
        "segments": arguments["segments"],
        "normalization": {
            "maximumSegmentLength": arguments["maxWaterPipeSegmentLength"],
            "segmentCount": len(arguments["segments"]),
        },
        "pipePrefabIsWaterNetwork": pipe_prefab_is_water,
        "waterPair": {
            "safe": pair.get("safe", False),
            "risks": pair.get("risks", []),
            "warnings": pair.get("warnings", []),
            "geometry": pair.get("geometry"),
            "flow": pair.get("flow"),
            "pollution": pair.get("pollution"),
            "unlockedReach": pair.get("unlockedReach"),
        },
        "pipeTopology": topology,
        "segmentPreviews": previews,
        "totalConstructionCost": total_cost,
    }


def _is_water_network_prefab(prefab_name: str) -> bool:
    payload = _request("GET", "/prefabs/networks", query={"service": "Water"})
    return any(
        str(item.get("name", item.get("prefab", ""))) == prefab_name
        for item in payload.get("networks", [])
    )


def _placement_body(arguments: dict[str, Any]) -> dict[str, Any]:
    body = {
        "buildingPrefab": arguments["buildingPrefab"],
        "position": arguments["position"],
        "angleDegrees": arguments.get("angleDegrees", 0),
        "ignoreBuildingId": arguments.get("buildingId", 0),
        "requireRoadAccess": arguments.get("requireRoadAccess", False),
        "maxRoadDistance": arguments.get("maxRoadDistance", 64),
    }
    return body


def _preview_power_plant_candidate(arguments: dict[str, Any]) -> dict[str, Any]:
    placement = _request(
        "POST",
        "/commands/preview-placement",
        body=_placement_body(arguments),
    )
    profile = placement.get("electricityProfile")
    risks: list[str] = []
    warnings: list[str] = []
    if not placement.get("valid", False):
        risks.append("nativePlacementUnsafe")
    if not profile:
        risks.append("prefabIsNotPowerPlant")
        return {
            "ok": True,
            "safe": False,
            "risks": risks,
            "placement": placement,
        }

    minimum_output_ratio = float(arguments.get("minimumVariableOutputRatio", 0.75))
    output_ratio = float(profile.get("estimatedOutputRatio", 0) or 0)
    if profile.get("locationDependentProduction", False) and output_ratio < minimum_output_ratio:
        risks.append("locationDependentOutputBelowMinimum")

    environmental = placement.get("environmental", {})
    maximum_noise_exposure = int(arguments.get("maximumResidentialNoiseExposure", 0))
    maximum_pollution_exposure = int(arguments.get("maximumResidentialPollutionExposure", 0))
    noise_exposure = int(environmental.get("residentialBuildingsInsideNoiseRadius", 0) or 0)
    pollution_exposure = int(
        environmental.get("residentialBuildingsInsideGroundPollutionRadius", 0) or 0
    )
    if noise_exposure > maximum_noise_exposure:
        risks.append("excessResidentialNoiseExposure")
    if pollution_exposure > maximum_pollution_exposure:
        risks.append("excessResidentialGroundPollutionExposure")

    reachability = None
    if profile.get("fuelBased", False):
        road_access = placement.get("roadAccess", {})
        segment_id = int(road_access.get("nearestSegmentId", 0) or 0)
        if not road_access.get("accessible", False) or segment_id <= 0:
            risks.append("fuelPlantLacksRoadAccess")
        else:
            reachability = _request(
                "GET",
                "/state/road-reachability",
                query={"id": segment_id},
            )
            if not reachability.get("safeForTwoWayFreight", False):
                risks.append("fuelDeliveryCannotReachAndReturnFromOutside")
            inbound = reachability.get("inboundFromOutside", {})
            outbound = reachability.get("outboundToOutside", {})
            maximum_route_distance = float(arguments.get("maximumFuelRouteDistance", 5000))
            maximum_traffic_density = int(
                arguments.get("maximumFuelRouteTrafficDensity", 200)
            )
            if max(
                float(inbound.get("distance", 0) or 0),
                float(outbound.get("distance", 0) or 0),
            ) > maximum_route_distance:
                risks.append("fuelRouteTooLong")
            if max(
                int(inbound.get("maximumTrafficDensity", 0) or 0),
                int(outbound.get("maximumTrafficDensity", 0) or 0),
            ) > maximum_traffic_density:
                risks.append("fuelRouteTooCongested")
            if reachability.get("limitations", {}).get(
                "turnRestrictionsAndDistrictHeavyTrafficPoliciesNotEvaluated", False
            ):
                warnings.append("postBuildFuelVehiclePathVerificationRequired")

    capacity = _request(
        "GET",
        "/state/service-capacity",
        query={"service": "Electricity"},
    )
    return {
        "ok": True,
        "safe": len(risks) == 0,
        "risks": risks,
        "warnings": warnings,
        "classification": {
            "aiType": profile.get("aiType"),
            "renewable": profile.get("renewable"),
            "locationDependentProduction": profile.get(
                "locationDependentProduction", False
            ),
            "fuelBased": profile.get("fuelBased", False),
            "resourceType": profile.get("resourceType"),
        },
        "production": {
            "productionRatePercent": profile.get("productionRatePercent"),
            "maximum": profile.get("maximumElectricityProduction"),
            "estimated": profile.get("estimatedElectricityProduction"),
            "estimatedOutputRatio": output_ratio,
            "minimumVariableOutputRatio": minimum_output_ratio,
        },
        "environmental": {
            "noiseAccumulation": profile.get("noiseAccumulation"),
            "noiseRadius": profile.get("noiseRadius"),
            "pollutionAccumulation": profile.get("pollutionAccumulation"),
            "pollutionRadius": profile.get("pollutionRadius"),
            "residentialNoiseExposure": noise_exposure,
            "residentialGroundPollutionExposure": pollution_exposure,
            "maximumResidentialNoiseExposure": maximum_noise_exposure,
            "maximumResidentialPollutionExposure": maximum_pollution_exposure,
        },
        "fuel": {
            "resourceType": profile.get("resourceType"),
            "resourceCapacity": profile.get("resourceCapacity"),
            "resourceConsumption": profile.get("resourceConsumption"),
            "roadReachability": reachability,
        },
        "cityElectricityCapacity": capacity,
        "placement": placement,
    }


def _roadside_power_candidate_cells(
    anchor: dict[str, Any],
    search_radius: float,
) -> list[tuple[float, dict[str, Any]]]:
    networks = _request(
        "GET",
        "/state/networks",
        query={"service": "Road", "limit": 5000},
    )
    candidates: list[tuple[float, dict[str, Any]]] = []
    seen: set[tuple[int, int, int]] = set()
    for segment in networks.get("segments", []):
        prefab = str(segment.get("prefab", ""))
        if "Highway" in prefab or segment.get("isUnderground", False):
            continue
        start = segment.get("start") or {}
        end = segment.get("end") or {}
        dx = float(end.get("x", 0)) - float(start.get("x", 0))
        dz = float(end.get("z", 0)) - float(start.get("z", 0))
        length = math.hypot(dx, dz)
        if length < 16:
            continue
        normal_x = -dz / length
        normal_z = dx / length
        for fraction in (0.2, 0.4, 0.6, 0.8):
            road_x = float(start.get("x", 0)) + dx * fraction
            road_z = float(start.get("z", 0)) + dz * fraction
            for offset in (32, 40, 48, 56, 64, 72, 80):
                for side in (-1, 1):
                    x = road_x + normal_x * offset * side
                    z = road_z + normal_z * offset * side
                    position = {"x": round(x, 3), "z": round(z, 3)}
                    distance = _point_distance(position, anchor)
                    if distance > search_radius:
                        continue
                    axis_x = -normal_x * side
                    axis_z = -normal_z * side
                    facing_angle = math.degrees(math.atan2(axis_x, axis_z)) % 360
                    key = (round(x), round(z), round(facing_angle))
                    if key in seen:
                        continue
                    seen.add(key)
                    candidates.append(
                        (
                            distance,
                            {
                                "position": position,
                                "buildable": True,
                                "water": {"present": False},
                                "slope": 0,
                                "_source": "roadside",
                                "_roadSegmentId": segment.get("id"),
                                # a lot's front is local +Z, (-sin a, cos a) at
                                # angle a since the Bridge lays lots the game's
                                # way round (live: a coal plant's road access
                                # point moved with it so): -facing_angle faces
                                # the road, its reverse is the fallback
                                "_angles": [
                                    round((-facing_angle) % 360, 3),
                                    round((180 - facing_angle) % 360, 3),
                                ],
                            },
                        )
                    )
    candidates.sort(key=lambda item: item[0])
    return candidates


def _power_plant_requires_road_candidates(
    arguments: dict[str, Any],
    terrain_candidates: list[tuple[float, dict[str, Any]]],
) -> tuple[bool, str]:
    if bool(arguments.get("requireRoadAccess", False)):
        return True, "callerRequired"
    if not terrain_candidates:
        return False, "noTerrainCandidateForNativeProbe"
    probe_cell = terrain_candidates[0][1]
    probe = _request(
        "POST",
        "/commands/preview-placement",
        body={
            "buildingPrefab": arguments["buildingPrefab"],
            "position": probe_cell["position"],
            "angleDegrees": 0,
            "requireRoadAccess": False,
            "maxRoadDistance": arguments.get("maxRoadDistance", 64),
        },
    )
    profile = probe.get("electricityProfile") or {}
    native_required = bool(probe.get("roadAccess", {}).get("requiredByAI", False))
    fuel_based = bool(profile.get("fuelBased", False))
    if native_required or fuel_based:
        return True, "nativeBuildingAI"
    return False, "notRequiredByNativeBuildingAI"


HOME_GRID = 64.0  # metres a cell of the homes' spatial index spans


def _residential_positions(homes_only: bool = True) -> dict[tuple[int, int], list[tuple[float, float]]]:
    """The homes standing (or, homes_only False, every growable and facility), indexed by HOME_GRID cell."""
    growables = next((value for value in _request("GET", "/state/growables", query={"limit": 50000}).values()
                      if isinstance(value, list)), [])
    if not homes_only:
        growables = growables + _request("GET", "/state/facilities", query={"limit": 50000}).get("facilities", [])
    index: dict[tuple[int, int], list[tuple[float, float]]] = {}
    for item in growables:
        position = item.get("position") or {}
        if (homes_only and item.get("service") != "Residential") or "x" not in position or "z" not in position:
            continue
        x, z = float(position["x"]), float(position["z"])
        index.setdefault((int(x // HOME_GRID), int(z // HOME_GRID)), []).append((x, z))
    return index


def _home_within(index: dict[tuple[int, int], list[tuple[float, float]]], position: dict[str, float],
                 radius: float) -> bool:
    x, z = float(position["x"]), float(position["z"])
    reach = int(radius // HOME_GRID) + 1
    cx, cz = int(x // HOME_GRID), int(z // HOME_GRID)
    return any(math.hypot(hx - x, hz - z) < radius
               for ix in range(cx - reach, cx + reach + 1) for iz in range(cz - reach, cz + reach + 1)
               for hx, hz in index.get((ix, iz), ()))


def _propose_power_plant_candidates(arguments: dict[str, Any]) -> dict[str, Any]:
    resolution = int(arguments.get("resolution", 32))
    anchor = arguments["serviceAreaAnchor"]
    search_radius = float(arguments.get("searchRadius", 640))
    all_cells, sampling = _sample_unlocked_map(resolution)
    avoid_areas = arguments.get("avoidAreas", [])
    candidates = []
    for cell in all_cells:
        position = cell.get("position") or {}
        if not cell.get("buildable", False):
            continue
        if cell.get("water", {}).get("present", False):
            continue
        distance = _point_distance(position, anchor)
        if distance > search_radius:
            continue
        candidates.append((distance, cell))
    candidates.sort(key=lambda item: (item[0], float(item[1].get("slope", 0))))
    road_candidates_required, road_requirement_source = (
        _power_plant_requires_road_candidates(arguments, candidates)
    )
    roadside_candidates: list[tuple[float, dict[str, Any]]] = []
    if road_candidates_required:
        roadside_candidates = _roadside_power_candidate_cells(anchor, search_radius)
        candidates = roadside_candidates + candidates

    maximum_previews = int(arguments.get("maxPreviewAttempts", 64))
    maximum_results = int(arguments.get("maxResults", 5))
    planned_use_buffer = float(arguments.get("plannedUseBuffer", 128))
    attempts = 0
    safe_candidates: list[dict[str, Any]] = []
    rejected_counts: dict[str, int] = {}
    # a spot whose plant would have a home inside its noise or pollution radius fails its preview
    # whatever the angle: once a preview gives the plant's radius, such spots are passed unpreviewed
    # (only while no home may be exposed, the default)
    homes_strict = (int(arguments.get("maximumResidentialNoiseExposure", 0)) == 0
                    and int(arguments.get("maximumResidentialPollutionExposure", 0)) == 0)
    homes: dict[tuple[int, int], list[tuple[float, float]]] | None = None
    reach = 0.0
    # a spot a standing building's centre lies inside the plant's lot (half its diagonal from the
    # spot) fails as an overlap whatever the angle (live: all 48 previews of a 1,920m turbine
    # search went to spots inside the town, the nearest first)
    buildings: dict[tuple[int, int], list[tuple[float, float]]] | None = None
    lot_reach = 0.0
    for distance, cell in candidates:
        position = {
            "x": round(float(cell["position"]["x"]), 3),
            "z": round(float(cell["position"]["z"]), 3),
        }
        if reach > 0 and homes_strict:
            if homes is None:
                homes = _residential_positions()
            if _home_within(homes, position, reach):
                rejected_counts["homesWithinReach"] = rejected_counts.get("homesWithinReach", 0) + 1
                continue
        if lot_reach > 0:
            if buildings is None:
                buildings = _residential_positions(homes_only=False)
            if _home_within(buildings, position, lot_reach):
                rejected_counts["buildingWithinLot"] = rejected_counts.get("buildingWithinLot", 0) + 1
                continue
        prefiltered = False
        for area in avoid_areas:
            if _point_distance(position, area["center"]) < (
                float(area.get("radius", 0)) + planned_use_buffer
            ):
                risk = "plannedUseExposure:" + str(area.get("name", "unnamed"))
                rejected_counts[risk] = rejected_counts.get(risk, 0) + 1
                prefiltered = True
                break
        if prefiltered:
            continue
        for angle in cell.get("_angles", (0, 90, 180, 270)):
            if attempts >= maximum_previews:
                break
            attempts += 1
            preview_arguments = {
                key: value
                for key, value in arguments.items()
                if key
                in {
                    "buildingPrefab",
                    "minimumVariableOutputRatio",
                    "maximumResidentialNoiseExposure",
                    "maximumResidentialPollutionExposure",
                    "maximumFuelRouteDistance",
                    "maximumFuelRouteTrafficDensity",
                    "maxRoadDistance",
                }
            }
            preview_arguments["requireRoadAccess"] = road_candidates_required
            preview_arguments.update(
                {
                    "position": position,
                    "angleDegrees": angle,
                }
            )
            try:
                preview = _preview_power_plant_candidate(preview_arguments)
            except BridgeRequestError as error:
                if "command was not run" not in str(error):
                    raise
                continue  # the game thread was too busy for this spot: the next one is tried
            candidate_risks = list(preview.get("risks", []))
            noise_radius = float(
                preview.get("environmental", {}).get("noiseRadius", 0) or 0
            )
            pollution_radius = float(
                preview.get("environmental", {}).get("pollutionRadius", 0) or 0
            )
            reach = max(reach, noise_radius, pollution_radius)
            footprint = (preview.get("placement") or {}).get("footprint") or {}
            if footprint.get("width") and footprint.get("length"):
                lot_reach = max(lot_reach, math.hypot(float(footprint["width"]), float(footprint["length"])) / 2)
            for area in avoid_areas:
                area_position = area["center"]
                clearance = float(area.get("radius", 0)) + max(
                    noise_radius, pollution_radius
                )
                if _point_distance(position, area_position) < clearance:
                    candidate_risks.append(
                        "plannedUseExposure:" + str(area.get("name", "unnamed"))
                    )
            if candidate_risks:
                for risk in set(candidate_risks):
                    rejected_counts[risk] = rejected_counts.get(risk, 0) + 1
                continue
            production = preview.get("production", {})
            placement = preview.get("placement", {})
            safe_candidates.append(
                {
                    "buildingPrefab": arguments["buildingPrefab"],
                    "position": position,
                    "angleDegrees": angle,
                    "distanceToServiceArea": round(distance, 3),
                    "terrain": placement.get("terrain"),
                    "roadAccess": placement.get("roadAccess"),
                    "production": production,
                    "environmental": preview.get("environmental"),
                    "fuel": preview.get("fuel"),
                    "warnings": preview.get("warnings", []),
                }
            )
            break
        if attempts >= maximum_previews:
            break

    safe_candidates.sort(
        key=lambda item: (
            -float(item.get("production", {}).get("estimated", 0) or 0),
            float(item.get("distanceToServiceArea", 0)),
        )
    )
    selected = safe_candidates[:maximum_results]
    return {
        "ok": True,
        "safe": bool(selected),
        "risks": [] if selected else ["noSafePowerPlantCandidateFound"],
        "sampling": sampling,
        "search": {
            "serviceAreaAnchor": anchor,
            "radius": search_radius,
            "buildableCandidateCount": len(candidates),
            "previewAttempts": attempts,
            "maxPreviewAttempts": maximum_previews,
            "plannedUseBuffer": planned_use_buffer,
            "roadsideCandidateCount": len(roadside_candidates),
            "roadCandidatesRequired": road_candidates_required,
            "roadRequirementSource": road_requirement_source,
        },
        "rejectedCounts": rejected_counts,
        "candidates": selected,
    }


def _inspect_power_plant_operation(arguments: dict[str, Any]) -> dict[str, Any]:
    building_id = int(arguments["buildingId"])
    facilities = _request(
        "GET",
        "/state/facilities",
        query={"service": "Electricity", "limit": 1000},
    )
    facility = next(
        (
            item
            for item in facilities.get("facilities", [])
            if int(item.get("id", 0)) == building_id
        ),
        None,
    )
    if facility is None:
        return {
            "ok": True,
            "safe": False,
            "risks": ["powerPlantNotFound"],
            "buildingId": building_id,
        }

    preview_arguments = dict(arguments)
    preview_arguments.update(
        {
            "buildingPrefab": facility["prefab"],
            "buildingId": building_id,
            "position": {
                "x": facility["position"]["x"],
                "z": facility["position"]["z"],
            },
            "angleDegrees": facility.get("angleDegrees", 0),
        }
    )
    preview = _preview_power_plant_candidate(preview_arguments)
    risks = list(preview.get("risks", []))
    profile = preview.get("placement", {}).get("electricityProfile") or {}
    fuel_based = bool(profile.get("fuelBased", False))
    resource_capacity = int(profile.get("resourceCapacity", 0) or 0)
    resource_buffer = int(facility.get("customBuffer1", 0) or 0)
    fuel_ratio = (
        float(resource_buffer) / resource_capacity if resource_capacity > 0 else 1.0
    )
    minimum_fuel_ratio = float(arguments.get("minimumFuelBufferRatio", 0.15))
    deliveries = None
    if fuel_based:
        deliveries = _request(
            "GET",
            "/state/building-deliveries",
            query={"buildingId": building_id, "limit": 256},
        )
    if fuel_based and fuel_ratio < minimum_fuel_ratio:
        risks.append("fuelBufferBelowMinimum")
        if int((deliveries or {}).get("inbound", 0) or 0) == 0:
            risks.append("lowFuelWithoutInboundDelivery")
        elif int((deliveries or {}).get("withPath", 0) or 0) == 0:
            risks.append("fuelDeliveryHasNoPath")
    if int(facility.get("productionRate", 0) or 0) <= 0:
        risks.append("powerPlantProductionStopped")
    if str(facility.get("problems", "")).strip():
        risks.append("powerPlantReportsProblems")
    electricity_coverage = _request(
        "GET",
        "/state/electricity-service-coverage",
        query={"limit": 500, "includeCovered": False, "includeNoDemand": False},
    )
    if int(
        electricity_coverage.get("summary", {}).get("missingElectricity", 0) or 0
    ) > 0:
        risks.append("demandingBuildingsStillMissingElectricity")

    return {
        "ok": True,
        "safe": len(risks) == 0,
        "risks": sorted(set(risks)),
        "buildingId": building_id,
        "facility": facility,
        "fuelBuffer": {
            "fuelBased": fuel_based,
            "amount": resource_buffer,
            "capacity": resource_capacity,
            "ratio": round(fuel_ratio, 4),
            "minimumRatio": minimum_fuel_ratio,
        },
        "deliveries": deliveries,
        "electricityServiceCoverage": electricity_coverage,
        "candidateRecheck": preview,
    }


def _inspect_water_network_topology(arguments: dict[str, Any]) -> dict[str, Any]:
    intake_id = int(arguments["intakeBuildingId"])
    outlet_id = int(arguments["outletBuildingId"])
    facilities = _request("GET", "/state/facilities", query={"service": "Water", "limit": 1000})
    if int(facilities.get("returned", 0)) < int(facilities.get("total", 0)):
        return {
            "ok": True,
            "safe": False,
            "risks": ["waterFacilityInventoryTruncated"],
        }
    by_id = {int(item["id"]): item for item in facilities.get("facilities", [])}
    intake = by_id.get(intake_id)
    outlet = by_id.get(outlet_id)
    risks: list[str] = []
    if intake is None:
        risks.append("intakeBuildingNotFound")
    if outlet is None:
        risks.append("outletBuildingNotFound")
    if risks:
        return {"ok": True, "safe": False, "risks": risks}

    intake_node = int(intake.get("netNodeId", 0))
    outlet_node = int(outlet.get("netNodeId", 0))
    if intake_node == 0:
        risks.append("intakeNetNodeMissing")
    if outlet_node == 0:
        risks.append("outletNetNodeMissing")
    if risks:
        return {
            "ok": True,
            "safe": False,
            "risks": risks,
            "intake": intake,
            "outlet": outlet,
        }

    networks = _request("GET", "/state/networks", query={"service": "Water", "limit": 5000})
    if int(networks.get("returned", 0)) < int(networks.get("total", 0)):
        return {
            "ok": True,
            "safe": False,
            "risks": ["waterNetworkInventoryTruncated"],
            "intake": intake,
            "outlet": outlet,
        }
    segments = networks.get("segments", [])
    adjacency: dict[int, list[tuple[int, int]]] = {}
    for segment in segments:
        start = int(segment.get("startNodeId", 0))
        end = int(segment.get("endNodeId", 0))
        segment_id = int(segment.get("id", 0))
        if start == 0 or end == 0 or segment_id == 0:
            continue
        adjacency.setdefault(start, []).append((end, segment_id))
        adjacency.setdefault(end, []).append((start, segment_id))

    queue = [intake_node]
    previous: dict[int, tuple[int, int] | None] = {intake_node: None}
    while queue and outlet_node not in previous:
        node = queue.pop(0)
        for neighbor, segment_id in adjacency.get(node, []):
            if neighbor in previous:
                continue
            previous[neighbor] = (node, segment_id)
            queue.append(neighbor)

    path_nodes: list[int] = []
    path_segments: list[int] = []
    if outlet_node in previous:
        cursor = outlet_node
        path_nodes.append(cursor)
        while previous[cursor] is not None:
            prior, segment_id = previous[cursor]  # type: ignore[misc]
            path_segments.append(segment_id)
            cursor = prior
            path_nodes.append(cursor)
        path_nodes.reverse()
        path_segments.reverse()
    else:
        risks.append("facilitiesNotConnected")

    direct_segment = len(path_segments) == 1
    if direct_segment:
        risks.append("directFacilityToFacilityPipe")
    if path_segments and len(path_segments) < int(arguments.get("minPathSegments", 2)):
        risks.append("missingIntermediateTrunkNode")

    component_nodes = {intake_node}
    component_segments: set[int] = set()
    queue = [intake_node]
    while queue:
        node = queue.pop(0)
        for neighbor, segment_id in adjacency.get(node, []):
            component_segments.add(segment_id)
            if neighbor not in component_nodes:
                component_nodes.add(neighbor)
                queue.append(neighbor)
    min_component_segments = int(arguments.get("minComponentSegments", 3))
    if len(component_segments) < min_component_segments:
        risks.append("waterComponentLacksSharedTrunk")
    path_node_set = set(path_nodes)
    trunk_branches = []
    for node in path_nodes[1:-1]:
        off_path_segments = [
            segment_id
            for neighbor, segment_id in adjacency.get(node, [])
            if neighbor not in path_node_set
        ]
        if off_path_segments:
            trunk_branches.append(
                {"nodeId": node, "offPathSegmentIds": sorted(set(off_path_segments))}
            )
    if not trunk_branches:
        risks.append("waterComponentLacksServicedAreaBranch")

    return {
        "ok": True,
        "safe": len(risks) == 0,
        "risks": risks,
        "intakeBuildingId": intake_id,
        "outletBuildingId": outlet_id,
        "intakeNetNodeId": intake_node,
        "outletNetNodeId": outlet_node,
        "path": {
            "nodeIds": path_nodes,
            "segmentIds": path_segments,
            "hasIntermediateNode": len(path_nodes) >= 3,
            "directFacilityConnection": direct_segment,
        },
        "component": {
            "nodeCount": len(component_nodes),
            "segmentCount": len(component_segments),
            "minRequiredSegments": min_component_segments,
            "servicedAreaBranches": trunk_branches,
        },
    }


def _water_service_branch_plan_id(arguments: dict[str, Any]) -> str:
    canonical = {
        "intakeBuildingId": int(arguments["intakeBuildingId"]),
        "outletBuildingId": int(arguments["outletBuildingId"]),
        "targetBuildingIds": [int(value) for value in arguments["targetBuildingIds"]],
        "sourceNodeId": int(arguments["sourceNodeId"]),
        "pipePrefab": str(arguments["pipePrefab"]),
        "segments": arguments["segments"],
    }
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "water-service-" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _read_complete_water_network() -> dict[str, Any]:
    payload = _request("GET", "/state/networks", query={"service": "Water", "limit": 5000})
    returned = int(payload.get("returned", len(payload.get("segments", []))))
    total = int(payload.get("total", returned))
    if returned < total:
        raise RuntimeError(
            f"Water network inventory is truncated ({returned}/{total}); "
            "refusing to plan or build from incomplete topology."
        )
    return payload


def _water_component_node_positions(
    segments: list[dict[str, Any]], start_node_id: int
) -> dict[int, dict[str, float]]:
    adjacency: dict[int, set[int]] = {}
    positions: dict[int, dict[str, float]] = {}
    for segment in segments:
        start_id = int(segment.get("startNodeId", 0) or 0)
        end_id = int(segment.get("endNodeId", 0) or 0)
        if start_id <= 0 or end_id <= 0:
            continue
        adjacency.setdefault(start_id, set()).add(end_id)
        adjacency.setdefault(end_id, set()).add(start_id)
        positions[start_id] = {
            "x": float((segment.get("start") or {}).get("x", 0)),
            "z": float((segment.get("start") or {}).get("z", 0)),
        }
        positions[end_id] = {
            "x": float((segment.get("end") or {}).get("x", 0)),
            "z": float((segment.get("end") or {}).get("z", 0)),
        }
    if start_node_id not in positions:
        return {}
    reached = {start_node_id}
    queue = [start_node_id]
    while queue:
        node_id = queue.pop(0)
        for neighbor in adjacency.get(node_id, set()):
            if neighbor not in reached:
                reached.add(neighbor)
                queue.append(neighbor)
    return {node_id: positions[node_id] for node_id in sorted(reached)}


def _read_water_target_buildings(building_ids: list[int]) -> dict[int, dict[str, Any]]:
    payload = _request(
        "GET",
        "/state/water-service-coverage",
        query={"limit": 50000, "includeCovered": True, "includeNoDemand": False},
    )
    requiring = int((payload.get("summary") or {}).get("requiringWater", 0))
    returned = int(payload.get("returned", len(payload.get("buildings", []))))
    if returned < requiring:
        raise RuntimeError(
            f"Water coverage inventory is truncated ({returned}/{requiring}); "
            "refusing to plan or build without exact target state."
        )
    wanted = set(building_ids)
    return {
        int(building["id"]): building
        for building in payload.get("buildings", [])
        if int(building.get("id", 0)) in wanted
    }


def _split_water_service_leg(
    start: dict[str, Any],
    end: dict[str, Any],
    maximum_length: float,
    target_building_id: int,
) -> list[dict[str, Any]]:
    distance = _point_distance(start, end)
    if distance <= 1e-6:
        return []
    split = _split_straight_pipe(start, end, maximum_length)
    return [
        {
            "start": piece_start,
            "end": piece_end,
            "targetBuildingId": target_building_id if index == len(split) - 1 else 0,
            "name": "Water Service Branch",
        }
        for index, (piece_start, piece_end) in enumerate(split)
    ]


def _generate_water_service_branch_segments(
    source: dict[str, Any],
    targets: dict[int, dict[str, Any]],
    maximum_length: float,
) -> list[dict[str, Any]]:
    current = {"x": float(source["x"]), "z": float(source["z"])}
    remaining = dict(targets)
    segments: list[dict[str, Any]] = []
    while remaining:
        target_id, target = min(
            remaining.items(),
            key=lambda item: (_point_distance(current, item[1]["position"]), item[0]),
        )
        segments.extend(
            _split_water_service_leg(
                current, target["position"], maximum_length, target_id
            )
        )
        current = {
            "x": float(target["position"]["x"]),
            "z": float(target["position"]["z"]),
        }
        del remaining[target_id]
    return segments


def _bend_first_service_joint(
    segments: list[dict[str, Any]], degrees: float, maximum_length: float
) -> list[dict[str, Any]] | None:
    """Bend the branch's first joint when it would extend a dead-end pipe in line.

    A collinear first piece from a dead-end source invokes native MoveMiddleNode;
    rejecting the closest source then made replanning start from farther trunk
    nodes and leave stranded stubs.
    """
    if not segments:
        return None
    bent = [dict(segment) for segment in segments]
    start, end = bent[0]["start"], bent[0]["end"]
    dx, dz = float(end["x"]) - float(start["x"]), float(end["z"]) - float(start["z"])
    radians = math.radians(degrees)
    # Never move a target position; insert a dogleg instead.
    if len(bent) >= 2 and not bent[0].get("targetBuildingId"):
        moved = {"x": round(float(start["x"]) + dx * math.cos(radians) - dz * math.sin(radians), 3),
                 "z": round(float(start["z"]) + dx * math.sin(radians) + dz * math.cos(radians), 3)}
        bent[0] = {**bent[0], "end": moved}
        bent[1] = {**bent[1], "start": moved}
    else:
        side = math.tan(radians) * 0.5
        middle = {"x": round(float(start["x"]) + dx * 0.5 - dz * side, 3),
                  "z": round(float(start["z"]) + dz * 0.5 + dx * side, 3)}
        bent = [{**bent[0], "end": middle, "targetBuildingId": 0}, {**bent[0], "start": middle}] + bent[1:]
    if any(_point_distance(item["start"], item["end"]) > maximum_length for item in bent):
        return None
    return bent


def _preview_service_branch(
    segments: list[dict[str, Any]],
    prefab: str,
    source_node_id: int,
    rejected_candidates: list[dict[str, Any]],
    remaining_previews: int,
) -> tuple[list[dict[str, Any]], bool, bool]:
    """Preview one branch variant; also report a collinear first-joint rejection."""
    segment_previews: list[dict[str, Any]] = []
    for index, segment in enumerate(segments):
        if len(segment_previews) >= remaining_previews:
            rejected_candidates.append(
                {"sourceNodeId": source_node_id, "reason": "previewBudgetExhausted"}
            )
            return segment_previews, False, False
        preview = _request(
            "POST",
            "/commands/preview-network",
            body={
                "roadPrefab": prefab,
                "start": segment["start"],
                "end": segment["end"],
                "name": segment["name"],
            },
        )
        segment_previews.append({"index": index, "preview": preview})
        if not (preview.get("ok", False) and preview.get("valid", False)):
            rejected_candidates.append(
                {
                    "sourceNodeId": source_node_id,
                    "segmentIndex": index,
                    "reason": str(preview.get("toolErrors", "nativePreviewRejected")),
                }
            )
            return segment_previews, False, index == 0 and preview.get("nativeMoveMiddleNodeRisk") is True
    return segment_previews, True, False


def _propose_water_service_connection(arguments: dict[str, Any]) -> dict[str, Any]:
    target_ids = list(dict.fromkeys(int(value) for value in arguments["targetBuildingIds"]))
    topology = _inspect_water_network_topology(arguments)
    if not topology.get("safe", False):
        return {
            "ok": True,
            "safe": False,
            "planningOnly": True,
            "risks": ["existingWaterComponentUnsafe"],
            "topology": topology,
        }
    targets = _read_water_target_buildings(target_ids)
    missing_ids = [building_id for building_id in target_ids if building_id not in targets]
    if missing_ids:
        return {
            "ok": True,
            "safe": False,
            "planningOnly": True,
            "risks": ["targetWaterDemandBuildingNotFound"],
            "missingTargetBuildingIds": missing_ids,
            "topology": topology,
        }
    uncovered = {
        building_id: building
        for building_id, building in targets.items()
        if not bool(building.get("hasWater", False))
        or not bool(building.get("hasSewage", False))
    }
    if not uncovered:
        return {
            "ok": True,
            "safe": True,
            "planningOnly": True,
            "actionRequired": False,
            "reason": "allTargetBuildingsAlreadyHaveWaterAndSewage",
            "targetBuildings": list(targets.values()),
            "topology": topology,
            "segments": [],
        }
    network = _read_complete_water_network()
    component_nodes = _water_component_node_positions(
        network.get("segments", []), int(topology["intakeNetNodeId"])
    )
    if not component_nodes:
        return {
            "ok": True,
            "safe": False,
            "planningOnly": True,
            "risks": ["verifiedWaterComponentHasNoUsableNodes"],
            "topology": topology,
        }
    maximum_length = float(arguments.get("maxWaterPipeSegmentLength", 160))
    prefab = str(arguments.get("pipePrefab", "Water Pipe"))
    if not _is_water_network_prefab(prefab):
        return {
            "ok": True,
            "safe": False,
            "planningOnly": True,
            "risks": ["pipePrefabIsNotWaterNetwork"],
            "topology": topology,
        }
    preview_limit = int(arguments.get("maxPreviewAttempts", 96))
    source_candidates = sorted(
        component_nodes.items(),
        key=lambda item: (
            min(_point_distance(item[1], target["position"]) for target in uncovered.values()),
            item[0],
        ),
    )
    preview_attempts = 0
    rejected_candidates = []
    for source_node_id, source_position in source_candidates:
        generated = _generate_water_service_branch_segments(
            source_position, uncovered, maximum_length
        )
        if not generated or len(generated) > 64:
            rejected_candidates.append(
                {"sourceNodeId": source_node_id, "reason": "invalidSegmentCount"}
            )
            continue
        variants = [generated] + [
            variant for variant in (
                _bend_first_service_joint(generated, angle, maximum_length) for angle in (6.0, -6.0)
            ) if variant is not None
        ]
        candidate_valid = False
        for segments in variants:
            segment_previews, candidate_valid, collinear_first = _preview_service_branch(
                segments, prefab, source_node_id, rejected_candidates, preview_limit - preview_attempts)
            preview_attempts += len(segment_previews)
            if candidate_valid or not collinear_first:
                break
        if not candidate_valid:
            continue
        plan_arguments = {
            "intakeBuildingId": int(arguments["intakeBuildingId"]),
            "outletBuildingId": int(arguments["outletBuildingId"]),
            "targetBuildingIds": target_ids,
            "sourceNodeId": source_node_id,
            "pipePrefab": prefab,
            "segments": segments,
        }
        return {
            "ok": True,
            "safe": True,
            "planningOnly": True,
            "actionRequired": True,
            "planId": _water_service_branch_plan_id(plan_arguments),
            "topology": topology,
            "sourceNode": {"id": source_node_id, "position": source_position},
            "targetBuildings": list(targets.values()),
            "segments": segments,
            "segmentPreviews": segment_previews,
            "previewAttempts": preview_attempts,
            "rejectedCandidates": rejected_candidates,
            "verification": [
                "createCheckpointBeforeEachSegment",
                "buildInReturnedOrder",
                "replanAfterEachAcceptedSegment",
                "settleSimulation",
                "inspectWaterServiceCoverageForEveryTarget",
                "verifyRegression",
            ],
        }
    return {
        "ok": True,
        "safe": False,
        "planningOnly": True,
        "risks": ["noNativeValidWaterServiceBranchFound"],
        "topology": topology,
        "targetBuildings": list(targets.values()),
        "previewAttempts": preview_attempts,
        "rejectedCandidates": rejected_candidates,
    }


def _inspect_water_service_branch_plan(
    arguments: dict[str, Any], completed_segment_indexes: set[int] | None = None
) -> dict[str, Any]:
    completed_segment_indexes = completed_segment_indexes or set()
    topology = _inspect_water_network_topology(arguments)
    risks = [] if topology.get("safe", False) else ["existingWaterComponentUnsafe"]
    target_ids = list(dict.fromkeys(int(value) for value in arguments["targetBuildingIds"]))
    targets = _read_water_target_buildings(target_ids)
    missing_ids = [building_id for building_id in target_ids if building_id not in targets]
    if missing_ids:
        risks.append("targetWaterDemandBuildingNotFound")
    uncovered_ids = [
        building_id
        for building_id in target_ids
        if building_id in targets
        and (
            not bool(targets[building_id].get("hasWater", False))
            or not bool(targets[building_id].get("hasSewage", False))
        )
    ]
    network = _read_complete_water_network()
    intake_node_id = int(topology.get("intakeNetNodeId", 0) or 0)
    component_nodes = _water_component_node_positions(
        network.get("segments", []), intake_node_id
    )
    source_node_id = int(arguments["sourceNodeId"])
    if source_node_id not in component_nodes:
        risks.append("sourceNodeNotInVerifiedWaterComponent")
    segments = arguments["segments"]
    tolerance = float(arguments.get("nodeTolerance", 2))
    maximum_length = float(arguments.get("maxWaterPipeSegmentLength", 160))
    if len(segments) < 1 or len(segments) > 64:
        risks.append("waterServiceBranchSegmentCountUnsafe")
    if segments and source_node_id in component_nodes:
        if _point_distance(segments[0]["start"], component_nodes[source_node_id]) > tolerance:
            risks.append("waterServiceBranchDoesNotStartAtSourceNode")
    for index, segment in enumerate(segments):
        length = _point_distance(segment["start"], segment["end"])
        if length <= 1e-6:
            risks.append("waterServiceBranchContainsZeroLengthSegment")
        if length > maximum_length + 1e-6:
            risks.append("waterServiceBranchSegmentTooLong")
        if index > 0 and _point_distance(
            segments[index - 1]["end"], segment["start"]
        ) > tolerance:
            risks.append("waterServiceBranchIsDiscontinuous")
    for target_id in target_ids:
        target = targets.get(target_id)
        if target is None:
            continue
        if not any(
            _point_distance(segment["end"], target["position"]) <= tolerance
            for segment in segments
        ):
            risks.append("waterServiceBranchDoesNotVisitEveryTarget")
            break
    expected_plan_id = _water_service_branch_plan_id(arguments)
    if str(arguments.get("planId", "")) != expected_plan_id:
        risks.append("waterServiceBranchPlanIdMismatch")
    prefab = str(arguments["pipePrefab"])
    if not _is_water_network_prefab(prefab):
        risks.append("pipePrefabIsNotWaterNetwork")
    live_segments = network.get("segments", [])
    previews = []
    for index, segment in enumerate(segments):
        if index in completed_segment_indexes:
            existing = _matching_existing_water_segment(
                live_segments, segment, prefab, tolerance=tolerance
            )
            if existing is None:
                risks.append("completedServiceBranchSegmentMissing:" + str(index))
            previews.append(
                {
                    "index": index,
                    "valid": existing is not None,
                    "alreadyBuilt": existing is not None,
                    "segmentId": None if existing is None else existing.get("id"),
                }
            )
            continue
        preview = _request(
            "POST",
            "/commands/preview-network",
            body={
                "roadPrefab": prefab,
                "start": segment["start"],
                "end": segment["end"],
                "name": segment.get("name", ""),
            },
        )
        valid = bool(preview.get("ok", False)) and bool(preview.get("valid", False))
        if not valid:
            risks.append("serviceBranchSegmentPreviewFailed:" + str(index))
        previews.append(
            {
                "index": index,
                "valid": valid,
                "alreadyBuilt": False,
                "toolErrors": preview.get("toolErrors"),
                "constructionCost": preview.get("constructionCost"),
                "length": preview.get("length"),
            }
        )
    next_index = len(completed_segment_indexes)
    if next_index < len(segments) and component_nodes:
        current_start = segments[next_index]["start"]
        if min(
            (_point_distance(current_start, position) for position in component_nodes.values()),
            default=float("inf"),
        ) > tolerance:
            risks.append("nextServiceBranchSegmentNotConnectedToLiveComponent")
    return {
        "ok": True,
        "safe": not risks,
        "actionRequired": bool(uncovered_ids),
        "planId": expected_plan_id,
        "risks": sorted(set(risks)),
        "topology": topology,
        "sourceNodeId": source_node_id,
        "targetBuildings": list(targets.values()),
        "uncoveredTargetBuildingIds": uncovered_ids,
        "segments": segments,
        "segmentPreviews": previews,
    }


def _snapshot_city_identity(snapshot: dict[str, Any]) -> dict[str, str]:
    mayor_snapshot = snapshot.get("mayorSnapshot", {})
    return _city_identity(mayor_snapshot)


def _live_city_identity_or_none() -> dict[str, str] | None:
    try:
        snapshot = _request("GET", "/state/mayor-snapshot", query={"detailLimit": 0})
    except (BridgeRequestError, RuntimeError):
        return None
    return _city_identity(snapshot)


def _capture_live_spatial_snapshot() -> Path:
    artifacts = ROOT / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    path = (artifacts / f"city-spatial-snapshot-live-{stamp}-{os.getpid()}.json").resolve()
    script = ROOT / "scripts" / "capture-offline-spatial-snapshot.ps1"
    completed = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-BaseUrl",
            BASE_URL,
            "-OutputPath",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        # Windows PowerShell writes the console code page; a non-ASCII
        # repository path must not crash the reader and hide the real error.
        errors="replace",
        timeout=180,
    )
    if completed.returncode != 0 or not path.exists():
        detail = (completed.stderr or "").strip() or (completed.stdout or "").strip() or "unknown capture failure"
        raise RuntimeError(f"Could not capture the current city spatial snapshot: {detail}")
    return path


def _require_snapshot_city_match(
    snapshot: dict[str, Any],
    path: Path,
    live_identity: dict[str, str],
) -> None:
    snapshot_identity = _snapshot_city_identity(snapshot)
    if snapshot_identity["instanceId"] != live_identity["instanceId"]:
        raise RuntimeError(
            "Offline spatial snapshot belongs to a different city instance; "
            f"snapshot={snapshot_identity['name']} ({snapshot_identity['instanceId']}), "
            f"live={live_identity['name']} ({live_identity['instanceId']}), path={path}. "
            "Capture a fresh snapshot after loading the current city."
        )


def _offline_snapshot(arguments: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    requested = str(arguments.get("snapshotPath", "")).strip()
    live_identity = _live_city_identity_or_none()
    if requested:
        path = Path(requested).resolve()
    elif live_identity is not None:
        path = _capture_live_spatial_snapshot()
    else:
        path = latest_snapshot(ROOT)

    snapshot = load_snapshot(path)
    if live_identity is not None:
        _require_snapshot_city_match(snapshot, path, live_identity)
    return path, snapshot


def _curve_accuracy(roads: list[dict[str, Any]]) -> str:
    native_count = sum(road.get("curveSource") == "nativeGenerateBezier" for road in roads)
    if native_count == len(roads):
        return "nativeGenerateBezier"
    if native_count:
        return "mixedNativeBezierAndQuadraticFallback"
    return "quadraticThroughReportedStartMiddleEnd"


def _inspect_road_layers_3d(arguments: dict[str, Any]) -> dict[str, Any]:
    path, snapshot = _offline_snapshot(arguments)
    segment_ids = arguments.get("segmentIds")
    roads = build_layered_roads(
        snapshot,
        segment_ids=segment_ids,
        sample_count=int(arguments.get("sampleCount", 9)),
    )
    limit = max(0, min(5000, int(arguments.get("limit", 500))))
    counts: dict[str, int] = {}
    for road in roads:
        counts[road["structureType"]] = counts.get(road["structureType"], 0) + 1
    return {
        "ok": True,
        "offline": True,
        "snapshotPath": str(path),
        "capturedAtUtc": snapshot.get("capturedAtUtc"),
        "curveAccuracy": _curve_accuracy(roads),
        "total": len(roads),
        "returned": min(limit, len(roads)),
        "countsByStructure": counts,
        "roads": roads[:limit],
    }


def _inspect_grade_separated_crossings(arguments: dict[str, Any]) -> dict[str, Any]:
    path, snapshot = _offline_snapshot(arguments)
    roads = build_layered_roads(
        snapshot,
        segment_ids=arguments.get("segmentIds"),
        sample_count=int(arguments.get("sampleCount", 9)),
    )
    threshold = float(arguments.get("gradeSeparationThreshold", 4))
    crossings = inspect_crossings(roads, grade_separation_threshold=threshold)
    relation_filter = str(arguments.get("relation", "")).strip()
    if relation_filter:
        crossings = [item for item in crossings if item["relation"] == relation_filter]
    limit = max(0, min(5000, int(arguments.get("limit", 500))))
    counts: dict[str, int] = {}
    for crossing in crossings:
        counts[crossing["relation"]] = counts.get(crossing["relation"], 0) + 1
    return {
        "ok": True,
        "offline": True,
        "snapshotPath": str(path),
        "capturedAtUtc": snapshot.get("capturedAtUtc"),
        "curveAccuracy": _curve_accuracy(roads),
        "gradeSeparationThreshold": threshold,
        "total": len(crossings),
        "returned": min(limit, len(crossings)),
        "countsByRelation": counts,
        "crossings": crossings[:limit],
    }


def _inspect_building_layers_3d(arguments: dict[str, Any]) -> dict[str, Any]:
    path, snapshot = _offline_snapshot(arguments)
    buildings = build_layered_buildings(
        snapshot,
        building_ids=arguments.get("buildingIds"),
        service=str(arguments.get("service", "")),
        include_original=bool(arguments.get("includeOriginal", True)),
        include_road_structures=bool(arguments.get("includeRoadStructures", True)),
        include_network_helpers=bool(arguments.get("includeNetworkHelpers", True)),
    )
    limit = max(0, min(5000, int(arguments.get("limit", 500))))
    known = sum(building["heightKnown"] for building in buildings)
    counts: dict[str, int] = {}
    for building in buildings:
        service = building["service"]
        counts[service] = counts.get(service, 0) + 1
    return {
        "ok": True,
        "offline": True,
        "snapshotPath": str(path),
        "capturedAtUtc": snapshot.get("capturedAtUtc"),
        "total": len(buildings),
        "returned": min(limit, len(buildings)),
        "nativeHeightCount": known,
        "legacyFootprintOnlyCount": len(buildings) - known,
        "countsByService": counts,
        "buildings": buildings[:limit],
    }


def _inspect_building_road_clearance_3d(arguments: dict[str, Any]) -> dict[str, Any]:
    path, snapshot = _offline_snapshot(arguments)
    buildings = build_layered_buildings(
        snapshot,
        building_ids=arguments.get("buildingIds"),
        service=str(arguments.get("service", "")),
        include_original=bool(arguments.get("includeOriginal", False)),
        include_road_structures=bool(arguments.get("includeRoadStructures", False)),
        include_network_helpers=bool(arguments.get("includeNetworkHelpers", False)),
    )
    roads = build_layered_roads(
        snapshot,
        segment_ids=arguments.get("segmentIds"),
        sample_count=int(arguments.get("sampleCount", 17)),
    )
    road_half_height = float(arguments.get("roadHalfHeight", 2))
    results = inspect_building_road_clearance(buildings, roads, road_half_height=road_half_height)
    relation_filter = str(arguments.get("relation", "")).strip()
    if relation_filter:
        results = [item for item in results if item["relation"] == relation_filter]
    limit = max(0, min(5000, int(arguments.get("limit", 500))))
    counts: dict[str, int] = {}
    for result in results:
        counts[result["relation"]] = counts.get(result["relation"], 0) + 1
    return {
        "ok": True,
        "offline": True,
        "snapshotPath": str(path),
        "capturedAtUtc": snapshot.get("capturedAtUtc"),
        "roadHalfHeight": road_half_height,
        "total": len(results),
        "returned": min(limit, len(results)),
        "countsByRelation": counts,
        "results": results[:limit],
    }


def _inspect_planning_area(arguments: dict[str, Any]) -> dict[str, Any]:
    path, snapshot = _offline_snapshot(arguments)
    context = inspect_planning_area(
        snapshot,
        center=arguments["center"],
        radius=float(arguments.get("radius", 256)),
        intended_use=str(arguments.get("intendedUse", "Mixed")),
        sample_count=int(arguments.get("sampleCount", 9)),
        max_features=int(arguments.get("maxFeatures", 20)),
        candidate_limit=int(arguments.get("candidateLimit", 12)),
    )
    _attach_exact_frontage_evidence(context, arguments, snapshot)
    return {
        "ok": True,
        "offline": True,
        "snapshotPath": str(path),
        "capturedAtUtc": snapshot.get("capturedAtUtc"),
        "context": context,
    }


def _attach_exact_frontage_evidence(
    context: dict[str, Any], arguments: dict[str, Any], snapshot: dict[str, Any]
) -> None:
    """Attach authoritative zone-block evidence only for a current live capture."""
    if str(arguments.get("snapshotPath", "")).strip():
        context["actionableZoneBlocks"] = {
            "evidenceStatus": "unavailableForExplicitOfflineSnapshot",
            "totalMatching": None,
            "returned": 0,
            "blocks": [],
        }
        return
    if _live_city_identity_or_none() is None:
        context["actionableZoneBlocks"] = {
            "evidenceStatus": "unavailableWithoutLiveBridge",
            "totalMatching": None,
            "returned": 0,
            "blocks": [],
        }
        return
    blocks = _request(
        "GET",
        "/state/zone-blocks",
        query={
            "limit": 200,
            "onlyFullyUnzoned": "true",
            "requireClearOccupancy": "true",
            "requireDevelopmentFrontage": "true",
        },
    )
    context["actionableZoneBlocks"] = {
        **blocks,
        "blocks": annotate_actionable_zone_block_clearance(
            blocks.get("blocks", []), snapshot
        ),
        "evidenceStatus": "liveExact",
    }


def _propose_frontage_road(arguments: dict[str, Any]) -> dict[str, Any]:
    from frontage_road import dead_end_anchors, plan_frontage_extensions
    from road_corridor import unlocked_bounds

    identity = _current_city_identity()
    failures = _road_failures(identity)
    if any(not recovery_verified(entry) for entry in failures["failures"]):
        return {"ok": True, "safe": False, "planningOnly": True, "mutationAuthorized": False,
                "risks": ["unverifiedRoadRecovery"], "city": identity}
    intended_zone = str(arguments.get("intendedZone", "Residential"))
    roads = _request("GET", "/state/networks", query={"limit": 5000, "service": "Road"})
    if int(roads.get("returned", 0)) < int(roads.get("total", 0)):
        return {"ok": True, "safe": False, "planningOnly": True, "mutationAuthorized": False,
                "risks": ["roadInventoryTruncated"], "city": identity}
    growables = _request("GET", "/state/growables", query={"limit": 50000})
    buildings = next((value for value in growables.values() if isinstance(value, list)), [])
    conflicting_service = "Industrial" if intended_zone == "Residential" else (
        "Residential" if intended_zone == "Industrial" else "")
    conflicting = [
        (float(item["position"]["x"]), float(item["position"]["z"]))
        for item in buildings
        if conflicting_service and item.get("service") == conflicting_service
    ]
    center = arguments.get("center")
    anchors = dead_end_anchors(
        roads["segments"],
        center=(float(center["x"]), float(center["z"])) if center else None,
        radius=float(arguments.get("radius", 512)) if center else None,
    )
    result = plan_frontage_extensions(
        anchors=anchors,
        segments=roads["segments"],
        bounds=unlocked_bounds(_request("GET", "/state/map-areas")),
        preview=lambda candidate: _request("POST", "/commands/preview-network", body=candidate),
        accepted=lambda preview: _road_preview_survives_settlement(preview, 0.7),
        intended_zone=intended_zone,
        conflicting_points=conflicting,
        rejected=lambda candidate: is_rejected(candidate, failures),
        road_prefab=arguments.get("roadPrefab") or None,
        maximum_previews=int(arguments.get("maxPreviews", 24)),
        limit=int(arguments.get("limit", 5)),
    )
    return {**result, "safe": bool(result["candidates"]), "city": identity,
            "conflictingBuildingsConsidered": len(conflicting)}


def _purchase_map_area(tile_index: int, reserve_display: float) -> dict[str, Any]:
    """Buy one tile the game currently offers, at its live price, keeping a cash reserve.

    /commands/unlock-map-areas stays unexposed because it unlocks without normal
    progression; this path charges the native land price and leaves maxAreaCount alone.
    """
    before = _request("GET", "/state/map-areas")
    tile = next((area for area in before.get("areas", []) if area.get("index") == tile_index), None)
    if tile is None or not tile.get("canUnlock"):
        raise RuntimeError(f"Map tile {tile_index} is not purchasable now")
    cash = _request("GET", "/state/city-health")["cash"]["amount"]
    price_display = float(tile["price"]) / MONEY_INTERNAL_SCALE
    remaining_display = float(cash) / MONEY_INTERNAL_SCALE - price_display
    if remaining_display < reserve_display:
        raise RuntimeError(
            f"Buying tile {tile_index} for {price_display:.0f} would leave {remaining_display:.0f}, "
            f"below the {reserve_display:.0f} reserve"
        )
    result = _request("POST", "/commands/purchase-map-area", body={"tileIndex": tile_index})
    after = _request("GET", "/state/map-areas")
    owned = next(area for area in after["areas"] if area["index"] == tile_index)
    verified = (
        bool(result.get("ok"))
        and bool(owned.get("unlocked"))
        and int(after["areaCount"]) == int(before["areaCount"]) + 1
        and int(after["maxAreaCount"]) == int(before["maxAreaCount"])
    )
    return {"ok": verified, "purchase": result, "priceDisplay": price_display,
            "areaCount": after["areaCount"], "maxAreaCount": after["maxAreaCount"]}


def _propose_development_plan(arguments: dict[str, Any]) -> dict[str, Any]:
    path, snapshot = _offline_snapshot(arguments)
    intended_use = str(arguments.get("intendedUse", "Mixed"))
    context = inspect_planning_area(
        snapshot,
        center=arguments["center"],
        radius=float(arguments.get("radius", 256)),
        intended_use=intended_use,
        sample_count=int(arguments.get("sampleCount", 9)),
        max_features=int(arguments.get("maxFeatures", 12)),
        candidate_limit=max(12, int(arguments.get("maxPlans", 3)) * 4),
    )
    _attach_exact_frontage_evidence(context, arguments, snapshot)
    plans = propose_development_plans(
        context,
        intended_use=intended_use,
        max_plans=int(arguments.get("maxPlans", 3)),
    )
    local_road_planning = _preview_local_development_road_candidates(
        snapshot, plans, arguments
    )
    accepted_by_plan: dict[str, list[dict[str, Any]]] = {}
    for candidate in local_road_planning.get("accepted", []):
        accepted_by_plan.setdefault(str(candidate.get("planId", "")), []).append(candidate)
    for plan in plans:
        road_access = plan.get("components", {}).get("roadAccess", {})
        accepted = accepted_by_plan.get(str(plan.get("planId", "")), [])
        road_access["nativePreviewCandidateCount"] = len(accepted)
        road_access["preferredNativePreviewCandidate"] = accepted[0] if accepted else None
    return {
        "ok": True,
        "offline": not local_road_planning.get("available", False),
        "planningOnly": True,
        "snapshotPath": str(path),
        "capturedAtUtc": snapshot.get("capturedAtUtc"),
        "center": context["center"],
        "radius": context["radius"],
        "intendedUse": intended_use,
        "contextSummary": {
            "demand": context["demand"],
            "terrain": context["terrain"],
            "utilities": context["utilities"],
            "roads": {
                "count": context["roads"]["count"],
                "countsByStructure": context["roads"]["countsByStructure"],
            },
            "buildings": context["buildings"],
            "candidateSiteDiagnostics": context["candidateSiteDiagnostics"],
            "actionableZoneBlocks": context["actionableZoneBlocks"],
            "limitations": context["limitations"],
        },
        "candidateDiagnostics": {
            **context["candidateSiteDiagnostics"],
            "scope": (
                "liveCaptureWithNativeRoadPreview"
                if local_road_planning.get("available", False)
                else "offlinePlanningOnly"
            ),
            "nativePreviewCalls": local_road_planning.get("nativePreviewCalls", 0),
            "acceptedPlanningAlternatives": len(plans),
            "acceptedNativePreviewCandidates": len(local_road_planning.get("accepted", [])),
        },
        "localRoadPlanning": local_road_planning,
        "total": len(plans),
        "plans": plans,
    }


def _preview_local_development_road_candidates(
    snapshot: dict[str, Any],
    plans: list[dict[str, Any]],
    arguments: dict[str, Any],
) -> dict[str, Any]:
    if str(arguments.get("snapshotPath", "")).strip():
        return {
            "planningOnly": True,
            "mutationAuthorized": False,
            "available": False,
            "reason": "nativePreviewUnavailableForExplicitOfflineSnapshot",
            "nativePreviewCalls": 0,
            "accepted": [],
        }
    if _live_city_identity_or_none() is None:
        return {
            "planningOnly": True,
            "mutationAuthorized": False,
            "available": False,
            "reason": "nativePreviewUnavailableWithoutLiveBridge",
            "nativePreviewCalls": 0,
            "accepted": [],
        }
    preview_limit = int(arguments.get("roadPreviewLimit", 12))
    reserve_display = float(arguments.get("reserveDisplay", 10000))
    cash_value = (
        snapshot.get("mayorSnapshot", {})
        .get("cityHealth", {})
        .get("cash", {})
        .get("amount")
    )
    cash_display = (
        float(cash_value) / MONEY_INTERNAL_SCALE
        if isinstance(cash_value, (int, float)) and not isinstance(cash_value, bool)
        else None
    )
    generation_limit = min(40, max(4, preview_limit * 4))
    generated = generate_local_development_road_candidates(
        snapshot,
        plans,
        road_prefab=str(arguments.get("roadPrefab", "")),
        limit=generation_limit,
    )
    statically_eligible = generated.get("staticallyEligible", 0)
    unlocked_payload = (
        _request("GET", "/prefabs/roads")
        if statically_eligible and preview_limit > 0
        else {"roads": []}
    )
    unlocked = {
        str(item.get("name", ""))
        for item in unlocked_payload.get("roads", [])
        if item.get("unlocked") is True
    }
    evaluated = []
    accepted = []
    rejection_counts = dict(generated.get("rejectionCounts", {}))
    native_calls = 0
    deferred_for_budget = 0
    for candidate in generated.get("candidates", []):
        if not candidate.get("staticEligible", False):
            continue
        if preview_limit <= 0:
            deferred_for_budget += 1
            evaluated.append({
                **candidate,
                "nativePreviewed": False,
                "accepted": False,
                "rejectionReasons": [],
                "deferredReason": "nativePreviewBudget",
            })
            continue
        road_prefab = str(candidate.get("roadPrefab", ""))
        if road_prefab not in unlocked:
            rejection_counts["roadPrefabLockedOrMissing"] = (
                rejection_counts.get("roadPrefabLockedOrMissing", 0) + 1
            )
            evaluated.append({**candidate, "nativePreviewed": False,
                              "accepted": False,
                              "rejectionReasons": ["roadPrefabLockedOrMissing"]})
            continue
        if native_calls >= preview_limit:
            deferred_for_budget += 1
            evaluated.append({
                **candidate,
                "nativePreviewed": False,
                "accepted": False,
                "rejectionReasons": [],
                "deferredReason": "nativePreviewBudget",
            })
            continue
        body = {
            "roadPrefab": road_prefab,
            "start": candidate["start"],
            "end": candidate["end"],
            "name": "Agent local development street",
        }
        native_calls += 1
        try:
            preview = _request("POST", "/commands/preview-network", body=body)
            survives = _road_preview_survives_settlement(preview, 0.7)
            reasons = []
            if not survives:
                if preview.get("nativeMoveMiddleNodeRisk"):
                    reasons.append("nativeMoveMiddleNodeRisk")
                if preview.get("shortCollinearMergeRisk"):
                    reasons.append("shortCollinearMergeRisk")
                if preview.get("toolErrors") not in (None, "", "None"):
                    reasons.append("nativeToolErrors")
                if preview.get("terrainProfile", {}).get("safe") is False:
                    reasons.append("terrainProfileUnsafe")
                if preview.get("valid") is not True:
                    reasons.append("nativePreviewInvalid")
                if not reasons:
                    reasons.append("settlementAcceptancePolicy")
            construction_cost = preview.get("constructionCost")
            cost_display = (
                float(construction_cost) / MONEY_INTERNAL_SCALE
                if isinstance(construction_cost, (int, float))
                and not isinstance(construction_cost, bool)
                else None
            )
            projected_cash = (
                cash_display - cost_display
                if cash_display is not None and cost_display is not None
                else None
            )
            reserve_passed = (
                projected_cash + 1e-6 >= reserve_display
                if projected_cash is not None
                else None
            )
            cash_policy = {
                "evidenceStatus": (
                    "available" if reserve_passed is not None else "cashOrCostUnavailable"
                ),
                "cashDisplay": cash_display,
                "constructionCostDisplay": cost_display,
                "reserveDisplay": reserve_display,
                "projectedCashDisplay": projected_cash,
                "passed": reserve_passed,
            }
            if survives and reserve_passed is False:
                survives = False
                reasons.append("cashReservePolicy")
            record = _compact_road_attempt(
                {
                    **candidate,
                    "nativePreviewed": True,
                    "accepted": survives,
                    "rejectionReasons": sorted(set(reasons)),
                    "cashPolicy": cash_policy,
                    "preview": preview,
                }
            )
        except BridgeRequestError as error:
            survives = False
            record = {
                **candidate,
                "nativePreviewed": True,
                "accepted": False,
                "rejectionReasons": ["bridgePreviewError"],
                "previewError": str(error),
            }
        for reason in record.get("rejectionReasons", []):
            rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
        evaluated.append(record)
        if survives:
            accepted.append(record)
    return {
        "planningOnly": True,
        "mutationAuthorized": False,
        "available": True,
        "generated": generated.get("generated", 0),
        "staticallyEligible": statically_eligible,
        "nativePreviewBudget": preview_limit,
        "reserveDisplay": reserve_display,
        "cashDisplay": cash_display,
        "nativePreviewCalls": native_calls,
        "nativePreviewCandidatesDeferred": deferred_for_budget,
        "acceptedCount": len(accepted),
        "rejectionCounts": dict(sorted(rejection_counts.items())),
        "accepted": accepted,
        "evaluated": evaluated,
        "limits": generated.get("limits", {}),
        "nextGate": (
            "create_checkpointAndCaptureRegressionBaselineBeforeOneBuild"
            if accepted
            else "inspectRejectionEvidenceAndRegenerateGeometry"
        ),
    }


def _validated_carved_road_terrain_profile(profile: dict[str, Any], expected_stage: str) -> dict[str, Any]:
    """A ground road judged as a player sees it (a road left to the game once settled, or any ground
    road inspected): accepted when the terrain the game draws does not cover it."""
    numeric_fields = ("buriedDepth", "finalSurfaceTolerance", "finalSurfaceDelta", "deepestCut",
                      "maxLocalTerrainGrade", "maximumLocalTerrainGrade")
    sample_count = profile.get("sampleCount")
    if (expected_stage not in ("settled", "inspection") or profile.get("verificationStage") != expected_stage
            or profile.get("source") != "nativeGenerateBezierAndFinalTerrain"
            or profile.get("required") is not True
            or isinstance(sample_count, bool) or not isinstance(sample_count, int) or sample_count < 3
            or any(isinstance(profile.get(field), bool) or not isinstance(profile.get(field), (int, float))
                   or not math.isfinite(profile[field]) for field in numeric_fields)):
        raise RuntimeError("Settled terrain verification returned incomplete evidence; "
                           "keep paused and reconcile before retrying.")
    tolerance = 0.002
    within = (profile["buriedDepth"] <= profile["finalSurfaceTolerance"] + tolerance
              and profile["maxLocalTerrainGrade"] <= profile["maximumLocalTerrainGrade"] + tolerance)
    if profile.get("safe") is not True or not within:
        raise RuntimeError("Settled terrain measurements contradict their safety thresholds.")
    return profile


def _validated_native_road_terrain_profile(
        profile: Any, expected_stage: str, require_ground: bool,
        provisional: bool = False) -> dict[str, Any]:
    """Validate immediate/settled native terrain telemetry without stage mixing.

    ``provisional`` marks a pending ground road's immediate profile: it may be unsafe
    (including a negative clearance) because the Bridge's settled verification decides
    and rolls the segment back locally on failure. Raising here skipped that rollback
    and left a checkpoint restore as the only recovery.
    """
    if isinstance(profile, dict) and profile.get("clearanceModel") == "gameShapedSurface":
        return _validated_carved_road_terrain_profile(profile, expected_stage)
    numeric_fields = (
        "maxRoadToTerrainDelta", "minimumRoadAboveTerrain",
        "maximumRoadAboveTerrain", "maxLocalTerrainGrade",
        "maximumRoadToTerrainDelta", "minimumRoadAboveTerrainAllowed",
        "maximumLocalTerrainGrade",
    )
    if (not isinstance(profile, dict)
            or profile.get("source") != "nativeGenerateBezierAndLiveTerrain"
            or profile.get("verificationStage") != expected_stage
            or profile.get("clearanceModel") != "legacyNodeInsetProxyNotCalibratedSurface"
            or not isinstance(profile.get("required"), bool)
            or not isinstance(profile.get("originalNodeTransition"), bool)):
        raise RuntimeError(
            f"{expected_stage.capitalize()} road verification returned incomplete terrain evidence; "
            "keep paused and reconcile before retrying.")
    sample_count = profile.get("sampleCount")
    if isinstance(sample_count, bool) or not isinstance(sample_count, int):
        raise RuntimeError(f"{expected_stage.capitalize()} road verification returned an invalid terrain sample count.")
    if profile["required"] is False:
        if require_ground or sample_count != 0 or profile.get("safe") is not True:
            raise RuntimeError(f"{expected_stage.capitalize()} ground-road terrain evidence was not required.")
        return profile
    if (sample_count < 3 or not isinstance(profile.get("safe"), bool)
            or (profile["safe"] is not True and not provisional)):
        raise RuntimeError(f"{expected_stage.capitalize()} ground-road terrain evidence is incomplete or unsafe.")
    # A transition from an original map node may sit as low as that node, which maps
    # can place flush with (or slightly into) the terrain; the Bridge floors it at -0.5.
    transition = profile["originalNodeTransition"] is True

    def negative_allowed(field: str) -> bool:
        if provisional and field == "minimumRoadAboveTerrain":
            return True
        return (transition and field in ("minimumRoadAboveTerrain", "minimumRoadAboveTerrainAllowed")
                and profile[field] >= ORIGINAL_NODE_TRANSITION_MINIMUM_CLEARANCE)

    if any(isinstance(profile.get(field), bool)
           or not isinstance(profile.get(field), (int, float))
           or not math.isfinite(profile[field])
           or (profile[field] < 0 and not negative_allowed(field))
           for field in numeric_fields):
        raise RuntimeError(f"{expected_stage.capitalize()} road verification returned invalid terrain measurements.")
    if profile["safe"] is not True:
        return profile  # provisional: settled verification decides
    tolerance = 0.002
    if (profile["minimumRoadAboveTerrain"] + tolerance
            < profile["minimumRoadAboveTerrainAllowed"]
            or profile["maxRoadToTerrainDelta"]
            > profile["maximumRoadToTerrainDelta"] + tolerance
            or profile["maxLocalTerrainGrade"]
            > profile["maximumLocalTerrainGrade"] + tolerance
            or profile["maximumRoadAboveTerrain"] + tolerance
            < profile["minimumRoadAboveTerrain"]):
        raise RuntimeError(f"{expected_stage.capitalize()} road terrain measurements contradict their safety thresholds.")
    return profile


def _validated_settled_road_terrain_profile(
        final: dict[str, Any], expected_segment_id: int) -> dict[str, Any]:
    """Return complete settled terrain evidence or fail closed."""
    if not isinstance(final, dict) or final.get("ok") is not True:
        raise RuntimeError("Settled road verification did not report exact success.")
    segment_id = final.get("segmentId")
    if (isinstance(segment_id, bool) or not isinstance(segment_id, int)
            or segment_id != expected_segment_id):
        raise RuntimeError("Settled road verification returned a different segment identity.")
    if final.get("settledVerification") is not True:
        raise RuntimeError("Settled road verification did not identify the settled stage.")
    return _validated_native_road_terrain_profile(
        final.get("terrainProfile"), "settled", require_ground=True)


SETTLE_BUSY_TRIES = 3
SETTLE_BUSY_SECONDS = 5.0
NOT_RUN = "command was not run"


def _settle_request(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    """A settlement read or finalize, sent again when the game thread was too busy to run it: such
    a timeout ran nothing, and finalizing a segment twice only reads its settled state again."""
    for attempt in range(SETTLE_BUSY_TRIES):
        try:
            return _request(method, path, body=body) if body is not None else _request(method, path)
        except BridgeRequestError as error:
            if NOT_RUN not in str(error) or attempt == SETTLE_BUSY_TRIES - 1:
                raise
            time.sleep(SETTLE_BUSY_SECONDS)
    raise RuntimeError("unreachable")


def _settled_after_busy(entry: dict[str, Any]) -> bool:
    """Whether a road recorded as unverified was in fact built and only its settlement check went
    unrun: its segment, finalized now, settles on the ground as the game laid it."""
    evidence = entry.get("evidence") or {}
    built = evidence.get("buildResult") or {}
    segment_id = built.get("segmentId")
    if built.get("ok") is not True or isinstance(segment_id, bool) or not isinstance(segment_id, int) \
            or segment_id <= 0 or NOT_RUN not in str(evidence.get("error", "")):
        return False
    try:
        for _ in range(8):
            _settle_request("GET", "/state/summary")
            final = _settle_request("POST", "/commands/finalize-road-build", body={"segmentId": segment_id})
            if final.get("verificationPending", False):
                time.sleep(0.1)
                continue
            _validated_settled_road_terrain_profile(final, segment_id)
            return final.get("ok") is True
    except (BridgeRequestError, RuntimeError, OSError):
        return False
    return False


def _road_failures(identity: dict[str, Any]) -> dict[str, Any]:
    """This city's road failures, after checking again any road that was built but whose settlement
    the busy game thread never ran: one that settles now is no failure and is dropped."""
    history = load_failures(ROAD_FAILURE_PATH, identity["instanceId"])
    settled = [entry for entry in history["failures"]
               if not recovery_verified(entry) and _settled_after_busy(entry)]
    if settled:
        history = {**history, "failures": [entry for entry in history["failures"] if entry not in settled]}
        clear_successful_attempt(ROAD_FAILURE_PATH, history)
    return history


def _build_verified_network(body: dict[str, Any], checkpoint_name: str = "") -> dict[str, Any]:
    normalized_prefab = "".join(
        character
        for character in str(body.get("roadPrefab", "")).casefold()
        if character.isalnum()
    )
    if "powerline" in normalized_prefab:
        obstacles = _read_live_power_line_obstacles()
        if not _power_line_pair_clear_of_buildings(
            body.get("start", {}), body.get("end", {}), obstacles
        ):
            raise RuntimeError(
                "Power-line corridor intersects an existing building footprint "
                f"plus the {POWER_LINE_BUILDING_CLEARANCE_METERS:g}m pylon clearance; "
                "re-run propose_power_line_connection or propose_minimal_power_grid."
            )
    identity = _current_city_identity()
    history = _road_failures(identity)
    if any(not recovery_verified(entry) for entry in history["failures"]):
        raise RuntimeError("Previous road recovery is unverified; stop construction and inspect the checkpoint.")
    if is_rejected(body, history):
        raise RuntimeError("This road alignment already failed in this city; replan before construction.")
    result: dict[str, Any] = {}
    candidate_evidence = {**body, "checkpointName": checkpoint_name}
    record_failure(ROAD_FAILURE_PATH, history, candidate_evidence, {
        "ok": False, "error": "Road request is in flight or was interrupted; inspect live state before retrying.",
        "recovery": {"localRollbackSucceeded": False, "checkpointRestoreRequired": True},
    })
    try:
        result = _request("POST", "/commands/build-network", body=body)
        if result.get("ok") is not True:
            raise RuntimeError("Network build did not report exact success.")
        segment_id = result.get("segmentId")
        if isinstance(segment_id, bool) or not isinstance(segment_id, int) or segment_id <= 0:
            raise RuntimeError("Network build returned an invalid segment identity.")
        immediate_profile = _validated_native_road_terrain_profile(
            result.get("actualTerrainProfile"), "immediate", require_ground=False,
            provisional=result.get("verificationPending") is True)
        expected_pending = immediate_profile["required"] is True
        if result.get("verificationPending") is not expected_pending:
            raise RuntimeError(
                "Network build terrain requirement disagrees with its settlement state.")
        if result.get("verificationPending", False):
            # The Bridge may require several distinct update turns to establish
            # stability. A successful pending response is NOT final acceptance.
            for _ in range(8):
                _settle_request("GET", "/state/summary")
                final = _settle_request("POST", "/commands/finalize-road-build",
                                        body={"segmentId": result["segmentId"]})
                if final.get("verificationPending", False):
                    time.sleep(0.1)
                    continue
                settled_profile = _validated_settled_road_terrain_profile(
                    final, result["segmentId"])
                result["verificationPending"] = False
                result["settledVerification"] = final
                result["settledTerrainProfile"] = settled_profile
                result["ok"] = result.get("ok") is True and final.get("ok") is True
                break
            else:
                raise RuntimeError("Road settlement did not finish; keep paused and recover or finish verification before building again.")
    except (BridgeRequestError, RuntimeError, OSError) as error:
        detail = error.detail if isinstance(error, BridgeRequestError) else {
            "ok": False, "error": str(error),
            "recovery": {"localRollbackSucceeded": False, "checkpointRestoreRequired": True},
        }
        bridge_error = str(detail.get("error", "")).casefold() if isinstance(detail, dict) else ""
        if (not result and isinstance(error, BridgeRequestError) and isinstance(detail, dict)
                and "recovery" not in detail
                and ("timed out" not in bridge_error or "command was not run" in bridge_error)):
            # The Bridge refused the request during validation, or withdrew it unstarted
            # after a game-thread timeout, so NetTool created nothing to roll back. Any
            # other timeout is excluded: the command may still have run.
            detail = {**detail, "recovery": {"noConstructionAttempted": True}}
        # Retain the creation response even when finalization is an HTTP error.
        # Previously the returned entity and immediate geometry were lost here.
        detail = {**detail, "city": identity, "candidate": candidate_evidence, "buildResult": result}
        record_failure(ROAD_FAILURE_PATH, history, candidate_evidence, detail)
        raise BridgeRequestError(detail) from error
    if not result.get("ok", False):
        evidence = result.get("settledVerification", result)
        record_failure(ROAD_FAILURE_PATH, history, candidate_evidence, evidence)
    else:
        clear_successful_attempt(ROAD_FAILURE_PATH, history)
    return result


def _growth_pacing(demand: dict[str, Any], zones: dict[str, Any]) -> dict[str, Any]:
    """Combine current demand with occupied/vacant zoned capacity; never authorize zoning."""
    capacity = zones.get("capacityByZone")
    if not isinstance(capacity, list):
        return {
            "available": False,
            "reason": "capacityByZoneUnavailable",
            "mutationAuthorized": False,
            "highDemandAloneAuthorizesExpansion": False,
        }
    by_zone = {
        str(item.get("zone", "")): item
        for item in capacity
        if isinstance(item, dict) and item.get("zone")
    }
    groups = {
        "residential": ("ResidentialLow", "ResidentialHigh"),
        "commercial": ("CommercialLow", "CommercialHigh"),
        "workplace": ("Industrial", "Office"),
    }
    results = {}
    for name, zone_names in groups.items():
        zoned = sum(
            int(by_zone.get(zone, {}).get("validUnsharedCells", 0) or 0)
            for zone in zone_names
        )
        occupied = sum(
            int(by_zone.get(zone, {}).get("occupiedCells", 0) or 0)
            for zone in zone_names
        )
        vacant = sum(
            int(by_zone.get(zone, {}).get("vacantCells", 0) or 0)
            for zone in zone_names
        )
        # Cells stranded behind an existing building never develop; pace on the
        # road-reachable vacancy when the Bridge reports it.
        reports_developable = all(
            "developableVacantCells" in by_zone[zone] for zone in zone_names if zone in by_zone
        )
        developable = sum(
            int(by_zone.get(zone, {}).get("developableVacantCells", 0) or 0)
            for zone in zone_names
        ) if reports_developable else vacant
        demand_value = demand.get(name)
        demand_value = (
            int(demand_value)
            if isinstance(demand_value, (int, float)) and not isinstance(demand_value, bool)
            else None
        )
        occupancy_ratio = occupied / zoned if zoned > 0 else None
        decision, reason, maximum_blocks, target_vacant = gameplay_rules.pacing_decision(
            demand_value, zoned, developable)
        results[name] = {
            "demand": demand_value,
            "zonedValidUnsharedCells": zoned,
            "occupiedCells": occupied,
            "vacantCells": vacant,
            "developableVacantCells": developable,
            "strandedVacantCells": vacant - developable,
            "occupancyRatio": None if occupancy_ratio is None else round(occupancy_ratio, 3),
            "targetVacantCells": target_vacant,
            "decision": decision,
            "reason": reason,
            "maximumAdditionalBlocksThisWake": maximum_blocks,
        }
    pacing = gameplay_rules.load_rules()["growthPacing"]
    return {
        "available": True,
        "mutationAuthorized": False,
        "highDemandThreshold": pacing["highDemandThreshold"],
        "vacantTargetTiers": pacing["vacantTargetTiers"],
        "maximumBlocksPerWake": pacing["maximumBlocksPerWake"],
        "rulesSource": "references/cs1-gameplay-rules.json#growthPacing",
        "highDemandAloneAuthorizesExpansion": False,
        "usesCurrentDemandAndZonedOccupancy": True,
        "categories": results,
        "limitations": [
            "Current demand and occupancy are pacing evidence, not calibrated forecasts.",
            "Vacant supply should lead high demand, but each wake is capped so zoning stays in small repeated batches; every block still requires frontage, utility, reserve, checkpoint, settlement, and regression gates.",
            "Demand direction requires comparable later-game snapshots from assess_city; paused repeated reads are not a trend.",
        ],
    }


def _frontage_block_diagnostics(
    blocks: dict[str, Any], sample_limit: int
) -> dict[str, Any]:
    """Summarize why otherwise-clear unzoned blocks fail the frontage gate."""
    items = [item for item in blocks.get("blocks", []) if isinstance(item, dict)]
    blocked = [item for item in items if item.get("developmentFrontageSafe") is not True]
    counts: dict[str, int] = {}
    for item in blocked:
        reason = str(item.get("frontageReason", "")).strip() or "unknown"
        counts[reason] = counts.get(reason, 0) + 1
    total_matching = int(blocks.get("totalMatching", len(items)) or 0)
    returned = int(blocks.get("returned", len(items)) or 0)
    return {
        "ok": blocks.get("ok") is True,
        "totalClearUnzonedBlocks": total_matching,
        "returned": returned,
        "diagnosticComplete": returned >= total_matching,
        "developmentFrontageSafeReturned": sum(
            item.get("developmentFrontageSafe") is True for item in items
        ),
        "blockedCountsByReason": dict(sorted(counts.items())),
        "blockedSamples": [
            {
                key: item.get(key)
                for key in (
                    "blockId",
                    "roadSegmentId",
                    "roadPrefab",
                    "roadName",
                    "center",
                    "rows",
                    "validCells",
                    "developmentFrontageSafe",
                    "frontageReason",
                    "junctionClearanceSafe",
                )
            }
            for item in blocked[:sample_limit]
        ],
        "limitations": [
            "These are exact live block rejection reasons, not authorization to weaken the frontage gate.",
            "When diagnosticComplete is false, reason counts cover only the returned bounded sample.",
        ],
    }


def call_tool(name: str, arguments: dict[str, Any]) -> Any:
    _validate_arguments(name, arguments)
    arguments = dict(arguments)

    if name == "assess_city":
        snapshot = _request("GET", "/state/mayor-snapshot", query={"detailLimit": 0})
        result = assess_city(snapshot, arguments.get("previousSnapshot"),
                             reserve_display=arguments.get("reserveDisplay", 10000))
        result["offline"] = False
        return result
    if name == "inspect_economy":
        return _request("GET", "/state/economy-details")
    if name == "observe_city":
        snapshot = _request("GET", "/state/mayor-snapshot", query={"detailLimit": arguments.get("detailLimit", 10)})
        return _annotate_display_money(snapshot)
    if name == "validate_progression_mode":
        return _validate_progression_mode()
    if name == "inspect_toolbar_unlocks":
        return _request("GET", "/state/toolbar-unlocks")
    if name == "inspect_terrain_editing":
        return _request("GET", "/state/terrain-editing")
    if name == "inspect_service_coverage":
        return _request("GET", "/state/service-coverage", query=arguments)
    if name == "inspect_service_capacity":
        return _request("GET", "/state/service-capacity", query=arguments)
    if name == "inspect_facility_influence":
        return _request("GET", "/state/facility-influence", query=arguments)
    if name == "inspect_water_service_coverage":
        return _request("GET", "/state/water-service-coverage", query=arguments)
    if name == "inspect_electricity_service_coverage":
        return _request("GET", "/state/electricity-service-coverage", query=arguments)
    if name == "diagnose_utility_shortfall":
        return _diagnose_utility_shortfall(arguments)
    if name == "diagnose_noise":
        return _diagnose_noise(arguments)
    if name == "inspect_electricity_connection_area":
        return _inspect_electricity_connection_area(arguments)
    if name == "inspect_power_plant_operation":
        return _inspect_power_plant_operation(arguments)
    if name == "inspect_zone_cell_geometry":
        return _request("GET", "/state/zone-cell-geometry", query=arguments)
    if name == "propose_network_crossing":
        return _propose_network_crossing(arguments)
    if name == "propose_expansion_areas":
        return _propose_expansion_areas(arguments)
    if name == "propose_zoning_blocks":
        return _propose_zoning_blocks(arguments)
    if name == "inspect_growth_capacity":
        limit = arguments.get("limit", 20)
        demand = _request("GET", "/state/demand")
        zones = _request("GET", "/state/zones")
        actionable_blocks = _request(
            "GET",
            "/state/zone-blocks",
            query={
                "limit": limit,
                "onlyFullyUnzoned": "true",
                "requireClearOccupancy": "true",
                "requireDevelopmentFrontage": "true",
            },
        )
        diagnostic_blocks = _request(
            "GET",
            "/state/zone-blocks",
            query={
                "limit": 200,
                "onlyFullyUnzoned": "true",
                "requireClearOccupancy": "true",
                "requireDevelopmentFrontage": "false",
            },
        )
        return {
            "ok": True,
            "demand": demand,
            "zones": zones,
            "growthPacing": _growth_pacing(demand, zones),
            "actionableBlocks": actionable_blocks,
            "frontageDiagnostics": _frontage_block_diagnostics(
                diagnostic_blocks, min(int(limit), 20)
            ),
            "anomalies": _request(
                "GET",
                "/state/zone-anomalies",
                query={"limit": limit, "includeUnzonedHoles": "true"},
            ),
        }
    if name == "inspect_traffic":
        return _request("GET", "/state/traffic", query=arguments)
    if name == "propose_bypass_roads":
        return _propose_bypass_roads(arguments)
    if name == "propose_traffic_remedies":
        return _propose_traffic_remedies(arguments)
    if name == "assess_traffic_congestion":
        return _assess_traffic_congestion(arguments)
    if name == "assess_problem_remedies":
        return _assess_problem_remedies(arguments)
    if name == "compare_service_options":
        return _compare_service_options(arguments)
    if name == "propose_service_sites":
        return _propose_service_sites(arguments)
    if name == "inspect_junction":
        return _request("GET", "/state/junction", query={"id": arguments["nodeId"]})
    if name == "inspect_segment_routes":
        query = {"id": arguments["segmentId"], "limit": arguments.get("limit", 10)}
        if arguments.get("vehicleAI"):
            query["vehicleAI"] = arguments["vehicleAI"]
        return _request("GET", "/state/segment-routes", query=query)
    if name == "inspect_road_result":
        allow_terminal = arguments.get("allowTerminalDeadEnd", False)
        terminal_node_id = int(arguments.get("terminalNodeId", 0) or 0)
        if allow_terminal and terminal_node_id <= 0:
            raise ValueError("terminalNodeId is required when allowTerminalDeadEnd is true")
        if not allow_terminal and terminal_node_id:
            raise ValueError("allowTerminalDeadEnd must be true when terminalNodeId is provided")
        return _inspect_road_result(
            arguments["segmentId"],
            arguments.get("includeDeadEnds", True),
            terminal_node_id,
            arguments.get("outsideAccessRequirement", "two-way"),
        )
    if name == "inspect_transport":
        limit = arguments.get("limit", 50)
        max_stop_distance = arguments.get("maxStopDistance", 96)
        return {
            "ok": True,
            "lines": _request("GET", "/state/transport-lines", query={"limit": limit}),
            "lineAnomalies": _request("GET", "/state/transport-line-anomalies", query={"limit": limit}),
            "stationAnomalies": _request(
                "GET",
                "/state/transport-station-anomalies",
                query={"limit": limit, "maxStopDistance": max_stop_distance},
            ),
        }
    if name == "list_unlocked_assets":
        kind = arguments["kind"]
        service = arguments.get("service", "")
        payload = _request("GET", f"/prefabs/{kind}", query={"service": service} if service else None)
        key = {"roads": "roads", "networks": "networks", "buildings": "buildings"}[kind]
        items = [item for item in payload.get(key, []) if item.get("unlocked", False)]
        return {"ok": True, "kind": kind, "service": service, "count": len(items), "items": items}
    if name == "preview_service_impact":
        return _request("POST", "/commands/preview-service-impact", body=arguments)
    if name == "preview_building_candidate":
        body = dict(arguments)
        body["ignoreBuildingId"] = body.pop("buildingId", 0)
        return _request("POST", "/commands/preview-placement", body=body)
    if name == "preview_power_plant_candidate":
        return _preview_power_plant_candidate(arguments)
    if name == "inspect_wind_resource_map":
        return _inspect_wind_resource_map(arguments)
    if name == "propose_power_plant_candidates":
        return _propose_power_plant_candidates(arguments)
    if name == "propose_power_line_connection":
        return _propose_power_line_connection(arguments)
    if name == "propose_minimal_power_grid":
        return _propose_minimal_power_grid(arguments)
    if name == "inspect_power_grid_efficiency":
        return _inspect_power_grid_efficiency(arguments)
    if name == "preview_network":
        return _request("POST", "/commands/preview-network", body=arguments)
    if name == "propose_outside_road_connection":
        return _propose_outside_road_connection(arguments)
    if name == "preview_road_upgrade":
        body = dict(arguments)
        body["dryRun"] = True
        return _request("POST", "/commands/upgrade-road", body=body)
    if name == "preview_road_reversal":
        body = dict(arguments)
        body["dryRun"] = True
        return _request("POST", "/commands/reverse-road", body=body)
    if name == "preview_bulldoze_impact":
        return _request("POST", "/commands/preview-bulldoze-impact", body=arguments)
    if name == "preview_operational_change":
        body = dict(arguments)
        body["dryRun"] = True
        return _request("POST", "/commands/set-building-active", body=body)
    if name == "propose_transit_lines":
        return _propose_transit_lines(arguments)
    if name == "preview_transport_plan":
        body = dict(arguments)
        body["dryRun"] = True
        return _request("POST", "/commands/create-transport-line", body=body)
    if name == "preview_district_specialization":
        body = dict(arguments)
        body["dryRun"] = True
        return _request("POST", "/commands/set-district-specialization", body=body)
    if name == "inspect_water_pair":
        return _inspect_water_pair(arguments)
    if name == "propose_water_pair_candidates":
        return _propose_water_pair_candidates(arguments)
    if name == "propose_groundwater_pump_sites":
        return _propose_groundwater_pump_sites(arguments)
    if name == "propose_water_service_connection":
        return _propose_water_service_connection(arguments)
    if name == "preview_water_system_plan":
        return _inspect_water_system_plan(arguments)
    if name == "inspect_water_network_topology":
        return _inspect_water_network_topology(arguments)
    if name == "inspect_road_layers_3d":
        return _inspect_road_layers_3d(arguments)
    if name == "inspect_grade_separated_crossings":
        return _inspect_grade_separated_crossings(arguments)
    if name == "inspect_building_layers_3d":
        return _inspect_building_layers_3d(arguments)
    if name == "inspect_building_road_clearance_3d":
        return _inspect_building_road_clearance_3d(arguments)
    if name == "inspect_planning_area":
        return _inspect_planning_area(arguments)
    if name == "propose_development_plan":
        return _propose_development_plan(arguments)
    if name == "propose_frontage_road":
        return _propose_frontage_road(arguments)
    if name == "create_checkpoint":
        result = _request("POST", "/commands/create-checkpoint", body=arguments)
        actual_name = str(result.get("saveName") or arguments["name"])
        verified = _wait_checkpoint(actual_name)
        return {
            "ok": bool(result.get("ok")) and verified,
            "checkpoint": result,
            "requestedName": arguments["name"],
            "checkpointName": actual_name,
            "verified": verified,
        }
    if name == "capture_regression_baseline":
        result = _request("POST", "/commands/capture-regression-baseline", body=arguments)
        if result.get("ok") is True and isinstance(result.get("baseline"), dict):
            identity = _current_city_identity()
            mayor_regression.save_baseline(
                REGRESSION_BASELINES_PATH,
                str(result.get("name") or arguments["name"]),
                identity,
                result["baseline"],
            )
            result = {
                **result,
                "persistence": {
                    "stored": True,
                    "cityInstanceId": identity["instanceId"],
                },
            }
        return result
    if name == "verify_regression":
        try:
            result = _request("GET", "/state/regression-report", query=arguments)
        except BridgeRequestError as error:
            # The Bridge returns a non-2xx response when its process-local
            # baseline was lost during a restart. Preserve other HTTP errors,
            # but let this expected miss fall through to the durable store.
            result = error.detail
            if "baseline was not found" not in str(
                result.get("error", "")
            ).lower():
                raise
        if result.get("ok") is True:
            return {**result, "baselineSource": "liveProcess"}
        if "baseline was not found" not in str(result.get("error", "")).lower():
            return result
        record = mayor_regression.load_baseline(
            REGRESSION_BASELINES_PATH, arguments["name"]
        )
        if record is None:
            return result
        snapshot = _request("GET", "/state/mayor-snapshot", query={"detailLimit": 0})
        identity = _city_identity(snapshot)
        stored_identity = record.get("cityIdentity", {})
        if stored_identity.get("instanceId") != identity["instanceId"]:
            return {
                "ok": False,
                "error": "Persisted regression baseline belongs to a different city instance",
                "expectedCityIdentity": stored_identity,
                "liveCityIdentity": identity,
            }
        temporary_name = "__recovery_" + hashlib.sha256(
            arguments["name"].encode("utf-8")
        ).hexdigest()[:16]
        current = _request(
            "POST", "/commands/capture-regression-baseline", body={"name": temporary_name}
        )
        if current.get("ok") is not True or not isinstance(current.get("baseline"), dict):
            return current
        comparison = mayor_regression.compare(
            arguments["name"], record["baseline"], current["baseline"]
        )
        return {
            **comparison,
            "baselineSource": "persistentRestartRecovery",
            "capturedAtUtc": record.get("capturedAtUtc"),
            "cityIdentity": identity,
        }
    if name == "set_simulation_speed":
        return _request("POST", "/commands/set-simulation-speed", body=arguments)
    if name == "place_service":
        _require_normal_progression()
        placement = _request(
            "POST",
            "/commands/preview-placement",
            body=_placement_body(arguments),
        )
        if placement.get("waterPlacement") is not None:
            raise RuntimeError(
                "Water facilities must use place_water_facility with a verified "
                "intake/outlet pair."
            )
        if placement.get("electricityProfile") is not None:
            raise RuntimeError(
                "Power plants must use place_power_plant with production, "
                "environmental, and fuel-logistics gates."
            )
        if not placement.get("valid", False):
            raise RuntimeError("Native building placement preview is unsafe.")
        body = _require_checkpoint(arguments)
        return _request("POST", "/commands/place-building", body=body)
    if name == "rebuild_full_facility":
        _require_normal_progression()
        _require_checkpoint(dict(arguments))
        return _rebuild_full_facility(arguments)
    if name == "place_water_facility":
        _require_normal_progression()
        arguments = _with_placed_partner(arguments)
        pair = _inspect_water_pair(arguments)
        if not pair.get("safe", False):
            raise RuntimeError(
                "Water facility pair is unsafe: "
                + ", ".join(str(risk) for risk in pair.get("risks", []))
            )
        role = str(arguments["role"])
        candidate = arguments[role]
        _require_checkpoint(arguments)
        result = _request(
            "POST",
            "/commands/place-building",
            body={
                "buildingPrefab": candidate["buildingPrefab"],
                "position": candidate["position"],
                "angleDegrees": candidate.get("angleDegrees", 0),
                "nativePlacementVerified": True,
            },
        )
        return {
            "ok": bool(result.get("ok", False)),
            "role": role,
            "waterPairVerified": True,
            "mutation": result,
            "requiresPairAndTopologyVerification": True,
        }
    if name == "place_power_plant":
        _require_normal_progression()
        preview = _preview_power_plant_candidate(arguments)
        if not preview.get("safe", False):
            raise RuntimeError(
                "Power-plant candidate is unsafe: "
                + ", ".join(str(risk) for risk in preview.get("risks", []))
            )
        if preview.get("classification", {}).get("locationDependentProduction", False):
            # WindTurbineAI's native placement preview queues its tutorial-guide
            # activation. A game-thread read acts as a deterministic queue barrier so
            # immediate construction cannot collide with that still-pending action.
            _request("GET", "/state/summary")
        _require_checkpoint(arguments)
        result = _request(
            "POST",
            "/commands/place-building",
            body={
                "buildingPrefab": arguments["buildingPrefab"],
                "position": arguments["position"],
                "angleDegrees": arguments.get("angleDegrees", 0),
                "nativePlacementVerified": True,
            },
        )
        return {
            "ok": bool(result.get("ok", False)),
            "previewVerified": True,
            "mutation": result,
            "requiresOperationalVerification": True,
        }
    if name == "build_network":
        _require_normal_progression()
        if _is_water_network_prefab(str(arguments["roadPrefab"])):
            raise RuntimeError(
                "Water-pipe mutations must use build_water_pipe (a new water system), "
                "build_water_service_pipe (buildings) or connect_water_facility (a new "
                "intake or outlet) so hydrology and network topology cannot be bypassed."
            )
        checkpoint_name = arguments["checkpointName"]
        body = _require_checkpoint(arguments)
        return _build_verified_network(body, checkpoint_name)
    if name == "connect_water_facility":
        _require_normal_progression()
        if not arguments.get("dryRun"):
            arguments = _require_checkpoint(arguments)
        return _connect_water_facility(arguments)
    if name == "build_water_pipe":
        _require_normal_progression()
        arguments = _normalize_water_pipe_segments(arguments)
        segment_index = int(arguments["segmentIndex"])
        verification = _inspect_water_system_plan(
            arguments,
            completed_segment_indexes=set(range(segment_index)),
        )
        if not verification["safe"]:
            raise RuntimeError(
                "Water-system plan is unsafe: "
                + ", ".join(str(risk) for risk in verification["risks"])
            )
        segments = arguments["segments"]
        if segment_index < 0 or segment_index >= len(segments):
            raise ValueError("segmentIndex is outside the verified water-system plan")
        segment = segments[segment_index]
        body = _require_checkpoint(arguments)
        result = _request(
            "POST",
            "/commands/build-network",
            body={
                "roadPrefab": body["pipePrefab"],
                "start": segment["start"],
                "end": segment["end"],
                "name": segment.get("name", ""),
            },
        )
        segment_id = int(result.get("segmentId", 0) or 0)
        details = (
            _request("GET", "/state/network-segment-details", query={"id": segment_id})
            if segment_id > 0
            else None
        )
        water_segment_created = bool(
            details
            and str(details.get("service", "")) == "Water"
            and "Created" in str(details.get("flags", ""))
        )
        return {
            "ok": bool(result.get("ok", False)) and water_segment_created,
            "planId": verification["planId"],
            "segmentIndex": segment_index,
            "role": segment["role"],
            "planVerified": True,
            "mutation": result,
            "segment": details,
            "waterSegmentCreated": water_segment_created,
            "requiresFinalTopologyVerification": True,
        }
    if name == "build_water_service_pipe":
        _require_normal_progression()
        segment_index = int(arguments["segmentIndex"])
        segments = arguments["segments"]
        if segment_index < 0 or segment_index >= len(segments):
            raise ValueError("segmentIndex is outside the water service branch plan")
        verification = _inspect_water_service_branch_plan(
            arguments, completed_segment_indexes=set(range(segment_index))
        )
        if not verification["safe"]:
            raise RuntimeError(
                "Water service branch plan is unsafe: "
                + ", ".join(str(risk) for risk in verification["risks"])
            )
        if not verification["actionRequired"]:
            raise RuntimeError(
                "Target buildings already have water and sewage; no branch mutation is required."
            )
        segment = segments[segment_index]
        body = _require_checkpoint(arguments)
        result = _request(
            "POST",
            "/commands/build-network",
            body={
                "roadPrefab": body["pipePrefab"],
                "start": segment["start"],
                "end": segment["end"],
                "name": segment.get("name", ""),
            },
        )
        segment_id = int(result.get("segmentId", 0) or 0)
        details = (
            _request("GET", "/state/network-segment-details", query={"id": segment_id})
            if segment_id > 0
            else None
        )
        water_segment_created = bool(
            details
            and str(details.get("service", "")) == "Water"
            and "Created" in str(details.get("flags", ""))
        )
        return {
            "ok": bool(result.get("ok", False)) and water_segment_created,
            "planId": verification["planId"],
            "segmentIndex": segment_index,
            "planVerified": True,
            "mutation": result,
            "segment": details,
            "waterSegmentCreated": water_segment_created,
            "requiresSettlementAndExactCoverageVerification": True,
        }
    if name == "upgrade_road":
        _require_normal_progression()
        body = _require_checkpoint(arguments)
        body["confirm"] = True
        result = _request("POST", "/commands/upgrade-road", body=body)
        return _verify_rebuilt_zoning(result, [result["newSegmentId"]] if result.get("newSegmentId") else [], "Upgrade")
    if name == "inspect_building_zoning_support":
        return _request("GET", "/state/zoning-support", query=arguments)
    if name == "repair_building_zoning":
        _require_normal_progression()
        body = _require_checkpoint(arguments)
        body["confirm"] = True
        return _request("POST", "/commands/repair-building-zoning", body=body)
    if name == "split_road_segment":
        _require_normal_progression()
        body = _require_checkpoint(arguments)
        result = _request("POST", "/commands/split-road", body=body)
        return _verify_rebuilt_zoning(result, list(result.get("newSegmentIds") or []), "Split")
    if name == "reverse_road":
        _require_normal_progression()
        body = _require_checkpoint(arguments)
        return _request("POST", "/commands/reverse-road", body=body)
    if name == "widen_road_chain":
        _require_normal_progression()
        _require_checkpoint(dict(arguments))
        return _widen_road_chain(arguments)
    if name == "trial_junction_control":
        _require_normal_progression()
        _require_checkpoint(dict(arguments))
        return _trial_junction_control(arguments)
    if name == "set_junction_control":
        _require_normal_progression()
        body = _require_checkpoint(arguments)
        if (body["control"] == "stopSign") != ("segmentId" in body):
            raise ValueError("segmentId is required for stopSign and not allowed for trafficLights")
        return _request("POST", "/commands/set-junction-control", body=body)
    if name == "clear_abandoned_buildings":
        _require_normal_progression()
        _require_checkpoint(dict(arguments))
        return _clear_abandoned_buildings(arguments)
    if name == "bulldoze_entity":
        _require_normal_progression()
        body = _require_checkpoint(arguments)
        return _request("POST", "/commands/bulldoze", body=body)
    if name == "set_building_active":
        _require_normal_progression()
        body = _require_checkpoint(arguments)
        return _request("POST", "/commands/set-building-active", body=body)
    if name == "create_district":
        _require_normal_progression()
        body = _require_checkpoint(arguments)
        return _request("POST", "/commands/create-district", body=body)
    if name == "set_district_specialization":
        _require_normal_progression()
        body = _require_checkpoint(arguments)
        return _request("POST", "/commands/set-district-specialization", body=body)
    if name == "zone_block":
        _require_normal_progression()
        body = _require_checkpoint(arguments)
        if "blockId" not in body and ("center" not in body or "radius" not in body):
            raise ValueError("zone_block requires blockId or both center and radius")
        body.setdefault("preserveOccupied", True)
        result = _request("POST", "/commands/set-zone", body=body)
        if ("blockId" in body and result.get("ok") is True
                and result.get("dryRun") is not True and result.get("changedCells") == 0):
            if _block_zoned_as(int(body["blockId"]), str(body.get("zone"))):
                # its cells overlap a neighbour's, zoned before it this wake: zoned as asked (live:
                # a 2-cell block at a junction read "changed no cells" and stopped the loop)
                return {**result, "alreadyZoned": True}
            # Older Bridges reported success after a gate skipped the exact block.
            return {**result, "ok": False,
                    "error": "Exact zone block changed no cells; inspect skipped gate counts before retrying."}
        return result
    if name == "set_service_budget":
        _require_normal_progression()
        # putting a budget back to the default undoes an earlier trim; the cooldown, there to space
        # out fiscal experiments, must not keep a town short of power while it waits (live: a power
        # budget left at 50% bought four wind turbines in an hour)
        restoring = arguments.get("reason") == "post-change-correction" and int(arguments.get("budget", 0)) == 100
        state = _load_city_scoped_memory()[0] if restoring else _require_fiscal_cooldown_clear()
        body = _require_checkpoint(arguments)
        reason = str(body.pop("reason"))
        expected_outcome = str(body.pop("expectedOutcome"))
        # The Bridge rejects an omitted subService; apply the schema default.
        body.setdefault("subService", "None")
        # CS1 keeps separate day and night budgets; setting only one leaves the
        # other period unchanged, and the cooldown would delay the second half.
        day_and_night = bool(body.pop("dayAndNight", False))
        if day_and_night:
            body.pop("night", None)
            day = _request("POST", "/commands/set-service-budget", body={**body, "night": False})
            night = (_request("POST", "/commands/set-service-budget", body={**body, "night": True})
                     if day.get("ok") else {"ok": False, "error": "Day budget change failed; night budget not changed."})
            result = {"ok": bool(day.get("ok")) and bool(night.get("ok")), "dayAndNight": True,
                      "day": day, "night": night}
        else:
            result = _request("POST", "/commands/set-service-budget", body=body)
        if result.get("ok") and not restoring:  # an undone trim starts no new cooldown
            _record_fiscal_adjustment(
                state,
                action="set_service_budget",
                reason=reason,
                expected_outcome=expected_outcome,
            )
        return result
    if name == "set_tax_rate":
        _require_normal_progression()
        state = _require_fiscal_cooldown_clear()
        body = _require_checkpoint(arguments)
        reason = str(body.pop("reason"))
        expected_outcome = str(body.pop("expectedOutcome"))
        result = _request("POST", "/commands/set-tax-rate", body=body)
        if result.get("ok"):
            _record_fiscal_adjustment(
                state,
                action="set_tax_rate",
                reason=reason,
                expected_outcome=expected_outcome,
            )
        return result
    if name == "take_loan":
        _require_normal_progression()
        body = _require_checkpoint(arguments)
        body.pop("reason")
        body.pop("expectedOutcome")
        return _request("POST", "/commands/take-loan", body={"slot": body["slot"], "option": body.get("option", 0)})
    if name == "purchase_map_area":
        _require_normal_progression()
        body = _require_checkpoint(arguments)
        return _purchase_map_area(int(body["tileIndex"]), float(body.get("reserveDisplay", 10000)))
    if name == "create_transport_line":
        _require_normal_progression()
        body = _require_checkpoint(arguments)
        return _request("POST", "/commands/create-transport-line", body=body)
    if name == "set_transport_line":
        _require_normal_progression()
        body = _require_checkpoint(arguments)
        return _request("POST", "/commands/set-transport-line", body=body)
    if name == "delete_transport_line":
        _require_normal_progression()
        body = _require_checkpoint(arguments)
        return _request("POST", "/commands/delete-transport-line", body=body)
    if name == "save_city":
        _require_normal_progression()
        result = _request("POST", "/commands/save", body=arguments)
        verified = _wait_checkpoint(arguments["name"])
        return {"ok": bool(result.get("ok")) and verified, "save": result, "verified": verified}
    if name == "load_mayor_memory":
        state, scope = _load_city_scoped_memory()
        return {"ok": True, "path": str(MEMORY_PATH), "state": state, "scope": scope}
    if name == "save_mayor_memory":
        return _save_city_scoped_memory(arguments["state"])
    if name == "score_candidates":
        return _score_candidates(arguments["candidates"])
    raise ValueError(f"Unknown tool: {name}")


def main() -> int:
    # The CLI contract is UTF-8 JSON regardless of the active Windows console
    # code page. Tests and external agents read this stream directly.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    definitions = subparsers.add_parser("definitions", help="Print function-calling tool definitions.")
    selection = definitions.add_mutually_exclusive_group()
    selection.add_argument("--profile", choices=("all", "mayor"), default="all")
    selection.add_argument("--cluster", help="Load one domain cluster on demand.")
    selection.add_argument("--names", nargs="+", help="Load exact domain tools on demand.")
    subparsers.add_parser("clusters", help="List tool clusters without loading their schemas.")
    call = subparsers.add_parser("call", help="Validate and execute one named tool.")
    call.add_argument("name")
    argument_source = call.add_mutually_exclusive_group()
    argument_source.add_argument("--arguments", help="Inline JSON object.")
    argument_source.add_argument("--arguments-file", help="Path to a UTF-8 JSON object file, or '-' for stdin.")
    subparsers.add_parser("memory-path", help="Print the persistent mayor memory path.")
    args = parser.parse_args()

    try:
        if args.command == "definitions":
            result = {"tools": _select_definitions(args.profile, args.names, args.cluster)}
        elif args.command == "clusters":
            result = _cluster_summary()
        elif args.command == "memory-path":
            result = {"path": str(MEMORY_PATH)}
        else:
            if args.arguments_file == "-":
                source = sys.stdin.read()
            elif args.arguments_file:
                source = Path(args.arguments_file).read_text(encoding="utf-8-sig")
            else:
                source = args.arguments or "{}"
            parsed = json.loads(source.lstrip("\ufeff"))
            result = call_tool(args.name, parsed)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if isinstance(result, dict) and result.get("ok") is False else 0
    except BridgeRequestError as error:
        print(json.dumps(error.detail, ensure_ascii=False, indent=2))
        return 1
    except (OSError, ValueError, RuntimeError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

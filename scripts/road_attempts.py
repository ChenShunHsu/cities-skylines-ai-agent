"""City-scoped, bounded road failure evidence. Never authorizes construction."""
from __future__ import annotations

import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any

# The Bridge's road gates this history was judged under. Raise it with any change to those gates:
# a failure under older gates no longer predicts the same alignment failing, so it stops blocking it.
# 2: a cut is judged by the drawn surface, and a neighbouring road only when buried.
# 3: beside a node of the map's original network a cut is only checked for not being buried.
GATE_POLICY = 4


def geometry_key(candidate: dict[str, Any]) -> str:
    # Deliberately independent of prefab and traversal direction: changing the
    # road skin must not repeat a known failed alignment on unchanged terrain.
    points = []
    for side in ("start", "end"):
        point = candidate[side]
        values = [float(point[axis]) for axis in ("x", "z")]
        if not all(math.isfinite(value) for value in values):
            raise ValueError("Road failure coordinates must be finite")
        points.append(tuple(round(value, 2) for value in values))
    return json.dumps(sorted(points), separators=(",", ":"))


def load_failures(path: Path, instance_id: str) -> dict[str, Any]:
    if not instance_id.strip():
        raise ValueError("Live city identity is required for road failure history")
    empty = {"version": 1, "instanceId": instance_id, "failures": []}
    if not path.exists():
        return empty
    state = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(state, dict) or state.get("version") != 1:
        raise ValueError("Invalid road failure history; preserve and inspect it before building")
    if state.get("instanceId") != instance_id:
        return empty
    failures = state.get("failures")
    if not isinstance(failures, list) or len(failures) > 100:
        raise ValueError("Invalid road failure list")
    for failure in failures:
        if not isinstance(failure, dict) or not isinstance(failure.get("key"), str):
            raise ValueError("Invalid road failure record")
        evidence = failure.get("evidence")
        if not isinstance(evidence, dict) or not isinstance(evidence.get("recovery", {}), dict):
            raise ValueError("Invalid road failure recovery evidence")
        try:
            candidate_key = geometry_key(failure["candidate"])
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            raise ValueError("Invalid road failure candidate; preserve and inspect history") from error
        if candidate_key != failure["key"]:
            raise ValueError("Road failure key does not match its candidate")
    return state


def record_failure(path: Path, state: dict[str, Any], candidate: dict[str, Any],
                   evidence: dict[str, Any]) -> None:
    key = geometry_key(candidate)
    failures = [entry for entry in state["failures"] if entry["key"] != key]
    failures.append({"key": key, "candidate": candidate, "evidence": evidence, "gatePolicy": GATE_POLICY})
    updated = {**state, "failures": failures[-100:]}
    _write(path, updated)


def clear_successful_attempt(path: Path, state: dict[str, Any]) -> None:
    # Restore the previous failures after this one operation settles. The
    # pre-request intent remains on disk if the process dies mid-request.
    _write(path, state)


def _write(path: Path, state: dict[str, Any]) -> None:
    payload = json.dumps(state, indent=2, ensure_ascii=False, allow_nan=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp", delete=False) as output:
            temporary = output.name
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def rejected_keys(state: dict[str, Any]) -> set[str]:
    return {entry["key"] for entry in state["failures"]}


def recovery_verified(entry: dict[str, Any]) -> bool:
    """Accept either exact local rollback or an externally verified checkpoint restore."""
    recovery = entry.get("evidence", {}).get("recovery", {})
    if not isinstance(recovery, dict):
        return False
    return (
        recovery.get("localRollbackSucceeded") is True
        or recovery.get("checkpointRestoreVerified") is True
        or recovery.get("noConstructionAttempted") is True
    )


def is_rejected(candidate: dict[str, Any], state: dict[str, Any], tolerance: float = 8.0) -> bool:
    """Conservative retry policy, not a claim about native collision distance."""
    def distance(first, second):
        return math.hypot(float(first["x"]) - float(second["x"]),
                          float(first["z"]) - float(second["z"]))

    key = geometry_key(candidate)
    for entry in state["failures"]:
        if entry.get("gatePolicy", 1) != GATE_POLICY:
            continue  # judged under other gates (an unrecovered one still blocks: see recovery_verified)
        previous = entry.get("candidate", {})
        recovery = entry.get("evidence", {}).get("recovery", {})
        if (candidate.get("allowTerrainCut") is True and previous.get("allowTerrainCut") is not True
                and isinstance(recovery, dict) and recovery.get("noConstructionAttempted") is True):
            # refused before construction under the raw-clearance model; a cut is judged by
            # the carved final surface instead, so it is a different attempt, not a retry
            continue
        if entry["key"] == key:
            return True
        previous = entry.get("candidate", {})
        if "start" not in previous or "end" not in previous:
            continue
        for start, end in (("start", "end"), ("end", "start")):
            if (distance(candidate["start"], previous[start]) <= tolerance and
                    distance(candidate["end"], previous[end]) <= tolerance):
                return True
    return False

"""Version-one mayor memory persistence and live-city scoping."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


DEFAULT_CITY_VISION = (
    "Build a financially stable, safe, accessible base-game city that grows "
    "through evidence-backed incremental changes."
)


def default_memory(fiscal_cooldown_minutes: int) -> dict[str, Any]:
    return {
        "version": 1,
        "cityIdentity": {"instanceId": "", "name": "", "map": ""},
        "cityVision": DEFAULT_CITY_VISION,
        "phase": "bootstrap",
        "activeGoals": [],
        "recentActions": [],
        "blockedGoals": [],
        "cooldowns": {},
        "financialPolicy": {
            "growthPriority": True,
            "fiscalCooldownMinutes": fiscal_cooldown_minutes,
            "maxFiscalActionsPerWake": 1,
        },
        "lastSnapshot": {},
        "updatedAt": datetime.now(timezone.utc).isoformat(),
    }


def load_memory(path: Path, default_factory: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    if not path.exists():
        return default_factory()
    state = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(state, dict) or state.get("version") != 1:
        raise ValueError("Unsupported mayor memory version")
    return state


def city_identity(snapshot: dict[str, Any]) -> dict[str, str]:
    if not isinstance(snapshot, dict) or snapshot.get("ok") is False:
        raise RuntimeError("The live city snapshot failed; refusing to change mayor memory.")
    city = snapshot.get("city", {})
    instance_id = city.get("instanceId") if isinstance(city, dict) else None
    if not isinstance(instance_id, str) or not instance_id.strip():
        raise RuntimeError("The live city instanceId is unavailable; refusing to change mayor memory.")
    return {
        "instanceId": instance_id.strip(),
        "name": str(city.get("name", "")).strip(),
        "map": str(city.get("map", "")).strip(),
    }


def _live_identity(
    request: Callable[..., Any], identity: Callable[[], dict[str, str]] | None
) -> dict[str, str]:
    """The live city's identity: from the caller when it already knows it (a loop wake remembers
    the city it read; the snapshot that reports it costs seconds of game thread), else read."""
    if identity is not None:
        return identity()
    return city_identity(request("GET", "/state/mayor-snapshot", query={"detailLimit": 0}))


def load_city_scoped_memory(
    request: Callable[..., Any],
    load: Callable[[], dict[str, Any]],
    save: Callable[[dict[str, Any]], dict[str, Any]],
    default_factory: Callable[[], dict[str, Any]],
    identity: Callable[[], dict[str, str]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    live_identity = _live_identity(request, identity)
    state = load()
    previous_identity = dict(state.get("cityIdentity", {}))
    reset = previous_identity.get("instanceId") != live_identity["instanceId"]
    if reset:
        state = default_factory()
        state["cityIdentity"] = live_identity
        save(state)
    elif state.get("cityIdentity") != live_identity:
        state["cityIdentity"] = live_identity
        save(state)
    return state, {
        "resetForNewCity": reset,
        "previousCityIdentity": previous_identity if reset else None,
        "liveCityIdentity": live_identity,
    }


def save_city_scoped_memory(
    state: dict[str, Any],
    request: Callable[..., Any],
    save: Callable[[dict[str, Any]], dict[str, Any]],
    identity: Callable[[], dict[str, str]] | None = None,
) -> dict[str, Any]:
    live_identity = _live_identity(request, identity)
    requested_identity = state.get("cityIdentity", {})
    requested_instance = str(requested_identity.get("instanceId", "")).strip()
    if requested_instance and requested_instance != live_identity["instanceId"]:
        raise RuntimeError("Mayor memory belongs to a different city instance; reload memory before saving.")
    scoped = dict(state)
    scoped["cityIdentity"] = live_identity
    return save(scoped)


def save_memory(
    path: Path,
    state: dict[str, Any],
    default_factory: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    state = dict(state)
    defaults = default_factory()
    state["version"] = 1
    state["updatedAt"] = datetime.now(timezone.utc).isoformat()
    for required, default in (
        ("cityVision", defaults["cityVision"]),
        ("cityIdentity", {"instanceId": "", "name": "", "map": ""}),
        ("phase", "bootstrap"),
        ("activeGoals", []),
        ("recentActions", []),
        ("blockedGoals", []),
        ("cooldowns", {}),
        ("financialPolicy", defaults["financialPolicy"]),
        ("lastSnapshot", {}),
    ):
        state.setdefault(required, default)
    state["recentActions"] = state["recentActions"][-100:]
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix="mayor-state-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(state, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return {"ok": True, "path": str(path), "state": state}

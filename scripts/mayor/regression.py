"""Durable, city-scoped regression baselines for restart recovery checks."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


VERSION = 1
MAX_BASELINES = 100


def _load_store(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": VERSION, "baselines": {}}
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict) or value.get("version") != VERSION:
        raise ValueError("Unsupported regression baseline store version")
    if not isinstance(value.get("baselines"), dict):
        raise ValueError("Invalid regression baseline store")
    return value


def _write_store(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(
        prefix="regression-baselines-", suffix=".json", dir=str(path.parent)
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_baseline(
    path: Path,
    name: str,
    city_identity: dict[str, str],
    baseline: dict[str, Any],
) -> dict[str, Any]:
    store = _load_store(path)
    records = store["baselines"]
    records[name] = {
        "cityIdentity": dict(city_identity),
        "capturedAtUtc": datetime.now(timezone.utc).isoformat(),
        "baseline": baseline,
    }
    if len(records) > MAX_BASELINES:
        oldest = sorted(
            records,
            key=lambda key: str(records[key].get("capturedAtUtc", "")),
        )[: len(records) - MAX_BASELINES]
        for key in oldest:
            records.pop(key, None)
    _write_store(path, store)
    return records[name]


def load_baseline(path: Path, name: str) -> dict[str, Any] | None:
    record = _load_store(path)["baselines"].get(name)
    if record is None:
        return None
    if not isinstance(record, dict) or not isinstance(record.get("baseline"), dict):
        raise ValueError("Invalid persisted regression baseline")
    return record


def compare(name: str, baseline: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    def number(source: dict[str, Any], key: str) -> float:
        value = source.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"Regression snapshot is missing numeric field: {key}")
        return value

    def headroom(source: dict[str, Any], service: str) -> float:
        value = source.get(service)
        if not isinstance(value, dict):
            raise ValueError(f"Regression snapshot is missing service field: {service}")
        return number(value, "capacity") - number(value, "used")

    before_progression = baseline.get("progression")
    after_progression = current.get("progression")
    if not isinstance(before_progression, dict) or not isinstance(after_progression, dict):
        raise ValueError("Regression snapshot is missing progression evidence")
    return {
        "ok": True,
        "name": name,
        "baseline": baseline,
        "current": current,
        "delta": {
            "cash": number(current, "cash") - number(baseline, "cash"),
            "periodNet": number(current, "periodNet") - number(baseline, "periodNet"),
            "population": number(current, "population") - number(baseline, "population"),
            "problemEntities": number(current, "problemEntities")
            - number(baseline, "problemEntities"),
            "roadSegments": number(current, "roadSegments")
            - number(baseline, "roadSegments"),
            "trafficDensity": round(
                number(current, "trafficDensity") - number(baseline, "trafficDensity"), 6
            ),
            "progressionChanged": after_progression != before_progression,
            "electricityHeadroom": headroom(current, "electricity")
            - headroom(baseline, "electricity"),
            "waterHeadroom": headroom(current, "water") - headroom(baseline, "water"),
            "sewageHeadroom": headroom(current, "sewage") - headroom(baseline, "sewage"),
        },
    }

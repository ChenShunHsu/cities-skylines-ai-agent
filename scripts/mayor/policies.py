"""Replayable Policy-as-Code metrics and read-only alert candidates.

The controller owns every value in this module.  Configuration contains only
threshold data; formulas remain typed Python functions and no expression from a
policy file is evaluated.
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_POLICY_PATH = ROOT / "references" / "mayor-policies-v1.json"
UTILITY_NAMES = ("electricity", "water", "sewage")
DEMAND_NAMES = ("residential", "commercial", "workplace")
ZONE_GROUPS = {
    "residential": ("ResidentialLow", "ResidentialHigh"),
    "commercial": ("CommercialLow", "CommercialHigh"),
    "workplace": ("Industrial", "Office"),
}
UTILITY_SERVICE_NAMES = {
    "electricity": "Electricity",
    "water": "Water",
    "sewage": "Sewage",
}
SEVERITY_ORDER = {"critical": 4, "high": 3, "medium": 2, "low": 1}


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) and value.get("ok") is not False else {}


def _number(value: Any, *, nonnegative: bool = False) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if math.isfinite(number) and (not nonnegative or number >= 0):
            return number
    return None


def _snapshot(value: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    value = _object(value)
    return _object(value.get("mayorSnapshot", value))


def _parse_game_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _metadata(
    snapshot: dict[str, Any], policy_version: str, evidence_ref: str, freshness: str
) -> dict[str, Any]:
    city = _object(snapshot.get("city"))
    capture = _object(snapshot.get("snapshot"))
    return {
        "instanceId": city.get("instanceId"),
        "observedAtGameTime": capture.get("gameTime"),
        "freshness": freshness,
        "policyVersion": policy_version,
        "evidenceRef": evidence_ref,
    }


def _known(value: Any, sources: list[str], metadata: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "status": "known",
        "value": value,
        "sourceFields": sources,
        **metadata,
        **extra,
    }


def _unknown(reason: str, sources: list[str], metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "unknown",
        "value": None,
        "reasonCode": reason,
        "sourceFields": sources,
        **metadata,
    }


def load_policy_config(path: Path = DEFAULT_POLICY_PATH) -> dict[str, Any]:
    """Load threshold-only policy data and reject executable or malformed values."""

    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict) or not isinstance(value.get("policyVersion"), str):
        raise ValueError("policy config requires a string policyVersion")
    policies = value.get("policies")
    if not isinstance(policies, list) or not policies:
        raise ValueError("policy config requires a non-empty policies list")
    expected = {
        "id", "metric", "enterWhen", "exitWhen", "consecutiveSamples",
        "cooldownGameDays", "severity", "candidateActionClass", "recheckCondition",
    }
    identifiers: set[str] = set()
    for index, policy in enumerate(policies):
        if not isinstance(policy, dict) or set(policy) != expected:
            raise ValueError(f"policy[{index}] has unexpected fields")
        policy_id = policy.get("id")
        if not isinstance(policy_id, str) or not policy_id or policy_id in identifiers:
            raise ValueError(f"policy[{index}] has an invalid or duplicate id")
        identifiers.add(policy_id)
        if policy.get("severity") not in SEVERITY_ORDER:
            raise ValueError(f"policy[{index}] has an invalid severity")
        if policy.get("candidateActionClass") != "read_only_inspection":
            raise ValueError(f"policy[{index}] may only create read-only inspection candidates")
        if not isinstance(policy.get("consecutiveSamples"), int) or policy["consecutiveSamples"] < 1:
            raise ValueError(f"policy[{index}] requires a positive consecutiveSamples")
        if _number(policy.get("cooldownGameDays"), nonnegative=True) is None:
            raise ValueError(f"policy[{index}] requires a finite cooldownGameDays")
        for gate_name, expected_operator in (("enterWhen", "gte"), ("exitWhen", "lte")):
            gate = policy.get(gate_name)
            if not isinstance(gate, dict) or set(gate) != {"operator", "value"}:
                raise ValueError(f"policy[{index}].{gate_name} is malformed")
            if gate.get("operator") != expected_operator or _number(gate.get("value")) is None:
                raise ValueError(f"policy[{index}].{gate_name} is invalid")
        if policy["exitWhen"]["value"] >= policy["enterWhen"]["value"]:
            raise ValueError(f"policy[{index}] requires hysteresis between exit and enter")
    return value


def derive_metrics(
    snapshot: dict[str, Any],
    previous: dict[str, Any] | None = None,
    *,
    zones: dict[str, Any] | None = None,
    reserve_display: float = 10_000,
    policy_version: str = "mayor-policies-v1",
    evidence_ref: str = "bridge://observe_city/current",
    freshness: str = "current_call",
) -> dict[str, Any]:
    """Derive comparable controller metrics without authorizing an action."""

    if _number(reserve_display, nonnegative=True) is None:
        raise ValueError("reserve_display must be a finite non-negative number")
    current = _snapshot(snapshot)
    prior = _snapshot(previous)
    metadata = _metadata(current, policy_version, evidence_ref, freshness)
    health = _object(current.get("cityHealth"))
    utilities = _object(health.get("utilities"))
    cash = _object(health.get("cash"))
    summary = _object(current.get("summary"))

    utility_metrics: dict[str, Any] = {}
    for name in UTILITY_NAMES:
        fields = [f"cityHealth.utilities.{name}.used", f"cityHealth.utilities.{name}.capacity"]
        raw = _object(utilities.get(name))
        used = _number(raw.get("used"), nonnegative=True)
        capacity = _number(raw.get("capacity"), nonnegative=True)
        if used is None or capacity is None:
            utility_metrics[name] = {
                "utilization": _unknown("missing_or_invalid_utility_telemetry", fields, metadata),
                "headroom": _unknown("missing_or_invalid_utility_telemetry", fields, metadata),
            }
        elif capacity == 0:
            utility_metrics[name] = {
                "utilization": _unknown("zero_capacity", fields, metadata),
                "headroom": _known(capacity - used, fields, metadata),
            }
        else:
            utility_metrics[name] = {
                "utilization": _known(used / capacity, fields, metadata),
                "headroom": _known(capacity - used, fields, metadata),
            }

    money_fields = ["cityHealth.cash.amount", "cityHealth.cash.periodNet"]
    raw_amount = _number(cash.get("amount"))
    raw_net = _number(cash.get("periodNet"))
    if raw_amount is None:
        cash_above_reserve = _unknown("missing_or_invalid_cash", [money_fields[0]], metadata)
    else:
        cash_above_reserve = _known(raw_amount / 100 - reserve_display, [money_fields[0]], metadata)
    if raw_amount is None or raw_net is None:
        deficit_runway = _unknown("missing_or_invalid_cash", money_fields, metadata)
    elif raw_net >= 0:
        deficit_runway = _known(None, money_fields, metadata, applicability="not_in_deficit")
    else:
        deficit_runway = _known(
            max(0.0, raw_amount / 100 - reserve_display) / abs(raw_net / 100),
            money_fields,
            metadata,
            unit="comparable_accounting_periods",
        )

    current_id = _object(current.get("city")).get("instanceId")
    prior_id = _object(prior.get("city")).get("instanceId")
    current_time = _parse_game_time(_object(current.get("snapshot")).get("gameTime"))
    prior_time = _parse_game_time(_object(prior.get("snapshot")).get("gameTime"))
    elapsed_days = None
    if current_id and current_id == prior_id and current_time and prior_time and current_time > prior_time:
        elapsed_days = (current_time - prior_time).total_seconds() / 86_400
    demand_metrics: dict[str, Any] = {}
    current_demand = _object(summary.get("demand"))
    prior_demand = _object(_object(prior.get("summary")).get("demand"))
    for name in DEMAND_NAMES:
        fields = [f"summary.demand.{name}", f"previous.summary.demand.{name}"]
        value = _number(current_demand.get(name), nonnegative=True)
        prior_value = _number(prior_demand.get(name), nonnegative=True)
        if elapsed_days is None:
            demand_metrics[name] = _unknown("snapshots_not_comparable", fields, metadata)
        elif value is None or prior_value is None:
            demand_metrics[name] = _unknown("missing_or_invalid_demand", fields, metadata)
        else:
            demand_metrics[name] = _known(
                (value - prior_value) / elapsed_days,
                fields,
                metadata,
                elapsedGameDays=elapsed_days,
            )

    zone_rows = zones.get("capacityByZone") if isinstance(zones, dict) else None
    frontage: dict[str, Any] = {}
    by_zone = {
        str(row.get("zone")): row
        for row in zone_rows or []
        if isinstance(row, dict) and isinstance(row.get("zone"), str)
    }
    for category, zone_names in ZONE_GROUPS.items():
        fields = [f"zones.capacityByZone[{name}]" for name in zone_names]
        if not isinstance(zone_rows, list):
            frontage[category] = _unknown("capacity_by_zone_unavailable", fields, metadata)
            continue
        occupied = 0.0
        vacant = 0.0
        valid = True
        found = False
        for zone_name in zone_names:
            row = by_zone.get(zone_name)
            if row is None:
                continue
            found = True
            row_occupied = _number(row.get("occupiedCells"), nonnegative=True)
            row_vacant = _number(row.get("vacantCells"), nonnegative=True)
            if row_occupied is None or row_vacant is None:
                valid = False
                break
            occupied += row_occupied
            vacant += row_vacant
        if not found or not valid:
            frontage[category] = _unknown("missing_or_invalid_zone_capacity", fields, metadata)
        elif occupied + vacant == 0:
            frontage[category] = _unknown("zero_zoned_capacity", fields, metadata)
        else:
            frontage[category] = _known(
                occupied / (occupied + vacant),
                fields,
                metadata,
                occupiedCells=occupied,
                vacantCells=vacant,
            )

    return {
        "policyVersion": policy_version,
        "mutationAuthorized": False,
        "utilities": utility_metrics,
        "cashAboveReserve": cash_above_reserve,
        "deficitRunway": deficit_runway,
        "demandTrendPerGameDay": demand_metrics,
        "frontageSaturation": frontage,
    }


def _utility_candidates(name: str) -> list[dict[str, Any]]:
    service = UTILITY_SERVICE_NAMES[name]
    coverage_tool = (
        "inspect_electricity_service_coverage"
        if name == "electricity"
        else "inspect_water_service_coverage"
    )
    return [
        {
            "candidateId": f"inspect-{name}-capacity",
            "toolName": "inspect_service_capacity",
            "arguments": {"service": service},
            "expectedEvidence": ["tool_result"],
        },
        {
            "candidateId": f"inspect-{name}-delivery",
            "toolName": coverage_tool,
            "arguments": {"limit": 50},
            "expectedEvidence": ["tool_result"],
        },
    ]


def evaluate_policies(
    metrics: dict[str, Any],
    config: dict[str, Any] | None = None,
    *,
    active_policy_ids: set[str] | frozenset[str] | None = None,
) -> dict[str, Any]:
    """Create alerts and fixed read-only candidates; never generate live identifiers."""

    config = config or load_policy_config()
    if metrics.get("policyVersion") != config.get("policyVersion"):
        raise ValueError("metric and policy versions differ")
    active_policy_ids = set(active_policy_ids or ())
    alerts: list[dict[str, Any]] = []
    for policy in config["policies"]:
        utility = policy["id"].removesuffix("_capacity_pressure")
        metric = _object(_object(metrics.get("utilities")).get(utility)).get("utilization")
        if not isinstance(metric, dict) or metric.get("status") != "known":
            continue
        value = _number(metric.get("value"))
        was_active = policy["id"] in active_policy_ids
        triggered = (
            value is not None
            and (
                value > policy["exitWhen"]["value"]
                if was_active
                else value >= policy["enterWhen"]["value"]
            )
        )
        if not triggered:
            continue
        alerts.append(
            {
                "policyId": policy["id"],
                "metric": policy["metric"],
                "severity": policy["severity"],
                "observed": {"utilization": value},
                "evidenceStatus": "known",
                "transition": "continued" if was_active else "entered",
                "enterWhen": policy["enterWhen"],
                "exitWhen": policy["exitWhen"],
                "consecutiveSamples": policy["consecutiveSamples"],
                "cooldownGameDays": policy["cooldownGameDays"],
                "recheckCondition": policy["recheckCondition"],
                "evidenceRef": metric["evidenceRef"],
                "candidates": _utility_candidates(utility),
            }
        )
    alerts.sort(key=lambda item: (-SEVERITY_ORDER[item["severity"]], item["policyId"]))
    unknown_metrics = []
    for name, values in _object(metrics.get("utilities")).items():
        metric = _object(values).get("utilization")
        if isinstance(metric, dict) and metric.get("status") == "unknown":
            unknown_metrics.append(
                {"metric": f"utilities.{name}.utilization", "reasonCode": metric.get("reasonCode")}
            )
    return {
        "policyVersion": config["policyVersion"],
        "mutationAuthorized": False,
        "alerts": alerts,
        "unknownMetrics": unknown_metrics,
    }


def compact_decision_packet(
    mission_id: str,
    stage: str,
    policy_result: dict[str, Any],
    *,
    remaining_steps: int,
) -> dict[str, Any]:
    """Return one alert and its controller-owned candidates for a local model."""

    alerts = policy_result.get("alerts")
    if not isinstance(alerts, list) or not alerts:
        return {
            "missionId": mission_id,
            "stage": stage,
            "alert": None,
            "candidates": [],
            "remainingSteps": remaining_steps,
            "terminalReason": "no_active_policy_alert",
            "forbidden": ["invent_arguments", "call_unlisted_tool", "change_policy"],
        }
    alert = alerts[0]
    return {
        "missionId": mission_id,
        "stage": stage,
        "alert": {key: value for key, value in alert.items() if key != "candidates"},
        "candidates": alert["candidates"],
        "remainingSteps": remaining_steps,
        "forbidden": ["invent_arguments", "call_unlisted_tool", "change_policy"],
    }

"""Fail-closed contracts for a bounded local-model mayor executor.

This module validates model output and mission scope only. It does not grant
mutation authority or replace native preview, checkpoint, settlement, focused
QA, regression, recovery, or save gates.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from . import schema as mayor_schema


ROOT = Path(__file__).resolve().parents[2]
REFERENCES = ROOT / "references"
TOOL_DEFINITIONS_PATH = REFERENCES / "mayor-tools.json"
TOOL_CONTRACT_FIXTURE = ROOT / "scripts" / "fixtures" / "mayor-tools-contract-v1.json"

SCHEMA_PATHS = {
    "mission": REFERENCES / "mayor-mission-contract-v1.schema.json",
    "action": REFERENCES / "mayor-action-envelope-v1.schema.json",
    "progress": REFERENCES / "mayor-progress-summary-v1.schema.json",
    "escalation": REFERENCES / "mayor-escalation-summary-v1.schema.json",
}
MAX_MODEL_OUTPUT_CHARS = 65_536

READ_ONLY_TOOLS = frozenset(
    {
        "inspect_zone_cell_geometry",
        "assess_city",
        "inspect_economy",
        "observe_city",
        "inspect_road_layers_3d",
        "inspect_grade_separated_crossings",
        "inspect_building_layers_3d",
        "inspect_building_road_clearance_3d",
        "inspect_planning_area",
        "propose_development_plan",
        "propose_frontage_road",
        "validate_progression_mode",
        "inspect_toolbar_unlocks",
        "inspect_service_coverage",
        "inspect_service_capacity",
        "inspect_facility_influence",
        "inspect_growth_capacity",
        "propose_zoning_blocks",
        "propose_expansion_areas",
        "propose_network_crossing",
        "inspect_building_zoning_support",
        "inspect_traffic",
        "assess_traffic_congestion",
        "propose_traffic_remedies",
        "propose_bypass_roads",
        "inspect_segment_routes",
        "inspect_junction",
        "inspect_road_result",
        "inspect_transport",
        "propose_transit_lines",
        "list_unlocked_assets",
        "preview_service_impact",
        "assess_problem_remedies",
        "compare_service_options",
        "propose_service_sites",
        "preview_network",
        "propose_outside_road_connection",
        "propose_water_pair_candidates",
        "propose_groundwater_pump_sites",
        "inspect_water_pair",
        "propose_water_service_connection",
        "preview_water_system_plan",
        "inspect_water_service_coverage",
        "inspect_electricity_connection_area",
        "propose_power_line_connection",
        "propose_minimal_power_grid",
        "inspect_power_grid_efficiency",
        "inspect_electricity_service_coverage",
        "diagnose_utility_shortfall",
        "diagnose_noise",
        "inspect_wind_resource_map",
        "propose_power_plant_candidates",
        "preview_power_plant_candidate",
        "inspect_power_plant_operation",
        "preview_road_upgrade",
        "preview_road_reversal",
        "inspect_water_network_topology",
        "verify_regression",
        "preview_building_candidate",
        "preview_bulldoze_impact",
        "preview_operational_change",
        "preview_transport_plan",
        "preview_district_specialization",
        "load_mayor_memory",
        "score_candidates",
        "inspect_terrain_editing",
    }
)

TOOL_ACTION_KINDS = {
    "create_checkpoint": "checkpoint",
    "capture_regression_baseline": "checkpoint",
    "set_simulation_speed": "simulation_control",
    "upgrade_road": "modify_road",
    "reverse_road": "modify_road",
    "split_road_segment": "modify_road",
    "set_junction_control": "modify_road",
    "trial_junction_control": "modify_road",
    "widen_road_chain": "modify_road",
    "place_service": "place_service",
    "rebuild_full_facility": "place_service",
    "place_power_plant": "place_power",
    "place_water_facility": "place_water",
    "build_network": "build_network",
    "connect_water_facility": "build_water",
    "build_water_pipe": "build_water",
    "build_water_service_pipe": "build_water",
    "zone_block": "zone",
    "repair_building_zoning": "zone",
    "set_service_budget": "change_service_budget",
    "set_tax_rate": "change_tax",
    "take_loan": "change_loan",
    "purchase_map_area": "purchase_area",
    "create_transport_line": "change_transport",
    "set_transport_line": "change_transport",
    "delete_transport_line": "change_transport",
    "bulldoze_entity": "bulldoze",
    "clear_abandoned_buildings": "bulldoze",
    "set_building_active": "change_building_state",
    "create_district": "change_specialization",
    "set_district_specialization": "change_specialization",
    "save_city": "save_city",
    "save_mayor_memory": "memory_write",
}


@dataclass(frozen=True)
class ExecutorContext:
    """Authoritative state supplied by the deterministic controller."""

    instance_id: str
    tool_contract_version: str
    registered_tool_names: frozenset[str]
    game_time: datetime | str
    completed_steps: int = 0
    spent_display: float = 0.0


def load_contract_schema(kind: str) -> dict[str, Any]:
    try:
        path = SCHEMA_PATHS[kind]
    except KeyError as exc:
        raise ValueError(f"Unknown executor contract kind: {kind}") from exc
    return json.loads(path.read_text(encoding="utf-8-sig"))


def repository_tool_definitions() -> list[dict[str, Any]]:
    return mayor_schema.load_definitions(TOOL_DEFINITIONS_PATH)


def repository_tool_contract_version() -> str:
    fixture = json.loads(TOOL_CONTRACT_FIXTURE.read_text(encoding="utf-8-sig"))
    return f"mayor-tools-v{fixture['version']}:{fixture['canonicalSha256']}"


def repository_tool_action_kinds() -> dict[str, str]:
    names = {
        item["function"]["name"] for item in repository_tool_definitions()
    }
    classified = READ_ONLY_TOOLS | TOOL_ACTION_KINDS.keys()
    missing = names - classified
    stale = classified - names
    if missing or stale:
        details = []
        if missing:
            details.append("unclassified=" + ",".join(sorted(missing)))
        if stale:
            details.append("unknown=" + ",".join(sorted(stale)))
        raise RuntimeError("Mayor tool action policy is out of sync: " + "; ".join(details))
    return {name: TOOL_ACTION_KINDS.get(name, "read_only") for name in names}


def _validate_document(kind: str, value: Any) -> None:
    contract_schema = load_contract_schema(kind)
    mayor_schema.validate_schema(value, contract_schema, contract_schema, kind)


def parse_model_document(raw: str, kind: str) -> dict[str, Any]:
    """Parse one bounded JSON object and reject duplicate keys or trailing text."""

    if not isinstance(raw, str):
        raise ValueError("local model output must be text")
    if len(raw) > MAX_MODEL_OUTPUT_CHARS:
        raise ValueError("local model output exceeds the bounded character limit")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"local model output contains duplicate key: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(raw.lstrip("\ufeff"), object_pairs_hook=reject_duplicates)
    except json.JSONDecodeError as exc:
        raise ValueError("local model output is malformed JSON") from exc
    _validate_document(kind, value)
    return value


def _parse_game_time(value: datetime | str, path: str) -> datetime:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        raise ValueError(f"{path} must be an ISO-8601 string or datetime")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{path} must be a valid ISO-8601 game time") from exc


def _ensure_comparable_times(left: datetime, right: datetime) -> None:
    if (left.tzinfo is None) != (right.tzinfo is None):
        raise ValueError("contract and controller game times must use compatible timezone forms")


def _validate_evidence_refs(refs: list[str], path: str) -> None:
    for index, reference in enumerate(refs):
        if not reference.startswith("artifact://") or any(char.isspace() for char in reference):
            raise ValueError(f"{path}[{index}] must be a whitespace-free artifact:// reference")


def validate_mission_contract(
    contract: dict[str, Any],
    context: ExecutorContext,
    *,
    tool_action_kinds: Mapping[str, str] | None = None,
    require_active: bool = True,
) -> None:
    """Validate schema, city scope, tool surface, budgets, and expiry."""

    _validate_document("mission", contract)
    policies = dict(tool_action_kinds or repository_tool_action_kinds())
    if contract["instanceId"] != context.instance_id:
        raise ValueError("mission.instanceId does not match the live city instance")
    if contract["toolContractVersion"] != context.tool_contract_version:
        raise ValueError("mission.toolContractVersion does not match the controller")
    if context.completed_steps < 0:
        raise ValueError("controller completed steps must be non-negative")
    if require_active and context.completed_steps >= contract["maxExecutorSteps"]:
        raise ValueError("mission executor step budget is exhausted")
    if not math.isfinite(context.spent_display) or context.spent_display < 0:
        raise ValueError("controller spent display amount must be finite and non-negative")
    if require_active and context.spent_display > contract["budgetLimitDisplay"]:
        raise ValueError("mission display-cost budget is exhausted")

    expires = _parse_game_time(contract["expiresAtGameTime"], "mission.expiresAtGameTime")
    current = _parse_game_time(context.game_time, "context.game_time")
    _ensure_comparable_times(expires, current)
    if require_active and current >= expires:
        raise ValueError("mission contract is expired at the current game time")

    registered = set(context.registered_tool_names)
    unknown = set(contract["allowedToolNames"]) - registered
    if unknown:
        raise ValueError("mission allows unregistered tools: " + ", ".join(sorted(unknown)))
    unclassified = set(contract["allowedToolNames"]) - set(policies)
    if unclassified:
        raise ValueError("mission allows tools without action policy: " + ", ".join(sorted(unclassified)))
    forbidden = set(contract["forbiddenActions"])
    contradictions = sorted(
        name for name in contract["allowedToolNames"] if policies[name] in forbidden
    )
    if contradictions:
        raise ValueError(
            "mission tool allowlist contradicts forbidden actions: " + ", ".join(contradictions)
        )


def validate_action_envelope(
    envelope: dict[str, Any],
    contract: dict[str, Any],
    context: ExecutorContext,
    *,
    tool_definitions: list[dict[str, Any]] | None = None,
    tool_action_kinds: Mapping[str, str] | None = None,
) -> None:
    """Validate one local-model decision without executing it."""

    policies = dict(tool_action_kinds or repository_tool_action_kinds())
    validate_mission_contract(contract, context, tool_action_kinds=policies)
    _validate_document("action", envelope)
    for field in ("missionId", "instanceId", "toolContractVersion"):
        if envelope[field] != contract[field]:
            raise ValueError(f"action.{field} does not match the mission contract")
    if envelope["step"] != context.completed_steps + 1:
        raise ValueError("action.step must be the next sequential executor step")

    decision = envelope["decision"]
    tool_fields = {"toolName", "arguments", "expectedEvidence", "onMissingEvidence", "estimatedCostDisplay"}
    present_tool_fields = tool_fields & envelope.keys()
    if decision != "call_tool":
        if present_tool_fields:
            raise ValueError("non-tool decisions must not include tool-call fields")
        if "reasonCode" not in envelope:
            raise ValueError("non-tool decisions require reasonCode")
        if decision == "complete" and envelope["reasonCode"] != "mission_complete":
            raise ValueError("complete decision requires mission_complete reason")
        if decision == "wait" and envelope["reasonCode"] not in {
            "awaiting_settlement",
            "stage_complete",
        }:
            raise ValueError("wait decision requires a bounded wait reason")
        if decision == "escalate" and envelope["reasonCode"] not in contract["escalateWhen"]:
            raise ValueError("action escalation reason is not declared by the mission")
        return

    missing = tool_fields - envelope.keys()
    if missing:
        raise ValueError("call_tool action is missing: " + ", ".join(sorted(missing)))
    if "reasonCode" in envelope:
        raise ValueError("call_tool action must not include reasonCode")
    tool_name = envelope["toolName"]
    if tool_name not in contract["allowedToolNames"]:
        raise ValueError(f"action tool is outside the mission allowlist: {tool_name}")
    action_kind = policies.get(tool_name)
    if action_kind is None:
        raise ValueError(f"action tool has no controller policy: {tool_name}")
    if action_kind in contract["forbiddenActions"]:
        raise ValueError(f"action kind is forbidden by the mission: {action_kind}")
    projected = context.spent_display + envelope["estimatedCostDisplay"]
    if projected > contract["budgetLimitDisplay"]:
        raise ValueError("action would exceed the mission display-cost budget")
    definitions = tool_definitions or repository_tool_definitions()
    mayor_schema.validate_arguments(definitions, tool_name, envelope["arguments"])


def validate_progress_summary(
    summary: dict[str, Any], contract: dict[str, Any], context: ExecutorContext
) -> None:
    validate_mission_contract(contract, context, require_active=False)
    _validate_document("progress", summary)
    for field in ("missionId", "instanceId", "toolContractVersion"):
        if summary[field] != contract[field]:
            raise ValueError(f"progress.{field} does not match the mission contract")
    if summary["executorSteps"] != context.completed_steps:
        raise ValueError("progress.executorSteps does not match controller state")
    if summary["currentStage"] != contract["currentStage"]:
        raise ValueError("progress.currentStage does not match the mission contract")
    expected_remaining_steps = contract["maxExecutorSteps"] - context.completed_steps
    if summary["remainingSteps"] != expected_remaining_steps:
        raise ValueError("progress.remainingSteps does not match the mission budget")
    if not math.isclose(summary["spentDisplay"], context.spent_display, abs_tol=0.005):
        raise ValueError("progress.spentDisplay does not match controller state")
    expected_remaining_cost = contract["budgetLimitDisplay"] - context.spent_display
    if not math.isclose(summary["remainingBudgetDisplay"], expected_remaining_cost, abs_tol=0.005):
        raise ValueError("progress.remainingBudgetDisplay does not match the mission budget")
    _validate_evidence_refs(summary["evidenceRefs"], "progress.evidenceRefs")


def validate_escalation_summary(
    summary: dict[str, Any], contract: dict[str, Any], context: ExecutorContext
) -> None:
    validate_mission_contract(contract, context, require_active=False)
    _validate_document("escalation", summary)
    for field in ("missionId", "instanceId", "toolContractVersion"):
        if summary[field] != contract[field]:
            raise ValueError(f"escalation.{field} does not match the mission contract")
    if summary["executorSteps"] != context.completed_steps:
        raise ValueError("escalation.executorSteps does not match controller state")
    if summary["blockerCode"] not in contract["escalateWhen"]:
        raise ValueError("escalation blocker is not declared by the mission")
    _validate_evidence_refs(summary["evidenceRefs"], "escalation.evidenceRefs")

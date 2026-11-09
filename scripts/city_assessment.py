"""Pure, replayable decision brief for CS1. No network, memory writes or simulation."""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

from mayor import gameplay_rules
from mayor import policies as policy_engine

# Capability that must be unlocked to build each service level; school levels unlock at
# later milestones than the education service. Older Bridges report only "education".
SERVICE_CAPABILITIES = {
    "healthcare": ("healthcare", None),
    "elementaryEducation": ("education", None),
    "highSchoolEducation": ("highSchool", "education"),
    "universityEducation": ("university", "education"),
}


def _object(value: Any) -> dict:
    return value if isinstance(value, dict) and value.get("ok") is not False else {}


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return float(value) if math.isfinite(value) else None
        except OverflowError:
            pass
    return None


def _game_time(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    return None


def _snapshot(value: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Expected a mayor snapshot JSON object")
    value = _object(value)
    return _object(value.get("mayorSnapshot", value))


def _copy_evidence(value: Any) -> Any:
    """Keep replay baselines valid JSON even when malformed telemetry uses NaN."""
    if isinstance(value, dict):
        return {key: _copy_evidence(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copy_evidence(item) for item in value]
    if isinstance(value, (int, float)) and not isinstance(value, bool) and _number(value) is None:
        return None
    return value


def assess_city(snapshot: dict, previous: dict | None = None, *, reserve_display: float = 10000) -> dict:
    """Assess evidence, not construction permission. Thresholds are initial policy defaults."""
    if _number(reserve_display) is None or reserve_display < 0:
        raise ValueError("reserve_display must be a finite nonnegative number")
    current = _snapshot(snapshot)
    health = _object(current.get("cityHealth"))
    summary = _object(current.get("summary"))
    city = _object(current.get("city"))
    clock = _object(current.get("snapshot"))
    cash = _object(health.get("cash"))
    wellbeing = _object(health.get("wellbeing"))
    problems = _object(current.get("problems"))
    counts = _object(problems.get("countsByProblem"))
    missing: list[str] = []
    objectives: list[dict] = []

    def metric(value: Any, path: str, *, nonnegative: bool = False) -> float | None:
        number = _number(value)
        if number is not None and nonnegative and number < 0:
            number = None
        if number is None:
            missing.append(path)
        return number

    def objective(name: str, priority: int, reason: str, tools: list[str], target: str) -> None:
        objectives.append({"id": name, "priority": priority, "reason": reason,
                           "inspectTools": tools, "successCondition": target})

    capabilities = _object(_object(current.get("progression")).get("capabilities"))

    def locked_objective(name: str, capability: str, reason: str) -> None:
        # A locked service cannot be built, so it must not outrank actionable
        # work. Only an explicit false demotes it; unknown stays fail-closed.
        objectives.append({"id": name, "priority": 30, "blockedBy": "locked",
                           "reason": f"{reason} The {capability} capability is locked at the current milestone; it cannot be built yet.",
                           "inspectTools": ["inspect_toolbar_unlocks"],
                           "successCondition": f"The {capability} capability and its toolbar button are unlocked before planning facilities"})

    # Read raw money only: never double-convert previously annotated snapshots.
    raw_amount = metric(cash.get("amount"), "cityHealth.cash.amount")
    raw_net = metric(cash.get("periodNet"), "cityHealth.cash.periodNet")
    amount = None if raw_amount is None else raw_amount / 100
    net = None if raw_net is None else raw_net / 100
    population = metric(summary.get("population"), "summary.population", nonnegative=True)
    happiness = metric(wellbeing.get("happiness"), "cityHealth.wellbeing.happiness", nonnegative=True)
    unemployment = metric(_object(health.get("employment")).get("unemployment"), "cityHealth.employment.unemployment", nonnegative=True)
    total_problems = metric(problems.get("total"), "problems.total", nonnegative=True)
    counts_valid = isinstance(problems.get("countsByProblem"), dict) and not any(
        _number(value) is None or value < 0 for value in counts.values()
    ) and problems["countsByProblem"].get("ok") is not False
    if not counts_valid:
        missing.append("problems.countsByProblem")
    instance_id = city.get("instanceId")
    identity_valid = isinstance(instance_id, str) and bool(instance_id.strip())
    if not identity_valid:
        missing.append("city.instanceId")
    current_time = _game_time(clock.get("gameTime"))
    if current_time is None:
        missing.append("snapshot.gameTime")

    # Growth requires two observations of the SAME city at increasing simulation times.
    prior = _snapshot(previous) if previous is not None else {}
    comparable = False
    same_observation_time = False
    prior_time = None
    comparison_reason = "No previous snapshot supplied"
    if prior:
        comparison_reason = "City identity differs or simulation time did not advance"
        try:
            same_city = identity_valid and instance_id == _object(prior.get("city")).get("instanceId")
            prior_time = _game_time(_object(prior.get("snapshot")).get("gameTime"))
            if same_city and current_time is not None and prior_time is not None:
                comparable = current_time > prior_time
                same_observation_time = current_time == prior_time
        except (KeyError, TypeError, ValueError):
            comparable = False
        if comparable:
            comparison_reason = (
                "Same city, later simulation time; rates are normalized by elapsed "
                "game days but are not causal proof"
            )
    elapsed_game_days = (
        (current_time - prior_time).total_seconds() / 86400
        if comparable and current_time is not None and prior_time is not None
        else None
    )
    prior_population = _number(_object(prior.get("summary")).get("population")) if comparable else None
    if prior_population is not None and prior_population < 0:
        prior_population = None
    prior_net = _number(_object(_object(prior.get("cityHealth")).get("cash")).get("periodNet")) if comparable else None
    population_delta = population - prior_population if population is not None and prior_population is not None else None
    population_rate = (
        population_delta / elapsed_game_days
        if population_delta is not None and elapsed_game_days
        else None
    )
    sustained_deficit = net is not None and net < 0 and prior_net is not None and prior_net < 0

    utility_metrics = {}
    for name in ("electricity", "water", "sewage"):
        value = _object(_object(health.get("utilities")).get(name))
        used = metric(value.get("used"), f"cityHealth.utilities.{name}.used", nonnegative=True)
        capacity = metric(value.get("capacity"), f"cityHealth.utilities.{name}.capacity", nonnegative=True)
        ratio = used / capacity if used is not None and capacity is not None and capacity > 0 else None
        state = "unknown"
        if used is not None and capacity is not None:
            state = "insufficient" if used > capacity else "near-capacity" if ratio is not None and ratio >= 0.9 else "available" if capacity > 0 else "not-established"
        utility_metrics[name] = {"used": used, "capacity": capacity, "utilization": ratio, "state": state}
        capability = "electricity" if name == "electricity" else "water"
        if state in ("insufficient", "near-capacity", "not-established") and capabilities.get(capability) is False:
            locked_objective(name, capability, f"{name}: {state}; used={used}, capacity={capacity}.")
        elif state in ("insufficient", "near-capacity", "not-established"):
            objective(name, 100 if state == "insufficient" else 80,
                      f"{name}: {state}; used={used}, capacity={capacity}. Capacity does not prove delivery.",
                      ["inspect_electricity_service_coverage" if name == "electricity" else "inspect_water_service_coverage", "inspect_service_capacity"],
                      "Demanding buildings receive service and measured capacity has headroom")

    details = problems.get("problems")
    network_only = (
        total_problems is not None and total_problems > 0
        and isinstance(details, list) and len(details) == total_problems
        and all(isinstance(item, dict) and item.get("entityType") in ("netNode", "netSegment")
                for item in details)
    )
    if network_only:
        # A complete list of road-network flags (for example a fresh map's
        # unconnected highway stubs) is not a resident emergency.
        objective("network-problems", 60, f"{int(total_problems)} road-network entities report problems and no building does. Flags: {json.dumps(_copy_evidence(counts), sort_keys=True)}.",
                  ["observe_city", "inspect_road_result"],
                  "Network flags clear once the affected roads are connected, without new building problems")
    elif total_problems is not None and total_problems > 0:
        objective("resident-problems", 100, f"{int(total_problems)} entities report problems; counts are citywide, details may be truncated. Flags: {json.dumps(_copy_evidence(counts), sort_keys=True)}.",
                  ["observe_city", "inspect_service_coverage", "inspect_traffic"],
                  "Targeted problem count falls after simulation settles, without new regressions")
    # Ambulances, fire trucks and water trucks set this when their path to the building fails.
    road_access_failed = _number(counts.get("RoadAccessFailed"))
    if road_access_failed:
        road_anomalies = _object(current.get("roadAnomalies"))
        overlong_count = _number(_object(road_anomalies.get("counts")).get("overlongRoadSegment")) or 0
        overlong_ids = [item.get("segmentId") for item in road_anomalies.get("anomalies") or []
                        if isinstance(item, dict) and item.get("type") == "overlongRoadSegment"][:10]
        objective("road-access", 90, f"{int(road_access_failed)} buildings report RoadAccessFailed: service vehicles could not find a route to them. "
                  "Vehicles cannot find buildings near the middle of road segments longer than 160m; split_road_segment fixes those. "
                  f"Overlong road segments: {int(overlong_count)}{', including ' + json.dumps(overlong_ids) if overlong_ids else ''}.",
                  ["inspect_road_result"],
                  "RoadAccessFailed clears from the affected buildings after the simulation settles")
    # Garbage/deathcare aggregate incineration/cremation capacity excludes storage
    # alternatives, so it cannot establish a shortage on its own. Landfills and
    # cemeteries store instead: their combined fill level is the shortage signal.
    for group, name, capability, facility, facilities_label in (
            ("utilities", "garbage", "garbage", "landfill", "landfills"),
            ("services", "deathcare", "healthcare", "cemetery", "cemeteries")):
        storage = _object(_object(_object(health.get(group)).get(name)).get("storage"))
        stored = _number(storage.get("amount"))
        storage_capacity = _number(storage.get("capacity"))
        if stored is None or storage_capacity is None or stored < 0 or storage_capacity <= 0:
            continue  # older Bridge without storage telemetry, or no facility yet
        fill = stored / storage_capacity
        # Full facilities stop collecting: when most are full, the remaining headroom sits in
        # a few facilities whose vehicles cannot reach the whole city, whatever the combined fill.
        facilities = _number(storage.get("facilities"))
        full = _number(storage.get("fullFacilities"))
        mostly_full = facilities is not None and full is not None and facilities >= 2 and full * 2 >= facilities
        if fill < 0.85 and not mostly_full:
            continue
        reason = (f"{facility} storage is {fill:.0%} full ({stored:,.0f}/{storage_capacity:,.0f}); "
                  f"full {facilities_label} stop collecting.")
        if mostly_full:
            reason += f" {full:.0f} of {facilities:.0f} {facilities_label} are full."
        if capabilities.get(capability) is False:
            locked_objective(f"{facility}-storage", capability, reason)
            continue
        priority = 90 if fill >= 0.97 or (mostly_full and full == facilities) else 80 if mostly_full else 75
        action = (" Replace full ones (bulldozing discards their contents) or add storage near uncollected buildings."
                  if mostly_full else " Add storage before it runs out.")
        objective(f"{facility}-storage", priority, reason + action,
                  ["inspect_service_capacity", "list_unlocked_assets", "preview_service_impact", "preview_bulldoze_impact"],
                  f"Most {facilities_label} have headroom and collect again after the change settles")
    for name in ("healthcare", "elementaryEducation", "highSchoolEducation", "universityEducation"):
        service = _object(_object(health.get("services")).get(name))
        used = metric(service.get("used"), f"cityHealth.services.{name}.used", nonnegative=True)
        capacity = metric(service.get("capacity"), f"cityHealth.services.{name}.capacity", nonnegative=True)
        if used is not None and capacity is not None and used > capacity:
            capability, fallback = SERVICE_CAPABILITIES[name]
            if capability not in capabilities and fallback:
                capability = fallback
            if capabilities.get(capability) is False:
                locked_objective("service-" + name, capability, f"{name} need {used:g} exceeds capacity {capacity:g}.")
                continue
            objective("service-" + name, 85, f"{name} need {used:g} exceeds capacity {capacity:g}; inspect unlocks and geographic reach.",
                      ["inspect_service_capacity", "inspect_service_coverage", "inspect_toolbar_unlocks"],
                      "Measured need is served by reachable, unlocked facilities")
    for category, tool in (("roadAnomalies", "inspect_road_result"), ("buildingAnomalies", "preview_building_candidate")):
        result = _object(current.get(category))
        count = metric(result.get("total"), category + ".total", nonnegative=True)
        if count is not None and count > 0:
            objective(category, 95, f"{int(count)} geometry signals need classification; totals alone do not justify demolition.",
                      ["observe_city", tool], "Focused live inspection resolves the anomaly or verifies safe geometry")
    if amount is not None and amount < reserve_display:
        objective("cash-reserve", 90, f"Cash {amount:g} is below policy reserve {reserve_display:g}.",
                  ["inspect_economy"], "Cash returns above reserve without reducing essential service delivery")
    elif sustained_deficit:
        objective("sustained-deficit", 65, "Period net is negative in two distinct simulation snapshots.",
                  ["inspect_economy", "inspect_growth_capacity"], "Period net recovers over subsequent simulation observations")
    if happiness is not None and population is not None and population > 0 and happiness < 60:
        objective("wellbeing", 75, f"Happiness {happiness:g} is below the initial policy target 60.",
                  ["inspect_service_coverage", "inspect_service_capacity"], "Happiness improves with no increase in resident problems")
    if unemployment is not None and population is not None and population > 0 and unemployment > 15:
        objective("employment", 70, f"Unemployment {unemployment:g} exceeds initial policy target 15.",
                  ["inspect_growth_capacity", "observe_city"], "Unemployment falls with appropriate reachable workplaces")
    if population_delta is not None and population_delta < 0:
        objective("population-decline", 70, f"Population changed by {population_delta:g}; investigate causes before expanding.",
                  ["observe_city", "inspect_growth_capacity"], "Population stabilizes while resident problems and finances improve")

    traffic = _object(current.get("traffic"))
    severe = metric(traffic.get("severeSegments"), "traffic.severeSegments", nonnegative=True)
    if severe is not None and severe > 0:
        objective("traffic", 75, f"{int(severe)} road segments have severe density; verify route impact.",
                  ["inspect_traffic"], "Target routes remain reachable and severe congestion declines")
    demand = _object(summary.get("demand"))
    prior_demand = _object(_object(prior.get("summary")).get("demand")) if comparable else {}
    demand_values = {}
    demand_deltas = {}
    demand_rates = {}
    for name in ("residential", "commercial", "workplace"):
        value = metric(demand.get(name), "summary.demand." + name, nonnegative=True)
        previous_value = _number(prior_demand.get(name)) if comparable else None
        if previous_value is not None and previous_value < 0:
            previous_value = None
        demand_values[name] = value
        demand_deltas[name] = (
            value - previous_value
            if value is not None and previous_value is not None
            else None
        )
        demand_rates[name] = (
            demand_deltas[name] / elapsed_game_days
            if demand_deltas[name] is not None and elapsed_game_days
            else None
        )
        if value is not None and value >= 50:
            trend = (
                f" and changed by {demand_deltas[name]:g} "
                f"({demand_rates[name]:g} per game day)"
                if demand_rates[name] is not None
                else ""
            )
            objective("growth-" + name, 55, f"{name} demand is {value:g}{trend}; inspect occupied and vacant zoned capacity before adding frontage.",
                      ["inspect_growth_capacity", "inspect_toolbar_unlocks"],
                      "Existing zoned capacity is nearly full before one small addition; occupied development and population/jobs then increase after settling")

    if missing:
        objective("missing-evidence", 110, "Required observations are missing or invalid: " + ", ".join(missing),
                  ["observe_city"], "Required telemetry is available; unknown values are never treated as healthy")
    if population == 0:
        objective("bootstrap-neighborhood", 96,
                  "No residents. Complete one usable neighborhood: verify directed outside access and road QA, "
                  "exact safe residential frontage, power/water/sewage delivery, then observe actual move-ins. "
                  "Individual road previews and intermediate stubs are not development progress.",
                  ["inspect_road_result", "inspect_growth_capacity", "inspect_toolbar_unlocks",
                   "inspect_electricity_service_coverage", "inspect_water_service_coverage"],
                  "Verified two-way access and serviced residential buildings receive residents after simulation advances")
    objectives.sort(key=lambda item: (-item["priority"], item["id"]))
    phase = "bootstrap" if population == 0 else "recovery" if any(item["priority"] >= 90 for item in objectives) else "early-growth" if any(item["id"].startswith("growth-") for item in objectives) else "optimization"
    dimensions = {
        "basicSupply": "unknown" if any(v["state"] == "unknown" for v in utility_metrics.values()) else "needs-attention" if any(v["state"] != "available" for v in utility_metrics.values()) else "capacity-available-delivery-unverified",
        "finance": "unknown" if amount is None or net is None else "below-reserve" if amount < reserve_display else "deficit" if net < 0 else "nonnegative-net",
        "residents": "unknown" if total_problems is None or happiness is None or not counts_valid else "needs-attention" if total_problems > 0 or (population and happiness < 60) else "no-aggregate-alert",
        "development": "unknown" if population_delta is None else "growing" if population_delta > 0 else "declining" if population_delta < 0 else "stable",
        "traffic": "unknown" if severe is None else "needs-inspection" if severe > 0 else "no-severe-density",
    }
    next_step = {"kind": "inspect", **objectives[0]} if objectives else {
        "kind": "observe-after-settlement", "id": "wait", "inspectTools": ["observe_city"],
        "reason": "No aggregate signal justifies construction. Verify prior mutations before resuming simulation.",
        "successCondition": "A later simulation snapshot confirms stable services and finances",
    }
    prior_focus = _object(prior.get("assessmentFocus")) if comparable or same_observation_time else {}
    unchanged = (next_step["id"] != "wait" and prior_focus.get("id") == next_step["id"]
                 and prior_focus.get("reason") == next_step["reason"])
    prior_count = _number(prior_focus.get("observations")) or 0
    observations = min(100, max(1, int(prior_count)) + int(comparable)) if unchanged else 1
    next_step["unchangedObservations"] = observations
    if unchanged and observations >= 3:
        next_step["kind"] = "resolve-blocker"
        next_step["escalation"] = (
            "The same bottleneck persists across three simulation observations. Obtain the missing "
            "candidate/preview evidence or report the failed inspection and required telemetry. "
            "Do not repeat a no-op wait or substitute an unrelated tax adjustment."
        )
    baseline = _copy_evidence({key: current[key] for key in ("city", "snapshot", "summary", "cityHealth") if key in current})
    baseline["assessmentFocus"] = {"id": next_step["id"], "reason": next_step["reason"], "observations": observations}
    derived_policy_metrics = policy_engine.derive_metrics(
        current,
        prior if prior else None,
        reserve_display=reserve_display,
        evidence_ref="bridge://mayor_snapshot/current",
        freshness="assessment_input",
    )
    return _copy_evidence({
        "ok": True, "schemaVersion": 1, "planningOnly": True, "mutationAuthorized": False,
        "city": city, "snapshot": clock, "phase": phase,
        "policy": {"reserveDisplay": reserve_display, "utilityHeadroomTarget": 0.1,
                   "happinessTarget": 60, "unemploymentCeiling": 15, "highDemandThreshold": 50},
        "dimensions": dimensions,
        "metrics": {"population": population, "populationDelta": population_delta,
                    "populationDeltaPerGameDay": population_rate,
                    "cashDisplay": amount, "periodNetDisplay": net, "sustainedDeficit": sustained_deficit,
                    "happiness": happiness, "unemployment": unemployment, "problemEntities": total_problems,
                    "problemCounts": _copy_evidence(counts), "utilities": utility_metrics,
                    "severeTrafficSegments": severe, "demand": demand_values,
                    "demandDelta": demand_deltas,
                    "demandDeltaPerGameDay": demand_rates},
        "comparison": {"comparable": comparable, "elapsedGameDays": elapsed_game_days,
                       "reason": comparison_reason},
        "missingEvidence": missing, "objectives": objectives, "nextStep": next_step,
        # Sourced CS1 planning knowledge, compressed to a few lines per wake.
        "planningHints": gameplay_rules.planning_hints(current),
        "derivedPolicyMetrics": derived_policy_metrics,
        "baseline": baseline,
        "limitations": [
            "Policy thresholds are initial heuristics, not a calibrated prosperity score; reserve is configurable.",
            "Aggregate capacity does not prove building delivery, service coverage or directed road access.",
            "Inspection tools need their own arguments; this brief supplies no executable coordinates.",
            "Live progression, preview, checkpoint, regression and settlement checks remain required.",
            "Offline replay evaluates recorded evidence; it does not simulate CS1 or predict action outcomes.",
        ],
    })


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--previous", type=Path)
    parser.add_argument("--reserve", type=float, default=10000)
    args = parser.parse_args()
    try:
        current = json.loads(args.snapshot.read_text(encoding="utf-8-sig"))
        previous = json.loads(args.previous.read_text(encoding="utf-8-sig")) if args.previous else None
        result = assess_city(current, previous, reserve_display=args.reserve)
        result["offline"] = True
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (OSError, ValueError) as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

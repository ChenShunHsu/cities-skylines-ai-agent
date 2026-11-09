"""Source-linked CS1 planning rules turned into compact, deterministic hints.

Rules come from references/cs1-gameplay-rules.json. Hints are decision support
only: they never authorize a mutation or replace live preview and QA gates.
"""
from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path
from typing import Any

RULES_PATH = Path(__file__).resolve().parents[2] / "references" / "cs1-gameplay-rules.json"


@lru_cache(maxsize=1)
def load_rules() -> dict[str, Any]:
    return json.loads(RULES_PATH.read_text(encoding="utf-8-sig"))


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except OverflowError:
        return None


def target_vacant_cells(demand: float, rules: dict[str, Any] | None = None) -> int:
    pacing = (rules or load_rules())["growthPacing"]
    for tier in pacing["vacantTargetTiers"]:
        if demand >= tier["minimumDemand"]:
            return int(tier["targetVacantCells"])
    return 0


def pacing_decision(demand: int | None, zoned: int, vacant: int,
                    rules: dict[str, Any] | None = None) -> tuple[str, str, int, int]:
    """Return (decision, reason, maximum blocks, target vacant cells)."""
    pacing = (rules or load_rules())["growthPacing"]
    if demand is None:
        return "unknown", "demandUnavailable", 0, 0
    if demand < pacing["highDemandThreshold"]:
        return "hold", "demandBelowHighThreshold", 0, 0
    target = target_vacant_cells(demand, rules)
    shortfall = target - vacant
    if shortfall <= 0:
        return "waitForAbsorption", "vacantSupplyAlreadyMeetsDemandTier", 0, target
    blocks = min(pacing["maximumBlocksPerWake"], math.ceil(shortfall / pacing["cellsPerBlock"]))
    reason = "highDemandAndNoZonedCapacity" if zoned == 0 else "vacantSupplyBelowDemandTier"
    return "considerSmallBlocks", reason, blocks, target


def service_level_percent(budget: float) -> float:
    """Wiki formula for a budget at or below 100%."""
    return (budget / 10.0) ** 2


def minimum_safe_budget(used: float, capacity_at_full_budget: float,
                        rules: dict[str, Any] | None = None) -> int | None:
    policy = (rules or load_rules())["utilityBudget"]
    if capacity_at_full_budget <= 0:
        return None
    for budget in range(int(policy["minimumBudget"]), 101, 5):
        if capacity_at_full_budget * service_level_percent(budget) / 100.0 >= used * policy["requiredHeadroomFactor"]:
            return budget
    return None


def _next_milestone(capabilities: dict[str, Any], rules: dict[str, Any]) -> dict[str, Any] | None:
    for milestone in rules["milestones"]["progression"]:
        capability = milestone.get("unlockCapability")
        if capability is None:
            continue
        unlocked = capabilities.get(capability)
        if unlocked is False:
            return milestone
        if unlocked is not True:
            return None  # Unknown progression must not produce a guess.
    return None


def planning_hints(snapshot: dict[str, Any], rules: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """At most four one-line hints derived from live snapshot facts and sourced rules."""
    rules = rules or load_rules()
    hints: list[dict[str, Any]] = []
    health = snapshot.get("cityHealth") if isinstance(snapshot.get("cityHealth"), dict) else {}
    summary = snapshot.get("summary") if isinstance(snapshot.get("summary"), dict) else {}
    cash = health.get("cash") if isinstance(health.get("cash"), dict) else {}
    population = _number(summary.get("population"))
    raw_net = _number(cash.get("periodNet"))
    deficit = raw_net is not None and raw_net < 0
    capabilities = ((snapshot.get("progression") or {}).get("capabilities") or {})

    milestone = _next_milestone(capabilities, rules) if isinstance(capabilities, dict) else None
    if milestone is not None and population is not None:
        low, high = milestone["populationRange"]
        hints.append({
            "id": "next-milestone",
            "text": (f"Population {population:g}; next milestone {milestone['name']} needs {low}-{high} "
                     f"(map-dependent), pays {milestone['cashBonusDisplay']} and unlocks "
                     f"{', '.join(milestone['unlocks'])}. Residential growth is the main early income lever."),
            "sources": rules["milestones"]["source"],
        })

    economy = rules["economy"]
    if deficit and population is not None and population < economy["earlyProfitPopulationEstimate"]:
        hints.append({
            "id": "early-deficit",
            "text": (f"A deficit below about {economy['earlyProfitPopulationEstimate']} population is normal "
                     f"(community estimate). Prefer growth and trimming oversupplied utility budgets over tax "
                     f"changes; defer {', '.join(economy['deferExpensivePoliciesUntilProfit'])} policies."),
            "sources": economy["source"],
        })

    policy = rules["utilityBudget"]
    utilities = health.get("utilities") if isinstance(health.get("utilities"), dict) else {}
    if deficit:
        for name, service in policy["budgetedServices"].items():
            value = utilities.get(name) if isinstance(utilities.get(name), dict) else {}
            used, capacity = _number(value.get("used")), _number(value.get("capacity"))
            if used is None or capacity is None or capacity <= 0:
                continue
            if used / capacity >= policy["oversupplyUtilization"]:
                continue
            budget = minimum_safe_budget(used, capacity, rules)
            if budget is None or budget >= 100:
                continue
            hints.append({
                "id": f"{name}-budget-oversupply",
                "service": service,
                "suggestedBudget": budget,
                "text": (f"{name} uses {used / capacity:.1%} of capacity. If its budget is 100%, {budget}% keeps "
                         f"{policy['requiredHeadroomFactor']:g}x headroom (service level (B/10)^2). Day and night budgets "
                         "are separate settings. Read live budgets, respect the fiscal cooldown, and verify capacity "
                         "and coverage after the change."),
                "sources": policy["source"],
            })
    return hints[:4]

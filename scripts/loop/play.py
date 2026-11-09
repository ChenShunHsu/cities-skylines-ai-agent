"""Run wakes until something needs a planner, and say why.

Each wake runs as its own process (python -m loop.cycle). The loop stops when:
- a capability unlocks (new buildings, roads or policies to plan with), or cash falls under reserve
- a mutation failed or a regression check failed (zoning, utilities, services, traffic, expansion)
- the local model disagreed with the controller's reading (only with --model)
- utility capacity runs short (a new water pair or plant is a siting decision)
- buildings stay without water or power after the repairs
- services escalate: an unmapped problem, an economy problem still rising, a service the loop could
  not place, a transit line that stays unconnected
- traffic escalates: the executable remedies of a hotspot are spent (cargo rail, interchanges)
- new land needs a grade separation or a bridge
Each wake's report is appended to play-log.jsonl in the city's private folder.

Usage: python scripts/loop/play.py <wakes> [seconds-per-wake] [--reserve N] [--model]

The local-model cross-check is off by default; --model turns it on for every wake. --no-model is
still accepted (it is the default) and wins over --model.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

from loop import city as city_module  # noqa: E402
from loop import utilities  # noqa: E402

UTILITY_PRESSURE = 0.85
UTILITY_CRITICAL = 0.95  # pressure that stops the loop even while new water capacity settles


def failed(action: dict[str, Any]) -> bool:
    # a budget at its maximum is a fact, not a failure; a cooldown (ok None) waits a wake
    return (action.get("ok") is False and action.get("stage") not in ("atMaximum", "noSite")) \
        or action.get("regressionOk") is False


def stop_reasons(report: dict[str, Any], baseline_unlocks: set[str], reserve: float) -> list[Any]:
    reasons: list[Any] = []
    wake = report["wake"]
    if set(wake["unlocked"]) - baseline_unlocks:
        reasons.append({"unlocked": sorted(set(wake["unlocked"]) - baseline_unlocks)})
    if (wake.get("cash") or 0) < reserve:
        reasons.append("cashBelowReserve")
    if wake.get("modelMismatches"):
        reasons.append({"modelDisagreement": wake["modelMismatches"]})
    if (report.get("network") or {}).get("isolatedRoads"):
        reasons.append({"roadsNotConnected": report["network"]["isolatedRoads"]})
    growth = report["growth"]
    zoning = growth["zoned"] + growth["densified"]
    if any((not z["ok"] and not z.get("gateSkipped")) or z.get("regressionOk") is False for z in zoning):
        reasons.append("zoningFailed")
    expansion = growth.get("expansion") or {}
    if expansion.get("stage") == "needsCrossing":
        reasons.append({"landNeedsCrossing": expansion})
    elif expansion and expansion.get("ok") is False:
        reasons.append({"expansionFailed": expansion})
    ruins = report["ruins"]
    if ruins.get("regressionOk") is False or ruins.get("ok") is False:
        reasons.append({"ruinClearingFailed": ruins})
    plant, system = report.get("powerPlant") or {}, report.get("waterSystem") or {}
    if plant.get("ok") is False or plant.get("regressionOk") is False:
        reasons.append({"powerPlantFailed": plant})
    if system.get("ok") is False or system.get("regressionOk") is False:
        reasons.append({"waterSystemFailed": system})
    capacity = report.get("waterCapacity") or {}
    if capacity.get("regressionOk") is False:
        reasons.append({"waterCapacityFailed": capacity})
    # a trimmed budget turned back up this wake adds capacity too; one that was not turned up stops
    # the loop (live: a raise the fiscal cooldown held was let through for two wakes)
    restored = any(action.get("ok") for action in report.get("waterBudget") or []) \
        or bool((report["water"].get("supply") or {}).get("ok")) or bool((report.get("waterCapacity") or {}).get("ok"))
    # the wakes after a new pump or outlet let it settle before the next one is added (water first,
    # then sewage): the capacity is in hand, unless the use nears the limit (live: sewage at 88%
    # the wake a new pump settled stopped the loop; the outlet was due the next wake)
    settling = (report.get("waterCapacity") or {}).get("stage") == "settling"
    for key in ("elecUtil", "waterUtil", "sewageUtil"):
        if (key == "elecUtil" and plant.get("ok")) or (key != "elecUtil" and (system.get("ok") or restored)):
            continue  # this wake added capacity
        if key != "elecUtil" and settling and (wake.get(key) or 0) < UTILITY_CRITICAL:
            continue
        if (wake.get(key) or 0) >= UTILITY_PRESSURE:
            reasons.append({"utilityPressure": {key: wake[key]}})
    # a dry facility being moved is repaired over the next wakes, not a stop; nor are buildings
    # still unpiped after a wake's pipes all held (the next wake lays more), nor dry homes the wake
    # that built the system (its intake has not pumped yet, so it reads short of supply) or turned
    # its budget back up (the capacity reaches them over the next days)
    steps = report["water"].get("steps") or []
    # all its pipes held, and it laid as many as it may or ran out of dry buildings to pipe
    progressing = (len(steps) >= utilities.MAX_PIPES or report["water"].get("caughtUp"))         and all(step.get("ok") and step.get("regressionOk") for step in steps)
    if (report["water"]["summary"]["missingWater"] or report["water"]["summary"]["missingSewage"]) \
            and not report["water"].get("relocating") and not progressing and not system.get("ok") \
            and not restored:
        reasons.append({"waterUnrepaired": report["water"]})
    if (report.get("stacks") or {}).get("regressionOk") is False:
        reasons.append({"unstackFailed": report["stacks"]})
    stranded = [item for item in (report.get("orphans") or {}).get("found", []) if not item.get("removed")]
    if stranded:
        reasons.append({"orphansNotCleared": stranded})
    # buildings dark only for want of supply wait a wake for a plant this wake built; those the
    # grid does not reach stop the loop either way
    # and those beyond it wait a wake for a plant this wake built among them
    # buildings beyond the grid stop it only when no line this wake got anywhere (lines laid wake by
    # wake reach them, live: 4 of 170 dark buildings left beyond it as a line went in)
    island = (report["power"].get("island") or {}).get("ok")
    lined = any(step.get("ok") for step in report["power"].get("steps") or [])
    if report["power"]["summary"]["missingElectricity"] and (
            not (plant.get("ok") or island or lined) or (report["power"].get("unreached") and not island and not lined)):
        reasons.append({"unpowered": report["power"]["unpowered"]})
    for key in ("services", "servicesAfterTraffic"):
        solved = report.get(key) or {}
        if solved.get("escalations"):
            reasons.append({"serviceEscalation": solved["escalations"]})
        broken = [action for action in solved.get("actions", []) if failed(action)
                  or any(failed(r) for r in action.get("results", []))]
        if broken:
            reasons.append({"serviceActionFailed": broken})
    cargo = report.get("cargo") or {}
    if cargo.get("ok") is False and not (cargo.get("undo") or {"removedAll": True}).get("removedAll"):
        reasons.append({"cargoRailNotUndone": cargo})  # a terminal or track left half built
    traffic = report.get("traffic") or {}
    if traffic.get("escalation"):
        reasons.append({"trafficNeedsPlanning": traffic["escalation"]})
    for key in ("trial", "widening", "transit"):
        if traffic.get(key) and (not traffic[key]["ok"] or traffic[key].get("regressionOk") is False):
            reasons.append({f"traffic{key.title()}Failed": traffic[key]})
    if traffic.get("metro") and traffic["metro"].get("ok") is False and not traffic["metro"].get("undone"):
        reasons.append({"trafficMetroNotUndone": traffic["metro"]})  # stations or track left behind
    if traffic.get("highwayExit") and traffic["highwayExit"].get("ok") is False \
            and not traffic["highwayExit"].get("undone", True):
        reasons.append({"trafficExitNotUndone": traffic["highwayExit"]})  # a ramp piece left behind
    if traffic.get("bypass") and not traffic["bypass"]["ok"] and traffic["bypass"].get("stage") != "clearing":
        reasons.append({"trafficBypassFailed": traffic["bypass"]})  # a piece failed: a dead end may remain
    return reasons


def summary_line(index: int, report: dict[str, Any]) -> dict[str, Any]:
    wake, growth = report["wake"], report["growth"]
    return {"wake": index, "time": str(wake.get("gameTime"))[:10], "pop": wake.get("population"),
            "cash": wake.get("cash"), "unemp": wake.get("unemployment"), "demand": growth["demand"],
            "vacancy": growth["vacancy"], "allowed": growth["allowed"],
            "zoned": [(z["blockId"], z["zone"][:4], z["ok"]) for z in growth["zoned"] + growth["densified"]],
            "expansion": {k: (growth.get("expansion") or {}).get(k) for k in ("use", "ok", "stage", "collectorNodes")}
            if growth.get("expansion") else None,
            "problems": wake.get("problems"), "traffic": {k: report["traffic"].get(k) for k in
                                                          ("flowRatio", "hotspots", "action", "judged")},
            "cargo": {k: v for k, v in (report.get("cargo") or {}).items()
                      if k in ("noResources", "ok", "reason", "stage", "built", "judged", "buildingId")} or None,
            "fixed": [(a.get("problem") or "rebuild", a.get("prefab"), a.get("ok")) for key in
                      ("services", "servicesAfterTraffic") for a in (report.get(key) or {}).get("actions", [])]}


def wake_command(seconds: str, attempt: int, use_model: bool = False) -> list[str]:
    """The cycle for one attempt at a wake. A retry runs no clock: the first attempt already let the
    game run, and running it again on every retry let months of upkeep pass while the failing step
    stayed undone (live: three retries of one wake took the city from February to May). The
    local-model cross-check is passed on only when asked for."""
    return [sys.executable, "-m", "loop.cycle", seconds if attempt == 0 else "0"] + (["--model"] if use_model else [])


SAVE_STUCK = "save did not finish"


def save_stuck(stderr: str | None) -> dict[str, Any] | None:
    """The stop for a wake that failed waiting on a save that never finishes: retrying only waits
    on it again, and the game cannot even quit until it is closed by force (live: a checkpoint
    requested while the simulation thread had stalled never wrote its file, every retry waited
    three minutes on it, and leaving the game hung on "Waiting for simulation to quit")."""
    if SAVE_STUCK not in (stderr or ""):
        return None
    return {"stop": "saveStuck", "advice": "restart the game (end it by force if it will not quit) and resume "
                                            "from the save continue_game.json names"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("wakes", type=int)
    parser.add_argument("seconds", nargs="?", default="60")
    parser.add_argument("--reserve", type=float)
    parser.add_argument("--model", action="store_true", help="cross-check each reading with the local model")
    parser.add_argument("--no-model", action="store_true", help="the default; kept for older commands")
    options = parser.parse_args()
    use_model = options.model and not options.no_model
    city = city_module.current()
    reserve = options.reserve if options.reserve is not None else float(city.profile["settings"]["reserve"])
    baseline_unlocks: set[str] | None = None
    for index in range(options.wakes):
        for attempt in range(3):  # a game-thread timeout mid-wake is usually transient
            run = subprocess.run(wake_command(options.seconds, attempt, use_model), cwd=str(SCRIPTS),
                                 capture_output=True, text=True, encoding="utf-8", errors="replace")
            if run.returncode == 0 and run.stdout.strip():
                break
            stuck = save_stuck(run.stderr)
            if stuck:
                print(json.dumps(stuck), flush=True)
                return
            (city.dir / f"wake-error-{attempt}.log").write_text(run.stderr or "", encoding="utf-8")
            print(json.dumps({"wakeRetry": attempt, "stderr": (run.stderr or "")[-300:]}), flush=True)
            time.sleep(10)
        if run.returncode != 0 or not run.stdout.strip():
            print(json.dumps({"stop": "wakeError", "stderr": (run.stderr or "")[-800:]}))
            return
        report = json.loads(run.stdout.strip().splitlines()[-1])
        city.log("play-log", {"at": datetime.now(timezone.utc).isoformat(), **report})
        print(json.dumps(summary_line(index, report), ensure_ascii=False, default=str), flush=True)
        # a brand-new city reads no capabilities before its clock first runs: its starting set is
        # the baseline, not an unlock to stop for
        if not baseline_unlocks:
            baseline_unlocks = set(report["wake"]["unlocked"])
        reasons = stop_reasons(report, baseline_unlocks, reserve)
        if reasons:
            print(json.dumps({"stop": reasons}, ensure_ascii=False, default=str))
            return


if __name__ == "__main__":
    main()

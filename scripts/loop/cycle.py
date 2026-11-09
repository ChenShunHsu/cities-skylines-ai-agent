"""One wake of the loop: simulate and read, grow, clear ruins, repair utilities, solve problems,
then traffic. Prints the wake's report as one JSON line (play.py runs each wake as its own process,
so a crash or an edited module never carries over between wakes).

Usage: python -m loop.cycle <seconds-at-speed-3> [--model]

The local-model cross-check of the reading runs only with --model (wake.py); --no-model is still
accepted and wins over --model.
"""

from __future__ import annotations

import json
import math
import statistics
import sys
import time
from typing import Any, Callable

from loop import city as city_module
from loop import cargo, growth, protocol, services, traffic, utilities, wake
from loop.city import City, mt


NOT_RUN = "command was not run"


def unless_busy(phase: Callable[..., Any], *args: Any) -> Any:
    """A phase's report, or a busy note when the game thread was too busy to run one of its
    commands: such a timeout ran nothing, each change before it was verified on its own, and the
    rest of the wake goes on (live: a ruin clearing the busy thread did not run failed the whole
    wake, which was then run again from the start)."""
    try:
        return phase(*args)
    except (mt.BridgeRequestError, RuntimeError) as error:
        if NOT_RUN not in str(error):
            raise
        return {"ok": None, "busy": True, "error": str(error)[:200]}


RUIN_CAUSES_KEPT = 8
BURN_REACH = 300.0  # burned ruins this near one another are one fire


def burn_knot(points: list[tuple[float, float]]) -> dict[str, Any] | None:
    """The densest knot of burned ruins: the one with the most others within BURN_REACH, and
    their middle. A fire is out before the next wake, so its ruins are the only trace of it."""
    if not points:
        return None
    best = max(points, key=lambda p: sum(1 for q in points if math.dist(p, q) <= BURN_REACH))
    knot = [q for q in points if math.dist(best, q) <= BURN_REACH]
    return {"count": len(knot), "center": {"x": round(sum(q[0] for q in knot) / len(knot), 1),
                                           "z": round(sum(q[1] for q in knot) / len(knot), 1)}}


def ruin_kinds(growables: list[dict[str, Any]], pending: list[int]) -> dict[str, Any]:
    """What the ruins about to be cleared were and the problems the game still shows on them, so
    a wave of abandonment can be traced to its cause before the evidence is bulldozed (live: 15-40
    buildings a wake were cleared with nothing kept but the count)."""
    by_id = {item["id"]: item for item in growables}
    kinds: dict[str, int] = {}
    for building_id in pending:
        item = by_id.get(building_id) or {}
        kind = f"{item.get('service')}/{item.get('subService')}"
        kinds[kind] = kinds.get(kind, 0) + 1
    causes: dict[str, int] = {}
    wanted = set(pending)
    for row in mt._request("GET", "/state/problems", query={"limit": 50000}).get("problems", []):
        if row.get("id") in wanted:
            for name in row.get("problemNames") or []:
                if name not in ("Abandoned", "MajorProblem", "FatalProblem"):
                    causes[name] = causes.get(name, 0) + 1
    top = dict(sorted(causes.items(), key=lambda item: -item[1])[:RUIN_CAUSES_KEPT])
    burned = [(by_id[i]["position"]["x"], by_id[i]["position"]["z"]) for i in pending
              if i in by_id and "BurnedDown" in str(by_id[i].get("flags")) and by_id[i].get("position")]
    return {"kinds": kinds, "causes": top, "burned": burn_knot(burned)}


def clear_ruins(city: City, label: str) -> dict[str, Any]:
    """Abandoned, burned and collapsed growables never recover; one change clears them all.
    A building the game refuses to release is remembered and not retried. What they were and
    the problems on them are reported first (ruin_kinds)."""
    stuck = set(city.load_state("bulldoze-stuck", []))
    growables = next((value for value in mt._request("GET", "/state/growables", query={"limit": 50000}).values()
                      if isinstance(value, list)), [])
    pending = [item["id"] for item in growables
               if (item.get("abandoned") or any(flag in str(item.get("flags")) for flag in ("BurnedDown", "Collapsed")))
               and item["id"] not in stuck]
    if not pending:
        return {"cleared": 0, "stuck": sorted(stuck)}
    try:
        seen = ruin_kinds(growables, pending)
    except mt.BridgeRequestError as error:  # the evidence is a note: it never holds up the clearing
        seen = {"error": str(error)[:120]}
    name = protocol.checkpoint(f"clear-ruins-{label}")
    result = mt.call_tool("clear_abandoned_buildings", {"checkpointName": name, "limit": 200})
    regression = protocol.verify(f"clear-ruins-{label}")
    refused = [item["id"] for item in result.get("refused", [])]
    stuck.update(refused)
    city.save_state("bulldoze-stuck", sorted(stuck))
    return {"cleared": len(result.get("cleared", [])), "skipped": result.get("skipped"), "refused": refused,
            "ok": not result.get("stillPresent"), "regressionOk": regression.get("ok"), **seen}


CASH_TREND_WAKES = 3
CASH_FALL = 0.05


def cash_falling(city: City, cash: float) -> bool:
    """Cash down more than CASH_FALL over the last CASH_TREND_WAKES wakes (utilities.in_deficit)."""
    trend = (city.load_state("cash-trend", []) + [cash])[-(CASH_TREND_WAKES + 1):]
    city.save_state("cash-trend", trend)
    return len(trend) > CASH_TREND_WAKES and cash < trend[0] * (1 - CASH_FALL)


UNSTACK_PER_WAKE = 300


def stacked_buildings(growables: list[dict[str, Any]]) -> list[int]:
    """Growables standing on another one's exact lot (same position and angle), all but the newest
    of each lot. The game grows one building per lot; a stack is what Bridge commands run beside the
    simulation thread left behind (live: 861 growables on 16 lots, up to 136 on one, each simulated
    as its own), so the extras are cleared like ruins."""
    lots: dict[tuple, list[int]] = {}
    for item in growables:
        position = item.get("position") or {}
        if "x" not in position:
            continue
        key = (round(position["x"], 2), round(position["z"], 2), round(float(item.get("angleDegrees") or 0), 1))
        lots.setdefault(key, []).append(int(item["id"]))
    return sorted(i for ids in lots.values() if len(ids) > 1 for i in sorted(ids)[:-1])


def clear_stacks(label: str) -> dict[str, Any]:
    """The stacked growables bulldozed after one checkpoint, UNSTACK_PER_WAKE a wake."""
    growables = next((value for value in mt._request("GET", "/state/growables", query={"limit": 50000}).values()
                      if isinstance(value, list)), [])
    extra = stacked_buildings(growables)
    if not extra:
        return {"cleared": 0}
    name = protocol.checkpoint(f"unstack-{label}")
    cleared, refused = [], []
    for building in extra[:UNSTACK_PER_WAKE]:
        try:
            outcome = mt.call_tool("bulldoze_entity", {"checkpointName": name, "entityType": "building",
                                                      "id": building})
        except mt.BridgeRequestError as error:
            refused.append({"id": building, "error": str(error)[:120]})
            continue
        (cleared if outcome.get("ok") else refused).append(building if outcome.get("ok") else
                                                           {"id": building, "error": str(outcome.get("error"))[:120]})
    regression = protocol.verify(f"unstack-{label}")
    # the lots stacked, to trace how a stack still forms: each lot's buildings, newest last
    by_id = {int(item["id"]): item for item in growables}
    lots: dict[tuple, list[int]] = {}
    for building in extra:
        position = by_id[building]["position"]
        lots.setdefault((round(position["x"], 1), round(position["z"], 1)), []).append(building)
    traced = [{"x": key[0], "z": key[1], "service": by_id[ids[0]].get("service"), "ids": ids[-5:],
               "flags": sorted({str(by_id[i].get("flags")) for i in ids})[:3]}
              for key, ids in sorted(lots.items(), key=lambda kv: -len(kv[1]))[:8]]
    return {"found": len(extra), "cleared": len(cleared), "refused": refused[:10],
            "regressionOk": regression.get("ok"), "lots": traced}


ORPHAN_SETBACK = 24.0  # a lot's far edge from its road's centre line: four cells and half a road
# nothing within a zone block's depth of a road (half a road, four 8m cells and a margin) is cut off:
# the game grows homes there, behind the front row (live: four 1x1 blockhouses 29-39m from their road
# were bulldozed as orphans in 56 of 60 wakes and grew back on the same spots each time)
ZONE_REACH = 44.0


def orphaned_buildings() -> list[dict[str, Any]]:
    """Growables no local road reaches: the road they grew on is gone, yet the game raises no
    problem for them (live: three shops 71-101m from any road, flagged nothing, and powered and
    piped by the loop for nothing). Highways serve no lot, so they do not count."""
    roads = [s for s in mt._request("GET", "/state/networks", query={"limit": 20000, "service": "Road"})
             .get("segments", []) if "Highway" not in str(s.get("prefab"))]
    growables = next((value for value in mt._request("GET", "/state/growables", query={"limit": 50000}).values()
                      if isinstance(value, list)), [])
    found = []
    for building in growables:
        reach = max(float((building.get("spatial3d") or {}).get("lengthMeters") or 32.0) / 2 + ORPHAN_SETBACK,
                    ZONE_REACH)
        distance = min((mt._distance_to_segment(building["position"], s["start"], s["end"]) for s in roads),
                       default=float("inf"))
        if distance > reach:
            found.append({"id": building["id"], "service": building.get("service"),
                          "roadDistance": round(distance, 1) if distance != float("inf") else None})
    return found


def clear_orphans(label: str) -> dict[str, Any]:
    """Buildings cut off from every road are bulldozed, as a player clears them: they never get
    customers, workers or deliveries, and only draw lines and pipes. A bulldoze that fails is tried
    once more and its error kept (live: one of four failed with no error recorded, stopped the
    loop, and went at the first retry)."""
    found = orphaned_buildings()
    for item in found:
        for attempt in range(2):
            outcome = protocol.run(f"orphan-{label}-{item['id']}" + (f"-r{attempt}" if attempt else ""),
                                   "bulldoze_entity", {"entityType": "building", "id": item["id"]})
            if outcome["ok"]:
                break
        item["removed"] = bool(outcome["ok"])
        if not outcome["ok"]:
            item["error"] = str(outcome.get("error") or outcome.get("result"))[:200]
    return {"found": found}


def network_check() -> dict[str, Any]:
    """Roads that reach no outside connection. The loop only extends the connected network, so a
    cut-off road means something went wrong (or was built by hand): its buildings cannot be served."""
    from mayor import network
    _, isolated = network.road_components(mt._request("GET", "/state/networks", query={"limit": 20000})
                                          .get("segments", []))
    return {"isolatedRoads": sorted(isolated)[:50]}


EXPAND_CASH_FLOOR = 15000.0  # display cash under which new land grows only a road or two a wake
EXPAND_CASH_MINIMUM = 5000.0  # and under which none is opened


FIRM_RETRY = 10  # wakes a lull's search waits after it found no site


def firm_wanted(city: City, wake_report: dict[str, Any]) -> bool:
    """Whether the grid has ever run short where no reading saw it: OUTAGE_MIN buildings flagged
    without electricity at a wake's reading. Remembered: a wind lull comes back. A search for it
    that found no site waits FIRM_RETRY wakes (each is many native previews)."""
    state = city.load_state("power-outages", {"seen": 0})
    state["wakes"] = state.get("wakes", 0) + 1
    count = int((wake_report.get("problemCounts") or {}).get("Electricity") or 0)
    if count >= utilities.OUTAGE_MIN:
        state.update(seen=state.get("seen", 0) + 1, last=count, noSiteAt=None)  # a new outage looks at once
    city.save_state("power-outages", state)
    waited = state.get("noSiteAt") is None or state["wakes"] - state["noSiteAt"] >= FIRM_RETRY
    return state.get("seen", 0) > 0 and waited


YIELD_WAKES = 10  # wakes whose turbine yield the typical one is the median of


def typical_yield(city: City) -> float | None:
    """The median share of their rating the variable plants gave over the last YIELD_WAKES wakes,
    this one included: a lull is sized from what they typically give."""
    state = city.load_state("power-outages", {"seen": 0})
    sample = utilities.yield_sample()
    if sample:
        state["yields"] = (state.get("yields", []) + [sample])[-YIELD_WAKES:]
        city.save_state("power-outages", state)
    return statistics.median(state["yields"]) if state.get("yields") else None


def firm_unsited(city: City, plant: dict[str, Any] | None) -> None:
    """Remember a lull's search that found no site (firm_wanted then waits)."""
    if (plant or {}).get("reason") == "firmNoSite":
        state = city.load_state("power-outages", {"seen": 0})
        state["noSiteAt"] = state.get("wakes", 0)
        city.save_state("power-outages", state)


def serve(city: City, label: str, unlocked: list[str], power_peak: float | None = None,
          firm: bool = False, wind_yield: float | None = None) -> dict[str, Any]:
    """A first water system and plant when the town has none, then pipes and lines to every
    building left without them. power_peak is the power use the wake read (add_power)."""
    return {"waterSystem": utilities.add_water(city, label, unlocked), "waterBudget": utilities.restore_budgets(label),
            "waterCapacity": utilities.add_water_capacity(city, label), "water": utilities.repair_water(label, city),
            "powerPlant": utilities.add_power(label, unlocked, power_peak, firm, wind_yield),
            "power": utilities.repair_power(label)}


def model_requested(argv: list[str]) -> bool:
    """The cross-check is opt-in: a wake started with no flag (as an older play.py starts it) reads
    without the local model."""
    return "--model" in argv and "--no-model" not in argv


def main() -> None:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0
    mt.enable_read_cache()  # large reads are reused between changes (mayor_tools.CACHED_READS)
    protocol.wait_save()  # a save in progress holds the game thread and times out the reads
    pruned = protocol.prune_checkpoints()  # before the pause, which lets the game take in the deletions
    time.sleep(5)
    city = city_module.current()
    report: dict[str, Any] = {"city": city.identity, "checkpointsPruned": pruned}
    # a town no road leads into cannot grow: its clock stays held, so upkeep does not run on
    linked = growth.connected(city)
    report["wake"] = wake.wake(city, seconds if linked else 0.0, use_model=model_requested(sys.argv))
    report["clockHeld"] = not linked
    label = str(report["wake"].get("gameTime") or "wake")[:10]
    report["network"] = network_check()
    # homes already standing get water and power before any money goes to new land; utilities wait
    # for the road in (built earlier they only cost upkeep)
    firm = firm_wanted(city, report["wake"])
    wind_yield = typical_yield(city) if firm and linked else None
    if linked:
        protocol.new_batch()
        report.update(serve(city, label, report["wake"]["unlocked"], report["wake"].get("elecUtil"), firm,
                            wind_yield))
        firm_unsited(city, report.get("powerPlant"))
    unserved = bool((report.get("water") or {}).get("summary", {}).get("missingWater")
                    or (report.get("water") or {}).get("summary", {}).get("missingSewage")
                    or (report.get("power") or {}).get("unpowered"))
    # new land waits while homes lack water or power; short of cash a town grows a little at a
    # time (a road or two), and stops only near empty: stopping outright left a town that needed
    # land to earn the money for land with neither
    cash = report["wake"].get("cash") or 0
    hold = "waitingForUtilities" if unserved else ("waitingForCash" if cash < EXPAND_CASH_MINIMUM else None)
    protocol.new_batch()
    report["growth"] = growth.grow(city, label, "highDensityResidential" in report["wake"]["unlocked"],
                                   hold_land=hold, small=cash < EXPAND_CASH_FLOOR,
                                   offices="office" in report["wake"]["unlocked"])
    if not linked and growth.connected(city):  # joined to the outside this wake
        protocol.new_batch()
        report.update(serve(city, label, report["wake"]["unlocked"], report["wake"].get("elecUtil"), firm,
                            wind_yield))
    protocol.new_batch()
    report["ruins"] = unless_busy(clear_ruins, city, label)
    report["orphans"] = unless_busy(clear_orphans, label)
    report["stacks"] = unless_busy(clear_stacks, label)
    protocol.new_batch()
    high, offices = "highDensityResidential" in report["wake"]["unlocked"], "office" in report["wake"]["unlocked"]
    burned, ruined = (report["ruins"] or {}).get("burned"), (report["ruins"] or {}).get("causes")
    report["services"] = unless_busy(services.solve, city, label, high, offices, burned, ruined)
    protocol.new_batch()
    report["budgets"] = unless_busy(utilities.balance, label, cash_falling(city, cash)) if linked else []
    protocol.new_batch()
    report["traffic"] = unless_busy(traffic.watch, city, label)
    if linked:
        # goods by rail once industry keeps lacking them or trucks fill a hotspot (loop.cargo)
        protocol.new_batch()
        report["cargo"] = cargo.step(city, label, report["wake"], report["traffic"])
    if any(report["traffic"].get(key) for key in ("trial", "widening", "bypass", "transit", "outsideLink")):
        # a trial runs the simulation for game months; solve what appeared meanwhile
        protocol.new_batch()
        report["servicesAfterTraffic"] = unless_busy(services.solve, city, label + "-after-traffic", high, offices)
    print(json.dumps(report, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()

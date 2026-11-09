"""Solve every reported problem each wake (assess_problem_remedies decides the remedy kind).

- service: a full landfill or cemetery is rebuilt in place. A cluster of a problem farther than
  REACH from a working facility of its family gets a facility at once when it has IMMEDIATE_CLUSTER
  buildings, a smaller or nearer one after PERSIST wakes (capacity, not distance). The building
  is chosen per need by compare_service_options: beyond reach -> coverage, persisting within
  reach -> capacity, a family that keeps filling -> fillsUp. A big building with no site gives
  way to the next option. After OUTCOME_WAKES a placement whose cluster did not shrink is marked
  ineffective for that need, so the next choice skips it. With no site at all, the family's
  budget rises; a cluster still unresolved ESCALATE_AFTER wakes later goes to the planner.
- LandValueLow: the park with the most entertainment per upkeep at the cluster, once it persists.
- economy problems go to the planner only when their count keeps rising to ECONOMY_MIN.
- a transit line that stays unconnected for PERSIST wakes goes to the planner.
- a problem repaired elsewhere in the wake or by time (persist) goes to the planner after PERSIST
  wakes; a facility's status (status) is no problem.
- the sick and pupils beyond the places for them are reported like flagged problems
  (mayor.problems.shortfall_rows): the game flags no building for them.
- unmapped problems always go to the planner.
Displacement is not capped: sites rank by it, and each building gets its own bulldoze preview.
"""

from __future__ import annotations

import math
from typing import Any

from loop import growth, industry, protocol, roads, utilities
from loop.city import City, mt
from mayor import problems as mayor_problems

PERSIST = 3
IMMEDIATE_CLUSTER = 3
# a problem that persists within a working facility's reach builds another only at this many
# buildings: one or two are a truck or hearse not there yet, not a shortage (live: two combustion
# plants in two wakes, each for one building's garbage, beside three plants and three landfills)
PERSIST_CLUSTER = 3
ESCALATE_AFTER = 3
# wakes between reports of one unresolved cluster: once reported, it was reported again every wake and
# stopped the loop each time (live: four homes' garbage 1.3km from the nearest plant, no site for a
# polluting one by the homes); between reports the site search is tried again as the town changes
SERVICE_REPORT_EVERY = 20
ECONOMY_MIN = 8
# industry short of goods at this many buildings on four wakes running gets a specialised district
# though the count no longer rises (live: 42, 63, 63, 61, the first cause of abandonment)
SUPPLY_PERSIST = 20
# shops short of customers this many a wake for four wakes get their empty commercial land rezoned
# as when the count rises (live: 22 to 33 a wake for months, never four rises in a row, and 160 shops
# abandoned over 30 wakes while commercial demand stood at 0)
SHOPS_PERSIST = 15
MAX_PLACEMENTS = 2  # new facilities per wake, so one bad assessment cannot spend the treasury
# a fire burns a building down within a wake or two, so a fire never persists into a cluster that
# gets a station: stations follow coverage instead (live: two stations for 19,000 people, 1,936 of
# 3,309 buildings beyond 1km of one, and two or three burned down every wake)
FIRE_COVERAGE_MIN = 40  # buildings beyond every station's reach, in one FIRE_CELL, that get a station
FIRE_CELL = 500.0
OUTCOME_WAKES = 3
FILLS_UP_REBUILDS = 2
FILLS_UP_WINDOW = 10
SPEND_SHARE = 0.25  # one facility may cost at most this share of cash
# the first building of a family may cost more: nothing serves its need until it stands (live: a
# 24,000 high school for 196 pupils was out of reach at 25% of 80,000)
FIRST_SPEND_SHARE = 0.5
SAVING_WAKES = 8  # wakes a cluster may wait for the city to afford its building
BUDGET_STEP, BUDGET_MAX = 10, 150
# garbage trucks cross the city and a landfill keeps 300m from homes, so garbage is never "far"
REACH = {"deathcare": 900, "garbage": 1e9, "police": 650, "fire": 650, "healthcare": 650,
         "elementary": 500, "highSchool": 1000, "university": 1500}
BUDGET_SERVICE = {"deathcare": "HealthCare", "healthcare": "HealthCare", "garbage": "Garbage",
                  "police": "PoliceDepartment", "fire": "FireDepartment", "elementary": "Education",
                  "highSchool": "Education", "university": "Education"}
# a family's capacity reading in city_health; a cluster within reach of its facilities while the
# family runs under CAPACITY_IDLE of that capacity is no shortage (trucks late in traffic, a route
# the vehicles do not take), and no building is added for it (live: a 20th incineration plant for
# a garbage cluster while 19 ran 36% used, garbage a third of the upkeep of a city in deficit)
# deathcare's own utilization sets the bodies the cemeteries hold against what the crematoria burn
# (live: 18,526 held against 1,365, "13.6" while 25 cemeteries stood a quarter full and 13 crematoria
# had nothing to burn): its storage reading is what it uses of its capacity
CAPACITY_READINGS = {"garbage": ("utilities", "garbage"), "deathcare": ("services", "deathcare", "storage"),
                     "healthcare": ("services", "healthcare"), "police": ("services", "police"),
                     "elementary": ("services", "elementaryEducation"),
                     "highSchool": ("services", "highSchoolEducation"),
                     "university": ("services", "universityEducation")}
CAPACITY_IDLE = 0.6


def idle_load(family: str, health: dict[str, Any]) -> float | None:
    """The family's use of its capacity when it is under CAPACITY_IDLE, else None (or no reading)."""
    path = CAPACITY_READINGS.get(family)
    load = utilities.reading(health, path) if path else None
    return load if load is not None and load < CAPACITY_IDLE else None


POLLUTER_RADII = (1000, 2500)
LAND_STEPS = 8  # road pieces laid on into open land before giving up on it
LOT_DEPTH = 60.0  # a facility's lot reaches this far past the road end it is placed beside


def _key(problem: str, cluster: dict[str, Any]) -> str:
    # clusters drift as buildings change; a 300m grid cell keeps the same cluster's key
    return f"{problem}@{int(cluster['center']['x'] // 300)},{int(cluster['center']['z'] // 300)}"


def cash() -> float:
    return float(mt.city_health()["cash"]["amount"])


def choose_prefabs(family: str, need: str, state: dict[str, Any], share: float = SPEND_SHARE) -> list[dict[str, Any]]:
    """The family's unlocked buildings for the need, best first, each marked affordable or not at
    share of the city's cash; buildings that did not work for the need are left out."""
    try:
        options = mt.call_tool("compare_service_options", {"family": family, "need": need,
                                                            "budget": cash() * share})["options"]
    except (mt.BridgeRequestError, RuntimeError, ValueError):
        return []
    failed = set(state.setdefault("ineffective", {}).get(f"{family}:{need}", []))
    kept = [option for option in options if option["name"] not in failed]
    if options and not kept:
        # every building the need takes was judged not to have worked: building none works less, and
        # the town has changed since, so the judgements start over (live: both garbage buildings were
        # marked for fillsUp and both deathcare ones for capacity; nothing was built again, the
        # budgets sat at their maximum and the clusters stayed for dozens of wakes)
        state["ineffective"][f"{family}:{need}"] = []
        return options
    return kept


def best_park() -> str | None:
    """The unlocked park with the most entertainment accumulation per upkeep."""
    parks = [item for item in mt._request("GET", "/prefabs/buildings", query={"service": "Beautification"})
             .get("buildings", []) if item.get("unlocked") and (item.get("aiProfile") or {}).get("aiType") == "ParkAI"]

    def value(item: dict[str, Any]) -> float:
        fields = item["aiProfile"]["fields"]
        return float(fields.get("entertainmentAccumulation") or 0) / max(1.0, float(item.get("maintenanceCost") or 0))
    parks = [item for item in parks if (item["aiProfile"]["fields"].get("entertainmentAccumulation") or 0) > 0]
    return max(parks, key=value)["name"] if parks else None


def open_land(city: City, label: str, clearance: float) -> dict[str, Any]:
    """A road out to ground no home is within clearance of, for a polluting facility the town has
    no such road for (live: every road lay within 300m of a home, so no landfill could be placed
    and garbage piled up for five wakes). It enters the free land a new industrial district would
    take, which keeps the same gap from homes and groundwater pumps, and runs on toward its
    middle until clear."""
    homes = [(item["position"]["x"], item["position"]["z"]) for item in next(
        (value for value in mt._request("GET", "/state/growables", query={"limit": 50000}).values()
         if isinstance(value, list)), []) if item.get("service") == "Residential"]
    wells = mt._land_use_context()["wells"]

    def clear(point: tuple[float, float]) -> bool:
        return all(math.dist(point, home) >= clearance + LOT_DEPTH for home in homes) and \
            all(math.dist(point, well) >= mt.GROUNDWATER_CLEARANCE + LOT_DEPTH for well in wells)
    areas = mt.call_tool("propose_expansion_areas", {"use": "workplace", "limit": 4})["areas"]
    for index, area in enumerate(areas):
        for way in [way for way in area["access"] if way["clear"] and not way["crosses"]][:2]:
            opened = growth.open_access(city, f"{label}-land{index}", way)
            if not opened["ok"]:
                continue
            end = tuple(opened["end"])
            target = (area["center"]["x"], area["center"]["z"])
            for step in range(LAND_STEPS):
                if clear(end):
                    return {"ok": True, "end": {"x": end[0], "z": end[1]}, "pieces": step}
                heading = math.degrees(math.atan2(target[0] - end[0], -(target[1] - end[1])))
                outcome = {"ok": False}
                for _, candidate in roads.step_ends(end, heading):
                    outcome = roads.build(f"{label}-land{index}-{step}", "Basic Road", end, candidate, city)
                    if outcome["ok"] or not roads.retryable(outcome):
                        break
                if not outcome["ok"]:
                    break
                end = (outcome["end"]["x"], outcome["end"]["z"])
            return {"ok": False, "stage": "landNotClear", "end": {"x": end[0], "z": end[1]}}
    return {"ok": False, "stage": "noLand"}


def place_site(label: str, prefab: str, site: dict[str, Any]) -> dict[str, Any]:
    """The prefab on one proposed site (propose_service_sites), its growables cleared first once
    every one's bulldoze preview is safe; stage "unsafe" names those the game would not release."""
    unsafe = [covered["id"] for covered in site["coveredGrowables"]
              if not mt.call_tool("preview_bulldoze_impact", {"entityType": "building", "id": covered["id"]}).get("safe")]
    if unsafe:
        return {"prefab": prefab, "ok": None, "stage": "unsafe", "unsafe": unsafe, "position": site["position"]}
    cleared = []
    for covered in site["coveredGrowables"]:
        outcome = protocol.run(f"{label}-clear-{covered['id']}", "bulldoze_entity",
                               {"entityType": "building", "id": covered["id"]})
        if not outcome["ok"]:
            return {"prefab": prefab, "ok": False, "stage": "clearing", "cleared": cleared,
                    "position": site["position"]}
        cleared.append(covered["id"])
    outcome = protocol.run(label, "place_service", {"buildingPrefab": prefab, "position": site["position"],
                                                    "angleDegrees": site["angleDegrees"]}, 5)
    return {"prefab": prefab, "ok": outcome["ok"], "regressionOk": outcome["regressionOk"],
            "buildingId": (outcome.get("result") or {}).get("buildingId"), "position": site["position"],
            "cleared": cleared}


def place_near(label: str, prefab: str, near: dict[str, float], polluting: bool = False) -> dict[str, Any]:
    """The prefab on the first proposed site whose growables may all be cleared: every one's bulldoze
    preview is read before any is cleared, and a site with one the game would not release is passed
    for the next (live: a school's first site held a building whose clearing was unsafe, and the
    other sites were never tried)."""
    sites: dict[str, Any] = {"sites": []}
    for radius in (POLLUTER_RADII if polluting else (500, 1000)):
        sites = mt.call_tool("propose_service_sites", {"buildingPrefab": prefab, "near": near, "radius": radius,
                                                        "limit": 3})
        if sites["sites"]:
            break
    passed = []
    for site in sites["sites"]:
        placed = place_site(label, prefab, site)
        if placed.get("stage") == "unsafe":
            passed.append({"position": site["position"], "unsafe": placed["unsafe"]})
            continue
        return {**placed, **({"passed": passed} if passed else {})}
    if passed:
        # nothing was cleared: the cluster waits for the next wake, when other sites may be free
        return {"prefab": prefab, "ok": None, "stage": "sitesTaken", "passed": passed, "near": near}
    return {"prefab": prefab, "ok": False, "stage": "noSite", "near": near}


LOT_TAKEN = "The lot cannot take the rebuilt facility"


def clear_lot(label: str, building_id: int) -> list[int] | None:
    """Clear the growables that have grown onto a full facility's lot, each after its own bulldoze
    preview, as for a new site; None when a service building or road stands in the way or a
    clearing is refused (live: a sweatshop beside a full landfill kept it from being rebuilt)."""
    facility = next((item for item in mt._request("GET", "/state/facilities", query={"limit": 50000})
                     .get("facilities", []) if int(item["id"]) == building_id), None)
    if facility is None:
        return None
    preview = mt._request("POST", "/commands/preview-placement", body={
        "buildingPrefab": facility["prefab"], "position": {"x": facility["position"]["x"], "z": facility["position"]["z"]},
        "angleDegrees": facility.get("angleDegrees", 0), "requireRoadAccess": True, "ignoreBuildingId": building_id})
    growables = {int(item["id"]) for item in next(
        (value for value in mt._request("GET", "/state/growables", query={"limit": 50000}).values()
         if isinstance(value, list)), [])}
    covered = [int(item["id"]) for item in preview.get("buildingOverlaps") or []]
    if not covered or preview.get("roadOverlaps") or any(item not in growables for item in covered):
        return None
    for covered_id in covered:
        if not mt.call_tool("preview_bulldoze_impact", {"entityType": "building", "id": covered_id}).get("safe"):
            return None
        if not protocol.run(f"{label}-clear-{covered_id}", "bulldoze_entity",
                            {"entityType": "building", "id": covered_id})["ok"]:
            return None
    return covered


def rebuild_full(building_ids: list[int]) -> list[dict[str, Any]]:
    """Each full landfill or cemetery rebuilt on its own lot, the growables grown onto the lot
    cleared first. A lot still taken leaves the full one standing (ok None, lotTaken), not a failed
    wake: it counts as a fill all the same, so the family soon builds what does not fill."""
    results = []
    for building_id in building_ids:
        label = f"rebuild-full-{building_id}"
        outcome = protocol.run(label, "rebuild_full_facility", {"buildingId": building_id}, 5)
        cleared = None
        if not outcome["ok"] and LOT_TAKEN in str((outcome.get("result") or {}).get("error")):
            cleared = clear_lot(label, building_id)
            if cleared:
                outcome = protocol.run(label, "rebuild_full_facility", {"buildingId": building_id}, 5)
        error = (outcome.get("result") or {}).get("error")
        result = {"label": label, "ok": outcome["ok"], "regressionOk": outcome["regressionOk"], "error": error,
                  "cleared": cleared}
        if not outcome["ok"] and LOT_TAKEN in str(error):
            result.update(ok=None, stage="lotTaken")
        results.append(result)
    return results


def raise_budget(family: str, label: str) -> dict[str, Any]:
    service = BUDGET_SERVICE[family]
    current = next((item["day"] for item in mt._request("GET", "/state/budgets")["budgets"]
                    if item["service"] == service and item.get("subService", "None") == "None"), 100)
    if current >= BUDGET_MAX:
        return {"budget": service, "ok": False, "stage": "atMaximum", "value": current}
    outcome = protocol.run(f"budget-{family}-{label}", "set_service_budget", {
        "service": service, "budget": min(BUDGET_MAX, current + BUDGET_STEP), "dayAndNight": True,
        "reason": "critical-capacity", "expectedOutcome": f"more {family} vehicles reach the unserved cluster"})
    if outcome.get("error"):  # a fiscal cooldown refuses budget changes for a while: try next wake
        return {"budget": service, "ok": None, "stage": "cooldown", "error": outcome["error"][:120]}
    return {"budget": service, "ok": outcome["ok"], "regressionOk": outcome["regressionOk"],
            "value": min(BUDGET_MAX, current + BUDGET_STEP)}


def fire_gap() -> dict[str, Any] | None:
    """The FIRE_CELL with the most buildings beyond every fire station's own radius (the prefab's,
    REACH["fire"] when unknown), when it holds at least FIRE_COVERAGE_MIN: their centre and count.
    Counted at 650m, nine stations of 700-1200m left 615 buildings uncovered and a station went up
    every wake; at their own radii 32 were (live)."""
    radii = {option["name"]: float((option.get("roles") or {}).get("radius") or 0) for option in
             mt.call_tool("compare_service_options", {"family": "fire", "need": "coverage"}).get("options", [])}
    stations = [((item["position"]["x"], item["position"]["z"]), radii.get(str(item.get("prefab"))) or REACH["fire"])
                for item in mt._request("GET", "/state/facilities",
                                        query={"service": "FireDepartment", "limit": 500}).get("facilities", [])
                if item.get("position")]
    growables = next((value for value in mt._request("GET", "/state/growables", query={"limit": 50000}).values()
                      if isinstance(value, list)), [])
    cells: dict[tuple[int, int], list[tuple[float, float]]] = {}
    for building in growables:
        if building.get("abandoned") or not building.get("position"):
            continue
        point = (building["position"]["x"], building["position"]["z"])
        if any(math.dist(point, station) <= radius for station, radius in stations):
            continue
        cells.setdefault((round(point[0] / FIRE_CELL), round(point[1] / FIRE_CELL)), []).append(point)
    if not cells:
        return None
    points = max(cells.values(), key=len)
    if len(points) < FIRE_COVERAGE_MIN:
        return None
    return {"center": {"x": sum(x for x, _ in points) / len(points), "z": sum(z for _, z in points) / len(points)},
            "count": len(points)}


def judge_placements(state: dict[str, Any], assessment: dict[str, Any]) -> list[dict[str, Any]]:
    """A placement that left a cluster of the same problem around it after OUTCOME_WAKES did not work
    for that need: the next choice for the need skips that building."""
    verdicts = []
    clusters = {entry["problem"]: entry.get("clusters", []) for entry in assessment["problems"]}
    for record in state.setdefault("placed", []):
        if record.get("verdict") or state["wake"] - record["wake"] < OUTCOME_WAKES:
            continue
        remaining = [cluster for cluster in clusters.get(record["problem"], [])
                     if math.dist((cluster["center"]["x"], cluster["center"]["z"]),
                                  (record["center"]["x"], record["center"]["z"])) < 600]
        worked = not remaining or max(cluster["count"] for cluster in remaining) < record["count"]
        record["verdict"] = "worked" if worked else "ineffective"
        ineffective = state.setdefault("ineffective", {}).setdefault(f"{record['family']}:{record['need']}", [])
        if not worked and record["prefab"] not in ineffective:
            ineffective.append(record["prefab"])
        if worked and record["prefab"] in ineffective:
            ineffective.remove(record["prefab"])
        verdicts.append({key: record[key] for key in ("problem", "prefab", "need", "verdict")})
    pending = [record for record in state["placed"] if not record.get("verdict")]
    judged = [record for record in state["placed"] if record.get("verdict")]
    state["placed"] = pending[-20:] + judged[-20:]
    return verdicts


def streak(state: dict[str, Any], name: str) -> int:
    """Consecutive wakes in which the problem was reported, this one included."""
    record = state.setdefault("streaks", {}).get(name, {"wake": -1, "length": 0})
    length = record["length"] + 1 if record["wake"] == state["wake"] - 1 else 1
    state["streaks"][name] = {"wake": state["wake"], "length": length}
    return length


BURN_WAVE = 10  # burned ruins in one knot that call for another fire station there


def with_ruins(problems: list[dict[str, Any]], ruined: dict[str, int] | None) -> list[dict[str, Any]]:
    """The economy problems counted with the buildings abandoned of them this wake: a building that
    goes abandoned leaves the count, so the worse the problem the fewer it showed (live: NoResources
    read 0 while 51 factories had been abandoned of it over 30 wakes). One with no building left
    showing it is added from the ruins alone."""
    ruined = ruined or {}
    counted = []
    for entry in problems:
        extra = ruined.get(entry["problem"], 0) if (entry.get("remedy") or {}).get("kind") == "economy" else 0
        counted.append({**entry, "count": entry["count"] + extra, **({"ruined": extra} if extra else {})})
    present = {entry["problem"] for entry in problems}
    for name, count in ruined.items():
        remedy = mayor_problems.REMEDIES.get(name) or {}
        if name not in present and remedy.get("kind") == "economy" and count:
            counted.append({"problem": name, "count": count, "remedy": remedy, "buildingIds": [], "ruined": count})
    return counted


def solve(city: City, label: str, high_density: bool = False, offices: bool = False,
          burned: dict[str, Any] | None = None, ruined: dict[str, int] | None = None) -> dict[str, Any]:
    state = city.load_state("services", {"clusters": {}, "counts": {}, "noSite": {}, "wake": 0})
    state["wake"] = state.get("wake", 0) + 1
    assessment = mt.call_tool("assess_problem_remedies", {"clusterLimit": 4})
    assessment = {**assessment, "problems": with_ruins(assessment.get("problems") or [], ruined)}
    actions: list[dict[str, Any]] = []
    escalations: list[dict[str, Any]] = []
    seen: set[str] = set()
    verdicts = judge_placements(state, assessment)
    if verdicts:
        actions.append({"judged": verdicts})
    placements = 0
    # one new facility per family a wake: the clusters were read before it stood, and the next
    # wake judges what it left unserved (live: two landfills for two garbage clusters in one wake)
    placed_families: set[str] = set()
    health: dict[str, Any] | None = None  # city_health, read once the first capacity need asks for it
    full = sorted({facility for entry in assessment["problems"]
                   for facility in (entry.get("facilities") or {}).get("full", [])})
    if full:
        actions.append({"rebuildFull": full, "results": rebuild_full(full)})
        rebuilds = state.setdefault("fullRebuilds", {})
        for entry in assessment["problems"]:
            if (entry.get("facilities") or {}).get("full") and entry["remedy"].get("family"):
                family = entry["remedy"]["family"]
                rebuilds[family] = [wake for wake in rebuilds.get(family, [])
                                    if state["wake"] - wake < FILLS_UP_WINDOW] + [state["wake"]]
    if burned and burned["count"] >= BURN_WAVE and placements < MAX_PLACEMENTS and "fire" not in placed_families:
        # a fire wave is out before the next wake reads it: the ruins cleared this wake are all that
        # shows it, so the stations there had too few engines, covered or not (live: 66 industrial
        # buildings burned in one wake on ground with full fire coverage, the nearest station 267m).
        # It goes before the other needs: live, a crematory and a landfill took the wake's two
        # placements while 61 homes had burned
        result = {"ok": False, "stage": "noOption"}
        offered = [item for item in choose_prefabs("fire", "capacity", state, SPEND_SHARE)
                   if item.get("affordable", True)][:3]
        for option in offered:
            result = place_near(f"fix-fire-wave-{label}", option["name"], burned["center"])
            if result.get("stage") not in ("noSite", "sitesTaken"):
                break
        actions.append({"problem": "BurnWave", "cluster": burned["count"], "need": "capacity", **result})
        if result.get("ok"):
            placements += 1
            placed_families.add("fire")
    for entry in assessment["problems"]:
        name, remedy = entry["problem"], entry["remedy"]
        state["counts"][name] = (state["counts"].get(name, []) + [entry["count"]])[-4:]
        kind = remedy["kind"]
        if kind == "unmapped":
            escalations.append({"unmapped": name, "count": entry["count"]})
        elif kind == "transitLine":
            length = streak(state, name)
            if length >= PERSIST:
                escalations.append({"transitLineBroken": name, "wakes": length, "count": entry["count"]})
        elif kind == "persist":  # repaired elsewhere in the wake or by time, unless it stays
            length = streak(state, name)
            if length >= PERSIST:
                escalations.append({"persists": name, "wakes": length, "count": entry["count"],
                                    "lever": remedy.get("lever")})
        elif kind == "economy" and name != "LandValueLow":
            counts = state["counts"][name]
            rising = len(counts) == 4 and counts[0] < counts[1] < counts[2] < counts[3] and counts[3] >= ECONOMY_MIN
            if not rising and remedy.get("lever") == "industrySupply" and len(counts) == 4 \
                    and min(counts) >= SUPPLY_PERSIST:
                supplied = industry.supply(city, label, state["wake"], counts[-1])
                if supplied is not None:
                    actions.append({"problem": name, "specialised": supplied, **{
                        key: supplied.get(key) for key in ("ok", "stage", "regressionOk")}})
            persisting = len(counts) == 4 and min(counts) >= SHOPS_PERSIST
            if rising or (persisting and remedy.get("lever") == "slowCommercial"):
                if remedy.get("lever") == "slowCommercial":
                    # shops short of customers: empty commercial land by them becomes homes (or
                    # offices where homes may not go); the planner hears of it only when none is left
                    rezoned = growth.rezone_idle_shops(label, entry.get("buildingIds") or [], high_density,
                                                       offices)
                    if any(item["ok"] for item in rezoned):
                        actions.append({"problem": name, "rezonedForHomes": rezoned,
                                        "ok": all(item["ok"] for item in rezoned)})
                        continue
                if remedy.get("lever") == "industrySupply":
                    # industry short of goods: a specialised district on renewable ground supplies it
                    # (loop.industry); the planner hears of it only when none can be added
                    supplied = industry.supply(city, label, state["wake"], counts[-1])
                    if supplied is not None:
                        actions.append({"problem": name, "specialised": supplied, **{
                            key: supplied.get(key) for key in ("ok", "stage", "regressionOk")}})
                        continue
                if not rising:
                    continue  # persisting with no empty commercial land left: the planner hears when it rises
                escalation = {"economyRising": name, "counts": counts, "lever": remedy.get("lever")}
                if name == "Noise":  # the planner is told what makes the noise, not only that it rises
                    noise = mt.call_tool("diagnose_noise", {"limit": 8})
                    escalation["sources"] = {"bySource": noise.get("bySource"),
                                             "homes": [{"id": home["id"], "sources": home["sources"]}
                                                       for home in noise.get("homes", [])]}
                escalations.append(escalation)
        elif kind == "service":
            family = remedy["family"]
            for cluster in entry.get("clusters", []):
                key = _key(name, cluster)
                seen.add(key)
                wakes = state["clusters"].get(key, 0) + 1
                state["clusters"][key] = wakes
                nearest = cluster.get("nearestWorkingFacility")
                far = nearest is None or nearest["distance"] > REACH.get(family, 650)
                persists = wakes >= PERSIST and (far or cluster["count"] >= PERSIST_CLUSTER)
                if not ((far and cluster["count"] >= IMMEDIATE_CLUSTER) or persists) \
                        or placements >= MAX_PLACEMENTS or family in placed_families:
                    continue
                if state["noSite"].get(key, 0) > 0:
                    state["noSite"][key] += 1  # the budget lever has been pulled; give it time
                    if state["noSite"][key] > ESCALATE_AFTER:
                        reported = state.setdefault("reportedAt", {})
                        if state["wake"] - reported.get(key, -SERVICE_REPORT_EVERY) >= SERVICE_REPORT_EVERY:
                            reported[key] = state["wake"]
                            escalations.append({"serviceUnresolved": name, "center": cluster["center"],
                                                "wakes": wakes})
                        state["noSite"][key] = 0  # the next wake it persists looks for a site again
                    continue
                fills = len(state.get("fullRebuilds", {}).get(family, [])) >= FILLS_UP_REBUILDS
                need = "fillsUp" if fills else "coverage" if far else "capacity"
                if need != "coverage":
                    if health is None:
                        health = mt.city_health()
                    load = idle_load(family, health)
                    if load is not None:
                        actions.append({"problem": name, "cluster": cluster["count"], "ok": None,
                                        "stage": "capacityIdle", "need": need, "utilization": round(load, 2)})
                        continue
                offered = choose_prefabs(family, need, state, FIRST_SPEND_SHARE if nearest is None else SPEND_SHARE)
                options = [option for option in offered if option.get("affordable", True)][:3]
                if offered and not options:
                    # the building the need takes costs more than the city may spend yet: it saves
                    # up, as a budget raise funds no building that is not there (live: noSite was
                    # reported and the education budget raised for a high school it could not buy)
                    cheapest = min(offered, key=lambda option: float(option.get("constructionCost") or 0))
                    actions.append({"problem": name, "cluster": cluster["count"], "ok": None, "stage": "saving",
                                    "prefab": cheapest["name"], "cost": cheapest.get("constructionCost")})
                    if wakes >= SAVING_WAKES:  # the money is not coming: the planner decides
                        escalations.append({"serviceUnaffordable": name, "center": cluster["center"],
                                            "prefab": cheapest["name"], "cost": cheapest.get("constructionCost"),
                                            "wakes": wakes})
                    continue
                placements += 1
                result: dict[str, Any] = {"ok": False, "stage": "noSite"}
                for option in options:
                    result = place_near(f"fix-{family}-{label}-{placements}", option["name"], cluster["center"],
                                        polluting=bool(option.get("pollutionRadius")))
                    if result.get("stage") not in ("noSite", "sitesTaken"):  # a smaller one may fit elsewhere
                        break
                polluting = next((option for option in options if option.get("pollutionRadius")), None)
                if result.get("stage") == "noSite" and polluting:
                    # no road in town lies far enough from the homes: lay one out into open land
                    land = open_land(city, f"fix-{family}-{label}", 2 * float(polluting["pollutionRadius"]))
                    if land["ok"]:
                        result = place_near(f"fix-{family}-{label}-{placements}", polluting["name"], land["end"],
                                            polluting=True)
                    result["land"] = land
                result["need"] = need
                if result["ok"]:
                    placed_families.add(family)
                    state["clusters"][key] = 0
                    state.setdefault("placed", []).append({
                        "problem": name, "family": family, "need": need, "prefab": result["prefab"],
                        "center": cluster["center"], "count": cluster["count"], "wake": state["wake"]})
                    actions.append({"problem": name, "cluster": cluster["count"],
                                    "reason": "far" if far else "persists", **result})
                elif result.get("stage") == "noSite":
                    state["noSite"][key] = 1
                    # a higher budget sends more vehicles from the buildings there are; with none
                    # it changes nothing, and the cluster goes to the planner after ESCALATE_AFTER
                    lever = raise_budget(family, label) if nearest is not None else \
                        {"ok": None, "stage": "noFacilityToFund"}
                    actions.append({"problem": name, "cluster": cluster["count"], "reason": "noSiteBudget",
                                    "land": result.get("land"), **lever})
                else:
                    actions.append({"problem": name, "cluster": cluster["count"], **result})
    burning = any(entry["problem"] in ("Fire", "BurnedDown") for entry in assessment["problems"])
    if burning and placements < MAX_PLACEMENTS and "fire" not in placed_families:
        gap = fire_gap()
        if gap is not None:
            offered = choose_prefabs("fire", "coverage", state, SPEND_SHARE)
            result: dict[str, Any] = {"ok": False, "stage": "noOption"}
            for option in [item for item in offered if item.get("affordable", True)][:3]:
                result = place_near(f"fix-fire-coverage-{label}", option["name"], gap["center"])
                if result.get("stage") not in ("noSite", "sitesTaken"):
                    break
            if offered:
                actions.append({"problem": "FireCoverage", "cluster": gap["count"], "need": "coverage", **result})
            if result.get("ok"):
                placements += 1
                placed_families.add("fire")
    low = next((entry for entry in assessment["problems"] if entry["problem"] == "LandValueLow"), None)
    if low and placements < MAX_PLACEMENTS:
        seen.add("LandValueLow")
        state["clusters"]["LandValueLow"] = state["clusters"].get("LandValueLow", 0) + 1
        park = best_park()
        if state["clusters"]["LandValueLow"] >= PERSIST and park:
            rows = [row for row in mt._request("GET", "/state/problems", query={"limit": 50000}).get("problems", [])
                    if "LandValueLow" in (row.get("problemNames") or [])]
            if rows:
                center = {"x": sum(r["position"]["x"] for r in rows) / len(rows),
                          "z": sum(r["position"]["z"] for r in rows) / len(rows)}
                result = place_near(f"fix-park-{label}", park, center)
                actions.append({"problem": "LandValueLow", "count": low["count"], **result})
                if result["ok"]:
                    state["clusters"]["LandValueLow"] = 0
    state["clusters"] = {key: value for key, value in state["clusters"].items() if key in seen}
    state["noSite"] = {key: value for key, value in state["noSite"].items() if key in seen}
    escalations = fresh_reports(state, escalations)
    city.save_state("services", state)
    return {"problems": {entry["problem"]: entry["count"] for entry in assessment["problems"]},
            "actions": actions, "escalations": escalations}


def fresh_reports(state: dict[str, Any], escalations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The escalations not reported in the last SERVICE_REPORT_EVERY wakes, by kind and problem: a
    problem still rising or persisting was reported again every wake and stopped the loop each time
    (live: NoResources rising 5, 7, 11, 13 while its cause, the one road out of town, waited for the
    planner)."""
    reported = state.setdefault("reportedAt", {})
    fresh = []
    for escalation in escalations:
        kind = next(iter(escalation))
        if kind == "serviceUnresolved":
            fresh.append(escalation)  # paced by its own cluster key
            continue
        key = f"{kind}:{escalation[kind]}"
        if state["wake"] - reported.get(key, -SERVICE_REPORT_EVERY) >= SERVICE_REPORT_EVERY:
            reported[key] = state["wake"]
            fresh.append(escalation)
    return fresh

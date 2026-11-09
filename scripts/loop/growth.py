"""Grow the city each wake: zone the uses in demand, densify, and open new land.

For each use (residential, commercial, workplace):
- allowance: inspect_growth_capacity's pacing, raised when last wake's new blocks were absorbed
  (developable vacancy grew by less than a quarter of the cells zoned: double what was zoned, up
  to ABSORB_MAX_BLOCKS) and when vacancy that never builds holds the pacing back (demand at least
  20 and vacancy unchanged for STALE_WAKES: a few blocks anyway). A large city absorbs a fixed
  vacancy target within one wake; industry took 3 blocks a wake while unemployment rose 3% -> 15%.
- blocks: propose_zoning_blocks (neighbourhood use within distance rules; the city plan overrides).
  Once high density is unlocked, homes and shops on paved roads are zoned high density from the
  start (a low-density block densified a wake later loses the buildings it just grew); on gravel,
  or before the unlock, low density.
- no block left and demand at least DENSIFY_DEMAND: built-up low-density blocks become high density,
  centre first.
- still none and demand at least EXPAND_DEMAND: new land. The best area from
  propose_expansion_areas reachable without crossing gets an access road and a fishbone, a few
  collector steps a wake, until the land ends. Every clear way in is tried in turn (homes in the
  way are cleared; ground that will not hold a road at an existing junction is skipped). Land
  beyond a railway, highway or road is reached over an overpass (propose_network_crossing); land
  across water goes to the planner. A new city with no road yet starts from the highway: its
  stubs (an exit and an entrance) are joined at one junction and the first collector grows on.
- no land the use can reach on the tiles owned: the next map tile is bought (loop.land), and the
  following wakes open it; with none to buy, the planner decides.
"""

from __future__ import annotations

import math
from typing import Any

from mayor import land_use, layouts

from loop import bridge, district, land, protocol, roads, utilities
from loop.city import City, mt
from road_attempts import GATE_POLICY

USES = ("residential", "commercial", "workplace")
LOW_ZONE = {"residential": "ResidentialLow", "commercial": "CommercialLow", "workplace": "Industrial",
            "office": "Office"}
HIGH_ZONE = {"residential": "ResidentialHigh", "commercial": "CommercialHigh"}
EXPAND_USES = ("residential", "workplace")  # shops follow on the through roads of new districts
ABSORB_DEMAND = 30
ABSORB_MAX_BLOCKS = 12
CELLS_PER_BLOCK = 24
STALE_WAKES = 3
# workers short: the jobs wait for people, so homes are zoned each wake while they are wanted, not
# only once a vacancy has sat for STALE_WAKES (live: unemployment 2%, workplace demand 50,
# residential demand 45 held at a 24-cell tier, and the town shrank by 0.5% a wake)
LABOUR_SHORT = 3  # unemployment, percent, at or below which workers are short
LABOUR_DEMAND = 30  # workplace demand that says the jobs are still wanted
LABOUR_HOMES = 2  # residential blocks a wake while workers are short (and none for jobs)
# jobs are short: many without work while jobs are wanted, though the zoned workplace land is held
# to be enough (live: unemployment rose 2% to 7% over ten wakes, 439 open jobs for some 2,200 out of
# work at workplace demand 62, no block zoned while 183 vacant cells were slow to be built on)
JOBS_SHORT = 6  # unemployment, percent, at or above which jobs are short
# workplace blocks kept ready to build on while jobs are short, for each point of unemployment from
# JOBS_SHORT, up to JOBS_MAX; land already zoned and empty counts toward them. The town builds jobs at
# the pace demand sets, not the pace land is zoned (live: three blocks a wake held unemployment at 7%
# for seven wakes; nine to twelve a wake for nine more held it there too, while the empty workplace
# land grew from 156 to 294 cells)
JOBS_BLOCKS = 3
JOBS_MAX = 12
# cells the vacancy must fall by, from where the wait began, to count as being built on: scraps a
# block cannot grow on wobble it by a few cells either way (live: 77, 86, 77 cells for four wakes at
# 27,200 people with residential demand at 48, and no block zoned)
STALE_DROP = 6
DENSIFY_DEMAND = 40
DENSIFY_PER_WAKE = 6
DENSIFY_SCAN = 60  # zone-cell reads per wake while looking for low-density blocks
EXPAND_DEMAND = 40
EXPAND_STEPS_PER_WAKE = 4
EXPAND_EDGES_PER_WAKE = 10  # roads of a non-fishbone district laid per wake
FAILED_START_RADIUS = 60.0  # a way in landing where a collector could not start is not used again...
FAILED_START_WAKES = 20  # ...for this many wakes: the city and the loop's road building change
ACCESS_TRIES = 8  # clear ways in tried per wake
CROSSING_WAYS = 3  # ways into one area an overpass is tried from (live: the first collided, two others held)
THROUGH_MAX = 4  # buildings a way into new land may clear when no other way is left (never homes)
THROUGH_LINES = 1  # power line segments it may take down (and the grid is joined again behind it)
THROUGH_TRIES = 3
STUB_PAIR = 300.0  # an exit and an entrance stub this close are joined at one junction
OUTSIDE_TRIES = 4  # link attempts built per wake
OUTSIDE_FAILURES = 16  # failed link attempts in all before the planner decides: each costs the roads it built
# (counted under one road gate policy: attempts the gates refused before they changed say nothing now)
CONTINUATIONS = (60.0, 90.0, 40.0)  # how far each stub runs on before the two meet
LEG_GAP = 24.0  # two 16m-wide roads that do not meet keep this far apart (centre lines)
MEET_ANGLE = 30.0  # sharpest angle two roads of one attempt may meet at
JOIN_DISTANCES = (40.0, 60.0, 25.0)  # how far beyond two roads already run on from the stubs they meet
CROSSING_REPORT_EVERY = 20  # wakes between reports of land that needs a crossing
LAND_REPORT_EVERY = 8  # wakes between reports that a use has no land it can reach and no tile to buy


def absorbed_allowance(category: dict[str, Any], record: dict[str, Any] | None) -> int | None:
    """Twice what was zoned last wake when it was absorbed and demand is still high; None otherwise."""
    vacancy = category.get("developableVacantCells")
    if not record or not record.get("zonedBlocks") or vacancy is None or (category.get("demand") or 0) < ABSORB_DEMAND:
        return None
    if vacancy - (record.get("vacancy") or 0) < record["zonedBlocks"] * CELLS_PER_BLOCK / 4:
        return min(record["zonedBlocks"] * 2, ABSORB_MAX_BLOCKS)
    return None


def stale_allowance(category: dict[str, Any], record: dict[str, Any]) -> tuple[int | None, dict[str, Any]]:
    """A few blocks when the pacing has waited STALE_WAKES with demand and a vacancy that is not
    being built on: the occupied cells have not grown STALE_DROP cells since the wait began (or,
    without an occupancy reading, the vacancy has not fallen STALE_DROP cells). The occupied cells
    say what was built; the vacancy also moves with rezoning and cleared lots (live: residential
    demand 63, vacancy 115-148 that rose and fell with rezoned shop land, so the wait kept starting
    over while the town stood at 49,400)."""
    # a pacing that allows one block keeps waiting too: alternating 0 and 1 the wait never added up
    # (live: 27,200 people, residential demand 50, allowances 1, 0, 0, 1 and the town shrinking)
    waiting = category.get("maximumAdditionalBlocksThisWake", 0) <= 1 and (category.get("demand") or 0) >= 20
    vacancy = category.get("developableVacantCells")
    occupied = category.get("occupiedCells")
    began, built = record.get("vacancy"), record.get("occupied")
    if occupied is not None and built is not None:
        unbuilt = occupied < built + STALE_DROP
    else:
        unbuilt = vacancy is not None and began is not None and vacancy > began - STALE_DROP
    if waiting and vacancy is not None and record.get("waits") and unbuilt:
        record = {"vacancy": began, "occupied": built, "waits": record.get("waits", 0) + 1}
    else:
        record = {"vacancy": vacancy, "occupied": occupied, "waits": 1 if waiting else 0}
    if record["waits"] >= STALE_WAKES:
        return 3, {"vacancy": vacancy, "occupied": occupied, "waits": 0}
    return None, record


def _unemployment() -> float | None:
    """The city's unemployment, or None when it cannot be read: the labour rules then stand aside."""
    try:
        return (mt.city_health().get("employment") or {}).get("unemployment")
    except (mt.BridgeRequestError, RuntimeError):
        return None


def jobs_unfilled(categories: dict[str, Any], unemployment: float | None) -> bool:
    """Few are without work while homes are wanted: new jobs would wait for the people the homes
    bring, whatever the workplace demand (live: unemployment 2%, workplace demand 27 just under
    LABOUR_DEMAND, and three more workplace blocks were zoned)."""
    return unemployment is not None and unemployment <= LABOUR_SHORT \
        and (categories["residential"].get("demand") or 0) >= LABOUR_DEMAND


def labour_short(categories: dict[str, Any], unemployment: float | None) -> bool:
    """Workers are short: few without work, jobs still wanted, and homes wanted too."""
    return unemployment is not None and unemployment <= LABOUR_SHORT \
        and (categories["workplace"].get("demand") or 0) >= LABOUR_DEMAND \
        and (categories["residential"].get("demand") or 0) >= LABOUR_DEMAND


def jobs_short(categories: dict[str, Any], unemployment: float | None) -> bool:
    """Jobs are short: many without work and jobs still wanted."""
    return unemployment is not None and unemployment >= JOBS_SHORT \
        and (categories["workplace"].get("demand") or 0) >= LABOUR_DEMAND


def allowances(categories: dict[str, Any], state: dict[str, Any],
               unemployment: float | None = None) -> dict[str, int]:
    result = {}
    for use in USES:
        category = categories[use]
        base = int(category.get("maximumAdditionalBlocksThisWake") or 0)
        stale, state["stale"][use] = stale_allowance(category, state["stale"].get(use, {}))
        absorbed = absorbed_allowance(category, state["absorption"].get(use))
        result[use] = max(base, stale or 0, absorbed or 0)
    if labour_short(categories, unemployment):
        result["residential"] = max(result["residential"], LABOUR_HOMES)
    if jobs_short(categories, unemployment):
        wanted = min(JOBS_MAX, JOBS_BLOCKS * int(unemployment - JOBS_SHORT + 1)) \
            - int(categories["workplace"].get("developableVacantCells") or 0) // CELLS_PER_BLOCK
        result["workplace"] = max(result["workplace"], wanted)
    if jobs_unfilled(categories, unemployment):
        # no more jobs: zoned now they would stand short of workers too (live: six office blocks
        # zoned in the wake unemployment reached 2%)
        result["workplace"] = 0
    return result


def zone_workplace(city: City, allowance: int, label: str, skipped: list[int],
                   offices: bool, offices_first: bool = False) -> list[dict[str, Any]]:
    """Jobs: industrial blocks first, then, once offices are unlocked, offices for the rest of the
    allowance in the land between industry and homes (land_use: homes may not take it). Industry
    keeps its own districts; offices pollute nothing, so the buffer holds jobs instead of standing
    empty (live: 254 free blocks allowed only shops, unemployment rose to 12% while industry
    waited for land, and the town stalled at 6,400). Offices go first when they fill faster
    (office_fills_faster)."""
    order = ("office", "workplace") if offices and offices_first else ("workplace", "office") if offices \
        else ("workplace",)
    zoned: list[dict[str, Any]] = []
    for use in order:
        left = allowance - sum(1 for item in zoned if item["ok"])
        if left > 0:
            zoned += zone_blocks(city, use, left, label, skipped)
    return zoned


def office_fills_faster(capacity: dict[str, Any]) -> bool:
    """Office land stands less vacant than industrial land: the town's workers take office jobs
    sooner (an educated town wants offices over industry; live: 0.9% of office land vacant, 2.0% of
    industrial, while unemployment held at 7% and industry was zoned first)."""
    by_zone = {item.get("zone"): item for item in (capacity.get("zones") or {}).get("capacityByZone") or []}

    def vacant(zone: str) -> float:
        item = by_zone.get(zone) or {}
        return (item.get("developableVacantCells") or 0) / max(1, item.get("validUnsharedCells") or 0)
    return "Office" in by_zone and "Industrial" in by_zone and vacant("Office") < vacant("Industrial")


def zone_blocks(city: City, use: str, allowance: int, label: str, skipped: list[int],
                high: bool = False) -> list[dict[str, Any]]:
    """Zone up to `allowance` proposed blocks. A block whose zoning failed (a safety gate, a cell the
    listing does not see) is remembered in `skipped` and never proposed again."""
    if allowance <= 0:
        return []
    proposal = mt.call_tool("propose_zoning_blocks", {"use": use, "limit": allowance + 3 + min(len(skipped), 50),
                                                       "plan": city.plan})
    zoned = []
    for candidate in [item for item in proposal["candidates"] if item["blockId"] not in skipped]:
        if len([z for z in zoned if z["ok"]]) >= allowance:
            break
        zone = HIGH_ZONE[use] if high and use in HIGH_ZONE and candidate.get("roadPrefab") != "Gravel Road" \
            else LOW_ZONE[use]
        outcome = protocol.run(f"zone-{use[:3]}-{candidate['blockId']}", "zone_block",
                               {"zone": zone, "blockId": candidate["blockId"]})
        zoned.append({"blockId": candidate["blockId"], "zone": zone, "ok": outcome["ok"],
                      "regressionOk": outcome["regressionOk"],
                      "gateSkipped": not outcome["ok"] and "skipped by a safety gate" in str(outcome["result"]),
                      "error": None if outcome["ok"] else str(outcome.get("error") or outcome["result"])[:200]})
        if not outcome["ok"]:
            skipped.append(candidate["blockId"])
    return zoned


def densify(use: str, limit: int, center: tuple[float, float] | None, label: str) -> list[dict[str, Any]]:
    """Built-up low-density blocks on paved roads, nearest the centre first, rezoned high density:
    the game replaces the houses or shops within about a game month."""
    if use not in HIGH_ZONE or limit <= 0:
        return []
    blocks = [block for block in mt._request("GET", "/state/zone-blocks", query={"limit": 5000}).get("blocks", [])
              if block.get("roadPrefab") != "Gravel Road" and (block.get("zonedCells") or 0) >= 16
              and not block.get("occupancyClear") and block.get("developmentFrontageSafe")]
    if center:
        blocks.sort(key=lambda block: math.dist((block["center"]["x"], block["center"]["z"]), center))
    done = []
    for block in blocks[:DENSIFY_SCAN]:
        if len(done) >= limit:
            break
        cells = mt._request("GET", "/state/zone-cell-geometry", query={"blockId": block["blockId"]}).get("cells", [])
        if {cell["zone"] for cell in cells if cell.get("valid")} != {LOW_ZONE[use]}:
            continue
        outcome = protocol.run(f"densify-{use[:3]}-{block['blockId']}", "zone_block",
                               {"zone": HIGH_ZONE[use], "blockId": block["blockId"], "preserveOccupied": False})
        done.append({"blockId": block["blockId"], "zone": HIGH_ZONE[use], "ok": outcome["ok"],
                     "regressionOk": outcome["regressionOk"], "densified": True,
                     "gateSkipped": not outcome["ok"] and "skipped by a safety gate" in str(outcome["result"])})
    return done


def expand(city: City, use: str, state: dict[str, Any], label: str, small: bool = False) -> dict[str, Any]:
    """Continue the open expansion, or start one into the best clear area for the use."""
    current = state.get("expansion")
    if current is not None and not current.get("ended") and current.get("use") != use:
        # land for another use is sought where that use may grow: industry kept on extending a
        # residential district (live) never got a block, as homes stood too near
        current["ended"] = "otherUseWanted"
    if current is not None and current.get("ended") and current.get("collectorNodes", 0) <= 1 \
            and not current.get("failedRecorded"):
        # never started: its landing and the way that led there (a way steered off its line lands
        # far from its own ends, and was taken again; live: three stubs into one area)
        spots = [current["start"][:2]] + [end for end in current.get("way") or []]
        state.setdefault("failedStarts", []).extend([*spot, state.get("wake", 0)] for spot in spots)
        current["failedRecorded"] = True
    if current is None or current.get("ended"):
        proposal = mt.call_tool("propose_expansion_areas", {"use": use, "limit": 8, "plan": city.plan,
                                                             "includePoints": True})
        failed = [spot[:2] for spot in state.get("failedStarts", [])
                  if len(spot) > 2 and state.get("wake", 0) - spot[2] < FAILED_START_WAKES]
        areas = [{**area, "access": [way for way in area["access"] if not any(
            math.dist((way[end]["x"], way[end]["z"]), tuple(spot)) < FAILED_START_RADIUS
            for spot in failed for end in ("from", "to"))]} for area in proposal["areas"] if area.get("fishbone")]
        clear, landing, refused, used = None, None, [], None
        outside = state.get("outside") or {}
        if outside.get("junction") and not state.get("expansions"):
            # a new city's first collector grows on from the junction joining the highway stubs
            point = {"x": outside["junction"][0], "z": outside["junction"][1]}
            clear, landing = {"center": point, "fishbone": {"collectorHeadingDegrees": outside["heading"],
                                                            "branchLength": 96.0}}, tuple(outside["junction"])
        # land the planner asks for next (plan entries with crossTo) is reached first, over water too
        wanted = [area for area in areas if any(item.get("crossTo") and item["minX"] <= area["center"]["x"] <= item["maxX"]
                                                and item["minZ"] <= area["center"]["z"] <= item["maxZ"] for item in city.plan)]
        if clear is None and wanted and any("water" in (area["access"] or [{}])[0].get("crosses", []) for area in wanted):
            crossed = cross_water(city, wanted, label)
            if crossed.get("area"):
                clear, landing = crossed["area"], crossed["landing"]
            elif crossed.get("report", {}).get("ok") is not None:
                return {"use": use, **crossed["report"]}
            # a crossing waiting for money does not stop the town growing on this side meanwhile
        # homes start their district beyond industry's reach: a collector begun inside it lays
        # roads whose blocks only industry may take (live: a residential district opened from the
        # middle of the industrial bank got three branches of workplace-only land)
        industry = _industry() if use == "residential" else []
        if use in ("residential", "commercial"):
            # homes and shops grow on from the town, as a player extends it: the largest open land
            # a kilometre off, reached through the industrial bank, was opened first (live)
            town = _town()
            if town is not None:
                areas.sort(key=lambda area: math.dist(town, (area["center"]["x"], area["center"]["z"])))
        # ways that run over nothing first, from any area: a way over homes is refused piece by piece
        tries = sorted([(area, way) for area in areas for way in area["access"] if way["clear"]
                        and _homes_may_grow(way, industry)], key=lambda pair: bool(pair[1].get("blocked")))
        for area, way in tries[:ACCESS_TRIES]:
            if clear is not None:
                break
            opened = open_access(city, label, way)
            if opened["ok"]:
                clear, landing, used = area, opened["end"], way
                break
            refused.append({"nodeId": way.get("nodeId"), "stage": opened.get("stage"),
                            "reason": opened.get("reason")})
        if clear is None:
            # land beyond a railway, highway or road is reached over an overpass; water needs a bridge
            raised_failed: list[dict[str, Any]] = []
            for area in areas:
                ways = [way for way in area["access"] if "water" not in way["crosses"]][:CROSSING_WAYS]
                for way_index, way in enumerate(ways):
                    crossing = mt.call_tool("propose_network_crossing", {"start": way["from"], "target": way["to"]})
                    overpass = next((item for item in crossing["candidates"] if item["valid"]), None)
                    if overpass is None:
                        continue
                    raised: list[int] = []
                    for index, piece in enumerate(overpass["pieces"]):
                        outcome = roads.build_raised(f"expand-{label}-w{way_index}-o{index}", crossing["roadPrefab"],
                                                     piece)
                        if outcome.get("segmentId"):
                            raised.append(outcome["segmentId"])
                        if not outcome["ok"]:
                            # a ramp left standing leads nowhere: the pieces built come down again, and
                            # the next way in is tried (live: one way's first piece refused stopped the loop)
                            removed = _remove(f"overpass-{label}", raised)
                            failure = {"stage": f"overpass{index}", "pieceStage": outcome.get("stage"),
                                       "errors": outcome.get("errors"), "error": str(outcome.get("error") or "")[:200],
                                       "center": area["center"], "removed": removed,
                                       "busy": "command was not run" in str(outcome.get("error"))}
                            if set(removed) != set(raised):  # some of it still stands: the planner looks
                                return {"use": use, "ok": False, **failure, "left": sorted(set(raised) - set(removed))}
                            raised_failed.append(failure)
                            break
                        landing = (outcome["end"]["x"], outcome["end"]["z"])
                    else:
                        clear = area
                        break
                if clear is not None:
                    break
            if clear is None and raised_failed:
                if all(item["busy"] for item in raised_failed):
                    return {"use": use, "ok": None, "stage": "busy", "overpasses": raised_failed}
                return {"use": use, "ok": False, "stage": raised_failed[0]["stage"],
                        "center": raised_failed[0]["center"], "removed": [], "overpasses": raised_failed}
        if clear is None and refused:
            # the last way in runs through a few workplaces, as a player bulldozes a row of
            # factories to open a district (live: 2.2km2 of land behind a row of sweatshops, the
            # highway too steep to bridge, no tile left to buy)
            through = sorted([(area, way) for area in areas for way in area["access"]
                              if way["clear"] and 0 < int(way.get("blocked") or 0) <= THROUGH_MAX
                              and _homes_may_grow(way, industry)],
                             key=lambda pair: (pair[1]["blocked"], pair[1]["distance"]))
            for area, way in through[:THROUGH_TRIES]:
                opened = open_through(city, label, way)
                if opened["ok"]:
                    clear, landing, used = area, opened["end"], way
                    break
                refused.append({"nodeId": way.get("nodeId"), "stage": f"through-{opened.get('stage')}",
                                "reason": opened.get("reason")})
        if clear is None and any(item.get("stage") == "busy" for item in refused):
            return {"use": use, "ok": None, "stage": "busy", "refused": refused}  # tried again next wake
        if clear is None and refused:
            return {"use": use, "ok": False, "stage": "access", "refused": refused}
        if clear is None:
            crossed = cross_water(city, areas, label)
            if crossed.get("area"):
                clear, landing = crossed["area"], crossed["landing"]
            elif crossed.get("report"):
                return {"use": use, **crossed["report"]}
        if clear is None:
            # the planner decides on a grade separation; repeat the report only now and then
            if state.get("wake", 0) - state.get("crossingReportedAt", -CROSSING_REPORT_EVERY) < CROSSING_REPORT_EVERY:
                return {"use": use, "ok": None, "stage": "waitingForPlanner"}
            state["crossingReportedAt"] = state.get("wake", 0)
            return {"use": use, "ok": False, "stage": "needsCrossing",
                    "areas": [{"center": area["center"], "areaSquareMeters": area["areaSquareMeters"],
                               "crosses": area["access"][0]["crosses"] if area["access"] else None}
                              for area in areas[:3]]}
        layout, start = clear["fishbone"], landing
        # the layout's heading is for the area's first way in; from the way actually used the
        # collector keeps the area's axis, turned to point into the area
        heading = layout["collectorHeadingDegrees"]
        hx, hz = roads.heading_vector(heading)
        if (clear["center"]["x"] - start[0]) * hx + (clear["center"]["z"] - start[1]) * hz < 0:
            heading = heading - 180.0 if heading > 0 else heading + 180.0
        # the layout the land suits (a new city's first district: the land nearest the junction)
        land = clear if clear.get("points") else min(
            (area for area in proposal["areas"] if area.get("points")),
            key=lambda area: math.dist(start, (area["center"]["x"], area["center"]["z"])), default=None)
        choice = ((land or {}).get("layouts") or [{"layout": "fishbone", "reason": "no ranking for this land"}])[0]
        current = {"name": f"expansion-{len(state.get('expansions', [])) + 1}", "use": use,
                   "start": list(start), "heading": heading, "layout": choice["layout"],
                   "layoutReason": choice["reason"],
                   "branch": layout["branchLength"], "center": clear["center"], "ended": None,
                   "way": [[used[end]["x"], used[end]["z"]] for end in ("from", "to")
                           if "x" in (used or {}).get(end, {})]}
        state.setdefault("expansions", []).append(current["name"])
        state["expansion"] = current
        if current["layout"] != "fishbone":
            hx, hz = roads.heading_vector(heading)
            graph = layouts.graph(current["layout"], land, tuple(start), math.degrees(math.atan2(hz, hx)))
            return _grow_district(city, current, graph, small)
    if current.get("layout", "fishbone") != "fishbone":
        return _grow_district(city, current, None, small)  # the district keeps its graph in its own state
    branch = current["branch"]
    lengths = tuple(sorted({round(branch), round(max(48.0, branch * 0.75)), 48}, reverse=True))
    built = roads.grow_fishbone(city, current["name"], tuple(current["start"]), current["heading"],
                                1 if small else EXPAND_STEPS_PER_WAKE, branch_lengths=lengths,
                                label=f"{current['name']}")
    current["ended"] = built.get("ended")
    current["collectorNodes"] = built["collectorNodes"]
    grew = any(item["ok"] for item in built["collector"]) or any(item["ok"] for item in built["branches"])
    # ground that refuses every step only ends this collector: the next wake tries another way in;
    # a road that was built and then failed its checks is a failure
    broken = any(not item["ok"] and item.get("stage") not in ("preview", "busy", None) for item in built["collector"])
    return {"use": use, "ok": True if grew and not broken else (False if broken else None),
            "name": current["name"], "collectorNodes": built["collectorNodes"], "ended": current["ended"],
            "branchesBuilt": sum(1 for item in built["branches"] if item["ok"])}


def cross_water(city: City | None, areas: list[dict[str, Any]], label: str) -> dict[str, Any]:
    """Land reached only over water: the nearest such area gets a bridge (loop.bridge), and its
    first district is a bridgehead grown from where the bridge lands. Waiting for cash is reported,
    not failed; land no crossing reaches is left to the planner."""
    beyond = [area for area in areas if area.get("access") and "water" in area["access"][0]["crosses"]
              and area.get("points")]
    if not beyond:
        return {}
    area = min(beyond, key=lambda item: item["access"][0]["distance"])
    cash = mt.call_tool("inspect_economy", {})["cash"]["amount"] / 100.0
    crossing = bridge.cross(city, (area["center"]["x"], area["center"]["z"]), label, cash)
    if not crossing.get("ok"):
        return {"report": {"ok": crossing.get("ok"), "stage": f"bridge:{crossing.get('stage')}",
                           "crossing": {k: v for k, v in crossing.items() if k not in ("pieces", "ok", "stage")}}}
    return {"landing": tuple(crossing["landing"]),
            "area": {**area, "fishbone": {"collectorHeadingDegrees": crossing["headingDegrees"], "branchLength": 96.0},
                     "layouts": [{"layout": "bridgehead",
                                  "reason": f"first district across the water ({crossing['span']}m bridge)"}],
                     "bridge": {k: crossing[k] for k in ("roads", "cost", "span", "approach")}}}


def _grow_district(city: City, current: dict[str, Any], graph: dict[str, Any] | None,
                   small: bool = False) -> dict[str, Any]:
    """A district of any layout but fishbone: its road graph, a few edges a wake."""
    built = district.grow(city, current["name"], graph, 3 if small else EXPAND_EDGES_PER_WAKE, current["name"])
    current["ended"] = "layoutDone" if built["ended"] else None
    current["collectorNodes"] = built["nodesBuilt"]
    # ground that refuses every edge only ends this district; a road built and then failed is a failure
    # a road that failed its checks and was taken out again left nothing behind (live: one edge of a
    # ring of six, removed, stopped the loop as a failed district)
    broken = any(item["stage"] not in ("preview", "busy", None) and not item.get("removed")
                 for item in built["failed"])
    return {"use": current["use"], "ok": True if built["built"] and not broken else (False if broken else None),
            "name": current["name"], "layout": current["layout"], "layoutReason": current["layoutReason"],
            "collectorNodes": built["nodesBuilt"], "ended": current["ended"], "edgesBuilt": len(built["built"]),
            "edgesFailed": len(built["failed"]), "edgesLeft": built["edgesLeft"]}


def highway_stubs() -> list[dict[str, Any]]:
    """Highway ends inside the map that lead on to no other highway: where a new city attaches to the
    outside, whether or not town roads already run on from them. Each has its node, its tip and the
    unit direction pointing on into the map."""
    highways = [segment for segment in mt._request("GET", "/state/networks", query={"limit": 20000}).get("segments", [])
                if "Highway" in str(segment.get("prefab"))]
    ends: dict[int, int] = {}
    for segment in highways:
        for tip in ("start", "end"):
            ends[int(segment[f"{tip}NodeId"])] = ends.get(int(segment[f"{tip}NodeId"]), 0) + 1
    stubs = []
    for segment in highways:
        if segment.get("isTunnel") or segment.get("isUnderground"):
            continue
        for tip, back in (("start", "end"), ("end", "start")):
            if ends[int(segment[f"{tip}NodeId"])] != 1 or segment.get(f"{tip}Outside"):
                continue
            point = (segment[tip]["x"], segment[tip]["z"])
            dx, dz = point[0] - segment[back]["x"], point[1] - segment[back]["z"]
            length = math.hypot(dx, dz) or 1.0
            stubs.append({"nodeId": segment.get(f"{tip}NodeId"), "point": point, "inward": (dx / length, dz / length)})
    return stubs


def stub_pairs(stubs: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Exit and entrance stubs side by side, closest pairs first. A lone stub is one way only, so a
    town joined to it alone could be entered but not left (or the reverse)."""
    close = sorted((math.dist(a["point"], b["point"]), i, j) for i, a in enumerate(stubs)
                   for j, b in enumerate(stubs) if i < j and math.dist(a["point"], b["point"]) < STUB_PAIR)
    pairs, used = [], set()
    for _, i, j in close:
        if i not in used and j not in used:
            used |= {i, j}
            pairs.append([stubs[i], stubs[j]])
    return pairs


def _local_roads() -> tuple[list[dict[str, Any]], set[int], set[int]]:
    """Local road segments, the highway nodes, and the local roads that reach the outside."""
    from mayor import network
    segments = mt._request("GET", "/state/networks", query={"limit": 20000}).get("segments", [])
    highway_nodes = {int(s[f"{end}NodeId"]) for s in segments if "Highway" in str(s.get("prefab"))
                     for end in ("start", "end")}
    local = [s for s in segments if s.get("service") == "Road" and "Highway" not in str(s.get("prefab"))]
    reached, _ = network.road_components(segments)
    return local, highway_nodes, {int(s["id"]) for s in local} & reached


def dangling_stub_roads(local: list[dict[str, Any]], highway_nodes: set[int]) -> list[int]:
    """Roads left by a connection that never closed: on a highway node and ending in a dead end."""
    return [int(s["id"]) for s in local for tip, far in (("start", "end"), ("end", "start"))
            if int(s[f"{tip}NodeId"]) in highway_nodes and s.get(f"{far}NodeDegree") == 1]


def dangling_ends(local: list[dict[str, Any]], highway_nodes: set[int]) -> dict[int, tuple[float, float]]:
    """For each highway stub with a road already run on from it into the map: that road's far end."""
    return {int(s[f"{tip}NodeId"]): (s[far]["x"], s[far]["z"]) for s in local
            for tip, far in (("start", "end"), ("end", "start"))
            if int(s[f"{tip}NodeId"]) in highway_nodes and s.get(f"{far}NodeDegree") == 1}


def _joins(pair: list[dict[str, Any]], ends: dict[int, tuple[float, float]]
           ) -> list[tuple[list[tuple[tuple[float, float], tuple[float, float]]], tuple[float, float], float]]:
    """When both stubs of a pair already run on into the map, as a player would leave them: the two
    road ends meet at a junction a little farther in, nearest first. Nothing built is thrown away."""
    if not all(stub["nodeId"] in ends for stub in pair):
        return []
    a, b = (ends[stub["nodeId"]] for stub in pair)
    hx, hz = sum(stub["inward"][0] for stub in pair), sum(stub["inward"][1] for stub in pair)
    norm = math.hypot(hx, hz) or 1.0
    hx, hz = hx / norm, hz / norm
    heading = math.degrees(math.atan2(hx, -hz))  # the comb builder's heading: 0 due south
    joins = []
    for distance in JOIN_DISTANCES:
        junction = ((a[0] + b[0]) / 2 + hx * distance, (a[1] + b[1]) / 2 + hz * distance)
        joins.append(([(a, junction), (b, junction)], junction, heading))
    return joins


def _junction_spots(pair: list[dict[str, Any]]) -> tuple[list[tuple[float, float]], float]:
    mx = sum(stub["point"][0] for stub in pair) / len(pair)
    mz = sum(stub["point"][1] for stub in pair) / len(pair)
    hx, hz = sum(stub["inward"][0] for stub in pair), sum(stub["inward"][1] for stub in pair)
    length = math.hypot(hx, hz) or 1.0
    hx, hz = hx / length, hz / length
    spots = [(mx + hx * distance - hz * offset, mz + hz * distance + hx * offset)
             for offset in (0.0, 16.0, -16.0, 32.0, -32.0) for distance in (60.0, 100.0, 40.0, 120.0, 80.0)]
    return spots, math.degrees(math.atan2(hx, -hz))  # the comb builder's heading: 0 due south, + toward +x


def _continuation(pair: list[dict[str, Any]], length: float) -> tuple[list[tuple[tuple[float, float], tuple[float, float]]], tuple[float, float], float]:
    """Roads that join a stub pair as a player drags them: each stub runs on into the map, bent a
    little away from the other (a straight run on would merge into the highway's end node), and a
    cross road joins the two ends; the town grows on from the first end. A road ending on a node
    next to a highway stub settles where the highway shapes it (live: bulging 13m aside), so no
    road ends there; and ends bent toward each other left them closer than two roads are wide."""
    mx = sum(stub["point"][0] for stub in pair) / len(pair)
    mz = sum(stub["point"][1] for stub in pair) / len(pair)
    hx, hz = sum(stub["inward"][0] for stub in pair), sum(stub["inward"][1] for stub in pair)
    norm = math.hypot(hx, hz) or 1.0
    hx, hz = hx / norm, hz / norm
    nx, nz = -hz, hx
    ends = []
    for stub in pair:
        side = 1.0 if (stub["point"][0] - mx) * nx + (stub["point"][1] - mz) * nz > 0 else -1.0
        dx, dz = hx + side * math.tan(math.radians(6.0)) * nx, hz + side * math.tan(math.radians(6.0)) * nz
        size = math.hypot(dx, dz) or 1.0
        ends.append((stub["point"][0] + dx / size * length, stub["point"][1] + dz / size * length))
    legs = [(pair[0]["point"], ends[0]), (pair[1]["point"], ends[1]), (ends[1], ends[0])]
    return legs, ends[0], math.degrees(math.atan2(hx, -hz))  # the comb builder's heading: 0 due south


def _segment_gap(first: tuple[tuple[float, float], tuple[float, float]],
                 second: tuple[tuple[float, float], tuple[float, float]]) -> float:
    """Shortest distance between two straight roads on the ground plane (0 when they cross)."""
    def side(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    def to_point(p, a, b):
        dx, dz = b[0] - a[0], b[1] - a[1]
        span = dx * dx + dz * dz
        t = 0.0 if not span else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dz) / span))
        return math.dist(p, (a[0] + t * dx, a[1] + t * dz))

    (a, b), (c, d) = first, second
    if side(a, b, c) * side(a, b, d) < 0 and side(c, d, a) * side(c, d, b) < 0:
        return 0.0
    return min(to_point(a, c, d), to_point(b, c, d), to_point(c, a, b), to_point(d, a, b))


def roads_clear_of_each_other(legs: list[tuple[tuple[float, float], tuple[float, float]]]) -> bool:
    """The roads of one attempt, checked against each other before any is built (a preview sees only
    what stands): roads that do not meet keep two road widths apart, roads that meet part at no
    sharper angle than MEET_ANGLE. Live, legs ending 17m apart collided once the first was built."""
    for i, first in enumerate(legs):
        for second in legs[i + 1:]:
            shared = {first[0], first[1]} & {second[0], second[1]}
            if not shared:
                if _segment_gap(first, second) < LEG_GAP:
                    return False
                continue
            node = shared.pop()
            far = [leg[1] if leg[0] == node else leg[0] for leg in (first, second)]
            angles = [math.atan2(p[1] - node[1], p[0] - node[0]) for p in far]
            between = abs((math.degrees(angles[0] - angles[1]) + 180.0) % 360.0 - 180.0)
            if between < MEET_ANGLE:
                return False
    return True


def _attempt(label: str, city: City | None, legs: list[tuple[tuple[float, float], tuple[float, float]]]
             ) -> tuple[list[int], dict[str, Any] | None, tuple[float, float] | None]:
    """Build the legs in order, each from where the leg it continues ended. One change in several
    roads: until the last, the roads are dead ends the game reports as not connected, so only the
    road that completes the link must pass its checks. (built, failure, junction reached)"""
    built: list[int] = []
    reached: dict[tuple[float, float], tuple[float, float]] = {}
    end = None
    for index, (start, target) in enumerate(legs):
        start = reached.get(start, start)
        target = reached.get(target, target)
        # a leg is a dead end until the last one links it: kept, the attempt removes them together
        outcome = roads.build(f"outside-{label}-{index}", "Basic Road", start, target, city, keep_failed=True)
        if not outcome.get("segmentId") or (index == len(legs) - 1 and not outcome["ok"]):
            built += [outcome["segmentId"]] if outcome.get("segmentId") else []
            return built, {"failed": outcome.get("stage"), "road": index,
                           "why": (outcome.get("tried") or [{}])[0] if outcome.get("stage") == "preview"
                           else str(outcome.get("error"))[:160]}, None
        built.append(outcome["segmentId"])
        end = (outcome["end"]["x"], outcome["end"]["z"])
        reached[legs[index][1]] = end
    return built, None, end


def _remove(label: str, segments: list[int]) -> list[int]:
    removed = []
    for segment in segments:
        outcome = protocol.run(f"outside-undo-{label}-{segment}", "bulldoze_entity",
                               {"entityType": "netSegment", "id": segment})
        if outcome["ok"]:
            removed.append(segment)
    return removed


def ensure_outside(city: City, label: str, state: dict[str, Any]) -> dict[str, Any] | None:
    """A new city's first link to the outside: an exit and an entrance stub joined at one junction.
    Every stub pair and several junction spots are tried; an attempt whose roads do not all settle
    is removed before the next one, and roads an earlier attempt left on one stub only are cleared
    first, so the town never grows from a one-way link. Done once some local road reaches the outside
    other than such a leftover (or there are no stubs on the map)."""
    record = state.get("outside") or {}
    if record.get("complete") and record.get("reason") != "noStubPair":  # once marked done on stubs missed
        return None
    if record.get("gatePolicy") != GATE_POLICY:
        record = {**record, "failedAttempts": 0, "gatePolicy": GATE_POLICY}
    local, highway_nodes, reaching = _local_roads()
    dangling = dangling_stub_roads(local, highway_nodes)
    if reaching - set(dangling):
        state["outside"] = {"complete": True}
        return None
    if record.get("failedAttempts", 0) >= OUTSIDE_FAILURES:
        return {"ok": False, "stage": "outsideConnection", "reason": "attemptsSpent",
                "failedAttempts": record["failedAttempts"]}
    pairs = stub_pairs(highway_stubs())
    if not pairs:  # looked for again each wake: nothing is removed or marked done on a map without one
        return {"ok": None, "stage": "outsideConnection", "reason": "noStubPair"}
    tried = []
    # roads already run on from both stubs of a pair are joined first; the rest are cleared before
    # anything new is laid. Then each stub runs on into the map and the two ends meet a little
    # farther in; then a junction right by the stubs. Every pair gets a turn at each kind of attempt,
    # near and far: a stub pair or a distance the ground refuses must not use up the attempts
    ends = dangling_ends(local, highway_nodes)
    options, removed = [], []
    for pair in pairs:
        for legs, junction, heading in _joins(pair, ends):
            options.append((pair, legs, junction, heading))
    joining = len(options)
    for length in CONTINUATIONS:
        for pair in pairs:
            legs, junction, heading = _continuation(pair, length)
            options.append((pair, legs, junction, heading))
    plans = [(pair, *_junction_spots(pair)) for pair in pairs]
    for index in range(max(len(spots) for _, spots, _ in plans)):
        for pair, spots, heading in plans:
            if index < len(spots):
                legs = [(stub["point"], spots[index]) for stub in pair]
                options.append((pair, legs, spots[index], heading))
    for index, (pair, legs, junction, heading) in enumerate(options):
        if len(tried) >= OUTSIDE_TRIES:
            break
        if index == joining:
            removed = _remove(label, dangling)  # no join held: the leftovers make way
        # every road of an attempt passes its preview before the first is built (a road from an end
        # not built yet is previewed from that free point): a refused last road must not leave the
        # earlier ones to be bulldozed at a loss
        if not roads_clear_of_each_other(legs) \
                or not all(any(roads.preview("Basic Road", a, b, label, lift)[0] for lift in roads.LIFTS) for a, b in legs) \
                or not roads.can_step(junction, heading, label):
            continue
        built, failure, end = _attempt(label, city, legs)
        if not failure and index < joining:
            built = [*dangling, *built]
        if failure:
            record["failedAttempts"] = record.get("failedAttempts", 0) + 1
            state["outside"] = record
            tried.append({"stubs": [s["nodeId"] for s in pair], "junction": [round(junction[0]), round(junction[1])],
                          "roads": len(legs), **failure, "removed": _remove(label, built)})
            continue
        state["outside"] = {"complete": True, "junction": list(end), "heading": round(heading, 1),
                            "stubs": [s["nodeId"] for s in pair], "roads": built}
        return {"ok": True, "junction": list(end), "stubs": [s["nodeId"] for s in pair],
                "roads": len(built), "attempts": len(tried) + 1, "removed": removed}
    return {"ok": False, "stage": "outsideConnection", "removed": removed, "tried": tried[-4:]}


def connected(city: City) -> bool:
    """Whether some local road reaches the outside: until one does, nothing can move in, so the
    clock is held and no utility is built to run up upkeep for an empty town."""
    record = city.load_state("growth", {}).get("outside") or {}
    if record.get("complete") and record.get("reason") != "noStubPair":
        return True
    local, highway_nodes, reaching = _local_roads()
    return bool(reaching - set(dangling_stub_roads(local, highway_nodes)))


REZONE_REACH = 400.0  # empty commercial land this near shops short of customers becomes homes
REZONE_PER_WAKE = 4
# commercial land standing empty this many cells while no one wants shops goes to what is wanted:
# live, 12 shops abandoned of no customers in one wake left 615 cells empty at commercial demand 0,
# workplace demand 59 and unemployment going 2% to 6%
COMMERCE_SURPLUS = 200
SURPLUS_PER_WAKE = 8
# a commercial block with at least this many empty cells beside its shops is surplus land too: the
# zoning keeps the occupied cells (live: 600 commercial cells stood empty, scattered over blocks
# with a shop or two, and only blocks wholly empty were rezoned, one or none a wake)
SURPLUS_BLOCK_CELLS = 8
SURPLUS_JOBS = 20  # workplace demand below which surplus shop land takes no offices
BLOCK_COLUMNS = 4  # a zone block is four cells wide; its masks give each row eight bits


def rezone_idle_shops(label: str, shop_ids: list[int], high_density: bool = False,
                      offices: bool = False) -> list[dict[str, Any]]:
    """Shops short of customers with commercial land still empty around them, met as a player
    meets it: the empty commercial blocks nearest them are zoned for homes, whose residents are
    the customers, where homes may be zoned (live: 21 shops without customers in shopping streets
    with few or no homes within 300m, while 285 commercial cells stood empty). Where homes may not
    go (an arterial or the buffer by industry) the block becomes offices once they are unlocked:
    fewer shops to share the customers (live: all 21 empty blocks by such shops were buffer land)."""
    growables = next((value for value in mt._request("GET", "/state/growables", query={"limit": 50000}).values()
                      if isinstance(value, list)), [])
    by_id = {int(b["id"]): b for b in growables}
    spots = [(by_id[i]["position"]["x"], by_id[i]["position"]["z"]) for i in map(int, shop_ids) if i in by_id]
    if not spots:
        return []
    near = sorted((min(math.dist((b["center"]["x"], b["center"]["z"]), spot) for spot in spots), b)
                  for b in _empty_commerce())
    return _rezone([item for item in near if item[0] <= REZONE_REACH], REZONE_PER_WAKE, high_density, offices)


def vacant_cells(block: dict[str, Any]) -> int:
    """The block's valid cells no building stands on, from its cell masks (bit row * 8 + column)."""
    try:
        valid = int(block.get("validMask") or "0", 16)
        taken = int(block.get("occupiedMask1") or "0", 16) | int(block.get("occupiedMask2") or "0", 16)
    except ValueError:
        return 0
    free = valid & ~taken
    return sum(1 for row in range(int(block.get("rows") or 0)) for column in range(BLOCK_COLUMNS)
               if free >> (row * 8 + column) & 1)


def _surplus_commerce() -> list[dict[str, Any]]:
    """Commercial blocks wholly empty or with SURPLUS_BLOCK_CELLS empty beside their shops."""
    return [b for b in mt._request("GET", "/state/zone-blocks", query={"limit": 5000}).get("blocks", [])
            if (b.get("zonedCells") or 0) > 0 and str(b.get("zone", "")).startswith("Commercial")
            and (b.get("occupancyClear") or vacant_cells(b) >= SURPLUS_BLOCK_CELLS)]


def _empty_commerce() -> list[dict[str, Any]]:
    return [b for b in mt._request("GET", "/state/zone-blocks", query={"limit": 5000}).get("blocks", [])
            if (b.get("zonedCells") or 0) > 0 and b.get("occupancyClear")
            and str(b.get("zone", "")).startswith("Commercial")]


def rezone_surplus_commerce(label: str, categories: dict[str, dict[str, Any]], high_density: bool = False,
                            offices: bool = False, workers_short: bool = False) -> list[dict[str, Any]]:
    """Empty commercial land while no one wants shops (demand 0 and COMMERCE_SURPLUS cells empty)
    is zoned for what is wanted: offices before homes while workplaces are wanted more (the jobs
    the closed shops took with them), homes otherwise, as the land allows."""
    commercial = categories.get("commercial") or {}
    if (commercial.get("demand") or 0) > 0 or (commercial.get("developableVacantCells") or 0) < COMMERCE_SURPLUS:
        return []
    work = (categories.get("workplace") or {}).get("demand") or 0
    homes = (categories.get("residential") or {}).get("demand") or 0
    town = _town()
    blocks = sorted(((math.dist((b["center"]["x"], b["center"]["z"]), town) if town else 0.0, b)
                     for b in _surplus_commerce()), key=lambda item: item[0])
    # while workers are short, homes only: offices would stand short of workers too, and land fit
    # only for them waits (live: all eight blocks of a wake at 2% unemployment became offices)
    # and only while jobs are wanted (live: workplace demand 10 after sixteen office blocks, and seven
    # of the next eight blocks became offices again)
    return _rezone(blocks, SURPLUS_PER_WAKE, high_density, offices and not workers_short and work >= SURPLUS_JOBS,
                   offices_first=offices and work > homes, shop_land=True)


def _held(block_id: int, zone: str, partial: bool = False) -> bool:
    """Whether the game keeps the block's zoning for something standing on it, though the block
    listing reads it clear (live: a building reaching over from the next block; the zoning changed
    no cells, and the half-failed rezoning stopped play)."""
    try:
        preview = mt._request("POST", "/commands/set-zone", body={"zone": zone, "blockId": block_id, "dryRun": True,
                                                                 "preserveOccupied": True, "vacantCellsOnly": partial})
    except (mt.BridgeRequestError, RuntimeError):
        return False
    if partial:  # a Bridge without vacantCellsOnly reports no vacant cells and skips the block
        return not preview.get("vacantCells")
    return bool(preview.get("skippedOccupiedBlocks")) and not preview.get("changedCells")


def _rezone(near: list[tuple[float, dict[str, Any]]], cap: int, high_density: bool, offices: bool,
            offices_first: bool = False, shop_land: bool = False) -> list[dict[str, Any]]:
    """Each block in turn zoned for homes (or offices first) where the land allows, until cap. With
    shop_land, land fit only for shops (an arterial, kept off homes for its noise) takes offices,
    which mind neither noise nor traffic (live: 11 of the 12 empty commercial blocks were such land,
    offices were allowed only in the buffer by industry, and nothing was rezoned while 674 cells
    stood empty and unemployment rose to 9%)."""
    context = mt._land_use_context()
    done: list[dict[str, Any]] = []
    for distance, block in near:
        if len([item for item in done if item["ok"]]) >= cap:
            break
        verdict = land_use.classify(block, context["growables"], polluters=context["polluters"],
                                    arterial_corners=context["corners"], through_roads=context["through"])
        office = offices and ("office" in verdict["allowed"] or (shop_land and "commercial" in verdict["allowed"]))
        if office and offices_first:
            zone = LOW_ZONE["office"]
        elif "residential" in verdict["allowed"]:
            zone = HIGH_ZONE["residential"] if high_density and block.get("roadPrefab") != "Gravel Road" \
                else LOW_ZONE["residential"]
        elif office:
            zone = LOW_ZONE["office"]
        else:
            continue
        partial = not block.get("occupancyClear")
        if _held(block["blockId"], zone, partial):
            continue
        outcome = protocol.run(f"rezone-{zone}-{block['blockId']}", "zone_block",
                               {"zone": zone, "blockId": block["blockId"], **({"vacantCellsOnly": True} if partial else {})})
        done.append({"blockId": block["blockId"], "zone": zone, "ok": outcome["ok"],
                     "regressionOk": outcome["regressionOk"], "distance": round(distance)})
    return done


def _town() -> tuple[float, float] | None:
    growables = next((value for value in mt._request("GET", "/state/growables", query={"limit": 50000}).values()
                      if isinstance(value, list)), [])
    return land_use.town_center(growables)


def _industry() -> list[tuple[float, float]]:
    growables = next((value for value in mt._request("GET", "/state/growables", query={"limit": 50000}).values()
                      if isinstance(value, list)), [])
    blocks = mt._request("GET", "/state/zone-blocks", query={"limit": 5000}).get("blocks", [])
    return land_use.industry(growables, land_use.zoned_land(blocks))


def _homes_may_grow(way: dict[str, Any], industry: list[tuple[float, float]]) -> bool:
    """Whether a district opened along this way starts where homes may be zoned."""
    if not industry:
        return True
    landing = (way["to"]["x"], way["to"]["z"])
    return all(math.dist(landing, point) > land_use.INDUSTRY_HOME_GAP for point in industry)


def open_access(city: City, label: str, way: dict[str, Any]) -> dict[str, Any]:
    """Build the road from the network along one way into new land; ok with where it ends. The
    straight line first; where the ground refuses a piece of it, an arc through the same two ends
    (roads.arc_points, bowing a little then more, either side) whose every piece previews before
    any is built. Never a road steered piece by piece off its way."""
    start = (way["from"]["x"], way["from"]["z"])
    if way["distance"] <= 8:
        return {"ok": True, "end": start}
    from mayor import bypass  # the same piece cutting: under 100m, bent joints
    straight = [(piece["end"]["x"], piece["end"]["z"]) for piece in bypass.pieces(way["from"], way["to"])]
    outcome = _lay_access(city, f"expand-{label}-a", start, straight, label)
    if outcome["ok"] or not outcome["retryable"]:
        return outcome
    curved = roads.lay_arc(f"expand-{label}", "Basic Road", start, (way["to"]["x"], way["to"]["z"]), city)
    if curved["ok"]:
        return {"ok": True, "end": (curved["end"]["x"], curved["end"]["z"]), "curve": curved["curve"]}
    return outcome


def _lay_access(city: City, name: str, start: tuple[float, float], points: list[tuple[float, float]],
                label: str) -> dict[str, Any]:
    """The pieces from start through points in turn; a refused one has those before it removed."""
    built: list[int] = []
    for index, end in enumerate(points):
        # a way into new land clears nothing: begun in the town it bulldozed homes piece by piece
        # and, refused further on, left them gone and a stub behind (live: 27 buildings)
        outcome = roads.build(f"{name}{index}", "Basic Road", start, end, city)
        if not outcome["ok"]:
            return {"ok": False, "stage": outcome.get("stage"), "removed": _remove(f"access-{label}", built),
                    "reason": outcome.get("error") or outcome.get("blocked") or outcome.get("tried"),
                    "retryable": roads.retryable(outcome)}
        built.append(outcome["segmentId"])
        start = (outcome["end"]["x"], outcome["end"]["z"])
    return {"ok": True, "end": start}


def open_through(city: City, label: str, way: dict[str, Any]) -> dict[str, Any]:
    """The road along one way into new land, through what stands in its way: every piece is
    previewed first, and only when that is at most THROUGH_MAX shops, offices or factories and
    THROUGH_LINES power line segments (no home or service building) are the buildings cleared,
    each after its own bulldoze preview, the line taken down and the pieces built. A piece refused
    after that has the pieces before it removed, so no stub is left (live: a way begun among homes
    once cleared 27).

    A ground road across a power line deletes the whole line silently, so the line is taken down
    first and the buildings it fed are joined to the grid again at once (repair_power: a line from
    the nearest powered ground; live: a 1.3km line to the first sewage outlet fenced 2.2km2 of land
    for twenty wakes while the town could not reach its next milestone)."""
    from mayor import bypass
    start = (way["from"]["x"], way["from"]["z"])
    pieces = []
    under: dict[int, dict[str, Any]] = {}
    lines: set[int] = set()
    for piece in bypass.pieces(way["from"], way["to"]):
        end = (piece["end"]["x"], piece["end"]["z"])
        ok, _, info = roads.preview("Basic Road", start, end, f"through-{label}")
        obstacles = info.get("obstacles") or []
        if not ok and not obstacles:
            return {"ok": False, "stage": "ground", "reason": info.get("errors")}
        for item in obstacles:
            if "segmentId" in item and "service" not in item:
                lines.add(int(item["segmentId"]))
                continue
            if item.get("service") not in roads.GROWABLE or item.get("service") == "Residential":
                return {"ok": False, "stage": "inTheWay", "reason": item}
            under[int(item["id"])] = item
        pieces.append((start, end))
        start = end
    if len(under) > THROUGH_MAX or len(lines) > THROUGH_LINES:
        return {"ok": False, "stage": "tooMany", "reason": {"buildings": sorted(under), "lines": sorted(lines)}}
    cleared = roads.clear_buildings(f"through-{label}", sorted(under)) if under else {"ok": True, "cleared": []}
    if not cleared["ok"]:
        return {"ok": False, "stage": "clearing", "reason": cleared}
    down = []
    for line in sorted(lines):
        outcome = protocol.run(f"through-line-{label}-{line}", "bulldoze_entity",
                               {"entityType": "netSegment", "id": line})
        if not outcome["ok"]:
            return {"ok": False, "stage": "lineDown", "reason": outcome.get("error"), "cleared": cleared["cleared"]}
        down.append(line)
    built: list[int] = []
    result: dict[str, Any] = {"ok": True, "cleared": cleared["cleared"], "linesDown": down}
    for index, (start, end) in enumerate(pieces):
        outcome = roads.build(f"expand-{label}-t{index}", "Basic Road", start, end, city)
        if not outcome["ok"]:
            result = {"ok": False, "stage": outcome.get("stage"), "removed": _remove(f"through-{label}", built),
                      "cleared": cleared["cleared"], "linesDown": down,
                      "reason": outcome.get("error") or outcome.get("tried")}
            break
        built.append(outcome["segmentId"])
        result["end"] = (outcome["end"]["x"], outcome["end"]["z"])
    if down:
        joined = utilities.repair_power(f"through-{label}")
        result["rejoined"] = {"missingElectricity": joined["summary"].get("missingElectricity"),
                              "lines": sum(1 for step in joined["steps"] if step.get("ok"))}
    return result


def grow(city: City, label: str, high_density: bool = False, hold_land: str | None = None,
         small: bool = False, offices: bool = False) -> dict[str, Any]:
    """Zone, densify, and open new land unless hold_land names why not this wake (only a road or
    two of it when small)."""
    state = city.load_state("growth", {"absorption": {}, "stale": {}})
    state.setdefault("skipped", [])
    state["wake"] = state.get("wake", 0) + 1
    outside = ensure_outside(city, label, state)
    capacity = mt.call_tool("inspect_growth_capacity", {"limit": 1})
    categories = capacity["growthPacing"]["categories"]
    unemployment = _unemployment()
    allowed = allowances(categories, state, unemployment)
    report: dict[str, Any] = {"allowed": allowed, "zoned": [], "densified": [], "exhausted": [], "short": [],
                              "demand": {use: categories[use].get("demand") for use in USES},
                              "vacancy": {use: categories[use].get("developableVacantCells") for use in USES},
                              "outside": outside}
    center = None
    for use in USES:
        zoned = zone_workplace(city, allowed[use], label, state["skipped"], offices,
                               office_fills_faster(capacity)) if use == "workplace" \
            else zone_blocks(city, use, allowed[use], label, state["skipped"], high_density)
        report["zoned"] += zoned
        new_blocks = sum(1 for item in zoned if item["ok"])
        state["absorption"][use] = {"vacancy": categories[use].get("developableVacantCells"),
                                    "allowed": allowed[use], "zonedBlocks": new_blocks}
        level = categories[use].get("demand") or 0
        if allowed[use] > 0 and new_blocks < allowed[use]:
            report["short"].append(use)
        if allowed[use] > 0 and new_blocks == 0:
            report["exhausted"].append(use)
            if level >= DENSIFY_DEMAND and use in HIGH_ZONE and high_density:
                if center is None:
                    center = mt.call_tool("propose_zoning_blocks", {"use": use, "limit": 1}).get("townCenter")
                report["densified"] += densify(use, DENSIFY_PER_WAKE, tuple(center) if center else None, label)
    surplus = rezone_surplus_commerce(label, categories, high_density, offices,
                                      labour_short(categories, unemployment))
    if surplus:
        report["rezonedSurplus"] = surplus
    # new land for the use that most needs it, once its own blocks fall short of what it may zone:
    # a use that still finds a block or two a wake never ran out, and no land opened while its
    # demand stood at 60 (live: homes zoned 2 of 4 blocks a wake for five wakes at 11,500 people)
    # while workers are short, homes need new land at the demand they are zoned at (live: the town
    # shrank by 2.5% over fifteen wakes, unemployment 3%, twelve home blocks allowed and none left to
    # zone at residential demand 39, so no district was opened)
    workers_short = labour_short(categories, unemployment)
    wanting = sorted((use for use in report["short"]
                      if use in EXPAND_USES and ((categories[use].get("demand") or 0) >= EXPAND_DEMAND
                                                 or use == "residential" and workers_short)),
                     key=lambda use: -(categories[use].get("demand") or 0))
    if outside and outside.get("ok") is False:
        report["expansion"] = {"use": "residential", **outside}  # no link to the outside: nothing can grow
    elif wanting and hold_land:
        report["expansion"] = {"use": wanting[0], "ok": None, "stage": hold_land}
    elif wanting:
        # an open district goes on only while land is wanted: roads laid ahead of the homes on the
        # roads already built only cost money and upkeep
        open_use = (state.get("expansion") or {}).get("use") if state.get("expansion") \
            and not state["expansion"].get("ended") else None
        use = open_use if open_use in wanting else wanting[0]
        report["expansion"] = expand(city, use, state, label, small)
        if land.stuck(report["expansion"]):
            bought = land.buy(city, label, state, _town())
            if bought.get("ok"):
                report["expansion"] = {"use": use, "ok": None, "stage": "boughtLand", "land": bought,
                                       "stuck": report["expansion"]}
            elif state.get("wake", 0) - state.get("landReportedAt", -LAND_REPORT_EVERY) < LAND_REPORT_EVERY:
                # reported already: the town fills the blocks it has while it waits for a milestone
                # to put a tile on offer, which the next wakes buy (live: three tiles owned, none on
                # offer, the land left fenced by the water outlet's power line and the highway)
                report["expansion"] = {"use": use, "ok": None, "stage": "landLimited", "land": bought}
            else:
                state["landReportedAt"] = state.get("wake", 0)
                report["expansion"] = {**report["expansion"], "land": bought}
    city.save_state("growth", state)
    return report

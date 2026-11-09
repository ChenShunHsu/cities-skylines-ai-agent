"""Cross a river to new land (mayor.bridge chooses where): every road of the crossing is previewed
and its cost summed before the first is built; the crossing is built in order under the protocol,
and a crossing that fails partway is bulldozed (at the game's refund) so no road is left hanging
over the water. The new district then grows from where the bridge lands.
"""

from __future__ import annotations

import math
from typing import Any

from loop import protocol, roads
from loop.city import City, mt
from mayor import bridge as plan
from mayor.expansion import MAP_EDGE

GROUND = "Basic Road"
DECK_PREFAB = "Basic Road Bridge"
RAMP_PREFAB = "Basic Road Elevated"
WET_DEPTH = 1.0  # water this deep is river, not a puddle on the bank
RESERVE = 15000.0  # display cash a crossing must leave the town
STARTS = 40  # road ends nearest the land considered as places to set out from
RESOLUTION = 64  # water samples across a tile (about 30m apart)


TILE = 1920.0


def _key(point: tuple[float, float], step: float) -> tuple[int, int]:
    """The sample nearest a point: indices counted from the map edge, where every tile's grid starts."""
    return round((point[0] - MAP_EDGE) / step), round((point[1] - MAP_EDGE) / step)


def _water() -> tuple[dict[tuple[int, int], bool], float]:
    """Deep water over the unlocked tiles, on a 30m grid."""
    tiles = [tile for tile in mt._request("GET", "/state/map-areas").get("areas", []) if tile.get("unlocked")]
    wet: dict[tuple[int, int], bool] = {}
    step = TILE / (RESOLUTION - 1)  # map analysis spaces its samples over the tile, ends included
    for tile in tiles:
        min_x, min_z = MAP_EDGE + tile["x"] * TILE, MAP_EDGE + tile["z"] * TILE
        cells = mt._request("GET", "/state/map-analysis", query={
            "minX": min_x, "maxX": min_x + TILE, "minZ": min_z, "maxZ": min_z + TILE, "resolution": RESOLUTION}).get("cells", [])
        for cell in cells:
            water = cell.get("water") or {}
            # map analysis reports the terrain under water as "level" and the surface as "depth"
            deep = bool(water.get("present")) and float(water.get("depth", 0)) - float(water.get("level", 0)) >= WET_DEPTH
            wet[_key((cell["position"]["x"], cell["position"]["z"]), step)] = deep
    return wet, step


def _road_ends() -> list[tuple[float, float]]:
    segments = mt._request("GET", "/state/networks", query={"limit": 20000}).get("segments", [])
    points = {}
    for segment in segments:
        if segment.get("service") != "Road" or "Highway" in str(segment.get("prefab")) \
                or "Original" in str(segment.get("flags")):
            continue
        for end in ("start", "end"):
            points[int(segment[f"{end}NodeId"])] = (segment[end]["x"], segment[end]["z"])
    return list(points.values())


def _preview(prefab: str, piece: dict[str, Any]) -> dict[str, Any]:
    """A ground piece is checked as the loop builds it (roads.preview); one over homes, shops or
    workplaces is valid once they are cleared, as for any access road. A raised piece is the
    game's own preview."""
    if "startHeightOffset" not in piece:
        start, end = (piece["start"]["x"], piece["start"]["z"]), (piece["end"]["x"], piece["end"]["z"])
        ok, _, info = roads.preview(prefab, start, end, "bridge-plan")
        obstacles = info.get("obstacles", [])
        # only growables in the way: cleared when the piece is built (build_through), which
        # previews the road again; a failure then undoes the crossing
        clearable = bool(obstacles) and all(item.get("service") in roads.GROWABLE for item in obstacles)
        return {"valid": ok or clearable, "toolErrors": info.get("errors"), "cost": 0.0}
    body = {"roadPrefab": prefab, "start": piece["start"], "end": piece["end"],
            "startHeightOffset": piece["startHeightOffset"], "endHeightOffset": piece["endHeightOffset"]}
    try:
        result = mt.call_tool("preview_network", body)
    except mt.BridgeRequestError as error:
        return {"valid": False, "error": str(error)[:160]}
    return {"valid": bool(result.get("valid")), "toolErrors": result.get("toolErrors"),
            "cost": (result.get("constructionCost") or 0) / 100.0}


def _prefab(piece: dict[str, Any]) -> str:
    return DECK_PREFAB if piece["overWater"] else RAMP_PREFAB


def propose(target: tuple[float, float]) -> dict[str, Any]:
    """The cheapest crossing toward the target whose every road previews valid, with its cost."""
    wet_cells, step = _water()

    def wet(point: tuple[float, float]) -> bool:
        return wet_cells.get(_key(point, step), False)

    ends = sorted(_road_ends(), key=lambda point: math.dist(point, target))[:STARTS]
    tried = []
    for crossing in plan.crossings(ends, target, wet):
        for setback in plan.SETBACKS:
            laid = plan.pieces(crossing, setback=setback)
            previews = [_preview(GROUND, piece) for piece in laid["ground"]] + \
                       [_preview(_prefab(piece), piece) for piece in laid["bridge"]]
            # ground roads cost about a twelfth of a bridge piece of the same length (live previews)
            cost = round(sum(item.get("cost") or 0 for item in previews) + 5.3 * max(0.0, crossing["approach"] - setback))
            summary = {"start": [round(v) for v in crossing["start"]], "approach": round(crossing["approach"]),
                       "span": round(crossing["span"]), "setback": setback, "cost": cost}
            if all(item["valid"] for item in previews):
                return {"ok": True, **summary, "pieces": laid, "heading": crossing["heading"]}
            tried.append({**summary, "refused": [i for i, item in enumerate(previews) if not item["valid"]][:4],
                          "errors": sorted({str(item.get("toolErrors") or item.get("error")) for item in previews
                                            if not item["valid"]})[:3]})
    return {"ok": False, "stage": "noCrossing", "tried": tried[:6]}


def cross(city: City | None, target: tuple[float, float], label: str, cash: float) -> dict[str, Any]:
    """Build the cheapest valid crossing toward the target, if the town can afford it."""
    proposal = propose(target)
    if not proposal["ok"]:
        return proposal
    loan = None
    if cash - proposal["cost"] < RESERVE:
        # a town that earns more than it spends borrows for a crossing it has planned and priced, as
        # a player does; one that runs a deficit waits
        loan = borrow(label, proposal["cost"] + RESERVE - cash)
        if not (loan or {}).get("ok"):
            return {"ok": None, "stage": "waitingForCash", "cost": proposal["cost"], "cash": round(cash),
                    "loan": loan}
    built: list[int] = []
    laid = proposal["pieces"]
    start = tuple(proposal["start"])
    for index, piece in enumerate(laid["ground"]):
        end = (piece["end"]["x"], piece["end"]["z"])
        # an approach ends at the bridge until it is laid: kept, a failed crossing removes it all
        outcome = roads.build_through(f"bridge-{label}-g{index}", GROUND, start, end, city, keep_failed=True)
        if not outcome.get("segmentId"):
            return {**_undo(label, built), "ok": False, "stage": f"ground{index}", "why": outcome.get("stage")}
        built.append(outcome["segmentId"])
        start = (outcome["end"]["x"], outcome["end"]["z"])
    for index, piece in enumerate(laid["bridge"]):
        piece = {**piece, "start": {"x": start[0], "z": start[1]}}
        outcome = roads.build_raised(f"bridge-{label}-b{index}", _prefab(piece), piece)
        if not outcome.get("segmentId"):
            return {**_undo(label, built), "ok": False, "stage": f"bridge{index}",
                    "why": outcome.get("errors") or outcome.get("error")}
        built.append(outcome["segmentId"])
        start = (outcome["end"]["x"], outcome["end"]["z"])
    heading = proposal["heading"]
    return {"ok": True, "landing": list(start), "roads": built, "cost": proposal["cost"], "loan": loan,
            "span": proposal["span"], "approach": proposal["approach"],
            # the comb builder's heading (0 due south, + toward +x) on into the new land
            "headingDegrees": math.degrees(math.atan2(heading[0], -heading[1]))}


def borrow(label: str, need: float) -> dict[str, Any] | None:
    """The smallest free loan covering `need`, while the city's periods end in profit."""
    if (mt.call_tool("inspect_economy", {})["cash"].get("periodNet") or 0) <= 0:
        return {"ok": False, "reason": "deficit"}
    offers = sorted(((slot["slot"], option) for slot in mt._request("GET", "/state/loan-options").get("slots", [])
                     if slot.get("available") and not slot.get("active")
                     for option in (slot.get("options") or [])[:1]), key=lambda item: item[1]["amount"])
    for slot, option in offers:
        if option["amount"] >= need:
            outcome = protocol.run(f"loan-{label}", "take_loan", {
                "slot": slot, "reason": "fund-planned-construction",
                "expectedOutcome": f"a planned river crossing is built and the loan repaid from tax income"})
            return {"ok": outcome["ok"], "slot": slot, "amount": option["amount"], "interest": option["interest"],
                    "error": outcome.get("error")}
    return {"ok": False, "reason": "noLoanLargeEnough", "need": round(need)}


def _undo(label: str, built: list[int]) -> dict[str, Any]:
    removed = []
    for segment in reversed(built):
        outcome = protocol.run(f"bridge-undo-{label}-{segment}", "bulldoze_entity",
                               {"entityType": "netSegment", "id": segment})
        if outcome["ok"]:
            removed.append(segment)
    return {"removed": removed}

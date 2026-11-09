"""Buy a map tile when the city has no land it can reach for a use it needs.

A city starts on one tile and may own up to maxAreaCount as milestones are reached. Once the
open land on its tiles is gone, or every way into it runs over buildings, growth stops (live: at
1,541 people the one industrial area left was ringed by buildings and the city still owned only
its first tile of nine, with four on offer for 2,500-5,300). A player buys the next tile there.

The tile bought is one the game offers (canUnlock), affordable with LAND_RESERVE left over,
with at least MIN_FLAT_SHARE of flat dry ground, nearest the town first. One tile at most every
BUY_EVERY wakes: the next wakes open the bought land, and a use still stuck after that goes to
the planner.
"""

from __future__ import annotations

import math
from typing import Any, Iterable

from mayor import expansion as mayor_expansion

from loop import protocol
from loop.city import City, mt

LAND_RESERVE = 20000.0  # display cash a purchase must leave
BUY_EVERY = 4
MIN_FLAT_SHARE = 0.3
TILE_RESOLUTION = 24
# expansion outcomes that mean the use has no land it can reach on the tiles owned
STUCK_STAGES = ("access", "needsCrossing", "waitingForPlanner")


def stuck(expansion: dict[str, Any] | None) -> bool:
    return bool(expansion) and expansion.get("ok") is not True and expansion.get("stage") in STUCK_STAGES


def flat_share(cells: Iterable[dict[str, Any]]) -> float:
    """Share of a tile's samples flat enough for streets and dry, whoever owns it."""
    cells = list(cells)
    if not cells:
        return 0.0
    good = sum(1 for cell in cells if float(cell.get("slope") or 0) <= mayor_expansion.MAX_SLOPE
               and not (cell.get("water") or {}).get("present"))
    return good / len(cells)


def tile_center(tile: dict[str, Any], size: float) -> tuple[float, float]:
    return (mayor_expansion.MAP_EDGE + (tile["x"] + 0.5) * size, mayor_expansion.MAP_EDGE + (tile["z"] + 0.5) * size)


def choose_tile(tiles: Iterable[dict[str, Any]], shares: dict[int, float], town: tuple[float, float] | None,
                cash_display: float, size: float) -> dict[str, Any] | None:
    """The offered tile to buy: affordable with LAND_RESERVE left, enough flat dry ground, nearest
    the town (the largest share of flat ground when the town is unknown)."""
    offered = [tile for tile in tiles if tile.get("canUnlock") and not tile.get("unlocked")
               and cash_display - float(tile.get("price") or 0) / mt.MONEY_INTERNAL_SCALE >= LAND_RESERVE
               and shares.get(int(tile["index"]), 0.0) >= MIN_FLAT_SHARE]
    if not offered:
        return None
    if town is None:
        return max(offered, key=lambda tile: shares[int(tile["index"])])
    return min(offered, key=lambda tile: math.dist(town, tile_center(tile, size)))


def buy(city: City, label: str, state: dict[str, Any], town: tuple[float, float] | None) -> dict[str, Any]:
    """Buy one tile for the land the city cannot reach, unless one was bought within BUY_EVERY
    wakes, none is on offer at a price that leaves the reserve, or none has ground to build on."""
    wake = int(state.get("wake", 0))
    last = state.get("landBoughtAt")
    if last is not None and wake - int(last) < BUY_EVERY:
        return {"ok": None, "stage": "recentlyBought", "wake": last}
    areas = mt._request("GET", "/state/map-areas")
    if int(areas.get("areaCount") or 0) >= int(areas.get("maxAreaCount") or 0):
        return {"ok": None, "stage": "tileLimit", "areaCount": areas.get("areaCount")}
    size = mt.MAP_TILE_SIZE
    cash_display = float(mt.city_health()["cash"]["amount"]) / mt.MONEY_INTERNAL_SCALE
    shares = {}
    for tile in areas.get("areas", []):
        if not tile.get("canUnlock") or tile.get("unlocked"):
            continue
        min_x = mayor_expansion.MAP_EDGE + tile["x"] * size
        min_z = mayor_expansion.MAP_EDGE + tile["z"] * size
        cells = mt._request("GET", "/state/map-analysis", query={
            "minX": min_x, "maxX": min_x + size, "minZ": min_z, "maxZ": min_z + size,
            "resolution": TILE_RESOLUTION}).get("cells", [])
        shares[int(tile["index"])] = round(flat_share(cells), 3)
    tile = choose_tile(areas.get("areas", []), shares, town, cash_display, size)
    if tile is None:
        return {"ok": None, "stage": "noTileToBuy", "flatShares": shares, "cash": round(cash_display)}
    outcome = protocol.run(f"land-{label}-{tile['index']}", "purchase_map_area",
                           {"tileIndex": int(tile["index"]), "reserveDisplay": LAND_RESERVE})
    if outcome["ok"]:
        state["landBoughtAt"] = wake
        city.log("land", {"tile": tile["index"], "price": float(tile["price"]) / mt.MONEY_INTERNAL_SCALE,
                          "flatShare": shares[int(tile["index"])]})
    return {"ok": outcome["ok"], "regressionOk": outcome.get("regressionOk"), "tile": int(tile["index"]),
            "price": float(tile["price"]) / mt.MONEY_INTERNAL_SCALE, "flatShare": shares[int(tile["index"])],
            "error": outcome.get("error")}

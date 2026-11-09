"""A specialised industry district where industry keeps lacking goods, as a player sets one up.

Generic industry takes its inputs (lumber, food, petrol, coal) from the city's specialised
processors, or imports them by road. With no specialised district every input crosses the outside
connection, and industry reports NoResources while no truck is on its way (live: 66 buildings at
45,000 people, none with an inbound delivery). A district painted over industrial blocks on
renewable resource ground (forest: Forestry, fertile soil: Farming) makes their lots grow
extractors and processors that supply the rest.

Site: the industrial zone blocks are sampled on the map-analysis grid; the circle of RADIUS around
a block holding the most blocks on ground of one resource at MIN_INTENSITY or more wins, with at
least MIN_BLOCKS of them and no cell of another district (create_district refuses an overlap; a
dry run checks it first). Ore and oil run out, so they are never chosen. One district at a time:
the next waits SETTLE_WAKES (its lots take that long to regrow), at most MAX_DISTRICTS in all.
"""

from __future__ import annotations

import math
from typing import Any

from loop import protocol
from loop.city import City, mt

RESOURCES = {"forest": "Forestry", "fertility": "Farming"}  # renewable; ore and oil run dry
RADIUS = 250.0
MIN_INTENSITY = 64
MIN_BLOCKS = 8
MAX_DISTRICTS = 3
SETTLE_WAKES = 8
GRID = 48  # map-analysis cells a side over the industrial area
CANDIDATES = 5  # sites dry-run before the search gives up


def candidates(blocks: list[dict[str, Any]], cells: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Circles around industrial blocks, the most blocks on one renewable resource first."""
    def ground(block: dict[str, Any]) -> dict[str, int]:
        x, z = block["center"]["x"], block["center"]["z"]
        cell = min(cells, key=lambda c: (c["position"]["x"] - x) ** 2 + (c["position"]["z"] - z) ** 2)
        return cell.get("resources") or {}

    rich = [(block, ground(block)) for block in blocks]
    found = []
    for block, _ in rich:
        here = (block["center"]["x"], block["center"]["z"])
        for resource, specialization in RESOURCES.items():
            on = [other for other, soil in rich if (soil.get(resource) or 0) >= MIN_INTENSITY
                  and math.dist(here, (other["center"]["x"], other["center"]["z"])) <= RADIUS]
            if len(on) >= MIN_BLOCKS:
                found.append({"center": {"x": round(here[0], 1), "z": round(here[1], 1)}, "resource": resource,
                              "specialization": specialization, "blocks": len(on)})
    found.sort(key=lambda item: -item["blocks"])
    # one circle per neighbourhood: a site within RADIUS of a better one covers the same blocks
    kept: list[dict[str, Any]] = []
    for item in found:
        point = (item["center"]["x"], item["center"]["z"])
        if all(math.dist(point, (other["center"]["x"], other["center"]["z"])) > RADIUS for other in kept):
            kept.append(item)
    return kept


def _industrial_blocks() -> list[dict[str, Any]]:
    return [block for block in mt._request("GET", "/state/zone-blocks", query={"limit": 5000}).get("blocks", [])
            if block.get("zone") == "Industrial" and (block.get("zonedCells") or 0) > 0]


def _cells(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    xs = [block["center"]["x"] for block in blocks]
    zs = [block["center"]["z"] for block in blocks]
    return mt._request("GET", "/state/map-analysis", query={
        "minX": min(xs), "maxX": max(xs), "minZ": min(zs), "maxZ": max(zs), "resolution": GRID}).get("cells", [])


def _free(site: dict[str, Any]) -> bool:
    try:
        check = mt._request("POST", "/commands/create-district",
                            body={"center": site["center"], "radius": RADIUS, "dryRun": True})
    except mt.BridgeRequestError:  # it overlaps a district, or covers no cell
        return False
    return bool(check.get("ok")) and not check.get("occupiedCells")


def supply(city: City, label: str, wake: int, count: int) -> dict[str, Any] | None:
    """The answer to NoResources still rising: a new district, a wait while the last one regrows,
    or None when no district can be added (the planner hears of it)."""
    state = {"districts": [], **city.load_state("industry-districts", {})}  # a fresh list each time
    districts = state["districts"]
    if districts and wake - districts[-1]["wake"] < SETTLE_WAKES:
        return {"ok": None, "stage": "settling", "districtId": districts[-1]["districtId"],
                "since": wake - districts[-1]["wake"]}
    if len(districts) >= MAX_DISTRICTS:
        return None
    blocks = _industrial_blocks()
    if not blocks:
        return None
    sites = [site for site in candidates(blocks, _cells(blocks))][:CANDIDATES]
    site = next((item for item in sites if _free(item)), None)
    if site is None:
        return None
    made = protocol.run(f"district-{site['specialization']}-{label}", "create_district",
                        {"center": site["center"], "radius": RADIUS})
    district = (made.get("result") or {}).get("districtId")
    if not made["ok"] or not district or made.get("regressionOk") is False:
        return {"ok": False, "stage": "createDistrict", "error": made.get("error"), "site": site,
                "regressionOk": made.get("regressionOk")}
    special = protocol.run(f"specialize-{district}-{label}", "set_district_specialization",
                           {"districtId": int(district), "specialization": site["specialization"],
                            "requireLocalResource": True, "minResourceIntensity": MIN_INTENSITY})
    districts.append({"wake": wake, "districtId": int(district), **site, "noResources": count,
                      "specialized": bool(special["ok"])})
    city.save_state("industry-districts", {**state, "districts": districts})
    return {"ok": special["ok"], "regressionOk": special.get("regressionOk"), "stage": None if special["ok"]
            else "specialization", "error": special.get("error"), "districtId": int(district), **site}

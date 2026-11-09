"""Keep every building on water, sewage and power.

Water: a connected intake and outlet pair is found among the city's facilities (no fixed ids), and
each wake builds the service pipes that propose_water_service_connection plans for the buildings
missing water or sewage, re-reading coverage after every pipe (one pipe often serves a cluster).
Power: a plant when the grid has no capacity or runs near full (add_power), then one
native-proposed line at a time to the next unpowered building, re-reading coverage.
Water capacity running short is reported for the planner.
"""

from __future__ import annotations

import math
from typing import Any

from mayor import problems as mayor_problems

from loop import protocol
from loop.city import City, mt

MAX_PIPES = 6
MAX_LINES = 3
POWER_PRESSURE = 0.85  # the same share play.py treats as utility pressure
# dark buildings within the grid's reach that mean it is short of power; fewer, below the pressure,
# are buildings no line joins (live: an Advanced Wind Turbine went up for two dark buildings while
# the grid ran at 48%; a short grid darkens every one of the SHORTFALL_SAMPLE checked)
STARVED_MIN = 3
TREE_BUILDINGS = 12  # dark buildings a wake's line tree is planned for; the next wake takes the rest
SHORTFALL_SAMPLE = 6  # buildings without a service checked for which of the two causes holds
# a player turns down the budget of a utility running far below capacity while the city loses money:
# kept so the use takes at most a third of what remains (live: a town doubled in two wakes and ran
# a power grid trimmed to half its use past full), never under the game's 50%
BUDGET_HEADROOM = 0.35
BUDGET_FLOOR = 50
BUDGET_STEP = 10
# utilities whose budget a deficit trims while they sit idle (trim_budgets). Power was left full, as
# wind output swings with the weather; yet a grid run at 21% of its capacity for 25,000 a week, the
# second upkeep of a city going broke, keeps a wide margin at the trim (use under BUDGET_HEADROOM
# of the trimmed capacity), and a grid that runs short turns its budget back up before anything is
# built (restore_budgets, add_power)
UTILITY_BUDGETS = {"water": "Water", "electricity": "Electricity"}
# with homes still wanted, a deficit is met by taxes as a player meets it: a point at a time, homes and
# shops together, no higher than players keep them while a town grows
TAX_DEMAND = 60
TAX_CEILING = 12
TAXED = ("Residential", "Commercial")
# plant site searches, strict first: (radius around the town, least share of a variable plant's output)
SITE_SEARCHES = ((640.0, 0.75), (1280.0, 0.75), (1280.0, 0.5))
# a lull is built for ahead of need, so its search may reach further (live: every spot within
# 1,280m of the town and its industry held turbines already)
FIRM_SEARCHES = SITE_SEARCHES + ((1920.0, 0.5),)  # the tool reaches 1,920m at most
KW_PER_PRODUCTION = 16.0  # electricityProduction 500 is a wind turbine's 8 MW (8,000 kW of capacity)
VARIABLE_OUTPUT = ("WindTurbineAI", "SolarPowerPlantAI")  # output follows the site's wind or sun
WIND_YIELD_DEFAULT = 0.6  # before any turbine stands to measure it by
OUTAGE_MIN = 20  # buildings flagged without electricity at one reading: the grid ran short unseen
LULL_SHARE = 0.5  # of what they give now, what turbines give in a lull (live: 40 read 57%, then 115%)
POWER_TARGET = 0.7  # new plants bring the grid's use down to this share of its capacity
MAX_PLANTS = 3  # plants built a wake
PLANT_SPEND_SHARE = 0.5  # of the cash, at most, on a wake's plants
INDUSTRY_ANCHORS = 2  # industrial districts a plant is also sought beside, largest first
INDUSTRY_LINK = 250.0  # factories this near each other make one district
PIPE_PIECE = 150.0  # water pipes are built in pieces under 160m
MIN_PIPE = 16.0  # and none shorter than this: the game refuses a pipe a few metres long
PORT_TOLERANCE = 2.0  # a pipe end this near a facility's port is joined to it
ZIGZAG = 10.0  # sideways shift of pipe joints: about 8 degrees of bend over a 150m piece
WATER_SEARCH_RESOLUTIONS = (48, 24)  # a coarse search misses narrow moving water
NEAR_TOWN_GAP = 100000.0  # how far from the river's ends a near-town pair may sit
JUNCTION_SPREAD = 60.0  # least angle between the three pipes at the water junction
JUNCTION_TRIES = 6  # junction spots previewed per design
GROUNDWATER_OFFSET = 200.0  # least distance from the town to a groundwater pump
WATER_ATTEMPTS = 3  # designs tried before a first water system goes to the planner


def water_pair() -> tuple[int | None, int | None]:
    """A working intake and outlet that both report connected to the pipe network."""
    facilities = mt._request("GET", "/state/facilities", query={"service": "Water", "limit": 1000}).get("facilities", [])
    prefabs = {item["name"]: item for item in mt._request("GET", "/prefabs/buildings", query={"service": "Water"})
               .get("buildings", [])}

    def role(facility: dict[str, Any]) -> str | None:
        fields = ((prefabs.get(facility.get("prefab")) or {}).get("aiProfile") or {}).get("fields") or {}
        if fields.get("waterIntake"):  # a groundwater pump serves as the intake
            return "intake"
        if fields.get("sewageOutlet"):
            return "outlet"
        return None

    # idle counts: a new system pumps nothing (and reads inactive) until the first homes are piped
    connected = [f for f in facilities if (f.get("active") or f.get("productionRate"))
                 and "WaterNotConnected" not in str(f.get("problems"))]
    intake = next((int(f["id"]) for f in connected if role(f) == "intake"), None)
    outlet = next((int(f["id"]) for f in connected if role(f) == "outlet"), None)
    return intake, outlet


def dry_water_facilities() -> list[dict[str, Any]]:
    """Standing intakes the river left, judged as the game judges them: a powered intake takes a
    water source on its first pump and lets it go only when no water lies within its full reach
    (WaterFacilityAI.HandleWaterSource), so one powered and without a source is dry. An outlet is
    never dry to the game (it makes its own source wherever it stands), and the placement radius,
    half that reach, judged working facilities dry."""
    dry = []
    for role, facility in _water_facilities().items():
        # a groundwater pump holds no water source: it is never dry
        if role == "intake" and not facility.get("groundwater") and not facility["waterSource"] and facility["active"] \
                and "Electricity" not in facility["problems"]:
            dry.append({"role": role, "buildingId": facility["buildingId"]})
    return dry


def relocate_dry(city: City, label: str) -> list[dict[str, Any]]:
    """A facility dry on two wakes running is bulldozed and the bootstrap opened again: the next
    wake designs a partner for the facility still standing, on water that reaches its port. One
    wake is not enough: an intake placed this wake has not pumped yet, so holds no source either."""
    found = dry_water_facilities()
    seen = set(city.load_state("water-dry", {}).get("seen", []))
    city.save_state("water-dry", {"seen": [item["buildingId"] for item in found]})
    dry = [item for item in found if item["buildingId"] in seen]
    for item in dry:
        outcome = protocol.run(f"water-dry-{item['role']}-{label}", "bulldoze_entity",
                               {"entityType": "building", "id": item["buildingId"]})
        item["removed"] = bool(outcome["ok"])
    if dry:
        state = city.load_state("water-bootstrap", {})
        city.save_state("water-bootstrap", {"avoid": state.get("avoid", []), "attempts": 0,
                                            "relocate": [item["role"] for item in dry if item["removed"]]})
    return dry


def repair_water(label: str, city: City | None = None) -> dict[str, Any]:
    summary = mt.call_tool("inspect_water_service_coverage", {"limit": 64})["summary"]
    if not (summary["missingWater"] or summary["missingSewage"]):
        return {"summary": summary, "steps": []}
    if city is not None:
        dry = relocate_dry(city, label)
        if dry:
            return {"summary": summary, "steps": [], "relocating": dry}
    intake, outlet = water_pair()
    if intake is None or outlet is None:
        return {"summary": summary, "steps": [], "error": "noConnectedPair"}
    diagnosis = shortfall("water")
    supply = None
    if len(diagnosis.get("coveredButShort") or []) >= 2:
        # homes beside the pipes with no water: the system is short. Its budget sets its capacity,
        # so a trimmed one goes back up; at full budget it needs another pair, a planner's choice
        supply = set_budget(label, "Water", 100, "post-change-correction", "water capacity back to full") \
            if budget_of("Water") < 100 else {"ok": False, "stage": "waterSupplyShort"}
    reach = {int(item["id"]) for item in diagnosis.get("notCovered") or []}
    steps = []
    caught_up = False
    piped: set[int] = set()
    for step in range(MAX_PIPES if reach else 0):
        coverage = mt.call_tool("inspect_water_service_coverage", {"limit": 64})
        targets = [building["id"] for building in coverage["buildings"]
                   if int(building["id"]) in reach or len(reach) >= SHORTFALL_SAMPLE]
        if not targets:
            # the listing stops at 64 and also lists served buildings with a trace of pollution
            # (live: 4,584 read 1 of 255 and hid the 3 dry ones, so no pipe was laid and play
            # stopped): the dry ones not piped this wake come from the diagnosis
            targets = [int(item["id"]) for item in shortfall("water").get("notCovered") or []
                       if int(item["id"]) not in piped]
        if not targets:
            # every dry building piped: homes still growing in a new district may read dry by the
            # wake's end, and the next wake pipes them (live: 8 grew while 4 pipes were laid)
            caught_up = bool(steps)
            break
        plan: dict[str, Any] = {}
        length = 160
        for group, length in [(targets[:64], 160), (targets[:64], 96)] + [([t], 96) for t in targets[:8]]:
            plan = mt.call_tool("propose_water_service_connection", {
                "intakeBuildingId": intake, "outletBuildingId": outlet, "targetBuildingIds": group,
                "maxWaterPipeSegmentLength": length, "maxPreviewAttempts": 512})
            if plan.get("safe"):
                break
        if not plan.get("safe"):
            steps.append({"step": step, "planSafe": False, "risks": plan.get("risks")})
            break
        if plan.get("actionRequired") is False:
            # the buildings read dry have water by now: nothing to lay (live: the planner answered
            # safe with no planId and the wake failed three times on it)
            steps.append({"step": step, "alreadyServed": len(targets)})
            caught_up = True
            break
        piped.update(int(target["id"]) for target in plan["targetBuildings"])
        outcome = protocol.run(f"svcpipe-{label}-{step}", "build_water_service_pipe", {
            "planId": plan["planId"], "segments": plan["segments"], "intakeBuildingId": intake,
            "outletBuildingId": outlet, "targetBuildingIds": [t["id"] for t in plan["targetBuildings"]],
            "sourceNodeId": plan["sourceNode"]["id"], "pipePrefab": plan.get("pipePrefab", "Water Pipe"),
            "segmentIndex": 0, "maxWaterPipeSegmentLength": length}, 40)
        steps.append({"step": step, "ok": outcome["ok"], "regressionOk": outcome["regressionOk"]})
        if not outcome["ok"] or not outcome["regressionOk"]:
            break
    return {"summary": mt.call_tool("inspect_water_service_coverage", {"limit": 64})["summary"], "steps": steps,
            "diagnosis": diagnosis.get("verdict"), **({"supply": supply} if supply else {}),
            **({"caughtUp": True} if caught_up else {})}


def _toward(a: tuple[float, float], b: tuple[float, float], distance: float) -> tuple[float, float]:
    length = math.dist(a, b) or 1.0
    return a[0] + (b[0] - a[0]) * distance / length, a[1] + (b[1] - a[1]) * distance / length


def _aside(a: tuple[float, float], b: tuple[float, float], point: tuple[float, float],
           offset: float) -> tuple[float, float]:
    """point moved `offset` metres to the left of the direction a to b."""
    length = math.dist(a, b) or 1.0
    return point[0] - (b[1] - a[1]) / length * offset, point[1] + (b[0] - a[0]) / length * offset


def _legs(role: str, a: tuple[float, float], b: tuple[float, float]) -> list[dict[str, Any]]:
    """a to b as pipe segments no longer than PIPE_PIECE, the joints alternately ZIGZAG metres either
    side of the straight line: a piece built straight on from another makes the joint a middle node
    the game may move, and the Bridge refuses it."""
    count = max(1, math.ceil(math.dist(a, b) / PIPE_PIECE))
    points = [(a[0] + (b[0] - a[0]) * i / count, a[1] + (b[1] - a[1]) * i / count) for i in range(count + 1)]
    points = [p if i in (0, count) else _aside(a, b, p, ZIGZAG if i % 2 else -ZIGZAG) for i, p in enumerate(points)]
    return [{"role": role, "start": {"x": round(p[0], 2), "z": round(p[1], 2)},
             "end": {"x": round(q[0], 2), "z": round(q[1], 2)}} for p, q in zip(points, points[1:])]


def _bearing(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]))


def junctions(intake: tuple[float, float], outlet: tuple[float, float], town: tuple[float, float],
              avoid: list[tuple[float, float]] = ()) -> list[tuple[float, float]]:
    """Spots for the three-way junction around the middle of the pair, or of either facility and
    the town (a groundwater pump stands near the town, far from its outlet), shortest total pipe
    first. Pipes meeting at a node need room between them (one within about 40 degrees of another
    collides), so the ways to the intake, the outlet and the town must part by JUNCTION_SPREAD."""
    middles = [((a[0] + b[0]) / 2, (a[1] + b[1]) / 2) for a, b in ((intake, outlet), (intake, town), (outlet, town))]
    found = []
    for middle, radius in [(middle, radius) for middle in middles for radius in (0.0, 100.0, 150.0, 250.0, 350.0)]:
        for degrees in range(0, 360, 15) if radius else (0,):
            j = (middle[0] + radius * math.cos(math.radians(degrees)), middle[1] + radius * math.sin(math.radians(degrees)))
            if min(math.dist(j, intake), math.dist(j, outlet)) < 80.0 or math.dist(j, town) < 80.0:
                continue  # each branch needs room for its piece and a bend
            if any(math.dist(j, spot) < 50.0 for spot in avoid):
                continue
            ways = [_bearing(j, intake), _bearing(j, outlet), _bearing(j, town)]
            spread = min(abs((p - q + 180) % 360 - 180) for i, p in enumerate(ways) for q in ways[i + 1:])
            if spread >= JUNCTION_SPREAD:
                found.append((math.dist(j, intake) + math.dist(j, outlet) + math.dist(j, town), j))
    return [j for _, j in sorted(found)]


def water_plan(intake: tuple[float, float], outlet: tuple[float, float], town: tuple[float, float],
               j: tuple[float, float], branch: float = 120.0) -> list[dict[str, Any]]:
    """A first water system around the three-way junction j: each facility's branch (one piece, at
    most `branch` long) heads for j, and the shared trunk joins both branch ends at j and runs on to
    the town. The facilities are never piped to each other directly."""
    # each branch ends to one side of its line to J, so the trunk leaves it with a bend
    a = _aside(intake, j, _toward(intake, j, min(branch, math.dist(intake, j) * 0.6)), ZIGZAG)
    b = _aside(outlet, j, _toward(outlet, j, min(branch, math.dist(outlet, j) * 0.6)), -ZIGZAG)
    return (_legs("intakeBranch", intake, a) + _legs("outletBranch", outlet, b)
            + _legs("sharedTrunk", a, j) + _legs("sharedTrunk", b, j) + _legs("sharedTrunk", j, town))


def _facility(candidate: dict[str, Any]) -> dict[str, Any]:
    return {"buildingPrefab": candidate["buildingPrefab"], "position": candidate["position"],
            "angleDegrees": candidate.get("angleDegrees", 0)}


def _placed_id(candidate: dict[str, Any]) -> int | None:
    facilities = mt._request("GET", "/state/facilities", query={"service": "Water", "limit": 1000}).get("facilities", [])
    spot = (candidate["position"]["x"], candidate["position"]["z"])
    return next((int(f["id"]) for f in facilities if f.get("prefab") == candidate["buildingPrefab"]
                 and math.dist((f["position"]["x"], f["position"]["z"]), spot) <= 2.0), None)


def _water_facilities() -> dict[str, dict[str, Any]]:
    """Intakes (a groundwater pump among them) and outlets already standing (a bootstrap
    interrupted before its pipes), told apart by what they pump rather than by name."""
    facilities = mt._request("GET", "/state/facilities", query={"service": "Water", "limit": 1000}).get("facilities", [])
    prefabs = {item["name"]: ((item.get("aiProfile") or {}).get("fields") or {})
               for item in mt._request("GET", "/prefabs/buildings", query={"service": "Water"}).get("buildings", [])}
    found: dict[str, dict[str, Any]] = {}
    for facility in facilities:
        fields = prefabs.get(str(facility.get("prefab"))) or {}
        role = "intake" if fields.get("waterIntake") else "outlet" if fields.get("sewageOutlet") else None
        if role and role not in found:
            found[role] = {"buildingPrefab": facility["prefab"], "buildingId": int(facility["id"]),
                           "position": {"x": facility["position"]["x"], "z": facility["position"]["z"]},
                           "angleDegrees": facility.get("angleDegrees", 0),
                           "waterSource": int(facility.get("waterSourceId") or 0),
                           "active": "Completed" in str(facility.get("flags")),
                           "groundwater": bool(fields.get("useGroundWater")),
                           "problems": str(facility.get("problems") or "")}
    return found


def groundwater_pair(town: tuple[float, float], search: dict[str, Any],
                     standing: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Where no river pair is safe (live: the unlocked shore of one map too short to keep any
    outlet downstream of an intake), a water tower near the town draws ground water and the
    sewage goes to the outlet site nearest the town that the river search found valid."""
    # the pump stands off the town: beside it, the pipes to it and to the town could not part at a junction
    pumps = mt.call_tool("propose_groundwater_pump_sites", {
        "serviceAreaAnchor": {"x": town[0], "z": town[1]}, "minDistance": GROUNDWATER_OFFSET, "searchRadius": 800,
        "maxResults": 6, "maxPreviewAttempts": 160})
    outlets = [attempt["outlet"] for attempt in search.get("attempts") or [] if attempt.get("outlet")]
    if standing.get("outlet"):
        outlets = [standing["outlet"]]
    if not pumps.get("candidates") or not outlets:
        return {"safe": False, "risks": (search.get("risks") or []) + (pumps.get("risks") or []) +
                ([] if outlets else ["noOutletCandidate"]), "candidateCounts": search.get("candidateCounts")}
    outlets.sort(key=lambda item: math.dist(town, (item["position"]["x"], item["position"]["z"])))
    risks = []
    point = lambda item: (item["position"]["x"], item["position"]["z"])  # noqa: E731
    for outlet in outlets[:3]:
        for intake in [standing["intake"]] if standing.get("intake") else [_facility(c) for c in pumps["candidates"]]:
            if not junctions(point(intake), point(outlet), town):
                risks.append("noJunctionForPipes")
                continue
            inspected = mt.call_tool("inspect_water_pair", {"intake": intake, "outlet": _facility(outlet)})
            if inspected.get("safe"):
                return {"safe": True, "intake": intake, "outlet": outlet, "groundwater": True}
            risks += inspected.get("risks") or []
    return {"safe": False, "risks": sorted(set(risks)), "candidateCounts": search.get("candidateCounts")}


def design_water(town: tuple[float, float], avoid: list[tuple[float, float]] = ()) -> dict[str, Any]:
    """The facilities (those standing, else the safest pair the hydrology allows, a finer search when
    a coarse one finds none) and the first water_plan, over the junctions in order, whose whole
    preview is safe."""
    # the plan names a facility by where it stands; its running state is not part of a plan
    standing = {role: {key: facility[key] for key in ("buildingPrefab", "buildingId", "position", "angleDegrees")}
                for role, facility in _water_facilities().items()}
    if "intake" in standing and "outlet" in standing:
        intake, outlet = standing["intake"], standing["outlet"]
    else:
        pair: dict[str, Any] = {}
        attempts: list[dict[str, Any]] = []  # every outlet the river searches found valid
        # near the town first (short pipes), then anywhere on the unlocked river
        near = {"serviceAreaAnchor": {"x": round(town[0], 2), "z": round(town[1], 2)}}
        for search in [{"resolution": r, **near} for r in WATER_SEARCH_RESOLUTIONS] + \
                [{"resolution": r} for r in WATER_SEARCH_RESOLUTIONS]:
            pair = mt.call_tool("propose_water_pair_candidates", search)
            if pair.get("safe"):
                break
            attempts += pair.get("attempts") or []
        else:
            pair = groundwater_pair(town, {**pair, "attempts": attempts}, standing)
            if not pair.get("safe"):
                return {"ok": False, "stage": "pair", "risks": pair.get("risks"), "counts": pair.get("candidateCounts")}
        # one facility already standing (its partner could not be placed) is kept; the search
        # supplies the other
        intake = standing.get("intake") or _facility(pair["intake"])
        outlet = standing.get("outlet") or _facility(pair["outlet"])
    ports = mt.call_tool("inspect_water_pair", {"intake": intake, "outlet": outlet}).get("previews", {})

    def port(role: str, facility: dict[str, Any]) -> tuple[float, float]:
        nodes = [n for p in ports.get(role, {}).get("networkPaths", []) if p.get("service") == "Water"
                 for n in p.get("nodes", [])]
        return (nodes[0]["x"], nodes[0]["z"]) if nodes else (facility["position"]["x"], facility["position"]["z"])

    risks = []
    ends = port("intake", intake), port("outlet", outlet)
    rejoins = []
    if len(standing) == 1:
        # a facility replacing one the river left joins the pipes already laid, as a player would:
        # a second trunk to the town only doubles them
        kept = next(iter(standing))
        new = "intake" if kept == "outlet" else "outlet"
        rejoins = rejoin_designs(new, ends[0] if new == "intake" else ends[1],
                                 ends[1] if new == "intake" else ends[0], avoid)
    designs = rejoins or [(j, water_plan(*ends, town, j), 0) for j in junctions(*ends, town, avoid)[:JUNCTION_TRIES]]
    for j, segments, laid in designs:
        plan = {"intake": intake, "outlet": outlet, "pipePrefab": "Water Pipe", "segments": segments,
                # a rejoin serves the town through the pipes laid: the joint stands for it
                "serviceAreaAnchor": {"x": round((j if laid else town)[0], 2), "z": round((j if laid else town)[1], 2)},
                # a pair sited near the town need not sit at the ends of the unlocked river
                "maxUpstreamGap": NEAR_TOWN_GAP, "maxDownstreamGap": NEAR_TOWN_GAP}
        # the pipes already laid are checked against the live network, not previewed again
        try:
            preview = mt._inspect_water_system_plan(plan, completed_segment_indexes=set(range(laid))) if laid \
                else mt.call_tool("preview_water_system_plan", plan)
        except mt.BridgeRequestError as error:  # the game refuses a piece outright: the next design
            preview = {"risks": [str(error)[:120]]}
        if preview.get("safe"):
            return {"ok": True, "plan": plan, "junction": list(j), "laid": laid}
        risks.append({"junction": [round(j[0]), round(j[1])], "risks": preview.get("risks")})
    return {"ok": False, "stage": "plan", "tried": risks}


def rejoin_designs(role: str, end: tuple[float, float], kept_port: tuple[float, float],
                   avoid: list[tuple[float, float]] = ()) -> list[tuple[tuple[float, float], list[dict[str, Any]], int]]:
    """Plans joining a new facility's port `end` to the pipes laid from the kept facility, nearest
    joint first. The pipes laid lead each plan (the kept branch, the way from it to the joint and
    one further pipe, so three meet as a water system needs) and the new pipes follow: a trunk from
    the joint and the new branch, one piece, to the port. Each design: (joint, segments, laid)."""
    live = mt._request("GET", "/state/networks", query={"service": "Water", "limit": 5000}).get("segments", [])
    point: dict[int, tuple[float, float]] = {}
    edges: dict[int, list[tuple[int, dict[str, Any]]]] = {}
    for s in live:
        a, b = int(s["startNodeId"]), int(s["endNodeId"])
        point[a], point[b] = (s["start"]["x"], s["start"]["z"]), (s["end"]["x"], s["end"]["z"])
        edges.setdefault(a, []).append((b, s))
        edges.setdefault(b, []).append((a, s))
    first = min(point, key=lambda node: math.dist(point[node], kept_port), default=None)
    if first is None or math.dist(point[first], kept_port) > 2.0 or len(edges[first]) != 1:
        return []
    junction, branch = edges[first][0]
    parent: dict[int, tuple[int, dict[str, Any]] | None] = {junction: None}
    queue = [junction]
    while queue:
        node = queue.pop(0)
        for other, s in edges[node]:
            if other not in parent and s is not branch:
                parent[other] = (node, s)
                queue.append(other)

    def piece(s: dict[str, Any], name: str) -> dict[str, Any]:
        return {"role": name, "start": {"x": round(s["start"]["x"], 2), "z": round(s["start"]["z"], 2)},
                "end": {"x": round(s["end"]["x"], 2), "z": round(s["end"]["z"], 2)}}

    kept = "outlet" if role == "intake" else "intake"
    designs = []
    for joint in sorted(parent, key=lambda node: math.dist(point[node], end)):
        if any(math.dist(point[joint], spot) < 50.0 for spot in avoid):
            continue
        path, nodes, node = [], [joint], joint
        while parent[node] is not None:
            node, s = parent[node]
            path.append(s)
            nodes.append(node)
        target = point[joint]
        # a pipe already ending at the port (the branch of the facility the new one replaces, where
        # the river still serves) is the new branch: nothing is laid
        reused = path[:1] if math.dist(end, target) <= PORT_TOLERANCE else []
        # a third pipe off the way, where it leaves one of its joints
        third = next((s for n in nodes[len(reused):] for _, s in edges[n]
                      if s is not branch and all(s is not p for p in path)), None)
        if third is None:
            continue
        laid = [piece(branch, f"{kept}Branch")] + [piece(s, "sharedTrunk") for s in path[len(reused):] + [third]]
        if reused:
            laid.append(piece(reused[0], f"{role}Branch"))
            new, target = [], point[nodes[1]]  # the joint is where that branch meets the trunk
        elif math.dist(end, target) < MIN_PIPE:
            continue  # the game will not lay a pipe this short
        elif math.dist(end, target) <= PIPE_PIECE:  # the branch alone reaches the joint
            new = _legs(f"{role}Branch", target, end)
        else:
            bend = _aside(end, target, _toward(end, target, 120.0), ZIGZAG)
            new = _legs("sharedTrunk", target, bend) + _legs(f"{role}Branch", bend, end)
        designs.append((target, laid + new, len(laid)))
        if len(designs) == JUNCTION_TRIES:
            break
    return designs


def _built_pipes(plan: dict[str, Any], laid: int = 0) -> list[int]:
    """Live water pipes lying on the plan's segments (built by it: not the first `laid`, which
    stood before it)."""
    pieces = {tuple(sorted(((round(s["start"]["x"]), round(s["start"]["z"])),
                            (round(s["end"]["x"]), round(s["end"]["z"]))))) for s in plan["segments"][laid:]}
    live = mt._request("GET", "/state/networks", query={"service": "Water", "limit": 5000}).get("segments", [])
    return [int(s["id"]) for s in live if tuple(sorted(((round(s["start"]["x"]), round(s["start"]["z"])),
                                                        (round(s["end"]["x"]), round(s["end"]["z"]))))) in pieces]


def abandon_plan(city: City, state: dict[str, Any], label: str, step: str, error: Any) -> dict[str, Any]:
    """A pipe the plan cannot lay (something grew in its way, or the joint will not take it): the
    plan's pipes are removed and its junction avoided, so the next wake designs around them with
    the facilities kept. After WATER_ATTEMPTS designs the planner decides."""
    removed = []
    for pipe in _built_pipes(state["plan"], state.get("laid", 0)):
        outcome = protocol.run(f"water-undo-{label}-{pipe}", "bulldoze_entity", {"entityType": "netSegment", "id": pipe})
        if outcome["ok"]:
            removed.append(pipe)
    attempts = state.get("attempts", 1)
    city.save_state("water-bootstrap", {"avoid": state.get("avoid", []) + ([state["junction"]] if state.get("junction") else []),
                                        "attempts": attempts})
    return {"ok": None if attempts < WATER_ATTEMPTS else False, "stage": step, "error": str(error)[:200],
            "removedPipes": removed, "attempts": attempts}


def add_water(city: City, label: str, unlocked: list[str]) -> dict[str, Any] | None:
    """A city's first water system, when it has none: design_water, then the facilities and each
    pipe in order. The plan and the steps built are kept in the city state, so a wake that stops
    partway is continued by the next one rather than replanned around half a system. The pieces are
    one change: a facility not yet piped reports itself unconnected, so only the last step's
    regression check decides."""
    if "water" not in unlocked:
        return None
    state = city.load_state("water-bootstrap", {})
    if state.get("complete"):
        return None
    if not state.get("plan"):
        utilities = mt.city_health()["utilities"]
        # a bootstrap already under way goes on: capacity can still show pipes it just removed
        if not state.get("attempts") and not state.get("relocate") and (
                (utilities["water"].get("capacity") or 0) > 0 or (utilities["sewage"].get("capacity") or 0) > 0):
            return None  # an existing system grows by a planner's choice of a further pair
        town = town_center()
        if town is None:
            return None
        design = design_water(town, [tuple(spot) for spot in state.get("avoid", [])])
        if not design["ok"]:
            return design
        laid = design.get("laid", 0)
        state = {"plan": design["plan"], "junction": design["junction"], "laid": laid,
                 "done": [f"pipe{index}" for index in range(laid)],
                 "avoid": state.get("avoid", []), "attempts": state.get("attempts", 0) + 1}
        city.save_state("water-bootstrap", state)
    plan, done = state["plan"], state["done"]
    outcome: dict[str, Any] = {}
    steps = [("intake", "place_water_facility"), ("outlet", "place_water_facility")] + \
        [(f"pipe{index}", "build_water_pipe") for index in range(len(plan["segments"]))]
    for step, tool in steps:
        if step in done:
            continue
        if tool == "place_water_facility":
            if not plan[step].get("buildingId"):
                outcome = protocol.run(f"water-{step}-{label}", tool,
                                       {"role": step, "intake": plan["intake"], "outlet": plan["outlet"],
                                        **{key: plan[key] for key in ("maxUpstreamGap", "maxDownstreamGap")
                                           if key in plan}}, 4)
                placed = _placed_id(plan[step])
                if not outcome["ok"] or placed is None:
                    # the shore changed since the plan (live: water receded from an outlet site):
                    # design again, keeping what stands
                    return abandon_plan(city, state, label, step, outcome.get("error"))
                plan[step] = {**plan[step], "buildingId": placed}
        else:
            outcome = protocol.run(f"water-{step}-{label}", tool, {**plan, "segmentIndex": int(step[4:])})
            if not outcome["ok"]:
                return abandon_plan(city, state, label, step, outcome.get("error"))
        done.append(step)
        city.save_state("water-bootstrap", state)
    state["complete"] = True
    city.save_state("water-bootstrap", state)
    return {"ok": True, "regressionOk": outcome.get("regressionOk"), "intake": plan["intake"]["buildingId"],
            "outlet": plan["outlet"]["buildingId"], "pipes": len(plan["segments"])}


PUMP_BRANCH = 360.0  # a new facility's pipe port this near a live pipe node is joined (connect_water_facility: 400m)
PORT_SLACK = 40.0  # a facility's pipe port stands at most this far from where it stands
PUMP_SPACING = 60.0  # a new pump keeps off the lots of the pumps standing
# the pump sites are searched around the first pump, then around points PUMP_RING metres from it and
# the town centre, while the pumps standing take every site found: a search returns its 8 nearest
# (live: thirteen towers stood within 400m of the first, all eight sites lay within 60m of one, and
# water at 85% stopped the loop; around the ring 28 sites lay clear, 64-130m from the pipes)
PUMP_RING = 500.0
PUMP_RING_STEPS = 6
PUMP_SETTLE_WAKES = 2  # wakes after adding a pump before another: its capacity shows once it runs


def _pipe_nodes() -> list[tuple[float, float]]:
    return [(segment[end]["x"], segment[end]["z"])
            for segment in mt._request("GET", "/state/networks", query={"service": "Water", "limit": 5000})
            .get("segments", []) if str(segment.get("service", "Water")) == "Water" for end in ("start", "end")]


def add_water_capacity(city: City, label: str) -> dict[str, Any] | None:
    """Another facility when the water system runs near its capacity at full budget (the budget
    sets capacity, so only a trimmed one is turned up first: restore_budgets), placed as a safe pair
    with the facility of the other role and joined to the nearest pipe:

    - water: a groundwater pump, sited as the first one was (propose_groundwater_pump_sites: clean
      ground, no homes in its noise, off what pollutes the ground; live: at 2,095 people one water
      tower ran at 89%). A city drawing river water grows by another intake on that river: that
      siting is the planner's, and the pressure stops the loop as before.
    - sewage: an outlet on a shore the river searches find valid (live: at 4,060 people one outlet
      ran at 86% while two towers pumped).

    One facility a wake, water first; PUMP_SETTLE_WAKES pass before another."""
    state = city.load_state("water-capacity", {"settle": 0})
    if state.get("settle", 0) > 0:
        state["settle"] -= 1
        city.save_state("water-capacity", state)
        return {"ok": None, "stage": "settling", "facility": state.get("facility") or state.get("pump")}
    utilities = mt.city_health()["utilities"]
    water = (utilities.get("water") or {}).get("utilization") or 0
    sewage = (utilities.get("sewage") or {}).get("utilization") or 0
    if max(water, sewage) < POWER_PRESSURE or budget_of("Water") < 100:
        return None
    standing = _water_facilities()
    intake, outlet = standing.get("intake"), standing.get("outlet")
    if not intake or not outlet:
        return None
    if water >= POWER_PRESSURE:
        if not intake["groundwater"]:
            return {"ok": None, "stage": "riverIntakeForPlanner", "use": water}
        return _add_water_facility(city, label, "intake", intake, outlet, _pump_sites(intake), water)
    return _add_water_facility(city, label, "outlet", outlet, intake, _outlet_sites(outlet["position"]), sewage)


def _outlet_sites(near: dict[str, float]) -> list[dict[str, Any]]:
    """Every outlet site the river searches find valid, near the outlet standing and anywhere on
    the unlocked river: each search's outlet candidates, its safe pair's and each attempt's (live:
    the sites of whole-river pairs lay 180-270m from the pipes, beyond a facility's branch)."""
    found: dict[tuple[int, int], dict[str, Any]] = {}
    for search in [{"resolution": r, "serviceAreaAnchor": near} for r in WATER_SEARCH_RESOLUTIONS] + \
            [{"resolution": r} for r in WATER_SEARCH_RESOLUTIONS]:
        result = mt.call_tool("propose_water_pair_candidates", search)
        outlets = list(result.get("outletCandidates") or []) + \
            ([result["outlet"]] if result.get("safe") and result.get("outlet") else []) + \
            [attempt["outlet"] for attempt in result.get("attempts") or [] if attempt.get("outlet")]
        for item in outlets:
            found.setdefault((round(item["position"]["x"]), round(item["position"]["z"])), item)
    return list(found.values())


def _standing(prefab: str) -> list[tuple[float, float]]:
    return [(f["position"]["x"], f["position"]["z"]) for f in mt._request(
        "GET", "/state/facilities", query={"service": "Water", "limit": 1000}).get("facilities", [])
        if f.get("prefab") == prefab]


def _pump_sites(intake: dict[str, Any]) -> list[dict[str, Any]]:
    """Groundwater pump sites around the first pump, else around the next anchor (PUMP_RING, the
    town centre) while every site found lies on the lots of the pumps standing."""
    standing = _standing(intake["buildingPrefab"])
    first = (intake["position"]["x"], intake["position"]["z"])
    ring = [(first[0] + PUMP_RING * math.cos(step * 2 * math.pi / PUMP_RING_STEPS),
             first[1] + PUMP_RING * math.sin(step * 2 * math.pi / PUMP_RING_STEPS)) for step in range(PUMP_RING_STEPS)]
    sites: list[dict[str, Any]] = []
    for anchor in [first] + ring + [None]:
        anchor = anchor or town_center()  # read only when the ring is spent
        if anchor is None:
            continue
        sites = mt.call_tool("propose_groundwater_pump_sites", {
            "serviceAreaAnchor": {"x": round(anchor[0], 1), "z": round(anchor[1], 1)},
            "buildingPrefab": intake["buildingPrefab"], "minDistance": PUMP_SPACING, "searchRadius": 400,
            "maxResults": 8, "maxPreviewAttempts": 160}).get("candidates") or []
        if any(all(math.dist((site["position"]["x"], site["position"]["z"]), spot) >= PUMP_SPACING
                   for spot in standing) for site in sites):
            break
    return sites


def _add_water_facility(city: City, label: str, role: str, like: dict[str, Any], partner: dict[str, Any],
                        sites: list[dict[str, Any]], use: float) -> dict[str, Any]:
    """The first of the sites PUMP_SPACING off the facilities of its kind standing, whose pipe port
    (from a native pair preview; live: an outlet's port stood 18m nearer the pipes than the outlet)
    lies within PUMP_BRANCH of a live pipe node, that places as a safe pair with partner and joins
    the pipes, nearest the pipes first; one the pipes cannot reach is removed, as it adds nothing
    and costs upkeep."""
    other = "outlet" if role == "intake" else "intake"
    standing = _standing(like["buildingPrefab"])
    nodes = _pipe_nodes()
    reach = sorted((min((math.dist((site["position"]["x"], site["position"]["z"]), node) for node in nodes),
                        default=math.inf), index, site)
                   for index, site in enumerate(sites)
                   if all(math.dist((site["position"]["x"], site["position"]["z"]), spot) >= PUMP_SPACING
                          for spot in standing))
    tried = []
    near = [item for item in reach if item[0] <= PUMP_BRANCH + PORT_SLACK]
    for _, _, site in near[:6]:
        if sum(1 for item in tried if item["stage"] in ("place", "connect")) >= 3:
            break
        candidate = _facility({"buildingPrefab": like["buildingPrefab"], **site})
        pairing = {"role": role, role: candidate, other: _facility(partner) | {"buildingId": partner["buildingId"]}}
        pair = mt.call_tool("inspect_water_pair", {role: candidate, other: pairing[other]})
        ports = mt._preview_water_pipe_connections((pair.get("previews") or {}).get(role) or {})
        distance = min((math.dist((port["x"], port["z"]), node) for port in ports for node in nodes), default=math.inf)
        if not pair.get("safe") or distance > PUMP_BRANCH:
            tried.append({"position": site["position"], "stage": "unsafe" if not pair.get("safe") else "farPort",
                          "risks": pair.get("risks"), "port": round(distance, 1) if ports else None})
            continue
        placed = protocol.run(f"water-{role}-{label}", "place_water_facility", pairing, 4)
        facility = _placed_id(candidate) if placed["ok"] else None
        if facility is None:
            tried.append({"position": site["position"], "stage": "place", "error": placed.get("error")})
            continue
        joined = protocol.run(f"water-{role}-pipe-{label}", "connect_water_facility",
                              {"buildingId": facility, "pairBuildingId": partner["buildingId"]}, 4)
        if not (joined["ok"] and (joined.get("result") or {}).get("connected", True)):
            protocol.run(f"water-{role}-undo-{label}", "bulldoze_entity", {"entityType": "building", "id": facility})
            tried.append({"position": site["position"], "stage": "connect", "error": joined.get("error")})
            continue
        city.save_state("water-capacity", {"settle": PUMP_SETTLE_WAKES, "facility": facility, "role": role})
        return {"ok": True, "regressionOk": joined.get("regressionOk"), "role": role, "facility": facility,
                "use": use, "branch": round(distance, 1)}
    return {"ok": False, "stage": "noPumpSite" if role == "intake" else "noOutletSite", "role": role, "use": use,
            "tried": tried, "sites": len(sites), "nearPipes": len(near)}


def town_center() -> tuple[float, float] | None:
    """Where the city lives: the centre of its zoned blocks, else of its local roads."""
    blocks = mt._request("GET", "/state/zone-blocks", query={"limit": 5000}).get("blocks", [])
    points = [(b["center"]["x"], b["center"]["z"]) for b in blocks if (b.get("zonedCells") or 0) > 0]
    if not points:
        segments = mt._request("GET", "/state/networks", query={"limit": 20000}).get("segments", [])
        points = [((s["start"]["x"] + s["end"]["x"]) / 2, (s["start"]["z"] + s["end"]["z"]) / 2) for s in segments
                  if "Road" in str(s.get("prefab")) and "Highway" not in str(s.get("prefab"))]
    if not points:
        return None
    return sum(p[0] for p in points) / len(points), sum(p[1] for p in points) / len(points)


def budget_of(service: str) -> int:
    return next((int(item["day"]) for item in mt._request("GET", "/state/budgets")["budgets"]
                 if item["service"] == service and item.get("subService", "None") == "None"), 100)


def set_budget(label: str, service: str, value: int, reason: str, expected: str) -> dict[str, Any]:
    outcome = protocol.run(f"budget-{service}-{label}", "set_service_budget", {
        "service": service, "budget": value, "dayAndNight": True, "reason": reason, "expectedOutcome": expected})
    if outcome.get("error"):  # the fiscal cooldown refuses budget changes for a while: next wake
        return {"budget": service, "ok": None, "stage": "cooldown", "error": outcome["error"][:120]}
    return {"budget": service, "ok": outcome["ok"], "regressionOk": outcome["regressionOk"], "value": value}


def restore_budgets(label: str) -> list[dict[str, Any]]:
    """A utility trimmed while idle that now runs near its capacity has its budget put back to full:
    capacity follows the budget, so it needs no new facility before the budget is full again (live:
    water at 87% of a budget trimmed to 50). Full undoes the trim, which the fiscal cooldown lets
    through; a partial raise is a new adjustment it holds for an hour (live: a raise to 80 held
    two wakes while the town grew, until 29 homes ran dry)."""
    utilities = mt.city_health()["utilities"]
    actions = []
    for family, service in UTILITY_BUDGETS.items():
        # the water budget runs the outlets too: sewage pressure counts as much
        names = (family, "sewage") if family == "water" else (family,)
        use = max((utilities.get(name) or {}).get("utilization") or 0 for name in names)
        current = budget_of(service)
        if use < POWER_PRESSURE or current >= 100:
            continue
        actions.append(set_budget(label, service, 100, "post-change-correction",
                                  f"{family} capacity back to full as its use nears it"))
    return actions


def in_deficit(health: dict[str, Any], falling: bool = False) -> bool:
    """The week's net is negative, or the cash keeps falling though the week balances: one-off
    spending (facilities, roads, zoning) left the week's net near zero while cash fell 250,000 over
    eight wakes (live), and neither budgets nor taxes were looked at."""
    return falling or (health["cash"].get("periodNet") or 0) < 0


def trim_budgets(label: str, falling: bool = False) -> list[dict[str, Any]]:
    """While the city runs a deficit, each utility far below its capacity has its budget turned
    down (capacity follows the budget) to what keeps the use under BUDGET_HEADROOM of it; one that
    runs short is turned back up first (add_power, the pressure stop). One fiscal change is allowed
    an hour, so the costliest upkeep goes first."""
    health = mt.city_health()
    if not in_deficit(health, falling):
        return []
    upkeep = {row["service"]: row.get("expenses") or 0
              for row in mt.call_tool("inspect_economy", {}).get("services", [])}
    actions = []
    for family, service in sorted(UTILITY_BUDGETS.items(), key=lambda item: -upkeep.get(item[1], 0)):
        use = health["utilities"][family]
        if not use.get("capacity") or starved(family):
            continue  # its use reads low because it delivers too little, not because it is idle
        current = budget_of(service)
        needed = (use.get("used") or 0) / use["capacity"] * current / BUDGET_HEADROOM
        target = max(BUDGET_FLOOR, int(math.ceil(needed / BUDGET_STEP) * BUDGET_STEP))
        if target <= current - BUDGET_STEP:
            actions.append(set_budget(label, service, target, "sustained-deficit",
                                      f"{family} upkeep down while use stays under half the capacity"))
            break  # the next change waits for the fiscal cooldown
    return actions


# a service budget raised above full for more vehicles (services.raise_budget) and the readings of
# every family that budget pays for; it comes back toward full while all of them stay under
# RAISED_IDLE (live: garbage at 150% for 32 facilities 36% in use, a third of the city's upkeep,
# while the city ran a deficit). Fire has no capacity reading, so its budget is left as it is.
RAISED_LOADS = {"Garbage": (("utilities", "garbage"),),
                "HealthCare": (("services", "healthcare"), ("services", "deathcare", "storage")),
                "PoliceDepartment": (("services", "police"),),
                "Education": (("services", "elementaryEducation"), ("services", "highSchoolEducation"),
                              ("services", "universityEducation"))}
RAISED_IDLE = 0.6


def reading(health: dict[str, Any], path: tuple[str, ...]) -> float | None:
    """The utilization at path in city_health (group, family[, storage]), or None."""
    node: Any = health
    for key in path:
        node = (node or {}).get(key) if isinstance(node, dict) else None
    return (node or {}).get("utilization") if isinstance(node, dict) else None


def lower_raised_budgets(label: str, falling: bool = False) -> list[dict[str, Any]]:
    """While the city runs a deficit, a budget raised above full whose families all stay under
    RAISED_IDLE of their capacity goes back to full in one change: the raise bought vehicles for a
    cluster no facility could be placed for, and nothing turned it down again. One change, as the
    fiscal cooldown allows one an hour (live: 150 to 140 and then an hour's wait while the city lost
    35,000 a wake); full capacity at under 60% use stays under 90% after it."""
    health = mt.city_health()
    if not in_deficit(health, falling):
        return []
    for service, readings in RAISED_LOADS.items():
        current = budget_of(service)
        if current <= 100:
            continue
        loads = [reading(health, path) for path in readings]
        if any(load is None or load >= RAISED_IDLE for load in loads):
            continue
        return [set_budget(label, service, 100, "sustained-deficit",
                           f"{service} back to full while its capacity stays under {RAISED_IDLE:.0%} in use")]
    return []


def tax_rate(service: str) -> int:
    rates = mt._request("GET", "/state/economy").get("aggregateTaxRates", [])
    return max((int(item["rate"]) for item in rates if item.get("service") == service), default=9)


def balance(label: str, falling: bool = False) -> list[dict[str, Any]]:
    """One fiscal change a wake while the city runs a deficit: an idle utility's budget first (it
    saves the most), then a raised service budget whose capacity sits idle, else, while homes are
    still wanted, a point more tax on homes and shops."""
    trimmed = trim_budgets(label, falling) or lower_raised_budgets(label, falling)
    if trimmed:
        return trimmed
    if not in_deficit(mt.city_health(), falling) \
            or (mt._request("GET", "/state/summary")["demand"].get("residential") or 0) < TAX_DEMAND:
        return []
    rates = {service: tax_rate(service) for service in TAXED}
    service = min(TAXED, key=lambda name: rates[name])  # homes and shops take turns
    if rates[service] >= TAX_CEILING:
        return []
    outcome = protocol.run(f"tax-{label}", "set_tax_rate", {
        "service": service, "rate": rates[service] + 1, "reason": "sustained-deficit",
        "expectedOutcome": "the deficit closes while homes are still wanted"})
    if outcome.get("error"):
        return [{"tax": service, "ok": None, "stage": "cooldown", "error": outcome["error"][:120]}]
    return [{"tax": service, "ok": outcome["ok"], "regressionOk": outcome["regressionOk"], "rate": rates[service] + 1}]


def shortfall(service: str) -> dict[str, Any]:
    """diagnose_utility_shortfall: the buildings without power or water split into those the network
    reaches yet gives nothing (the supply is short) and those beyond it (a line or pipe is missing).
    Both are checked whenever either service is missing anywhere."""
    return mt.call_tool("diagnose_utility_shortfall", {"service": service, "limit": SHORTFALL_SAMPLE})


def starved(service: str = "electricity") -> bool:
    """Buildings dark within the network's reach: it has too little to give. The game counts only
    what it delivers as used, so a short supply still reads well below capacity (live: 46 buildings
    dark at 68%, all lit by one more turbine)."""
    return len(shortfall(service).get("coveredButShort") or []) >= STARVED_MIN


SUN = "SolarPowerPlantAI"


def _standing_rated(options: list[dict[str, Any]]) -> list[tuple[str, float]]:
    """Each standing plant's AI type and rating, in capacity units."""
    kinds = {option["name"]: option for option in options}
    rated = []
    for plant in mt._request("GET", "/state/facilities", query={"service": "Electricity", "limit": 500}) \
            .get("facilities", []):
        option = kinds.get(str(plant.get("prefab")))
        if option is None or not plant.get("productionRate"):
            continue
        rated.append((str(option.get("aiType")), float((option.get("roles") or {}).get("power") or 0) * KW_PER_PRODUCTION))
    return rated


def grid_mix(options: list[dict[str, Any]]) -> tuple[float, float]:
    """What the standing plants rate, steady and variable (wind and sun), in capacity units."""
    standing = _standing_rated(options)
    return (sum(kw for ai, kw in standing if ai not in VARIABLE_OUTPUT),
            sum(kw for ai, kw in standing if ai in VARIABLE_OUTPUT))


def sun_rating(options: list[dict[str, Any]]) -> float:
    """What the standing solar plants rate: none of it comes at night."""
    return sum(kw for ai, kw in _standing_rated(options) if ai == SUN)


def variable_yield(options: list[dict[str, Any]], capacity: float) -> float:
    """The share of their rated output the standing wind turbines give: the grid's capacity less
    what the steady plants rate, over what the turbines rate (live: 13 turbines rated 104 MW gave
    45 MW, 44%)."""
    steady, variable = grid_mix(options)
    if variable <= 0:
        return WIND_YIELD_DEFAULT
    return max(0.2, min(1.0, (capacity - steady) / variable))


def expected_kw(option: dict[str, Any], wind: float) -> float:
    rated = float((option.get("roles") or {}).get("power") or 0) * KW_PER_PRODUCTION
    return rated * (wind if option.get("aiType") in VARIABLE_OUTPUT else 1.0)


def yield_sample() -> float | None:
    """What the variable plants give now, as a share of what they rate."""
    options = mt.call_tool("compare_service_options", {"family": "power", "need": "power"})["options"]
    steady, variable = grid_mix(options)
    capacity = float(mt.city_health()["utilities"]["electricity"].get("capacity") or 0)
    return round(max(0.0, capacity - steady) / variable, 3) if variable > 0 else None


def firm_shortfall(used: float, capacity: float, typical_yield: float | None = None) -> float:
    """What the grid lacks to carry its use at POWER_PRESSURE through a lull: the steady plants give
    all they rate, the turbines LULL_SHARE of what they typically give, and solar plants nothing (a
    lull may come at night). Typically is typical_yield of what they rate (the median of recent
    wakes, cycle.typical_yield); without it, what the grid gives now less the solar rating: one
    reading that is already a lull halved again (live: at 39% use, a mild lull read asked for 107 MW
    more, and 28 turbines were added in a few wakes)."""
    options = mt.call_tool("compare_service_options", {"family": "power", "need": "power"})["options"]
    steady, variable = grid_mix(options)
    sun = sun_rating(options)
    wind = typical_yield * max(0.0, variable - sun) if typical_yield else max(0.0, capacity - steady - sun)
    return used / POWER_PRESSURE - (steady + wind * LULL_SHARE)


def add_power(label: str, unlocked: list[str], peak: float | None = None,
              firm: bool = False, typical_yield: float | None = None) -> dict[str, Any] | None:
    """Power plants when the grid has no capacity, runs near full, or is starved: enough to bring
    its use down to POWER_TARGET (MAX_PLANTS a wake), the options ranked by the output they will
    give (place_plant). One turbine a wake fell behind a growing town (live: 2,900 people at 134%
    of capacity, 170 buildings dark).

    peak is the use the wake read while its clock ran: a grid of wind turbines gives what the wind
    gives, and read once it can look easy while it runs short in a lull (live: 40 turbines read 57%
    here and 115% at the wake's reading, and nothing was built). The higher reading decides, and a
    lull is sized to: the capacity it left (used / peak) is brought up to POWER_TARGET.

    firm: the grid once ran short where no reading saw it (cycle.firm_wanted). It is then built
    up until it carries its use through a lull (firm_shortfall): steady plants first, and where none
    has a site, turbines, each counted at LULL_SHARE (live: in a town grown dense no coal or oil
    plant had a site; turbines always had)."""
    if "electricity" not in unlocked:
        return None
    electricity = mt.city_health()["utilities"]["electricity"]
    capacity, utilization = electricity.get("capacity") or 0, electricity.get("utilization") or 0
    lull = bool(peak and peak > utilization)
    utilization = max(utilization, peak or 0)
    firm_need = firm_shortfall(electricity.get("used") or 0, capacity, typical_yield) if firm and capacity > 0 else 0.0
    short = capacity <= 0 or utilization >= POWER_PRESSURE or starved()
    if not short and firm_need <= 0:
        return None
    if capacity > 0 and budget_of("Electricity") < 100:
        # a budget turned down earlier is turned back up before anything is built; while the fiscal
        # cooldown refuses that, the town is not left without power: a plant is built
        raised = set_budget(label, "Electricity", 100, "post-change-correction", "power capacity back to full")
        if raised.get("ok") is not None:
            return raised
    anchor = town_center()
    if anchor is None:
        return None
    # a starved grid reads below its capacity: it gets one plant at least
    used = electricity.get("used") or 0
    need = used / POWER_TARGET - (used / utilization if lull else capacity) if capacity > 0 else 0.0
    steady = firm_need > 0
    lull_only = firm_need > 0 and not short
    searches = FIRM_SEARCHES if lull_only else SITE_SEARCHES
    budget = cash_display() * PLANT_SPEND_SHARE
    plants: list[dict[str, Any]] = []
    unsited: set[str] = set()
    while len(plants) < MAX_PLANTS and (need > 0 or firm_need > 0 or not plants):
        plant = place_plant(f"{label}-{len(plants)}", anchor, unsited, budget, industry_anchors(), steady, searches,
                            not lull_only)
        if steady and not plant["ok"] and plant.get("reason") == "noSite":
            steady = False  # no steady plant has a site: turbines carry the lull
            plant = place_plant(f"{label}-{len(plants)}", anchor, unsited, budget, industry_anchors(), False, searches,
                                not lull_only)
        plants.append(plant)
        if not plant["ok"]:
            break
        need -= plant["expectedKw"]
        firm_need -= plant["expectedKw"] * (1.0 if plant.get("steady") else 0.0 if plant.get("sun") else LULL_SHARE)
        budget -= plant["cost"]
    built = [plant for plant in plants if plant["ok"]]
    first = built[0] if built else plants[0]
    if not built and not short and first.get("reason") == "noSite":
        # only the lull wanted it: a precaution with no site does not stop the loop (cycle waits
        # FIRM_RETRY wakes before it looks again)
        return {**first, "ok": None, "reason": "firmNoSite", "firmNeededKw": round(max(0.0, firm_need))}
    return {**first, "ok": bool(built) if built else first["ok"],
            "regressionOk": False if any(plant.get("regressionOk") is False for plant in plants) else first.get("regressionOk"),
            "plants": len(built), "stillNeededKw": round(max(0.0, need)),
            # every plant tried, so a wake that stopped short says why (the first is spread above)
            "attempts": [{key: plant.get(key) for key in ("prefab", "ok", "reason", "error") if plant.get(key) is not None}
                         for plant in plants],
            **({"firmNeededKw": round(max(0.0, firm_need))} if firm else {})}


def industry_anchors() -> list[tuple[float, float]]:
    """The centres of the largest industrial districts: a plant's noise and pollution trouble
    homes, not factories (live: every site within 1,280m of a grown town was rejected for its
    homes while the industrial district on a bought tile had room)."""
    factories = [{"id": item["id"], "x": item["position"]["x"], "z": item["position"]["z"]} for item in next(
        (value for value in mt._request("GET", "/state/growables", query={"limit": 50000}).values()
         if isinstance(value, list)), []) if item.get("service") == "Industrial" and item.get("position")]
    groups = sorted(mayor_problems.clusters(factories, INDUSTRY_LINK), key=lambda group: -group["count"])
    return [(group["center"]["x"], group["center"]["z"]) for group in groups[:INDUSTRY_ANCHORS]]


def cash_display() -> float:
    return float(mt.city_health()["cash"]["amount"]) / mt.MONEY_INTERNAL_SCALE


PLANT_PREVIEWS = 48  # native previews one plant site search may make


def place_plant(label: str, anchor: tuple[float, float], unsited: set[str] | None = None,
                budget: float | None = None, more: list[tuple[float, float]] = (),
                steady: bool = False, searches: tuple = SITE_SEARCHES, sun: bool = True) -> dict[str, Any]:
    """The power option that gives the most per upkeep, sited by propose_power_plant_candidates near
    the anchor (it keeps noise and pollution off homes; a plant that pollutes the ground keeps off
    the groundwater pumps too), from a strict search to a looser one. A wind turbine is counted at
    the share of its rating the standing ones give, and of two equal, the larger goes first (fewer
    sites and lines). An option with no site this wake is not searched again (unsited). Each
    search is tried from the anchor, then from the further anchors (more: industrial districts)."""
    unsited = set() if unsited is None else unsited
    electricity = mt.city_health()["utilities"]["electricity"]
    every = mt.call_tool("compare_service_options", {"family": "power", "need": "power"})["options"]
    wind = variable_yield(every, float(electricity.get("capacity") or 0))
    options = [o for o in every if o.get("affordable") and o["name"] not in unsited
               and not (steady and o.get("aiType") in VARIABLE_OUTPUT)  # steady: no wind or sun
               and (sun or o.get("aiType") != SUN)  # for a lull: no sun (it may come at night)
               and (budget is None or float(o.get("constructionCost") or 0) / mt.MONEY_INTERNAL_SCALE <= budget)]
    options.sort(key=lambda o: (-round(expected_kw(o, wind) / max(1.0, float(o.get("maintenanceCost") or 0)), 3),
                                -expected_kw(o, wind)))
    wells = mt._land_use_context()["wells"] if any(o.get("pollutionRadius") for o in options) else []
    tried = []
    for option in options:
        for (radius, ratio), spot in [(search, spot) for search in searches for spot in [anchor, *more]]:
            try:
                # each attempt is a native preview on the game thread: 48 a search, not 128, and a
                # search the thread was too busy to answer moves on (live: at 27,000 people one timed
                # out in every one of three tries of a wake, and the wake failed)
                proposal = mt.call_tool("propose_power_plant_candidates", {
                    "buildingPrefab": option["name"], "serviceAreaAnchor": {"x": spot[0], "z": spot[1]},
                    "searchRadius": radius, "minimumVariableOutputRatio": ratio,
                    "maxPreviewAttempts": PLANT_PREVIEWS, "maxResults": 3})
            except (mt.BridgeRequestError, RuntimeError) as error:
                if "command was not run" not in str(error):
                    raise
                tried.append({"prefab": option["name"], "radius": radius, "ratio": ratio, "found": False,
                              "anchor": [round(spot[0]), round(spot[1])], "busy": True})
                continue
            sites = (proposal.get("candidates") or []) if proposal.get("safe") else []
            if option.get("pollutionRadius"):
                sites = [s for s in sites if all(math.dist((s["position"]["x"], s["position"]["z"]), well)
                                                 >= mt.GROUNDWATER_CLEARANCE for well in wells)]
            site = next(iter(sites), None)
            tried.append({"prefab": option["name"], "radius": radius, "ratio": ratio, "found": site is not None,
                          "anchor": [round(spot[0]), round(spot[1])]})
            if site is None:
                continue
            outcome = protocol.run(f"power-plant-{label}", "place_power_plant", {
                "buildingPrefab": option["name"], "position": site["position"],
                "angleDegrees": site.get("angleDegrees", 0), "minimumVariableOutputRatio": ratio}, 8)
            return {"prefab": option["name"], "position": site["position"], "ok": outcome["ok"],
                    "regressionOk": outcome["regressionOk"], "error": outcome.get("error"), "tried": tried,
                    "expectedKw": round(expected_kw(option, wind)),
                    "steady": option.get("aiType") not in VARIABLE_OUTPUT, "sun": option.get("aiType") == SUN,
                    "cost": float(option.get("constructionCost") or 0) / mt.MONEY_INTERNAL_SCALE}
        unsited.add(option["name"])
    return {"ok": False, "reason": "noSite", "tried": tried, "expectedKw": 0, "cost": 0.0}


def repair_power(label: str) -> dict[str, Any]:
    skipped: set[int] = set()
    steps = []
    coverage = mt.call_tool("inspect_electricity_service_coverage", {"limit": 64})
    diagnosis = shortfall("electricity") if coverage.get("buildings") else {}
    # a building the grid already reaches waits for power (add_power), not for a line; but one
    # alone dark while the grid has power to spare is not starved: it is not joined (live: a new
    # outlet read "reached but short" at 44% use, and a 58m line lit it)
    reach = {int(item["id"]) for item in diagnosis.get("notCovered") or []}
    short = diagnosis.get("coveredButShort") or []
    use = ((diagnosis.get("supply") or {}).get("electricity") or {}).get("utilization") or 0
    if 0 < len(short) < STARVED_MIN and use < POWER_PRESSURE:
        reach |= {int(item["id"]) for item in short}
    positions: dict[int, tuple[float, float]] = {}
    # a building no line joins is skipped without spending a line: its neighbour may take one whose
    # grid then reaches it (live: three skipped buildings used every try, the one beside them with a
    # valid line was never tried)
    for _ in range(MAX_LINES * 4 if reach else 0):
        if sum(1 for step in steps if step["ok"]) >= MAX_LINES:
            break
        coverage = mt.call_tool("inspect_electricity_service_coverage", {"limit": 64})
        positions.update({int(b["id"]): (b["position"]["x"], b["position"]["z"])
                          for b in coverage.get("buildings", []) if b.get("position")})
        # a building just joined can read dark until the game updates its grid: one line each
        joined = {step["building"] for step in steps}
        targets = [b["id"] for b in coverage.get("buildings", []) if b["id"] not in skipped | joined
                   and (int(b["id"]) in reach or len(reach) >= SHORTFALL_SAMPLE)]
        if not targets:
            break
        building = targets[0]
        proposal = mt.call_tool("propose_power_line_connection", {"buildingId": building})
        candidate = proposal.get("candidate")
        if not proposal.get("actionRequired") or not (proposal.get("safe") and candidate):
            skipped.add(building)
            continue
        outcome = protocol.run(f"power-line-{label}-{building}", "build_network", {
            "roadPrefab": candidate["powerLinePrefab"],
            "start": {"x": candidate["start"]["x"], "z": candidate["start"]["z"]},
            "end": {"x": candidate["end"]["x"], "z": candidate["end"]["z"]}}, 8)
        steps.append({"building": building, "ok": outcome["ok"], "regressionOk": outcome["regressionOk"]})
        if not outcome["ok"]:
            break
    if skipped:
        spots = [positions[item] for item in skipped if item in positions]
        near = (sum(x for x, _ in spots) / len(spots), sum(z for _, z in spots) / len(spots)) if spots else None
        steps += power_tree(label, sorted(skipped), near)
    coverage = mt.call_tool("inspect_electricity_service_coverage", {"limit": 64})
    # those still beyond the grid: a building only short of supply waits for the plant (add_power)
    beyond = (shortfall("electricity").get("notCovered") or []) \
        if coverage["summary"].get("missingElectricity") else []
    island = None
    spots = [(item["position"]["x"], item["position"]["z"]) for item in beyond if item.get("position")]
    if spots and not any(step["ok"] for step in steps):
        # no line gets through to them (live: industry 1km from any plant, every line refused for the
        # buildings packed along it): a plant among them powers them as a grid of its own
        island = place_plant(f"island-{label}", (sum(x for x, _ in spots) / len(spots),
                                                 sum(z for _, z in spots) / len(spots)))
    return {"summary": coverage["summary"], "steps": steps, "diagnosis": diagnosis.get("verdict"),
            "unpowered": [b["id"] for b in coverage.get("buildings", [])][:20],
            "unreached": [int(item["id"]) for item in beyond], "island": island}


def power_tree(label: str, buildings: list[int], near: tuple[float, float] | None = None) -> list[dict[str, Any]]:
    """Buildings no single line reaches (a water intake by a far river): the line tree
    propose_minimal_power_grid plans from the working plant nearest them, each line built in order,
    planned again once for those still apart (a building just powered becomes a stepping stone)."""
    plants = [f for f in mt._request("GET", "/state/facilities", query={"service": "Electricity", "limit": 200})
              .get("facilities", []) if f.get("active") or f.get("productionRate")]
    if not plants:
        return []
    if near is not None:  # the first plant listed stood 1.9km off, across the town (live)
        plants.sort(key=lambda f: math.dist(near, (f["position"]["x"], f["position"]["z"])))
    steps: list[dict[str, Any]] = []
    for _ in range(2):
        # 64 buildings at a time ran over five minutes (live); a few a wake, at the planner's own
        # reach (half of it could not join water works 1-2km out)
        plan = mt.call_tool("propose_minimal_power_grid", {
            "sourceBuildingId": int(plants[0]["id"]), "buildingIds": buildings[:TREE_BUILDINGS]})
        lines = plan.get("segments") or []
        if not lines:
            break
        for line in lines:
            outcome = protocol.run(f"power-tree-{label}-{line.get('toBuildingId')}", "build_network", {
                "roadPrefab": line["powerLinePrefab"],
                "start": {"x": line["start"]["x"], "z": line["start"]["z"]},
                "end": {"x": line["end"]["x"], "z": line["end"]["z"]}}, 8)
            steps.append({"building": line.get("toBuildingId"), "tree": True, "ok": outcome["ok"],
                          "regressionOk": outcome["regressionOk"]})
            if not outcome["ok"]:
                return steps
        if plan.get("safe"):
            break
    return steps

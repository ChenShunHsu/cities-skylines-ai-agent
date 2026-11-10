"""A cargo train terminal by the industry, joined to a railway far from it through a tunnel (loop.cargo,
when no site beside the railway holds): beside the railway every lot was refused, and on that map the
railway ran 2.4 km from the industry, so a terminal there would leave the trucks crossing town anyway.

Site: the lots proposed for the terminal as for a service building (propose_service_sites) around the
industry's centre, its industrial buildings weighted (NoResources three times) within INDUSTRY_REACH of
the densest one; the first lot whose way out holds (way_out, from where its track will end) is used.

Track, from the end of the terminal's own track nearest the railway: LEAD metres straight on at the
ground, a ramp RAMP metres down to DEPTH, tunnel pieces of about PIECE metres to an approach point
RAMP metres from a plain node of the railway, and a ramp up into that node at APPROACH degrees to the
railway (a train joins a track at a real angle; straight on, the game would merge the pieces), in two
pieces: up to RISE_DEPTH halfway, then into the railway (live: one ramp joined to both the tunnel and
the railway collided at every depth of its foot, while its halves, each joined at both ends, held). Tunnel
pieces zigzag ZIGZAG metres either side of the straight way and go deeper (DEPTHS) where they would
hit something, as the metro's do (loop.metro), the last coming back to DEPTH for the ramp up. Every
piece is previewed before it is built; the Bridge passes a train track underground at both ends
under the buildings and power lines on the surface.

By the industry the lead and the ramp down run over its buildings (live: two factories under the
lead, five to nine buildings under the ramp, at every end and turn): the growables under them are
cleared, at most SURFACE_CLEARED of them and SURFACE_HOMES homes, each after its own bulldoze
preview, but a service building, a power line or a road crossed at grade (loop.cargo.crossings, on
the part of the ramp above SURFACE_SHARE of its length) rules the way out: a collision the growables
seemed to explain was a road, and six buildings were cleared for a lead that could not be laid.

The ramp up is chosen before anything is built: the plain railway nodes nearest the industry, each
approached from either side and either way along, until one ramp previews clean (live: the one
nearest the terminal ran into the town, five others were clear). The lead and the ramp down are
previewed from each end of the terminal's track, the ramp turned up to DOWN_TURNS off the way, once
the terminal stands; with none clean it is taken down.

What fails, or a terminal whose track does not reach the outside, is taken down (loop.cargo.undo).
"""

from __future__ import annotations

import math
from typing import Any

from loop import cargo, protocol, roads, services
from loop.city import mt

PREFAB = cargo.PREFAB
TRACK = cargo.TRACK
INDUSTRY_REACH = 600.0
OWN_REACH = 150.0  # the terminal's own track ends this near it
LEAD = 40.0
RAMP = 160.0  # 12 m over 160 m: a grade of 7.5%
DEPTH = -12.0
DEPTHS = (DEPTH, -20.0, -28.0)
PIECE = 80.0
ZIGZAG = 10.0
APPROACH = 30.0
RISE_DEPTH = DEPTH / 2
MIN_LENGTH = 600.0  # nearer than this the planner beside the railway serves
APPROACH_NODES = 6
DOWN_TURNS = (0.0, 20.0, -20.0)
MIN_BEND = 10.0  # the ramp down turns at least this off the lead (straight on, the game merges them)
MAX_BEND = 45.0  # and at most this (live: turned 120 degrees back, it ran into the lead and was refused)
SURFACE_CLEARED = 10
SURFACE_HOMES = 2
GROWABLE = ("Residential", "Commercial", "Industrial", "Office")
SURFACE_SHARE = 0.67  # of the ramp down at -12 over 160 m, the part above -8 m
SITE_RADII = (500, 1000)  # lots searched around the industry (live: none held by its centre, two 900 m out)
Point = tuple[float, float]


def industry_center(growables: list[dict[str, Any]]) -> Point | None:
    """The weighted centre of the industry around its densest building (NoResources thrice)."""
    weighted = [(cargo._xz(b["position"]), 3 if "NoResources" in str(b.get("problems")) else 1)
                for b in growables if b.get("service") == "Industrial" and b.get("position")]
    if not weighted:
        return None
    densest = max(weighted, key=lambda item: sum(w for p, w in weighted if math.dist(p, item[0]) <= INDUSTRY_REACH))
    near = [(p, w) for p, w in weighted if math.dist(p, densest[0]) <= INDUSTRY_REACH]
    total = sum(w for _, w in near)
    return (round(sum(p[0] * w for p, w in near) / total, 1), round(sum(p[1] * w for p, w in near) / total, 1))


def _rotate(v: Point, degrees: float) -> Point:
    r = math.radians(degrees)
    return v[0] * math.cos(r) - v[1] * math.sin(r), v[0] * math.sin(r) + v[1] * math.cos(r)


def approaches(net: dict[str, Any], main: list[dict[str, Any]], toward: Point) -> list[tuple[int, Point, Point]]:
    """(railway node, its place, an approach point RAMP metres out at APPROACH degrees) for the
    APPROACH_NODES plain nodes of the railway nearest toward, each side and way along, the approach
    points nearest toward first."""
    plain = {}
    for segment in main:
        a, b = cargo._xz(segment["start"]), cargo._xz(segment["end"])
        for node, here, other in ((int(segment["startNodeId"]), a, b), (int(segment["endNodeId"]), b, a)):
            if net["degree"].get(node) == 2:
                plain[node] = (here, cargo._unit(cargo._sub(other, here)))
    nearest = sorted(plain.items(), key=lambda item: math.dist(item[1][0], toward))[:APPROACH_NODES]
    found = []
    for node, (here, along) in nearest:
        for direction in (along, (-along[0], -along[1])):
            for turn in (APPROACH, -APPROACH):
                out = _rotate(direction, turn)
                found.append((node, here, (round(here[0] + out[0] * RAMP, 2), round(here[1] + out[1] * RAMP, 2))))
    return sorted(found, key=lambda item: math.dist(item[2], toward))


def approach(net: dict[str, Any], main: list[dict[str, Any]], toward: Point) -> tuple[int, Point, Point] | None:
    """The approach nearest toward whatever the ground (approaches)."""
    found = approaches(net, main, toward)
    return found[0] if found else None


def _bend(outward: Point, way: Point) -> float:
    """Signed degrees from outward to way."""
    return math.degrees(math.atan2(outward[0] * way[1] - outward[1] * way[0], cargo._dot(outward, way)))


def route(start: Point, outward: Point, end: Point, node: Point, turn: float = 0.0) -> list[tuple[Point, Point, str]]:
    """(from, to, kind) pieces: lead, ramp down (turned off the way by turn), tunnel pieces, ramp
    up into the railway node."""
    lead = (round(start[0] + outward[0] * LEAD, 2), round(start[1] + outward[1] * LEAD, 2))
    way = _rotate(cargo._unit(cargo._sub(end, lead)), turn)
    bend = _bend(outward, way)
    if abs(bend) < MIN_BEND:  # a ramp straight on from the lead would merge with it (live: refused)
        way = _rotate(outward, MIN_BEND if bend >= 0 else -MIN_BEND)
    down = (round(lead[0] + way[0] * RAMP, 2), round(lead[1] + way[1] * RAMP, 2))
    way = cargo._unit(cargo._sub(end, down))
    pieces = [(start, lead, "lead"), (lead, down, "down")]
    length = math.dist(down, end)
    count = max(1, math.ceil(length / PIECE))
    side = (-way[1], way[0])
    points = []
    for i in range(count + 1):
        x, z = down[0] + (end[0] - down[0]) * i / count, down[1] + (end[1] - down[1]) * i / count
        if 0 < i < count:
            offset = ZIGZAG if i % 2 else -ZIGZAG
            x, z = x + side[0] * offset, z + side[1] * offset
        points.append((round(x, 2), round(z, 2)))
    pieces += [(a, b, "tunnel") for a, b in zip(points, points[1:])]
    half = (round((end[0] + node[0]) / 2, 2), round((end[1] + node[1]) / 2, 2))
    return pieces + [(end, half, "rise"), (half, node, "up")]


def _preview(start: Point, end: Point, start_depth: float, end_depth: float) -> bool:
    return bool(_why(start, end, start_depth, end_depth) is None)


def _why(start: Point, end: Point, start_depth: float, end_depth: float) -> dict[str, Any] | None:
    """What refuses the piece, or None when it previews clean."""
    try:
        preview = mt._request("POST", "/commands/preview-network", body={
            "roadPrefab": TRACK, "start": cargo._point(start), "end": cargo._point(end),
            "startHeightOffset": start_depth, "endHeightOffset": end_depth})
    except (mt.BridgeRequestError, RuntimeError) as error:
        return {"error": str(error)[:160]}
    if preview.get("validIfUnlocked") or preview.get("valid"):
        return None
    return {key: preview.get(key) for key in ("toolErrors", "grade", "shortCollinearMergeRisk",
                                               "nativeMoveMiddleNodeRisk", "reusedStartNode")} | {
        "under": len(preview.get("buildingsUnderRoad") or []), "lines": len(preview.get("powerLinesCrossed") or [])}


def _surface(start: Point, end: Point, end_depth: float, segments: list[dict[str, Any]] = (),
             joined: set[int] = frozenset()) -> list[dict[str, Any]] | None:
    """The growables a surface piece runs over (cleared before it is built), or None when something
    else is in the way: a road or track crossed at grade, a service building, a power line or
    another tool error."""
    share = SURFACE_SHARE if end_depth < 0 else 1.0
    above = (start[0] + (end[0] - start[0]) * share, start[1] + (end[1] - start[1]) * share)
    if cargo.crossings(start, above, list(segments), joined):
        return None
    try:
        preview = mt._request("POST", "/commands/preview-network", body={
            "roadPrefab": TRACK, "start": cargo._point(start), "end": cargo._point(end),
            "startHeightOffset": 0.0, "endHeightOffset": end_depth})
    except (mt.BridgeRequestError, RuntimeError):
        return None
    under = preview.get("buildingsUnderRoad") or []
    if preview.get("powerLinesCrossed") or any(item.get("service") not in GROWABLE for item in under):
        return None
    if str(preview.get("toolErrors") or "None") not in ("None", "ObjectCollision") or \
            (preview.get("toolErrors") == "ObjectCollision" and not under):
        return None
    return under


def sites(center: Point) -> list[dict[str, Any]]:
    """Terminal lots proposed around the industry's centre, nearest first, then further out."""
    found: list[dict[str, Any]] = []
    for radius in SITE_RADII:
        found += mt.call_tool("propose_service_sites", {"buildingPrefab": PREFAB, "radius": radius, "limit": 3,
                                                        "near": {"x": center[0], "z": center[1]}}).get("sites") or []
    return found


def way_out(ends: list[tuple[Point, Point]], end: Point, rail_at: Point, standing: list[dict[str, Any]]) \
        -> tuple[list[tuple[Point, Point, str]], list[int]] | None:
    """The route from the first track end and turn whose lead and ramp down hold, and the growables
    to clear for them, or None: the end facing the railway first, and no ramp that turns more than
    MAX_BEND off the lead (the lead stands only once the terminal does, so its preview cannot tell)."""
    def facing(item: tuple[Point, Point]) -> float:
        return -cargo._dot(item[1], cargo._unit(cargo._sub(end, item[0])))

    for start, outward in sorted(ends, key=facing):
        own = {int(s[key]) for s in standing for key, side in (("startNodeId", "start"), ("endNodeId", "end"))
               if math.dist(cargo._xz(s[side]), start) <= cargo.NODE_MATCH}
        for turn in DOWN_TURNS:
            way = route(start, outward, end, rail_at, turn)
            down = cargo._unit(cargo._sub(way[1][1], way[1][0]))
            if abs(_bend(outward, down)) > MAX_BEND:
                continue
            lead = _surface(way[0][0], way[0][1], 0.0, standing, own)
            down = _surface(way[1][0], way[1][1], DEPTH, standing, own)
            if lead is None or down is None:
                continue
            found = {int(item["id"]): item for item in lead + down}
            homes = sum(1 for item in found.values() if item.get("service") == "Residential")
            if len(found) <= SURFACE_CLEARED and homes <= SURFACE_HOMES:
                return way, sorted(found)
    return None


def _ends(building: dict[str, Any], before: set[int]) -> list[tuple[Point, Point]]:
    """The terminal's own track ends with the direction straight out of them."""
    here = cargo._xz(building["position"])
    found = []
    for segment in cargo._rail_segments():
        if int(segment["id"]) in before:
            continue
        a, b = cargo._xz(segment["start"]), cargo._xz(segment["end"])
        for point, back in ((a, b), (b, a)):
            if math.dist(point, here) <= OWN_REACH:
                found.append((point, cargo._unit(cargo._sub(point, back))))
    return found


def build(label: str, growables: list[dict[str, Any]], segments: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The terminal by the industry and its tunnel to the railway, or None with nothing to try."""
    center = industry_center(growables)
    net = cargo.rail_network(segments)
    main = cargo.main_line(net)
    found = approaches(net, main, center) if center else []
    if not found or math.dist(center, found[0][1]) < MIN_LENGTH:
        return None
    target = next((item for item in found if _preview(item[2], item[1], DEPTH, 0.0)), None)
    if target is None:
        return {"ok": False, "stage": "site", "result": {"stage": "noRampUp", "tried": len(found)}}
    label = f"cargo-remote-{label}"
    before = {int(s["id"]) for s in cargo._rail_segments()}
    built: dict[str, Any] = {"links": [], "buildingId": None}

    def failed(stage: str, **detail: Any) -> dict[str, Any]:
        return {"ok": False, "stage": stage, **detail, "undo": cargo.undo(label, built, before)}

    node, rail_at, end = target
    standing = mt._request("GET", "/state/networks", query={"limit": 20000}).get("segments", [])
    chosen = None
    for site in sites(center):
        frame = cargo.Frame(cargo._xz(site["position"]), float(site["angleDegrees"]))
        a, b = frame.world(*cargo.TRACK_LOCAL[0]), frame.world(*cargo.TRACK_LOCAL[1])
        way = way_out([(a, cargo._unit(cargo._sub(a, b))), (b, cargo._unit(cargo._sub(b, a)))],
                      end, rail_at, standing)
        if way:
            chosen = site
            break
    if chosen is None:
        return {"ok": False, "stage": "site", "result": {"stage": "noWayOut"}}
    placed = services.place_site(f"{label}-terminal", PREFAB, chosen)
    if not placed.get("ok"):
        return {"ok": False, "stage": "site", "result": {k: placed.get(k) for k in ("stage", "error", "unsafe")}}
    built["buildingId"] = int(placed["buildingId"])
    ends = _ends(placed, before)
    if not ends:
        return failed("ownTrack")
    standing = mt._request("GET", "/state/networks", query={"limit": 20000}).get("segments", [])
    found_way = way_out(ends, end, rail_at, standing)
    if found_way is None:
        return failed("surface", ends=len(ends))
    pieces, under = found_way
    if under:
        clearing = roads.clear_buildings(f"{label}-clear", under)
        if not clearing["ok"]:
            return failed("clearing", blocked=clearing["blocked"], cleared=clearing["cleared"])
    start = pieces[0][0]
    checkpoint = protocol.checkpoint(f"{label}-track")
    depth = 0.0
    for index, (a, b, kind) in enumerate(pieces):
        last_tunnel = kind == "tunnel" and pieces[index + 1][2] == "rise"
        choices = {"lead": (0.0,), "down": (DEPTH,), "rise": (RISE_DEPTH,), "up": (0.0,)}.get(
            kind, (DEPTH,) if last_tunnel else DEPTHS)
        end_depth = next((d for d in choices if _preview(a, b, depth, d)), None)
        if end_depth is None:
            return failed("preview", piece=index, kind=kind, at=a, why=_why(a, b, depth, choices[0]), cleared=under)
        laid = before | set(built["links"])
        try:
            outcome = mt.call_tool("build_network", {
                "checkpointName": checkpoint, "roadPrefab": TRACK, "name": f"{label}-{index}",
                "start": cargo._point(a), "end": cargo._point(b),
                "startHeightOffset": depth, "endHeightOffset": end_depth})
        except (mt.BridgeRequestError, RuntimeError) as error:
            outcome = {"ok": False, "error": str(error)[:200]}
        built["links"] += sorted({int(s["id"]) for s in cargo._rail_segments()} - laid - set(built["links"]))
        if not outcome.get("ok"):
            return failed("build", piece=index, kind=kind, error=outcome.get("error"))
        depth = end_depth
    regression = protocol.verify(f"{label}-track")
    if regression.get("ok") is False:
        return failed("regression")
    own = [int(s[key]) for s in cargo._rail_segments() for key, end in (("startNodeId", "start"), ("endNodeId", "end"))
           if math.dist(cargo._xz(s[end]), start) <= cargo.NODE_MATCH]
    joined = cargo.connected(built["buildingId"], own)
    if not joined["ok"]:
        return failed("notConnected", joined=joined)
    return {"ok": True, "buildingId": built["buildingId"], "links": built["links"], "railNode": node,
            "pieces": len(pieces), "length": round(math.dist(center, rail_at)), "cleared": under}

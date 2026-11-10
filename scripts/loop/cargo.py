"""A cargo train terminal joined to the map's railway, so goods go by train instead of by truck
through the town's highway exit (live: at 28,000 people half the trips at the hotspots went in or out
of town over one highway link, cargo trucks among them, while industry kept reporting NoResources).

Trigger: NoResources on at least NO_RESOURCES_MIN buildings for PERSIST wakes running, or a traffic
hotspot seen for PERSIST wakes whose diagnosis offers the cargoRail remedy (trucks are most of it).
Train must be unlocked. The terminal is built once; a failed attempt is tried again (another site)
after RETRY_AFTER wakes, at most MAX_ATTEMPTS times within ATTEMPT_MEMORY wakes.

Site: the map's railway is the train track reaching an outside connection. Its segments are ranked
by the industry within INDUSTRY_REACH (NoResources buildings count three times), and the terminal is
laid beside one of them (sites), its back (where its own track runs, 24 m inside) toward the railway
and its front onto a local road, slid away from the railway until it meets the road. Each end of
its track (24 m beyond the lot) is joined to a railway node ahead of it by one or two straight rail
pieces that meet the railway at a real angle (link_routes); two links are preferred, one is enough
(a train reverses at a dead end).

Every piece is previewed before anything is built: the terminal's native placement (no snapping,
road access, nothing but growables in the way), its own track beyond the lot, and each link with the
Train Track prefab (the Bridge previews it even while it is locked: Train Track unlocks only once a
train station or cargo terminal stands). Nothing may cross a road, wall or other track at grade (a
level crossing would cut the road) or a power line, nor run over anything but growables; the
growables are capped (MAX_CLEARED, MAX_HOMES) and each is cleared after its own bulldoze preview.

Then: clear, place the terminal, check that Train Track unlocked, build the links from the
terminal's own track nodes, and check that the terminal's track reaches an outside connection and
the terminal reports no access problem. A step that fails removes everything built (links, the
terminal and any track it left). The result is judged JUDGE_AFTER wakes later by the NoResources
count and the truck share at the hotspots.

It needs a Bridge that previews a locked network, reports what a ground track runs over, applies a
building's create-pass milestones after creating it (the first cargo terminal passes the game's
"Train Track Requirements" milestone; raised mid-creation, its UI refresh threw on the Bridge's
thread and the rest of the creation never ran) and lays its overlap box the game's way round; on
an older one the plan answers bridgeOutdated and nothing is built.
"""

from __future__ import annotations

import json
import math
from typing import Any, Callable

from loop import protocol, roads
from loop.city import City, mt

PREFAB = "Cargo Center"  # the Cargo Train Terminal
UNDO_ROUNDS = 6
TRACK = "Train Track"
STATION_TRACK = "Train Cargo Track"
NO_RESOURCES_MIN = 8
PERSIST = 3
HISTORY = 24
INDUSTRY_REACH = 800.0
MAX_SEGMENTS = 8
HALF_WIDTH, HALF_LENGTH = 64.0, 32.0  # the lot (16 x 8 cells)
TRACK_LOCAL = ((-88.0, -24.0), (88.0, -24.0))  # its own track's ends in the lot's frame (live preview)
ROAD_REACH = 260.0  # how far from the railway a road may run for a lot to front onto it
MIN_OFFSET = 40.0  # lot centre from the railway: its back edge 8 m clear of the track
SHIFTS = (0.0, -24.0, 24.0, -48.0, 48.0)  # along the railway from the segment's middle
SLIDE_STEP = 2.0
ROAD_CLEAR = 3.0  # the lot's edge from any road's centre line (the game counts a centre line on the lot)
RAIL_CLEAR = 6.0  # and from any track's
ACCESS = 14.0  # a road's centre line this near the lot gives it road access (the game asks 20 m to a lane)
SITES_PER_SEGMENT = 4
MAX_PREVIEWS = 40  # terminal placement previews a plan may ask for
LINK_MIN_AHEAD = 30.0
LINK_REACH = 240.0
LINK_MAX_ANGLE = 35.0
MIN_TURN = 5.0
KNEE_TURNS = (15.0, 30.0, 45.0)
KNEE_LENGTHS = (32.0, 48.0, 64.0, 88.0)
KNEE_MAX = 45.0
# live previews: pieces meeting the railway at about 30 degrees or less were refused (ObjectCollision),
# at 42 accepted
MERGE_MIN, MERGE_MAX = 25.0, 75.0
MIN_PIECE = 24.0
LINK_TRIES = 6  # ways previewed per end
MAX_RAIL_PREVIEWS = 120  # rail piece previews a plan may ask for
MAX_CLEARED = 8
MAX_HOMES = 2
MAX_ATTEMPTS = 3
# wakes a failed attempt counts against MAX_ATTEMPTS and its site is passed over: the town around
# the way changes (live: three tries by the industry failed and none was made for 300 wakes, while
# NoResources stood at 12-21 buildings; tried again, the lot, lead and ramp down that had failed held)
ATTEMPT_MEMORY = 40
GONE = "Building was not found"
RETRY_AFTER = 10
JUDGE_AFTER = 8
COST = 60000.0  # display cash (live preview: constructionCost 6,000,000 in cents)
RESERVE = 20000.0
SNAP = 1.0  # metres the native placement may move the lot before the site is passed
# the game levels a lot, so its own track ends where the levelling leaves them, not where the plan
# previewed the links from: on a lot whose ground rises more than this, every link came out too
# steep once the terminal stood (live: three lots on a 10-12m slope, SlopeTooSteep each time, after
# their growables were cleared)
LEVEL_MAX = 3.0
NODE_MATCH = 6.0
NOT_CONNECTED = ("RoadNotConnected", "CannotBeReached")
NOT_RUN = "command was not run"
GRADE_SEPARATED = ("Elevated", "Bridge", "Tunnel")
DEFAULT_STATE = {"wake": 0, "history": [], "attempts": [], "built": None}

Point = tuple[float, float]


def _xz(point: dict[str, Any]) -> Point:
    return float(point["x"]), float(point["z"])


def _sub(a: Point, b: Point) -> Point:
    return a[0] - b[0], a[1] - b[1]


def _dot(a: Point, b: Point) -> float:
    return a[0] * b[0] + a[1] * b[1]


def _unit(v: Point) -> Point:
    length = math.hypot(*v)
    return (v[0] / length, v[1] / length) if length else (0.0, 0.0)


def _point(p: Point) -> dict[str, float]:
    return {"x": round(p[0], 2), "z": round(p[1], 2)}


def _is_line(segment: dict[str, Any]) -> bool:
    """A transport line's route is drawn over the networks, not a structure."""
    return str(segment.get("prefab", "")).endswith("Line")


def _polyline(segment: dict[str, Any]) -> list[Point]:
    points = [_xz(segment["start"])]
    if segment.get("middle"):
        points.append(_xz(segment["middle"]))
    return points + [_xz(segment["end"])]


def _cross(a: Point, b: Point, c: Point, d: Point) -> bool:
    """Whether segments ab and cd cross (touching counts)."""
    def orient(p: Point, q: Point, r: Point) -> float:
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    def within(p: Point, q: Point, r: Point) -> bool:
        return min(p[0], r[0]) - 1e-6 <= q[0] <= max(p[0], r[0]) + 1e-6 and \
            min(p[1], r[1]) - 1e-6 <= q[1] <= max(p[1], r[1]) + 1e-6
    o1, o2, o3, o4 = orient(a, b, c), orient(a, b, d), orient(c, d, a), orient(c, d, b)
    if (o1 > 0) != (o2 > 0) and (o3 > 0) != (o4 > 0) and o1 and o2 and o3 and o4:
        return True
    return any(abs(o) < 1e-9 and within(p, q, r) for o, p, q, r in
               ((o1, a, c, b), (o2, a, d, b), (o3, c, a, d), (o4, c, b, d)))


def at_grade(segment: dict[str, Any]) -> bool:
    """A network a ground track may not cross: a road, a wall, other surface track. Lines, pipes,
    ship and air paths are not in the way; power lines are the Bridge's check."""
    prefab = str(segment.get("prefab", ""))
    if _is_line(segment) or segment.get("isTunnel") or segment.get("isUnderground") \
            or any(word in prefab for word in GRADE_SEPARATED):
        return False
    service, sub = segment.get("service"), str(segment.get("subService", ""))
    return service in ("Road", "Beautification") or (
        service == "PublicTransport" and sub in ("PublicTransportTrain", "PublicTransportTram", "PublicTransportMetro"))


def crossings(a: Point, b: Point, segments: list[dict[str, Any]], joined: set[int] = frozenset()) -> list[int]:
    """At-grade networks the straight piece a-b would cross; segments at a node in `joined` (where the
    piece is meant to join the railway) do not count."""
    hits = []
    for segment in segments:
        if not at_grade(segment) or {int(segment["startNodeId"]), int(segment["endNodeId"])} & joined:
            continue
        line = _polyline(segment)
        if any(_cross(a, b, p, q) for p, q in zip(line, line[1:])):
            hits.append(int(segment["id"]))
    return hits


class Frame:
    """The lot's frame: world = centre + lx * axis_x + lz * axis_z (the game's building angle)."""

    def __init__(self, center: Point, angle_degrees: float):
        self.center, self.angle = center, angle_degrees
        radians = math.radians(angle_degrees)
        self.axis_x = (math.cos(radians), math.sin(radians))
        self.axis_z = (-math.sin(radians), math.cos(radians))

    def world(self, lx: float, lz: float) -> Point:
        return (self.center[0] + lx * self.axis_x[0] + lz * self.axis_z[0],
                self.center[1] + lx * self.axis_x[1] + lz * self.axis_z[1])

    def corners(self) -> list[Point]:
        return [self.world(sx * HALF_WIDTH, sz * HALF_LENGTH) for sx, sz in ((-1, -1), (1, -1), (1, 1), (-1, 1))]

    def inside(self, point: Point, margin: float = 0.0) -> bool:
        v = _sub(point, self.center)
        return abs(_dot(v, self.axis_x)) <= HALF_WIDTH + margin and abs(_dot(v, self.axis_z)) <= HALF_LENGTH + margin

    def local(self, point: Point) -> Point:
        v = _sub(point, self.center)
        return _dot(v, self.axis_x), _dot(v, self.axis_z)

    def distance(self, p: Point, q: Point) -> float:
        """From the straight piece p-q to the lot (0 when it touches or crosses it)."""
        a, b = self.local(p), self.local(q)
        corners = [(sx * HALF_WIDTH, sz * HALF_LENGTH) for sx, sz in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
        if any(abs(x) <= HALF_WIDTH and abs(z) <= HALF_LENGTH for x, z in (a, b)) or any(
                _cross(a, b, c, d) for c, d in zip(corners, corners[1:] + corners[:1])):
            return 0.0

        def to_box(point: Point) -> float:
            return math.hypot(max(abs(point[0]) - HALF_WIDTH, 0.0), max(abs(point[1]) - HALF_LENGTH, 0.0))

        def to_piece(point: Point) -> float:
            d = _sub(b, a)
            t = max(0.0, min(1.0, _dot(_sub(point, a), d) / (_dot(d, d) or 1.0)))
            return math.dist(point, (a[0] + d[0] * t, a[1] + d[1] * t))
        return min([to_box(a), to_box(b)] + [to_piece(c) for c in corners])


def lot_blocked(frame: Frame, segments: list[dict[str, Any]], buildings: list[dict[str, Any]]) -> list[Any]:
    """What a quick look finds on the lot before the game is asked: an at-grade network through it or
    a service building standing on it."""
    corners = frame.corners()
    edges = list(zip(corners, corners[1:] + corners[:1]))
    found: list[Any] = []
    for segment in segments:
        if not at_grade(segment):
            continue
        line = _polyline(segment)
        if any(frame.inside(p) for p in line) or any(_cross(a, b, p, q) for a, b in edges for p, q in zip(line, line[1:])):
            found.append(int(segment["id"]))
    for building in buildings:
        if "Untouchable" in str(building.get("flags")):
            continue
        if frame.inside(_xz(building["position"]), 8.0):
            found.append({"building": int(building["id"])})
    return found


def rail_network(segments: list[dict[str, Any]]) -> dict[str, Any]:
    """The train tracks, which nodes reach an outside connection, and where the nodes are."""
    rail = [s for s in segments if s.get("subService") == "PublicTransportTrain" and not _is_line(s)]
    adjacent: dict[int, set[int]] = {}
    positions: dict[int, Point] = {}
    outside: set[int] = set()
    for segment in rail:
        a, b = int(segment["startNodeId"]), int(segment["endNodeId"])
        adjacent.setdefault(a, set()).add(b)
        adjacent.setdefault(b, set()).add(a)
        positions[a], positions[b] = _xz(segment["start"]), _xz(segment["end"])
        if segment.get("startOutside"):
            outside.add(a)
        if segment.get("endOutside"):
            outside.add(b)
        if "Connection" in str(segment.get("prefab")):
            outside.update((a, b))
    linked: set[int] = set()
    stack = list(outside)
    while stack:
        node = stack.pop()
        if node in linked:
            continue
        linked.add(node)
        stack.extend(adjacent.get(node, ()))
    return {"segments": rail, "linked": linked, "positions": positions, "outside": outside,
            "degree": {node: len(others) for node, others in adjacent.items()}}


def main_line(net: dict[str, Any]) -> list[dict[str, Any]]:
    """Ground Train Track segments of the railway that reaches the outside."""
    return [s for s in net["segments"] if s.get("prefab") == TRACK and int(s["startNodeId"]) in net["linked"]
            and int(s["endNodeId"]) in net["linked"] and at_grade(s)]


def rank_segments(main: list[dict[str, Any]], growables: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Railway segments by the industry within INDUSTRY_REACH of their middle, NoResources thrice."""
    industry = [(_xz(b["position"]), 3 if "NoResources" in str(b.get("problems")) else 1)
                for b in growables if b.get("service") == "Industrial"]
    scored = []
    for segment in main:
        middle = _xz(segment.get("middle") or segment["start"])
        score = sum(weight for place, weight in industry if math.dist(place, middle) <= INDUSTRY_REACH)
        if score:
            scored.append((-score, int(segment["id"]), segment))
    return [{**segment, "industryScore": -score} for score, _, segment in sorted(scored, key=lambda i: i[:2])][:MAX_SEGMENTS]


def angle_facing(front: Point) -> float:
    """The building angle whose local z axis (the lot's front, its road side) points along front."""
    return round(math.degrees(math.atan2(-front[0], front[1])) % 360.0, 3)


def first_road(origin: Point, direction: Point, segments: list[dict[str, Any]]) -> tuple[float, Point, Point] | None:
    """The nearest local road piece a ray from origin meets within ROAD_REACH: (distance, piece)."""
    tip = (origin[0] + direction[0] * ROAD_REACH, origin[1] + direction[1] * ROAD_REACH)
    best = None
    for segment in segments:
        if segment.get("service") != "Road" or "Highway" in str(segment.get("prefab")) or not at_grade(segment):
            continue
        line = _polyline(segment)
        for p, q in zip(line, line[1:]):
            if not _cross(origin, tip, p, q):
                continue
            r = _sub(q, p)
            denominator = direction[0] * r[1] - direction[1] * r[0]
            if abs(denominator) < 1e-9:
                continue
            w = _sub(p, origin)
            distance = (w[0] * r[1] - w[1] * r[0]) / denominator
            if best is None or distance < best[0]:
                best = (distance, p, q)
    return best


def sites(segment: dict[str, Any], segments: list[dict[str, Any]] = (),
          growables: list[dict[str, Any]] = ()) -> list[dict[str, Any]]:
    """Terminal lots beside the segment, their back (the terminal's own track, local z -24) toward
    the railway and their front toward a local road the game can reach them from (within ACCESS of
    the lot): the lot is laid parallel to the railway, or to the first road across from it, at
    SHIFTS along the railway, and slid away from the railway from MIN_OFFSET until it would touch a
    road; it is kept when it then stands RAIL_CLEAR off every track, clear of every road and with a
    road within ACCESS. Live, every lot at fixed offsets lacked road access, and lots fitted to the
    road where the ray met it touched the road where it bent. Best first: fewest homes, then fewest
    buildings under the lot (both estimated here; the native preview decides), at most
    SITES_PER_SEGMENT."""
    a, b = _xz(segment["start"]), _xz(segment["end"])
    u = _unit(_sub(b, a))
    middle = _xz(segment.get("middle") or {"x": (a[0] + b[0]) / 2, "z": (a[1] + b[1]) / 2})
    near = [s for s in segments if at_grade(s) and min(math.dist(middle, p) for p in _polyline(s)) <= ROAD_REACH + 200]
    rails = [piece for s in near if s.get("service") == "PublicTransport" for piece in zip(_polyline(s), _polyline(s)[1:])]
    others = [piece for s in near if s.get("service") != "PublicTransport" for piece in zip(_polyline(s), _polyline(s)[1:])]
    roads = [piece for s in near if s.get("service") == "Road" and "Highway" not in str(s.get("prefab"))
             for piece in zip(_polyline(s), _polyline(s)[1:])]
    homes = [_xz(g["position"]) for g in growables if g.get("service") == "Residential"]
    others_built = [_xz(g["position"]) for g in growables if g.get("service") != "Residential"]
    found = []
    for side in (1, -1):
        away = (-u[1] * side, u[0] * side)
        fronts = [("railParallel", away)]
        road = first_road(middle, away, near)
        if road is not None:
            r = _unit(_sub(road[2], road[1]))
            normal = (-r[1], r[0]) if _dot((-r[1], r[0]), away) > 0 else (r[1], -r[0])
            if 1.0 < _angle(normal, away) <= LINK_MAX_ANGLE:  # a road parallel to the railway adds nothing
                fronts.append(("roadParallel", normal))
        for fit, front in fronts:
            angle = angle_facing(front)
            for shift in SHIFTS:
                base = (middle[0] + u[0] * shift, middle[1] + u[1] * shift)
                kept = None
                offset = MIN_OFFSET
                while offset <= ROAD_REACH:
                    frame = Frame((base[0] + away[0] * offset, base[1] + away[1] * offset), angle)
                    if any(frame.distance(p, q) < ROAD_CLEAR for p, q in others):
                        break  # touching a road (or a wall): slid as far as it goes
                    if min((frame.distance(p, q) for p, q in rails), default=1e9) >= RAIL_CLEAR:
                        kept = (offset, frame)
                    offset += SLIDE_STEP
                if kept is None:
                    continue
                offset, frame = kept
                if min((frame.distance(p, q) for p, q in roads), default=1e9) > ACCESS:
                    continue
                found.append({"segmentId": int(segment["id"]), "side": side, "offset": round(offset, 1), "fit": fit,
                              "shift": shift, "center": frame.center, "angleDegrees": angle, "away": away,
                              "estimate": (sum(frame.inside(p, 4.0) for p in homes),
                                           sum(frame.inside(p, 4.0) for p in others_built))})
    found.sort(key=lambda site: (site["estimate"], abs(site["shift"]), site["offset"]))
    return found[:SITES_PER_SEGMENT]


def _angle(a: Point, b: Point) -> float:
    """Degrees between two directions."""
    return math.degrees(math.atan2(abs(a[0] * b[1] - a[1] * b[0]), _dot(a, b)))


def merge_angle(direction: Point, node: int, net: dict[str, Any], main: list[dict[str, Any]]) -> float:
    """How steeply a piece arriving along direction meets the railway at node: the smallest angle
    to the line of any railway segment there (0 runs along it)."""
    angles = [min(_angle(direction, chord), 180.0 - _angle(direction, chord))
              for s in main if node in (int(s["startNodeId"]), int(s["endNodeId"]))
              for chord in [_sub(_xz(s["end"]), _xz(s["start"]))]]
    return min(angles, default=0.0)


def link_routes(end: Point, outward: Point, toward: Point, net: dict[str, Any],
                main: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ways from the terminal track's end to a railway node ahead of it, best first: one straight
    piece, or two with a knee turned KNEE_TURNS toward the railway KNEE_LENGTHS out. The last piece
    must meet the railway between MERGE_MIN and MERGE_MAX degrees: live, every straight link that
    slid into the railway at a few degrees ran alongside it and the game refused it (ObjectCollision).
    No piece turns more than KNEE_MAX off the one before, and the first leaves the terminal track
    between MIN_TURN (a straight continuation lets the game merge the two) and LINK_MAX_ANGLE.
    Outside-connection nodes are left alone."""
    across = _unit((toward[0] - outward[0] * _dot(toward, outward), toward[1] - outward[1] * _dot(toward, outward)))
    nodes = {int(s[key]) for s in main for key in ("startNodeId", "endNodeId")} - net["outside"]
    routes = []
    for node in nodes:
        target = net["positions"][node]
        v = _sub(target, end)
        if _dot(v, outward) < LINK_MIN_AHEAD or math.hypot(*v) > LINK_REACH or _dot(v, toward) < 0:
            continue
        if MIN_TURN <= _angle(outward, v) <= LINK_MAX_ANGLE and \
                MERGE_MIN <= merge_angle(v, node, net, main) <= MERGE_MAX:
            routes.append({"points": [end, target], "nodeId": node, "length": math.hypot(*v)})
        for turn in KNEE_TURNS:
            radians = math.radians(turn)
            first = (outward[0] * math.cos(radians) + across[0] * math.sin(radians),
                     outward[1] * math.cos(radians) + across[1] * math.sin(radians))
            for reach in KNEE_LENGTHS:
                knee = (end[0] + first[0] * reach, end[1] + first[1] * reach)
                w = _sub(target, knee)
                if math.hypot(*w) < MIN_PIECE or _dot(w, first) <= 0 or _angle(first, w) > KNEE_MAX:
                    continue
                if not MERGE_MIN <= merge_angle(w, node, net, main) <= MERGE_MAX:
                    continue
                routes.append({"points": [end, knee, target], "nodeId": node, "length": reach + math.hypot(*w)})
    routes.sort(key=lambda route: (len(route["points"]), route["length"]))
    return routes[:LINK_TRIES]


def judge_placement(preview: dict[str, Any], site: dict[str, Any], growables: dict[int, dict[str, Any]]) -> dict[str, Any]:
    """The native placement preview of a lot: refused (with why) or its own track ends and the
    growables it would clear."""
    if "createPassMilestones" not in preview:
        return {"ok": False, "why": "bridgeOutdated"}
    placement = preview.get("gamePlacement") or {}
    moved = math.dist(_xz(placement.get("position") or {"x": 1e9, "z": 1e9}), site["center"])
    turned = abs((float(placement.get("angleDegrees", 1e9)) - site["angleDegrees"] + 180.0) % 360.0 - 180.0)
    # a terminal is loud: homes within its noise radius are noted, not refused (the strip by the
    # railway is mixed, and every lot there had some)
    risks = set((preview.get("safety") or {}).get("risks") or []) - {"buildingOverlap", "residentialInsideNoiseRadius"}
    overlaps = [int(item["id"]) for item in preview.get("buildingOverlaps") or []]
    if placement.get("errors") != "None":
        return {"ok": False, "why": f"native:{placement.get('errors')}"}
    if moved > SNAP or turned > 1.0:
        return {"ok": False, "why": "snapped"}  # the game would set it elsewhere than planned
    if risks or preview.get("roadOverlaps"):
        return {"ok": False, "why": sorted(risks) or "roadOverlap"}
    if not (preview.get("roadAccess") or {}).get("accessible"):
        return {"ok": False, "why": "roadAccess"}
    if float((preview.get("terrain") or {}).get("heightRange") or 0.0) > LEVEL_MAX:
        return {"ok": False, "why": "notLevel"}
    blocking = [item for item in overlaps if item not in growables]
    if blocking:
        return {"ok": False, "why": "blocked", "blocking": blocking}
    path = next((p for p in preview.get("networkPaths") or [] if "Train" in str(p.get("prefab"))
                 and len(p.get("nodes") or []) >= 2), None)
    if path is None:
        return {"ok": False, "why": "noTrack"}
    noisy = "residentialInsideNoiseRadius" in ((preview.get("safety") or {}).get("risks") or [])
    return {"ok": True, "ends": [_xz(path["nodes"][0]), _xz(path["nodes"][-1])], "clear": overlaps, "noisy": noisy}


def judge_rail(preview: dict[str, Any], growables: dict[int, dict[str, Any]], *, own_track: bool = False,
               joins: bool = True) -> dict[str, Any]:
    """A rail piece's preview: the growables under it, or why it is refused. The terminal's own
    track is only checked for what it runs over (it is laid by the game with the terminal); a link's
    last piece must end on the railway node it was aimed at, a knee on nothing."""
    if "groundRail" not in preview:
        return {"ok": False, "why": "bridgeOutdated"}
    under = [int(item["id"]) for item in preview.get("buildingsUnderRoad") or []]
    if preview.get("powerLinesCrossed"):
        return {"ok": False, "why": "powerLine"}
    blocking = [item for item in under if item not in growables]
    if blocking:
        return {"ok": False, "why": "blocked", "blocking": blocking}
    if not own_track:
        if not preview.get("validIfUnlocked", preview.get("valid")):
            return {"ok": False, "why": f"native:{preview.get('toolErrors')}"}
        if bool(preview.get("reusedEndNode")) != joins:
            return {"ok": False, "why": "notJoined" if joins else "kneeOnNode"}
    return {"ok": True, "clear": under}


def _preview_rail(prefab: str, start: Point, end: Point) -> dict[str, Any]:
    # a rail piece reads like a road piece to the Bridge: its preview reports what it runs over
    return mt.call_tool("preview_network", {"roadPrefab": prefab, "start": _point(start), "end": _point(end)})


def _preview_lot(site: dict[str, Any]) -> dict[str, Any]:
    return mt.call_tool("preview_building_candidate", {
        "buildingPrefab": PREFAB, "position": _point(site["center"]), "angleDegrees": site["angleDegrees"],
        "requireRoadAccess": True})


def plan(segments: list[dict[str, Any]], growables: list[dict[str, Any]], facilities: list[dict[str, Any]],
         tried: set[str] = frozenset(), preview_lot: Callable[[dict[str, Any]], dict[str, Any]] = None,
         preview_rail: Callable[[str, Point, Point], dict[str, Any]] = None) -> dict[str, Any]:
    """The best site: both ends linked if any site has them, else one; the fewest cleared."""
    preview_lot = preview_lot or _preview_lot
    preview_rail = preview_rail or _preview_rail
    net = rail_network(segments)
    main = main_line(net)
    if not main:
        return {"ok": False, "why": "noRailway"}
    by_id = {int(b["id"]): b for b in growables}
    best: dict[str, Any] | None = None
    previews, refused = 0, {}
    rail_previews = [0]

    def note(why: Any) -> None:
        key = str(why if not isinstance(why, list) else why[0])
        refused[key] = refused.get(key, 0) + 1

    for segment in rank_segments(main, growables):
        for site in sites(segment, segments, growables):
            key = f"{segment['id']}:{site['side']}:{site['fit']}:{int(site['shift'])}"
            if key in tried:
                continue
            frame = Frame(site["center"], site["angleDegrees"])
            ends = [frame.world(*local) for local in TRACK_LOCAL]
            if lot_blocked(frame, segments, facilities) or crossings(ends[0], ends[1], segments):
                note("lotBlocked")
                continue
            if previews >= MAX_PREVIEWS:
                return best or {"ok": False, "why": "previewBudget", "refused": refused}
            previews += 1
            try:
                lot = judge_placement(preview_lot(site), site, by_id)
            except mt.BridgeRequestError as error:
                if NOT_RUN in str(error):
                    return {"ok": None, "why": "busy"}
                note("previewError")
                continue
            if not lot["ok"]:
                if lot["why"] == "bridgeOutdated":
                    return {"ok": None, "why": "bridgeOutdated"}
                note(lot["why"])
                continue
            try:
                own = judge_rail(preview_rail(STATION_TRACK, lot["ends"][0], lot["ends"][1]), by_id, own_track=True)
            except mt.BridgeRequestError as error:
                if NOT_RUN in str(error):
                    return {"ok": None, "why": "busy"}
                own = {"ok": False, "why": "previewError"}
            if not own["ok"]:
                if own["why"] == "bridgeOutdated":
                    return {"ok": None, "why": "bridgeOutdated"}
                note(f"ownTrack:{own['why']}")
                continue
            clear = set(lot["clear"]) | set(own["clear"])
            toward = (-site["away"][0], -site["away"][1])
            links = []
            for index, end in enumerate(lot["ends"]):
                # along the lot's own track, away from the lot
                outward = frame.axis_x if _dot(_sub(end, site["center"]), frame.axis_x) > 0 else \
                    (-frame.axis_x[0], -frame.axis_x[1])
                for route in link_routes(end, outward, toward, net, main):
                    pieces = list(zip(route["points"], route["points"][1:]))
                    if any(crossings(p, q, segments, {route["nodeId"]} if i == len(pieces) - 1 else set())
                           for i, (p, q) in enumerate(pieces)):
                        continue
                    under: set[int] = set()
                    for i, (p, q) in enumerate(pieces):
                        if rail_previews[0] >= MAX_RAIL_PREVIEWS:
                            return best or {"ok": False, "why": "previewBudget", "refused": refused}
                        rail_previews[0] += 1
                        try:
                            piece = judge_rail(preview_rail(TRACK, p, q), by_id, joins=i == len(pieces) - 1)
                        except mt.BridgeRequestError as error:
                            if NOT_RUN in str(error):
                                return {"ok": None, "why": "busy"}
                            piece = {"ok": False, "why": "previewError"}
                        if not piece["ok"]:
                            if piece["why"] == "bridgeOutdated":
                                return {"ok": None, "why": "bridgeOutdated"}
                            note(f"link:{piece['why']}")
                            break
                        under |= set(piece["clear"])
                    else:
                        links.append({"end": index, "points": route["points"], "nodeId": route["nodeId"]})
                        clear |= under
                        break
            homes = sum(1 for item in clear if by_id[item].get("service") == "Residential")
            if not links:
                note("noLink")
                continue
            if len(clear) > MAX_CLEARED or homes > MAX_HOMES:
                note("wouldClear")
                continue
            found = {"ok": True, "key": key, "segmentId": site["segmentId"], "center": site["center"],
                     "angleDegrees": site["angleDegrees"], "offset": site["offset"], "side": site["side"],
                     "away": site["away"],
                     "industryScore": segment["industryScore"], "ends": lot["ends"], "links": links,
                     "clear": sorted(clear), "homes": homes, "noisy": lot["noisy"], "fit": site["fit"],
                     "previews": previews, "railPreviews": rail_previews[0]}
            if len(links) == 2 and not lot["noisy"]:
                return found
            if best is None or rank(found) < rank(best):
                best = found
    return best or {"ok": False, "why": "noSite", "previews": previews, "refused": refused}


def rank(site: dict[str, Any]) -> tuple:
    """Both ends linked first, then the fewest homes and buildings cleared, then away from homes."""
    return -len(site["links"]), site["homes"], len(site["clear"]), site["noisy"]


def _rail_segments() -> list[dict[str, Any]]:
    return [s for s in mt._request("GET", "/state/networks", query={"limit": 20000}).get("segments", [])
            if s.get("subService") == "PublicTransportTrain" and not _is_line(s)]


def _track_unlocked() -> bool:
    networks = mt._request("GET", "/prefabs/networks", query={"service": "PublicTransport"}).get("networks", [])
    return any(item.get("name") == TRACK and item.get("unlocked") for item in networks)


def own_nodes(before: set[int], after: list[dict[str, Any]], ends: list[Point]) -> list[int | None]:
    """The terminal's own track nodes nearest each planned end, among the tracks it brought."""
    places = {}
    for segment in after:
        if int(segment["id"]) in before:
            continue
        places[int(segment["startNodeId"])] = _xz(segment["start"])
        places[int(segment["endNodeId"])] = _xz(segment["end"])
    found = []
    for end in ends:
        near = min(places.items(), key=lambda item: math.dist(item[1], end), default=None)
        found.append(near[0] if near and math.dist(near[1], end) <= NODE_MATCH else None)
    return found


def connected(building_id: int, nodes: list[int]) -> dict[str, Any]:
    """Whether the terminal's track reaches an outside connection and the terminal has no access problem."""
    net = rail_network(mt._request("GET", "/state/networks", query={"limit": 20000}).get("segments", []))
    # transport buildings are listed only under their service (live: the unfiltered list, city
    # services only, never held the terminal, and a terminal joined at both ends was taken down)
    facility = next((item for item in mt._request("GET", "/state/facilities",
                                                  query={"service": "PublicTransport", "limit": 5000})
                     .get("facilities", []) if int(item["id"]) == building_id), None)
    problems = str((facility or {}).get("problems") or "")
    reached = [node for node in nodes if node in net["linked"]]
    return {"ok": bool(facility) and bool(reached) and not any(p in problems for p in NOT_CONNECTED),
            "reachesOutside": reached, "problems": problems or None, "standing": bool(facility)}


def _failed_run(outcome: dict[str, Any]) -> bool:
    return not outcome.get("ok") or outcome.get("regressionOk") is False


def undo(label: str, built: dict[str, Any], before: set[int]) -> dict[str, Any]:
    """Remove the links, the terminal, and any track it left behind (newest first)."""
    removed: list[Any] = []
    for segment in reversed(built["links"]):
        if protocol.run(f"{label}-undo-{segment}", "bulldoze_entity", {"entityType": "netSegment", "id": segment})["ok"]:
            removed.append(segment)
    if built.get("buildingId"):
        if protocol.run(f"{label}-undo-terminal", "bulldoze_entity",
                        {"entityType": "building", "id": built["buildingId"]})["ok"]:
            removed.append({"building": built["buildingId"]})
    # a piece inside a run of tunnel is refused while the pieces at its ends stand (live: six of
    # fifteen came down only once their neighbours were gone), so the rest is tried in rounds
    for _ in range(UNDO_ROUNDS):
        left = [int(s["id"]) for s in _rail_segments() if int(s["id"]) not in before]
        done = [segment for segment in left if protocol.run(
            f"{label}-undo-{segment}", "bulldoze_entity", {"entityType": "netSegment", "id": segment})["ok"]]
        removed += done
        if not done:
            break
    remaining = [int(s["id"]) for s in _rail_segments() if int(s["id"]) not in before]
    return {"removed": removed, "removedAll": not remaining and (
        not built.get("buildingId") or {"building": built["buildingId"]} in removed), "remaining": remaining}


def build_routes(site: dict[str, Any], link: dict[str, Any], node: int, start: Point,
                 segments: list[dict[str, Any]]) -> list[list[Point]]:
    """Ways to lay a link from the node the terminal's track actually ends at: the planned one
    first, then link_routes from that node, each clear of every at-grade network (the terminal's own
    track joins at the start, the railway at the end)."""
    routes = [[start] + [tuple(point) for point in link["points"][1:]]]
    if not site.get("away"):
        return routes
    net = rail_network(segments)
    frame = Frame(tuple(site["center"]), site["angleDegrees"])
    end = tuple(site["ends"][link["end"]])
    outward = frame.axis_x if _dot(_sub(end, tuple(site["center"])), frame.axis_x) > 0 else \
        (-frame.axis_x[0], -frame.axis_x[1])
    toward = (-site["away"][0], -site["away"][1])
    for route in link_routes(start, outward, toward, net, main_line(net)):
        pieces = list(zip(route["points"], route["points"][1:]))
        if any(crossings(p, q, segments, ({node} if i == 0 else set())
                         | ({route["nodeId"]} if i == len(pieces) - 1 else set()))
               for i, (p, q) in enumerate(pieces)):
            continue
        if route["points"] != routes[0]:
            routes.append(route["points"])
    return routes[:LINK_TRIES]


def _refusal(preview: dict[str, Any], first: bool, last: bool) -> str | None:
    """Why the game would not lay this piece now (Train Track is unlocked: the preview is the real one)."""
    check = judge_rail(preview, {}, joins=last)
    if not check["ok"]:
        return check["why"]
    if not preview.get("valid"):
        return f"native:{preview.get('toolErrors')}"
    if first and not preview.get("reusedStartNode"):
        return "notFromTerminal"
    return None


def lay_link(label: str, routes: list[list[Point]], built: dict[str, Any]) -> dict[str, Any]:
    """The first route whose every piece previews valid, laid piece by piece (each piece previewed
    again from the node just laid). A route refused part way is taken up again before the next is
    tried; a piece the game would not build, or one that would not come up, is a hard failure."""
    tried = []
    for way, points in enumerate(routes):
        ahead = points[1:]
        here, why = points[0], None
        for piece, point in enumerate(ahead):
            why = _refusal(_preview_rail(TRACK, here, point), piece == 0, piece == len(ahead) - 1)
            if why:
                tried.append({"way": way, "piece": piece, "why": why})
                break
            here = point
        if why:
            continue
        laid: list[int] = []
        here = points[0]
        for piece, point in enumerate(ahead):
            why = _refusal(_preview_rail(TRACK, here, point), piece == 0, piece == len(ahead) - 1)
            if why:
                tried.append({"way": way, "piece": piece, "why": why, "laid": len(laid)})
                break
            name = f"{label}-{way}-{piece}"
            outcome = protocol.run(name, "build_network", {"roadPrefab": TRACK, "start": _point(here),
                                                           "end": _point(point), "name": name})
            segment = (outcome.get("result") or {}).get("segmentId")
            if segment:
                laid.append(int(segment))
            if _failed_run(outcome) or not segment:
                built["links"].extend(laid)
                return {"ok": False, "hard": True, "way": way, "piece": piece,
                        "error": str(outcome.get("error"))[:200], "tried": tried}
            details = mt._request("GET", "/state/network-segment-details", query={"id": segment})
            here = _xz(details.get("end") or details.get("endPosition") or _point(point))
        else:
            built["links"].extend(laid)
            return {"ok": True, "way": way, "tried": tried}
        for segment in reversed(laid):  # a knee leading nowhere comes up before the next way
            if not protocol.run(f"{label}-takeup-{segment}", "bulldoze_entity",
                                {"entityType": "netSegment", "id": segment})["ok"]:
                built["links"].extend(laid)
                return {"ok": False, "hard": True, "way": way, "why": "notTakenUp", "tried": tried}
    return {"ok": False, "tried": tried}


def build(label: str, site: dict[str, Any]) -> dict[str, Any]:
    """Clear, place the terminal, join it to the railway and check it; undo it all on a failure."""
    label = f"cargo-{label}"
    built: dict[str, Any] = {"buildingId": None, "links": [], "cleared": []}
    before = {int(s["id"]) for s in _rail_segments()}

    def failed(stage: str, **detail: Any) -> dict[str, Any]:
        try:
            anything = built["buildingId"] or built["links"] or _rail_segments_added(before)
            removal = {"undo": undo(label, built, before)} if anything else {}
        except (mt.BridgeRequestError, RuntimeError) as error:
            # left half built: the loop stops for a look (play.stop_reasons, cargoRailNotUndone)
            removal = {"undo": {"removedAll": False, "error": str(error)[:200], "links": built["links"],
                                "buildingId": built["buildingId"]}}
        return {"ok": False, "stage": stage, **detail, "cleared": built["cleared"], **removal}

    try:
        if site["clear"]:
            clearing = roads.clear_buildings(label, site["clear"])
            built["cleared"] = clearing["cleared"]
            if not clearing["ok"]:
                return failed("clearing", blocked=clearing.get("blocked"))
        lot = _preview_lot(site)
        if not lot.get("valid"):
            return failed("placementPreview", risks=(lot.get("safety") or {}).get("risks"))
        outcome = protocol.run(label, "place_service", {"buildingPrefab": PREFAB, "position": _point(site["center"]),
                                                        "angleDegrees": site["angleDegrees"]})
        result = outcome.get("result") or {}
        built["buildingId"] = result.get("buildingId") if isinstance(result, dict) else None
        if _failed_run(outcome) or not built["buildingId"]:
            stage = "busy" if NOT_RUN in str(outcome.get("error")) and not built["buildingId"] else "place"
            return failed(stage, error=str(outcome.get("error"))[:200])
        if not _track_unlocked():
            return failed("trackLocked", milestones=result.get("createPassMilestonesPassed"))
        nodes = own_nodes(before, _rail_segments(), site["ends"])
        if not any(nodes):
            return failed("noTerminalTrack")
        segments = mt._request("GET", "/state/networks", query={"limit": 20000}).get("segments", [])
        positions = rail_network(segments)["positions"]
        # the lot is levelled under the terminal, so its track ends need not stand where the plan
        # previewed them from: each link is previewed again from its node and, refused, laid another
        # way; one end joined is enough, as the plan accepts (live: SlopeTooSteep from the levelled lot)
        links: list[dict[str, Any]] = []
        for index, link in enumerate(site["links"]):
            node = nodes[link["end"]]
            if node is None:
                links.append({"link": index, "ok": False, "why": "noTerminalTrack"})
                continue
            laid = lay_link(f"{label}-link{index}", build_routes(site, link, node, positions[node], segments), built)
            if laid.get("hard"):
                return failed("link", link=index, **{k: v for k, v in laid.items() if k not in ("ok", "hard")})
            links.append({"link": index, **laid})
        if not any(item["ok"] for item in links):
            return failed("linkPreview", links=links)
        check = connected(built["buildingId"], [node for node in nodes if node])
        if not check["ok"]:
            return failed("notConnected", check=check)
        return {"ok": True, "buildingId": built["buildingId"], "links": built["links"], "cleared": built["cleared"],
                "nodes": nodes, "check": check, "linked": links}
    except (mt.BridgeRequestError, RuntimeError) as error:
        return failed("busy" if NOT_RUN in str(error) and not built["buildingId"] else "error", error=str(error)[:200])


def _rail_segments_added(before: set[int]) -> bool:
    return any(int(s["id"]) not in before for s in _rail_segments())


def evidence(state: dict[str, Any], wake: dict[str, Any], traffic: dict[str, Any]) -> dict[str, Any]:
    count = int((wake.get("problemCounts") or {}).get("NoResources") or 0)
    state["history"] = (state["history"] + [count])[-HISTORY:]
    recent = state["history"][-PERSIST:]
    hotspots = [item for item in traffic.get("cargo") or [] if item.get("cargoRail") and (item.get("seen") or 0) >= PERSIST]
    return {"noResources": count, "persisting": len(recent) == PERSIST and min(recent) >= NO_RESOURCES_MIN,
            "cargoHotspots": [item["key"] for item in hotspots],
            "truckShare": max((float(item.get("cargoShare") or 0) for item in traffic.get("cargo") or []), default=None)}


def judge(state: dict[str, Any], seen: dict[str, Any]) -> dict[str, Any] | None:
    """After JUDGE_AFTER wakes: worked when NoResources fell by a third or by three buildings."""
    built = state["built"]
    if built.get("verdict") or state["wake"] - built["wake"] < JUDGE_AFTER:
        return None
    now = sum(state["history"][-PERSIST:]) / max(1, len(state["history"][-PERSIST:]))
    baseline = float(built.get("baseline") or 0)
    worked = now <= baseline * (2 / 3) or now <= baseline - 3
    built["verdict"] = "worked" if worked else "failed"
    built["judged"] = {"wake": state["wake"], "noResources": round(now, 1), "baseline": baseline,
                       "truckShareBefore": built.get("truckShare"), "truckShareNow": seen.get("truckShare")}
    return {"verdict": built["verdict"], **built["judged"]}


def standing(building_id: int) -> bool | None:
    """Whether the building still stands (the bulldoze preview finds it); None when it cannot be read."""
    try:
        mt.call_tool("preview_bulldoze_impact", {"entityType": "building", "id": building_id})
    except mt.BridgeRequestError as error:
        return False if GONE in str(error) else None
    except RuntimeError:
        return None
    return True


def step(city: City, label: str, wake: dict[str, Any], traffic: dict[str, Any]) -> dict[str, Any]:
    state = {**json.loads(json.dumps(DEFAULT_STATE)), **city.load_state("cargo-rail", {})}  # fresh lists
    state["wake"] += 1
    seen = evidence(state, wake, traffic)
    report: dict[str, Any] = {"noResources": seen["noResources"]}
    try:
        if state["built"] and standing(int(state["built"]["buildingId"])) is False:
            # a terminal that no longer stands is let go and the industry may have one elsewhere
            # (live: one by the forestry was removed, its imports trucked 3km across town to the
            # densest industry, which then lay far from any terminal)
            state.setdefault("gone", []).append({**state["built"], "goneAt": state["wake"]})
            state["built"] = None
            report["gone"] = state["gone"][-1]["buildingId"]
        if state["built"]:
            verdict = judge(state, seen)
            report.update({"built": state["built"]["buildingId"], **({"judged": verdict} if verdict else {})})
            return report
        if "train" not in (wake.get("unlocked") or []):
            return {**report, "ok": None, "reason": "trainLocked"}
        if not seen["persisting"] and not seen["cargoHotspots"]:
            return {**report, "ok": None, "reason": "noEvidence"}
        # the tries beside the railway and those by the industry (loop.cargo_remote) count apart
        recent = [item for item in state["attempts"] if state["wake"] - item["wake"] < ATTEMPT_MEMORY]
        attempts = [item for item in recent if item.get("counts") and item.get("key") != "remote"]
        remote_attempts = [item for item in recent if item.get("counts") and item.get("key") == "remote"]
        if len(attempts) >= MAX_ATTEMPTS and len(remote_attempts) >= MAX_ATTEMPTS:
            return {**report, "ok": None, "reason": "attemptsSpent"}
        last = max((item["wake"] for item in state["attempts"]), default=None)
        if last is not None and state["wake"] - last < RETRY_AFTER:
            return {**report, "ok": None, "reason": "retryLater", "since": state["wake"] - last}
        if float(wake.get("cash") or 0) < COST + RESERVE:
            return {**report, "ok": None, "reason": "waitingForCash"}
        try:
            segments = mt._request("GET", "/state/networks", query={"limit": 20000}).get("segments", [])
            growables = next((value for value in mt._request("GET", "/state/growables", query={"limit": 50000})
                              .values() if isinstance(value, list)), [])
            facilities = mt._request("GET", "/state/facilities", query={"limit": 50000}).get("facilities", [])
            tried = {item["key"] for item in recent if item.get("key")}
            site = plan(segments, growables, facilities, tried) if len(attempts) < MAX_ATTEMPTS                 else {"ok": False, "why": "besideSpent"}
        except (mt.BridgeRequestError, RuntimeError) as error:
            # nothing was built while planning: a busy game thread waits for the next wake, and a
            # read that failed must not end the wake (the loop retries a crashed wake, then stops)
            return {**report, "ok": None, "reason": "busy" if NOT_RUN in str(error) else "readFailed",
                    "error": str(error)[:200]}
        if not site.get("ok"):
            # no site beside the railway: a terminal by the industry, joined to it through a tunnel
            from loop import cargo_remote  # it builds on this module's helpers
            remote = cargo_remote.build(label, growables, segments)                 if site.get("ok") is False and len(remote_attempts) < MAX_ATTEMPTS else None
            if remote is None or remote.get("stage") == "site":
                if site.get("ok") is False:  # no site now: looked again after RETRY_AFTER wakes
                    state["attempts"].append({"wake": state["wake"], "stage": site.get("why"), "counts": False})
                return {**report, "ok": None, "reason": site.get("why"), "refused": site.get("refused"),
                        **({"remote": remote} if remote else {})}
            state["attempts"].append({"wake": state["wake"], "key": "remote", "ok": remote["ok"], "counts": True,
                                      "stage": remote.get("stage"), "undo": remote.get("undo"),
                                      **{key: remote[key] for key in ("piece", "kind", "at", "why", "error")
                                         if remote.get(key) is not None}})
            if remote["ok"]:
                recent = state["history"][-PERSIST:]
                state["built"] = {"wake": state["wake"], "buildingId": remote["buildingId"], "links": remote["links"],
                                  "remote": True, "cleared": [], "verdict": None, "evidence": seen,
                                  "baseline": round(sum(recent) / max(1, len(recent)), 1),
                                  "truckShare": seen["truckShare"]}
            return {**report, **remote, "site": "remote"}
        outcome = build(label, site)
        if outcome.get("stage") != "busy":  # a game thread too busy to place it built nothing: next wake
            state["attempts"].append({"wake": state["wake"], "key": site["key"], "ok": outcome["ok"], "counts": True,
                                      "stage": outcome.get("stage"), "undo": outcome.get("undo")})
        if outcome["ok"]:
            recent = state["history"][-PERSIST:]
            state["built"] = {"wake": state["wake"], "buildingId": outcome["buildingId"], "links": outcome["links"],
                              "center": _point(site["center"]), "angleDegrees": site["angleDegrees"],
                              "cleared": outcome["cleared"], "baseline": round(sum(recent) / max(1, len(recent)), 1),
                              "truckShare": seen["truckShare"], "evidence": seen, "verdict": None}
        return {**report, **outcome, "site": {k: site[k] for k in ("key", "segmentId", "offset", "side", "industryScore")},
                "linkCount": len(site["links"])}
    finally:
        state["attempts"] = state["attempts"][-20:]
        city.save_state("cargo-rail", state)

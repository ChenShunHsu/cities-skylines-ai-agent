"""One ground road segment under the full protocol, and a fishbone grown from it.

build: preview (valid, terrain safe, nothing under the road, no collinear-merge risk; a road that
only dips into bumps is built as a cut, which the game carves), trying small end lifts while the
ground is gently convex; checkpoint and baseline; build_network (the Bridge
verifies the settled road and rolls a failed one back, so the next lift is tried); the road's QA
(a dead end is allowed); regression.

grow_fishbone: a collector in SPACING steps alternating +-WIGGLE degrees (collinear pieces merge
their middle node), each node getting a dead-end branch on both sides, so parallel branches sit
SPACING apart and their four-cell zones meet. A step keeps its planned line: one the ground will not
hold as one piece is laid as two half pieces on the same line, whose middle node follows the ground;
where neither fits, the collector bends BEND degrees and keeps the new heading, always to the
same side, up to MAX_BEND from where it began: a run of such steps is one smooth curve (an 88m
step at 15 degrees is a 340m radius) that follows a slope's contour. The collector stops where the
land ends (no such step can be previewed), and keeps its progress in the city state so a later wake
continues it.

A road is never steered or shortened around what is in its way, step by step: roads that dodged
turned at every angle and the town looked drawn by chance. Curves are allowed: a bend kept to one side, or an arc (arc_points) through a way's own two ends.
"""

from __future__ import annotations

import math
from typing import Any

from loop import protocol
from loop.city import City, mt

LIFTS = (0.0, 0.04, 0.08, 0.12, 0.15, 0.2, 0.25)
SPACING = 88.0
WIGGLE = 6.0
MAX_PIECE = 96.0  # the native road tool builds pieces under 100m
# a road keeps its planned line: ground it cannot pass ends it, and another way is planned
STEER = (0.0,)
STEP_LENGTHS = (SPACING,)
BEND = 15.0  # a collector's turn where its line will not fit, kept to one side: a curve, not a dodge
MAX_BEND = 90.0  # how far a collector may curve from its first heading
ARC_BULGES = (0.15, 0.3)  # how far an arc through a way's ends bows out, as a share of the way's length
GROWABLE = {"Residential", "Commercial", "Industrial", "Office"}
NOT_RUN = '"noConstructionAttempted": true'  # a Bridge timeout before the command ran


def preview(prefab: str, start: tuple[float, float], end: tuple[float, float], label: str,
            lift: float = 0.0) -> tuple[bool, dict[str, Any], dict[str, Any]]:
    body: dict[str, Any] = {"roadPrefab": prefab, "start": {"x": start[0], "z": start[1]},
                            "end": {"x": end[0], "z": end[1]}, "name": label}
    if lift:
        body["endTerrainFitLift"] = lift
    try:
        result = mt.call_tool("preview_network", body)
    except mt.BridgeRequestError as error:
        return False, body, {"valid": False, "errors": str(error)[:200]}
    hits = (result.get("buildingsUnderRoad") or []) + (result.get("powerLinesCrossed") or [])
    if hits:
        return False, body, {"valid": False, "obstacles": hits, "errors": result.get("toolErrors")}
    terrain = result.get("terrainProfile") or {}
    risk = result.get("nativeMoveMiddleNodeRisk")
    risky = bool(risk.get("risk") if isinstance(risk, dict) else risk)
    strict = bool(result.get("valid") and terrain.get("safe") and not risky)
    # a road that only dips into bumps is built as a cut: the game carves the ground it crosses
    cut = not strict and bool(result.get("validWithTerrainCut")) and not risky
    if cut:
        body["allowTerrainCut"] = True
    return strict or cut, body, {"valid": result.get("valid"), "terrainSafe": terrain.get("safe"), "cut": cut,
                                 "reusedEnd": bool(result.get("reusedEndNode")),
                                 "settledMinAbove": (terrain.get("settledShape") or {}).get("minimumRoadAboveTerrain"),
                                 "originalNode": terrain.get("originalNodeTransition"),
                                 "carve": (result.get("terrainCut") or {}).get("carvable"),
                                 "moveMiddleNodeRisk": risky, "errors": result.get("toolErrors"),
                                 "minAbove": terrain.get("minimumRoadAboveTerrain")}


def build(label: str, prefab: str, start: tuple[float, float], end: tuple[float, float],
          city: City | None = None, keep_failed: bool = False) -> dict[str, Any]:
    """One road piece, previewed at each lift, built and checked. A piece that fails its checks is
    taken out again, unless the caller keeps it (pieces of one change that only pass together,
    which the caller removes as a whole)."""
    tried = []
    for lift in LIFTS:
        ok, body, info = preview(prefab, start, end, label, lift)
        tried.append({"lift": lift, **info})
        if not ok:
            if info.get("reusedEnd"):  # a lift raises a new end only; this road ends on an existing node
                return {"label": label, "ok": False, "stage": "preview", "tried": tried}
            continue
        name = protocol.checkpoint(label)
        try:
            result = mt.call_tool("build_network", {**body, "checkpointName": name})
            break
        except (mt.BridgeRequestError, RuntimeError) as error:
            text = str(error)
            tried[-1]["settledFailure"] = text[:300]
            if city:
                city.log("road-calibration", {"label": label, "prefab": prefab, "preview": tried[-1]})
            if "already failed in this city" in text:
                continue  # the Bridge remembers alignments that did not settle: another lift may differ
            if NOT_RUN in text:
                # the game thread was too busy to take the command: nothing was built, and the road
                # waits for the next wake (live: a grid edge read as built-and-failed and stopped the loop)
                return {"label": label, "ok": False, "stage": "busy", "error": text[:300], "tried": tried}
            if '"localRollbackSucceeded": true' not in text:
                return {"label": label, "ok": False, "stage": "build", "error": text[:600], "tried": tried}
    else:
        return {"label": label, "ok": False, "stage": "preview", "tried": tried[:1] + tried[1:][-1:]}
    segment = result.get("segmentId")
    if not result.get("ok") or not segment:
        return {"label": label, "ok": False, "stage": "build"}
    details = mt._request("GET", "/state/network-segment-details", query={"id": segment})
    qa = mt.call_tool("inspect_road_result", {"segmentId": segment, "allowTerminalDeadEnd": True,
                                              "terminalNodeId": details["endNodeId"]})
    regression = protocol.verify(label)
    if (not qa.get("ok") or regression.get("ok") is False) and not keep_failed:
        # a road that failed its checks comes straight out again (75% back): left standing, the
        # next angle tried was built beside it, and one node grew a fork of failed roads (live)
        undo = protocol.run(f"{label}-undo", "bulldoze_entity", {"entityType": "netSegment", "id": segment})
        return {"label": label, "ok": False, "stage": "checks", "segmentId": segment, "removed": bool(undo["ok"]),
                "anomalies": [item.get("type") for item in qa.get("focusedAnomalies") or []],
                "qaOk": qa.get("ok"), "regressionOk": regression.get("ok")}
    return {"label": label, "ok": bool(qa.get("ok")) and regression.get("ok") is not False, "segmentId": segment,
            **({} if qa.get("ok") and regression.get("ok") is not False else {"stage": "checks", "removed": False}),
            "nodes": [details["startNodeId"], details["endNodeId"]],
            "end": details.get("end") or details.get("endPosition"),
            "qaOk": qa.get("ok"), "regressionOk": regression.get("ok")}


GONE = "Building was not found"


def clear_buildings(label: str, ids: list[int]) -> dict[str, Any]:
    """Bulldoze buildings one by one, each after its own bulldoze preview. Displacement is not
    capped; a building whose removal the preview rates unsafe stops the clearing. One already gone
    is passed: a listing read earlier may name buildings the game has since removed (live: a piece
    built over its neighbours took them down, and the next piece's clearing stopped the wake)."""
    cleared = []
    for building in ids:
        try:
            impact = mt.call_tool("preview_bulldoze_impact", {"entityType": "building", "id": building})
        except mt.BridgeRequestError as error:
            if GONE in str(error):
                continue
            raise
        if not impact.get("safe"):
            return {"ok": False, "blocked": building, "cleared": cleared}
        outcome = protocol.run(f"{label}-clear-{building}", "bulldoze_entity", {"entityType": "building", "id": building})
        if not outcome["ok"]:
            return {"ok": False, "blocked": building, "cleared": cleared}
        cleared.append(building)
    return {"ok": True, "cleared": cleared}


def blocked_beyond(info: dict[str, Any]) -> bool:
    """Something besides the homes, shops and workplaces under the road stops it: the game reports a
    collision for them only rarely (a building counts when it is important), so a collision with
    nothing but growables under it is another road or building the clearing would not move (live:
    four homes cleared for a bypass piece the game then refused for the same collision; probing roads
    over growables only, 66 of 68 collisions lay on or beside another road, 13 of 15 clear ones did
    not)."""
    obstacles = [item for item in info.get("obstacles") or [] if "service" in item]
    return bool(obstacles) and all(item["service"] in GROWABLE for item in obstacles) \
        and "ObjectCollision" in str(info.get("errors") or "")


def build_through(label: str, prefab: str, start: tuple[float, float], end: tuple[float, float],
                  city: City | None = None, keep_failed: bool = False) -> dict[str, Any]:
    """build, after clearing the homes, shops and workplaces the road would run over; a service
    building in the way is left standing and the road is not built."""
    _, _, info = preview(prefab, start, end, label)
    under = [item for item in info.get("obstacles", []) if "service" in item]
    if blocked_beyond(info):
        return {"label": label, "ok": False, "stage": "blockedBeyond", "error": info.get("errors")}
    if under and all(item["service"] in GROWABLE for item in under):
        cleared = clear_buildings(label, [item["id"] for item in under])
        if not cleared["ok"]:
            return {"label": label, "ok": False, "stage": "clearing", **cleared}
    return build(label, prefab, start, end, city, keep_failed)


def build_raised(label: str, prefab: str, piece: dict[str, Any]) -> dict[str, Any]:
    """One piece of an overpass (start and end height offsets), previewed then built under the
    protocol. Terrain lifts do not apply above ground."""
    body = {"roadPrefab": prefab, "start": piece["start"], "end": piece["end"], "name": label,
            "startHeightOffset": piece["startHeightOffset"], "endHeightOffset": piece["endHeightOffset"]}
    try:
        preview = mt.call_tool("preview_network", body)
    except mt.BridgeRequestError as error:
        return {"label": label, "ok": False, "stage": "preview", "error": str(error)[:200]}
    if not preview.get("valid"):
        return {"label": label, "ok": False, "stage": "preview", "errors": preview.get("toolErrors")}
    outcome = protocol.run(label, "build_network", body, 3)
    segment = (outcome.get("result") or {}).get("segmentId")
    if not outcome["ok"] or not segment:
        return {"label": label, "ok": False, "stage": "build", "error": outcome.get("error")}
    details = mt._request("GET", "/state/network-segment-details", query={"id": segment})
    return {"label": label, "ok": outcome["regressionOk"] is not False, "segmentId": segment,
            "end": details.get("end") or details.get("endPosition"), "regressionOk": outcome["regressionOk"]}


def heading_vector(degrees: float) -> tuple[float, float]:
    """Heading 0 is due south (-z); positive turns toward +x."""
    radians = math.radians(degrees)
    return math.sin(radians), -math.cos(radians)


def build_leg(label: str, prefab: str, start: tuple[float, float], angle: float, length: float,
              city: City | None = None) -> dict[str, Any]:
    """A straight-ish leg; one longer than MAX_PIECE is two halves bent +-3 degrees. Buildings
    cannot use the middle of a road segment longer than about 160m, so legs stay short."""
    if length <= MAX_PIECE:
        end = (start[0] + math.cos(angle) * length, start[1] + math.sin(angle) * length)
        return build(label, prefab, start, end, city)
    half = length / 2.0
    first_angle, second_angle = angle - math.radians(3.0), angle + math.radians(3.0)
    middle = (start[0] + math.cos(first_angle) * half, start[1] + math.sin(first_angle) * half)
    first = build(label, prefab, start, middle, city)
    if not first["ok"]:
        return first
    middle = (first["end"]["x"], first["end"]["z"])
    end = (middle[0] + math.cos(second_angle) * half, middle[1] + math.sin(second_angle) * half)
    second = build(label + "-h2", prefab, middle, end, city)
    return second if second["ok"] else first


def step_ends(start: tuple[float, float], heading: float,
              turns: tuple[float, ...] = STEER) -> list[tuple[float, tuple[float, float]]]:
    """The collector steps to try from start, planned heading first: (heading, end)."""
    ends = []
    for length, turn in [(length, turn) for length in STEP_LENGTHS for turn in turns]:
        hx, hz = heading_vector(heading + turn)
        ends.append((heading + turn, (start[0] + hx * length, start[1] + hz * length)))
    return ends


def step_paths(start: tuple[float, float], heading: float) -> list[tuple[float, list[tuple[float, float]]]]:
    """step_ends as one-piece paths, then each steered heading as two half pieces: a middle node
    lets the road follow ground that bulges under a straight 88m piece."""
    paths = [(step, [end]) for step, end in step_ends(start, heading)]
    for turn in STEER:
        hx, hz = heading_vector(heading + turn)
        middle = (start[0] + hx * SPACING / 2, start[1] + hz * SPACING / 2)
        paths.append((heading + turn, [middle, (middle[0] + hx * SPACING / 2, middle[1] + hz * SPACING / 2)]))
    return paths


def follow(label: str, prefab: str, start: tuple[float, float], path: list[tuple[float, float]],
           city: City | None = None) -> dict[str, Any]:
    """Build a path's pieces in turn: the last built piece once the first is built, else the failure."""
    done = None
    for index, point in enumerate(path):
        outcome = build(label if index == 0 else f"{label}-{index}", prefab, start, point, city)
        if not outcome["ok"]:
            return done or outcome
        done, start = outcome, (outcome["end"]["x"], outcome["end"]["z"])
    return done or {"label": label, "ok": False, "stage": "preview"}


def arc_points(start: tuple[float, float], end: tuple[float, float], bulge: float,
               side: int) -> list[tuple[float, float]]:
    """The piece ends of a circular arc from start to end that bows out bulge times its chord to
    the left (side 1, looking from start to end) or right (-1), in equal pieces of at most SPACING,
    end exactly last. Equal pieces turn by the same angle at every joint, so the road reads as one
    curve."""
    chord = math.dist(start, end)
    if chord < 1.0:
        return [end]
    sagitta = bulge * chord
    radius = (chord * chord / 4.0 + sagitta * sagitta) / (2.0 * sagitta)
    sweep = 2.0 * math.asin(min(1.0, chord / (2.0 * radius)))
    pieces = max(2, math.ceil(radius * sweep / SPACING))
    ux, uz = (end[0] - start[0]) / chord, (end[1] - start[1]) / chord
    nx, nz = -uz * side, ux * side  # toward the bow
    middle = ((start[0] + end[0]) / 2.0, (start[1] + end[1]) / 2.0)
    center = (middle[0] - nx * (radius - sagitta), middle[1] - nz * (radius - sagitta))
    first = math.atan2(start[1] - center[1], start[0] - center[0])
    last = math.atan2(end[1] - center[1], end[0] - center[0])
    turn = (last - first + math.pi) % (2.0 * math.pi) - math.pi  # the minor arc, through the bow
    points = [(center[0] + radius * math.cos(first + turn * index / pieces),
               center[1] + radius * math.sin(first + turn * index / pieces)) for index in range(1, pieces)]
    return points + [end]


def passable(prefab: str, start: tuple[float, float], end: tuple[float, float], label: str) -> bool:
    """Whether the piece previews at some lift: nothing in its way and ground that will hold it."""
    for lift in LIFTS:
        ok, _, info = preview(prefab, start, end, label, lift)
        if ok:
            return True
        if info.get("obstacles") or info.get("reusedEnd"):
            return False  # no lift moves a building, a line or an existing node
    return False


def lay_arc(label: str, prefab: str, start: tuple[float, float], end: tuple[float, float],
            city: City | None = None) -> dict[str, Any]:
    """A road from start to end as an arc through both (arc_points), for where the straight line
    will not fit: bowing a little then more, either side, an arc is built only when every piece of
    it previews, and one refused after that has its built pieces removed, so the road reaches end
    or nothing is left. The last piece's outcome, with the curve taken; every arc tried goes to
    the city's road-arcs log (which piece refused it), so live play shows how the curves fare."""
    tried: list[dict[str, Any]] = []

    def logged(result: dict[str, Any]) -> dict[str, Any]:
        if city:
            city.log("road-arcs", {"label": label, "start": start, "end": end, "ok": result["ok"], "tried": tried})
        return result

    for bulge in ARC_BULGES:
        for side in (1, -1):
            points = arc_points(start, end, bulge, side)
            ends = [start] + points
            refused = next((index for index, (a, b) in enumerate(zip(ends, ends[1:]))
                            if not passable(prefab, a, b, f"{label}-arc")), None)
            tried.append({"bulge": bulge, "side": side, "pieces": len(points), "refusedAt": refused})
            if refused is not None:
                continue
            built: list[int] = []
            here, outcome = start, {"ok": False}
            for index, point in enumerate(points):
                outcome = build(f"{label}-r{index}", prefab, here, point, city)
                if not outcome["ok"]:
                    break
                built.append(outcome["segmentId"])
                here = (outcome["end"]["x"], outcome["end"]["z"])
            if outcome["ok"]:
                return logged({**outcome, "curve": {"bulge": bulge, "side": side, "pieces": len(points)}})
            tried[-1]["built"] = {"stage": outcome.get("stage"), "removed": len(built)}
            for segment in reversed(built):
                protocol.run(f"{label}-arc-undo-{segment}", "bulldoze_entity",
                             {"entityType": "netSegment", "id": segment})
    return logged({"label": label, "ok": False, "stage": "preview"})


def can_step(start: tuple[float, float], heading: float, label: str, prefab: str = "Basic Road") -> bool:
    """Whether some collector step from start previews (no lift: a quick, slightly strict test)."""
    return any(preview(prefab, start, end, label)[0] for _, end in step_ends(start, heading))


def retryable(outcome: dict[str, Any]) -> bool:
    """A step the ground refused, or one built, failed and taken out again: the next way is tried."""
    return outcome.get("stage") == "preview" or (outcome.get("stage") == "checks" and outcome.get("removed"))


def grow_fishbone(city: City, name: str, start: tuple[float, float], heading: float, segments: int,
                  branch_lengths: tuple[float, ...] = (128.0, 96.0, 64.0), collector_prefab: str = "Basic Road",
                  branch_prefab: str = "Basic Road", label: str | None = None) -> dict[str, Any]:
    """Add up to `segments` collector steps to the named fishbone (created at start/heading the
    first time), branching each new node on both sides. Returns what was built this call."""
    label = label or name
    state = city.load_state(f"fishbone-{name}", {"collector": [{"node": list(start), "heading": heading}],
                                                 "branches": [], "base": heading})
    built = {"collector": [], "branches": []}

    def branch(index: int) -> None:
        node = state["collector"][index]
        following = state["collector"][index + 1]["heading"] if index + 1 < len(state["collector"]) else node["heading"]
        hx, hz = heading_vector((node["heading"] + following) / 2.0)
        for side, (px, pz) in (("east", (-hz, hx)), ("west", (hz, -hx))):
            if any(item["index"] == index and item["side"] == side and item["ok"] for item in state["branches"]):
                continue
            outcome = {"ok": False}
            for length in branch_lengths:
                outcome = build_leg(f"{label}-b{index}{side[0]}", branch_prefab, tuple(node["node"]),
                                    math.atan2(pz, px), length, city)
                if outcome["ok"] or not retryable(outcome):
                    break
            state["branches"].append({"index": index, "side": side, "ok": outcome["ok"],
                                      "segmentId": outcome.get("segmentId")})
            built["branches"].append({"index": index, "side": side, "ok": outcome["ok"]})

    def lay(heading: float) -> tuple[float, dict[str, Any]]:
        last = state["collector"][-1]
        outcome: dict[str, Any] = {"ok": False, "stage": "preview"}
        for step, path in step_paths(tuple(last["node"]), heading):
            outcome = follow(f"{label}-c{len(state['collector'])}", collector_prefab, tuple(last["node"]), path, city)
            if outcome["ok"] or not retryable(outcome):
                return step, outcome
        return heading, outcome

    for _ in range(segments):
        last = state["collector"][-1]
        planned = state["base"] + (-WIGGLE if last["heading"] - state["base"] > 0 else WIGGLE)
        step, outcome = lay(planned)
        bent = None
        if not outcome["ok"] and retryable(outcome):
            # the line will not fit: bend, to the side this collector already curves to
            for side in ([state["bend"]] if state.get("bend") else [1, -1]):
                turned = last["heading"] + side * BEND
                if abs(turned - state["collector"][0]["heading"]) > MAX_BEND:
                    continue
                turned_step, attempt = lay(turned)
                if attempt["ok"]:
                    step, outcome, bent = turned_step, attempt, side
                    state["bend"], state["base"] = side, turned_step
                    break
                if not retryable(attempt):
                    outcome = attempt
                    break
        built["collector"].append({"ok": outcome["ok"], "stage": outcome.get("stage"),
                                   **({"bend": bent} if bent else {})})
        if not outcome["ok"]:
            if outcome.get("stage") != "busy":  # a busy game thread ends nothing: the next wake goes on
                state["ended"] = outcome.get("stage") or "build"  # an end with no stage was tried again
            break
        state["collector"].append({"node": [outcome["end"]["x"], outcome["end"]["z"]], "heading": step,
                                   "segmentId": outcome["segmentId"]})
        if len(state["collector"]) >= 3:
            branch(len(state["collector"]) - 2)  # the node that now has collector roads on both sides
        city.save_state(f"fishbone-{name}", state)
    if len(state["collector"]) >= 2:
        branch(len(state["collector"]) - 1)  # the end node: zone frontage would block its branches later
    city.save_state(f"fishbone-{name}", state)
    built["collectorNodes"] = len(state["collector"])
    built["ended"] = state.get("ended")
    return built

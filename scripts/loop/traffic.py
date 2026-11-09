"""Per-wake congestion watch driven by propose_traffic_remedies.

A hotspot has to persist for PERSIST wakes before anything changes (traffic swings with the time
of day). Then its remedy kinds come in the order its route evidence supports, and the loop
executes the kinds it has an executor for, one action a wake: junction trials judge and undo
themselves; widening, a transit line and a bypass road are judged after JUDGE_AFTER wakes by the
hotspot's density (or its disappearance). A kind that failed most of at least three tries in this
city goes last. A bypass is the structural step the loop takes itself before a planner.

A hotspot whose own trips mostly go in and out of town (externalShare) gets one more kind, after
all the others: outsideLink widens the town's link to the highway and the first stretch of the
trunk all outside trips share. Only then does it go to the planner.

A hotspot whose executable kinds are spent goes to the planner with its diagnosis (cargo rail or
an interchange redesign need planning), at most once every ESCALATE_EVERY wakes, and its kinds
are retried after RETRY_AFTER wakes, since the city around it changes.
"""

from __future__ import annotations

import heapq
import json
import math
from typing import Any, Callable

from loop import metro, protocol, ramps, roads, services
from loop.city import City, mt
from mayor import network

PERSIST = 3
JUDGE_AFTER = 2
RETRY_AFTER = 8
ESCALATE_EVERY = 80
MAX_JUNCTION_TRIALS = 3
DEPOT_PREFAB = "Bus Depot"
# a bypass is worth building when it carries at least this many of the hotspot's vehicles, and two for
# every building it clears (live: nine buildings cleared for a 228m connector that served four)
BYPASS_MIN_VEHICLES = 10
BYPASS_VEHICLES_PER_CLEARED = 2
TRANSIT_REACH = 800.0  # a relief line stops this close to its hotspot
# a bus line is reviewed by its riders (live: 12 relief lines ran some 240 buses for about 360 riders
# a week, one 25 buses for 2, and buses on the road are traffic too)
RELIEF_PREFIX = "Relief"  # the lines the loop made; others are the player's and left alone
LINE_REVIEW_AFTER = 8  # wakes a line runs, and a halved line runs again, before its riders count
# riders a week per bus below which a relief line is halved, then deleted: a bus holds about 30, and
# ten relief lines carrying 2 to 11 a bus a week ran 177 buses on the congested roads (live)
LOW_RIDERS_PER_BUS = 10.0
MAX_RELIEF_LINES = 6  # past this a hotspot's local trips go to a metro line, under the roads
# stops within OVERLAP_REACH of another line's stop are served already: a new line whose stops are
# NEW_OVERLAP so served is not opened, and a relief line with DUPLICATE_OVERLAP of its stops by one
# busier line is deleted (live: two relief lines had every stop by another line's)
OVERLAP_REACH = 60.0
NEW_OVERLAP = 0.6
DUPLICATE_OVERLAP = 0.8
MAX_LINE_CHANGES = 3  # line changes a wake
# a relief line whose loop runs more than this many times the way out along its stops (and back)
# spends its riders' time on detours (live: lines at 2.4 and 3.0 carried 10 or 11 riders a week,
# those at 1.9 or less 12 to 81)
DETOUR_MAX = 2.2
EXECUTABLE = ("relieveDownstream", "transit", "junctionControl", "widen", "bypass", "outsideLink")
HIGHWAY_FED = 0.5  # share of a hotspot's vehicles on the highway past which it gets an exit ramp
# outsideLink: live (2052, 27,000 people, one highway link): hotspot-559, 600m east of the link
# junction, had half its paired trips (externalShare 0.505) going to or from outside after transit,
# bypass, widen and relieveDownstream were spent, while the two off-ramp link roads (Small 4 Lane
# Road) stood at density 99-100 and NoResources rose: every truck in or out uses that one link
OUTSIDE_SHARE = 0.4
# three lanes each way match the three-lane highway ramps; Medium Road when Large is locked
OUTSIDE_LINK_PREFABS = ("Large Road", "Medium Road")
WIDE_PREFIXES = ("Large Road", "Medium Road")  # already as wide as the link gets: left as they are
OUTSIDE_LINK_REACH = 1000.0  # metres from the highway along the route
OUTSIDE_LINK_MAX_SEGMENTS = 12
# a wider road runs over the lots that fronted the narrow one (live: the first trunk segment past the
# junction would clear 11 buildings, 10 of them homes; the whole 800m trunk to the hotspot 64): a
# way in stops at the first segment that would take the clearing past these
OUTSIDE_LINK_MAX_CLEARED = 6
OUTSIDE_LINK_MAX_HOMES = 3
DEFAULT_STATE = {"hotspots": [], "tried": {}, "actions": [], "kindStats": {}, "spent": {}, "escalatedAt": {},
                 "widened": {}, "wake": 0}


def same(a: list[int], b: list[int]) -> bool:
    left, right = set(a), set(b)
    return len(left & right) >= 0.5 * min(len(left), len(right))


def order(kinds: list[str], stats: dict[str, dict[str, int]]) -> list[str]:
    """Evidence order, but a kind that failed most of at least three tries here goes last."""
    def failing(kind: str) -> bool:
        record = stats.get(kind, {})
        tries = record.get("worked", 0) + record.get("failed", 0)
        return tries >= 3 and record.get("failed", 0) / tries > 0.7
    return sorted(kinds, key=failing)


def kinds_for(hotspot: dict[str, Any], remedies: dict[str, dict[str, Any]],
              stats: dict[str, dict[str, int]], metro_ready: bool = False) -> list[str]:
    """The executable kinds in the order they are tried. A hotspot whose paired trips mostly go to
    or come from outside (externalShare at least OUTSIDE_SHARE) gets outsideLink after all of them,
    even after a kind that keeps failing: it rebuilds roads and clears lots, so it waits until the
    cheaper local kinds are spent, and it comes before the planner. A hotspot of local cars that
    transit fits gets a metro line (loop.metro) once metro is unlocked, after the cheaper kinds."""
    kinds = order([kind for kind in remedies if kind in EXECUTABLE and kind not in ("outsideLink", "metro")], stats)
    if metro_ready and "transit" in remedies:
        remedies["metro"] = {"kind": "metro", "candidates": None,
                             "reason": "local car trips a bus in the same traffic does not take"}
        kinds.append("metro")
    diagnosis = hotspot.get("diagnosis") or {}
    highway = float(diagnosis.get("highwayShare") or 0)
    outside = float(diagnosis.get("externalShare") or 0)
    if highway >= HIGHWAY_FED or outside >= OUTSIDE_SHARE:
        # the traffic arrives by highway, or comes from outside across town to where it goes (live: a
        # town hotspot with no highway in it carried 61% outside trips from the one link at the far
        # end): an exit nearer where it goes (loop.ramps), before the link is widened
        remedies["highwayExit"] = {"kind": "highwayExit", "candidates": None,
                                   "reason": f"{max(highway, outside):.0%} of its vehicles use the highway"
                                             if highway >= outside else f"{outside:.0%} of its trips come from outside"}
        kinds.append("highwayExit")
    share = float((hotspot.get("diagnosis") or {}).get("externalShare") or 0)
    if share >= OUTSIDE_SHARE:
        remedies["outsideLink"] = {"kind": "outsideLink", "candidates": None,
                                   "reason": f"{share:.0%} of paired trips go to or come from outside"}
        kinds.append("outsideLink")
    return kinds


def count(state: dict[str, Any], kind: str, verdict: str) -> None:
    record = state["kindStats"].setdefault(kind, {"worked": 0, "failed": 0})
    record["worked" if verdict == "worked" else "failed"] += 1


def record(state: dict[str, Any], key: str, kind: str, hotspot: dict[str, Any], detail: dict[str, Any],
           verdict: str | None = None) -> dict[str, Any]:
    action = {"key": key, "kind": kind, "wake": state["wake"], "meanDensity": hotspot["meanDensity"],
              "segments": hotspot["segmentCount"], "detail": detail, "verdict": verdict}
    state["actions"].append(action)
    if verdict:
        count(state, kind, verdict)
    return action


def judge(state: dict[str, Any], hotspots: list[dict[str, Any]], tracked: list[dict[str, Any]],
          label: str) -> list[dict[str, Any]]:
    """Widening, transit and bypasses cannot undo themselves; judge them by the hotspot later."""
    verdicts = []
    lines: dict[int, dict[str, Any]] | None = None
    for action in state["actions"]:
        if action["verdict"] or state["wake"] - action["wake"] < JUDGE_AFTER:
            continue
        current = next((hotspot for hotspot, item in zip(hotspots, tracked) if item["key"] == action["key"]), None)
        worked = (current is None or current["meanDensity"] <= action["meanDensity"] - 5
                  or current["segmentCount"] <= 0.7 * action["segments"])
        if action["kind"] == "transit" and worked:
            # a hotspot that eased while the line ran empty eased for another reason (live: lines of
            # 2 to 11 riders a bus counted 18 of 31 transit actions as working)
            if lines is None:
                lines = {int(line["id"]): line for line in _bus_lines()}
            line = lines.get(int(action["detail"].get("lineId") or 0))
            buses = int((line or {}).get("vehicles") or 0)
            worked = bool(line) and (not buses or _riders(line) / buses >= LOW_RIDERS_PER_BUS)
        action["verdict"] = "worked" if worked else "failed"
        count(state, action["kind"], action["verdict"])
        if action["kind"] == "transit" and not worked and action["detail"].get("lineId"):
            # a line that did not relieve the hotspot keeps running on half the buses
            protocol.run(f"transit-halve-{action['detail']['lineId']}-{label}", "set_transport_line",
                         {"lineId": action["detail"]["lineId"], "budget": 50})
        verdicts.append({"key": action["key"], "kind": action["kind"], "verdict": action["verdict"]})
    state["actions"] = state["actions"][-60:]
    return verdicts


def trial(label: str, candidate: dict[str, Any], watch_segments: list[int]) -> tuple[dict[str, Any], dict[str, Any]]:
    arguments = {k: v for k, v in candidate["trial"].items() if k != "tool"}
    arguments["watchSegmentIds"] = watch_segments[:50]
    outcome = protocol.run(f"traffic-{label}-{candidate['nodeId']}", "trial_junction_control", arguments)
    result = outcome.get("result") or {}
    return outcome, {"nodeId": candidate["nodeId"], "kind": candidate["kind"], "ok": outcome["ok"],
                     "reverted": result.get("reverted"), "regressionOk": outcome["regressionOk"]}


def widen(label: str, upgrades: list[dict[str, Any]]) -> dict[str, Any]:
    segment_ids = [c["segmentId"] for c in upgrades][:40]
    outcome = protocol.run(f"widen-{label}", "widen_road_chain", {"segmentIds": segment_ids})
    return {"ok": outcome["ok"], "regressionOk": outcome["regressionOk"], "segments": segment_ids,
            "error": (outcome.get("result") or {}).get("error")}


def _line_preview(stops: list[dict[str, float]]) -> dict[str, Any]:
    return mt._request("POST", "/commands/create-transport-line", body={
        "transportType": "Bus", "stops": stops, "completeLoop": True, "dryRun": True})


def _wants_only_a_depot(preview: dict[str, Any]) -> bool:
    """Every stop fits its road and keeps its distance, and no depot serves the line."""
    depot = preview.get("depot") or {}
    checks = preview.get("stopChecks") or []
    return bool(preview.get("ok")) and not preview.get("valid") and bool(depot.get("required")) \
        and not depot.get("safe") and bool(checks) \
        and all(check.get("compatibleNetwork") and check.get("separated") for check in checks)


def _has_depot() -> bool:
    return any(item.get("prefab") == DEPOT_PREFAB
               for item in mt._request("GET", "/state/facilities", query={"limit": 50000}).get("facilities", []))


def _bus_lines() -> list[dict[str, Any]]:
    return [line for line in mt._request("GET", "/state/transport-lines", query={"limit": 200}).get("lines", [])
            if line.get("transportType") == "Bus"]


def _riders(line: dict[str, Any]) -> int:
    return int((line.get("weeklyPassengers") or {}).get("total") or 0)


def relief_underused(lines: list[dict[str, Any]] | None = None) -> bool:
    """Whether the relief lines running carry fewer than LOW_RIDERS_PER_BUS riders a week a bus."""
    relief = [line for line in (lines if lines is not None else _bus_lines())
              if str(line.get("name") or "").startswith(RELIEF_PREFIX) and int(line.get("vehicles") or 0) > 0]
    buses = sum(int(line["vehicles"]) for line in relief)
    return bool(buses) and sum(_riders(line) for line in relief) / buses < LOW_RIDERS_PER_BUS


def _stops(line: dict[str, Any]) -> list[tuple[float, float]]:
    return [(stop["position"]["x"], stop["position"]["z"]) for stop in line.get("stopNodes") or []
            if (stop.get("position") or {}).get("x") is not None]


def overlap(stops: list[tuple[float, float]], others: list[tuple[float, float]]) -> float:
    """The share of stops within OVERLAP_REACH of any of others."""
    if not stops:
        return 0.0
    return sum(1 for stop in stops if any(math.dist(stop, other) <= OVERLAP_REACH for other in others)) / len(stops)


def duplicate_of(line: dict[str, Any], lines: list[dict[str, Any]]) -> int | None:
    """A busier line serving DUPLICATE_OVERLAP of this relief line's stops, or None."""
    stops = _stops(line)
    for other in lines:
        if other["id"] != line["id"] and _riders(other) >= _riders(line) \
                and overlap(stops, _stops(other)) >= DUPLICATE_OVERLAP:
            return int(other["id"])
    return None


def detour(line: dict[str, Any]) -> float | None:
    """The line's loop over twice the way along its stops; None before the game has laid its path."""
    stops = [(stop["position"]["x"], stop["position"]["z"]) for stop in line.get("stopNodes") or []
             if (stop.get("position") or {}).get("x") is not None]
    span = sum(math.dist(a, b) for a, b in zip(stops, stops[1:]))
    length = float(line.get("totalLength") or 0)
    return round(length / (2 * span), 2) if span > 0 and length > 0 else None


def review_lines(state: dict[str, Any], label: str) -> list[dict[str, Any]]:
    """Bus lines by their riders, MAX_LINE_CHANGES a wake: a line with no stops is deleted; a relief
    line that has run LINE_REVIEW_AFTER wakes with fewer than LOW_RIDERS_PER_BUS riders a week a bus
    is halved (budget 50), and one still that low LINE_REVIEW_AFTER wakes after it was halved is
    deleted. A relief line whose loop detours more than DETOUR_MAX, or whose stops a busier line
    serves (duplicate_of), is deleted at once. Lines first
    seen here count from when the loop made them, else as long run. A relief line the game reports
    not connected is deleted at once."""
    made = {int(action["detail"]["lineId"]): action["wake"] for action in state["actions"]
            if action["kind"] == "transit" and (action.get("detail") or {}).get("lineId")}
    records = state.setdefault("lines", {})
    changes: list[dict[str, Any]] = []
    lines = _bus_lines()
    # a line the game reports not connected runs no bus at all (live: two relief lines each ran
    # stops the buses could not reach, no bus came out, and the loop stopped on them)
    broken = {int(item["lineId"]) for item in mt._request("GET", "/state/transport-line-anomalies",
                                                         query={"limit": 200}).get("anomalies", [])
              if item.get("type") == "lineNotConnected" and item.get("lineId")}
    for line in lines:
        line_id = int(line["id"])
        record = records.setdefault(str(line_id), {"since": made.get(line_id, state["wake"] - LINE_REVIEW_AFTER)})
        if len(changes) >= MAX_LINE_CHANGES:
            continue
        riders, buses = _riders(line), int(line.get("vehicles") or 0)
        change = None
        relief = str(line.get("name") or "").startswith(RELIEF_PREFIX)
        if not int(line.get("stops") or 0):
            change = ("deleted", "delete_transport_line", {"lineId": line_id})  # an empty line serves no one
        elif relief and (line_id in broken or (detour(line) or 0) > DETOUR_MAX
                         or duplicate_of(line, [other for other in lines if int(other["id"]) not in
                                                {change["lineId"] for change in changes
                                                 if change["change"] == "deleted"}])):
            change = ("deleted", "delete_transport_line", {"lineId": line_id})
        elif (str(line.get("name") or "").startswith(RELIEF_PREFIX) and buses
              and state["wake"] - record["since"] >= LINE_REVIEW_AFTER and riders / buses < LOW_RIDERS_PER_BUS):
            if int(line.get("budget") or 100) > 50:
                change = ("halved", "set_transport_line", {"lineId": line_id, "budget": 50})
            elif state["wake"] - record.get("halvedAt", record["since"]) >= LINE_REVIEW_AFTER:
                change = ("deleted", "delete_transport_line", {"lineId": line_id})
        if change is None:
            continue
        outcome = protocol.run(f"line-{change[0]}-{line_id}-{label}", change[1], change[2])
        if change[0] == "halved" and outcome["ok"]:
            record["halvedAt"] = state["wake"]
        changes.append({"lineId": line_id, "change": change[0], "riders": riders, "buses": buses,
                        "detour": detour(line), **({"notConnected": True} if line_id in broken else {}),
                        "ok": outcome["ok"], "regressionOk": outcome.get("regressionOk"),
                        **({"error": str(outcome.get("error"))[:200]} if not outcome["ok"] else {})})
    deleted = {str(change["lineId"]) for change in changes if change["change"] == "deleted" and change["ok"]}
    standing = {str(int(line["id"])) for line in lines} - deleted
    state["lines"] = {key: value for key, value in records.items() if key in standing}
    return changes


def transit(label: str, hotspot: dict[str, Any]) -> dict[str, Any] | None:
    """The best previewed bus line from the car trips of the congested roads that stops within
    TRANSIT_REACH of this hotspot. A city with no bus depot gets one first, by the middle stop of
    the nearest line, when the depot is all its preview lacks: no line runs without one (live: all
    three relief lines failed their preview for want of a depot, and the hotspot stayed six wakes).
    A city that has a depot the line still cannot use gets no second one. No line is added while
    the relief lines running carry too few riders a bus (relief_underused): more buses would be
    more traffic; nor past MAX_RELIEF_LINES, nor one whose stops other lines mostly serve."""
    lines = _bus_lines()
    if relief_underused(lines) or sum(1 for line in lines if str(line.get("name") or "")
                                      .startswith(RELIEF_PREFIX)) >= MAX_RELIEF_LINES:
        return None
    served = [stop for line in lines for stop in _stops(line)]
    proposals = mt.call_tool("propose_transit_lines", {"hotspotLimit": 3, "corridorLimit": 3})["proposals"]
    center = hotspot["center"]
    near = [proposal for proposal in proposals if len(proposal.get("stops") or []) >= 2 and any(
        ((stop["x"] - center["x"]) ** 2 + (stop["z"] - center["z"]) ** 2) ** 0.5 < TRANSIT_REACH
        for stop in proposal["stops"])
        and overlap([(stop["x"], stop["z"]) for stop in proposal["stops"]], served) < NEW_OVERLAP]
    depot = None
    for proposal in near:
        preview = proposal.get("preview") or {}
        stops = proposal["stops"]
        if not (preview.get("ok") and preview.get("valid", True)):
            if depot is not None or not _wants_only_a_depot(_line_preview(stops)) or _has_depot():
                continue
            middle = stops[len(stops) // 2]
            depot = services.place_near(f"depot-{label}", DEPOT_PREFAB, {"x": middle["x"], "z": middle["z"]})
            if not depot.get("ok"):
                return {"ok": False, "stage": "depot", "depot": depot}
            if not _line_preview(stops).get("valid"):
                continue
        outcome = protocol.run(f"transit-{label}", "create_transport_line",
                               {"transportType": "Bus", "stops": stops, "completeLoop": True,
                                "name": f"Relief {label}"})
        result = outcome.get("result") or {}
        return {"ok": outcome["ok"], "regressionOk": outcome["regressionOk"],
                "lineId": result.get("lineId"), "stops": len(stops), "carTrips": proposal.get("carTrips"),
                **({"depot": depot.get("buildingId")} if depot else {})}
    return {"ok": False, "stage": "depot", "depot": depot} if depot else None


def worth_building(candidate: dict[str, Any]) -> bool:
    vehicles = float(candidate.get("servesVehicles") or 0)
    cleared = len(candidate.get("clearGrowables") or [])
    return vehicles >= BYPASS_MIN_VEHICLES and vehicles >= BYPASS_VEHICLES_PER_CLEARED * cleared


def bypass(city: City, label: str, hotspot: dict[str, Any]) -> dict[str, Any] | None:
    """Build the best buildable connector around the hotspot (propose_bypass_roads) that carries
    enough of its traffic for what it clears (worth_building): clear the growables under it after
    their own bulldoze preview, then build its pieces in order."""
    proposal = mt.call_tool("propose_bypass_roads", {"limit": 5, "candidateLimit": 3})
    match = next((item for item in proposal["hotspots"] if same(item["segmentIds"], hotspot["segmentIds"])), None)
    candidate = next((item for item in (match or {}).get("candidates", []) if item["buildable"]
                      and worth_building(item)), None)
    if candidate is None:
        return None
    clearing = roads.clear_buildings("bypass", candidate["clearGrowables"])
    cleared = clearing["cleared"]
    if not clearing["ok"]:
        return {"ok": False, "stage": "clearing", "blocked": clearing["blocked"], "cleared": cleared}
    built, start = [], (candidate["from"]["x"], candidate["from"]["z"])
    for index, piece in enumerate(candidate["pieces"]):
        outcome = roads.build(f"bypass-{label}-{index}", "Basic Road", start,
                              (piece["end"]["x"], piece["end"]["z"]), city)
        if not outcome["ok"]:
            return {"ok": False, "stage": f"piece{index}", "built": built, "cleared": cleared}
        built.append(outcome.get("segmentId"))
        start = (outcome["end"]["x"], outcome["end"]["z"])
    return {"ok": True, "segments": built, "cleared": cleared, "length": candidate["length"],
            "detour": candidate["detour"], "servesVehicles": candidate["servesVehicles"]}


def _point(segment: dict[str, Any], end: str) -> tuple[float, float]:
    return float(segment[end]["x"]), float(segment[end]["z"])


def outside_route(segments: list[dict[str, Any]], center: dict[str, float]) -> list[dict[str, Any]]:
    """The local roads every outside trip toward this hotspot shares, nearest the highway first: the
    shortest way from each highway node a town road joins (the ramp stubs) to the road node nearest
    the hotspot, within OUTSIDE_LINK_REACH of the highway. So the link roads come first, then the
    trunk past the link junction. Found from the network, not from the growth record: an upgrade
    gives a segment a new id, so recorded road ids go stale (live: two of the three had)."""
    highway_nodes = {int(s[f"{end}NodeId"]) for s in segments if "Highway" in str(s.get("prefab"))
                     for end in ("start", "end")}
    local = [s for s in segments if s.get("service") == "Road" and "Highway" not in str(s.get("prefab"))]
    graph: dict[int, list[tuple[int, dict[str, Any], float]]] = {}
    places: dict[int, tuple[float, float]] = {}
    for segment in local:
        a, b = int(segment["startNodeId"]), int(segment["endNodeId"])
        length = math.dist(_point(segment, "start"), _point(segment, "end"))
        graph.setdefault(a, []).append((b, segment, length))
        graph.setdefault(b, []).append((a, segment, length))
        places[a], places[b] = _point(segment, "start"), _point(segment, "end")
    sources = sorted(node for node in graph if node in highway_nodes)
    if not sources:
        return []
    target = min(places, key=lambda node: math.dist(places[node], (center["x"], center["z"])))
    starts: dict[int, tuple[float, dict[str, Any]]] = {}
    for source in sources:
        distance, previous, queue = {source: 0.0}, {}, [(0.0, source)]
        while queue:
            here, node = heapq.heappop(queue)
            if node == target:
                break
            if here > distance[node]:
                continue
            for other, segment, length in graph[node]:
                if here + length < distance.get(other, math.inf):
                    distance[other] = here + length
                    previous[other] = (node, segment)
                    heapq.heappush(queue, (here + length, other))
        if target not in distance:
            continue
        node = target
        while node != source:
            node, segment = previous[node]
            start = distance[node]
            if start < OUTSIDE_LINK_REACH and start < starts.get(int(segment["id"]), (math.inf,))[0]:
                # fromNode: the end nearer the highway on this way in; routeStart 0 marks a link road
                starts[int(segment["id"])] = (start, {**segment, "fromNode": node, "routeStart": round(start, 1)})
    ordered = sorted(starts.values(), key=lambda item: (item[0], int(item[1]["id"])))
    return [segment for _, segment in ordered][:OUTSIDE_LINK_MAX_SEGMENTS]


def select_outside_link(route: list[dict[str, Any]],
                        preview: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
    """The route's segments to widen, nearest the highway first, and the growables to clear first.
    A segment already wide or one-way is passed over (a one-way link is not ours to make two-way).
    A way in ends at its first segment whose upgrade preview fails, that would cross a power line
    (a ground road across one releases it), run over anything but a growable (a service building,
    a prop), or take the clearing past OUTSIDE_LINK_MAX_CLEARED buildings or OUTSIDE_LINK_MAX_HOMES
    homes: a segment is only taken when the widened road reaches it unbroken from the highway. The
    other way in goes on (live: the trunk past the junction would clear 11, yet the off-ramp's second
    link road, at density 100, cleared nothing more than the first)."""
    chosen: list[int] = []
    clear: dict[int, dict[str, Any]] = {}
    reached = {int(segment["fromNode"]) for segment in route if not segment.get("routeStart")}
    passed, stopped = [], []
    for segment in route:
        sid, prefab = int(segment["id"]), str(segment.get("prefab"))
        if int(segment["fromNode"]) not in reached:
            continue  # beyond a segment that was not widened
        onward = int(segment["endNodeId"]) if int(segment["startNodeId"]) == int(segment["fromNode"])             else int(segment["startNodeId"])
        if prefab.startswith(WIDE_PREFIXES) or segment.get("isOneWay"):
            passed.append({"segmentId": sid, "reason": "oneWay" if segment.get("isOneWay") else "wide"})
            reached.add(onward)
            continue
        check = preview(segment)
        under = check.get("under") or []
        new = {int(item["id"]): item for item in under if int(item["id"]) not in clear}
        homes = sum(1 for item in list(clear.values()) + list(new.values()) if item.get("service") == "Residential")
        reason: dict[str, Any] | None = None
        if not check.get("valid"):
            reason = {"preview": str(check.get("error") or "invalid")[:160]}
        elif check.get("powerLines"):
            reason = {"powerLines": check["powerLines"]}
        elif any(item.get("service") not in roads.GROWABLE for item in under):
            reason = {"blocking": [int(item["id"]) for item in under if item.get("service") not in roads.GROWABLE]}
        elif len(clear) + len(new) > OUTSIDE_LINK_MAX_CLEARED or homes > OUTSIDE_LINK_MAX_HOMES:
            reason = {"wouldClear": len(clear) + len(new), "homes": homes}
        if reason:
            stopped.append({"segmentId": sid, **reason})
            continue
        chosen.append(sid)
        clear.update(new)
        reached.add(onward)
    return {"segments": chosen, "clear": sorted(clear),
            "homes": sum(1 for item in clear.values() if item.get("service") == "Residential"),
            "passed": passed, "stoppedAt": stopped[:4]}


def _upgrade_preview(segment_id: int, prefab: str) -> dict[str, Any]:
    try:
        result = mt._request("POST", "/commands/upgrade-road",
                             body={"segmentId": segment_id, "roadPrefab": prefab, "dryRun": True})
    except mt.BridgeRequestError as error:
        return {"valid": False, "error": str(error)[:160]}
    return {"valid": result.get("ok") is not False and result.get("valid") is not False,
            "cost": result.get("cost"), "error": result.get("error")}


def _link_preview(segment: dict[str, Any], prefab: str) -> dict[str, Any]:
    """The native upgrade preview, and what the wider road would run over: preview_network of the
    wider prefab along the segment, in two chords through its middle so a curve is followed. Its own
    tool errors (the old road is in the way) do not count; the buildings and power lines under it do."""
    check = _upgrade_preview(int(segment["id"]), prefab)
    if not check["valid"]:
        return check
    under: dict[int, dict[str, Any]] = {}
    lines = 0
    middle = segment.get("middle") or {"x": (segment["start"]["x"] + segment["end"]["x"]) / 2,
                                       "z": (segment["start"]["z"] + segment["end"]["z"]) / 2}
    for a, b in ((segment["start"], middle), (middle, segment["end"])):
        try:
            result = mt.call_tool("preview_network", {"roadPrefab": prefab, "start": {"x": a["x"], "z": a["z"]},
                                                      "end": {"x": b["x"], "z": b["z"]}})
        except mt.BridgeRequestError as error:
            return {"valid": False, "error": f"chord: {str(error)[:150]}"}
        for item in result.get("buildingsUnderRoad") or []:
            under[int(item["id"])] = {"id": int(item["id"]), "service": item.get("service"),
                                      "prefab": item.get("prefab")}
        lines += len(result.get("powerLinesCrossed") or [])
    return {**check, "under": list(under.values()), "powerLines": lines}


def _link_prefab(route: list[dict[str, Any]]) -> str | None:
    """The widest unlocked prefab the first narrow two-way segment of the route upgrades to."""
    first = next((s for s in route if not str(s.get("prefab")).startswith(WIDE_PREFIXES) and not s.get("isOneWay")),
                 None)
    if first is None:
        return None
    return next((prefab for prefab in OUTSIDE_LINK_PREFABS if _upgrade_preview(int(first["id"]), prefab)["valid"]),
                None)


def outside_link(label: str, hotspot: dict[str, Any]) -> dict[str, Any]:
    """Widen the town's link to the highway toward this hotspot (outside_route, select_outside_link):
    clear the growables the wider road runs over, each after its own bulldoze preview, then upgrade
    the segments as one chain (widen_road_chain: native preview per segment, zoning repaired or its
    unsupported growables cleared once the chain is done). Upgrades keep the nodes, so the link stays
    joined to the highway; a road cut off from the outside afterwards still fails the action.
    ok None when there is nothing to widen (the reason is kept)."""
    segments = mt._request("GET", "/state/networks", query={"limit": 20000}).get("segments", [])
    route = outside_route(segments, hotspot["center"])
    prefab = _link_prefab(route)
    if prefab is None:
        return {"ok": None, "reason": "noUpgrade" if route else "noRoute", "route": [int(s["id"]) for s in route]}
    selection = select_outside_link(route, lambda segment: _link_preview(segment, prefab))
    summary = {"prefab": prefab, "route": [int(s["id"]) for s in route], **selection}
    if not selection["segments"]:
        return {"ok": None, "reason": "nothingSelectable", **summary}
    nodes = {frozenset((int(s["startNodeId"]), int(s["endNodeId"]))) for s in route
             if int(s["id"]) in selection["segments"]}
    _, isolated_before = network.road_components(segments)
    if selection["clear"]:
        clearing = roads.clear_buildings(f"outside-{label}", selection["clear"])
        summary["cleared"] = clearing["cleared"]
        if not clearing["ok"]:
            return {"ok": False, "stage": "clearing", "blocked": clearing["blocked"], **summary}
    outcome = protocol.run(f"outside-link-{label}", "widen_road_chain",
                           {"segmentIds": selection["segments"], "roadPrefab": prefab})
    after = mt._request("GET", "/state/networks", query={"limit": 20000}).get("segments", [])
    upgraded = [int(s["id"]) for s in after if frozenset((int(s["startNodeId"]), int(s["endNodeId"]))) in nodes
                and str(s.get("prefab")) == prefab]
    _, isolated_after = network.road_components(after)
    cut_off = sorted(isolated_after - isolated_before)[:20]
    return {**summary, "ok": bool(outcome["ok"]) and bool(upgraded) and not cut_off,
            "regressionOk": outcome["regressionOk"], "upgraded": upgraded, "cutOff": cut_off,
            "error": (outcome.get("result") or {}).get("error")}


def act(city: City, label: str, state: dict[str, Any], key: str, hotspot: dict[str, Any],
        remedy: dict[str, Any], summary: dict[str, Any]) -> dict[str, Any] | None:
    """Execute one candidate of the remedy kind; None when this kind has nothing left to try."""
    kind, candidates = remedy["kind"], remedy.get("candidates")
    done = {(a["key"], a["kind"], json.dumps(a["detail"].get("target"), sort_keys=True)) for a in state["actions"]
            if state["wake"] - a["wake"] < RETRY_AFTER or a["verdict"] is None}

    def untried(target: Any) -> bool:
        return (key, kind, json.dumps(target, sort_keys=True)) not in done

    def junction(candidate: dict[str, Any], watch: list[int]) -> dict[str, Any]:
        outcome, summary["trial"] = trial(label, candidate, watch)
        state["tried"][str(candidate["nodeId"])] = {"kind": candidate["kind"], "ok": outcome["ok"],
                                                    "reverted": summary["trial"]["reverted"], "label": label}
        verdict = "failed" if summary["trial"]["reverted"] or not outcome["ok"] else "worked"
        return record(state, key, kind, hotspot, {"target": candidate["nodeId"]}, verdict)

    if kind == "junctionControl":
        trials = [j for j in candidates or [] if str(j["nodeId"]) not in state["tried"]]
        spent = sum(1 for j in candidates or [] if str(j["nodeId"]) in state["tried"])
        if not trials or spent >= MAX_JUNCTION_TRIALS:
            return None
        return junction(trials[0], hotspot["segmentIds"])
    if kind == "relieveDownstream":
        candidates = candidates or {}
        downstream = candidates.get("junctionTrial")
        if downstream and str(downstream["nodeId"]) not in state["tried"]:
            return junction(downstream, hotspot["segmentIds"] + [candidates["exitSegmentId"]])
        upgrades = candidates.get("upgradeCandidates") or []
        target = sorted(c["segmentId"] for c in upgrades)
        if upgrades and untried(target):
            summary["widening"] = widen(label, upgrades)
            return record(state, key, kind, hotspot, {"target": target, "widening": summary["widening"]})
        return None
    if kind == "widen":
        target = sorted(c["segmentId"] for c in candidates or [])
        if not target or not untried(target) or key in state["widened"]:
            return None
        summary["widening"] = widen(label, candidates)
        state["widened"][key] = summary["widening"]
        return record(state, key, kind, hotspot, {"target": target, "widening": summary["widening"]})
    if kind == "transit":
        if not untried("line"):
            return None
        line = transit(label, hotspot)
        if line is None:
            return record(state, key, kind, hotspot, {"target": "line", "none": True}, "failed")
        summary["transit"] = line
        return record(state, key, kind, hotspot, {"target": "line", **line})
    if kind == "bypass":
        if not untried("connector"):
            return None
        road = bypass(city, label, hotspot)
        if road is None:
            return record(state, key, kind, hotspot, {"target": "connector", "none": True}, "failed")
        summary["bypass"] = road
        return record(state, key, kind, hotspot, {"target": "connector", **road}, None if road["ok"] else "failed")
    if kind == "metro":
        if not untried("metro"):
            return None
        built = metro.build_line(label, hotspot, len(state.setdefault("metroLines", [])), services.cash())
        if built is None:
            return None
        if built["ok"]:
            state["metroLines"].append({"wake": state["wake"], "key": key, "lineId": built["lineId"]})
        summary["metro"] = built
        return record(state, key, kind, hotspot, {"target": "metro", **built}, None if built["ok"] else "failed")
    if kind == "highwayExit":
        if not untried("exit"):
            return None
        exit_ramp = ramps.build_exit(label, hotspot, len(state.setdefault("exits", [])))
        if exit_ramp is None:
            return None
        if exit_ramp["ok"]:
            state["exits"].append({"wake": state["wake"], "key": key, "node": exit_ramp["node"]})
        summary["highwayExit"] = exit_ramp
        return record(state, key, kind, hotspot, {"target": "exit", **exit_ramp},
                      None if exit_ramp["ok"] else "failed")
    if kind == "outsideLink":
        linked = state.setdefault("outsideLinked", {})
        if key in linked or not untried("link"):
            return None
        link = outside_link(label, hotspot)
        if link["ok"] is None:
            # nothing to widen within the caps: the kind is spent and the hotspot goes on to the planner
            summary["outsideLinkNone"] = {item: link.get(item) for item in ("reason", "prefab", "stoppedAt")}
            return None
        summary["outsideLink"] = link
        if link["ok"]:
            linked[key] = {"wake": state["wake"], "upgraded": link["upgraded"], "prefab": link["prefab"]}
        return record(state, key, kind, hotspot, {"target": "link", **link}, None if link["ok"] else "failed")
    return None


def watch(city: City, label: str) -> dict[str, Any]:
    state = {**DEFAULT_STATE, **city.load_state("traffic", {})}
    state["wake"] += 1
    proposal = mt.call_tool("propose_traffic_remedies", {"limit": 5})
    hotspots = proposal["hotspots"]
    tracked = []
    for hotspot in hotspots:
        previous = next((item for item in state["hotspots"] if same(item["segmentIds"], hotspot["segmentIds"])), None)
        tracked.append({"segmentIds": hotspot["segmentIds"], "seen": (previous or {"seen": 0})["seen"] + 1,
                        "key": (previous or {}).get("key") or f"hotspot-{min(hotspot['segmentIds'])}"})
    state["hotspots"] = tracked  # a hotspot that cleared this wake starts over
    summary: dict[str, Any] = {"status": proposal["status"], "flowRatio": proposal["flowRatio"],
                               "hotspots": [(len(item["segmentIds"]), item["seen"]) for item in tracked]}
    # each hotspot's truck share and whether its diagnosis offers cargo rail, for loop.cargo
    summary["cargo"] = [{"key": item["key"], "seen": item["seen"],
                         "cargoShare": (hotspot.get("diagnosis") or {}).get("cargoShare"),
                         "cargoRail": any(r.get("kind") == "cargoRail" for r in hotspot.get("remedies") or [])}
                        for hotspot, item in zip(hotspots, tracked)]
    verdicts = judge(state, hotspots, tracked, label)
    if verdicts:
        summary["judged"] = verdicts
    lines = review_lines(state, label)
    if lines:
        summary["lines"] = lines
    metro_ready: bool | None = None  # read once a hotspot asks for remedies
    persistent = sorted(((h, t) for h, t in zip(hotspots, tracked) if t["seen"] >= PERSIST),
                        key=lambda pair: -pair[0]["meanDensity"] * pair[0]["segmentCount"])
    for hotspot, item in persistent:
        key = item["key"]
        if state["wake"] - state["spent"].get(key, -RETRY_AFTER) < RETRY_AFTER:
            continue
        remedies = {remedy["kind"]: remedy for remedy in hotspot["remedies"]}
        remedies.setdefault("bypass", {"kind": "bypass", "reason": "structural fallback", "candidates": None})
        action = None
        if metro_ready is None:
            metro_ready = metro.available()
        for kind in kinds_for(hotspot, remedies, state["kindStats"], metro_ready):
            action = act(city, label, state, key, hotspot, remedies[kind], summary)
            if action:
                summary["action"] = {"key": key, "kind": kind, "verdict": action["verdict"],
                                     "reason": remedies[kind]["reason"]}
                break
        if action:
            break  # one action a wake: trials run the simulation for game months
        state["spent"][key] = state["wake"]
        if state["wake"] - state["escalatedAt"].get(key, -ESCALATE_EVERY) >= ESCALATE_EVERY:
            state["escalatedAt"][key] = state["wake"]
            summary["escalation"] = {"key": key, "segments": hotspot["segmentCount"],
                                     "meanDensity": hotspot["meanDensity"], "center": hotspot["center"],
                                     "diagnosis": hotspot["diagnosis"],
                                     "planning": [r for r in hotspot["remedies"] if r["kind"] not in EXECUTABLE]}
            break
    city.save_state("traffic", state)
    return summary

"""Bounded connected-road search over current native terrain samples.

Plans are conditional: previews cannot model junctions that do not exist yet.
No mutation, terrain relaxation, road removal, or scenario-specific coordinates.
"""
import heapq
import json
import math
import time
from collections import Counter


class PreviewBudgetExhausted(Exception):
    pass


class PreviewBudget:
    """One budget and exact-result cache for a whole planning invocation.

    Cache lifetime is deliberately one paused live-state read, never a save,
    reload or mutation. Orientation and hypothetical heights are significant.
    """
    def __init__(self, preview, accepted, maximum=96, seconds=30, progress=None):
        self.preview, self.accepted = preview, accepted
        self.maximum, self.seconds = maximum, seconds
        self.started = time.monotonic()
        self.cache, self.calls, self.cache_hits = {}, 0, 0
        self.rejections = Counter()
        self.progress = progress
        self.stop_reason = None

    def summary(self):
        return {'nativePreviewCalls': self.calls, 'maximumNativePreviews': self.maximum,
                'budgetFullyConsumed': self.calls >= self.maximum,
                'budgetLimitReached': self.calls >= self.maximum,
                'cacheHits': self.cache_hits, 'elapsedSeconds': round(time.monotonic()-self.started, 3),
                'maximumSeconds': self.seconds, 'stopReason': self.stop_reason,
                'rejectionLayers': dict(self.rejections)}

    def __call__(self, candidate):
        key = json.dumps({k: v for k, v in candidate.items() if k != 'name'}, sort_keys=True, allow_nan=False)
        if key in self.cache:
            self.cache_hits += 1
            return self.cache[key]
        if time.monotonic()-self.started >= self.seconds:
            self.stop_reason = 'timeBudget'
            raise PreviewBudgetExhausted()
        if self.calls >= self.maximum:
            self.stop_reason = 'nativePreviewBudget'
            raise PreviewBudgetExhausted()
        self.calls += 1
        if self.progress:
            self.progress({**self.summary(), 'status': 'requesting', 'candidate': candidate})
        try:
            result = self.preview(candidate)
        except Exception as error:
            self.stop_reason = 'bridgeError'
            if self.progress:
                self.progress({**self.summary(), 'status': 'error', 'error': str(error)})
            raise
        self.cache[key] = result
        if result.get('excludedPreviousFailure'):
            self.calls -= 1  # Adapter rejected locally; no native request.
        if not self.accepted(result):
            native = result.get('toolErrors')
            layer = ('nativeEngine' if native and native != 'None' else
                     'failureHistory' if result.get('excludedPreviousFailure') else
                     'previewEligibility' if result.get('reason') == 'futureNodeHeightEligibility' else
                     'predictionOrAcceptancePolicy')
            self.rejections[layer] += 1
        if self.progress:
            self.progress({**self.summary(), 'status': 'received', 'candidate': candidate,
                           'nativeErrors': result.get('toolErrors'), 'accepted': self.accepted(result)})
        return result


def point(p):
    return (float(p['x']), float(p['z']))


def vector(a, b):
    return (b[0] - a[0], b[1] - a[1])


def unit(v):
    length = math.hypot(*v)
    if not math.isfinite(length) or length < 1e-6:
        raise ValueError('Missing finite native road direction')
    return (v[0] / length, v[1] / length)


def turn(a, b):
    a, b = unit(a), unit(b)
    return math.degrees(math.acos(max(-1, min(1, a[0]*b[0] + a[1]*b[1]))))


def unlocked_bounds(areas):
    """CS1 base-game 5x5 tile protocol; ownership always comes from live data."""
    if areas.get('ok') is not True or areas.get('totalAreaCount') != 25:
        raise ValueError('Unsupported or missing live build-area grid')
    result = []
    for tile in areas['areas']:
        if tile.get('unlocked') is True:
            x, z = tile['x'], tile['z']
            if not (0 <= x < 5 and 0 <= z < 5):
                raise ValueError('Invalid live tile index')
            result.append(((x-2)*1920-960, (z-2)*1920-960,
                           (x-2)*1920+960, (z-2)*1920+960))
    if not result:
        raise ValueError('No verified unlocked area')
    return result


def line_in_bounds(a, b, bounds, margin=0):
    """Cover the complete line by a union of clipped tile intervals.

    A negative result is a conservative planning rejection, never a native
    OutOfArea diagnosis. Shrinking each tile can reject a seam crossing.
    """
    intervals = []
    for left, bottom, right, top in bounds:
        low, high = 0., 1.
        for start, delta, minimum, maximum in (
            (a[0], b[0]-a[0], left+margin, right-margin),
            (a[1], b[1]-a[1], bottom+margin, top-margin)):
            if abs(delta) < 1e-12:
                if not minimum <= start <= maximum:
                    high = -1
                    break
            else:
                first, last = sorted(((minimum-start)/delta, (maximum-start)/delta))
                low, high = max(low, first), min(high, last)
        if high >= low:
            intervals.append((low, high))
    covered = 0.
    for low, high in sorted(intervals):
        if low > covered + 1e-9:
            return False
        covered = max(covered, high)
    return covered >= 1-1e-9


def segment_distance(a, b, c, d):
    def cross(v, w):
        return v[0]*w[1]-v[1]*w[0]
    ab, cd = vector(a, b), vector(c, d)
    if (cross(ab, vector(a, c))*cross(ab, vector(a, d)) < 0 and
            cross(cd, vector(c, a))*cross(cd, vector(c, b)) < 0):
        return 0.
    def distance(p, q, r):
        v, w = vector(q, r), vector(q, p)
        denominator = v[0]**2+v[1]**2
        t = max(0, min(1, (v[0]*w[0]+v[1]*w[1])/denominator)) if denominator else 0
        return math.dist(p, (q[0]+t*v[0], q[1]+t*v[1]))
    return min(distance(a, c, d), distance(b, c, d), distance(c, a, b), distance(d, a, b))


def run_progressive_trials(trials, search, initial_limit=24, deeper_limits=(48, 72, 96),
                           should_stop=lambda: False,
                           is_acceptable=lambda result: result.get('found')):
    """Give every orientation a base search before deepening promising ones.

    The callback receives ``(trial, local_preview_limit)``. Records retain all
    pass diagnostics and attempts while their latest result drives ranking.
    """
    records = []

    def run(record, limit):
        result = search(record['trial'], limit)
        # A larger pass deterministically replays the smaller pass through the
        # shared native cache, so its attempts already contain that prefix.
        record['attempts'] = result.get('attempts', [])
        record['result'] = result
        record['searchPasses'].append({key: result.get(key) for key in (
            'found', 'candidateCount', 'expansions', 'acceptedDepthCounts',
            'candidateStreamDiagnostics',
            'localMaximumPreviews', 'localStopReason', 'queueRemaining',
            'queuedDepthCounts', 'poppedDepthCounts', 'deepestPoppedDepth',
            'deepestQueuedDepth', 'schedulerPopCounts', 'goalEdgeQueued', 'goalEdgePops',
            'goalEligibilityCounts', 'goalReserveRejections',
            'goalConnectorQueued', 'goalConnectorPops',
            'goalApproachPreviews', 'goalSeedPreviews', 'goalSeedAccepted',
            'goalConnectorAcceptedTransitions', 'goalConnectorRejectedTransitions',
            'goalConnectorRejectionCounts',
            'distinctOrientedStatesQueued', 'duplicateOrientedStatesQueued',
            'reverseSkeletonMatches', 'reverseSkeletonStatesQueued',
            'reverseSkeletonStatePops', 'reverseSkeletonAcceptedTransitions',
            'reverseSkeletonRejectedTransitions',
            'reverseSkeletonRejectionCounts')})
        return result

    for index, trial in enumerate(trials):
        if should_stop():
            break
        record = {'trial': trial, 'trialIndex': index, 'attempts': [], 'searchPasses': []}
        records.append(record)
        if is_acceptable(run(record, initial_limit)):
            return records

    for limit in deeper_limits:
        promising = [record for record in records
                     if record['result'].get('localStopReason') == 'trialPreviewBudget'
                     and record['result'].get('queueRemaining', 0) > 0]
        promising.sort(key=lambda record: (
            -max(map(int, record['result'].get('acceptedDepthCounts', {})), default=0),
            -sum(record['result'].get('acceptedDepthCounts', {}).values()),
            -record['result'].get('queueRemaining', 0),
            record['trialIndex']))
        for record in promising:
            if should_stop():
                return records
            if is_acceptable(run(record, limit)):
                return records
    return records


def search_corridor(first, second, cells, bounds, width, prefab, preview,
                    accepted, max_previews=96, max_segments=8, route_points=None,
                    maximum_total_cost=None):
    """A* with turn-aware states and native-previewed complete connected paths.

    First/second are exact live nodes with outward native segment directions.
    Interior anchors are exact live terrain samples. No independent-side
    success can produce a plan. Native previews remain cached by orientation.
    """
    if not math.isfinite(width) or width <= 0:
        raise ValueError('Native road width required')
    start, goal = point(first['position']), point(second['position'])
    start_direction = None if first.get('junctionDirections') else unit(point(first['direction']))
    arrival_direction = None if second.get('junctionDirections') else tuple(-v for v in unit(point(second['direction'])))
    points = [start, goal]
    seen = set(points)
    for cell in cells:
        p = point(cell['position'])
        if (p not in seen and all(math.isfinite(v) for v in p)
                and cell.get('unlocked') is True and cell.get('buildable') is True
                and line_in_bounds(p, p, bounds, width/2)):
            points.append(p)
            seen.add(p)
    minimum, maximum = max(32., width*2), max(96., width*4)
    maximum_turn_degrees = 90.
    counts, turn_counts, goal_eligibility, stream_counts, cache, attempts = (
        Counter(), Counter(), Counter(), Counter(), {}, [])
    turn_diagnostics = {}
    local_stop_reason = None
    route_indices = None
    if route_points is not None:
        # Hints must match current native endpoints and fresh terrain samples.
        requested = [point(p) for p in route_points]
        if (not 2 <= len(requested) <= max_segments+1 or requested[0] != start
                or requested[-1] != goal or len(set(requested)) != len(requested)
                or any(p not in points for p in requested)):
            return {'found': False, 'candidateCount': 0, 'expansions': 0,
                    'rejections': {'staleRouteHint': 1}, 'attempts': [],
                    'candidateStreamDiagnostics': {
                        'generatedTransitionAttempts': 0,
                        'staticallyEligibleTransitionAttempts': 0,
                        'locallyEvaluatedPreviewVariants': 0,
                        'locallyAcceptedPreviewVariants': 0}}
        route_indices = [points.index(p) for p in requested]
    def reject_turn(reason, angle, threshold, relation):
        counts['turnPolicy'] += 1
        turn_counts[reason] += 1
        existing = turn_diagnostics.get(reason)
        distance = abs(angle-threshold)
        if existing is None or distance < existing['distanceToThresholdDegrees']:
            turn_diagnostics[reason] = {
                'nearestRejectedAngleDegrees': round(angle, 3),
                'thresholdDegrees': threshold,
                'relation': relation,
                'distanceToThresholdDegrees': round(distance, 3),
            }

    def evaluate(a, b, future_y=None, target_lift=None, source_lift=None,
                 is_goal=False, try_target_lifts=False):
        nonlocal local_stop_reason
        # The future shared node height is part of search state and cache key.
        # Preview-only fields cannot be sent to a construction endpoint.
        for u, v in ((a, b), (b, a)):
            planned_side = 'planningStartY' if u == a else 'planningEndY'
            lift_side = 'startTerrainFitLift' if u == b else 'endTerrainFitLift'
            source_lift_side = ('startTerrainFitLift' if u == a
                                else 'endTerrainFitLift')
            key = (u, v,
                   planned_side if future_y is not None else None, future_y,
                   lift_side if target_lift is not None else None, target_lift,
                   source_lift_side if source_lift is not None else None,
                   source_lift)
            if key not in cache:
                if len(cache) >= max_previews:
                    local_stop_reason = 'trialPreviewBudget'
                    return None
                candidate = {'roadPrefab': prefab,
                    'start': dict(zip(('x', 'z'), points[u])),
                    'end': dict(zip(('x', 'z'), points[v])),
                    'name': 'Agent connected outside corridor'}
                if future_y is not None:
                    candidate[planned_side] = future_y
                if target_lift is not None:
                    candidate[lift_side] = target_lift
                if source_lift is not None:
                    candidate[source_lift_side] = source_lift
                try:
                    result = preview(candidate)
                except PreviewBudgetExhausted:
                    local_stop_reason = preview.stop_reason or 'sharedPreviewBudget'
                    return None
                valid = accepted(result)
                stream_counts['locallyEvaluatedPreviewVariants'] += 1
                if future_y is not None and result.get('planningStartNode' if u == a else 'planningEndNode') is not True:
                    valid = False
                    counts['futureNodePreviewUnsupported'] += 1
                samples = (result.get('predictionGeometryEvidence') or {}).get('samples', [])
                if valid and (len(samples) < 3 or any(
                        not line_in_bounds(point(x['position']), point(y['position']), bounds, width/2)
                        for x, y in zip(samples, samples[1:]))):
                    valid = False
                    counts['missingOrOutsideNativeCurve'] += 1
                cache[key] = (candidate, result, valid)
                if valid:
                    stream_counts['locallyAcceptedPreviewVariants'] += 1
                attempts.append({'candidate': candidate, 'preview': result,
                                 'valid': valid, 'goalEdge': is_goal})
                if not valid:
                    counts['nativeOrTerrainPolicy'] += 1
            if cache[key][2]:
                return cache[key]
        # A newly sampled ground-road endpoint may need the Bridge-supported
        # bounded lift to keep the native control curve above uneven terrain.
        # Existing terminals and hypothetical shared nodes are never moved;
        # every lift variant is still accepted only by a fresh native preview.
        if (try_target_lifts and target_lift is None and source_lift is None
                and b not in (0, 1)):
            categories = cached_rejection_categories(a, b, future_y)
            if categories.get('terrainSettlementPolicy', 0):
                for lift in (.1, .2, .3):
                    counts['terrainFitVariantsTried'] += 1
                    lifted = evaluate(a, b, future_y, target_lift=lift,
                                      is_goal=is_goal)
                    if lifted is None:
                        return None
                    if lifted:
                        return lifted
        return False

    def eligible(path, index):
        stream_counts['generatedTransitionAttempts'] += 1
        is_goal = index == 1
        if is_goal:
            goal_eligibility['considered'] += 1
        if index in path:
            if is_goal:
                goal_eligibility['alreadyInPath'] += 1
            return None
        current, target = points[path[-1]], points[index]
        length = math.dist(current, target)
        if not minimum <= length <= maximum:
            if is_goal:
                goal_eligibility['lengthBelowMinimum' if length < minimum
                                 else 'lengthAboveMaximum'] += 1
            return None
        direction = vector(current, target)
        incoming = start_direction if len(path) == 1 else vector(points[path[-2]], current)
        angle = turn(incoming, direction) if incoming else 0
        if angle > maximum_turn_degrees:
            reject_turn('interiorTurnAboveMaximum', angle,
                        maximum_turn_degrees, 'maximum')
            if is_goal:
                goal_eligibility['interiorTurnAboveMaximum'] += 1
            return None
        if index == 1 and arrival_direction:
            arrival_angle = turn(direction, arrival_direction)
            if arrival_angle > maximum_turn_degrees:
                reject_turn('goalArrivalTurnAboveMaximum', arrival_angle,
                            maximum_turn_degrees, 'maximum')
                goal_eligibility['arrivalTurnAboveMaximum'] += 1
                return None
        if len(path) == 1:
            branch_angles = [turn(direction, point(v)) for v in first.get('junctionDirections', [])]
            if any(branch < 30 for branch in branch_angles):
                reject_turn('startJunctionBranchBelowMinimum',
                            max(branch for branch in branch_angles if branch < 30), 30, 'minimum')
                return None
        if index == 1:
            outgoing = tuple(-v for v in direction)
            branch_angles = [turn(outgoing, point(v)) for v in second.get('junctionDirections', [])]
            if any(branch < 30 for branch in branch_angles):
                reject_turn('goalJunctionBranchBelowMinimum',
                            max(branch for branch in branch_angles if branch < 30), 30, 'minimum')
                goal_eligibility['junctionBranchBelowMinimum'] += 1
                return None
        if not line_in_bounds(current, target, bounds, width/2):
            counts['buildAreaPolicy'] += 1
            if is_goal:
                goal_eligibility['buildAreaPolicy'] += 1
            return None
        if any(segment_distance(current, target, points[a], points[b]) < width
               for a, b in zip(path[:-2], path[1:-1])):
            counts['plannedCorridorOverlap'] += 1
            if is_goal:
                goal_eligibility['plannedCorridorOverlap'] += 1
            return None
        if is_goal:
            goal_eligibility['queued'] += 1
        stream_counts['staticallyEligibleTransitionAttempts'] += 1
        return length + angle*.3

    def candidate_stream_diagnostics():
        return {key: stream_counts.get(key, 0) for key in (
            'generatedTransitionAttempts', 'staticallyEligibleTransitionAttempts',
            'locallyEvaluatedPreviewVariants', 'locallyAcceptedPreviewVariants')}

    # Guided multi-heuristic A*: two goal-biased pops, one depth-fair pop, then
    # one standard A* anchor pop. All heaps reference the same pending states,
    # so detours stay available and native previews are never duplicated.
    anchor_queue, guided_queue, deepest_queue, pending = [], [], [], {}
    heuristic_weight = 1.5
    anchor_interval = 4
    serial = 0
    expansions = 0
    depth_counts = Counter()
    popped_depth_counts = Counter()
    scheduler_pop_counts = Counter()
    goal_edge_queued = 0
    goal_edge_pops = 0
    goal_reserve_rejections = 0
    goal_connector_queued = 0
    goal_connector_pops = 0
    goal_approach_previews = 0
    goal_connector_accepted = 0
    goal_connector_rejected = 0
    goal_connector_rejections = Counter()
    oriented_state_signatures = Counter()
    duplicate_oriented_states_queued = 0
    reverse_skeleton_matches = 0
    reverse_skeleton_states_queued = 0
    reverse_skeleton_state_pops = 0
    reverse_skeleton_accepted = 0
    reverse_skeleton_rejected = 0
    reverse_skeleton_rejections = Counter()
    pending_depth_on_stop = None

    def queue_diagnostics():
        queued = Counter(len(item[1]) for item in pending.values())
        remaining = len(pending)
        if pending_depth_on_stop is not None:
            queued[pending_depth_on_stop] += 1
            remaining += 1
        deepest_queued = max(queued, default=0)
        return remaining, dict(queued), deepest_queued

    def pop_pending():
        schedule = ('guided', 'guided', 'deepest', 'anchor')
        preferred = schedule[sum(scheduler_pop_counts.values()) % len(schedule)]
        queues = {'guided': guided_queue, 'deepest': deepest_queue, 'anchor': anchor_queue}
        choices = [(preferred, queues[preferred])] + [
            (label, queues[label]) for label in ('guided', 'deepest', 'anchor')
            if label != preferred]
        for label, queue in choices:
            while queue:
                _, _, candidate_id = heapq.heappop(queue)
                if candidate_id in pending:
                    scheduler_pop_counts[label] += 1
                    return pending.pop(candidate_id)
        raise IndexError('No pending corridor state')

    def cached_rejection_categories(a, b, future_y, target_lift=None,
                                    source_lift=None):
        categories = Counter()
        for u, v in ((a, b), (b, a)):
            planned_side = 'planningStartY' if u == a else 'planningEndY'
            lift_side = 'startTerrainFitLift' if u == b else 'endTerrainFitLift'
            source_lift_side = ('startTerrainFitLift' if u == a
                                else 'endTerrainFitLift')
            key = (u, v,
                   planned_side if future_y is not None else None, future_y,
                   lift_side if target_lift is not None else None, target_lift,
                   source_lift_side if source_lift is not None else None,
                   source_lift)
            cached = cache.get(key)
            if cached is None or cached[2]:
                continue
            result = cached[1]
            tool_error = result.get('toolErrors')
            if tool_error not in (None, '', 'None', 'unknown'):
                category = 'nativeEngine'
            elif result.get('nativeMoveMiddleNodeRisk'):
                category = 'nativeTopologyRisk'
            elif result.get('shortCollinearMergeRisk'):
                category = 'shortCollinearMergeRisk'
            elif (result.get('terrainProfile') or {}).get('safe') is False:
                category = 'terrainSettlementPolicy'
            elif result.get('reason') == 'futureNodeHeightEligibility':
                category = 'futureNodeHeightEligibility'
            else:
                category = 'otherPredictionOrAcceptancePolicy'
            categories[category] += 1
        return categories

    def can_connect_goal_next(path, index):
        if index == 1:
            return False
        current, candidate = points[path[-1]], points[index]
        length = math.dist(candidate, goal)
        if not minimum <= length <= maximum:
            return False
        direction = vector(candidate, goal)
        if turn(vector(current, candidate), direction) > maximum_turn_degrees:
            return False
        if (arrival_direction
                and turn(direction, arrival_direction) > maximum_turn_degrees):
            return False
        outgoing = tuple(-value for value in direction)
        if any(turn(outgoing, point(value)) < 30
               for value in second.get('junctionDirections', [])):
            return False
        if not line_in_bounds(candidate, goal, bounds, width/2):
            return False
        new_path = path+(index,)
        return not any(segment_distance(candidate, goal, points[a], points[b]) < width
                       for a, b in zip(new_path[:-2], new_path[1:-1]))

    # The terrain sampler is intentionally dense so that a selected anchor has
    # an exact live terrain sample.  Expanding every sampled cell from every
    # path state, however, turns that evidence grid into an all-to-all graph and
    # spends the native-preview budget before a useful heading sequence is
    # explored.  Build a small, rotation-invariant motion lattice around the
    # current heading and supplement it with bounded reverse goal skeletons.
    # This is a search policy only: every returned edge still passes eligible(),
    # the native preview, terrain acceptance and later settled verification.
    lattice_turn_degrees = (0., -36., 36., -72., 72.)
    lattice_lengths = tuple(sorted(set((
        minimum,
        max(minimum, minimum * 1.5),
        (minimum + maximum) / 2,
        maximum,
    ))))
    # Keep enough tolerance for a rotated lattice target to select the nearest
    # exact terrain sample, while remaining below one minimum road length.
    anchor_snap_distance = max(width * 1.5, minimum * .75)

    def rotated(direction, degrees):
        radians = math.radians(degrees)
        cosine, sine = math.cos(radians), math.sin(radians)
        return (direction[0]*cosine-direction[1]*sine,
                direction[0]*sine+direction[1]*cosine)

    def nearest_anchor(target, path):
        candidates = ((math.dist(points[index], target), index)
                      for index in range(2, len(points)) if index not in path)
        distance_to_anchor, index = min(candidates, default=(math.inf, None))
        return index if distance_to_anchor <= anchor_snap_distance else None

    def goal_leg_static_eligible(index):
        candidate = points[index]
        length = math.dist(candidate, goal)
        if not minimum <= length <= maximum:
            return False
        direction = vector(candidate, goal)
        if (arrival_direction
                and turn(direction, arrival_direction) > maximum_turn_degrees):
            return False
        outgoing = tuple(-value for value in direction)
        if any(turn(outgoing, point(value)) < 30
               for value in second.get('junctionDirections', [])):
            return False
        return line_in_bounds(candidate, goal, bounds, width/2)

    # Terminal-local sampling can contribute thousands of legal cells. Reverse
    # skeleton scoring is quadratic in those approaches, so retain a bounded
    # nearest-terminal set; farther approaches remain reachable through the
    # ordinary motion lattice and connector scan.
    goal_approach_indices = tuple(sorted(
        (index for index in range(2, len(points))
         if goal_leg_static_eligible(index)),
        key=lambda index: (math.dist(points[index], goal), index))[:256])
    goal_seed_previews = 0
    goal_seed_accepted = 0
    seeded_goal_approaches = []
    # Spend a small bounded prefix on authoritative final-edge evidence before
    # exploring long prefixes. Without this reverse grounding, the planner can
    # consume its full budget reaching approaches whose final native curve is
    # terrain-invalid. Cached seed previews are reused if a connector arrives.
    seed_limit = (min(12, max_previews//16)
                  if route_points is None else 0)
    for approach_index in goal_approach_indices[:seed_limit]:
        seeded = evaluate(approach_index, 1, is_goal=True)
        if seeded is None:
            break
        goal_seed_previews += 1
        if seeded:
            seeded_goal_approaches.append(approach_index)
            goal_seed_accepted += 1
    reverse_goal_approach_indices = (
        tuple(seeded_goal_approaches) if seeded_goal_approaches
        else goal_approach_indices)

    def reverse_skeleton_score(path, index):
        """Return a static three-edge-to-goal score, without claiming safety."""
        if index in path or index == 1 or not reverse_goal_approach_indices:
            return None
        previous, candidate = points[path[-1]], points[index]
        first_length = math.dist(previous, candidate)
        if not minimum <= first_length <= maximum:
            return None
        first_direction = vector(previous, candidate)
        incoming = start_direction if len(path) == 1 else vector(points[path[-2]], previous)
        if incoming and turn(incoming, first_direction) > maximum_turn_degrees:
            return None
        best = math.inf
        for approach_index in reverse_goal_approach_indices:
            if approach_index == index or approach_index in path:
                continue
            approach = points[approach_index]
            middle_length = math.dist(candidate, approach)
            if not minimum <= middle_length <= maximum:
                continue
            middle_direction = vector(candidate, approach)
            final_direction = vector(approach, goal)
            if (turn(first_direction, middle_direction) > maximum_turn_degrees
                    or turn(middle_direction, final_direction) > maximum_turn_degrees
                    or not line_in_bounds(candidate, approach, bounds, width/2)):
                continue
            # Use the same overlap, arrival and terminal-junction policy that
            # will be applied after the first skeleton edge is accepted. This
            # keeps a reported reverse skeleton genuinely connector-reachable.
            if not can_connect_goal_next(path+(index,), approach_index):
                continue
            # A long final native curve is disproportionately sensitive to the
            # fixed existing-terminal height on uneven terrain. Prefer shorter
            # terminal approaches from the live focused samples while keeping
            # total detour cost in the score; preview remains authoritative.
            final_length = math.dist(approach, goal)
            score = first_length + middle_length + 3*final_length
            if score < best:
                best = score
        return best if math.isfinite(best) else None

    def candidate_choices(path):
        if route_indices:
            return [route_indices[len(path)]], set(), 0
        current = points[path[-1]]
        incoming = start_direction if len(path) == 1 else unit(vector(points[path[-2]], current))
        chosen = {1}
        if incoming:
            for degrees in lattice_turn_degrees:
                direction = rotated(incoming, degrees)
                for length in lattice_lengths:
                    target = (current[0]+direction[0]*length,
                              current[1]+direction[1]*length)
                    index = nearest_anchor(target, path)
                    if index is not None:
                        chosen.add(index)

        # Add the best immediately connectable anchors even when terrain-grid
        # snapping omitted them.  They retain the existing strong queue bonus.
        connectors = [index for index in range(2, len(points))
                      if index not in path and can_connect_goal_next(path, index)]
        connectors.sort(key=lambda index: math.dist(current, points[index])
                        + math.dist(points[index], goal))
        chosen.update(connectors[:6])

        # Also seed a few states that have a statically valid two-edge suffix to
        # the goal.  This is the discriminating reverse-reachability check that
        # the previous diagnostics requested; native and terrain checks remain
        # authoritative and can still reject every skeleton edge.
        reverse_scores = []
        for index in range(2, len(points)):
            score = reverse_skeleton_score(path, index)
            if score is not None:
                reverse_scores.append((score, index))
        reverse_scores.sort()
        # Keep geometric diversity near uneven approaches. Limiting this set
        # to only the six shortest skeletons repeatedly selected the same
        # terrain-hostile shortcuts even while the bounded queue still had
        # room for longer, fully policy-compliant alternatives.
        reverse_indices = {index for _, index in reverse_scores[:24]}
        chosen.update(reverse_indices)
        return sorted(chosen), reverse_indices, len(reverse_scores)

    def enqueue(path, cost, edges):
        nonlocal serial, goal_edge_queued, goal_connector_queued
        nonlocal duplicate_oriented_states_queued
        nonlocal reverse_skeleton_matches, reverse_skeleton_states_queued
        choices, reverse_indices, reverse_match_count = candidate_choices(path)
        reverse_skeleton_matches += reverse_match_count
        for index in choices:
            extra = eligible(path, index)
            if extra is None:
                continue
            new_cost = cost + extra
            serial += 1
            remaining = math.dist(points[index], goal)
            depth = len(path)
            goal_edge = index == 1
            goal_connector = can_connect_goal_next(path, index)
            if goal_edge:
                goal_edge_queued += 1
            if goal_connector:
                goal_connector_queued += 1
            signature = (path[-1], index)
            if oriented_state_signatures[signature]:
                duplicate_oriented_states_queued += 1
            oriented_state_signatures[signature] += 1
            if index in reverse_indices:
                reverse_skeleton_states_queued += 1
            reverse_skeleton_state = index in reverse_indices
            pending[serial] = (new_cost, path, index, edges, goal_connector,
                               reverse_skeleton_state)
            heapq.heappush(anchor_queue, (new_cost + remaining, -depth, serial))
            guided_priority = new_cost + heuristic_weight*remaining
            if goal_edge:
                # A statically eligible final edge is the cheapest available
                # proof or disproof of a complete corridor. Preview it before
                # spending the bounded native budget on unrelated branches.
                guided_priority -= maximum*4
            elif goal_connector:
                guided_priority -= maximum*2
            elif reverse_skeleton_state:
                guided_priority -= maximum
            heapq.heappush(guided_queue, (guided_priority, -depth, serial))
            heapq.heappush(deepest_queue, (-depth, new_cost + remaining, serial))

    enqueue((0,), 0., ())
    while pending and expansions < 1000:
        cost, path, index, previous_edges, goal_connector, reverse_skeleton_state = pop_pending()
        if goal_connector:
            goal_connector_pops += 1
        if reverse_skeleton_state:
            reverse_skeleton_state_pops += 1
        popped_depth_counts[len(path)] += 1
        def endpoint_side(value, endpoint_index):
            candidate = value[0]
            return 'start' if point(candidate['start']) == points[endpoint_index] else 'end'
        def shared_height(value):
            candidate, result, _ = value
            side = 'a' if endpoint_side(value, path[-1]) == 'start' else 'd'
            return ((result.get('predictionGeometryEvidence') or {}).get('curve') or {}).get(side, {}).get('y')
        future_y = shared_height(previous_edges[-1]) if previous_edges else None
        if previous_edges and (not isinstance(future_y, (int, float)) or not math.isfinite(future_y)):
            counts['futureNodeHeightMismatchOrUnknown'] += 1
            if reverse_skeleton_state:
                reverse_skeleton_rejected += 1
                reverse_skeleton_rejections['routeStateConsistency'] += 1
            continue
        if index == 1:
            goal_edge_pops += 1
        edge = None
        target_lift = None
        if goal_connector and route_indices is None:
            goal_approach_previews += 1
            budget_stopped = False
            for requested_lift in (None, .1, .2, .3):
                goal_approach = evaluate(index, 1, source_lift=requested_lift,
                                         is_goal=True)
                if goal_approach is None:
                    budget_stopped = True
                    break
                if not goal_approach:
                    goal_connector_rejections.update(
                        cached_rejection_categories(
                            index, 1, None, source_lift=requested_lift))
                    continue
                terrain_fit = goal_approach[1].get('terrainFit') or {}
                lift_key = endpoint_side(goal_approach, index)+'Lift'
                target_lift = terrain_fit.get(lift_key)
                if (not isinstance(target_lift, (int, float))
                        or not math.isfinite(target_lift) or target_lift < 0):
                    counts['goalApproachLiftMissingOrInvalid'] += 1
                    continue
                edge = evaluate(path[-1], index, future_y,
                                target_lift=target_lift,
                                is_goal=index == 1)
                if edge is None:
                    budget_stopped = True
                    break
                if edge:
                    break
                goal_connector_rejections.update(
                    cached_rejection_categories(
                        path[-1], index, future_y, target_lift=target_lift))
            if budget_stopped:
                pending_depth_on_stop = len(path)
                break
        else:
            edge = evaluate(path[-1], index, future_y, is_goal=index == 1,
                            try_target_lifts=reverse_skeleton_state)
            if edge is None:
                pending_depth_on_stop = len(path)
                break
        if not edge:
            if reverse_skeleton_state:
                reverse_skeleton_rejected += 1
                reverse_skeleton_rejections.update(
                    cached_rejection_categories(path[-1], index, future_y, target_lift))
            if goal_connector:
                goal_connector_rejected += 1
                goal_connector_rejections.update(
                    cached_rejection_categories(path[-1], index, future_y, target_lift))
            continue
        if goal_connector:
            goal_connector_accepted += 1
        if reverse_skeleton_state:
            reverse_skeleton_accepted += 1
        if previous_edges:
            actual_y = shared_height(edge)
            if not isinstance(actual_y, (int, float)) or not math.isfinite(actual_y) or abs(actual_y-future_y) > .01:
                counts['futureNodeHeightMismatchOrUnknown'] += 1
                if reverse_skeleton_state:
                    reverse_skeleton_accepted -= 1
                    reverse_skeleton_rejected += 1
                    reverse_skeleton_rejections['routeStateConsistency'] += 1
                continue
        edges = previous_edges+(edge,)
        new_path = path+(index,)
        expansions += 1
        depth_counts[len(new_path)-1] += 1
        if index == 1:
            total_cost = sum(float(item[1]['constructionCost']) for item in edges)/100
            if maximum_total_cost is not None and total_cost > maximum_total_cost:
                goal_reserve_rejections += 1
                counts['goalReservePolicy'] += 1
                continue
            queue_remaining, queued_depth_counts, deepest_queued = queue_diagnostics()
            return {'found': True, 'segments': [e[0] for e in edges],
                'previews': [e[1] for e in edges],
                'traversalPoints': [dict(zip(('x', 'z'), points[i])) for i in new_path],
                'length': sum(math.dist(points[a],points[b]) for a,b in zip(new_path,new_path[1:])),
                'searchCost': cost, 'roadWidth': width, 'minimumSegmentLength': minimum,
                'maximumTurnDegrees': maximum_turn_degrees,
                'minimumJunctionBranchAngleDegrees': 30,
                'candidateCount': len(cache), 'expansions': expansions, 'acceptedDepthCounts': dict(depth_counts),
                'candidateStreamDiagnostics': candidate_stream_diagnostics(),
                'turnPolicyBreakdown': dict(turn_counts), 'turnDiagnostics': turn_diagnostics,
                'localMaximumPreviews': max_previews, 'localStopReason': local_stop_reason,
                'queueRemaining': queue_remaining,
                'queuedDepthCounts': queued_depth_counts,
                'queuePolicy': 'guidedDepthFairAStarWithAnchor',
                'heuristicWeight': heuristic_weight, 'anchorInterval': anchor_interval,
                'schedulerPopCounts': dict(scheduler_pop_counts),
                'poppedDepthCounts': dict(popped_depth_counts),
                'deepestPoppedDepth': max(popped_depth_counts, default=0),
                'deepestQueuedDepth': deepest_queued,
                'goalEdgeQueued': goal_edge_queued,
                'goalEdgePops': goal_edge_pops,
                'goalEligibilityCounts': dict(goal_eligibility),
                'goalReserveRejections': goal_reserve_rejections,
                'goalConnectorQueued': goal_connector_queued,
                'goalConnectorPops': goal_connector_pops,
                'goalApproachPreviews': goal_approach_previews,
                'goalSeedPreviews': goal_seed_previews,
                'goalSeedAccepted': goal_seed_accepted,
                'goalConnectorAcceptedTransitions': goal_connector_accepted,
                'goalConnectorRejectedTransitions': goal_connector_rejected,
                'goalConnectorRejectionCounts': dict(goal_connector_rejections),
                'distinctOrientedStatesQueued': len(oriented_state_signatures),
                'duplicateOrientedStatesQueued': duplicate_oriented_states_queued,
                'reverseSkeletonMatches': reverse_skeleton_matches,
                'reverseSkeletonStatesQueued': reverse_skeleton_states_queued,
                'reverseSkeletonStatePops': reverse_skeleton_state_pops,
                'reverseSkeletonAcceptedTransitions': reverse_skeleton_accepted,
                'reverseSkeletonRejectedTransitions': reverse_skeleton_rejected,
                'reverseSkeletonRejectionCounts': dict(reverse_skeleton_rejections),
                'routeOptimality': 'boundedNotGuaranteed',
                'frontage': {'potentialStraightLength': sum(max(0, math.dist(points[a],points[b])-128)
                    for a,b in zip(new_path,new_path[1:])), 'verifiedZoningCapacity': False},
                'futureJunctionsVerified': False, 'requiresReplanAfterEveryBuild': True,
                'rejections': dict(counts), 'attempts': attempts}
        if len(new_path)-1 < max_segments:
            enqueue(new_path, cost, edges)
    if pending and expansions >= 1000 and local_stop_reason is None:
        local_stop_reason = 'expansionBudget'
    queue_remaining, queued_depth_counts, deepest_queued = queue_diagnostics()
    return {'found': False, 'candidateCount': len(cache), 'expansions': expansions,
            'acceptedDepthCounts': dict(depth_counts), 'rejections': dict(counts),
            'candidateStreamDiagnostics': candidate_stream_diagnostics(),
            'turnPolicyBreakdown': dict(turn_counts), 'turnDiagnostics': turn_diagnostics,
            'attempts': attempts,
            'localMaximumPreviews': max_previews, 'localStopReason': local_stop_reason,
            'queueRemaining': queue_remaining,
            'queuedDepthCounts': queued_depth_counts,
            'queuePolicy': 'guidedDepthFairAStarWithAnchor',
            'heuristicWeight': heuristic_weight, 'anchorInterval': anchor_interval,
            'schedulerPopCounts': dict(scheduler_pop_counts),
            'poppedDepthCounts': dict(popped_depth_counts),
            'deepestPoppedDepth': max(popped_depth_counts, default=0),
            'deepestQueuedDepth': deepest_queued,
            'goalEdgeQueued': goal_edge_queued,
            'goalEdgePops': goal_edge_pops,
            'goalEligibilityCounts': dict(goal_eligibility),
            'goalReserveRejections': goal_reserve_rejections,
            'goalConnectorQueued': goal_connector_queued,
            'goalConnectorPops': goal_connector_pops,
            'goalApproachPreviews': goal_approach_previews,
            'goalSeedPreviews': goal_seed_previews,
            'goalSeedAccepted': goal_seed_accepted,
            'goalConnectorAcceptedTransitions': goal_connector_accepted,
            'goalConnectorRejectedTransitions': goal_connector_rejected,
            'goalConnectorRejectionCounts': dict(goal_connector_rejections),
            'distinctOrientedStatesQueued': len(oriented_state_signatures),
            'duplicateOrientedStatesQueued': duplicate_oriented_states_queued,
            'reverseSkeletonMatches': reverse_skeleton_matches,
            'reverseSkeletonStatesQueued': reverse_skeleton_states_queued,
            'reverseSkeletonStatePops': reverse_skeleton_state_pops,
            'reverseSkeletonAcceptedTransitions': reverse_skeleton_accepted,
            'reverseSkeletonRejectedTransitions': reverse_skeleton_rejected,
            'reverseSkeletonRejectionCounts': dict(reverse_skeleton_rejections),
            'routeOptimality': 'boundedNotGuaranteed',
            'scope': 'boundedSampledCorridorSearchNotProofOfImpossibility'}


def propose_live_corridor(arguments, read, preview, accepted, preferred_route=None, progress=None):
    budget = PreviewBudget(preview, accepted, arguments.get('maxNativePreviews', 96),
                           arguments.get('maxPlanningSeconds', 30), progress)
    result = _propose_live_corridor(arguments, read, budget, accepted, preferred_route)
    # A progressive pass can consume the final preview exactly and return
    # without asking the budget for one more candidate. That is still the
    # effective global stop for a blocked search and must not be reported as
    # ``budgetFullyConsumed: true`` with ``stopReason: null``.
    unfinished_frontier = (result.get('remainingQueuedCandidates', 0) > 0
        or result.get('planningCompletionReason') in (
            'frontierNotExploredWithinSchedule', 'localTrialLimitsReached'))
    if (not result.get('safe') and unfinished_frontier
            and budget.stop_reason is None and budget.calls >= budget.maximum):
        budget.stop_reason = 'nativePreviewBudget'
    result['outcome'] = 'ready' if result.get('safe') else 'blocked'
    result['mutationAuthorized'] = False
    result['searchDiagnostics'] = budget.summary()
    if not result.get('safe'):
        layers = dict(budget.rejections)
        result['candidateRejectionLayers'] = layers
        route_rejections = Counter()
        for report in result.get('corridorReports', []):
            route_rejections.update(report.get('rejections', {}))
        route_policy_keys = {
            'turnPolicy', 'buildAreaPolicy', 'plannedCorridorOverlap', 'staleRouteHint',
        }
        route_policy_count = sum(
            count for name, count in route_rejections.items()
            if name in route_policy_keys)
        prediction_geometry_count = sum(route_rejections.get(name, 0) for name in (
            'futureNodePreviewUnsupported', 'missingOrOutsideNativeCurve'))
        route_state_count = route_rejections.get('futureNodeHeightMismatchOrUnknown', 0)
        reserve_policy_count = route_rejections.get('goalReservePolicy', 0)
        result['contributingLayers'] = {
            **({'routeSearchPolicy': route_policy_count}
               if route_policy_count else {}),
            **({'predictionGeometryPolicy': prediction_geometry_count}
               if prediction_geometry_count else {}),
            **({'routeStateConsistency': route_state_count}
               if route_state_count else {}),
            **({'reservePolicy': reserve_policy_count}
               if reserve_policy_count else {}),
            **{name: count for name, count in layers.items() if count},
        }
        observed_layers = [name for name, count in result['contributingLayers'].items() if count]
        observed_rejecting_layer = (observed_layers[0] if len(observed_layers) == 1 else
            'mixedCandidateRejection' if observed_layers else None)
        if budget.stop_reason:
            result['terminationLayer'] = 'planningBudget'
            result['limitingResource'] = budget.stop_reason
            result['rejectingLayer'] = (observed_rejecting_layer or
                result.get('rejectingLayer', 'routeSearchScheduling'))
            result['provenRootCause'] = None
            result['nextCheck'] = (
                'Measure reverse-reachable complete route skeletons and duplicate oriented states '
                'before changing the preview budget; exhaustion does not prove route impossibility.')
        else:
            goal_summary = result.get('goalEligibilitySummary', {})
            completion = result.get('planningCompletionReason')
            if completion in ('frontierNotExploredWithinSchedule', 'localTrialLimitsReached'):
                result['terminationLayer'] = 'routeSearchScheduling'
                if observed_rejecting_layer:
                    result['rejectingLayer'] = observed_rejecting_layer
                elif goal_summary.get('considered', 0):
                    result['rejectingLayer'] = 'routeSearchPolicy'
                else:
                    result['rejectingLayer'] = 'routeSearchScheduling'
                result['provenRootCause'] = None
                result['nextCheck'] = (
                    'Measure queued goal edges and reverse-reachable complete route skeletons; '
                    'a bounded scheduling stop does not prove route impossibility.')
            elif goal_summary.get('considered', 0) and not goal_summary.get('queued', 0):
                result['terminationLayer'] = 'routeGoalEligibility'
                result['rejectingLayer'] = 'routeSearchPolicy'
                result['provenRootCause'] = None
                result['nextCheck'] = (
                    'Inspect goal-approach length, heading and junction eligibility; '
                    'no explored state queued a goal edge, but sampled-graph impossibility is unproven.')
            elif completion == 'boundedSearchCompleted':
                result['terminationLayer'] = 'boundedSearchCompletion'
                result['rejectingLayer'] = (observed_rejecting_layer or
                    result.get('rejectingLayer', 'previewEvaluation'))
                result['provenRootCause'] = None
            else:
                result['rejectingLayer'] = (observed_rejecting_layer or
                    result.get('rejectingLayer', 'previewEvaluation'))
        result.setdefault('provenRootCause', None)
    if progress:
        final = {**budget.summary(), 'status': 'finished', 'safe': result.get('safe'),
                 'outcome': result['outcome'], 'mutationAuthorized': False}
        for key in ('blocker', 'rejectingLayer', 'candidateRejectionLayers',
                    'terminationLayer', 'planningCompletionReason',
                    'remainingQueuedCandidates', 'goalEligibilitySummary',
                    'candidateStreamDiagnostics', 'goalSearchDiagnostics',
                    'contributingLayers',
                    'provenRootCause', 'limitingResource',
                    'code', 'planningDiagnostics', 'nextCheck'):
            if key in result:
                final[key] = result[key]
        progress(final)
    return result


def _propose_live_corridor(arguments, read, preview, accepted, preferred_route):
    prefab = arguments['roadPrefab']
    assets = read('GET', '/prefabs/roads')
    asset = next((a for a in assets.get('roads', [])
                  if a.get('name') == prefab and a.get('unlocked') is True), None)
    if asset is None:
        return {'ok': True, 'safe': False, 'blocker': 'Requested road is not verified unlocked.',
                'rejectingLayer': 'candidateGeneration', 'code': 'requestedPrefabNotUnlocked',
                'terminationLayer': 'candidateGenerationPrerequisite',
                'provenRootCause': {'scope': 'currentInvocation',
                    'layer': 'candidateGeneration', 'code': 'requestedPrefabNotUnlocked'},
                'nextCheck': 'Refresh loaded and unlocked road prefabs for the current city instance.'}
    width = asset.get('planningWidthMeters')
    if (not isinstance(width, (int, float)) or isinstance(width, bool)
            or not math.isfinite(width) or width <= 0):
        reported_width = width if (isinstance(width, (int, float))
                                   and not isinstance(width, bool) and math.isfinite(width)) else None
        return {'ok': True, 'safe': False,
                'blocker': 'Native planning width for requested prefab is unavailable.',
                'rejectingLayer': 'candidateGeneration',
                'code': 'requestedPrefabGeometryUnavailable',
                'terminationLayer': 'candidateGenerationPrerequisite',
                'provenRootCause': {'scope': 'currentInvocation',
                    'layer': 'candidateGeneration', 'code': 'requestedPrefabGeometryUnavailable'},
                'nextCheck': 'Inspect loaded NetInfo width telemetry for the requested prefab.',
                'planningDiagnostics': {'prefabUnlocked': True,
                    'widthSource': asset.get('widthSource'),
                    'planningWidthRule': asset.get('planningWidthRule'),
                    'planningWidthMeters': reported_width}}
    if asset.get('isBidirectional') is not True:
        return {'ok': True, 'safe': False,
                'blocker': 'Requested road prefab is not verified bidirectional.',
                'rejectingLayer': 'candidateGeneration',
                'code': 'requestedPrefabNotBidirectional',
                'terminationLayer': 'candidateGenerationPrerequisite',
                'provenRootCause': {'scope': 'currentInvocation',
                    'layer': 'candidateGeneration', 'code': 'requestedPrefabNotBidirectional'},
                'nextCheck': 'Select a currently unlocked bidirectional road prefab.',
                'planningDiagnostics': {'prefabUnlocked': True,
                    'planningWidthMeters': width, 'isBidirectional': asset.get('isBidirectional')}}
    areas = read('GET', '/state/map-areas')
    bounds = unlocked_bounds(areas)
    networks = read('GET', '/state/networks', query={'limit': 5000, 'service': 'Road'})
    roads = networks.get('segments', [])
    if len(roads) >= 5000:
        raise ValueError('Road capture may be truncated')
    templates = [r for r in roads if r.get('prefab') == prefab]
    planning_diagnostics = {'prefabUnlocked': True, 'planningWidthMeters': width,
                            'widthSource': asset.get('widthSource'),
                            'planningWidthRule': asset.get('planningWidthRule'),
                            'existingTemplateCount': len(templates)}
    detail_cache = {}
    if templates:
        template = read('GET', '/state/network-segment-details', query={'id': templates[0]['id']})
        detail_cache[templates[0]['id']] = template
        geometry = template.get('geometryEvidence') or {}
        if geometry.get('created') and geometry.get('left', {}).get('d') and geometry.get('right', {}).get('d'):
            planning_diagnostics['settledTemplateWidthMeters'] = math.dist(
                point(geometry['left']['d']), point(geometry['right']['d']))
    terminals = []
    area_filtered = 0
    for road in roads:
        for side in ('start', 'end'):
            if road.get(side+'NodeDegree') != 1 or road.get(side+'Outside'):
                continue
            position = road[side]
            if not line_in_bounds(point(position), point(position), bounds, width/2):
                area_filtered += 1
                continue
            reach = read('GET', '/state/road-reachability', query={'id': road['id']})
            inbound = reach.get('inboundFromOutside', {}).get('found') is True
            outbound = reach.get('outboundToOutside', {}).get('found') is True
            if not inbound and not outbound:
                continue
            details = read('GET', '/state/network-segment-details', query={'id': road['id']})
            native = details.get('geometryEvidence') or {}
            exact = native.get(side+'Node')
            direction = details.get(side+'Direction')
            if not exact or not direction:
                raise ValueError('Missing exact live terminal geometry')
            terminals.append({'nodeId': road[side+'NodeId'], 'segmentId': road['id'],
                'position': {k: exact[k] for k in ('x', 'z')},
                'direction': {k: -direction[k] for k in ('x', 'z')},
                'inbound': inbound, 'outbound': outbound})
    # Follow current connected roads from verified one-sided terminals. A
    # usable junction can complete access without modifying a blocked leaf.
    adjacency = {}
    for road in roads:
        for side in ('start', 'end'):
            adjacency.setdefault(road[side+'NodeId'], []).append((road, side))
    anchor_nodes = {t['nodeId'] for t in terminals}
    seeds = list(terminals)
    reach_cache = {}
    for seed in seeds:
        pending = [seed['nodeId']]
        visited = set()
        while pending and len(visited) < 64:
            node_id = pending.pop(0)
            if node_id in visited:
                continue
            visited.add(node_id)
            incident = adjacency.get(node_id, [])
            if not incident:
                continue
            road, side = incident[0]
            pos = road[side]
            if (math.dist(point(pos), point(seed['position'])) > arguments.get('maxTerminalSeparation', 256)
                    or not line_in_bounds(point(pos), point(pos), bounds, width/2)):
                continue
            if 2 <= len(incident) <= 3 and node_id not in anchor_nodes and not road.get(side+'Outside'):
                rid = road['id']
                if rid not in reach_cache:
                    reach_cache[rid] = read('GET', '/state/road-reachability', query={'id': rid})
                reach = reach_cache[rid]
                inbound = reach.get('inboundFromOutside', {}).get('found') is True
                outbound = reach.get('outboundToOutside', {}).get('found') is True
                if inbound != outbound:
                    directions = []
                    exact = None
                    for edge, end in incident:
                        eid = edge['id']
                        if eid not in detail_cache:
                            detail_cache[eid] = read('GET', '/state/network-segment-details', query={'id': eid})
                        details = detail_cache[eid]
                        exact = (details.get('geometryEvidence') or {}).get(end+'Node')
                        directions.append(details[end+'Direction'])
                    if not exact:
                        raise ValueError('Missing exact junction geometry')
                    terminals.append({'nodeId': node_id, 'segmentId': rid,
                        'position': {k: exact[k] for k in ('x', 'z')},
                        'junctionDirections': directions, 'inbound': inbound, 'outbound': outbound})
                    anchor_nodes.add(node_id)
            for edge, end in incident:
                pending.append(edge['endNodeId' if end == 'start' else 'startNodeId'])
    pairs = [(math.dist(point(a['position']), point(b['position'])), a, b)
             for a in terminals for b in terminals
             if a['nodeId'] != b['nodeId'] and a['inbound'] and b['outbound']
             and math.dist(point(a['position']), point(b['position'])) <= arguments.get('maxTerminalSeparation', 256)]
    pairs.sort(key=lambda p: (p[0], p[1]['nodeId'], p[2]['nodeId']))
    hint_points = preferred_route or []
    hint_pair = None
    if 2 <= len(hint_points) <= 9:
        coordinates = [point(p) for p in hint_points]
        matches = []
        for pair in pairs:
            a, b = point(pair[1]['position']), point(pair[2]['position'])
            if a in coordinates and b in coordinates:
                lo, hi = sorted((coordinates.index(a), coordinates.index(b)))
                matches.append((hi-lo, lo, hi, pair))
        if matches:
            _, lo, hi, hint_pair = min(matches, key=lambda p: p[:3])
            hint_points = hint_points[lo:hi+1]
            pairs.remove(hint_pair)
            pairs.insert(0, hint_pair)
    if arguments.get('hintOnly') and not hint_pair:
        return {'ok': True, 'safe': False, 'blocker': 'No saved route matches current directed live anchors.',
                'hintStatus': 'missingOrStale', 'corridorReports': [],
                'rejectingLayer': 'routeHintValidation',
                'terminationLayer': 'routeHintValidation',
                'code': 'savedRouteMissingOrStale',
                'provenRootCause': {'scope': 'currentInvocation',
                    'layer': 'routeHintValidation', 'code': 'savedRouteMissingOrStale'},
                'nextCheck': 'Run a fresh bounded corridor search or create a new live-state hint.'}
    contexts = []
    hint_trial = None
    for separation, inbound, outbound in pairs[:8]:
        # Sample a corridor around BOTH approaches. Exact sampled anchors,
        # live heights, water/buildability and owned-area data replace offsets.
        a, b = point(inbound['position']), point(outbound['position'])
        padding = max(128, separation, width*4)
        query = {'minX': min(a[0], b[0])-padding, 'maxX': max(a[0], b[0])+padding,
                 'minZ': min(a[1], b[1])-padding, 'maxZ': max(a[1], b[1])+padding,
                 'resolution': 32, 'maxBuildSlope': .25}
        terrain = read('GET', '/state/map-analysis', query=query)
        if terrain.get('ok') is not True or not terrain.get('cells'):
            raise ValueError('Live corridor terrain unavailable')
        # A broad grid establishes the corridor, but short safe transitions to
        # an existing terminal can occupy a narrow terrain-fit band between its
        # grid lines. Add small terminal-local grids and keep only legal road
        # length annuli. This derives from live geometry and remains invariant
        # under map translation/rotation; it does not special-case a city.
        focused_cells = []
        # The 32m minimum segment annulus can fall between a coarse terminal
        # grid point that previews safely but is too short and the next point
        # whose longer native curve no longer follows terrain. Sample densely
        # enough to preserve legal anchors just outside that boundary.
        focused_resolution = 32
        focused_radius = max(104., width*6.5)
        minimum_length, maximum_length = max(32., width*2), max(96., width*4)
        for terminal in (inbound, outbound):
            center = point(terminal['position'])
            focused = read('GET', '/state/map-analysis', query={
                'minX': center[0]-focused_radius, 'maxX': center[0]+focused_radius,
                'minZ': center[1]-focused_radius, 'maxZ': center[1]+focused_radius,
                'resolution': focused_resolution, 'maxBuildSlope': .25})
            if focused.get('ok') is not True:
                raise ValueError('Live terminal terrain unavailable')
            focused_cells.extend(cell for cell in focused.get('cells', [])
                                  if minimum_length <= math.dist(
                                      point(cell['position']), center) <= maximum_length)
        combined_cells = []
        sampled_points = set()
        for cell in terrain['cells'] + focused_cells:
            sampled = point(cell['position'])
            if sampled not in sampled_points:
                combined_cells.append(cell)
                sampled_points.add(sampled)
        terrain = {**terrain, 'cells': combined_cells,
                   'focusedTerminalResolution': focused_resolution,
                   'focusedTerminalSampleCount': len(focused_cells)}
        context_index = len(contexts)
        contexts.append({'terrain': terrain, 'query': query})
        # Revalidate only a saved route first. Each interior point is sampled
        # again at its exact location; a hint is never cached authorization.
        if hint_pair and (inbound is hint_pair[1] and outbound is hint_pair[2]):
            fresh = []
            for p in hint_points[1:-1]:
                sampled = read('GET', '/state/map-analysis', query={
                    'minX': p['x'], 'minZ': p['z'], 'maxX': p['x']+8,
                    'maxZ': p['z']+8, 'resolution': 2, 'maxBuildSlope': .25})
                fresh.extend(c for c in sampled.get('cells', []) if point(c['position']) == point(p))
            first, second = ((inbound, outbound) if point(inbound['position']) == point(hint_points[0]) else (outbound, inbound))
            hint_trial = (first, second, fresh, hint_points, context_index)
        if arguments.get('hintOnly'):
            break
        # Start from the historically less explored outbound side first.
        # This is a role policy, independent of map, IDs and fixed coordinates.
        contexts[-1]['directionTrials'] = [
            (first, second, terrain['cells'], None, context_index)
            for first, second in ((outbound, inbound), (inbound, outbound))]

    available_cash = None

    def run_trial(trial, local_limit):
        nonlocal available_cash
        first, second, samples, route, _ = trial
        if available_cash is None:
            # the city's cash alone: the full snapshot reruns every anomaly scan (seconds of game thread)
            available_cash = read('GET', '/state/city-health')['cash']['amount']/100
        result = search_corridor(first, second, samples, bounds, width,
                                 prefab, preview, accepted, max_previews=local_limit,
                                 route_points=route,
                                 maximum_total_cost=max(0, available_cash-10000))
        if result.get('found'):
            total_cost = sum(float(p['constructionCost']) for p in result['previews'])/100
            result['estimatedTotalCostDisplay'] = total_cost
            result['reserveRejected'] = available_cash-total_cost < 10000
        return result

    def planning_exhausted():
        if preview.stop_reason is not None:
            return True
        if time.monotonic()-preview.started >= preview.seconds:
            preview.stop_reason = 'timeBudget'
            return True
        if preview.calls >= preview.maximum:
            preview.stop_reason = 'nativePreviewBudget'
            return True
        return False

    records = []
    if hint_trial is not None:
        result = run_trial(hint_trial, 16)
        records.append({'trial': hint_trial, 'trialIndex': -1,
                        'attempts': result.get('attempts', []), 'result': result,
                        'searchPasses': [{key: result.get(key) for key in (
                            'found', 'candidateCount', 'expansions', 'acceptedDepthCounts',
                            'candidateStreamDiagnostics',
                            'localMaximumPreviews', 'localStopReason', 'queueRemaining',
                            'queuedDepthCounts', 'poppedDepthCounts', 'deepestPoppedDepth',
                            'deepestQueuedDepth', 'schedulerPopCounts', 'goalEdgeQueued', 'goalEdgePops',
                            'goalEligibilityCounts', 'goalReserveRejections',
                            'goalConnectorQueued', 'goalConnectorPops',
                            'goalApproachPreviews', 'goalSeedPreviews', 'goalSeedAccepted',
                            'goalConnectorAcceptedTransitions', 'goalConnectorRejectedTransitions',
                            'goalConnectorRejectionCounts',
                            'distinctOrientedStatesQueued', 'duplicateOrientedStatesQueued',
                            'reverseSkeletonMatches', 'reverseSkeletonStatesQueued',
                            'reverseSkeletonStatePops', 'reverseSkeletonAcceptedTransitions',
                            'reverseSkeletonRejectedTransitions',
                            'reverseSkeletonRejectionCounts')}]})
    if (not arguments.get('hintOnly', False)
            and not any(record['result'].get('found') and not record['result'].get('reserveRejected')
                        for record in records)
            and not planning_exhausted()):
        direction_trials = [trial for context in contexts
                            for trial in context.get('directionTrials', [])]
        if direction_trials:
            # Reserve roughly half the shared budget for evidence-driven
            # deepening after every pair/orientation receives a base probe.
            exploration_budget = max(len(direction_trials), preview.maximum//2)
            base_limit = min(24, max(1, exploration_budget//len(direction_trials)))
            deeper_limits = tuple(sorted({
                limit for limit in (24, 48, 72, 96, 144, 192, 256, preview.maximum)
                if base_limit < limit <= preview.maximum
            }))
            records.extend(run_progressive_trials(
                direction_trials, run_trial, initial_limit=base_limit,
                deeper_limits=deeper_limits, should_stop=planning_exhausted,
                is_acceptable=lambda result: result.get('found')
                and not result.get('reserveRejected')))

    reports = []
    for record in records:
        first, second, samples, route, context_index = record['trial']
        context = contexts[context_index]
        terrain, query = context['terrain'], context['query']
        result = {key: value for key, value in record['result'].items() if key != 'attempts'}
        attempts = record['attempts']
        report = {'terminalNodeIds': [first['nodeId'], second['nodeId']],
                  'revalidatedRouteHint': route is not None,
                  'terrainBounds': terrain.get('bounds'),
                  'terrainResolution': terrain.get('resolution', query['resolution']),
                  'focusedTerminalResolution': terrain.get('focusedTerminalResolution'),
                  'focusedTerminalSampleCount': terrain.get('focusedTerminalSampleCount', 0),
                  'searchPasses': record['searchPasses'], **result,
                  'rejectedExamples': [], 'goalEdgeRejectedExamples': []}
        native_counts = Counter()
        acceptance_counts = Counter()
        goal_rejection_counts = Counter()
        for attempt in attempts:
            if not attempt['valid']:
                p = attempt['preview']
                reason = p.get('toolErrors', 'unknown')
                native_counts[reason] += 1
                if reason not in (None, '', 'None', 'unknown'):
                    category = 'nativeEngine'
                elif p.get('nativeMoveMiddleNodeRisk'):
                    category = 'nativeTopologyRisk'
                elif p.get('shortCollinearMergeRisk'):
                    category = 'shortCollinearMergeRisk'
                elif (p.get('terrainProfile') or {}).get('safe') is False:
                    category = 'terrainSettlementPolicy'
                elif p.get('reason') == 'futureNodeHeightEligibility':
                    category = 'futureNodeHeightEligibility'
                else:
                    category = 'otherPredictionOrAcceptancePolicy'
                acceptance_counts[category] += 1
                if attempt.get('goalEdge'):
                    goal_rejection_counts[category] += 1
                    if len(report['goalEdgeRejectedExamples']) < 4:
                        report['goalEdgeRejectedExamples'].append({
                            'candidate': attempt['candidate'],
                            'category': category,
                            'toolErrors': reason,
                            'terrainProfile': p.get('terrainProfile'),
                        })
                if len(report['rejectedExamples']) < 8:
                    report['rejectedExamples'].append({'candidate': attempt['candidate'],
                        'toolErrors': reason, 'terrainProfile': p.get('terrainProfile'),
                        'nativeMoveMiddleNodeRisk': p.get('nativeMoveMiddleNodeRisk')})
        report['nativeRejectionCounts'] = dict(native_counts)
        report['acceptanceRejectionCounts'] = dict(acceptance_counts)
        report['goalEdgeRejectionCounts'] = dict(goal_rejection_counts)
        reports.append(report)
        if result['found']:
            total_cost = result['estimatedTotalCostDisplay']
            if result.get('reserveRejected'):
                continue
            return {'ok': True, 'safe': True, 'source': 'liveTerrainConnectedCorridor',
                'plan': {'iterative': len(result['segments']) > 1,
                    'strategy': 'connectedTerrainCorridor', 'valid': True,
                    'terminalNodeIds': report['terminalNodeIds'],
                    'connectionAnchors': [first, second],
                    'segments': result['segments'][:1], 'previews': result['previews'][:1],
                    'outsideAccessRequirement': 'two-way' if len(result['segments']) == 1 else
                        ('outbound' if first['outbound'] else 'inbound'),
                    'wholeRoute': result, 'estimatedTotalCostDisplay': total_cost,
                    'settlementTerrainReserve': max(.7, arguments.get('settlementTerrainReserve', .7))},
                'areaFilteredTerminals': area_filtered, 'unlockedBounds': bounds,
                'planningDiagnostics': planning_diagnostics,
                'instructions': 'Conditional complete corridor, not proof of future construction. Build only the first segment after checkpoint/baseline; settle, inspect retained neighbors and regression, then replan the entire remaining route. Zoning requires full two-way QA and exact safe frontage.'}
    remaining_queued = sum(report.get('queueRemaining', 0) for report in reports)
    goal_summary = Counter()
    stream_summary = Counter()
    goal_edge_rejections = Counter()
    goal_connector_rejections = Counter()
    scheduler_pops = Counter()
    for report in reports:
        goal_summary.update(report.get('goalEligibilityCounts', {}))
        stream_summary.update(report.get('candidateStreamDiagnostics', {}))
        goal_edge_rejections.update(report.get('goalEdgeRejectionCounts', {}))
        goal_connector_rejections.update(report.get('goalConnectorRejectionCounts', {}))
        scheduler_pops.update(report.get('schedulerPopCounts', {}))
    goal_search_diagnostics = {
        'goalConnectorQueued': sum(report.get('goalConnectorQueued', 0) for report in reports),
        'goalConnectorPops': sum(report.get('goalConnectorPops', 0) for report in reports),
        'goalApproachPreviews': sum(
            report.get('goalApproachPreviews', 0) for report in reports),
        'goalSeedPreviews': sum(
            report.get('goalSeedPreviews', 0) for report in reports),
        'goalSeedAccepted': sum(
            report.get('goalSeedAccepted', 0) for report in reports),
        'goalConnectorAcceptedTransitions': sum(
            report.get('goalConnectorAcceptedTransitions', 0) for report in reports),
        'goalConnectorRejectedTransitions': sum(
            report.get('goalConnectorRejectedTransitions', 0) for report in reports),
        'goalConnectorRejectionCounts': dict(goal_connector_rejections),
        'goalEdgeQueued': sum(report.get('goalEdgeQueued', 0) for report in reports),
        'goalEdgePops': sum(report.get('goalEdgePops', 0) for report in reports),
        'goalReserveRejections': sum(report.get('goalReserveRejections', 0) for report in reports),
        'goalEdgeRejectionCounts': dict(goal_edge_rejections),
        'schedulerPopCounts': dict(scheduler_pops),
        # These are sums across independent terminal/orientation trials. They
        # diagnose redundant scheduling pressure; they are not a global graph
        # cardinality and therefore must not be used as an impossibility proof.
        'distinctOrientedStatesQueuedTrialSum': sum(
            report.get('distinctOrientedStatesQueued', 0) for report in reports),
        'duplicateOrientedStatesQueued': sum(
            report.get('duplicateOrientedStatesQueued', 0) for report in reports),
        'reverseSkeletonMatches': sum(
            report.get('reverseSkeletonMatches', 0) for report in reports),
        'reverseSkeletonStatesQueued': sum(
            report.get('reverseSkeletonStatesQueued', 0) for report in reports),
        'reverseSkeletonStatePops': sum(
            report.get('reverseSkeletonStatePops', 0) for report in reports),
        'reverseSkeletonAcceptedTransitions': sum(
            report.get('reverseSkeletonAcceptedTransitions', 0) for report in reports),
        'reverseSkeletonRejectedTransitions': sum(
            report.get('reverseSkeletonRejectedTransitions', 0) for report in reports),
        'reverseSkeletonRejectionCounts': dict(sum(
            (Counter(report.get('reverseSkeletonRejectionCounts', {}))
             for report in reports), Counter())),
    }
    frontier_unexplored = any(
        report.get('deepestQueuedDepth', 0) > report.get('deepestPoppedDepth', 0)
        for report in reports)
    completion_reason = ('globalPlanningBudgetStop' if preview.stop_reason else
                         'frontierNotExploredWithinSchedule' if frontier_unexplored else
                         'localTrialLimitsReached' if reports and all(
                             report.get('localStopReason') == 'trialPreviewBudget'
                             for report in reports) else
                         'boundedSearchCompleted')
    return {'ok': True, 'safe': False, 'source': 'liveTerrainConnectedCorridor',
            'terminalCount': len(terminals), 'areaFilteredTerminals': area_filtered,
            'unlockedBounds': bounds, 'corridorReports': reports,
            'planningCompletionReason': completion_reason,
            'remainingQueuedCandidates': remaining_queued,
            'goalEligibilitySummary': dict(goal_summary),
            'candidateStreamDiagnostics': dict(stream_summary),
            'goalSearchDiagnostics': goal_search_diagnostics,
            'rejectingLayer': 'candidateGeneration' if preview.calls == 0 else 'previewEvaluation',
            'code': 'boundedCorridorSearchExhausted',
            'nextCheck': 'Inspect preview rejection layers and representative rejected candidates.',
            'planningDiagnostics': planning_diagnostics,
            'blocker': 'No connected corridor passed this bounded native-preview search. No intermediate extension is proposed. Distinguish terrain/native rejection from conservative turn/width/area policy; this is not impossibility proof.'}

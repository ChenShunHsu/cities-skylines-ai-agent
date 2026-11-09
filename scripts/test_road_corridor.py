import math
import unittest
import tempfile
import hashlib
from pathlib import Path
from unittest import mock

import mayor_tools
from road_corridor import (line_in_bounds, propose_live_corridor,
                           run_progressive_trials, search_corridor,
                           segment_distance, unlocked_bounds, PreviewBudget,
                           PreviewBudgetExhausted)
from road_attempts import load_failures, record_failure


def terminal(x, z, dx, dz):
    return {'position': {'x': x, 'z': z}, 'direction': {'x': dx, 'z': dz}}


def cell(x, z, **extra):
    return {'position': {'x': x, 'z': z}, 'unlocked': True, 'buildable': True, **extra}


def native_preview(c):
    a, b = c['start'], c['end']
    start_lift=c.get('startTerrainFitLift',0)
    end_lift=c.get('endTerrainFitLift',0)
    start_y, end_y = c.get('planningStartY',100+start_lift), c.get('planningEndY',100+end_lift)
    return {'valid': True, 'planningStartNode':'planningStartY' in c, 'planningEndNode':'planningEndY' in c,
            'reusedStartNode':False, 'reusedEndNode':False, 'constructionCost': 32000,
            'nativeMoveMiddleNodeRisk':False, 'shortCollinearMergeRisk':False,
            'toolErrors':'None',
             'terrainFit': {'startLift':start_lift,'endLift':end_lift,
                 'startLiftRequested':'startTerrainFitLift' in c,
                 'endLiftRequested':'endTerrainFitLift' in c,
                 'maximumNewEndpointLift':.3,'existingNodesMoved':False},
            'length': math.hypot(b['x']-a['x'], b['z']-a['z']),
            'terrainProfile': {'source':'predictedControlBezierAndLiveTerrain',
                'predictsSettledSurface':False,'required':True,'safe':True,'sampleCount':3,
                'minimumRoadAboveTerrain':.2,
                'minimumRoadAboveTerrainAllowed':.12,'maxRoadToTerrainDelta':.2,
                'maxLocalTerrainGrade':.01,'maximumLocalTerrainGrade':.25,
                'maximumRoadToTerrainDelta':1,'previewMaximumRoadToTerrainDelta':.3,
                'settlementTerrainReserve':.7,'settlementTerrainReserveApplied':True,
                'settlementClearanceReserve':.1,'settlementClearanceReserveApplied':True,
                'settledMinimumRoadAboveTerrainAllowed':.02,
                'originalNodeTransition':False},
            'predictionGeometryEvidence': {'source':'predictedControlBezier',
                'clearanceModel':'terrainSeededControlCurve','predictsSettledSurface':False,
                'curve': {'a':{**a,'y':start_y},'d':{**b,'y':end_y}},
                'samples': [{'t':t,'position': {**{k: a[k]+(b[k]-a[k])*t for k in ('x', 'z')},
                    'y':start_y+(end_y-start_y)*t},
                    'terrainY':start_y+(end_y-start_y)*t-.2,
                    'predictedClearance':.2} for t in (0, .5, 1)]}}


class CorridorTests(unittest.TestCase):
    def run_search(self, first=None, second=None, cells=None, width=16, preview=native_preview, bounds=None, **kw):
        return search_corridor(first or terminal(0,0,1,0), second or terminal(192,0,-1,0),
            cells if cells is not None else [cell(64,0),cell(128,0)],
            bounds or [(-1000,-1000,1000,1000)], width, 'fixture road', preview,
            lambda p: mayor_tools._road_preview_survives_settlement(p,.7), **kw)

    def test_returns_complete_connected_path_and_only_conditional_future_evidence(self):
        r=self.run_search()
        self.assertTrue(r['found'])
        self.assertEqual(3,len(r['segments']))
        self.assertGreaterEqual(r['goalEdgeQueued'],1)
        self.assertGreaterEqual(r['goalEdgePops'],1)
        self.assertGreaterEqual(r['goalEligibilityCounts']['queued'],1)
        self.assertTrue(any('startTerrainFitLift' in segment
                            or 'endTerrainFitLift' in segment
                            for segment in r['segments']))
        stream=r['candidateStreamDiagnostics']
        self.assertGreaterEqual(stream['generatedTransitionAttempts'],stream['staticallyEligibleTransitionAttempts'])
        self.assertEqual(r['candidateCount'],stream['locallyEvaluatedPreviewVariants'])
        self.assertGreaterEqual(stream['locallyEvaluatedPreviewVariants'],stream['locallyAcceptedPreviewVariants'])
        self.assertGreater(stream['locallyAcceptedPreviewVariants'],0)
        self.assertFalse(r['futureJunctionsVerified'])
        self.assertFalse(r['frontage']['verifiedZoningCapacity'])
        for a,b in zip(r['segments'],r['segments'][1:]):
            self.assertEqual(a['end'],b['start'])

    def test_individually_viable_approaches_do_not_make_a_complete_route(self):
        def isolated(c):
            p=native_preview(c)
            p['valid']=not (min(c['start']['x'],c['end']['x']) < 100 < max(c['start']['x'],c['end']['x']))
            return p
        self.assertFalse(self.run_search(preview=isolated)['found'])

    def test_original_outbound_clearance_failure_remains_rejected(self):
        def unsafe(c):
            result=native_preview(c)
            return {**result, 'terrainProfile': {**result['terrainProfile'], 'safe':False,
                'maxRoadToTerrainDelta':.434184253}}
        self.assertFalse(self.run_search(preview=unsafe)['found'])

    def test_bounded_terrain_fit_lift_is_previewed_for_new_interior_endpoint(self):
        def needs_lift(candidate):
            result=native_preview(candidate)
            lift=max(candidate.get('startTerrainFitLift',0),
                     candidate.get('endTerrainFitLift',0))
            endpoints={candidate['start']['x'],candidate['end']['x']}
            interior_edge=not endpoints.issubset({0,192}) and 192 not in endpoints
            if interior_edge and lift < .2:
                result['valid']=False
                result['terrainProfile']={**result['terrainProfile'],
                    'safe':False,'minimumRoadAboveTerrain':-.05}
            return result
        result=self.run_search(preview=needs_lift,max_previews=48)
        self.assertTrue(result['found'],result)
        self.assertTrue(any(max(segment.get('startTerrainFitLift',0),
                                segment.get('endTerrainFitLift',0)) >= .2
                            for segment in result['segments']))
        self.assertGreater(result['rejections'].get('terrainFitVariantsTried',0),0)

    def test_native_rewrite_and_merge_rejections_stay_blocking(self):
        for gate in ('nativeMoveMiddleNodeRisk','shortCollinearMergeRisk'):
            with self.subTest(gate=gate):
                self.assertFalse(self.run_search(preview=lambda c: {**native_preview(c),gate:True})['found'])

    def test_translated_rotated_terrain_routes_and_road_widths(self):
        for angle in (0,.37,math.pi/2,math.pi):
            for width in (8,16,32):
                def p(x,z):
                    return (200+x*math.cos(angle)-z*math.sin(angle),-130+x*math.sin(angle)+z*math.cos(angle))
                direction=(math.cos(angle),math.sin(angle))
                r=self.run_search(terminal(*p(0,0),*direction),
                    terminal(*p(192,0),*(-direction[0],-direction[1])),
                    [cell(*p(64,0)),cell(*p(128,0))],width=width)
                self.assertTrue(r['found'],(angle,width,r))

    def test_translated_rotated_right_angle_terminal_connection_is_allowed(self):
        for angle in (0,.37,math.pi/2,math.pi):
            def p(x,z):
                return (300+x*math.cos(angle)-z*math.sin(angle),
                        -170+x*math.sin(angle)+z*math.cos(angle))
            def d(x,z):
                return (x*math.cos(angle)-z*math.sin(angle),
                        x*math.sin(angle)+z*math.cos(angle))
            start_direction=d(1,0)
            second_direction=d(0,1)
            result=self.run_search(
                terminal(*p(0,0),*start_direction),
                terminal(*p(0,-32),*second_direction),
                cells=[])
            self.assertTrue(result['found'],(angle,result))
            self.assertEqual(90,result['maximumTurnDegrees'])

    def test_dense_terrain_u_turn_uses_sparse_reverse_reachable_lattice(self):
        for angle in (0,.61,math.pi/2):
            def p(x,z):
                return (340+x*math.cos(angle)-z*math.sin(angle),
                        -210+x*math.sin(angle)+z*math.cos(angle))
            direction=(math.cos(angle),math.sin(angle))
            cells=[cell(*p(x,z)) for x in range(16,193,16)
                   for z in range(-160,161,16)]
            result=self.run_search(
                terminal(*p(0,32),*direction),
                terminal(*p(0,-32),*direction),
                cells, max_previews=96, max_segments=8)
            self.assertTrue(result['found'],(angle,result))
            stream=result['candidateStreamDiagnostics']
            self.assertLess(stream['generatedTransitionAttempts'],5000)
            self.assertGreaterEqual(
                result['goalConnectorQueued']+result['goalEdgeQueued'],1)
            self.assertGreaterEqual(
                result['goalConnectorPops']+result['goalEdgePops'],1)
            self.assertGreater(result['reverseSkeletonMatches'],0)
            self.assertGreater(result['distinctOrientedStatesQueued'],0)
            self.assertGreaterEqual(result['duplicateOrientedStatesQueued'],0)

    def test_water_or_locked_sample_does_not_become_an_anchor(self):
        for key in ('unlocked','buildable'):
            self.assertFalse(self.run_search(cells=[cell(64,0,**{key:False}),cell(128,0)])['found'])

    def test_turning_terminal_requires_connected_turn_not_reverse_stub(self):
        self.assertFalse(self.run_search(second=terminal(192,0,1,0))['found'])
        route=[cell(64,0),cell(112,48),cell(112,96),cell(64,144)]
        self.assertTrue(self.run_search(second=terminal(0,144,1,0),cells=route)['found'])

    def test_width_and_full_line_bounds_checked_before_native_preview(self):
        calls=[]
        self.assertFalse(self.run_search(bounds=[(-10,-4,210,4)], preview=lambda c: calls.append(c))['found'])
        self.assertEqual([],calls)
        self.assertFalse(line_in_bounds((0,0),(200,0),[(-10,-10,90,10),(110,-10,210,10)]))
        self.assertTrue(line_in_bounds((0,0),(200,0),[(-10,-10,100,10),(100,-10,210,10)]))

    def test_predicted_native_curve_must_also_fit_bounds(self):
        def bad_curve(c):
            r=native_preview(c)
            r['predictionGeometryEvidence']['samples'][1]['position']['z']=2000
            return r
        self.assertFalse(self.run_search(preview=bad_curve)['found'])
        self.assertFalse(self.run_search(preview=lambda c:{'valid':True})['found'])

    def test_native_preview_budget_is_bounded(self):
        result=self.run_search(max_previews=1)
        self.assertLessEqual(result['candidateCount'],1)
        self.assertEqual('trialPreviewBudget',result['localStopReason'])
        self.assertEqual(1,result['localMaximumPreviews'])
        self.assertGreater(result['queueRemaining'],0)
        stream=result['candidateStreamDiagnostics']
        self.assertGreater(stream['generatedTransitionAttempts'],stream['locallyEvaluatedPreviewVariants'])
        self.assertEqual(1,stream['locallyEvaluatedPreviewVariants'])

    def test_same_orientation_skips_expensive_goal_for_affordable_detour(self):
        def priced(candidate):
            result=native_preview(candidate)
            direct=(candidate['start']['z']==0 and candidate['end']['z']==0)
            result['constructionCost']=200000 if direct else 32000
            return result
        result=self.run_search(second=terminal(64,0,-1,0),cells=[cell(32,24)],
                               preview=priced,maximum_total_cost=1000)
        self.assertTrue(result['found'],result)
        self.assertEqual(1,result['goalReserveRejections'])
        self.assertEqual(2,len(result['segments']))
        self.assertGreaterEqual(result['goalConnectorQueued'],1)
        self.assertGreaterEqual(result['goalConnectorPops'],1)
        self.assertEqual('boundedNotGuaranteed',result['routeOptimality'])

    def test_reverse_build_orientation_preserved_separately_from_route_order(self):
        def reverse_only(c):
            return {**native_preview(c), 'valid':c['start']['x'] > c['end']['x']}
        r=self.run_search(preview=reverse_only)
        self.assertTrue(r['found'])
        for c,a,b in zip(r['segments'],r['traversalPoints'],r['traversalPoints'][1:]):
            self.assertEqual(c['start'],b)
            self.assertEqual(c['end'],a)
        self.assertFalse(r['futureJunctionsVerified'])

    def test_budget_reaches_deeper_states_before_exhausting_root_neighbors(self):
        cells=[cell(64,0),cell(128,0)]+[cell(40+i*.4, .01+i*.01) for i in range(100)]
        r=self.run_search(cells=cells,max_previews=8)
        self.assertTrue(r['found'],r)
        self.assertIn(3,r['acceptedDepthCounts'])
        self.assertGreaterEqual(r['deepestPoppedDepth'],3)
        self.assertEqual('guidedDepthFairAStarWithAnchor',r['queuePolicy'])
        self.assertLessEqual(r['candidateCount'],8)

    def test_guided_queue_keeps_standard_astar_anchor_share(self):
        cells=[cell(40+i*2,20+(i%3)*20) for i in range(40)]
        result=self.run_search(cells=cells,max_previews=8,
                               preview=lambda candidate:{'valid':False,'toolErrors':'ObjectCollision'})
        pops=result['schedulerPopCounts']
        self.assertGreaterEqual(pops.get('guided',0),1)
        self.assertGreaterEqual(pops.get('anchor',0),1)
        self.assertGreaterEqual(pops.get('deepest',0),1)
        self.assertEqual(sum(result['poppedDepthCounts'].values()),sum(pops.values()))

    def test_non_greedy_frontier_finds_route_that_initially_moves_away_from_goal(self):
        route=[(0,0),(-64,0),(-96,48),(-64,112),(0,160),(0,192)]
        allowed={frozenset((route[i],route[i+1])) for i in range(len(route)-1)}
        def route_only(candidate):
            edge=frozenset(((float(candidate['start']['x']),float(candidate['start']['z'])),
                            (float(candidate['end']['x']),float(candidate['end']['z']))))
            return {**native_preview(candidate),'valid':edge in allowed}
        first=terminal(0,0,-1,0)
        second=terminal(0,192,0,-1)
        cells=[cell(x,z) for x,z in route[1:-1]]+[cell(48+i*2,32+(i%4)*24) for i in range(30)]
        result=self.run_search(first,second,cells,preview=route_only,max_previews=40)
        self.assertTrue(result['found'],result)
        self.assertGreater(math.dist(route[1],route[-1]),math.dist(route[0],route[-1]))
        self.assertGreater(result['schedulerPopCounts'].get('anchor',0),0)
        self.assertGreater(result['schedulerPopCounts'].get('deepest',0),0)

    def test_deep_dead_branch_does_not_starve_shallower_complete_route(self):
        correct=[(0,0),(64,0),(128,0),(192,0)]
        dead=[(0,0),(40,20),(80,40),(120,60),(160,80),(200,100),(240,120)]
        allowed={frozenset((path[i],path[i+1])) for path in (correct,dead)
                 for i in range(len(path)-1)}
        def selected_edges(candidate):
            edge=frozenset(((float(candidate['start']['x']),float(candidate['start']['z'])),
                            (float(candidate['end']['x']),float(candidate['end']['z']))))
            return {**native_preview(candidate),'valid':edge in allowed}
        cells=[cell(x,z) for x,z in correct[1:-1]+dead[1:]]
        result=self.run_search(cells=cells,preview=selected_edges,max_previews=24)
        self.assertTrue(result['found'],result)
        self.assertEqual(correct,result['traversalPoints'] and [
            (point['x'],point['z']) for point in result['traversalPoints']])
        self.assertGreater(result['schedulerPopCounts'].get('deepest',0),0)

    def test_goal_connector_hint_never_bypasses_native_rejection(self):
        def reject_goal(candidate):
            result=native_preview(candidate)
            if candidate['end']['x']==192 or candidate['start']['x']==192:
                result.update(valid=False,toolErrors='ObjectCollision')
            return result
        result=self.run_search(preview=reject_goal,max_previews=24)
        self.assertFalse(result['found'])
        self.assertGreater(result['goalConnectorQueued'],0)
        self.assertGreater(result['goalConnectorPops'],0)
        self.assertGreater(result['goalApproachPreviews'],0)
        self.assertEqual(0,result['goalEdgeQueued'])
        self.assertEqual(0,result['goalEdgePops'])

    def test_junction_branches_use_all_incident_directions_and_native_gates(self):
        for angle in (0,.73,math.pi/2):
            def p(x,z):return (400+x*math.cos(angle)-z*math.sin(angle),200+x*math.sin(angle)+z*math.cos(angle))
            def d(x,z):return {'x':x*math.cos(angle)-z*math.sin(angle),'z':x*math.sin(angle)+z*math.cos(angle)}
            first={'position':dict(zip(('x','z'),p(0,0))),'junctionDirections':[d(0,1),d(0,-1)]}
            second={'position':dict(zip(('x','z'),p(192,0))),'junctionDirections':[d(0,1),d(0,-1)]}
            cells=[cell(*p(64,0)),cell(*p(128,0))]
            self.assertTrue(self.run_search(first,second,cells)['found'])
            second['junctionDirections'].append(d(-1,0))
            self.assertFalse(self.run_search(first,second,cells)['found'])

    def test_bridge_clearance_threshold_is_the_planner_acceptance(self):
        def marginal(c):
            result=native_preview(c)
            result.update(reusedStartNode=False,reusedEndNode=False)
            result['predictionGeometryEvidence']['source']='predictedControlBezier'
            for sample,value in zip(result['predictionGeometryEvidence']['samples'],(.2,.134,.2)):
                sample['predictedClearance']=value
                sample['terrainY']=sample['position']['y']-value
            return {**result,'terrainProfile':{**result['terrainProfile'],
                'minimumRoadAboveTerrain':.134,'maxRoadToTerrainDelta':.2,
                'minimumRoadAboveTerrainAllowed':.12}}
        self.assertTrue(self.run_search(preview=marginal)['found'])

    def test_required_terrain_without_valid_bridge_threshold_fails_closed(self):
        for threshold in (None, True, float('nan'), -0.01):
            def invalid(c):
                result=native_preview(c)
                terrain={**result['terrainProfile'],'minimumRoadAboveTerrain':.1,
                    'maxRoadToTerrainDelta':.2}
                if threshold is not None:
                    terrain['minimumRoadAboveTerrainAllowed']=threshold
                else:
                    terrain.pop('minimumRoadAboveTerrainAllowed')
                return {**result,'terrainProfile':terrain}
            self.assertFalse(self.run_search(preview=invalid)['found'])

    def test_ground_road_acceptance_requires_complete_terrain_evidence(self):
        baseline=native_preview({'start':{'x':0,'z':0},'end':{'x':64,'z':0}})
        for corrupt in (
            {**baseline,'terrainProfile':None},
            {**baseline,'terrainProfile':{}},
            {**baseline,'terrainProfile':{**baseline['terrainProfile'],'required':False}},
            {**baseline,'predictionGeometryEvidence':None},
            {**baseline,'predictionGeometryEvidence':['not','an','object']},
            {**baseline,'predictionGeometryEvidence':{
                **baseline['predictionGeometryEvidence'],'clearanceModel':'legacyFixedOffset'}},
            {**baseline,'predictionGeometryEvidence':{
                key:value for key,value in baseline['predictionGeometryEvidence'].items()
                if key != 'clearanceModel'}},
            {**baseline,'predictionGeometryEvidence':{
                **baseline['predictionGeometryEvidence'],'curve':{'a':[],'d':{}}}},
            {**baseline,'predictionGeometryEvidence':{
                **baseline['predictionGeometryEvidence'],'samples':[
                    {'position':{},'predictedClearance':.1} for _ in range(3)]}},
            {**baseline,'predictionGeometryEvidence':{
                **baseline['predictionGeometryEvidence'],'samples':[
                    {'position':{'x':0,'y':100,'z':float('nan')},'predictedClearance':.1}
                    for _ in range(3)]}},
            {**baseline,'valid':1},
            {key:value for key,value in baseline.items() if key != 'nativeMoveMiddleNodeRisk'},
            {**baseline,'shortCollinearMergeRisk':None},
            {**baseline,'toolErrors':None},
            {**baseline,'terrainProfile':{
                **baseline['terrainProfile'],'source':'legacyPreview'}},
            {**baseline,'terrainProfile':{
                **baseline['terrainProfile'],'sampleCount':4}},
            {**baseline,'terrainProfile':{
                **baseline['terrainProfile'],'settlementTerrainReserveApplied':False}},
            {**baseline,'terrainProfile':{
                **baseline['terrainProfile'],'settlementClearanceReserveApplied':False}},
            {**baseline,'terrainProfile':{
                **baseline['terrainProfile'],'settlementClearanceReserve':0}},
            {**baseline,'terrainProfile':{
                **baseline['terrainProfile'],'minimumRoadAboveTerrainAllowed':.02}},
            {**baseline,'terrainProfile':{
                key:value for key,value in baseline['terrainProfile'].items()
                if key != 'settledMinimumRoadAboveTerrainAllowed'}},
            {**baseline,'terrainProfile':{
                key:value for key,value in baseline['terrainProfile'].items()
                if key != 'maxLocalTerrainGrade'}},
            {**baseline,'terrainProfile':{
                **baseline['terrainProfile'],'maximumLocalTerrainGrade':True}},
            {**baseline,'terrainProfile':{
                **baseline['terrainProfile'],'maxLocalTerrainGrade':.3}},
            {**baseline,'terrainProfile':{
                **baseline['terrainProfile'],'originalNodeTransition':'false'}},
            {**baseline,'predictionGeometryEvidence':{
                **baseline['predictionGeometryEvidence'],'predictsSettledSurface':True}},
            {**baseline,'predictionGeometryEvidence':{
                **baseline['predictionGeometryEvidence'],'samples':[
                    ({**sample,'terrainY':sample['terrainY']-.2} if index == 1 else sample)
                    for index,sample in enumerate(
                        baseline['predictionGeometryEvidence']['samples'])]}},
            {**baseline,'terrainProfile':{
                **baseline['terrainProfile'],'minimumRoadAboveTerrain':.3}},
            {**baseline,'terrainProfile':{**baseline['terrainProfile'],'maxRoadToTerrainDelta':-0.1}},
        ):
            self.assertFalse(mayor_tools._road_preview_survives_settlement(corrupt,.7))

    def test_original_node_transition_uses_explicit_settled_minimum_without_reserve(self):
        baseline=native_preview({'start':{'x':0,'z':0},'end':{'x':64,'z':0}})
        transition={**baseline,'terrainProfile':{**baseline['terrainProfile'],
            'originalNodeTransition':True,'settlementClearanceReserve':0,
            'settlementClearanceReserveApplied':False,'minimumRoadAboveTerrainAllowed':.02}}
        self.assertTrue(mayor_tools._road_preview_survives_settlement(transition,.7))
        # The relaxation must be declared consistently and never applies elsewhere.
        for corrupt in (
            {**transition,'terrainProfile':{**transition['terrainProfile'],'settlementClearanceReserveApplied':True}},
            {**transition,'terrainProfile':{**transition['terrainProfile'],'originalNodeTransition':False}},
            {**transition,'terrainProfile':{**transition['terrainProfile'],'minimumRoadAboveTerrainAllowed':.0}},
        ):
            self.assertFalse(mayor_tools._road_preview_survives_settlement(corrupt,.7))

    def test_shared_future_node_height_conflict_and_missing_evidence_reject_route(self):
        for elevation in (-100,100,1500):
            def conflicting(c):
                r=native_preview(c)
                curve=r['predictionGeometryEvidence']['curve']
                curve['a']['y']=elevation
                curve['d']['y']=elevation+.3
                return r
            self.assertFalse(self.run_search(preview=conflicting)['found'])
        def unknown(c):
            r=native_preview(c);del r['predictionGeometryEvidence']['curve']
            return r
        self.assertFalse(self.run_search(preview=unknown)['found'])

    def test_future_height_is_propagated_and_unsupported_preview_fails_closed(self):
        calls=[]
        def lifted(c):
            r=native_preview(c)
            if c['start']['x']==0:
                r['predictionGeometryEvidence']['curve']['d']['y']=100.3
                r['predictionGeometryEvidence']['samples'][-1]['position']['y']=100.3
                r['predictionGeometryEvidence']['samples'][-1]['terrainY']=100.1
            calls.append((c,r))
            return r
        r=self.run_search(preview=lifted)
        self.assertTrue(r['found'])
        self.assertNotIn('planningStartY',r['segments'][0])
        self.assertAlmostEqual(100.3,r['segments'][1]['planningStartY'])
        self.assertAlmostEqual(100.3,r['previews'][1]['predictionGeometryEvidence']['curve']['a']['y'])
        def unsupported(c):
            p=native_preview(c);p.pop('planningStartNode');p.pop('planningEndNode');return p
        self.assertFalse(self.run_search(preview=unsupported)['found'])

    def test_hypothetical_heights_are_read_only_and_do_not_overwrite_existing_nodes(self):
        source=(mayor_tools.ROOT/'src/RoadCommands.cs').read_text(encoding='utf-8')
        block=source.split('bool planningStart =')[1].split('float terrainFitStartLift;')[0]
        self.assertIn('if (!forceDryRun',block)
        self.assertIn('startNode != 0',block)
        self.assertIn('endNode != 0',block)
        self.assertIn('MaximumNewEndpointTerrainFit',block)
        self.assertIn('float.IsNaN',block)
        self.assertIn('float.IsInfinity',block)
        self.assertIn('requestedStartLift',block)
        self.assertIn('requestedEndLift',block)
        self.assertIn('startTerrainFitLift',source)
        self.assertIn('endTerrainFitLift',source)

    def test_precise_samples_cannot_round_into_clearance_or_deviation_pass(self):
        for minimum,maximum,expected in ((.1195788455,.2,False),(.1201,.3004,False),(.1201,.2999,True)):
            for x,z in ((0,0),(200,-130)):
                c={'start':{'x':x,'z':z},'end':{'x':x+64,'z':z}}
                r=native_preview(c)
                r.update(reusedStartNode=True,reusedEndNode=False,
                    terrainProfile={**r['terrainProfile'],'minimumRoadAboveTerrain':round(minimum,3),
                        'maxRoadToTerrainDelta':round(maximum,3),'previewMaximumRoadToTerrainDelta':.3,
                        'minimumRoadAboveTerrainAllowed':.12})
                r['predictionGeometryEvidence']['source']='predictedControlBezier'
                for sample,value in zip(r['predictionGeometryEvidence']['samples'],(.4,minimum,maximum)):
                    sample['predictedClearance']=value
                    sample['terrainY']=sample['position']['y']-value
                self.assertEqual(expected,mayor_tools._road_preview_survives_settlement(r,.7))

    def test_requested_settlement_reserve_is_not_overstated(self):
        candidate=native_preview({'start':{'x':0,'z':0},'end':{'x':64,'z':0}})
        candidate['terrainProfile']['minimumRoadAboveTerrain']=.2
        candidate['terrainProfile']['maxRoadToTerrainDelta']=.2
        for sample in candidate['predictionGeometryEvidence']['samples']:
            sample['predictedClearance']=.2
            sample['terrainY']=sample['position']['y']-.2
        self.assertTrue(mayor_tools._road_preview_survives_settlement(candidate,.7))
        self.assertFalse(mayor_tools._road_preview_survives_settlement(candidate,.9))

    def test_segment_distance_detects_crossings_parallel_overlap_and_separation(self):
        self.assertEqual(0,segment_distance((0,0),(100,0),(50,-50),(50,50)))
        self.assertEqual(0,segment_distance((0,0),(100,0),(25,0),(75,0)))
        self.assertEqual(32,segment_distance((0,0),(100,0),(25,32),(75,32)))

    def test_live_ownership_required_and_unknown_grid_fails_closed(self):
        a={'ok':True,'totalAreaCount':25,'areas':[{'x':1,'z':3,'unlocked':True},{'x':2,'z':2,'unlocked':False}]}
        self.assertEqual([(-2880,960,-960,2880)],unlocked_bounds(a))
        with self.assertRaises(ValueError): unlocked_bounds({**a,'totalAreaCount':81})

    def test_straight_snapped_end_does_not_invert_adjacent_tangent(self):
        source=(mayor_tools.ROOT/'src/RoadCommands.cs').read_text(encoding='utf-8')
        method=source.split('private static Vector3 ResolveConnectionDirection(')[1].split('private static Vector3 ReadPoint')[0]
        self.assertIn('return requestedDirection;',method)
        self.assertNotIn('candidate = -outward',method)


class LiveAdapterTests(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.failure_path=Path(folder.name)/'failures.json'
        patch=mock.patch.object(mayor_tools,'ROAD_FAILURE_PATH',self.failure_path)
        patch.start();self.addCleanup(patch.stop)
        for name in ('ROAD_CORRIDOR_HINT_PATH', 'ROAD_SEARCH_PROGRESS_PATH'):
            patch=mock.patch.object(mayor_tools,name,Path(folder.name)/(name+'.json'))
            patch.start();self.addCleanup(patch.stop)
        self.calls=[]
        self.guard=''
        self.cash=10000000
        self.unlocked=True
        self.roads=[]
        for i,(start,end,dx) in enumerate(((-64,0,1),(256,192,-1))):
            self.roads.append({'id':10+i,'prefab':'fixture road','startNodeId':20+i*2,
                'endNodeId':21+i*2,'startNodeDegree':2,'endNodeDegree':1,
                'start':{'x':start,'z':0},'end':{'x':end,'z':0},
                'startDirection':{'x':dx,'z':0},'endDirection':{'x':-dx,'z':0}})

    def request(self,method,path,**kwargs):
        self.calls.append((method,path,kwargs))
        if path=='/state/mayor-snapshot':
            return {'city':{'instanceId':'synthetic-city','name':'Fixture','map':'Fixture'},
                    'cityHealth':{'cash':{'amount':self.cash}}}
        if path=='/state/city-health':return {'cash':{'amount':self.cash}}
        if path=='/prefabs/roads':return {'roads':[{'name':'fixture road','unlocked':self.unlocked,
            'planningWidthMeters':16,'widthSource':'loadedNetInfo','isBidirectional':True}]}
        if path=='/state/map-areas':return {'ok':True,'totalAreaCount':25,'areas':[{'x':2,'z':2,'unlocked':True}]}
        if path=='/state/networks':return {'segments':self.roads}
        if path=='/state/road-reachability':
            return {'inboundFromOutside':{'found':kwargs['query']['id']==10},
                    'outboundToOutside':{'found':kwargs['query']['id']==11}}
        if path=='/state/network-segment-details':
            r=next(r for r in self.roads if r['id']==kwargs['query']['id'])
            return {**r,'isBidirectional':True,'geometryEvidence':{'created':True,
                'startNode':r['start'],'endNode':r['end'],'left':{'d':{'x':r['end']['x'],'z':-8}},
                'right':{'d':{'x':r['end']['x'],'z':8}}}}
        if path=='/state/map-analysis':
            q=kwargs['query']
            return {'ok':True,'cells':[cell(q['minX'],q['minZ'])] if q['resolution']==2 else [cell(64,0),cell(128,0)]}
        self.assertEqual(('POST','/commands/preview-network'),(method,path))
        return {**native_preview(kwargs['body']),self.guard:bool(self.guard)}

    def plan(self, **arguments):
        with mock.patch.object(mayor_tools,'_request',side_effect=self.request):
            return mayor_tools.call_tool('propose_outside_road_connection',{'roadPrefab':'fixture road', **arguments})

    def test_total_budget_applies_across_both_search_directions(self):
        self.guard='nativeMoveMiddleNodeRisk'
        r=self.plan(maxNativePreviews=3)
        native=[c for c in self.calls if c[1]=='/commands/preview-network']
        self.assertEqual(3,len(native))
        self.assertEqual(3,r['searchDiagnostics']['nativePreviewCalls'])
        self.assertEqual('nativePreviewBudget',r['searchDiagnostics']['stopReason'])
        self.assertEqual(2,len(r['corridorReports']))
        self.assertEqual('predictionOrAcceptancePolicy',r['rejectingLayer'])
        self.assertEqual('planningBudget',r['terminationLayer'])
        self.assertEqual('nativePreviewBudget',r['limitingResource'])
        self.assertEqual({'predictionOrAcceptancePolicy':3},r['contributingLayers'])
        self.assertIsNone(r['provenRootCause'])
        self.assertEqual({'predictionOrAcceptancePolicy':3},r['candidateRejectionLayers'])
        self.assertEqual('globalPlanningBudgetStop',r['planningCompletionReason'])
        diagnostics=r['goalSearchDiagnostics']
        self.assertEqual(sum(report['goalConnectorQueued'] for report in r['corridorReports']),
                         diagnostics['goalConnectorQueued'])
        self.assertEqual(sum(report['goalConnectorPops'] for report in r['corridorReports']),
                         diagnostics['goalConnectorPops'])
        self.assertEqual(sum(report['goalEdgePops'] for report in r['corridorReports']),
                         diagnostics['goalEdgePops'])
        progress=__import__('json').loads(mayor_tools.ROAD_SEARCH_PROGRESS_PATH.read_text())
        self.assertEqual(diagnostics,progress['goalSearchDiagnostics'])
        self.assertFalse(r['safe'])

    def test_saved_route_is_revalidated_with_one_preview_per_edge(self):
        self.assertTrue(self.plan()['safe'])
        self.calls.clear()
        r=self.plan(hintOnly=True)
        self.assertTrue(r['safe'])
        self.assertEqual(3,r['searchDiagnostics']['nativePreviewCalls'])
        self.assertEqual(3,len([c for c in self.calls if c[1]=='/commands/preview-network']))

    def test_hint_only_does_not_fall_back_when_route_is_rejected(self):
        self.plan();self.calls.clear();self.guard='nativeMoveMiddleNodeRisk'
        r=self.plan(hintOnly=True)
        self.assertFalse(r['safe'])
        self.assertEqual(2,r['searchDiagnostics']['nativePreviewCalls'])

    def test_missing_hint_does_not_launch_search(self):
        r=self.plan(hintOnly=True)
        self.assertFalse(r['safe'])
        self.assertEqual(0,r['searchDiagnostics']['nativePreviewCalls'])
        self.assertEqual('routeHintValidation',r['rejectingLayer'])
        self.assertEqual('routeHintValidation',r['terminationLayer'])
        self.assertEqual('savedRouteMissingOrStale',r['provenRootCause']['code'])

    def test_city_change_invalidates_hint(self):
        import json
        self.plan()
        p=mayor_tools.ROAD_CORRIDOR_HINT_PATH
        hint=json.loads(p.read_text());hint['instanceId']='another-city';p.write_text(json.dumps(hint))
        r=self.plan(hintOnly=True)
        self.assertFalse(r['safe'])
        self.assertEqual(0,r['searchDiagnostics']['nativePreviewCalls'])

    def test_typed_planner_returns_one_step_with_a_complete_connected_corridor(self):
        r=self.plan();self.assertTrue(r['safe'])
        plan=r['plan'];self.assertEqual(1,len(plan['segments']))
        self.assertEqual(3,len(plan['wholeRoute']['segments']))
        self.assertEqual('outbound',plan['outsideAccessRequirement'])
        self.assertFalse(plan['wholeRoute']['futureJunctionsVerified'])
        self.assertEqual([23,21],plan['terminalNodeIds'])

    def test_live_search_requests_dense_terrain_candidates(self):
        result=self.plan()
        requests=[kw['query'] for _,path,kw in self.calls if path=='/state/map-analysis'
                  and kw['query']['resolution'] != 2]
        self.assertTrue(requests)
        self.assertIn(32,{query['resolution'] for query in requests})
        self.assertTrue(all(report['focusedTerminalResolution']==32
                            for report in result.get('corridorReports', [])))

    def test_close_parallel_terminals_are_not_rejected_as_a_corridor_pair(self):
        self.roads[1]['end']={'x':24,'z':0}
        result=self.plan(maxNativePreviews=1)
        self.assertGreater(result['searchDiagnostics']['nativePreviewCalls'],0)
        self.assertTrue(result['corridorReports'])

    def test_fresh_map_uses_loaded_prefab_width_without_existing_template(self):
        for road in self.roads:
            road['prefab']='Highway Ramp'
        r=self.plan()
        self.assertTrue(r['safe'],r)
        self.assertGreater(r['searchDiagnostics']['nativePreviewCalls'],0)
        self.assertEqual(0,r['planningDiagnostics']['existingTemplateCount'])
        self.assertEqual('loadedNetInfo',r['planningDiagnostics']['widthSource'])
        implementation=r['plannerImplementation']
        planner=(mayor_tools.ROOT/'scripts/road_corridor.py').read_bytes()
        adapter=(mayor_tools.ROOT/'scripts/mayor_tools.py').read_bytes()
        self.assertEqual(2,implementation['schemaVersion'])
        self.assertEqual(hashlib.sha256(planner).hexdigest(),implementation['sourceSha256'])
        self.assertEqual(hashlib.sha256(adapter).hexdigest(),implementation['acceptanceAdapterSha256'])
        self.assertEqual(hashlib.sha256(planner+b'\0'+adapter).hexdigest(),implementation['combinedSha256'])
        self.assertEqual('terrainSeededControlCurve',implementation['requiredClearanceModel'])
        progress=__import__('json').loads(mayor_tools.ROAD_SEARCH_PROGRESS_PATH.read_text())
        self.assertEqual(implementation,progress['plannerImplementation'])

    def test_prefab_geometry_and_direction_fail_closed_before_preview(self):
        request=self.request
        for field,value,code in (('planningWidthMeters',None,'requestedPrefabGeometryUnavailable'),
                                 ('planningWidthMeters',0,'requestedPrefabGeometryUnavailable'),
                                 ('planningWidthMeters',float('nan'),'requestedPrefabGeometryUnavailable'),
                                 ('isBidirectional',False,'requestedPrefabNotBidirectional')):
            self.calls.clear()
            def invalid_asset(method,path,field=field,value=value,**kwargs):
                if path=='/prefabs/roads':
                    asset={'name':'fixture road','unlocked':True,'planningWidthMeters':16,
                           'widthSource':'loadedNetInfo','isBidirectional':True}
                    asset[field]=value
                    return {'roads':[asset]}
                return request(method,path,**kwargs)
            with self.subTest(field=field,value=value):
                with mock.patch.object(mayor_tools,'_request',side_effect=invalid_asset):
                    result=mayor_tools.call_tool('propose_outside_road_connection',{'roadPrefab':'fixture road'})
                self.assertFalse(result['safe'])
                self.assertEqual(code,result['code'])
                self.assertEqual(0,result['searchDiagnostics']['nativePreviewCalls'])
                self.assertEqual(code,result['provenRootCause']['code'])
                self.assertEqual('currentInvocation',result['provenRootCause']['scope'])
                self.assertEqual('candidateGenerationPrerequisite',result['terminationLayer'])
                self.assertEqual({},result['contributingLayers'])
                progress=__import__('json').loads(mayor_tools.ROAD_SEARCH_PROGRESS_PATH.read_text())
                self.assertEqual('candidateGeneration',progress['rejectingLayer'])
                self.assertEqual('blocked',progress['outcome'])
                self.assertIn('provenRootCause',progress)
                self.assertEqual(code,progress['provenRootCause']['code'])

    def test_adapter_follows_connected_roads_to_exact_existing_junctions(self):
        extra={**self.roads[0],'id':12,'startNodeId':20,'endNodeId':30,
               'start':{'x':-64,'z':0},'end':{'x':-64,'z':-64},
               'startNodeDegree':2,'endNodeDegree':2,
               'startDirection':{'x':0,'z':-1},'endDirection':{'x':0,'z':1}}
        self.roads.append(extra)
        request=self.request
        def with_reach(method,path,**kwargs):
            if path=='/state/road-reachability' and kwargs['query']['id']==12:
                return {'inboundFromOutside':{'found':True},'outboundToOutside':{'found':False}}
            return request(method,path,**kwargs)
        with mock.patch.object(mayor_tools,'_request',side_effect=with_reach):
            result=mayor_tools.call_tool('propose_outside_road_connection',{'roadPrefab':'fixture road'})
        self.assertTrue(result['safe'])
        self.assertTrue(any(path=='/state/network-segment-details' and kw['query']['id']==12
                            for _,path,kw in self.calls))

    def test_native_topology_rewrite_still_rejects_the_whole_route(self):
        self.guard='nativeMoveMiddleNodeRisk'
        result=self.plan()
        self.assertFalse(result['safe'])
        self.assertEqual('boundedSearchCompleted',result['planningCompletionReason'])
        self.assertEqual(0,result['remainingQueuedCandidates'])
        for report in result['corridorReports']:
            self.assertEqual({'nativeTopologyRisk':report['candidateCount']},
                             report['acceptanceRejectionCounts'])

    def test_unknown_recovery_blocks_all_search_before_previews(self):
        candidate={'roadPrefab':'fixture road','start':{'x':192,'z':0},'end':{'x':128,'z':0}}
        record_failure(self.failure_path,load_failures(self.failure_path,'synthetic-city'),candidate,{})
        result=self.plan()
        self.assertFalse(result['safe'])
        self.assertEqual(1,len(self.calls))
        self.assertEqual('recoveryState',result['rejectingLayer'])
        self.assertEqual('recoveryPrerequisite',result['terminationLayer'])
        self.assertEqual('unverifiedRoadRecovery',result['provenRootCause']['code'])
        progress=__import__('json').loads(mayor_tools.ROAD_SEARCH_PROGRESS_PATH.read_text())
        self.assertEqual('finished',progress['status'])
        self.assertEqual('blocked',progress['outcome'])
        self.assertFalse(progress['mutationAuthorized'])

    def test_recovered_failed_alignment_is_not_sent_to_native_again(self):
        candidate={'roadPrefab':'fixture road','start':{'x':192,'z':0},'end':{'x':128,'z':0}}
        record_failure(self.failure_path,load_failures(self.failure_path,'synthetic-city'),candidate,
                       {'recovery':{'localRollbackSucceeded':True}})
        self.assertFalse(self.plan()['safe'])
        for _,path,kwargs in self.calls:
            if path=='/commands/preview-network':
                self.assertNotEqual({128,192},{kwargs['body']['start']['x'],kwargs['body']['end']['x']})

    def test_cash_reserve_covers_the_entire_intended_route(self):
        self.cash=1005000
        self.assertFalse(self.plan()['safe'])

    def test_locked_prefab_is_rejected_before_network_or_preview_calls(self):
        self.unlocked=False
        result=self.plan()
        self.assertFalse(result['safe'])
        self.assertEqual('candidateGenerationPrerequisite',result['terminationLayer'])
        self.assertEqual(['/state/mayor-snapshot','/prefabs/roads'],[c[1] for c in self.calls])


class SharedBudgetTests(unittest.TestCase):
    def test_local_failure_history_rejection_survives_zero_native_calls(self):
        def inner(arguments, read, budget, accepted, preferred_route):
            budget({'start':{'x':0,'z':0},'end':{'x':64,'z':0},'roadPrefab':'fixture'})
            return {'ok':True,'safe':False,'planningCompletionReason':'boundedSearchCompleted',
                    'remainingQueuedCandidates':0,'corridorReports':[]}
        local_rejection=lambda candidate:{'ok':True,'valid':False,'excludedPreviousFailure':True}
        with mock.patch('road_corridor._propose_live_corridor',side_effect=inner):
            result=propose_live_corridor({},lambda *a,**k:{},local_rejection,
                                         lambda preview:False)
        self.assertEqual(0,result['searchDiagnostics']['nativePreviewCalls'])
        self.assertEqual({'failureHistory':1},result['candidateRejectionLayers'])
        self.assertEqual({'failureHistory':1},result['contributingLayers'])
        self.assertEqual('failureHistory',result['rejectingLayer'])
        self.assertEqual('boundedSearchCompletion',result['terminationLayer'])

    def test_exactly_consumed_global_budget_reports_effective_stop(self):
        progress=[]
        def inner(arguments, read, budget, accepted, preferred_route):
            budget({'start':{'x':0,'z':0},'end':{'x':64,'z':0},'roadPrefab':'fixture'})
            return {'ok':True,'safe':False,
                    'planningCompletionReason':'frontierNotExploredWithinSchedule',
                    'remainingQueuedCandidates':50,
                    'goalEligibilitySummary':{'considered':1,'queued':0},
                    'candidateStreamDiagnostics':{'generatedTransitionAttempts':20},
                    'corridorReports':[{'rejections':{'turnPolicy':1}}]}
        with mock.patch('road_corridor._propose_live_corridor',side_effect=inner):
            result=propose_live_corridor({'maxNativePreviews':1},lambda *a,**k:{},
                                         native_preview,lambda preview:preview['valid'],
                                         progress=progress.append)
        self.assertTrue(result['searchDiagnostics']['budgetFullyConsumed'])
        self.assertTrue(result['searchDiagnostics']['budgetLimitReached'])
        self.assertEqual('nativePreviewBudget',result['searchDiagnostics']['stopReason'])
        self.assertEqual('planningBudget',result['terminationLayer'])
        self.assertEqual('routeSearchPolicy',result['rejectingLayer'])
        self.assertEqual('nativePreviewBudget',result['limitingResource'])
        self.assertIsNone(result['provenRootCause'])
        self.assertEqual({'routeSearchPolicy':1},result['contributingLayers'])
        self.assertIn('reverse-reachable',result['nextCheck'])
        self.assertIsNone(progress[-1]['provenRootCause'])
        self.assertEqual({'routeSearchPolicy':1},progress[-1]['contributingLayers'])
        self.assertEqual({'generatedTransitionAttempts':20},
                         progress[-1]['candidateStreamDiagnostics'])

    def test_exactly_consumed_budget_does_not_fake_a_completed_search_stop(self):
        def inner(arguments, read, budget, accepted, preferred_route):
            budget({'start':{'x':0,'z':0},'end':{'x':64,'z':0},'roadPrefab':'fixture'})
            return {'ok':True,'safe':False,'planningCompletionReason':'boundedSearchCompleted',
                    'remainingQueuedCandidates':0,'corridorReports':[]}
        with mock.patch('road_corridor._propose_live_corridor',side_effect=inner):
            result=propose_live_corridor({'maxNativePreviews':1},lambda *a,**k:{},
                                         native_preview,lambda preview:preview['valid'])
        self.assertTrue(result['searchDiagnostics']['budgetLimitReached'])
        self.assertIsNone(result['searchDiagnostics']['stopReason'])
        self.assertEqual('boundedSearchCompletion',result['terminationLayer'])
        self.assertIsNone(result['provenRootCause'])

    def test_goal_ineligibility_requests_goal_approach_diagnostics(self):
        def inner(arguments, read, budget, accepted, preferred_route):
            budget({'start':{'x':0,'z':0},'end':{'x':64,'z':0},'roadPrefab':'fixture'})
            return {'ok':True,'safe':False,'planningCompletionReason':'boundedSearchCompleted',
                    'remainingQueuedCandidates':0,
                    'goalEligibilitySummary':{'considered':8,'queued':0},
                    'corridorReports':[{'rejections':{'turnPolicy':8}}]}
        with mock.patch('road_corridor._propose_live_corridor',side_effect=inner):
            result=propose_live_corridor({},lambda *a,**k:{},native_preview,
                                         lambda preview:preview['valid'])
        self.assertEqual('routeGoalEligibility',result['terminationLayer'])
        self.assertIsNone(result['provenRootCause'])
        self.assertIn('goal-approach',result['nextCheck'])

    def test_route_state_consistency_rejection_is_not_lost_from_contributors(self):
        def inner(arguments, read, budget, accepted, preferred_route):
            budget({'start':{'x':0,'z':0},'end':{'x':64,'z':0},'roadPrefab':'fixture'})
            return {'ok':True,'safe':False,'planningCompletionReason':'boundedSearchCompleted',
                    'remainingQueuedCandidates':0,
                    'corridorReports':[{'rejections':{
                        'futureNodeHeightMismatchOrUnknown':3}}]}
        with mock.patch('road_corridor._propose_live_corridor',side_effect=inner):
            result=propose_live_corridor({},lambda *a,**k:{},native_preview,
                                         lambda preview:preview['valid'])
        self.assertEqual({'routeStateConsistency':3},result['contributingLayers'])
        self.assertEqual('routeStateConsistency',result['rejectingLayer'])

    def test_static_rejections_survive_zero_native_calls_and_zero_goal_consideration(self):
        def inner(arguments, read, budget, accepted, preferred_route):
            return {'ok':True,'safe':False,
                    'planningCompletionReason':'localTrialLimitsReached',
                    'remainingQueuedCandidates':7,
                    'goalEligibilitySummary':{'considered':0,'queued':0},
                    'corridorReports':[{'rejections':{'turnPolicy':4}}]}
        with mock.patch('road_corridor._propose_live_corridor',side_effect=inner):
            result=propose_live_corridor({},lambda *a,**k:{},native_preview,
                                         lambda preview:preview['valid'])
        self.assertEqual(0,result['searchDiagnostics']['nativePreviewCalls'])
        self.assertEqual({},result['candidateRejectionLayers'])
        self.assertEqual({'routeSearchPolicy':4},result['contributingLayers'])
        self.assertEqual('routeSearchPolicy',result['rejectingLayer'])
        self.assertEqual('routeSearchScheduling',result['terminationLayer'])

    def test_local_trial_limit_is_a_scheduling_termination_not_goal_rejection(self):
        def inner(arguments, read, budget, accepted, preferred_route):
            budget({'start':{'x':0,'z':0},'end':{'x':64,'z':0},'roadPrefab':'fixture'})
            return {'ok':True,'safe':False,'planningCompletionReason':'localTrialLimitsReached',
                    'remainingQueuedCandidates':14214,
                    'goalEligibilitySummary':{'considered':115,'queued':0},
                    'corridorReports':[{'rejections':{'turnPolicy':115}}]}
        with mock.patch('road_corridor._propose_live_corridor',side_effect=inner):
            result=propose_live_corridor({},lambda *a,**k:{},native_preview,
                                         lambda preview:preview['valid'])
        self.assertEqual('routeSearchScheduling',result['terminationLayer'])
        self.assertEqual('routeSearchPolicy',result['rejectingLayer'])
        self.assertIsNone(result['provenRootCause'])
        self.assertEqual({'routeSearchPolicy':115},result['contributingLayers'])
        self.assertIn('reverse-reachable',result['nextCheck'])

    def test_route_policy_is_reported_when_native_previews_all_pass(self):
        def inner(arguments, read, budget, accepted, preferred_route):
            budget({'start':{'x':0,'z':0},'end':{'x':64,'z':0},'roadPrefab':'fixture'})
            return {'ok':True,'safe':False,'rejectingLayer':'previewEvaluation',
                    'corridorReports':[{'rejections':{'turnPolicy':1}}]}
        with mock.patch('road_corridor._propose_live_corridor',side_effect=inner):
            result=propose_live_corridor({},lambda *a,**k:{},native_preview,
                                         lambda preview:preview['valid'])
        self.assertEqual({},result['candidateRejectionLayers'])
        self.assertEqual('routeSearchPolicy',result['rejectingLayer'])

    def test_progressive_trials_probe_every_orientation_before_deepening(self):
        calls=[]
        def search(trial, limit):
            calls.append((trial,limit))
            found=trial=='reverse' and limit==24
            return {'found':found,'attempts':[], 'acceptedDepthCounts':{1:1},
                    'localStopReason':None if found else 'trialPreviewBudget',
                    'queueRemaining':10,'candidateCount':limit,'expansions':1,
                    'localMaximumPreviews':limit,'queuedDepthCounts':{1:10}}
        records=run_progressive_trials(['forward','reverse'],search)
        self.assertEqual([('forward',24),('reverse',24)],calls)
        self.assertTrue(records[-1]['result']['found'])

    def test_progressive_trials_deepen_by_stage_and_progress(self):
        calls=[]
        def search(trial, limit):
            calls.append((trial,limit))
            depth=2 if trial=='deep' else 1
            return {'found':False,'attempts':[], 'acceptedDepthCounts':{depth:depth},
                    'localStopReason':'trialPreviewBudget','queueRemaining':10,
                    'candidateCount':limit,'expansions':depth,
                    'localMaximumPreviews':limit,'queuedDepthCounts':{depth:10}}
        run_progressive_trials(['shallow','deep'],search)
        self.assertEqual([('shallow',24),('deep',24),('deep',48),('shallow',48),
                          ('deep',72),('shallow',72),('deep',96),('shallow',96)],calls)

    def test_root_breadth_can_deepen_without_an_accepted_first_edge(self):
        calls=[]
        def search(trial, limit):
            calls.append(limit)
            return {'found':limit==48,'attempts':[], 'acceptedDepthCounts':{},
                    'localStopReason':None if limit==48 else 'trialPreviewBudget',
                    'queueRemaining':20,'candidateCount':limit,'expansions':0,
                    'localMaximumPreviews':limit,'queuedDepthCounts':{1:20}}
        records=run_progressive_trials(['trial'],search,deeper_limits=(48,))
        self.assertEqual([24,48],calls)
        self.assertTrue(records[0]['result']['found'])

    def test_all_pairs_receive_base_probe_before_any_pair_deepens(self):
        calls=[]
        def search(trial, limit):
            calls.append((trial,limit))
            return {'found':trial=='pair-b-reverse' and limit==6,'attempts':[],
                    'acceptedDepthCounts':{1:1},
                    'localStopReason':'trialPreviewBudget','queueRemaining':10,
                    'candidateCount':limit,'expansions':1,
                    'localMaximumPreviews':limit,'queuedDepthCounts':{1:10}}
        records=run_progressive_trials(
            ['pair-a-forward','pair-a-reverse','pair-b-forward','pair-b-reverse'],
            search,initial_limit=6)
        self.assertEqual([('pair-a-forward',6),('pair-a-reverse',6),
                          ('pair-b-forward',6),('pair-b-reverse',6)],calls)
        self.assertTrue(records[-1]['result']['found'])

    def test_exploration_share_leaves_budget_for_promising_pair(self):
        maximum=96
        trial_count=8
        base=min(24,max(1,max(trial_count,maximum//2)//trial_count))
        self.assertEqual(6,base)
        self.assertEqual(48,base*trial_count)
        self.assertGreater(maximum-base*trial_count,0)

    def test_progressive_replay_replaces_attempts_instead_of_double_counting(self):
        def search(trial, limit):
            return {'found':False,'attempts':[{'candidate':i} for i in range(limit)],
                    'acceptedDepthCounts':{1:1},'localStopReason':'trialPreviewBudget',
                    'queueRemaining':1,'candidateCount':limit,'expansions':1,
                    'localMaximumPreviews':limit,'queuedDepthCounts':{1:1}}
        record=run_progressive_trials(['trial'],search,deeper_limits=(48,))[0]
        self.assertEqual(48,len(record['attempts']))

    def test_unaffordable_found_route_does_not_hide_affordable_orientation(self):
        calls=[]
        def search(trial, limit):
            calls.append((trial,limit))
            return {'found':True,'reserveRejected':trial=='expensive','attempts':[],
                    'acceptedDepthCounts':{1:1},'localStopReason':None,
                    'queueRemaining':0,'candidateCount':1,'expansions':1,
                    'localMaximumPreviews':limit,'queuedDepthCounts':{}}
        records=run_progressive_trials(
            ['expensive','affordable'],search,
            is_acceptable=lambda result: result['found'] and not result['reserveRejected'])
        self.assertEqual([('expensive',24),('affordable',24)],calls)
        self.assertTrue(records[-1]['result']['found'])
        self.assertFalse(records[-1]['result']['reserveRejected'])

    def test_exact_cache_preserves_orientation_and_height(self):
        calls=[]
        def preview(c):calls.append(c);return native_preview(c)
        budget=PreviewBudget(preview,lambda p:p['valid'])
        c={'start':{'x':0,'z':0},'end':{'x':64,'z':0},'roadPrefab':'fixture'}
        budget(c);budget({**c,'name':'different label'})
        budget({**c,'start':c['end'],'end':c['start']})
        budget({**c,'planningStartY':100})
        self.assertEqual(3,len(calls));self.assertEqual(1,budget.cache_hits)

    def test_time_limit_stops_before_another_native_call(self):
        with mock.patch('road_corridor.time.monotonic',return_value=0):
            budget=PreviewBudget(lambda c:self.fail('must not request'),lambda p:True,seconds=5)
        with mock.patch('road_corridor.time.monotonic',return_value=6):
            with self.assertRaises(PreviewBudgetExhausted):budget({})
        self.assertEqual(0,budget.calls);self.assertEqual('timeBudget',budget.stop_reason)

    def test_cached_preview_remains_available_after_time_budget(self):
        calls=[]
        with mock.patch('road_corridor.time.monotonic',return_value=0):
            budget=PreviewBudget(lambda c:(calls.append(c) or {'valid':True}),lambda p:True,seconds=5)
            candidate={'start':{'x':0,'z':0},'end':{'x':64,'z':0},'roadPrefab':'fixture'}
            budget(candidate)
        with mock.patch('road_corridor.time.monotonic',return_value=6):
            self.assertTrue(budget(candidate)['valid'])
            with self.assertRaises(PreviewBudgetExhausted):
                budget({**candidate,'end':{'x':128,'z':0}})
        self.assertEqual(1,len(calls))
        self.assertEqual(1,budget.cache_hits)
        self.assertEqual('timeBudget',budget.stop_reason)

    def test_native_errors_are_distinct_from_acceptance_rejections(self):
        budget=PreviewBudget(lambda c:{'valid':False,'toolErrors':c['error']},lambda p:False)
        budget({'error':'OutOfArea'});budget({'error':'None'})
        self.assertEqual({'nativeEngine':1,'predictionOrAcceptancePolicy':1},dict(budget.rejections))

    def test_summary_separates_consumed_budget_from_blocking_stop_reason(self):
        budget=PreviewBudget(native_preview,lambda p:True,maximum=1)
        budget({'start':{'x':0,'z':0},'end':{'x':64,'z':0},'roadPrefab':'fixture'})
        self.assertTrue(budget.summary()['budgetFullyConsumed'])
        self.assertIsNone(budget.summary()['stopReason'])
        with self.assertRaises(PreviewBudgetExhausted):
            budget({'start':{'x':64,'z':0},'end':{'x':128,'z':0},'roadPrefab':'fixture'})
        self.assertEqual('nativePreviewBudget',budget.summary()['stopReason'])


if __name__=='__main__':
    unittest.main()

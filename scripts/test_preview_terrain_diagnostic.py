import copy
import math
import unittest
import mayor_tools as tools

class PreviewTerrainDiagnosticTests(unittest.TestCase):
    def preview(self, start=True, end=False):
        # Synthetic shape mirrors the live blocker: exempt endpoint 0.4m,
        # controlling INTERIOR 0.434m exceeds the unchanged 0.3m budget.
        clearances = [0.4, 0.434, 0.31, 0.2, 0.15, 0.1, 0.09, 0.08, 0.07]
        return {'valid': False, 'reusedStartNode': start, 'reusedEndNode': end,
                'terrainProfile': {'safe': False, 'previewMaximumRoadToTerrainDelta': 0.3},
                'predictionGeometryEvidence': {'source': 'predictedControlBezier',
                    'clearanceModel': 'terrainSeededControlCurve',
                    'predictsSettledSurface': False,
                    'curve': {
                        'a': {'x': 0, 'y': 100+clearances[0], 'z': 0},
                        'd': {'x': 32, 'y': 100+clearances[-1], 'z': 8}},
                    'samples': [{'t': i/8, 'position': {'x': i*4, 'y': 100+c, 'z': i},
                                 'terrainY': 100, 'predictedClearance': c}
                                for i,c in enumerate(clearances)]}}

    def test_reports_controlling_interior_and_exempt_endpoint_separately(self):
        p=self.preview(); before=copy.deepcopy(p)
        d=tools._compact_road_attempt({'preview': p})['preview']['terrainDiagnostic']
        self.assertTrue(d['available'])
        self.assertEqual(1, d['maximumAbsoluteDeviationSample']['sampleIndex'])
        self.assertEqual(0.434, d['maximumAbsoluteDeviationSample']['predictedClearance'])
        self.assertEqual([0], [x['sampleIndex'] for x in d['excludedEndpointSamples']])
        self.assertEqual(8, d['countedSampleCount'])
        self.assertFalse(d['predictsSettledSurface'])
        self.assertEqual(before,p)

    def test_new_and_existing_endpoints_and_negative_deviation(self):
        for start,end in ((False,False),(True,False),(False,True),(True,True)):
            p=self.preview(start,end); samples=p['predictionGeometryEvidence']['samples']
            samples[0]['predictedClearance']=-2; samples[0]['position']['y']=98
            samples[-1]['predictedClearance']=3; samples[-1]['position']['y']=103
            p['predictionGeometryEvidence']['curve']['a']=dict(samples[0]['position'])
            p['predictionGeometryEvidence']['curve']['d']=dict(samples[-1]['position'])
            d=tools._preview_terrain_diagnostic(p)
            expected=8 if not end else (0 if not start else 1)
            self.assertEqual(expected,d['maximumAbsoluteDeviationSample']['sampleIndex'])
            self.assertEqual(9-int(start)-int(end),d['countedSampleCount'])
            self.assertEqual(0 if not start else 7,d['minimumClearanceSample']['sampleIndex'])

    def test_rigid_transforms_and_vertical_datum_preserve_sample_selection(self):
        for angle,dx,dz,dy in ((0,1000,-800,50),(math.pi/2,-40,12,-20),(1.2,400,600,300)):
            p=self.preview()
            for sample in p['predictionGeometryEvidence']['samples']:
                pos=sample['position']; x,z=pos['x'],pos['z']
                pos.update(x=x*math.cos(angle)-z*math.sin(angle)+dx,
                           z=x*math.sin(angle)+z*math.cos(angle)+dz,y=pos['y']+dy)
                sample['terrainY']+=dy
            p['predictionGeometryEvidence']['curve']['a']=dict(p['predictionGeometryEvidence']['samples'][0]['position'])
            p['predictionGeometryEvidence']['curve']['d']=dict(p['predictionGeometryEvidence']['samples'][-1]['position'])
            d=tools._preview_terrain_diagnostic(p)
            self.assertEqual(p['predictionGeometryEvidence']['samples'][1]['position'],d['maximumAbsoluteDeviationSample']['position'])
            self.assertEqual(1,d['maximumAbsoluteDeviationSample']['sampleIndex'])

    def test_missing_or_invalid_evidence_is_unknown(self):
        for key in ('reusedStartNode','reusedEndNode','predictionGeometryEvidence'):
            p=self.preview(); del p[key]
            self.assertFalse(tools._preview_terrain_diagnostic(p)['available'])
        for bad in (None,True,float('nan'),float('inf'),'0.4'):
            p=self.preview(); p['predictionGeometryEvidence']['samples'][1]['predictedClearance']=bad
            self.assertFalse(tools._preview_terrain_diagnostic(p)['available'])

    def test_compact_output_keeps_independent_native_rejection(self):
        p=self.preview(); p['toolErrors']='OutOfArea, ObjectCollision'
        r=tools._compact_road_attempt({'preview':p})['preview']
        self.assertEqual(p['toolErrors'],r['toolErrors'])
        self.assertFalse(r['valid'])
        self.assertNotIn('predictionGeometryEvidence',r)
        self.assertEqual(p['terrainProfile'],r['terrainDiagnostic']['reportedTerrainProfile'])

if __name__=='__main__': unittest.main()

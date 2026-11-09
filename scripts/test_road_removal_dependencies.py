"""Execute the production C# graph analyzer; synthetic topology, not game QA."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
HARNESS = r'''
using System;
using System.Collections.Generic;
using SkylinesAgentBridge;
class Harness {
    static void Main(string[] args) {
        var edges = new List<RoadRemovalDependencies.Edge>();
        var outside = new HashSet<ushort>();
        var access = new List<RoadRemovalDependencies.Access>();
        foreach (string line in System.IO.File.ReadAllLines(args[0])) {
            string[] p = line.Split(',');
            if (p[0] == "e") edges.Add(new RoadRemovalDependencies.Edge {
                Id=ushort.Parse(p[1]), Start=ushort.Parse(p[2]), End=ushort.Parse(p[3]) });
            if (p[0] == "o") outside.Add(ushort.Parse(p[1]));
            if (p[0] == "b") access.Add(new RoadRemovalDependencies.Access {
                Building=ushort.Parse(p[1]), Segment=ushort.Parse(p[2]), Service=p[3]=="1" });
        }
        try { Console.WriteLine(RoadRemovalDependencies.Analyze(ushort.Parse(args[1]), edges, outside, access).ToJson()); }
        catch (ArgumentException) { Console.WriteLine("{\"invalid\":true}"); }
    }
}
'''


class RoadRemovalDependencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        windir = Path(os.environ.get('WINDIR', 'C:/Windows'))
        compiler = windir / 'Microsoft.NET/Framework/v3.5/csc.exe'
        if os.name != 'nt' or not compiler.exists():
            raise unittest.SkipTest('Windows .NET 3.5 compiler required for native C# harness')
        runtime = ROOT / '.runtime/tmp'
        runtime.mkdir(parents=True, exist_ok=True)
        cls.folder = tempfile.TemporaryDirectory(dir=runtime)
        cls.addClassCleanup(cls.folder.cleanup)
        cls.directory = Path(cls.folder.name)
        source = cls.directory / 'Harness.cs'
        source.write_text(HARNESS, encoding='utf-8')
        cls.exe = cls.directory / 'Harness.exe'
        subprocess.run([str(compiler), '/nologo', '/target:exe', '/out:' + str(cls.exe),
                        str(source), str(ROOT / 'src/RoadRemovalDependencies.cs')],
                       check=True, capture_output=True, timeout=30)

    def analyze(self, edges, access=(), outside=(1,), target=10):
        path = self.directory / 'graph.csv'
        lines = [f'e,{i},{a},{b}' for i, a, b in edges]
        lines += [f'b,{i},{s},{int(service)}' for i, s, service in access]
        lines += [f'o,{i}' for i in outside]
        path.write_text('\n'.join(lines), encoding='ascii')
        result = subprocess.run([str(self.exe), str(path), str(target)],
                                capture_output=True, text=True, check=True, timeout=10)
        return json.loads(result.stdout)

    def test_unused_leaf_remains_unapproved_and_incomplete(self):
        r = self.analyze([(10, 2, 3), (11, 1, 2)])
        self.assertTrue(r['separatedAfterRemoval'])
        self.assertEqual((2, 1), (r['startDegreeBefore'], r['endDegreeBefore']))
        self.assertEqual(0, r['endSide']['segmentCount'])
        self.assertEqual(1, r['startSide']['outsideNodeCount'])
        self.assertEqual(0, r['directAccessBuildingCount'])
        self.assertFalse(r['operationalImpactComplete'])
        self.assertFalse(r['removalAuthorized'])

    def test_leaf_can_directly_serve_a_facility(self):
        r = self.analyze([(10, 2, 3), (11, 1, 2)], [(60, 10, True)])
        self.assertEqual([60], r['directAccessBuildings'])
        self.assertEqual(1, r['directAccessServiceCount'])
        self.assertEqual(0, r['endSide']['accessBuildingCount'])

    def test_branch_separates_downstream_residents_and_services(self):
        r = self.analyze([(10, 2, 3), (11, 1, 2), (12, 3, 4), (13, 4, 5)],
                         [(60, 11, False), (61, 12, False), (62, 13, True)])
        self.assertEqual([60], r['startSide']['accessBuildings'])
        self.assertEqual([61, 62], r['endSide']['accessBuildings'])
        self.assertEqual(1, r['endSide']['accessServiceCount'])

    def test_cycles_and_parallel_edges_have_overlapping_sides(self):
        for edges in [[(10, 1, 2), (11, 1, 3), (12, 3, 2)], [(10, 1, 2), (11, 1, 2)]]:
            with self.subTest(edges=edges):
                r = self.analyze(edges, [(60, 11, False)])
                self.assertFalse(r['separatedAfterRemoval'])
                self.assertEqual(r['startSide'], r['endSide'])

    def test_missing_and_stale_native_access_are_unknown(self):
        r = self.analyze([(10, 1, 2)], [(60, 0, False), (61, 999, True)])
        self.assertEqual(2, r['unresolvedRoadAccessBuildingCountCitywide'])
        self.assertEqual(0, r['directAccessBuildingCount'])

    def test_reversed_endpoints_relabeling_and_order_do_not_change_evidence(self):
        a = self.analyze([(10, 2, 3), (11, 1, 2)], [(60, 11, True)])
        b = self.analyze([(211, 101, 102), (210, 103, 102)], [(160, 211, True)], (101,), 210)
        self.assertEqual(a['startSide']['segmentCount'], b['endSide']['segmentCount'])
        self.assertEqual(a['startSide']['accessServiceCount'], b['endSide']['accessServiceCount'])
        self.assertEqual(a['endSide'], b['startSide'])

    def test_counts_remain_complete_when_id_lists_are_bounded(self):
        r = self.analyze([(10, 1, 2)], [(i, 10, False) for i in range(1, 80)])
        self.assertEqual(79, r['directAccessBuildingCount'])
        self.assertEqual(64, len(r['directAccessBuildings']))
        self.assertEqual(64, r['buildingIdListLimit'])

    def test_invalid_graph_or_duplicate_buildings_fail_explicitly(self):
        for edges, access in [([], []), ([(10, 0, 1)], []),
                              ([(10, 1, 2), (10, 2, 3)], []),
                              ([(10, 1, 2)], [(60, 10, False), (60, 10, True)])]:
            with self.subTest(edges=edges, access=access):
                self.assertEqual({'invalid': True}, self.analyze(edges, access))


if __name__ == '__main__':
    unittest.main()

"""Run the production cell layout math; synthetic masks/geometry, not growth QA."""
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
import mayor_tools

ROOT = Path(__file__).resolve().parents[1]
HARNESS = r'''
using System;
using System.Globalization;
using SkylinesAgentBridge;
class Harness {
 static void Main(string[] a) {
  System.Threading.Thread.CurrentThread.CurrentCulture = CultureInfo.InvariantCulture;
  try {
   if (a[0] == "count") Console.WriteLine(ZoneCellLayout.Count(ulong.Parse(a[1]), int.Parse(a[2])));
   else if (a[0] == "bit") Console.WriteLine(ZoneCellLayout.Bit(int.Parse(a[1]), int.Parse(a[2])));
   else { double[] p = ZoneCellLayout.Point(double.Parse(a[1]),double.Parse(a[2]),double.Parse(a[3]),double.Parse(a[4]),double.Parse(a[5])); Console.WriteLine("["+p[0].ToString("R")+","+p[1].ToString("R")+"]"); }
  } catch (ArgumentOutOfRangeException) { Console.WriteLine("null"); }
 }
}
'''


class ZoneCellTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Microsoft.NET/Framework/v3.5/csc.exe'
        if os.name != 'nt' or not compiler.exists():
            raise unittest.SkipTest('Windows C# compiler required')
        runtime = ROOT / '.runtime/tmp'
        runtime.mkdir(parents=True, exist_ok=True)
        cls.folder = tempfile.TemporaryDirectory(dir=runtime)
        cls.addClassCleanup(cls.folder.cleanup)
        cls.directory = Path(cls.folder.name)
        source = cls.directory / 'Harness.cs'
        source.write_text(HARNESS, encoding='utf-8')
        cls.exe = cls.directory / 'Harness.exe'
        subprocess.run([str(compiler), '/nologo', '/target:exe', '/out:' + str(cls.exe),
                        str(source), str(ROOT / 'src/ZoneCellLayout.cs')],
                       check=True, capture_output=True, timeout=30)

    def run_layout(self, *args):
        result = subprocess.run([str(self.exe), *map(str, args)], capture_output=True,
                                text=True, check=True, timeout=10)
        return json.loads(result.stdout)

    def test_upper_rows_and_padding_are_distinct(self):
        # Original live-shaped mask: two valid rows above the old 4-bit indexing range.
        self.assertEqual(5, self.run_layout('count', int('FFFF030700000000', 16), 6))
        self.assertEqual(0, self.run_layout('count', int('F0F0F0F0F0F0F0F0', 16), 8))
        self.assertEqual(32, self.run_layout('count', (1 << 64) - 1, 8))
        self.assertEqual(0, self.run_layout('count', (1 << 64) - 1, 0))

    def test_every_cell_round_trips_without_aliasing(self):
        for z in range(8):
            for x in range(4):
                with self.subTest(x=x, z=z):
                    bit = self.run_layout('bit', x, z)
                    self.assertEqual(2 ** (8 * z + x), bit)
                    self.assertEqual(1, self.run_layout('count', bit, z + 1))
                    self.assertEqual(0, self.run_layout('count', bit, z))

    def test_invalid_coordinates_do_not_wrap_shifts(self):
        for x, z in [(-1, 0), (4, 0), (0, -1), (0, 8)]:
            self.assertIsNone(self.run_layout('bit', x, z))
        for rows in [-1, 9]:
            self.assertIsNone(self.run_layout('count', 1, rows))

    def test_known_footprint_and_rigid_transforms(self):
        self.assertEqual([-28, 28], self.run_layout('point', 0, 0, 0, .5, .5))
        for angle in [0, .3, math.pi / 2, math.pi, -1.2]:
            corners = [self.run_layout('point', 123.4, -55.2, angle, x, z)
                       for x, z in [(1, 2), (2, 2), (2, 3), (1, 3)]]
            for i in range(4):
                self.assertAlmostEqual(8, math.dist(corners[i], corners[(i + 1) % 4]), places=9)
            center = self.run_layout('point', 123.4, -55.2, angle, 1.5, 2.5)
            self.assertAlmostEqual(center[0], sum(p[0] for p in corners) / 4, places=9)
            self.assertAlmostEqual(center[1], sum(p[1] for p in corners) / 4, places=9)

    def test_adapter_is_read_only_and_rejects_unbounded_requests(self):
        evidence = {'ok': True, 'mutationAuthorized': False}
        with mock.patch.object(mayor_tools, '_request', return_value=evidence) as request:
            self.assertEqual(evidence, mayor_tools.call_tool('inspect_zone_cell_geometry', {'blockId': 12}))
        request.assert_called_once_with('GET', '/state/zone-cell-geometry', query={'blockId': 12})
        for arguments in [{}, {'blockId': 0}, {'blockId': 65536}, {'blockId': 1, 'radius': 1000}]:
            with self.assertRaises(ValueError):
                mayor_tools.call_tool('inspect_zone_cell_geometry', arguments)


if __name__ == '__main__':
    unittest.main()

"""Run the production SettledRoadShape rule: settled roads keep slope only through straight Middle nodes."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
HARNESS = r'''
using System;
using System.Globalization;
using SkylinesAgentBridge;
class Harness {
 static void Main(string[] a) {
  float[] v = new float[5];
  for (int i = 0; i < 5; i++) v[i] = float.Parse(a[i], CultureInfo.InvariantCulture);
  Console.WriteLine(SettledRoadShape.KeepsSlope((int)v[0], v[1], v[2], v[3], v[4]) ? "keeps" : "level");
 }
}
'''


class SettledRoadShapeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Microsoft.NET/Framework/v3.5/csc.exe'
        if os.name != 'nt' or not compiler.exists():
            raise unittest.SkipTest('Windows C# compiler required')
        runtime = ROOT / '.runtime/tmp'
        runtime.mkdir(parents=True, exist_ok=True)
        cls.folder = tempfile.TemporaryDirectory(dir=runtime)
        cls.addClassCleanup(cls.folder.cleanup)
        directory = Path(cls.folder.name)
        source = directory / 'Harness.cs'
        source.write_text(HARNESS, encoding='utf-8')
        cls.exe = directory / 'Harness.exe'
        subprocess.run([str(compiler), '/nologo', '/target:exe', '/out:' + str(cls.exe), str(source),
                        str(ROOT / 'src' / 'SettledRoadShape.cs')],
                       check=True, capture_output=True, timeout=60)

    def shape(self, segments, existing, new):
        args = [str(segments), *(str(value) for value in (*existing, *new))]
        return subprocess.run([str(self.exe), *args], capture_output=True, text=True, check=True,
                              timeout=30).stdout.strip()

    def test_new_end_node_levels(self):
        self.assertEqual(self.shape(0, (0, 0), (1, 0)), 'level')

    def test_straight_continuation_keeps_slope(self):
        self.assertEqual(self.shape(1, (-1, 0), (96, 0.5)), 'keeps')

    def test_bend_levels(self):
        # a new road leaving an existing road end at about 145 degrees
        self.assertEqual(self.shape(1, (0.81, 0.58), (-1, 0.03)), 'level')

    def test_slight_angle_is_already_a_bend(self):
        self.assertEqual(self.shape(1, (-1, 0), (1, 0.06)), 'level')

    def test_junction_levels(self):
        self.assertEqual(self.shape(2, (-1, 0), (1, 0)), 'level')


if __name__ == '__main__':
    unittest.main()

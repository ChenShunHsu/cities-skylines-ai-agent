import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent


class LaunchScriptContractTests(unittest.TestCase):
    def _read(self, name: str) -> str:
        return (SCRIPT_DIR / name).read_text(encoding="utf-8-sig")

    def test_resume_does_not_require_obsolete_launcher_window_title(self):
        source = self._read("start-resume.ps1")
        self.assertNotIn(
            'Wait-ProcessWindow "Paradox Launcher" "Cities: Skylines"', source
        )
        self.assertGreaterEqual(
            source.count('Wait-ProcessWindow "Paradox Launcher" ""'), 2
        )

    def test_launch_timeout_reports_visible_same_process_windows(self):
        for script in ("start-resume.ps1", "start-new-map.ps1"):
            with self.subTest(script=script):
                source = self._read(script)
                self.assertIn("Visible candidates: $candidateSummary", source)
                self.assertIn("$_.MainWindowHandle -ne 0", source)

    def test_bridge_is_installed_only_after_the_game_is_stopped(self):
        # build.ps1 refuses to install while Cities.exe runs, so a rebuild
        # issued before the stop step aborts the whole restart.
        for script in ("start-resume.ps1", "start-new-map.ps1"):
            with self.subTest(script=script):
                source = self._read(script).replace("\r\n", "\n")
                stop = source.index("Stop-Process -Force")
                install = source.index('& (Join-Path $PSScriptRoot "build.ps1")\n')
                self.assertLess(stop, install)

    def test_build_failure_stops_before_the_game_is_touched(self):
        # build.ps1 reports a compile error through its exit code, which & does
        # not turn into an exception; unchecked, the old Bridge was relaunched.
        for script in ("start-resume.ps1", "start-new-map.ps1"):
            with self.subTest(script=script):
                source = self._read(script).replace("\r\n", "\n")
                compile_step = source.index('& (Join-Path $PSScriptRoot "build.ps1") -NoInstall')
                self.assertLess(compile_step, source.index("Stop-Process -Force"))
                for call in ('build.ps1") -NoInstall\n', 'build.ps1")\n'):
                    after = source[source.index(call) + len(call):]
                    self.assertTrue(after.lstrip().startswith("if ($LASTEXITCODE -ne 0) { throw"), call)


if __name__ == "__main__":
    unittest.main()

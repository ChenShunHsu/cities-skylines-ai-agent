"""Run the production CommandQueue: a request that times out must never run later."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
HARNESS = r'''
using System;
using System.Threading;
using SkylinesAgentBridge;
class Harness {
 static void Main(string[] a) {
  CommandQueue queue = new CommandQueue();
  CommandResult r;
  if (a[0] == "abandoned") {
   int ran = 0;
   r = queue.RunSync(delegate { ran++; return CommandResult.FromJson("{\"ok\":true}"); }, 50);
   queue.Process(10);
   Console.WriteLine((r.Ok ? "ok" : r.Error) + "|ran=" + ran);
  } else if (a[0] == "running") {
   Thread game = new Thread(delegate () { Thread.Sleep(20); queue.Process(10); });
   game.Start();
   r = queue.RunSync(delegate { Thread.Sleep(300); return CommandResult.FromJson("{\"ok\":true,\"applied\":true}"); }, 100);
   game.Join();
   Console.WriteLine(r.Json);
  } else if (a[0] == "dispatch") {
   // a command meant for the simulation thread is handed to the dispatcher, a main-thread one runs at once
   Thread simulation = null;
   queue.SimulationDispatch = delegate (Action action) { simulation = new Thread(delegate () { action(); }); simulation.Start(); };
   string where = "";
   Thread game = new Thread(delegate () { Thread.Sleep(10); queue.Process(10); });
   game.Start();
   r = queue.RunSync(delegate { where += Thread.CurrentThread == game ? "main" : "simulation"; return CommandResult.FromJson("{\"ok\":true}"); }, 1000);
   game.Join();
   game = new Thread(delegate () { Thread.Sleep(10); queue.Process(10); });
   game.Start();
   queue.RunSync(delegate { where += "," + (Thread.CurrentThread == game ? "main" : "simulation"); return CommandResult.FromJson("{\"ok\":true}"); }, 1000, false);
   game.Join();
   Console.WriteLine(where);
  } else {
   Thread game = new Thread(delegate () { Thread.Sleep(10); queue.Process(10); });
   game.Start();
   r = queue.RunSync(delegate { return CommandResult.FromJson("{\"ok\":true}"); }, 1000);
   game.Join();
   Console.WriteLine(r.Json);
  }
 }
}
'''


class CommandQueueTests(unittest.TestCase):
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
                        *(str(ROOT / 'src' / name) for name in ('CommandQueue.cs', 'CommandResult.cs', 'JsonUtil.cs'))],
                       check=True, capture_output=True, timeout=60)

    def run_case(self, case):
        return subprocess.run([str(self.exe), case], capture_output=True, text=True, check=True,
                              timeout=30).stdout.strip()

    def test_a_command_that_times_out_before_it_starts_is_withdrawn(self):
        # It used to stay queued and run later, so a mutation reported as failed was still applied.
        self.assertEqual('Timed out waiting for the game thread; the command was not run.|ran=0',
                         self.run_case('abandoned'))

    def test_a_command_already_running_at_the_timeout_reports_its_real_result(self):
        self.assertIn('"applied":true', self.run_case('running'))

    def test_simulation_commands_go_to_the_dispatcher_and_main_thread_ones_do_not(self):
        self.assertEqual('simulation,main', self.run_case('dispatch'))

    def test_fast_commands_are_unchanged(self):
        self.assertEqual('{"ok":true}', self.run_case('fast'))


if __name__ == '__main__':
    unittest.main()

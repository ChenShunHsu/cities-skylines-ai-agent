using System;
using System.Collections.Generic;
using System.Threading;

namespace SkylinesAgentBridge
{
    public sealed class CommandQueue
    {
        // A command that already started is finished and reported, never abandoned midway.
        private const int RunningCommandGraceMs = 120000;

        private readonly object gate = new object();
        private readonly Queue<QueuedCommand> commands = new Queue<QueuedCommand>();

        // hands an action to the simulation thread (AgentBridge: SimulationManager.AddAction);
        // unset, a command runs where the queue is processed
        public Action<Action> SimulationDispatch;

        // A command runs on the simulation thread unless it needs the main thread (a save): the
        // game changes buildings, networks and citizens only there (its tools hand every change to
        // SimulationManager.AddAction). Run on the main thread while the simulation thread walked
        // the same lists, a building release threw midway ("Array index is out of range") and left
        // buildings half released on the grid, and growth stacked up to 136 buildings on one lot.
        public CommandResult RunSync(Func<CommandResult> work, int timeoutMs)
        {
            return RunSync(work, timeoutMs, true);
        }

        public CommandResult RunSync(Func<CommandResult> work, int timeoutMs, bool onSimulationThread)
        {
            QueuedCommand command = new QueuedCommand(work, onSimulationThread);

            lock (gate)
            {
                commands.Enqueue(command);
            }

            if (command.Wait(timeoutMs))
            {
                return command.Result;
            }

            // A timed-out command used to stay queued and run later, so a mutation reported as
            // failed could still be applied. Withdraw it if the game thread has not started it.
            if (command.TryAbandon())
            {
                return CommandResult.Fail("Timed out waiting for the game thread; the command was not run.");
            }

            if (command.Wait(RunningCommandGraceMs))
            {
                return command.Result;
            }

            return CommandResult.Fail(
                "Timed out while the game thread is still running the command; its outcome is unknown. Inspect or restore the checkpoint before retrying.");
        }

        public void Process(int maxCount)
        {
            int processed = 0;

            while (processed < maxCount)
            {
                QueuedCommand command = null;

                lock (gate)
                {
                    if (commands.Count == 0)
                    {
                        return;
                    }

                    command = commands.Dequeue();
                }

                if (command.OnSimulationThread && SimulationDispatch != null)
                {
                    // actions run at the start of every simulation step, paused or not
                    QueuedCommand handed = command;
                    SimulationDispatch(delegate { handed.Execute(); });
                    processed++;
                }
                else if (command.Execute())
                {
                    processed++;
                }
            }
        }

        public void Clear()
        {
            lock (gate)
            {
                while (commands.Count > 0)
                {
                    commands.Dequeue().Cancel("Level is unloading.");
                }
            }
        }

        private sealed class QueuedCommand
        {
            private const int Queued = 0;
            private const int Running = 1;
            private const int Abandoned = 2;

            private readonly Func<CommandResult> work;
            private readonly bool onSimulationThread;
            private readonly ManualResetEvent done = new ManualResetEvent(false);
            private CommandResult result;
            private int state = Queued;

            public QueuedCommand(Func<CommandResult> work, bool onSimulationThread)
            {
                this.work = work;
                this.onSimulationThread = onSimulationThread;
            }

            public bool OnSimulationThread
            {
                get { return onSimulationThread; }
            }

            public CommandResult Result
            {
                get { return result; }
            }

            public bool Wait(int timeoutMs)
            {
                return done.WaitOne(timeoutMs, false);
            }

            public bool TryAbandon()
            {
                return Interlocked.CompareExchange(ref state, Abandoned, Queued) == Queued;
            }

            // Returns false when the caller already gave up on the command, which is then skipped.
            public bool Execute()
            {
                if (Interlocked.CompareExchange(ref state, Running, Queued) != Queued)
                {
                    return false;
                }

                try
                {
                    result = work();
                }
                catch (Exception ex)
                {
                    // the game method that threw, so a rare failure can be traced from one occurrence
                    string where = ex.TargetSite == null || ex.TargetSite.DeclaringType == null
                        ? "" : " (in " + ex.TargetSite.DeclaringType.Name + "." + ex.TargetSite.Name + ")";
                    result = CommandResult.Fail(ex.GetType().Name + ": " + ex.Message + where);
                }
                finally
                {
                    done.Set();
                }
                return true;
            }

            public void Cancel(string message)
            {
                if (TryAbandon())
                {
                    result = CommandResult.Fail(message);
                    done.Set();
                }
            }
        }
    }
}

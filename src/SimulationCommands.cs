using ColossalFramework;

namespace SkylinesAgentBridge
{
    public static class SimulationCommands
    {
        public static CommandResult SetSimulationSpeed(string body)
        {
            bool paused = JsonUtil.GetBool(body, "paused", false);
            SimulationManager simulation = Singleton<SimulationManager>.instance;
            // An omitted speed keeps the selected speed, so pausing does not
            // silently reset a later resume to speed 1.
            int speed = (int)JsonUtil.GetNumber(body, "speed", simulation.SelectedSimulationSpeed);

            if (speed < 1)
            {
                speed = 1;
            }
            if (speed > 3)
            {
                speed = 3;
            }

            simulation.ForcedSimulationPaused = false;
            try
            {
                simulation.SelectedSimulationSpeed = speed;
            }
            catch (System.InvalidOperationException ex)
            {
                // The property updates the simulation field before notifying guide
                // triggers. Some resumed saves throw when that notification is made
                // from the simulation thread, so accept only the verified field write.
                if (ex.Message == null ||
                    ex.Message.IndexOf("Already in the same thread") < 0 ||
                    simulation.SelectedSimulationSpeed != speed)
                {
                    throw;
                }
            }
            try
            {
                simulation.SimulationPaused = paused;
            }
            catch (System.InvalidOperationException ex)
            {
                if (ex.Message == null ||
                    ex.Message.IndexOf("Already in the same thread") < 0 ||
                    simulation.SimulationPaused != paused)
                {
                    throw;
                }
            }

            return CommandResult.FromJson("{\"ok\":true,\"paused\":" + JsonUtil.Bool(simulation.SimulationPaused) +
                ",\"selectedSpeed\":" + simulation.SelectedSimulationSpeed +
                ",\"finalSpeed\":" + simulation.FinalSimulationSpeed + "}");
        }
    }
}

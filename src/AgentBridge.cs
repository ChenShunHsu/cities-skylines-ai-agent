using ColossalFramework;
using UnityEngine;

namespace SkylinesAgentBridge
{
    public sealed class AgentBridge
    {
        private static readonly AgentBridge singleton = new AgentBridge();
        private readonly CommandQueue queue = new CommandQueue();
        private ApiServer server;
        private bool levelLoaded;

        public static AgentBridge Instance
        {
            get { return singleton; }
        }

        public bool LevelLoaded
        {
            get { return levelLoaded; }
        }

        public CommandQueue Queue
        {
            get { return queue; }
        }

        public void OnLevelLoaded()
        {
            queue.SimulationDispatch = delegate(System.Action action)
            {
                Singleton<SimulationManager>.instance.AddAction(action);
            };
            levelLoaded = true;
            SaveCommands.MarkLevelLoaded();
            EnsureServer();
            AgentBridgeNotifier.Notify("API ready: Skylines Agent Bridge");
            Debug.Log("[SkylinesAgentBridge] Level loaded. API bridge is ready.");
        }

        public void OnLevelUnloading()
        {
            levelLoaded = false;
            queue.Clear();
            RoadCommands.ClearPendingRoadBuilds();
            AgentBridgeNotifier.Destroy();
            Debug.Log("[SkylinesAgentBridge] Level unloading. Pending API commands cleared.");
        }

        public void ProcessGameThreadQueue(float realTimeDelta)
        {
            queue.Process(4);
            AgentBridgeNotifier.Update(realTimeDelta);
        }

        private void EnsureServer()
        {
            if (server != null && server.IsRunning)
            {
                return;
            }

            server = new ApiServer(this, 32123);
            server.Start();
        }
    }
}

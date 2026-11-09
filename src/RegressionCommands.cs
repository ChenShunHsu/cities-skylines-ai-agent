using System;
using System.Collections.Generic;
using ColossalFramework;

namespace SkylinesAgentBridge
{
    public static class RegressionCommands
    {
        private static readonly Dictionary<string, Snapshot> Baselines =
            new Dictionary<string, Snapshot>(StringComparer.OrdinalIgnoreCase);

        public static CommandResult CaptureBaseline(string body)
        {
            string name = JsonUtil.GetString(body, "name", "default").Trim();
            if (name.Length == 0 || name.Length > 64) return CommandResult.Fail("name must contain 1 to 64 characters.");
            Snapshot snapshot = Capture();
            Baselines[name] = snapshot;
            return CommandResult.FromJson("{\"ok\":true,\"name\":\"" + JsonUtil.Escape(name) +
                "\",\"baseline\":" + snapshot.ToJson() + "}");
        }

        public static CommandResult BuildRegressionReport(string name)
        {
            if (name == null || name.Trim().Length == 0) name = "default";
            Snapshot before;
            if (!Baselines.TryGetValue(name, out before))
            {
                return CommandResult.Fail("Regression baseline was not found: " + name);
            }
            Snapshot after = Capture();
            return CommandResult.FromJson("{\"ok\":true,\"name\":\"" + JsonUtil.Escape(name) +
                "\",\"baseline\":" + before.ToJson() +
                ",\"current\":" + after.ToJson() +
                ",\"delta\":{\"cash\":" + (after.Cash - before.Cash) +
                ",\"periodNet\":" + (after.PeriodNet - before.PeriodNet) +
                ",\"population\":" + ((long)after.Population - before.Population) +
                ",\"problemEntities\":" + (after.ProblemEntities - before.ProblemEntities) +
                ",\"roadSegments\":" + (after.RoadSegments - before.RoadSegments) +
                ",\"trafficDensity\":" + JsonUtil.Number(after.TrafficDensity - before.TrafficDensity) +
                ",\"progressionChanged\":" + JsonUtil.Bool(
                    after.CurrentMilestone != before.CurrentMilestone ||
                    after.CapabilitiesJson != before.CapabilitiesJson) +
                ",\"electricityHeadroom\":" + ((after.ElectricityCapacity - after.ElectricityUsed) - (before.ElectricityCapacity - before.ElectricityUsed)) +
                ",\"waterHeadroom\":" + ((after.WaterCapacity - after.WaterUsed) - (before.WaterCapacity - before.WaterUsed)) +
                ",\"sewageHeadroom\":" + ((after.SewageCapacity - after.SewageUsed) - (before.SewageCapacity - before.SewageUsed)) +
                "}}");
        }

        private static Snapshot Capture()
        {
            Snapshot result = new Snapshot();
            District city = Singleton<DistrictManager>.instance.m_districts.m_buffer[0];
            EconomyManager economy = Singleton<EconomyManager>.instance;
            result.Cash = economy.LastCashAmount;
            result.PeriodNet = economy.LastCashDelta;
            result.Population = city.m_populationData.m_finalCount;
            result.ElectricityUsed = city.GetElectricityConsumption();
            result.ElectricityCapacity = city.GetElectricityCapacity();
            result.WaterUsed = city.GetWaterConsumption();
            result.WaterCapacity = city.GetWaterCapacity();
            result.SewageUsed = city.GetSewageAccumulation();
            result.SewageCapacity = city.GetSewageCapacity();
            UnlockManager unlocks = Singleton<UnlockManager>.instance;
            MilestoneInfo milestone = unlocks.GetCurrentMilestone();
            result.CurrentMilestone = milestone == null ? "" : milestone.name;
            result.CapabilitiesJson = MayorSnapshotCommands.BuildCapabilitiesJson(unlocks);

            BuildingManager buildings = Singleton<BuildingManager>.instance;
            for (int id = 1; id < buildings.m_buildings.m_size; id++)
            {
                Building building = buildings.m_buildings.m_buffer[id];
                if ((building.m_flags & Building.Flags.Created) == Building.Flags.None) continue;
                if (!building.m_problems.IsNone) result.ProblemEntities++;
            }

            NetManager net = Singleton<NetManager>.instance;
            long density = 0;
            int densitySegments = 0;
            for (int id = 1; id < net.m_segments.m_buffer.Length; id++)
            {
                NetSegment segment = net.m_segments.m_buffer[id];
                if ((segment.m_flags & NetSegment.Flags.Created) == NetSegment.Flags.None) continue;
                if (!segment.m_problems.IsNone) result.ProblemEntities++;
                if (segment.Info == null || segment.Info.m_class == null ||
                    segment.Info.m_class.m_service != ItemClass.Service.Road) continue;
                result.RoadSegments++;
                density += segment.m_trafficDensity;
                densitySegments++;
            }
            result.TrafficDensity = densitySegments == 0 ? 0f : (float)density / densitySegments;
            return result;
        }

        private struct Snapshot
        {
            public long Cash;
            public long PeriodNet;
            public uint Population;
            public int ProblemEntities;
            public int RoadSegments;
            public float TrafficDensity;
            public int ElectricityUsed;
            public int ElectricityCapacity;
            public int WaterUsed;
            public int WaterCapacity;
            public int SewageUsed;
            public int SewageCapacity;
            public string CurrentMilestone;
            public string CapabilitiesJson;

            public string ToJson()
            {
                return "{\"cash\":" + Cash +
                    ",\"periodNet\":" + PeriodNet +
                    ",\"population\":" + Population +
                    ",\"problemEntities\":" + ProblemEntities +
                    ",\"roadSegments\":" + RoadSegments +
                    ",\"trafficDensity\":" + JsonUtil.Number(TrafficDensity) +
                    ",\"progression\":{\"currentMilestone\":\"" +
                    JsonUtil.Escape(CurrentMilestone) + "\",\"capabilities\":" +
                    CapabilitiesJson + "}" +
                    ",\"electricity\":{\"used\":" + ElectricityUsed + ",\"capacity\":" + ElectricityCapacity + "}" +
                    ",\"water\":{\"used\":" + WaterUsed + ",\"capacity\":" + WaterCapacity + "}" +
                    ",\"sewage\":{\"used\":" + SewageUsed + ",\"capacity\":" + SewageCapacity + "}}";
            }
        }
    }
}

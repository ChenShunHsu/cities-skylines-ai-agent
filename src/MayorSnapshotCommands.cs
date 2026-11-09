using System;
using System.Text;
using ColossalFramework;

namespace SkylinesAgentBridge
{
    public static class MayorSnapshotCommands
    {
        public static CommandResult BuildMayorSnapshotJson(int detailLimit)
        {
            detailLimit = Clamp(detailLimit, 0, 50);
            District city = Singleton<DistrictManager>.instance.m_districts.m_buffer[0];
            EconomyManager economy = Singleton<EconomyManager>.instance;
            UnlockManager unlocks = Singleton<UnlockManager>.instance;
            SimulationManager simulation = Singleton<SimulationManager>.instance;
            SimulationMetaData metadata = simulation.m_metaData;
            ZoneManager zones = Singleton<ZoneManager>.instance;

            StringBuilder attention = new StringBuilder();
            bool firstAttention = true;
            AddAttention(attention, ref firstAttention, economy.LastCashDelta < 0,
                "negative-period-net", "economy", 40, "One period net is negative; compare reserve and later accounting observations before considering fiscal changes.");
            AddCapacityAttention(attention, ref firstAttention, unlocks.Unlocked(ItemClass.Service.Electricity), "electricity",
                city.GetElectricityConsumption(), city.GetElectricityCapacity());
            AddCapacityAttention(attention, ref firstAttention, unlocks.Unlocked(ItemClass.Service.Water), "water",
                city.GetWaterConsumption(), city.GetWaterCapacity());
            AddCapacityAttention(attention, ref firstAttention, unlocks.Unlocked(ItemClass.Service.Water), "sewage",
                city.GetSewageAccumulation(), city.GetSewageCapacity());
            // Incineration capacity excludes landfill storage, so it cannot establish a
            // garbage shortage; the combined landfill (and cemetery) fill level can.
            AddCapacityAttention(attention, ref firstAttention, unlocks.Unlocked(ItemClass.Service.Garbage), "landfill-storage",
                city.GetGarbageAmount(), city.GetGarbageCapacity());
            AddCapacityAttention(attention, ref firstAttention, unlocks.Unlocked(ItemClass.Service.HealthCare), "cemetery-storage",
                city.GetDeadAmount(), city.GetDeadCapacity());
            AddCapacityAttention(attention, ref firstAttention, unlocks.Unlocked(ItemClass.Service.HealthCare), "healthcare",
                city.GetSickCount(), city.GetHealCapacity());
            AddCapacityAttention(attention, ref firstAttention, unlocks.Unlocked(ItemClass.Service.Education), "elementary-education",
                city.GetEducation1Need(), city.GetEducation1Capacity());
            AddCapacityAttention(attention, ref firstAttention, IsSchoolLevelUnlocked(unlocks, ItemClass.Level.Level2), "high-school-education",
                city.GetEducation2Need(), city.GetEducation2Capacity());
            AddCapacityAttention(attention, ref firstAttention, IsSchoolLevelUnlocked(unlocks, ItemClass.Level.Level3), "university-education",
                city.GetEducation3Need(), city.GetEducation3Capacity());
            AddAttention(attention, ref firstAttention, zones.m_residentialDemand >= 50,
                "residential-demand", "growth", 55, "Residential demand is positive; inspect exact road-generated zoning capacity.");
            AddAttention(attention, ref firstAttention, zones.m_commercialDemand >= 50,
                "commercial-demand", "growth", 45, "Commercial demand is positive; inspect matching zoning capacity.");
            AddAttention(attention, ref firstAttention, zones.m_workplaceDemand >= 50,
                "workplace-demand", "growth", 50, "Workplace demand is positive; inspect industrial and office capacity.");

            MilestoneInfo milestone = unlocks.GetCurrentMilestone();
            CommandResult summary = GameState.BuildSummaryJson();
            CommandResult health = GovernanceCommands.BuildCityHealthJson();
            CommandResult education = EducationCommands.BuildEducationJson();
            CommandResult problems = GameState.BuildProblemsJson(detailLimit);
            CommandResult traffic = TrafficStateCommands.BuildTrafficJson(detailLimit, 160);
            CommandResult roads = GameState.BuildRoadAnomaliesJson(detailLimit, 18f, 32f, false);
            CommandResult buildings = GameState.BuildBuildingAnomaliesJson(detailLimit, 0f, false);
            CommandResult zonesState = GameState.BuildZoneAnomaliesJson(detailLimit, 3, 6, true);
            CommandResult transport = TransportCommands.BuildTransportLineAnomaliesJson(detailLimit);
            CommandResult naturalResources = WorldStateCommands.BuildNaturalResourcesJson(8, Math.Min(detailLimit, 8), 32, true);

            return CommandResult.FromJson("{\"ok\":true" +
                ",\"snapshot\":{\"frame\":" + simulation.m_currentFrameIndex +
                ",\"gameTime\":\"" + JsonUtil.Escape(simulation.m_currentGameTime.ToString("s")) + "\"" +
                ",\"detailLimit\":" + detailLimit + "}" +
                ",\"city\":{\"instanceId\":\"" + JsonUtil.Escape(metadata == null ? "" : metadata.m_gameInstanceIdentifier) + "\"" +
                ",\"name\":\"" + JsonUtil.Escape(metadata == null ? "" : metadata.m_CityName) + "\"" +
                ",\"map\":\"" + JsonUtil.Escape(metadata == null ? "" : metadata.m_MapName) + "\"}" +
                ",\"progression\":{\"currentMilestone\":\"" +
                JsonUtil.Escape(milestone == null ? "" : milestone.name) + "\"" +
                ",\"nextMilestone\":" + UnlockStateCommands.NextMilestoneJson(unlocks) +
                ",\"capabilities\":" + BuildCapabilitiesJson(unlocks) + "}" +
                ",\"attention\":[" + attention.ToString() + "]" +
                ",\"summary\":" + Json(summary) +
                ",\"cityHealth\":" + Json(health) +
                ",\"education\":" + Json(education) +
                ",\"problems\":" + Json(problems) +
                ",\"traffic\":" + Json(traffic) +
                ",\"roadAnomalies\":" + Json(roads) +
                ",\"buildingAnomalies\":" + Json(buildings) +
                ",\"zoneAnomalies\":" + Json(zonesState) +
                ",\"transportAnomalies\":" + Json(transport) +
                ",\"naturalResources\":" + Json(naturalResources) +
                ",\"followUpTools\":[\"inspect_service_coverage\",\"inspect_growth_capacity\",\"list_unlocked_assets\",\"preview_service_impact\"]}");
        }

        internal static string BuildCapabilitiesJson(UnlockManager unlocks)
        {
            return "{" +
                "\"lowDensityResidential\":" + JsonUtil.Bool(unlocks.Unlocked(ItemClass.Zone.ResidentialLow)) +
                ",\"highDensityResidential\":" + JsonUtil.Bool(unlocks.Unlocked(ItemClass.Zone.ResidentialHigh)) +
                ",\"commercial\":" + JsonUtil.Bool(unlocks.Unlocked(ItemClass.Zone.CommercialLow)) +
                ",\"industry\":" + JsonUtil.Bool(unlocks.Unlocked(ItemClass.Zone.Industrial)) +
                ",\"office\":" + JsonUtil.Bool(unlocks.Unlocked(ItemClass.Zone.Office)) +
                ",\"electricity\":" + JsonUtil.Bool(unlocks.Unlocked(ItemClass.Service.Electricity)) +
                ",\"water\":" + JsonUtil.Bool(unlocks.Unlocked(ItemClass.Service.Water)) +
                ",\"garbage\":" + JsonUtil.Bool(unlocks.Unlocked(ItemClass.Service.Garbage)) +
                ",\"healthcare\":" + JsonUtil.Bool(unlocks.Unlocked(ItemClass.Service.HealthCare)) +
                ",\"fire\":" + JsonUtil.Bool(unlocks.Unlocked(ItemClass.Service.FireDepartment)) +
                ",\"police\":" + JsonUtil.Bool(unlocks.Unlocked(ItemClass.Service.PoliceDepartment)) +
                ",\"education\":" + JsonUtil.Bool(unlocks.Unlocked(ItemClass.Service.Education)) +
                ",\"highSchool\":" + JsonUtil.Bool(IsSchoolLevelUnlocked(unlocks, ItemClass.Level.Level2)) +
                ",\"university\":" + JsonUtil.Bool(IsSchoolLevelUnlocked(unlocks, ItemClass.Level.Level3)) +
                ",\"districts\":" + JsonUtil.Bool(unlocks.Unlocked(InfoManager.InfoMode.Districts)) +
                ",\"naturalResources\":" + JsonUtil.Bool(unlocks.Unlocked(InfoManager.InfoMode.NaturalResources)) +
                ",\"bus\":" + JsonUtil.Bool(unlocks.Unlocked(ItemClass.SubService.PublicTransportBus)) +
                ",\"metro\":" + JsonUtil.Bool(unlocks.Unlocked(ItemClass.SubService.PublicTransportMetro)) +
                ",\"train\":" + JsonUtil.Bool(unlocks.Unlocked(ItemClass.SubService.PublicTransportTrain)) +
                "}";
        }

        // The education service unlocks with elementary schools; high schools and universities
        // unlock at later milestones, so a need at those levels is only actionable once a
        // school prefab of that level is placeable.
        internal static bool IsSchoolLevelUnlocked(UnlockManager unlocks, ItemClass.Level level)
        {
            int count = PrefabCollection<BuildingInfo>.LoadedCount();
            for (int i = 0; i < count; i++)
            {
                BuildingInfo info = PrefabCollection<BuildingInfo>.GetLoaded((uint)i);
                if (info == null || info.m_class == null ||
                    info.m_class.m_service != ItemClass.Service.Education ||
                    info.m_class.m_level != level || !(info.m_buildingAI is SchoolAI) ||
                    AssetPolicy.IsBlockedBuildingPrefab(info) || info.GetConstructionCost() <= 0)
                {
                    continue;
                }
                if (info.m_UnlockMilestone == null || unlocks.Unlocked(info.m_UnlockMilestone))
                {
                    return true;
                }
            }
            return false;
        }

        private static void AddCapacityAttention(
            StringBuilder json,
            ref bool first,
            bool unlocked,
            string name,
            int used,
            int capacity)
        {
            // A locked service cannot be built, so a zero-capacity need is not
            // actionable and must not become the snapshot's top urgency.
            if (!unlocked) return;
            float utilization = capacity <= 0 ? (used > 0 ? 1f : 0f) : (float)used / capacity;
            AddAttention(json, ref first, utilization >= 0.9f,
                name + "-capacity", "capacity", utilization >= 1f ? 100 : 80,
                name + " utilization is at or above 90%; inspect live capacity and geographic coverage.");
        }

        private static void AddAttention(
            StringBuilder json,
            ref bool first,
            bool include,
            string id,
            string category,
            int urgency,
            string reason)
        {
            if (!include) return;
            if (!first) json.Append(",");
            json.Append("{\"id\":\"").Append(JsonUtil.Escape(id)).Append("\"");
            json.Append(",\"category\":\"").Append(JsonUtil.Escape(category)).Append("\"");
            json.Append(",\"urgency\":").Append(urgency);
            json.Append(",\"reason\":\"").Append(JsonUtil.Escape(reason)).Append("\"}");
            first = false;
        }

        private static string Json(CommandResult result)
        {
            return result == null || string.IsNullOrEmpty(result.Json)
                ? "{\"ok\":false,\"error\":\"No result\"}"
                : result.Json;
        }

        private static int Clamp(int value, int minimum, int maximum)
        {
            return value < minimum ? minimum : (value > maximum ? maximum : value);
        }
    }
}

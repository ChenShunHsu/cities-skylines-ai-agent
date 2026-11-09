using System;
using System.Text;
using ColossalFramework;
using UnityEngine;

namespace SkylinesAgentBridge
{
    public static class ServiceStateCommands
    {
        public static CommandResult BuildServiceCoverageJson(
            string serviceName,
            float minX,
            float minZ,
            float maxX,
            float maxZ,
            int resolution,
            int lowCoverageThreshold)
        {
            ImmaterialResourceManager.Resource resource;
            if (!TryCoverageResource(serviceName, out resource))
            {
                return CommandResult.Fail("Unsupported service coverage layer: " + serviceName);
            }
            if (maxX <= minX || maxZ <= minZ) return CommandResult.Fail("maxX/maxZ must be greater than minX/minZ.");
            resolution = Clamp(resolution, 2, 64);
            lowCoverageThreshold = Clamp(lowCoverageThreshold, 0, 255);

            ImmaterialResourceManager manager = Singleton<ImmaterialResourceManager>.instance;
            GameAreaManager areas = Singleton<GameAreaManager>.instance;
            StringBuilder samples = new StringBuilder();
            StringBuilder lowCoverage = new StringBuilder();
            bool first = true;
            bool firstLow = true;
            int unlockedSamples = 0;
            int coveredSamples = 0;
            long total = 0;

            for (int row = 0; row < resolution; row++)
            {
                float z = minZ + (maxZ - minZ) * row / (resolution - 1);
                for (int column = 0; column < resolution; column++)
                {
                    float x = minX + (maxX - minX) * column / (resolution - 1);
                    Vector3 position = new Vector3(x, 0f, z);
                    bool unlocked = !areas.PointOutOfArea(position);
                    int value;
                    manager.CheckLocalResource(resource, position, out value);
                    value = Clamp(value, 0, 255);
                    if (unlocked)
                    {
                        unlockedSamples++;
                        total += value;
                        if (value > lowCoverageThreshold) coveredSamples++;
                    }

                    if (!first) samples.Append(",");
                    samples.Append("{\"row\":").Append(row).Append(",\"column\":").Append(column);
                    samples.Append(",\"position\":{\"x\":").Append(JsonUtil.Number(x));
                    samples.Append(",\"z\":").Append(JsonUtil.Number(z)).Append("}");
                    samples.Append(",\"unlocked\":").Append(JsonUtil.Bool(unlocked));
                    samples.Append(",\"value\":").Append(value).Append("}");
                    first = false;

                    if (unlocked && value <= lowCoverageThreshold)
                    {
                        if (!firstLow) lowCoverage.Append(",");
                        lowCoverage.Append("{\"x\":").Append(JsonUtil.Number(x));
                        lowCoverage.Append(",\"z\":").Append(JsonUtil.Number(z));
                        lowCoverage.Append(",\"value\":").Append(value).Append("}");
                        firstLow = false;
                    }
                }
            }

            float average = unlockedSamples == 0 ? 0f : (float)total / unlockedSamples;
            float coveredRatio = unlockedSamples == 0 ? 0f : (float)coveredSamples / unlockedSamples;
            return CommandResult.FromJson("{\"ok\":true,\"service\":\"" + JsonUtil.Escape(serviceName) +
                "\",\"resource\":\"" + resource +
                "\",\"bounds\":{\"minX\":" + JsonUtil.Number(minX) +
                ",\"minZ\":" + JsonUtil.Number(minZ) +
                ",\"maxX\":" + JsonUtil.Number(maxX) +
                ",\"maxZ\":" + JsonUtil.Number(maxZ) +
                "},\"resolution\":" + resolution +
                ",\"lowCoverageThreshold\":" + lowCoverageThreshold +
                ",\"summary\":{\"unlockedSamples\":" + unlockedSamples +
                ",\"average\":" + JsonUtil.Number(average) +
                ",\"coveredRatio\":" + JsonUtil.Number(coveredRatio) +
                "},\"lowCoverageCenters\":[" + lowCoverage.ToString() +
                "],\"samples\":[" + samples.ToString() + "]}");
        }

        public static CommandResult BuildUtilityCoverageJson(
            float minX,
            float minZ,
            float maxX,
            float maxZ,
            int resolution)
        {
            if (maxX <= minX || maxZ <= minZ) return CommandResult.Fail("maxX/maxZ must be greater than minX/minZ.");
            resolution = Clamp(resolution, 2, 64);
            WaterManager water = Singleton<WaterManager>.instance;
            ElectricityManager electricity = Singleton<ElectricityManager>.instance;
            GameAreaManager areas = Singleton<GameAreaManager>.instance;
            StringBuilder samples = new StringBuilder();
            bool first = true;
            int unlockedCount = 0;
            int electricityCount = 0;
            int waterCount = 0;
            int sewageCount = 0;
            int heatingCount = 0;

            for (int row = 0; row < resolution; row++)
            {
                float z = minZ + (maxZ - minZ) * row / (resolution - 1);
                for (int column = 0; column < resolution; column++)
                {
                    float x = minX + (maxX - minX) * column / (resolution - 1);
                    Vector3 position = new Vector3(x, 0f, z);
                    bool unlocked = !areas.PointOutOfArea(position);
                    bool hasElectricity;
                    bool hasWater;
                    bool hasSewage;
                    bool hasHeating;
                    byte pollution;
                    electricity.CheckElectricity(position, out hasElectricity);
                    water.CheckWater(position, out hasWater, out hasSewage, out pollution);
                    water.CheckHeating(position, out hasHeating);

                    if (unlocked)
                    {
                        unlockedCount++;
                        if (hasElectricity) electricityCount++;
                        if (hasWater) waterCount++;
                        if (hasSewage) sewageCount++;
                        if (hasHeating) heatingCount++;
                    }

                    if (!first) samples.Append(",");
                    samples.Append("{\"row\":").Append(row).Append(",\"column\":").Append(column);
                    samples.Append(",\"position\":{\"x\":").Append(JsonUtil.Number(x));
                    samples.Append(",\"z\":").Append(JsonUtil.Number(z)).Append("}");
                    samples.Append(",\"unlocked\":").Append(JsonUtil.Bool(unlocked));
                    samples.Append(",\"electricity\":").Append(JsonUtil.Bool(hasElectricity));
                    samples.Append(",\"water\":").Append(JsonUtil.Bool(hasWater));
                    samples.Append(",\"sewage\":").Append(JsonUtil.Bool(hasSewage));
                    samples.Append(",\"heating\":").Append(JsonUtil.Bool(hasHeating));
                    samples.Append(",\"waterPollution\":").Append(pollution).Append("}");
                    first = false;
                }
            }

            return CommandResult.FromJson("{\"ok\":true,\"bounds\":{\"minX\":" + JsonUtil.Number(minX) +
                ",\"minZ\":" + JsonUtil.Number(minZ) +
                ",\"maxX\":" + JsonUtil.Number(maxX) +
                ",\"maxZ\":" + JsonUtil.Number(maxZ) +
                "},\"resolution\":" + resolution +
                ",\"summary\":{\"unlockedSamples\":" + unlockedCount +
                ",\"electricityCoverage\":" + JsonUtil.Number(Ratio(electricityCount, unlockedCount)) +
                ",\"waterCoverage\":" + JsonUtil.Number(Ratio(waterCount, unlockedCount)) +
                ",\"sewageCoverage\":" + JsonUtil.Number(Ratio(sewageCount, unlockedCount)) +
                ",\"heatingCoverage\":" + JsonUtil.Number(Ratio(heatingCount, unlockedCount)) +
                "},\"samples\":[" + samples.ToString() + "]}");
        }

        public static CommandResult BuildWaterServiceCoverageJson(
            int limit,
            string serviceFilter,
            bool includeCovered,
            bool includeNoDemand)
        {
            limit = Clamp(limit, 0, 50000);
            BuildingManager buildings = Singleton<BuildingManager>.instance;
            WaterManager water = Singleton<WaterManager>.instance;
            StringBuilder items = new StringBuilder();
            StringBuilder services = new StringBuilder();
            System.Collections.Generic.Dictionary<string, int> demandByService =
                new System.Collections.Generic.Dictionary<string, int>();
            int created = 0;
            int requiringWater = 0;
            int covered = 0;
            int uncoveredWater = 0;
            int uncoveredSewage = 0;
            int polluted = 0;
            int returned = 0;
            bool first = true;

            for (ushort id = 1; id < buildings.m_buildings.m_size; id++)
            {
                Building building = buildings.m_buildings.m_buffer[id];
                if ((building.m_flags & Building.Flags.Created) == Building.Flags.None ||
                    (building.m_flags & Building.Flags.Deleted) != Building.Flags.None)
                {
                    continue;
                }

                BuildingInfo info = building.Info;
                if (info == null || info.m_buildingAI == null || info.m_class == null)
                {
                    continue;
                }

                created++;
                string service = info.m_class.m_service.ToString();
                if (serviceFilter != null && serviceFilter.Length > 0 && !EqualsName(serviceFilter, service))
                {
                    continue;
                }

                int waterConsumption = info.m_buildingAI.GetWaterConsumption();
                bool requiresWater = waterConsumption > 0 ||
                    IsZonedGrowableService(info.m_class.m_service);
                if (requiresWater)
                {
                    requiringWater++;
                    if (demandByService.ContainsKey(service)) demandByService[service]++;
                    else demandByService[service] = 1;
                }

                bool hasWater;
                bool hasSewage;
                byte waterPollution;
                water.CheckWater(building.m_position, out hasWater, out hasSewage, out waterPollution);
                if (requiresWater)
                {
                    if (hasWater && hasSewage) covered++;
                    if (!hasWater) uncoveredWater++;
                    if (!hasSewage) uncoveredSewage++;
                    if (waterPollution > 0) polluted++;
                }

                if ((!requiresWater && !includeNoDemand) ||
                    (requiresWater && hasWater && hasSewage && waterPollution == 0 && !includeCovered) ||
                    returned >= limit)
                {
                    continue;
                }

                if (!first) items.Append(",");
                items.Append("{\"id\":").Append(id);
                items.Append(",\"prefab\":\"").Append(JsonUtil.Escape(info.name)).Append("\"");
                items.Append(",\"displayName\":\"").Append(JsonUtil.Escape(info.GetUncheckedLocalizedTitle())).Append("\"");
                items.Append(",\"service\":\"").Append(JsonUtil.Escape(service)).Append("\"");
                items.Append(",\"subService\":\"").Append(JsonUtil.Escape(info.m_class.m_subService.ToString())).Append("\"");
                items.Append(",\"active\":").Append(JsonUtil.Bool((building.m_flags & Building.Flags.Active) != Building.Flags.None));
                items.Append(",\"waterConsumption\":").Append(waterConsumption);
                items.Append(",\"requiresWater\":").Append(JsonUtil.Bool(requiresWater));
                items.Append(",\"hasWater\":").Append(JsonUtil.Bool(hasWater));
                items.Append(",\"hasSewage\":").Append(JsonUtil.Bool(hasSewage));
                items.Append(",\"waterPollution\":").Append(waterPollution);
                items.Append(",\"problems\":\"").Append(JsonUtil.Escape(building.m_problems.IsNone ? "" : building.m_problems.ToString())).Append("\"");
                items.Append(",\"position\":{\"x\":").Append(JsonUtil.Number(building.m_position.x));
                items.Append(",\"y\":").Append(JsonUtil.Number(building.m_position.y));
                items.Append(",\"z\":").Append(JsonUtil.Number(building.m_position.z)).Append("}}");
                first = false;
                returned++;
            }

            bool firstService = true;
            foreach (System.Collections.Generic.KeyValuePair<string, int> pair in demandByService)
            {
                if (!firstService) services.Append(",");
                services.Append("\"").Append(JsonUtil.Escape(pair.Key)).Append("\":").Append(pair.Value);
                firstService = false;
            }

            return CommandResult.FromJson("{\"ok\":true,\"summary\":{\"createdBuildings\":" + created +
                ",\"requiringWater\":" + requiringWater +
                ",\"fullyCovered\":" + covered +
                ",\"missingWater\":" + uncoveredWater +
                ",\"missingSewage\":" + uncoveredSewage +
                ",\"pollutedWater\":" + polluted +
                "},\"demandByService\":{" + services.ToString() + "}" +
                ",\"serviceFilter\":\"" + JsonUtil.Escape(serviceFilter) + "\"" +
                ",\"includeCovered\":" + JsonUtil.Bool(includeCovered) +
                ",\"includeNoDemand\":" + JsonUtil.Bool(includeNoDemand) +
                ",\"returned\":" + returned +
                ",\"buildings\":[" + items.ToString() + "]}");
        }

        public static CommandResult BuildElectricityServiceCoverageJson(
            int limit,
            string serviceFilter,
            bool includeCovered,
            bool includeNoDemand)
        {
            limit = Clamp(limit, 0, 50000);
            BuildingManager buildings = Singleton<BuildingManager>.instance;
            ElectricityManager electricity = Singleton<ElectricityManager>.instance;
            StringBuilder items = new StringBuilder();
            int requiringElectricity = 0;
            int covered = 0;
            int missing = 0;
            int returned = 0;
            bool first = true;

            for (ushort id = 1; id < buildings.m_buildings.m_size; id++)
            {
                Building building = buildings.m_buildings.m_buffer[id];
                if ((building.m_flags & Building.Flags.Created) == Building.Flags.None ||
                    (building.m_flags & Building.Flags.Deleted) != Building.Flags.None) continue;
                BuildingInfo info = building.Info;
                if (info == null || info.m_buildingAI == null || info.m_class == null) continue;
                string service = info.m_class.m_service.ToString();
                if (!string.IsNullOrEmpty(serviceFilter) && !EqualsName(serviceFilter, service)) continue;

                int consumption = info.m_buildingAI.GetElectricityConsumption();
                bool requiresElectricity = consumption > 0 ||
                    IsZonedGrowableService(info.m_class.m_service);
                bool hasElectricity;
                electricity.CheckElectricity(building.m_position, out hasElectricity);
                if (requiresElectricity)
                {
                    requiringElectricity++;
                    if (hasElectricity) covered++; else missing++;
                }
                if ((!requiresElectricity && !includeNoDemand) ||
                    (requiresElectricity && hasElectricity && !includeCovered) ||
                    returned >= limit) continue;

                if (!first) items.Append(",");
                items.Append("{\"id\":").Append(id);
                items.Append(",\"prefab\":\"").Append(JsonUtil.Escape(info.name)).Append("\"");
                items.Append(",\"service\":\"").Append(JsonUtil.Escape(service)).Append("\"");
                items.Append(",\"electricityConsumption\":").Append(consumption);
                items.Append(",\"requiresElectricity\":").Append(JsonUtil.Bool(requiresElectricity));
                items.Append(",\"hasElectricity\":").Append(JsonUtil.Bool(hasElectricity));
                items.Append(",\"problems\":\"").Append(JsonUtil.Escape(building.m_problems.IsNone ? "" : building.m_problems.ToString())).Append("\"");
                items.Append(",\"position\":{\"x\":").Append(JsonUtil.Number(building.m_position.x));
                items.Append(",\"y\":").Append(JsonUtil.Number(building.m_position.y));
                items.Append(",\"z\":").Append(JsonUtil.Number(building.m_position.z)).Append("}}");
                first = false;
                returned++;
            }

            return CommandResult.FromJson("{\"ok\":true,\"summary\":{\"requiringElectricity\":" +
                requiringElectricity + ",\"covered\":" + covered + ",\"missingElectricity\":" + missing +
                "},\"serviceFilter\":\"" + JsonUtil.Escape(serviceFilter) + "\"" +
                ",\"includeCovered\":" + JsonUtil.Bool(includeCovered) +
                ",\"includeNoDemand\":" + JsonUtil.Bool(includeNoDemand) +
                ",\"returned\":" + returned +
                ",\"buildings\":[" + items.ToString() + "]}");
        }

        public static CommandResult BuildElectricityConnectionAreaJson(
            int buildingId,
            float padding,
            int resolution)
        {
            if (buildingId <= 0 || buildingId >= BuildingManager.instance.m_buildings.m_size)
            {
                return CommandResult.Fail("A valid buildingId is required.");
            }
            Building building = BuildingManager.instance.m_buildings.m_buffer[buildingId];
            if ((building.m_flags & Building.Flags.Created) == Building.Flags.None ||
                (building.m_flags & Building.Flags.Deleted) != Building.Flags.None ||
                building.Info == null ||
                building.Info.m_buildingAI == null)
            {
                return CommandResult.Fail("Building was not found: " + buildingId);
            }

            padding = Mathf.Clamp(padding, 32f, 1920f);
            resolution = Clamp(resolution, 8, 128);
            Vector3 center = building.m_position;
            float gridRadius = Mathf.Max(8f, building.Info.m_buildingAI.ElectricityGridRadius());
            float extent = gridRadius + padding;
            float minX = center.x - extent;
            float minZ = center.z - extent;
            float maxX = center.x + extent;
            float maxZ = center.z + extent;
            ElectricityManager electricity = Singleton<ElectricityManager>.instance;
            TerrainManager terrain = Singleton<TerrainManager>.instance;
            StringBuilder cells = new StringBuilder();
            bool first = true;
            int conductiveCount = 0;
            int poweredCount = 0;
            int targetAreaCount = 0;
            int poweredTargetAreaCount = 0;

            for (int row = 0; row < resolution; row++)
            {
                float z = minZ + (maxZ - minZ) * row / (resolution - 1);
                for (int column = 0; column < resolution; column++)
                {
                    float x = minX + (maxX - minX) * column / (resolution - 1);
                    Vector3 point = new Vector3(x, 0f, z);
                    point.y = terrain.SampleRawHeightSmooth(point);
                    bool conductive = electricity.CheckConductivity(point);
                    bool powered;
                    electricity.CheckElectricity(point, out powered);
                    float distance = new Vector2(x - center.x, z - center.z).magnitude;
                    bool insideTargetArea = distance <= gridRadius;
                    if (conductive) conductiveCount++;
                    if (powered) poweredCount++;
                    if (insideTargetArea)
                    {
                        targetAreaCount++;
                        if (powered) poweredTargetAreaCount++;
                    }

                    if (!first) cells.Append(",");
                    cells.Append("{\"row\":").Append(row).Append(",\"column\":").Append(column);
                    cells.Append(",\"position\":{\"x\":").Append(JsonUtil.Number(x));
                    cells.Append(",\"y\":").Append(JsonUtil.Number(point.y));
                    cells.Append(",\"z\":").Append(JsonUtil.Number(z)).Append("}");
                    cells.Append(",\"distanceToBuilding\":").Append(JsonUtil.Number(distance));
                    cells.Append(",\"insideTargetGridRadius\":").Append(JsonUtil.Bool(insideTargetArea));
                    cells.Append(",\"conductive\":").Append(JsonUtil.Bool(conductive));
                    cells.Append(",\"powered\":").Append(JsonUtil.Bool(powered)).Append("}");
                    first = false;
                }
            }

            StringBuilder targetConnectionSamples = new StringBuilder();
            bool firstTarget = true;
            int targetConnectionSampleCount = 0;
            int poweredTargetConnectionSampleCount = 0;
            const int angularSamples = 32;
            float[] radialFractions = new float[] { 0.25f, 0.5f, 0.75f, 0.95f };
            float footprintHalfWidth = building.Info.GetWidth() * 4f;
            float footprintHalfLength = building.Info.GetLength() * 4f;
            // the game's lot frame (Building.CalculatePosition), as in GameState.AppendBuildingSpatial3d
            Vector2 footprintAxisX = new Vector2(Mathf.Cos(building.m_angle), Mathf.Sin(building.m_angle));
            Vector2 footprintAxisZ = new Vector2(-Mathf.Sin(building.m_angle), Mathf.Cos(building.m_angle));
            for (int radialIndex = 0; radialIndex < radialFractions.Length; radialIndex++)
            {
                float radius = gridRadius * radialFractions[radialIndex];
                for (int angularIndex = 0; angularIndex < angularSamples; angularIndex++)
                {
                    float angle = angularIndex * Mathf.PI * 2f / angularSamples;
                    Vector3 point = new Vector3(
                        center.x + Mathf.Cos(angle) * radius,
                        0f,
                        center.z + Mathf.Sin(angle) * radius);
                    point.y = terrain.SampleRawHeightSmooth(point);
                    bool conductive = electricity.CheckConductivity(point);
                    bool powered;
                    electricity.CheckElectricity(point, out powered);
                    Vector2 footprintDelta = new Vector2(point.x - center.x, point.z - center.z);
                    float footprintLocalX = Vector2.Dot(footprintDelta, footprintAxisX);
                    float footprintLocalZ = Vector2.Dot(footprintDelta, footprintAxisZ);
                    bool outsideBuildingFootprint =
                        Mathf.Abs(footprintLocalX) >= footprintHalfWidth + 4f ||
                        Mathf.Abs(footprintLocalZ) >= footprintHalfLength + 4f;
                    if (powered) poweredTargetConnectionSampleCount++;

                    if (!firstTarget) targetConnectionSamples.Append(",");
                    targetConnectionSamples.Append("{\"radialFraction\":").Append(JsonUtil.Number(radialFractions[radialIndex]));
                    targetConnectionSamples.Append(",\"angleDegrees\":").Append(JsonUtil.Number(angle * Mathf.Rad2Deg));
                    targetConnectionSamples.Append(",\"position\":{\"x\":").Append(JsonUtil.Number(point.x));
                    targetConnectionSamples.Append(",\"y\":").Append(JsonUtil.Number(point.y));
                    targetConnectionSamples.Append(",\"z\":").Append(JsonUtil.Number(point.z)).Append("}");
                    targetConnectionSamples.Append(",\"distanceToBuilding\":").Append(JsonUtil.Number(radius));
                    targetConnectionSamples.Append(",\"insideTargetGridRadius\":true");
                    targetConnectionSamples.Append(",\"outsideBuildingFootprint\":").Append(JsonUtil.Bool(outsideBuildingFootprint));
                    targetConnectionSamples.Append(",\"conductive\":").Append(JsonUtil.Bool(conductive));
                    targetConnectionSamples.Append(",\"powered\":").Append(JsonUtil.Bool(powered)).Append("}");
                    firstTarget = false;
                    targetConnectionSampleCount++;
                }
            }

            bool buildingPowered;
            electricity.CheckElectricity(center, out buildingPowered);
            return CommandResult.FromJson("{\"ok\":true,\"building\":{\"id\":" + buildingId +
                ",\"prefab\":\"" + JsonUtil.Escape(building.Info.name) + "\"" +
                ",\"position\":{\"x\":" + JsonUtil.Number(center.x) +
                ",\"y\":" + JsonUtil.Number(center.y) +
                ",\"z\":" + JsonUtil.Number(center.z) + "}" +
                ",\"electricityConsumption\":" + building.Info.m_buildingAI.GetElectricityConsumption() +
                ",\"electricityGridRadius\":" + JsonUtil.Number(gridRadius) +
                ",\"footprintWidth\":" + JsonUtil.Number(footprintHalfWidth * 2f) +
                ",\"footprintLength\":" + JsonUtil.Number(footprintHalfLength * 2f) +
                ",\"powered\":" + JsonUtil.Bool(buildingPowered) + "}" +
                ",\"semantics\":{\"conductive\":\"ElectricityManager.CheckConductivity\"" +
                ",\"powered\":\"ElectricityManager.CheckElectricity\"" +
                ",\"targetArea\":\"BuildingAI.ElectricityGridRadius\"}" +
                ",\"bounds\":{\"minX\":" + JsonUtil.Number(minX) +
                ",\"minZ\":" + JsonUtil.Number(minZ) +
                ",\"maxX\":" + JsonUtil.Number(maxX) +
                ",\"maxZ\":" + JsonUtil.Number(maxZ) + "}" +
                ",\"resolution\":" + resolution +
                ",\"summary\":{\"conductiveSamples\":" + conductiveCount +
                ",\"poweredSamples\":" + poweredCount +
                ",\"targetAreaSamples\":" + targetAreaCount +
                ",\"poweredTargetAreaSamples\":" + poweredTargetAreaCount +
                ",\"targetConnectionSamples\":" + targetConnectionSampleCount +
                ",\"poweredTargetConnectionSamples\":" + poweredTargetConnectionSampleCount + "}" +
                ",\"targetConnectionSamples\":[" + targetConnectionSamples.ToString() + "]" +
                ",\"cells\":[" + cells.ToString() + "]}");
        }

        public static CommandResult BuildServiceCapacityJson(string serviceName)
        {
            District city = Singleton<DistrictManager>.instance.m_districts.m_buffer[0];
            int used;
            int capacity;
            string storage = null;
            if (EqualsName(serviceName, "Electricity")) { used = city.GetElectricityConsumption(); capacity = city.GetElectricityCapacity(); }
            else if (EqualsName(serviceName, "Water")) { used = city.GetWaterConsumption(); capacity = city.GetWaterCapacity(); }
            else if (EqualsName(serviceName, "Sewage")) { used = city.GetSewageAccumulation(); capacity = city.GetSewageCapacity(); }
            else if (EqualsName(serviceName, "Garbage"))
            {
                used = city.GetGarbageAccumulation();
                capacity = city.GetIncinerationCapacity();
                storage = GovernanceCommands.StorageJson(ItemClass.Service.Garbage, city.GetGarbageAmount(), city.GetGarbageCapacity());
            }
            else if (EqualsName(serviceName, "HealthCare")) { used = city.GetSickCount(); capacity = city.GetHealCapacity(); }
            else if (EqualsName(serviceName, "DeathCare"))
            {
                used = city.GetDeadAmount();
                capacity = city.GetCremateCapacity();
                storage = GovernanceCommands.StorageJson(ItemClass.Service.HealthCare, city.GetDeadAmount(), city.GetDeadCapacity());
            }
            else if (EqualsName(serviceName, "PoliceDepartment")) { used = city.GetCriminalAmount(); capacity = city.GetCriminalCapacity(); }
            else if (EqualsName(serviceName, "EducationElementary")) { used = city.GetEducation1Need(); capacity = city.GetEducation1Capacity(); }
            else if (EqualsName(serviceName, "EducationHighSchool")) { used = city.GetEducation2Need(); capacity = city.GetEducation2Capacity(); }
            else if (EqualsName(serviceName, "EducationUniversity")) { used = city.GetEducation3Need(); capacity = city.GetEducation3Capacity(); }
            else return CommandResult.Fail("Unsupported service capacity: " + serviceName);

            int facilities;
            int activeFacilities;
            CountFacilities(serviceName, out facilities, out activeFacilities);
            return CommandResult.FromJson("{\"ok\":true,\"service\":\"" + JsonUtil.Escape(serviceName) +
                "\",\"used\":" + used + ",\"capacity\":" + capacity +
                ",\"utilization\":" + JsonUtil.Number(capacity <= 0 ? (used > 0 ? 1f : 0f) : (float)used / capacity) +
                (storage == null ? "" : ",\"storage\":" + storage) +
                ",\"facilities\":" + facilities + ",\"activeFacilities\":" + activeFacilities + "}");
        }

        private static bool IsZonedGrowableService(ItemClass.Service service)
        {
            return service == ItemClass.Service.Residential ||
                service == ItemClass.Service.Commercial ||
                service == ItemClass.Service.Industrial ||
                service == ItemClass.Service.Office;
        }

        private static void CountFacilities(string serviceName, out int facilities, out int activeFacilities)
        {
            facilities = 0;
            activeFacilities = 0;
            BuildingManager manager = Singleton<BuildingManager>.instance;
            for (int id = 1; id < manager.m_buildings.m_size; id++)
            {
                Building building = manager.m_buildings.m_buffer[id];
                if ((building.m_flags & Building.Flags.Created) == Building.Flags.None) continue;
                BuildingInfo info = building.Info;
                if (info == null || info.m_class == null) continue;
                string service = info.m_class.m_service.ToString();
                if (!EqualsName(serviceName, service) &&
                    !(EqualsName(serviceName, "DeathCare") && EqualsName(service, "HealthCare")) &&
                    !(serviceName.StartsWith("Education") && EqualsName(service, "Education"))) continue;
                facilities++;
                if ((building.m_flags & Building.Flags.Active) != Building.Flags.None) activeFacilities++;
            }
        }

        private static bool TryCoverageResource(string name, out ImmaterialResourceManager.Resource resource)
        {
            if (EqualsName(name, "HealthCare")) { resource = ImmaterialResourceManager.Resource.HealthCare; return true; }
            if (EqualsName(name, "FireDepartment")) { resource = ImmaterialResourceManager.Resource.FireDepartment; return true; }
            if (EqualsName(name, "PoliceDepartment")) { resource = ImmaterialResourceManager.Resource.PoliceDepartment; return true; }
            if (EqualsName(name, "EducationElementary")) { resource = ImmaterialResourceManager.Resource.EducationElementary; return true; }
            if (EqualsName(name, "EducationHighSchool")) { resource = ImmaterialResourceManager.Resource.EducationHighSchool; return true; }
            if (EqualsName(name, "EducationUniversity")) { resource = ImmaterialResourceManager.Resource.EducationUniversity; return true; }
            if (EqualsName(name, "DeathCare")) { resource = ImmaterialResourceManager.Resource.DeathCare; return true; }
            if (EqualsName(name, "PublicTransport")) { resource = ImmaterialResourceManager.Resource.PublicTransport; return true; }
            if (EqualsName(name, "NoisePollution")) { resource = ImmaterialResourceManager.Resource.NoisePollution; return true; }
            if (EqualsName(name, "CrimeRate")) { resource = ImmaterialResourceManager.Resource.CrimeRate; return true; }
            if (EqualsName(name, "Health")) { resource = ImmaterialResourceManager.Resource.Health; return true; }
            if (EqualsName(name, "Wellbeing")) { resource = ImmaterialResourceManager.Resource.Wellbeing; return true; }
            if (EqualsName(name, "LandValue")) { resource = ImmaterialResourceManager.Resource.LandValue; return true; }
            if (EqualsName(name, "FireHazard")) { resource = ImmaterialResourceManager.Resource.FireHazard; return true; }
            if (EqualsName(name, "CargoTransport")) { resource = ImmaterialResourceManager.Resource.CargoTransport; return true; }
            resource = ImmaterialResourceManager.Resource.None;
            return false;
        }

        private static bool EqualsName(string left, string right)
        {
            return string.Equals(left, right, StringComparison.OrdinalIgnoreCase);
        }

        private static int Clamp(int value, int minimum, int maximum)
        {
            return value < minimum ? minimum : (value > maximum ? maximum : value);
        }

        private static float Ratio(int value, int total)
        {
            return total <= 0 ? 0f : (float)value / total;
        }
    }
}

using System;
using System.Reflection;
using System.Text;
using ColossalFramework;
using ColossalFramework.Math;
using UnityEngine;

namespace SkylinesAgentBridge
{
    public static class PlacementCommands
    {
        public static CommandResult PreviewBuildingPlacement(string body)
        {
            string prefabName = JsonUtil.GetString(body, "buildingPrefab", "");
            float x = JsonUtil.GetPointNumber(body, "position", "x", float.NaN);
            float z = JsonUtil.GetPointNumber(body, "position", "z", float.NaN);
            float angleDegrees = JsonUtil.GetNumber(body, "angleDegrees", 0f);
            ushort ignoreBuildingId = (ushort)JsonUtil.GetNumber(body, "ignoreBuildingId", 0f);
            bool requireRoadAccess = JsonUtil.GetBool(body, "requireRoadAccess", false);
            float maxRoadDistance = JsonUtil.GetNumber(body, "maxRoadDistance", 64f);

            if (string.IsNullOrEmpty(prefabName)) return CommandResult.Fail("buildingPrefab is required.");
            if (float.IsNaN(x) || float.IsNaN(z)) return CommandResult.Fail("position.x and position.z are required.");
            if (maxRoadDistance < 0f || maxRoadDistance > 512f) return CommandResult.Fail("maxRoadDistance must be between 0 and 512.");

            BuildingInfo info = PrefabCollection<BuildingInfo>.FindLoaded(prefabName);
            if (info == null) return CommandResult.Fail("Building prefab was not found: " + prefabName);
            if (AssetPolicy.IsBlockedBuildingPrefab(info))
            {
                return CommandResult.Fail("Building prefab is blocked: " + prefabName + " (" + AssetPolicy.BlockReason(prefabName) + ")");
            }

            Vector3 position = new Vector3(x, 0f, z);
            TerrainManager terrain = Singleton<TerrainManager>.instance;
            position.y = terrain.SampleRawHeightSmooth(position);
            float angle = angleDegrees * Mathf.Deg2Rad;
            float halfWidth = info.GetWidth() * 4f;
            float halfLength = info.GetLength() * 4f;
            // The game's lot frame (Building.CalculatePosition, the building quad): x along
            // (cos, sin), z along (-sin, cos). The box here turned the other way, so at any angle
            // but a right angle it was the lot mirrored about its centre (live: a cargo terminal lot
            // 31 degrees off its real one reported a road overlap at every site, and the growables
            // under the real lot were not the ones listed).
            Vector2 axisX = new Vector2(Mathf.Cos(angle), Mathf.Sin(angle));
            Vector2 axisZ = new Vector2(-Mathf.Sin(angle), Mathf.Cos(angle));

            float minHeight = float.MaxValue;
            float maxHeight = float.MinValue;
            float maxWaterDepth = 0f;
            for (int sx = -1; sx <= 1; sx += 2)
            {
                for (int sz = -1; sz <= 1; sz += 2)
                {
                    Vector2 corner = new Vector2(x, z) + axisX * (halfWidth * sx) + axisZ * (halfLength * sz);
                    float height = terrain.SampleRawHeightSmooth(new Vector3(corner.x, 0f, corner.y));
                    if (height < minHeight) minHeight = height;
                    if (height > maxHeight) maxHeight = height;
                    float waterLevel;
                    float waterDepth;
                    Vector3 velocity;
                    Vector3 surface;
                    if (terrain.SampleWaterData(corner, out waterLevel, out waterDepth, out velocity, out surface) && waterDepth > maxWaterDepth)
                    {
                        maxWaterDepth = waterDepth;
                    }
                }
            }

            StringBuilder buildingOverlaps = new StringBuilder();
            int buildingOverlapCount = 0;
            BuildingManager buildings = Singleton<BuildingManager>.instance;
            OrientedBox candidate = new OrientedBox(new Vector2(x, z), axisX, axisZ, halfWidth, halfLength);
            for (ushort id = 1; id < buildings.m_buildings.m_size; id++)
            {
                if (id == ignoreBuildingId) continue;
                Building building = buildings.m_buildings.m_buffer[id];
                if ((building.m_flags & Building.Flags.Created) == Building.Flags.None) continue;
                // Untouchable buildings belong to network nodes (pipe junctions, poles,
                // pillars); the native placement check judges them, and an underground
                // pipe junction otherwise blocked rebuilding a facility on its own lot.
                if ((building.m_flags & Building.Flags.Untouchable) != Building.Flags.None) continue;
                BuildingInfo otherInfo = building.Info;
                if (otherInfo == null) continue;
                float otherAngle = building.m_angle;
                OrientedBox other = new OrientedBox(
                    new Vector2(building.m_position.x, building.m_position.z),
                    new Vector2(Mathf.Cos(otherAngle), Mathf.Sin(otherAngle)),
                    new Vector2(-Mathf.Sin(otherAngle), Mathf.Cos(otherAngle)),
                    otherInfo.GetWidth() * 4f,
                    otherInfo.GetLength() * 4f);
                if (!Overlaps(candidate, other)) continue;
                if (buildingOverlapCount > 0) buildingOverlaps.Append(",");
                buildingOverlaps.Append("{\"id\":").Append(id);
                buildingOverlaps.Append(",\"prefab\":\"").Append(JsonUtil.Escape(otherInfo.name)).Append("\"}");
                buildingOverlapCount++;
                if (buildingOverlapCount >= 50) break;
            }

            StringBuilder roadOverlaps = new StringBuilder();
            int roadOverlapCount = 0;
            ushort nearestRoadId = 0;
            float nearestRoadDistance = float.MaxValue;
            NetManager net = Singleton<NetManager>.instance;
            for (ushort id = 1; id < net.m_segments.m_buffer.Length; id++)
            {
                NetSegment segment = net.m_segments.m_buffer[id];
                if ((segment.m_flags & NetSegment.Flags.Created) == NetSegment.Flags.None || segment.Info == null || segment.Info.m_class == null) continue;
                ItemClass.Service networkService = segment.Info.m_class.m_service;
                // A transport line's segments (bus, metro, train lines) are routes drawn over
                // the roads, not structures; a new bus line blocked rebuilding a full cemetery.
                if (segment.Info.m_netAI is TransportLineAI) continue;
                // Surface rail (and tram) blocks a building as much as a road does; only
                // roads were checked, and five facilities were placed across a train track.
                bool surfaceRail = networkService == ItemClass.Service.PublicTransport &&
                    ((net.m_nodes.m_buffer[segment.m_startNode].m_flags & NetNode.Flags.Underground) == NetNode.Flags.None ||
                     (net.m_nodes.m_buffer[segment.m_endNode].m_flags & NetNode.Flags.Underground) == NetNode.Flags.None);
                if (networkService != ItemClass.Service.Road && !surfaceRail) continue;
                Vector3 start3 = net.m_nodes.m_buffer[segment.m_startNode].m_position;
                Vector3 end3 = net.m_nodes.m_buffer[segment.m_endNode].m_position;
                Vector2 start = new Vector2(start3.x, start3.z);
                Vector2 end = new Vector2(end3.x, end3.z);
                float distance = DistanceSegmentToBox(start, end, candidate);
                if (distance > 0.01f) continue;
                if (roadOverlapCount > 0) roadOverlaps.Append(",");
                roadOverlaps.Append("{\"id\":").Append(id);
                roadOverlaps.Append(",\"prefab\":\"").Append(JsonUtil.Escape(segment.Info.name)).Append("\"}");
                roadOverlapCount++;
                if (roadOverlapCount >= 50) break;
            }

            UnlockManager unlocks = Singleton<UnlockManager>.instance;
            bool unlocked = info.m_UnlockMilestone == null || unlocks.Unlocked(info.m_UnlockMilestone);
            int constructionCost = info.GetConstructionCost();
            int maintenanceCost = info.GetMaintenanceCost();
            bool roadRequiredByAI = RequiresRoadAccess(info);
            bool effectiveRoadAccessRequired = requireRoadAccess || roadRequiredByAI;
            float effectiveMaxRoadDistance = Mathf.Min(maxRoadDistance, 20f);
            int groundPollutionAccumulation;
            int noiseAccumulation;
            info.m_buildingAI.GetPollutionAccumulation(out groundPollutionAccumulation, out noiseAccumulation);
            float groundPollutionRadius = ReadNumber(info.m_buildingAI, new string[] { "m_pollutionRadius" });
            float noiseRadius = ReadNumber(info.m_buildingAI, new string[] { "m_noiseRadius" });
            int residentialInsideGroundPollution = CountResidentialWithin(position, groundPollutionRadius);
            int residentialInsideNoise = CountResidentialWithin(position, noiseRadius);
            byte currentGroundPollution;
            Singleton<NaturalResourceManager>.instance.CheckPollution(position, out currentGroundPollution);
            Vector3 checkedPosition = position;
            float checkedAngle = angle;
            Segment3 connectionSegment = default(Segment3);
            int checkedProductionRate;
            int checkedConstructionCost;
            ToolBase.ToolErrors placementErrors = info.m_buildingAI.CheckBuildPosition(
                ignoreBuildingId,
                ref checkedPosition,
                ref checkedAngle,
                0f,
                0f,
                ref connectionSegment,
                out checkedProductionRate,
                out checkedConstructionCost);
            Vector3 roadAccessPosition;
            Vector3 closestRoadLanePosition;
            bool roadAccessible = TryFindNativeRoadAccess(
                info,
                checkedPosition,
                checkedAngle,
                out nearestRoadId,
                out nearestRoadDistance,
                out roadAccessPosition,
                out closestRoadLanePosition) &&
                nearestRoadDistance <= effectiveMaxRoadDistance;
            string waterPlacement = "null";
            WaterFacilityAI waterFacility = info.m_buildingAI as WaterFacilityAI;
            byte groundWaterIntakePollution = 0;
            if (waterFacility != null)
            {
                Vector3 waterLocation = Building.CalculatePosition(position, angle, waterFacility.m_waterLocationOffset);
                Vector3 closestWater = waterLocation;
                bool waterFound = waterFacility.m_useGroundWater ||
                    terrain.GetClosestWaterPos(ref closestWater, waterFacility.m_maxWaterDistance * 0.5f);
                waterPlacement = "{\"location\":{\"x\":" + JsonUtil.Number(waterLocation.x) +
                    ",\"y\":" + JsonUtil.Number(waterLocation.y) +
                    ",\"z\":" + JsonUtil.Number(waterLocation.z) +
                    "},\"offset\":{\"x\":" + JsonUtil.Number(waterFacility.m_waterLocationOffset.x) +
                    ",\"y\":" + JsonUtil.Number(waterFacility.m_waterLocationOffset.y) +
                    ",\"z\":" + JsonUtil.Number(waterFacility.m_waterLocationOffset.z) +
                    "},\"closestWater\":{\"found\":" + JsonUtil.Bool(waterFound) +
                    ",\"x\":" + JsonUtil.Number(closestWater.x) +
                    ",\"y\":" + JsonUtil.Number(closestWater.y) +
                    ",\"z\":" + JsonUtil.Number(closestWater.z) +
                    "},\"maxDistance\":" + JsonUtil.Number(waterFacility.m_maxWaterDistance) +
                    ",\"waterIntake\":" + waterFacility.m_waterIntake +
                    ",\"waterOutlet\":" + waterFacility.m_waterOutlet +
                    ",\"sewageOutlet\":" + waterFacility.m_sewageOutlet + "}";
                if (waterFacility.m_useGroundWater && waterFacility.m_waterIntake > 0)
                {
                    Singleton<NaturalResourceManager>.instance.CheckPollution(waterLocation, out groundWaterIntakePollution);
                }
            }
            StringBuilder networkPaths = new StringBuilder();
            int networkPathCount = 0;
            if (info.m_paths != null)
            {
                for (int pathIndex = 0; pathIndex < info.m_paths.Length; pathIndex++)
                {
                    BuildingInfo.PathInfo path = info.m_paths[pathIndex];
                    if (path == null || path.m_nodes == null || path.m_nodes.Length == 0) continue;
                    NetInfo pathInfo = path.m_finalNetInfo != null ? path.m_finalNetInfo : path.m_netInfo;
                    if (pathInfo == null) continue;

                    if (networkPathCount > 0) networkPaths.Append(",");
                    networkPaths.Append("{\"index\":").Append(pathIndex);
                    networkPaths.Append(",\"prefab\":\"").Append(JsonUtil.Escape(pathInfo.name)).Append("\"");
                    networkPaths.Append(",\"service\":\"").Append(JsonUtil.Escape(pathInfo.m_class.m_service.ToString())).Append("\"");
                    networkPaths.Append(",\"nodes\":[");
                    for (int nodeIndex = 0; nodeIndex < path.m_nodes.Length; nodeIndex++)
                    {
                        if (nodeIndex > 0) networkPaths.Append(",");
                        Vector3 node = Building.CalculatePosition(position, angle, path.m_nodes[nodeIndex]);
                        networkPaths.Append("{\"x\":").Append(JsonUtil.Number(node.x));
                        networkPaths.Append(",\"y\":").Append(JsonUtil.Number(node.y));
                        networkPaths.Append(",\"z\":").Append(JsonUtil.Number(node.z)).Append("}");
                    }
                    networkPaths.Append("]}");
                    networkPathCount++;
                }
            }
            PowerPlantAI powerPlantAI = info.m_buildingAI as PowerPlantAI;
            bool locationDependentElectricity = powerPlantAI is WindTurbineAI;
            int effectiveProductionRate =
                powerPlantAI != null && !locationDependentElectricity
                    ? 100
                    : checkedProductionRate;
            bool valid = unlocked &&
                buildingOverlapCount == 0 &&
                roadOverlapCount == 0 &&
                maxHeight - minHeight <= 16f &&
                (!effectiveRoadAccessRequired || roadAccessible) &&
                placementErrors == ToolBase.ToolErrors.None &&
                groundWaterIntakePollution == 0 &&
                (powerPlantAI == null || effectiveProductionRate > 0);
            string electricityProfile = BuildElectricityProfile(info, effectiveProductionRate);
            StringBuilder safetyRisks = new StringBuilder();
            int safetyRiskCount = 0;
            AppendRisk(safetyRisks, ref safetyRiskCount, !unlocked, "buildingLocked");
            AppendRisk(safetyRisks, ref safetyRiskCount, buildingOverlapCount > 0, "buildingOverlap");
            AppendRisk(safetyRisks, ref safetyRiskCount, roadOverlapCount > 0, "roadOverlap");
            AppendRisk(safetyRisks, ref safetyRiskCount, maxHeight - minHeight > 16f, "terrainHeightRangeExceeds16m");
            AppendRisk(
                safetyRisks,
                ref safetyRiskCount,
                effectiveRoadAccessRequired && !roadAccessible,
                "requiredRoadAccessMissing");
            AppendRisk(safetyRisks, ref safetyRiskCount, placementErrors != ToolBase.ToolErrors.None, "gamePlacementRejected");
            AppendRisk(safetyRisks, ref safetyRiskCount, groundWaterIntakePollution > 0, "pollutedGroundWaterIntake");
            AppendRisk(
                safetyRisks,
                ref safetyRiskCount,
                powerPlantAI != null && effectiveProductionRate <= 0,
                "zeroElectricityProduction");
            AppendRisk(
                safetyRisks,
                ref safetyRiskCount,
                groundPollutionAccumulation > 0 && residentialInsideGroundPollution > 0,
                "residentialInsideGroundPollutionRadius");
            AppendRisk(
                safetyRisks,
                ref safetyRiskCount,
                noiseAccumulation > 0 && residentialInsideNoise > 0,
                "residentialInsideNoiseRadius");

            return CommandResult.FromJson("{\"ok\":true,\"valid\":" + JsonUtil.Bool(valid) +
                ",\"buildingPrefab\":\"" + JsonUtil.Escape(prefabName) +
                "\",\"position\":{\"x\":" + JsonUtil.Number(x) +
                ",\"y\":" + JsonUtil.Number(position.y) +
                ",\"z\":" + JsonUtil.Number(z) +
                "},\"angleDegrees\":" + JsonUtil.Number(angleDegrees) +
                ",\"footprint\":{\"width\":" + JsonUtil.Number(halfWidth * 2f) +
                ",\"length\":" + JsonUtil.Number(halfLength * 2f) +
                "},\"gamePlacement\":{\"errors\":\"" + JsonUtil.Escape(placementErrors.ToString()) +
                "\",\"position\":{\"x\":" + JsonUtil.Number(checkedPosition.x) +
                ",\"y\":" + JsonUtil.Number(checkedPosition.y) +
                ",\"z\":" + JsonUtil.Number(checkedPosition.z) +
                "},\"angleDegrees\":" + JsonUtil.Number(checkedAngle * Mathf.Rad2Deg) +
                ",\"productionRate\":" + checkedProductionRate +
                ",\"effectiveProductionRate\":" + effectiveProductionRate +
                ",\"constructionCost\":" + checkedConstructionCost +
                "},\"waterPlacement\":" + waterPlacement +
                ",\"electricityProfile\":" + electricityProfile +
                ",\"aiProfile\":" + BuildingAiProfile.Build(info) +
                // what creating it would unlock (a cargo terminal passes the milestone Train Track
                // needs); present only on a Bridge that applies them safely on creation
                ",\"createPassMilestones\":" + GameThreadHelpers.DescribeCreatePassMilestones(info) +
                ",\"unlocked\":" + JsonUtil.Bool(unlocked) +
                ",\"constructionCost\":" + constructionCost +
                ",\"maintenanceCost\":" + maintenanceCost +
                ",\"environmental\":{\"currentGroundPollution\":" + currentGroundPollution +
                ",\"groundWaterIntakePollution\":" + groundWaterIntakePollution +
                ",\"groundPollutionAccumulation\":" + groundPollutionAccumulation +
                ",\"groundPollutionRadius\":" + JsonUtil.Number(groundPollutionRadius) +
                ",\"noiseAccumulation\":" + noiseAccumulation +
                ",\"noiseRadius\":" + JsonUtil.Number(noiseRadius) +
                ",\"residentialBuildingsInsideGroundPollutionRadius\":" + residentialInsideGroundPollution +
                ",\"residentialBuildingsInsideNoiseRadius\":" + residentialInsideNoise +
                ",\"electricityConsumption\":" + info.m_buildingAI.GetElectricityConsumption() +
                ",\"waterConsumption\":" + info.m_buildingAI.GetWaterConsumption() + "}" +
                ",\"safety\":{\"hardSafe\":" + JsonUtil.Bool(valid) +
                ",\"riskCount\":" + safetyRiskCount +
                ",\"risks\":[" + safetyRisks.ToString() + "]}" +
                ",\"terrain\":{\"minHeight\":" + JsonUtil.Number(minHeight) +
                ",\"maxHeight\":" + JsonUtil.Number(maxHeight) +
                ",\"heightRange\":" + JsonUtil.Number(maxHeight - minHeight) +
                ",\"maxWaterDepth\":" + JsonUtil.Number(maxWaterDepth) +
                "},\"roadAccess\":{\"required\":" + JsonUtil.Bool(effectiveRoadAccessRequired) +
                ",\"requestedByCaller\":" + JsonUtil.Bool(requireRoadAccess) +
                ",\"requiredByAI\":" + JsonUtil.Bool(roadRequiredByAI) +
                ",\"accessible\":" + JsonUtil.Bool(roadAccessible) +
                ",\"nearestSegmentId\":" + nearestRoadId +
                ",\"distanceToFootprint\":" + (nearestRoadDistance == float.MaxValue ? "null" : JsonUtil.Number(nearestRoadDistance)) +
                ",\"distanceFromSidewalkToVehicleLane\":" + (nearestRoadDistance == float.MaxValue ? "null" : JsonUtil.Number(nearestRoadDistance)) +
                ",\"maximumDistanceToFootprint\":" + JsonUtil.Number(effectiveMaxRoadDistance) +
                ",\"roadAccessPosition\":{" +
                "\"x\":" + JsonUtil.Number(roadAccessPosition.x) +
                ",\"y\":" + JsonUtil.Number(roadAccessPosition.y) +
                ",\"z\":" + JsonUtil.Number(roadAccessPosition.z) + "}" +
                ",\"closestVehicleLanePosition\":{" +
                "\"x\":" + JsonUtil.Number(closestRoadLanePosition.x) +
                ",\"y\":" + JsonUtil.Number(closestRoadLanePosition.y) +
                ",\"z\":" + JsonUtil.Number(closestRoadLanePosition.z) + "}" +
                "},\"networkPaths\":[" + networkPaths.ToString() +
                "],\"buildingOverlaps\":[" + buildingOverlaps.ToString() +
                "],\"roadOverlaps\":[" + roadOverlaps.ToString() + "]}");
        }

        internal static bool TryFindNativeRoadAccess(
            BuildingInfo info,
            Vector3 position,
            float angle,
            out ushort segmentId,
            out float distance,
            out Vector3 roadAccessPosition,
            out Vector3 closestLanePosition)
        {
            segmentId = 0;
            distance = float.MaxValue;
            closestLanePosition = position;
            float sidewalkOffset = 0f;
            if (info.m_zoningMode == BuildingInfo.ZoningMode.CornerLeft)
            {
                sidewalkOffset = info.m_cellWidth * 4f;
            }
            else if (info.m_zoningMode == BuildingInfo.ZoningMode.CornerRight)
            {
                sidewalkOffset = info.m_cellWidth * -4f;
            }
            roadAccessPosition = Building.CalculateSidewalkPosition(
                position,
                angle,
                info.m_cellLength,
                sidewalkOffset,
                4f);

            NetManager manager = Singleton<NetManager>.instance;
            float bestDistanceSquared = 400f;
            // Same search window as native PlayerBuildingAI.FindRoadAccess: a 40m box around the
            // access point, and a segment counts only if one of its end nodes lies within that box
            // grown by 64m. The middle of a long (>~170m) segment is therefore never an access road,
            // even when its lane is a few metres away; previews that skipped this passed sites the
            // created building then rejected.
            Bounds searchBounds = new Bounds(roadAccessPosition, new Vector3(40f, 40f, 40f));
            for (ushort id = 1; id < manager.m_segments.m_buffer.Length; id++)
            {
                NetSegment segment = manager.m_segments.m_buffer[id];
                NetInfo road = segment.Info;
                if ((segment.m_flags & NetSegment.Flags.Created) == NetSegment.Flags.None ||
                    road == null || road.m_class == null ||
                    road.m_class.m_service != ItemClass.Service.Road ||
                    road.m_netAI == null || road.m_netAI.IsUnderground() ||
                    road.m_netAI.IsOverground() || !(road.m_netAI is RoadBaseAI) ||
                    !road.m_hasPedestrianLanes ||
                    (!road.m_hasForwardVehicleLanes && !road.m_hasBackwardVehicleLanes))
                {
                    continue;
                }
                if (!IsNativeAccessCandidate(manager, segment, searchBounds))
                {
                    continue;
                }

                Vector3 lanePositionA;
                int laneIndexA;
                float laneOffsetA;
                Vector3 lanePositionB;
                int laneIndexB;
                float laneOffsetB;
                if (!segment.GetClosestLanePosition(
                    roadAccessPosition,
                    NetInfo.LaneType.Vehicle | NetInfo.LaneType.TransportVehicle,
                    VehicleInfo.VehicleType.Car,
                    VehicleInfo.VehicleCategory.RoadTransport,
                    VehicleInfo.VehicleType.None,
                    false,
                    out lanePositionA,
                    out laneIndexA,
                    out laneOffsetA,
                    out lanePositionB,
                    out laneIndexB,
                    out laneOffsetB))
                {
                    continue;
                }

                float candidateDistanceSquared = Vector3.SqrMagnitude(
                    roadAccessPosition - lanePositionA);
                if (candidateDistanceSquared >= bestDistanceSquared)
                {
                    continue;
                }
                bestDistanceSquared = candidateDistanceSquared;
                segmentId = id;
                closestLanePosition = lanePositionA;
            }

            if (segmentId == 0)
            {
                return false;
            }
            distance = Mathf.Sqrt(bestDistanceSquared);
            return true;
        }

        private static bool IsNativeAccessCandidate(NetManager manager, NetSegment segment, Bounds bounds)
        {
            return (IsNearSearchBounds(manager.m_nodes.m_buffer[segment.m_startNode].m_position, bounds) ||
                    IsNearSearchBounds(manager.m_nodes.m_buffer[segment.m_endNode].m_position, bounds)) &&
                segment.m_bounds.Intersects(bounds);
        }

        private static bool IsNearSearchBounds(Vector3 node, Bounds bounds)
        {
            return Mathf.Max(
                Mathf.Max(bounds.min.x - 64f - node.x, bounds.min.z - 64f - node.z),
                Mathf.Max(node.x - bounds.max.x - 64f, node.z - bounds.max.z - 64f)) < 0f;
        }

        internal static bool RequiresRoadAccess(BuildingInfo info)
        {
            PlayerBuildingAI playerBuilding = info == null
                ? null
                : info.m_buildingAI as PlayerBuildingAI;
            return playerBuilding != null && playerBuilding.RequireRoadAccess();
        }

        private static string BuildElectricityProfile(BuildingInfo info, int productionRate)
        {
            PowerPlantAI powerPlant = info == null ? null : info.m_buildingAI as PowerPlantAI;
            if (powerPlant == null)
            {
                return "null";
            }

            int maximumProduction = powerPlant.m_electricityProduction;
            int estimatedProduction = maximumProduction * Mathf.Max(0, productionRate) / 100;
            string resourceType = powerPlant.m_resourceType.ToString();
            bool fuelBased = powerPlant.m_resourceConsumption > 0 &&
                !string.Equals(resourceType, "None", StringComparison.OrdinalIgnoreCase);
            bool locationDependent = powerPlant is WindTurbineAI;
            return "{\"aiType\":\"" + JsonUtil.Escape(powerPlant.GetType().Name) + "\"" +
                ",\"renewable\":" + JsonUtil.Bool(powerPlant.m_isRenewable) +
                ",\"locationDependentProduction\":" + JsonUtil.Bool(locationDependent) +
                ",\"productionRatePercent\":" + productionRate +
                ",\"maximumElectricityProduction\":" + maximumProduction +
                ",\"estimatedElectricityProduction\":" + estimatedProduction +
                ",\"estimatedOutputRatio\":" +
                    JsonUtil.Number(maximumProduction <= 0 ? 0f : (float)estimatedProduction / maximumProduction) +
                ",\"fuelBased\":" + JsonUtil.Bool(fuelBased) +
                ",\"resourceType\":\"" + JsonUtil.Escape(resourceType) + "\"" +
                ",\"resourceCapacity\":" + powerPlant.m_resourceCapacity +
                ",\"resourceConsumption\":" + powerPlant.m_resourceConsumption +
                ",\"noiseAccumulation\":" + powerPlant.m_noiseAccumulation +
                ",\"noiseRadius\":" + JsonUtil.Number(powerPlant.m_noiseRadius) +
                ",\"pollutionAccumulation\":" + powerPlant.m_pollutionAccumulation +
                ",\"pollutionRadius\":" + JsonUtil.Number(powerPlant.m_pollutionRadius) + "}";
        }

        private static void AppendRisk(StringBuilder json, ref int count, bool condition, string risk)
        {
            if (!condition) return;
            if (count > 0) json.Append(",");
            json.Append("\"").Append(JsonUtil.Escape(risk)).Append("\"");
            count++;
        }

        private static int CountResidentialWithin(Vector3 position, float radius)
        {
            if (radius <= 0f) return 0;
            float radiusSq = radius * radius;
            int count = 0;
            BuildingManager manager = Singleton<BuildingManager>.instance;
            for (ushort i = 1; i < manager.m_buildings.m_buffer.Length; i++)
            {
                Building building = manager.m_buildings.m_buffer[i];
                if ((building.m_flags & Building.Flags.Created) == Building.Flags.None ||
                    (building.m_flags & Building.Flags.Deleted) != Building.Flags.None ||
                    building.Info == null ||
                    building.Info.m_class == null ||
                    building.Info.m_class.m_service != ItemClass.Service.Residential)
                {
                    continue;
                }
                Vector3 delta = building.m_position - position;
                delta.y = 0f;
                if (delta.sqrMagnitude <= radiusSq) count++;
            }
            return count;
        }

        private static float ReadNumber(object instance, string[] names)
        {
            if (instance == null) return 0f;
            for (Type type = instance.GetType(); type != null; type = type.BaseType)
            {
                for (int i = 0; i < names.Length; i++)
                {
                    FieldInfo field = type.GetField(
                        names[i],
                        BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly);
                    if (field == null) continue;
                    object value = field.GetValue(instance);
                    if (value == null) continue;
                    try { return Convert.ToSingle(value); } catch { }
                }
            }
            return 0f;
        }

        private static bool Overlaps(OrientedBox a, OrientedBox b)
        {
            return AxisOverlaps(a, b, a.AxisX) &&
                AxisOverlaps(a, b, a.AxisZ) &&
                AxisOverlaps(a, b, b.AxisX) &&
                AxisOverlaps(a, b, b.AxisZ);
        }

        private static bool AxisOverlaps(OrientedBox a, OrientedBox b, Vector2 axis)
        {
            float distance = Mathf.Abs(Vector2.Dot(b.Center - a.Center, axis));
            float radiusA = Mathf.Abs(Vector2.Dot(a.AxisX, axis)) * a.HalfWidth +
                Mathf.Abs(Vector2.Dot(a.AxisZ, axis)) * a.HalfLength;
            float radiusB = Mathf.Abs(Vector2.Dot(b.AxisX, axis)) * b.HalfWidth +
                Mathf.Abs(Vector2.Dot(b.AxisZ, axis)) * b.HalfLength;
            return distance < radiusA + radiusB;
        }

        private static float DistanceSegmentToBox(Vector2 start, Vector2 end, OrientedBox box)
        {
            Vector2 localStart = ToLocal(start, box);
            Vector2 localEnd = ToLocal(end, box);
            if (SegmentIntersectsBox(localStart, localEnd, box.HalfWidth, box.HalfLength)) return 0f;
            float best = DistancePointToSegment(new Vector2(-box.HalfWidth, -box.HalfLength), localStart, localEnd);
            best = Mathf.Min(best, DistancePointToSegment(new Vector2(-box.HalfWidth, box.HalfLength), localStart, localEnd));
            best = Mathf.Min(best, DistancePointToSegment(new Vector2(box.HalfWidth, -box.HalfLength), localStart, localEnd));
            best = Mathf.Min(best, DistancePointToSegment(new Vector2(box.HalfWidth, box.HalfLength), localStart, localEnd));
            best = Mathf.Min(best, DistancePointToBox(localStart, box.HalfWidth, box.HalfLength));
            best = Mathf.Min(best, DistancePointToBox(localEnd, box.HalfWidth, box.HalfLength));
            return best;
        }

        private static Vector2 ToLocal(Vector2 point, OrientedBox box)
        {
            Vector2 delta = point - box.Center;
            return new Vector2(Vector2.Dot(delta, box.AxisX), Vector2.Dot(delta, box.AxisZ));
        }

        private static bool SegmentIntersectsBox(Vector2 start, Vector2 end, float halfWidth, float halfLength)
        {
            float tMin = 0f;
            float tMax = 1f;
            Vector2 delta = end - start;
            return Clip(-delta.x, start.x + halfWidth, ref tMin, ref tMax) &&
                Clip(delta.x, halfWidth - start.x, ref tMin, ref tMax) &&
                Clip(-delta.y, start.y + halfLength, ref tMin, ref tMax) &&
                Clip(delta.y, halfLength - start.y, ref tMin, ref tMax);
        }

        private static bool Clip(float p, float q, ref float tMin, ref float tMax)
        {
            if (Mathf.Abs(p) < 0.0001f) return q >= 0f;
            float t = q / p;
            if (p < 0f)
            {
                if (t > tMax) return false;
                if (t > tMin) tMin = t;
            }
            else
            {
                if (t < tMin) return false;
                if (t < tMax) tMax = t;
            }
            return true;
        }

        private static float DistancePointToSegment(Vector2 point, Vector2 start, Vector2 end)
        {
            Vector2 delta = end - start;
            float lengthSq = delta.sqrMagnitude;
            if (lengthSq < 0.0001f) return (point - start).magnitude;
            float t = Mathf.Clamp01(Vector2.Dot(point - start, delta) / lengthSq);
            return (point - (start + delta * t)).magnitude;
        }

        private static float DistancePointToBox(Vector2 point, float halfWidth, float halfLength)
        {
            float dx = Mathf.Max(Mathf.Abs(point.x) - halfWidth, 0f);
            float dz = Mathf.Max(Mathf.Abs(point.y) - halfLength, 0f);
            return Mathf.Sqrt(dx * dx + dz * dz);
        }

        private struct OrientedBox
        {
            public readonly Vector2 Center;
            public readonly Vector2 AxisX;
            public readonly Vector2 AxisZ;
            public readonly float HalfWidth;
            public readonly float HalfLength;

            public OrientedBox(Vector2 center, Vector2 axisX, Vector2 axisZ, float halfWidth, float halfLength)
            {
                Center = center;
                AxisX = axisX;
                AxisZ = axisZ;
                HalfWidth = halfWidth;
                HalfLength = halfLength;
            }
        }
    }
}

using System;
using System.Reflection;
using System.Text;
using ColossalFramework;
using ColossalFramework.Math;
using UnityEngine;

namespace SkylinesAgentBridge
{
    public static class BuildingCommands
    {
        public static CommandResult PlaceBuilding(string body)
        {
            bool dryRun = JsonUtil.GetBool(body, "dryRun", false);
            bool nativePlacementVerified = JsonUtil.GetBool(body, "nativePlacementVerified", false);
            string prefabName = JsonUtil.GetString(body, "buildingPrefab", "");
            float angleDegrees = JsonUtil.GetNumber(body, "angleDegrees", 0f);
            Vector3 position = ReadPoint(body, "position");

            if (prefabName == null || prefabName.Length == 0)
            {
                return CommandResult.Fail("buildingPrefab is required.");
            }

            BuildingInfo info = PrefabCollection<BuildingInfo>.FindLoaded(prefabName);
            if (info == null)
            {
                return CommandResult.Fail("Building prefab was not found: " + prefabName);
            }
            if (AssetPolicy.IsBlockedBuildingPrefab(info))
            {
                return CommandResult.Fail("Building prefab is blocked and must not be used: " + prefabName + " (" + AssetPolicy.BlockReason(prefabName) + ")");
            }
            UnlockManager unlocks = Singleton<UnlockManager>.instance;
            if (info.m_UnlockMilestone != null && !unlocks.Unlocked(info.m_UnlockMilestone))
            {
                return CommandResult.Fail("Building prefab is still locked: " + prefabName);
            }

            TerrainManager terrain = TerrainManager.instance;
            position.y = terrain.SampleRawHeightSmoothWithWater(position, false, 0f);

            if (dryRun)
            {
                return PlacementCommands.PreviewBuildingPlacement(body);
            }

            if (info.m_class != null &&
                !UiStateCommands.IsServiceToolbarUsable(info.m_class.m_service))
            {
                return CommandResult.Fail(
                    "Building service toolbar is still locked or stale: " +
                    info.m_class.m_service + ". Refresh and verify /state/toolbar-unlocks before mutation.");
            }

            SimulationManager simulation = Singleton<SimulationManager>.instance;
            BuildingManager buildings = BuildingManager.instance;
            Randomizer randomizer = simulation.m_randomizer;
            ushort buildingId = 0;
            float angle = angleDegrees * Mathf.Deg2Rad;
            Segment3 connectionSegment = default(Segment3);
            int productionRate;
            int placementConstructionCost;
            ToolBase.ToolErrors placementErrors;
            bool suppressedWindGuideSideEffect = false;
            if (info.m_buildingAI is WindTurbineAI && nativePlacementVerified)
            {
                // The guarded mayor command has already completed the native preview
                // off-thread. Repeating WindTurbineAI.CheckBuildPosition here would
                // activate the tutorial guide and race its queued simulation action.
                placementErrors = ToolBase.ToolErrors.None;
                productionRate = Mathf.Clamp(
                    Mathf.RoundToInt(Singleton<WeatherManager>.instance.SampleWindSpeed(position, true) * 100f),
                    0,
                    100);
                placementConstructionCost = 0;
                suppressedWindGuideSideEffect = true;
            }
            else try
            {
                placementErrors = info.m_buildingAI.CheckBuildPosition(
                    0,
                    ref position,
                    ref angle,
                    0f,
                    0f,
                    ref connectionSegment,
                    out productionRate,
                    out placementConstructionCost);
            }
            catch (InvalidOperationException ex)
            {
                // WindTurbineAI.CheckBuildPosition activates its tutorial guide.  That
                // activation schedules a simulation action and throws when this command
                // is already executing on the simulation thread.  The mayor tool has
                // just run the same native preview off-thread, so suppress only this
                // known guide side effect and retain deterministic wind production.
                if (!(info.m_buildingAI is WindTurbineAI) ||
                    ex.Message == null ||
                    ex.Message.IndexOf("Already in the same thread") < 0)
                {
                    throw;
                }

                placementErrors = ToolBase.ToolErrors.None;
                productionRate = Mathf.Clamp(
                    Mathf.RoundToInt(Singleton<WeatherManager>.instance.SampleWindSpeed(position, true) * 100f),
                    0,
                    100);
                // The value is informational here and the actual economy charge is
                // applied by the normal building creation path.  Do not invoke the
                // economy wrapper from this simulation-thread recovery branch.
                placementConstructionCost = 0;
                suppressedWindGuideSideEffect = true;
            }
            string hardSafetyError = ValidateHardPlacementSafety(info, position, angle, placementErrors, productionRate);
            if (hardSafetyError != null)
            {
                return CommandResult.Fail(hardSafetyError);
            }

            // BuildingManager.CreateBuilding does not charge money; the game's
            // tools charge construction separately, as NetTool does for roads.
            EconomyManager economy = Singleton<EconomyManager>.instance;
            int constructionCost = info.GetConstructionCost();
            if (constructionCost > 0 &&
                economy.PeekResource(EconomyManager.Resource.Construction, constructionCost) != constructionCost)
            {
                return CommandResult.Fail("Not enough money to construct " + prefabName + ".");
            }

            bool recoveredSameThreadCompletion = false;
            bool created;
            // PlayerBuildingAI.CreateBuilding unlocks the building's create-pass milestones
            // mid-way; one that changes a referenced unlock (the first cargo terminal passes
            // "Train Track Requirements") dispatches a UI refresh that throws on this thread, and
            // the rest of the creation (the station's lines and worker units, the building's grid
            // entry and update) never ran. The milestones are applied once the building stands.
            ManualMilestone[] passMilestones = GameThreadHelpers.DetachCreatePassMilestones(info);
            try
            {
                created = buildings.CreateBuilding(
                    out buildingId,
                    ref randomizer,
                    info,
                    position,
                    angle,
                    0,
                    simulation.m_currentBuildIndex);
            }
            catch (InvalidOperationException ex)
            {
                Building partial = buildingId == 0
                    ? default(Building)
                    : buildings.m_buildings.m_buffer[buildingId];
                recoveredSameThreadCompletion =
                    ex.Message != null &&
                    ex.Message.IndexOf("Already in the same thread") >= 0 &&
                    buildingId != 0 &&
                    (partial.m_flags & Building.Flags.Created) != Building.Flags.None &&
                    partial.Info == info;
                if (!recoveredSameThreadCompletion)
                {
                    throw;
                }
                created = true;
            }
            finally
            {
                GameThreadHelpers.RestoreCreatePassMilestones(info, passMilestones);
            }

            simulation.m_randomizer = randomizer;

            if (!created)
            {
                return CommandResult.Fail("Failed to create building.");
            }

            string createPassMilestones = "[]";
            bool createPassMilestonesPassed = true;
            if (buildingId != 0 &&
                (buildings.m_buildings.m_buffer[buildingId].m_flags & Building.Flags.Created) != Building.Flags.None)
            {
                // applied before any rollback below, so a release relocks exactly what was unlocked
                createPassMilestonesPassed = GameThreadHelpers.ChangeCreatePassMilestones(
                    passMilestones, true, out createPassMilestones);
            }

            Building createdBuilding = buildings.m_buildings.m_buffer[buildingId];
            if (buildingId == 0 ||
                (createdBuilding.m_flags & Building.Flags.Created) == Building.Flags.None ||
                createdBuilding.Info != info)
            {
                string rollbackError = null;
                bool rolledBack = buildingId != 0 &&
                    GameThreadHelpers.TryReleaseBuilding(buildings, buildingId, out rollbackError);
                return CommandResult.FailWithRecovery(
                    "Building creation produced an incomplete entity; local rollback " +
                    (rolledBack ? "succeeded." : "failed, restore the checkpoint. " + rollbackError),
                    buildingId != 0,
                    rolledBack,
                    "building",
                    buildingId);
            }

            ushort verifiedAccessSegmentId = 0;
            PlayerBuildingAI createdPlayerBuildingAI = info.m_buildingAI as PlayerBuildingAI;
            bool createdRequiresRoadAccess = PlacementCommands.RequiresRoadAccess(info);
            if (createdRequiresRoadAccess)
            {
                createdPlayerBuildingAI.CheckRoadAccess(buildingId, ref createdBuilding);
                buildings.m_buildings.m_buffer[buildingId] = createdBuilding;
                verifiedAccessSegmentId = createdBuilding.m_accessSegment;
                bool roadAccessFailed =
                    (createdBuilding.m_flags & Building.Flags.RoadAccessFailed) != Building.Flags.None;
                if (verifiedAccessSegmentId == 0 || roadAccessFailed)
                {
                    string roadRollbackError;
                    bool roadRolledBack = GameThreadHelpers.TryReleaseBuilding(
                        buildings,
                        buildingId,
                        out roadRollbackError);
                    return CommandResult.FailWithRecovery(
                        "Building creation did not acquire native road access; local rollback " +
                        (roadRolledBack
                            ? "succeeded."
                            : "failed, restore the checkpoint. " + roadRollbackError),
                        true,
                        roadRolledBack,
                        "building",
                        buildingId);
                }
            }

            simulation.m_currentBuildIndex += 1u;
            int constructionCharged = constructionCost > 0
                ? economy.FetchResource(EconomyManager.Resource.Construction, constructionCost, info.m_class)
                : 0;

            string json = "{\"ok\":true,\"dryRun\":false,\"buildingId\":" + buildingId +
                ",\"constructionCost\":" + constructionCost +
                ",\"constructionCharged\":" + constructionCharged +
                ",\"buildingPrefab\":\"" + JsonUtil.Escape(prefabName) + "\"" +
                ",\"service\":\"" + JsonUtil.Escape(info.m_class.m_service.ToString()) + "\"" +
                ",\"subService\":\"" + JsonUtil.Escape(info.m_class.m_subService.ToString()) + "\"" +
                ",\"nativePlacementVerified\":" + JsonUtil.Bool(nativePlacementVerified) +
                ",\"nativeRoadAccessVerified\":" + JsonUtil.Bool(
                    !createdRequiresRoadAccess || verifiedAccessSegmentId != 0) +
                ",\"accessSegmentId\":" + verifiedAccessSegmentId +
                ",\"suppressedWindGuideSideEffect\":" + JsonUtil.Bool(suppressedWindGuideSideEffect) +
                ",\"recoveredSameThreadCompletion\":" + JsonUtil.Bool(recoveredSameThreadCompletion) +
                ",\"createPassMilestones\":" + createPassMilestones +
                ",\"createPassMilestonesPassed\":" + JsonUtil.Bool(createPassMilestonesPassed) +
                ",\"netNodeId\":" + buildings.m_buildings.m_buffer[buildingId].m_netNode + "}";

            Debug.Log("[SkylinesAgentBridge] Placed building " + buildingId + " with prefab " + prefabName);
            return CommandResult.FromJson(json);
        }

        public static CommandResult MoveBuilding(string body)
        {
            ushort id = (ushort)JsonUtil.GetNumber(body, "id", 0f);
            bool dryRun = JsonUtil.GetBool(body, "dryRun", false);

            if (id == 0)
            {
                return CommandResult.Fail("id is required.");
            }

            BuildingManager buildings = BuildingManager.instance;
            if ((buildings.m_buildings.m_buffer[id].m_flags & Building.Flags.Created) == Building.Flags.None)
            {
                return CommandResult.Fail("Building was not found: " + id);
            }

            Building oldBuilding = buildings.m_buildings.m_buffer[id];
            BuildingInfo info = oldBuilding.Info;
            if (info == null)
            {
                return CommandResult.Fail("Building prefab info was not found for building: " + id);
            }
            if (AssetPolicy.IsBlockedBuildingPrefab(info))
            {
                return CommandResult.Fail("Building prefab is blocked and must not be used for move/recreate: " + info.name + " (" + AssetPolicy.BlockReason(info.name) + ")");
            }
            UnlockManager unlocks = Singleton<UnlockManager>.instance;
            if (info.m_UnlockMilestone != null && !unlocks.Unlocked(info.m_UnlockMilestone))
            {
                return CommandResult.Fail("Building prefab is still locked: " + info.name);
            }

            Vector3 position = ReadPoint(body, "position");
            TerrainManager terrain = TerrainManager.instance;
            position.y = terrain.SampleRawHeightSmoothWithWater(position, false, 0f);
            float angleDegrees = JsonUtil.GetNumber(body, "angleDegrees", oldBuilding.m_angle * Mathf.Rad2Deg);
            float angle = angleDegrees * Mathf.Deg2Rad;
            Segment3 connectionSegment = default(Segment3);
            int productionRate;
            int placementConstructionCost;
            ToolBase.ToolErrors placementErrors = info.m_buildingAI.CheckBuildPosition(
                id,
                ref position,
                ref angle,
                0f,
                0f,
                ref connectionSegment,
                out productionRate,
                out placementConstructionCost);
            string hardSafetyError = ValidateHardPlacementSafety(info, position, angle, placementErrors, productionRate);
            if (hardSafetyError != null)
            {
                return CommandResult.Fail(hardSafetyError);
            }

            if (dryRun)
            {
                return CommandResult.FromJson("{\"ok\":true,\"dryRun\":true,\"valid\":true,\"id\":" + id +
                    ",\"buildingPrefab\":\"" + JsonUtil.Escape(info.name) + "\"" +
                    ",\"gamePlacement\":{\"errors\":\"None\",\"productionRate\":" + productionRate +
                    ",\"constructionCost\":" + placementConstructionCost + "}}");
            }

            SimulationManager simulation = Singleton<SimulationManager>.instance;
            Randomizer randomizer = simulation.m_randomizer;
            ushort newBuildingId;
            bool created;
            // as in PlaceBuilding: create-pass milestones are applied once the building stands
            ManualMilestone[] passMilestones = GameThreadHelpers.DetachCreatePassMilestones(info);
            try
            {
                created = buildings.CreateBuilding(
                    out newBuildingId,
                    ref randomizer,
                    info,
                    position,
                    angle,
                    0,
                    simulation.m_currentBuildIndex);
            }
            finally
            {
                GameThreadHelpers.RestoreCreatePassMilestones(info, passMilestones);
            }

            simulation.m_randomizer = randomizer;
            if (!created)
            {
                return CommandResult.Fail("Failed to create moved building.");
            }
            if (newBuildingId != 0 &&
                (buildings.m_buildings.m_buffer[newBuildingId].m_flags & Building.Flags.Created) != Building.Flags.None)
            {
                GameThreadHelpers.ChangeCreatePassMilestones(passMilestones, true);
            }

            Building newBuilding = buildings.m_buildings.m_buffer[newBuildingId];
            if (newBuildingId == 0 ||
                (newBuilding.m_flags & Building.Flags.Created) == Building.Flags.None ||
                newBuilding.Info != info)
            {
                string incompleteRollbackError = null;
                bool incompleteRolledBack = newBuildingId != 0 &&
                    GameThreadHelpers.TryReleaseBuilding(buildings, newBuildingId, out incompleteRollbackError);
                return CommandResult.FailWithRecovery(
                    "Building move produced an incomplete replacement; local rollback " +
                    (incompleteRolledBack ? "succeeded." : "failed, restore the checkpoint. " + incompleteRollbackError),
                    newBuildingId != 0,
                    incompleteRolledBack,
                    "building",
                    newBuildingId);
            }

            simulation.m_currentBuildIndex += 1u;
            string releaseError;
            if (!GameThreadHelpers.TryReleaseBuilding(buildings, id, out releaseError))
            {
                string rollbackError;
                bool rolledBack = GameThreadHelpers.TryReleaseBuilding(buildings, newBuildingId, out rollbackError);
                return CommandResult.FailWithRecovery(
                    "Building move could not remove the original building; replacement rollback " +
                    (rolledBack
                        ? "succeeded. Original building remains active."
                        : "failed, restore the checkpoint. Release error: " + releaseError +
                          "; rollback error: " + rollbackError),
                    true,
                    rolledBack,
                    "building",
                    newBuildingId);
            }

            ReassignTransportLineDepot(id, newBuildingId);

            string json = "{\"ok\":true,\"dryRun\":false,\"oldBuildingId\":" + id +
                ",\"newBuildingId\":" + newBuildingId +
                ",\"buildingPrefab\":\"" + JsonUtil.Escape(info.name) + "\"" +
                ",\"position\":{\"x\":" + JsonUtil.Number(position.x) +
                ",\"y\":" + JsonUtil.Number(position.y) +
                ",\"z\":" + JsonUtil.Number(position.z) + "}}";

            Debug.Log("[SkylinesAgentBridge] Moved building " + id + " to " + newBuildingId + " with prefab " + info.name);
            return CommandResult.FromJson(json);
        }

        private static void ReassignTransportLineDepot(ushort oldBuildingId, ushort newBuildingId)
        {
            TransportManager transport = Singleton<TransportManager>.instance;
            if (transport == null)
            {
                return;
            }

            for (ushort lineId = 1; lineId < transport.m_lines.m_buffer.Length; lineId++)
            {
                TransportLine line = transport.m_lines.m_buffer[lineId];
                if ((line.m_flags & TransportLine.Flags.Created) == TransportLine.Flags.None)
                {
                    continue;
                }

                if (line.m_building != oldBuildingId)
                {
                    continue;
                }

                line.m_building = newBuildingId;
                transport.m_lines.m_buffer[lineId] = line;
            }
        }

        private static string ValidateHardPlacementSafety(
            BuildingInfo info,
            Vector3 position,
            float angle,
            ToolBase.ToolErrors placementErrors,
            int productionRate)
        {
            if (placementErrors != ToolBase.ToolErrors.None)
            {
                return "Game-native building placement rejected the candidate: " + placementErrors + ".";
            }

            if (PlacementCommands.RequiresRoadAccess(info))
            {
                ushort accessSegmentId;
                float accessDistance;
                Vector3 accessPosition;
                Vector3 lanePosition;
                if (!PlacementCommands.TryFindNativeRoadAccess(
                    info,
                    position,
                    angle,
                    out accessSegmentId,
                    out accessDistance,
                    out accessPosition,
                    out lanePosition))
                {
                    return "Building AI requires road access, but no native ground vehicle lane is within 20 metres of its sidewalk access point.";
                }
            }

            WaterFacilityAI water = info.m_buildingAI as WaterFacilityAI;
            if (water != null && water.m_useGroundWater && water.m_waterIntake > 0)
            {
                Vector3 waterLocation = Building.CalculatePosition(position, angle, water.m_waterLocationOffset);
                byte groundPollution;
                Singleton<NaturalResourceManager>.instance.CheckPollution(waterLocation, out groundPollution);
                if (groundPollution > 0)
                {
                    return "Ground-water intake placement is polluted at the actual water-side location (pollution=" +
                        groundPollution + ").";
                }
            }

            PowerPlantAI powerPlant = info.m_buildingAI as PowerPlantAI;
            if (powerPlant is WindTurbineAI && productionRate <= 0)
            {
                return "Location-dependent electricity facility has zero game-native production at the requested position.";
            }
            if (powerPlant != null &&
                !(powerPlant is WindTurbineAI) &&
                powerPlant.m_electricityProduction <= 0)
            {
                return "Fixed-output electricity facility has no rated production.";
            }

            return null;
        }

        public static CommandResult SetBuildingActive(string body)
        {
            ushort id = (ushort)JsonUtil.GetNumber(body, "id", 0f);
            bool active = JsonUtil.GetBool(body, "active", true);
            bool dryRun = JsonUtil.GetBool(body, "dryRun", false);
            bool confirmRisks = JsonUtil.GetBool(body, "confirmRisks", false);

            if (id == 0)
            {
                return CommandResult.Fail("id is required.");
            }

            BuildingManager buildings = BuildingManager.instance;
            Building building = buildings.m_buildings.m_buffer[id];
            if ((building.m_flags & Building.Flags.Created) == Building.Flags.None)
            {
                return CommandResult.Fail("Building was not found: " + id);
            }

            Building.Flags before = building.m_flags;
            BuildingInfo info = building.Info;
            StringBuilder risks = new StringBuilder();
            int riskCount = 0;
            int storedUnits = building.m_garbageBuffer + building.m_customBuffer1 + building.m_customBuffer2;
            int assignedLines = CountAssignedTransportLines(id);
            bool lastActiveFacility = !active && IsLastActiveFacilityOfType(id, building);
            AppendRisk(risks, ref riskCount, !active && IsStorageFacility(info) && storedUnits > 0, "nonEmptyStorageFacility");
            AppendRisk(risks, ref riskCount, !active && assignedLines > 0, "assignedTransportDepot");
            AppendRisk(risks, ref riskCount, lastActiveFacility, "lastActiveFacilityOfService");
            if (dryRun)
            {
                return CommandResult.FromJson("{\"ok\":true,\"dryRun\":true,\"id\":" + id +
                    ",\"requestedActive\":" + JsonUtil.Bool(active) +
                    ",\"safe\":" + JsonUtil.Bool(riskCount == 0) +
                    ",\"riskCount\":" + riskCount +
                    ",\"risks\":[" + risks.ToString() + "]" +
                    ",\"storedUnits\":" + storedUnits +
                    ",\"assignedTransportLines\":" + assignedLines +
                    ",\"lastActiveFacilityOfService\":" + JsonUtil.Bool(lastActiveFacility) + "}");
            }
            if (riskCount > 0 && !confirmRisks)
            {
                return CommandResult.Fail(
                    "Building operational change contains blocking risks. Preview with dryRun=true and set confirmRisks=true only after capacity, storage, and transport dependencies are verified.");
            }
            if (info != null && info.m_buildingAI != null)
            {
                InvokeManualActivation(info.m_buildingAI, id, ref building, active);
            }

            if (active)
            {
                building.m_flags |= Building.Flags.Active;
            }
            else
            {
                building.m_flags &= ~Building.Flags.Active;
            }
            buildings.m_buildings.m_buffer[id] = building;

            string prefab = info == null ? "" : info.name;
            string json = "{\"ok\":true,\"id\":" + id +
                ",\"active\":" + JsonUtil.Bool(active) +
                ",\"prefab\":\"" + JsonUtil.Escape(prefab) + "\"" +
                ",\"beforeFlags\":\"" + JsonUtil.Escape(before.ToString()) + "\"" +
                ",\"afterFlags\":\"" + JsonUtil.Escape(building.m_flags.ToString()) + "\"}";
            Debug.Log("[SkylinesAgentBridge] Set building " + id + " active=" + active);
            return CommandResult.FromJson(json);
        }

        private static bool IsStorageFacility(BuildingInfo info)
        {
            string aiName = info == null || info.m_buildingAI == null ? "" : info.m_buildingAI.GetType().Name;
            return StringUtil.ContainsIgnoreCase(aiName, "Landfill") ||
                StringUtil.ContainsIgnoreCase(aiName, "Cemetery");
        }

        private static int CountAssignedTransportLines(ushort buildingId)
        {
            int count = 0;
            TransportManager manager = Singleton<TransportManager>.instance;
            for (ushort i = 1; i < manager.m_lines.m_buffer.Length; i++)
            {
                TransportLine line = manager.m_lines.m_buffer[i];
                if ((line.m_flags & TransportLine.Flags.Created) != TransportLine.Flags.None &&
                    line.m_building == buildingId) count++;
            }
            return count;
        }

        private static bool IsLastActiveFacilityOfType(ushort buildingId, Building target)
        {
            BuildingInfo info = target.Info;
            if (info == null || info.m_class == null ||
                (target.m_flags & Building.Flags.Active) == Building.Flags.None) return false;
            if (info.m_class.m_service == ItemClass.Service.Residential ||
                info.m_class.m_service == ItemClass.Service.Commercial ||
                info.m_class.m_service == ItemClass.Service.Industrial ||
                info.m_class.m_service == ItemClass.Service.Office) return false;

            BuildingManager manager = Singleton<BuildingManager>.instance;
            for (ushort i = 1; i < manager.m_buildings.m_buffer.Length; i++)
            {
                if (i == buildingId) continue;
                Building other = manager.m_buildings.m_buffer[i];
                if ((other.m_flags & (Building.Flags.Created | Building.Flags.Active)) !=
                    (Building.Flags.Created | Building.Flags.Active) ||
                    other.Info == null ||
                    other.Info.m_class == null) continue;
                if (other.Info.m_class.m_service == info.m_class.m_service &&
                    other.Info.m_class.m_subService == info.m_class.m_subService) return false;
            }
            return true;
        }

        private static void AppendRisk(StringBuilder json, ref int count, bool condition, string risk)
        {
            if (!condition) return;
            if (count > 0) json.Append(",");
            json.Append("\"").Append(JsonUtil.Escape(risk)).Append("\"");
            count++;
        }

        private static void InvokeManualActivation(BuildingAI ai, ushort id, ref Building building, bool active)
        {
            string methodName = active ? "ManualActivation" : "ManualDeactivation";
            MethodInfo method = ai.GetType().GetMethod(methodName, BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
            if (method == null)
            {
                method = typeof(BuildingAI).GetMethod(methodName, BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
            }
            if (method == null)
            {
                return;
            }

            object[] args = new object[] { id, building };
            method.Invoke(ai, args);
            building = (Building)args[1];
        }

        private static Vector3 ReadPoint(string body, string name)
        {
            float x = JsonUtil.GetPointNumber(body, name, "x", 0f);
            float z = JsonUtil.GetPointNumber(body, name, "z", 0f);
            float y = JsonUtil.GetPointNumber(body, name, "y", 0f);
            return new Vector3(x, y, z);
        }
    }
}

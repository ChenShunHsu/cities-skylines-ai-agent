using ColossalFramework;
using ColossalFramework.Math;
using ColossalFramework.Threading;
using System.Collections.Generic;
using UnityEngine;

namespace SkylinesAgentBridge
{
    public static partial class RoadCommands
    {
        private const float PowerLineRoadClearance = 24f;
        private const float MaximumWaterPipeSegmentLength = 192f;
        internal const float GroundRoadTerrainDeviationLimit = 1f;
        internal const float GroundRoadLocalGradeLimit = 0.25f;
        private const float GroundRoadTargetTerrainClearance = 0.1f;
        private const float GroundRoadSettlementTerrainReserve = 0.7f;
        private const float GroundRoadPreviewClearanceReserve = 0.1f;
        // Margin over the settled acceptance limits for the level-ended settled-shape
        // prediction, which leaves out native corner offsets at bends and junctions.
        private const float SettledShapeClearanceReserve = 0.03f;
        private const float NativeShortSegmentMergeLength = 32f;
        internal const float GroundRoadMinimumTerrainClearance = 0.02f;
        internal const float ExistingNodeTransitionMaximumLength = 64f;
        private const float ExistingNodeTransitionMinimumClearance = -0.5f;
        internal const float ExistingNodeTransitionMaximumDeviation = 3f;
        private const float OriginalNodeClearanceTolerance = 0.005f;
        private const float AdjacentRegressionTolerance = 0.05f;

        internal static bool IsOriginalNodeTransition(
            NetManager manager,
            ushort startNode,
            ushort endNode,
            float flatLength)
        {
            if (flatLength > ExistingNodeTransitionMaximumLength)
            {
                return false;
            }
            return IsOriginalNode(manager, startNode) || IsOriginalNode(manager, endNode);
        }

        internal static bool IsOriginalNodeTransition(
            NetManager manager,
            NetSegment segment,
            float flatLength)
        {
            return IsOriginalNodeTransition(
                manager,
                segment.m_startNode,
                segment.m_endNode,
                flatLength);
        }

        internal static bool IsReportedTerrainProfileAcceptable(
            bool strictResult,
            bool originalNodeTransition,
            float transitionMinimumClearance,
            float maximumDeviation,
            float maximumLocalGrade,
            float minimumClearance)
        {
            return IsTerrainProfileAcceptable(
                strictResult,
                originalNodeTransition,
                transitionMinimumClearance,
                maximumDeviation,
                maximumLocalGrade,
                minimumClearance);
        }

        // A transition can sit no higher above the terrain than the original map node it leaves,
        // and maps place some original nodes (highways in particular) flush with the ground.
        internal static float TransitionMinimumClearance(NetManager manager, ushort startNode, ushort endNode)
        {
            float originalNodeClearance = Mathf.Min(
                OriginalNodeClearance(manager, startNode),
                OriginalNodeClearance(manager, endNode));
            float allowed = Mathf.Min(
                GroundRoadMinimumTerrainClearance,
                originalNodeClearance - OriginalNodeClearanceTolerance);
            return Mathf.Max(ExistingNodeTransitionMinimumClearance, allowed);
        }

        private static float OriginalNodeClearance(NetManager manager, ushort nodeId)
        {
            if (!IsOriginalNode(manager, nodeId))
            {
                return float.MaxValue;
            }
            Vector3 position = manager.m_nodes.m_buffer[nodeId].m_position;
            return position.y - TerrainManager.instance.SampleRawHeightSmoothWithWater(position, false, 0f);
        }

        private static bool IsOriginalNode(NetManager manager, ushort nodeId)
        {
            return nodeId != 0 &&
                (manager.m_nodes.m_buffer[nodeId].m_flags & NetNode.Flags.Original) != NetNode.Flags.None;
        }
        private static readonly Dictionary<ushort, PendingRoadBuild> PendingRoadBuilds =
            new Dictionary<ushort, PendingRoadBuild>();

        private static bool allowTerrainCut;

        private sealed class PendingRoadBuild
        {
            public NetInfo Info;
            public Vector3 Start;
            public Vector3 End;
            public long CashBeforeConstruction;
            public int ChargedCost;
            public Dictionary<ushort, RoadTerrainSnapshot> AdjacentTerrainBefore;
            public bool ExistingNodeTransition;
            public bool TerrainCut;
            public ManualMilestone CreatePassMilestone;
            public string ImmediateGeometry;
            public string LastGeometry;
            public int StableReads;
            public int SettlementReads;
        }

        private sealed class RoadTerrainSnapshot
        {
            public bool Safe;
            public float MaximumDeviation;
            public float MaximumLocalGrade;
            public float MinimumClearance;
        }

        public static CommandResult BuildRoad(string body)
        {
            return BuildRoad(body, false);
        }

        public static CommandResult PreviewNetwork(string body)
        {
            return BuildRoad(body, true);
        }

        public static void ClearPendingRoadBuilds()
        {
            PendingRoadBuilds.Clear();
        }

        private static bool TryApplyCreatePassMilestone(
            ManualMilestone milestone,
            out bool uiRefreshQueued)
        {
            uiRefreshQueued = false;
            if (milestone == null) return true;
            bool applied = true;
            try
            {
                milestone.Unlock();
            }
            catch (System.InvalidOperationException ex)
            {
                if (ex.Message == null || ex.Message.IndexOf("Already in the same thread") < 0)
                {
                    throw;
                }
                applied = Singleton<UnlockManager>.instance.Unlocked(milestone);
            }
            if (!applied) return false;
            try
            {
                ThreadHelper.dispatcher.Dispatch(delegate
                {
                    Singleton<UnlockManager>.instance.MilestonesUpdated();
                });
            }
            catch (System.InvalidOperationException ex)
            {
                if (ex.Message == null || ex.Message.IndexOf("Already in the same thread") < 0)
                {
                    throw;
                }
                Singleton<UnlockManager>.instance.MilestonesUpdated();
            }
            uiRefreshQueued = true;
            return true;
        }

        public static CommandResult FinalizeRoadBuild(string body)
        {
            ushort segmentId = (ushort)JsonUtil.GetNumber(body, "segmentId", 0f);
            PendingRoadBuild pending;
            if (segmentId == 0 || !PendingRoadBuilds.TryGetValue(segmentId, out pending))
            {
                return CommandResult.Fail(
                    "No pending ground-road verification exists for segment " + segmentId + ".");
            }

            // Later API turns observe junction/curve/terrain updates. One turn
            // is not proof of settlement; require a bounded stable sequence.
            NetManager net = NetManager.instance;
            // Refresh only once. Re-queuing node/renderer updates on every poll
            // can itself prevent the state we are measuring from settling.
            if (pending.SettlementReads == 0) RefreshRoadRenderState(net, segmentId);
            string settledGeometry = CaptureGeometryEvidence(net, segmentId);
            pending.SettlementReads++;
            pending.StableReads = settledGeometry == pending.LastGeometry ? pending.StableReads + 1 : 1;
            pending.LastGeometry = settledGeometry;
            int cutSampleCount = 0;
            float cutSurfaceDelta = 0f;
            float cutDepth = 0f;
            float cutBuried = 0f;
            bool cutCarved = pending.TerrainCut &&
                RoadTerrainCut.InspectFinalSurface(net, segmentId, out cutSampleCount, out cutSurfaceDelta, out cutDepth,
                    out cutBuried);
            if ((pending.StableReads < 3 || (pending.TerrainCut && !cutCarved)) && pending.SettlementReads < 8)
            {
                return CommandResult.FromJson("{\"ok\":true,\"segmentId\":" + segmentId +
                    ",\"verificationPending\":true,\"stableReads\":" + pending.StableReads +
                    ",\"settlementReads\":" + pending.SettlementReads + "}");
            }
            NetSegment segment = net.m_segments.m_buffer[segmentId];
            bool segmentCreated =
                (segment.m_flags & NetSegment.Flags.Created) != NetSegment.Flags.None;
            bool prefabMatches = segmentCreated && IsBuiltAs(segment.Info, pending.Info);
            bool endpointsValid = segmentCreated &&
                segment.m_startNode != 0 &&
                segment.m_endNode != 0 &&
                segment.m_startNode != segment.m_endNode;
            bool geometryMatches = endpointsValid &&
                SegmentEndsMatch(net, segment, pending.Start, pending.End, 4f);
            int expectedLaneCount = pending.Info == null || pending.Info.m_lanes == null
                ? 0
                : pending.Info.m_lanes.Length;
            int actualLaneCount = segmentCreated ? CountSegmentLanes(net, segment) : -1;
            bool laneCountMatches = actualLaneCount == expectedLaneCount;
            bool terrainRequired;
            int terrainSampleCount;
            float terrainDeviation;
            float terrainLocalGrade;
            float minimumTerrainClearance;
            float maximumTerrainClearance;
            bool terrainSafe = InspectActualGroundTerrainProfile(
                net,
                segmentId,
                out terrainRequired,
                out terrainSampleCount,
                out terrainDeviation,
                out terrainLocalGrade,
                out minimumTerrainClearance,
                out maximumTerrainClearance);
            float transitionMinimumClearance = TransitionMinimumClearance(net, segment.m_startNode, segment.m_endNode);
            terrainSafe = IsTerrainProfileAcceptable(
                terrainSafe,
                pending.ExistingNodeTransition,
                transitionMinimumClearance,
                terrainDeviation,
                terrainLocalGrade,
                minimumTerrainClearance);
            if (pending.TerrainCut)
            {
                terrainSafe = cutCarved && terrainLocalGrade <= GroundRoadLocalGradeLimit;
            }
            float roadSurfaceOffset = GetRoadSurfaceOffset(net, segmentId, segment);
            List<ushort> adjacentRoadSegments = GetAdjacentRoadSegments(net, segmentId, segment);
            ushort unsafeAdjacentSegmentId;
            float unsafeAdjacentMinimumClearance;
            bool adjacentTerrainSafe = InspectRoadTerrainRegression(
                net,
                adjacentRoadSegments,
                pending.AdjacentTerrainBefore,
                out unsafeAdjacentSegmentId,
                out unsafeAdjacentMinimumClearance);

            if (pending.StableReads < 3 || !segmentCreated ||
                !prefabMatches ||
                !endpointsValid ||
                !geometryMatches ||
                !laneCountMatches ||
                !terrainSafe ||
                !adjacentTerrainSafe)
            {
                string rollbackError = null;
                bool rolledBack = !segmentCreated ||
                    GameThreadHelpers.TryReleaseSegment(net, segmentId, false, out rollbackError);
                string refundError;
                bool economyRecovered = RefundConstructionCost(
                    Singleton<EconomyManager>.instance,
                    pending.CashBeforeConstruction,
                    pending.ChargedCost,
                    pending.Info,
                    out refundError);
                ushort recoveryUnsafeAdjacentSegmentId;
                float recoveryUnsafeAdjacentMinimumClearance;
                bool adjacentTerrainRecovered = InspectRoadTerrainRegression(
                    net,
                    adjacentRoadSegments,
                    pending.AdjacentTerrainBefore,
                    out recoveryUnsafeAdjacentSegmentId,
                    out recoveryUnsafeAdjacentMinimumClearance);
                CommandResult failure = CommandResult.FailWithRecovery(
                    "Settled road verification failed (created=" + segmentCreated +
                    ", stableReads=" + pending.StableReads +
                    ", prefabMatches=" + prefabMatches +
                    ", endpointsValid=" + endpointsValid +
                    ", geometryMatches=" + geometryMatches +
                    ", expectedLanes=" + expectedLaneCount +
                    ", actualLanes=" + actualLaneCount +
                    ", terrainProfileSafe=" + terrainSafe +
                    (pending.TerrainCut ? ", terrainCut=true, buriedDepth=" + cutBuried + ", finalSurfaceDelta=" + cutSurfaceDelta +
                        ", deepestCut=" + cutDepth : "") +
                    ", maxRoadToTerrainDelta=" + terrainDeviation +
                    ", minimumRoadAboveTerrain=" + minimumTerrainClearance +
                    ", maxLocalTerrainGrade=" + terrainLocalGrade +
                    ", adjacentTerrainSafe=" + adjacentTerrainSafe +
                    ", unsafeAdjacentSegmentId=" + unsafeAdjacentSegmentId +
                    ", unsafeAdjacentMinimumRoadAboveTerrain=" + unsafeAdjacentMinimumClearance +
                    "); local rollback " +
                    (rolledBack && economyRecovered && adjacentTerrainRecovered
                        ? "succeeded, including construction-cost recovery."
                        : "was incomplete; fully restart and load the checkpoint. Entity rollback: " +
                          rollbackError + "; economy rollback: " + refundError +
                          "; adjacent terrain recovered: " + adjacentTerrainRecovered +
                          "; remaining unsafe adjacent segment: " + recoveryUnsafeAdjacentSegmentId +
                          " at " + recoveryUnsafeAdjacentMinimumClearance),
                    segmentCreated,
                    rolledBack && economyRecovered && adjacentTerrainRecovered,
                    "netSegment",
                    segmentId);
                PendingRoadBuilds.Remove(segmentId);
                return WithGeometryEvidence(failure, pending.ImmediateGeometry, settledGeometry);
            }

            bool milestoneUiRefreshQueued;
            bool milestoneApplied = TryApplyCreatePassMilestone(
                pending.CreatePassMilestone,
                out milestoneUiRefreshQueued);
            if (!milestoneApplied)
            {
                string rollbackError;
                bool rolledBack = GameThreadHelpers.TryReleaseSegment(
                    net, segmentId, false, out rollbackError);
                string refundError;
                bool economyRecovered = RefundConstructionCost(
                    Singleton<EconomyManager>.instance,
                    pending.CashBeforeConstruction,
                    pending.ChargedCost,
                    pending.Info,
                    out refundError);
                PendingRoadBuilds.Remove(segmentId);
                CommandResult failure = CommandResult.FailWithRecovery(
                    "Settled road was valid but its normal create-pass milestone did not apply; local rollback " +
                    (rolledBack && economyRecovered
                        ? "succeeded, including construction-cost recovery."
                        : "was incomplete; fully restart and load the checkpoint. Entity rollback: " +
                          rollbackError + "; economy rollback: " + refundError),
                    true,
                    rolledBack && economyRecovered,
                    "netSegment",
                    segmentId);
                return WithGeometryEvidence(failure, pending.ImmediateGeometry, settledGeometry);
            }

            PendingRoadBuilds.Remove(segmentId);
            float maximumTerrainDeviationAllowed = pending.ExistingNodeTransition
                ? ExistingNodeTransitionMaximumDeviation
                : GroundRoadTerrainDeviationLimit;
            return CommandResult.FromJson(
                "{\"ok\":true,\"segmentId\":" + segmentId +
                ",\"settledVerification\":true" +
                ",\"createPassMilestoneApplied\":" + JsonUtil.Bool(milestoneApplied) +
                ",\"milestoneUiRefreshQueued\":" + JsonUtil.Bool(milestoneUiRefreshQueued) +
                ",\"stableReads\":" + pending.StableReads +
                ",\"geometryEvidence\":" + settledGeometry +
                ",\"immediateGeometryEvidence\":" + pending.ImmediateGeometry +
                ",\"roadSurfaceOffset\":" + JsonUtil.Number(roadSurfaceOffset) +
                ",\"adjacentRoadSegmentsChecked\":" + adjacentRoadSegments.Count +
                (pending.TerrainCut
                    ? ",\"terrainProfile\":{\"source\":\"nativeGenerateBezierAndFinalTerrain\"" +
                      ",\"verificationStage\":\"settled\"" +
                      ",\"clearanceModel\":\"gameShapedSurface\"" +
                      ",\"buriedDepth\":" + JsonUtil.Number(cutBuried) +
                      ",\"required\":true" +
                      ",\"sampleCount\":" + cutSampleCount +
                      ",\"finalSurfaceDelta\":" + JsonUtil.Number(cutSurfaceDelta) +
                      ",\"finalSurfaceTolerance\":" + JsonUtil.Number(RoadTerrainCut.FinalSurfaceTolerance) +
                      ",\"deepestCut\":" + JsonUtil.Number(cutDepth) +
                      ",\"maximumCut\":" + JsonUtil.Number(RoadTerrainCut.MaximumCut) +
                      ",\"maxLocalTerrainGrade\":" + JsonUtil.Number(terrainLocalGrade) +
                      ",\"maximumLocalTerrainGrade\":" + JsonUtil.Number(GroundRoadLocalGradeLimit) +
                      ",\"originalNodeTransition\":false" +
                      ",\"safe\":" + JsonUtil.Bool(terrainSafe) + "}}"
                    : ",\"terrainProfile\":{\"source\":\"nativeGenerateBezierAndLiveTerrain\"" +
                ",\"verificationStage\":\"settled\"" +
                ",\"clearanceModel\":\"legacyNodeInsetProxyNotCalibratedSurface\"" +
                ",\"required\":" + JsonUtil.Bool(terrainRequired) +
                ",\"sampleCount\":" + terrainSampleCount +
                ",\"maxRoadToTerrainDelta\":" + JsonUtil.Number(terrainDeviation) +
                ",\"minimumRoadAboveTerrain\":" + JsonUtil.Number(minimumTerrainClearance) +
                ",\"maximumRoadAboveTerrain\":" + JsonUtil.Number(maximumTerrainClearance) +
                ",\"maxLocalTerrainGrade\":" + JsonUtil.Number(terrainLocalGrade) +
                ",\"maximumRoadToTerrainDelta\":" + JsonUtil.Number(maximumTerrainDeviationAllowed) +
                ",\"minimumRoadAboveTerrainAllowed\":" + JsonUtil.Number(pending.ExistingNodeTransition
                    ? transitionMinimumClearance
                    : GroundRoadMinimumTerrainClearance) +
                ",\"maximumLocalTerrainGrade\":" + JsonUtil.Number(GroundRoadLocalGradeLimit) +
                ",\"originalNodeTransition\":" + JsonUtil.Bool(pending.ExistingNodeTransition) +
                ",\"safe\":" + JsonUtil.Bool(terrainSafe) + "}}"));
        }

        public static CommandResult ReverseRoadSegment(string body)
        {
            ushort segmentId = (ushort)JsonUtil.GetNumber(body, "segmentId", JsonUtil.GetNumber(body, "id", 0f));
            bool dryRun = JsonUtil.GetBool(body, "dryRun", false);
            NetManager manager = NetManager.instance;
            if (segmentId == 0 || segmentId >= manager.m_segments.m_buffer.Length)
            {
                return CommandResult.Fail("segmentId is required and must be in range.");
            }
            NetSegment segment = manager.m_segments.m_buffer[segmentId];
            if ((segment.m_flags & NetSegment.Flags.Created) == NetSegment.Flags.None || segment.Info == null)
            {
                return CommandResult.Fail("Network segment was not found: " + segmentId);
            }
            if (segment.Info.m_class == null || segment.Info.m_class.m_service != ItemClass.Service.Road)
            {
                return CommandResult.Fail("Only road segments can be reversed.");
            }
            bool before = (segment.m_flags & NetSegment.Flags.Invert) != NetSegment.Flags.None;
            if (!dryRun)
            {
                segment.m_flags ^= NetSegment.Flags.Invert;
                manager.m_segments.m_buffer[segmentId] = segment;
                segment.Info.m_netAI.UpdateSegmentFlags(segmentId, ref segment);
                manager.m_segments.m_buffer[segmentId] = segment;
                manager.UpdateSegmentFlags(segmentId);
                manager.UpdateNode(segment.m_startNode, segmentId, 0);
                manager.UpdateNode(segment.m_endNode, segmentId, 0);
                bool after = (manager.m_segments.m_buffer[segmentId].m_flags & NetSegment.Flags.Invert) != NetSegment.Flags.None;
                if (after == before)
                {
                    NetSegment rollback = manager.m_segments.m_buffer[segmentId];
                    rollback.m_flags = before
                        ? rollback.m_flags | NetSegment.Flags.Invert
                        : rollback.m_flags & ~NetSegment.Flags.Invert;
                    manager.m_segments.m_buffer[segmentId] = rollback;
                    rollback.Info.m_netAI.UpdateSegmentFlags(segmentId, ref rollback);
                    manager.m_segments.m_buffer[segmentId] = rollback;
                    manager.UpdateSegmentFlags(segmentId);
                return CommandResult.FailWithRecovery(
                    "Road reversal did not change direction and was rolled back.",
                    true,
                    true,
                    "netSegment",
                    segmentId);
                }
            }
            return CommandResult.FromJson("{\"ok\":true,\"dryRun\":" + JsonUtil.Bool(dryRun) +
                ",\"segmentId\":" + segmentId +
                ",\"beforeInverted\":" + JsonUtil.Bool(before) +
                ",\"afterInverted\":" + JsonUtil.Bool(dryRun ? !before : (manager.m_segments.m_buffer[segmentId].m_flags & NetSegment.Flags.Invert) != NetSegment.Flags.None) + "}");
        }

        public static CommandResult UpgradeRoadSegment(string body)
        {
            ushort segmentId = (ushort)JsonUtil.GetNumber(body, "segmentId", JsonUtil.GetNumber(body, "id", 0f));
            string prefabName = JsonUtil.GetString(body, "roadPrefab", "");
            bool dryRun = JsonUtil.GetBool(body, "dryRun", false);
            bool confirm = JsonUtil.GetBool(body, "confirm", false);
            if (prefabName.Length == 0) return CommandResult.Fail("roadPrefab is required.");

            NetManager manager = NetManager.instance;
            if (segmentId == 0 || segmentId >= manager.m_segments.m_buffer.Length)
            {
                return CommandResult.Fail("segmentId is required and must be in range.");
            }
            NetSegment existing = manager.m_segments.m_buffer[segmentId];
            if ((existing.m_flags & NetSegment.Flags.Created) == NetSegment.Flags.None || existing.Info == null)
            {
                return CommandResult.Fail("Network segment was not found: " + segmentId);
            }
            if ((existing.m_flags & NetSegment.Flags.Untouchable) != NetSegment.Flags.None)
            {
                return CommandResult.Fail("Untouchable map or building network segments cannot be upgraded.");
            }
            if (existing.Info.m_class == null || existing.Info.m_class.m_service != ItemClass.Service.Road)
            {
                return CommandResult.Fail("Only road segments can be upgraded.");
            }

            NetInfo requested = PrefabCollection<NetInfo>.FindLoaded(prefabName);
            if (requested == null) return CommandResult.Fail("Road prefab was not found: " + prefabName);
            if (requested.m_class == null || requested.m_class.m_service != ItemClass.Service.Road)
            {
                return CommandResult.Fail("Requested prefab is not a road: " + prefabName);
            }
            if (requested.m_UnlockMilestone != null && !Singleton<UnlockManager>.instance.Unlocked(requested.m_UnlockMilestone))
            {
                return CommandResult.Fail("Road prefab is still locked: " + prefabName);
            }
            ToolBase.ToolErrors compatibility = existing.Info.m_netAI.CanUpgradeFrom(segmentId);
            if (!existing.Info.m_netAI.CanUpgradeTo(requested) || compatibility != ToolBase.ToolErrors.None)
            {
                return CommandResult.Fail("Road upgrade is incompatible: " + compatibility);
            }

            NetNode startNode = manager.m_nodes.m_buffer[existing.m_startNode];
            NetNode endNode = manager.m_nodes.m_buffer[existing.m_endNode];
            int cost = requested.m_netAI.GetConstructionCost(
                startNode.m_position,
                endNode.m_position,
                startNode.m_position.y,
                endNode.m_position.y);
            bool invert = (existing.m_flags & NetSegment.Flags.Invert) != NetSegment.Flags.None;
            if (dryRun)
            {
                return CommandResult.FromJson("{\"ok\":true,\"dryRun\":true,\"valid\":true" +
                    ",\"oldSegmentId\":" + segmentId +
                    ",\"newSegmentId\":0" +
                    ",\"fromPrefab\":\"" + JsonUtil.Escape(existing.Info.name) + "\"" +
                    ",\"toPrefab\":\"" + JsonUtil.Escape(prefabName) + "\"" +
                    ",\"cost\":" + cost +
                    ",\"toolErrors\":\"None\"}");
            }
            if (!confirm)
            {
                return CommandResult.Fail("Set confirm=true after creating and verifying a checkpoint.");
            }

            NetInfo previousInfo = existing.Info;
            // the replacement segment gets a generated street name; keep a custom one, since the
            // agent's road names carry the planned land use
            string customName = (existing.m_flags & NetSegment.Flags.CustomName) != NetSegment.Flags.None
                ? manager.GetSegmentName(segmentId)
                : null;
            ushort startNodeId = existing.m_startNode;
            ushort endNodeId = existing.m_endNode;
            Vector3 startDirection = existing.m_startDirection;
            Vector3 endDirection = existing.m_endDirection;
            SimulationManager simulation = Singleton<SimulationManager>.instance;
            Randomizer randomizer = simulation.m_randomizer;
            string releaseError;
            if (!GameThreadHelpers.TryReleaseSegment(manager, segmentId, true, out releaseError))
            {
                return CommandResult.Fail(
                    "Road upgrade could not remove the original segment. No replacement was attempted: " +
                    releaseError);
            }
            ushort newSegment;
            bool created = TryCreateCompleteSegment(
                manager,
                ref randomizer,
                requested,
                startNodeId,
                endNodeId,
                startDirection,
                endDirection,
                simulation.m_currentBuildIndex,
                invert,
                out newSegment);
            simulation.m_randomizer = randomizer;
            if (!created)
            {
                string partialReleaseError;
                bool partialRemoved = newSegment == 0 ||
                    GameThreadHelpers.TryReleaseSegment(manager, newSegment, true, out partialReleaseError);
                if (newSegment != 0)
                {
                    simulation.m_currentBuildIndex += 2u;
                }
                ushort rollbackSegment = 0;
                bool rolledBack = partialRemoved && TryCreateCompleteSegment(
                    manager,
                    ref randomizer,
                    previousInfo,
                    startNodeId,
                    endNodeId,
                    startDirection,
                    endDirection,
                    simulation.m_currentBuildIndex,
                    invert,
                    out rollbackSegment);
                simulation.m_randomizer = randomizer;
                if (rolledBack)
                {
                    simulation.m_currentBuildIndex += 2u;
                    RestoreSegmentName(manager, rollbackSegment, customName);
                }
                return CommandResult.FailWithRecovery(
                    "Road upgrade failed; local rollback " +
                    (rolledBack
                        ? "restored segment " + rollbackSegment + "."
                        : "failed, restore the checkpoint."),
                    true,
                    rolledBack,
                    "netSegment",
                    rolledBack ? rollbackSegment : newSegment);
            }
            simulation.m_currentBuildIndex += 2u;
            requested.m_netAI.UpgradeSucceeded();
            bool nameKept = RestoreSegmentName(manager, newSegment, customName);
            return CommandResult.FromJson("{\"ok\":true,\"dryRun\":" + JsonUtil.Bool(dryRun) +
                ",\"valid\":true" +
                ",\"oldSegmentId\":" + segmentId +
                ",\"newSegmentId\":" + newSegment +
                ",\"fromPrefab\":\"" + JsonUtil.Escape(previousInfo.name) + "\"" +
                ",\"toPrefab\":\"" + JsonUtil.Escape(prefabName) + "\"" +
                ",\"cost\":" + cost +
                ",\"name\":\"" + JsonUtil.Escape(manager.GetSegmentName(newSegment) ?? "") + "\"" +
                ",\"customNameKept\":" + JsonUtil.Bool(customName == null || nameKept) +
                ",\"toolErrors\":\"None\"}");
        }

        private static bool RestoreSegmentName(NetManager manager, ushort segmentId, string name)
        {
            if (segmentId == 0 || string.IsNullOrEmpty(name)) return false;
            try
            {
                manager.SetSegmentNameImpl(segmentId, name);
            }
            catch (System.InvalidOperationException ex)
            {
                // SetName dispatches a UI refresh; on the game thread that throws after the name is stored
                if (ex.Message == null || ex.Message.IndexOf("Already in the same thread") < 0) throw;
            }
            return manager.GetSegmentName(segmentId) == name;
        }

        private static CommandResult BuildRoad(string body, bool forceDryRun)
        {
            bool dryRun = forceDryRun || JsonUtil.GetBool(body, "dryRun", false);
            // A road that only dips into bumps may be built as a cut (see RoadTerrainCut).
            allowTerrainCut = JsonUtil.GetBool(body, "allowTerrainCut", false);
            string prefabName = JsonUtil.GetString(body, "roadPrefab", "Basic Road");
            string name = JsonUtil.GetString(body, "name", "");
            float heightOffset = JsonUtil.GetNumber(body, "heightOffset", 0f);
            // Grade-separated crossings need ramps: each endpoint may carry its own
            // elevation, defaulting to the shared heightOffset.
            float startHeightOffset = Mathf.Clamp(JsonUtil.GetNumber(body, "startHeightOffset", heightOffset), -64f, 64f);
            float endHeightOffset = Mathf.Clamp(JsonUtil.GetNumber(body, "endHeightOffset", heightOffset), -64f, 64f);

            Vector3 start = ReadPoint(body, "start");
            Vector3 end = ReadPoint(body, "end");

            NetInfo info = PrefabCollection<NetInfo>.FindLoaded(prefabName);
            if (info == null)
            {
                return CommandResult.Fail("Road prefab was not found: " + prefabName);
            }
            UnlockManager unlocks = Singleton<UnlockManager>.instance;
            bool networkUnlocked = info.m_UnlockMilestone == null || unlocks.Unlocked(info.m_UnlockMilestone);
            // A dry run changes nothing, so a locked network may still be previewed (reported
            // invalid, with networkUnlocked false): Train Track stays locked until a train station
            // or cargo terminal stands (its "Train Track Requirements" milestone), and the rail
            // that joins a planned terminal must be previewed before the terminal is built.
            if (!networkUnlocked && !dryRun)
            {
                return CommandResult.Fail("Network prefab is still locked: " + prefabName);
            }

            if ((end - start).sqrMagnitude < 16f)
            {
                return CommandResult.Fail("Road is too short.");
            }

            TerrainManager terrain = TerrainManager.instance;
            start.y = terrain.SampleRawHeightSmoothWithWater(start, false, 0f);
            end.y = terrain.SampleRawHeightSmoothWithWater(end, false, 0f);
            if (heightOffset < -64f)
            {
                heightOffset = -64f;
            }
            else if (heightOffset > 64f)
            {
                heightOffset = 64f;
            }
            if (IsGroundRoad(info) &&
                (Mathf.Abs(startHeightOffset) > 0.01f || Mathf.Abs(endHeightOffset) > 0.01f))
            {
                return CommandResult.Fail(
                    "Ground roads cannot take endpoint height offsets; use an elevated, bridge, or tunnel prefab.");
            }
            start.y += startHeightOffset;
            end.y += endHeightOffset;
            // Shared value for checks that only distinguish ground from grade-separated geometry.
            heightOffset = Mathf.Abs(startHeightOffset) >= Mathf.Abs(endHeightOffset) ? startHeightOffset : endHeightOffset;
            if (IsGroundRoad(info))
            {
                // Seed requested nodes at the same clearance measured by the
                // preview control curve. Native corner/node height offsets do
                // not exist until creation and must not be guessed here.
                start.y += GroundRoadTargetTerrainClearance;
                end.y += GroundRoadTargetTerrainClearance;
            }

            NetManager net = NetManager.instance;
            ushort startNode = FindNearbyNode(start, 2f, info);
            ushort endNode = FindNearbyNode(end, 2f, info);
            if (startNode != 0)
            {
                start = net.m_nodes.m_buffer[startNode].m_position;
            }
            if (endNode != 0)
            {
                end = net.m_nodes.m_buffer[endNode].m_position;
            }
            bool planningStart = body.IndexOf("\"planningStartY\"", System.StringComparison.Ordinal) >= 0;
            bool planningEnd = body.IndexOf("\"planningEndY\"", System.StringComparison.Ordinal) >= 0;
            bool requestedStartLift = body.IndexOf(
                "\"startTerrainFitLift\"", System.StringComparison.Ordinal) >= 0;
            bool requestedEndLift = body.IndexOf(
                "\"endTerrainFitLift\"", System.StringComparison.Ordinal) >= 0;
            float requestedStartLiftValue = JsonUtil.GetNumber(
                body, "startTerrainFitLift", float.NaN);
            float requestedEndLiftValue = JsonUtil.GetNumber(
                body, "endTerrainFitLift", float.NaN);
            if (planningStart || planningEnd)
            {
                // Hypothetical future junctions are READ-ONLY planning evidence.
                // They are never accepted by the construction endpoint.
                if (!forceDryRun || !IsGroundRoad(info) || Mathf.Abs(heightOffset) > 0.01f)
                    return CommandResult.Fail("Future node heights require the read-only ground-road preview endpoint.");
                float startY = JsonUtil.GetNumber(body, "planningStartY", float.NaN);
                float endY = JsonUtil.GetNumber(body, "planningEndY", float.NaN);
                if ((planningStart && (startNode != 0 || float.IsNaN(startY) || float.IsInfinity(startY) ||
                        startY < start.y - 0.001f || startY > start.y + MaximumNewEndpointTerrainFit + 0.001f)) ||
                    (planningEnd && (endNode != 0 || float.IsNaN(endY) || float.IsInfinity(endY) ||
                        endY < end.y - 0.001f || endY > end.y + MaximumNewEndpointTerrainFit + 0.001f)))
                    return CommandResult.Fail("Future height must match a new terrain-seeded endpoint within the existing bounded fit range.");
                if (planningStart) start.y = startY;
                if (planningEnd) end.y = endY;
            }
            if (requestedStartLift || requestedEndLift)
            {
                if (!IsGroundRoad(info) || Mathf.Abs(heightOffset) > 0.01f ||
                    (requestedStartLift && (startNode != 0 || planningStart ||
                        float.IsNaN(requestedStartLiftValue) || float.IsInfinity(requestedStartLiftValue) ||
                        requestedStartLiftValue < 0f ||
                        requestedStartLiftValue > MaximumNewEndpointTerrainFit + 0.001f)) ||
                    (requestedEndLift && (endNode != 0 || planningEnd ||
                        float.IsNaN(requestedEndLiftValue) || float.IsInfinity(requestedEndLiftValue) ||
                        requestedEndLiftValue < 0f ||
                        requestedEndLiftValue > MaximumNewEndpointTerrainFit + 0.001f)))
                    return CommandResult.Fail(
                        "Requested terrain-fit lift must target a new ground-road endpoint within the bounded fit range.");
            }
            float terrainFitStartLift;
            float terrainFitEndLift;
            FitNewGroundRoadEndpoints(net, info, startNode, endNode, planningStart, planningEnd, heightOffset,
                requestedStartLift, requestedStartLiftValue,
                requestedEndLift, requestedEndLiftValue,
                ref start, ref end, out terrainFitStartLift, out terrainFitEndLift);
            Vector3 requestedDirection = (end - start).normalized;
            Vector3 startConnectionDirection = IsGroundRoad(info)
                ? ResolveConnectionDirection(
                    net, startNode, info, requestedDirection, true)
                : requestedDirection;
            Vector3 endConnectionDirection = IsGroundRoad(info)
                ? ResolveConnectionDirection(
                    net, endNode, info, requestedDirection, false)
                : requestedDirection;
            bool requiresExistingUndergroundMetroNodes = IsMetroBuildProtected(info, prefabName) && heightOffset > -8f;
            if (requiresExistingUndergroundMetroNodes && (startNode == 0 || endNode == 0))
            {
                return CommandResult.Fail("Metro track creation at terrain height is blocked. Use an underground heightOffset or connect to existing metro station/track nodes.");
            }

            float flatLength = new Vector2(end.x - start.x, end.z - start.z).magnitude;
            if (info.m_class != null &&
                info.m_class.m_service == ItemClass.Service.Water &&
                flatLength > MaximumWaterPipeSegmentLength)
            {
                return CommandResult.Fail(
                    "Water-pipe segment exceeds the 192m native update-safe limit. Split the route into shorter connected segments.");
            }
            if (IsOverlongRoadSegment(info, flatLength))
            {
                return CommandResult.Fail(
                    "Road segment is " + JsonUtil.Number(flatLength) + "m; vehicles cannot reach buildings near the middle of road segments longer than " +
                    JsonUtil.Number(MaximumRoadSegmentLength) + "m (the native road tool cuts roads into pieces under 100m). Split the route into shorter connected segments.");
            }
            float startRoadDistance;
            float endRoadDistance;
            bool startHasElectricity;
            bool endHasElectricity;
            bool powerGridConnectionSafe;
            bool powerClearanceSafe = ValidatePowerLineRoadClearance(
                info,
                start,
                end,
                startNode,
                endNode,
                out startRoadDistance,
                out endRoadDistance,
                out startHasElectricity,
                out endHasElectricity,
                out powerGridConnectionSafe);
            int terrainSampleCount;
            float maxTerrainDeviation;
            float maxTerrainLocalGrade;
            float minimumTerrainClearance;
            bool originalNodeTransition = IsOriginalNodeTransition(
                net, startNode, endNode, flatLength);
            float transitionMinimumClearance = TransitionMinimumClearance(net, startNode, endNode);
            bool settledStartKeepsSlope = SettledEndKeepsSlope(net, startNode, planningStart, end - start);
            bool settledEndKeepsSlope = SettledEndKeepsSlope(net, endNode, planningEnd, start - end);
            float settledShapeMinimumClearance;
            float settledShapeMaxDeviation;
            bool terrainProfileSafe = ValidateRoadTerrainProfile(
                info,
                start,
                end,
                requestedDirection,
                -endConnectionDirection,
                startNode != 0 || planningStart,
                endNode != 0 || planningEnd,
                settledStartKeepsSlope,
                settledEndKeepsSlope,
                originalNodeTransition,
                transitionMinimumClearance,
                heightOffset,
                flatLength,
                out terrainSampleCount,
                out maxTerrainDeviation,
                out maxTerrainLocalGrade,
                out minimumTerrainClearance,
                out settledShapeMinimumClearance,
                out settledShapeMaxDeviation);
            float terrainCutDepth, terrainCutFill, settledCutDepth, settledCutFill;
            bool cutReusesStart = startNode != 0 || planningStart;
            bool cutReusesEnd = endNode != 0 || planningEnd;
            RoadTerrainCut.Measure(PredictedControlCurve(start, end, requestedDirection, -endConnectionDirection),
                cutReusesStart, cutReusesEnd, out terrainCutDepth, out terrainCutFill);
            RoadTerrainCut.Measure(SettledShapeCurve(start, end, requestedDirection, -endConnectionDirection,
                    settledStartKeepsSlope, settledEndKeepsSlope, cutReusesStart, cutReusesEnd, info.m_halfWidth),
                cutReusesStart, cutReusesEnd, out settledCutDepth, out settledCutFill);
            // left to the game like a player's road: any ground road the native tool accepts, on ground
            // no steeper than the grade limit; the settled check refuses one the terrain then covers
            bool terrainCarvable = IsGroundRoad(info) && Mathf.Abs(heightOffset) <= 0.01f &&
                maxTerrainLocalGrade <= GroundRoadLocalGradeLimit;
            bool nativeMoveMiddleNodeRisk = WouldInvokeNativeMoveMiddleNode(
                net, info, startNode, endNode, start, end);
            bool shortCollinearMergeRisk = WouldMergeShortCollinearExtension(
                net, startNode, endNode, start, end, flatLength);
            float effectiveMinimumTerrainClearance = PreviewMinimumTerrainClearance(
                originalNodeTransition, transitionMinimumClearance);
            float effectiveMaximumTerrainDeviation = originalNodeTransition
                ? ExistingNodeTransitionMaximumDeviation
                : GroundRoadTerrainDeviationLimit;
            if (!dryRun && !powerClearanceSafe)
            {
                return CommandResult.Fail(
                    "Power-line pylon endpoint is too close to a road. New pylons require at least " +
                    JsonUtil.Number(PowerLineRoadClearance) + "m clearance and must not use road endpoints, nodes, junctions, or centerlines.");
            }
            if (!dryRun && !powerGridConnectionSafe)
            {
                return CommandResult.Fail(
                    "Power line is isolated. At least one endpoint must reuse an existing compatible power node or lie inside a live electricity coverage area.");
            }
            if (!dryRun && !terrainProfileSafe && !(allowTerrainCut && terrainCarvable))
            {
                return CommandResult.Fail(
                    "Ground-road terrain profile is unsafe. Settled roads leave End, Bend and Junction nodes level, so a segment climbing a rise sags about " +
                    JsonUtil.Number(SettledRoadShape.FlatEndSagPerMetreOfRise) +
                    " x the rise below its slope near the low end. Split the climb into shorter segments, continue straight through existing nodes, or follow the contours.");
            }
            if (!dryRun && nativeMoveMiddleNodeRisk)
            {
                return CommandResult.Fail(
                    "Road extension would invoke CS1's native MoveMiddleNode behavior and rewrite a pre-existing segment. " +
                    "Choose a non-collinear junction or start beyond the movable terminal node so checkpoint rollback remains reliable.");
            }
            if (!dryRun && shortCollinearMergeRisk)
            {
                return CommandResult.Fail(
                    "Road extension would trigger native short-segment merging and make local rollback ambiguous. " +
                    "Use a longer terrain-safe segment or a non-collinear junction candidate.");
            }

            if (dryRun)
            {
                NetTool.ControlPoint startPoint = new NetTool.ControlPoint();
                startPoint.m_position = start;
                startPoint.m_direction = startConnectionDirection;
                startPoint.m_node = startNode;
                startPoint.m_elevation = startHeightOffset;
                NetTool.ControlPoint middlePoint = new NetTool.ControlPoint();
                middlePoint.m_position = (start + end) * 0.5f;
                middlePoint.m_direction = requestedDirection;
                middlePoint.m_elevation = (startHeightOffset + endHeightOffset) * 0.5f;
                NetTool.ControlPoint endPoint = new NetTool.ControlPoint();
                endPoint.m_position = end;
                endPoint.m_direction = endConnectionDirection;
                endPoint.m_node = endNode;
                endPoint.m_elevation = endHeightOffset;
                BuildingInfo ownerBuilding;
                Vector3 ownerPosition;
                Vector3 ownerDirection;
                int productionRate = 0;
                ToolBase.ToolErrors errors = info.m_netAI.CheckBuildPosition(
                    true,
                    false,
                    false,
                    false,
                    ref startPoint,
                    ref middlePoint,
                    ref endPoint,
                    out ownerBuilding,
                    out ownerPosition,
                    out ownerDirection,
                    out productionRate);
                FastList<NetTool.NodePosition> previewNodePositions =
                    new FastList<NetTool.NodePosition>();
                ushort previewTestedNode;
                ushort previewTestedSegment;
                int previewTestedCost;
                int previewTestedProductionRate;
                ToolBase.ToolErrors createNodeTestErrors = NetTool.CreateNode(
                    info,
                    startPoint,
                    middlePoint,
                    endPoint,
                    previewNodePositions,
                    1,
                    true,
                    false,
                    true,
                    true,
                    false,
                    false,
                    0,
                    out previewTestedNode,
                    out previewTestedSegment,
                    out previewTestedCost,
                    out previewTestedProductionRate);
                ToolBase.ToolErrors finalErrors = errors != ToolBase.ToolErrors.None
                    ? errors
                    : createNodeTestErrors;
                float grade = flatLength <= 0.001f ? 0f : Mathf.Abs(end.y - start.y) / flatLength;
                int constructionCost = previewTestedCost > 0
                    ? previewTestedCost
                    : info.m_netAI.GetConstructionCost(start, end, start.y, end.y);
                int maintenanceCost = info.m_netAI.GetMaintenanceCost(start, end);
                int buildingsUnderRoadCount = 0;
                bool onGround = RunsOnGround(info, startHeightOffset, endHeightOffset);
                string buildingsUnderRoad = onGround
                    ? CollectBuildingsUnderRoad(start, end, info.m_halfWidth, out buildingsUnderRoadCount)
                    : "[]";
                int powerLinesCrossedCount = 0;
                string powerLinesCrossed = onGround
                    ? CollectPowerLinesCrossed(start, end, info.m_halfWidth, out powerLinesCrossedCount)
                    : "[]";
                bool validApartFromTerrain = buildingsUnderRoadCount == 0 &&
                    powerLinesCrossedCount == 0 &&
                    finalErrors == ToolBase.ToolErrors.None &&
                    powerClearanceSafe &&
                    powerGridConnectionSafe &&
                    !nativeMoveMiddleNodeRisk &&
                    !shortCollinearMergeRisk;
                bool validIfUnlocked = validApartFromTerrain && terrainProfileSafe;
                bool valid = validIfUnlocked && networkUnlocked;
                return CommandResult.FromJson("{\"ok\":true,\"dryRun\":true,\"valid\":" + JsonUtil.Bool(valid) +
                    ",\"networkUnlocked\":" + JsonUtil.Bool(networkUnlocked) +
                    ",\"validIfUnlocked\":" + JsonUtil.Bool(validIfUnlocked) +
                    ",\"groundRail\":" + JsonUtil.Bool(IsGroundRail(info)) +
                    ",\"roadPrefab\":\"" + JsonUtil.Escape(prefabName) + "\",\"heightOffset\":" + JsonUtil.Number(heightOffset) +
                    ",\"reusedStartNode\":" + JsonUtil.Bool(startNode != 0) +
                    ",\"reusedEndNode\":" + JsonUtil.Bool(endNode != 0) +
                    ",\"length\":" + JsonUtil.Number(flatLength) +
                    ",\"grade\":" + JsonUtil.Number(grade) +
                    ",\"planningStartNode\":" + JsonUtil.Bool(planningStart) +
                    ",\"planningEndNode\":" + JsonUtil.Bool(planningEnd) +
                    ",\"futureJunctionsVerified\":false" +
                    ",\"terrainFit\":{\"startLift\":" + JsonUtil.Number(terrainFitStartLift) +
                    ",\"endLift\":" + JsonUtil.Number(terrainFitEndLift) +
                    ",\"startLiftRequested\":" + JsonUtil.Bool(requestedStartLift) +
                    ",\"endLiftRequested\":" + JsonUtil.Bool(requestedEndLift) +
                    ",\"maximumNewEndpointLift\":" + JsonUtil.Number(MaximumNewEndpointTerrainFit) +
                    ",\"existingNodesMoved\":false}" +
                    ",\"predictionGeometryEvidence\":" + (IsGroundRoad(info)
                        ? CapturePreviewGeometry(start, end, requestedDirection, -endConnectionDirection) : "null") +
                    ",\"buildingsUnderRoad\":" + buildingsUnderRoad +
                    ",\"powerLinesCrossed\":" + powerLinesCrossed +
                    ",\"nativeMoveMiddleNodeRisk\":" + JsonUtil.Bool(nativeMoveMiddleNodeRisk) +
                    ",\"shortCollinearMergeRisk\":" + JsonUtil.Bool(shortCollinearMergeRisk) +
                    ",\"powerLineRoadClearance\":{\"required\":" + JsonUtil.Bool(IsPowerLine(info)) +
                    ",\"minimum\":" + JsonUtil.Number(PowerLineRoadClearance) +
                    ",\"startDistance\":" + JsonNumberOrNull(startRoadDistance) +
                    ",\"endDistance\":" + JsonNumberOrNull(endRoadDistance) +
                    ",\"startReusesExistingNode\":" + JsonUtil.Bool(startNode != 0) +
                    ",\"endReusesExistingNode\":" + JsonUtil.Bool(endNode != 0) +
                    ",\"startHasElectricity\":" + JsonUtil.Bool(startHasElectricity) +
                    ",\"endHasElectricity\":" + JsonUtil.Bool(endHasElectricity) +
                    ",\"connectedToExistingGrid\":" + JsonUtil.Bool(powerGridConnectionSafe) +
                    ",\"safe\":" + JsonUtil.Bool(powerClearanceSafe && powerGridConnectionSafe) + "}" +
                    ",\"terrainProfile\":{\"source\":\"predictedControlBezierAndLiveTerrain\",\"predictsSettledSurface\":false,\"required\":" + JsonUtil.Bool(IsGroundRoad(info)) +
                    ",\"sampleCount\":" + terrainSampleCount +
                    ",\"maxRoadToTerrainDelta\":" + JsonUtil.Number(maxTerrainDeviation) +
                    ",\"minimumRoadAboveTerrain\":" + JsonUtil.Number(minimumTerrainClearance) +
                    ",\"maxLocalTerrainGrade\":" + JsonUtil.Number(maxTerrainLocalGrade) +
                    ",\"maximumRoadToTerrainDelta\":" + JsonUtil.Number(effectiveMaximumTerrainDeviation) +
                    ",\"previewMaximumRoadToTerrainDelta\":" + JsonUtil.Number(
                        effectiveMaximumTerrainDeviation - GroundRoadSettlementTerrainReserve) +
                    ",\"settlementTerrainReserve\":" + JsonUtil.Number(GroundRoadSettlementTerrainReserve) +
                    ",\"settlementTerrainReserveApplied\":true" +
                    ",\"settlementClearanceReserve\":" + JsonUtil.Number(originalNodeTransition
                        ? 0f
                        : effectiveMinimumTerrainClearance - GroundRoadMinimumTerrainClearance) +
                    ",\"settlementClearanceReserveApplied\":" + JsonUtil.Bool(!originalNodeTransition) +
                    ",\"settledMinimumRoadAboveTerrainAllowed\":" + JsonUtil.Number(originalNodeTransition
                        ? transitionMinimumClearance
                        : GroundRoadMinimumTerrainClearance) +
                    ",\"minimumRoadAboveTerrainAllowed\":" + JsonUtil.Number(effectiveMinimumTerrainClearance) +
                    ",\"originalNodeTransition\":" + JsonUtil.Bool(originalNodeTransition) +
                    ",\"maximumLocalTerrainGrade\":" + JsonUtil.Number(GroundRoadLocalGradeLimit) +
                    ",\"settledShape\":{\"model\":\"levelEndsExceptStraightMiddleNodes\"" +
                    ",\"startKeepsSlope\":" + JsonUtil.Bool(settledStartKeepsSlope) +
                    ",\"endKeepsSlope\":" + JsonUtil.Bool(settledEndKeepsSlope) +
                    ",\"minimumRoadAboveTerrain\":" + JsonUtil.Number(settledShapeMinimumClearance) +
                    ",\"maxRoadToTerrainDelta\":" + JsonUtil.Number(settledShapeMaxDeviation) +
                    ",\"minimumRoadAboveTerrainAllowed\":" + JsonUtil.Number(
                        SettledShapeMinimumClearance(originalNodeTransition, transitionMinimumClearance)) +
                    ",\"maximumRoadToTerrainDelta\":" + JsonUtil.Number(
                        effectiveMaximumTerrainDeviation - SettledShapeClearanceReserve) + "}" +
                    ",\"safe\":" + JsonUtil.Bool(terrainProfileSafe) + "}" +
                    ",\"terrainCut\":{\"model\":\"finalSurfaceFollowsRoad\"" +
                    ",\"carvable\":" + JsonUtil.Bool(terrainCarvable) +
                    ",\"deepestCut\":" + JsonUtil.Number(terrainCutDepth) +
                    ",\"highestFill\":" + JsonUtil.Number(terrainCutFill) +
                    ",\"settledDeepestCut\":" + JsonUtil.Number(settledCutDepth) +
                    ",\"settledHighestFill\":" + JsonUtil.Number(settledCutFill) +
                    ",\"maximumCut\":" + JsonUtil.Number(RoadTerrainCut.MaximumCut) +
                    ",\"maximumFill\":" + JsonUtil.Number(GroundRoadTerrainDeviationLimit - GroundRoadSettlementTerrainReserve) + "}" +
                    ",\"validWithTerrainCut\":" + JsonUtil.Bool(validApartFromTerrain && (terrainProfileSafe || terrainCarvable)) +
                    ",\"constructionCost\":" + constructionCost +
                    ",\"maintenanceCost\":" + maintenanceCost +
                    ",\"toolErrors\":\"" + JsonUtil.Escape(finalErrors.ToString()) + "\"" +
                    ",\"checkBuildPositionErrors\":\"" + JsonUtil.Escape(errors.ToString()) + "\"" +
                    ",\"createNodeTestErrors\":\"" + JsonUtil.Escape(createNodeTestErrors.ToString()) + "\"" +
                    ",\"productionRate\":" + previewTestedProductionRate + "}");
            }

            if (info.m_class != null &&
                !UiStateCommands.IsServiceToolbarUsable(info.m_class.m_service))
            {
                return CommandResult.Fail(
                    "Network service toolbar is still locked or stale: " +
                    info.m_class.m_service + ". Refresh and verify /state/toolbar-unlocks before mutation.");
            }

            // Native NetTool releases the zoned buildings and power lines a ground track runs over
            // without any tool error, exactly as for a ground road, so rail gets the same refusal.
            if (RunsOnGround(info, startHeightOffset, endHeightOffset))
            {
                int buildingsUnderRoadCount;
                string buildingsUnderRoad = CollectBuildingsUnderRoad(
                    start, end, info.m_halfWidth, out buildingsUnderRoadCount);
                if (buildingsUnderRoadCount > 0)
                {
                    return CommandResult.Fail(
                        "Road corridor overlaps existing building footprints that CS1 would demolish: " +
                        buildingsUnderRoad + ".");
                }
                int powerLinesCrossedCount;
                string powerLinesCrossed = CollectPowerLinesCrossed(
                    start, end, info.m_halfWidth, out powerLinesCrossedCount);
                if (powerLinesCrossedCount > 0)
                {
                    return CommandResult.Fail(
                        "Road corridor crosses existing power lines that CS1 would delete: " +
                        powerLinesCrossed + ".");
                }
            }

            NetTool.ControlPoint buildStart = CreateControlPoint(
                start, startConnectionDirection, startNode, startHeightOffset);
            NetTool.ControlPoint buildMiddle = CreateControlPoint(
                (start + end) * 0.5f, requestedDirection, 0, (startHeightOffset + endHeightOffset) * 0.5f);
            NetTool.ControlPoint buildEnd = CreateControlPoint(
                end, endConnectionDirection, endNode, endHeightOffset);
            FastList<NetTool.NodePosition> nodePositions = new FastList<NetTool.NodePosition>();
            EconomyManager economy = Singleton<EconomyManager>.instance;
            long cashBeforeConstruction = economy.LastCashAmount;
            Dictionary<ushort, RoadTerrainSnapshot> adjacentTerrainBefore =
                CaptureRoadTerrainAtNodes(net, startNode, endNode);

            ushort testedNode;
            ushort testedSegment;
            int testedCost;
            int testedProductionRate;
            ToolBase.ToolErrors buildErrors = NetTool.CreateNode(
                info,
                buildStart,
                buildMiddle,
                buildEnd,
                nodePositions,
                1,
                true,
                false,
                true,
                true,
                false,
                false,
                0,
                out testedNode,
                out testedSegment,
                out testedCost,
                out testedProductionRate);
            if (buildErrors != ToolBase.ToolErrors.None)
            {
                return CommandResult.Fail("Road construction test failed: " + buildErrors + ".");
            }

            ushort builtNode = 0;
            ushort segment = 0;
            int chargedCost = 0;
            int actualProductionRate = 0;
            bool sameThreadCompletion = false;
            PlayerNetAI playerNetAI = info.m_netAI as PlayerNetAI;
            ManualMilestone createPassMilestone = playerNetAI == null
                ? null
                : playerNetAI.m_createPassMilestone;
            if (playerNetAI != null)
            {
                playerNetAI.m_createPassMilestone = null;
            }
            try
            {
                buildErrors = NetTool.CreateNode(
                    info,
                    buildStart,
                    buildMiddle,
                    buildEnd,
                    nodePositions,
                    1,
                    false,
                    false,
                    true,
                    true,
                    false,
                    false,
                    0,
                    out builtNode,
                    out segment,
                    out chargedCost,
                    out actualProductionRate);
            }
            catch (System.InvalidOperationException ex)
            {
                if (ex.Message == null ||
                    ex.Message.IndexOf("Already in the same thread") < 0 ||
                    segment == 0)
                {
                    throw;
                }
                sameThreadCompletion = true;
                buildErrors = ToolBase.ToolErrors.None;
            }
            finally
            {
                if (playerNetAI != null)
                {
                    playerNetAI.m_createPassMilestone = createPassMilestone;
                }
            }
            if (buildErrors != ToolBase.ToolErrors.None || segment == 0)
            {
                string rollbackError = null;
                bool rolledBack = segment == 0 ||
                    GameThreadHelpers.TryReleaseSegment(net, segment, false, out rollbackError);
                string refundError;
                bool economyRecovered = RefundConstructionCost(
                    economy,
                    cashBeforeConstruction,
                    chargedCost,
                    info,
                    out refundError);
                return CommandResult.FailWithRecovery(
                    "Road construction failed: " + buildErrors + "; local rollback " +
                    (rolledBack && economyRecovered
                        ? "succeeded, including construction-cost recovery."
                        : "was incomplete; fully restart and load the checkpoint. Entity rollback: " +
                          rollbackError + "; economy rollback: " + refundError),
                    segment != 0,
                    rolledBack && economyRecovered,
                    "netSegment",
                    segment);
            }

            RefreshRoadRenderState(net, segment);
            bool waterGridRefreshApplied = RefreshWaterNetworkState(net, segment, info);
            NetSegment builtSegment = net.m_segments.m_buffer[segment];
            string immediateGeometry = CaptureGeometryEvidence(net, segment);
            bool segmentCreated =
                (builtSegment.m_flags & NetSegment.Flags.Created) != NetSegment.Flags.None;
            bool prefabMatches = IsBuiltAs(builtSegment.Info, info);
            bool endpointsValid = builtSegment.m_startNode != 0 &&
                builtSegment.m_endNode != 0 &&
                builtSegment.m_startNode != builtSegment.m_endNode;
            bool geometryMatches = endpointsValid &&
                SegmentEndsMatch(net, builtSegment, start, end, 4f);
            int expectedLaneCount = info.m_lanes == null ? 0 : info.m_lanes.Length;
            int actualLaneCount = segmentCreated ? CountSegmentLanes(net, builtSegment) : -1;
            bool laneCountMatches = actualLaneCount == expectedLaneCount;
            bool actualTerrainRequired;
            int actualTerrainSampleCount;
            float actualTerrainDeviation;
            float actualTerrainLocalGrade;
            float actualMinimumTerrainClearance;
            float actualMaximumTerrainClearance;
            bool actualTerrainSafe = InspectActualGroundTerrainProfile(
                net,
                segment,
                out actualTerrainRequired,
                out actualTerrainSampleCount,
                out actualTerrainDeviation,
                out actualTerrainLocalGrade,
                out actualMinimumTerrainClearance,
                out actualMaximumTerrainClearance);
            bool existingNodeTransition = originalNodeTransition;
            float actualTransitionMinimumClearance = TransitionMinimumClearance(
                net, builtSegment.m_startNode, builtSegment.m_endNode);
            actualTerrainSafe = IsTerrainProfileAcceptable(
                actualTerrainSafe,
                existingNodeTransition,
                actualTransitionMinimumClearance,
                actualTerrainDeviation,
                actualTerrainLocalGrade,
                actualMinimumTerrainClearance);
            if (!segmentCreated ||
                !prefabMatches ||
                !endpointsValid ||
                !geometryMatches ||
                !laneCountMatches ||
                (!IsGroundRoad(info) && !actualTerrainSafe))
            {
                string rollbackError = null;
                bool rolledBack = true;
                if (segmentCreated)
                {
                    rolledBack = GameThreadHelpers.TryReleaseSegment(net, segment, false, out rollbackError);
                }
                string refundError;
                bool economyRecovered = RefundConstructionCost(
                    economy,
                    cashBeforeConstruction,
                    chargedCost,
                    info,
                    out refundError);
                CommandResult failure = CommandResult.FailWithRecovery(
                    "Road construction verification failed (created=" + segmentCreated +
                    ", prefabMatches=" + prefabMatches +
                    ", endpointsValid=" + endpointsValid +
                    ", geometryMatches=" + geometryMatches +
                    ", expectedLanes=" + expectedLaneCount +
                    ", actualLanes=" + actualLaneCount +
                    ", terrainProfileSafe=" + actualTerrainSafe +
                    ", maxRoadToTerrainDelta=" + actualTerrainDeviation +
                    ", minimumRoadAboveTerrain=" + actualMinimumTerrainClearance +
                    "); local rollback " +
                    (rolledBack && economyRecovered
                        ? "succeeded, including construction-cost recovery."
                        : "was incomplete; fully restart and load the checkpoint. Entity rollback: " +
                          rollbackError + "; economy rollback: " + refundError),
                    segmentCreated,
                    rolledBack && economyRecovered,
                    "netSegment",
                    segment);
                return WithGeometryEvidence(failure, immediateGeometry, immediateGeometry);
            }

            bool verificationPending = IsGroundRoad(info);
            bool milestonePending = verificationPending && createPassMilestone != null;
            bool milestoneUiRefreshQueued = false;
            bool milestoneApplied = milestonePending
                ? false
                : TryApplyCreatePassMilestone(
                    createPassMilestone,
                    out milestoneUiRefreshQueued);
            if (!milestonePending && !milestoneApplied)
            {
                string rollbackError;
                bool rolledBack = GameThreadHelpers.TryReleaseSegment(net, segment, false, out rollbackError);
                string refundError;
                bool economyRecovered = RefundConstructionCost(
                    economy,
                    cashBeforeConstruction,
                    chargedCost,
                    info,
                    out refundError);
                return CommandResult.FailWithRecovery(
                    "Road was constructed but its normal create-pass milestone did not apply; local rollback " +
                    (rolledBack && economyRecovered
                        ? "succeeded, including construction-cost recovery."
                        : "was incomplete; fully restart and load the checkpoint. Entity rollback: " +
                          rollbackError + "; economy rollback: " + refundError),
                    true,
                    rolledBack && economyRecovered,
                    "netSegment",
                    segment);
            }

            startNode = builtSegment.m_startNode;
            endNode = builtSegment.m_endNode;

            bool nameApplied = false;
            if (name != null && name.Length > 0)
            {
                try
                {
                    net.SetSegmentNameImpl(segment, name);
                    nameApplied = true;
                }
                catch (System.InvalidOperationException ex)
                {
                    if (ex.Message == null || ex.Message.IndexOf("Already in the same thread") < 0)
                    {
                        throw;
                    }
                }
            }

            if (verificationPending)
            {
                PendingRoadBuilds[segment] = new PendingRoadBuild
                {
                    Info = info,
                    Start = start,
                    End = end,
                    CashBeforeConstruction = cashBeforeConstruction,
                    ChargedCost = chargedCost,
                    AdjacentTerrainBefore = adjacentTerrainBefore,
                    ExistingNodeTransition = existingNodeTransition,
                    TerrainCut = allowTerrainCut && terrainCarvable,
                    CreatePassMilestone = createPassMilestone,
                    ImmediateGeometry = immediateGeometry
                };
            }

            string json = "{\"ok\":true,\"dryRun\":false,\"segmentId\":" + segment +
                ",\"startNodeId\":" + startNode +
                ",\"endNodeId\":" + endNode +
                ",\"roadPrefab\":\"" + JsonUtil.Escape(prefabName) + "\"" +
                ",\"heightOffset\":" + JsonUtil.Number(heightOffset) +
                ",\"requestedName\":\"" + JsonUtil.Escape(name) + "\"" +
                ",\"nameApplied\":" + JsonUtil.Bool(nameApplied) +
                ",\"constructionCost\":" + testedCost +
                ",\"sameThreadCompletion\":" + JsonUtil.Bool(sameThreadCompletion) +
                ",\"createPassMilestoneApplied\":" + JsonUtil.Bool(milestoneApplied) +
                ",\"createPassMilestonePending\":" + JsonUtil.Bool(milestonePending) +
                ",\"milestoneUiRefreshQueued\":" + JsonUtil.Bool(milestoneUiRefreshQueued) +
                ",\"waterGridRefreshApplied\":" + JsonUtil.Bool(waterGridRefreshApplied) +
                ",\"verificationPending\":" + JsonUtil.Bool(verificationPending) +
                ",\"geometryEvidence\":" + immediateGeometry +
                ",\"actualTerrainProfile\":{\"source\":\"nativeGenerateBezierAndLiveTerrain\"" +
                ",\"verificationStage\":\"immediate\"" +
                ",\"clearanceModel\":\"legacyNodeInsetProxyNotCalibratedSurface\"" +
                ",\"required\":" + JsonUtil.Bool(actualTerrainRequired) +
                ",\"sampleCount\":" + actualTerrainSampleCount +
                ",\"maxRoadToTerrainDelta\":" + JsonUtil.Number(actualTerrainDeviation) +
                ",\"minimumRoadAboveTerrain\":" + JsonUtil.Number(actualMinimumTerrainClearance) +
                ",\"maximumRoadAboveTerrain\":" + JsonUtil.Number(actualMaximumTerrainClearance) +
                ",\"maxLocalTerrainGrade\":" + JsonUtil.Number(actualTerrainLocalGrade) +
                ",\"maximumRoadToTerrainDelta\":" + JsonUtil.Number(
                    existingNodeTransition
                        ? ExistingNodeTransitionMaximumDeviation
                        : GroundRoadTerrainDeviationLimit) +
                ",\"minimumRoadAboveTerrainAllowed\":" + JsonUtil.Number(existingNodeTransition
                    ? actualTransitionMinimumClearance
                    : GroundRoadMinimumTerrainClearance) +
                ",\"maximumLocalTerrainGrade\":" + JsonUtil.Number(GroundRoadLocalGradeLimit) +
                ",\"originalNodeTransition\":" + JsonUtil.Bool(existingNodeTransition) +
                ",\"safe\":" + JsonUtil.Bool(actualTerrainSafe) + "}" +
                ",\"toolErrors\":\"" + JsonUtil.Escape(buildErrors.ToString()) + "\"}";

            Debug.Log("[SkylinesAgentBridge] Built road segment " + segment + " with prefab " + prefabName);
            return CommandResult.FromJson(json);
        }

        private static NetTool.ControlPoint CreateControlPoint(
            Vector3 position,
            Vector3 direction,
            ushort node,
            float elevation)
        {
            NetTool.ControlPoint point = new NetTool.ControlPoint();
            point.m_position = position;
            point.m_direction = direction;
            point.m_node = node;
            point.m_elevation = elevation;
            return point;
        }

        private static bool TryCreateCompleteSegment(
            NetManager manager,
            ref Randomizer randomizer,
            NetInfo info,
            ushort startNode,
            ushort endNode,
            Vector3 startDirection,
            Vector3 endDirection,
            uint buildIndex,
            bool invert,
            out ushort segmentId)
        {
            segmentId = 0;
            bool created = false;
            try
            {
                created = manager.CreateSegment(
                    out segmentId,
                    ref randomizer,
                    info,
                    startNode,
                    endNode,
                    startDirection,
                    endDirection,
                    buildIndex,
                    buildIndex,
                    invert);
            }
            catch (System.InvalidOperationException ex)
            {
                if (ex.Message == null || ex.Message.IndexOf("Already in the same thread") < 0)
                {
                    return false;
                }
                created = segmentId != 0;
            }

            if (!created || segmentId == 0 || segmentId >= manager.m_segments.m_buffer.Length)
            {
                return false;
            }
            NetSegment segment = manager.m_segments.m_buffer[segmentId];
            return (segment.m_flags & NetSegment.Flags.Created) != NetSegment.Flags.None &&
                segment.Info == info &&
                segment.m_startNode == startNode &&
                segment.m_endNode == endNode &&
                segment.m_startNode != segment.m_endNode &&
                CountSegmentLanes(manager, segment) == (info.m_lanes == null ? 0 : info.m_lanes.Length);
        }

        private static int CountSegmentLanes(NetManager manager, NetSegment segment)
        {
            int count = 0;
            uint laneId = segment.m_lanes;
            int maximum = segment.Info == null || segment.Info.m_lanes == null
                ? 0
                : segment.Info.m_lanes.Length;
            while (laneId != 0 && count <= maximum && laneId < manager.m_lanes.m_buffer.Length)
            {
                count++;
                laneId = manager.m_lanes.m_buffer[laneId].m_nextLane;
            }
            return laneId == 0 ? count : -1;
        }

        private static bool SegmentEndsMatch(
            NetManager manager,
            NetSegment segment,
            Vector3 requestedStart,
            Vector3 requestedEnd,
            float tolerance)
        {
            Vector3 actualStart = manager.m_nodes.m_buffer[segment.m_startNode].m_position;
            Vector3 actualEnd = manager.m_nodes.m_buffer[segment.m_endNode].m_position;
            float toleranceSquared = tolerance * tolerance;
            // NetTool is allowed to settle node elevation to native terrain. Endpoint
            // identity is therefore an XZ constraint; vertical correctness is checked
            // independently by InspectActualGroundTerrainProfile. Including Y here
            // incorrectly rolls back valid terrain-following roads on slopes.
            bool direct = HorizontalDistanceSquared(actualStart, requestedStart) <= toleranceSquared &&
                HorizontalDistanceSquared(actualEnd, requestedEnd) <= toleranceSquared;
            bool reversed = HorizontalDistanceSquared(actualStart, requestedEnd) <= toleranceSquared &&
                HorizontalDistanceSquared(actualEnd, requestedStart) <= toleranceSquared;
            return direct || reversed;
        }

        private static float HorizontalDistanceSquared(Vector3 first, Vector3 second)
        {
            float x = first.x - second.x;
            float z = first.z - second.z;
            return x * x + z * z;
        }

        private static bool RefundConstructionCost(
            EconomyManager economy,
            long cashBeforeConstruction,
            int chargedCost,
            NetInfo info,
            out string error)
        {
            error = null;
            long missing = cashBeforeConstruction - economy.LastCashAmount;
            long refund = chargedCost > 0 ? chargedCost : missing;
            if (refund <= 0)
            {
                return true;
            }
            if (refund > int.MaxValue)
            {
                error = "Required refund exceeds the supported integer range: " + refund + ".";
                return false;
            }
            // NetTool can return the charged construction cost before LastCashAmount
            // reflects that expense. Queue the matching refund from the authoritative
            // charged cost instead of treating an unchanged same-frame cash value as
            // proof that no refund is needed. The caller's regression check verifies
            // the settled economy value on a later API turn.
            economy.AddResource(EconomyManager.Resource.RefundAmount, (int)refund, info.m_class);
            return true;
        }

        // Native NetTool only reports ObjectCollision for "important" buildings;
        // growables under a new road are silently released on creation. List
        // every building whose footprint the road corridor overlaps so preview
        // and build can refuse instead of demolishing homes or factories.
        private static string CollectBuildingsUnderRoad(Vector3 start, Vector3 end, float roadHalfWidth, out int count)
        {
            count = 0;
            System.Text.StringBuilder items = new System.Text.StringBuilder();
            BuildingManager buildings = Singleton<BuildingManager>.instance;
            float length = VectorUtils.LengthXZ(end - start);
            int samples = Mathf.Max(2, Mathf.CeilToInt(length / 2f));
            float minX = Mathf.Min(start.x, end.x) - roadHalfWidth - 40f;
            float maxX = Mathf.Max(start.x, end.x) + roadHalfWidth + 40f;
            float minZ = Mathf.Min(start.z, end.z) - roadHalfWidth - 40f;
            float maxZ = Mathf.Max(start.z, end.z) + roadHalfWidth + 40f;
            for (int id = 1; id < buildings.m_buildings.m_buffer.Length; id++)
            {
                Building building = buildings.m_buildings.m_buffer[id];
                // Untouchable buildings belong to network nodes (pipe junctions, poles,
                // pillars); they move with their network instead of being demolished.
                if ((building.m_flags & Building.Flags.Created) == Building.Flags.None ||
                    (building.m_flags & Building.Flags.Deleted) != Building.Flags.None ||
                    (building.m_flags & Building.Flags.Untouchable) != Building.Flags.None ||
                    building.Info == null)
                {
                    continue;
                }
                Vector3 position = building.m_position;
                if (position.x < minX || position.x > maxX || position.z < minZ || position.z > maxZ)
                {
                    continue;
                }
                BuildingInfo info = building.Info;
                float halfWidth = Mathf.Max(1f, info.GetWidth() * 4f);
                float halfLength = Mathf.Max(1f, info.GetLength() * 4f);
                float cos = Mathf.Cos(-building.m_angle);
                float sin = Mathf.Sin(-building.m_angle);
                float best = float.MaxValue;
                for (int index = 0; index <= samples; index++)
                {
                    Vector3 point = Vector3.Lerp(start, end, (float)index / samples);
                    float dx = point.x - position.x;
                    float dz = point.z - position.z;
                    float localX = Mathf.Abs(dx * cos - dz * sin) - halfWidth;
                    float localZ = Mathf.Abs(dx * sin + dz * cos) - halfLength;
                    float distance = new Vector2(Mathf.Max(localX, 0f), Mathf.Max(localZ, 0f)).magnitude;
                    best = Mathf.Min(best, distance);
                }
                // Growables legitimately front onto the road edge (distance == half width).
                if (best >= roadHalfWidth - 0.5f)
                {
                    continue;
                }
                if (count > 0)
                {
                    items.Append(",");
                }
                items.Append("{\"id\":").Append(id);
                items.Append(",\"prefab\":\"").Append(JsonUtil.Escape(info.name)).Append("\"");
                items.Append(",\"service\":\"").Append(info.m_class != null ? info.m_class.m_service.ToString() : "").Append("\"");
                items.Append(",\"footprintDistance\":").Append(JsonUtil.Number(best)).Append("}");
                count++;
                if (count >= 20)
                {
                    break;
                }
            }
            return "[" + items.ToString() + "]";
        }

        // A ground road created across an existing power line releases the whole
        // power line segment without any tool error, cutting off whatever it fed.
        private static string CollectPowerLinesCrossed(Vector3 start, Vector3 end, float roadHalfWidth, out int count)
        {
            count = 0;
            System.Text.StringBuilder items = new System.Text.StringBuilder();
            NetManager net = NetManager.instance;
            float length = VectorUtils.LengthXZ(end - start);
            int samples = Mathf.Max(2, Mathf.CeilToInt(length / 2f));
            for (int id = 1; id < net.m_segments.m_buffer.Length; id++)
            {
                NetSegment segment = net.m_segments.m_buffer[id];
                if ((segment.m_flags & NetSegment.Flags.Created) == NetSegment.Flags.None ||
                    (segment.m_flags & NetSegment.Flags.Deleted) != NetSegment.Flags.None ||
                    !IsPowerLine(segment.Info))
                {
                    continue;
                }
                Vector3 lineStart = net.m_nodes.m_buffer[segment.m_startNode].m_position;
                Vector3 lineEnd = net.m_nodes.m_buffer[segment.m_endNode].m_position;
                float best = float.MaxValue;
                for (int index = 0; index <= samples; index++)
                {
                    Vector3 point = Vector3.Lerp(start, end, (float)index / samples);
                    best = Mathf.Min(best, DistanceToSegmentXZ(point, lineStart, lineEnd));
                }
                if (best >= roadHalfWidth + 4f)
                {
                    continue;
                }
                if (count > 0)
                {
                    items.Append(",");
                }
                items.Append("{\"segmentId\":").Append(id);
                items.Append(",\"distance\":").Append(JsonUtil.Number(best)).Append("}");
                count++;
                if (count >= 20)
                {
                    break;
                }
            }
            return "[" + items.ToString() + "]";
        }

        private static float DistanceToSegmentXZ(Vector3 point, Vector3 a, Vector3 b)
        {
            Vector2 p = new Vector2(point.x, point.z);
            Vector2 from = new Vector2(a.x, a.z);
            Vector2 delta = new Vector2(b.x - a.x, b.z - a.z);
            float lengthSquared = delta.sqrMagnitude;
            float t = lengthSquared > 0.0001f ? Mathf.Clamp01(Vector2.Dot(p - from, delta) / lengthSquared) : 0f;
            return (p - (from + delta * t)).magnitude;
        }

        private static ushort FindNearbyNode(Vector3 position, float maxDistance, NetInfo info)
        {
            NetManager net = NetManager.instance;
            float maxDistanceSq = maxDistance * maxDistance;

            for (ushort i = 1; i < net.m_nodes.m_buffer.Length; i++)
            {
                NetNode node = net.m_nodes.m_buffer[i];
                if ((node.m_flags & NetNode.Flags.Created) == NetNode.Flags.None)
                {
                    continue;
                }
                if (!CanReuseNode(node.Info, info))
                {
                    continue;
                }

                Vector3 delta = node.m_position - position;
                delta.y = 0f;
                if (delta.sqrMagnitude <= maxDistanceSq)
                {
                    return i;
                }
            }

            return 0;
        }

        private static bool WouldMergeShortCollinearExtension(
            NetManager manager,
            ushort startNode,
            ushort endNode,
            Vector3 requestedStart,
            Vector3 requestedEnd,
            float requestedLength)
        {
            if (requestedLength >= NativeShortSegmentMergeLength)
            {
                return false;
            }
            return IsShortCollinearDeadEndExtension(
                    manager, startNode, requestedStart, requestedEnd) ||
                IsShortCollinearDeadEndExtension(
                    manager, endNode, requestedEnd, requestedStart);
        }

        private static bool IsShortCollinearDeadEndExtension(
            NetManager manager,
            ushort nodeId,
            Vector3 nodePosition,
            Vector3 requestedOther)
        {
            if (nodeId == 0)
            {
                return false;
            }
            NetNode node = manager.m_nodes.m_buffer[nodeId];
            if (node.CountSegments() != 1)
            {
                return false;
            }
            ushort adjacentId = RoadGraphHelpers.GetSegmentId(node, 0);
            if (adjacentId == 0)
            {
                return false;
            }
            NetSegment adjacent = manager.m_segments.m_buffer[adjacentId];
            ushort otherNodeId = adjacent.m_startNode == nodeId
                ? adjacent.m_endNode
                : adjacent.m_startNode;
            Vector3 existingDirection = manager.m_nodes.m_buffer[otherNodeId].m_position - nodePosition;
            Vector3 requestedDirection = requestedOther - nodePosition;
            existingDirection.y = 0f;
            requestedDirection.y = 0f;
            if (existingDirection.sqrMagnitude < 0.01f || requestedDirection.sqrMagnitude < 0.01f)
            {
                return false;
            }
            existingDirection.Normalize();
            requestedDirection.Normalize();
            return Vector3.Dot(existingDirection, requestedDirection) <= -0.98f;
        }

        private static bool IsMetroBuildProtected(NetInfo info, string prefabName)
        {
            if (info == null || info.m_class == null)
            {
                return false;
            }

            return info.m_class.m_service == ItemClass.Service.PublicTransport &&
                info.m_class.m_subService == ItemClass.SubService.PublicTransportMetro &&
                prefabName != null &&
                prefabName.IndexOf("Metro Line") < 0;
        }

        private static bool ValidatePowerLineRoadClearance(
            NetInfo info,
            Vector3 start,
            Vector3 end,
            ushort startNode,
            ushort endNode,
            out float startRoadDistance,
            out float endRoadDistance,
            out bool startHasElectricity,
            out bool endHasElectricity,
            out bool gridConnectionSafe)
        {
            startRoadDistance = float.MaxValue;
            endRoadDistance = float.MaxValue;
            startHasElectricity = false;
            endHasElectricity = false;
            gridConnectionSafe = true;
            if (!IsPowerLine(info))
            {
                return true;
            }

            ElectricityManager electricity = Singleton<ElectricityManager>.instance;
            electricity.CheckElectricity(start, out startHasElectricity);
            electricity.CheckElectricity(end, out endHasElectricity);
            startHasElectricity = startHasElectricity || HasPowerPlantConnectionAreaNear(start);
            endHasElectricity = endHasElectricity || HasPowerPlantConnectionAreaNear(end);
            gridConnectionSafe = startNode != 0 || endNode != 0 || startHasElectricity || endHasElectricity;
            if (startNode == 0)
            {
                startRoadDistance = FindNearestRoadDistance(start);
            }
            if (endNode == 0)
            {
                endRoadDistance = FindNearestRoadDistance(end);
            }

            return (startNode != 0 || startRoadDistance >= PowerLineRoadClearance) &&
                (endNode != 0 || endRoadDistance >= PowerLineRoadClearance);
        }

        private static bool HasPowerPlantConnectionAreaNear(Vector3 point)
        {
            BuildingManager buildings = Singleton<BuildingManager>.instance;
            for (ushort id = 1; id < buildings.m_buildings.m_buffer.Length; id++)
            {
                Building building = buildings.m_buildings.m_buffer[id];
                if ((building.m_flags & Building.Flags.Created) == Building.Flags.None ||
                    building.Info == null ||
                    !(building.Info.m_buildingAI is PowerPlantAI))
                {
                    continue;
                }

                PowerPlantAI powerPlant = (PowerPlantAI)building.Info.m_buildingAI;
                if (powerPlant.m_electricityProduction <= 0)
                {
                    continue;
                }

                float connectionRadius = Mathf.Max(
                    8f,
                    building.Info.m_buildingAI.ElectricityGridRadius());
                Vector2 delta = new Vector2(
                    point.x - building.m_position.x,
                    point.z - building.m_position.z);
                if (delta.sqrMagnitude <= connectionRadius * connectionRadius)
                {
                    return true;
                }
            }
            return false;
        }

        private static bool ValidateRoadTerrainProfile(
            NetInfo info,
            Vector3 start,
            Vector3 end,
            Vector3 startDirection,
            Vector3 endDirection,
            bool reusesStartNode,
            bool reusesEndNode,
            bool startKeepsSlope,
            bool endKeepsSlope,
            bool originalNodeTransition,
            float transitionMinimumClearance,
            float heightOffset,
            float flatLength,
            out int sampleCount,
            out float maxTerrainDeviation,
            out float maxLocalTerrainGrade,
            out float minimumTerrainClearance,
            out float settledShapeMinimumClearance,
            out float settledShapeMaxDeviation)
        {
            sampleCount = 0;
            maxTerrainDeviation = 0f;
            maxLocalTerrainGrade = 0f;
            minimumTerrainClearance = 0f;
            settledShapeMinimumClearance = 0f;
            settledShapeMaxDeviation = 0f;
            if (!IsGroundRoad(info))
            {
                return true;
            }
            if (Mathf.Abs(heightOffset) > 0.01f)
            {
                return false;
            }
            minimumTerrainClearance = float.MaxValue;

            // Use the game's middle-point solver, not the straight endpoint
            // chord. This is still a pre-construction control curve: native
            // corners and junction updates only exist after construction.
            Bezier3 predicted = PredictedControlCurve(start, end, startDirection, endDirection);
            sampleCount = GeometrySampleCount(predicted);
            TerrainManager terrain = TerrainManager.instance;
            float previousTerrain = 0f;
            Vector3 previousPoint = start;
            for (int i = 0; i < sampleCount; i++)
            {
                float t = (float)i / (sampleCount - 1);
                Vector3 sample = predicted.Position(t);
                float terrainY = terrain.SampleRawHeightSmoothWithWater(sample, false, 0f);
                // The requested ground-road endpoints already include the
                // bounded placement lift above live terrain. Do not subtract
                // the historical 0.3m estimate here: native GenerateBezier
                // applies corner/node height offsets after construction, and
                // that fixed subtraction double-counted an uncalibrated
                // offset at original-map transitions. This remains a
                // pre-construction control-curve proxy; settled native curves
                // and terrain are still authoritative after mutation.
                float intendedRoadY = sample.y;
                float clearance = intendedRoadY - terrainY;
                // Stock map highway stubs can have a centerline endpoint a
                // fraction below the smoothed terrain. A new road must inherit
                // that exact node height to connect, so judge the new alignment
                // from its own samples while still retaining the endpoint for
                // local-grade measurement. This exemption applies only to an
                // endpoint that actually reuses an existing node.
                bool inheritedEndpoint =
                    (i == 0 && reusesStartNode) ||
                    (i == sampleCount - 1 && reusesEndNode);
                if (!inheritedEndpoint)
                {
                    minimumTerrainClearance = Mathf.Min(minimumTerrainClearance, clearance);
                    maxTerrainDeviation = Mathf.Max(maxTerrainDeviation, Mathf.Abs(clearance));
                }
                float sampleSpacing = new Vector2(sample.x - previousPoint.x, sample.z - previousPoint.z).magnitude;
                if (i > 0 && sampleSpacing > 0.001f)
                {
                    maxLocalTerrainGrade = Mathf.Max(
                        maxLocalTerrainGrade,
                        Mathf.Abs(terrainY - previousTerrain) / sampleSpacing);
                }
                previousTerrain = terrainY;
                previousPoint = sample;
            }

            if (minimumTerrainClearance == float.MaxValue)
            {
                minimumTerrainClearance = 0f;
            }
            float maximumAllowed = originalNodeTransition
                ? ExistingNodeTransitionMaximumDeviation
                : GroundRoadTerrainDeviationLimit;
            SampleSettledShapeClearance(start, end, startDirection, endDirection, startKeepsSlope, endKeepsSlope,
                reusesStartNode, reusesEndNode, info.m_halfWidth,
                out settledShapeMinimumClearance, out settledShapeMaxDeviation);
            return minimumTerrainClearance >= PreviewMinimumTerrainClearance(originalNodeTransition, transitionMinimumClearance) &&
                maxTerrainDeviation <= maximumAllowed - GroundRoadSettlementTerrainReserve &&
                maxLocalTerrainGrade <= GroundRoadLocalGradeLimit &&
                settledShapeMinimumClearance >= SettledShapeMinimumClearance(originalNodeTransition, transitionMinimumClearance) &&
                settledShapeMaxDeviation <= maximumAllowed - SettledShapeClearanceReserve;
        }

        private static float SettledShapeMinimumClearance(bool originalNodeTransition, float transitionMinimumClearance)
        {
            return (originalNodeTransition ? transitionMinimumClearance : GroundRoadMinimumTerrainClearance) +
                SettledShapeClearanceReserve;
        }

        // The control curve above follows the requested slope at both ends, but
        // the settled road levels its ends at every node that is not a straight
        // Middle node (see SettledRoadShape). Sample that level-ended curve too,
        // so a climb that settles below the terrain fails here instead of after
        // construction, rollback and a burned alignment.
        // At an existing Bend or Junction node the settled curve also starts at
        // the node's corner, offset along the road but at the node's height (live:
        // 4.8m at a 145-degree bend), so it starts lower against a rising terrain.
        // The road's half width stands in for that offset before construction.
        private static void SampleSettledShapeClearance(Vector3 start, Vector3 end,
            Vector3 startDirection, Vector3 endDirection, bool startKeepsSlope, bool endKeepsSlope,
            bool reusesStartNode, bool reusesEndNode, float cornerOffset,
            out float minimumClearance, out float maxDeviation)
        {
            Bezier3 settled = SettledShapeCurve(start, end, startDirection, endDirection,
                startKeepsSlope, endKeepsSlope, reusesStartNode, reusesEndNode, cornerOffset);
            int count = GeometrySampleCount(settled);
            TerrainManager terrain = TerrainManager.instance;
            minimumClearance = float.MaxValue;
            maxDeviation = 0f;
            for (int i = 0; i < count; i++)
            {
                if ((i == 0 && reusesStartNode) || (i == count - 1 && reusesEndNode))
                {
                    continue;
                }
                Vector3 sample = settled.Position((float)i / (count - 1));
                float clearance = sample.y - terrain.SampleRawHeightSmoothWithWater(sample, false, 0f);
                minimumClearance = Mathf.Min(minimumClearance, clearance);
                maxDeviation = Mathf.Max(maxDeviation, Mathf.Abs(clearance));
            }
            if (minimumClearance == float.MaxValue)
            {
                minimumClearance = 0f;
            }
        }

        private static Bezier3 SettledShapeCurve(Vector3 start, Vector3 end,
            Vector3 startDirection, Vector3 endDirection, bool startKeepsSlope, bool endKeepsSlope,
            bool reusesStartNode, bool reusesEndNode, float cornerOffset)
        {
            Vector3 startLevel = LevelDirection(startDirection);
            Vector3 endLevel = LevelDirection(endDirection);
            Bezier3 settled = new Bezier3();
            settled.a = reusesStartNode && !startKeepsSlope ? start + startLevel * cornerOffset : start;
            settled.d = reusesEndNode && !endKeepsSlope ? end + endLevel * cornerOffset : end;
            NetSegment.CalculateMiddlePoints(settled.a, startKeepsSlope ? startDirection : startLevel,
                settled.d, endKeepsSlope ? endDirection : endLevel,
                startKeepsSlope, endKeepsSlope, out settled.b, out settled.c);
            return settled;
        }

        private static Vector3 LevelDirection(Vector3 direction)
        {
            Vector3 level = new Vector3(direction.x, 0f, direction.z);
            return level.sqrMagnitude < 0.000001f ? direction : level.normalized;
        }

        // A reused node keeps the settled slope only if the new segment turns it
        // into a straight Middle node. A planning node's successor is unknown, so
        // it keeps the control-curve prediction that route previews already use.
        private static bool SettledEndKeepsSlope(NetManager manager, ushort nodeId, bool planningNode,
            Vector3 awayFromNode)
        {
            if (nodeId == 0)
            {
                return planningNode;
            }
            NetNode node = manager.m_nodes.m_buffer[nodeId];
            int existingSegments = 0;
            Vector3 existingDirection = Vector3.zero;
            for (int index = 0; index < 8; index++)
            {
                ushort segmentId = node.GetSegment(index);
                if (segmentId == 0)
                {
                    continue;
                }
                NetSegment segment = manager.m_segments.m_buffer[segmentId];
                existingSegments++;
                existingDirection = segment.m_startNode == nodeId ? segment.m_startDirection : segment.m_endDirection;
            }
            return SettledRoadShape.KeepsSlope(existingSegments, existingDirection.x, existingDirection.z,
                awayFromNode.x, awayFromNode.z);
        }

        // A short transition from a map-original node starts at the map's own
        // fixed height, which can sit only the settled minimum above flat
        // terrain. Requiring the extra preview reserve there rejects every
        // such entrance even though settled acceptance below allows it.
        private static float PreviewMinimumTerrainClearance(bool originalNodeTransition, float transitionMinimumClearance)
        {
            return originalNodeTransition
                ? transitionMinimumClearance
                : GroundRoadMinimumTerrainClearance + GroundRoadPreviewClearanceReserve;
        }

        private static bool IsTerrainProfileAcceptable(
            bool strictResult,
            bool existingNodeTransition,
            float transitionMinimumClearance,
            float maximumDeviation,
            float maximumLocalGrade,
            float minimumClearance)
        {
            return strictResult ||
                existingNodeTransition &&
                minimumClearance >= transitionMinimumClearance &&
                maximumDeviation <= ExistingNodeTransitionMaximumDeviation &&
                maximumLocalGrade <= GroundRoadLocalGradeLimit;
        }

        private static bool WouldInvokeNativeMoveMiddleNode(
            NetManager manager,
            NetInfo requestedInfo,
            ushort startNode,
            ushort endNode,
            Vector3 requestedStart,
            Vector3 requestedEnd)
        {
            return WouldInvokeNativeMoveMiddleNodeAtEndpoint(
                    manager, requestedInfo, startNode, requestedStart, requestedEnd) ||
                WouldInvokeNativeMoveMiddleNodeAtEndpoint(
                    manager, requestedInfo, endNode, requestedEnd, requestedStart);
        }

        private static bool WouldInvokeNativeMoveMiddleNodeAtEndpoint(
            NetManager manager,
            NetInfo requestedInfo,
            ushort nodeId,
            Vector3 nodePosition,
            Vector3 requestedOther)
        {
            if (nodeId == 0)
            {
                return false;
            }
            NetNode node = manager.m_nodes.m_buffer[nodeId];
            NetNode.Flags required = NetNode.Flags.End | NetNode.Flags.Moveable;
            if ((node.m_flags & (required | NetNode.Flags.Untouchable)) != required ||
                node.Info == null ||
                !node.Info.IsCombatible(requestedInfo))
            {
                return false;
            }

            for (int index = 0; index < 8; index++)
            {
                ushort segmentId = RoadGraphHelpers.GetSegmentId(node, index);
                if (segmentId == 0)
                {
                    continue;
                }
                NetSegment adjacent = manager.m_segments.m_buffer[segmentId];
                if (!NetSegment.IsStraight(
                    manager.m_nodes.m_buffer[adjacent.m_startNode].m_position,
                    adjacent.m_startDirection,
                    manager.m_nodes.m_buffer[adjacent.m_endNode].m_position,
                    adjacent.m_endDirection))
                {
                    return false;
                }

                Vector3 existingDirection = adjacent.m_startNode == nodeId
                    ? adjacent.m_startDirection
                    : adjacent.m_endDirection;
                Vector3 requestedDirection = requestedOther - nodePosition;
                requestedDirection.y = 0f;
                if (requestedDirection.sqrMagnitude < 0.01f)
                {
                    return false;
                }
                requestedDirection.Normalize();
                return requestedDirection.x * existingDirection.x +
                    requestedDirection.z * existingDirection.z < -0.999f;
            }
            return false;
        }

        private static void RefreshRoadRenderState(NetManager manager, ushort segmentId)
        {
            NetSegment segment = manager.m_segments.m_buffer[segmentId];
            if ((segment.m_flags & NetSegment.Flags.Created) == NetSegment.Flags.None)
            {
                return;
            }
            manager.UpdateSegment(segmentId);
            manager.UpdateNode(segment.m_startNode);
            manager.UpdateNode(segment.m_endNode);
            manager.UpdateSegmentRenderer(segmentId, true);
            manager.UpdateNodeRenderer(segment.m_startNode, true);
            manager.UpdateNodeRenderer(segment.m_endNode, true);
        }

        private static bool RefreshWaterNetworkState(
            NetManager manager,
            ushort segmentId,
            NetInfo info)
        {
            if (info == null || info.m_class == null ||
                info.m_class.m_service != ItemClass.Service.Water)
            {
                return false;
            }

            NetSegment segment = manager.m_segments.m_buffer[segmentId];
            ushort[] nodeIds = new ushort[] { segment.m_startNode, segment.m_endNode };
            for (int index = 0; index < nodeIds.Length; index++)
            {
                ushort nodeId = nodeIds[index];
                if (nodeId == 0) continue;
                NetNode node = manager.m_nodes.m_buffer[nodeId];
                NetInfo nodeInfo = node.Info;
                if (nodeInfo != null && nodeInfo.m_netAI != null)
                {
                    nodeInfo.m_netAI.UpdateNode(nodeId, ref node);
                    manager.m_nodes.m_buffer[nodeId] = node;
                    manager.UpdateNodeFlags(nodeId);
                }
            }

            Vector3 start = manager.m_nodes.m_buffer[segment.m_startNode].m_position;
            Vector3 end = manager.m_nodes.m_buffer[segment.m_endNode].m_position;
            Singleton<WaterManager>.instance.UpdateGrid(
                Mathf.Min(start.x, end.x) - 100f,
                Mathf.Min(start.z, end.z) - 100f,
                Mathf.Max(start.x, end.x) + 100f,
                Mathf.Max(start.z, end.z) + 100f);
            return true;
        }

        private static List<ushort> GetAdjacentRoadSegments(
            NetManager manager,
            ushort segmentId,
            NetSegment segment)
        {
            List<ushort> result = new List<ushort>();
            ushort[] nodeIds = new ushort[] { segment.m_startNode, segment.m_endNode };
            for (int nodeIndex = 0; nodeIndex < nodeIds.Length; nodeIndex++)
            {
                ushort nodeId = nodeIds[nodeIndex];
                if (nodeId == 0 || nodeId >= manager.m_nodes.m_buffer.Length)
                {
                    continue;
                }
                NetNode node = manager.m_nodes.m_buffer[nodeId];
                for (int slot = 0; slot < 8; slot++)
                {
                    ushort adjacentId = node.GetSegment(slot);
                    if (adjacentId == 0 || adjacentId == segmentId || result.Contains(adjacentId))
                    {
                        continue;
                    }
                    NetSegment adjacent = manager.m_segments.m_buffer[adjacentId];
                    if ((adjacent.m_flags & NetSegment.Flags.Created) == NetSegment.Flags.None ||
                        adjacent.Info == null ||
                        adjacent.Info.m_class == null ||
                        adjacent.Info.m_class.m_service != ItemClass.Service.Road)
                    {
                        continue;
                    }
                    result.Add(adjacentId);
                }
            }
            return result;
        }

        private static Dictionary<ushort, RoadTerrainSnapshot> CaptureRoadTerrainAtNodes(
            NetManager manager,
            ushort startNodeId,
            ushort endNodeId)
        {
            Dictionary<ushort, RoadTerrainSnapshot> result =
                new Dictionary<ushort, RoadTerrainSnapshot>();
            ushort[] nodeIds = new ushort[] { startNodeId, endNodeId };
            for (int nodeIndex = 0; nodeIndex < nodeIds.Length; nodeIndex++)
            {
                ushort nodeId = nodeIds[nodeIndex];
                if (nodeId == 0 || nodeId >= manager.m_nodes.m_buffer.Length)
                {
                    continue;
                }
                NetNode node = manager.m_nodes.m_buffer[nodeId];
                for (int slot = 0; slot < 8; slot++)
                {
                    ushort segmentId = node.GetSegment(slot);
                    if (segmentId == 0 || result.ContainsKey(segmentId))
                    {
                        continue;
                    }
                    NetSegment segment = manager.m_segments.m_buffer[segmentId];
                    if ((segment.m_flags & NetSegment.Flags.Created) == NetSegment.Flags.None ||
                        !IsGroundRoad(segment.Info))
                    {
                        continue;
                    }
                    bool required;
                    int samples;
                    float deviation;
                    float localGrade;
                    float minimumClearance;
                    float maximumClearance;
                    bool safe = InspectActualGroundTerrainProfile(
                        manager,
                        segmentId,
                        out required,
                        out samples,
                        out deviation,
                        out localGrade,
                        out minimumClearance,
                        out maximumClearance);
                    result[segmentId] = new RoadTerrainSnapshot
                    {
                        Safe = safe,
                        MaximumDeviation = deviation,
                        MaximumLocalGrade = localGrade,
                        MinimumClearance = minimumClearance
                    };
                }
            }
            return result;
        }

        private static bool InspectRoadTerrainRegression(
            NetManager manager,
            List<ushort> segmentIds,
            Dictionary<ushort, RoadTerrainSnapshot> baseline,
            out ushort unsafeSegmentId,
            out float unsafeMinimumClearance)
        {
            unsafeSegmentId = 0;
            unsafeMinimumClearance = 0f;
            for (int index = 0; index < segmentIds.Count; index++)
            {
                ushort segmentId = segmentIds[index];
                RefreshRoadRenderState(manager, segmentId);
                bool required;
                int samples;
                float deviation;
                float localGrade;
                float minimumClearance;
                float maximumClearance;
                bool safe = InspectActualGroundTerrainProfile(
                    manager,
                    segmentId,
                    out required,
                    out samples,
                    out deviation,
                    out localGrade,
                    out minimumClearance,
                    out maximumClearance);
                RoadTerrainSnapshot previous = null;
                bool hadBaseline = baseline != null &&
                    baseline.TryGetValue(segmentId, out previous);
                NetSegment inspectedSegment = manager.m_segments.m_buffer[segmentId];
                bool originalMapRoad =
                    (inspectedSegment.m_flags & NetSegment.Flags.Original) != NetSegment.Flags.None;
                float adjacentMinimumAllowed = originalMapRoad
                    ? ExistingNodeTransitionMinimumClearance
                    : GroundRoadMinimumTerrainClearance;
                bool junctionTransitionSafe =
                    minimumClearance >= adjacentMinimumAllowed &&
                    deviation <= ExistingNodeTransitionMaximumDeviation &&
                    localGrade <= GroundRoadLocalGradeLimit;
                // A road already outside the limits before (an original-map highway can sit at its
                // limit in a cut) regresses only when it gets worse than its own baseline: judged
                // against the limit alone, a rollback leaving it within centimetres of where it was
                // read as an unrecovered road and blocked all further construction.
                bool worseThanBaseline = hadBaseline &&
                    (minimumClearance < previous.MinimumClearance - AdjacentRegressionTolerance ||
                     deviation > previous.MaximumDeviation + AdjacentRegressionTolerance);
                bool regressed = !hadBaseline
                    ? !safe && !junctionTransitionSafe
                    : previous.Safe
                        ? !safe && !junctionTransitionSafe
                        : !junctionTransitionSafe && worseThanBaseline;
                // The raw heightmap keeps bumps the game has carved away, so a neighbour lower than
                // it is only harmed when the drawn terrain actually covers it.
                if (regressed && RoadTerrainCut.BuriedDepth(manager, segmentId) <= RoadTerrainCut.FinalSurfaceTolerance)
                {
                    regressed = false;
                }
                if (regressed)
                {
                    unsafeSegmentId = segmentId;
                    unsafeMinimumClearance = minimumClearance;
                    return false;
                }
            }
            return true;
        }

        private static float FindNearestRoadDistance(Vector3 point)
        {
            NetManager manager = NetManager.instance;
            float nearest = float.MaxValue;
            for (ushort i = 1; i < manager.m_segments.m_buffer.Length; i++)
            {
                NetSegment segment = manager.m_segments.m_buffer[i];
                if ((segment.m_flags & NetSegment.Flags.Created) == NetSegment.Flags.None ||
                    segment.Info == null ||
                    segment.Info.m_class == null ||
                    segment.Info.m_class.m_service != ItemClass.Service.Road)
                {
                    continue;
                }

                Vector3 start = manager.m_nodes.m_buffer[segment.m_startNode].m_position;
                Vector3 end = manager.m_nodes.m_buffer[segment.m_endNode].m_position;
                nearest = Mathf.Min(nearest, DistancePointToSegment2D(point, start, end));
            }
            return nearest;
        }

        private static float DistancePointToSegment2D(Vector3 point, Vector3 start, Vector3 end)
        {
            float dx = end.x - start.x;
            float dz = end.z - start.z;
            float lengthSq = dx * dx + dz * dz;
            if (lengthSq < 0.001f)
            {
                dx = point.x - start.x;
                dz = point.z - start.z;
                return Mathf.Sqrt(dx * dx + dz * dz);
            }

            float t = ((point.x - start.x) * dx + (point.z - start.z) * dz) / lengthSq;
            t = Mathf.Clamp01(t);
            float nearestX = start.x + dx * t;
            float nearestZ = start.z + dz * t;
            dx = point.x - nearestX;
            dz = point.z - nearestZ;
            return Mathf.Sqrt(dx * dx + dz * dz);
        }

        private static bool IsPowerLine(NetInfo info)
        {
            return info != null &&
                info.m_class != null &&
                info.m_class.m_service == ItemClass.Service.Electricity;
        }

        internal static bool InspectActualGroundTerrainProfile(
            NetManager manager,
            ushort segmentId,
            out bool required,
            out int sampleCount,
            out float maxTerrainDeviation,
            out float maxLocalTerrainGrade,
            out float minimumTerrainClearance,
            out float maximumTerrainClearance)
        {
            required = false;
            sampleCount = 0;
            maxTerrainDeviation = 0f;
            maxLocalTerrainGrade = 0f;
            minimumTerrainClearance = float.MaxValue;
            maximumTerrainClearance = float.MinValue;
            if (segmentId == 0 || segmentId >= manager.m_segments.m_buffer.Length)
            {
                return false;
            }

            NetSegment segment = manager.m_segments.m_buffer[segmentId];
            if ((segment.m_flags & NetSegment.Flags.Created) == NetSegment.Flags.None ||
                segment.Info == null)
            {
                return false;
            }
            required = IsGroundRoad(segment.Info);
            if (!required)
            {
                minimumTerrainClearance = 0f;
                maximumTerrainClearance = 0f;
                return true;
            }

            Bezier3 curve = segment.GenerateBezier(segmentId, segment.m_startNode);
            sampleCount = GeometrySampleCount(curve);
            TerrainManager terrain = TerrainManager.instance;
            float roadSurfaceOffset = GetRoadSurfaceOffset(manager, segmentId, segment);
            Vector3 previous = curve.a;
            float previousTerrainY = terrain.SampleRawHeightSmoothWithWater(previous, false, 0f);
            for (int index = 0; index < sampleCount; index++)
            {
                float t = sampleCount == 1 ? 0f : (float)index / (sampleCount - 1);
                float inverse = 1f - t;
                Vector3 point =
                    inverse * inverse * inverse * curve.a +
                    3f * inverse * inverse * t * curve.b +
                    3f * inverse * t * t * curve.c +
                    t * t * t * curve.d;
                float terrainY = terrain.SampleRawHeightSmoothWithWater(point, false, 0f);
                // Legacy acceptance proxy retained pending calibration. Native
                // GenerateBezier already includes m_surfaceLevel; do not label
                // this node-inset correction as proven physical road height.
                // CaptureGeometryEvidence reports raw and proxy values apart.
                float clearance = point.y + roadSurfaceOffset - terrainY;
                bool inheritedEndpoint =
                    (index == 0 && NodeHasOtherGroundRoadSegment(
                        manager, segment.m_startNode, segmentId)) ||
                    (index == sampleCount - 1 && NodeHasOtherGroundRoadSegment(
                        manager, segment.m_endNode, segmentId));
                if (!inheritedEndpoint)
                {
                    minimumTerrainClearance = Mathf.Min(minimumTerrainClearance, clearance);
                    maximumTerrainClearance = Mathf.Max(maximumTerrainClearance, clearance);
                    maxTerrainDeviation = Mathf.Max(
                        maxTerrainDeviation,
                        Mathf.Abs(clearance));
                }
                if (index > 0)
                {
                    float flatDistance = new Vector2(
                        point.x - previous.x,
                        point.z - previous.z).magnitude;
                    if (flatDistance > 0.001f)
                    {
                        maxLocalTerrainGrade = Mathf.Max(
                            maxLocalTerrainGrade,
                            Mathf.Abs(terrainY - previousTerrainY) / flatDistance);
                    }
                }
                previous = point;
                previousTerrainY = terrainY;
            }
            if (minimumTerrainClearance == float.MaxValue)
            {
                minimumTerrainClearance = 0f;
                maximumTerrainClearance = 0f;
            }
            return minimumTerrainClearance >= GroundRoadMinimumTerrainClearance &&
                maxTerrainDeviation <= GroundRoadTerrainDeviationLimit &&
                maxLocalTerrainGrade <= GroundRoadLocalGradeLimit;
        }

        private static bool NodeHasOtherGroundRoadSegment(
            NetManager manager,
            ushort nodeId,
            ushort excludedSegmentId)
        {
            if (manager == null || nodeId == 0 ||
                nodeId >= manager.m_nodes.m_buffer.Length)
            {
                return false;
            }
            NetNode node = manager.m_nodes.m_buffer[nodeId];
            for (int slot = 0; slot < 8; slot++)
            {
                ushort candidateId = node.GetSegment(slot);
                if (candidateId == 0 || candidateId == excludedSegmentId)
                {
                    continue;
                }
                NetSegment candidate = manager.m_segments.m_buffer[candidateId];
                if ((candidate.m_flags & NetSegment.Flags.Created) != NetSegment.Flags.None &&
                    IsGroundRoad(candidate.Info))
                {
                    return true;
                }
            }
            return false;
        }

        private static float GetRoadSurfaceOffset(
            NetManager manager,
            ushort segmentId,
            NetSegment segment)
        {
            if (manager == null ||
                segmentId == 0 ||
                segment.m_startNode == 0 ||
                segment.m_endNode == 0)
            {
                return 0f;
            }
            Bezier3 curve = segment.GenerateBezier(segmentId, segment.m_startNode);
            float startInset =
                manager.m_nodes.m_buffer[segment.m_startNode].m_position.y - curve.a.y;
            float endInset =
                manager.m_nodes.m_buffer[segment.m_endNode].m_position.y - curve.d.y;
            float total = 0f;
            int count = 0;
            if (startInset >= 0f && startInset <= 1f)
            {
                total += startInset;
                count++;
            }
            if (endInset >= 0f && endInset <= 1f)
            {
                total += endInset;
                count++;
            }
            return count == 0 ? 0f : total / count;
        }

        internal static bool IsGroundRoad(NetInfo info)
        {
            if (info == null || info.m_class == null || info.m_class.m_service != ItemClass.Service.Road)
            {
                return false;
            }

            string name = info.name == null ? "" : info.name;
            string aiName = info.m_netAI == null ? "" : info.m_netAI.GetType().Name;
            return !StringUtil.ContainsIgnoreCase(name, "Elevated") &&
                !StringUtil.ContainsIgnoreCase(name, "Bridge") &&
                !StringUtil.ContainsIgnoreCase(name, "Tunnel") &&
                !StringUtil.ContainsIgnoreCase(name, "Slope") &&
                !StringUtil.ContainsIgnoreCase(aiName, "Elevated") &&
                !StringUtil.ContainsIgnoreCase(aiName, "Bridge") &&
                !StringUtil.ContainsIgnoreCase(aiName, "Tunnel");
        }

        // Height offset at or below which a piece runs in a tunnel (the metro's underground line).
        internal const float UndergroundOffset = -8f;

        // A ground road, or ground train track that does not run in a tunnel end to end: the game
        // lays a Train Track with both ends underground as its tunnel, under the buildings and power
        // lines on the surface, so only a piece with an end at the surface clears what it runs over.
        internal static bool RunsOnGround(NetInfo info, float startHeightOffset, float endHeightOffset)
        {
            return IsGroundRoad(info) || (IsGroundRail(info) &&
                !(startHeightOffset <= UndergroundOffset && endHeightOffset <= UndergroundOffset));
        }

        // The requested network, or the variant of it the game lays for the piece's height: a train
        // track ramped or run underground is built as its slope or tunnel prefab.
        internal static bool IsBuiltAs(NetInfo built, NetInfo requested)
        {
            if (built == requested)
            {
                return true;
            }
            if (built == null || requested == null)
            {
                return false;
            }
            TrainTrackAI track = requested.m_netAI as TrainTrackAI;
            if (track != null)
            {
                return built == track.m_elevatedInfo || built == track.m_bridgeInfo ||
                    built == track.m_slopeInfo || built == track.m_tunnelInfo;
            }
            RoadAI road = requested.m_netAI as RoadAI;
            return road != null && (built == road.m_elevatedInfo || built == road.m_bridgeInfo ||
                built == road.m_slopeInfo || built == road.m_tunnelInfo);
        }

        // Surface train track (a station's own track included): it runs on the ground like a road,
        // so what lies under it is released. Elevated, bridge, slope and tunnel pieces are not.
        internal static bool IsGroundRail(NetInfo info)
        {
            if (info == null || info.m_class == null ||
                info.m_class.m_service != ItemClass.Service.PublicTransport ||
                info.m_class.m_subService != ItemClass.SubService.PublicTransportTrain ||
                info.m_netAI is TransportLineAI)
            {
                return false;
            }

            string name = info.name == null ? "" : info.name;
            string aiName = info.m_netAI == null ? "" : info.m_netAI.GetType().Name;
            return !StringUtil.ContainsIgnoreCase(name, "Elevated") &&
                !StringUtil.ContainsIgnoreCase(name, "Bridge") &&
                !StringUtil.ContainsIgnoreCase(name, "Tunnel") &&
                !StringUtil.ContainsIgnoreCase(name, "Slope") &&
                !StringUtil.ContainsIgnoreCase(aiName, "Elevated") &&
                !StringUtil.ContainsIgnoreCase(aiName, "Bridge") &&
                !StringUtil.ContainsIgnoreCase(aiName, "Tunnel");
        }

        private static string JsonNumberOrNull(float value)
        {
            return value == float.MaxValue ? "null" : JsonUtil.Number(value);
        }

        private static bool CanReuseNode(NetInfo existing, NetInfo requested)
        {
            if (existing == null || requested == null || existing.m_class == null || requested.m_class == null)
            {
                return false;
            }

            if (existing == requested)
            {
                return true;
            }

            if (existing.m_class.m_service == ItemClass.Service.PublicTransport &&
                requested.m_class.m_service == ItemClass.Service.PublicTransport &&
                existing.m_class.m_subService == requested.m_class.m_subService)
            {
                return true;
            }

            return existing.m_class.m_service == ItemClass.Service.Road &&
                requested.m_class.m_service == ItemClass.Service.Road;
        }

        private static Vector3 ResolveConnectionDirection(
            NetManager manager,
            ushort nodeId,
            NetInfo requested,
            Vector3 requestedDirection,
            bool isStart)
        {
            // Straight two-control-point NetTool placement derives its end
            // direction from the requested control points, including a snapped
            // existing node. Negating an adjacent segment direction here made
            // the terminal Bezier fold back into that segment. Node identity
            // and position stay fixed; native CreateNode and all geometry,
            // terrain, collision and settlement gates remain authoritative.
            return requestedDirection;
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

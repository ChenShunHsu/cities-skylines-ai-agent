using System.Collections.Generic;
using System.Text;
using ColossalFramework;

namespace SkylinesAgentBridge
{
    public static class BulldozeCommands
    {
        public static CommandResult PreviewBulldozeImpact(string body)
        {
            string entityType = JsonUtil.GetString(body, "entityType", "");
            ushort id = (ushort)JsonUtil.GetNumber(body, "id", 0f);
            if (id == 0) return CommandResult.Fail("id is required.");
            return Analyze(entityType, id);
        }

        public static CommandResult Bulldoze(string body)
        {
            string entityType = JsonUtil.GetString(body, "entityType", "");
            ushort id = (ushort)JsonUtil.GetNumber(body, "id", 0f);
            bool keepNodes = JsonUtil.GetBool(body, "keepNodes", false);
            bool dryRun = JsonUtil.GetBool(body, "dryRun", false);
            bool confirmRisks = JsonUtil.GetBool(body, "confirmRisks", false);

            if (id == 0) return CommandResult.Fail("id is required.");

            CommandResult impact = Analyze(entityType, id);
            if (!impact.Ok) return impact;
            bool safe = impact.Json.IndexOf("\"safe\":true") >= 0;
            if (dryRun)
            {
                return CommandResult.FromJson("{\"ok\":true,\"dryRun\":true,\"impact\":" + impact.Json + "}");
            }
            if (!safe && !confirmRisks)
            {
                return CommandResult.Fail(
                    "Bulldoze impact contains blocking risks. Review /commands/preview-bulldoze-impact and set confirmRisks=true only after a verified checkpoint and an evidence-backed recovery plan.");
            }

            int refund = 0;
            if (entityType == "building")
            {
                BuildingManager manager = BuildingManager.instance;
                BuildingInfo info = manager.m_buildings.m_buffer[id].Info;
                refund = BulldozeRefund.Building(id);
                try
                {
                    GameThreadHelpers.ReleaseBuilding(manager, id);
                    BulldozeRefund.Pay(refund, info == null ? null : info.m_class);
                }
                catch (System.Exception ex)
                {
                    return CommandResult.Fail("Failed to release building " + id + ": " + ex.Message);
                }
            }
            else if (entityType == "netSegment")
            {
                NetManager manager = NetManager.instance;
                NetInfo info = manager.m_segments.m_buffer[id].Info;
                refund = BulldozeRefund.Segment(id);
                try
                {
                    GameThreadHelpers.ReleaseSegment(manager, id, keepNodes);
                    BulldozeRefund.Pay(refund, info == null ? null : info.m_class);
                }
                catch (System.Exception ex)
                {
                    return CommandResult.Fail("Failed to release net segment " + id + ": " + ex.Message);
                }
            }
            else if (entityType == "netNode")
            {
                NetManager manager = NetManager.instance;
                string releaseError;
                if (!GameThreadHelpers.TryReleaseNode(manager, id, out releaseError))
                {
                    return CommandResult.Fail("Failed to release net node " + id + ": " + releaseError);
                }
            }
            else
            {
                return CommandResult.Fail("Unsupported entityType. Use building, netSegment, or netNode.");
            }

            return CommandResult.FromJson("{\"ok\":true,\"dryRun\":false,\"entityType\":\"" +
                JsonUtil.Escape(entityType) + "\",\"id\":" + id +
                ",\"keepNodes\":" + JsonUtil.Bool(keepNodes) +
                ",\"refund\":" + JsonUtil.Number(refund / 100f) +
                ",\"confirmedRisks\":" + JsonUtil.Bool(confirmRisks) +
                ",\"impact\":" + impact.Json + "}");
        }

        private static CommandResult Analyze(string entityType, ushort id)
        {
            StringBuilder risks = new StringBuilder();
            int riskCount = 0;
            string details;

            if (entityType == "building")
            {
                BuildingManager manager = BuildingManager.instance;
                if (id >= manager.m_buildings.m_buffer.Length) return CommandResult.Fail("Building id is out of range: " + id);
                Building building = manager.m_buildings.m_buffer[id];
                if ((building.m_flags & Building.Flags.Created) == Building.Flags.None) return CommandResult.Fail("Building was not found: " + id);
                BuildingInfo info = building.Info;
                string aiName = info == null || info.m_buildingAI == null ? "" : info.m_buildingAI.GetType().Name;
                int stored = building.m_garbageBuffer + building.m_customBuffer1 + building.m_customBuffer2;
                bool storageFacility = StringUtil.ContainsIgnoreCase(aiName, "Landfill") ||
                    StringUtil.ContainsIgnoreCase(aiName, "Cemetery");
                int assignedLines = CountAssignedTransportLines(id);
                bool lastActive = IsLastActiveFacilityOfType(id, building);
                AppendRisk(risks, ref riskCount, storageFacility && stored > 0, "nonEmptyStorageFacility");
                AppendRisk(risks, ref riskCount, assignedLines > 0, "assignedTransportDepot");
                AppendRisk(risks, ref riskCount, lastActive, "lastActiveFacilityOfService");
                details = "{\"prefab\":\"" + JsonUtil.Escape(info == null ? "" : info.name) +
                    "\",\"service\":\"" + JsonUtil.Escape(info == null || info.m_class == null ? "" : info.m_class.m_service.ToString()) +
                    "\",\"storedUnits\":" + stored +
                    ",\"assignedTransportLines\":" + assignedLines +
                    ",\"lastActiveFacilityOfService\":" + JsonUtil.Bool(lastActive) + "}";
            }
            else if (entityType == "netSegment")
            {
                NetManager manager = NetManager.instance;
                if (id >= manager.m_segments.m_buffer.Length) return CommandResult.Fail("Net segment id is out of range: " + id);
                NetSegment segment = manager.m_segments.m_buffer[id];
                if ((segment.m_flags & NetSegment.Flags.Created) == NetSegment.Flags.None) return CommandResult.Fail("Net segment was not found: " + id);
                // a dead-end segment (one endpoint has no other segment) only drops that leaf; it
                // cannot split the remaining network, so it is not a cut
                bool leaf = CountSegments(segment.m_startNode) <= 1 || CountSegments(segment.m_endNode) <= 1;
                bool cut = !leaf && !HasAlternatePath(segment.m_startNode, segment.m_endNode, id, segment.Info);
                bool outside = IsOutsideNode(segment.m_startNode) || IsOutsideNode(segment.m_endNode);
                AppendRisk(risks, ref riskCount, cut, "networkCutSegment");
                // the cut check no longer covers leaves, so buildings fronting a dead end stay protected
                AppendRisk(risks, ref riskCount, leaf && CountAccessBuildings(id) > 0, "deadEndAccessBuildings");
                AppendRisk(risks, ref riskCount, outside, "outsideConnectionSegment");
                details = "{\"prefab\":\"" + JsonUtil.Escape(segment.Info == null ? "" : segment.Info.name) +
                    "\",\"startNodeId\":" + segment.m_startNode +
                    ",\"endNodeId\":" + segment.m_endNode +
                    ",\"networkCut\":" + JsonUtil.Bool(cut) +
                    ",\"deadEndSegment\":" + JsonUtil.Bool(leaf) +
                    ",\"touchesOutsideConnection\":" + JsonUtil.Bool(outside) +
                    ",\"roadDependencies\":" + InspectRoadDependencies(id, segment.Info) + "}";
            }
            else if (entityType == "netNode")
            {
                NetManager manager = NetManager.instance;
                if (id >= manager.m_nodes.m_buffer.Length) return CommandResult.Fail("Net node id is out of range: " + id);
                NetNode node = manager.m_nodes.m_buffer[id];
                if ((node.m_flags & NetNode.Flags.Created) == NetNode.Flags.None) return CommandResult.Fail("Net node was not found: " + id);
                int connected = CountConnectedSegments(node);
                bool outside = (node.m_flags & NetNode.Flags.Outside) != NetNode.Flags.None;
                AppendRisk(risks, ref riskCount, connected > 0, "connectedNetworkNode");
                AppendRisk(risks, ref riskCount, outside, "outsideConnectionNode");
                details = "{\"prefab\":\"" + JsonUtil.Escape(node.Info == null ? "" : node.Info.name) +
                    "\",\"connectedSegments\":" + connected +
                    ",\"outsideConnection\":" + JsonUtil.Bool(outside) + "}";
            }
            else
            {
                return CommandResult.Fail("Unsupported entityType. Use building, netSegment, or netNode.");
            }

            return CommandResult.FromJson("{\"ok\":true,\"entityType\":\"" + JsonUtil.Escape(entityType) +
                "\",\"id\":" + id +
                ",\"safe\":" + JsonUtil.Bool(riskCount == 0) +
                ",\"riskCount\":" + riskCount +
                ",\"risks\":[" + risks.ToString() + "]" +
                ",\"details\":" + details + "}");
        }

        private static string InspectRoadDependencies(ushort targetId, NetInfo info)
        {
            if (info == null || info.m_class == null || info.m_class.m_service != ItemClass.Service.Road)
                return "{\"supported\":false,\"operationalImpactComplete\":false,\"reason\":\"Target is not a Road network\"}";
            try
            {
                NetManager manager = NetManager.instance;
                List<RoadRemovalDependencies.Edge> edges = new List<RoadRemovalDependencies.Edge>();
                HashSet<ushort> outside = new HashSet<ushort>();
                for (int i = 1; i < manager.m_segments.m_buffer.Length; i++)
                {
                    NetSegment road = manager.m_segments.m_buffer[i];
                    if ((road.m_flags & (NetSegment.Flags.Created | NetSegment.Flags.Deleted)) != NetSegment.Flags.Created ||
                        road.Info == null || road.Info.m_class == null || road.Info.m_class.m_service != ItemClass.Service.Road) continue;
                    edges.Add(new RoadRemovalDependencies.Edge { Id = (ushort)i, Start = road.m_startNode, End = road.m_endNode });
                    if (IsOutsideNode(road.m_startNode)) outside.Add(road.m_startNode);
                    if (IsOutsideNode(road.m_endNode)) outside.Add(road.m_endNode);
                }
                List<RoadRemovalDependencies.Access> access = new List<RoadRemovalDependencies.Access>();
                BuildingManager buildings = BuildingManager.instance;
                for (int i = 1; i < buildings.m_buildings.m_buffer.Length; i++)
                {
                    Building building = buildings.m_buildings.m_buffer[i];
                    if ((building.m_flags & (Building.Flags.Created | Building.Flags.Deleted)) != Building.Flags.Created) continue;
                    BuildingInfo buildingInfo = building.Info;
                    bool unknown = buildingInfo == null || buildingInfo.m_class == null;
                    ItemClass.Service service = unknown ? ItemClass.Service.None : buildingInfo.m_class.m_service;
                    bool growable = service == ItemClass.Service.Residential || service == ItemClass.Service.Commercial ||
                        service == ItemClass.Service.Industrial || service == ItemClass.Service.Office;
                    // Missing native associations remain unknown; do not substitute the nearest road.
                    if (unknown || growable || building.m_accessSegment != 0 || PlacementCommands.RequiresRoadAccess(buildingInfo))
                        access.Add(new RoadRemovalDependencies.Access { Building = (ushort)i,
                            Segment = unknown ? (ushort)0 : building.m_accessSegment, Service = !unknown && !growable });
                }
                return RoadRemovalDependencies.Analyze(targetId, edges, outside, access).ToJson();
            }
            catch (System.Exception error)
            {
                // Diagnostic failure must never turn the original impact policy into a pass.
                return "{\"supported\":false,\"operationalImpactComplete\":false,\"reason\":\"" +
                    JsonUtil.Escape(error.GetType().Name + ": " + error.Message) + "\"}";
            }
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

        private static int CountAccessBuildings(ushort segmentId)
        {
            BuildingManager manager = Singleton<BuildingManager>.instance;
            int count = 0;
            for (int i = 1; i < manager.m_buildings.m_buffer.Length; i++)
            {
                Building building = manager.m_buildings.m_buffer[i];
                if ((building.m_flags & (Building.Flags.Created | Building.Flags.Deleted)) == Building.Flags.Created &&
                    building.m_accessSegment == segmentId) count++;
            }
            return count;
        }

        private static int CountSegments(ushort node)
        {
            return node == 0 ? 0 : Singleton<NetManager>.instance.m_nodes.m_buffer[node].CountSegments();
        }

        private static bool HasAlternatePath(ushort startNode, ushort endNode, ushort excludedSegment, NetInfo targetInfo)
        {
            if (startNode == 0 || endNode == 0) return false;
            NetManager manager = Singleton<NetManager>.instance;
            Queue<ushort> queue = new Queue<ushort>();
            HashSet<ushort> visited = new HashSet<ushort>();
            queue.Enqueue(startNode);
            visited.Add(startNode);
            while (queue.Count > 0)
            {
                ushort nodeId = queue.Dequeue();
                if (nodeId == endNode) return true;
                NetNode node = manager.m_nodes.m_buffer[nodeId];
                for (int i = 0; i < 8; i++)
                {
                    ushort segmentId = GetSegment(node, i);
                    if (segmentId == 0 || segmentId == excludedSegment) continue;
                    NetSegment segment = manager.m_segments.m_buffer[segmentId];
                    if ((segment.m_flags & NetSegment.Flags.Created) == NetSegment.Flags.None ||
                        !SameNetworkClass(segment.Info, targetInfo)) continue;
                    ushort next = segment.m_startNode == nodeId ? segment.m_endNode : segment.m_startNode;
                    if (next != 0 && visited.Add(next)) queue.Enqueue(next);
                }
            }
            return false;
        }

        private static bool SameNetworkClass(NetInfo a, NetInfo b)
        {
            if (a == null || b == null || a.m_class == null || b.m_class == null) return false;
            return a.m_class.m_service == b.m_class.m_service &&
                a.m_class.m_subService == b.m_class.m_subService;
        }

        private static bool IsOutsideNode(ushort nodeId)
        {
            if (nodeId == 0) return false;
            return (Singleton<NetManager>.instance.m_nodes.m_buffer[nodeId].m_flags &
                NetNode.Flags.Outside) != NetNode.Flags.None;
        }

        private static int CountConnectedSegments(NetNode node)
        {
            int count = 0;
            for (int i = 0; i < 8; i++) if (GetSegment(node, i) != 0) count++;
            return count;
        }

        private static ushort GetSegment(NetNode node, int index)
        {
            if (index == 0) return node.m_segment0;
            if (index == 1) return node.m_segment1;
            if (index == 2) return node.m_segment2;
            if (index == 3) return node.m_segment3;
            if (index == 4) return node.m_segment4;
            if (index == 5) return node.m_segment5;
            if (index == 6) return node.m_segment6;
            return node.m_segment7;
        }

        private static void AppendRisk(StringBuilder json, ref int count, bool condition, string risk)
        {
            if (!condition) return;
            if (count > 0) json.Append(",");
            json.Append("\"").Append(JsonUtil.Escape(risk)).Append("\"");
            count++;
        }
    }
}

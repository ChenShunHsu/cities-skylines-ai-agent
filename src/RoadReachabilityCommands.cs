using System.Collections.Generic;
using System.Text;
using ColossalFramework;
using UnityEngine;

namespace SkylinesAgentBridge
{
    public static class RoadReachabilityCommands
    {
        public static CommandResult BuildRoadReachabilityJson(ushort segmentId)
        {
            NetManager manager = NetManager.instance;
            if (segmentId == 0 || segmentId >= manager.m_segments.m_buffer.Length)
            {
                return CommandResult.Fail("id is required and must be a valid road segment.");
            }
            NetSegment candidate = manager.m_segments.m_buffer[segmentId];
            if (!RoadGraphHelpers.IsCreatedRoad(candidate))
            {
                return CommandResult.Fail("Road segment was not found: " + segmentId);
            }

            bool startToEnd;
            bool endToStart;
            RoadGraphHelpers.GetVehicleDirections(candidate, out startToEnd, out endToStart);
            List<ushort> inboundStarts = new List<ushort>();
            List<ushort> outboundStarts = new List<ushort>();
            if (startToEnd)
            {
                inboundStarts.Add(candidate.m_startNode);
                outboundStarts.Add(candidate.m_endNode);
            }
            if (endToStart)
            {
                inboundStarts.Add(candidate.m_endNode);
                outboundStarts.Add(candidate.m_startNode);
            }

            RouteResult inbound = FindOutside(manager, inboundStarts, true);
            RouteResult outbound = FindOutside(manager, outboundStarts, false);
            bool safe = inbound.Found && outbound.Found;
            return CommandResult.FromJson("{\"ok\":true,\"segmentId\":" + segmentId +
                ",\"inverted\":" + JsonUtil.Bool((candidate.m_flags & NetSegment.Flags.Invert) != NetSegment.Flags.None) +
                ",\"directionality\":\"" + Directionality(startToEnd, endToStart) + "\"" +
                ",\"inboundFromOutside\":" + inbound.ToJson() +
                ",\"outboundToOutside\":" + outbound.ToJson() +
                ",\"safeForTwoWayFreight\":" + JsonUtil.Bool(safe) +
                ",\"limitations\":{\"nativeLaneAndSegmentDirections\":true," +
                "\"turnRestrictionsAndDistrictHeavyTrafficPoliciesNotEvaluated\":true," +
                "\"postBuildVehiclePathVerificationRequired\":true}}");
        }

        private static RouteResult FindOutside(
            NetManager manager,
            List<ushort> starts,
            bool reverse)
        {
            RouteResult result = new RouteResult();
            if (starts.Count == 0)
            {
                return result;
            }

            bool[] visited = new bool[manager.m_nodes.m_buffer.Length];
            ushort[] previousNode = new ushort[manager.m_nodes.m_buffer.Length];
            ushort[] previousSegment = new ushort[manager.m_nodes.m_buffer.Length];
            Queue<ushort> queue = new Queue<ushort>();
            for (int i = 0; i < starts.Count; i++)
            {
                ushort node = starts[i];
                if (node == 0 || visited[node]) continue;
                visited[node] = true;
                queue.Enqueue(node);
            }

            ushort outside = 0;
            while (queue.Count > 0)
            {
                ushort nodeId = queue.Dequeue();
                if (RoadGraphHelpers.IsOutsideNode(manager, nodeId))
                {
                    outside = nodeId;
                    break;
                }
                NetNode node = manager.m_nodes.m_buffer[nodeId];
                for (int slot = 0; slot < 8; slot++)
                {
                    ushort connectedId = RoadGraphHelpers.GetSegmentId(node, slot);
                    if (connectedId == 0) continue;
                    NetSegment connected = manager.m_segments.m_buffer[connectedId];
                    if (!RoadGraphHelpers.IsCreatedRoad(connected)) continue;
                    bool startToEnd;
                    bool endToStart;
                    RoadGraphHelpers.GetVehicleDirections(connected, out startToEnd, out endToStart);
                    ushort neighbor = 0;
                    if (!reverse)
                    {
                        if (nodeId == connected.m_startNode && startToEnd) neighbor = connected.m_endNode;
                        else if (nodeId == connected.m_endNode && endToStart) neighbor = connected.m_startNode;
                    }
                    else
                    {
                        if (nodeId == connected.m_endNode && startToEnd) neighbor = connected.m_startNode;
                        else if (nodeId == connected.m_startNode && endToStart) neighbor = connected.m_endNode;
                    }
                    if (neighbor == 0 || visited[neighbor]) continue;
                    visited[neighbor] = true;
                    previousNode[neighbor] = nodeId;
                    previousSegment[neighbor] = connectedId;
                    queue.Enqueue(neighbor);
                }
            }

            if (outside == 0)
            {
                return result;
            }

            result.Found = true;
            result.OutsideNodeId = outside;
            ushort cursor = outside;
            while (previousSegment[cursor] != 0)
            {
                ushort routeSegmentId = previousSegment[cursor];
                result.SegmentIds.Add(routeSegmentId);
                NetSegment routeSegment = manager.m_segments.m_buffer[routeSegmentId];
                Vector3 start = manager.m_nodes.m_buffer[routeSegment.m_startNode].m_position;
                Vector3 end = manager.m_nodes.m_buffer[routeSegment.m_endNode].m_position;
                result.Distance += Vector3.Distance(start, end);
                result.TrafficDensityTotal += routeSegment.m_trafficDensity;
                if (routeSegment.m_trafficDensity > result.MaximumTrafficDensity)
                {
                    result.MaximumTrafficDensity = routeSegment.m_trafficDensity;
                }
                cursor = previousNode[cursor];
            }
            if (!reverse)
            {
                result.SegmentIds.Reverse();
            }
            return result;
        }

        private static string Directionality(bool startToEnd, bool endToStart)
        {
            if (startToEnd && endToStart) return "Bidirectional";
            if (startToEnd) return "StartToEnd";
            if (endToStart) return "EndToStart";
            return "NoVehicleLanes";
        }

        private sealed class RouteResult
        {
            public bool Found;
            public ushort OutsideNodeId;
            public float Distance;
            public int TrafficDensityTotal;
            public byte MaximumTrafficDensity;
            public readonly List<ushort> SegmentIds = new List<ushort>();

            public string ToJson()
            {
                StringBuilder ids = new StringBuilder();
                for (int i = 0; i < SegmentIds.Count; i++)
                {
                    if (i > 0) ids.Append(",");
                    ids.Append(SegmentIds[i]);
                }
                float averageTraffic = SegmentIds.Count == 0
                    ? 0f
                    : (float)TrafficDensityTotal / SegmentIds.Count;
                return "{\"found\":" + JsonUtil.Bool(Found) +
                    ",\"outsideNodeId\":" + OutsideNodeId +
                    ",\"segmentCount\":" + SegmentIds.Count +
                    ",\"distance\":" + JsonUtil.Number(Distance) +
                    ",\"averageTrafficDensity\":" + JsonUtil.Number(averageTraffic) +
                    ",\"maximumTrafficDensity\":" + MaximumTrafficDensity +
                    ",\"segmentIds\":[" + ids.ToString() + "]}";
            }
        }
    }
}

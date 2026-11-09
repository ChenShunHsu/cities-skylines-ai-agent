using System;
using System.Collections.Generic;
using System.Text;
using ColossalFramework;
using UnityEngine;

namespace SkylinesAgentBridge
{
    public static class TrafficStateCommands
    {
        public static CommandResult BuildTrafficJson(int limit, int minimumDensity)
        {
            limit = Mathf.Clamp(limit, 0, 500);
            minimumDensity = Mathf.Clamp(minimumDensity, 0, 255);
            NetManager net = Singleton<NetManager>.instance;
            VehicleManager vehicles = Singleton<VehicleManager>.instance;
            List<TrafficSegment> congested = new List<TrafficSegment>();
            int roadSegments = 0;
            long totalDensity = 0;
            int severeSegments = 0;

            for (ushort id = 1; id < net.m_segments.m_buffer.Length; id++)
            {
                NetSegment segment = net.m_segments.m_buffer[id];
                if ((segment.m_flags & NetSegment.Flags.Created) == NetSegment.Flags.None || segment.Info == null || segment.Info.m_class == null) continue;
                if (segment.Info.m_class.m_service != ItemClass.Service.Road) continue;
                roadSegments++;
                int density = segment.m_trafficDensity;
                totalDensity += density;
                if (density >= 192) severeSegments++;
                if (density < minimumDensity) continue;
                Vector3 start = net.m_nodes.m_buffer[segment.m_startNode].m_position;
                Vector3 end = net.m_nodes.m_buffer[segment.m_endNode].m_position;
                congested.Add(new TrafficSegment(id, segment.Info.name, density, segment.m_trafficBuffer, segment.m_averageLength, (start + end) * 0.5f));
            }

            congested.Sort(TrafficSegment.Compare);
            StringBuilder items = new StringBuilder();
            int emitted = Mathf.Min(limit, congested.Count);
            for (int i = 0; i < emitted; i++)
            {
                TrafficSegment item = congested[i];
                if (i > 0) items.Append(",");
                items.Append("{\"id\":").Append(item.Id);
                items.Append(",\"prefab\":\"").Append(JsonUtil.Escape(item.Prefab)).Append("\"");
                items.Append(",\"density\":").Append(item.Density);
                items.Append(",\"trafficBuffer\":").Append(item.TrafficBuffer);
                items.Append(",\"length\":").Append(JsonUtil.Number(item.Length));
                items.Append(",\"position\":{\"x\":").Append(JsonUtil.Number(item.Position.x));
                items.Append(",\"z\":").Append(JsonUtil.Number(item.Position.z)).Append("}}");
            }

            float average = roadSegments == 0 ? 0f : (float)totalDensity / roadSegments;
            return CommandResult.FromJson("{\"ok\":true,\"roadSegments\":" + roadSegments +
                ",\"averageDensity\":" + JsonUtil.Number(average) +
                ",\"severeSegments\":" + severeSegments +
                ",\"cityVehicles\":" + vehicles.cityVehicleCount +
                ",\"totalTrafficFlow\":" + vehicles.m_totalTrafficFlow +
                ",\"maximumTrafficFlow\":" + vehicles.m_maxTrafficFlow +
                ",\"minimumDensity\":" + minimumDensity +
                ",\"matchingSegments\":" + congested.Count +
                ",\"segments\":[" + items.ToString() + "]}");
        }

        private sealed class TrafficSegment
        {
            public readonly ushort Id;
            public readonly string Prefab;
            public readonly int Density;
            public readonly int TrafficBuffer;
            public readonly float Length;
            public readonly Vector3 Position;

            public TrafficSegment(ushort id, string prefab, int density, int trafficBuffer, float length, Vector3 position)
            {
                Id = id;
                Prefab = prefab;
                Density = density;
                TrafficBuffer = trafficBuffer;
                Length = length;
                Position = position;
            }

            public static int Compare(TrafficSegment left, TrafficSegment right)
            {
                int density = right.Density.CompareTo(left.Density);
                return density != 0 ? density : left.Id.CompareTo(right.Id);
            }
        }
    }
}

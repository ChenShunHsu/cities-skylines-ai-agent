using ColossalFramework;
using UnityEngine;

namespace SkylinesAgentBridge
{
    internal static class RoadGraphHelpers
    {
        public static void GetVehicleDirections(
            NetSegment segment,
            out bool startToEnd,
            out bool endToStart)
        {
            startToEnd = false;
            endToStart = false;
            NetInfo info = segment.Info;
            if (info == null || info.m_lanes == null)
            {
                return;
            }

            bool prefabForward = false;
            bool prefabBackward = false;
            for (int i = 0; i < info.m_lanes.Length; i++)
            {
                NetInfo.Lane lane = info.m_lanes[i];
                if ((lane.m_laneType & NetInfo.LaneType.Vehicle) == NetInfo.LaneType.None)
                {
                    continue;
                }
                if ((lane.m_finalDirection & NetInfo.Direction.Forward) != NetInfo.Direction.None)
                {
                    prefabForward = true;
                }
                if ((lane.m_finalDirection & NetInfo.Direction.Backward) != NetInfo.Direction.None)
                {
                    prefabBackward = true;
                }
            }

            bool inverted = (segment.m_flags & NetSegment.Flags.Invert) != NetSegment.Flags.None;
            startToEnd = inverted ? prefabBackward : prefabForward;
            endToStart = inverted ? prefabForward : prefabBackward;
        }

        public static bool IsCreatedRoad(NetSegment segment)
        {
            return (segment.m_flags & NetSegment.Flags.Created) != NetSegment.Flags.None &&
                segment.Info != null &&
                segment.Info.m_class != null &&
                segment.Info.m_class.m_service == ItemClass.Service.Road;
        }

        public static bool IsOutsideNode(NetManager manager, ushort nodeId)
        {
            if (nodeId == 0 || nodeId >= manager.m_nodes.m_buffer.Length)
            {
                return false;
            }
            NetNode node = manager.m_nodes.m_buffer[nodeId];
            if ((node.m_flags & NetNode.Flags.Created) == NetNode.Flags.None)
            {
                return false;
            }
            if (StringUtil.ContainsIgnoreCase(node.m_flags.ToString(), "Outside"))
            {
                return true;
            }
            Vector3 position = node.m_position;
            return Mathf.Abs(position.x) >= 8600f || Mathf.Abs(position.z) >= 8600f;
        }

        public static ushort GetSegmentId(NetNode node, int index)
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
    }
}

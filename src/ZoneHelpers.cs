namespace SkylinesAgentBridge
{
    public static class ZoneHelpers
    {
        private const float ConnectedRoadEndClearance = 64f;
        // Below 45 degrees the other leg of a two-road node recedes along the
        // block axis faster than it approaches laterally, so it continues the
        // frontage instead of acting as the perpendicular road the corner
        // clearance protects against.
        private const float MaximumContinuationDeflectionDegrees = 45f;

        public static int GetRowCount(ZoneBlock block)
        {
            int rows = block.RowCount;
            if (rows <= 0)
            {
                rows = 4;
            }
            if (rows > 8)
            {
                rows = 8;
            }
            return rows;
        }

        public static bool IsDevelopmentBlockClearOfConnectedRoadEnds(ZoneBlock block)
        {
            if (block.m_segment == 0)
            {
                return false;
            }

            NetManager manager = NetManager.instance;
            if (block.m_segment >= manager.m_segments.m_buffer.Length)
            {
                return false;
            }
            NetSegment segment = manager.m_segments.m_buffer[block.m_segment];
            if ((segment.m_flags & NetSegment.Flags.Created) == NetSegment.Flags.None)
            {
                return false;
            }

            return IsClearOfConnectedRoadEnd(manager, block.m_segment, segment.m_startNode, block.m_position) &&
                IsClearOfConnectedRoadEnd(manager, block.m_segment, segment.m_endNode, block.m_position);
        }

        public static bool HasAnyZonedCells(ZoneBlock block)
        {
            int rows = GetRowCount(block);
            for (int z = 0; z < rows; z++)
            {
                for (int x = 0; x < 4; x++)
                {
                    if (block.GetZone(x, z) != ItemClass.Zone.Unzoned)
                    {
                        return true;
                    }
                }
            }
            return false;
        }

        private static bool IsClearOfConnectedRoadEnd(
            NetManager manager,
            ushort blockSegmentId,
            ushort nodeId,
            UnityEngine.Vector3 blockPosition)
        {
            if (nodeId == 0 || nodeId >= manager.m_nodes.m_buffer.Length)
            {
                return false;
            }

            NetNode node = manager.m_nodes.m_buffer[nodeId];
            int connectedRoads = 0;
            ushort otherRoadSegmentId = 0;
            for (int i = 0; i < 8; i++)
            {
                ushort segmentId = node.GetSegment(i);
                if (segmentId == 0)
                {
                    continue;
                }
                NetSegment connected = manager.m_segments.m_buffer[segmentId];
                if ((connected.m_flags & NetSegment.Flags.Created) != NetSegment.Flags.None &&
                    connected.Info != null &&
                    connected.Info.m_class != null &&
                    connected.Info.m_class.m_service == ItemClass.Service.Road)
                {
                    connectedRoads++;
                    if (segmentId != blockSegmentId)
                    {
                        otherRoadSegmentId = segmentId;
                    }
                }
            }
            if (connectedRoads < 2)
            {
                return true;
            }
            if (connectedRoads == 2 &&
                IsGentleContinuation(manager, blockSegmentId, otherRoadSegmentId, nodeId))
            {
                return true;
            }

            UnityEngine.Vector3 delta = blockPosition - node.m_position;
            delta.y = 0f;
            return delta.sqrMagnitude >
                ConnectedRoadEndClearance * ConnectedRoadEndClearance;
        }

        public static float ContinuationDeflectionDegrees(
            NetManager manager,
            ushort firstSegmentId,
            ushort secondSegmentId,
            ushort nodeId)
        {
            if (firstSegmentId == 0 || secondSegmentId == 0 || firstSegmentId == secondSegmentId)
            {
                return 180f;
            }
            UnityEngine.Vector3 first = DirectionAwayFromNode(manager.m_segments.m_buffer[firstSegmentId], nodeId);
            UnityEngine.Vector3 second = DirectionAwayFromNode(manager.m_segments.m_buffer[secondSegmentId], nodeId);
            first.y = 0f;
            second.y = 0f;
            if (first.sqrMagnitude < 0.0001f || second.sqrMagnitude < 0.0001f)
            {
                return 180f;
            }
            // A straight continuation leaves the node in opposite directions.
            float cosine = -UnityEngine.Vector3.Dot(first.normalized, second.normalized);
            return UnityEngine.Mathf.Acos(UnityEngine.Mathf.Clamp(cosine, -1f, 1f)) * UnityEngine.Mathf.Rad2Deg;
        }

        private static bool IsGentleContinuation(
            NetManager manager,
            ushort blockSegmentId,
            ushort otherRoadSegmentId,
            ushort nodeId)
        {
            return ContinuationDeflectionDegrees(manager, blockSegmentId, otherRoadSegmentId, nodeId) <
                MaximumContinuationDeflectionDegrees;
        }

        private static UnityEngine.Vector3 DirectionAwayFromNode(NetSegment segment, ushort nodeId)
        {
            if (segment.m_startNode == nodeId)
            {
                return segment.m_startDirection;
            }
            if (segment.m_endNode == nodeId)
            {
                return segment.m_endDirection;
            }
            return UnityEngine.Vector3.zero;
        }
    }
}

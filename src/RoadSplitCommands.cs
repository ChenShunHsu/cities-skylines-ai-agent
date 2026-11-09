using System.Collections.Generic;
using System.Reflection;
using System.Text;
using ColossalFramework.Math;
using UnityEngine;

namespace SkylinesAgentBridge
{
    public static partial class RoadCommands
    {
        // PathManager.FindPathPosition only considers a segment when one of its end nodes lies within
        // maxDistance + 64m of the queried position, so buildings near the middle of a longer segment
        // cannot be found by service vehicles (ambulances, fire trucks) and are flagged RoadAccessFailed.
        // The native road tool never gets close: it cuts every drag into pieces shorter than 100m.
        internal const float MaximumRoadSegmentLength = 160f;
        // Endpoint fitting stretches a nominal 160m planner road by a fraction of a metre.
        private const float RoadSegmentLengthTolerance = 1f;
        private const float ZoneCellSize = 8f;

        // Private native helper used when a road is connected to the middle of another one. It keeps
        // the name, flags and zoning (released zone blocks are cached and restored by the new ones).
        private static readonly MethodInfo NativeSplitSegmentMethod = typeof(NetTool).GetMethod(
            "SplitSegment", BindingFlags.Static | BindingFlags.NonPublic | BindingFlags.Public);

        internal static bool IsOverlongRoadSegment(NetInfo info, float flatLength)
        {
            return info != null && info.m_class != null &&
                info.m_class.m_service == ItemClass.Service.Road &&
                flatLength > MaximumRoadSegmentLength + RoadSegmentLengthTolerance;
        }

        // As few cuts as possible: every cut re-lays the zone blocks of both pieces, and short end
        // pieces lose the end blocks that held their corner cells at the original junctions.
        internal static int SplitPieceCount(float flatLength)
        {
            return Mathf.CeilToInt(flatLength / MaximumRoadSegmentLength);
        }

        public static CommandResult SplitRoadSegment(string body)
        {
            ushort segmentId = (ushort)JsonUtil.GetNumber(body, "segmentId", 0f);
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
                return CommandResult.Fail("Only road segments can be split.");
            }
            Vector3 start = manager.m_nodes.m_buffer[segment.m_startNode].m_position;
            Vector3 end = manager.m_nodes.m_buffer[segment.m_endNode].m_position;
            float flatLength = new Vector2(end.x - start.x, end.z - start.z).magnitude;
            if (!IsOverlongRoadSegment(segment.Info, flatLength))
            {
                return CommandResult.Fail("Segment " + segmentId + " is " + JsonUtil.Number(flatLength) +
                    "m, already within the " + JsonUtil.Number(MaximumRoadSegmentLength) + "m vehicle path-lookup reach.");
            }
            if (NativeSplitSegmentMethod == null)
            {
                return CommandResult.Fail("The native segment split helper is unavailable in this game version.");
            }

            int pieces = SplitPieceCount(flatLength);
            Bezier3 curve = new Bezier3();
            curve.a = start;
            curve.d = end;
            NetSegment.CalculateMiddlePoints(
                start, segment.m_startDirection, end, segment.m_endDirection,
                (manager.m_nodes.m_buffer[segment.m_startNode].m_flags & NetNode.Flags.Middle) != NetNode.Flags.None,
                (manager.m_nodes.m_buffer[segment.m_endNode].m_flags & NetNode.Flags.Middle) != NetNode.Flags.None,
                out curve.b, out curve.c);
            List<Vector3> splitPoints = SplitPointsOnZoneGrid(curve, pieces);

            List<ushort> newNodes = new List<ushort>();
            if (!dryRun)
            {
                ushort farNode = segment.m_endNode;
                ushort current = segmentId;
                foreach (Vector3 point in splitPoints)
                {
                    object[] arguments = new object[] { current, (ushort)0, point };
                    object split = NativeSplitSegmentMethod.Invoke(null, arguments);
                    ushort node = (ushort)arguments[1];
                    if (!(split is bool) || !(bool)split || node == 0)
                    {
                        return CommandResult.FailWithRecovery(
                            "Native split of segment " + current + " failed after " + newNodes.Count + " of " +
                            splitPoints.Count + " cuts.",
                            newNodes.Count > 0,
                            false,
                            "netSegment",
                            current);
                    }
                    newNodes.Add(node);
                    current = ConnectingSegment(manager, node, farNode);
                    if (current == 0)
                    {
                        break;
                    }
                }
            }

            StringBuilder json = new StringBuilder();
            json.Append("{\"ok\":true,\"dryRun\":").Append(JsonUtil.Bool(dryRun));
            json.Append(",\"segmentId\":").Append(segmentId);
            json.Append(",\"length\":").Append(JsonUtil.Number(flatLength));
            json.Append(",\"pieces\":").Append(pieces);
            json.Append(",\"splitPoints\":[");
            for (int i = 0; i < splitPoints.Count; i++)
            {
                if (i > 0) json.Append(",");
                json.Append("{\"x\":").Append(JsonUtil.Number(splitPoints[i].x))
                    .Append(",\"z\":").Append(JsonUtil.Number(splitPoints[i].z)).Append("}");
            }
            json.Append("]");
            if (!dryRun)
            {
                json.Append(",\"newNodeIds\":[");
                for (int i = 0; i < newNodes.Count; i++)
                {
                    if (i > 0) json.Append(",");
                    json.Append(newNodes[i]);
                }
                json.Append("],\"newSegmentIds\":[");
                bool first = true;
                foreach (ushort piece in SegmentsAround(manager, newNodes))
                {
                    if (!first) json.Append(",");
                    json.Append(piece);
                    first = false;
                }
                json.Append("]");
            }
            json.Append("}");
            return CommandResult.FromJson(json.ToString());
        }

        // Each piece lays its zone cells from its own start node, so a cut that is not a whole number
        // of 8m cells from the original start shifts the next piece's grid and the buildings that
        // straddle the cut lose half their cells. Cut on cell boundaries instead.
        private static List<Vector3> SplitPointsOnZoneGrid(Bezier3 curve, int pieces)
        {
            const int Samples = 128;
            float[] distance = new float[Samples + 1];
            Vector3 previous = curve.a;
            for (int i = 1; i <= Samples; i++)
            {
                Vector3 point = curve.Position((float)i / Samples);
                distance[i] = distance[i - 1] + new Vector2(point.x - previous.x, point.z - previous.z).magnitude;
                previous = point;
            }
            int cells = Mathf.FloorToInt(distance[Samples] / ZoneCellSize + 0.1f);
            List<Vector3> points = new List<Vector3>();
            for (int piece = 1; piece < pieces; piece++)
            {
                float target = Mathf.RoundToInt((float)cells * piece / pieces) * ZoneCellSize;
                int index = 1;
                while (index < Samples && distance[index] < target)
                {
                    index++;
                }
                float span = distance[index] - distance[index - 1];
                float fraction = span <= 0f ? 0f : (target - distance[index - 1]) / span;
                points.Add(curve.Position((index - 1 + Mathf.Clamp01(fraction)) / Samples));
            }
            return points;
        }

        private static ushort ConnectingSegment(NetManager manager, ushort node, ushort otherNode)
        {
            for (int i = 0; i < 8; i++)
            {
                ushort candidate = manager.m_nodes.m_buffer[node].GetSegment(i);
                if (candidate != 0 && manager.m_segments.m_buffer[candidate].GetOtherNode(node) == otherNode)
                {
                    return candidate;
                }
            }
            return 0;
        }

        private static List<ushort> SegmentsAround(NetManager manager, List<ushort> nodes)
        {
            List<ushort> segments = new List<ushort>();
            foreach (ushort node in nodes)
            {
                for (int i = 0; i < 8; i++)
                {
                    ushort candidate = manager.m_nodes.m_buffer[node].GetSegment(i);
                    if (candidate != 0 && !segments.Contains(candidate))
                    {
                        segments.Add(candidate);
                    }
                }
            }
            return segments;
        }
    }
}

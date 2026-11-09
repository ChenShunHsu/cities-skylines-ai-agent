using System.Globalization;
using System.Text;
using ColossalFramework.Math;
using UnityEngine;

namespace SkylinesAgentBridge
{
    public static partial class RoadCommands
    {
        private static Bezier3 PredictedControlCurve(Vector3 start, Vector3 end,
            Vector3 startDirection, Vector3 endDirection)
        {
            Bezier3 curve = new Bezier3();
            curve.a = start;
            curve.d = end;
            NetSegment.CalculateMiddlePoints(start, startDirection, end, endDirection,
                true, true, out curve.b, out curve.c);
            return curve;
        }

        private static string CapturePreviewGeometry(Vector3 start, Vector3 end,
            Vector3 startDirection, Vector3 endDirection)
        {
            Bezier3 curve = PredictedControlCurve(start, end, startDirection, endDirection);
            StringBuilder json = new StringBuilder();
            json.Append("{\"source\":\"predictedControlBezier\",\"clearanceModel\":\"terrainSeededControlCurve\",\"predictsSettledSurface\":false,\"curve\":");
            json.Append(CurveEvidence(curve)).Append(",\"samples\":[");
            int count = GeometrySampleCount(curve);
            for (int index = 0; index < count; index++)
            {
                if (index > 0) json.Append(',');
                float t = (float)index / (count - 1);
                Vector3 point = curve.Position(t);
                float terrain = TerrainManager.instance.SampleRawHeightSmoothWithWater(point, false, 0f);
                json.Append("{\"t\":").Append(Precise(t));
                json.Append(",\"position\":").Append(PointEvidence(point));
                json.Append(",\"terrainY\":").Append(Precise(terrain));
                json.Append(",\"predictedClearance\":").Append(Precise(point.y - terrain));
                json.Append('}');
            }
            return json.Append("]}").ToString();
        }

        private static int GeometrySampleCount(Bezier3 curve)
        {
            float length = Vector3.Distance(curve.a, curve.b) +
                Vector3.Distance(curve.b, curve.c) + Vector3.Distance(curve.c, curve.d);
            return Mathf.Clamp(Mathf.CeilToInt(length / 8f) + 1, 9, 129);
        }

        private static string Precise(float value)
        {
            return float.IsNaN(value) || float.IsInfinity(value)
                ? "null" : value.ToString("R", CultureInfo.InvariantCulture);
        }

        private static string PointEvidence(Vector3 point)
        {
            return "{\"x\":" + Precise(point.x) + ",\"y\":" + Precise(point.y) +
                ",\"z\":" + Precise(point.z) + "}";
        }

        private static string CurveEvidence(Bezier3 curve)
        {
            return "{\"a\":" + PointEvidence(curve.a) + ",\"b\":" + PointEvidence(curve.b) +
                ",\"c\":" + PointEvidence(curve.c) + ",\"d\":" + PointEvidence(curve.d) + "}";
        }

        // Capture BEFORE releasing the segment. Native GenerateBezier already
        // uses CalculateCorner and NetInfo.m_surfaceLevel. Keep that native data
        // separate from the legacy node-inset clearance proxy until calibrated.
        internal static string CaptureGeometryEvidence(NetManager manager, ushort segmentId)
        {
            if (segmentId == 0 || segmentId >= manager.m_segments.m_buffer.Length)
                return "{\"created\":false}";
            NetSegment segment = manager.m_segments.m_buffer[segmentId];
            if ((segment.m_flags & NetSegment.Flags.Created) == 0 || segment.Info == null ||
                segment.m_startNode == 0 || segment.m_endNode == 0)
                return "{\"created\":false}";
            Bezier3 left;
            Bezier3 right;
            Bezier3 center = segment.GenerateBezier(segmentId, segment.m_startNode, out left, out right);
            NetNode start = manager.m_nodes.m_buffer[segment.m_startNode];
            NetNode end = manager.m_nodes.m_buffer[segment.m_endNode];
            float proxyOffset = GetRoadSurfaceOffset(manager, segmentId, segment);
            StringBuilder json = new StringBuilder();
            json.Append("{\"created\":true,\"schemaVersion\":1,\"source\":\"nativeGenerateBezierWithSurfaceLevel\"");
            json.Append(",\"clearanceGateModel\":\"legacyNodeInsetProxyNotCalibratedSurface\"");
            json.Append(",\"segmentId\":").Append(segmentId);
            json.Append(",\"flags\":\"").Append(JsonUtil.Escape(segment.m_flags.ToString())).Append("\"");
            json.Append(",\"startNodeId\":").Append(segment.m_startNode);
            json.Append(",\"endNodeId\":").Append(segment.m_endNode);
            json.Append(",\"startFlags\":\"").Append(JsonUtil.Escape(start.m_flags.ToString())).Append("\"");
            json.Append(",\"endFlags\":\"").Append(JsonUtil.Escape(end.m_flags.ToString())).Append("\"");
            json.Append(",\"startNode\":").Append(PointEvidence(start.m_position));
            json.Append(",\"endNode\":").Append(PointEvidence(end.m_position));
            json.Append(",\"nativeSurfaceLevel\":").Append(Precise(segment.Info.m_surfaceLevel));
            json.Append(",\"legacyProxyOffset\":").Append(Precise(proxyOffset));
            json.Append(",\"center\":").Append(CurveEvidence(center));
            json.Append(",\"left\":").Append(CurveEvidence(left));
            json.Append(",\"right\":").Append(CurveEvidence(right));
            json.Append(",\"samples\":[");
            int count = GeometrySampleCount(center);
            for (int index = 0; index < count; index++)
            {
                if (index > 0) json.Append(',');
                float t = (float)index / (count - 1);
                Vector3 point = center.Position(t);
                float terrain = TerrainManager.instance.SampleRawHeightSmoothWithWater(point, false, 0f);
                json.Append("{\"t\":").Append(Precise(t));
                json.Append(",\"position\":").Append(PointEvidence(point));
                json.Append(",\"terrainY\":").Append(Precise(terrain));
                json.Append(",\"nativeClearance\":").Append(Precise(point.y - terrain));
                json.Append(",\"legacyProxyClearance\":").Append(Precise(point.y + proxyOffset - terrain));
                json.Append('}');
            }
            json.Append("]}");
            return json.ToString();
        }

        private static CommandResult WithGeometryEvidence(CommandResult result, string immediate, string settled)
        {
            result.Json = result.Json.Substring(0, result.Json.Length - 1) +
                ",\"immediateGeometryEvidence\":" + immediate +
                ",\"settledGeometryEvidence\":" + settled + "}";
            return result;
        }
    }
}

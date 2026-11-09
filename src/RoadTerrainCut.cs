using ColossalFramework.Math;
using UnityEngine;

namespace SkylinesAgentBridge
{
    // A ground road shapes the terrain it is built through, as it does for a player dragging one:
    // the game lowers the drawn (detail) terrain to the road and raises it under a fill, while the
    // raw heightmap the clearance gates sample keeps the bump. Measured live on a natural map: roads
    // 0.49m and 1.32m below the raw terrain left the final surface within 0.06m of the road and formed
    // normal zone blocks. The clearance gates therefore refused most roads a player would simply drag,
    // most of all from highway stubs. A road may instead be left to the game: built wherever the
    // native tool builds it, and accepted after settlement unless the drawn terrain covers it.
    internal static class RoadTerrainCut
    {
        // Reported only: the deepest cut measured live before roads were left to the game.
        internal const float MaximumCut = 1.3f;
        // The drawn terrain beside a ground road sits at its sidewalks, -m_surfaceLevel (0.3m for a
        // basic road) above the curve the road is generated on; measured live, the carved surface
        // stayed within 0.06m of that level along cut and ordinary roads alike.
        internal const float FinalSurfaceTolerance = 0.15f;

        // Deepest point below raw terrain and highest above it along a curve; endpoints on
        // existing nodes keep their heights and are skipped.
        internal static void Measure(Bezier3 curve, bool skipStart, bool skipEnd,
            out float deepestCut, out float highestFill)
        {
            deepestCut = 0f;
            highestFill = 0f;
            TerrainManager terrain = TerrainManager.instance;
            int count = Mathf.Max(3, Mathf.CeilToInt(Vector3.Distance(curve.a, curve.d) / 4f) + 1);
            for (int i = 0; i < count; i++)
            {
                if ((i == 0 && skipStart) || (i == count - 1 && skipEnd))
                {
                    continue;
                }
                Vector3 point = curve.Position((float)i / (count - 1));
                float clearance = point.y - terrain.SampleRawHeightSmoothWithWater(point, false, 0f);
                deepestCut = Mathf.Max(deepestCut, -clearance);
                highestFill = Mathf.Max(highestFill, clearance);
            }
        }

        // Beside a node of the map's original network (a highway stub) the ground follows that network,
        // not the new road: measured where town roads join a highway, the drawn terrain lay up to 1m
        // below the road for the first ~24m. There a road is only checked for not being buried.
        internal const float OriginalTransition = 32f;

        private static bool TouchesOriginalNetwork(NetManager manager, ushort nodeId)
        {
            if (nodeId == 0)
            {
                return false;
            }
            NetNode node = manager.m_nodes.m_buffer[nodeId];
            for (int index = 0; index < 8; index++)
            {
                ushort other = node.GetSegment(index);
                if (other != 0 &&
                    (manager.m_segments.m_buffer[other].m_flags & NetSegment.Flags.Original) != NetSegment.Flags.None)
                {
                    return true;
                }
            }
            return false;
        }

        private static bool Near(Vector3 point, Vector3 node)
        {
            return !float.IsNaN(node.x) &&
                new Vector2(point.x - node.x, point.z - node.z).magnitude <= OriginalTransition;
        }

        // How far the drawn terrain rises above a road's sidewalk level anywhere along it: a road is
        // only really under the ground when this is positive. A road below the raw heightmap is not:
        // measured on an original-map highway in a cut, the drawn terrain lay up to 1m beneath it.
        internal static float BuriedDepth(NetManager manager, ushort segmentId)
        {
            NetSegment segment = manager.m_segments.m_buffer[segmentId];
            if ((segment.m_flags & NetSegment.Flags.Created) == NetSegment.Flags.None || segment.Info == null)
            {
                return 0f;
            }
            Bezier3 curve = segment.GenerateBezier(segmentId, segment.m_startNode);
            TerrainManager terrain = TerrainManager.instance;
            float sidewalk = -segment.Info.m_surfaceLevel;
            int count = Mathf.Max(3, Mathf.CeilToInt(Vector3.Distance(curve.a, curve.d) / 4f) + 1);
            float buried = float.MinValue;
            for (int i = 0; i < count; i++)
            {
                Vector3 point = curve.Position((float)i / (count - 1));
                buried = Mathf.Max(buried, terrain.SampleDetailHeightSmooth(point) - point.y - sidewalk);
            }
            return buried;
        }

        // The settled road against the terrain the game draws after shaping it: accepted unless that
        // terrain covers the road. How far it lies from the road elsewhere and how deep the cut runs
        // are reported, not judged: a player's road is not refused for either.
        internal static bool InspectFinalSurface(NetManager manager, ushort segmentId,
            out int sampleCount, out float maxSurfaceDelta, out float deepestCut, out float buried)
        {
            sampleCount = 0;
            maxSurfaceDelta = 0f;
            deepestCut = 0f;
            buried = 0f;
            NetSegment segment = manager.m_segments.m_buffer[segmentId];
            if ((segment.m_flags & NetSegment.Flags.Created) == NetSegment.Flags.None || segment.Info == null)
            {
                return false;
            }
            Bezier3 curve = segment.GenerateBezier(segmentId, segment.m_startNode);
            TerrainManager terrain = TerrainManager.instance;
            float sidewalk = -segment.Info.m_surfaceLevel;
            sampleCount = Mathf.Max(3, Mathf.CeilToInt(Vector3.Distance(curve.a, curve.d) / 4f) + 1);
            Vector3 startOriginal = TouchesOriginalNetwork(manager, segment.m_startNode)
                ? manager.m_nodes.m_buffer[segment.m_startNode].m_position : new Vector3(float.NaN, 0f, 0f);
            Vector3 endOriginal = TouchesOriginalNetwork(manager, segment.m_endNode)
                ? manager.m_nodes.m_buffer[segment.m_endNode].m_position : new Vector3(float.NaN, 0f, 0f);
            for (int i = 0; i < sampleCount; i++)
            {
                Vector3 point = curve.Position((float)i / (sampleCount - 1));
                float delta = terrain.SampleDetailHeightSmooth(point) - point.y - sidewalk;
                buried = Mathf.Max(buried, delta);
                if (Near(point, startOriginal) || Near(point, endOriginal))
                {
                    continue;  // the map's own network shapes the ground here: only a buried road counts
                }
                maxSurfaceDelta = Mathf.Max(maxSurfaceDelta, Mathf.Abs(delta));
                deepestCut = Mathf.Max(deepestCut,
                    terrain.SampleRawHeightSmoothWithWater(point, false, 0f) - point.y - sidewalk);
            }
            return buried <= FinalSurfaceTolerance;
        }
    }
}

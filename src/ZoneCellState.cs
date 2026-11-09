using System.Text;
using ColossalFramework.Math;
using UnityEngine;

namespace SkylinesAgentBridge
{
    public static class ZoneCellState
    {
        private static Vector2 XZ(ZoneBlock block, double x, double z)
        {
            double[] p = ZoneCellLayout.Point(block.m_position.x, block.m_position.z, block.m_angle, x, z);
            return new Vector2((float)p[0], (float)p[1]);
        }

        private static string Point(ZoneBlock block, double x, double z)
        {
            double[] p = ZoneCellLayout.Point(block.m_position.x, block.m_position.z, block.m_angle, x, z);
            float terrain = TerrainManager.instance.SampleRawHeightSmoothWithWater(
                new Vector3((float)p[0], block.m_position.y, (float)p[1]), false, 0f);
            return "{\"x\":" + JsonUtil.Number((float)p[0]) + ",\"z\":" +
                JsonUtil.Number((float)p[1]) + ",\"terrainY\":" + JsonUtil.Number(terrain) + "}";
        }

        public static CommandResult Inspect(int blockId)
        {
            ZoneManager zones = ZoneManager.instance;
            if (blockId <= 0 || blockId >= zones.m_blocks.m_buffer.Length)
                return CommandResult.FromJson("{\"ok\":false,\"error\":\"Invalid blockId\"}");
            ZoneBlock block = zones.m_blocks.m_buffer[blockId];
            if ((block.m_flags & ZoneBlock.FLAG_CREATED) == 0 || block.RowCount < 1 || block.RowCount > 8)
                return CommandResult.FromJson("{\"ok\":false,\"error\":\"Block missing or unsupported row count\"}");
            int rows = block.RowCount;
            StringBuilder json = new StringBuilder();
            json.Append("{\"ok\":true,\"planningOnly\":true,\"mutationAuthorized\":false,\"blockId\":").Append(blockId);
            json.Append(",\"roadSegmentId\":").Append(block.m_segment);
            json.Append(",\"rows\":").Append(rows);
            json.Append(",\"maskRowStride\":8,\"cellSize\":8,\"validCells\":").Append(ZoneCellLayout.Count(block.m_valid, rows));
            json.Append(",\"unsharedValidCells\":").Append(ZoneCellLayout.Count(block.m_valid & ~block.m_shared, rows));
            json.Append(",\"vacantUnsharedCells\":").Append(ZoneCellLayout.Count(block.m_valid & ~block.m_shared & ~(block.m_occupied1 | block.m_occupied2), rows));
            json.Append(",\"validMask\":\"").Append(block.m_valid.ToString("X16")).Append("\"");
            json.Append(",\"sharedMask\":\"").Append(block.m_shared.ToString("X16")).Append("\"");
            json.Append(",\"junctionClearanceSafe\":").Append(JsonUtil.Bool(ZoneHelpers.IsDevelopmentBlockClearOfConnectedRoadEnds(block)));
            json.Append(",\"buildingFootprintVerified\":false,\"verticalClearanceVerified\":false,\"cells\":[");
            for (int z = 0; z < rows; z++)
                for (int x = 0; x < 4; x++)
                {
                    if (x != 0 || z != 0) json.Append(',');
                    ulong bit = ZoneCellLayout.Bit(x, z);
                    json.Append("{\"x\":").Append(x).Append(",\"z\":").Append(z);
                    json.Append(",\"valid\":").Append(JsonUtil.Bool((block.m_valid & bit) != 0));
                    json.Append(",\"shared\":").Append(JsonUtil.Bool((block.m_shared & bit) != 0));
                    json.Append(",\"occupied1\":").Append(JsonUtil.Bool(block.IsOccupied1(x, z)));
                    json.Append(",\"occupied2\":").Append(JsonUtil.Bool(block.IsOccupied2(x, z)));
                    json.Append(",\"zone\":\"").Append(block.GetZone(x, z).ToString()).Append("\"");
                    json.Append(",\"center\":").Append(Point(block, x + 0.5, z + 0.5));
                    json.Append(",\"corners\":[").Append(Point(block, x, z)).Append(',').Append(Point(block, x + 1, z));
                    json.Append(',').Append(Point(block, x + 1, z + 1)).Append(',').Append(Point(block, x, z + 1)).Append("]}");
                }
            json.Append("],\"nearbyRoads\":[");
            // A conservative control-point bounding box includes every curve near
            // this at-most 32x64m block. This selection is not a collision verdict.
            NetManager nets = NetManager.instance;
            int found = 0, emitted = 0;
            for (int id = 1; id < nets.m_segments.m_buffer.Length; id++)
            {
                NetSegment segment = nets.m_segments.m_buffer[id];
                NetInfo info = segment.Info;
                if ((segment.m_flags & NetSegment.Flags.Created) == 0 || info == null || info.m_class == null ||
                    info.m_class.m_service != ItemClass.Service.Road) continue;
                // Match the native collision broad phase, which differs from
                // the renderer's clipped GenerateBezier at bends/junctions.
                Bezier3 curve = new Bezier3();
                curve.a = nets.m_nodes.m_buffer[segment.m_startNode].m_position;
                curve.d = nets.m_nodes.m_buffer[segment.m_endNode].m_position;
                NetSegment.CalculateMiddlePoints(curve.a, segment.m_startDirection,
                    curve.d, segment.m_endDirection, true, true, out curve.b, out curve.c);
                float width = info.m_netAI.GetCollisionHalfWidth();
                float reach = 100f + Mathf.Max(info.m_halfWidth, width);
                float minX = Mathf.Min(Mathf.Min(curve.a.x, curve.b.x), Mathf.Min(curve.c.x, curve.d.x));
                float maxX = Mathf.Max(Mathf.Max(curve.a.x, curve.b.x), Mathf.Max(curve.c.x, curve.d.x));
                float minZ = Mathf.Min(Mathf.Min(curve.a.z, curve.b.z), Mathf.Min(curve.c.z, curve.d.z));
                float maxZ = Mathf.Max(Mathf.Max(curve.a.z, curve.b.z), Mathf.Max(curve.c.z, curve.d.z));
                if (block.m_position.x < minX - reach || block.m_position.x > maxX + reach ||
                    block.m_position.z < minZ - reach || block.m_position.z > maxZ + reach) continue;
                found++;
                if (emitted >= 64) continue;
                if (emitted++ > 0) json.Append(',');
                json.Append("{\"segmentId\":").Append(id).Append(",\"halfWidth\":").Append(JsonUtil.Number(info.m_halfWidth));
                json.Append(",\"collisionHalfWidth\":").Append(JsonUtil.Number(width));
                json.Append(",\"canCollide\":").Append(JsonUtil.Bool(info.m_canCollide));
                json.Append(",\"nativeCollisionType\":\"").Append(info.m_netAI.GetCollisionType().ToString()).Append("\"");
                json.Append(",\"nativeCollisionXZCells\":[");
                bool firstCollision = true;
                for (int z = 0; z < rows; z++)
                    for (int x = 0; x < 4; x++)
                    {
                        Quad2 quad = new Quad2();
                        quad.a = XZ(block, x, z); quad.b = XZ(block, x + 1, z);
                        quad.c = XZ(block, x + 1, z + 1); quad.d = XZ(block, x, z + 1);
                        // Same native collision type and unbounded height deliberately
                        // ask only about XZ. Never interpret a miss/hit as 3D clearance.
                        if (!segment.OverlapQuad((ushort)id, quad, float.MinValue, float.MaxValue,
                            info.m_netAI.GetCollisionType())) continue;
                        if (!firstCollision) json.Append(',');
                        firstCollision = false;
                        json.Append("{\"x\":").Append(x).Append(",\"z\":").Append(z).Append('}');
                    }
                json.Append(']');
                json.Append(",\"geometry\":").Append(RoadCommands.CaptureGeometryEvidence(nets, (ushort)id)).Append('}');
            }
            json.Append("],\"nearbyRoadCount\":").Append(found);
            json.Append(",\"nearbyRoadsTruncated\":").Append(JsonUtil.Bool(found > emitted));
            json.Append(",\"nativeCollisionXZMethod\":\"NetSegment.OverlapQuad; same collision type, unbounded height; includes owning road; full 8m cell square\"");
            json.Append(",\"limits\":\"Cell XZ footprints and sampled terrain are not growable foundation bounds; native collision XZ and rendered road curves do not prove vertical clearance. Existing zoning gates remain authoritative.\"}");
            return CommandResult.FromJson(json.ToString());
        }
    }
}

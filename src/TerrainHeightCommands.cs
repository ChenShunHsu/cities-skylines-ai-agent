using System;
using System.Globalization;
using System.Text;
using UnityEngine;

namespace SkylinesAgentBridge
{
    // Raw terrain is the heightmap before buildings and networks reshape it; detail terrain is the
    // surface the game draws after they do. Comparing both under a road shows whether the road
    // carved the ground it was built through.
    public static class TerrainHeightCommands
    {
        private const int MaximumPoints = 256;

        public static CommandResult Sample(string points)
        {
            if (string.IsNullOrEmpty(points)) return CommandResult.Fail("points is required: x,z;x,z;...");
            string[] pairs = points.Split(';');
            if (pairs.Length > MaximumPoints) return CommandResult.Fail("At most " + MaximumPoints + " points.");
            TerrainManager terrain = TerrainManager.instance;
            StringBuilder json = new StringBuilder("{\"ok\":true,\"samples\":[");
            for (int i = 0; i < pairs.Length; i++)
            {
                string[] parts = pairs[i].Split(',');
                float x, z;
                if (parts.Length != 2 ||
                    !float.TryParse(parts[0], NumberStyles.Float, CultureInfo.InvariantCulture, out x) ||
                    !float.TryParse(parts[1], NumberStyles.Float, CultureInfo.InvariantCulture, out z))
                {
                    return CommandResult.Fail("Point " + i + " is not x,z.");
                }
                Vector3 point = new Vector3(x, 0f, z);
                if (i > 0) json.Append(',');
                json.Append("{\"x\":").Append(JsonUtil.Number(x))
                    .Append(",\"z\":").Append(JsonUtil.Number(z))
                    .Append(",\"raw\":").Append(JsonUtil.Number(terrain.SampleRawHeightSmoothWithWater(point, false, 0f)))
                    .Append(",\"detail\":").Append(JsonUtil.Number(terrain.SampleDetailHeightSmooth(point)))
                    .Append('}');
            }
            json.Append("]}");
            return CommandResult.FromJson(json.ToString());
        }
    }
}

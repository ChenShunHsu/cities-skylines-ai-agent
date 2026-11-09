using System.Collections.Generic;
using System.Text;
using ColossalFramework;
using ColossalFramework.Math;
using UnityEngine;

namespace SkylinesAgentBridge
{
    // Native Building.CheckZoning decides whether a growable keeps standing: every footprint cell
    // must be covered by a valid, unshared zone-block cell of the building's zone, otherwise the
    // next simulation pass flags it Demolishing. Rebuilding a road segment (upgrade) recreates its
    // zone blocks from the zone cache, and cells near a changed node can come back unzoned.
    public static class BuildingZoningSupport
    {
        public static CommandResult Inspect(float x, float z, float radius, int limit)
        {
            BuildingManager buildings = Singleton<BuildingManager>.instance;
            Vector2 center = new Vector2(x, z);
            int checkedCount = 0;
            int failingCount = 0;
            StringBuilder failing = new StringBuilder("[");
            for (int i = 1; i < buildings.m_buildings.m_buffer.Length; i++)
            {
                Building building = buildings.m_buildings.m_buffer[i];
                if (!IsLiveGrowable(building)) continue;
                if (Vector2.Distance(center, VectorUtils.XZ(building.m_position)) > radius) continue;
                checkedCount++;
                if (IsSupported(building)) continue;
                failingCount++;
                if (failingCount > limit) continue;
                int repairable;
                int covered;
                CountFootprintCells(building, false, out repairable, out covered);
                if (failingCount > 1) failing.Append(',');
                failing.Append("{\"id\":").Append(i)
                    .Append(",\"prefab\":\"").Append(JsonUtil.Escape(building.Info.name)).Append('"')
                    .Append(",\"zone\":\"").Append(building.Info.m_class.GetZone()).Append('"')
                    .Append(",\"position\":{\"x\":").Append(JsonUtil.Number(building.m_position.x))
                    .Append(",\"z\":").Append(JsonUtil.Number(building.m_position.z)).Append('}')
                    .Append(",\"footprintCells\":").Append(building.Width * building.Length)
                    .Append(",\"repairableCells\":").Append(repairable)
                    .Append(",\"supportedCells\":").Append(covered).Append('}');
            }
            failing.Append(']');
            return CommandResult.FromJson("{\"ok\":true,\"source\":\"nativeBuildingCheckZoning\"" +
                ",\"center\":{\"x\":" + JsonUtil.Number(x) + ",\"z\":" + JsonUtil.Number(z) + "}" +
                ",\"radius\":" + JsonUtil.Number(radius) +
                ",\"checkedGrowables\":" + checkedCount +
                ",\"failingCount\":" + failingCount +
                ",\"safe\":" + JsonUtil.Bool(failingCount == 0) +
                ",\"failing\":" + failing + "}");
        }

        // Re-zone only cells under the building's own footprint that are valid, unshared and
        // currently unzoned, to the building's zone: this restores the zoning it grew on.
        public static CommandResult Repair(string body)
        {
            ushort id = (ushort)JsonUtil.GetNumber(body, "buildingId", 0f);
            bool dryRun = JsonUtil.GetBool(body, "dryRun", false);
            bool confirm = JsonUtil.GetBool(body, "confirm", false);
            BuildingManager buildings = Singleton<BuildingManager>.instance;
            if (id == 0 || id >= buildings.m_buildings.m_buffer.Length) return CommandResult.Fail("buildingId is required.");
            Building building = buildings.m_buildings.m_buffer[id];
            if (!IsLiveGrowable(building)) return CommandResult.Fail("Building " + id + " is not a live growable.");
            bool supportedBefore = IsSupported(building);
            int repairable;
            int covered;
            CountFootprintCells(building, false, out repairable, out covered);
            if (dryRun || supportedBefore || repairable == 0)
            {
                return CommandResult.FromJson("{\"ok\":true,\"dryRun\":" + JsonUtil.Bool(dryRun) +
                    ",\"buildingId\":" + id + ",\"supportedBefore\":" + JsonUtil.Bool(supportedBefore) +
                    ",\"repairableCells\":" + repairable + ",\"changedCells\":0" +
                    ",\"supportedAfter\":" + JsonUtil.Bool(supportedBefore) + "}");
            }
            if (!confirm) return CommandResult.Fail("Set confirm=true after creating and verifying a checkpoint.");

            int changed;
            CountFootprintCells(building, true, out changed, out covered);
            bool supportedAfter = IsSupported(buildings.m_buildings.m_buffer[id]);
            return CommandResult.FromJson("{\"ok\":" + JsonUtil.Bool(supportedAfter) + ",\"dryRun\":false" +
                ",\"buildingId\":" + id + ",\"supportedBefore\":false" +
                ",\"repairableCells\":" + repairable + ",\"changedCells\":" + changed +
                ",\"supportedAfter\":" + JsonUtil.Bool(supportedAfter) +
                (supportedAfter ? "" : ",\"error\":\"Footprint still lacks valid zoned cells; restore the checkpoint.\"") + "}");
        }

        private static bool IsLiveGrowable(Building building)
        {
            return (building.m_flags & (Building.Flags.Created | Building.Flags.Deleted)) == Building.Flags.Created &&
                building.Info != null && building.Info.m_class != null &&
                building.Info.m_buildingAI is PrivateBuildingAI;
        }

        private static bool IsSupported(Building building)
        {
            // CheckZoning also refreshes the HighDensity flag, so call it on this copy only
            return building.CheckZoning(building.Info.m_class.GetZone(), building.Info.m_class.GetSecondaryZone(), true);
        }

        // Walks zone-block cells under the footprint with the same geometry as the native
        // Building.CheckZoning. "repairable" counts valid, unshared, unzoned cells (and zones them
        // when apply is set); "covered" counts footprint cells already holding the building's zone.
        private static void CountFootprintCells(Building building, bool apply, out int repairable, out int covered)
        {
            repairable = 0;
            covered = 0;
            ItemClass.Zone zone = building.Info.m_class.GetZone();
            int width = building.Width;
            int length = building.Length;
            Vector3 axisX = new Vector3(Mathf.Cos(building.m_angle), 0f, Mathf.Sin(building.m_angle));
            Vector3 axisZ = new Vector3(axisX.z, 0f, -axisX.x);
            Vector3 halfX = axisX * (width * 4f);
            Vector3 halfZ = axisZ * (length * 4f);
            Vector3 min = Vector3.Min(Vector3.Min(building.m_position - halfX - halfZ, building.m_position + halfX - halfZ),
                Vector3.Min(building.m_position + halfX + halfZ, building.m_position - halfX + halfZ));
            Vector3 max = Vector3.Max(Vector3.Max(building.m_position - halfX - halfZ, building.m_position + halfX - halfZ),
                Vector3.Max(building.m_position + halfX + halfZ, building.m_position - halfX + halfZ));
            int minX = Mathf.Max((int)((min.x - 46f) / 64f + 75f), 0);
            int minZ = Mathf.Max((int)((min.z - 46f) / 64f + 75f), 0);
            int maxX = Mathf.Min((int)((max.x + 46f) / 64f + 75f), 149);
            int maxZ = Mathf.Min((int)((max.z + 46f) / 64f + 75f), 149);
            ZoneManager zones = Singleton<ZoneManager>.instance;
            HashSet<ushort> seen = new HashSet<ushort>();
            HashSet<int> coveredCells = new HashSet<int>();
            Vector3 cellX = axisX * 8f;
            Vector3 cellZ = axisZ * 8f;
            for (int gz = minZ; gz <= maxZ; gz++)
            {
                for (int gx = minX; gx <= maxX; gx++)
                {
                    ushort blockId = zones.m_zoneGrid[gz * 150 + gx];
                    int guard = 0;
                    while (blockId != 0 && guard++ < 49152)
                    {
                        if (seen.Add(blockId))
                        {
                            Visit(zones, blockId, building, zone, width, length, cellX, cellZ, apply, ref repairable, coveredCells);
                        }
                        blockId = zones.m_blocks.m_buffer[blockId].m_nextGridBlock;
                    }
                }
            }
            covered = coveredCells.Count;
        }

        private static void Visit(ZoneManager zones, ushort blockId, Building building, ItemClass.Zone zone,
            int width, int length, Vector3 cellX, Vector3 cellZ, bool apply, ref int repairable, HashSet<int> coveredCells)
        {
            ZoneBlock block = zones.m_blocks.m_buffer[blockId];
            if ((block.m_flags & ZoneBlock.FLAG_CREATED) == 0) return;
            Vector3 rowAxis = new Vector3(Mathf.Cos(block.m_angle), 0f, Mathf.Sin(block.m_angle)) * 8f;
            Vector3 columnAxis = new Vector3(rowAxis.z, 0f, -rowAxis.x);
            Vector3 origin = block.m_position - building.m_position + cellX * (width * 0.5f - 0.5f) + cellZ * (length * 0.5f - 0.5f);
            bool changed = false;
            for (int row = 0; row < block.RowCount; row++)
            {
                for (int column = 0; column < 4; column++)
                {
                    if ((block.m_valid & ~block.m_shared & (1UL << ((row << 3) | column))) == 0) continue;
                    Vector3 offset = origin + (column - 3.5f) * rowAxis + (row - 3.5f) * columnAxis;
                    int fx = Mathf.RoundToInt((cellX.x * offset.x + cellX.z * offset.z) / 64f);
                    int fz = Mathf.RoundToInt((cellZ.x * offset.x + cellZ.z * offset.z) / 64f);
                    if (fx < 0 || fz < 0 || fx >= width || fz >= length) continue;
                    ItemClass.Zone current = block.GetZone(column, row);
                    if (current == zone)
                    {
                        coveredCells.Add(fz * 8 + fx);
                    }
                    else if (current == ItemClass.Zone.Unzoned)
                    {
                        repairable++;
                        if (apply && zones.m_blocks.m_buffer[blockId].SetZone(column, row, zone)) changed = true;
                    }
                }
            }
            if (changed)
            {
                zones.m_blocks.m_buffer[blockId].RefreshZoning(blockId);
                zones.UpdateBlock(blockId);
            }
        }
    }
}

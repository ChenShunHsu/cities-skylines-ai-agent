using System.Collections.Generic;
using System.Text;
using ColossalFramework;
using UnityEngine;

namespace SkylinesAgentBridge
{
    // Buildings the game simulates but cannot find on its building grid. Growth checks a lot by
    // the grid, so a growable missing from it is overgrown again and again: live, 863 growables
    // stood on 16 lots, up to 136 on one, each simulated (goods, workers, fires) as its own.
    internal static class BuildingGridCheck
    {
        public static CommandResult BuildJson(int limit)
        {
            BuildingManager manager = Singleton<BuildingManager>.instance;
            Building[] buffer = manager.m_buildings.m_buffer;
            int resolution = BuildingManager.BUILDINGGRID_RESOLUTION;
            float cellSize = BuildingManager.BUILDINGGRID_CELL_SIZE;

            HashSet<ushort> onGrid = new HashSet<ushort>();
            for (int cell = 0; cell < manager.m_buildingGrid.Length; cell++)
            {
                ushort id = manager.m_buildingGrid[cell];
                int guard = 0;
                while (id != 0 && guard++ < 49152)
                {
                    onGrid.Add(id);
                    id = buffer[id].m_nextGridBuilding;
                }
            }

            int created = 0;
            int missing = 0;
            StringBuilder items = new StringBuilder();
            int emitted = 0;
            for (ushort i = 1; i < buffer.Length; i++)
            {
                Building building = buffer[i];
                if ((building.m_flags & Building.Flags.Created) == Building.Flags.None) continue;
                if ((building.m_flags & Building.Flags.Deleted) != Building.Flags.None) continue;
                created++;
                if (onGrid.Contains(i)) continue;
                missing++;
                if (emitted >= limit) continue;
                BuildingInfo info = building.Info;
                int x = Mathf.Clamp((int)(building.m_position.x / cellSize + resolution / 2f), 0, resolution - 1);
                int z = Mathf.Clamp((int)(building.m_position.z / cellSize + resolution / 2f), 0, resolution - 1);
                if (emitted > 0) items.Append(",");
                items.Append("{\"id\":").Append(i);
                items.Append(",\"prefab\":\"").Append(JsonUtil.Escape(info != null ? info.name : "")).Append("\"");
                items.Append(",\"service\":\"").Append(info != null && info.m_class != null ? info.m_class.m_service.ToString() : "").Append("\"");
                items.Append(",\"flags\":\"").Append(JsonUtil.Escape(building.m_flags.ToString())).Append("\"");
                items.Append(",\"gridCell\":").Append(z * resolution + x);
                items.Append(",\"position\":{\"x\":").Append(JsonUtil.Number(building.m_position.x));
                items.Append(",\"z\":").Append(JsonUtil.Number(building.m_position.z)).Append("}}");
                emitted++;
            }

            StringBuilder stale = new StringBuilder();
            int staleCount = 0;
            foreach (ushort id in onGrid)
            {
                Building.Flags flags = buffer[id].m_flags;
                if ((flags & Building.Flags.Created) != Building.Flags.None && (flags & Building.Flags.Deleted) == Building.Flags.None) continue;
                if (staleCount < limit)
                {
                    if (staleCount > 0) stale.Append(",");
                    stale.Append("{\"id\":").Append(id).Append(",\"flags\":\"").Append(JsonUtil.Escape(flags.ToString())).Append("\"}");
                }
                staleCount++;
            }

            return CommandResult.FromJson("{\"ok\":true,\"created\":" + created + ",\"onGrid\":" + onGrid.Count +
                ",\"staleOnGrid\":" + staleCount + ",\"stale\":[" + stale.ToString() + "]" +
                ",\"missing\":" + missing + ",\"returned\":" + emitted + ",\"buildings\":[" + items.ToString() + "]}");
        }
    }
}

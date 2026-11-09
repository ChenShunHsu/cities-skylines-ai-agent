using System;
using System.Reflection;
using System.Text;
using ColossalFramework;

namespace SkylinesAgentBridge
{
    // Finishes the release of buildings left half released: flagged Created and Deleted, still on
    // the building grid, their id never freed. BuildingManager.ReleaseBuildingImplementation sets
    // Deleted first and returns at once when it is set, so a release that threw midway (Bridge
    // commands then ran beside the simulation thread) can never be run again; the rest of it is
    // done here: citizen units, paths, flags, grid entry and id, each step on its own.
    internal static class BuildingReleaseRepair
    {
        public static CommandResult Repair(string body)
        {
            bool dryRun = JsonUtil.GetBool(body, "dryRun", true);
            BuildingManager manager = Singleton<BuildingManager>.instance;
            Building[] buffer = manager.m_buildings.m_buffer;
            MethodInfo releasePaths = typeof(BuildingManager).GetMethod("ReleasePaths",
                BindingFlags.Instance | BindingFlags.NonPublic, null,
                new Type[] { typeof(ushort), typeof(Building).MakeByRefType() }, null);
            MethodInfo removeFromGrid = typeof(BuildingManager).GetMethod("RemoveFromGrid",
                BindingFlags.Instance | BindingFlags.NonPublic, null,
                new Type[] { typeof(ushort), typeof(Building).MakeByRefType() }, null);
            if (removeFromGrid == null)
            {
                return CommandResult.Fail("BuildingManager.RemoveFromGrid was not found.");
            }

            StringBuilder items = new StringBuilder();
            int found = 0;
            int repaired = 0;
            for (ushort id = 1; id < buffer.Length; id++)
            {
                Building.Flags flags = buffer[id].m_flags;
                if ((flags & Building.Flags.Created) == Building.Flags.None ||
                    (flags & Building.Flags.Deleted) == Building.Flags.None)
                {
                    continue;
                }
                found++;
                string error = null;
                if (!dryRun)
                {
                    try
                    {
                        if (buffer[id].m_citizenUnits != 0)
                        {
                            Singleton<CitizenManager>.instance.ReleaseUnits(buffer[id].m_citizenUnits);
                            buffer[id].m_citizenUnits = 0u;
                        }
                    }
                    catch (Exception ex) { error = "units: " + ex.Message; }
                    try
                    {
                        if (releasePaths != null)
                        {
                            object[] args = new object[] { id, buffer[id] };
                            releasePaths.Invoke(manager, args);
                            buffer[id] = (Building)args[1];
                        }
                    }
                    catch (Exception ex) { error = (error == null ? "" : error + "; ") + "paths: " + ex.Message; }
                    try
                    {
                        Building data = buffer[id];
                        data.m_flags = Building.Flags.None;
                        object[] args = new object[] { id, data };
                        removeFromGrid.Invoke(manager, args);
                        buffer[id] = (Building)args[1];
                        buffer[id].m_flags = Building.Flags.None;
                        manager.m_buildings.ReleaseItem(id);
                        manager.m_buildingCount = (int)(manager.m_buildings.ItemCount() - 1);
                        repaired++;
                    }
                    catch (Exception ex) { error = (error == null ? "" : error + "; ") + "grid: " + ex.Message; }
                }
                if (found <= 200)
                {
                    if (found > 1) items.Append(",");
                    BuildingInfo info = buffer[id].Info;
                    items.Append("{\"id\":").Append(id);
                    items.Append(",\"flags\":\"").Append(JsonUtil.Escape(flags.ToString())).Append("\"");
                    items.Append(",\"position\":{\"x\":").Append(JsonUtil.Number(buffer[id].m_position.x));
                    items.Append(",\"z\":").Append(JsonUtil.Number(buffer[id].m_position.z)).Append("}");
                    if (error != null) items.Append(",\"error\":\"").Append(JsonUtil.Escape(error)).Append("\"");
                    items.Append("}");
                }
            }
            return CommandResult.FromJson("{\"ok\":true,\"dryRun\":" + JsonUtil.Bool(dryRun) + ",\"found\":" + found +
                ",\"repaired\":" + repaired + ",\"buildings\":[" + items.ToString() + "]}");
        }
    }
}

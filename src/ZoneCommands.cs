using UnityEngine;

namespace SkylinesAgentBridge
{
    public static class ZoneCommands
    {
        public static CommandResult SetZone(string body)
        {
            bool dryRun = JsonUtil.GetBool(body, "dryRun", false);
            bool preserveOccupied = JsonUtil.GetBool(body, "preserveOccupied", true);
            string zoneName = JsonUtil.GetString(body, "zone", "ResidentialLow");
            ushort requestedBlockId = (ushort)JsonUtil.GetNumber(body, "blockId", 0f);
            float radius = JsonUtil.GetNumber(body, "radius", 32f);
            Vector3 center = ReadPoint(body, "center");

            if (requestedBlockId == 0 && (radius <= 0f || radius > 256f))
            {
                return CommandResult.Fail("Radius must be between 0 and 256.");
            }

            ItemClass.Zone zone;
            if (!TryParseZone(zoneName, out zone))
            {
                return CommandResult.Fail("Unsupported zone: " + zoneName);
            }

            ZoneManager manager = ZoneManager.instance;
            if (requestedBlockId >= manager.m_blocks.m_buffer.Length ||
                (requestedBlockId != 0 &&
                 (manager.m_blocks.m_buffer[requestedBlockId].m_flags & ZoneBlock.FLAG_CREATED) == 0))
            {
                return CommandResult.Fail("Requested zone block does not exist in the current city.");
            }
            float radiusSq = radius * radius;
            int touchedBlocks = 0;
            int skippedOccupiedBlocks = 0;
            int changedCells = 0;

            for (int i = 1; i < manager.m_blocks.m_buffer.Length; i++)
            {
                if (requestedBlockId != 0 && i != requestedBlockId)
                {
                    continue;
                }

                ZoneBlock block = manager.m_blocks.m_buffer[i];
                if ((block.m_flags & ZoneBlock.FLAG_CREATED) == 0)
                {
                    continue;
                }

                if (requestedBlockId == 0)
                {
                    Vector3 delta = block.m_position - center;
                    delta.y = 0f;
                    if (delta.sqrMagnitude > radiusSq)
                    {
                        continue;
                    }
                }

                touchedBlocks++;
                // An exact block is judged by its own native occupancy and by
                // building footprints that reach its cells. The broad radius
                // guard exists for legacy center/radius repaint and would
                // otherwise forbid every block across the street from a home.
                if (preserveOccupied &&
                    (requestedBlockId != 0
                        ? ExactBlockHasProtectedBuilding(block)
                        : ZoneBlockHasProtectedBuilding(block)))
                {
                    skippedOccupiedBlocks++;
                    continue;
                }

                if (!dryRun)
                {
                    int rows = ZoneHelpers.GetRowCount(block);

                    for (int z = 0; z < rows; z++)
                    {
                        for (int x = 0; x < 4; x++)
                        {
                            try
                            {
                                // Invalid cells keep a zone value but can never develop,
                                // so only valid cells count as changed capacity.
                                if (manager.m_blocks.m_buffer[i].SetZone(x, z, zone) &&
                                    (block.m_valid & ZoneCellLayout.Bit(x, z)) != 0)
                                {
                                    changedCells++;
                                }
                            }
                            catch (System.Exception ex)
                            {
                                Debug.Log("[SkylinesAgentBridge] Skipped zone cell " + i + "/" + x + "/" + z + ": " + ex.Message);
                            }
                        }
                    }

                    try
                    {
                        manager.m_blocks.m_buffer[i].RefreshZoning((ushort)i);
                        manager.UpdateBlock((ushort)i);
                    }
                    catch (System.Exception ex)
                    {
                        Debug.Log("[SkylinesAgentBridge] Failed to refresh zone block " + i + ": " + ex.Message);
                    }
                }
            }

            if (dryRun)
            {
                return CommandResult.FromJson("{\"ok\":true,\"dryRun\":true,\"zone\":\"" + JsonUtil.Escape(zoneName) + "\",\"preserveOccupied\":" + JsonUtil.Bool(preserveOccupied) + ",\"requestedBlockId\":" + requestedBlockId + ",\"matchingBlocks\":" + touchedBlocks + ",\"skippedOccupiedBlocks\":" + skippedOccupiedBlocks + "}");
            }

            if (requestedBlockId != 0 && zone != ItemClass.Zone.Unzoned &&
                skippedOccupiedBlocks > 0)
            {
                return CommandResult.FromJson("{\"ok\":false,\"error\":\"Requested zone block was skipped by a safety gate; no cells were changed.\",\"dryRun\":false,\"zone\":\"" + JsonUtil.Escape(zoneName) + "\",\"requestedBlockId\":" + requestedBlockId + ",\"skippedOccupiedBlocks\":" + skippedOccupiedBlocks + ",\"changedCells\":0}");
            }

            return CommandResult.FromJson("{\"ok\":true,\"dryRun\":false,\"zone\":\"" + JsonUtil.Escape(zoneName) + "\",\"preserveOccupied\":" + JsonUtil.Bool(preserveOccupied) + ",\"requestedBlockId\":" + requestedBlockId + ",\"touchedBlocks\":" + touchedBlocks + ",\"skippedOccupiedBlocks\":" + skippedOccupiedBlocks + ",\"changedCells\":" + changedCells + "}");
        }

        public static CommandResult RepairZonesToGrowables(string body)
        {
            bool dryRun = JsonUtil.GetBool(body, "dryRun", false);
            ZoneManager manager = ZoneManager.instance;
            int inspectedBlocks = 0;
            int repairableBlocks = 0;
            int skippedMixedUseBlocks = 0;
            int changedCells = 0;

            for (int i = 1; i < manager.m_blocks.m_buffer.Length; i++)
            {
                ZoneBlock block = manager.m_blocks.m_buffer[i];
                if ((block.m_flags & ZoneBlock.FLAG_CREATED) == 0)
                {
                    continue;
                }

                inspectedBlocks++;
                ItemClass.Zone expectedZone;
                bool hasGrowable;
                bool mixedUse;
                GetExpectedZoneFromNearbyGrowables(block, out expectedZone, out hasGrowable, out mixedUse);
                if (!hasGrowable)
                {
                    continue;
                }
                if (mixedUse)
                {
                    skippedMixedUseBlocks++;
                    continue;
                }

                int rows = ZoneHelpers.GetRowCount(block);
                bool needsRepair = false;
                for (int z = 0; z < rows; z++)
                {
                    for (int x = 0; x < 4; x++)
                    {
                        ItemClass.Zone current = block.GetZone(x, z);
                        if (current != ItemClass.Zone.Unzoned && current != expectedZone)
                        {
                            needsRepair = true;
                        }
                    }
                }

                if (!needsRepair)
                {
                    continue;
                }

                repairableBlocks++;
                if (dryRun)
                {
                    continue;
                }

                for (int z = 0; z < rows; z++)
                {
                    for (int x = 0; x < 4; x++)
                    {
                        try
                        {
                            ItemClass.Zone current = manager.m_blocks.m_buffer[i].GetZone(x, z);
                            if (current != ItemClass.Zone.Unzoned && manager.m_blocks.m_buffer[i].SetZone(x, z, expectedZone))
                            {
                                changedCells++;
                            }
                        }
                        catch (System.Exception ex)
                        {
                            Debug.Log("[SkylinesAgentBridge] Skipped growable zone repair cell " + i + "/" + x + "/" + z + ": " + ex.Message);
                        }
                    }
                }

                try
                {
                    manager.m_blocks.m_buffer[i].RefreshZoning((ushort)i);
                    manager.UpdateBlock((ushort)i);
                }
                catch (System.Exception ex)
                {
                    Debug.Log("[SkylinesAgentBridge] Failed to refresh repaired zone block " + i + ": " + ex.Message);
                }
            }

            return CommandResult.FromJson("{\"ok\":true,\"dryRun\":" + JsonUtil.Bool(dryRun) +
                ",\"inspectedBlocks\":" + inspectedBlocks +
                ",\"repairableBlocks\":" + repairableBlocks +
                ",\"skippedMixedUseBlocks\":" + skippedMixedUseBlocks +
                ",\"changedCells\":" + changedCells + "}");
        }

        public static CommandResult RepairZoneClusters(string body)
        {
            bool dryRun = JsonUtil.GetBool(body, "dryRun", false);
            bool includePatchy = JsonUtil.GetBool(body, "includePatchy", true);
            bool fillUnzoned = JsonUtil.GetBool(body, "fillUnzoned", true);
            bool preferGrowableZone = JsonUtil.GetBool(body, "preferGrowableZone", true);
            float gridSize = JsonUtil.GetNumber(body, "gridSize", 80f);
            int minMinorityCells = (int)JsonUtil.GetNumber(body, "minMinorityCells", 3f);
            int minUnzonedCells = (int)JsonUtil.GetNumber(body, "minUnzonedCells", 6f);
            if (gridSize < 24f)
            {
                gridSize = 24f;
            }
            if (gridSize > 160f)
            {
                gridSize = 160f;
            }
            if (minMinorityCells < 1)
            {
                minMinorityCells = 1;
            }
            if (minUnzonedCells < 1)
            {
                minUnzonedCells = 1;
            }

            ZoneManager manager = ZoneManager.instance;
            System.Collections.Generic.Dictionary<string, ZoneClusterRepairStats> clusters = BuildZoneClusters(manager, gridSize);
            int repairableClusters = 0;
            int repairedBlocks = 0;
            int changedCells = 0;

            foreach (System.Collections.Generic.KeyValuePair<string, ZoneClusterRepairStats> pair in clusters)
            {
                ZoneClusterRepairStats stats = pair.Value;
                bool isMixed = stats.DistinctZoned > 1 && stats.MinorityCells >= minMinorityCells;
                bool isPatchy = includePatchy && stats.DistinctZoned == 1 && stats.ZonedCells >= minMinorityCells && stats.UnzonedCells >= minUnzonedCells;
                if (!isMixed && !isPatchy)
                {
                    continue;
                }

                ItemClass.Zone clusterTargetZone;
                if (!TryParseZone(stats.DominantZone, out clusterTargetZone) || clusterTargetZone == ItemClass.Zone.Unzoned)
                {
                    continue;
                }

                repairableClusters++;
                if (dryRun)
                {
                    continue;
                }

                for (int i = 0; i < stats.BlockIds.Count; i++)
                {
                    ushort blockId = stats.BlockIds[i];
                    ZoneBlock block = manager.m_blocks.m_buffer[blockId];
                    if ((block.m_flags & ZoneBlock.FLAG_CREATED) == 0)
                    {
                        continue;
                    }

                    bool blockChanged = false;
                    ItemClass.Zone blockTargetZone = clusterTargetZone;
                    if (preferGrowableZone)
                    {
                        ItemClass.Zone growableZone;
                        bool hasGrowable;
                        bool mixedUse;
                        GetExpectedZoneFromNearbyGrowables(block, out growableZone, out hasGrowable, out mixedUse);
                        if (hasGrowable && !mixedUse && growableZone != ItemClass.Zone.Unzoned)
                        {
                            blockTargetZone = growableZone;
                        }
                    }

                    int rows = ZoneHelpers.GetRowCount(block);
                    for (int z = 0; z < rows; z++)
                    {
                        for (int x = 0; x < 4; x++)
                        {
                            try
                            {
                                ItemClass.Zone current = manager.m_blocks.m_buffer[blockId].GetZone(x, z);
                                if (current == blockTargetZone)
                                {
                                    continue;
                                }
                                if (!fillUnzoned && current == ItemClass.Zone.Unzoned)
                                {
                                    continue;
                                }

                                if (manager.m_blocks.m_buffer[blockId].SetZone(x, z, blockTargetZone))
                                {
                                    changedCells++;
                                    blockChanged = true;
                                }
                            }
                            catch (System.Exception ex)
                            {
                                Debug.Log("[SkylinesAgentBridge] Skipped zone cluster repair cell " + blockId + "/" + x + "/" + z + ": " + ex.Message);
                            }
                        }
                    }

                    if (blockChanged)
                    {
                        repairedBlocks++;
                        try
                        {
                            manager.m_blocks.m_buffer[blockId].RefreshZoning(blockId);
                            manager.UpdateBlock(blockId);
                        }
                        catch (System.Exception ex)
                        {
                            Debug.Log("[SkylinesAgentBridge] Failed to refresh cluster-repaired zone block " + blockId + ": " + ex.Message);
                        }
                    }
                }
            }

            return CommandResult.FromJson("{\"ok\":true,\"dryRun\":" + JsonUtil.Bool(dryRun) +
                ",\"gridSize\":" + JsonUtil.Number(gridSize) +
                ",\"includePatchy\":" + JsonUtil.Bool(includePatchy) +
                ",\"fillUnzoned\":" + JsonUtil.Bool(fillUnzoned) +
                ",\"preferGrowableZone\":" + JsonUtil.Bool(preferGrowableZone) +
                ",\"clusterCount\":" + clusters.Count +
                ",\"repairableClusters\":" + repairableClusters +
                ",\"repairedBlocks\":" + repairedBlocks +
                ",\"changedCells\":" + changedCells + "}");
        }

        private static bool TryParseZone(string name, out ItemClass.Zone zone)
        {
            if (name == "Unzoned") { zone = ItemClass.Zone.Unzoned; return true; }
            if (name == "ResidentialLow") { zone = ItemClass.Zone.ResidentialLow; return true; }
            if (name == "ResidentialHigh") { zone = ItemClass.Zone.ResidentialHigh; return true; }
            if (name == "CommercialLow") { zone = ItemClass.Zone.CommercialLow; return true; }
            if (name == "CommercialHigh") { zone = ItemClass.Zone.CommercialHigh; return true; }
            if (name == "Industrial") { zone = ItemClass.Zone.Industrial; return true; }
            if (name == "Office") { zone = ItemClass.Zone.Office; return true; }
            zone = ItemClass.Zone.Unzoned;
            return false;
        }

        private static bool ExactBlockHasProtectedBuilding(ZoneBlock block)
        {
            int rows = ZoneHelpers.GetRowCount(block);
            ulong cells = 0;
            for (int z = 0; z < rows; z++)
            {
                for (int x = 0; x < 4; x++)
                {
                    cells |= ZoneCellLayout.Bit(x, z);
                }
            }
            ulong valid = block.m_valid & cells;
            if (((block.m_occupied1 | block.m_occupied2) & valid) != 0)
            {
                return true;
            }

            BuildingManager manager = BuildingManager.instance;
            for (ushort i = 1; i < manager.m_buildings.m_buffer.Length; i++)
            {
                Building building = manager.m_buildings.m_buffer[i];
                if ((building.m_flags & Building.Flags.Created) == Building.Flags.None)
                {
                    continue;
                }
                BuildingInfo info = building.Info;
                // Zoning never demolishes a building; a power pole only blocks
                // the cells it stands on, which native occupancy already covers.
                if (info == null || info.m_class == null || IsInternalNetworkHelperBuilding(info) ||
                    info.m_buildingAI is PowerPoleAI || !ShouldProtectBuilding(info))
                {
                    continue;
                }
                if (FootprintReachesBlockCell(building, info, block, rows, valid))
                {
                    return true;
                }
            }
            return false;
        }

        private static bool FootprintReachesBlockCell(
            Building building,
            BuildingInfo info,
            ZoneBlock block,
            int rows,
            ulong valid)
        {
            // Separating-axis test between the rotated building footprint and
            // each 8m cell. Buildings that only share an edge with the block,
            // such as a home on the neighbouring block, are not overlaps.
            const float penetrationTolerance = 0.5f;
            float halfWidth = info.GetWidth() * 4f;
            float halfLength = info.GetLength() * 4f;
            Vector3 across = new Vector3(Mathf.Cos(building.m_angle), 0f, Mathf.Sin(building.m_angle));
            Vector3 along = new Vector3(across.z, 0f, -across.x);
            Vector3 cellX = new Vector3(Mathf.Cos(block.m_angle), 0f, Mathf.Sin(block.m_angle));
            Vector3 cellZ = new Vector3(cellX.z, 0f, -cellX.x);
            Vector3[] axes = { across, along, cellX, cellZ };
            for (int z = 0; z < rows; z++)
            {
                for (int x = 0; x < 4; x++)
                {
                    if ((valid & ZoneCellLayout.Bit(x, z)) == 0)
                    {
                        continue;
                    }
                    double[] p = ZoneCellLayout.Point(block.m_position.x, block.m_position.z, block.m_angle, x + 0.5, z + 0.5);
                    Vector3 delta = new Vector3((float)p[0] - building.m_position.x, 0f, (float)p[1] - building.m_position.z);
                    bool separated = false;
                    for (int a = 0; a < axes.Length && !separated; a++)
                    {
                        Vector3 n = axes[a];
                        float buildingRadius = halfWidth * Mathf.Abs(Vector3.Dot(across, n)) +
                            halfLength * Mathf.Abs(Vector3.Dot(along, n));
                        float cellRadius = 4f * Mathf.Abs(Vector3.Dot(cellX, n)) +
                            4f * Mathf.Abs(Vector3.Dot(cellZ, n));
                        separated = Mathf.Abs(Vector3.Dot(delta, n)) >=
                            buildingRadius + cellRadius - penetrationTolerance;
                    }
                    if (!separated)
                    {
                        return true;
                    }
                }
            }
            return false;
        }

        private static bool ZoneBlockHasProtectedBuilding(ZoneBlock block)
        {
            BuildingManager manager = BuildingManager.instance;
            for (ushort i = 1; i < manager.m_buildings.m_buffer.Length; i++)
            {
                Building building = manager.m_buildings.m_buffer[i];
                if ((building.m_flags & Building.Flags.Created) == Building.Flags.None)
                {
                    continue;
                }

                BuildingInfo info = building.Info;
                if (info == null || info.m_class == null || IsInternalNetworkHelperBuilding(info))
                {
                    continue;
                }

                if (!ShouldProtectBuilding(info))
                {
                    continue;
                }

                float protectionRadius = Mathf.Max(info.GetWidth(), info.GetLength()) * 4f + 38f;
                Vector3 delta = building.m_position - block.m_position;
                delta.y = 0f;
                if (delta.sqrMagnitude <= protectionRadius * protectionRadius)
                {
                    return true;
                }
            }

            return false;
        }

        private static void GetExpectedZoneFromNearbyGrowables(ZoneBlock block, out ItemClass.Zone expectedZone, out bool hasGrowable, out bool mixedUse)
        {
            expectedZone = ItemClass.Zone.Unzoned;
            hasGrowable = false;
            mixedUse = false;
            BuildingManager manager = BuildingManager.instance;
            float closestSq = float.MaxValue;

            for (ushort i = 1; i < manager.m_buildings.m_buffer.Length; i++)
            {
                Building building = manager.m_buildings.m_buffer[i];
                if ((building.m_flags & Building.Flags.Created) == Building.Flags.None)
                {
                    continue;
                }

                BuildingInfo info = building.Info;
                if (info == null || info.m_class == null)
                {
                    continue;
                }

                ItemClass.Zone buildingZone;
                if (!TryGetGrowableZone(info, out buildingZone))
                {
                    continue;
                }

                float protectionRadius = Mathf.Max(info.GetWidth(), info.GetLength()) * 4f + 38f;
                Vector3 delta = building.m_position - block.m_position;
                delta.y = 0f;
                if (delta.sqrMagnitude > protectionRadius * protectionRadius)
                {
                    continue;
                }

                if (!hasGrowable || delta.sqrMagnitude < closestSq)
                {
                    expectedZone = buildingZone;
                    hasGrowable = true;
                    closestSq = delta.sqrMagnitude;
                }
                else if (expectedZone != buildingZone && Mathf.Abs(delta.sqrMagnitude - closestSq) < 100f)
                {
                    mixedUse = true;
                    return;
                }
            }
        }

        private static bool TryGetGrowableZone(BuildingInfo info, out ItemClass.Zone zone)
        {
            string service = info.m_class.m_service.ToString();
            string subService = info.m_class.m_subService.ToString();
            if (service == "Residential")
            {
                zone = subService == "ResidentialHigh" ? ItemClass.Zone.ResidentialHigh : ItemClass.Zone.ResidentialLow;
                return true;
            }
            if (service == "Commercial")
            {
                zone = subService == "CommercialHigh" ? ItemClass.Zone.CommercialHigh : ItemClass.Zone.CommercialLow;
                return true;
            }
            if (service == "Industrial")
            {
                zone = ItemClass.Zone.Industrial;
                return true;
            }
            if (service == "Office")
            {
                zone = ItemClass.Zone.Office;
                return true;
            }

            zone = ItemClass.Zone.Unzoned;
            return false;
        }

        private static System.Collections.Generic.Dictionary<string, ZoneClusterRepairStats> BuildZoneClusters(ZoneManager manager, float gridSize)
        {
            System.Collections.Generic.Dictionary<string, ZoneClusterRepairStats> clusters = new System.Collections.Generic.Dictionary<string, ZoneClusterRepairStats>();
            for (ushort blockId = 1; blockId < manager.m_blocks.m_buffer.Length; blockId++)
            {
                ZoneBlock block = manager.m_blocks.m_buffer[blockId];
                if ((block.m_flags & ZoneBlock.FLAG_CREATED) == 0)
                {
                    continue;
                }

                string key = ClusterKey(block.m_position, gridSize);
                ZoneClusterRepairStats stats;
                if (!clusters.TryGetValue(key, out stats))
                {
                    stats = new ZoneClusterRepairStats(key);
                    clusters[key] = stats;
                }
                stats.Add(blockId, block);
            }

            return clusters;
        }

        private static string ClusterKey(Vector3 position, float gridSize)
        {
            int x = Mathf.FloorToInt((position.x + gridSize * 0.5f) / gridSize);
            int z = Mathf.FloorToInt((position.z + gridSize * 0.5f) / gridSize);
            return x.ToString() + ":" + z.ToString();
        }

        private sealed class ZoneClusterRepairStats
        {
            public readonly string Key;
            public readonly System.Collections.Generic.List<ushort> BlockIds = new System.Collections.Generic.List<ushort>();
            private readonly System.Collections.Generic.Dictionary<string, int> zoneCounts = new System.Collections.Generic.Dictionary<string, int>();
            public int CellCount;
            public int ZonedCells;
            public int UnzonedCells;
            public int DominantCells;
            public int DistinctZoned;
            public int MinorityCells;
            public string DominantZone = "Unzoned";

            public ZoneClusterRepairStats(string key)
            {
                Key = key;
            }

            public void Add(ushort blockId, ZoneBlock block)
            {
                BlockIds.Add(blockId);
                int rows = ZoneHelpers.GetRowCount(block);
                for (int z = 0; z < rows; z++)
                {
                    for (int x = 0; x < 4; x++)
                    {
                        ItemClass.Zone zone = block.GetZone(x, z);
                        string zoneName = zone.ToString();
                        if (zoneCounts.ContainsKey(zoneName))
                        {
                            zoneCounts[zoneName]++;
                        }
                        else
                        {
                            zoneCounts[zoneName] = 1;
                        }

                        CellCount++;
                        if (zone == ItemClass.Zone.Unzoned)
                        {
                            UnzonedCells++;
                        }
                        else
                        {
                            ZonedCells++;
                        }
                    }
                }

                Recalculate();
            }

            private void Recalculate()
            {
                DominantCells = 0;
                DominantZone = "Unzoned";
                DistinctZoned = 0;
                foreach (System.Collections.Generic.KeyValuePair<string, int> pair in zoneCounts)
                {
                    if (pair.Key == "Unzoned")
                    {
                        continue;
                    }

                    DistinctZoned++;
                    if (pair.Value > DominantCells)
                    {
                        DominantCells = pair.Value;
                        DominantZone = pair.Key;
                    }
                }

                MinorityCells = ZonedCells - DominantCells;
            }
        }

        private static bool ShouldProtectBuilding(BuildingInfo info)
        {
            string service = info.m_class.m_service.ToString();
            return service == "Residential" ||
                service == "Commercial" ||
                service == "Industrial" ||
                service == "Office" ||
                service == "Water" ||
                service == "Electricity" ||
                service == "Garbage" ||
                service == "HealthCare" ||
                service == "PoliceDepartment" ||
                service == "FireDepartment" ||
                service == "Education" ||
                service == "Disaster" ||
                service == "Beautification" ||
                service == "Monument";
        }

        private static bool IsInternalNetworkHelperBuilding(BuildingInfo info)
        {
            return info.name == "Water Pipe Junction" || info.name == "Heating Pipe Junction";
        }

        private static Vector3 ReadPoint(string body, string name)
        {
            float x = JsonUtil.GetPointNumber(body, name, "x", 0f);
            float z = JsonUtil.GetPointNumber(body, name, "z", 0f);
            float y = JsonUtil.GetPointNumber(body, name, "y", 0f);
            return new Vector3(x, y, z);
        }
    }
}

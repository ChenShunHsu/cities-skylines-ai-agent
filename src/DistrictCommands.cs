using System;
using System.Text;
using ColossalFramework;
using UnityEngine;

namespace SkylinesAgentBridge
{
    public static class DistrictCommands
    {
        private const int GridResolution = 512;
        private const float GridCellSize = 19.2f;

        public static CommandResult BuildDistrictsJson()
        {
            DistrictManager manager = Singleton<DistrictManager>.instance;
            StringBuilder items = new StringBuilder();
            bool first = true;
            int count = 0;

            for (int id = 1; id < 128; id++)
            {
                District district = manager.m_districts.m_buffer[id];
                if ((district.m_flags & District.Flags.Created) == District.Flags.None) continue;
                if (!first) items.Append(",");

                int minX;
                int minZ;
                int maxX;
                int maxZ;
                manager.GetDistrictArea((byte)id, out minX, out minZ, out maxX, out maxZ);
                items.Append("{\"id\":").Append(id);
                items.Append(",\"name\":\"").Append(JsonUtil.Escape(manager.GetDistrictName(id))).Append("\"");
                items.Append(",\"position\":{\"x\":").Append(JsonUtil.Number(district.m_nameLocation.x));
                items.Append(",\"z\":").Append(JsonUtil.Number(district.m_nameLocation.z)).Append("}");
                items.Append(",\"gridBounds\":{\"minX\":").Append(minX).Append(",\"minZ\":").Append(minZ);
                items.Append(",\"maxX\":").Append(maxX).Append(",\"maxZ\":").Append(maxZ).Append("}");
                items.Append(",\"specialization\":\"").Append(SpecializationName(district.m_specializationPolicies)).Append("\"");
                items.Append(",\"specializationFlags\":\"").Append(JsonUtil.Escape(district.m_specializationPolicies.ToString())).Append("\"");
                items.Append(",\"population\":").Append(district.m_populationData.m_finalCount);
                items.Append(",\"workers\":").Append(district.GetWorkerCount());
                items.Append(",\"workplaces\":").Append(district.GetWorkplaceCount());
                items.Append(",\"unemployment\":").Append(district.GetUnemployment());
                items.Append(",\"happiness\":").Append(district.m_finalHappiness);
                items.Append(",\"crimeRate\":").Append(district.m_finalCrimeRate);
                items.Append(",\"landValue\":").Append(district.GetLandValue());
                items.Append(",\"groundPollution\":").Append(district.GetGroundPollution());
                items.Append(",\"industryArea\":{\"generic\":").Append(district.GetGenericIndustryArea());
                items.Append(",\"farming\":").Append(district.GetFarmingIndustryArea());
                items.Append(",\"forestry\":").Append(district.GetForestryIndustryArea());
                items.Append(",\"ore\":").Append(district.GetOreIndustryArea());
                items.Append(",\"oil\":").Append(district.GetOilIndustryArea()).Append("}}");
                first = false;
                count++;
            }

            return CommandResult.FromJson("{\"ok\":true,\"count\":" + count + ",\"districts\":[" + items.ToString() + "]}");
        }

        public static CommandResult CreateDistrict(string body)
        {
            float x = JsonUtil.GetPointNumber(body, "center", "x", float.NaN);
            float z = JsonUtil.GetPointNumber(body, "center", "z", float.NaN);
            float radius = JsonUtil.GetNumber(body, "radius", 0f);
            bool replaceExisting = JsonUtil.GetBool(body, "replaceExisting", false);
            bool dryRun = JsonUtil.GetBool(body, "dryRun", false);

            if (float.IsNaN(x) || float.IsNaN(z)) return CommandResult.Fail("center.x and center.z are required.");
            if (radius < GridCellSize || radius > 1000f) return CommandResult.Fail("radius must be between 19.2 and 1000 metres.");

            DistrictManager manager = Singleton<DistrictManager>.instance;
            int minColumn = WorldToGrid(x - radius);
            int maxColumn = WorldToGrid(x + radius);
            int minRow = WorldToGrid(z - radius);
            int maxRow = WorldToGrid(z + radius);
            int targetCells = 0;
            int occupiedCells = 0;

            for (int row = minRow; row <= maxRow; row++)
            {
                for (int column = minColumn; column <= maxColumn; column++)
                {
                    Vector2 center = CellCenter(column, row);
                    if ((center - new Vector2(x, z)).sqrMagnitude > radius * radius) continue;
                    targetCells++;
                    byte existing = manager.GetDistrict(column, row);
                    if (existing != 0) occupiedCells++;
                }
            }

            if (targetCells == 0) return CommandResult.Fail("The requested circle does not cover any district cells.");
            if (occupiedCells > 0 && !replaceExisting)
            {
                return CommandResult.Fail("The requested area overlaps " + occupiedCells + " existing district cells; set replaceExisting=true only after review.");
            }
            if (dryRun)
            {
                return CommandResult.FromJson("{\"ok\":true,\"dryRun\":true,\"targetCells\":" + targetCells + ",\"occupiedCells\":" + occupiedCells + "}");
            }

            byte districtId;
            if (!manager.CreateDistrict(out districtId)) return CommandResult.Fail("No free district slot is available.");
            if ((manager.m_districts.m_buffer[districtId].m_flags & District.Flags.Created) == District.Flags.None)
            {
                manager.ReleaseDistrict(districtId);
                return CommandResult.Fail("DistrictManager allocated an ID but did not create the district.");
            }

            try
            {
                for (int row = minRow; row <= maxRow; row++)
                {
                    for (int column = minColumn; column <= maxColumn; column++)
                    {
                        Vector2 center = CellCenter(column, row);
                        if ((center - new Vector2(x, z)).sqrMagnitude > radius * radius) continue;
                        DistrictManager.Cell cell = new DistrictManager.Cell();
                        cell.m_district1 = districtId;
                        cell.m_alpha1 = 255;
                        manager.ModifyCell(column, row, cell);
                    }
                }
                manager.AreaModified(minColumn, minRow, maxColumn, maxRow, true);
            }
            catch (Exception ex)
            {
                manager.ReleaseDistrict(districtId);
                return CommandResult.Fail("District painting failed and the new district was released: " + ex.Message);
            }

            return CommandResult.FromJson("{\"ok\":true,\"dryRun\":false,\"districtId\":" + districtId +
                ",\"targetCells\":" + targetCells + ",\"replacedCells\":" + occupiedCells + "}");
        }

        public static CommandResult SetDistrictSpecialization(string body)
        {
            int id = (int)JsonUtil.GetNumber(body, "districtId", 0f);
            string requested = JsonUtil.GetString(body, "specialization", "").Trim();
            bool dryRun = JsonUtil.GetBool(body, "dryRun", false);
            bool requireLocalResource = JsonUtil.GetBool(body, "requireLocalResource", false);
            int minResourceIntensity = Mathf.Clamp((int)JsonUtil.GetNumber(body, "minResourceIntensity", 32f), 1, 255);
            if (id <= 0 || id >= 128) return CommandResult.Fail("districtId must be between 1 and 127.");

            DistrictManager manager = Singleton<DistrictManager>.instance;
            District district = manager.m_districts.m_buffer[id];
            if ((district.m_flags & District.Flags.Created) == District.Flags.None) return CommandResult.Fail("District was not found: " + id);

            DistrictPolicies.Policies policy;
            if (!TrySpecializationPolicy(requested, out policy))
            {
                return CommandResult.Fail("specialization must be None, Forestry, Farming, Oil, or Ore.");
            }
            if (policy != DistrictPolicies.Policies.None &&
                !Singleton<UnlockManager>.instance.Unlocked(policy))
            {
                return CommandResult.Fail("District specialization is still locked: " + requested);
            }
            string canonicalRequested = "None";
            if (policy == DistrictPolicies.Policies.Forest) canonicalRequested = "Forestry";
            else if (policy == DistrictPolicies.Policies.Farming) canonicalRequested = "Farming";
            else if (policy == DistrictPolicies.Policies.Oil) canonicalRequested = "Oil";
            else if (policy == DistrictPolicies.Policies.Ore) canonicalRequested = "Ore";

            string before = SpecializationName(district.m_specializationPolicies);
            int resourceSamples;
            int matchingResourceSamples;
            int maximumResourceIntensity;
            float averageResourceIntensity;
            AnalyzeDistrictResource(
                manager,
                (byte)id,
                canonicalRequested,
                minResourceIntensity,
                out resourceSamples,
                out matchingResourceSamples,
                out maximumResourceIntensity,
                out averageResourceIntensity);
            bool resourceApplicable = canonicalRequested == "None" || matchingResourceSamples > 0;
            if (requireLocalResource && !resourceApplicable)
            {
                return CommandResult.Fail(
                    "District has no sampled " + canonicalRequested + " resource at or above intensity " +
                    minResourceIntensity + "; local-extraction specialization was rejected.");
            }
            if (!dryRun)
            {
                district.UnsetPolicy(DistrictPolicies.Policies.Forest);
                district.UnsetPolicy(DistrictPolicies.Policies.Farming);
                district.UnsetPolicy(DistrictPolicies.Policies.Oil);
                district.UnsetPolicy(DistrictPolicies.Policies.Ore);
                if (policy != DistrictPolicies.Policies.None)
                {
                    district.SetPolicy(policy);
                }
                manager.m_districts.m_buffer[id] = district;
            }

            District afterDistrict = manager.m_districts.m_buffer[id];
            return CommandResult.FromJson("{\"ok\":true,\"dryRun\":" + JsonUtil.Bool(dryRun) +
                ",\"districtId\":" + id +
                ",\"before\":\"" + JsonUtil.Escape(before) +
                "\",\"after\":\"" + JsonUtil.Escape(dryRun ? canonicalRequested : SpecializationName(afterDistrict.m_specializationPolicies)) + "\"" +
                ",\"resourceEvidence\":{\"requiredForLocalExtraction\":" + JsonUtil.Bool(requireLocalResource) +
                ",\"minimumIntensity\":" + minResourceIntensity +
                ",\"samples\":" + resourceSamples +
                ",\"matchingSamples\":" + matchingResourceSamples +
                ",\"maximumIntensity\":" + maximumResourceIntensity +
                ",\"averageIntensity\":" + JsonUtil.Number(averageResourceIntensity) +
                ",\"localResourcePresent\":" + JsonUtil.Bool(resourceApplicable) +
                ",\"importDependentWarning\":" + JsonUtil.Bool(canonicalRequested != "None" && !resourceApplicable) + "}}");
        }

        private static void AnalyzeDistrictResource(
            DistrictManager districts,
            byte districtId,
            string specialization,
            int minimum,
            out int samples,
            out int matching,
            out int maximum,
            out float average)
        {
            samples = 0;
            matching = 0;
            maximum = 0;
            long total = 0;
            if (specialization == "None")
            {
                average = 0f;
                return;
            }

            int minX;
            int minZ;
            int maxX;
            int maxZ;
            districts.GetDistrictArea(districtId, out minX, out minZ, out maxX, out maxZ);
            NaturalResourceManager resources = Singleton<NaturalResourceManager>.instance;
            for (int row = minZ; row <= maxZ; row += 2)
            {
                for (int column = minX; column <= maxX; column += 2)
                {
                    if (districts.GetDistrict(column, row) != districtId) continue;
                    Vector2 center = CellCenter(column, row);
                    NaturalResourceManager.ResourceCell cell = SampleResource(resources, center.x, center.y);
                    int intensity = ResourceIntensity(cell, specialization);
                    samples++;
                    total += intensity;
                    if (intensity > maximum) maximum = intensity;
                    if (intensity >= minimum) matching++;
                }
            }
            average = samples == 0 ? 0f : (float)total / samples;
        }

        private static NaturalResourceManager.ResourceCell SampleResource(
            NaturalResourceManager manager,
            float x,
            float z)
        {
            int resolution = NaturalResourceManager.RESOURCEGRID_RESOLUTION;
            float size = NaturalResourceManager.RESOURCEGRID_CELL_SIZE;
            int column = Mathf.Clamp(Mathf.FloorToInt(x / size + resolution * 0.5f), 0, resolution - 1);
            int row = Mathf.Clamp(Mathf.FloorToInt(z / size + resolution * 0.5f), 0, resolution - 1);
            return manager.m_naturalResources[row * resolution + column];
        }

        private static int ResourceIntensity(
            NaturalResourceManager.ResourceCell cell,
            string specialization)
        {
            if (specialization == "Forestry") return cell.m_forest;
            if (specialization == "Farming") return cell.m_fertility;
            if (specialization == "Oil") return cell.m_oil;
            if (specialization == "Ore") return cell.m_ore;
            return 0;
        }

        public static CommandResult UpdateDistrictBoundary(string body)
        {
            int id = (int)JsonUtil.GetNumber(body, "districtId", 0f);
            float x = JsonUtil.GetPointNumber(body, "center", "x", float.NaN);
            float z = JsonUtil.GetPointNumber(body, "center", "z", float.NaN);
            float radius = JsonUtil.GetNumber(body, "radius", 0f);
            string mode = JsonUtil.GetString(body, "mode", "add");
            bool replaceExisting = JsonUtil.GetBool(body, "replaceExisting", false);
            bool dryRun = JsonUtil.GetBool(body, "dryRun", false);
            if (id <= 0 || id >= 128) return CommandResult.Fail("districtId must be between 1 and 127.");
            if (float.IsNaN(x) || float.IsNaN(z)) return CommandResult.Fail("center.x and center.z are required.");
            if (radius < GridCellSize || radius > 1000f) return CommandResult.Fail("radius must be between 19.2 and 1000 metres.");
            bool remove = string.Equals(mode, "remove", StringComparison.OrdinalIgnoreCase) ||
                string.Equals(mode, "erase", StringComparison.OrdinalIgnoreCase);
            if (!remove && !string.Equals(mode, "add", StringComparison.OrdinalIgnoreCase))
            {
                return CommandResult.Fail("mode must be add or remove.");
            }

            DistrictManager manager = Singleton<DistrictManager>.instance;
            if ((manager.m_districts.m_buffer[id].m_flags & District.Flags.Created) == District.Flags.None)
            {
                return CommandResult.Fail("District was not found: " + id);
            }
            int minColumn = WorldToGrid(x - radius);
            int maxColumn = WorldToGrid(x + radius);
            int minRow = WorldToGrid(z - radius);
            int maxRow = WorldToGrid(z + radius);
            int targetCells = 0;
            int changedCells = 0;
            int conflictingCells = 0;

            for (int row = minRow; row <= maxRow; row++)
            {
                for (int column = minColumn; column <= maxColumn; column++)
                {
                    Vector2 center = CellCenter(column, row);
                    if ((center - new Vector2(x, z)).sqrMagnitude > radius * radius) continue;
                    targetCells++;
                    byte existing = manager.GetDistrict(column, row);
                    if (!remove && existing != 0 && existing != id) conflictingCells++;
                }
            }
            if (!remove && conflictingCells > 0 && !replaceExisting)
            {
                return CommandResult.Fail("The requested addition overlaps " + conflictingCells + " cells belonging to another district.");
            }

            if (!dryRun)
            {
                for (int row = minRow; row <= maxRow; row++)
                {
                    for (int column = minColumn; column <= maxColumn; column++)
                    {
                        Vector2 center = CellCenter(column, row);
                        if ((center - new Vector2(x, z)).sqrMagnitude > radius * radius) continue;
                        int gridIndex = row * GridResolution + column;
                        DistrictManager.Cell current = manager.m_districtGrid[gridIndex];
                        if (remove)
                        {
                            DistrictManager.Cell updated;
                            if (!RemoveDistrictLayer(current, (byte)id, out updated)) continue;
                            manager.ModifyCell(column, row, updated);
                            changedCells++;
                        }
                        else
                        {
                            if (manager.GetDistrict(column, row) == id) continue;
                            DistrictManager.Cell updated = new DistrictManager.Cell();
                            updated.m_district1 = (byte)id;
                            updated.m_alpha1 = 255;
                            manager.ModifyCell(column, row, updated);
                            changedCells++;
                        }
                    }
                }
                manager.AreaModified(minColumn, minRow, maxColumn, maxRow, true);
            }

            return CommandResult.FromJson("{\"ok\":true,\"dryRun\":" + JsonUtil.Bool(dryRun) +
                ",\"districtId\":" + id +
                ",\"mode\":\"" + (remove ? "remove" : "add") + "\"" +
                ",\"targetCells\":" + targetCells +
                ",\"changedCells\":" + changedCells +
                ",\"conflictingCells\":" + conflictingCells + "}");
        }

        public static CommandResult RemoveDistrict(string body)
        {
            int id = (int)JsonUtil.GetNumber(body, "districtId", 0f);
            bool dryRun = JsonUtil.GetBool(body, "dryRun", false);
            if (id <= 0 || id >= 128) return CommandResult.Fail("districtId must be between 1 and 127.");
            DistrictManager manager = Singleton<DistrictManager>.instance;
            District district = manager.m_districts.m_buffer[id];
            if ((district.m_flags & District.Flags.Created) == District.Flags.None) return CommandResult.Fail("District was not found: " + id);
            string name = manager.GetDistrictName(id);
            int minX;
            int minZ;
            int maxX;
            int maxZ;
            manager.GetDistrictArea((byte)id, out minX, out minZ, out maxX, out maxZ);
            int clearedCells = 0;
            if (!dryRun)
            {
                for (int row = minZ; row <= maxZ; row++)
                {
                    for (int column = minX; column <= maxX; column++)
                    {
                        int gridIndex = row * GridResolution + column;
                        DistrictManager.Cell cell = manager.m_districtGrid[gridIndex];
                        DistrictManager.Cell updated;
                        if (!RemoveDistrictLayer(cell, (byte)id, out updated)) continue;
                        manager.ModifyCell(column, row, updated);
                        clearedCells++;
                    }
                }
                manager.AreaModified(minX, minZ, maxX, maxZ, true);
                manager.ReleaseDistrict((byte)id);
            }
            return CommandResult.FromJson("{\"ok\":true,\"dryRun\":" + JsonUtil.Bool(dryRun) +
                ",\"districtId\":" + id + ",\"name\":\"" + JsonUtil.Escape(name) +
                "\",\"clearedCells\":" + clearedCells + "}");
        }

        public static CommandResult SetDistrictName(string body)
        {
            int id = (int)JsonUtil.GetNumber(body, "districtId", 0f);
            string name = JsonUtil.GetString(body, "name", "").Trim();
            bool dryRun = JsonUtil.GetBool(body, "dryRun", false);
            if (id <= 0 || id >= 128) return CommandResult.Fail("districtId must be between 1 and 127.");
            if (name.Length == 0 || name.Length > 64) return CommandResult.Fail("name must contain 1 to 64 characters.");
            DistrictManager manager = Singleton<DistrictManager>.instance;
            District district = manager.m_districts.m_buffer[id];
            if ((district.m_flags & District.Flags.Created) == District.Flags.None) return CommandResult.Fail("District was not found: " + id);
            string before = manager.GetDistrictName(id);
            if (!dryRun)
            {
                System.Collections.Generic.IEnumerator<bool> operation = manager.SetDistrictName(id, name);
                while (operation.MoveNext()) { }
            }
            return CommandResult.FromJson("{\"ok\":true,\"dryRun\":" + JsonUtil.Bool(dryRun) +
                ",\"districtId\":" + id +
                ",\"before\":\"" + JsonUtil.Escape(before) +
                "\",\"after\":\"" + JsonUtil.Escape(dryRun ? before : manager.GetDistrictName(id)) + "\"}");
        }

        private static bool TrySpecializationPolicy(string value, out DistrictPolicies.Policies policy)
        {
            if (string.Equals(value, "None", StringComparison.OrdinalIgnoreCase)) { policy = DistrictPolicies.Policies.None; return true; }
            if (string.Equals(value, "Forest", StringComparison.OrdinalIgnoreCase) || string.Equals(value, "Forestry", StringComparison.OrdinalIgnoreCase)) { policy = DistrictPolicies.Policies.Forest; return true; }
            if (string.Equals(value, "Farming", StringComparison.OrdinalIgnoreCase)) { policy = DistrictPolicies.Policies.Farming; return true; }
            if (string.Equals(value, "Oil", StringComparison.OrdinalIgnoreCase)) { policy = DistrictPolicies.Policies.Oil; return true; }
            if (string.Equals(value, "Ore", StringComparison.OrdinalIgnoreCase)) { policy = DistrictPolicies.Policies.Ore; return true; }
            policy = DistrictPolicies.Policies.None;
            return false;
        }

        private static string SpecializationName(DistrictPolicies.Specialization value)
        {
            if ((value & DistrictPolicies.Specialization.Forest) != 0) return "Forestry";
            if ((value & DistrictPolicies.Specialization.Farming) != 0) return "Farming";
            if ((value & DistrictPolicies.Specialization.Oil) != 0) return "Oil";
            if ((value & DistrictPolicies.Specialization.Ore) != 0) return "Ore";
            return value == DistrictPolicies.Specialization.None ? "None" : value.ToString();
        }

        private static int WorldToGrid(float value)
        {
            return Mathf.Clamp(Mathf.FloorToInt(value / GridCellSize + GridResolution * 0.5f), 0, GridResolution - 1);
        }

        private static Vector2 CellCenter(int column, int row)
        {
            return new Vector2(
                (column - GridResolution * 0.5f + 0.5f) * GridCellSize,
                (row - GridResolution * 0.5f + 0.5f) * GridCellSize);
        }

        private static bool RemoveDistrictLayer(DistrictManager.Cell cell, byte districtId, out DistrictManager.Cell updated)
        {
            byte[] districts = { cell.m_district1, cell.m_district2, cell.m_district3, cell.m_district4 };
            byte[] alphas = { cell.m_alpha1, cell.m_alpha2, cell.m_alpha3, cell.m_alpha4 };
            byte[] remainingDistricts = new byte[4];
            byte[] remainingAlphas = new byte[4];
            int remaining = 0;
            bool removed = false;

            for (int i = 0; i < 4; i++)
            {
                if (districts[i] == districtId && alphas[i] > 0)
                {
                    removed = true;
                    continue;
                }
                if (alphas[i] == 0) continue;
                remainingDistricts[remaining] = districts[i];
                remainingAlphas[remaining] = alphas[i];
                remaining++;
            }

            updated = new DistrictManager.Cell();
            updated.m_district1 = remainingDistricts[0];
            updated.m_alpha1 = remainingAlphas[0];
            updated.m_district2 = remainingDistricts[1];
            updated.m_alpha2 = remainingAlphas[1];
            updated.m_district3 = remainingDistricts[2];
            updated.m_alpha3 = remainingAlphas[2];
            updated.m_district4 = remainingDistricts[3];
            updated.m_alpha4 = remainingAlphas[3];
            return removed;
        }
    }
}

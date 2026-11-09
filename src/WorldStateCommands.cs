using System;
using System.Collections.Generic;
using System.Text;
using ColossalFramework;
using UnityEngine;

namespace SkylinesAgentBridge
{
    public static class WorldStateCommands
    {
        public static CommandResult BuildNaturalResourcesJson(int step, int limit, int minIntensity, bool unlockedOnly)
        {
            step = Clamp(step, 1, 32);
            limit = Clamp(limit, 0, 100);
            minIntensity = Clamp(minIntensity, 0, 255);

            NaturalResourceManager resources = Singleton<NaturalResourceManager>.instance;
            GameAreaManager areas = Singleton<GameAreaManager>.instance;
            int resolution = NaturalResourceManager.RESOURCEGRID_RESOLUTION;
            float cellSize = NaturalResourceManager.RESOURCEGRID_CELL_SIZE;
            NaturalResourceManager.ResourceCell[] cells = resources.m_naturalResources;
            List<ResourceSample> samples = new List<ResourceSample>();

            for (int z = 0; z < resolution; z += step)
            {
                for (int x = 0; x < resolution; x += step)
                {
                    int index = z * resolution + x;
                    if (index < 0 || index >= cells.Length) continue;

                    float worldX = GridToWorld(x, resolution, cellSize);
                    float worldZ = GridToWorld(z, resolution, cellSize);
                    Vector3 position = new Vector3(worldX, 0f, worldZ);
                    bool unlocked = !areas.PointOutOfArea(position);
                    if (unlockedOnly && !unlocked) continue;

                    NaturalResourceManager.ResourceCell cell = cells[index];
                    AddSample(samples, "ore", cell.m_ore, worldX, worldZ, unlocked, minIntensity);
                    AddSample(samples, "oil", cell.m_oil, worldX, worldZ, unlocked, minIntensity);
                    AddSample(samples, "forest", cell.m_forest, worldX, worldZ, unlocked, minIntensity);
                    AddSample(samples, "fertility", cell.m_fertility, worldX, worldZ, unlocked, minIntensity);
                }
            }

            samples.Sort(ResourceSample.Compare);
            uint unlockedOre;
            uint unlockedOil;
            uint unlockedForest;
            uint unlockedFertility;
            uint unlockedWater;
            resources.CalculateUnlockedResources(out unlockedOre, out unlockedOil, out unlockedForest, out unlockedFertility, out unlockedWater);

            uint usedOre;
            uint usedOil;
            uint usedForest;
            uint usedFertility;
            resources.CalculateUsedResources(out usedOre, out usedOil, out usedForest, out usedFertility);

            StringBuilder json = new StringBuilder();
            json.Append("{\"ok\":true,\"scope\":\"").Append(unlockedOnly ? "unlocked" : "wholeMap").Append("\"");
            json.Append(",\"sampling\":{\"step\":").Append(step);
            json.Append(",\"cellSize\":").Append(JsonUtil.Number(cellSize));
            json.Append(",\"sampleSpacing\":").Append(JsonUtil.Number(cellSize * step));
            json.Append(",\"minIntensity\":").Append(minIntensity).Append("}");
            json.Append(",\"unlockedTotals\":{\"ore\":").Append(unlockedOre);
            json.Append(",\"oil\":").Append(unlockedOil);
            json.Append(",\"forest\":").Append(unlockedForest);
            json.Append(",\"fertility\":").Append(unlockedFertility);
            json.Append(",\"water\":").Append(unlockedWater).Append("}");
            json.Append(",\"usedTotals\":{\"ore\":").Append(usedOre);
            json.Append(",\"oil\":").Append(usedOil);
            json.Append(",\"forest\":").Append(usedForest);
            json.Append(",\"fertility\":").Append(usedFertility).Append("}");
            AppendResource(json, samples, "ore", limit, true);
            AppendResource(json, samples, "oil", limit, false);
            AppendResource(json, samples, "forest", limit, false);
            AppendResource(json, samples, "fertility", limit, false);
            json.Append("}}");
            return CommandResult.FromJson(json.ToString());
        }

        public static CommandResult BuildMapAnalysisJson(
            float minX,
            float minZ,
            float maxX,
            float maxZ,
            int resolution,
            float maxBuildSlope)
        {
            resolution = Clamp(resolution, 2, 64);
            if (maxX <= minX || maxZ <= minZ)
            {
                return CommandResult.Fail("maxX/maxZ must be greater than minX/minZ.");
            }
            if (maxX - minX > 20000f || maxZ - minZ > 20000f)
            {
                return CommandResult.Fail("Requested map bounds are too large.");
            }
            if (maxBuildSlope <= 0f) maxBuildSlope = 0.25f;

            TerrainManager terrain = Singleton<TerrainManager>.instance;
            GameAreaManager areas = Singleton<GameAreaManager>.instance;
            NaturalResourceManager resources = Singleton<NaturalResourceManager>.instance;
            WeatherManager weather = Singleton<WeatherManager>.instance;
            StringBuilder cells = new StringBuilder();
            bool first = true;
            int buildableCount = 0;
            float spacingX = (maxX - minX) / (resolution - 1);
            float spacingZ = (maxZ - minZ) / (resolution - 1);
            float slopeProbe = Mathf.Max(8f, Mathf.Min(spacingX, spacingZ) * 0.5f);

            for (int row = 0; row < resolution; row++)
            {
                float z = minZ + spacingZ * row;
                for (int column = 0; column < resolution; column++)
                {
                    float x = minX + spacingX * column;
                    Vector3 position = new Vector3(x, 0f, z);
                    float height = terrain.SampleRawHeightSmooth(position);
                    float dx = terrain.SampleRawHeightSmooth(new Vector3(x + slopeProbe, 0f, z)) -
                        terrain.SampleRawHeightSmooth(new Vector3(x - slopeProbe, 0f, z));
                    float dz = terrain.SampleRawHeightSmooth(new Vector3(x, 0f, z + slopeProbe)) -
                        terrain.SampleRawHeightSmooth(new Vector3(x, 0f, z - slopeProbe));
                    float slope = Mathf.Sqrt(dx * dx + dz * dz) / (slopeProbe * 2f);

                    float waterLevel;
                    float waterDepth;
                    Vector3 waterVelocity;
                    Vector3 waterSurface;
                    bool hasWater = terrain.SampleWaterData(new Vector2(x, z), out waterLevel, out waterDepth, out waterVelocity, out waterSurface);
                    bool unlocked = !areas.PointOutOfArea(position);
                    float windSpeed = weather.GetWindSpeed(new Vector3(x, height, z));
                    NaturalResourceManager.ResourceCell resource = SampleResource(resources, x, z);
                    bool buildable = unlocked && (!hasWater || waterDepth < 0.1f) && slope <= maxBuildSlope;
                    if (buildable) buildableCount++;

                    if (!first) cells.Append(",");
                    cells.Append("{\"row\":").Append(row).Append(",\"column\":").Append(column);
                    cells.Append(",\"position\":{\"x\":").Append(JsonUtil.Number(x)).Append(",\"z\":").Append(JsonUtil.Number(z)).Append("}");
                    cells.Append(",\"height\":").Append(JsonUtil.Number(height));
                    cells.Append(",\"slope\":").Append(JsonUtil.Number(slope));
                    cells.Append(",\"wind\":{\"speed\":").Append(JsonUtil.Number(windSpeed)).Append("}");
                    cells.Append(",\"unlocked\":").Append(JsonUtil.Bool(unlocked));
                    cells.Append(",\"buildable\":").Append(JsonUtil.Bool(buildable));
                    cells.Append(",\"water\":{\"present\":").Append(JsonUtil.Bool(hasWater));
                    cells.Append(",\"level\":").Append(JsonUtil.Number(waterLevel));
                    cells.Append(",\"depth\":").Append(JsonUtil.Number(waterDepth));
                    cells.Append(",\"velocity\":{\"x\":").Append(JsonUtil.Number(waterVelocity.x));
                    cells.Append(",\"z\":").Append(JsonUtil.Number(waterVelocity.z)).Append("}}");
                    cells.Append(",\"resources\":{\"ore\":").Append(resource.m_ore);
                    cells.Append(",\"oil\":").Append(resource.m_oil);
                    cells.Append(",\"forest\":").Append(resource.m_forest);
                    cells.Append(",\"fertility\":").Append(resource.m_fertility);
                    cells.Append(",\"pollution\":").Append(resource.m_pollution).Append("}}");
                    first = false;
                }
            }

            return CommandResult.FromJson("{\"ok\":true,\"bounds\":{\"minX\":" + JsonUtil.Number(minX) +
                ",\"minZ\":" + JsonUtil.Number(minZ) +
                ",\"maxX\":" + JsonUtil.Number(maxX) +
                ",\"maxZ\":" + JsonUtil.Number(maxZ) +
                "},\"resolution\":" + resolution +
                ",\"maxBuildSlope\":" + JsonUtil.Number(maxBuildSlope) +
                ",\"buildableSamples\":" + buildableCount +
                ",\"cells\":[" + cells.ToString() + "]}");
        }

        public static CommandResult BuildWindMapJson(
            float minX,
            float minZ,
            float maxX,
            float maxZ,
            int resolution,
            bool unlockedOnly)
        {
            resolution = Clamp(resolution, 2, 128);
            if (maxX <= minX || maxZ <= minZ)
            {
                return CommandResult.Fail("maxX/maxZ must be greater than minX/minZ.");
            }
            if (maxX - minX > 20000f || maxZ - minZ > 20000f)
            {
                return CommandResult.Fail("Requested wind-map bounds are too large.");
            }

            WeatherManager weather = Singleton<WeatherManager>.instance;
            TerrainManager terrain = Singleton<TerrainManager>.instance;
            GameAreaManager areas = Singleton<GameAreaManager>.instance;
            float spacingX = (maxX - minX) / (resolution - 1);
            float spacingZ = (maxZ - minZ) / (resolution - 1);
            StringBuilder cells = new StringBuilder();
            bool first = true;
            int sampleCount = 0;
            float minimum = float.MaxValue;
            float maximum = float.MinValue;
            double total = 0d;

            for (int row = 0; row < resolution; row++)
            {
                float z = minZ + spacingZ * row;
                for (int column = 0; column < resolution; column++)
                {
                    float x = minX + spacingX * column;
                    Vector3 position = new Vector3(x, 0f, z);
                    bool unlocked = !areas.PointOutOfArea(position);
                    if (unlockedOnly && !unlocked) continue;

                    position.y = terrain.SampleRawHeightSmooth(position);
                    float windSpeed = weather.GetWindSpeed(position);
                    if (windSpeed < minimum) minimum = windSpeed;
                    if (windSpeed > maximum) maximum = windSpeed;
                    total += windSpeed;
                    sampleCount++;

                    if (!first) cells.Append(",");
                    cells.Append("{\"row\":").Append(row).Append(",\"column\":").Append(column);
                    cells.Append(",\"position\":{\"x\":").Append(JsonUtil.Number(x));
                    cells.Append(",\"y\":").Append(JsonUtil.Number(position.y));
                    cells.Append(",\"z\":").Append(JsonUtil.Number(z)).Append("}");
                    cells.Append(",\"unlocked\":").Append(JsonUtil.Bool(unlocked));
                    cells.Append(",\"windSpeed\":").Append(JsonUtil.Number(windSpeed)).Append("}");
                    first = false;
                }
            }

            if (sampleCount == 0)
            {
                minimum = 0f;
                maximum = 0f;
            }
            return CommandResult.FromJson("{\"ok\":true,\"source\":\"WeatherManager.GetWindSpeed\"" +
                ",\"bounds\":{\"minX\":" + JsonUtil.Number(minX) +
                ",\"minZ\":" + JsonUtil.Number(minZ) +
                ",\"maxX\":" + JsonUtil.Number(maxX) +
                ",\"maxZ\":" + JsonUtil.Number(maxZ) +
                "},\"resolution\":" + resolution +
                ",\"unlockedOnly\":" + JsonUtil.Bool(unlockedOnly) +
                ",\"sampleCount\":" + sampleCount +
                ",\"summary\":{\"minimum\":" + JsonUtil.Number(minimum) +
                ",\"maximum\":" + JsonUtil.Number(maximum) +
                ",\"average\":" + JsonUtil.Number(sampleCount == 0 ? 0f : (float)(total / sampleCount)) +
                "},\"cells\":[" + cells.ToString() + "]}");
        }

        private static NaturalResourceManager.ResourceCell SampleResource(NaturalResourceManager manager, float x, float z)
        {
            int resolution = NaturalResourceManager.RESOURCEGRID_RESOLUTION;
            float size = NaturalResourceManager.RESOURCEGRID_CELL_SIZE;
            int column = Mathf.Clamp(Mathf.FloorToInt(x / size + resolution * 0.5f), 0, resolution - 1);
            int row = Mathf.Clamp(Mathf.FloorToInt(z / size + resolution * 0.5f), 0, resolution - 1);
            return manager.m_naturalResources[row * resolution + column];
        }

        private static void AddSample(List<ResourceSample> samples, string type, byte intensity, float x, float z, bool unlocked, int minimum)
        {
            if (intensity >= minimum)
            {
                samples.Add(new ResourceSample(type, intensity, x, z, unlocked));
            }
        }

        private static void AppendResource(StringBuilder json, List<ResourceSample> samples, string type, int limit, bool first)
        {
            if (first) json.Append(",\"hotspots\":{");
            else json.Append(",");
            json.Append("\"").Append(type).Append("\":[");
            int emitted = 0;
            for (int i = 0; i < samples.Count && emitted < limit; i++)
            {
                ResourceSample sample = samples[i];
                if (sample.Type != type) continue;
                if (emitted > 0) json.Append(",");
                json.Append("{\"intensity\":").Append(sample.Intensity);
                json.Append(",\"position\":{\"x\":").Append(JsonUtil.Number(sample.X));
                json.Append(",\"z\":").Append(JsonUtil.Number(sample.Z)).Append("}");
                json.Append(",\"unlocked\":").Append(JsonUtil.Bool(sample.Unlocked)).Append("}");
                emitted++;
            }
            json.Append("]");
        }

        private static float GridToWorld(int index, int resolution, float cellSize)
        {
            return (index - resolution * 0.5f + 0.5f) * cellSize;
        }

        private static int Clamp(int value, int minimum, int maximum)
        {
            return value < minimum ? minimum : (value > maximum ? maximum : value);
        }

        private sealed class ResourceSample
        {
            public readonly string Type;
            public readonly int Intensity;
            public readonly float X;
            public readonly float Z;
            public readonly bool Unlocked;

            public ResourceSample(string type, int intensity, float x, float z, bool unlocked)
            {
                Type = type;
                Intensity = intensity;
                X = x;
                Z = z;
                Unlocked = unlocked;
            }

            public static int Compare(ResourceSample left, ResourceSample right)
            {
                int intensity = right.Intensity.CompareTo(left.Intensity);
                if (intensity != 0) return intensity;
                int type = string.Compare(left.Type, right.Type, StringComparison.Ordinal);
                if (type != 0) return type;
                int x = left.X.CompareTo(right.X);
                return x != 0 ? x : left.Z.CompareTo(right.Z);
            }
        }
    }
}

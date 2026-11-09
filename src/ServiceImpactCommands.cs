using System;
using System.Reflection;
using System.Text;
using ColossalFramework;
using UnityEngine;

namespace SkylinesAgentBridge
{
    public static class ServiceImpactCommands
    {
        public static CommandResult PreviewServiceImpact(string body)
        {
            string prefabName = JsonUtil.GetString(body, "buildingPrefab", "").Trim();
            float x = JsonUtil.GetPointNumber(body, "position", "x", float.NaN);
            float z = JsonUtil.GetPointNumber(body, "position", "z", float.NaN);
            int lowCoverageThreshold = Clamp((int)JsonUtil.GetNumber(body, "lowCoverageThreshold", 32f), 0, 255);
            int limit = Clamp((int)JsonUtil.GetNumber(body, "limit", 100f), 0, 500);

            if (prefabName.Length == 0) return CommandResult.Fail("buildingPrefab is required.");
            if (float.IsNaN(x) || float.IsNaN(z)) return CommandResult.Fail("position.x and position.z are required.");

            BuildingInfo info = PrefabCollection<BuildingInfo>.FindLoaded(prefabName);
            if (info == null) return CommandResult.Fail("Building prefab not found: " + prefabName);

            CoverageProfile profile;
            if (!TryGetCoverageProfile(info, out profile))
            {
                return CommandResult.Fail("Unsupported service-impact prefab: " + prefabName +
                    ". Supported base-game facilities are healthcare, elementary/high-school/university education, fire, police, and deathcare.");
            }

            CommandResult placement = PlacementCommands.PreviewBuildingPlacement(body);
            if (!placement.Ok) return placement;

            Vector3 candidate = new Vector3(x, TerrainManager.instance.SampleRawHeightSmoothWithWater(new Vector3(x, 0f, z), false, 0f), z);
            BuildingManager buildings = Singleton<BuildingManager>.instance;
            ImmaterialResourceManager resources = Singleton<ImmaterialResourceManager>.instance;
            CitizenManager citizens = Singleton<CitizenManager>.instance;

            int residentialBuildings = 0;
            int residentsReached = 0;
            int lowCoverageResidentsReached = 0;
            int newlyCoveredResidents = 0;
            int overlapResidents = 0;
            int emitted = 0;
            long currentWeighted = 0;
            long projectedWeighted = 0;
            StringBuilder affected = new StringBuilder();

            for (int id = 1; id < buildings.m_buildings.m_size; id++)
            {
                Building building = buildings.m_buildings.m_buffer[id];
                if ((building.m_flags & Building.Flags.Created) == Building.Flags.None ||
                    (building.m_flags & Building.Flags.Deleted) != Building.Flags.None) continue;
                BuildingInfo buildingInfo = building.Info;
                if (buildingInfo == null || buildingInfo.m_class == null ||
                    buildingInfo.m_class.m_service != ItemClass.Service.Residential) continue;

                float distance = Vector3.Distance(candidate, building.m_position);
                if (distance > profile.Radius) continue;

                float falloff = profile.Radius <= 0f ? 0f : Mathf.Clamp01(1f - distance / profile.Radius);
                int estimatedAdded = Mathf.RoundToInt(profile.Accumulation * falloff);
                if (estimatedAdded <= 0) continue;

                int current;
                resources.CheckLocalResource(profile.Resource, building.m_position, out current);
                current = Clamp(current, 0, 255);
                int projected = Clamp(current + estimatedAdded, 0, 255);
                int residentCount = CountResidents(citizens, building.m_citizenUnits);

                residentialBuildings++;
                residentsReached += residentCount;
                currentWeighted += (long)current * residentCount;
                projectedWeighted += (long)projected * residentCount;
                if (current <= lowCoverageThreshold)
                {
                    lowCoverageResidentsReached += residentCount;
                    if (projected > lowCoverageThreshold) newlyCoveredResidents += residentCount;
                }
                else
                {
                    overlapResidents += residentCount;
                }

                if (emitted < limit)
                {
                    if (emitted > 0) affected.Append(",");
                    affected.Append("{\"buildingId\":").Append(id);
                    affected.Append(",\"residents\":").Append(residentCount);
                    affected.Append(",\"distance\":").Append(JsonUtil.Number(distance));
                    affected.Append(",\"currentCoverage\":").Append(current);
                    affected.Append(",\"estimatedAddedCoverage\":").Append(estimatedAdded);
                    affected.Append(",\"estimatedProjectedCoverage\":").Append(projected);
                    affected.Append(",\"newlyCovered\":").Append(JsonUtil.Bool(current <= lowCoverageThreshold && projected > lowCoverageThreshold));
                    affected.Append(",\"position\":{\"x\":").Append(JsonUtil.Number(building.m_position.x));
                    affected.Append(",\"z\":").Append(JsonUtil.Number(building.m_position.z)).Append("}}");
                    emitted++;
                }
            }

            float averageCurrent = residentsReached <= 0 ? 0f : (float)currentWeighted / residentsReached;
            float averageProjected = residentsReached <= 0 ? 0f : (float)projectedWeighted / residentsReached;
            int capacity = ReadInt(profile.AI, profile.CapacityFields);
            int vehicles = ReadInt(profile.AI, profile.VehicleFields);
            int noiseAccumulation = ReadInt(profile.AI, new string[] { "m_noiseAccumulation" });
            float noiseRadius = ReadNumber(profile.AI, new string[] { "m_noiseRadius" });
            bool unlocked = info.m_UnlockMilestone == null ||
                Singleton<UnlockManager>.instance.Unlocked(info.m_UnlockMilestone);

            return CommandResult.FromJson("{\"ok\":true,\"estimate\":true" +
                ",\"method\":\"radial-linear-estimate\"" +
                ",\"warning\":\"Actual CS1 service propagation and dispatch also depend on roads, pathfinding, budget, and simulation time. Verify after placement.\"" +
                ",\"buildingPrefab\":\"" + JsonUtil.Escape(prefabName) + "\"" +
                ",\"service\":\"" + JsonUtil.Escape(profile.ServiceName) + "\"" +
                ",\"coverageResource\":\"" + profile.Resource + "\"" +
                ",\"unlocked\":" + JsonUtil.Bool(unlocked) +
                ",\"position\":{\"x\":" + JsonUtil.Number(candidate.x) +
                ",\"y\":" + JsonUtil.Number(candidate.y) +
                ",\"z\":" + JsonUtil.Number(candidate.z) + "}" +
                ",\"facility\":{\"constructionCost\":" + info.GetConstructionCost() +
                ",\"maintenanceCost\":" + info.GetMaintenanceCost() +
                ",\"capacity\":" + capacity +
                ",\"serviceVehicles\":" + vehicles +
                ",\"serviceAccumulation\":" + profile.Accumulation +
                ",\"serviceRadius\":" + JsonUtil.Number(profile.Radius) +
                ",\"noiseAccumulation\":" + noiseAccumulation +
                ",\"noiseRadius\":" + JsonUtil.Number(noiseRadius) + "}" +
                ",\"impact\":{\"lowCoverageThreshold\":" + lowCoverageThreshold +
                ",\"residentialBuildingsReached\":" + residentialBuildings +
                ",\"residentsReached\":" + residentsReached +
                ",\"lowCoverageResidentsReached\":" + lowCoverageResidentsReached +
                ",\"newlyCoveredResidents\":" + newlyCoveredResidents +
                ",\"overlapResidents\":" + overlapResidents +
                ",\"averageCurrentCoverage\":" + JsonUtil.Number(averageCurrent) +
                ",\"estimatedAverageProjectedCoverage\":" + JsonUtil.Number(averageProjected) + "}" +
                ",\"placement\":" + placement.Json +
                ",\"affectedResidentialBuildings\":[" + affected.ToString() + "]}");
        }

        public static CommandResult BuildFacilityInfluenceJson(
            ushort buildingId,
            string serviceFilter,
            bool includeInactive,
            int limit)
        {
            limit = Clamp(limit, 0, 5000);
            BuildingManager buildings = Singleton<BuildingManager>.instance;
            ImmaterialResourceManager resources = Singleton<ImmaterialResourceManager>.instance;
            StringBuilder items = new StringBuilder();
            int total = 0;
            int emitted = 0;
            bool first = true;

            for (ushort id = 1; id < buildings.m_buildings.m_size; id++)
            {
                if (buildingId != 0 && id != buildingId) continue;
                Building building = buildings.m_buildings.m_buffer[id];
                if ((building.m_flags & Building.Flags.Created) == Building.Flags.None ||
                    (building.m_flags & Building.Flags.Deleted) != Building.Flags.None) continue;
                bool active = (building.m_flags & Building.Flags.Active) != Building.Flags.None;
                if (!includeInactive && !active) continue;

                BuildingInfo info = building.Info;
                if (info == null || info.m_class == null || info.m_buildingAI == null) continue;
                CoverageProfile profile;
                bool hasServiceCoverage = TryGetCoverageProfile(info, out profile);
                int noiseAccumulation = ReadInt(info.m_buildingAI, new string[] { "m_noiseAccumulation" });
                float noiseRadius = ReadNumber(info.m_buildingAI, new string[] { "m_noiseRadius" });
                bool hasNoise = noiseAccumulation > 0 && noiseRadius > 0f;
                if (!hasServiceCoverage && !hasNoise) continue;

                string classService = info.m_class.m_service.ToString();
                if (!string.IsNullOrEmpty(serviceFilter) &&
                    !string.Equals(classService, serviceFilter, StringComparison.OrdinalIgnoreCase) &&
                    !(hasServiceCoverage && string.Equals(profile.ServiceName, serviceFilter, StringComparison.OrdinalIgnoreCase)))
                {
                    continue;
                }

                total++;
                if (emitted >= limit) continue;
                if (!first) items.Append(",");

                Vector3 position = building.m_position;
                int liveNoise = 0;
                if (hasNoise)
                {
                    resources.CheckLocalResource(ImmaterialResourceManager.Resource.NoisePollution, position, out liveNoise);
                    liveNoise = Clamp(liveNoise, 0, 255);
                }

                items.Append("{\"buildingId\":").Append(id);
                items.Append(",\"prefab\":\"").Append(JsonUtil.Escape(info.name)).Append("\"");
                items.Append(",\"displayName\":\"").Append(JsonUtil.Escape(info.GetUncheckedLocalizedTitle())).Append("\"");
                items.Append(",\"classService\":\"").Append(JsonUtil.Escape(classService)).Append("\"");
                items.Append(",\"active\":").Append(JsonUtil.Bool(active));
                items.Append(",\"productionRate\":").Append(building.m_productionRate);
                items.Append(",\"position\":{\"x\":").Append(JsonUtil.Number(position.x));
                items.Append(",\"y\":").Append(JsonUtil.Number(position.y));
                items.Append(",\"z\":").Append(JsonUtil.Number(position.z)).Append("}");

                if (hasServiceCoverage)
                {
                    int liveCoverage;
                    resources.CheckLocalResource(profile.Resource, position, out liveCoverage);
                    liveCoverage = Clamp(liveCoverage, 0, 255);
                    items.Append(",\"serviceInfluence\":{\"resource\":\"").Append(profile.Resource).Append("\"");
                    items.Append(",\"service\":\"").Append(JsonUtil.Escape(profile.ServiceName)).Append("\"");
                    items.Append(",\"accumulation\":").Append(profile.Accumulation);
                    items.Append(",\"radius\":").Append(JsonUtil.Number(profile.Radius));
                    items.Append(",\"liveValueAtSource\":").Append(liveCoverage);
                    AppendCircleBounds(items, position, profile.Radius);
                    items.Append("}");
                }
                else
                {
                    items.Append(",\"serviceInfluence\":null");
                }

                if (hasNoise)
                {
                    items.Append(",\"noiseInfluence\":{\"resource\":\"NoisePollution\"");
                    items.Append(",\"accumulation\":").Append(noiseAccumulation);
                    items.Append(",\"radius\":").Append(JsonUtil.Number(noiseRadius));
                    items.Append(",\"liveValueAtSource\":").Append(liveNoise);
                    AppendCircleBounds(items, position, noiseRadius);
                    items.Append("}");
                }
                else
                {
                    items.Append(",\"noiseInfluence\":null");
                }
                items.Append("}");
                emitted++;
                first = false;
            }

            return CommandResult.FromJson("{\"ok\":true,\"buildingId\":" + buildingId +
                ",\"serviceFilter\":\"" + JsonUtil.Escape(serviceFilter) + "\"" +
                ",\"includeInactive\":" + JsonUtil.Bool(includeInactive) +
                ",\"total\":" + total +
                ",\"returned\":" + emitted +
                ",\"limit\":" + limit +
                ",\"interpretation\":{\"declaredRadiiAreSourceParameters\":true," +
                "\"liveCoverageRequiresServiceCoverageGrid\":true}" +
                ",\"facilities\":[" + items.ToString() + "]}");
        }

        private static void AppendCircleBounds(StringBuilder json, Vector3 position, float radius)
        {
            json.Append(",\"bounds\":{\"minX\":").Append(JsonUtil.Number(position.x - radius));
            json.Append(",\"minZ\":").Append(JsonUtil.Number(position.z - radius));
            json.Append(",\"maxX\":").Append(JsonUtil.Number(position.x + radius));
            json.Append(",\"maxZ\":").Append(JsonUtil.Number(position.z + radius)).Append("}");
        }

        private static bool TryGetCoverageProfile(BuildingInfo info, out CoverageProfile profile)
        {
            BuildingAI ai = info.m_buildingAI;
            HospitalAI hospital = ai as HospitalAI;
            if (hospital != null)
            {
                profile = new CoverageProfile(ai, "HealthCare", ImmaterialResourceManager.Resource.HealthCare,
                    ReadInt(ai, new string[] { "m_healthCareAccumulation" }),
                    ReadNumber(ai, new string[] { "m_healthCareRadius" }),
                    new string[] { "m_patientCapacity" },
                    new string[] { "m_ambulanceCount" });
                return profile.Valid;
            }

            SchoolAI school = ai as SchoolAI;
            if (school != null)
            {
                string service;
                ImmaterialResourceManager.Resource resource;
                if (school.GetEducationLevel1()) { service = "EducationElementary"; resource = ImmaterialResourceManager.Resource.EducationElementary; }
                else if (school.GetEducationLevel2()) { service = "EducationHighSchool"; resource = ImmaterialResourceManager.Resource.EducationHighSchool; }
                else if (school.GetEducationLevel3()) { service = "EducationUniversity"; resource = ImmaterialResourceManager.Resource.EducationUniversity; }
                else { profile = null; return false; }
                profile = new CoverageProfile(ai, service, resource,
                    ReadInt(ai, new string[] { "m_educationAccumulation" }),
                    ReadNumber(ai, new string[] { "m_educationRadius" }),
                    new string[] { "m_studentCount" },
                    new string[0]);
                return profile.Valid;
            }

            FireStationAI fire = ai as FireStationAI;
            if (fire != null)
            {
                profile = new CoverageProfile(ai, "FireDepartment", ImmaterialResourceManager.Resource.FireDepartment,
                    ReadInt(ai, new string[] { "m_fireDepartmentAccumulation" }),
                    ReadNumber(ai, new string[] { "m_fireDepartmentRadius" }),
                    new string[0],
                    new string[] { "m_fireTruckCount" });
                return profile.Valid;
            }

            PoliceStationAI police = ai as PoliceStationAI;
            if (police != null)
            {
                profile = new CoverageProfile(ai, "PoliceDepartment", ImmaterialResourceManager.Resource.PoliceDepartment,
                    ReadInt(ai, new string[] { "m_policeDepartmentAccumulation" }),
                    ReadNumber(ai, new string[] { "m_policeDepartmentRadius" }),
                    new string[] { "m_jailCapacity" },
                    new string[] { "m_policeCarCount" });
                return profile.Valid;
            }

            CemeteryAI cemetery = ai as CemeteryAI;
            if (cemetery != null)
            {
                profile = new CoverageProfile(ai, "DeathCare", ImmaterialResourceManager.Resource.DeathCare,
                    ReadInt(ai, new string[] { "m_deathCareAccumulation" }),
                    ReadNumber(ai, new string[] { "m_deathCareRadius" }),
                    new string[] { "m_graveCount" },
                    new string[] { "m_hearseCount" });
                return profile.Valid;
            }

            profile = null;
            return false;
        }

        private static int CountResidents(CitizenManager manager, uint unitId)
        {
            int count = 0;
            int guard = 0;
            while (unitId != 0 && guard++ < 524288)
            {
                CitizenUnit unit = manager.m_units.m_buffer[unitId];
                if ((unit.m_flags & CitizenUnit.Flags.Home) != CitizenUnit.Flags.None)
                {
                    if (unit.m_citizen0 != 0) count++;
                    if (unit.m_citizen1 != 0) count++;
                    if (unit.m_citizen2 != 0) count++;
                    if (unit.m_citizen3 != 0) count++;
                    if (unit.m_citizen4 != 0) count++;
                }
                unitId = unit.m_nextUnit;
            }
            return count;
        }

        private static int ReadInt(object instance, string[] names)
        {
            return Mathf.RoundToInt(ReadNumber(instance, names));
        }

        private static float ReadNumber(object instance, string[] names)
        {
            if (instance == null) return 0f;
            for (Type type = instance.GetType(); type != null; type = type.BaseType)
            {
                for (int i = 0; i < names.Length; i++)
                {
                    FieldInfo field = type.GetField(names[i], BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly);
                    if (field == null) continue;
                    object value = field.GetValue(instance);
                    if (value == null) continue;
                    try { return Convert.ToSingle(value); } catch { }
                }
            }
            return 0f;
        }

        private static int Clamp(int value, int minimum, int maximum)
        {
            return value < minimum ? minimum : (value > maximum ? maximum : value);
        }

        private sealed class CoverageProfile
        {
            public readonly BuildingAI AI;
            public readonly string ServiceName;
            public readonly ImmaterialResourceManager.Resource Resource;
            public readonly int Accumulation;
            public readonly float Radius;
            public readonly string[] CapacityFields;
            public readonly string[] VehicleFields;
            public bool Valid { get { return Accumulation > 0 && Radius > 0f; } }

            public CoverageProfile(
                BuildingAI ai,
                string serviceName,
                ImmaterialResourceManager.Resource resource,
                int accumulation,
                float radius,
                string[] capacityFields,
                string[] vehicleFields)
            {
                AI = ai;
                ServiceName = serviceName;
                Resource = resource;
                Accumulation = accumulation;
                Radius = radius;
                CapacityFields = capacityFields;
                VehicleFields = vehicleFields;
            }
        }
    }
}

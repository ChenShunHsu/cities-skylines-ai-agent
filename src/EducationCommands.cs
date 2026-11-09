using System;
using System.Text;
using ColossalFramework;

namespace SkylinesAgentBridge
{
    public static class EducationCommands
    {
        public static CommandResult BuildEducationJson()
        {
            District city = Singleton<DistrictManager>.instance.m_districts.m_buffer[0];
            StringBuilder facilities = new StringBuilder();
            bool first = true;
            int elementaryFacilities = 0;
            int highSchoolFacilities = 0;
            int universityFacilities = 0;

            BuildingManager manager = Singleton<BuildingManager>.instance;
            for (int id = 1; id < manager.m_buildings.m_size; id++)
            {
                Building building = manager.m_buildings.m_buffer[id];
                if ((building.m_flags & Building.Flags.Created) == Building.Flags.None) continue;
                BuildingInfo info = building.Info;
                SchoolAI school = info == null ? null : info.m_buildingAI as SchoolAI;
                if (school == null) continue;

                string level = EducationLevel(school);
                if (level == "Elementary") elementaryFacilities++;
                else if (level == "HighSchool") highSchoolFacilities++;
                else if (level == "University") universityFacilities++;

                int students;
                int capacity;
                int education;
                school.GetStudentCount((ushort)id, ref building, out students, out capacity, out education);
                if (!first) facilities.Append(",");
                facilities.Append("{\"id\":").Append(id);
                facilities.Append(",\"prefab\":\"").Append(JsonUtil.Escape(info.name)).Append("\"");
                facilities.Append(",\"name\":\"").Append(JsonUtil.Escape(manager.GetBuildingName((ushort)id, default(InstanceID)))).Append("\"");
                facilities.Append(",\"level\":\"").Append(level).Append("\"");
                facilities.Append(",\"active\":").Append(JsonUtil.Bool((building.m_flags & Building.Flags.Active) != Building.Flags.None));
                facilities.Append(",\"students\":").Append(students);
                facilities.Append(",\"capacity\":").Append(capacity);
                facilities.Append(",\"utilization\":").Append(JsonUtil.Number(capacity <= 0 ? 0f : (float)students / capacity));
                facilities.Append(",\"educationAccumulation\":").Append(education);
                facilities.Append(",\"position\":{\"x\":").Append(JsonUtil.Number(building.m_position.x));
                facilities.Append(",\"y\":").Append(JsonUtil.Number(building.m_position.y));
                facilities.Append(",\"z\":").Append(JsonUtil.Number(building.m_position.z)).Append("}}");
                first = false;
            }

            return CommandResult.FromJson("{\"ok\":true,\"levels\":{" +
                "\"elementary\":{\"eligible\":" + city.GetEducation1Need() +
                ",\"capacity\":" + city.GetEducation1Capacity() +
                ",\"facilities\":" + elementaryFacilities + "}," +
                "\"highSchool\":{\"eligible\":" + city.GetEducation2Need() +
                ",\"capacity\":" + city.GetEducation2Capacity() +
                ",\"facilities\":" + highSchoolFacilities + "}," +
                "\"university\":{\"eligible\":" + city.GetEducation3Need() +
                ",\"capacity\":" + city.GetEducation3Capacity() +
                ",\"facilities\":" + universityFacilities + "}}," +
                "\"facilities\":[" + facilities.ToString() + "]}");
        }

        public static CommandResult PlaceEducationFacility(string body)
        {
            string level = JsonUtil.GetString(body, "level", "").Trim();
            string prefabName = JsonUtil.GetString(body, "buildingPrefab", "").Trim();
            bool dryRun = JsonUtil.GetBool(body, "dryRun", false);
            float x = JsonUtil.GetPointNumber(body, "position", "x", float.NaN);
            float z = JsonUtil.GetPointNumber(body, "position", "z", float.NaN);
            float angle = JsonUtil.GetNumber(body, "angleDegrees", 0f);

            if (float.IsNaN(x) || float.IsNaN(z)) return CommandResult.Fail("position.x and position.z are required.");
            string normalizedLevel;
            string defaultPrefab;
            if (!TryEducationLevel(level, out normalizedLevel, out defaultPrefab))
            {
                return CommandResult.Fail("level must be Elementary, HighSchool, or University.");
            }
            if (prefabName.Length == 0) prefabName = defaultPrefab;

            BuildingInfo info = PrefabCollection<BuildingInfo>.FindLoaded(prefabName);
            SchoolAI school = info == null ? null : info.m_buildingAI as SchoolAI;
            if (school == null) return CommandResult.Fail("Building prefab is not an education facility: " + prefabName);
            string actualLevel = EducationLevel(school);
            if (!string.Equals(actualLevel, normalizedLevel, StringComparison.OrdinalIgnoreCase))
            {
                return CommandResult.Fail("Building prefab level is " + actualLevel + ", not " + normalizedLevel + ".");
            }

            string request = "{\"dryRun\":" + JsonUtil.Bool(dryRun) +
                ",\"buildingPrefab\":\"" + JsonUtil.Escape(prefabName) + "\"" +
                ",\"position\":{\"x\":" + JsonUtil.Number(x) + ",\"z\":" + JsonUtil.Number(z) + "}" +
                ",\"angleDegrees\":" + JsonUtil.Number(angle) + "}";
            return dryRun
                ? PlacementCommands.PreviewBuildingPlacement(request)
                : BuildingCommands.PlaceBuilding(request);
        }

        private static string EducationLevel(SchoolAI school)
        {
            if (school.GetEducationLevel1()) return "Elementary";
            if (school.GetEducationLevel2()) return "HighSchool";
            if (school.GetEducationLevel3()) return "University";
            return "Other";
        }

        private static bool TryEducationLevel(string value, out string normalized, out string prefab)
        {
            if (string.Equals(value, "Elementary", StringComparison.OrdinalIgnoreCase) ||
                string.Equals(value, "ElementarySchool", StringComparison.OrdinalIgnoreCase) ||
                string.Equals(value, "Primary", StringComparison.OrdinalIgnoreCase))
            {
                normalized = "Elementary";
                prefab = "Elementary School";
                return true;
            }
            if (string.Equals(value, "HighSchool", StringComparison.OrdinalIgnoreCase) ||
                string.Equals(value, "High School", StringComparison.OrdinalIgnoreCase) ||
                string.Equals(value, "Secondary", StringComparison.OrdinalIgnoreCase))
            {
                normalized = "HighSchool";
                prefab = "High School";
                return true;
            }
            if (string.Equals(value, "University", StringComparison.OrdinalIgnoreCase))
            {
                normalized = "University";
                prefab = "University";
                return true;
            }
            normalized = "";
            prefab = "";
            return false;
        }
    }
}

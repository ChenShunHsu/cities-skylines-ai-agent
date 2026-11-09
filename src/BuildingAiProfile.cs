using System;
using System.Reflection;
using System.Text;

namespace SkylinesAgentBridge
{
    // The tuning numbers of a building's AI (capacities, radii, vehicle counts, processing
    // and electricity rates), read generically so that service options can be compared
    // without one hand-written profile per AI type.
    public static class BuildingAiProfile
    {
        public static string Build(BuildingInfo info)
        {
            if (info == null || info.m_buildingAI == null)
            {
                return "null";
            }

            Type type = info.m_buildingAI.GetType();
            StringBuilder fields = new StringBuilder();
            bool first = true;
            foreach (FieldInfo field in type.GetFields(BindingFlags.Public | BindingFlags.Instance))
            {
                if (!field.Name.StartsWith("m_", StringComparison.Ordinal))
                {
                    continue;
                }

                object value = field.GetValue(info.m_buildingAI);
                string text;
                if (value is int)
                {
                    text = ((int)value).ToString(System.Globalization.CultureInfo.InvariantCulture);
                }
                else if (value is float)
                {
                    text = JsonUtil.Number((float)value);
                }
                else if (value is bool)
                {
                    text = JsonUtil.Bool((bool)value);
                }
                else if (value is Enum)
                {
                    text = "\"" + JsonUtil.Escape(value.ToString()) + "\"";
                }
                else
                {
                    continue;
                }

                if (!first)
                {
                    fields.Append(",");
                }

                fields.Append("\"").Append(JsonUtil.Escape(field.Name.Substring(2))).Append("\":").Append(text);
                first = false;
            }

            return "{\"aiType\":\"" + JsonUtil.Escape(type.Name) + "\",\"fields\":{" + fields + "}}";
        }
    }
}

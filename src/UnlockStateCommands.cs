using System;
using System.Text;
using ColossalFramework;

namespace SkylinesAgentBridge
{
    public static class UnlockStateCommands
    {
        public static CommandResult BuildUnlocksJson()
        {
            UnlockManager manager = Singleton<UnlockManager>.instance;
            StringBuilder zones = new StringBuilder();
            StringBuilder services = new StringBuilder();
            StringBuilder subServices = new StringBuilder();
            StringBuilder infoModes = new StringBuilder();
            AppendEnumUnlocks<ItemClass.Zone>(zones, delegate(ItemClass.Zone value) { return manager.Unlocked(value); });
            AppendEnumUnlocks<ItemClass.Service>(services, delegate(ItemClass.Service value) { return manager.Unlocked(value); });
            AppendEnumUnlocks<ItemClass.SubService>(subServices, delegate(ItemClass.SubService value) { return manager.Unlocked(value); });
            AppendEnumUnlocks<InfoManager.InfoMode>(infoModes, delegate(InfoManager.InfoMode value) { return manager.Unlocked(value); });
            MilestoneInfo current = manager.GetCurrentMilestone();
            return CommandResult.FromJson("{\"ok\":true,\"currentMilestone\":\"" +
                JsonUtil.Escape(current == null ? "" : current.name) +
                "\",\"nextMilestone\":" + NextMilestoneJson(manager) +
                ",\"zones\":[" + zones.ToString() +
                "],\"services\":[" + services.ToString() +
                "],\"subServices\":[" + subServices.ToString() +
                "],\"infoModes\":[" + infoModes.ToString() + "]}");
        }

        // GetCurrentMilestone returns the first milestone not yet passed, so "currentMilestone" is
        // the next target. Its native progress says what that milestone still needs (for the base
        // progression: a population target scaled to the map), which tells an agent when the next
        // map tile and unlocks arrive.
        internal static string NextMilestoneJson(UnlockManager manager)
        {
            MilestoneInfo next = manager.GetCurrentMilestone();
            if (next == null)
            {
                return "null";
            }
            FastList<MilestoneInfo.ProgressInfo> subProgress;
            MilestoneInfo.ProgressInfo total = next.GetLocalizedProgress(out subProgress);
            StringBuilder json = new StringBuilder();
            json.Append("{\"name\":\"").Append(JsonUtil.Escape(next.name)).Append("\"");
            json.Append(",\"passed\":").Append(JsonUtil.Bool(total.m_passed));
            json.Append(",\"requirements\":[");
            bool first = true;
            if (subProgress != null && subProgress.m_size > 0)
            {
                for (int i = 0; i < subProgress.m_size; i++)
                {
                    AppendProgress(json, subProgress.m_buffer[i], ref first);
                }
            }
            else
            {
                AppendProgress(json, total, ref first);
            }
            json.Append("]}");
            return json.ToString();
        }

        private static void AppendProgress(StringBuilder json, MilestoneInfo.ProgressInfo progress, ref bool first)
        {
            if (!first) json.Append(",");
            json.Append("{\"description\":\"").Append(JsonUtil.Escape(progress.m_description ?? "")).Append("\"");
            json.Append(",\"progress\":\"").Append(JsonUtil.Escape(progress.m_progress ?? "")).Append("\"");
            json.Append(",\"current\":").Append(JsonUtil.Number(progress.m_current));
            json.Append(",\"target\":").Append(JsonUtil.Number(progress.m_max));
            json.Append(",\"passed\":").Append(JsonUtil.Bool(progress.m_passed)).Append("}");
            first = false;
        }

        private static void AppendEnumUnlocks<T>(StringBuilder json, Func<T, bool> check)
        {
            Array values = Enum.GetValues(typeof(T));
            bool first = true;
            for (int i = 0; i < values.Length; i++)
            {
                T value = (T)values.GetValue(i);
                bool unlocked;
                try { unlocked = check(value); }
                catch { unlocked = false; }
                if (!first) json.Append(",");
                json.Append("{\"name\":\"").Append(JsonUtil.Escape(value.ToString())).Append("\"");
                json.Append(",\"value\":").Append(Convert.ToInt32(value));
                json.Append(",\"unlocked\":").Append(JsonUtil.Bool(unlocked)).Append("}");
                first = false;
            }
        }
    }
}

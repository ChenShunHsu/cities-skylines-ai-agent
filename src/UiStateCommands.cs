using System.Text;
using ColossalFramework;
using ColossalFramework.UI;

namespace SkylinesAgentBridge
{
    public static class UiStateCommands
    {
        // Diagnostic: select (and optionally camera-follow) an entity the way a player click does,
        // so release paths can be verified while the game UI holds a reference to the target.
        public static CommandResult SelectInstance(string body)
        {
            string entityType = JsonUtil.GetString(body, "entityType", "");
            ushort id = (ushort)JsonUtil.GetNumber(body, "id", 0f);
            bool follow = JsonUtil.GetBool(body, "follow", false);
            if (id == 0) return CommandResult.Fail("id is required.");

            InstanceID target = InstanceID.Empty;
            if (entityType == "building") target.Building = id;
            else if (entityType == "netSegment") target.NetSegment = id;
            else if (entityType == "netNode") target.NetNode = id;
            else return CommandResult.Fail("entityType must be building, netSegment or netNode.");

            InstanceManager instances = Singleton<InstanceManager>.instance;
            bool selected = instances.SelectInstance(target);
            bool followed = follow && instances.FollowInstance(target);
            if (!selected) return CommandResult.Fail(entityType + " #" + id + " is not a valid instance.");
            return CommandResult.FromJson("{\"ok\":true,\"entityType\":\"" + entityType + "\",\"id\":" + id +
                ",\"selected\":true,\"followed\":" + (followed ? "true" : "false") + "}");
        }

        public static CommandResult BuildToolbarUnlocksJson()
        {
            UnlockManager unlocks = Singleton<UnlockManager>.instance;
            UIComponent toolbar = ToolsModifierControl.mainToolbar == null
                ? null
                : ToolsModifierControl.mainToolbar.component;
            StringBuilder items = new StringBuilder();
            bool first = true;
            AppendService(items, ref first, toolbar, unlocks, ItemClass.Service.Road);
            AppendService(items, ref first, toolbar, unlocks, ItemClass.Service.Electricity);
            AppendService(items, ref first, toolbar, unlocks, ItemClass.Service.Water);
            AppendService(items, ref first, toolbar, unlocks, ItemClass.Service.Garbage);
            AppendService(items, ref first, toolbar, unlocks, ItemClass.Service.HealthCare);
            AppendService(items, ref first, toolbar, unlocks, ItemClass.Service.FireDepartment);
            AppendService(items, ref first, toolbar, unlocks, ItemClass.Service.PoliceDepartment);
            AppendService(items, ref first, toolbar, unlocks, ItemClass.Service.Education);

            return CommandResult.FromJson(
                "{\"ok\":true,\"toolbarFound\":" + JsonUtil.Bool(toolbar != null) +
                ",\"source\":\"GameMainToolbar button state plus UnlockManager and ToolsModifierControl\"" +
                ",\"services\":[" + items.ToString() + "]}");
        }

        public static bool IsServiceToolbarUsable(ItemClass.Service service)
        {
            if (!Singleton<UnlockManager>.instance.Unlocked(service) ||
                !ToolsModifierControl.IsUnlocked(service))
            {
                return false;
            }
            UIComponent toolbar = ToolsModifierControl.mainToolbar == null
                ? null
                : ToolsModifierControl.mainToolbar.component;
            UIButton button = FindServiceButton(toolbar, service);
            return button != null &&
                button.isEnabled &&
                button.isVisible &&
                button.isInteractive;
        }

        private static void AppendService(
            StringBuilder json,
            ref bool first,
            UIComponent toolbar,
            UnlockManager unlocks,
            ItemClass.Service service)
        {
            UIButton button = FindServiceButton(toolbar, service);
            bool simulationUnlocked = unlocks.Unlocked(service);
            bool toolUnlocked = ToolsModifierControl.IsUnlocked(service);
            bool uiUsable = button != null &&
                button.isEnabled &&
                button.isVisible &&
                button.isInteractive;

            if (!first) json.Append(",");
            json.Append("{\"service\":\"").Append(service.ToString()).Append("\"");
            json.Append(",\"simulationUnlocked\":").Append(JsonUtil.Bool(simulationUnlocked));
            json.Append(",\"toolUnlocked\":").Append(JsonUtil.Bool(toolUnlocked));
            json.Append(",\"buttonFound\":").Append(JsonUtil.Bool(button != null));
            json.Append(",\"buttonName\":\"").Append(JsonUtil.Escape(button == null ? "" : button.name)).Append("\"");
            json.Append(",\"isEnabled\":").Append(JsonUtil.Bool(button != null && button.isEnabled));
            json.Append(",\"isVisible\":").Append(JsonUtil.Bool(button != null && button.isVisible));
            json.Append(",\"isInteractive\":").Append(JsonUtil.Bool(button != null && button.isInteractive));
            json.Append(",\"uiUsable\":").Append(JsonUtil.Bool(uiUsable));
            json.Append(",\"consistent\":").Append(JsonUtil.Bool(
                simulationUnlocked == toolUnlocked &&
                (!simulationUnlocked || uiUsable)));
            json.Append("}");
            first = false;
        }

        private static UIButton FindServiceButton(
            UIComponent toolbar,
            ItemClass.Service service)
        {
            if (toolbar == null) return null;
            string[] names;
            switch (service)
            {
                case ItemClass.Service.Road:
                    names = new string[] { "Road", "Roads" };
                    break;
                case ItemClass.Service.Water:
                    names = new string[] { "Water", "WaterAndSewage" };
                    break;
                case ItemClass.Service.HealthCare:
                    names = new string[] { "HealthCare", "Healthcare" };
                    break;
                case ItemClass.Service.PoliceDepartment:
                    names = new string[] { "PoliceDepartment", "Police" };
                    break;
                default:
                    names = new string[] { service.ToString() };
                    break;
            }
            for (int i = 0; i < names.Length; i++)
            {
                UIButton button = toolbar.Find(names[i]) as UIButton;
                if (button != null) return button;
            }
            return null;
        }
    }
}

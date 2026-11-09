using System.Text;
using ColossalFramework;
using ColossalFramework.UI;

namespace SkylinesAgentBridge
{
    // Capability inspection only. Never selects the interactive TerrainTool,
    // touches height buffers, buys soil, changes money, or unlocks a feature.
    public static class TerrainEditingState
    {
        public static CommandResult Inspect()
        {
            UnlockManager unlocks = Singleton<UnlockManager>.instance;
            TerrainManager terrain = Singleton<TerrainManager>.instance;
            bool unlocked = unlocks.Unlocked(UnlockManager.Feature.Landscaping);
            UIComponent toolbar = ToolsModifierControl.mainToolbar == null
                ? null : ToolsModifierControl.mainToolbar.component;
            UIButton button = toolbar == null ? null : toolbar.Find("Landscaping") as UIButton;
            bool usable = button != null && button.isEnabled && button.isVisible && button.isInteractive;
            int feature = (int)UnlockManager.Feature.Landscaping;
            MilestoneInfo milestone = null;
            if (unlocks.m_properties != null && unlocks.m_properties.m_FeatureMilestones != null &&
                feature < unlocks.m_properties.m_FeatureMilestones.Length)
                milestone = unlocks.m_properties.m_FeatureMilestones[feature];
            StringBuilder json = new StringBuilder();
            json.Append("{\"ok\":true,\"planningOnly\":true,\"mutationAuthorized\":false");
            json.Append(",\"feature\":\"Landscaping\",\"nativeUnlocked\":").Append(JsonUtil.Bool(unlocked));
            json.Append(",\"requiredMilestone\":\"").Append(JsonUtil.Escape(milestone == null ? "" : milestone.name)).Append("\"");
            json.Append(",\"toolbarButtonFound\":").Append(JsonUtil.Bool(button != null));
            json.Append(",\"toolbarUsable\":").Append(JsonUtil.Bool(usable));
            json.Append(",\"nativeUiAvailable\":").Append(JsonUtil.Bool(unlocked && usable));
            json.Append(",\"soilBufferNativeUnits\":").Append(terrain.DirtBuffer);
            json.Append(",\"nativeHeightUnitPriceInternal\":");
            if (terrain.m_properties == null) json.Append("null");
            else json.Append(terrain.m_properties.m_dirtPrice);
            json.Append(",\"soilUnitsAreNotCubicMeters\":true,\"internalCurrencyUnitsPerDisplayUnit\":100");
            json.Append(",\"nativeModes\":[\"Shift\",\"Level\",\"Soften\",\"Slope\"]");
            json.Append(",\"bridgeTerrainMutationImplemented\":false");
            json.Append(",\"limitations\":[\"Capability is not a terrain-edit preview\",\"No affected-area, flood, soil-balance or complete cost assessment\",\"Road surface prediction remains separate from terrain editing\"]}");
            return CommandResult.FromJson(json.ToString());
        }
    }
}

using System.Reflection;
using ColossalFramework;

namespace SkylinesAgentBridge
{
    // What the game's own bulldozer gives back: three quarters of the construction cost of something
    // built recently (SimulationManager.IsRecentBuildIndex), nothing for anything older. Asked of the
    // bulldozer itself, so the refund always follows the game's rule.
    internal static class BulldozeRefund
    {
        private const BindingFlags Private = BindingFlags.Instance | BindingFlags.NonPublic;

        internal static int Segment(ushort segmentId)
        {
            return Ask("GetSegmentRefundAmount", segmentId);
        }

        internal static int Building(ushort buildingId)
        {
            return Ask("GetBuildingRefundAmount", buildingId);
        }

        private static int Ask(string method, ushort id)
        {
            BulldozeTool tool = ToolsModifierControl.GetTool<BulldozeTool>();
            MethodInfo amount = typeof(BulldozeTool).GetMethod(method, Private);
            if (tool == null || amount == null)
            {
                return 0;
            }
            return (int)amount.Invoke(tool, new object[] { id });
        }

        internal static void Pay(int amount, ItemClass itemClass)
        {
            if (amount > 0 && itemClass != null)
            {
                Singleton<EconomyManager>.instance.AddResource(EconomyManager.Resource.RefundAmount, amount, itemClass);
            }
        }
    }
}

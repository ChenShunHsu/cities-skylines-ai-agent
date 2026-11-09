using System;
using System.Reflection;
using System.Text;
using ColossalFramework;
using UnityEngine;

namespace SkylinesAgentBridge
{
    // Base-game junction settings (Traffic Routes > Junctions): traffic lights per junction node and
    // stop signs per road end. The native RoadBaseAI.ClickNodeButton only acts while that info view
    // is open, so this applies the same flag changes behind the same CanEnable* gates.
    public static class JunctionCommands
    {
        // Both gates are private on RoadBaseAI; a missing method fails closed.
        private static readonly MethodInfo CanEnableTrafficLightsMethod = typeof(RoadBaseAI).GetMethod(
            "CanEnableTrafficLights", BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.Public);
        private static readonly MethodInfo CanEnableYieldMethod = typeof(RoadBaseAI).GetMethod(
            "CanEnableYield", BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.Public);

        public static CommandResult BuildJunctionJson(ushort nodeId)
        {
            NetManager net = Singleton<NetManager>.instance;
            string error = ValidateJunction(net, nodeId);
            return error != null ? CommandResult.Fail(error) : CommandResult.FromJson("{\"ok\":true," + JunctionFields(net, nodeId) + "}");
        }

        public static CommandResult SetJunctionControl(string body)
        {
            ushort nodeId = (ushort)JsonUtil.GetNumber(body, "nodeId", 0f);
            string control = JsonUtil.GetString(body, "control", "");
            bool enabled = JsonUtil.GetBool(body, "enabled", false);
            NetManager net = Singleton<NetManager>.instance;
            string error = ValidateJunction(net, nodeId);
            if (error != null) return CommandResult.Fail(error);
            RoadBaseAI ai = (RoadBaseAI)net.m_nodes.m_buffer[nodeId].Info.m_netAI;

            bool changed;
            if (control == "trafficLights")
            {
                if (!NativeGate(CanEnableTrafficLightsMethod, ai, net, nodeId))
                {
                    return CommandResult.Fail("The game does not allow traffic-light changes at node " + nodeId + ".");
                }
                changed = HasLights(net, nodeId) != enabled;
                if (changed)
                {
                    NetNode.Flags flags = net.m_nodes.m_buffer[nodeId].m_flags;
                    flags = enabled ? flags | NetNode.Flags.TrafficLights : flags & ~NetNode.Flags.TrafficLights;
                    // CustomTrafficLights keeps UpdateNodeFlags from restoring the automatic choice.
                    net.m_nodes.m_buffer[nodeId].m_flags = flags | NetNode.Flags.CustomTrafficLights;
                    ai.UpdateNodeFlags(nodeId, ref net.m_nodes.m_buffer[nodeId]);
                    if (HasLights(net, nodeId) != enabled)
                    {
                        return CommandResult.Fail("Traffic lights at node " + nodeId + " did not take the requested state.");
                    }
                }
            }
            else if (control == "stopSign")
            {
                ushort segmentId = (ushort)JsonUtil.GetNumber(body, "segmentId", 0f);
                if (!ConnectsTo(net, nodeId, segmentId))
                {
                    return CommandResult.Fail("Segment " + segmentId + " does not end at node " + nodeId + ".");
                }
                if (!NativeGate(CanEnableYieldMethod, ai, net, nodeId))
                {
                    return CommandResult.Fail("The game does not allow stop signs at node " + nodeId + "; junctions with traffic lights cannot have them.");
                }
                NetSegment.Flags flag = YieldFlag(net, nodeId, segmentId);
                changed = ((net.m_segments.m_buffer[segmentId].m_flags & flag) != NetSegment.Flags.None) != enabled;
                if (changed)
                {
                    net.m_segments.m_buffer[segmentId].m_flags ^= flag;
                    net.m_segments.m_buffer[segmentId].UpdateLanes(segmentId, true);
                }
            }
            else
            {
                return CommandResult.Fail("control must be trafficLights or stopSign.");
            }
            return CommandResult.FromJson("{\"ok\":true,\"control\":\"" + JsonUtil.Escape(control) + "\",\"enabled\":" +
                JsonUtil.Bool(enabled) + ",\"changed\":" + JsonUtil.Bool(changed) + "," + JunctionFields(net, nodeId) + "}");
        }

        private static string ValidateJunction(NetManager net, ushort nodeId)
        {
            if (nodeId == 0 || nodeId >= net.m_nodes.m_buffer.Length ||
                (net.m_nodes.m_buffer[nodeId].m_flags & NetNode.Flags.Created) == NetNode.Flags.None)
            {
                return "Network node does not exist: " + nodeId;
            }
            NetNode node = net.m_nodes.m_buffer[nodeId];
            if (!(node.Info != null && node.Info.m_netAI is RoadBaseAI))
            {
                return "Node " + nodeId + " is not a road node.";
            }
            if ((node.m_flags & NetNode.Flags.Junction) == NetNode.Flags.None)
            {
                return "Node " + nodeId + " is not a junction.";
            }
            return null;
        }

        private static bool NativeGate(MethodInfo gate, RoadBaseAI ai, NetManager net, ushort nodeId)
        {
            if (gate == null) return false;
            object result = gate.Invoke(ai, new object[] { nodeId, net.m_nodes.m_buffer[nodeId] });
            return result is bool && (bool)result;
        }

        private static bool HasLights(NetManager net, ushort nodeId)
        {
            return (net.m_nodes.m_buffer[nodeId].m_flags & NetNode.Flags.TrafficLights) != NetNode.Flags.None;
        }

        private static bool ConnectsTo(NetManager net, ushort nodeId, ushort segmentId)
        {
            if (segmentId == 0) return false;
            for (int i = 0; i < 8; i++)
            {
                if (net.m_nodes.m_buffer[nodeId].GetSegment(i) == segmentId) return true;
            }
            return false;
        }

        private static NetSegment.Flags YieldFlag(NetManager net, ushort nodeId, ushort segmentId)
        {
            return net.m_segments.m_buffer[segmentId].m_startNode == nodeId ? NetSegment.Flags.YieldStart : NetSegment.Flags.YieldEnd;
        }

        private static string JunctionFields(NetManager net, ushort nodeId)
        {
            NetNode node = net.m_nodes.m_buffer[nodeId];
            RoadBaseAI ai = (RoadBaseAI)node.Info.m_netAI;
            StringBuilder json = new StringBuilder();
            json.Append("\"nodeId\":").Append(nodeId);
            json.Append(",\"prefab\":\"").Append(JsonUtil.Escape(node.Info.name)).Append("\"");
            json.Append(",\"position\":{\"x\":").Append(JsonUtil.Number(node.m_position.x)).Append(",\"z\":").Append(JsonUtil.Number(node.m_position.z)).Append("}");
            json.Append(",\"trafficLights\":").Append(JsonUtil.Bool(HasLights(net, nodeId)));
            json.Append(",\"customTrafficLights\":").Append(JsonUtil.Bool((node.m_flags & NetNode.Flags.CustomTrafficLights) != NetNode.Flags.None));
            json.Append(",\"canToggleTrafficLights\":").Append(JsonUtil.Bool(NativeGate(CanEnableTrafficLightsMethod, ai, net, nodeId)));
            json.Append(",\"canUseStopSigns\":").Append(JsonUtil.Bool(NativeGate(CanEnableYieldMethod, ai, net, nodeId)));
            json.Append(",\"segments\":[");
            bool first = true;
            for (int i = 0; i < 8; i++)
            {
                ushort segmentId = node.GetSegment(i);
                if (segmentId == 0) continue;
                NetSegment segment = net.m_segments.m_buffer[segmentId];
                ushort other = segment.m_startNode == nodeId ? segment.m_endNode : segment.m_startNode;
                Vector3 toward = net.m_nodes.m_buffer[other].m_position - node.m_position;
                if (!first) json.Append(",");
                json.Append("{\"segmentId\":").Append(segmentId);
                json.Append(",\"prefab\":\"").Append(JsonUtil.Escape(segment.Info == null ? "" : segment.Info.name)).Append("\"");
                json.Append(",\"density\":").Append(segment.m_trafficDensity);
                json.Append(",\"stopSign\":").Append(JsonUtil.Bool((segment.m_flags & YieldFlag(net, nodeId, segmentId)) != NetSegment.Flags.None));
                json.Append(",\"headingDegrees\":").Append(JsonUtil.Number(Mathf.Atan2(toward.z, toward.x) * Mathf.Rad2Deg)).Append("}");
                first = false;
            }
            return json.Append("]").ToString();
        }
    }
}

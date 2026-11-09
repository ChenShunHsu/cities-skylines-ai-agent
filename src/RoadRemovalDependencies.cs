using System;
using System.Collections.Generic;
using System.Text;

namespace SkylinesAgentBridge
{
    // Pure graph evidence. Neither a removal policy nor a traffic simulation.
    public static class RoadRemovalDependencies
    {
        public sealed class Edge
        {
            public ushort Id, Start, End;
        }

        public sealed class Access
        {
            public ushort Building, Segment;
            public bool Service;
        }

        public sealed class Side
        {
            public HashSet<ushort> Nodes = new HashSet<ushort>();
            public HashSet<ushort> Segments = new HashSet<ushort>();
            public List<ushort> Buildings = new List<ushort>();
            public int Services, OutsideNodes;
        }

        public sealed class Report
        {
            public Side Start, End;
            public bool Separated;
            public int StartDegree, EndDegree, UnresolvedAccess;
            public List<ushort> DirectBuildings = new List<ushort>();
            public int DirectServices;

            public string ToJson()
            {
                return "{\"supported\":true,\"source\":\"liveRoadGraphAndNativeBuildingAccess\"," +
                    "\"graphScope\":\"allRoadSubservicesUndirected\",\"operationalImpactComplete\":false," +
                    "\"removalAuthorized\":false,\"buildingIdListLimit\":64,\"separatedAfterRemoval\":" + Bool(Separated) +
                    ",\"startDegreeBefore\":" + StartDegree + ",\"endDegreeBefore\":" + EndDegree +
                    ",\"directAccessBuildings\":" + Ids(DirectBuildings) +
                    ",\"directAccessBuildingCount\":" + DirectBuildings.Count +
                    ",\"directAccessServiceCount\":" + DirectServices +
                    ",\"unresolvedRoadAccessBuildingCountCitywide\":" + UnresolvedAccess +
                    ",\"startSide\":" + SideJson(Start) + ",\"endSide\":" + SideJson(End) +
                    ",\"limitations\":[\"Native access associations are dependencies, not proof of lost service.\"," +
                    "\"Directed lanes, vehicle paths, transport lines, utility propagation and service coverage are not simulated.\"," +
                    "\"Zero listed buildings does not authorize removal or prove zero operational impact.\"]}";
            }
        }

        public static Report Analyze(ushort targetId, IList<Edge> edges,
            HashSet<ushort> outside, IList<Access> access)
        {
            Dictionary<ushort, Edge> byId = new Dictionary<ushort, Edge>();
            Dictionary<ushort, List<Edge>> adjacency = new Dictionary<ushort, List<Edge>>();
            foreach (Edge edge in edges)
            {
                if (edge.Id == 0 || edge.Start == 0 || edge.End == 0 || byId.ContainsKey(edge.Id))
                    throw new ArgumentException("Invalid or duplicate road graph edge");
                byId.Add(edge.Id, edge);
                Add(adjacency, edge.Start, edge);
                if (edge.End != edge.Start) Add(adjacency, edge.End, edge);
            }
            if (!byId.ContainsKey(targetId)) throw new ArgumentException("Target road absent");
            Edge target = byId[targetId];
            Report result = new Report();
            result.StartDegree = adjacency[target.Start].Count;
            result.EndDegree = adjacency[target.End].Count;
            result.Start = Walk(target.Start, targetId, adjacency, outside);
            result.End = Walk(target.End, targetId, adjacency, outside);
            result.Separated = !result.Start.Nodes.Contains(target.End);
            HashSet<ushort> buildings = new HashSet<ushort>();
            foreach (Access item in access)
            {
                if (item.Building == 0 || !buildings.Add(item.Building))
                    throw new ArgumentException("Invalid or duplicate building access record");
                if (item.Segment == 0 || !byId.ContainsKey(item.Segment))
                {
                    result.UnresolvedAccess++;
                    continue;
                }
                if (item.Segment == targetId)
                {
                    result.DirectBuildings.Add(item.Building);
                    if (item.Service) result.DirectServices++;
                    continue;
                }
                AddAccess(result.Start, item);
                AddAccess(result.End, item);
            }
            return result;
        }

        private static void Add(Dictionary<ushort, List<Edge>> graph, ushort node, Edge edge)
        {
            if (!graph.ContainsKey(node)) graph[node] = new List<Edge>();
            graph[node].Add(edge);
        }

        private static Side Walk(ushort root, ushort excluded,
            Dictionary<ushort, List<Edge>> graph, HashSet<ushort> outside)
        {
            Side side = new Side();
            Queue<ushort> queue = new Queue<ushort>();
            queue.Enqueue(root);
            side.Nodes.Add(root);
            while (queue.Count > 0)
            {
                ushort node = queue.Dequeue();
                if (outside.Contains(node)) side.OutsideNodes++;
                foreach (Edge edge in graph[node])
                {
                    if (edge.Id == excluded) continue;
                    side.Segments.Add(edge.Id);
                    ushort next = edge.Start == node ? edge.End : edge.Start;
                    if (side.Nodes.Add(next)) queue.Enqueue(next);
                }
            }
            return side;
        }

        private static void AddAccess(Side side, Access access)
        {
            if (!side.Segments.Contains(access.Segment)) return;
            side.Buildings.Add(access.Building);
            if (access.Service) side.Services++;
        }

        private static string SideJson(Side side)
        {
            return "{\"nodeCount\":" + side.Nodes.Count + ",\"segmentCount\":" + side.Segments.Count +
                ",\"outsideNodeCount\":" + side.OutsideNodes + ",\"accessBuildingCount\":" + side.Buildings.Count +
                ",\"accessServiceCount\":" + side.Services + ",\"accessBuildings\":" + Ids(side.Buildings) + "}";
        }

        private static string Ids(List<ushort> values)
        {
            List<ushort> sorted = new List<ushort>(values);
            sorted.Sort();
            StringBuilder json = new StringBuilder("[");
            for (int i = 0; i < sorted.Count && i < 64; i++)
            {
                if (i != 0) json.Append(',');
                json.Append(sorted[i]);
            }
            return json.Append(']').ToString();
        }

        private static string Bool(bool value) { return value ? "true" : "false"; }
    }
}

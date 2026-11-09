using System;
using System.Collections.Generic;
using System.Text;
using ColossalFramework;
using UnityEngine;

namespace SkylinesAgentBridge
{
    // Native "Traffic Routes" view for one road segment: which vehicles plan to use it,
    // where they come from and go to, and which segments they enter from and leave by.
    // Vehicles fix their route when the trip starts and never reroute around congestion,
    // so this is the evidence for whether a bypass, upgrade, or junction change helps.
    public static class TrafficRouteCommands
    {
        private const int MaxPathUnitsPerVehicle = 512;
        private const float CellSize = 256f;

        public static CommandResult BuildSegmentRoutesJson(ushort segmentId, int limit, string vehicleAI)
        {
            NetManager net = Singleton<NetManager>.instance;
            if (segmentId == 0 || segmentId >= net.m_segments.m_buffer.Length ||
                (net.m_segments.m_buffer[segmentId].m_flags & NetSegment.Flags.Created) == NetSegment.Flags.None)
            {
                return CommandResult.Fail("Road segment does not exist: " + segmentId);
            }
            limit = Mathf.Clamp(limit, 1, 100);
            NetSegment target = net.m_segments.m_buffer[segmentId];
            Vector3 startPosition = net.m_nodes.m_buffer[target.m_startNode].m_position;
            Vector3 endPosition = net.m_nodes.m_buffer[target.m_endNode].m_position;

            Vehicle[] vehicles = Singleton<VehicleManager>.instance.m_vehicles.m_buffer;
            Building[] buildings = Singleton<BuildingManager>.instance.m_buildings.m_buffer;
            PathUnit[] units = Singleton<PathManager>.instance.m_pathUnits.m_buffer;
            Dictionary<string, int> byType = new Dictionary<string, int>();
            Dictionary<int, int> entering = new Dictionary<int, int>();
            Dictionary<int, int> exiting = new Dictionary<int, int>();
            Dictionary<long, int> origins = new Dictionary<long, int>();
            Dictionary<long, int> destinations = new Dictionary<long, int>();
            // Origin and destination cells counted together: separate lists cannot say who goes where.
            Dictionary<string, int> pairs = new Dictionary<string, int>();
            int scanned = 0;
            int matched = 0;
            int towardEnd = 0;
            int towardStart = 0;

            for (int i = 1; i < vehicles.Length; i++)
            {
                Vehicle vehicle = vehicles[i];
                if ((vehicle.m_flags & Vehicle.Flags.Created) == 0 || vehicle.m_leadingVehicle != 0 || vehicle.m_path == 0) continue;
                VehicleInfo info = vehicle.Info;
                if (info == null || info.m_vehicleType != VehicleInfo.VehicleType.Car) continue;
                scanned++;

                ushort before;
                ushort after;
                Vector3 lastPosition;
                if (!FindSegmentInPath(units, net, vehicle.m_path, vehicle.m_pathPositionIndex >> 1, segmentId,
                    out before, out after, out lastPosition))
                {
                    continue;
                }
                string type = info.m_vehicleAI == null ? "Unknown" : info.m_vehicleAI.GetType().Name;
                // e.g. PassengerCarAI alone: who could ride transit instead, without service vehicles
                if (!string.IsNullOrEmpty(vehicleAI) && type != vehicleAI) continue;
                matched++;
                Increment(byType, type);
                Increment(entering, before);
                Increment(exiting, after);
                Vector3 origin = vehicle.m_sourceBuilding != 0 ? buildings[vehicle.m_sourceBuilding].m_position : vehicle.GetLastFramePosition();
                Vector3 destination = vehicle.m_targetBuilding != 0 ? buildings[vehicle.m_targetBuilding].m_position : lastPosition;
                Increment(origins, CellKey(origin));
                Increment(destinations, CellKey(destination));
                Increment(pairs, CellKey(origin) + ">" + CellKey(destination));
                // Heading through the segment: compare the entry side with the segment's two ends.
                if (before != 0)
                {
                    NetSegment entry = net.m_segments.m_buffer[before];
                    bool fromStart = entry.m_startNode == target.m_startNode || entry.m_endNode == target.m_startNode;
                    if (fromStart) towardEnd++;
                    else towardStart++;
                }
            }

            StringBuilder json = new StringBuilder();
            json.Append("{\"ok\":true,\"segmentId\":").Append(segmentId);
            json.Append(",\"prefab\":\"").Append(JsonUtil.Escape(target.Info == null ? "" : target.Info.name)).Append("\"");
            json.Append(",\"density\":").Append(target.m_trafficDensity);
            json.Append(",\"startNodeId\":").Append(target.m_startNode).Append(",\"endNodeId\":").Append(target.m_endNode);
            json.Append(",\"start\":").Append(Point(startPosition)).Append(",\"end\":").Append(Point(endPosition));
            json.Append(",\"vehiclesScanned\":").Append(scanned);
            json.Append(",\"vehiclesPlanningToUse\":").Append(matched);
            json.Append(",\"vehicleAIFilter\":\"").Append(JsonUtil.Escape(vehicleAI ?? "")).Append("\"");
            json.Append(",\"direction\":{\"towardEnd\":").Append(towardEnd).Append(",\"towardStart\":").Append(towardStart).Append("}");
            json.Append(",\"byVehicleAI\":").Append(StringCounts(byType));
            json.Append(",\"enteringFromSegments\":").Append(SegmentCounts(entering, limit));
            json.Append(",\"exitingToSegments\":").Append(SegmentCounts(exiting, limit));
            json.Append(",\"originCells\":").Append(CellCounts(origins, limit));
            json.Append(",\"destinationCells\":").Append(CellCounts(destinations, limit));
            json.Append(",\"originDestinationPairs\":").Append(PairCounts(pairs, limit));
            json.Append(",\"cellSize\":").Append(JsonUtil.Number(CellSize));
            json.Append(",\"interpretation\":{\"remainingPathsOnly\":true,\"segmentZeroMeansTripStartsOrEndsHere\":true,");
            json.Append("\"originUsesSourceBuildingElseCurrentPosition\":true}}");
            return CommandResult.FromJson(json.ToString());
        }

        // Walk the remaining path; report the segments just before and after the target.
        private static bool FindSegmentInPath(PathUnit[] units, NetManager net, uint pathId, int startIndex, ushort segmentId,
            out ushort before, out ushort after, out Vector3 lastPosition)
        {
            before = 0;
            after = 0;
            lastPosition = Vector3.zero;
            bool found = false;
            ushort previous = 0;
            ushort lastSegment = 0;
            int guard = 0;
            int index = startIndex;
            while (pathId != 0 && pathId < units.Length && guard++ < MaxPathUnitsPerVehicle)
            {
                PathUnit unit = units[pathId];
                for (int i = index; i < unit.m_positionCount; i++)
                {
                    ushort segment = unit.GetPosition(i).m_segment;
                    if (segment == lastSegment) continue;
                    if (found && after == 0) after = segment;
                    if (!found && segment == segmentId)
                    {
                        found = true;
                        before = previous;
                    }
                    previous = segment;
                    lastSegment = segment;
                }
                index = 0;
                pathId = unit.m_nextPathUnit;
            }
            if (lastSegment != 0)
            {
                NetSegment last = net.m_segments.m_buffer[lastSegment];
                lastPosition = (net.m_nodes.m_buffer[last.m_startNode].m_position + net.m_nodes.m_buffer[last.m_endNode].m_position) * 0.5f;
            }
            return found;
        }

        private static long CellKey(Vector3 position)
        {
            long x = (long)Mathf.FloorToInt(position.x / CellSize);
            long z = (long)Mathf.FloorToInt(position.z / CellSize);
            return (x << 32) ^ (z & 0xffffffffL);
        }

        private static void Increment<T>(Dictionary<T, int> counts, T key)
        {
            int count;
            counts.TryGetValue(key, out count);
            counts[key] = count + 1;
        }

        private static List<KeyValuePair<T, int>> Ranked<T>(Dictionary<T, int> counts)
        {
            List<KeyValuePair<T, int>> items = new List<KeyValuePair<T, int>>(counts);
            items.Sort(delegate (KeyValuePair<T, int> left, KeyValuePair<T, int> right) { return right.Value.CompareTo(left.Value); });
            return items;
        }

        private static string StringCounts(Dictionary<string, int> counts)
        {
            StringBuilder json = new StringBuilder("{");
            bool first = true;
            foreach (KeyValuePair<string, int> item in Ranked(counts))
            {
                if (!first) json.Append(",");
                json.Append("\"").Append(JsonUtil.Escape(item.Key)).Append("\":").Append(item.Value);
                first = false;
            }
            return json.Append("}").ToString();
        }

        private static string SegmentCounts(Dictionary<int, int> counts, int limit)
        {
            NetManager net = Singleton<NetManager>.instance;
            StringBuilder json = new StringBuilder("[");
            List<KeyValuePair<int, int>> items = Ranked(counts);
            for (int i = 0; i < items.Count && i < limit; i++)
            {
                if (i > 0) json.Append(",");
                json.Append("{\"segmentId\":").Append(items[i].Key).Append(",\"vehicles\":").Append(items[i].Value);
                if (items[i].Key != 0)
                {
                    NetSegment segment = net.m_segments.m_buffer[items[i].Key];
                    Vector3 middle = (net.m_nodes.m_buffer[segment.m_startNode].m_position + net.m_nodes.m_buffer[segment.m_endNode].m_position) * 0.5f;
                    json.Append(",\"prefab\":\"").Append(JsonUtil.Escape(segment.Info == null ? "" : segment.Info.name)).Append("\"");
                    json.Append(",\"density\":").Append(segment.m_trafficDensity);
                    json.Append(",\"position\":").Append(Point(middle));
                }
                json.Append("}");
            }
            return json.Append("]").ToString();
        }

        private static string CellCounts(Dictionary<long, int> counts, int limit)
        {
            StringBuilder json = new StringBuilder("[");
            List<KeyValuePair<long, int>> items = Ranked(counts);
            for (int i = 0; i < items.Count && i < limit; i++)
            {
                if (i > 0) json.Append(",");
                json.Append("{\"center\":").Append(CellCenter(items[i].Key)).Append(",\"vehicles\":").Append(items[i].Value).Append("}");
            }
            return json.Append("]").ToString();
        }

        private static string PairCounts(Dictionary<string, int> counts, int limit)
        {
            StringBuilder json = new StringBuilder("[");
            List<KeyValuePair<string, int>> items = Ranked(counts);
            for (int i = 0; i < items.Count && i < limit; i++)
            {
                string[] cells = items[i].Key.Split('>');
                if (i > 0) json.Append(",");
                json.Append("{\"origin\":").Append(CellCenter(long.Parse(cells[0])));
                json.Append(",\"destination\":").Append(CellCenter(long.Parse(cells[1])));
                json.Append(",\"vehicles\":").Append(items[i].Value).Append("}");
            }
            return json.Append("]").ToString();
        }

        private static string CellCenter(long key)
        {
            int x = (int)(key >> 32);
            int z = (int)(key & 0xffffffffL);
            return "{\"x\":" + JsonUtil.Number((x + 0.5f) * CellSize) + ",\"z\":" + JsonUtil.Number((z + 0.5f) * CellSize) + "}";
        }

        private static string Point(Vector3 position)
        {
            return "{\"x\":" + JsonUtil.Number(position.x) + ",\"z\":" + JsonUtil.Number(position.z) + "}";
        }
    }
}

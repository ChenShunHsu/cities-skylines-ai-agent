using System;
using System.Collections.Generic;
using System.Reflection;
using System.Text;
using ColossalFramework;
using ColossalFramework.Math;
using UnityEngine;

namespace SkylinesAgentBridge
{
    public static class TransportCommands
    {
        public static CommandResult BuildTransportLinesJson(int limit)
        {
            if (limit < 0) limit = 0;
            if (limit > 512) limit = 512;

            TransportManager manager = TransportManager.instance;
            StringBuilder items = new StringBuilder();
            int total = 0;
            int emitted = 0;
            bool first = true;

            for (ushort lineId = 1; lineId < manager.m_lines.m_buffer.Length; lineId++)
            {
                TransportLine line = manager.m_lines.m_buffer[lineId];
                if ((line.m_flags & TransportLine.Flags.Created) == TransportLine.Flags.None)
                {
                    continue;
                }

                total++;
                if (emitted >= limit)
                {
                    continue;
                }

                if (!first)
                {
                    items.Append(",");
                }

                TransportInfo info = line.Info;
                VehicleInfo vehicleInfo = null;
                try { vehicleInfo = line.GetLineVehicle(lineId); } catch { }
                string name = "";
                try { name = manager.GetLineName(lineId); } catch { }
                string defaultVehicleName = "";
                int defaultVehicleIndex = -1;
                try { defaultVehicleName = manager.GetDefaultLineVehicleName(lineId); } catch { }
                try { defaultVehicleIndex = manager.GetDefaultLineVehicleIndex(lineId); } catch { }

                items.Append("{\"id\":").Append(lineId);
                items.Append(",\"name\":\"").Append(JsonUtil.Escape(name)).Append("\"");
                items.Append(",\"transportType\":\"").Append(JsonUtil.Escape(info == null ? "" : info.m_transportType.ToString())).Append("\"");
                items.Append(",\"vehicleType\":\"").Append(JsonUtil.Escape(info == null ? "" : info.m_vehicleType.ToString())).Append("\"");
                items.Append(",\"vehicleReason\":\"").Append(JsonUtil.Escape(info == null ? "" : info.m_vehicleReason.ToString())).Append("\"");
                items.Append(",\"citizenReason\":\"").Append(JsonUtil.Escape(info == null ? "" : info.m_citizenReason.ToString())).Append("\"");
                items.Append(",\"defaultVehicleDistance\":").Append(JsonUtil.Number(info == null ? 0f : info.m_defaultVehicleDistance));
                items.Append(",\"defaultVehicleIndex\":").Append(defaultVehicleIndex);
                items.Append(",\"defaultVehicleName\":\"").Append(JsonUtil.Escape(defaultVehicleName)).Append("\"");
                items.Append(",\"lineVehiclePrefab\":\"").Append(JsonUtil.Escape(vehicleInfo == null ? "" : vehicleInfo.name)).Append("\"");
                items.Append(",\"flags\":\"").Append(JsonUtil.Escape(line.m_flags.ToString())).Append("\"");
                items.Append(",\"budget\":").Append(line.m_budget);
                items.Append(",\"ticketPrice\":").Append(line.m_ticketPrice);
                items.Append(",\"averageInterval\":").Append(line.m_averageInterval);
                items.Append(",\"buildingId\":").Append(line.m_building);
                items.Append(",\"firstVehicleId\":").Append(line.m_vehicles);
                items.Append(",\"targetVehicles\":").Append(line.CalculateTargetVehicleCount());
                items.Append(",\"stops\":").Append(line.CountStops(lineId));
                items.Append(",\"vehicles\":").Append(CountVehicles(line.m_vehicles));
                // The line panel's ridership is the weekly passenger data, not
                // CalculatePassengerCount, which read 0 on a line the panel showed
                // carrying 190 passengers.
                items.Append(",\"passengersOnBoard\":").Append(line.CalculatePassengerCount(lineId));
                items.Append(",\"weeklyPassengers\":{\"residents\":").Append(line.m_passengers.m_residentPassengers.m_averageCount);
                items.Append(",\"tourists\":").Append(line.m_passengers.m_touristPassengers.m_averageCount);
                items.Append(",\"total\":").Append(line.m_passengers.m_residentPassengers.m_averageCount +
                    line.m_passengers.m_touristPassengers.m_averageCount);
                items.Append(",\"carOwning\":").Append(line.m_passengers.m_carOwningPassengers.m_averageCount).Append("}");
                items.Append(",\"totalLength\":").Append(JsonUtil.Number(line.m_totalLength));
                items.Append(",\"color\":\"#").Append(line.m_color.r.ToString("X2")).Append(line.m_color.g.ToString("X2")).Append(line.m_color.b.ToString("X2")).Append("\"");
                items.Append(",\"stopNodes\":[");
                AppendStops(items, lineId, line);
                items.Append("]}");

                first = false;
                emitted++;
            }

            return CommandResult.FromJson("{\"ok\":true,\"total\":" + total +
                ",\"returned\":" + emitted +
                ",\"limit\":" + limit +
                ",\"lines\":[" + items.ToString() + "]}");
        }

        public static CommandResult BuildTransportVehiclesJson(ushort lineId, int limit)
        {
            if (limit < 0) limit = 0;
            if (limit > 1024) limit = 1024;

            TransportManager transport = TransportManager.instance;
            VehicleManager vehicles = VehicleManager.instance;
            StringBuilder items = new StringBuilder();
            int total = 0;
            int emitted = 0;
            bool first = true;

            if (lineId != 0)
            {
                TransportLine line = transport.m_lines.m_buffer[lineId];
                if ((line.m_flags & TransportLine.Flags.Created) == TransportLine.Flags.None)
                {
                    return CommandResult.Fail("Transport line was not found: " + lineId);
                }
            }

            for (ushort vehicleId = 1; vehicleId < vehicles.m_vehicles.m_buffer.Length; vehicleId++)
            {
                Vehicle vehicle = vehicles.m_vehicles.m_buffer[vehicleId];
                if ((vehicle.m_flags & Vehicle.Flags.Created) == 0)
                {
                    continue;
                }
                if (lineId != 0 && vehicle.m_transportLine != lineId)
                {
                    continue;
                }

                total++;
                if (emitted >= limit)
                {
                    continue;
                }

                if (!first)
                {
                    items.Append(",");
                }

                VehicleInfo info = vehicle.Info;
                Vector3 smooth = vehicle.GetSmoothPosition(vehicleId);
                Vector3 frame0 = vehicle.m_frame0.m_position;
                Vector3 velocity = vehicle.m_frame0.m_velocity;
                float speed = velocity.magnitude;

                items.Append("{\"id\":").Append(vehicleId);
                items.Append(",\"transportLine\":").Append(vehicle.m_transportLine);
                items.Append(",\"prefab\":\"").Append(JsonUtil.Escape(info == null ? "" : info.name)).Append("\"");
                items.Append(",\"flags\":\"").Append(JsonUtil.Escape(vehicle.m_flags.ToString())).Append("\"");
                items.Append(",\"flags2\":\"").Append(JsonUtil.Escape(vehicle.m_flags2.ToString())).Append("\"");
                items.Append(",\"path\":").Append(vehicle.m_path);
                items.Append(",\"pathPositionIndex\":").Append(vehicle.m_pathPositionIndex);
                items.Append(",\"waitCounter\":").Append(vehicle.m_waitCounter);
                items.Append(",\"blockCounter\":").Append(vehicle.m_blockCounter);
                items.Append(",\"sourceBuilding\":").Append(vehicle.m_sourceBuilding);
                items.Append(",\"targetBuilding\":").Append(vehicle.m_targetBuilding);
                items.Append(",\"leadingVehicle\":").Append(vehicle.m_leadingVehicle);
                items.Append(",\"trailingVehicle\":").Append(vehicle.m_trailingVehicle);
                items.Append(",\"nextLineVehicle\":").Append(vehicle.m_nextLineVehicle);
                items.Append(",\"speed\":").Append(JsonUtil.Number(speed));
                items.Append(",\"position\":{\"x\":").Append(JsonUtil.Number(smooth.x));
                items.Append(",\"y\":").Append(JsonUtil.Number(smooth.y));
                items.Append(",\"z\":").Append(JsonUtil.Number(smooth.z)).Append("}");
                items.Append(",\"framePosition\":{\"x\":").Append(JsonUtil.Number(frame0.x));
                items.Append(",\"y\":").Append(JsonUtil.Number(frame0.y));
                items.Append(",\"z\":").Append(JsonUtil.Number(frame0.z)).Append("}");
                items.Append(",\"velocity\":{\"x\":").Append(JsonUtil.Number(velocity.x));
                items.Append(",\"y\":").Append(JsonUtil.Number(velocity.y));
                items.Append(",\"z\":").Append(JsonUtil.Number(velocity.z)).Append("}}");

                first = false;
                emitted++;
            }

            return CommandResult.FromJson("{\"ok\":true,\"lineId\":" + lineId +
                ",\"total\":" + total +
                ",\"returned\":" + emitted +
                ",\"limit\":" + limit +
                ",\"vehicles\":[" + items.ToString() + "]}");
        }

        public static CommandResult BuildTransportLineAnomaliesJson(int limit)
        {
            if (limit < 0) limit = 0;
            if (limit > 1024) limit = 1024;

            TransportManager manager = TransportManager.instance;
            NetManager net = NetManager.instance;
            StringBuilder items = new StringBuilder();
            StringBuilder counts = new StringBuilder();
            Dictionary<string, int> countByType = new Dictionary<string, int>();
            int total = 0;
            int emitted = 0;
            bool first = true;

            for (ushort lineId = 1; lineId < manager.m_lines.m_buffer.Length; lineId++)
            {
                TransportLine line = manager.m_lines.m_buffer[lineId];
                if ((line.m_flags & TransportLine.Flags.Created) == TransportLine.Flags.None)
                {
                    continue;
                }
                if ((line.m_flags & TransportLine.Flags.Temporary) != TransportLine.Flags.None)
                {
                    continue;
                }

                TransportInfo info = line.Info;
                string name = "";
                try { name = manager.GetLineName(lineId); } catch { }
                int stopCount = line.CountStops(lineId);
                int vehicleCount = CountVehicles(line.m_vehicles);
                int targetVehicles = line.CalculateTargetVehicleCount();

                if (stopCount < 2)
                {
                    AddLineAnomaly("tooFewStops", lineId, name, info, line, 0, net.m_nodes.m_buffer[0], "", stopCount, vehicleCount, targetVehicles, ref total, ref emitted, limit, countByType, items, ref first);
                }

                if ((line.m_flags & TransportLine.Flags.Complete) == TransportLine.Flags.None ||
                    (line.m_flags & TransportLine.Flags.CompleteSet) == TransportLine.Flags.None)
                {
                    AddLineAnomaly("incompleteLine", lineId, name, info, line, 0, net.m_nodes.m_buffer[0], "", stopCount, vehicleCount, targetVehicles, ref total, ref emitted, limit, countByType, items, ref first);
                }

                if (targetVehicles > 0 && vehicleCount == 0)
                {
                    AddLineAnomaly("noVehicles", lineId, name, info, line, 0, net.m_nodes.m_buffer[0], "", stopCount, vehicleCount, targetVehicles, ref total, ref emitted, limit, countByType, items, ref first);
                }

                ushort stop = line.m_stops;
                int guard = 0;
                while (stop != 0 && guard < 256)
                {
                    NetNode node = net.m_nodes.m_buffer[stop];
                    string problemText = node.m_problems.IsNone ? "" : node.m_problems.ToString();
                    if (problemText.IndexOf("LineNotConnected", StringComparison.OrdinalIgnoreCase) >= 0)
                    {
                        AddLineAnomaly("lineNotConnected", lineId, name, info, line, stop, node, problemText, stopCount, vehicleCount, targetVehicles, ref total, ref emitted, limit, countByType, items, ref first);
                    }
                    else if (!node.m_problems.IsNone)
                    {
                        AddLineAnomaly("stopNodeProblem", lineId, name, info, line, stop, node, problemText, stopCount, vehicleCount, targetVehicles, ref total, ref emitted, limit, countByType, items, ref first);
                    }

                    stop = TransportLine.GetNextStop(stop);
                    guard++;
                    if (stop == line.m_stops)
                    {
                        break;
                    }
                }
            }

            bool firstCount = true;
            foreach (KeyValuePair<string, int> pair in countByType)
            {
                if (!firstCount)
                {
                    counts.Append(",");
                }
                counts.Append("\"").Append(JsonUtil.Escape(pair.Key)).Append("\":").Append(pair.Value);
                firstCount = false;
            }

            return CommandResult.FromJson("{\"ok\":true,\"total\":" + total +
                ",\"returned\":" + emitted +
                ",\"limit\":" + limit +
                ",\"counts\":{" + counts.ToString() + "}" +
                ",\"anomalies\":[" + items.ToString() + "]}");
        }

        public static CommandResult BuildTransportStationAnomaliesJson(int limit, float maxStopDistance)
        {
            if (limit < 0) limit = 0;
            if (limit > 1024) limit = 1024;
            if (maxStopDistance < 8f) maxStopDistance = 8f;
            if (maxStopDistance > 256f) maxStopDistance = 256f;

            BuildingManager buildings = BuildingManager.instance;
            TransportManager transport = TransportManager.instance;
            NetManager net = NetManager.instance;
            StringBuilder items = new StringBuilder();
            StringBuilder counts = new StringBuilder();
            Dictionary<string, int> countByType = new Dictionary<string, int>();
            int total = 0;
            int emitted = 0;
            bool first = true;
            float maxStopDistanceSq = maxStopDistance * maxStopDistance;

            for (ushort buildingId = 1; buildingId < buildings.m_buildings.m_buffer.Length; buildingId++)
            {
                Building building = buildings.m_buildings.m_buffer[buildingId];
                if ((building.m_flags & Building.Flags.Created) == Building.Flags.None ||
                    (building.m_flags & Building.Flags.Deleted) != Building.Flags.None)
                {
                    continue;
                }

                BuildingInfo buildingInfo = building.Info;
                if (!IsPassengerStationBuilding(buildingInfo))
                {
                    continue;
                }

                TransportInfo.TransportType stationType;
                if (!TryInferStationTransportType(buildingInfo, out stationType))
                {
                    continue;
                }

                ushort nearestStop;
                ushort nearestLine;
                string nearestLineName;
                float nearestDistance;
                bool hasStop = FindNearestLineStop(building.m_position, stationType, maxStopDistanceSq, out nearestStop, out nearestLine, out nearestLineName, out nearestDistance);
                if (!hasStop)
                {
                    AddStationAnomaly("stationWithoutLine", buildingId, building, buildingInfo, stationType, nearestStop, nearestLine, nearestLineName, nearestDistance, maxStopDistance, ref total, ref emitted, limit, countByType, items, ref first);
                }
            }

            bool firstCount = true;
            foreach (KeyValuePair<string, int> pair in countByType)
            {
                if (!firstCount)
                {
                    counts.Append(",");
                }
                counts.Append("\"").Append(JsonUtil.Escape(pair.Key)).Append("\":").Append(pair.Value);
                firstCount = false;
            }

            return CommandResult.FromJson("{\"ok\":true,\"total\":" + total +
                ",\"returned\":" + emitted +
                ",\"limit\":" + limit +
                ",\"maxStopDistance\":" + JsonUtil.Number(maxStopDistance) +
                ",\"counts\":{" + counts.ToString() + "}" +
                ",\"anomalies\":[" + items.ToString() + "]}");
        }

        public static CommandResult BuildTransportLinePathDetailsJson(ushort lineId)
        {
            if (lineId == 0)
            {
                return CommandResult.Fail("lineId is required.");
            }

            TransportManager transport = TransportManager.instance;
            NetManager net = NetManager.instance;
            PathManager paths = PathManager.instance;
            TransportLine line = transport.m_lines.m_buffer[lineId];
            if ((line.m_flags & TransportLine.Flags.Created) == TransportLine.Flags.None)
            {
                return CommandResult.Fail("Transport line was not found: " + lineId);
            }

            string lineName = "";
            try { lineName = transport.GetLineName(lineId); } catch { }

            StringBuilder stops = new StringBuilder();
            StringBuilder segments = new StringBuilder();
            bool firstStop = true;
            bool firstSegment = true;
            ushort stop = line.m_stops;
            int guard = 0;

            while (stop != 0 && guard < 256)
            {
                NetNode node = net.m_nodes.m_buffer[stop];
                if (!firstStop)
                {
                    stops.Append(",");
                }
                stops.Append("{\"nodeId\":").Append(stop);
                stops.Append(",\"flags\":\"").Append(JsonUtil.Escape(node.m_flags.ToString())).Append("\"");
                stops.Append(",\"problems\":\"").Append(JsonUtil.Escape(node.m_problems.IsNone ? "" : node.m_problems.ToString())).Append("\"");
                stops.Append(",\"transportLine\":").Append(node.m_transportLine);
                stops.Append(",\"connectCount\":").Append(node.m_connectCount);
                stops.Append(",\"position\":{\"x\":").Append(JsonUtil.Number(node.m_position.x));
                stops.Append(",\"y\":").Append(JsonUtil.Number(node.m_position.y));
                stops.Append(",\"z\":").Append(JsonUtil.Number(node.m_position.z)).Append("}");
                stops.Append(",\"segments\":[");
                AppendNodeSegments(stops, node);
                stops.Append("]}");
                firstStop = false;

                ushort nextSegment = TransportLine.GetNextSegment(stop);
                ushort prevSegment = TransportLine.GetPrevSegment(stop);
                if (nextSegment != 0)
                {
                    AppendLineSegmentDetails(segments, nextSegment, net, paths, ref firstSegment);
                }
                if (prevSegment != 0 && prevSegment != nextSegment)
                {
                    AppendLineSegmentDetails(segments, prevSegment, net, paths, ref firstSegment);
                }

                stop = TransportLine.GetNextStop(stop);
                guard++;
                if (stop == line.m_stops)
                {
                    break;
                }
            }

            return CommandResult.FromJson("{\"ok\":true,\"lineId\":" + lineId +
                ",\"lineName\":\"" + JsonUtil.Escape(lineName) + "\"" +
                ",\"flags\":\"" + JsonUtil.Escape(line.m_flags.ToString()) + "\"" +
                ",\"stops\":[" + stops.ToString() + "]" +
                ",\"lineSegments\":[" + segments.ToString() + "]}");
        }

        public static CommandResult CreateTransportLine(string body)
        {
            bool dryRun = JsonUtil.GetBool(body, "dryRun", false);
            string typeName = JsonUtil.GetString(body, "transportType", "Bus");
            string lineName = JsonUtil.GetString(body, "name", "");
            bool fixedPlatform = JsonUtil.GetBool(body, "fixedPlatform", false);
            bool closeLoop = JsonUtil.GetBool(
                body,
                "closeLoop",
                JsonUtil.GetBool(body, "completeLoop", true));
            float heightOffset = JsonUtil.GetNumber(body, "heightOffset", 0f);
            bool useExplicitStopY = JsonUtil.GetBool(body, "useExplicitStopY", false);
            List<string> stopObjects = JsonUtil.GetObjectArray(body, "stops");

            TransportInfo.TransportType transportType;
            if (!TryParseTransportType(typeName, out transportType))
            {
                return CommandResult.Fail("Unsupported transportType: " + typeName);
            }

            if (stopObjects.Count < 2)
            {
                return CommandResult.Fail("At least two stops are required.");
            }

            TransportManager manager = TransportManager.instance;
            TransportInfo info = FindTransportInfo(transportType);
            if (info == null)
            {
                return CommandResult.Fail("Transport info is not loaded for: " + transportType.ToString());
            }
            ItemClass.SubService transportSubService = TransportSubService(transportType);
            UnlockManager unlocks = Singleton<UnlockManager>.instance;
            if (!unlocks.Unlocked(ItemClass.Service.PublicTransport) ||
                !unlocks.Unlocked(transportSubService))
            {
                return CommandResult.Fail("Transport type is still locked: " + transportType.ToString());
            }
            if (heightOffset < -64f)
            {
                heightOffset = -64f;
            }
            else if (heightOffset > 64f)
            {
                heightOffset = 64f;
            }

            List<Vector3> stops = new List<Vector3>();
            for (int i = 0; i < stopObjects.Count; i++)
            {
                Vector3 position = ReadPosition(stopObjects[i]);
                if (!useExplicitStopY)
                {
                    position.y = TerrainManager.instance.SampleRawHeightSmoothWithWater(position, false, 0f);
                }
                position.y += heightOffset;
                stops.Add(position);
            }

            StringBuilder stopChecks = new StringBuilder();
            bool stopsSafe = true;
            for (int i = 0; i < stops.Count; i++)
            {
                float networkDistance = FindNearestCompatibleStopNetwork(stops[i], transportType);
                float nearestOtherStop = float.MaxValue;
                for (int j = 0; j < stops.Count; j++)
                {
                    if (i == j) continue;
                    nearestOtherStop = Mathf.Min(nearestOtherStop, Vector3.Distance(stops[i], stops[j]));
                }
                bool compatibleNetwork = networkDistance <= 64f;
                bool separated = nearestOtherStop >= 32f;
                stopsSafe = stopsSafe && compatibleNetwork && separated;
                if (i > 0) stopChecks.Append(",");
                stopChecks.Append("{\"index\":").Append(i);
                stopChecks.Append(",\"networkDistance\":").Append(JsonUtil.Number(networkDistance));
                stopChecks.Append(",\"nearestOtherStop\":").Append(JsonUtil.Number(nearestOtherStop));
                stopChecks.Append(",\"compatibleNetwork\":").Append(JsonUtil.Bool(compatibleNetwork));
                stopChecks.Append(",\"separated\":").Append(JsonUtil.Bool(separated)).Append("}");
            }
            ushort matchingDepot = FindDepotBuilding(info);
            bool depotRequired = transportType == TransportInfo.TransportType.Bus;
            bool depotSafe = !depotRequired || matchingDepot != 0;
            if (!dryRun && !stopsSafe)
            {
                return CommandResult.Fail(
                    "Transport plan contains a stop farther than 64m from a compatible network or stops closer than 32m.");
            }
            if (!dryRun && !depotSafe)
            {
                return CommandResult.Fail("Bus transport plan requires an active matching depot before line creation.");
            }

            if (dryRun)
            {
                return CommandResult.FromJson("{\"ok\":true,\"dryRun\":true,\"valid\":" + JsonUtil.Bool(stopsSafe && depotSafe) +
                    ",\"transportType\":\"" +
                    JsonUtil.Escape(transportType.ToString()) + "\",\"stops\":" + stops.Count +
                    ",\"heightOffset\":" + JsonUtil.Number(heightOffset) +
                    ",\"useExplicitStopY\":" + JsonUtil.Bool(useExplicitStopY) +
                    ",\"depot\":{\"required\":" + JsonUtil.Bool(depotRequired) +
                    ",\"buildingId\":" + matchingDepot +
                    ",\"safe\":" + JsonUtil.Bool(depotSafe) + "}" +
                    ",\"stopChecks\":[" + stopChecks.ToString() + "]}");
            }

            SimulationManager simulation = Singleton<SimulationManager>.instance;
            Randomizer randomizer = simulation.m_randomizer;
            ushort lineId;
            bool created = manager.CreateLine(out lineId, ref randomizer, info, false);
            simulation.m_randomizer = randomizer;

            if (!created || lineId == 0)
            {
                string rollbackError = null;
                bool rolledBack = lineId == 0 || TryReleaseLine(manager, lineId, out rollbackError);
                return CommandResult.FailWithRecovery(
                    "Failed to create transport line; local rollback " +
                    (rolledBack ? "succeeded." : "failed, restore the checkpoint. " + rollbackError),
                    lineId != 0,
                    rolledBack,
                    "transportLine",
                    lineId);
            }

            TransportLine createdLine = manager.m_lines.m_buffer[lineId];
            if ((createdLine.m_flags & TransportLine.Flags.Created) == TransportLine.Flags.None ||
                createdLine.Info != info)
            {
                string rollbackError;
                bool rolledBack = TryReleaseLine(manager, lineId, out rollbackError);
                return CommandResult.FailWithRecovery(
                    "Transport line creation produced an incomplete entity; local rollback " +
                    (rolledBack ? "succeeded." : "failed, restore the checkpoint. " + rollbackError),
                    true,
                    rolledBack,
                    "transportLine",
                    lineId);
            }

            manager.m_lines.m_buffer[lineId].m_budget = 100;
            manager.m_lines.m_buffer[lineId].m_ticketPrice = (ushort)Mathf.Max(0, info.m_ticketPrice);
            manager.m_lines.m_buffer[lineId].m_building = matchingDepot;
            TransportLine.Flags beforeFlags = manager.m_lines.m_buffer[lineId].m_flags;
            for (int i = 0; i < stops.Count; i++)
            {
                bool added = manager.m_lines.m_buffer[lineId].AddStop(lineId, i, stops[i], fixedPlatform);
                if (!added)
                {
                    string rollbackError;
                    bool rolledBack = TryReleaseLine(manager, lineId, out rollbackError);
                    return CommandResult.FailWithRecovery("Failed to add stop " + i + " near x=" +
                        JsonUtil.Number(stops[i].x) + ", z=" + JsonUtil.Number(stops[i].z) +
                        "; local rollback " + RollbackStatus(rolledBack, rollbackError),
                        true,
                        rolledBack,
                        "transportLine",
                        lineId);
                }
                int duplicateA;
                int duplicateB;
                if (HasDuplicateStops(lineId, 8f, out duplicateA, out duplicateB))
                {
                    string rollbackError;
                    bool rolledBack = TryReleaseLine(manager, lineId, out rollbackError);
                    return CommandResult.FailWithRecovery("Stops " + duplicateA + " and " + duplicateB +
                        " snapped to the same platform or road position. Use different network segments or stations; local rollback " +
                        RollbackStatus(rolledBack, rollbackError),
                        true,
                        rolledBack,
                        "transportLine",
                        lineId);
                }
            }

            if (closeLoop)
            {
                bool closed = manager.m_lines.m_buffer[lineId].AddStop(lineId, stops.Count, stops[0], fixedPlatform);
                if (!closed)
                {
                    string rollbackError;
                    bool rolledBack = TryReleaseLine(manager, lineId, out rollbackError);
                    return CommandResult.FailWithRecovery("Failed to close transport line near first stop x=" +
                        JsonUtil.Number(stops[0].x) + ", z=" + JsonUtil.Number(stops[0].z) +
                        "; local rollback " + RollbackStatus(rolledBack, rollbackError),
                        true,
                        rolledBack,
                        "transportLine",
                        lineId);
                }
            }

            manager.m_lines.m_buffer[lineId].m_flags |= TransportLine.Flags.Complete | TransportLine.Flags.CompleteSet;
            ApplyColor(body, lineId);
            ApplyLineName(manager, lineId, lineName);
            manager.m_lines.m_buffer[lineId].UpdatePaths(lineId);
            manager.m_lines.m_buffer[lineId].UpdateMeshData(lineId);
            manager.UpdateLine(lineId);
            manager.UpdateLinesNow();
            manager.CheckTransportLineVehicles();

            TransportLine line = manager.m_lines.m_buffer[lineId];
            if ((line.m_flags & TransportLine.Flags.Created) == TransportLine.Flags.None ||
                (line.m_flags & TransportLine.Flags.Complete) == TransportLine.Flags.None ||
                line.Info != info ||
                line.m_building != matchingDepot ||
                line.CountStops(lineId) < stops.Count)
            {
                string rollbackError;
                bool rolledBack = TryReleaseLine(manager, lineId, out rollbackError);
                return CommandResult.FailWithRecovery(
                    "Transport line failed post-creation verification; local rollback " +
                    RollbackStatus(rolledBack, rollbackError),
                    true,
                    rolledBack,
                    "transportLine",
                    lineId);
            }
            string json = "{\"ok\":true,\"dryRun\":false,\"lineId\":" + lineId +
                ",\"name\":\"" + JsonUtil.Escape(lineName) + "\"" +
                ",\"transportType\":\"" + JsonUtil.Escape(transportType.ToString()) + "\"" +
                ",\"buildingId\":" + line.m_building +
                ",\"stops\":" + line.CountStops(lineId) +
                ",\"heightOffset\":" + JsonUtil.Number(heightOffset) +
                ",\"useExplicitStopY\":" + JsonUtil.Bool(useExplicitStopY) +
                ",\"beforeFlags\":\"" + JsonUtil.Escape(beforeFlags.ToString()) + "\"" +
                ",\"afterFlags\":\"" + JsonUtil.Escape(line.m_flags.ToString()) + "\"}";

            Debug.Log("[SkylinesAgentBridge] Created transport line " + lineId + " type " + transportType.ToString() + " with " + stops.Count + " stops");
            return CommandResult.FromJson(json);
        }

        public static CommandResult AssignTransportLineDepot(string body)
        {
            ushort lineId = (ushort)JsonUtil.GetNumber(body, "id", 0f);
            ushort buildingId = (ushort)JsonUtil.GetNumber(body, "buildingId", 0f);

            if (lineId == 0)
            {
                return CommandResult.Fail("id is required.");
            }

            TransportManager manager = TransportManager.instance;
            TransportLine line = manager.m_lines.m_buffer[lineId];
            if ((line.m_flags & TransportLine.Flags.Created) == TransportLine.Flags.None)
            {
                return CommandResult.Fail("Transport line was not found: " + lineId);
            }

            TransportInfo info = line.Info;
            if (info == null)
            {
                return CommandResult.Fail("Transport info was not found for line: " + lineId);
            }

            if (buildingId == 0)
            {
                buildingId = FindDepotBuilding(info);
            }

            if (buildingId == 0)
            {
                return CommandResult.Fail("No active matching depot building was found for line: " + lineId);
            }

            BuildingManager buildings = BuildingManager.instance;
            Building building = buildings.m_buildings.m_buffer[buildingId];
            if (!IsUsableDepot(building, info))
            {
                return CommandResult.Fail("Building is not a usable depot for this line: " + buildingId);
            }

            manager.m_lines.m_buffer[lineId].m_building = buildingId;
            manager.UpdateLine(lineId);
            manager.UpdateLinesNow();
            manager.CheckTransportLineVehicles();

            return CommandResult.FromJson("{\"ok\":true,\"lineId\":" + lineId +
                ",\"buildingId\":" + buildingId + "}");
        }

        public static CommandResult ReleaseTransportLine(string body)
        {
            ushort lineId = (ushort)JsonUtil.GetNumber(body, "id", 0f);
            bool dryRun = JsonUtil.GetBool(body, "dryRun", false);

            if (lineId == 0)
            {
                return CommandResult.Fail("id is required.");
            }

            TransportManager manager = TransportManager.instance;
            if (lineId >= manager.m_lines.m_buffer.Length)
            {
                return CommandResult.Fail("Transport line id is out of range: " + lineId);
            }
            TransportLine line = manager.m_lines.m_buffer[lineId];
            if ((line.m_flags & TransportLine.Flags.Created) == TransportLine.Flags.None)
            {
                return CommandResult.Fail("Transport line was not found: " + lineId);
            }

            if (!dryRun)
            {
                string releaseError;
                if (!TryReleaseLine(manager, lineId, out releaseError))
                {
                    return CommandResult.Fail("Failed to release transport line " + lineId + ": " + releaseError);
                }
            }

            return CommandResult.FromJson("{\"ok\":true,\"dryRun\":" + JsonUtil.Bool(dryRun) +
                ",\"lineId\":" + lineId + "}");
        }

        public static CommandResult SetTransportLineSettings(string body)
        {
            ushort lineId = (ushort)JsonUtil.GetNumber(body, "lineId", JsonUtil.GetNumber(body, "id", 0f));
            int budget = (int)JsonUtil.GetNumber(body, "budget", -1f);
            int ticketPrice = (int)JsonUtil.GetNumber(body, "ticketPrice", -1f);
            string name = JsonUtil.GetString(body, "name", "");
            string vehiclePrefab = JsonUtil.GetString(body, "vehiclePrefab", "");
            bool dryRun = JsonUtil.GetBool(body, "dryRun", false);

            TransportManager manager = TransportManager.instance;
            TransportLine line;
            CommandResult error;
            if (!TryGetLine(manager, lineId, out line, out error)) return error;
            if (budget != -1 && (budget < 50 || budget > 200)) return CommandResult.Fail("budget must be between 50 and 200.");
            if (ticketPrice < -1 || ticketPrice > 10000) return CommandResult.Fail("ticketPrice must be between 0 and 10000.");

            VehicleInfo vehicle = null;
            if (vehiclePrefab.Length > 0)
            {
                vehicle = PrefabCollection<VehicleInfo>.FindLoaded(vehiclePrefab);
                if (vehicle == null) return CommandResult.Fail("Vehicle prefab was not found: " + vehiclePrefab);
                if (!line.IsCompatibleVehicle(lineId, vehicle))
                {
                    return CommandResult.Fail("Vehicle prefab is not compatible with line " + lineId + ": " + vehiclePrefab);
                }
            }

            ushort beforeBudget = line.m_budget;
            ushort beforeTicketPrice = line.m_ticketPrice;
            if (!dryRun)
            {
                if (budget != -1) line.m_budget = (ushort)budget;
                if (ticketPrice != -1) line.m_ticketPrice = (ushort)ticketPrice;
                manager.m_lines.m_buffer[lineId] = line;
                if (name.Length > 0) ApplyLineName(manager, lineId, name);
                if (vehicle != null && !ReplaceLineVehicles(lineId, vehicle))
                {
                    return CommandResult.Fail("The game rejected the requested vehicle model.");
                }
                ApplyColor(body, lineId);
                UpdateLine(manager, lineId);
            }

            TransportLine after = manager.m_lines.m_buffer[lineId];
            return CommandResult.FromJson("{\"ok\":true,\"dryRun\":" + JsonUtil.Bool(dryRun) +
                ",\"lineId\":" + lineId +
                ",\"before\":{\"budget\":" + beforeBudget + ",\"ticketPrice\":" + beforeTicketPrice + "}" +
                ",\"after\":{\"budget\":" + (dryRun ? (budget == -1 ? beforeBudget : budget) : after.m_budget) +
                ",\"ticketPrice\":" + (dryRun ? (ticketPrice == -1 ? beforeTicketPrice : ticketPrice) : after.m_ticketPrice) + "}" +
                ",\"vehiclePrefab\":\"" + JsonUtil.Escape(vehiclePrefab) + "\"}");
        }

        public static CommandResult DeleteTransportLine(string body)
        {
            ushort lineId = (ushort)JsonUtil.GetNumber(body, "lineId", JsonUtil.GetNumber(body, "id", 0f));
            bool dryRun = JsonUtil.GetBool(body, "dryRun", false);

            TransportManager manager = TransportManager.instance;
            TransportLine line;
            CommandResult error;
            if (!TryGetLine(manager, lineId, out line, out error)) return error;
            int stops = line.CountStops(lineId);
            int vehicles = CountVehicles(line.m_vehicles);
            if (!dryRun)
            {
                string releaseError;
                if (!TryReleaseLine(manager, lineId, out releaseError))
                {
                    return CommandResult.Fail("The game did not release line " + lineId + ": " + releaseError);
                }
            }

            bool gone = (manager.m_lines.m_buffer[lineId].m_flags & TransportLine.Flags.Created) == TransportLine.Flags.None;
            return CommandResult.FromJson("{\"ok\":" + JsonUtil.Bool(dryRun || gone) + ",\"dryRun\":" + JsonUtil.Bool(dryRun) +
                ",\"lineId\":" + lineId + ",\"stops\":" + stops + ",\"vehicles\":" + vehicles +
                ",\"released\":" + JsonUtil.Bool(!dryRun && gone) + "}");
        }

        public static CommandResult AddTransportStop(string body)
        {
            ushort lineId = (ushort)JsonUtil.GetNumber(body, "lineId", 0f);
            int index = (int)JsonUtil.GetNumber(body, "index", -1f);
            bool fixedPlatform = JsonUtil.GetBool(body, "fixedPlatform", false);
            bool dryRun = JsonUtil.GetBool(body, "dryRun", false);
            bool useExplicitY = JsonUtil.GetBool(body, "useExplicitY", false);
            Vector3 position = ReadPosition(body);

            TransportManager manager = TransportManager.instance;
            TransportLine line;
            CommandResult error;
            if (!TryGetLine(manager, lineId, out line, out error)) return error;
            int count = line.CountStops(lineId);
            if (index < 0) index = count;
            if (index < 0 || index > count) return CommandResult.Fail("index is outside the transport line stop range.");
            if (!useExplicitY) position.y = TerrainManager.instance.SampleRawHeightSmoothWithWater(position, false, 0f);
            float closestStopDistance = DistanceToClosestStop(lineId, position, -1);
            if (closestStopDistance < 32f)
            {
                return CommandResult.Fail("The requested stop is only " + JsonUtil.Number(closestStopDistance) +
                    " metres from an existing stop; use at least 32 metres to avoid platform snapping.");
            }
            if (!line.CanAddStop(lineId, index, position)) return CommandResult.Fail("The transport line cannot add a stop at the requested position.");

            if (!dryRun)
            {
                if (!line.AddStop(lineId, index, position, fixedPlatform)) return CommandResult.Fail("The game rejected the new stop.");
                manager.m_lines.m_buffer[lineId] = line;
                UpdateLine(manager, lineId);
                int duplicateA;
                int duplicateB;
                if (HasDuplicateStops(lineId, 8f, out duplicateA, out duplicateB))
                {
                    bool restored = RemoveDuplicateStop(lineId, index);
                    UpdateLine(manager, lineId);
                    if (!restored)
                    {
                        return CommandResult.Fail("The new stop duplicated an existing stop and automatic rollback could not verify a clean line. Restore the checkpoint.");
                    }
                    return CommandResult.Fail("The new stop snapped too close to stop " + (duplicateA == index ? duplicateB : duplicateA) + ".");
                }
                line = manager.m_lines.m_buffer[lineId];
            }
            ushort nodeId = dryRun ? (ushort)0 : line.GetStop(index);
            return CommandResult.FromJson("{\"ok\":true,\"dryRun\":" + JsonUtil.Bool(dryRun) +
                ",\"lineId\":" + lineId + ",\"index\":" + index +
                ",\"nodeId\":" + nodeId + ",\"stopCount\":" + (dryRun ? count + 1 : line.CountStops(lineId)) + "}");
        }

        public static CommandResult MoveTransportStop(string body)
        {
            ushort lineId = (ushort)JsonUtil.GetNumber(body, "lineId", 0f);
            int index = (int)JsonUtil.GetNumber(body, "index", -1f);
            bool fixedPlatform = JsonUtil.GetBool(body, "fixedPlatform", false);
            bool dryRun = JsonUtil.GetBool(body, "dryRun", false);
            bool useExplicitY = JsonUtil.GetBool(body, "useExplicitY", false);
            Vector3 position = ReadPosition(body);

            TransportManager manager = TransportManager.instance;
            TransportLine line;
            CommandResult error;
            if (!TryGetLine(manager, lineId, out line, out error)) return error;
            int count = line.CountStops(lineId);
            if (index < 0 || index >= count) return CommandResult.Fail("index is outside the transport line stop range.");
            if (!useExplicitY) position.y = TerrainManager.instance.SampleRawHeightSmoothWithWater(position, false, 0f);
            float closestStopDistance = DistanceToClosestStop(lineId, position, index);
            if (closestStopDistance < 32f)
            {
                return CommandResult.Fail("The requested stop position is only " + JsonUtil.Number(closestStopDistance) +
                    " metres from another stop; use at least 32 metres to avoid platform snapping.");
            }
            if (!line.CanMoveStop(lineId, index, position)) return CommandResult.Fail("The transport line cannot move the stop to the requested position.");
            ushort oldNodeId = line.GetStop(index);
            Vector3 oldPosition = oldNodeId == 0 ? position : NetManager.instance.m_nodes.m_buffer[oldNodeId].m_position;

            if (!dryRun)
            {
                if (!line.MoveStop(lineId, index, position, fixedPlatform)) return CommandResult.Fail("The game rejected the stop move.");
                manager.m_lines.m_buffer[lineId] = line;
                int duplicateA;
                int duplicateB;
                if (HasDuplicateStops(lineId, 8f, out duplicateA, out duplicateB))
                {
                    line = manager.m_lines.m_buffer[lineId];
                    line.MoveStop(lineId, index, oldPosition, fixedPlatform);
                    manager.m_lines.m_buffer[lineId] = line;
                    UpdateLine(manager, lineId);
                    return CommandResult.Fail("The moved stop snapped too close to another stop and was restored.");
                }
                UpdateLine(manager, lineId);
                line = manager.m_lines.m_buffer[lineId];
            }
            return CommandResult.FromJson("{\"ok\":true,\"dryRun\":" + JsonUtil.Bool(dryRun) +
                ",\"lineId\":" + lineId + ",\"index\":" + index +
                ",\"nodeId\":" + (dryRun ? 0 : line.GetStop(index)) + "}");
        }

        public static CommandResult RemoveTransportStop(string body)
        {
            ushort lineId = (ushort)JsonUtil.GetNumber(body, "lineId", 0f);
            int index = (int)JsonUtil.GetNumber(body, "index", -1f);
            bool dryRun = JsonUtil.GetBool(body, "dryRun", false);

            TransportManager manager = TransportManager.instance;
            TransportLine line;
            CommandResult error;
            if (!TryGetLine(manager, lineId, out line, out error)) return error;
            int count = line.CountStops(lineId);
            if (count <= 2) return CommandResult.Fail("A transport line must retain at least two stops.");
            if (index < 0 || index >= count) return CommandResult.Fail("index is outside the transport line stop range.");
            ushort nodeId = line.GetStop(index);
            if (!dryRun)
            {
                if (!line.RemoveStop(lineId, index)) return CommandResult.Fail("The game rejected the stop removal.");
                manager.m_lines.m_buffer[lineId] = line;
                UpdateLine(manager, lineId);
                line = manager.m_lines.m_buffer[lineId];
            }
            return CommandResult.FromJson("{\"ok\":true,\"dryRun\":" + JsonUtil.Bool(dryRun) +
                ",\"lineId\":" + lineId + ",\"index\":" + index +
                ",\"removedNodeId\":" + nodeId + ",\"stopCount\":" + (dryRun ? count - 1 : line.CountStops(lineId)) + "}");
        }

        public static CommandResult RefreshTransportNetwork(string body)
        {
            string typeName = JsonUtil.GetString(body, "transportType", "Metro");
            bool updateLines = JsonUtil.GetBool(body, "updateLines", true);
            TransportInfo.TransportType transportType;
            if (!TryParseTransportType(typeName, out transportType))
            {
                return CommandResult.Fail("Unsupported transportType: " + typeName);
            }

            ItemClass.SubService subService = TransportSubService(transportType);
            NetManager net = NetManager.instance;
            TransportManager transport = TransportManager.instance;
            int nodes = 0;
            int segments = 0;
            int lines = 0;

            for (ushort i = 1; i < net.m_nodes.m_buffer.Length; i++)
            {
                NetNode node = net.m_nodes.m_buffer[i];
                if ((node.m_flags & NetNode.Flags.Created) == NetNode.Flags.None)
                {
                    continue;
                }

                NetInfo info = node.Info;
                if (!IsTransportNetInfo(info, subService) || info.m_netAI == null)
                {
                    continue;
                }

                if (transportType == TransportInfo.TransportType.Metro)
                {
                    node.m_flags &= ~NetNode.Flags.Disabled;
                    if (IsUndergroundMetroNetInfo(info))
                    {
                        node.m_flags |= NetNode.Flags.Underground;
                    }
                }
                info.m_netAI.UpdateNodeFlags(i, ref node);
                if (transportType == TransportInfo.TransportType.Metro)
                {
                    node.m_flags &= ~NetNode.Flags.Disabled;
                    if (IsUndergroundMetroNetInfo(info))
                    {
                        node.m_flags |= NetNode.Flags.Underground;
                    }
                }
                info.m_netAI.UpdateLaneConnection(i, ref node);
                info.m_netAI.UpdateNode(i, ref node);
                net.m_nodes.m_buffer[i] = node;
                nodes++;
            }

            for (ushort i = 1; i < net.m_segments.m_buffer.Length; i++)
            {
                NetSegment segment = net.m_segments.m_buffer[i];
                if ((segment.m_flags & NetSegment.Flags.Created) == NetSegment.Flags.None)
                {
                    continue;
                }

                NetInfo info = segment.Info;
                if (!IsTransportNetInfo(info, subService) || info.m_netAI == null)
                {
                    continue;
                }

                info.m_netAI.UpdateSegmentFlags(i, ref segment);
                info.m_netAI.UpdateSegment(i, ref segment);
                net.m_segments.m_buffer[i] = segment;
                segments++;
            }

            if (updateLines)
            {
                for (ushort lineId = 1; lineId < transport.m_lines.m_buffer.Length; lineId++)
                {
                    TransportLine line = transport.m_lines.m_buffer[lineId];
                    if ((line.m_flags & TransportLine.Flags.Created) == TransportLine.Flags.None)
                    {
                        continue;
                    }

                    TransportInfo info = line.Info;
                    if (info == null || info.m_transportType != transportType)
                    {
                        continue;
                    }

                    transport.m_lines.m_buffer[lineId].UpdatePaths(lineId);
                    transport.m_lines.m_buffer[lineId].UpdateMeshData(lineId);
                    transport.UpdateLine(lineId);
                    lines++;
                }
                transport.UpdateLinesNow();
                transport.CheckTransportLineVehicles();
            }

            return CommandResult.FromJson("{\"ok\":true,\"transportType\":\"" + JsonUtil.Escape(transportType.ToString()) +
                "\",\"nodesRefreshed\":" + nodes +
                ",\"segmentsRefreshed\":" + segments +
                ",\"linesRefreshed\":" + lines + "}");
        }

        private static void AppendStops(StringBuilder json, ushort lineId, TransportLine line)
        {
            NetManager net = NetManager.instance;
            ushort stop = line.m_stops;
            bool first = true;
            int guard = 0;

            while (stop != 0 && guard < 256)
            {
                if (!first)
                {
                    json.Append(",");
                }

                Vector3 position = net.m_nodes.m_buffer[stop].m_position;
                json.Append("{\"nodeId\":").Append(stop);
                json.Append(",\"position\":{\"x\":").Append(JsonUtil.Number(position.x));
                json.Append(",\"y\":").Append(JsonUtil.Number(position.y));
                json.Append(",\"z\":").Append(JsonUtil.Number(position.z)).Append("}}");

                stop = TransportLine.GetNextStop(stop);
                first = false;
                guard++;

                if (stop == line.m_stops)
                {
                    break;
                }
            }
        }

        private static int CountVehicles(ushort firstVehicle)
        {
            VehicleManager vehicles = VehicleManager.instance;
            int count = 0;
            ushort vehicle = firstVehicle;
            int guard = 0;

            while (vehicle != 0 && guard < 16384)
            {
                count++;
                vehicle = vehicles.m_vehicles.m_buffer[vehicle].m_nextLineVehicle;
                guard++;
            }

            return count;
        }

        private static void AddLineAnomaly(
            string type,
            ushort lineId,
            string lineName,
            TransportInfo info,
            TransportLine line,
            ushort nodeId,
            NetNode node,
            string problemText,
            int stopCount,
            int vehicleCount,
            int targetVehicles,
            ref int total,
            ref int emitted,
            int limit,
            Dictionary<string, int> countByType,
            StringBuilder items,
            ref bool first)
        {
            total++;
            if (countByType.ContainsKey(type))
            {
                countByType[type]++;
            }
            else
            {
                countByType[type] = 1;
            }

            if (emitted >= limit)
            {
                return;
            }

            if (!first)
            {
                items.Append(",");
            }

            items.Append("{\"type\":\"").Append(JsonUtil.Escape(type)).Append("\"");
            items.Append(",\"lineId\":").Append(lineId);
            items.Append(",\"lineName\":\"").Append(JsonUtil.Escape(lineName)).Append("\"");
            items.Append(",\"transportType\":\"").Append(JsonUtil.Escape(info == null ? "" : info.m_transportType.ToString())).Append("\"");
            items.Append(",\"flags\":\"").Append(JsonUtil.Escape(line.m_flags.ToString())).Append("\"");
            items.Append(",\"stops\":").Append(stopCount);
            items.Append(",\"vehicles\":").Append(vehicleCount);
            items.Append(",\"targetVehicles\":").Append(targetVehicles);
            items.Append(",\"nodeId\":").Append(nodeId);
            items.Append(",\"problem\":\"").Append(JsonUtil.Escape(problemText)).Append("\"");
            if (nodeId != 0)
            {
                items.Append(",\"position\":{\"x\":").Append(JsonUtil.Number(node.m_position.x));
                items.Append(",\"y\":").Append(JsonUtil.Number(node.m_position.y));
                items.Append(",\"z\":").Append(JsonUtil.Number(node.m_position.z)).Append("}");
            }
            items.Append("}");

            emitted++;
            first = false;
        }

        private static void AddStationAnomaly(
            string type,
            ushort buildingId,
            Building building,
            BuildingInfo buildingInfo,
            TransportInfo.TransportType stationType,
            ushort nearestStop,
            ushort nearestLine,
            string nearestLineName,
            float nearestDistance,
            float maxStopDistance,
            ref int total,
            ref int emitted,
            int limit,
            Dictionary<string, int> countByType,
            StringBuilder items,
            ref bool first)
        {
            total++;
            if (countByType.ContainsKey(type))
            {
                countByType[type]++;
            }
            else
            {
                countByType[type] = 1;
            }

            if (emitted >= limit)
            {
                return;
            }

            if (!first)
            {
                items.Append(",");
            }

            string problems = building.m_problems.IsNone ? "" : building.m_problems.ToString();
            items.Append("{\"type\":\"").Append(JsonUtil.Escape(type)).Append("\"");
            items.Append(",\"buildingId\":").Append(buildingId);
            items.Append(",\"prefab\":\"").Append(JsonUtil.Escape(buildingInfo == null ? "" : buildingInfo.name)).Append("\"");
            items.Append(",\"displayName\":\"").Append(JsonUtil.Escape(buildingInfo == null ? "" : buildingInfo.GetUncheckedLocalizedTitle())).Append("\"");
            items.Append(",\"transportType\":\"").Append(JsonUtil.Escape(stationType.ToString())).Append("\"");
            items.Append(",\"flags\":\"").Append(JsonUtil.Escape(building.m_flags.ToString())).Append("\"");
            items.Append(",\"problems\":\"").Append(JsonUtil.Escape(problems)).Append("\"");
            items.Append(",\"nearestStopNodeId\":").Append(nearestStop);
            items.Append(",\"nearestLineId\":").Append(nearestLine);
            items.Append(",\"nearestLineName\":\"").Append(JsonUtil.Escape(nearestLineName)).Append("\"");
            items.Append(",\"nearestStopDistance\":").Append(nearestDistance < 0f ? "null" : JsonUtil.Number(nearestDistance));
            items.Append(",\"maxStopDistance\":").Append(JsonUtil.Number(maxStopDistance));
            items.Append(",\"position\":{\"x\":").Append(JsonUtil.Number(building.m_position.x));
            items.Append(",\"y\":").Append(JsonUtil.Number(building.m_position.y));
            items.Append(",\"z\":").Append(JsonUtil.Number(building.m_position.z)).Append("}}");
            emitted++;
            first = false;
        }

        private static bool FindNearestLineStop(
            Vector3 stationPosition,
            TransportInfo.TransportType stationType,
            float maxStopDistanceSq,
            out ushort nearestStop,
            out ushort nearestLine,
            out string nearestLineName,
            out float nearestDistance)
        {
            TransportManager transport = TransportManager.instance;
            NetManager net = NetManager.instance;
            nearestStop = 0;
            nearestLine = 0;
            nearestLineName = "";
            nearestDistance = -1f;
            float bestSq = float.MaxValue;

            for (ushort lineId = 1; lineId < transport.m_lines.m_buffer.Length; lineId++)
            {
                TransportLine line = transport.m_lines.m_buffer[lineId];
                if ((line.m_flags & TransportLine.Flags.Created) == TransportLine.Flags.None ||
                    (line.m_flags & TransportLine.Flags.Temporary) != TransportLine.Flags.None)
                {
                    continue;
                }

                TransportInfo info = line.Info;
                if (info == null || info.m_transportType != stationType)
                {
                    continue;
                }

                ushort stop = line.m_stops;
                int guard = 0;
                while (stop != 0 && guard < 256)
                {
                    NetNode node = net.m_nodes.m_buffer[stop];
                    Vector3 delta = node.m_position - stationPosition;
                    delta.y = 0f;
                    float distanceSq = delta.sqrMagnitude;
                    if (distanceSq < bestSq)
                    {
                        bestSq = distanceSq;
                        nearestStop = stop;
                        nearestLine = lineId;
                        try { nearestLineName = transport.GetLineName(lineId); } catch { nearestLineName = ""; }
                    }

                    stop = TransportLine.GetNextStop(stop);
                    guard++;
                    if (stop == line.m_stops)
                    {
                        break;
                    }
                }
            }

            if (nearestStop != 0)
            {
                nearestDistance = Mathf.Sqrt(bestSq);
            }

            return nearestStop != 0 && bestSq <= maxStopDistanceSq;
        }

        private static bool IsPassengerStationBuilding(BuildingInfo info)
        {
            if (info == null || info.m_class == null || info.m_class.m_service != ItemClass.Service.PublicTransport)
            {
                return false;
            }

            string aiName = info.m_buildingAI == null ? "" : info.m_buildingAI.GetType().Name;
            string prefabName = info.name == null ? "" : info.name;
            string name = (aiName + " " + prefabName).ToLowerInvariant();

            if (name.IndexOf("depot") >= 0 || name.IndexOf("garage") >= 0 || name.IndexOf("pillar") >= 0)
            {
                return false;
            }

            return name.IndexOf("station") >= 0 ||
                name.IndexOf("entrance") >= 0 ||
                name.IndexOf("hub") >= 0 ||
                name.IndexOf("stop") >= 0 ||
                name.IndexOf("dock") >= 0 ||
                name.IndexOf("harbor") >= 0 ||
                name.IndexOf("airport") >= 0;
        }

        private static bool TryInferStationTransportType(BuildingInfo info, out TransportInfo.TransportType transportType)
        {
            transportType = TransportInfo.TransportType.Bus;
            if (info == null || info.m_class == null)
            {
                return false;
            }

            ItemClass.SubService subService = info.m_class.m_subService;
            if (subService == ItemClass.SubService.PublicTransportMetro) { transportType = TransportInfo.TransportType.Metro; return true; }
            if (subService == ItemClass.SubService.PublicTransportTrain) { transportType = TransportInfo.TransportType.Train; return true; }
            if (subService == ItemClass.SubService.PublicTransportBus) { transportType = TransportInfo.TransportType.Bus; return true; }
            if (subService == ItemClass.SubService.PublicTransportTram) { transportType = TransportInfo.TransportType.Tram; return true; }
            if (subService == ItemClass.SubService.PublicTransportMonorail) { transportType = TransportInfo.TransportType.Monorail; return true; }
            if (subService == ItemClass.SubService.PublicTransportCableCar) { transportType = TransportInfo.TransportType.CableCar; return true; }
            if (subService == ItemClass.SubService.PublicTransportPlane) { transportType = TransportInfo.TransportType.Airplane; return true; }
            if (subService == ItemClass.SubService.PublicTransportShip) { transportType = TransportInfo.TransportType.Ship; return true; }
            return false;
        }

        private static void AppendNodeSegments(StringBuilder json, NetNode node)
        {
            bool first = true;
            AppendNodeSegment(json, node.m_segment0, ref first);
            AppendNodeSegment(json, node.m_segment1, ref first);
            AppendNodeSegment(json, node.m_segment2, ref first);
            AppendNodeSegment(json, node.m_segment3, ref first);
            AppendNodeSegment(json, node.m_segment4, ref first);
            AppendNodeSegment(json, node.m_segment5, ref first);
            AppendNodeSegment(json, node.m_segment6, ref first);
            AppendNodeSegment(json, node.m_segment7, ref first);
        }

        private static void AppendNodeSegment(StringBuilder json, ushort segmentId, ref bool first)
        {
            if (segmentId == 0)
            {
                return;
            }
            if (!first)
            {
                json.Append(",");
            }
            json.Append(segmentId);
            first = false;
        }

        private static void AppendLineSegmentDetails(StringBuilder json, ushort segmentId, NetManager net, PathManager paths, ref bool first)
        {
            if (segmentId == 0)
            {
                return;
            }

            NetSegment segment = net.m_segments.m_buffer[segmentId];
            if ((segment.m_flags & NetSegment.Flags.Created) == NetSegment.Flags.None)
            {
                return;
            }

            if (!first)
            {
                json.Append(",");
            }

            NetInfo info = segment.Info;
            uint pathId = segment.m_path;
            json.Append("{\"segmentId\":").Append(segmentId);
            json.Append(",\"prefab\":\"").Append(JsonUtil.Escape(info == null ? "" : info.name)).Append("\"");
            json.Append(",\"flags\":\"").Append(JsonUtil.Escape(segment.m_flags.ToString())).Append("\"");
            json.Append(",\"problems\":\"").Append(JsonUtil.Escape(segment.m_problems.IsNone ? "" : segment.m_problems.ToString())).Append("\"");
            json.Append(",\"path\":").Append(pathId);
            json.Append(",\"startNodeId\":").Append(segment.m_startNode);
            json.Append(",\"endNodeId\":").Append(segment.m_endNode);
            if (pathId != 0 && pathId < paths.m_pathUnits.m_buffer.Length)
            {
                PathUnit unit = paths.m_pathUnits.m_buffer[pathId];
                json.Append(",\"pathFindFlags\":").Append(unit.m_pathFindFlags);
                json.Append(",\"pathPositionCount\":").Append(unit.m_positionCount);
                json.Append(",\"pathLength\":").Append(JsonUtil.Number(unit.m_length));
                json.Append(",\"pathPositions\":[");
                for (int i = 0; i < unit.m_positionCount && i < 12; i++)
                {
                    if (i != 0)
                    {
                        json.Append(",");
                    }
                    PathUnit.Position position = unit.GetPosition(i);
                    json.Append("{\"segment\":").Append(position.m_segment);
                    json.Append(",\"lane\":").Append(position.m_lane);
                    json.Append(",\"offset\":").Append(position.m_offset).Append("}");
                }
                json.Append("]");
            }
            json.Append("}");
            first = false;
        }

        private static bool TryParseTransportType(string value, out TransportInfo.TransportType transportType)
        {
            string normalized = value == null ? "" : value.Trim();
            string[] names = Enum.GetNames(typeof(TransportInfo.TransportType));
            for (int i = 0; i < names.Length; i++)
            {
                if (string.Compare(names[i], normalized, true) == 0)
                {
                    transportType = (TransportInfo.TransportType)Enum.Parse(typeof(TransportInfo.TransportType), names[i]);
                    return true;
                }
            }

            transportType = TransportInfo.TransportType.Bus;
            return false;
        }

        private static TransportInfo FindTransportInfo(TransportInfo.TransportType transportType)
        {
            TransportInfo fallback = TransportManager.instance.GetTransportInfo(transportType);

            int count = PrefabCollection<TransportInfo>.LoadedCount();
            for (int i = 0; i < count; i++)
            {
                TransportInfo info = PrefabCollection<TransportInfo>.GetLoaded((uint)i);
                if (info == null)
                {
                    continue;
                }
                if (info.m_transportType != transportType)
                {
                    continue;
                }

                if (transportType == TransportInfo.TransportType.Bus &&
                    info.m_vehicleReason == TransferManager.TransferReason.Bus)
                {
                    return info;
                }

                if (fallback == null)
                {
                    fallback = info;
                }
            }

            return fallback;
        }

        private static ushort FindDepotBuilding(TransportInfo transportInfo)
        {
            if (transportInfo == null)
            {
                return 0;
            }

            BuildingManager buildings = BuildingManager.instance;
            for (ushort i = 1; i < buildings.m_buildings.m_buffer.Length; i++)
            {
                Building building = buildings.m_buildings.m_buffer[i];
                if (IsUsableDepot(building, transportInfo))
                {
                    return i;
                }
            }

            return 0;
        }

        private static bool IsUsableDepot(Building building, TransportInfo transportInfo)
        {
            if ((building.m_flags & Building.Flags.Created) == Building.Flags.None)
            {
                return false;
            }
            if ((building.m_flags & Building.Flags.Deleted) != Building.Flags.None)
            {
                return false;
            }
            if ((building.m_flags & Building.Flags.Active) == Building.Flags.None)
            {
                return false;
            }
            if ((building.m_flags & Building.Flags.RoadAccessFailed) != Building.Flags.None)
            {
                return false;
            }
            if (!building.m_problems.IsNone)
            {
                return false;
            }

            BuildingInfo buildingInfo = building.Info;
            if (buildingInfo == null || buildingInfo.m_class == null)
            {
                return false;
            }
            if (buildingInfo.m_class.m_service != ItemClass.Service.PublicTransport)
            {
                return false;
            }

            ItemClass.SubService stationSubService = RequiredDepotSubService(transportInfo);
            if (stationSubService != ItemClass.SubService.None &&
                buildingInfo.m_class.m_subService != stationSubService)
            {
                return false;
            }

            return true;
        }

        private static ItemClass.SubService RequiredDepotSubService(TransportInfo transportInfo)
        {
            if (transportInfo == null)
            {
                return ItemClass.SubService.None;
            }

            if (transportInfo.m_transportType == TransportInfo.TransportType.Bus)
            {
                return ItemClass.SubService.PublicTransportBus;
            }

            return transportInfo.m_stationSubService;
        }

        private static ItemClass.SubService TransportSubService(TransportInfo.TransportType transportType)
        {
            if (transportType == TransportInfo.TransportType.Metro) return ItemClass.SubService.PublicTransportMetro;
            if (transportType == TransportInfo.TransportType.Train) return ItemClass.SubService.PublicTransportTrain;
            if (transportType == TransportInfo.TransportType.Bus) return ItemClass.SubService.PublicTransportBus;
            if (transportType == TransportInfo.TransportType.Tram) return ItemClass.SubService.PublicTransportTram;
            if (transportType == TransportInfo.TransportType.Monorail) return ItemClass.SubService.PublicTransportMonorail;
            if (transportType == TransportInfo.TransportType.CableCar) return ItemClass.SubService.PublicTransportCableCar;
            if (transportType == TransportInfo.TransportType.Ship) return ItemClass.SubService.PublicTransportShip;
            if (transportType == TransportInfo.TransportType.Airplane) return ItemClass.SubService.PublicTransportPlane;
            return ItemClass.SubService.None;
        }

        private static bool IsTransportNetInfo(NetInfo info, ItemClass.SubService subService)
        {
            return info != null &&
                info.m_class != null &&
                info.m_class.m_service == ItemClass.Service.PublicTransport &&
                (subService == ItemClass.SubService.None || info.m_class.m_subService == subService);
        }

        private static bool IsUndergroundMetroNetInfo(NetInfo info)
        {
            if (info == null || info.name == null)
            {
                return false;
            }

            return info.name.IndexOf("Metro", StringComparison.OrdinalIgnoreCase) >= 0 &&
                info.name.IndexOf("Ground", StringComparison.OrdinalIgnoreCase) < 0 &&
                info.name.IndexOf("Elevated", StringComparison.OrdinalIgnoreCase) < 0 &&
                info.name.IndexOf("Bridge", StringComparison.OrdinalIgnoreCase) < 0 &&
                info.name.IndexOf("Slope", StringComparison.OrdinalIgnoreCase) < 0;
        }

        private static Vector3 ReadPosition(string json)
        {
            float x = JsonUtil.GetPointNumber(json, "position", "x", JsonUtil.GetNumber(json, "x", 0f));
            float y = JsonUtil.GetPointNumber(json, "position", "y", JsonUtil.GetNumber(json, "y", 0f));
            float z = JsonUtil.GetPointNumber(json, "position", "z", JsonUtil.GetNumber(json, "z", 0f));
            return new Vector3(x, y, z);
        }

        private static float FindNearestCompatibleStopNetwork(
            Vector3 position,
            TransportInfo.TransportType transportType)
        {
            NetManager manager = Singleton<NetManager>.instance;
            float nearest = float.MaxValue;
            ItemClass.SubService subService = TransportSubService(transportType);
            for (ushort i = 1; i < manager.m_segments.m_buffer.Length; i++)
            {
                NetSegment segment = manager.m_segments.m_buffer[i];
                if ((segment.m_flags & NetSegment.Flags.Created) == NetSegment.Flags.None ||
                    segment.Info == null ||
                    segment.Info.m_class == null)
                {
                    continue;
                }

                bool compatible;
                if (transportType == TransportInfo.TransportType.Bus)
                {
                    compatible = segment.Info.m_class.m_service == ItemClass.Service.Road;
                }
                else
                {
                    compatible = segment.Info.m_class.m_service == ItemClass.Service.PublicTransport &&
                        segment.Info.m_class.m_subService == subService;
                }
                if (!compatible) continue;

                Vector3 start = manager.m_nodes.m_buffer[segment.m_startNode].m_position;
                Vector3 end = manager.m_nodes.m_buffer[segment.m_endNode].m_position;
                nearest = Mathf.Min(nearest, DistancePointToSegment(position, start, end));
            }
            return nearest;
        }

        private static float DistancePointToSegment(Vector3 point, Vector3 start, Vector3 end)
        {
            Vector3 delta = end - start;
            float lengthSq = delta.sqrMagnitude;
            if (lengthSq < 0.001f) return Vector3.Distance(point, start);
            float t = Mathf.Clamp01(Vector3.Dot(point - start, delta) / lengthSq);
            return Vector3.Distance(point, start + delta * t);
        }

        private static bool TryGetLine(TransportManager manager, ushort lineId, out TransportLine line, out CommandResult error)
        {
            line = new TransportLine();
            error = null;
            if (lineId == 0 || lineId >= manager.m_lines.m_buffer.Length)
            {
                error = CommandResult.Fail("lineId is required and must be in range.");
                return false;
            }
            line = manager.m_lines.m_buffer[lineId];
            if ((line.m_flags & TransportLine.Flags.Created) == TransportLine.Flags.None)
            {
                error = CommandResult.Fail("Transport line was not found: " + lineId);
                return false;
            }
            return true;
        }

        private static void UpdateLine(TransportManager manager, ushort lineId)
        {
            manager.m_lines.m_buffer[lineId].UpdatePaths(lineId);
            manager.m_lines.m_buffer[lineId].UpdateMeshData(lineId);
            manager.UpdateLine(lineId);
            manager.UpdateLinesNow();
            manager.CheckTransportLineVehicles();
        }

        private static bool ReplaceLineVehicles(ushort lineId, VehicleInfo info)
        {
            MethodInfo method = typeof(TransportLine).GetMethod(
                "ReplaceVehicles",
                BindingFlags.Instance | BindingFlags.NonPublic,
                null,
                new Type[] { typeof(ushort), typeof(VehicleInfo) },
                null);
            if (method == null) return false;
            TransportManager manager = TransportManager.instance;
            TransportLine line = manager.m_lines.m_buffer[lineId];
            object boxed = line;
            object result = method.Invoke(boxed, new object[] { lineId, info });
            manager.m_lines.m_buffer[lineId] = (TransportLine)boxed;
            return result == null || !(result is bool) || (bool)result;
        }

        private static bool HasDuplicateStops(ushort lineId, float minimumDistance, out int firstIndex, out int secondIndex)
        {
            TransportLine line = TransportManager.instance.m_lines.m_buffer[lineId];
            int count = line.CountStops(lineId);
            NetManager net = NetManager.instance;
            float minimumDistanceSq = minimumDistance * minimumDistance;
            for (int i = 0; i < count; i++)
            {
                ushort firstNode = line.GetStop(i);
                if (firstNode == 0) continue;
                Vector3 first = net.m_nodes.m_buffer[firstNode].m_position;
                for (int j = i + 1; j < count; j++)
                {
                    ushort secondNode = line.GetStop(j);
                    if (secondNode == 0) continue;
                    Vector3 second = net.m_nodes.m_buffer[secondNode].m_position;
                    Vector2 delta = new Vector2(first.x - second.x, first.z - second.z);
                    if (firstNode == secondNode || delta.sqrMagnitude < minimumDistanceSq)
                    {
                        firstIndex = i;
                        secondIndex = j;
                        return true;
                    }
                }
            }
            firstIndex = -1;
            secondIndex = -1;
            return false;
        }

        private static bool RemoveDuplicateStop(ushort lineId, int preferredIndex)
        {
            TransportManager manager = TransportManager.instance;
            for (int attempt = 0; attempt < 3; attempt++)
            {
                int duplicateA;
                int duplicateB;
                if (!HasDuplicateStops(lineId, 8f, out duplicateA, out duplicateB)) return true;
                TransportLine line = manager.m_lines.m_buffer[lineId];
                int count = line.CountStops(lineId);
                int removeIndex = preferredIndex >= 0 && preferredIndex < count &&
                    (preferredIndex == duplicateA || preferredIndex == duplicateB)
                    ? preferredIndex
                    : duplicateB;
                if (!line.RemoveStop(lineId, removeIndex)) return false;
                manager.m_lines.m_buffer[lineId] = line;
            }
            int remainingA;
            int remainingB;
            return !HasDuplicateStops(lineId, 8f, out remainingA, out remainingB);
        }

        private static float DistanceToClosestStop(ushort lineId, Vector3 position, int excludedIndex)
        {
            TransportLine line = TransportManager.instance.m_lines.m_buffer[lineId];
            NetManager net = NetManager.instance;
            int count = line.CountStops(lineId);
            float best = float.MaxValue;
            for (int index = 0; index < count; index++)
            {
                if (index == excludedIndex) continue;
                ushort nodeId = line.GetStop(index);
                if (nodeId == 0) continue;
                Vector3 stop = net.m_nodes.m_buffer[nodeId].m_position;
                Vector2 delta = new Vector2(position.x - stop.x, position.z - stop.z);
                if (delta.sqrMagnitude < best * best) best = delta.magnitude;
            }
            return best;
        }

        private static void ApplyLineName(TransportManager manager, ushort lineId, string name)
        {
            if (name == null || name.Length == 0)
            {
                return;
            }

            try
            {
                System.Collections.Generic.IEnumerator<bool> enumerator = manager.SetLineName(lineId, name);
                while (enumerator.MoveNext())
                {
                }
            }
            catch
            {
            }
        }

        private static void ApplyColor(string body, ushort lineId)
        {
            string color = JsonUtil.GetString(body, "color", "");
            if (color == null || color.Length == 0)
            {
                return;
            }

            if (color.StartsWith("#"))
            {
                color = color.Substring(1);
            }

            if (color.Length != 6)
            {
                return;
            }

            try
            {
                byte r = Convert.ToByte(color.Substring(0, 2), 16);
                byte g = Convert.ToByte(color.Substring(2, 2), 16);
                byte b = Convert.ToByte(color.Substring(4, 2), 16);
                TransportManager.instance.m_lines.m_buffer[lineId].m_color = new Color32(r, g, b, 255);
            }
            catch
            {
            }
        }

        private static void ReleaseLine(TransportManager manager, ushort lineId)
        {
            string error;
            if (!TryReleaseLine(manager, lineId, out error))
            {
                throw new InvalidOperationException(error);
            }
        }

        private static bool TryReleaseLine(TransportManager manager, ushort lineId, out string error)
        {
            error = null;
            if (manager == null || lineId == 0 || lineId >= manager.m_lines.m_buffer.Length)
            {
                error = "Transport line release target is out of range: " + lineId + ".";
                return false;
            }
            try
            {
                manager.ReleaseLine(lineId);
            }
            catch (InvalidOperationException ex)
            {
                if (ex.Message == null || ex.Message.IndexOf("Already in the same thread") < 0)
                {
                    error = ex.Message;
                    return false;
                }

                MethodInfo method = typeof(TransportManager).GetMethod(
                    "ReleaseLineImplementation",
                    BindingFlags.Instance | BindingFlags.NonPublic,
                    null,
                    new Type[] { typeof(ushort), typeof(TransportLine).MakeByRefType() },
                    null);

                if (method == null)
                {
                    error = "TransportManager.ReleaseLineImplementation was not found.";
                    return false;
                }

                TransportLine line = manager.m_lines.m_buffer[lineId];
                object[] args = new object[] { lineId, line };
                try
                {
                    method.Invoke(manager, args);
                    manager.m_lines.m_buffer[lineId] = (TransportLine)args[1];
                }
                catch (Exception implementationError)
                {
                    Exception actual = implementationError is TargetInvocationException &&
                        implementationError.InnerException != null
                        ? implementationError.InnerException
                        : implementationError;
                    if ((manager.m_lines.m_buffer[lineId].m_flags & TransportLine.Flags.Created) != TransportLine.Flags.None)
                    {
                        error = actual.Message;
                        return false;
                    }
                }
            }
            if ((manager.m_lines.m_buffer[lineId].m_flags & TransportLine.Flags.Created) != TransportLine.Flags.None)
            {
                error = "Transport line release returned without removing line " + lineId + ".";
                return false;
            }
            return true;
        }

        private static string RollbackStatus(bool rolledBack, string error)
        {
            return rolledBack ? "succeeded." : "failed, restore the checkpoint. " + error;
        }
    }
}

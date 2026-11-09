using System;
using System.Text;
using ColossalFramework;
using UnityEngine;

namespace SkylinesAgentBridge
{
    public static class ServiceVehicleCommands
    {
        public static CommandResult BuildBuildingDeliveriesJson(int buildingId, int limit)
        {
            if (buildingId <= 0) return CommandResult.Fail("buildingId is required.");
            if (limit < 0) limit = 0;
            if (limit > 2048) limit = 2048;
            VehicleManager vehicles = Singleton<VehicleManager>.instance;
            StringBuilder items = new StringBuilder();
            bool first = true;
            int inbound = 0;
            int outbound = 0;
            int withPath = 0;
            int withoutPath = 0;
            int emitted = 0;

            for (ushort vehicleId = 1; vehicleId < vehicles.m_vehicles.m_buffer.Length; vehicleId++)
            {
                Vehicle vehicle = vehicles.m_vehicles.m_buffer[vehicleId];
                if ((vehicle.m_flags & Vehicle.Flags.Created) == 0) continue;
                bool isInbound = vehicle.m_targetBuilding == buildingId;
                bool isOutbound = vehicle.m_sourceBuilding == buildingId;
                if (!isInbound && !isOutbound) continue;
                bool hasPath = vehicle.m_path != 0;
                if (isInbound) inbound++;
                if (isOutbound) outbound++;
                if (hasPath) withPath++; else withoutPath++;
                if (emitted >= limit) continue;

                if (!first) items.Append(",");
                Vector3 position = vehicle.GetLastFramePosition();
                items.Append("{\"id\":").Append(vehicleId);
                items.Append(",\"prefab\":\"").Append(JsonUtil.Escape(vehicle.Info == null ? "" : vehicle.Info.name)).Append("\"");
                items.Append(",\"flags\":\"").Append(JsonUtil.Escape(vehicle.m_flags.ToString())).Append("\"");
                items.Append(",\"inbound\":").Append(JsonUtil.Bool(isInbound));
                items.Append(",\"outbound\":").Append(JsonUtil.Bool(isOutbound));
                items.Append(",\"sourceBuildingId\":").Append(vehicle.m_sourceBuilding);
                items.Append(",\"targetBuildingId\":").Append(vehicle.m_targetBuilding);
                items.Append(",\"transferType\":").Append(vehicle.m_transferType);
                items.Append(",\"transferReason\":\"").Append(JsonUtil.Escape(((TransferManager.TransferReason)vehicle.m_transferType).ToString())).Append("\"");
                items.Append(",\"pathId\":").Append(vehicle.m_path);
                items.Append(",\"hasPath\":").Append(JsonUtil.Bool(hasPath));
                items.Append(",\"waitCounter\":").Append(vehicle.m_waitCounter);
                items.Append(",\"blockCounter\":").Append(vehicle.m_blockCounter);
                items.Append(",\"position\":{\"x\":").Append(JsonUtil.Number(position.x));
                items.Append(",\"y\":").Append(JsonUtil.Number(position.y));
                items.Append(",\"z\":").Append(JsonUtil.Number(position.z)).Append("}}");
                emitted++;
                first = false;
            }

            return CommandResult.FromJson("{\"ok\":true,\"buildingId\":" + buildingId +
                ",\"inbound\":" + inbound +
                ",\"outbound\":" + outbound +
                ",\"withPath\":" + withPath +
                ",\"withoutPath\":" + withoutPath +
                ",\"returned\":" + emitted +
                ",\"vehicles\":[" + items.ToString() + "]}");
        }

        public static CommandResult BuildServiceVehiclesJson(string serviceFilter, int buildingId, int limit)
        {
            if (limit < 0) limit = 0;
            if (limit > 2048) limit = 2048;
            BuildingManager buildings = Singleton<BuildingManager>.instance;
            VehicleManager vehicles = Singleton<VehicleManager>.instance;
            StringBuilder facilities = new StringBuilder();
            bool firstFacility = true;
            int totalFacilities = 0;
            int returnedFacilities = 0;
            int totalVehicles = 0;
            int vehiclesWithPath = 0;

            for (int id = 1; id < buildings.m_buildings.m_size; id++)
            {
                if (buildingId > 0 && id != buildingId) continue;
                Building building = buildings.m_buildings.m_buffer[id];
                if ((building.m_flags & Building.Flags.Created) == Building.Flags.None) continue;
                if ((building.m_flags & Building.Flags.Original) != Building.Flags.None) continue;
                BuildingInfo info = building.Info;
                if (info == null || info.m_class == null || !IsServiceFacility(info.m_class.m_service)) continue;
                string service = info.m_class.m_service.ToString();
                if (serviceFilter.Length > 0 && !string.Equals(serviceFilter, service, StringComparison.OrdinalIgnoreCase)) continue;

                int vehicleCount = 0;
                int pathCount = 0;
                int respondingCount = 0;
                StringBuilder items = new StringBuilder();
                bool firstVehicle = true;
                bool emitFacility = returnedFacilities < limit;
                ushort vehicleId = building.m_ownVehicles;
                int guard = 0;
                while (vehicleId != 0 && guard < vehicles.m_vehicles.m_buffer.Length)
                {
                    Vehicle vehicle = vehicles.m_vehicles.m_buffer[vehicleId];
                    bool hasPath = vehicle.m_path != 0;
                    if (hasPath) pathCount++;
                    if (vehicle.m_targetBuilding != 0) respondingCount++;
                    if (emitFacility && vehicleCount < 64)
                    {
                        Vector3 position = vehicle.GetLastFramePosition();
                        if (!firstVehicle) items.Append(",");
                        items.Append("{\"id\":").Append(vehicleId);
                        items.Append(",\"prefab\":\"").Append(JsonUtil.Escape(vehicle.Info == null ? "" : vehicle.Info.name)).Append("\"");
                        items.Append(",\"flags\":\"").Append(JsonUtil.Escape(vehicle.m_flags.ToString())).Append("\"");
                        items.Append(",\"pathId\":").Append(vehicle.m_path);
                        items.Append(",\"pathPositionIndex\":").Append(vehicle.m_pathPositionIndex);
                        items.Append(",\"sourceBuildingId\":").Append(vehicle.m_sourceBuilding);
                        items.Append(",\"targetBuildingId\":").Append(vehicle.m_targetBuilding);
                        items.Append(",\"transferType\":").Append(vehicle.m_transferType);
                        items.Append(",\"position\":{\"x\":").Append(JsonUtil.Number(position.x));
                        items.Append(",\"y\":").Append(JsonUtil.Number(position.y));
                        items.Append(",\"z\":").Append(JsonUtil.Number(position.z)).Append("}}");
                        firstVehicle = false;
                    }
                    vehicleCount++;
                    totalVehicles++;
                    vehicleId = vehicle.m_nextOwnVehicle;
                    guard++;
                }

                if (emitFacility)
                {
                    if (!firstFacility) facilities.Append(",");
                    facilities.Append("{\"buildingId\":").Append(id);
                    facilities.Append(",\"prefab\":\"").Append(JsonUtil.Escape(info.name)).Append("\"");
                    facilities.Append(",\"service\":\"").Append(service).Append("\"");
                    facilities.Append(",\"active\":").Append(JsonUtil.Bool((building.m_flags & Building.Flags.Active) != Building.Flags.None));
                    facilities.Append(",\"fleet\":{\"vehicles\":").Append(vehicleCount);
                    facilities.Append(",\"withPath\":").Append(pathCount);
                    facilities.Append(",\"responding\":").Append(respondingCount);
                    facilities.Append(",\"pathUtilization\":").Append(JsonUtil.Number(vehicleCount == 0 ? 0f : (float)pathCount / vehicleCount)).Append("}");
                    facilities.Append(",\"vehicles\":[").Append(items.ToString()).Append("]}");
                    firstFacility = false;
                    returnedFacilities++;
                }
                totalFacilities++;
                vehiclesWithPath += pathCount;
            }

            return CommandResult.FromJson("{\"ok\":true,\"serviceFilter\":\"" + JsonUtil.Escape(serviceFilter) +
                "\",\"buildingId\":" + buildingId +
                ",\"totalFacilities\":" + totalFacilities +
                ",\"returnedFacilities\":" + returnedFacilities +
                ",\"totalVehicles\":" + totalVehicles +
                ",\"vehiclesWithPath\":" + vehiclesWithPath +
                ",\"facilities\":[" + facilities.ToString() + "]}");
        }

        private static bool IsServiceFacility(ItemClass.Service service)
        {
            return service == ItemClass.Service.FireDepartment ||
                service == ItemClass.Service.PoliceDepartment ||
                service == ItemClass.Service.HealthCare ||
                service == ItemClass.Service.Garbage ||
                service == ItemClass.Service.Education ||
                service == ItemClass.Service.PublicTransport ||
                service == ItemClass.Service.Water ||
                service == ItemClass.Service.Road ||
                service == ItemClass.Service.Disaster;
        }
    }
}

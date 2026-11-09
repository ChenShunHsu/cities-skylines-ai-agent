using System;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using UnityEngine;

namespace SkylinesAgentBridge
{
    public sealed class ApiServer
    {
        private readonly AgentBridge bridge;
        private readonly int port;
        private TcpListener listener;
        private Thread thread;
        private volatile bool running;

        public ApiServer(AgentBridge bridge, int port)
        {
            this.bridge = bridge;
            this.port = port;
        }

        public bool IsRunning
        {
            get { return running; }
        }

        public void Start()
        {
            if (running)
            {
                return;
            }

            listener = new TcpListener(IPAddress.Loopback, port);
            listener.Start();
            running = true;

            thread = new Thread(AcceptLoop);
            thread.IsBackground = true;
            thread.Name = "Skylines Agent Bridge API";
            thread.Start();

            Debug.Log("[SkylinesAgentBridge] API server listening on http://127.0.0.1:" + port);
        }

        private void AcceptLoop()
        {
            while (running)
            {
                try
                {
                    TcpClient client = listener.AcceptTcpClient();
                    ThreadPool.QueueUserWorkItem(HandleClient, client);
                }
                catch (Exception ex)
                {
                    Debug.Log("[SkylinesAgentBridge] API accept failed: " + ex.Message);
                }
            }
        }

        private void HandleClient(object state)
        {
            TcpClient client = (TcpClient)state;

            try
            {
                client.ReceiveTimeout = 5000;
                client.SendTimeout = 5000;

                NetworkStream stream = client.GetStream();
                HttpRequest request = HttpRequest.Read(stream);
                HttpResponse response = Route(request);
                response.Write(stream);
            }
            catch (Exception ex)
            {
                try
                {
                    HttpResponse.Json(500, "{\"ok\":false,\"error\":\"" + JsonUtil.Escape(ex.Message) + "\"}").Write(client.GetStream());
                }
                catch
                {
                }
            }
            finally
            {
                client.Close();
            }
        }

        private HttpResponse Route(HttpRequest request)
        {
            // The Bridge controls the game and has no authentication, so only local agents may
            // call it. Browsers attach Origin to cross-site requests (even CORS-simple text/plain
            // POSTs that skip preflight); agents (Python, curl, PowerShell) send none. A Host
            // other than loopback means a DNS-rebinding page reached the loopback listener.
            if (!IsLoopbackHost(request.Host) || (request.Origin != null && !IsLoopbackOrigin(request.Origin)))
            {
                return HttpResponse.Json(403, "{\"ok\":false,\"error\":\"Only local agents may call the Bridge; browser and non-loopback requests are refused.\"}");
            }

            if (request.Method == "OPTIONS")
            {
                return HttpResponse.Json(200, "{\"ok\":true}");
            }

            if (request.Method == "GET" && request.Path == "/health")
            {
                return HttpResponse.Json(200, "{\"ok\":true,\"mod\":\"Skylines Agent Bridge\",\"levelLoaded\":" + JsonUtil.Bool(bridge.LevelLoaded) + ",\"port\":" + port + "}");
            }

            if (request.Method == "GET" && request.Path == "/state/summary")
            {
                return RunOnGameThread(request, GameState.BuildSummaryJson);
            }

            if (request.Method == "GET" && request.Path == "/state/mayor-snapshot")
            {
                int detailLimit = request.GetQueryInt("detailLimit", 10);
                return RunOnGameThread(request, delegate { return MayorSnapshotCommands.BuildMayorSnapshotJson(detailLimit); });
            }

            if (request.Method == "GET" && request.Path == "/state/problems")
            {
                int limit = request.GetQueryInt("limit", 200);
                return RunOnGameThread(request, delegate { return GameState.BuildProblemsJson(limit); });
            }

            if (request.Method == "GET" && request.Path == "/state/demand")
            {
                return RunOnGameThread(request, GameState.BuildDemandJson);
            }

            if (request.Method == "GET" && request.Path == "/state/chirps")
            {
                int limit = request.GetQueryInt("limit", 50);
                return RunOnGameThread(request, delegate { return GameState.BuildChirpsJson(limit); });
            }

            if (request.Method == "GET" && request.Path == "/state/zones")
            {
                return RunOnGameThread(request, GameState.BuildZonesJson);
            }

            if (request.Method == "GET" && request.Path == "/state/zone-cell-geometry")
            {
                int blockId = request.GetQueryInt("blockId", 0);
                return RunOnGameThread(request, delegate { return ZoneCellState.Inspect(blockId); });
            }

            if (request.Method == "GET" && request.Path == "/state/zone-blocks")
            {
                int limit = request.GetQueryInt("limit", 100);
                bool onlyFullyUnzoned =
                    request.GetQueryString("onlyFullyUnzoned", "false") == "true";
                bool requireClearOccupancy =
                    request.GetQueryString("requireClearOccupancy", "false") == "true";
                bool requireDevelopmentFrontage =
                    request.GetQueryString("requireDevelopmentFrontage", "false") == "true";
                return RunOnGameThread(
                    request,
                    delegate
                    {
                        return GameState.BuildZoneBlocksJson(
                            limit,
                            onlyFullyUnzoned,
                            requireClearOccupancy,
                            requireDevelopmentFrontage);
                    });
            }

            if (request.Method == "GET" && request.Path == "/state/economy")
            {
                return RunOnGameThread(request, GameState.BuildEconomyJson);
            }

            if (request.Method == "GET" && request.Path == "/state/city-health")
            {
                return RunOnGameThread(request, GovernanceCommands.BuildCityHealthJson);
            }

            if (request.Method == "GET" && request.Path == "/state/economy-details")
            {
                return RunOnGameThread(request, GovernanceCommands.BuildEconomyDetailsJson);
            }

            if (request.Method == "GET" && request.Path == "/state/loan-options")
            {
                return RunOnGameThread(request, GovernanceCommands.BuildLoanOptionsJson);
            }

            if (request.Method == "GET" && request.Path == "/state/traffic")
            {
                int limit = request.GetQueryInt("limit", 100);
                int minimumDensity = request.GetQueryInt("minimumDensity", 64);
                return RunOnGameThread(request, delegate { return TrafficStateCommands.BuildTrafficJson(limit, minimumDensity); });
            }

            if (request.Method == "GET" && request.Path == "/state/budgets")
            {
                return RunOnGameThread(request, GovernanceCommands.BuildBudgetsJson);
            }

            if (request.Method == "GET" && request.Path == "/state/policies")
            {
                int districtId = request.GetQueryInt("districtId", 0);
                return RunOnGameThread(request, delegate { return GovernanceCommands.BuildPoliciesJson(districtId); });
            }

            if (request.Method == "GET" && request.Path == "/state/districts")
            {
                return RunOnGameThread(request, DistrictCommands.BuildDistrictsJson);
            }

            if (request.Method == "GET" && request.Path == "/state/natural-resources")
            {
                int step = request.GetQueryInt("step", 2);
                int limit = request.GetQueryInt("limit", 12);
                int minIntensity = request.GetQueryInt("minIntensity", 8);
                bool unlockedOnly = request.GetQueryString("unlockedOnly", "true") == "true";
                return RunOnGameThread(request, delegate { return WorldStateCommands.BuildNaturalResourcesJson(step, limit, minIntensity, unlockedOnly); });
            }

            if (request.Method == "GET" && request.Path == "/state/map-analysis")
            {
                float minX = request.GetQueryFloat("minX", -1000f);
                float minZ = request.GetQueryFloat("minZ", -1000f);
                float maxX = request.GetQueryFloat("maxX", 1000f);
                float maxZ = request.GetQueryFloat("maxZ", 1000f);
                int resolution = request.GetQueryInt("resolution", 16);
                float maxBuildSlope = request.GetQueryFloat("maxBuildSlope", 0.25f);
                return RunOnGameThread(request, delegate
                {
                    return WorldStateCommands.BuildMapAnalysisJson(minX, minZ, maxX, maxZ, resolution, maxBuildSlope);
                });
            }

            if (request.Method == "GET" && request.Path == "/state/wind-map")
            {
                float minX = request.GetQueryFloat("minX", -1000f);
                float minZ = request.GetQueryFloat("minZ", -1000f);
                float maxX = request.GetQueryFloat("maxX", 1000f);
                float maxZ = request.GetQueryFloat("maxZ", 1000f);
                int resolution = request.GetQueryInt("resolution", 32);
                bool unlockedOnly = request.GetQueryString("unlockedOnly", "true") == "true";
                return RunOnGameThread(request, delegate
                {
                    return WorldStateCommands.BuildWindMapJson(
                        minX, minZ, maxX, maxZ, resolution, unlockedOnly);
                });
            }

            if (request.Method == "GET" && request.Path == "/state/service-coverage")
            {
                string service = request.GetQueryString("service", "FireDepartment");
                float minX = request.GetQueryFloat("minX", -1000f);
                float minZ = request.GetQueryFloat("minZ", -1000f);
                float maxX = request.GetQueryFloat("maxX", 1000f);
                float maxZ = request.GetQueryFloat("maxZ", 1000f);
                int resolution = request.GetQueryInt("resolution", 16);
                int lowCoverageThreshold = request.GetQueryInt("lowCoverageThreshold", 32);
                return RunOnGameThread(request, delegate
                {
                    return ServiceStateCommands.BuildServiceCoverageJson(service, minX, minZ, maxX, maxZ, resolution, lowCoverageThreshold);
                });
            }

            if (request.Method == "GET" && request.Path == "/state/service-capacity")
            {
                string service = request.GetQueryString("service", "");
                return RunOnGameThread(request, delegate { return ServiceStateCommands.BuildServiceCapacityJson(service); });
            }

            if (request.Method == "GET" && request.Path == "/state/facility-influence")
            {
                ushort buildingId = (ushort)request.GetQueryInt("buildingId", 0);
                string service = request.GetQueryString("service", "");
                bool includeInactive = request.GetQueryString("includeInactive", "false") == "true";
                int limit = request.GetQueryInt("limit", 500);
                return RunOnGameThread(request, delegate
                {
                    return ServiceImpactCommands.BuildFacilityInfluenceJson(buildingId, service, includeInactive, limit);
                });
            }

            if (request.Method == "GET" && request.Path == "/state/utility-coverage")
            {
                float minX = request.GetQueryFloat("minX", -1000f);
                float minZ = request.GetQueryFloat("minZ", -1000f);
                float maxX = request.GetQueryFloat("maxX", 1000f);
                float maxZ = request.GetQueryFloat("maxZ", 1000f);
                int resolution = request.GetQueryInt("resolution", 16);
                return RunOnGameThread(request, delegate
                {
                    return ServiceStateCommands.BuildUtilityCoverageJson(minX, minZ, maxX, maxZ, resolution);
                });
            }

            if (request.Method == "GET" && request.Path == "/state/water-service-coverage")
            {
                int limit = request.GetQueryInt("limit", 500);
                string service = request.GetQueryString("service", "");
                bool includeCovered = request.GetQueryString("includeCovered", "false") == "true";
                bool includeNoDemand = request.GetQueryString("includeNoDemand", "false") == "true";
                return RunOnGameThread(request, delegate
                {
                    return ServiceStateCommands.BuildWaterServiceCoverageJson(limit, service, includeCovered, includeNoDemand);
                });
            }

            if (request.Method == "GET" && request.Path == "/state/electricity-service-coverage")
            {
                int limit = request.GetQueryInt("limit", 500);
                string service = request.GetQueryString("service", "");
                bool includeCovered = request.GetQueryString("includeCovered", "false") == "true";
                bool includeNoDemand = request.GetQueryString("includeNoDemand", "false") == "true";
                return RunOnGameThread(request, delegate
                {
                    return ServiceStateCommands.BuildElectricityServiceCoverageJson(limit, service, includeCovered, includeNoDemand);
                });
            }

            if (request.Method == "GET" && request.Path == "/state/electricity-connection-area")
            {
                int buildingId = request.GetQueryInt("buildingId", 0);
                float padding = request.GetQueryFloat("padding", 512f);
                int resolution = request.GetQueryInt("resolution", 48);
                return RunOnGameThread(request, delegate
                {
                    return ServiceStateCommands.BuildElectricityConnectionAreaJson(
                        buildingId, padding, resolution);
                });
            }

            if (request.Method == "GET" && request.Path == "/state/facilities")
            {
                int limit = request.GetQueryInt("limit", 500);
                string service = request.GetQueryString("service", "");
                bool includeMapObjects = request.GetQueryString("includeMapObjects", "false") == "true";
                return RunOnGameThread(request, delegate { return GameState.BuildFacilitiesJson(limit, service, includeMapObjects); });
            }

            if (request.Method == "GET" && request.Path == "/state/growables")
            {
                int limit = request.GetQueryInt("limit", 500);
                string service = request.GetQueryString("service", "");
                return RunOnGameThread(request, delegate { return GameState.BuildGrowablesJson(limit, service); });
            }

            if (request.Method == "GET" && request.Path == "/state/networks")
            {
                int limit = request.GetQueryInt("limit", 500);
                string service = request.GetQueryString("service", "");
                return RunOnGameThread(request, delegate { return GameState.BuildNetworksJson(limit, service); });
            }

            if (request.Method == "GET" && request.Path == "/state/segment-routes")
            {
                ushort id = (ushort)request.GetQueryInt("id", 0);
                int limit = request.GetQueryInt("limit", 10);
                string vehicleAI = request.GetQueryString("vehicleAI", "");
                return RunOnGameThread(request, delegate { return TrafficRouteCommands.BuildSegmentRoutesJson(id, limit, vehicleAI); });
            }

            if (request.Method == "GET" && request.Path == "/state/network-segment-details")
            {
                ushort id = (ushort)request.GetQueryInt("id", 0);
                return RunOnGameThread(request, delegate { return GameState.BuildNetworkSegmentDetailsJson(id); });
            }

            if (request.Method == "GET" && request.Path == "/state/network-node-details")
            {
                ushort id = (ushort)request.GetQueryInt("id", 0);
                return RunOnGameThread(request, delegate { return GameState.BuildNetworkNodeDetailsJson(id); });
            }

            if (request.Method == "GET" && request.Path == "/state/road-reachability")
            {
                ushort id = (ushort)request.GetQueryInt("id", 0);
                return RunOnGameThread(request, delegate { return RoadReachabilityCommands.BuildRoadReachabilityJson(id); });
            }

            if (request.Method == "GET" && request.Path == "/state/road-anomalies")
            {
                int limit = request.GetQueryInt("limit", 200);
                float nearMissDistance = request.GetQueryFloat("nearMissDistance", 16f);
                float shortSegmentLength = request.GetQueryFloat("shortSegmentLength", 28f);
                bool includeDeadEnds = request.GetQueryString("includeDeadEnds", "true") == "true";
                return RunOnGameThread(request, delegate { return GameState.BuildRoadAnomaliesJson(limit, nearMissDistance, shortSegmentLength, includeDeadEnds); });
            }

            if (request.Method == "GET" && request.Path == "/state/external-connections")
            {
                int limit = request.GetQueryInt("limit", 50);
                return RunOnGameThread(request, delegate { return GameState.BuildExternalConnectionsJson(limit); });
            }

            if (request.Method == "POST" && request.Path == "/commands/repair-half-released-buildings")
            {
                string body = request.Body;
                return RunOnGameThread(request, delegate { return BuildingReleaseRepair.Repair(body); });
            }

            if (request.Method == "GET" && request.Path == "/state/building-grid-check")
            {
                int limit = request.GetQueryInt("limit", 200);
                return RunOnGameThread(request, delegate { return BuildingGridCheck.BuildJson(limit); });
            }

            if (request.Method == "GET" && request.Path == "/state/building-anomalies")
            {
                int limit = request.GetQueryInt("limit", 200);
                float roadClearance = request.GetQueryFloat("roadClearance", 0f);
                bool includeOriginal = request.GetQueryString("includeOriginal", "false") == "true";
                return RunOnGameThread(request, delegate { return GameState.BuildBuildingAnomaliesJson(limit, roadClearance, includeOriginal); });
            }

            if (request.Method == "GET" && request.Path == "/state/zone-anomalies")
            {
                int limit = request.GetQueryInt("limit", 200);
                int minMinorityCells = request.GetQueryInt("minMinorityCells", 3);
                int minUnzonedCells = request.GetQueryInt("minUnzonedCells", 6);
                bool includeUnzonedHoles = request.GetQueryString("includeUnzonedHoles", "true") == "true";
                return RunOnGameThread(request, delegate { return GameState.BuildZoneAnomaliesJson(limit, minMinorityCells, minUnzonedCells, includeUnzonedHoles); });
            }

            if (request.Method == "GET" && request.Path == "/state/saves")
            {
                return RunOnGameThread(request, SaveCommands.ListSaves);
            }

            if (request.Method == "GET" && request.Path == "/state/save-status")
            {
                // answered here, off the game thread, so it stays quick while a save holds that thread
                CommandResult status = SaveCommands.BuildSaveStatusJson(request.GetQueryString("name", ""));
                return HttpResponse.Json(status.Ok ? 200 : 400, status.Json);
            }

            if (request.Method == "GET" && request.Path == "/state/terrain-heights")
            {
                string points = request.GetQueryString("points", "");
                return RunOnGameThread(request, delegate { return TerrainHeightCommands.Sample(points); });
            }

            if (request.Method == "GET" && request.Path == "/state/restore-status")
            {
                return RunOnGameThread(request, SaveCommands.BuildRestoreStatusJson);
            }

            if (request.Method == "GET" && request.Path == "/state/transport-lines")
            {
                int limit = request.GetQueryInt("limit", 256);
                return RunOnGameThread(request, delegate { return TransportCommands.BuildTransportLinesJson(limit); });
            }

            if (request.Method == "GET" && request.Path == "/state/transport-vehicles")
            {
                int limit = request.GetQueryInt("limit", 256);
                ushort lineId = (ushort)request.GetQueryInt("lineId", 0);
                return RunOnGameThread(request, delegate { return TransportCommands.BuildTransportVehiclesJson(lineId, limit); });
            }

            if (request.Method == "GET" && request.Path == "/state/transport-line-anomalies")
            {
                int limit = request.GetQueryInt("limit", 256);
                return RunOnGameThread(request, delegate { return TransportCommands.BuildTransportLineAnomaliesJson(limit); });
            }

            if (request.Method == "GET" && request.Path == "/state/transport-station-anomalies")
            {
                int limit = request.GetQueryInt("limit", 256);
                float maxStopDistance = request.GetQueryFloat("maxStopDistance", 96f);
                return RunOnGameThread(request, delegate { return TransportCommands.BuildTransportStationAnomaliesJson(limit, maxStopDistance); });
            }

            if (request.Method == "GET" && request.Path == "/state/transport-line-paths")
            {
                ushort lineId = (ushort)request.GetQueryInt("lineId", 0);
                return RunOnGameThread(request, delegate { return TransportCommands.BuildTransportLinePathDetailsJson(lineId); });
            }

            if (request.Method == "GET" && request.Path == "/state/service-vehicles")
            {
                string service = request.GetQueryString("service", "");
                int buildingId = request.GetQueryInt("buildingId", 0);
                int limit = request.GetQueryInt("limit", 256);
                return RunOnGameThread(request, delegate { return ServiceVehicleCommands.BuildServiceVehiclesJson(service, buildingId, limit); });
            }

            if (request.Method == "GET" && request.Path == "/state/building-deliveries")
            {
                int buildingId = request.GetQueryInt("buildingId", 0);
                int limit = request.GetQueryInt("limit", 256);
                return RunOnGameThread(request, delegate { return ServiceVehicleCommands.BuildBuildingDeliveriesJson(buildingId, limit); });
            }

            if (request.Method == "GET" && request.Path == "/state/regression-report")
            {
                string name = request.GetQueryString("name", "default");
                return RunOnGameThread(request, delegate { return RegressionCommands.BuildRegressionReport(name); });
            }

            if (request.Method == "GET" && request.Path == "/state/map-areas")
            {
                return RunOnGameThread(request, AreaCommands.BuildMapAreasJson);
            }

            if (request.Method == "GET" && request.Path == "/state/game-settings")
            {
                return RunOnGameThread(request, SettingsCommands.BuildGameSettingsJson);
            }

            if (request.Method == "GET" && request.Path == "/state/unlocks")
            {
                return RunOnGameThread(request, UnlockStateCommands.BuildUnlocksJson);
            }

            if (request.Method == "GET" && request.Path == "/state/toolbar-unlocks")
            {
                return RunOnGameThread(request, UiStateCommands.BuildToolbarUnlocksJson);
            }

            if (request.Method == "GET" && request.Path == "/state/terrain-editing")
            {
                return RunOnGameThread(request, TerrainEditingState.Inspect);
            }

            if (request.Method == "GET" && request.Path == "/state/education")
            {
                return RunOnGameThread(request, EducationCommands.BuildEducationJson);
            }

            if (request.Method == "GET" && request.Path == "/prefabs/roads")
            {
                return RunOnGameThread(request, GameState.BuildRoadPrefabsJson);
            }

            if (request.Method == "GET" && request.Path == "/prefabs/networks")
            {
                string service = request.GetQueryString("service", "");
                return RunOnGameThread(request, delegate { return GameState.BuildNetworkPrefabsJson(service); });
            }

            if (request.Method == "GET" && request.Path == "/prefabs/buildings")
            {
                string service = request.GetQueryString("service", "");
                return RunOnGameThread(request, delegate { return GameState.BuildBuildingPrefabsJson(service); });
            }

            if (request.Method == "POST" && request.Path == "/commands/build-road")
            {
                string body = request.Body;
                return RunOnGameThread(request, delegate { return RoadCommands.BuildRoad(body); });
            }

            if (request.Method == "POST" && request.Path == "/commands/build-network")
            {
                string body = request.Body;
                return RunOnGameThread(request, delegate { return RoadCommands.BuildRoad(body); });
            }

            if (request.Method == "POST" && request.Path == "/commands/finalize-road-build")
            {
                string body = request.Body;
                return RunOnGameThread(request, delegate { return RoadCommands.FinalizeRoadBuild(body); });
            }

            if (request.Method == "POST" && request.Path == "/commands/set-zone")
            {
                string body = request.Body;
                return RunOnGameThread(request, delegate { return ZoneCommands.SetZone(body); });
            }

            if (request.Method == "POST" && request.Path == "/commands/repair-zones-to-growables")
            {
                string body = request.Body;
                return RunOnGameThread(request, delegate { return ZoneCommands.RepairZonesToGrowables(body); });
            }

            if (request.Method == "POST" && request.Path == "/commands/repair-zone-clusters")
            {
                string body = request.Body;
                return RunOnGameThread(request, delegate { return ZoneCommands.RepairZoneClusters(body); });
            }

            if (request.Method == "POST" && request.Path == "/commands/place-building")
            {
                string body = request.Body;
                return RunOnGameThread(request, delegate { return BuildingCommands.PlaceBuilding(body); });
            }

            if (request.Method == "POST" && request.Path == "/commands/place-education-facility")
            {
                string body = request.Body;
                return RunOnGameThread(request, delegate { return EducationCommands.PlaceEducationFacility(body); });
            }

            if (request.Method == "POST" && request.Path == "/commands/move-building")
            {
                string body = request.Body;
                return RunOnGameThread(request, delegate { return BuildingCommands.MoveBuilding(body); });
            }

            if (request.Method == "POST" && request.Path == "/commands/set-building-active")
            {
                string body = request.Body;
                return RunOnGameThread(request, delegate { return BuildingCommands.SetBuildingActive(body); });
            }

            if (request.Method == "POST" && request.Path == "/commands/disable-blocked-assets")
            {
                return RunOnGameThread(request, AssetCommands.DisableBlockedAssets);
            }

            if (request.Method == "POST" && request.Path == "/commands/set-simulation-speed")
            {
                string body = request.Body;
                return RunOnGameThread(request, delegate { return SimulationCommands.SetSimulationSpeed(body); });
            }

            if (request.Method == "POST" && request.Path == "/commands/set-tax-rate")
            {
                string body = request.Body;
                return RunOnGameThread(request, delegate { return EconomyCommands.SetTaxRate(body); });
            }

            if (request.Method == "POST" && request.Path == "/commands/preview-placement")
            {
                string body = request.Body;
                return RunOnGameThread(request, delegate { return PlacementCommands.PreviewBuildingPlacement(body); });
            }

            if (request.Method == "POST" && request.Path == "/commands/preview-service-impact")
            {
                string body = request.Body;
                return RunOnGameThread(request, delegate { return ServiceImpactCommands.PreviewServiceImpact(body); });
            }

            if (request.Method == "POST" && request.Path == "/commands/preview-bulldoze-impact")
            {
                string body = request.Body;
                return RunOnGameThread(request, delegate { return BulldozeCommands.PreviewBulldozeImpact(body); });
            }

            if (request.Method == "POST" && request.Path == "/commands/preview-network")
            {
                string body = request.Body;
                return RunOnGameThread(request, delegate { return RoadCommands.PreviewNetwork(body); });
            }

            if (request.Method == "POST" && request.Path == "/commands/upgrade-road")
            {
                string body = request.Body;
                return RunOnGameThread(request, delegate { return RoadCommands.UpgradeRoadSegment(body); });
            }

            if (request.Method == "GET" && request.Path == "/state/junction")
            {
                ushort id = (ushort)request.GetQueryInt("id", 0);
                return RunOnGameThread(request, delegate { return JunctionCommands.BuildJunctionJson(id); });
            }

            if (request.Method == "POST" && request.Path == "/commands/set-junction-control")
            {
                string body = request.Body;
                return RunOnGameThread(request, delegate { return JunctionCommands.SetJunctionControl(body); });
            }

            if (request.Method == "POST" && request.Path == "/commands/split-road")
            {
                string body = request.Body;
                return RunOnGameThread(request, delegate { return RoadCommands.SplitRoadSegment(body); });
            }

            if (request.Method == "POST" && request.Path == "/commands/reverse-road")
            {
                string body = request.Body;
                return RunOnGameThread(request, delegate { return RoadCommands.ReverseRoadSegment(body); });
            }

            if (request.Method == "POST" && request.Path == "/commands/set-service-budget")
            {
                string body = request.Body;
                return RunOnGameThread(request, delegate { return GovernanceCommands.SetServiceBudget(body); });
            }

            if (request.Method == "POST" && request.Path == "/commands/take-loan")
            {
                string body = request.Body;
                return RunOnGameThread(request, delegate { return GovernanceCommands.TakeLoan(body); });
            }

            if (request.Method == "POST" && request.Path == "/commands/pay-loan")
            {
                string body = request.Body;
                return RunOnGameThread(request, delegate { return GovernanceCommands.PayLoan(body); });
            }

            if (request.Method == "POST" && request.Path == "/commands/set-policy")
            {
                string body = request.Body;
                return RunOnGameThread(request, delegate { return GovernanceCommands.SetPolicy(body); });
            }

            if (request.Method == "POST" && request.Path == "/commands/create-district")
            {
                string body = request.Body;
                return RunOnGameThread(request, delegate { return DistrictCommands.CreateDistrict(body); });
            }

            if (request.Method == "POST" && request.Path == "/commands/set-district-specialization")
            {
                string body = request.Body;
                return RunOnGameThread(request, delegate { return DistrictCommands.SetDistrictSpecialization(body); });
            }

            if (request.Method == "POST" && request.Path == "/commands/update-district")
            {
                string body = request.Body;
                return RunOnGameThread(request, delegate { return DistrictCommands.UpdateDistrictBoundary(body); });
            }

            if (request.Method == "POST" && request.Path == "/commands/remove-district")
            {
                string body = request.Body;
                return RunOnGameThread(request, delegate { return DistrictCommands.RemoveDistrict(body); });
            }

            if (request.Method == "POST" && request.Path == "/commands/set-district-name")
            {
                string body = request.Body;
                return RunOnGameThread(request, delegate { return DistrictCommands.SetDistrictName(body); });
            }

            if (request.Method == "GET" && request.Path == "/state/zoning-support")
            {
                float x = request.GetQueryFloat("x", 0f);
                float z = request.GetQueryFloat("z", 0f);
                float radius = Mathf.Clamp(request.GetQueryFloat("radius", 128f), 8f, 1920f);
                int limit = request.GetQueryInt("limit", 64);
                return RunOnGameThread(request, delegate { return BuildingZoningSupport.Inspect(x, z, radius, limit); });
            }

            if (request.Method == "POST" && request.Path == "/commands/repair-building-zoning")
            {
                string body = request.Body;
                return RunOnGameThread(request, delegate { return BuildingZoningSupport.Repair(body); });
            }

            if (request.Method == "POST" && request.Path == "/commands/select-instance")
            {
                string body = request.Body;
                return RunOnGameThread(request, delegate { return UiStateCommands.SelectInstance(body); });
            }

            if (request.Method == "POST" && request.Path == "/commands/bulldoze")
            {
                string body = request.Body;
                return RunOnGameThread(request, delegate { return BulldozeCommands.Bulldoze(body); });
            }

            if (request.Method == "POST" && request.Path == "/commands/save")
            {
                string body = request.Body;
                return RunOnGameThread(request, delegate { return SaveCommands.Save(body); });
            }

            if (request.Method == "POST" && request.Path == "/commands/create-checkpoint")
            {
                string body = request.Body;
                return RunOnGameThread(request, delegate { return SaveCommands.CreateCheckpoint(body); });
            }

            if (request.Method == "POST" && request.Path == "/commands/restore-checkpoint")
            {
                string body = request.Body;
                return RunOnGameThread(request, delegate { return SaveCommands.RestoreCheckpoint(body); });
            }

            if (request.Method == "POST" && request.Path == "/commands/create-transport-line")
            {
                string body = request.Body;
                return RunOnGameThread(request, delegate { return TransportCommands.CreateTransportLine(body); });
            }

            if (request.Method == "POST" && request.Path == "/commands/release-transport-line")
            {
                string body = request.Body;
                return RunOnGameThread(request, delegate { return TransportCommands.ReleaseTransportLine(body); });
            }

            if (request.Method == "POST" && request.Path == "/commands/assign-transport-line-depot")
            {
                string body = request.Body;
                return RunOnGameThread(request, delegate { return TransportCommands.AssignTransportLineDepot(body); });
            }

            if (request.Method == "POST" && request.Path == "/commands/refresh-transport-network")
            {
                string body = request.Body;
                return RunOnGameThread(request, delegate { return TransportCommands.RefreshTransportNetwork(body); });
            }

            if (request.Method == "POST" && request.Path == "/commands/set-transport-line")
            {
                string body = request.Body;
                return RunOnGameThread(request, delegate { return TransportCommands.SetTransportLineSettings(body); });
            }

            if (request.Method == "POST" && request.Path == "/commands/delete-transport-line")
            {
                string body = request.Body;
                return RunOnGameThread(request, delegate { return TransportCommands.DeleteTransportLine(body); });
            }

            if (request.Method == "POST" && request.Path == "/commands/add-transport-stop")
            {
                string body = request.Body;
                return RunOnGameThread(request, delegate { return TransportCommands.AddTransportStop(body); });
            }

            if (request.Method == "POST" && request.Path == "/commands/move-transport-stop")
            {
                string body = request.Body;
                return RunOnGameThread(request, delegate { return TransportCommands.MoveTransportStop(body); });
            }

            if (request.Method == "POST" && request.Path == "/commands/remove-transport-stop")
            {
                string body = request.Body;
                return RunOnGameThread(request, delegate { return TransportCommands.RemoveTransportStop(body); });
            }

            if (request.Method == "POST" && request.Path == "/commands/purchase-map-area")
            {
                string body = request.Body;
                return RunOnGameThread(request, delegate { return AreaCommands.PurchaseMapArea(body); });
            }

            if (request.Method == "POST" && request.Path == "/commands/unlock-map-areas")
            {
                string body = request.Body;
                return RunOnGameThread(request, delegate { return AreaCommands.UnlockMapAreas(body); });
            }

            if (request.Method == "POST" && request.Path == "/commands/set-autosave")
            {
                string body = request.Body;
                return RunOnGameThread(request, delegate { return SettingsCommands.SetAutoSave(body); });
            }

            if (request.Method == "POST" && request.Path == "/commands/capture-regression-baseline")
            {
                string body = request.Body;
                return RunOnGameThread(request, delegate { return RegressionCommands.CaptureBaseline(body); });
            }

            if (request.Method == "POST" && request.Path == "/commands/batch")
            {
                string body = request.Body;
                return RunOnGameThread(request, delegate { return BatchCommands.Execute(body); });
            }

            return HttpResponse.Json(404, "{\"ok\":false,\"error\":\"Not found\"}");
        }

        // saves and the game settings go through the main thread's save panel and settings files;
        // everything else runs on the simulation thread (CommandQueue.RunSync)
        private static readonly System.Collections.Generic.HashSet<string> MainThreadPaths =
            new System.Collections.Generic.HashSet<string>
            {
                "/state/saves", "/state/save-status", "/state/restore-status", "/state/game-settings",
                "/commands/save", "/commands/create-checkpoint", "/commands/restore-checkpoint",
                "/commands/set-autosave"
            };

        private HttpResponse RunOnGameThread(HttpRequest request, Func<CommandResult> action)
        {
            if (!bridge.LevelLoaded)
            {
                return HttpResponse.Json(409, "{\"ok\":false,\"error\":\"No city is loaded.\"}");
            }

            CommandResult result = bridge.Queue.RunSync(action, 10000, !MainThreadPaths.Contains(request.Path));
            // shown on the next frame; no second game-thread command to wait for
            AgentBridgeNotifier.Post((result.Ok ? "API OK: " : "API FAIL: ") + DescribeRequest(request));
            return HttpResponse.Json(result.Ok ? 200 : 500, result.Json);
        }

        private static string DescribeRequest(HttpRequest request)
        {
            string body = request.Body == null ? "" : request.Body;

            if (request.Method == "GET")
            {
                if (request.Path == "/state/summary") return "Read city summary";
                if (request.Path == "/state/mayor-snapshot") return "Read adaptive mayor snapshot";
                if (request.Path == "/state/problems") return "Read city problems";
                if (request.Path == "/state/demand") return "Read zone demand";
                if (request.Path == "/state/chirps") return "Read citizen chirps";
                if (request.Path == "/state/zones") return "Read zoning summary";
                if (request.Path == "/state/economy") return "Read economy state";
                if (request.Path == "/state/city-health") return "Read city health";
                if (request.Path == "/state/economy-details") return "Read economy details";
                if (request.Path == "/state/traffic") return "Read traffic state";
                if (request.Path == "/state/segment-routes") return "Read segment traffic routes";
                if (request.Path == "/state/junction") return "Read junction controls";
                if (request.Path == "/state/budgets") return "Read service budgets";
                if (request.Path == "/state/policies") return "Read policies";
                if (request.Path == "/state/districts") return "Read districts";
                if (request.Path == "/state/natural-resources") return "Read natural resources";
                if (request.Path == "/state/map-analysis") return "Analyze map";
                if (request.Path == "/state/wind-map") return "Read native wind resource map";
                if (request.Path == "/state/service-coverage") return "Read service coverage";
                if (request.Path == "/state/service-capacity") return "Read service capacity";
                if (request.Path == "/state/facility-influence") return "Inspect facility service and noise influence";
                if (request.Path == "/state/utility-coverage") return "Read utility coverage";
                if (request.Path == "/state/water-service-coverage") return "Inspect exact building water service coverage";
                if (request.Path == "/state/electricity-service-coverage") return "Inspect exact building electricity coverage";
                if (request.Path == "/state/electricity-connection-area") return "Inspect building electricity connection area";
                if (request.Path == "/state/unlocks") return "Read unlock state";
                if (request.Path == "/state/toolbar-unlocks") return "Read actual toolbar unlock state";
                if (request.Path == "/state/facilities") return "Read facilities";
                if (request.Path == "/state/growables") return "Read growable buildings";
                if (request.Path == "/state/zoning-support") return "Read growable zoning support";
                if (request.Path == "/state/networks") return "Read networks";
                if (request.Path == "/state/network-segment-details") return "Read network segment details";
                if (request.Path == "/state/network-node-details") return "Read network node details";
                if (request.Path == "/state/road-reachability") return "Inspect directed road reachability";
                if (request.Path == "/state/road-anomalies") return "Inspect road anomalies";
                if (request.Path == "/state/external-connections") return "Inspect external connections";
                if (request.Path == "/state/building-grid-check") return "Check buildings against the building grid";
                if (request.Path == "/state/building-anomalies") return "Inspect building placement";
                if (request.Path == "/state/zone-anomalies") return "Inspect zoning anomalies";
                if (request.Path == "/state/saves") return "List saves";
                if (request.Path == "/state/terrain-heights") return "Sample raw and detail terrain heights";
                if (request.Path == "/state/transport-lines") return "Read transport lines";
                if (request.Path == "/state/transport-vehicles") return "Read transport vehicles";
                if (request.Path == "/state/transport-line-anomalies") return "Inspect transport line anomalies";
                if (request.Path == "/state/transport-station-anomalies") return "Inspect transport station anomalies";
                if (request.Path == "/state/transport-line-paths") return "Inspect transport line paths";
                if (request.Path == "/state/building-deliveries") return "Inspect building freight vehicles";
                if (request.Path == "/prefabs/roads") return "List road prefabs";
                if (request.Path == "/prefabs/networks") return "List network prefabs";
                if (request.Path == "/prefabs/buildings") return "List building prefabs";
                return "GET " + request.Path;
            }

            if (request.Path == "/commands/build-network" || request.Path == "/commands/build-road")
            {
                string prefab = JsonUtil.GetString(body, "roadPrefab", "network");
                return "Build network " + prefab;
            }

            if (request.Path == "/commands/finalize-road-build")
            {
                return "Finalize road #" + ((int)JsonUtil.GetNumber(body, "segmentId", 0f)).ToString();
            }

            if (request.Path == "/commands/set-zone")
            {
                return "Set zone " + JsonUtil.GetString(body, "zone", "");
            }

            if (request.Path == "/commands/repair-zones-to-growables")
            {
                return "Repair zones to growables";
            }

            if (request.Path == "/commands/repair-zone-clusters")
            {
                return "Repair zone clusters";
            }

            if (request.Path == "/commands/place-building")
            {
                return "Place building " + JsonUtil.GetString(body, "buildingPrefab", "");
            }

            if (request.Path == "/commands/move-building")
            {
                return "Move building #" + ((int)JsonUtil.GetNumber(body, "id", 0f)).ToString();
            }

            if (request.Path == "/commands/set-building-active")
            {
                return "Set building active #" + ((int)JsonUtil.GetNumber(body, "id", 0f)).ToString();
            }

            if (request.Path == "/commands/disable-blocked-assets")
            {
                return "Disable blocked assets";
            }

            if (request.Path == "/commands/repair-building-zoning")
            {
                return "Repair zoning under building #" + ((int)JsonUtil.GetNumber(body, "buildingId", 0f)).ToString();
            }

            if (request.Path == "/commands/select-instance")
            {
                return "Select " + JsonUtil.GetString(body, "entityType", "entity") + " #" + ((int)JsonUtil.GetNumber(body, "id", 0f)).ToString();
            }

            if (request.Path == "/commands/bulldoze")
            {
                string entityType = JsonUtil.GetString(body, "entityType", "entity");
                return "Bulldoze " + entityType + " #" + ((int)JsonUtil.GetNumber(body, "id", 0f)).ToString();
            }

            if (request.Path == "/commands/save")
            {
                return "Save city " + JsonUtil.GetString(body, "name", "AgentAutoSave");
            }

            if (request.Path == "/commands/create-transport-line")
            {
                return "Create transport line " + JsonUtil.GetString(body, "transportType", "Bus");
            }

            if (request.Path == "/commands/release-transport-line")
            {
                return "Release transport line #" + ((int)JsonUtil.GetNumber(body, "id", 0f)).ToString();
            }

            if (request.Path == "/commands/assign-transport-line-depot")
            {
                return "Assign depot to transport line #" + ((int)JsonUtil.GetNumber(body, "id", 0f)).ToString();
            }

            if (request.Path == "/commands/refresh-transport-network")
            {
                return "Refresh transport network " + JsonUtil.GetString(body, "transportType", "Metro");
            }

            if (request.Path == "/commands/set-simulation-speed")
            {
                if (JsonUtil.GetBool(body, "paused", false))
                {
                    return "Pause simulation";
                }
                return "Set simulation speed " + ((int)JsonUtil.GetNumber(body, "speed", 0f)).ToString();
            }

            if (request.Path == "/commands/set-tax-rate")
            {
                return "Set tax rate " + ((int)JsonUtil.GetNumber(body, "rate", 0f)).ToString();
            }

            if (request.Path == "/commands/preview-placement")
            {
                return "Preview placement " + JsonUtil.GetString(body, "buildingPrefab", "");
            }

            if (request.Path == "/commands/preview-service-impact")
            {
                return "Preview service impact " + JsonUtil.GetString(body, "buildingPrefab", "");
            }

            if (request.Path == "/commands/preview-bulldoze-impact")
            {
                string entityType = JsonUtil.GetString(body, "entityType", "entity");
                return "Preview bulldoze impact " + entityType + " #" + ((int)JsonUtil.GetNumber(body, "id", 0f)).ToString();
            }

            if (request.Path == "/commands/preview-network")
            {
                return "Preview network " + JsonUtil.GetString(body, "roadPrefab", "");
            }

            if (request.Path == "/commands/set-service-budget")
            {
                return "Set service budget " + JsonUtil.GetString(body, "service", "");
            }

            if (request.Path == "/commands/set-policy")
            {
                return "Set policy " + JsonUtil.GetString(body, "policy", "");
            }

            if (request.Path == "/commands/create-district")
            {
                return "Create district";
            }

            if (request.Path == "/commands/set-district-specialization")
            {
                return "Set district specialization " + JsonUtil.GetString(body, "specialization", "");
            }

            if (request.Path == "/commands/remove-district")
            {
                return "Remove district #" + ((int)JsonUtil.GetNumber(body, "districtId", 0f)).ToString();
            }

            if (request.Path == "/commands/set-district-name")
            {
                return "Set district name #" + ((int)JsonUtil.GetNumber(body, "districtId", 0f)).ToString();
            }

            if (request.Path == "/commands/create-checkpoint")
            {
                return "Create checkpoint " + JsonUtil.GetString(body, "name", "");
            }

            if (request.Path == "/commands/batch")
            {
                return "Run batch commands";
            }

            return request.Method + " " + request.Path;
        }

        private static bool IsLoopbackHost(string host)
        {
            if (host == null) return true;  // HTTP/1.0 clients may omit Host
            string name = host.Trim().ToLowerInvariant();
            if (name.StartsWith("["))
            {
                int close = name.IndexOf(']');
                name = close > 0 ? name.Substring(0, close + 1) : name;
            }
            else
            {
                int colon = name.IndexOf(':');
                if (colon >= 0) name = name.Substring(0, colon);
            }
            return name == "127.0.0.1" || name == "localhost" || name == "[::1]";
        }

        private static bool IsLoopbackOrigin(string origin)
        {
            if (origin == "null") return false;  // sandboxed or file pages
            int scheme = origin.IndexOf("://");
            return scheme > 0 && IsLoopbackHost(origin.Substring(scheme + 3));
        }

        private sealed class HttpRequest
        {
            public string Method;
            public string Path;
            public string Query;
            public string Body;
            public string Host;
            public string Origin;

            public int GetQueryInt(string name, int defaultValue)
            {
                if (Query == null || Query.Length == 0)
                {
                    return defaultValue;
                }

                string[] pairs = Query.Split('&');
                for (int i = 0; i < pairs.Length; i++)
                {
                    string[] parts = pairs[i].Split('=');
                    if (parts.Length == 2 && parts[0] == name)
                    {
                        int value;
                        if (int.TryParse(parts[1], out value))
                        {
                            return value;
                        }
                    }
                }

                return defaultValue;
            }

            public float GetQueryFloat(string name, float defaultValue)
            {
                if (Query == null || Query.Length == 0)
                {
                    return defaultValue;
                }

                string[] pairs = Query.Split('&');
                for (int i = 0; i < pairs.Length; i++)
                {
                    string[] parts = pairs[i].Split('=');
                    if (parts.Length == 2 && parts[0] == name)
                    {
                        float value;
                        if (float.TryParse(parts[1], out value))
                        {
                            return value;
                        }
                    }
                }

                return defaultValue;
            }

            public string GetQueryString(string name, string defaultValue)
            {
                if (Query == null || Query.Length == 0)
                {
                    return defaultValue;
                }

                string[] pairs = Query.Split('&');
                for (int i = 0; i < pairs.Length; i++)
                {
                    string[] parts = pairs[i].Split('=');
                    if (parts.Length == 2 && parts[0] == name)
                    {
                        return Uri.UnescapeDataString(parts[1].Replace("+", " "));
                    }
                }

                return defaultValue;
            }

            public static HttpRequest Read(NetworkStream stream)
            {
                MemoryStream headerBytes = new MemoryStream();
                int matched = 0;

                while (true)
                {
                    int b = stream.ReadByte();
                    if (b < 0)
                    {
                        break;
                    }

                    headerBytes.WriteByte((byte)b);

                    if ((matched == 0 && b == '\r') ||
                        (matched == 1 && b == '\n') ||
                        (matched == 2 && b == '\r') ||
                        (matched == 3 && b == '\n'))
                    {
                        matched++;
                        if (matched == 4)
                        {
                            break;
                        }
                    }
                    else
                    {
                        matched = b == '\r' ? 1 : 0;
                    }

                    if (headerBytes.Length > 65536)
                    {
                        throw new InvalidOperationException("Request headers are too large.");
                    }
                }

                string headers = Encoding.UTF8.GetString(headerBytes.ToArray());
                string[] lines = headers.Split(new string[] { "\r\n" }, StringSplitOptions.None);
                string[] first = lines[0].Split(' ');
                int contentLength = 0;
                string host = null;
                string origin = null;

                for (int i = 1; i < lines.Length; i++)
                {
                    string line = lines[i];
                    int colon = line.IndexOf(':');
                    if (colon <= 0) continue;
                    string name = line.Substring(0, colon);
                    string value = line.Substring(colon + 1).Trim();
                    if (string.Compare(name, "Content-Length", true) == 0)
                    {
                        int.TryParse(value, out contentLength);
                    }
                    else if (string.Compare(name, "Host", true) == 0)
                    {
                        host = value;
                    }
                    else if (string.Compare(name, "Origin", true) == 0)
                    {
                        origin = value;
                    }
                }

                byte[] bodyBytes = new byte[contentLength];
                int offset = 0;
                while (offset < contentLength)
                {
                    int read = stream.Read(bodyBytes, offset, contentLength - offset);
                    if (read <= 0)
                    {
                        break;
                    }
                    offset += read;
                }

                HttpRequest request = new HttpRequest();
                request.Method = first.Length > 0 ? first[0].ToUpperInvariant() : "";
                string target = first.Length > 1 ? first[1] : "/";
                int queryIndex = target.IndexOf('?');
                if (queryIndex >= 0)
                {
                    request.Path = target.Substring(0, queryIndex);
                    request.Query = target.Substring(queryIndex + 1);
                }
                else
                {
                    request.Path = target;
                    request.Query = "";
                }
                request.Body = Encoding.UTF8.GetString(bodyBytes, 0, offset);
                request.Host = host;
                request.Origin = origin;
                return request;
            }
        }

        private sealed class HttpResponse
        {
            private readonly int status;
            private readonly string body;

            private HttpResponse(int status, string body)
            {
                this.status = status;
                this.body = body;
            }

            public static HttpResponse Json(int status, string body)
            {
                return new HttpResponse(status, body);
            }

            public void Write(NetworkStream stream)
            {
                byte[] bodyBytes = Encoding.UTF8.GetBytes(body);
                string header = "HTTP/1.1 " + status + " " + Reason(status) + "\r\n" +
                    "Content-Type: application/json; charset=utf-8\r\n" +
                    "Content-Length: " + bodyBytes.Length + "\r\n" +
                    "Connection: close\r\n\r\n";

                byte[] headerBytes = Encoding.ASCII.GetBytes(header);
                stream.Write(headerBytes, 0, headerBytes.Length);
                stream.Write(bodyBytes, 0, bodyBytes.Length);
            }

            private static string Reason(int status)
            {
                if (status == 200) return "OK";
                if (status == 403) return "Forbidden";
                if (status == 404) return "Not Found";
                if (status == 409) return "Conflict";
                return "Internal Server Error";
            }
        }
    }
}

# Base-game mayor API coverage

This inventory defines "complete" as every Cities: Skylines 1 base-game system
that an autonomous mayor must observe or control. It excludes DLC area systems,
editors, cosmetic placement, and developer-only controls.

## Implemented observation surfaces

| System | Endpoints |
|---|---|
| City overview | `/state/mayor-snapshot`, `/state/summary`, `/state/problems`, `/state/demand`, `/state/chirps` |
| Zoning and buildings | `/state/zones`, `/state/growables`, `/state/facilities`, `/state/zone-anomalies`, `/state/building-anomalies`, `inspect_building_layers_3d`, `inspect_building_road_clearance_3d` |
| World model | `/state/map-analysis`, `/state/natural-resources`, `/state/map-areas` |
| Districts | `/state/districts`, `/state/policies` |
| Economy | `/state/economy`, `/state/economy-details`, `/state/budgets`, `/state/city-health`, `/state/loan-options` |
| Education | `/state/education` |
| Services and utilities | `/state/service-coverage`, `/state/service-capacity`, `/state/facility-influence`, `/state/utility-coverage`, `/state/water-service-coverage`, `/state/electricity-service-coverage`, `/state/electricity-connection-area`, `/state/service-vehicles`, `/state/building-deliveries`, `/commands/preview-service-impact` |
| Roads and traffic | `/state/networks`, `/state/network-segment-details`, `/state/network-node-details`, `/state/road-reachability`, `/state/road-anomalies`, `/state/traffic`, `/state/external-connections` |
| Public transport | `/state/transport-lines`, `/state/transport-vehicles`, `/state/transport-line-anomalies`, `/state/transport-station-anomalies`, `/state/transport-line-paths` |
| Availability | `/state/unlocks`, `/prefabs/roads`, `/prefabs/networks`, `/prefabs/buildings` |
| Recovery and validation | `/state/saves`, `/state/save-status`, `/state/restore-status`, `/state/regression-report`, `/state/game-settings` |

## Implemented command surfaces

| System | Endpoints |
|---|---|
| Roads and utilities | `/commands/preview-network`, `/commands/build-road`, `/commands/build-network`, `/commands/upgrade-road`, `/commands/reverse-road` |
| Zoning | `/commands/set-zone`, `/commands/repair-zones-to-growables`, `/commands/repair-zone-clusters` |
| Base-game districts | `/commands/create-district`, `/commands/update-district`, `/commands/set-district-name`, `/commands/set-district-specialization`, `/commands/remove-district` |
| Buildings and education | `/commands/preview-placement`, `/commands/place-building`, `/commands/place-education-facility`, `/commands/move-building`, `/commands/set-building-active`, `/commands/bulldoze` |
| Governance | `/commands/set-tax-rate`, `/commands/set-service-budget`, `/commands/set-policy`, `/commands/take-loan`, `/commands/pay-loan` |
| Public transport | `/commands/create-transport-line`, `/commands/set-transport-line`, `/commands/add-transport-stop`, `/commands/move-transport-stop`, `/commands/remove-transport-stop`, `/commands/release-transport-line`, `/commands/assign-transport-line-depot`, `/commands/refresh-transport-network` |
| Simulation and saves | `/commands/set-simulation-speed`, `/commands/create-checkpoint`, `/commands/restore-checkpoint`, `/commands/save`, `/commands/set-autosave` |
| Regression verification | `/commands/capture-regression-baseline`, `/state/regression-report` |
| Batching | `/commands/batch` |

## Typed mayor-tool coverage

The raw base-game API surface and the safer typed mayor-tool surface are
different contracts. Raw endpoint coverage is broad, but the typed tool layer
is not yet complete.

Water construction now has a dedicated non-bypassable workflow:

1. `inspect_water_pair` validates shoreline placement, moving-water reach,
   upstream/downstream order, separation, pollution, and native production.
2. `preview_water_system_plan` validates the whole proposed pipe graph. It
   requires separate intake and outlet branches, a shared trunk, a three-way
   junction, a connection toward the serviced city area, and native preview of
   every segment.
3. `build_water_pipe` builds one verified segment per checkpoint. The generic
   `build_network` tool rejects Water prefabs.
4. `inspect_water_network_topology` requires a real off-path serviced-area
   branch from the intermediate trunk; merely inserting one node between the
   two facilities is not sufficient.
5. `inspect_water_service_coverage` verifies exact building demand after the
   simulation settles.
6. `propose_water_service_connection` extends an already verified component
   from a real live node to exact uncovered buildings and native-previews the
   complete branch without weakening the original system proof.
7. `build_water_service_pipe` builds one branch segment per checkpoint, then
   requires exact coverage and replanning so obsolete later segments are not
   constructed.

Power plants now use the same specialized-tool pattern:

1. `inspect_wind_resource_map` directly samples the native
   `WeatherManager.GetWindSpeed` field without mouse movement or image
   recognition.
2. `preview_power_plant_candidate` reads the native `PowerPlantAI` profile
   instead of identifying behavior from the prefab name.
3. Wind candidates must meet a configurable fraction of their native maximum
   location-dependent output.
4. Fuel-based candidates must avoid residential noise and ground-pollution
   exposure. Native `RequireRoadAccess()` is mandatory: the calculated sidewalk
   point must reach a ground vehicle lane within 20 metres, and the built plant
   must acquire a nonzero native access segment. Directed inbound and outbound
   freight paths, route length, and live traffic density are also verified.
5. `place_power_plant` is the only typed placement mutation for power plants;
   generic `place_service` rejects them.
6. `inspect_power_plant_operation` verifies production, live problems, fuel
   buffer, and road logistics after simulation settlement.
7. `inspect_electricity_connection_area` separates the native blue
   conductivity area from actual powered coverage, and
   `propose_power_line_connection` previews a line from a powered sample into
   the target building's native electricity grid radius.
   `propose_minimal_power_grid` jointly plans generator, facility, and
   growable-building regions as a native-previewed minimum spanning tree,
   reusing overlapping blue areas instead of pulling every line back to the
   generator.

The first shared planning layer is also available:

1. `inspect_planning_area` converts a bounded part of an offline city snapshot
   into a compact 3D context containing terrain, resources, utility coverage,
   road layers, nearby buildings, demand, and evidence-backed terrain samples.
   When the Bridge is live and no path is supplied, it captures the current
   city first. Every supplied snapshot must match the live city `instanceId`;
   reusing an older city with the same map is rejected.
2. `propose_development_plan` ranks alternative planning anchors and coordinates
   road access, zoning intent, and utility gaps without issuing mutations. It
   reports candidate-stream diagnostics, rejects industrial samples within the
   conservative residential-separation policy, and associates live exact blocks
   by both road identity and a geometry-derived block radius. For a current live
   capture it promotes and block-deduplicates reusable exact frontage before the
   plan limit, then derives local streets from reusable source-road endpoint nodes,
   applies incident-angle, terrain-cell and industrial endpoint-separation gates,
   then sequentially native-previews the bounded survivors. Static rejections do
   not consume the native-preview budget.
3. Every returned plan remains `planningOnly` and not `executionReady`.
   Captured terrain samples must never bypass fresh live placement, zoning,
   cost, topology, pathfinding, checkpoint, and regression gates.

## Next domain safety-gate refactors

The next tools should reuse native placement, directed road reachability,
building deliveries, exact demand, storage, and coverage telemetry instead of
adding more memory-only rules.

| Priority | Domain | Required preflight and post-build gates |
|---|---|---|
| 1 | Garbage and deathcare storage | Pollution/noise exposure, aggregate processing headroom, native storage amount and emptying mode, reachable receiving facility, truck/hearse paths, route distance and traffic |
| 2 | Fire, police, and healthcare dispatch | Exact uncovered or incident demand, native coverage field, vehicle availability, directed road reachability to affected districts, route congestion, and live responding paths |
| 3 | Schools | Eligible age-group demand, level-specific capacity, actual geographic education field, walking/road access, noise exposure, and enrollment after settlement |
| 4 | Specialized industry and cargo | Matching unlocked resource hotspot, import dependency, outside freight reachability, industrial traffic, pollution separation, and live inbound/outbound cargo paths |
| 5 | Transport depots and stations | Compatible network and stop geometry, depot-to-line path, outside connection where required, passenger/cargo demand, noise exposure, and spawned vehicles |

Landfills and cemeteries require their own tools rather than a generic service
placement because their stored contents and emptying state change which
mutations are safe. Emergency services need a shared dispatch-logistics tool,
while schools should not inherit vehicle-delivery constraints they do not use.

Typed wrappers still need parity work for raw commands covering building moves,
loans, policies, district lifecycle edits, transport stop/depot lifecycle,
checkpoint restoration, autosave configuration, and administrative zone
repair. These are the next tool-layer priority; agents should not construct raw
HTTP calls as a substitute.

`/commands/unlock-map-areas`, `/commands/batch`, and
`/commands/disable-blocked-assets` are intentionally not exposed to the
autonomous mayor as generic typed mutations. The first bypasses normal map
progression, while the latter two can combine or hide changes that require
individual checkpoint and regression verification. Normal play still needs a
separate `purchase_map_area` tool that buys exactly one currently eligible tile
at its live price without changing the game's maximum-area limit.

## Validation status

The base-game mayor surface above is implemented. The expanded endpoints were
compiled and exercised in Cities: Skylines 1 with developer progression
unlocked. Live round trips covered education placement, loan take/pay, district
boundary edits, exact network preview, road upgrade and reversal, bus-line
settings and stop edits, service-vehicle path reads, regression baselines, and
checkpoint restoration.

`/commands/preview-service-impact` combines placement validation, prefab
capacity/vehicle/radius data, current local service coverage, and a clearly
labelled radial estimate of residential buildings and residents reached. The
estimate does not replace post-placement verification because the live result
also depends on road access, pathfinding, budget, and simulation time.

Mutations must be issued sequentially: create a named checkpoint, execute one
justified action, wait for the game to settle, read back the affected entities,
and only then continue. Transit stops require at least 32 metres of separation
to prevent CS1 from snapping two requested stops onto one platform. DLC area
systems remain intentionally outside this base-game contract.

Ground-road construction uses a second command on a later Unity update before
the typed tool reports success. This catches native junction/transition curve
changes and locally removes the new segment with a full construction-cost
refund when the settled geometry is unsafe. Terrain inspection compares live
terrain against the native curve plus that segment's dynamically measured
node-to-curve vertical inset. Prefab-local mesh axes are not treated as world
height, and CS1's lower Bezier centerline alone is not misclassified as a
buried road.

# API Reference

Base URL:

```text
http://127.0.0.1:32123
```

While a city is loaded, every API request that touches game state also appears
in the CS1 UI as a short overlay notification. The overlay keeps the latest few
messages for several seconds, for example `API OK: Read city problems` or
`API OK: Build network Basic Road`. `/health` is intentionally excluded because
it can be called before a level exists.

## Agent Workflow

The intended workflow is intentionally generic:

1. Read state from the API.
2. Decide which small change is needed.
3. Call one command API.
4. Let the simulation settle.
5. Re-read state.
6. Save and verify the save file.

## GET /health

Returns bridge status without requiring a loaded city.

## GET /state/mayor-snapshot

Returns one bounded wake-up payload for the autonomous mayor:

```powershell
Invoke-RestMethod "http://127.0.0.1:32123/state/mayor-snapshot?detailLimit=10"
```

The snapshot includes progression capabilities, demand, finances, citywide
service capacity, education, serious problems, traffic, bounded road/building/
zone/transport anomalies, unlocked natural-resource evidence, and
non-prescriptive `attention` signals. `detailLimit` is clamped to `0..50`.

The endpoint does not choose or execute an action. The agent compares it with
persistent mayor memory, generates candidates, follows up with focused tools,
and may choose to wait.

## GET /state/summary

Returns a small city snapshot: game time, build index, network counts, UI
population, internal citizen-instance count, and demand values. `population`
comes from the whole-city district population and matches the UI.
`citizenInstances.count` is the lower-level `CitizenManager.m_citizenCount`;
it includes resident, tourist, commuter, and transient citizen records and is
explicitly marked `notUiPopulation: true`.

## GET /state/demand

Returns the three demand bars shown in the CS1 UI: residential, commercial,
and workplace demand. Values are `0..100`.

```powershell
Invoke-RestMethod http://127.0.0.1:32123/state/demand
```

## GET /state/chirps

Returns recent Chirper/citizen messages from CS1's message manager, including
sender name, sender id, text, message type, and message metadata when available.
This is useful for reading citizen feedback such as housing demand, tax,
traffic, service, and city satisfaction comments without OCR.

```powershell
Invoke-RestMethod "http://127.0.0.1:32123/state/chirps?limit=50"
```

## GET /state/zones

Returns zoning cell counts and approximate area by zone type. CS1 zoning cells
are reported as 8m x 8m cells, so `areaSquareMeters` is approximate but useful
for comparing residential, commercial, industrial, office, and unzoned area.

```powershell
Invoke-RestMethod http://127.0.0.1:32123/state/zones
```

## GET /state/growables

Returns existing growable residential, commercial, industrial, and office
buildings with service, sub-service, footprint size, position, active/abandoned
state, and problem flags. Use this before zoning to avoid painting over already
developed blocks.

```powershell
Invoke-RestMethod "http://127.0.0.1:32123/state/growables?limit=500"
```

## GET /prefabs/roads

Returns loaded `NetInfo` prefabs that look like roads.

## GET /prefabs/networks

Returns loaded network prefabs. Optional service filter:

```http
GET /prefabs/networks?service=Water
```

## GET /prefabs/buildings

Returns loaded building prefabs. Optional service filter:

```http
GET /prefabs/buildings?service=Electricity
```

Known broken/blocked building assets are omitted from this list. The current
blocked family is `Block Services - ...`.

## GET /state/problems

Returns in-game notification/problem icons from CS1 data, without using screenshots or computer vision.

```http
GET /state/problems?limit=200
```

The response includes both the legacy combined `problems` string and
structured `problemNames`, `problem1Raw`, `problem2Raw`, and
`countsByProblem` fields so agents can match individual alerts such as
`TaxesTooHigh` even when CS1 marks the same building as major or fatal.
Building rows also surface alert-like flags such as `Abandoned`, `BurnedDown`,
`Collapsed`, `Flooded`, and `RoadAccessFailed`.

Scanned entity types:

- `building`
- `netNode`
- `netSegment`

## GET /state/economy

Returns the currently configured tax rates for zoned residential, commercial,
industrial, and office sub-services across levels. `aggregateTaxRates` mirrors
the six tax sliders shown in the CS1 budget UI.

```powershell
Invoke-RestMethod http://127.0.0.1:32123/state/economy
```

`GET /state/economy-details` (`inspect_economy`) lists service budget rows and,
separately, zoned tax income rows in `taxes`. `totals.income` includes
`totals.taxIncome`; `totals.periodNetMinusRowNet` compares the rows with the
game's last period net so a missing income source becomes visible.

## GET /state/facilities

Returns current buildings grouped by CS1 service, with optional service
filtering. This is the API-friendly replacement for reading service icons from
the screen. Facility items include prefab footprint and rotation so an agent can
avoid placing large buildings through roads.

By default this excludes internal pipe helper buildings such as `Water Pipe
Junction` and `Heating Pipe Junction`; pass `includeMapObjects=true` when an
agent specifically needs raw map objects.

```powershell
Invoke-RestMethod http://127.0.0.1:32123/state/facilities?limit=500
Invoke-RestMethod http://127.0.0.1:32123/state/facilities?service=HealthCare
Invoke-RestMethod http://127.0.0.1:32123/state/facilities?service=PoliceDepartment
Invoke-RestMethod http://127.0.0.1:32123/state/facilities?includeMapObjects=true
```

The response includes:

- `countsByService`
- `countsBySubService`
- `facilities[]` with `id`, `prefab`, `displayName`, `service`, `subService`, `level`, `width`, `length`, `angleDegrees`, `problems`, and `position`

Response shape:

```json
{
  "ok": true,
  "total": 17,
  "returned": 17,
  "countsByService": {
    "Water": 14,
    "HealthCare": 3
  },
  "facilities": [
    {
      "id": 123,
      "prefab": "Inland Water Treatment Plant 01",
      "service": "Water",
      "width": 5,
      "length": 7,
      "angleDegrees": 180,
      "problems": "",
      "position": { "x": 10, "y": 0, "z": 20 }
    }
  ]
}
```

## GET /state/networks

Returns current network segments from CS1 data, without screenshots. Optional
service filtering is useful for checking roads, water pipes, heating pipes, and
power lines separately.

```powershell
Invoke-RestMethod http://127.0.0.1:32123/state/networks?service=Road
Invoke-RestMethod http://127.0.0.1:32123/state/networks?service=Water
Invoke-RestMethod http://127.0.0.1:32123/state/networks?limit=1000
```

Each segment includes `id`, `prefab`, `service`, `subService`, `problems`,
`name`, `startNodeId`, `endNodeId`, and exact `start`, `middle`, and `end`
world coordinates with `x`, `y`, and `z`. Road segments also expose
segment and endpoint flags, endpoint degree, and `startOutside`/`endOutside`.
These fields let a planner distinguish original map highway terminals from
ordinary player-created dead ends without retaining coordinates from an older
city.

The typed `propose_outside_road_connection` tool reads those live terminal
fields plus directed outside reachability, owned terrain samples, and native
width/directionality metadata from the currently loaded requested road prefab.
It therefore works on a fresh city before that prefab has any built segment.
The planner searches for a complete conditional corridor but returns only its
first construction step. After that step settles, the caller must inspect it
and replan from the changed live topology. Every returned step has passed the
native network preview and terrain gate. A candidate is rejected when it would
trigger CS1's native
`MoveMiddleNode` behavior, because that behavior rewrites an existing segment
and cannot be safely undone by deleting only the newly returned segment.
The planner reserves 0.7 metres of the normal road-to-terrain deviation budget
for native junction/terrain settlement observed between dry-run geometry and
the settled segment. Separately, preview/build preflight requires 0.12 metres
minimum clearance: the settled 0.02-metre threshold plus a 0.10-metre clearance
reserve. A new endpoint may carry `startTerrainFitLift` or
`endTerrainFitLift` only within 0–0.3 metres; an existing or read-only planning
node cannot be moved, and construction plus settled QA repeat the gates. It
fails closed when no complete bounded route passes;
it does not construct an unverified intermediate stub. Search directions are
derived from current topology, and no map coordinate or previous-city route is
retained. Its result separates query success (`ok`) from plan readiness
(`safe`, `outcome`) and always reports `mutationAuthorized: false`: checkpoint,
baseline, execution, settlement, focused QA, and regression remain separate
caller responsibilities.

The bounded frontier scheduler probes every terminal pair and orientation before
deepening promising trials. It interleaves goal-guided, depth-fair, and standard
A* anchor pops over one shared pending set and native-preview cache. This changes
exploration order only; turn, area, overlap, terrain, native-topology, cash
reserve, and complete-corridor gates remain unchanged. Diagnostics distinguish
candidate rejection layers from global budget termination and report local pass
limits, queued/popped depths, goal-connector and goal-edge rejection layers,
reverse-skeleton counts, duplicate oriented states, scheduler shares, cache
hits, and remaining frontier size. A returned route is
`boundedNotGuaranteed` rather than a shortest- or cheapest-route claim.

Road segments also expose
`startDirection` and `endDirection` vectors so curved geometry can be
reconstructed instead of treating the road as a straight chord. They also expose
`isUnderground`, `isTunnel`, `directionality`, `isBidirectional`, and
`isOneWay`. Directionality is derived from the prefab's final vehicle-lane
directions, while underground state comes from the game's native network AI.
`/state/network-segment-details` additionally returns `curve` from the live
native `NetSegment.GenerateBezier` result and a `terrainProfile` sampled from
that curve against the current terrain. Its explicit `isGroundRoad` must agree
with `terrainProfile.required`, preventing a tunnel/elevated segment from being
mistaken for missing ground-road evidence. This is post-build evidence and does
not depend on the requested coordinates, map, road name, or a stored city plan.
Short transitions connected to original map nodes use the same live terrain
threshold during preview, settled mutation verification, and later segment
inspection; all new road surfaces must still remain above the terrain.
Preview clearance uses the terrain-seeded control curve directly and does not
subtract a fixed native curve offset. Native corner and node height offsets are
not available until creation; the settled `GenerateBezier` terrain profile
remains authoritative and rolls back an unsafe ground road.

## GET /state/water-service-coverage

Returns exact building-level water and sewage status using each prefab's native
water demand plus the game's `WaterManager.CheckWater` result. By default it
returns only buildings with demand that are missing water, missing sewage, or
receiving polluted water; zero-demand objects such as electricity poles are
excluded.

```powershell
Invoke-RestMethod http://127.0.0.1:32123/state/water-service-coverage
Invoke-RestMethod "http://127.0.0.1:32123/state/water-service-coverage?includeCovered=true&service=Education"
```

The mayor tool `inspect_water_pair` additionally samples the connected moving
water reach across every unlocked map tile. It rejects dead water, an outlet
that is not downstream of the intake, and placements too far from the
currently unlocked upstream and downstream extremes. Moving water elsewhere in
the connected reach never overrides a dead or weak sample at either facility's
actual water-side location.

Pipe construction uses a separate typed workflow. Call
`preview_water_system_plan` with the complete proposed graph. A valid graph has
one intake branch, one outlet branch, one or more shared-trunk segments, a
three-way shared junction, and a trunk endpoint at the serviced city area.
`build_water_pipe` re-runs the full plan gate and builds one segment per
checkpoint. The generic `build_network` tool rejects Water prefabs so the
hydrology and topology checks cannot be skipped.

After every planned segment exists, call `inspect_water_network_topology`. It
rejects direct facility-to-facility pipes and also rejects a two-facility path
that has no off-path branch from an intermediate node toward the serviced city.
Then run `inspect_water_service_coverage` after simulation settlement.

For later growth, do not reuse the complete-system builder merely to extend an
already valid network. `propose_water_service_connection` first re-proves that
the live intake and outlet still share a component with an intermediate trunk
and serviced-area branch. It then chooses a real node in that component, visits
the exact current positions of the requested buildings that still lack water or
sewage, splits long legs, and native-previews every proposed pipe. Network and
coverage inventories must be complete; truncation fails closed.

`build_water_service_pipe` rechecks the topology, source-node membership, target
state, plan hash, segment continuity, previously created segments, prefab type,
and all native previews before building exactly one segment after a checkpoint.
Settle and query exact building coverage after each accepted segment, then rerun
the proposer. Stop as soon as it returns `actionRequired: false`; later segments
from the earlier static plan may have become unnecessary.

## GET /state/facility-influence

Returns native source parameters for existing facilities. Service facilities
include their coverage resource, accumulation, radius, source bounds, and live
value at the facility. Noisy buildings such as coal or oil power plants include
their `NoisePollution` accumulation, radius, bounds, and live source value.

```powershell
Invoke-RestMethod "http://127.0.0.1:32123/state/facility-influence?service=Electricity"
Invoke-RestMethod "http://127.0.0.1:32123/state/facility-influence?service=PoliceDepartment"
```

These radii are source parameters, not the final simulated coverage. Use
`inspect_service_coverage` (or `/state/service-coverage`) with
`NoisePollution`, `PoliceDepartment`, `EducationElementary`,
`EducationHighSchool`, or `EducationUniversity` to read the propagated live
field over explicit map bounds.

## Offline layered 3D road analysis

Capture a city before closing the game:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\capture-offline-spatial-snapshot.ps1
```

The snapshot contains roads and other networks, facilities, growables, unlocked
tile terrain and water samples, utility coverage, road prefab lane templates,
and focused anomalies. It can be analyzed without a running game through:

- `inspect_road_layers_3d`: samples every road in XYZ, bilinearly samples the
  captured terrain, reports terrain-relative height and classifies native
  `Ground`, `Elevated`, `Bridge`, `Slope`, and `Tunnel` prefabs.
- `inspect_grade_separated_crossings`: finds exact XZ curve crossings and
  interpolates both road heights there. Crossings separated by the configured
  vertical threshold are reported as grade-separated; close crossings without a
  shared node are reported as `UnconnectedSameGradeCrossing`.

Current snapshots include the cubic control points returned by CS1's native
`NetSegment.GenerateBezier` implementation and use them directly. Legacy
snapshots without `curve.a` through `curve.d` fall back to a quadratic through
the reported start, middle, and end points.

### Lightweight 3D buildings

Current facility and growable records also contain `spatial3d`: the native
placement XYZ, rotated eight-metre-cell footprint, four footprint corners,
model height, collision height, and vertical bounds. It intentionally stores no
mesh, texture, screenshot, or image.

- `inspect_building_layers_3d` reads these lightweight oriented building boxes
  offline.
- `inspect_building_road_clearance_3d` intersects the oriented footprint with
  native road curves and reports `RoadBelowBuilding`, `RoadAboveBuilding`, or
  `VerticalConflict`.

The vertical collision range starts at `building.m_position.y` and extends by
the greater of native model height and collision height. Render mesh bounds are
not used as a foundation because decorative basements and LOD geometry may
extend below the placement base. Original map structures, road pillars, and
pipe-junction helper buildings are excluded from clearance checks by default.

### Compact planning neighborhoods

`inspect_planning_area` reads a bounded neighborhood from the latest offline
snapshot instead of returning the entire city to the model. Given an exact XZ
center and radius, it summarizes:

- sampled terrain height, slope, buildability, water, pollution, and natural
  resources;
- local electricity, water, and sewage coverage ratios plus nearby pipe and
  power-line segments;
- nearby native 3D roads by structure and direction;
- local facilities and growable land uses;
- current citywide demand and progression capabilities; and
- ranked exact terrain samples that may serve as planning anchors.

`propose_development_plan` turns that compact context into up to ten ranked
alternatives coordinating road access, a demand-weighted land-use mix, utility
gaps, and the nearest captured pipe or power-line segments. Both tools are
read-only and their spatial analysis works while the game is closed. Native
road previews are attached only when the snapshot was freshly captured from a
live Bridge.

For a current live capture, both tools also attach exact road-generated zoning
blocks. Existing frontage is reusable only when a block matching that road passes
the development-frontage and junction gates and the planning sample falls within
that block's geometry-derived association radius. Industrial planning reports
clearance to residential footprints, and residential planning reports clearance
to industrial footprints. In both directions, samples below the conservative
64-metre policy separation are rejected, and a whole exact block is not reused
when its block-wide clearance lower bound fails the same gate. This reciprocal
rule prevents later residential zoning from undoing an earlier industrial
separation decision. It is a planning heuristic, not a claim about one growable
prefab's final pollution radius. An
explicit offline snapshot reports live frontage evidence as unavailable and does
not silently authorize reuse. Reusable exact-frontage sites are stably promoted
ahead of terrain-only alternatives and deduplicated by block before the plan limit
is applied. A high-scoring generic terrain sample therefore cannot hide a safer
existing block and trigger an unnecessary extra road.

When a live plan needs new frontage, the planner derives bounded local-street
candidates from actual source-road endpoint nodes, not arbitrary points on the
rendered curve. Each candidate heads from a reusable node toward its planning
anchor, keeps at least 30 degrees from every incident road, checks the captured
tile/buildability/water evidence, and applies a conservative 96-metre industrial
endpoint separation from residential footprints. Statically eligible candidates
use the current unlocked prefab catalog and sequential native network previews.
These angle and separation values are planning policies; native collision and
terrain results remain authoritative.

Every local-street alternative has a unique `candidateId` while retaining its
parent `planId`. When cash evidence and native construction cost are available,
`cashPolicy` converts the game's internal money units to display money and rejects
a candidate that would cross `reserveDisplay` (10,000 by default). Native-valid
geometry can therefore remain rejected by the separate acceptance-policy layer.

Planning responses include candidate diagnostics: generated terrain samples,
statically eligible samples, returned ranked samples, rejection counts and the
number of native preview calls. Static rejections do not consume the separate
native-preview budget. Explicit offline snapshots, or an unavailable Bridge,
report native preview as unavailable and keep that counter at zero.

A planning anchor is not an approved construction coordinate. It identifies an
exact captured terrain sample worth investigating. Before mutation, refresh the
live prefab catalog, unlocks, costs, exact road-generated zoning blocks,
pathfinding, utility topology, and every relevant native placement or network
preview. Plans therefore always report `planningOnly: true` and
`executionReady: false`.

## GET /state/road-anomalies

Detects road geometry that can look connected on screen but is not actually a
proper CS1 road graph connection.

```powershell
Invoke-RestMethod "http://127.0.0.1:32123/state/road-anomalies?nearMissDistance=18&shortSegmentLength=32&includeDeadEnds=true"
```

Detected anomaly types:

- `deadEndNearRoad`: a one-segment road endpoint is very close to another road segment, which often means the endpoint visually touches a road but did not create an intersection.
- `deadEndRoad`: a normal road dead end. This is legal in CS1, but useful for agent-side design QA because unwanted frontage/service-road stubs often look like this.
- `shortRoadStub`: a short road segment with a dead end, often left behind by failed frontage-road or service-road placement.
- `duplicateRoadSegments`: two road segments share the same pair of endpoint nodes, which usually means one should be removed.
- `overlappingRoadSegments`: two road segments run nearly on top of each other at the same height for a meaningful distance, which usually means a duplicate or accidental overlay.
- `roadCrossingWithoutNode`: two road segments cross at nearly the same height without sharing a node, which usually means they visually overlap but are not a real intersection.
- `roadTerrainCliff`: a ground road has a large height mismatch against nearby sampled terrain, which can indicate buried roads or terrain spikes/cliffs caused by bad road placement.
- `roadBelowLocalGrade`: an agent-built ground road sits far below the surrounding local road grade, which often means a sunken or buried road.

Each anomaly includes the affected node or segment IDs plus world coordinates,
so an agent can call `/commands/bulldoze` or add a connector road without using
image recognition.

Helper scripts:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\inspect-road-anomalies.ps1

# Remove suspicious short/dead-end service roads only in a bounded area.
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\repair-road-anomalies.ps1 `
  -MinX 450 -MaxX 620 -MinZ 90 -MaxZ 250
```

## GET /state/external-connections

Checks whether the city's local road component is connected to CS1 outside road
nodes. This is useful when a city visually has highways nearby but no outside
cars enter because the local road graph is still separate from the highway
network.

```powershell
Invoke-RestMethod "http://127.0.0.1:32123/state/external-connections?limit=50"
```

The response includes `cityConnectedToOutside`,
`disconnectedLocalRoadComponents`, outside node counts, and sampled road
components.

## GET /state/road-reachability

Checks one road segment against the directed vehicle-lane graph. It separately
reports whether freight can arrive from an outside road node and return to an
outside node, including approximate route distance and live average/maximum
traffic density.

```powershell
Invoke-RestMethod "http://127.0.0.1:32123/state/road-reachability?id=77"
```

Segment inversion is applied to lane direction. This fixes a previous
orientation ambiguity where reversing a one-way segment changed the game but
not the reported start-to-end direction. The graph does not claim visibility
into every modded turn restriction or district heavy-traffic rule, so
fuel-based facilities still require post-build vehicle-path verification.

## GET /state/building-deliveries

Lists live inbound and outbound vehicles whose target or source is one building.
It includes transfer reason, source/target building, path state, wait/block
counters, and XYZ position. `inspect_power_plant_operation` uses this when a
fuel buffer is low to distinguish an approaching fuel delivery from a facility
that has no delivery or whose vehicle has no path.

```powershell
Invoke-RestMethod "http://127.0.0.1:32123/state/building-deliveries?buildingId=152&limit=256"
```

## GET /state/electricity-service-coverage

Uses each building prefab's native electricity consumption and
`ElectricityManager.CheckElectricity` to report exact demanding buildings that
remain unpowered. Zero-demand helpers such as power-line poles are excluded by
default. This is the final grid-delivery check after adding generation or power
lines.

```powershell
Invoke-RestMethod "http://127.0.0.1:32123/state/electricity-service-coverage?includeCovered=false"
```

## GET /state/toolbar-unlocks

Compares three live layers after a create-pass road or milestone: the
simulation's `UnlockManager`, `ToolsModifierControl`, and each actual
`GameMainToolbar` button's enabled, visible, and interactive state. A newly
reported capability must not be used while its toolbar entry remains
inconsistent or unusable. API-built roads explicitly send the game's
`MilestonesUpdated()` notification so the existing toolbar refreshes without
unpausing the simulation.

```powershell
Invoke-RestMethod http://127.0.0.1:32123/state/toolbar-unlocks
```

## GET /state/electricity-connection-area

Reads a target building's native `BuildingAI.ElectricityGridRadius()` and
samples `ElectricityManager.CheckConductivity` separately from
`CheckElectricity`. Conductivity represents the building-to-building blue
connection area; powered reports whether that point currently belongs to a
working supplied grid.

The typed `inspect_electricity_connection_area` tool exposes the sampled area.
`propose_power_line_connection` searches from a powered sample into the target
building's native grid radius and runs the normal native network preview,
including pylon road clearance, before returning a segment. A target endpoint
must be both conductive and outside the target building footprint. Before any
native preview, the complete line corridor must avoid the oriented footprints
of every current live facility and growable plus a 4m pylon clearance. If the
5,000-item Bridge response limit truncates either building inventory, planning
and building fail closed instead of treating the partial inventory as safe. It returns
`actionRequired: false` when the target already has electricity.

`propose_minimal_power_grid` plans several connections together. Supply one
generator building ID plus all required facility and growable-building IDs.
It treats overlapping native blue regions as zero-length conductive links,
then applies Kruskal minimum-spanning-tree selection to the remaining region
pairs. Every returned line passes the normal native network preview, and lines
are ordered from the generator outward so a verified segment can energize the
next connection area. The response compares total proposed line length with a
direct generator-to-every-target region-gap estimate. This is a planning tool:
each segment still needs its own checkpoint, settlement, exact electricity
coverage check, and regression verification. Rerun the planner after every
accepted segment; live water boundaries, collisions, and newly conductive
growables can invalidate a later edge from the earlier static plan. Targets
already powered by the live grid are seeded as existing zero-cost connections,
so the tool does not propose duplicate lines back to the generator.
Candidate line endpoints must also be live samples with
`conductive: true`; being geometrically inside the nominal blue
`ElectricityGridRadius()` is not sufficient because the native conductivity
field can contain holes. The same all-building corridor gate is applied by
`propose_minimal_power_grid`, and `build_network` refreshes it immediately
before any power-line mutation so a stale or manually supplied segment fails
closed instead of bulldozing a building.

`inspect_power_grid_efficiency` ranks long existing lines for bounded
checkpoint experiments. A network-graph cut is not automatically unsafe or
safe because CS1 also bridges electricity through building conductivity areas.
The workflow therefore removes only one candidate, settles the simulation,
checks every demanding building and focused connection area, and restores the
checkpoint on any failure.

```powershell
Invoke-RestMethod "http://127.0.0.1:32123/state/electricity-connection-area?buildingId=42&padding=512&resolution=48"
```

## GET /state/building-anomalies

Detects user-placed and growable buildings whose footprint intersects a road
segment. This is for API-side QA when a building appears to be placed through a
road, without using screenshots. Pass `roadClearance` to also report buildings
whose footprint is very close to a road centerline, which helps catch road-edge
overlaps that are visually obvious but do not cross the exact centerline.
Original map objects such as rocks and bridge pillars are skipped by default;
pass `includeOriginal=true` when auditing map objects too.

```powershell
Invoke-RestMethod http://127.0.0.1:32123/state/building-anomalies?limit=200
Invoke-RestMethod "http://127.0.0.1:32123/state/building-anomalies?limit=200&roadClearance=0"
Invoke-RestMethod "http://127.0.0.1:32123/state/building-anomalies?limit=200&roadClearance=1&includeOriginal=true"
```

Detected anomaly types:

- `buildingRoadOverlap`: a road centerline intersects the building footprint.
- `buildingRoadTooClose`: the road centerline is within `roadClearance` meters of the building footprint.

## GET /state/zone-anomalies

Detects mottled zoning from CS1 zone blocks without using screenshots. This is
useful when circular or overlapping zone paint leaves residential, commercial,
industrial, office, and unzoned cells mixed inside the same block.

```powershell
Invoke-RestMethod "http://127.0.0.1:32123/state/zone-anomalies?limit=200&includeUnzonedHoles=true"
```

Detected anomaly types:

- `mixedZoneBlock`: one zoning block contains multiple non-empty zone types,
  such as residential cells mixed with commercial or industrial cells.
- `patchyUnzonedHoles`: one zoning block is mostly one zone type but contains
  many unzoned cells, which often means an agent left visible holes after
  repainting.

## POST /commands/build-network

Generic network creation. Use this for roads, water pipes, heating pipes, and
power lines. `roadPrefab` is kept as the request field name for compatibility
with the early bridge prototype; pass any loaded `NetInfo` prefab name.

Request:

```json
{
  "dryRun": true,
  "roadPrefab": "Basic Road",
  "start": { "x": 0, "z": 0 },
  "end": { "x": 80, "z": 0 },
  "name": "Agent Test Road"
}
```

Response:

```json
{
  "ok": true,
  "dryRun": true,
  "message": "Build-road validation passed."
}
```

When `dryRun` is `false`, the mod creates two nodes and one segment with `NetManager`.

Network construction has safety checks in the Bridge itself, not only in the
agent prompt or mayor memory:

- A newly created power-line pylon must be at least 24 metres from every road
  centerline, road node, and junction. Existing compatible power nodes may be
  reused so an agent can repair or extend an older grid without creating
  another misplaced pylon.
- Ground-road candidates are sampled along the complete centerline. The
  command rejects an alignment when its predicted control-curve elevation differs from
  the sampled terrain by more than 1 metre, when sampled local terrain grade
  exceeds 25%, or when a ground-road prefab is given a non-zero height offset.
  New Ground endpoints are terrain-seeded with a 0.1-metre target clearance;
  preview does not subtract a guessed native curve offset. The response marks
  this contract as
  `predictionGeometryEvidence.clearanceModel: "terrainSeededControlCurve"`.
  Every sampled
  predicted point must remain at least the reported
  `minimumRoadAboveTerrainAllowed` above live terrain, even when its absolute
  deviation is under 1 metre. Agents should
  split rejected routes into shorter terrain-following segments.
  A segment of 64 metres or less that touches a map-original node reports
  `originalNodeTransition: true`; it requires only the settled minimum, because
  the original node's own height can sit just that far above flat terrain.
- The same checks run again for a non-dry-run build, so calling the mutation
  endpoint without preview does not bypass them.
- After native construction, the Bridge re-reads the returned segment ID,
  drains a game-thread state-summary barrier so junction, terrain, and Bezier
  updates have settled, refreshes its segment and endpoint-node render state,
  samples its actual
  native Bezier against live terrain, verifies prefab,
  endpoints, geometry, and lanes, and locally removes/refunds the new segment
  if any hard check fails.

Dry-run responses include `powerLineRoadClearance` and `terrainProfile`
objects with the measured distances, sample count, maximum terrain deviation,
maximum local terrain grade, thresholds, and final safety result.
Successful builds include `actualTerrainProfile` with
`verificationStage: "immediate"`. For a ground road, the typed build adapter
does not treat that response as final: it waits for native updates, requires a
matching segment ID and a complete `verificationStage: "settled"` terrain
profile, and exposes both the raw `settledVerification` response and its
`settledTerrainProfile`. Missing, mistyped, contradictory, or unsafe settled
evidence fails closed. These post-build profiles label their current clearance
policy as `legacyNodeInsetProxyNotCalibratedSurface`; the native curves and raw
terrain samples remain separate evidence, not a claim that the proxy is the
physical rendered road surface. The typed `inspect_road_result` tool then
independently re-reads the segment and fails closed unless the actual terrain
profile has the complete `verificationStage: "inspection"` schema and satisfies
its thresholds. It also checks focused anomalies and endpoint topology, and
requires directed inbound and outbound reachability to an outside road. It
always uses the current segment ID
returned by the mutation.
Endpoint identity is compared in the horizontal XZ plane because CS1's road
tool settles node Y values onto terrain; vertical safety is checked separately
against the live native Bezier and terrain profile.
`/state/road-anomalies` also emits `roadBelowTerrain` for an agent-built Ground
road whose signed native-curve clearance later becomes negative, allowing any
subsequent mutation workflow to detect the regression.

## Mayor memory city scope

`load_mayor_memory` reads the live city `instanceId` before returning any
persistent goals. If the loaded city differs, it atomically replaces the old
memory with a fresh state for the live city, removing old coordinates and
building, node, or segment IDs. `save_mayor_memory` rejects a state carrying a
different city instance. Map names and city names are descriptive only and
are never used as identity or placement keys.

## POST /commands/build-road

Compatibility alias for `/commands/build-network`.

## POST /commands/set-zone

Request:

```json
{
  "dryRun": true,
  "preserveOccupied": true,
  "zone": "ResidentialLow",
  "center": { "x": 40, "z": 0 },
  "radius": 32
}
```

Supported zones:

- `Unzoned`
- `ResidentialLow`
- `ResidentialHigh`
- `CommercialLow`
- `CommercialHigh`
- `Industrial`
- `Office`

The command paints existing zone blocks near `center`. It works best after roads have created zoning blocks.
`preserveOccupied` defaults to `true` and skips zone blocks that already contain
residential, commercial, industrial, office, service, park, or monument
buildings, so broad zoning commands do not overwrite developed city blocks.
An exact `blockId` is instead skipped only when its own valid cells are natively
occupied or a protected building footprint reaches one of them, so a clear block
across the street from homes can be zoned. When a gate skips an exact block,
the command returns `ok: false` with the skipped counts and changes no cells.

## POST /commands/repair-zones-to-growables

Repairs zone blocks that contain existing growable buildings by aligning
non-empty zoning cells with the nearest residential, commercial, industrial, or
office building. Blocks with ambiguous mixed-use occupancy are skipped.

```json
{
  "dryRun": true
}
```

## POST /commands/repair-zone-clusters

Repairs larger 80m zoning clusters when a whole city block is visually mottled.
The command can fill unzoned holes and, by default, prefers the nearest existing
growable building's zone for occupied blocks so cluster repair does not blindly
convert developed buildings to the cluster's dominant zone.

```json
{
  "dryRun": true,
  "includePatchy": true,
  "fillUnzoned": true,
  "preferGrowableZone": true,
  "gridSize": 80
}
```

## POST /commands/place-building

A non-dry-run placement charges the prefab's native construction cost, like the
game's own tool, and fails without creating anything when cash is insufficient.
The response reports `constructionCost` and `constructionCharged`.

Request:

```json
{
  "dryRun": true,
  "buildingPrefab": "Wind Turbine",
  "position": { "x": 300, "z": 200 },
  "angleDegrees": 0
}
```

For growable zones, `/state/zones?actionable=true` reports
`junctionClearanceSafe: false` for blocks within 64 metres of a connected road
endpoint. It is information only: a block at a junction is zoned as a player
zones it, and the game itself invalidates the cells another road's blocks overlap.
A connected road end is a node with three or more roads, or a two-road bend
that deflects by 45 degrees or more. A two-road node with a gentler bend only
continues the same frontage, so it does not trigger the clearance.
Pass the exact `blockId` returned by `inspect_growth_capacity` to zone one
approved block without a radius-based selection. Empty blocks excluded by the
connected-road-end gate are also excluded from patchy-zone cluster warnings.

`build_water_pipe` is ordered and resumable. For `segmentIndex > 0`, it matches
all preceding planned segments against live Water-network endpoints and prefab
type, then native-previews only the current and future segments. A missing or
mismatched preceding segment blocks construction.

When a power plant requires road access, `propose_power_plant_candidates`
samples both sides of live non-highway road segments and derives angles that
face the road. These road-edge candidates are evaluated before coarse terrain
samples, preventing a fuel plant search from wasting its preview budget on
positions that can never attach to a road.
The requirement comes from native `PlayerBuildingAI.RequireRoadAccess()`, not
only `BuildingInfo.PlacementMode` and not an optional caller hint. A candidate
is connected only when its calculated sidewalk access point reaches a ground
vehicle lane within CS1's native 20-metre search distance. Proximity between a
road centerline and the building footprint is not accepted as road access.

Native `CheckBuildPosition` production rate is location-dependent for wind
turbines but may be zero for fixed-output fuel plants. Placement therefore
reports both the native `productionRate` and an `effectiveProductionRate`; only
location-dependent generators are rejected for a zero native rate.

Settled road verification also rechecks every existing road segment attached
to the new segment's endpoints. If junction regeneration makes any neighbor
float or sink, the new segment is removed, its cost is refunded, and recovery
is accepted only after those neighboring terrain profiles are safe again.

## POST /commands/preview-service-impact

Estimates how a candidate base-game healthcare, school, fire, police, or
deathcare facility would affect nearby residential buildings without changing
the city.

```json
{
  "buildingPrefab": "Elementary School",
  "position": { "x": 300, "z": 200 },
  "angleDegrees": 0,
  "requireRoadAccess": true,
  "lowCoverageThreshold": 32,
  "limit": 100
}
```

The response embeds the normal placement preview and reports the prefab's
capacity, service vehicles, service radius, accumulation, construction and
maintenance costs. `impact` includes reached residential buildings and
residents, residents currently in low coverage, residents estimated to cross
the threshold, overlap, and current/projected average coverage.

The endpoint labels its result `estimate: true` and
`method: radial-linear-estimate`. CS1's final propagation and dispatch depend
on roads, pathfinding, service budget, and elapsed simulation time, so the
agent must read live coverage and capacity again after placement.

`POST /commands/preview-placement` is also the unified building safety
preflight. It reports game-native placement errors and production, rotated
footprint collisions, road access, terrain, internal network attachment paths,
current ground pollution, ground-water intake pollution, generated
ground/noise pollution, nearby residential exposure, and utility consumption.
For native `PowerPlantAI` prefabs it also returns `electricityProfile` with the
maximum and location-adjusted estimated output, renewable/location-dependent
classification, fuel transfer resource, fuel capacity and consumption, and
native noise/pollution parameters.

The typed mayor workflow uses `preview_power_plant_candidate` and
`place_power_plant`. Wind candidates must meet the configured output ratio.
Coal and oil candidates must avoid residential noise/ground-pollution exposure
and have a native road access segment plus directed two-way freight
reachability to an outside connection with bounded route distance and traffic.
After creation, the mutation calls native `CheckRoadAccess`; a new plant with
`m_accessSegment == 0` or `RoadAccessFailed` is locally removed. After settlement,
`inspect_power_plant_operation` checks live problems, production, fuel buffer,
and logistics again. Generic `place_service` rejects power plants and water
facilities so these domain checks cannot be bypassed.

`GET /state/wind-map` samples `WeatherManager.GetWindSpeed` directly at fixed
world coordinates. The typed `inspect_wind_resource_map` tool returns the
strongest samples in a bounded area without moving the mouse, opening the wind
overlay, taking screenshots, or placing temporary turbines. Candidate placement
still uses the native `WindTurbineAI.CheckBuildPosition` result for the exact
prefab footprint and final estimated output.
The typed wind workflow drains a state-summary game-thread barrier after that
preview and passes its verified native-placement result into the mutation, so
queued guide calculations cannot race the actual placement.

Water candidate generation also rotates each prefab's native
`m_waterLocationOffset` into world space. The water port must point from the
sampled land cell toward the adjacent water cell with a cosine alignment of at
least `0.7`; valid placement and safe upstream/downstream order alone no longer
permit a pump or outlet that runs parallel to or faces away from the shoreline.
If the bounded native hydrology search returns no valid pair, the workflow is
blocked: it must not weaken moving-water, flow-agreement, upstream/downstream,
pollution, separation, or shoreline-orientation gates and must not place a pair
from guessed coordinates.

The place and move mutation endpoints repeat the hard checks. They reject
game-native placement errors, polluted ground-water intakes, and electricity
facilities with zero native production even when preview is skipped.

## POST /commands/move-building

Recreates an existing building at a new position with the same prefab and
deletes the old building. This is intentionally separate from detection and
from save operations so agents can make small, explicit repair steps.

```json
{
  "dryRun": false,
  "id": 123,
  "position": { "x": 340, "z": 200 },
  "angleDegrees": 180
}
```

## POST /commands/set-building-active

Turns an existing building on or off by id.

```json
{
  "id": 123,
  "active": false
}
```

Set `dryRun: true` to preview the operational change. Disabling a non-empty
landfill/cemetery, an assigned transport depot, or the last active facility of
its service type returns blocking risks. A risky real change requires
`confirmRisks: true` after checkpoint review.

## POST /commands/disable-blocked-assets

Disables known broken assets in CS1's package asset state so the game should not
use them. The current blocked family is `Block Services - ...`.

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:32123/commands/disable-blocked-assets
```

## POST /commands/bulldoze

Deletes a problem entity by API. Useful for agent-side repair loops after
reading `/state/problems`.

```powershell
$body = @{
  entityType = "netSegment"
  id = 21778
  keepNodes = $false
} | ConvertTo-Json

Invoke-RestMethod -Method Post -Uri http://127.0.0.1:32123/commands/bulldoze -Body $body -ContentType "application/json"
```

Supported `entityType` values:

- `building`
- `netSegment`
- `netNode`

Use `POST /commands/preview-bulldoze-impact` with the same `entityType` and
`id` first. It detects non-empty landfill/cemetery storage, assigned transport
lines, the last active service facility, network cut edges, connected network
nodes, and outside connections. `/commands/bulldoze` repeats the analysis and
requires `confirmRisks: true` for a risky deletion.
- `netNode`

## POST /commands/save

Requests an in-game save through CS1's `SavePanel.SaveGame`, the same code path
used by the normal UI save button. The game writes the `.crp` package
asynchronously, so poll `/state/save-status?name=<saveName>` until `save.exists`
is `true` and `isSaving` is `false`.

```powershell
$body = @{ name = "AgentAutoSave-20260512-1900" } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:32123/commands/save -Body $body -ContentType "application/json"

powershell -NoProfile -ExecutionPolicy Bypass -File scripts\save-city.ps1 -Name AgentAutoSave-test
```

## GET /state/saves

Lists local `.crp` saves with paths, timestamps, and file sizes. The top-level
`isSaving` field reports whether the asynchronous game save is still running;
do not treat a checkpoint as complete until it becomes `false`.

```powershell
Invoke-RestMethod http://127.0.0.1:32123/state/saves
```

## GET /state/save-status

Whether a save is being written (`isSaving`), the save folder (`directory`), the
last save the Bridge requested (`lastRequested`: name, path, requestedUtc), and,
with `name=<saveName>`, that one save's file (`save`: exists, length,
lastWriteTimeUtc). It is answered on the API thread, not the game thread, so it
stays quick while a save holds the game thread and does not grow with the number
of saves; poll it instead of `/state/saves` to wait for a save. A name that is a
path is refused.

```powershell
Invoke-RestMethod "http://127.0.0.1:32123/state/save-status?name=AgentAutoSave-test"
```

## GET /state/terrain-heights

Samples the terrain at up to 256 points, `points=x,z;x,z;...`. Each sample has
`raw` (the heightmap before buildings and networks reshape it, which the road
clearance gates sample) and `detail` (the surface the game draws after they do).
Under a built ground road `detail` follows the road's sidewalk level, so the two
differ where the road carved a bump or was raised over a dip.

```powershell
Invoke-RestMethod "http://127.0.0.1:32123/state/terrain-heights?points=300,800;332,800"
```

## GET /state/transport-line-anomalies

Detects broken public transport lines at the line level. This includes
`lineNotConnected` stop nodes, incomplete lines, too few stops, and lines that
target vehicles but have none spawned. Use this before treating a line as
operational; vehicle counts alone can be misleading.

```powershell
Invoke-RestMethod "http://127.0.0.1:32123/state/transport-line-anomalies?limit=200"
```

## GET /state/transport-station-anomalies

Detects passenger station buildings that have no nearby same-type public
transport line stop. This catches stations that are built but not actually
served by a route. `maxStopDistance` defaults to `96` meters.

```powershell
Invoke-RestMethod "http://127.0.0.1:32123/state/transport-station-anomalies?limit=200&maxStopDistance=96"
```

## GET /state/transport-vehicles

Returns live vehicles for all transport lines, or for one line with `lineId`.
Each vehicle includes its line id, path id, position, velocity-derived speed,
target/source buildings, and wait/block counters.

```powershell
Invoke-RestMethod "http://127.0.0.1:32123/state/transport-vehicles?lineId=29&limit=50"
```

## POST /commands/set-simulation-speed

Request:

```json
{
  "paused": false,
  "speed": 3
}
```

`speed` is clamped to `1..3`. Use `paused: true` to pause the
simulation while keeping the selected speed in a valid UI state. Omit
`speed` to keep the currently selected speed, for example when only pausing.

## POST /commands/set-tax-rate

Sets tax rates for zoned services. Omit `service`, `subService`, or `level` to
apply the rate broadly; pass `dryRun: true` to preview the affected tax rows.

```json
{
  "dryRun": false,
  "service": "Commercial",
  "rate": 9
}
```

Useful services are `Residential`, `Commercial`, `Industrial`, and `Office`.
`rate` must be between `0` and `29`.

## Expanded base-game mayor endpoints

Education:

- `GET /state/education` reports elementary, high-school, and university
  eligibility, capacity, facilities, students, and utilization.
- `POST /commands/place-education-facility` accepts `level` (`Elementary`,
  `HighSchool`, or `University`), `position`, optional `buildingPrefab`,
  `angleDegrees`, and `dryRun`.

Public transport:

- `POST /commands/create-transport-line` supports base-game `Bus`, `Train`, and
  `Metro` lines with a `stops` array. With `dryRun: true`, each stop must be
  within 64 metres of a compatible road/track, stops must be at least 32
  metres apart, and buses require an active matching depot. The real mutation
  repeats these checks before line creation.
- `POST /commands/set-transport-line` changes `budget`, `ticketPrice`, `name`,
  `color`, or a compatible `vehiclePrefab`.
- `POST /commands/add-transport-stop`, `/commands/move-transport-stop`, and
  `/commands/remove-transport-stop` edit line stops by `lineId` and `index`.
  Add and move requests must remain at least 32 metres from other stops.
- `GET /state/transport-lines`, `/state/transport-vehicles`, and
  `/state/transport-line-paths?lineId=...` verify the result.

Additional mayor controls:

- `GET /state/loan-options`; `POST /commands/take-loan`; `POST /commands/pay-loan`.
- `POST /commands/update-district` with `districtId`, `mode` (`add` or
  `remove`), `center`, and `radius`.
- `POST /commands/set-district-specialization` reports live Forestry, Farming,
  Oil, or Ore resource samples inside the district. Missing resources produce
  an import-dependency warning; set `requireLocalResource: true` to reject a
  specialization intended for local extraction.
- `POST /commands/upgrade-road` with `segmentId`, `roadPrefab`, `dryRun`, and
  `confirm`; `POST /commands/reverse-road`.
- `GET /state/service-vehicles` for per-facility vehicle targets and path IDs.
- `POST /commands/capture-regression-baseline`, then
  `GET /state/regression-report?name=...`.
- `POST /commands/restore-checkpoint` with `name` or `path`, `dryRun`, and
  `confirm`; poll `GET /state/restore-status` after the Bridge reconnects.

Road upgrades and checkpoint restoration require a verified named checkpoint.
Execute mutations sequentially and read back affected entities before issuing
the next mutation.

## POST /commands/batch

Request:

```json
{
  "dryRun": true,
  "stopOnError": true,
  "commands": [
    {
      "type": "build-road",
      "roadPrefab": "Basic Road",
      "start": { "x": 120, "z": 0 },
      "end": { "x": 200, "z": 0 },
      "name": "Agent Batch Road"
    },
    {
      "type": "set-zone",
      "zone": "ResidentialLow",
      "center": { "x": 160, "z": 0 },
      "radius": 48
    }
  ]
}
```

Supported command types:

- `build-road`
- `set-zone`

If an item does not include `dryRun`, it inherits the batch-level `dryRun` value. Batches are limited to 32 commands.

# Compact mayor decision loop

The mayor now has a read-only `assess_city` entry point. It reduces the existing
Bridge snapshot to health dimensions, ranked **inspection** objectives, measurable
success conditions, and missing evidence. Construction still uses the existing
typed preview/checkpoint/verification tools.

## Start with a small tool surface

```powershell
python scripts/mayor_tools.py definitions --profile mayor
python scripts/mayor_tools.py call assess_city
python scripts/mayor_tools.py clusters
python scripts/mayor_tools.py definitions --cluster development
python scripts/mayor_tools.py definitions --names inspect_growth_capacity inspect_toolbar_unlocks
```

The mayor profile contains six tools: assessment, observation, progression
validation, memory load/save, and simulation speed. `definitions` without options
still returns all tools. `clusters` returns only a small catalog of domain names,
tool names, and schema-size measurements. `--cluster` loads one validated domain
slice, while `--names` narrows the surface to exact tools. Start with `core`, swap
in the domain cluster named by the current objective, and use `--names` when the
next call is already known. The calling agent/host must register the returned
definitions or invoke the CLI. This is progressive disclosure, not a permissions
boundary or automatic tool registration.

The manifest currently provides `core`, `lifecycle`, `development`, `roads`,
`water`, `power`, `services`, `transport`, `economy`, and `buildings`. Clusters
intentionally overlap when a tool supports more than one domain. Manifest loading
fails closed for unknown tools, duplicate entries, unsupported versions, or any
typed tool left unclustered. Reported compact-JSON characters quantify relative
schema size but are not presented as tokenizer-specific token counts.

The [Traditional Chinese cluster note](tool-clusters-zh-TW.md) records the
measured sizes and intended workflow.

## What counts as progress

`inspect_zone_cell_geometry(blockId)` exposes up to 32 native cells from one
live block: validity, sharing, native occupancy, zone type, 8m XZ corners and
sampled terrain. It also returns up to 64 nearby native road curves and widths,
with an explicit truncation flag. Cell masks use an eight-bit row stride; the
four zoning columns must not be read as a packed four-bit stride.
For each road, `nativeCollisionXZCells` uses native `NetSegment.OverlapQuad`
against full cell squares, the road's collision type and an unbounded height
interval. It includes the owning road and intentionally does not resolve 3D
clearance. Collision curves can differ from rendered curves at junctions.
`/state/zones.totalCells` retains its allocated-slot meaning, including invalid
and shared cells; use the additional valid/unshared/vacant counts for diagnosis.
Neither these counts nor cell footprints prove safe growable foundations or
vertical road clearance. The existing frontage and mutation gates still apply.

The brief reports separate dimensions rather than an opaque prosperity score:

| Dimension | Evidence | Interpretation |
|---|---|---|
| Basic supply | Electricity, water, sewage used/capacity | Headroom is necessary; building delivery still needs focused checks |
| Finance | Display cash, period net, comparable prior net | One negative sample does not automatically justify tax changes |
| Residents | Citywide problem counts, happiness | Truncated detail lists cannot hide citywide totals |
| Development | Population delta across the same city's later game time | Demand or additional road length alone is not successful growth |
| Traffic | Severe road density count | A signal to inspect actual affected routes, not proof of broken access |

Missing/failed telemetry remains unknown. Initial policy thresholds are reserve
10,000 display currency, utility utilization 90%, happiness 60, unemployment 15,
and demand 50. Only reserve is currently a public argument; other thresholds are
explicit policy constants in the assessment and must be calibrated through live
runs. These are project heuristics, not verified game rules. Garbage incineration
and deathcare cremation capacity are intentionally not interpreted as total
service capacity: storage alternatives exist.

Invalid negative counts/capacities, failed problem maps, missing healthcare or
education capacity, invalid city IDs, and unparseable simulation timestamps now
produce missing evidence. Negative money remains valid financial evidence.
The raw snapshot's attention hints also avoid prioritizing fiscal changes from
one negative period or treating incineration capacity as all garbage capacity.

The result always has `planningOnly: true` and `mutationAuthorized: false`.
No aggregate assessment proves a city prosperous or a placement safe. It does not
check progression mode; run the existing progression gate separately.

## One wake

1. Follow the repository skill's pause, city-scoped memory and progression gates.
2. Resolve live discrete prerequisites first, then use the domain sampler for
   geometry-derived candidates. Record generated, statically eligible, native-
   previewed and accepted counts when bounded search fails; do not substitute a
   larger model score for missing native feasibility.
3. Call `assess_city`, passing memory's `lastSnapshot` as `previousSnapshot` when
   it contains an earlier assessment `baseline`. Reserve should match the city's
   financial policy. The brief itself does not read/write memory or pause.
4. Inspect `nextStep`; load its `inspectTools` definitions and supply their
   domain-specific arguments. These names are a checklist, not executable calls.
5. For demand, inspect exact vacant/occupied zoning and frontage before choosing
   zoning versus roads. For a service gap, inspect affected residents, reachability,
   unlocked assets, and candidate placement/impact. Missing candidates require
   focused evidence collection; a generic wait does not repair the gap.
6. Filter candidates through unlock, native preview, reserve, topology, access,
   and safety hard gates before utility/cost ranking. Use the operator-enforced
   mutation runbook for at most one action during initial
   trials: preview, verified checkpoint, baseline, mutation, simulation settlement,
   focused inspection and regression verification. Follow existing recovery rules.
7. Observe again after meaningful simulation time. Store the returned `baseline`
   in `lastSnapshot` using the existing city-scoped memory tool. This baseline is
   comparison data, not a savegame or a rollback checkpoint. Preserve active goals
   unless evidence justifies changing them. `phase` is a suggestion.

Three comparable observations with the same leading objective and reason produce
`nextStep.kind: resolve-blocker`. This asks for the missing candidate/preview or
telemetry diagnosis instead of another no-op cycle. It is a diagnostic signal;
the adapter does not enforce a scheduler stop. Changing city or rewinding game
time invalidates comparison. Repeated calls while paused do not establish growth.
Paused reads preserve an unchanged blocker count without incrementing it, so
intermediate inspections do not erase an existing escalation history.

A `wait` result means no aggregate signal currently justifies construction. It
does not permit resuming an unresolved mutation. After all verification passes,
advance simulation long enough for the chosen metric to settle and inspect again.
Population deltas are neither rates nor evidence that a particular action caused
growth. Two negative observations can still cover the same economy reporting
period; use spaced simulation observations before fiscal decisions.

## Spatial reasoning without a second simulator

CS1 remains the simulator. Use its existing local model as a 2.5D planning view:
XZ footprints and road connectivity, plus height, grade and terrain where needed.
The existing `inspect_planning_area` and `propose_development_plan` already provide
local semantic summaries. Load them only for a spatial objective; do not send the
whole city's sampled curves to the mayor on every wake.

During a live capture, those tools also attach exact road-generated zoning-block
evidence. A plan may reuse existing frontage only when a matching block passes
the development-frontage and junction gates. Offline evidence is explicitly
unknown rather than treated as permission to reuse a road.

Keep native Bezier curves, directed reachability and building vertical bounds in
the geometry layer. A flat map cannot distinguish an intersection from a bridge.
The offline model proposes candidates; fresh native previews and live results
decide whether they work. This change does not replace or loosen geometry checks,
implement a new 3D renderer, or predict CS1's economy offline.

## Offline replay

```powershell
python scripts/city_assessment.py --snapshot examples/mayor/synthetic-growth.json
python scripts/city_assessment.py --snapshot artifacts/current.json --previous artifacts/previous.json --reserve 15000
python -m unittest discover -s scripts -p 'test_*.py'
```

Replay accepts a raw `/state/mayor-snapshot`, the existing spatial capture format
with `mayorSnapshot`, and the prior brief's `baseline` as comparison input. UTF-8
BOM files are supported. The example is synthetic. The historical dashboard's
`data/latest.json` lacks the current contract and therefore yields missing evidence;
do not use it as proof that the new live adapter works.

This is a first decision-layer refactor. Live construction success, growth, long
run stability, model comparisons and end-to-end scheduling remain to be evaluated.

## Local validation and command failures

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build.ps1 -NoInstall
```

Override the game directory with `-GamePath` or `CS1_GAME_PATH` if necessary.
Installing the compiled DLL requires Cities: Skylines to be closed. `-NoInstall`
only compiles and is safe while the game is open.

The CLI validates nested objects, local schema references, numeric ranges,
finite numbers, string lengths, and array sizes before Bridge calls. Argument
files and mayor memory accept UTF-8 BOM files written by Windows PowerShell.
Missing files, timeouts, and tool results with `ok: false` return JSON errors
with a nonzero exit status. Check that status before scheduling another action.

## Road preview reliability and recovery evidence

Road previews now sample a predicted cubic control curve using CS1's native
middle-point solver, with the same bounded density policy as post-build checks
(at least 9 points, up to 129). This is **not** the final native junction surface:
`predictsSettledSurface: false` explicitly preserves that uncertainty.

`terrainProfile.settledShape` adds the one settlement effect that is known: a
settled ground road leaves every End, Bend and Junction node level and keeps its
slope only through a straight Middle node. A segment climbing a rise therefore
sags about 0.1 x the rise below its slope near the low end, and at an existing
Bend or Junction it also starts at the node's corner, uphill but at the node's
height. The preview samples that level-ended curve (with the road's half width
as the corner offset) and `safe` is false when it falls below
`settledShape.minimumRoadAboveTerrainAllowed`. Climb straight through existing
nodes, split the climb, or follow the contours instead.

Ground-road finalization requires three matching geometry observations over at
most eight API turns. A pending response is not acceptance. The typed adapter
polls it, preserves creation evidence even when finalization returns an HTTP
error, and fails closed on timeout. Focused road QA and regression verification
are still required after settlement.

`geometryEvidence` separates native center/left/right Bezier curves and
`m_surfaceLevel` from the legacy node-inset clearance proxy. It includes precise
coordinates and per-sample terrain, native clearance and proxy clearance.
The immediate profile is explicitly `verificationStage: immediate`; the typed
adapter accepts a ground-road mutation only after a same-segment
`verificationStage: settled` profile passes its complete schema and threshold
checks. It returns that final evidence as `settledTerrainProfile`. Immediate and
settled evidence are captured **before rollback**. Settled inspection keeps the
0.02m minimum clearance. Preview/build preflight requires 0.12m: the settled
0.02m threshold plus a separate 0.10m settlement-clearance reserve. This is
independent of the 0.7m road-to-terrain deviation reserve. These gates sample the
raw heightmap and remain the default.

A ground road shapes the terrain it is built through, as it does when a player
drags one: the game lowers the drawn (detail) terrain to the road and raises it
under a fill, while the raw heightmap keeps the bump. On gently rolling land most
straight roads therefore dip below raw terrain and the default gates refuse them
(on one natural map, 97 of 105 sampled 88m roads), and next to highway stubs they
refused nearly every road. Measured live, roads 0.49m, 1.05m and 1.32m below raw
terrain left the drawn surface within 0.06m of the road's sidewalk level
(-`m_surfaceLevel`, 0.3m above the generated curve) and formed normal zone
blocks. Preview therefore also reports `terrainCut`: `carvable` for any ground
road on ground no steeper than the grade limit, and `validWithTerrainCut` when
every other gate passes too. `build_network` with `allowTerrainCut: true` leaves
the road to the game: its settled verification reports `clearanceModel:
gameShapedSurface` and fails only when the drawn terrain rises more than
`finalSurfaceTolerance` (0.15m) above the sidewalk level anywhere along it
(`buriedDepth`); how deep the cut runs and how far the terrain lies below the
road elsewhere are reported, not judged. A road refused
before construction under the default model may be tried once as a cut; any
other failure still blocks the alignment.

The adapter atomically journals each network request before sending it in
`.runtime/road-failures.json`. An interrupted/uncertain request blocks further
network builds and outside-road planning. Verified local rollback retains the
failed alignment as an exclusion; reversed endpoints or another road prefab do
not evade it. Both corresponding endpoints within 8m also count as a retry;
this is a conservative search policy, not a native collision constant.
History is bounded to 100 entries and scoped to the live city
instance, never to a map name. Successful settlement removes only that request's
in-flight record, not previous failures. Only one controller may use this file.

Do not delete the journal merely to retry. Inspect its checkpoint and live
entities, verify recovery, and use the normal restart/load workflow when needed.
A new city instance does not inherit previous coordinates. Deeper automated
recovery reconciliation and physical surface calibration remain follow-up work.
This road-specific journal/finalizer is not a generic persisted mutation controller;
checkpoint, baseline, focused QA, regression and final save remain caller-run steps.

For read-only restart diagnostics, `start-resume.ps1 -PreserveAutoSave` leaves
the game's autosave setting unchanged. It still requires a matching newest save
when `-ExpectedSaveName` is supplied and must not replace a running mod DLL.

## Intermediate outside-access construction

`inspect_road_result` still requires two-way outside reachability by default.
For an explicitly planned intermediate bidirectional segment only, set
`outsideAccessRequirement` to `inbound` or `outbound` and supply
`allowTerminalDeadEnd: true`, its exact live `terminalNodeId`, and enable dead-end
inspection. The declared terminal must have degree one, its opposite anchor must
remain connected, and the selected directed outside route must exist. All other
terrain, lane and focused anomaly checks remain mandatory.

The result separates `acceptanceChecks` and `verificationScope` from the original
`checks`. `fullOutsideAccessVerified: false` remains visible even when the
intermediate step passes. This is permission to retain a verified construction
stage, not proof that the entrance is complete or that zoning/freight can begin.
Replan from live topology; complete the default two-way QA before development.

The outside-road planner now evaluates iterative candidates on both terminals
before proposing a single extension. If only one side has a safe candidate,
it returns `safe: false` and `terminalDiagnostics` with per-pair candidate counts,
rejection reasons and bounded exact candidates for fresh native previews.
This prevents repeated inbound extensions while the outbound approach remains
blocked. Both sides passing is current-state planning evidence, not a guarantee
of a later join; only the selected segment is authorized for the usual mutation
protocol. Native geometry, terrain, settlement reserve and recovery gates are
unchanged. A bounded search failure does not prove every alignment impossible.

Failed planning reports retain a bounded set of exact candidates and rejection
gates, omitting their bulky curve samples and marking `diagnosticsTruncated`.
Re-preview an exact candidate for full prediction data. Accepted plans and actual
construction/rollback evidence retain their full geometry.

## Connected corridor planning

The outside-road planner now searches an entire connected corridor over exact live
terrain samples. It uses a sparse rotation-invariant motion lattice, terminal-local
terrain refinement, reverse-reachable goal skeletons, directed terminal reachability,
native endpoint positions and tangents, measured road width, current unlocked tiles,
bounded turns and nonadjacent corridor overlap checks before native previews. It
prioritizes statically eligible final edges and reports queued/popped goal edges,
goal-connector rejection layers, reverse skeletons and duplicate oriented states.
A separate safe step on each approach is no longer enough. Legacy merge/lateral
offset arguments are accepted for compatibility but are unused.

Only the first segment of a complete conditional route is returned for construction.
`plan.wholeRoute` retains all segments and previews; `futureJunctionsVerified: false`
requires settlement, focused QA of affected neighbors, regression and full replanning
after every build. Standalone future previews do not simulate future junctions.
Frontage is a dimensional estimate, never permission to zone. The full route must
leave at least 10000 display cash. Native and settlement guards are unchanged.
New ground-road endpoints may carry an explicit terrain-fit lift only when a
complete-route preview requires a shared-node height. The lift is bounded to
0–0.3m, cannot move an existing or read-only planning node, and is revalidated by
construction plus settled QA.

The sampled search is bounded, not complete. The base-game tile grid is supported;
road width currently requires an existing native segment of the requested prefab.
Unknown geometry fails closed. Tile-width margins can conservatively reject tile
seams. Missing paths do not prove the map impossible. Straight snapped-end direction
now follows the requested control points instead of negating an adjacent road tangent.

The planner also follows nearby live connected roads to inspect existing junction
anchors with one-sided outside reachability. It requires at least a 30-degree
branch angle from each existing incident direction and retains native collision,
terrain and neighboring-road verification. A junction alternative does not remove
or authorize removal of a blocked leaf. The search queues individual untested edges
so later path depths can compete for the preview budget. It tests both construction
orientations of a bidirectional edge and preserves the exact accepted command;
`traversalPoints` describes route order separately. Counts report accepted path depth.
All paths remain conditional until each constructed junction has settled. The
planner compares precise predicted clearance against the Bridge-reported
`minimumRoadAboveTerrainAllowed`; it does not own a second hidden clearance
constant. A marginal preview remains conditional on settled verification and
rollback, not proof that it will survive junction settlement.

Precise controlling terrain samples override rounded compact aggregates for the
same thresholds. Missing precise telemetry fails closed when prediction evidence
is present. Successive edge previews must agree on the shared future node height
within 0.01m; unknown or inconsistent heights reject that route. Agreement is a
necessary planning check, not prediction of future native junction geometry.

Future route edges use read-only `planningStartY` / `planningEndY` previews to carry
that exact shared height forward. The Bridge freezes only this hypothetical
endpoint, checks its height against the existing terrain seed plus bounded 0..0.3m
fit, and retains every terrain/native guard. It rejects these fields on construction
endpoints and on an actual existing node. `planningStartNode` / `planningEndNode`
acknowledge this support explicitly. These previews do not create nodes or simulate
future incident roads. Only the first edge has a real-state construction candidate;
settle and replan before constructing any later edge. A hypothetical preview must
never be replayed as a build command.

## Bounded search and empty-city priorities

`maxNativePreviews` caps the entire call (default 96, maximum 192), shared across
every anchor pair and orientation. `maxPlanningSeconds` defaults to 30; it stops
new previews, but an in-flight request can take its transport timeout. Exact
candidates share a per-call cache; orientation and hypothetical heights remain
distinct. No native result is reused across reloads or construction.
`.runtime/road-search-progress.json` records counts, candidate, elapsed time,
rejection layers and errors during execution. A budget stop is not proof of an
impossible road and must not trigger an unchanged automatic retry.
Blocked searches also report candidate-stream attempt totals, locally evaluated
preview variants, contributing layers, and `provenRootCause`. Progressive replay
can reuse the invocation cache, so locally evaluated variants are not native-call
counts; `searchDiagnostics.nativePreviewCalls` is authoritative. Exact budget
consumption reports `stopReason: nativePreviewBudget` even when no extra request
was made. A null `provenRootCause` requires a discriminating geometry or
scheduling check before another run.
`terminationLayer` states why exploration stopped, `limitingResource` names an
exhausted bound, and `rejectingLayer` summarizes the observed candidate gates;
none of them alone proves why the unexplored sampled graph has no route.

The corridor turn policy accepts an exact 90-degree connection when native
preview, junction-angle, overlap, terrain, and settlement gates all pass. It
still rejects U-turns and angles above 90 degrees. This supports ordinary
perpendicular entrance pairs without map-specific coordinates and is covered by
translated and rotated synthetic layouts. Reverse-skeleton pop, acceptance,
rejection, and rejection-category counters distinguish scheduling failures from
native or policy rejection when bounded search still does not find a route.

`plannerImplementation.schemaVersion: 2` records separate SHA-256 hashes for the
route planner and acceptance adapter plus a combined hash. These identify the
executed Python sources, not the loaded Bridge DLL or a successful gameplay
outcome.

A successful route is retained in a city-scoped local hint. The next call
rechecks its current directed anchors, exact terrain samples, native geometry,
previews, shared heights and cash reserve. Built prefixes are trimmed only at
matching live anchors. Use `hintOnly: true` to return on a stale or rejected hint
without broad search. Future preview fields remain forbidden on construction.

An empty city's assessment prioritizes a complete usable neighborhood over
generic stub signals: verify outside access and road QA, safe residential
frontage, utility delivery, then actual move-ins. Missing telemetry and critical
resident/utility problems still outrank this inspection goal. No assessment
authorizes construction or declares intermediate roads successful urban growth.

In one live city, hint revalidation took 3, 2 and 1 native previews as three
segments were built. All eight local Basic Roads then passed full two-way QA;
regression and same-grade crossing checks passed. This verifies the entrance:
population remained zero, utilities/zoning were absent and safe zoning frontage
was unavailable. Actual multi-map growth is untested.

`inspect_terrain_editing` reads the native Landscaping feature unlock, its
required milestone, actual toolbar usability, soil buffer and native unit price.
Soil units are not reported as cubic metres. This is capability inspection only;
the Bridge does not yet implement terrain mutation or an affected-area/flood/cost
preview. Do not terraform to compensate for an uncalibrated road-surface proxy.
Compare a terrain-following route with bounded earthworks only after checking
the native unlock, usable UI, soil, budget, water and existing infrastructure.

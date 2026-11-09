<h1 align="center">cities-skylines-ai-agent</h1>

<p align="center">
  An AI mayor that plays Cities: Skylines 1 through a local API bridge mod: it reads the city as data,
  plans roads, zoning and services, and makes every change under a checkpoint with verification.
</p>

<p align="center">
  <a href="README.zh-TW.md">繁體中文 README</a> ·
  <a href="docs/api.md">API Reference</a> ·
  <a href="docs/research/cs1-planning-knowledge-zh-TW.md">CS1 planning knowledge (zh-TW)</a> ·
  <a href="CONTRIBUTING.md">Contributing</a>
</p>

This project started as a fork of
[Sunwood-ai-labs/cities-skylines1-agent-skill](https://github.com/Sunwood-ai-labs/cities-skylines1-agent-skill)
(see [Credits](#-credits)). The original provides the CS1 mod that turns the game into a local HTTP API.
This fork grows it into an agent that keeps a city running and expanding on its own:

- **99 typed mayor tools** with schemas, previews and safety gates. They cover zoning by exact block,
  frontage-road proposals, water and power planning, road upgrades, district policies, bulldoze impact
  checks and more.
- **A mutation protocol** for every change: preview → verified checkpoint → regression baseline →
  mutation → settle → focused QA → regression check.
- **Checks that use the game's own rules.** After a road upgrade the tools run the game's
  `Building.CheckZoning`, so no building is silently demolished. Road builds check how the road settles
  against the terrain.
- **An autonomous play loop** (`scripts/loop/`) that keeps a city running on its own: services, utilities,
  zoning pacing, traffic remedies (widening, junction control, transit, bypasses, highway exits, metro)
  and freight rail, each judged afterwards by what it changed. A large model (such as Claude) is needed
  only for the structural problems the loop hands it. An optional small local model can cross-check
  readings (`--model`); it never calls mutating tools.
- **Researched gameplay knowledge**, written as short rules. It covers demand pacing, the zoning
  junction gate, grade-separated highway crossings, utilities, landfills and road hierarchy
  ([docs/research](docs/research/cs1-planning-knowledge-zh-TW.md)).

The bridge never uses screenshots or computer vision: all city state comes from deterministic telemetry.

## ✨ What It Does

- Runs a CS1 mod (`SkylinesAgentBridge`) that listens on `http://127.0.0.1:32123`, and only on loopback.
- Exposes city state: problems, facilities, networks, traffic, zone blocks and cells, growth capacity,
  utilities, road/building/zoning anomalies, saves and prefabs.
- Exposes focused commands: building networks (including ramps with a height per endpoint), zoning,
  placing buildings, bulldozing, upgrading roads, budgets and taxes, transport lines, simulation speed
  and saving.
- Wraps these in typed tools (`scripts/mayor_tools.py`, `references/mayor-tools.json`) that require
  checkpoints and previews and verify each change afterwards.
- Includes Windows scripts to build the mod, resume a save, start a fresh map, inspect and repair, and save.
- Ships as an agent skill through [SKILL.md](SKILL.md) and [agents/openai.yaml](agents/openai.yaml).

## 🔒 Security model

The Bridge controls the running game and has no authentication, so it serves local agents only.

- It listens on the loopback interface only (`127.0.0.1`).
- It refuses requests whose `Host` header is not loopback, which blocks DNS rebinding.
- It refuses requests that carry a non-loopback `Origin` header and sends no CORS headers. Web pages you
  visit while the game runs therefore cannot read or change your city. Agents (Python, curl, PowerShell)
  are not affected.
- Mutating tools require a verified checkpoint save. Try it on a throwaway save first.

## 🚀 Quick Start

Requirements: Windows, Cities: Skylines 1 (Steam), PowerShell, the .NET Framework compiler (`csc.exe`,
included with Windows) and Python 3.11+ (use the newest patch release of your line). Building the docs
site also needs Node.js 24.22.1+ or 26.12.1+, the 2026-10-21 security release; `docs/.npmrc` refuses older
versions.

Build and install the mod. The script finds the game in any Steam library. If the game is installed
somewhere else, set `CS1_GAME_PATH` or pass `-GamePath`:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build.ps1
# elsewhere:  $env:CS1_GAME_PATH = "E:\Games\Cities_Skylines"
```

The script compiles `SkylinesAgentBridge.dll` and copies it into
`%LOCALAPPDATA%\Colossal Order\Cities_Skylines\Addons\Mods\SkylinesAgentBridge`.
Enable the mod in the content manager, load a city, then test:

```powershell
Invoke-RestMethod http://127.0.0.1:32123/health
Invoke-RestMethod http://127.0.0.1:32123/state/summary
```

Resume the newest local save through the launcher. Steam blocks `--loadSave` unless you confirm it by
hand, so the script clicks Resume instead:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-resume.ps1
```

Optional local model: start an OpenAI-compatible server. LM Studio's default address is
`http://127.0.0.1:1234/v1`; if yours differs, set `LOCAL_MODEL_URL` and `LOCAL_MODEL`.

Run the unit tests (standard library only, no game needed):

```powershell
cd scripts
python -m unittest discover -p "test_*.py"
```

See [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request.

## 🧭 Agent Repair Pattern

Keep the workflow generic. Prefer separate commands over a magical repair endpoint:

1. Inspect with `/state/problems`, `/state/road-anomalies`, `/state/building-anomalies`, `/state/facilities`, and `/state/networks`.
2. Remove bad objects with `/commands/bulldoze`.
3. Rebuild with `/commands/build-network`, `/commands/place-building`, `/commands/move-building`, and `/commands/set-zone`.
4. Let the simulation settle with `/commands/set-simulation-speed`.
5. Re-check state APIs.
6. Save with `/commands/save` or `scripts/save-city.ps1`, then verify with `/state/saves`.

## 🧠 Adaptive Mayor Loop

The skill separates deterministic safety from dynamic planning:

1. `load_mayor_memory` restores city vision, goals, blockers, cooldowns, and
   recent verified actions.
2. `observe_city` reads the bounded `/state/mayor-snapshot` wake-up payload.
3. The agent creates context-specific candidates and ranks them with
   `score_candidates`.
4. Zero to three justified actions may run through preview, checkpoint,
   mutation, focused verification, and save.
5. `save_mayor_memory` records the result and next recheck condition.

Function-calling schemas are in `references/mayor-tools.json`. The
dependency-free adapter can print definitions or execute one tool:

```powershell
python .\scripts\mayor_tools.py definitions
'{"detailLimit":10}' | python .\scripts\mayor_tools.py call observe_city --arguments-file -
```

The loop may choose to wait. It does not follow a fixed build order and cannot
use progression-locked assets.

For unattended play on any map, [`scripts/loop/`](docs/guide/autonomous-loop.md) runs
wakes on its own: it zones, densifies and opens new land, keeps utilities connected,
solves problems with the facility each need calls for, and tries measured traffic
remedies, stopping only for what needs a planner. What it learns about one city stays
in that city's private profile under `.runtime/`.

```powershell
python .\scripts\loop\play.py 8 60 --reserve 8000
```

The play loop does not call the local model unless you add `--model` (a cross-check of each wake's reading).

## 🌳 Current Tools Tree

From `references/mayor-tools.json`. There are
**99 typed tools**, including
the **6-tool mayor startup profile**. Each tool appears once below; the other
groups are documentation categories, not additional CLI profiles.

Sources: [function schemas](references/mayor-tools.json),
[Python dispatch and guards](scripts/mayor_tools.py), and
[C# HTTP routes](src/ApiServer.cs).

```text
CS1 mayor tools (99)
├── Mayor startup profile (6)
│   ├── assess_city
│   ├── observe_city
│   ├── validate_progression_mode
│   ├── load_mayor_memory
│   ├── save_mayor_memory
│   └── set_simulation_speed
├── Economy, growth and decisions (11)
│   ├── inspect_economy
│   ├── inspect_growth_capacity
│   ├── propose_zoning_blocks
│   ├── propose_expansion_areas
│   ├── propose_network_crossing
│   ├── score_candidates
│   ├── zone_block
│   ├── purchase_map_area
│   ├── set_service_budget
│   ├── set_tax_rate
│   └── take_loan
├── Roads and shared network construction (21)
│   ├── inspect_traffic
│   ├── assess_traffic_congestion
│   ├── propose_traffic_remedies
│   ├── propose_bypass_roads
│   ├── inspect_segment_routes
│   ├── propose_frontage_road
│   ├── propose_outside_road_connection
│   ├── preview_network
│   ├── build_network
│   ├── inspect_road_result
│   ├── preview_road_upgrade
│   ├── upgrade_road
│   ├── inspect_building_zoning_support
│   ├── repair_building_zoning
│   ├── preview_road_reversal
│   ├── reverse_road
│   ├── split_road_segment
│   ├── inspect_junction
│   ├── set_junction_control
│   ├── trial_junction_control
│   └── widen_road_chain
├── Snapshot geometry and local planning (7)
│   ├── inspect_zone_cell_geometry
│   ├── inspect_road_layers_3d
│   ├── inspect_grade_separated_crossings
│   ├── inspect_building_layers_3d
│   ├── inspect_building_road_clearance_3d
│   ├── inspect_planning_area
│   └── propose_development_plan
├── Unlocks, services and building operation (16)
│   ├── inspect_toolbar_unlocks
│   ├── list_unlocked_assets
│   ├── inspect_service_coverage
│   ├── inspect_service_capacity
│   ├── assess_problem_remedies
│   ├── diagnose_utility_shortfall
│   ├── diagnose_noise
│   ├── inspect_facility_influence
│   ├── preview_service_impact
│   ├── preview_building_candidate
│   ├── compare_service_options
│   ├── propose_service_sites
│   ├── place_service
│   ├── rebuild_full_facility
│   ├── preview_operational_change
│   └── set_building_active
├── Water and sewage (11)
│   ├── propose_water_pair_candidates
│   ├── propose_groundwater_pump_sites
│   ├── inspect_water_pair
│   ├── propose_water_service_connection
│   ├── preview_water_system_plan
│   ├── place_water_facility
│   ├── build_water_pipe
│   ├── build_water_service_pipe
│   ├── connect_water_facility
│   ├── inspect_water_network_topology
│   └── inspect_water_service_coverage
├── Electricity (10)
│   ├── inspect_wind_resource_map
│   ├── propose_power_plant_candidates
│   ├── preview_power_plant_candidate
│   ├── place_power_plant
│   ├── inspect_power_plant_operation
│   ├── inspect_electricity_connection_area
│   ├── propose_power_line_connection
│   ├── propose_minimal_power_grid
│   ├── inspect_power_grid_efficiency
│   └── inspect_electricity_service_coverage
├── Public transport (6)
│   ├── inspect_transport
│   ├── propose_transit_lines
│   ├── preview_transport_plan
│   ├── create_transport_line
│   ├── set_transport_line
│   └── delete_transport_line
├── Districts (3)
│   ├── create_district
│   ├── preview_district_specialization
│   └── set_district_specialization
├── Terrain capability inspection (1)
│   └── inspect_terrain_editing
└── Deletion, checkpoints and verification (7)
    ├── preview_bulldoze_impact
    ├── bulldoze_entity
    ├── clear_abandoned_buildings
    ├── create_checkpoint
    ├── capture_regression_baseline
    ├── verify_regression
    └── save_city
```

| Layer | What it provides | Boundary |
| --- | --- | --- |
| Mayor profile | Assessment, observation, progression, memory and speed | Includes file writes and simulation control; not a read-only profile or autonomous scheduler |
| Typed domain tools | Inspection, candidate generation, preview, construction and verification | `propose_*` / `preview_*` do not authorize construction; fresh live checks are still required |
| Snapshot geometry | Layered XYZ curves, crossings, clearance and local plans | Explicit snapshots support offline analysis; omitted paths may capture live data. This is not an economy/traffic simulator |
| C# Bridge | 57 GET and 47 POST route paths, including the `build-road` alias | Broader than the typed tools; POST includes previews and baseline capture, not just mutations |

Load exact schemas with `python scripts/mayor_tools.py definitions --profile mayor`,
`python scripts/mayor_tools.py definitions --cluster roads`, or
`python scripts/mayor_tools.py definitions --names preview_network inspect_road_result`.
The `clusters` command lists the available domain slices without embedding their
full schemas and reports compact-JSON character sizes as a tokenizer-independent
size proxy. Clusters may overlap where one tool legitimately supports multiple
domains; the manifest is validated so every typed tool belongs to at least one.
The host must register the returned definitions or call the CLI; selection is not
a permissions boundary. New complete water systems use `build_water_pipe`;
extensions from a verified existing component use `build_water_service_pipe`; other supported
networks use `build_network`. Power plants and water facilities have dedicated
placement gates.

The [compact loop](docs/guide/compact-mayor.md) still requires preview → verified
checkpoint → baseline → mutation → settlement → focused QA and regression → save.
Some guards run in the adapter; the complete workflow remains agent/skill managed.
There is no typed `restore_checkpoint` tool; recovery follows the existing skill
and restart/load procedure.

## 🔌 API Surface

Selected lower-level routes are listed below; this is not the complete tool or
HTTP inventory. The typed tool tree above and the source links define the audited
surface, while [docs/api.md](docs/api.md) provides usage examples.

Read APIs:

- `GET /health`
- `GET /state/mayor-snapshot`
- `GET /state/summary`
- `GET /state/problems`
- `GET /state/chirps`
- `GET /state/zones`
- `GET /state/economy`
- `GET /state/facilities`
- `GET /state/networks`
- `GET /state/road-anomalies`
- `GET /state/building-anomalies`
- `GET /state/zone-anomalies`
- `GET /state/saves`
- `GET /prefabs/roads`
- `GET /prefabs/networks`
- `GET /prefabs/buildings`

Command APIs:

- `POST /commands/build-network`
- `POST /commands/build-road` compatibility alias
- `POST /commands/set-zone`
- `POST /commands/place-building`
- `POST /commands/move-building`
- `POST /commands/bulldoze`
- `POST /commands/save`
- `POST /commands/set-simulation-speed`
- `POST /commands/set-tax-rate`
- `POST /commands/batch` optional convenience wrapper

See [docs/api.md](docs/api.md) for request examples and response shapes.

## 🧩 Skill Usage

The root [SKILL.md](SKILL.md) tells an agent how to operate CS1 through this bridge. The skill keeps the
upstream name `cities-skylines1-agent-skill` so existing installs keep working. Example prompt:

```text
Use $cities-skylines1-agent-skill to resume my CS1 city, inspect current problems, expand roads and zoning
where demand is high, verify every change, save the city, and report what changed.
```

## 📚 Documentation

- [Getting Started](docs/guide/getting-started.md) · [Agent Workflow](docs/guide/usage.md) ·
  [Architecture](docs/guide/architecture.md) · [Troubleshooting](docs/guide/troubleshooting.md)
- [API Reference](docs/api.md) and the [compact mayor loop](docs/guide/compact-mayor.md) · [autonomous play loop](docs/guide/autonomous-loop.md)
- [CS1 planning knowledge (zh-TW)](docs/research/cs1-planning-knowledge-zh-TW.md): gameplay rules with
  sources, and lessons from live play

## 🗂️ Repository Layout

```text
.
├── SKILL.md                 # Agent skill instructions
├── agents/openai.yaml       # Skill UI metadata
├── src/                     # CS1 mod (Bridge) source, C#
├── scripts/                 # Typed tools, build/launch/inspect/save scripts and tests
├── scripts/mayor/           # Transport, contracts and memory behind the tools CLI
├── references/              # Tool schemas, clusters and gameplay rules
├── docs/                    # Guides, API reference and research notes
└── .github/workflows/       # Docs validation and Pages deployment
```

Machine-local state (tools, caches, checkpoint archives, play logs) lives in the git-ignored `.runtime/`.

## ⚠️ Status

This is experimental and Windows-only, for Cities: Skylines 1. The bridge changes live simulation objects
on the game thread, so keep changes small, verify after each step, and back up saves you care about.
Known limitations are listed in the [knowledge doc](docs/research/cs1-planning-knowledge-zh-TW.md). For example,
widening a road that already has buildings along it still needs a human decision: the game rebuilds the zone
cells where a wider road meets a narrower one, and buildings standing on them can lose their zoning.
Cities: Skylines is a trademark of Colossal Order / Paradox Interactive; this is an independent fan project.

## 🙏 Credits

This repository is a fork of
**[Sunwood-ai-labs/cities-skylines1-agent-skill](https://github.com/Sunwood-ai-labs/cities-skylines1-agent-skill)**
by Sunwood-ai-labs. That project created the original CS1 agent bridge mod, its API, the scripts and the skill
(see its [docs site](https://sunwood-ai-labs.github.io/cities-skylines1-agent-skill/)). Thank you!
This fork keeps that work under its MIT license and adds the typed mayor tools, the verification protocol,
local-model delegation and the gameplay research.

## 📄 License

MIT. See [LICENSE](LICENSE). The original copyright of Sunwood-ai-labs is kept.

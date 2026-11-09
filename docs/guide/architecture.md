# Architecture

The repository has two connected parts: a CS1 mod that exposes a local API and an agent skill that teaches an agent how to use that API safely.

## Runtime Shape

```text
AI agent
  -> adaptive mayor skill
  -> typed tool adapter + persistent mayor memory
  -> PowerShell/Python scripts
  -> http://127.0.0.1:32123
  -> SkylinesAgentBridge mod
  -> CS1 game thread queue
  -> game managers and save panel
```

The API server accepts local HTTP requests. State reads and mutating commands are dispatched through a game-thread queue so CS1 simulation objects are touched from the correct thread.

## Source Layout

```text
src/
├── ApiServer.cs
├── GameState.cs
├── RoadCommands.cs
├── BuildingCommands.cs
├── ZoneCommands.cs
├── BulldozeCommands.cs
├── SimulationCommands.cs
├── SaveCommands.cs
├── BatchCommands.cs
└── AgentBridge*.cs
```

`ApiServer.cs` maps HTTP paths to state builders or command handlers. `GameState.cs` builds JSON for city state. The command files keep mutating operations separated so an agent can choose one clear action at a time.

`/state/mayor-snapshot` is the bounded wake-up surface. It supplies compact
evidence and attention signals but does not prescribe a fixed action.
`scripts/mayor_tools.py` exposes JSON-schema function definitions, validates
arguments, calls the Bridge, requires verified checkpoints for mutation tools,
persists mayor memory atomically, and ranks dynamic candidates.

The safety protocol is fixed: observe, preview, checkpoint, mutate, verify,
remember. The selected goal changes with progression unlocks, city vision,
demand, capacity, coverage, physical growth space, traffic, economics,
cooldowns, and the last verified result.

## In-Game Notifications

Game-state API calls are written to a small in-game API console instead of a
temporary toast. The console keeps recent request history, shows timestamps,
and has a minimize button so it can stay available without covering the city.

`/health` is intentionally quiet because it can be called before a level is loaded.

## Skill Metadata

`SKILL.md` contains the adaptive operating policy. Detailed function schemas
and persistent memory shape live in `references/` so they are loaded only when
needed. `agents/openai.yaml` provides the display name, default prompt, and
implicit invocation setting for agent skill surfaces.

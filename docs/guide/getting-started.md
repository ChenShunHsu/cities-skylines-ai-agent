# Getting Started

This guide assumes a Windows machine with Cities: Skylines 1 installed through Steam.

## Requirements

- Cities: Skylines 1.
- PowerShell.
- .NET Framework compiler tools available through Windows.
- CS1 managed assemblies under the game install directory.

`scripts/build.ps1` finds the game in any Steam library (it reads Steam's registry entry and
`steamapps\libraryfolders.vdf`). If the game lives somewhere else, point the scripts at the folder
that contains `Cities.exe`:

```powershell
$env:CS1_GAME_PATH = "E:\Games\Cities_Skylines"   # or: .\scripts\build.ps1 -GamePath <folder>
```

## Build and Install

From the repository root:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build.ps1
```

The script compiles `SkylinesAgentBridge.dll` and copies it into:

```text
%LOCALAPPDATA%\Colossal Order\Cities_Skylines\Addons\Mods\SkylinesAgentBridge
```

Enable the mod in the CS1 content manager before loading a city.

## Resume a City

The normal loop starts from the latest local save:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-resume.ps1
```

The script builds the mod unless `-SkipBuild` is passed, launches CS1 through Steam, clicks the launcher Resume button, and waits for the local API.

To guard an unattended QA run against resuming the wrong city, provide the
expected newest save and its city name. Resume continues the save the game
itself last saved or loaded, which file times do not always show, so the
script also checks the city that actually loaded:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-resume.ps1 -ExpectedSaveName "<your save name>" -ExpectedCityName "<your city name>"
```

The launcher deliberately sends no custom game arguments. Do not start
`Cities.exe` with `--loadSave`; Steam treats it as a custom launch and displays
a confirmation dialog. If the requested save is not the newest save,
`start-resume.ps1` stops before closing the game instead of falling back to that
interactive launch path.

## Start a Fresh Map

For clean experiments:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-new-map.ps1
```

Useful flags:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-new-map.ps1 -SkipBuild
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-new-map.ps1 -SkipNewMap
```

## Verify the Bridge

Once a city is loaded:

```powershell
Invoke-RestMethod http://127.0.0.1:32123/health
Invoke-RestMethod http://127.0.0.1:32123/state/summary
```

Use `scripts/smoke-test.ps1` for a broader read and dry-run command check.

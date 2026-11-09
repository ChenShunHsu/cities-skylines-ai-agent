param(
    [switch]$NoInstall,
    [string]$GamePath = $env:CS1_GAME_PATH
)

$ErrorActionPreference = "Stop"

# Find the Cities: Skylines install in any Steam library (registry + libraryfolders.vdf)
# unless -GamePath or CS1_GAME_PATH names it.
function Find-SteamGamePath {
    $steam = (Get-ItemProperty -Path "HKCU:\Software\Valve\Steam" -Name SteamPath -ErrorAction SilentlyContinue).SteamPath
    if (-not $steam) { return $null }
    $libraries = @($steam)
    $vdf = Join-Path $steam "steamapps\libraryfolders.vdf"
    if (Test-Path $vdf) {
        $libraries += Select-String -Path $vdf -Pattern '"path"\s+"([^"]+)"' |
            ForEach-Object { $_.Matches[0].Groups[1].Value -replace '\\\\', '\' }
    }
    foreach ($library in $libraries) {
        $candidate = Join-Path $library "steamapps\common\Cities_Skylines"
        if (Test-Path (Join-Path $candidate "Cities.exe")) { return $candidate }
    }
    return $null
}

if (-not $GamePath) { $GamePath = Find-SteamGamePath }
if (-not $GamePath -or -not (Test-Path (Join-Path $GamePath "Cities_Data\Managed"))) {
    throw "Cities: Skylines was not found. Pass -GamePath or set CS1_GAME_PATH to the game folder (the one containing Cities.exe)."
}

$repo = Split-Path -Parent $PSScriptRoot
$src = Join-Path $repo "src"
$out = Join-Path $repo "bin"
$game = $GamePath
$managed = Join-Path $game "Cities_Data\Managed"
$csc = "$env:WINDIR\Microsoft.NET\Framework\v3.5\csc.exe"

if (!(Test-Path $csc)) {
    $csc = "$env:WINDIR\Microsoft.NET\Framework\v4.0.30319\csc.exe"
}

if (!(Test-Path $csc)) {
    throw "csc.exe was not found. Install .NET Framework developer tools."
}

if (!(Test-Path (Join-Path $managed "ICities.dll"))) {
    throw "Cities: Skylines managed DLLs were not found at $managed"
}

New-Item -ItemType Directory -Force -Path $out | Out-Null

$sources = Get-ChildItem -LiteralPath $src -Filter *.cs | Sort-Object Name | ForEach-Object { $_.FullName }
$target = Join-Path $out "SkylinesAgentBridge.dll"

& $csc `
    /nologo `
    /target:library `
    /out:$target `
    /debug:pdbonly `
    /optimize+ `
    /define:TRACE `
    /reference:"$managed\ICities.dll" `
    /reference:"$managed\Assembly-CSharp.dll" `
    /reference:"$managed\Assembly-CSharp-firstpass.dll" `
    /reference:"$managed\ColossalManaged.dll" `
    /reference:"$managed\UnityEngine.dll" `
    $sources

if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host "Built $target"
if (!$NoInstall) {
    if (Get-Process -Name Cities -ErrorAction SilentlyContinue) {
        throw 'Cities: Skylines is running. Close the game before installing the Bridge, or use -NoInstall to compile only.'
    }
    $modDir = Join-Path $env:LOCALAPPDATA "Colossal Order\Cities_Skylines\Addons\Mods\SkylinesAgentBridge"
    New-Item -ItemType Directory -Force -Path $modDir | Out-Null
    Copy-Item -LiteralPath $target -Destination (Join-Path $modDir "SkylinesAgentBridge.dll") -Force
    Write-Host "Copied to $modDir"
}
else {
    Write-Host "Skipped live mod installation (-NoInstall)."
}

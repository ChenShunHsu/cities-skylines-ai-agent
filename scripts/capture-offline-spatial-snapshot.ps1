param(
    [string]$BaseUrl = "http://127.0.0.1:32123",
    [string]$OutputPath = ""
)

$ErrorActionPreference = "Stop"
$BaseUrl = $BaseUrl.TrimEnd("/")

function Read-BridgeJson {
    param([Parameter(Mandatory = $true)][string]$Path)
    Invoke-RestMethod -Method Get -Uri ($BaseUrl + $Path) -TimeoutSec 30
}

if ([string]::IsNullOrWhiteSpace($OutputPath)) {
    $stamp = [DateTime]::UtcNow.ToString("yyyyMMdd-HHmmss")
    $OutputPath = Join-Path (Join-Path $PSScriptRoot "..\artifacts") "city-spatial-snapshot-$stamp.json"
}

$areas = Read-BridgeJson "/state/map-areas"
$networks = Read-BridgeJson "/state/networks?limit=5000"
$roads = @($networks.segments | Where-Object { $_.service -eq "Road" })
$roadTemplates = @{}
foreach ($group in ($roads | Group-Object prefab)) {
    $representative = $group.Group | Select-Object -First 1
    $roadTemplates[$group.Name] = Read-BridgeJson ("/state/network-segment-details?id=" + $representative.id)
}

$tileSize = 1920.0
$tileAnalyses = @()
foreach ($tile in @($areas.areas | Where-Object { $_.unlocked })) {
    $centerX = ([int]$tile.x - 2) * $tileSize
    $centerZ = ([int]$tile.z - 2) * $tileSize
    $minX = $centerX - $tileSize * 0.5
    $minZ = $centerZ - $tileSize * 0.5
    $maxX = $centerX + $tileSize * 0.5
    $maxZ = $centerZ + $tileSize * 0.5
    $analysisPath = "/state/map-analysis?minX=$minX&minZ=$minZ&maxX=$maxX&maxZ=$maxZ&resolution=64&maxBuildSlope=0.35"
    $coveragePath = "/state/utility-coverage?minX=$minX&minZ=$minZ&maxX=$maxX&maxZ=$maxZ&resolution=64"
    $tileAnalyses += [ordered]@{
        tile = $tile
        bounds = [ordered]@{ minX = $minX; minZ = $minZ; maxX = $maxX; maxZ = $maxZ }
        map = Read-BridgeJson $analysisPath
        utilities = Read-BridgeJson $coveragePath
    }
}

$snapshot = [ordered]@{
    schemaVersion = 2
    capturedAtUtc = [DateTime]::UtcNow.ToString("o")
    source = $BaseUrl
    mayorSnapshot = Read-BridgeJson "/state/mayor-snapshot"
    areas = $areas
    zones = Read-BridgeJson "/state/zones"
    facilities = Read-BridgeJson "/state/facilities?limit=5000&includeMapObjects=true"
    facilityInfluence = Read-BridgeJson "/state/facility-influence?limit=5000&includeInactive=true"
    growables = Read-BridgeJson "/state/growables?limit=5000"
    networks = $networks
    roadTemplates = $roadTemplates
    tileAnalyses = $tileAnalyses
    roadAnomalies = Read-BridgeJson "/state/road-anomalies?limit=1000&includeDeadEnds=true"
    buildingAnomalies = Read-BridgeJson "/state/building-anomalies?limit=1000&includeOriginal=true"
    externalConnections = Read-BridgeJson "/state/external-connections?limit=500"
    naturalResources = Read-BridgeJson "/state/natural-resources"
    transportLines = Read-BridgeJson "/state/transport-lines?limit=1000"
}

$resolvedOutput = [IO.Path]::GetFullPath($OutputPath)
$directory = [IO.Path]::GetDirectoryName($resolvedOutput)
[IO.Directory]::CreateDirectory($directory) | Out-Null
$json = $snapshot | ConvertTo-Json -Depth 30 -Compress
[IO.File]::WriteAllText($resolvedOutput, $json, (New-Object Text.UTF8Encoding($false)))

[ordered]@{
    ok = $true
    path = $resolvedOutput
    bytes = (Get-Item -LiteralPath $resolvedOutput).Length
    roads = $roads.Count
    networks = @($networks.segments).Count
    facilities = @($snapshot.facilities.facilities).Count
    growables = @($snapshot.growables.growables).Count
    unlockedTiles = @($tileAnalyses).Count
    roadPrefabs = @($roadTemplates.Keys).Count
} | ConvertTo-Json -Compress

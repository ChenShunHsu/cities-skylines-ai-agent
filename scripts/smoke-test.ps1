$ErrorActionPreference = "Stop"

$base = "http://127.0.0.1:32123"

Write-Host "Checking $base/health"
Invoke-RestMethod "$base/health" | ConvertTo-Json -Depth 8

Write-Host "Checking $base/state/summary"
Invoke-RestMethod "$base/state/summary" | ConvertTo-Json -Depth 8

Write-Host "Checking $base/state/mayor-snapshot"
Invoke-RestMethod "$base/state/mayor-snapshot?detailLimit=5" | ConvertTo-Json -Depth 12

Write-Host "Checking $base/state/problems"
Invoke-RestMethod "$base/state/problems?limit=20" | ConvertTo-Json -Depth 10

Write-Host "Checking $base/prefabs/roads"
Invoke-RestMethod "$base/prefabs/roads" | ConvertTo-Json -Depth 8

Write-Host "Checking dry-run road command"
$road = @{
    dryRun = $true
    roadPrefab = "Basic Road"
    start = @{ x = 0; z = 0 }
    end = @{ x = 80; z = 0 }
    name = "Agent Smoke Test Road"
} | ConvertTo-Json -Depth 4

Invoke-RestMethod -Method Post -Uri "$base/commands/build-road" -Body $road -ContentType "application/json" | ConvertTo-Json -Depth 8

Write-Host "Checking dry-run batch command"
$batch = @{
    dryRun = $true
    stopOnError = $true
    commands = @(
        @{
            type = "build-road"
            roadPrefab = "Basic Road"
            start = @{ x = 120; z = 0 }
            end = @{ x = 200; z = 0 }
            name = "Agent Batch Smoke Road"
        },
        @{
            type = "set-zone"
            zone = "ResidentialLow"
            center = @{ x = 160; z = 0 }
            radius = 48
        }
    )
} | ConvertTo-Json -Depth 6

Invoke-RestMethod -Method Post -Uri "$base/commands/batch" -Body $batch -ContentType "application/json" | ConvertTo-Json -Depth 10

Write-Host "Checking base-game world and governance reads"
Invoke-RestMethod "$base/state/unlocks" | ConvertTo-Json -Depth 8
Invoke-RestMethod "$base/state/city-health" | ConvertTo-Json -Depth 8
Invoke-RestMethod "$base/state/economy-details" | ConvertTo-Json -Depth 8
Invoke-RestMethod "$base/state/budgets" | ConvertTo-Json -Depth 8
Invoke-RestMethod "$base/state/districts" | ConvertTo-Json -Depth 8
Invoke-RestMethod "$base/state/natural-resources?step=4&limit=4&minIntensity=16&unlockedOnly=true" | ConvertTo-Json -Depth 8
Invoke-RestMethod "$base/state/map-analysis?minX=-256&minZ=-256&maxX=256&maxZ=256&resolution=4" | ConvertTo-Json -Depth 8
Invoke-RestMethod "$base/state/service-capacity?service=Electricity" | ConvertTo-Json -Depth 8
Invoke-RestMethod "$base/state/service-coverage?service=FireDepartment&minX=-256&minZ=-256&maxX=256&maxZ=256&resolution=4" | ConvertTo-Json -Depth 8
Invoke-RestMethod "$base/state/utility-coverage?minX=-256&minZ=-256&maxX=256&maxZ=256&resolution=4" | ConvertTo-Json -Depth 8
Invoke-RestMethod "$base/state/traffic?limit=10&minimumDensity=64" | ConvertTo-Json -Depth 8
Invoke-RestMethod "$base/state/education" | ConvertTo-Json -Depth 8
Invoke-RestMethod "$base/state/loan-options" | ConvertTo-Json -Depth 8
Invoke-RestMethod "$base/state/service-vehicles?limit=10" | ConvertTo-Json -Depth 8
Invoke-RestMethod "$base/state/restore-status" | ConvertTo-Json -Depth 8

Write-Host "Checking service impact preview"
$serviceImpact = @{
    buildingPrefab = "Elementary School"
    position = @{ x = 100; z = 100 }
    requireRoadAccess = $false
    lowCoverageThreshold = 32
    limit = 10
} | ConvertTo-Json -Depth 4
Invoke-RestMethod -Method Post -Uri "$base/commands/preview-service-impact" -Body $serviceImpact -ContentType "application/json" | ConvertTo-Json -Depth 10

Write-Host "Checking regression baseline and report"
$baseline = @{
    name = "smoke-test"
} | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "$base/commands/capture-regression-baseline" -Body $baseline -ContentType "application/json" | ConvertTo-Json -Depth 8
Invoke-RestMethod "$base/state/regression-report?name=smoke-test" | ConvertTo-Json -Depth 8

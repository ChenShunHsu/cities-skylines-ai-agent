param(
    [int]$Keep = 20,
    [int]$KeepPaused = 5,
    [string[]]$Protect = @()
)

$ErrorActionPreference = "Stop"

# Delete all but the newest agent checkpoint and auto-paused saves; the agent writes one before
# every change, so they pile up by the hundred. Only while the game is closed: a running game
# indexes its saves, and files removed under it made every later save rescan the folder and throw
# for each missing one until its thread stopped answering. Saves the player named are never touched.
# During play the loop deletes a few of the oldest checkpoints each wake instead (loop/protocol.py
# prune_checkpoints), spaced so the game's file watcher sees every deletion; this clears the rest.
if (Get-Process Cities -ErrorAction SilentlyContinue) {
    throw "Cities.exe is running; prune agent saves only while the game is closed."
}
$saves = Join-Path $env:LOCALAPPDATA "Colossal Order\Cities_Skylines\Saves"
$protected = @($Protect | ForEach-Object { [IO.Path]::GetFileNameWithoutExtension($_) })
$continue = Join-Path $env:LOCALAPPDATA "Colossal Order\Cities_Skylines\continue_game.json"
if (Test-Path -LiteralPath $continue) {
    try { $protected += (Get-Content -LiteralPath $continue -Raw | ConvertFrom-Json).title } catch {}
}

$removed = 0
$freed = 0
foreach ($rule in @(@{ Filter = "AgentCheckpoint-*.crp"; Keep = $Keep },
                    @{ Filter = "AgentAutoPaused-*.crp"; Keep = $KeepPaused })) {
    $old = @(Get-ChildItem -Path $saves -Filter $rule.Filter -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -Skip $rule.Keep |
        Where-Object { $protected -notcontains $_.BaseName })
    foreach ($file in $old) {
        $freed += $file.Length
        Remove-Item -LiteralPath $file.FullName -Force
        $removed++
    }
}

# Older runs moved checkpoints here instead of deleting them; nothing reads them back.
$archive = Join-Path (Split-Path -Parent $PSScriptRoot) ".runtime\checkpoint-archive"
if (Test-Path -LiteralPath $archive) {
    $freed += (Get-ChildItem -LiteralPath $archive -Recurse -File | Measure-Object Length -Sum).Sum
    Remove-Item -LiteralPath $archive -Recurse -Force
}
if ($freed -gt 0) {
    Write-Host ("Pruned {0} agent saves, freed {1:N0} MB" -f $removed, ($freed / 1MB))
}

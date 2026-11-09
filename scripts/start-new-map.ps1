param(
    [int]$ApiPort = 32123,
    [string]$SteamAppId = "255710",
    [int]$LauncherTimeoutSeconds = 90,
    [int]$CitiesWindowTimeoutSeconds = 75,
    [int]$GameLoadTimeoutSeconds = 300,
    [ValidateRange(-1, 200)]
    [int]$MapSelectionOffset = -1,
    [switch]$SkipKill,
    [switch]$SkipBuild,
    [switch]$SkipNewMap
)

$ErrorActionPreference = "Stop"

function Write-Step($message) {
    Write-Host "[$(Get-Date -Format HH:mm:ss)] $message"
}

function Ensure-Win32Input {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

public struct SAB_RECT {
    public int Left;
    public int Top;
    public int Right;
    public int Bottom;
}

public static class SAB_Win32Input {
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out SAB_RECT lpRect);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool SetCursorPos(int X, int Y);
    [DllImport("user32.dll")] public static extern void mouse_event(uint dwFlags, uint dx, uint dy, uint dwData, UIntPtr dwExtraInfo);
}
'@ -ErrorAction SilentlyContinue
}

function Click-Screen([int]$x, [int]$y) {
    [SAB_Win32Input]::SetCursorPos($x, $y) | Out-Null
    Start-Sleep -Milliseconds 120
    [SAB_Win32Input]::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero)
    Start-Sleep -Milliseconds 90
    [SAB_Win32Input]::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero)
}

function Click-Relative($process, [double]$rx, [double]$ry) {
    $rect = New-Object SAB_RECT
    [SAB_Win32Input]::GetWindowRect($process.MainWindowHandle, [ref]$rect) | Out-Null
    [SAB_Win32Input]::SetForegroundWindow($process.MainWindowHandle) | Out-Null
    Start-Sleep -Milliseconds 400
    $x = $rect.Left + [int](($rect.Right - $rect.Left) * $rx)
    $y = $rect.Top + [int](($rect.Bottom - $rect.Top) * $ry)
    Click-Screen $x $y
}

function Wait-ProcessWindow([string]$name, [string]$title, [int]$timeoutSeconds) {
    $deadline = (Get-Date).AddSeconds($timeoutSeconds)
    do {
        $process = Get-Process -ErrorAction SilentlyContinue |
            Where-Object { $_.ProcessName -eq $name -and $_.MainWindowHandle -ne 0 -and ($title -eq "" -or $_.MainWindowTitle -eq $title) } |
            Select-Object -First 1

        if ($process) {
            return $process
        }

        Start-Sleep -Seconds 1
    } while ((Get-Date) -lt $deadline)

    $visibleCandidates = Get-Process -ErrorAction SilentlyContinue |
        Where-Object { $_.ProcessName -eq $name -and $_.MainWindowHandle -ne 0 } |
        ForEach-Object { "id=$($_.Id) title='$($_.MainWindowTitle)'" }
    $candidateSummary = if ($visibleCandidates) { $visibleCandidates -join "; " } else { "none" }
    throw "Timed out waiting for window: process=$name title=$title. Visible candidates: $candidateSummary"
}

function Test-ApiPort([int]$port) {
    $client = New-Object Net.Sockets.TcpClient
    try {
        $iar = $client.BeginConnect("127.0.0.1", $port, $null, $null)
        if ($iar.AsyncWaitHandle.WaitOne(500, $false)) {
            $client.EndConnect($iar)
            return $true
        }
        return $false
    }
    catch {
        return $false
    }
    finally {
        $client.Close()
    }
}

function Wait-AgentBridge([int]$port, [int]$timeoutSeconds) {
    $deadline = (Get-Date).AddSeconds($timeoutSeconds)
    do {
        if (Test-ApiPort $port) {
            try {
                return Invoke-RestMethod "http://127.0.0.1:$port/health"
            }
            catch {
                Start-Sleep -Seconds 1
            }
        }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)

    throw "Timed out waiting for Skylines Agent Bridge on port $port"
}

Ensure-Win32Input

$repo = Split-Path -Parent $PSScriptRoot

# Compile before stopping the game: a compile error then leaves the running
# city untouched instead of relaunching it on the previous Bridge build.
if (-not $SkipBuild) {
    Write-Step "Compiling SkylinesAgentBridge.dll"
    & (Join-Path $PSScriptRoot "build.ps1") -NoInstall
    if ($LASTEXITCODE -ne 0) { throw "Bridge build failed (exit $LASTEXITCODE); the running game was left untouched." }
}

if (-not $SkipKill) {
    Write-Step "Stopping existing Cities.exe"
    Get-Process Cities -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 3
    & (Join-Path $PSScriptRoot "prune-agent-saves.ps1")
}

if (-not $SkipBuild) {
    Write-Step "Installing SkylinesAgentBridge.dll"
    & (Join-Path $PSScriptRoot "build.ps1")
    if ($LASTEXITCODE -ne 0) { throw "Bridge install failed (exit $LASTEXITCODE)." }
}

Write-Step "Launching Cities: Skylines through Steam"
Start-Process "steam://rungameid/$SteamAppId"

Write-Step "Waiting for Paradox Launcher"
$launcher = Wait-ProcessWindow "Paradox Launcher" "" $LauncherTimeoutSeconds
Write-Step "Using launcher window: $($launcher.MainWindowTitle)"

Write-Step "Clicking launcher Play button"
Click-Relative $launcher 0.67 0.39

$cities = $null
for ($attempt = 1; $attempt -le 3 -and -not $cities; $attempt++) {
    Write-Step "Waiting for Cities.exe (attempt $attempt/3)"
    try {
        $cities = Wait-ProcessWindow "Cities" "Cities: Skylines" $CitiesWindowTimeoutSeconds
    }
    catch {
        Write-Step "Cities.exe did not appear yet; clicking launcher Play again"
        $launcher = Wait-ProcessWindow "Paradox Launcher" "" $LauncherTimeoutSeconds
        Write-Step "Using launcher window: $($launcher.MainWindowTitle)"
        Click-Relative $launcher 0.67 0.39
    }
}
if (-not $cities) {
    throw "Timed out waiting for Cities.exe"
}
Start-Sleep -Seconds 30

if (-not $SkipNewMap) {
    Write-Step "Dismissing startup modals and opening New Game"
    [SAB_Win32Input]::SetForegroundWindow($cities.MainWindowHandle) | Out-Null
    Start-Sleep -Milliseconds 500

    # Scale the known 2560x1440 reference points to the actual Cities window.
    # Absolute screen coordinates silently miss when the game resolution,
    # window position, or display scaling changes.
    # These close the "new features" modal, close the old-mod warning if
    # present, open New Game, and start the currently selected map.
    Click-Relative $cities 0.5 0.7764
    Start-Sleep -Seconds 2
    Click-Relative $cities 0.6527 0.3375
    Start-Sleep -Seconds 2
    Click-Relative $cities 0.5 0.3667
    Start-Sleep -Seconds 5

    if ($MapSelectionOffset -ge 0) {
        Write-Step "Selecting map by runtime list offset: $MapSelectionOffset"
        Add-Type -AssemblyName System.Windows.Forms
        # The first map row; 0.2111 is the "Sort by" dropdown above the list,
        # which swallowed the keys so the default map always started.
        Click-Relative $cities 0.2813 0.2667
        [System.Windows.Forms.SendKeys]::SendWait("{HOME}")
        Start-Sleep -Milliseconds 300
        for ($step = 0; $step -lt $MapSelectionOffset; $step++) {
            [System.Windows.Forms.SendKeys]::SendWait("{DOWN}")
            Start-Sleep -Milliseconds 100
        }
        Start-Sleep -Milliseconds 500
    }

    Click-Relative $cities 0.6086 0.8285
}

Write-Step "Waiting for Agent Bridge API"
$health = Wait-AgentBridge $ApiPort $GameLoadTimeoutSeconds
$deadline = (Get-Date).AddSeconds($GameLoadTimeoutSeconds)
while (-not $health.levelLoaded -and (Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 3
    $health = Invoke-RestMethod "http://127.0.0.1:$ApiPort/health"
}
if (-not $health.levelLoaded) {
    throw "The Bridge answered but no city finished loading within $GameLoadTimeoutSeconds seconds."
}

if (-not $SkipNewMap) {
    # A click that misses the menu can resume the last save instead; a new map starts empty.
    $snapshot = Invoke-RestMethod "http://127.0.0.1:$ApiPort/state/mayor-snapshot"
    if ($snapshot.cityHealth.population -gt 0) {
        throw "An existing city was loaded instead of a new map (population $($snapshot.cityHealth.population))."
    }
    Write-Step "New city on map: $($snapshot.city.map)"
}

Write-Step "Ready"
$health | ConvertTo-Json -Depth 8

param(
    [int]$ApiPort = 32123,
    [string]$SteamAppId = "255710",
    [int]$LauncherTimeoutSeconds = 90,
    [int]$GameLoadTimeoutSeconds = 300,
    [string]$ExpectedSaveName = "",
    [string]$ExpectedCityName = "",
    [switch]$SkipKill,
    [switch]$SkipBuild,
    [switch]$PreserveAutoSave
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

public static class SAB_ResumeWin32Input {
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out SAB_RECT lpRect);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool SetCursorPos(int X, int Y);
    [DllImport("user32.dll")] public static extern void mouse_event(uint dwFlags, uint dx, uint dy, uint dwData, UIntPtr dwExtraInfo);
}
'@ -ErrorAction SilentlyContinue
}

function Click-Screen([int]$x, [int]$y) {
    [SAB_ResumeWin32Input]::SetCursorPos($x, $y) | Out-Null
    Start-Sleep -Milliseconds 120
    [SAB_ResumeWin32Input]::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero)
    Start-Sleep -Milliseconds 90
    [SAB_ResumeWin32Input]::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero)
}

function Click-Relative($process, [double]$rx, [double]$ry) {
    $rect = New-Object SAB_RECT
    [SAB_ResumeWin32Input]::GetWindowRect($process.MainWindowHandle, [ref]$rect) | Out-Null
    [SAB_ResumeWin32Input]::SetForegroundWindow($process.MainWindowHandle) | Out-Null
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

function Disable-InGameAutoSave([int]$port) {
    try {
        $settings = Invoke-RestMethod "http://127.0.0.1:$port/state/game-settings"
        if ($settings.autoSave -eq $true) {
            Write-Step "Disabling in-game autosave to avoid AutoSave.crp sharing violations"
            $body = @{ enabled = $false } | ConvertTo-Json
            Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:$port/commands/set-autosave" -Body $body -ContentType "application/json" | Out-Null
        }
        else {
            Write-Step "In-game autosave is already disabled"
        }
    }
    catch {
        Write-Step "Could not verify autosave setting through the bridge: $($_.Exception.Message)"
    }
}

Ensure-Win32Input

$saveDir = Join-Path $env:LOCALAPPDATA "Colossal Order\Cities_Skylines\Saves"
$latestSave = Get-ChildItem -LiteralPath $saveDir -Filter *.crp -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1
if ($latestSave) {
    Write-Step "Latest local save before Resume: $($latestSave.Name) $($latestSave.LastWriteTime)"
}

if (-not [string]::IsNullOrWhiteSpace($ExpectedSaveName)) {
    $expectedFileName = $ExpectedSaveName
    if (-not $expectedFileName.EndsWith(".crp", [StringComparison]::OrdinalIgnoreCase)) {
        $expectedFileName += ".crp"
    }

    $expectedSave = Get-ChildItem -LiteralPath $saveDir -Filter $expectedFileName -ErrorAction SilentlyContinue |
        Where-Object { $_.Name.Equals($expectedFileName, [StringComparison]::OrdinalIgnoreCase) } |
        Select-Object -First 1
    if (-not $expectedSave) {
        throw "Expected save '$expectedFileName' was not found in '$saveDir'."
    }
    if (-not $latestSave -or -not $latestSave.FullName.Equals($expectedSave.FullName, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Expected save '$expectedFileName' is not the latest local save. Select it once through the in-game Load Game menu, then run this script again. The script will not fall back to Cities.exe --loadSave because Steam requires a manual custom-launch confirmation."
    }

    Write-Step "Expected Resume save verified: $expectedFileName"
}

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
    Get-Process Cities -ErrorAction SilentlyContinue | Wait-Process -Timeout 30 -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 3
    & (Join-Path $PSScriptRoot "prune-agent-saves.ps1") -Protect $ExpectedSaveName
}

# Install only after the game has exited: build.ps1 refuses to replace the
# Bridge while Cities.exe is running, which previously aborted every rebuild.
if (-not $SkipBuild) {
    Write-Step "Installing SkylinesAgentBridge.dll"
    & (Join-Path $PSScriptRoot "build.ps1")
    if ($LASTEXITCODE -ne 0) { throw "Bridge install failed (exit $LASTEXITCODE)." }
}

# Keep this launch argument-free. Passing --loadSave (either directly to
# Cities.exe or through Steam) triggers Steam's custom-launch confirmation and
# makes unattended operation impossible.
Write-Step "Launching Cities: Skylines through Steam without custom arguments"
Start-Process "steam://rungameid/$SteamAppId"

Write-Step "Waiting for Paradox Launcher"
$launcher = Wait-ProcessWindow "Paradox Launcher" "" $LauncherTimeoutSeconds
Write-Step "Using launcher window: $($launcher.MainWindowTitle)"

Write-Step "Clicking launcher Resume button"
Click-Relative $launcher 0.53 0.39

$cities = $null
for ($attempt = 1; $attempt -le 3 -and -not $cities; $attempt++) {
    Write-Step "Waiting for Cities.exe (attempt $attempt/3)"
    try {
        $cities = Wait-ProcessWindow "Cities" "Cities: Skylines" 45
    }
    catch {
        Write-Step "Cities.exe did not appear yet; clicking launcher Resume again"
        $launcher = Wait-ProcessWindow "Paradox Launcher" "" $LauncherTimeoutSeconds
        Write-Step "Using launcher window: $($launcher.MainWindowTitle)"
        Click-Relative $launcher 0.53 0.39
    }
}
if (-not $cities) {
    throw "Timed out waiting for Cities.exe"
}

Write-Step "Waiting for Agent Bridge API"
$health = Wait-AgentBridge $ApiPort $GameLoadTimeoutSeconds
if (-not $PreserveAutoSave) {
    Disable-InGameAutoSave $ApiPort
}

$deadline = (Get-Date).AddSeconds($GameLoadTimeoutSeconds)
while (-not $health.levelLoaded -and (Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 3
    $health = Invoke-RestMethod "http://127.0.0.1:$ApiPort/health"
}
if (-not $health.levelLoaded) {
    throw "The Bridge answered but no city finished loading within $GameLoadTimeoutSeconds seconds."
}

# Resume continues the save the game itself last saved or loaded, which the file times checked
# above do not always reflect, so the city that actually came up is reported and checked.
$city = (Invoke-RestMethod "http://127.0.0.1:$ApiPort/state/mayor-snapshot").city
Write-Step "Loaded city: $($city.name) on map $($city.map)"
if (-not [string]::IsNullOrWhiteSpace($ExpectedCityName) -and $city.name -ne $ExpectedCityName) {
    throw "Resume loaded the city '$($city.name)' instead of '$ExpectedCityName'. Load the expected save once through the in-game Load Game menu, then run this script again."
}

Write-Step "Ready"
$health | ConvertTo-Json -Depth 8

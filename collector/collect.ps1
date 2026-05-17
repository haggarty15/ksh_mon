<#
.SYNOPSIS
    Vanguard System Monitor - Data Collector
    Collects Vanguard-related events from Windows Event Log, network connections,
    system errors, SecuROM shell extension status, and Explorer crashes.

.DESCRIPTION
    Run via Windows Task Scheduler (daily + on-demand).
    Writes structured JSON to the path specified in config.json.
    Optionally stores data in the SQLite database via the FastAPI backend.

.PARAMETER ConfigPath
    Path to config.json. Defaults to the sibling config.json in the repo root.

.PARAMETER ApiBaseUrl
    Base URL of the running FastAPI backend. If provided, events are POSTed to it.
    Example: https://vanguard.yourdomain.com

.PARAMETER ApiKey
    API key to include as the x-api-key header when calling the backend.
    Required when VANGUARD_API_KEY (or api_key in config.json) is set on the server.

.PARAMETER PollMode
    When set, the script runs as a long-lived poller instead of a one-shot
    collector.  It checks GET /api/trigger/pending every 30 seconds and runs a
    full collection whenever the flag is set (i.e. when a remote trigger — e.g.
    a Google Home voice action via IFTTT — has been received by the cloud server).
    Requires -ApiBaseUrl and -ApiKey to be set.
#>

param(
    [string]$ConfigPath = "$PSScriptRoot\..\config.json",
    [string]$ApiBaseUrl = "",
    [string]$ApiKey     = "",
    [switch]$PollMode
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

function Write-Log {
    param([string]$Message, [string]$Level = "INFO")
    $ts = (Get-Date -Format "yyyy-MM-dd HH:mm:ss")
    Write-Host "[$ts][$Level] $Message"
}

function Get-Config {
    param([string]$Path)
    if (-not (Test-Path $Path)) {
        throw "Config file not found: $Path"
    }
    return (Get-Content $Path -Raw | ConvertFrom-Json)
}

function Ensure-Dir {
    param([string]$Dir)
    if (-not (Test-Path $Dir)) {
        New-Item -ItemType Directory -Force -Path $Dir | Out-Null
    }
}

# ---------------------------------------------------------------------------
# Collection functions
# ---------------------------------------------------------------------------

function Get-DriverEvents {
    <#
    Collects Event Log entries related to Vanguard driver (vgk/vgc) load/unload.
    Sources: System log, Service Control Manager.
    #>
    param([int]$HoursBack, [string[]]$DriverNames)

    $since = (Get-Date).AddHours(-$HoursBack)
    $events = @()

    $sources = @("System")
    foreach ($src in $sources) {
        try {
            $raw = Get-WinEvent -LogName $src -ErrorAction SilentlyContinue |
                Where-Object { $_.TimeCreated -ge $since } |
                Where-Object {
                    $msg = $_.Message
                    $DriverNames | Where-Object { $msg -like "*$_*" }
                }

            foreach ($ev in $raw) {
                $events += [PSCustomObject]@{
                    event_type  = "driver"
                    timestamp   = $ev.TimeCreated.ToString("o")
                    source      = $ev.ProviderName
                    event_id    = $ev.Id
                    level       = $ev.LevelDisplayName
                    message     = ($ev.Message -replace "`r`n", " " -replace "`n", " ")
                    raw_log     = $src
                }
            }
        } catch {
            Write-Log "Could not query log '$src': $_" "WARN"
        }
    }

    # Also check Service Control Manager for service start/stop
    try {
        $scmEvents = Get-WinEvent -LogName "System" -ErrorAction SilentlyContinue |
            Where-Object {
                $_.TimeCreated -ge $since -and
                $_.ProviderName -eq "Service Control Manager" -and
                ($_.Id -in @(7036, 7045, 7040))
            } |
            Where-Object {
                $msg = $_.Message
                $DriverNames | Where-Object { $msg -like "*$_*" }
            }

        foreach ($ev in $scmEvents) {
            # Deduplicate
            if (-not ($events | Where-Object { $_.timestamp -eq $ev.TimeCreated.ToString("o") -and $_.event_id -eq $ev.Id })) {
                $events += [PSCustomObject]@{
                    event_type  = "driver"
                    timestamp   = $ev.TimeCreated.ToString("o")
                    source      = $ev.ProviderName
                    event_id    = $ev.Id
                    level       = $ev.LevelDisplayName
                    message     = ($ev.Message -replace "`r`n", " " -replace "`n", " ")
                    raw_log     = "System/SCM"
                }
            }
        }
    } catch {
        Write-Log "Could not query SCM events: $_" "WARN"
    }

    Write-Log "Collected $($events.Count) driver events."
    return $events
}

function Get-NetworkConnections {
    <#
    Captures current network connections made by Vanguard-related processes.
    Uses netstat + Get-Process correlation.
    #>
    param([string[]]$ProcessNames)

    $connections = @()

    try {
        # Get all TCP connections with owning PID
        $netstatOutput = netstat -ano -p TCP 2>$null
        $pidToProcess = @{}

        # Build PID -> process name map for monitored processes
        foreach ($pname in $ProcessNames) {
            $procs = Get-Process -Name ($pname -replace "\.exe$", "") -ErrorAction SilentlyContinue
            foreach ($p in $procs) {
                $pidToProcess[$p.Id.ToString()] = $pname
            }
        }

        foreach ($line in $netstatOutput) {
            if ($line -notmatch "^\s+TCP") { continue }
            $parts = $line.Trim() -split "\s+"
            if ($parts.Count -lt 5) { continue }

            $proto        = $parts[0]
            $localAddr    = $parts[1]
            $remoteAddr   = $parts[2]
            $state        = $parts[3]
            $pid          = $parts[4]

            if (-not $pidToProcess.ContainsKey($pid)) { continue }

            $remoteIp   = $remoteAddr -replace ":\d+$", ""
            $remotePort = if ($remoteAddr -match ":(\d+)$") { $matches[1] } else { "" }
            $localIp    = $localAddr -replace ":\d+$", ""
            $localPort  = if ($localAddr -match ":(\d+)$") { $matches[1] } else { "" }

            $connections += [PSCustomObject]@{
                event_type   = "network"
                timestamp    = (Get-Date -Format "o")
                process      = $pidToProcess[$pid]
                pid          = [int]$pid
                local_ip     = $localIp
                local_port   = $localPort
                remote_ip    = $remoteIp
                remote_port  = $remotePort
                state        = $state
                protocol     = $proto
            }
        }
    } catch {
        Write-Log "Error collecting network connections: $_" "WARN"
    }

    Write-Log "Collected $($connections.Count) network connections."
    return $connections
}

function Get-SystemErrors {
    <#
    Collects system error/crash events attributed to vgk or vgc.
    Covers Application log crashes and BSOD/bugcheck events.
    #>
    param([int]$HoursBack, [string[]]$DriverNames)

    $since = (Get-Date).AddHours(-$HoursBack)
    $errors = @()

    $logs = @("Application", "System")
    foreach ($logName in $logs) {
        try {
            $raw = Get-WinEvent -LogName $logName -ErrorAction SilentlyContinue |
                Where-Object {
                    $_.TimeCreated -ge $since -and
                    $_.Level -in @(1, 2)  # Critical=1, Error=2
                } |
                Where-Object {
                    $msg = $_.Message
                    $DriverNames | Where-Object { $msg -like "*$_*" }
                }

            foreach ($ev in $raw) {
                $errors += [PSCustomObject]@{
                    event_type  = "error"
                    timestamp   = $ev.TimeCreated.ToString("o")
                    source      = $ev.ProviderName
                    event_id    = $ev.Id
                    level       = $ev.LevelDisplayName
                    message     = ($ev.Message -replace "`r`n", " " -replace "`n", " ")
                    raw_log     = $logName
                }
            }
        } catch {
            Write-Log "Could not query '$logName' for errors: $_" "WARN"
        }
    }

    # Bugcheck (BSOD) events - Event ID 41 (unexpected shutdown) and 1001 (BugCheck)
    try {
        $bsodEvents = Get-WinEvent -LogName "System" -ErrorAction SilentlyContinue |
            Where-Object {
                $_.TimeCreated -ge $since -and
                $_.Id -in @(41, 1001)
            }
        foreach ($ev in $bsodEvents) {
            $errors += [PSCustomObject]@{
                event_type  = "error"
                timestamp   = $ev.TimeCreated.ToString("o")
                source      = $ev.ProviderName
                event_id    = $ev.Id
                level       = "Critical"
                message     = ("BSOD/Bugcheck: " + ($ev.Message -replace "`r`n", " " -replace "`n", " "))
                raw_log     = "System"
            }
        }
    } catch {
        Write-Log "Could not query BSOD events: $_" "WARN"
    }

    Write-Log "Collected $($errors.Count) system error events."
    return $errors
}

function Get-ShellExtensionStatus {
    <#
    Checks whether the SecuROM shell extension (cmdlineext_x64.dll) is registered
    and enabled in the Windows Shell.
    Returns a single status event.
    #>
    param([string]$DllName)

    $timestamp = (Get-Date -Format "o")
    $status = "unknown"
    $details = ""
    $registered = $false

    # Check approved shell extensions in registry
    $approvedPaths = @(
        "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Shell Extensions\Approved",
        "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Shell Extensions\Approved"
    )
    foreach ($path in $approvedPaths) {
        try {
            $props = Get-ItemProperty -Path $path -ErrorAction SilentlyContinue
            if ($props) {
                $props.PSObject.Properties | Where-Object { $_.Value -like "*$DllName*" } | ForEach-Object {
                    $registered = $true
                    $details += "Approved: $($_.Name) = $($_.Value); "
                }
            }
        } catch {}
    }

    # Check COM InprocServer32 registrations
    try {
        $clsidPaths = Get-ChildItem "HKLM:\SOFTWARE\Classes\CLSID" -ErrorAction SilentlyContinue |
            Where-Object {
                $inproc = "$($_.PSPath)\InprocServer32"
                if (Test-Path $inproc) {
                    $val = (Get-ItemProperty -Path $inproc -ErrorAction SilentlyContinue)."(default)"
                    $val -like "*$DllName*"
                }
            }
        foreach ($clsid in $clsidPaths) {
            $registered = $true
            $details += "CLSID: $($clsid.PSChildName); "
        }
    } catch {
        Write-Log "Registry scan for $DllName failed: $_" "WARN"
    }

    # Check if the DLL file actually exists on disk
    $dllExists = $false
    $searchPaths = @(
        "$env:SystemRoot\System32\$DllName",
        "$env:SystemRoot\SysWOW64\$DllName",
        "$env:ProgramFiles\SecuROM\$DllName",
        "$env:ProgramFiles(x86)\SecuROM\$DllName"
    )
    foreach ($p in $searchPaths) {
        if (Test-Path $p) {
            $dllExists = $true
            $details += "DLL found at: $p; "
            break
        }
    }

    if ($registered -and $dllExists) {
        $status = "active"
    } elseif ($registered -and -not $dllExists) {
        $status = "registered_missing_dll"
    } elseif (-not $registered -and $dllExists) {
        $status = "dll_exists_not_registered"
    } else {
        $status = "not_present"
    }

    $result = [PSCustomObject]@{
        event_type  = "shell_ext"
        timestamp   = $timestamp
        dll_name    = $DllName
        registered  = $registered
        dll_exists  = $dllExists
        status      = $status
        details     = $details.TrimEnd("; ")
    }

    Write-Log "Shell extension '$DllName' status: $status"
    return @($result)
}

function Get-ExplorerCrashes {
    <#
    Collects Explorer.exe crash events from the Application event log.
    Looks for 0xc0000005 (access violation) and Windows Error Reporting entries.
    #>
    param([int]$HoursBack)

    $since = (Get-Date).AddHours(-$HoursBack)
    $crashes = @()

    try {
        # Application Error events (Event ID 1000) for explorer.exe
        $appErrors = Get-WinEvent -LogName "Application" -ErrorAction SilentlyContinue |
            Where-Object {
                $_.TimeCreated -ge $since -and
                $_.Id -eq 1000 -and
                $_.Message -like "*explorer.exe*"
            }

        foreach ($ev in $appErrors) {
            $isAccessViolation = $ev.Message -like "*0xc0000005*"
            $crashes += [PSCustomObject]@{
                event_type       = "crash"
                timestamp        = $ev.TimeCreated.ToString("o")
                source           = $ev.ProviderName
                event_id         = $ev.Id
                level            = $ev.LevelDisplayName
                process          = "explorer.exe"
                exception_code   = if ($isAccessViolation) { "0xc0000005" } else { "unknown" }
                access_violation = $isAccessViolation
                message          = ($ev.Message -replace "`r`n", " " -replace "`n", " ")
                raw_log          = "Application"
            }
        }
    } catch {
        Write-Log "Could not query Application log for Explorer crashes: $_" "WARN"
    }

    # Windows Error Reporting - Event ID 1001
    try {
        $werEvents = Get-WinEvent -LogName "Application" -ErrorAction SilentlyContinue |
            Where-Object {
                $_.TimeCreated -ge $since -and
                $_.Id -eq 1001 -and
                $_.Message -like "*explorer*"
            }

        foreach ($ev in $werEvents) {
            $crashes += [PSCustomObject]@{
                event_type       = "crash"
                timestamp        = $ev.TimeCreated.ToString("o")
                source           = $ev.ProviderName
                event_id         = $ev.Id
                level            = $ev.LevelDisplayName
                process          = "explorer.exe"
                exception_code   = "WER"
                access_violation = ($ev.Message -like "*0xc0000005*")
                message          = ($ev.Message -replace "`r`n", " " -replace "`n", " ")
                raw_log          = "Application/WER"
            }
        }
    } catch {
        Write-Log "Could not query WER events: $_" "WARN"
    }

    Write-Log "Collected $($crashes.Count) Explorer crash events."
    return $crashes
}

# ---------------------------------------------------------------------------
# Poll mode — long-lived loop for remote-trigger support
# ---------------------------------------------------------------------------

if ($PollMode) {
    if ($ApiBaseUrl -eq "") {
        Write-Log "-PollMode requires -ApiBaseUrl to be set." "ERROR"
        exit 1
    }

    $pollHeaders = @{}
    if ($ApiKey -ne "") {
        $pollHeaders["x-api-key"] = $ApiKey
    }

    Write-Log "=== Poll mode started. Checking $ApiBaseUrl/api/trigger/pending every 30 s ==="

    while ($true) {
        try {
            $pendingUri  = "$ApiBaseUrl/api/trigger/pending"
            $pendingResp = Invoke-RestMethod -Uri $pendingUri -Method GET -Headers $pollHeaders -ErrorAction Stop
            if ($pendingResp.pending) {
                Write-Log "Remote trigger received — running collection now..."
                # Re-invoke this script in one-shot mode, forwarding all parameters
                # except -PollMode so the same ConfigPath/ApiBaseUrl/ApiKey are used.
                $forwardParams = @{}
                foreach ($key in $PSBoundParameters.Keys) {
                    if ($key -ne "PollMode") {
                        $forwardParams[$key] = $PSBoundParameters[$key]
                    }
                }
                & $PSCommandPath @forwardParams
            }
        } catch {
            Write-Log "Failed to check trigger/pending: $_" "WARN"
        }
        Start-Sleep -Seconds 30
    }

    # unreachable, but satisfies strict mode
    exit 0
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

Write-Log "=== Vanguard System Monitor - Collector starting ==="

$config = Get-Config -Path $ConfigPath
$dbDir  = Split-Path -Parent (Join-Path $PSScriptRoot ".." $config.db_path)
Ensure-Dir -Dir (Join-Path $PSScriptRoot ".." (Split-Path -Parent $config.collector.output_json_path))

$hoursBack   = $config.collector.event_log_hours_back
$processes   = $config.collector.processes_to_monitor
$drivers     = $config.collector.driver_names
$shellExt    = $config.collector.shell_ext_dll
$outputPath  = Join-Path $PSScriptRoot ".." $config.collector.output_json_path

Write-Log "Collecting last $hoursBack hours of events..."

$allEvents = @()
$allEvents += Get-DriverEvents      -HoursBack $hoursBack -DriverNames $drivers
$allEvents += Get-NetworkConnections -ProcessNames $processes
$allEvents += Get-SystemErrors      -HoursBack $hoursBack -DriverNames $drivers
$allEvents += Get-ShellExtensionStatus -DllName $shellExt
$allEvents += Get-ExplorerCrashes   -HoursBack $hoursBack

$payload = [PSCustomObject]@{
    collected_at  = (Get-Date -Format "o")
    event_count   = $allEvents.Count
    events        = $allEvents
}

$json = $payload | ConvertTo-Json -Depth 10
$json | Set-Content -Path $outputPath -Encoding UTF8

Write-Log "Wrote $($allEvents.Count) events to $outputPath"

# If an API base URL is provided, POST the events to the ingest endpoint
if ($ApiBaseUrl -ne "") {
    try {
        $uri = "$ApiBaseUrl/api/ingest"
        Write-Log "POSTing events to $uri ..."
        $headers = @{ "Content-Type" = "application/json" }
        if ($ApiKey -ne "") {
            $headers["x-api-key"] = $ApiKey
        }
        $response = Invoke-RestMethod -Uri $uri -Method POST -Body $json -Headers $headers
        Write-Log "Ingest response: $($response | ConvertTo-Json -Compress)"
    } catch {
        Write-Log "Failed to POST events to API: $_" "WARN"
    }
}

Write-Log "=== Collection complete. Total events: $($allEvents.Count) ==="
exit 0

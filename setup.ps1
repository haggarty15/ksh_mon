<#
.SYNOPSIS
    Vanguard System Monitor – Setup Script
    Sets up the Python virtual environment, installs dependencies, and registers
    the Windows Task Scheduler task.

.DESCRIPTION
    Run once from a PowerShell Administrator prompt:
        .\setup.ps1
#>

param(
    [switch]$SkipScheduler,
    [string]$PythonExe = "python"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = $PSScriptRoot

function Write-Step { param([string]$Msg) Write-Host "`n==> $Msg" -ForegroundColor Cyan }
function Write-OK   { param([string]$Msg) Write-Host "    [OK] $Msg" -ForegroundColor Green }
function Write-Warn { param([string]$Msg) Write-Host "    [!!] $Msg" -ForegroundColor Yellow }

# ---------------------------------------------------------------------------
# 1. Create data directory
# ---------------------------------------------------------------------------
Write-Step "Creating data directory..."
$dataDir = Join-Path $RepoRoot "data"
if (-not (Test-Path $dataDir)) {
    New-Item -ItemType Directory -Path $dataDir | Out-Null
}
Write-OK "data/ exists"

# ---------------------------------------------------------------------------
# 2. Python virtual environment
# ---------------------------------------------------------------------------
Write-Step "Setting up Python virtual environment..."
$venvDir = Join-Path $RepoRoot ".venv"
if (-not (Test-Path $venvDir)) {
    & $PythonExe -m venv $venvDir
    Write-OK "Created .venv"
} else {
    Write-OK ".venv already exists"
}

$pip = Join-Path $venvDir "Scripts\pip.exe"
$py  = Join-Path $venvDir "Scripts\python.exe"

Write-Step "Installing Python dependencies..."
& $pip install --upgrade pip --quiet
& $pip install -r (Join-Path $RepoRoot "backend\requirements.txt") --quiet
Write-OK "Dependencies installed"

# ---------------------------------------------------------------------------
# 3. Initialise database
# ---------------------------------------------------------------------------
Write-Step "Initialising SQLite database..."
$initScript = @"
import asyncio, sys
sys.path.insert(0, r'$RepoRoot')
from backend.database import init_db
asyncio.run(init_db())
print('DB initialised')
"@
& $py -c $initScript
Write-OK "Database ready"

# ---------------------------------------------------------------------------
# 4. Windows Task Scheduler task
# ---------------------------------------------------------------------------
if (-not $SkipScheduler) {
    Write-Step "Registering Windows Task Scheduler task..."
    $taskXml = Join-Path $RepoRoot "scheduler\vanguard_monitor_task.xml"
    if (Test-Path $taskXml) {
        try {
            # Substitute the repo root path in the XML
            $xml = Get-Content $taskXml -Raw
            $xml = $xml -replace "REPO_ROOT_PLACEHOLDER", $RepoRoot
            $tmpXml = Join-Path $env:TEMP "vanguard_task_tmp.xml"
            $xml | Set-Content $tmpXml -Encoding UTF8

            schtasks /Create /TN "VanguardMonitor\DailyCollect" /XML $tmpXml /F 2>&1 | Out-Null
            Write-OK "Task registered: VanguardMonitor\DailyCollect"
            Remove-Item $tmpXml -ErrorAction SilentlyContinue
        } catch {
            Write-Warn "Could not register task (need Administrator): $_"
        }
    } else {
        Write-Warn "Task XML not found at $taskXml — skipping scheduler registration"
    }
}

# ---------------------------------------------------------------------------
# 5. Print launch instructions
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "================================================================" -ForegroundColor White
Write-Host "  Setup complete!" -ForegroundColor Green
Write-Host ""
Write-Host "  Start the monitor server:" -ForegroundColor White
Write-Host "    .venv\Scripts\python -m uvicorn backend.main:app --reload" -ForegroundColor Yellow
Write-Host ""
Write-Host "  Then open:  http://127.0.0.1:8000" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Run collector manually:" -ForegroundColor White
Write-Host "    powershell -File collector\collect.ps1 -ApiBaseUrl http://127.0.0.1:8000" -ForegroundColor Yellow
Write-Host "================================================================" -ForegroundColor White

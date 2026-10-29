# McTrader_Logging — run and operate
#
#   .\run.ps1            start the service on 127.0.0.1:8004
#   .\run.ps1 -Schema    create dbo.LogEntry / dbo.LogEvent and report counts
#   .\run.ps1 -Test      run the test suite (no database, no network)
#   .\run.ps1 -Demo      write a few lines, then read them back by correlation id
#   .\run.ps1 -Retention prune rows older than the retention window
#
# Use run.cmd instead of run.ps1 when scheduling: Task Scheduler runs under the machine
# execution policy (Restricted here), which refuses .ps1 files outright.

param(
    [switch]$Schema,
    [switch]$Test,
    [switch]$Demo,
    [switch]$Retention,
    [switch]$Apply,
    [int]$Months = 3
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $root '.venv\Scripts\python.exe'

if (-not (Test-Path $python)) {
    Write-Host "No virtualenv at $python" -ForegroundColor Red
    Write-Host "  python -m venv .venv"
    Write-Host "  .\.venv\Scripts\python.exe -m pip install -r requirements.txt"
    exit 1
}

Push-Location $root
try {
    if ($Test) {
        & $python -m pytest tests -q
        exit $LASTEXITCODE
    }
    if ($Schema) {
        & $python tools\apply_schema.py
        exit $LASTEXITCODE
    }
    if ($Demo) {
        & $python tools\demo_roundtrip.py
        exit $LASTEXITCODE
    }
    if ($Retention) {
        # Dry run unless -Apply: this is the one operation here that destroys data.
        $arguments = @('-m', 'jobs.retention', '--months', $Months)
        if ($Apply) { $arguments += '--apply' }
        & $python @arguments
        exit $LASTEXITCODE
    }

    Write-Host "McTrader_Logging on http://127.0.0.1:8004  (docs at /docs, health at /healthz)" -ForegroundColor Cyan
    & $python -m mctrader_logging
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}

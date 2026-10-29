<#
.SYNOPSIS
    Start McTrader_Logging if it is not already running. Idempotent.

.DESCRIPTION
    Run by a scheduled task at boot, and by hand to restart the service. Modelled on PaperTrader's EnsurePaperTrader.ps1, whose header
    records why a boot-only trigger is not enough -- a reboot left the trader down for 43 minutes
    before the US close.

    A logging service has a subtler version of that failure: it is not on the trading path, so nobody
    notices it is down until they go looking for a record of an incident that is now unrecoverable.

    READINESS IS JUDGED BY PORT, NOT BY PROCESS NAME

    Two python processes could both be `-m ...service` for different projects, and a name match cannot
    tell them apart. Port 8004 is unambiguous, and it also catches a wedged process that exists but
    holds no listener.

    IT WILL NOT THRASH

    Starts are recorded and capped per hour, so a service crashing on startup leaves a visible record
    instead of an invisible relaunch loop.

.NOTES
    Exit codes: 0 = running (started now or already), 1 = refused (thrash guard) or failed to start.
#>
param(
    [int]$Port = 8004,
    [int]$WaitSeconds = 45,
    [int]$MaxStartsPerHour = 30,
    [switch]$Stop,
    [switch]$Restart
)

$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root '.venv\Scripts\python.exe'
$Module = 'mctrader_logging.service'
$Logs = Join-Path $Root 'logs'
$Stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$Log = Join-Path $Logs "ensure-$Stamp.log"
$History = Join-Path $Logs 'ensure-history.txt'

New-Item -ItemType Directory -Force -Path $Logs | Out-Null

function Note([string]$Message) {
    $line = "{0}  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    Add-Content -Path $Log -Value $line
    Add-Content -Path $History -Value $line
    Write-Output $line
}

function Test-Port([int]$Number) {
    $hit = Get-NetTCPConnection -LocalPort $Number -State Listen -ErrorAction SilentlyContinue
    return [bool]$hit
}

function Get-RecentStarts {
    if (-not (Test-Path $History)) { return 0 }
    $cutoff = (Get-Date).AddHours(-1)
    $count = 0
    foreach ($line in (Get-Content $History -Tail 400 -ErrorAction SilentlyContinue)) {
        if ($line -notmatch '^\S+ \S+\s+START') { continue }
        $stampText = $line.Substring(0, 19)
        $when = [datetime]::MinValue
        if ([datetime]::TryParseExact($stampText, 'yyyy-MM-dd HH:mm:ss', $null,
                                      [Globalization.DateTimeStyles]::None, [ref]$when)) {
            if ($when -ge $cutoff) { $count++ }
        }
    }
    return $count
}

Note "ensure: port=$Port pid=$PID"

# Checked BEFORE anything is stopped, so a refusal leaves a running service alone.
#
# This ordering is not cosmetic. On 2026-10-29 the check came after the stop: the launcher killed the
# service, then refused to start a replacement because the hourly cap was reached, and exited 1. A
# safety feature left the service dead. A refusal must be inert.
$recent = Get-RecentStarts
if (-not $Stop -and $recent -ge $MaxStartsPerHour) {
    Note ("REFUSED: $recent start(s) in the last hour (cap $MaxStartsPerHour). " +
          "Something is crashing on startup -- read the service log, then re-run. " +
          "NOTHING WAS STOPPED: the running service is untouched.")
    exit 1
}

# Stop whatever holds the port before starting, unconditionally.
#
# This is McTrader's Stop-StaleListener, and it is what makes a bare run of this script a
# RESTART rather than a no-op. It is only safe because the scheduled task is boot-only: with a
# repeating trigger this would restart the service every ten minutes forever.
    $listening = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    if (-not $listening) {
        Note "nothing listening on $Port"
    }
    foreach ($connection in $listening) {
        $processId = $connection.OwningProcess
        Note "STOP pid $processId on port $Port"
        try {
            Stop-Process -Id $processId -Force -ErrorAction Stop
        } catch {
            Note ("FAILED to stop pid $processId : " + $_.Exception.Message)
            Note ("If the task was registered with -RunLevel Highest, stopping it needs an " +
                      "elevated prompt; otherwise re-run this.") 
            exit 1
        }
    }
    for ($attempt = 1; $attempt -le 20; $attempt++) {
        if (-not (Test-Port $Port)) { break }
        Start-Sleep -Seconds 1
    }
    if (Test-Port $Port) {
        Note "FAILED: port $Port is still held after stopping"
        exit 1
    }
    if ($listening) { Note "stopped cleanly" }
    # -Stop ends here. Without it the script carries on and starts the service again.
    if ($Stop) { exit 0 }

if (-not (Test-Path $Python)) {
    Note "FAILED: no interpreter at $Python"
    exit 1
}

Note "START (starts in the last hour: $recent)"

Start-Process -FilePath $Python `
    -ArgumentList @('-m', $Module) `
    -WorkingDirectory $Root `
    -WindowStyle Hidden | Out-Null

for ($second = 1; $second -le $WaitSeconds; $second++) {
    Start-Sleep -Seconds 1
    if (Test-Port $Port) {
        Note "UP after ${second}s on port $Port"
        exit 0
    }
}

Note "FAILED: not listening on $Port after ${WaitSeconds}s. Check $(Join-Path $Logs 'mctrader_logging.log')"
exit 1

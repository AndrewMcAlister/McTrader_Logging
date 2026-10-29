<#
.SYNOPSIS
    Install McTrader_Logging as a real Windows service, managed by the Service Control Manager.

.DESCRIPTION
    Uses NSSM (already installed on this machine) so the Python process runs under the SCM rather
    than as a scheduled task: it starts at boot, appears in services.msc, can be stopped and
    restarted with Start-Service/Stop-Service, and is restarted automatically if it exits badly.

    Must be run ELEVATED. Without elevation it prints the exact command to run instead of failing
    obscurely.

    The service runs as LocalSystem (NT AUTHORITY\SYSTEM) by default. That identity has been
    granted db_datareader + db_datawriter on the `Logging` database, so no password is stored
    anywhere. Use -AsUser to run as an interactive account instead.

.EXAMPLE
    .\install-service.ps1 -Install
    .\install-service.ps1 -Status
    .\install-service.ps1 -Remove
#>
param(
    [switch]$Install,
    [switch]$Remove,
    [switch]$Start,
    [switch]$Stop,
    [switch]$Restart,
    [switch]$Status,
    [switch]$TakeOver,
    [string]$AsUser
)

$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------- constants
$ServiceName = 'McTrader_Logging'
$DisplayName = 'McTrader Logging Service'
$Description = 'One log sink and one correlation id for every McTrader service. Writes dbo.LogEntry and dbo.LogEvent to the Logging database.'

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $Root '.venv\Scripts\python.exe'
$Arguments = '-m mctrader_logging.service'
$LogDir = Join-Path $Root 'logs'

# ---------------------------------------------------------------- helpers
function Test-Elevated {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Resolve-Nssm {
    $command = Get-Command nssm -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    throw "nssm not found on PATH. Install it with: winget install NSSM.NSSM"
}

function Assert-Elevated($action) {
    if (Test-Elevated) { return }
    Write-Host ""
    Write-Host "This action needs elevation." -ForegroundColor Yellow
    Write-Host "Open PowerShell as Administrator and run:" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "    cd `"$Root`"" -ForegroundColor Cyan
    Write-Host "    .\install-service.cmd -$action" -ForegroundColor Cyan
    Write-Host ""
    exit 1
}

function Get-ServiceState {
    $svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if (-not $svc) { return "not installed" }
    return "$($svc.Status) / $($svc.StartType)"
}

# ---------------------------------------------------------------- status
if ($Status -or (-not $Install -and -not $Remove -and -not $Start -and -not $Stop -and -not $Restart)) {
    if (-not $Status) {
        Write-Host ""
        Write-Host "STATUS ONLY -- nothing was installed and nothing changed." -ForegroundColor Yellow
        Write-Host "  to install the service:  .\install-service.ps1 -Install    (needs elevation)" -ForegroundColor Yellow
        Write-Host ""
    }
    Write-Host "service : $ServiceName"
    Write-Host "state   : $(Get-ServiceState)"

    $svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($svc) {
        $nssm = (Get-Command nssm -ErrorAction SilentlyContinue)
        if ($nssm) {
            Write-Host ""
            Write-Host "nssm configuration:"
            & $nssm.Source dump $ServiceName 2>$null | Select-String -Pattern 'Application|AppDirectory|AppParameters|AppExit|Start' | ForEach-Object { "  $_" }
        }
    }
    Write-Host ""
    Write-Host "log file: $(Join-Path $LogDir 'mctrader_logging.log')"
    exit 0
}

if (-not (Test-Path $Python)) {
    throw "No virtualenv at $Python. Create it with: python -m venv .venv"
}

# ---------------------------------------------------------------- install
if ($Install) {
    Assert-Elevated 'Install'
    $nssm = Resolve-Nssm
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

    if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
        Write-Host "Service $ServiceName already exists; removing it first." -ForegroundColor Yellow
        & $nssm stop $ServiceName confirm 2>$null | Out-Null
        & $nssm remove $ServiceName confirm 2>$null | Out-Null
        Start-Sleep -Seconds 2
    }

    Write-Host "installing $ServiceName -> $Python $Arguments"
    & $nssm install $ServiceName $Python $Arguments
    if ($LASTEXITCODE -ne 0) { throw "nssm install failed ($LASTEXITCODE)" }

    & $nssm set $ServiceName DisplayName $DisplayName
    & $nssm set $ServiceName Description $Description
    & $nssm set $ServiceName AppDirectory $Root

    # Auto-start at boot: this is the whole point.
    & $nssm set $ServiceName Start SERVICE_AUTO_START

    # Do not start before SQL Server: the app calls ensure_schema() during startup.
    & $nssm set $ServiceName DependOnService MSSQLSERVER

    # Restart on a bad exit, but not in a tight loop.
    & $nssm set $ServiceName AppExit Default Restart
    & $nssm set $ServiceName AppRestartDelay 5000
    & $nssm set $ServiceName AppThrottle 10000

    # Give in-flight requests time to finish on stop (uvicorn handles Ctrl+C).
    & $nssm set $ServiceName AppStopMethodConsole 15000

    # A crash before file logging is up still leaves a trace.
    & $nssm set $ServiceName AppStdout (Join-Path $LogDir 'nssm-stdout.log')
    & $nssm set $ServiceName AppStderr (Join-Path $LogDir 'nssm-stderr.log')
    & $nssm set $ServiceName AppRotateFiles 1
    & $nssm set $ServiceName AppRotateOnline 1
    & $nssm set $ServiceName AppRotateBytes 10485760

    # LocalSystem by default: no password stored anywhere, and its SQL access is a grant rather
    # than an inherited sysadmin.
    if ($AsUser) {
        $credential = Get-Credential -UserName $AsUser -Message "Password for the service account $AsUser"
        $plain = $credential.GetNetworkCredential().Password
        & $nssm set $ServiceName ObjectName $AsUser $plain
    }

    # ---- take over from the scheduled task ---------------------------------
    if ($TakeOver) {
        $taskName = 'McTrader_Logging_KeepAlive'
        $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        if ($task) {
            # A task and a service must not both run this: they would race for the port, and the
            # loser would appear to start and then die.
            Write-Host "  removing scheduled task $taskName"
            Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        }
        $listening = @(Get-NetTCPConnection -LocalPort 8004 -State Listen -ErrorAction SilentlyContinue)
        foreach ($connection in $listening) {
            Write-Host "  stopping pid $($connection.OwningProcess) holding port 8004"
            Stop-Process -Id $connection.OwningProcess -Force -ErrorAction SilentlyContinue
        }
        Start-Sleep -Seconds 2
        if (Get-NetTCPConnection -LocalPort 8004 -State Listen -ErrorAction SilentlyContinue) {
            Write-Host "  port 8004 is still held; the service will fail to bind" -ForegroundColor Yellow
        }
    }

    Write-Host ""
    Write-Host "installed. starting..." -ForegroundColor Cyan
    & $nssm start $ServiceName
    Start-Sleep -Seconds 3

    # The SCM says "Started" once the process exists, which is not the same as the service working.
    # Ask the service itself.
    $healthy = $null
    for ($attempt = 1; $attempt -le 25; $attempt++) {
        try {
            $healthy = Invoke-RestMethod "http://127.0.0.1:8004/healthz" -TimeoutSec 5 -ErrorAction Stop
            break
        } catch {
            Start-Sleep -Seconds 1
        }
    }

    Write-Host "state: $(Get-ServiceState)"
    if ($healthy) {
        Write-Host "healthz: $($healthy.status)  database=$($healthy.database)" -ForegroundColor Green
    } else {
        Write-Host ""
        Write-Host "The service is registered but /healthz did not answer." -ForegroundColor Yellow
        Write-Host "Most likely causes, in order:" -ForegroundColor Yellow
        Write-Host "  1069  the account lacks 'Log on as a service' -- fix in secpol.msc:" -ForegroundColor Yellow
        Write-Host "        Local Policies -> User Rights Assignment -> Log on as a service" -ForegroundColor Yellow
        Write-Host "  1067  the process exited on start -- read the log below" -ForegroundColor Yellow
        Write-Host "        Get-Content `"$(Join-Path $LogDir 'nssm-stderr.log')`" -Tail 30" -ForegroundColor Yellow
        Write-Host "        Get-Content `"$(Join-Path $LogDir 'nssm-stdout.log')`" -Tail 30" -ForegroundColor Yellow
    }
    Write-Host ""
    Write-Host "check it:" -ForegroundColor Cyan
    Write-Host "  Invoke-RestMethod http://127.0.0.1:8004/healthz"
    Write-Host "  Get-Content `"$(Join-Path $LogDir 'mctrader_logging.log')`" -Tail 20"
    exit 0
}

# ---------------------------------------------------------------- remove
if ($Remove) {
    Assert-Elevated 'Remove'
    $nssm = Resolve-Nssm
    if (-not (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue)) {
        Write-Host "Service $ServiceName is not installed."
        exit 0
    }
    & $nssm stop $ServiceName confirm 2>$null | Out-Null
    Start-Sleep -Seconds 2
    & $nssm remove $ServiceName confirm
    Write-Host "removed $ServiceName"
    exit 0
}

# ---------------------------------------------------------------- start / stop / restart
Assert-Elevated $(if ($Start) { 'Start' } elseif ($Stop) { 'Stop' } else { 'Restart' })

if (-not (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue)) {
    throw "Service $ServiceName is not installed. Run: .\install-service.ps1 -Install"
}

if ($Start)   { Start-Service -Name $ServiceName }
if ($Stop)    { Stop-Service -Name $ServiceName }
if ($Restart) { Restart-Service -Name $ServiceName }

Start-Sleep -Seconds 2
Write-Host "state: $(Get-ServiceState)"

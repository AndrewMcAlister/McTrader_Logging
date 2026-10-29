<#
.SYNOPSIS
    Register McTrader_Logging to start at boot under YOUR account, via Task Scheduler.

.DESCRIPTION
    The same shape as Market_Data_Service's task, and as McTrader_AutoStart_OnBoot: a boot trigger
    plus a repeating check, running as the interactive account with a stored password.

    WHY A REPEATING TRIGGER AS WELL AS BOOT

    PaperTrader's keep-alive header records the reason:

        "on 2026-10-14 the machine rebooted at 05:17 Sydney and the trader was gone 43 minutes
         before the US close... A boot-only trigger would not have restarted it that evening either."

    A logging service has a subtler version of that failure: it is not on the trading path, so nobody
    notices it is down until they go looking for a record of an incident that is now unrecoverable.
    The repeating trigger fires an idempotent launcher that starts it only if nothing holds the port.

    This service needs no vendor key, so `LogonType` could be `S4U`. It is `Password` anyway: the
    account is the same, the setup is identical to its sibling, and an S4U task runs without your
    credential vault -- a difference worth not having to remember.

.EXAMPLE
    .\install-task.ps1 -Install         # prompts for your password once
    .\install-task.ps1 -Status
    .\install-task.ps1 -Run
    .\install-task.ps1 -Remove
#>
param(
    [switch]$Install,
    [switch]$Remove,
    [switch]$Run,
    [switch]$Restart,
    [switch]$Status,
    [int]$IntervalMinutes = 10,
    [string]$User
)

$ErrorActionPreference = 'Stop'

$TaskName = 'McTrader_Logging_KeepAlive'
$Port = 8004
$Root = $PSScriptRoot   # this script lives at the project root
$Ensure = Join-Path $Root 'scripts\Ensure-LoggingService.ps1'
$PowerShell = 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'
$Description = 'One log sink and one correlation id for every McTrader service, on port 8004. Writes dbo.LogEntry and dbo.LogEvent to the Logging database.'

function Test-Elevated {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Assert-Elevated($action) {
    if (Test-Elevated) { return }
    Write-Host ""
    Write-Host "Registering a task that stores a password needs elevation." -ForegroundColor Yellow
    Write-Host "Open PowerShell as Administrator and run:" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "    cd `"$Root`"" -ForegroundColor Cyan
    Write-Host "    .\install-task.cmd -$action" -ForegroundColor Cyan
    Write-Host ""
    exit 1
}

function Show-State {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) {
        Write-Host "task    : $TaskName -- not registered"
    } else {
        Write-Host "task    : $TaskName  [$($task.State)]"
        Write-Host "account : $($task.Principal.UserId)  logon=$($task.Principal.LogonType)  level=$($task.Principal.RunLevel)"
        foreach ($t in $task.Triggers) { Write-Host "trigger : $($t.CimClass.CimClassName)" }
        foreach ($a in $task.Actions) { Write-Host "action  : $($a.Arguments)" }
    }
    $listening = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    Write-Host "port    : $Port $(if ($listening) { "LISTENING (pid $($listening[0].OwningProcess))" } else { 'not listening' })"
    Write-Host "log     : $(Join-Path $Root 'logs\ensure-history.txt')"
}

if ($Status -or (-not $Install -and -not $Remove -and -not $Run -and -not $Restart)) {
    if (-not $Status) {
        Write-Host ""
        Write-Host "STATUS ONLY -- nothing was installed and nothing changed." -ForegroundColor Yellow
        Write-Host "  to register the task:  .\install-task.ps1 -Install    (needs elevation, asks for your password)" -ForegroundColor Yellow
        Write-Host "  (a bare command shows status on purpose: a typo must not register a task that stores a password)" -ForegroundColor DarkGray
        Write-Host ""
    }
    Show-State
    exit 0
}

if ($Remove) {
    Assert-Elevated 'Remove'
    if (-not (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) {
        Write-Host "Task $TaskName is not registered."
        exit 0
    }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "removed $TaskName"
    exit 0
}

if ($Restart) {
    # Why this exists: Task Scheduler's "End" ends the task, not the service. The launcher starts the
    # service detached, so the process survives the task, and the next run reports "already
    # listening" and exits 0 -- a restart that looks successful and changes nothing.
    $listening = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    if (-not $listening) {
        Write-Host "nothing is listening on $Port -- starting it"
    }
    foreach ($connection in $listening) {
        $processId = $connection.OwningProcess
        Write-Host "stopping pid $processId on port $Port"
        try {
            Stop-Process -Id $processId -Force -ErrorAction Stop
        } catch {
            Write-Host ""
            Write-Host "could not stop pid $processId : $($_.Exception.Message)" -ForegroundColor Yellow
            Write-Host "It runs at runlevel Highest, so an unelevated shell cannot terminate it." -ForegroundColor Yellow
            Write-Host "Re-run this from an elevated prompt:" -ForegroundColor Yellow
            Write-Host "    .\install-task.ps1 -Restart" -ForegroundColor Cyan
            exit 1
        }
    }
    # Give the port a moment to be released, then start it through the same launcher the task uses,
    # so a manual restart exercises the same path the scheduled one does.
    Start-Sleep -Seconds 2
    & $PowerShell -NoProfile -ExecutionPolicy Bypass -File $Ensure
    exit $LASTEXITCODE
}

if ($Run) {
    Write-Host "running the launcher directly (no elevation needed)"
    & $PowerShell -NoProfile -ExecutionPolicy Bypass -File $Ensure
    exit $LASTEXITCODE
}

if ($Install) {
    Assert-Elevated 'Install'

    if (-not (Test-Path $Ensure)) { throw "launcher not found: $Ensure" }

    $account = if ($User) { $User } else { "$env:USERDOMAIN\$env:USERNAME" }
    Write-Host "account : $account"
    Write-Host "The password is used only to register the task; Task Scheduler stores it." -ForegroundColor DarkGray
    $credential = Get-Credential -UserName $account -Message "Password for $account (stored by Task Scheduler)"

    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Write-Host "already registered; replacing" -ForegroundColor Yellow
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    }

    # Register-ScheduledTask has separate parameter sets: -Principal OR -User/-Password, never both
    # (mixing them is "Parameter set cannot be resolved"). The User set is the one we need, because
    # supplying -Password is what stores the credential and sets LogonType=Password, so the task runs
    # whether or not you are logged on.
    $plainPassword = $credential.GetNetworkCredential().Password

    $action = New-ScheduledTaskAction -Execute $PowerShell `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$Ensure`"" `
        -WorkingDirectory $Root

    $boot = New-ScheduledTaskTrigger -AtStartup
    $boot.Delay = 'PT2M'

    # NO -RepetitionDuration. `[TimeSpan]::MaxValue` serialises to P99999999DT23H59M59S and Task
    # Scheduler refuses the whole task XML ("value ... incorrectly formatted or out of range").
    # Omitting it is what means "no end" -- verified by registering a throwaway task: with the value
    # the XML is rejected, without it the XML is accepted. Clearing it later works too, but omitting
    # is one less moving part.
    $repeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
        -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)

    $settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
        -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

    Register-ScheduledTask -TaskName $TaskName `
        -Action $action -Trigger @($boot, $repeat) -Settings $settings `
        -Description $Description `
        -User $credential.UserName -Password $plainPassword -RunLevel Highest | Out-Null

    Write-Host "`nregistered $TaskName" -ForegroundColor Cyan
    Write-Host "starting it now..."
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 8

    Show-State
    Write-Host ""
    Write-Host "check it:" -ForegroundColor Cyan
    Write-Host "  Invoke-RestMethod http://127.0.0.1:$Port/healthz"
    exit 0
}

@echo off
setlocal
REM ============================================================================
REM  Start (or restart) McTrader_Logging on port 8004.
REM ============================================================================
REM
REM  This does not touch the process directly. It asks Task Scheduler to run the
REM  launcher, because the service lives in SESSION 0 (that is where Task
REM  Scheduler starts it) and a process in your interactive session cannot
REM  terminate it -- "Access is denied", at any run level. Running the launcher
REM  from inside the task puts it back in session 0, where the kill succeeds.
REM
REM  Starting a task that holds a stored password needs no elevation, so this is
REM  a plain double-click.
REM
REM  The launcher stops whatever holds the port and starts it again, so this is a
REM  restart: the code on disk is picked up every time.
REM
REM  For scripted use, call: schtasks /run /tn "McTrader_Logging_KeepAlive"
REM ============================================================================

set TASK=McTrader_Logging_KeepAlive
set PORT=8004

echo.
echo   Starting McTrader_Logging...
echo.

schtasks /run /tn "%TASK%" >nul 2>&1
if errorlevel 1 (
    echo   Could not start the scheduled task "%TASK%".
    echo   It may not be registered. From an elevated prompt:
    echo       .\install-task.ps1 -Install
    echo.
    pause
    endlocal & exit /b 1
)

REM The task returns immediately; the launcher then stops and starts the service.
REM ping, not timeout: timeout refuses to run without a console
ping -n 9 127.0.0.1 >nul

echo   What the launcher did:
powershell -NoProfile -Command "Get-Content '%~dp0logs\ensure-history.txt' -Tail 5 | ForEach-Object { '     ' + $_ }" 2>nul

echo.
powershell -NoProfile -Command "try { $h = Invoke-RestMethod 'http://127.0.0.1:%PORT%/healthz' -TimeoutSec 10; Write-Host ('   healthz: ' + $h.status + '   db=' + $h.database) } catch { Write-Host '   healthz: NOT ANSWERING -- see the service log below' }"

echo.
echo   Service log: %~dp0logs\mctrader_logging.log
echo.
pause
endlocal & exit /b 0

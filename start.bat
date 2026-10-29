@echo off
setlocal
REM ============================================================================
REM  Start (or restart) McTrader_Logging on port 8004.
REM ============================================================================
REM
REM  This is the same call the scheduled task makes, so running it by hand
REM  exercises the real path rather than a parallel one.
REM
REM  The launcher stops whatever holds port 8004 and then starts it again. That
REM  is deliberate: it is what makes a plain run a RESTART, so the code on disk
REM  is picked up every time without a separate stop step.
REM
REM  It should not need elevation. The task is registered at the default run
REM  level; if it was ever re-registered with -RunLevel Highest, the service
REM  runs with an elevated token and only an elevated prompt can stop it.
REM
REM  Pauses at the end so a double-click gives you time to read the result.
REM  For scripted use, call scripts\Ensure-LoggingService.ps1 directly.
REM ============================================================================

echo.
echo   Starting McTrader_Logging on port 8004...
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\Ensure-LoggingService.ps1"
set RC=%ERRORLEVEL%

echo.
if "%RC%"=="0" (
    echo   Started.  Check: http://127.0.0.1:8004/healthz
    echo   Log:             %~dp0logs\mctrader_logging.log
) else (
    echo   Failed with exit code %RC%.
    echo   Launcher log:    %~dp0logs\ensure-history.txt
    echo   Service log:     %~dp0logs\mctrader_logging.log
)
echo.
pause
endlocal & exit /b %RC%

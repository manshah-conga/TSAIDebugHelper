@echo off
REM start_server.bat -- start the TS Intelligent Debug Helper web app.
REM
REM Works from cmd.exe, from PowerShell, and by double-clicking in Explorer.
REM Use this rather than start_server.ps1 unless you are already in a
REM PowerShell prompt -- in cmd.exe and Explorer, ".ps1" is associated with
REM Notepad, so "start_server.ps1" opens the script for editing instead of
REM running it.
REM
REM   start_server.bat install     -> create .venv, install dependencies, start
REM                                   (run this once, first)
REM   start_server.bat             -> 127.0.0.1:8000  (this machine only)
REM   start_server.bat all         -> 0.0.0.0:8000    (reachable from the network)
REM   start_server.bat all 9000    -> 0.0.0.0:9000     (any other port)
REM   start_server.bat local 9000  -> 127.0.0.1:9000

setlocal
set "ARG1=%~1"
set "PORT=%~2"
set "INSTALL="

if /I "%ARG1%"=="install" (
    set "INSTALL=-Install"
    set "ARG1=%~2"
    set "PORT=%~3"
)

if "%ARG1%"=="" set "ARG1=Local"
if "%PORT%"==""  set "PORT=8000"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_server.ps1" -Listen %ARG1% -Port %PORT% %INSTALL%

echo.
echo Server stopped.
pause
endlocal

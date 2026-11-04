@echo off
rem Team Room - open the page in the browser, starting the server if needed.
setlocal EnableExtensions

for %%I in ("%~dp0..") do set "APP=%%~fI"
set "PORT="
if exist "%APP%\data\run\port.txt" for /f "usebackq delims=" %%p in ("%APP%\data\run\port.txt") do set "PORT=%%p"
if not defined PORT set "PORT=8777"
if defined TEAMROOM_PORT set "PORT=%TEAMROOM_PORT%"

call "%~dp0team-room-server.cmd" -Port %PORT% -OpenBrowser
if errorlevel 1 (
  echo Team Room: the server did not start. Logs: %APP%\data\logs
  echo Run bin\team-room-server.cmd in a console to see the error.
  pause
  exit /b 1
)
exit /b 0

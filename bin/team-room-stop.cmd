@echo off
rem Team Room - stop the local server (only our own processes, never a foreign one).
setlocal EnableExtensions
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0team-room-stop.ps1" %*
exit /b %ERRORLEVEL%

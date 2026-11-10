@echo off
rem Team Room - hidden server start (autostart and team-room.cmd use this).
rem All logic lives in launch-server.ps1: WMI start, no console window, no duplicates.
setlocal EnableExtensions
powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0launch-server.ps1" %*
exit /b %ERRORLEVEL%

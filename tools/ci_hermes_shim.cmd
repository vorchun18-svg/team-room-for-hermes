@echo off
rem Team Room CI: thin 'hermes' replacement (see ci_hermes_shim.py).
rem CI puts this file into PATH as hermes.cmd next to a copy of ci_hermes_shim.py
rem (or sets TEAMROOM_CI_SHIM to the full path of ci_hermes_shim.py).
setlocal
set "SHIM=%~dp0ci_hermes_shim.py"
if defined TEAMROOM_CI_SHIM set "SHIM=%TEAMROOM_CI_SHIM%"
if not exist "%SHIM%" exit /b 2
python "%SHIM%" %*
exit /b %ERRORLEVEL%

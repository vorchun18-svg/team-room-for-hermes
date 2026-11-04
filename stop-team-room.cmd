@echo off
rem Остановить серверы «Комнаты команды»: боевой (8777) и демо-мок (8778).
rem По умолчанию оба порта; --port N можно указать несколько раз.
rem Убиваются только процессы Python, в командной строке которых есть team-room.
setlocal EnableExtensions
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
  echo Python не найден в PATH — не могу проверить процессы.
  exit /b 2
)

python "%~dp0tools\stop_team_room.py" %*
exit /b %ERRORLEVEL%

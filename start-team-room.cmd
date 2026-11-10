@echo off
rem «Комната команды» — локальный сервер + браузер. Автозапуск НЕ ставится.
rem Сервер слушает только 127.0.0.1:8777. Остановить — Ctrl+C в окне сервера.
setlocal
cd /d "%~dp0"

start "Комната команды" cmd /k python "%~dp0server\app.py"
timeout /t 3 /nobreak >nul
start "" http://127.0.0.1:8777

endlocal

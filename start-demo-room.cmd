@echo off
rem «Комната команды» — ДЕМО-стенд на мок-сервере. Порт по умолчанию 8778.
rem Данные выдуманы, агенты Hermes не запускаются, доска и Vault не трогаются.
rem Браузер по умолчанию НЕ открывается: добавьте --open, если нужно.
rem
rem   start-demo-room.cmd                  → http://127.0.0.1:8778
rem   start-demo-room.cmd --port 8785      → другой порт
rem   start-demo-room.cmd --no-browser     → явно без браузера (по умолчанию и так)
rem   start-demo-room.cmd --open           → открыть браузер после старта
rem   start-demo-room.cmd --phase-scale 2  → медленнее (1 = как в жизни, 35 с)
setlocal EnableExtensions
cd /d "%~dp0"

set "PORT=8778"
set "SCALE=1"
set "OPEN=0"

:parse
if "%~1"=="" goto run
if /I "%~1"=="--port" (
  set "PORT=%~2"
  shift
  shift
  goto parse
)
if /I "%~1"=="--phase-scale" (
  set "SCALE=%~2"
  shift
  shift
  goto parse
)
if /I "%~1"=="--no-browser" (
  set "OPEN=0"
  shift
  goto parse
)
if /I "%~1"=="--open" (
  set "OPEN=1"
  shift
  goto parse
)
if /I "%~1"=="--help" goto usage
if /I "%~1"=="-h" goto usage
echo Неизвестный параметр: %~1
goto usage

:usage
echo.
echo   start-demo-room.cmd [--port N] [--phase-scale X] [--no-browser] [--open]
echo.
echo   --port N         порт мока (по умолчанию 8778)
echo   --phase-scale X  скорость эволюции запроса (1 = 35 секунд)
echo   --no-browser     не открывать браузер (поведение по умолчанию)
echo   --open           открыть браузер после старта
echo.
exit /b 2

:run
where python >nul 2>&1
if errorlevel 1 (
  echo Python не найден в PATH. Установите Python 3.11+ и повторите.
  exit /b 2
)

if not exist "data\logs" mkdir "data\logs"
if not exist "data\run" mkdir "data\run"

netstat -ano | findstr /R /C:":%PORT% .*LISTENING" >nul 2>&1
if not errorlevel 1 (
  echo Порт %PORT% занят. Остановите другой экземпляр: stop-team-room.cmd --port %PORT%
  echo или запустите демо на другом порту: start-demo-room.cmd --port 8785
  exit /b 3
)

echo. >> "data\logs\demo.log"
echo [%DATE% %TIME%] start demo port=%PORT% phase-scale=%SCALE% >> "data\logs\demo.log"
start "team-room demo %PORT%" /min cmd /c "python "%~dp0tools\mock_server.py" --port %PORT% --phase-scale %SCALE% --pid-file "%~dp0data\run\demo-%PORT%.pid" >> "%~dp0data\logs\demo.log" 2>&1"

set /a WAIT=0
:wait
curl -s -o NUL --max-time 2 "http://127.0.0.1:%PORT%/api/health"
if not errorlevel 1 goto ready
set /a WAIT+=1
if %WAIT% GEQ 20 goto failed
"%SystemRoot%\System32\timeout.exe" /t 1 /nobreak >nul
goto wait

:failed
echo Демо не поднялось за 20 секунд. Смотрите data\logs\demo.log:
echo   type data\logs\demo.log
exit /b 1

:ready
echo.
echo ДЕМО «Комнаты команды»: http://127.0.0.1:%PORT%
echo   данные выдуманы, агенты не запускаются, доска и Vault не трогаются
echo   лог: data\logs\demo.log   pid: data\run\demo-%PORT%.pid
echo   остановить: stop-team-room.cmd --port %PORT%
echo.
if "%OPEN%"=="1" start "" "http://127.0.0.1:%PORT%"
exit /b 0

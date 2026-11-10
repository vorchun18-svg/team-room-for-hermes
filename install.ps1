<#
.SYNOPSIS
    Установка "Командных комнат" для Hermes Desktop на Windows - одной командой.

.DESCRIPTION
    Скачивает готовый архив с GitHub (git не нужен), ставит приложение в
    %LOCALAPPDATA%\TeamRoom, регистрирует плагин в Hermes Desktop, включает
    автозапуск сервера при входе в Windows и открывает страницу в браузере.

    Повторный запуск - это обновление: программа заменяется, а папка data
    с комнатами, журналом и настройками остаётся на месте.

    Нужен только Windows 10/11 и Python 3.11+. Права администратора не нужны.

.PARAMETER Version
    Тег версии на GitHub (например v1.0.0 или main). По умолчанию - последний
    выпуск, а если выпусков ещё нет - ветка main.

.PARAMETER InstallHome
    Куда ставить программу; на письме это ключ -Home. По умолчанию
    %LOCALAPPDATA%\TeamRoom (или значение переменной TEAMROOM_HOME).
    Данные лежат в <Home>\data и при обновлении не трогаются.
    (ключ -Home - это алиас: $HOME в PowerShell только для чтения.)

.PARAMETER Port
    Порт локального сервера. По умолчанию 8777. Менять нужно редко.

.PARAMETER SourceDir
    Папка с уже распакованным проектом - вместо скачивания с GitHub
    (пригодится без интернета и в тестах).

.PARAMETER NoAutostart
    Не включать автозапуск при входе в Windows (и убрать прежний, если был).

.PARAMETER NoBrowser
    Не открывать страницу в браузере после установки.

.PARAMETER Uninstall
    Удалить программу, плагин и автозапуск. Данные по умолчанию сохраняются.

.PARAMETER RemoveData
    Вместе с -Uninstall удалить и папку data (комнаты, журнал, настройки).

.PARAMETER InstallPython
    Явное согласие поставить Python через winget, если его нет.

.PARAMETER WaitSeconds
    Сколько секунд ждать ответа сервера после запуска. По умолчанию 30.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -Command "$s = irm https://raw.githubusercontent.com/vorchun18-svg/team-room-for-hermes/main/install.ps1; iex $s.TrimStart([char]0xFEFF)"
    Запуск одной строкой: скачивает скрипт и сразу выполняет (BOM снимается,
    иначе PowerShell считает его частью команды).

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1 -DryRun
    Только проверить компьютер и показать, чего не хватает.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1 -Port 8899 -NoAutostart -Home D:\TeamRoom

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1 -Uninstall

.NOTES
    Скрипт идемпотентный: повторный запуск не создаёт вторую задачу в
    планировщике и не поднимает второй сервер. Только встроенный PowerShell 5.1
    и стандартная библиотека Python - ничего доустанавливать не нужно.
    Ключ -Home - то же самое, что -InstallHome.
#>
[CmdletBinding()]
param(
    [string]$Version = '',
    [Alias('Home')]
    [string]$InstallHome = '',
    [int]$Port = 8777,
    [string]$SourceDir = '',
    [switch]$NoAutostart,
    [switch]$NoBrowser,
    [switch]$Uninstall,
    [switch]$RemoveData,
    [switch]$InstallDeps,
    [switch]$DryRun,
    [switch]$Yes,
    [int]$WaitSeconds = 30
)

$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
# В консоли PowerShell сам рисует русские буквы; в файл/пайп пишем UTF-8, чтобы
# логи и ответы агентам читались без «кракозябр».
try { if ([Console]::IsOutputRedirected) { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } } catch { }
try { [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12 } catch { }

$REPO = 'vorchun18-svg/team-room-for-hermes'
$TASK_NAME = 'TeamRoomServer'
$PLUGIN_ID = 'team-room'
$MIN_PY_CODE = 311   # 3.11 - минимальная версия

# ── вывод ────────────────────────────────────────────────────────────────────
function Write-Step([string]$text) {
    Write-Host ''
    Write-Host ('  ' + $text) -ForegroundColor Cyan
}
function Write-Ok([string]$text) { Write-Host ('  [ок] ' + $text) -ForegroundColor Green }
function Write-Warn2([string]$text) { Write-Host ('  [!] ' + $text) -ForegroundColor Yellow }
function Write-Bad([string]$text) { Write-Host ('  [х] ' + $text) -ForegroundColor Red }
function Write-Note([string]$text) { Write-Host ('      ' + $text) -ForegroundColor Gray }

function Resolve-FullPath([string]$path, [string]$fallback) {
    $value = $path
    if (-not $value) { $value = $fallback }
    if (-not $value) { return '' }
    $value = [Environment]::ExpandEnvironmentVariables($value)
    if (-not [System.IO.Path]::IsPathRooted($value)) {
        $value = Join-Path (Get-Location).Path $value
    }
    try { return [System.IO.Path]::GetFullPath($value).TrimEnd('\') } catch { return $value.TrimEnd('\') }
}

function Get-HermesHome {
    if ($env:HERMES_HOME) { return (Resolve-FullPath $env:HERMES_HOME '') }
    $local = $env:LOCALAPPDATA
    if (-not $local) { $local = Join-Path $env:USERPROFILE 'AppData\Local' }
    return (Resolve-FullPath '' (Join-Path $local 'hermes'))
}

# ── Python 3.11+ ─────────────────────────────────────────────────────────────
function Get-PyVersion([string]$exe) {
    # Одно число вместо строки: 311 для 3.11, 312 для 3.12.
    try {
        $raw = & $exe '-c' 'import sys;print(sys.version_info[0]*100+sys.version_info[1])' 2>$null
        if ($raw -and ("$raw".Trim() -match '^(\d{3,4})$')) {
            $code = [int]$Matches[1]
            return @{ Code = $code; Text = ('{0}.{1}' -f [math]::Floor($code / 100), ($code % 100)) }
        }
    }
    catch { }
    return $null
}

function Find-Python {
    $tried = @()
    $found = @()

    $launchers = @()
    foreach ($name in @('py.exe', 'py')) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd) { $launchers += $cmd.Source; break }
    }
    foreach ($launcher in $launchers) {
        foreach ($spec in @('-3.13', '-3.12', '-3.11', '-3')) {
            try {
                $exe = & $launcher $spec '-c' 'import sys;print(sys.executable)' 2>$null
            }
            catch { $exe = $null }
            if (-not $exe) { continue }
            $exe = ([string]$exe).Trim()
            if ($exe -and (Test-Path $exe)) { $found += $exe; break }
        }
        if ($found.Count -gt 0) { break }
    }

    $local = $env:LOCALAPPDATA
    if (-not $local) { $local = Join-Path $env:USERPROFILE 'AppData\Local' }
    $programs = Join-Path $local 'Programs\Python'
    if (Test-Path $programs) {
        $dirs = Get-ChildItem $programs -Directory -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match '^Python3(1[1-9]|[2-9]\d)$' } |
            Sort-Object Name -Descending
        foreach ($dir in $dirs) { $found += (Join-Path $dir.FullName 'python.exe') }
    }
    foreach ($guess in @('C:\Python312\python.exe', 'C:\Python311\python.exe')) {
        if (Test-Path $guess) { $found += $guess }
    }

    $onPath = Get-Command python -ErrorAction SilentlyContinue
    if ($onPath -and $onPath.Source) { $found += $onPath.Source }

    foreach ($exe in ($found | Select-Object -Unique)) {
        if (-not (Test-Path $exe)) { continue }
        $tried += $exe
        $ver = Get-PyVersion $exe
        if (-not $ver) { continue }
        if ($ver.Code -lt $MIN_PY_CODE) { continue }
        $windowExe = $exe
        $candidate = Join-Path (Split-Path $exe) 'pythonw.exe'
        if (Test-Path $candidate) { $windowExe = $candidate }
        return @{
            Exe       = $exe
            WindowExe = $windowExe
            Version   = $ver.Text
            Tried     = $tried
        }
    }
    return @{ Exe = ''; WindowExe = ''; Version = ''; Tried = $tried }
}

# ── health ───────────────────────────────────────────────────────────────────
# ── проверка окружения ─────────────────────────────────────────────────────
function Get-FreeSpace([string]$path) {
    try {
        $root = [System.IO.Path]::GetPathRoot($path)
        $drive = New-Object System.IO.DriveInfo($root)
        return [int64]$drive.AvailableFreeSpace
    }
    catch { return [int64]-1 }
}

function Format-Size([int64]$bytes) {
    if ($bytes -lt 0) { return 'неизвестно' }
    if ($bytes -ge 1GB) { return ('{0:N1} ГБ' -f ($bytes / 1GB)) }
    return ('{0:N0} МБ' -f ($bytes / 1MB))
}

function Get-PortState([int]$port, [string]$homeDir) {
    $owner = 0
    try {
        $conn = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction Stop | Select-Object -First 1
        if ($conn) { $owner = [int]$conn.OwningProcess }
    }
    catch { $owner = 0 }
    if (-not $owner) { return @{ Free = $true; Ours = $false; Pid = 0 } }
    $ours = $false
    try {
        $proc = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $owner) -ErrorAction Stop
        $line = [string]$proc.CommandLine
        if ($line -and $line.IndexOf($homeDir, [System.StringComparison]::OrdinalIgnoreCase) -ge 0 -and $line -match 'app\.py') { $ours = $true }
    }
    catch { }
    return @{ Free = $false; Ours = $ours; Pid = $owner }
}

function Test-Internet {
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $task = $client.ConnectAsync('github.com', 443)
        $ok = $task.Wait(2500) -and $client.Connected
        $client.Close()
        return $ok
    }
    catch { return $false }
}

function Get-Whisper([string]$pythonExe) {
    if (-not $pythonExe) { return $null }
    try {
        $raw = & $pythonExe '-c' 'import faster_whisper,sys;print(getattr(faster_whisper,"__version__","?"))' 2>$null
        if ($raw -and "$raw".Trim() -and "$raw" -notmatch 'Traceback') { return ("$raw").Trim() }
    }
    catch { }
    return $null
}

function New-Row([string]$name, [bool]$required, [bool]$ok, [string]$state, [string]$note, [string]$kind) {
    return [pscustomobject]@{ Name = $name; Required = $required; Ok = $ok; State = $state; Note = $note; Kind = $kind }
}

function Get-EnvReport([string]$hermesHome, [int]$port, [string]$dataDir, $python, [string]$sourceDir) {
    $rows = New-Object System.Collections.ArrayList

    $osBuild = [Environment]::OSVersion.Version.Build
    $osName = 'Windows 10'
    if ($osBuild -ge 22000) { $osName = 'Windows 11' }
    [void]$rows.Add((New-Row 'Windows 10/11' $true $true ('есть (' + $osName + ', сборка ' + $osBuild + ')') 'обязательно' 'os'))

    if ($python.Exe) {
        [void]$rows.Add((New-Row 'Python 3.11+' $true $true ('есть (' + $python.Version + ')') 'обязательно' 'python'))
    }
    else {
        [void]$rows.Add((New-Row 'Python 3.11+' $true $false 'нет' 'обязательно: без него сервер не запустится' 'python'))
    }

    $free = Get-FreeSpace $dataDir
    $freeOk = ($free -lt 0) -or ($free -ge 300MB)
    [void]$rows.Add((New-Row 'Место на диске' $true $freeOk ((Format-Size $free) + ' свободно') 'нужно около 300 МБ' 'disk'))

    $portState = Get-PortState $port $dataDir
    if ($portState.Free) {
        [void]$rows.Add((New-Row ('Порт ' + $port) $true $true 'свободен' 'обязательно' 'port'))
    }
    elseif ($portState.Ours) {
        [void]$rows.Add((New-Row ('Порт ' + $port) $true $true ('замет наш сервер, PID ' + $portState.Pid) 'обновится на месте' 'port'))
    }
    else {
        [void]$rows.Add((New-Row ('Порт ' + $port) $true $false ('занят PID ' + $portState.Pid) 'обязательно: возьмите другой -Port' 'port'))
    }

    $hermesOk = Test-Path $hermesHome
    [void]$rows.Add((New-Row 'Hermes Desktop' $false $hermesOk ($(if ($hermesOk) { 'есть' } else { 'нет' })) 'без него: только браузер, без строки в меню' 'hermes'))

    $cli = Get-Command hermes -ErrorAction SilentlyContinue
    if (-not $cli) {
        $maybe = Join-Path $env:LOCALAPPDATA 'hermes\bin\hermes.exe'
        if (Test-Path $maybe) { $cli = Get-Item $maybe }
    }
    [void]$rows.Add((New-Row 'hermes CLI' $false ([bool]$cli) ($(if ($cli) { 'есть' } else { 'нет' })) 'без него: не пишет в канбан, комнаты работают' 'cli'))

    $whisper = Get-Whisper $python.Exe
    $whisperOk = [bool]$whisper
    [void]$rows.Add((New-Row 'faster-whisper' $false $whisperOk ($(if ($whisperOk) { 'есть (' + $whisper + ')' } else { 'нет' })) 'без него: диктовка недоступна' 'whisper'))

    $ffmpeg = Get-Command ffmpeg -ErrorAction SilentlyContinue
    if (-not $ffmpeg) {
        $maybeFf = Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links\ffmpeg.exe'
        if (Test-Path $maybeFf) { $ffmpeg = Get-Item $maybeFf }
    }
    [void]$rows.Add((New-Row 'ffmpeg' $false ([bool]$ffmpeg) ($(if ($ffmpeg) { 'есть' } else { 'нет' })) 'необязательно: диктовка и так работает' 'ffmpeg'))

    if ($sourceDir) {
        [void]$rows.Add((New-Row 'Интернет (github.com)' $false $true 'не нужен' 'ставлю из папки' 'net'))
    }
    else {
        $net = Test-Internet
        [void]$rows.Add((New-Row 'Интернет (github.com)' $true $net ($(if ($net) { 'есть' } else { 'нет' })) 'обязательно для скачивания' 'net'))
    }

    return $rows
}

function Show-EnvTable($rows) {
    $w1 = 24
    $w2 = 26
    Write-Host ''
    Write-Host ('  ' + 'Что'.PadRight($w1) + ' ' + 'Состояние'.PadRight($w2) + ' Что это значит') -ForegroundColor DarkGray
    Write-Host ('  ' + ('-' * ($w1 + $w2 + 34))) -ForegroundColor DarkGray
    foreach ($row in $rows) {
        $color = 'Gray'
        if ($row.Ok) { $color = 'Green' }
        elseif ($row.Required) { $color = 'Red' }
        else { $color = 'Yellow' }
        $name = $row.Name.PadRight($w1)
        $state = $row.State.PadRight($w2)
        Write-Host ('  ' + $name + ' ' + $state + ' ' + $row.Note) -ForegroundColor $color
    }
}

function Show-Missing($rows) {
    $missing = @($rows | Where-Object { -not $_.Ok })
    if ($missing.Count -eq 0) {
        Write-Host ''
        Write-Ok 'Всё на месте — можно ставить.'
        return
    }
    Write-Host ''
    Write-Host '  Чего не хватает:' -ForegroundColor White
    foreach ($row in $missing) {
        if ($row.Kind -eq 'python') {
            Write-Host '   - Python 3.11+ — без него сервер не запустится вообще.' -ForegroundColor Yellow
            Write-Note 'winget install --id Python.Python.3.12 -e'
        }
        elseif ($row.Kind -eq 'port') {
            Write-Host ('   - Порт ' + $row.State + ' — сервер не сможет занять место.') -ForegroundColor Yellow
            Write-Note 'Запустите установку с другим портом: -Port 8788'
        }
        elseif ($row.Kind -eq 'disk') {
            Write-Host '   - Мало места на диске — скачивание может не закончиться.' -ForegroundColor Yellow
        }
        elseif ($row.Kind -eq 'net') {
            Write-Host '   - Нет связи с github.com — скачать программу не получится.' -ForegroundColor Yellow
            Write-Note 'Проверьте интернет или поставьте из папки: -SourceDir "D:\team-room"'
        }
        elseif ($row.Kind -eq 'hermes') {
            Write-Host '   - Hermes Desktop не найден — откроется только в браузере.' -ForegroundColor Yellow
            Write-Note 'Плагин встанет сам, когда Hermes появится: повторите установку.'
        }
        elseif ($row.Kind -eq 'cli') {
            Write-Host '   - hermes CLI не найден — записи в канбан не будет, остальное работает.' -ForegroundColor Yellow
        }
        elseif ($row.Kind -eq 'whisper') {
            Write-Host '   - faster-whisper нет — кнопка микрофона скажет "распознавание не установлено".' -ForegroundColor Yellow
            Write-Note 'python -m pip install --user faster-whisper'
        }
        elseif ($row.Kind -eq 'ffmpeg') {
            Write-Host '   - ffmpeg нет — часть аудиоформатов не конвертируется.' -ForegroundColor Yellow
            Write-Note 'winget install --id Gyan.FFmpeg -e'
        }
    }
}

function Test-Interactive {
    try {
        return ([Environment]::UserInteractive -and $Host.Name -eq 'ConsoleHost' -and $null -ne $Host.UI.RawUI)
    }
    catch { return $false }
}

function Install-Missing($rows, [string]$pythonExe) {
    $interactive = Test-Interactive
    try { if ([Console]::IsInputRedirected) { $interactive = $false } } catch { $interactive = $false }
    if (-not $interactive -and -not $Yes) {
        Write-Note 'Нет возможности спросить (ввод перенаправлен) - ничего не ставлю.'
        Write-Note 'Поставьте вручную по списку выше или запустите с ключом -InstallDeps -Yes.'
    }
    $logFile = Join-Path $env:TEMP 'team-room-install-deps.log'
    foreach ($row in @($rows | Where-Object { -not $_.Ok })) {
        if ($row.Kind -eq 'whisper') {
            $size = 'около 400 МБ, плюс модель 1–3 ГБ при первом включении'
            if (-not $interactive -and -not $Yes) {
                Write-Note 'faster-whisper: python -m pip install --user faster-whisper'
                continue
            }
            $answer = 'y'
            if ($interactive -and -not $Yes) {
                $answer = Read-Host ('  Поставить faster-whisper (диктовка)? ' + $size + ' [Y/n]')
            }
            if ($answer -match '^(n|no|н|нет)$') { continue }
            Write-Step 'Ставлю faster-whisper (диктовка)'
            Write-Note ('python -m pip install --user faster-whisper  ' + $size)
            & $pythonExe '-m' 'pip' 'install' '--user' 'faster-whisper' 2>&1 | Out-Host
            Add-Content -Path $logFile -Value ('pip faster-whisper: exit ' + $LASTEXITCODE)
            if ($LASTEXITCODE -eq 0) { Write-Ok 'faster-whisper поставлен' }
            else { Write-Warn2 'pip не справился — см. вывод выше' }
        }
        elseif ($row.Kind -eq 'ffmpeg') {
            $size = 'около 150 МБ'
            if (-not $interactive -and -not $Yes) {
                Write-Note 'ffmpeg: winget install --id Gyan.FFmpeg -e'
                continue
            }
            $answer = 'y'
            if ($interactive -and -not $Yes) {
                $answer = Read-Host ('  Поставить ffmpeg? ' + $size + ' [Y/n]')
            }
            if ($answer -match '^(n|no|н|нет)$') { continue }
            Write-Step 'Ставлю ffmpeg'
            Write-Note ('winget install --id Gyan.FFmpeg -e  ' + $size)
            & winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements 2>&1 | Out-Host
            Add-Content -Path $logFile -Value ('winget ffmpeg: exit ' + $LASTEXITCODE)
        }
        elseif ($row.Kind -eq 'python') {
            $size = 'около 30 МБ'
            if (-not $interactive -and -not $Yes) {
                Write-Note 'Python: winget install --id Python.Python.3.12 -e'
                continue
            }
            $answer = 'y'
            if ($interactive -and -not $Yes) {
                $answer = Read-Host ('  Поставить Python 3.12? ' + $size + ' [Y/n]')
            }
            if ($answer -match '^(n|no|н|нет)$') { continue }
            Write-Step 'Ставлю Python 3.12 через winget'
            Write-Note ('winget install --id Python.Python.3.12 -e  ' + $size)
            & winget install --id Python.Python.3.12 -e --accept-source-agreements --accept-package-agreements 2>&1 | Out-Host
            Add-Content -Path $logFile -Value ('winget python: exit ' + $LASTEXITCODE)
        }
        else {
            Write-Note ('Не ставлю автоматически: ' + $row.Name + ' — поставьте вручную.')
        }
    }
}

function Test-Health([int]$port) {
    $url = 'http://127.0.0.1:' + $port + '/api/health'
    try {
        $request = [System.Net.HttpWebRequest]::Create($url)
        $request.Proxy = $null
        $request.Timeout = 1500
        $request.ReadWriteTimeout = 1500
        $request.Method = 'GET'
        $response = $request.GetResponse()
        $code = [int]$response.StatusCode
        $response.Close()
        return ($code -eq 200)
    }
    catch {
        return $false
    }
}

function Wait-Health([int]$port, [int]$seconds) {
    $deadline = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-Health $port) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

# ── остановка прежнего сервера ───────────────────────────────────────────────
function Stop-TeamRoom([string]$homeDir, [string]$dataDir) {
    $ids = @()
    $runDir = Join-Path $dataDir 'run'
    $pidFiles = @()
    if (Test-Path $runDir) {
        $pidFiles = @(Get-ChildItem $runDir -Filter '*.pid' -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -like 'server-*' })
    }
    foreach ($file in $pidFiles) {
        $text = ''
        try { $text = (Get-Content $file.FullName -Raw -ErrorAction Stop) } catch { $text = '' }
        if ($text -and $text.Trim() -match '^\d+$') { $ids += [int]$text.Trim() }
    }

    # Только процессы, у которых в командной строке НАША папка и server\app.py:
    # чужие серверы на том же порту не трогаем.
    if ($homeDir) {
        try {
            $procs = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction Stop
            foreach ($proc in $procs) {
                $line = [string]$proc.CommandLine
                if (-not $line) { continue }
                if ($line.IndexOf($homeDir, [System.StringComparison]::OrdinalIgnoreCase) -lt 0) { continue }
                if ($line -notmatch 'app\.py') { continue }
                $ids += [int]$proc.ProcessId
            }
        }
        catch { }
    }

    $killed = 0
    foreach ($id in ($ids | Sort-Object -Unique)) {
        $proc = Get-Process -Id $id -ErrorAction SilentlyContinue
        if (-not $proc) { continue }
        if ($proc.ProcessName -notlike 'python*') { continue }
        Stop-Process -Id $id -Force -ErrorAction SilentlyContinue
        $killed++
    }
    foreach ($file in $pidFiles) { Remove-Item $file.FullName -Force -ErrorAction SilentlyContinue }
    return $killed
}

function Start-TeamRoom([string]$homeDir, [string]$dataDir, [int]$port, $python) {
    if (Test-Health $port) { return @{ Started = $false; Already = $true } }

    foreach ($sub in @('logs', 'run')) {
        $dir = Join-Path $dataDir $sub
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    }

    $scriptPath = Join-Path $homeDir 'server\app.py'
    # Процесс создаём через WMI: ни окна, ни унаследованных ручек — установщик
    # не "висит" и в консоли ничего не мигает.
    $commandLine = '"' + $python.WindowExe + '" "' + $scriptPath + '" --root "' + $homeDir + '" --home "' + $dataDir + '" --port ' + $port
    $pidValue = 0
    try {
        $result = Invoke-CimMethod -ClassName Win32_Process -MethodName Create `
            -Arguments @{ CommandLine = $commandLine; CurrentDirectory = $homeDir } -ErrorAction Stop
        if ($result -and $result.ReturnValue -eq 0) { $pidValue = [int]$result.ProcessId }
    }
    catch { $pidValue = 0 }

    if (-not $pidValue) {
        $arguments = @($scriptPath, '--root', $homeDir, '--home', $dataDir, '--port', "$port")
        try {
            $proc = Start-Process -FilePath $python.WindowExe -ArgumentList $arguments `
                -WorkingDirectory $homeDir -WindowStyle Hidden -PassThru -ErrorAction Stop
            if ($proc) { $pidValue = [int]$proc.Id }
        }
        catch { }
    }

    if ($pidValue) {
        Set-Content -Path (Join-Path $dataDir ("run\server-$port.pid")) -Value $pidValue -Encoding ASCII -ErrorAction SilentlyContinue
    }
    Set-Content -Path (Join-Path $dataDir 'run\port.txt') -Value "$port" -Encoding ASCII -ErrorAction SilentlyContinue
    return @{ Started = [bool]$pidValue; Already = $false; Pid = $pidValue }
}

# ── автозапуск ──────────────────────────────────────────────────────────────
function Remove-Autostart([string]$homeDir) {
    $removed = @()
    $existing = & schtasks.exe /Query /TN $TASK_NAME /XML 2>&1 | Out-String
    if ($LASTEXITCODE -eq 0 -and $existing) {
        # Чужую задачу с тем же именем не трогаем: удаляем, только если она ведёт в нашу папку.
        $ours = $existing.IndexOf($homeDir, [System.StringComparison]::OrdinalIgnoreCase) -ge 0
        if ($ours) {
            & schtasks.exe /Delete /TN $TASK_NAME /F 2>&1 | Out-Null
            if ($LASTEXITCODE -eq 0) { $removed += ('задача ' + $TASK_NAME) }
        }
        else {
            Write-Note ('задача ' + $TASK_NAME + ' ведёт в другую папку - не трогаю')
        }
    }
    $startup = [Environment]::GetFolderPath('Startup')
    if ($startup -and (Test-Path $startup)) {
        $shell = $null
        try { $shell = New-Object -ComObject WScript.Shell } catch { $shell = $null }
        foreach ($file in @(Get-ChildItem $startup -Filter '*.lnk' -ErrorAction SilentlyContinue)) {
            if ($file.BaseName -notlike '*омандные комнаты*' -and $file.BaseName -ne $TASK_NAME) { continue }
            $target = ''
            if ($shell) {
                try { $target = [string]$shell.CreateShortcut($file.FullName).TargetPath } catch { $target = '' }
            }
            if ($target -and $target.IndexOf($homeDir, [System.StringComparison]::OrdinalIgnoreCase) -lt 0) { continue }
            Remove-Item $file.FullName -Force -ErrorAction SilentlyContinue
            $removed += ('ярлык ' + $file.Name)
        }
    }
    return $removed
}

function Set-Autostart([string]$homeDir) {
    $launcher = Join-Path $homeDir 'bin\team-room-server.cmd'
    if (-not (Test-Path $launcher)) { return @{ Ok = $false; How = ''; Note = 'нет файла bin\team-room-server.cmd' } }

    $target = '"' + $launcher + '"'
    $output = & schtasks.exe /Create /F /городе ONLOGON /TN $TASK_NAME /TR $target 2>&1
    $check = & schtasks.exe /Query /TN $TASK_NAME 2>&1
    if ($LASTEXITCODE -eq 0) {
        return @{ Ok = $true; How = 'задача планировщика ' + $TASK_NAME; Note = '' }
    }

    $reason = ''
    if ($output) {
        # Из вывода schtasks берём только суть: остальное - служебная трассировка.
        $first = ((($output | Out-String) -split "`n") | Where-Object { $_.Trim() } | Select-Object -First 1)
        $first = ([string]$first -replace 'schtasks(\.exe)?\s*:\s*', '').Trim()
        if ($first.Length -gt 140) { $first = $first.Substring(0, 140) }
        $reason = $first
    }
    Write-Warn2 ('Планировщик не принял задачу: ' + $reason)
    Write-Note 'Ставлю ярлык в папку "Автозагрузка" - работает так же.'

    try {
        $startup = [Environment]::GetFolderPath('Startup')
        if (-not (Test-Path $startup)) { New-Item -ItemType Directory -Path $startup -Force | Out-Null }
        $link = Join-Path $startup 'Командные комнаты.lnk'
        $shell = New-Object -ComObject WScript.Shell
        $shortcut = $shell.CreateShortcut($link)
        $shortcut.TargetPath = $launcher
        $shortcut.WorkingDirectory = $homeDir
        $shortcut.WindowStyle = 7
        $shortcut.Description = 'Командные комнаты - локальный сервер'
        $shortcut.Save()
        return @{ Ok = $true; How = 'ярлык в "Автозагрузке"'; Note = $reason }
    }
    catch {
        return @{ Ok = $false; How = ''; Note = 'ни задача, ни ярлык не создались: ' + $_.Exception.Message }
    }
}

# ── плагин Hermes Desktop ────────────────────────────────────────────────────
function Install-Plugin([string]$homeDir, [string]$hermesHome, [int]$port, $python) {
    $script = Join-Path $homeDir 'tools\install_desktop_plugin.py'
    if (-not (Test-Path $script)) {
        return @{ Ok = $false; Note = 'в поставке нет tools\install_desktop_plugin.py' }
    }
    if (-not (Test-Path $hermesHome)) {
        return @{ Ok = $false; Note = 'папка Hermes не найдена: ' + $hermesHome }
    }
    try {
        $output = & $python.Exe $script '--root' $hermesHome '--port' "$port" 2>&1
        $code = $LASTEXITCODE
    }
    catch {
        return @{ Ok = $false; Note = $_.Exception.Message }
    }
    if ($code -ne 0) {
        $text = (($output | Out-String) -replace '\s+', ' ').Trim()
        return @{ Ok = $false; Note = $text }
    }
    return @{ Ok = $true; Note = '' }
}

function Remove-Plugin([string]$homeDir, [string]$hermesHome, $python) {
    $script = Join-Path $homeDir 'tools\install_desktop_plugin.py'
    if ((Test-Path $script) -and $python.Exe) {
        try { & $python.Exe $script '--root' $hermesHome '--uninstall' 2>&1 | Out-Null } catch { }
    }
    $folder = Join-Path $hermesHome ('desktop-plugins\' + $PLUGIN_ID)
    if (Test-Path $folder) {
        try { Remove-Item $folder -Recurse -Force -ErrorAction Stop } catch { }
    }
    return (Test-Path $folder)
}

# ── файлы программы ────────────────────────────────────────────────────────
function Install-Files([string]$source, [string]$homeDir, [string]$dataDir) {
    $dirs = @('server', 'web', 'tools', 'desktop-plugin', 'reportkit', 'bin', 'docs', 'tests', 'fixtures')
    $files = @('README.md', 'LICENSE', 'install.ps1', 'install.sh', 'VERSION', 'CHANGELOG.md',
               'start-team-room.cmd', 'stop-team-room.cmd', 'start-demo-room.cmd')

    if (-not (Test-Path $homeDir)) { New-Item -ItemType Directory -Path $homeDir -Force | Out-Null }

    foreach ($name in $dirs) {
        $from = Join-Path $source $name
        if (-not (Test-Path $from)) { continue }
        $to = Join-Path $homeDir $name
        if (Test-Path $to) {
            # Только что остановленный сервер и антивирус держат файлы ещё секунду-другую.
            $tries = 0
            while ((Test-Path $to) -and $tries -lt 10) {
                Remove-Item $to -Recurse -Force -ErrorAction SilentlyContinue
                if (Test-Path $to) { Start-Sleep -Milliseconds 400 }
                $tries++
            }
            if (Test-Path $to) { throw ('не удалось заменить папку ' + $name + ': она занята другой программой') }
        }
        New-Item -ItemType Directory -Path $to -Force | Out-Null
        Copy-Item -Path (Join-Path $from '*') -Destination $to -Recurse -Force -ErrorAction Stop
    }
    foreach ($name in $files) {
        $from = Join-Path $source $name
        if (-not (Test-Path $from)) { continue }
        Copy-Item $from (Join-Path $homeDir $name) -Force -ErrorAction SilentlyContinue
    }

    # кэш байткода из исходной папки не нужен
    Get-ChildItem $homeDir -Directory -Recurse -Filter '__pycache__' -ErrorAction SilentlyContinue |
        ForEach-Object { Remove-Item $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }

    if (-not (Test-Path $dataDir)) {
        New-Item -ItemType Directory -Path $dataDir -Force | Out-Null
    }
    return (Test-Path (Join-Path $homeDir 'server\app.py'))
}

# ── загрузка архива ────────────────────────────────────────────────────────
function Get-Archive([string]$version, [string]$work) {
    $name = $version
    if (-not $name) {
        try {
            $api = 'https://api.github.com/repos/' + $REPO + '/releases/latest'
            $release = Invoke-RestMethod -Uri $api -UseBasicParsing -TimeoutSec 20 `
                -Headers @{ 'User-Agent' = 'team-room-installer' }
            if ($release -and $release.tag_name) { $name = [string]$release.tag_name }
        }
        catch {
            $name = ''
        }
    }
    if (-not $name) { $name = 'main' }

    if ($name -match '^(main|master)$') {
        $url = 'https://github.com/' + $REPO + '/archive/refs/heads/' + $name + '.zip'
    }
    else {
        $url = 'https://github.com/' + $REPO + '/archive/refs/tags/' + $name + '.zip'
    }

    $zip = Join-Path $work 'team-room.zip'
    Write-Host ('      ' + $url) -ForegroundColor DarkGray
    Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing -TimeoutSec 120
    Expand-Archive -Path $zip -DestinationPath $work -Force

    $roots = @(Get-ChildItem $work -Directory -ErrorAction SilentlyContinue |
        Where-Object { Test-Path (Join-Path $_.FullName 'server\app.py') })
    if ($roots.Count -eq 0) {
        $roots = @(Get-ChildItem $work -Directory -ErrorAction SilentlyContinue |
            ForEach-Object { Get-ChildItem $_.FullName -Directory -ErrorAction SilentlyContinue } |
            Where-Object { Test-Path (Join-Path $_.FullName 'server\app.py') })
    }
    if ($roots.Count -eq 0) { throw 'в архиве не нашлась папка server\app.py' }
    return @{ Path = $roots[0].FullName; Label = $name; Url = $url }
}

# ─────────────────────────────────────────────────────────────────────────────
#  Начало
# ─────────────────────────────────────────────────────────────────────────────
Write-Host ''
Write-Host '  Командные комнаты - установка' -ForegroundColor White
Write-Host ('  ' + ('-' * 46)) -ForegroundColor DarkGray

if ($env:OS -and $env:OS -ne 'Windows_NT') {
    Write-Bad 'Это установка для Windows 10/11.'
    exit 2
}

$HomeDir = Resolve-FullPath $InstallHome $env:TEAMROOM_HOME
if (-not $HomeDir) {
    $local = $env:LOCALAPPDATA
    if (-not $local) { $local = Join-Path $env:USERPROFILE 'AppData\Local' }
    $HomeDir = Resolve-FullPath '' (Join-Path $local 'TeamRoom')
}
$DataDir = Resolve-FullPath $env:TEAMROOM_DATA (Join-Path $HomeDir 'data')
$HermesHome = Get-HermesHome

$manifest = Join-Path $HomeDir 'install.json'
if (Test-Path $manifest) {
    try {
        $saved = Get-Content $manifest -Raw -Encoding UTF8 | ConvertFrom-Json
        if (-not $PSBoundParameters.ContainsKey('Port') -and $saved.port) { $Port = [int]$saved.port }
    }
    catch { }
}
if ($Port -lt 1024 -or $Port -gt 65535) {
    Write-Bad ('Такой порт не подойдёт: ' + $Port + '. Возьмите 1024-65535, обычно 8777.')
    exit 2
}

Write-Note ('программа: ' + $HomeDir)
Write-Note ('данные:    ' + $DataDir)
Write-Note ('hermes:    ' + $HermesHome)

# Зеркало комнат по умолчанию пишется рядом с данными, если общей папки
# (корень диска + Hermes) нет: на чужой машине приложение не должно её создавать.
$sharedRoot = Join-Path ([System.IO.Path]::GetPathRoot($DataDir)) 'Hermes'
if (-not $env:TEAMROOM_VAULT_DIR -and -not (Test-Path $sharedRoot)) {
    $env:TEAMROOM_VAULT_DIR = Join-Path $DataDir 'vault'
    Write-Note ('зеркало:  ' + $env:TEAMROOM_VAULT_DIR)
}

$python = Find-Python

if (-not $Uninstall) {
    Write-Step 'Проверяю окружение'
    $rows = Get-EnvReport $HermesHome $Port $DataDir $python $SourceDir
    Show-EnvTable $rows
    Show-Missing $rows

    if ($InstallDeps) {
        Write-Step 'Ставлю недостающее'
        Install-Missing $rows $python.Exe
        $python = Find-Python
        $rows = Get-EnvReport $HermesHome $Port $DataDir $python $SourceDir
    }
}

if ($DryRun) {
    Write-Host ''
    Write-Host '  Это только проверка: ничего не скачано и не изменено.' -ForegroundColor White
    Write-Note 'Уберите ключ -DryRun, и установка пойдёт по-настоящему.'
    if (@($rows | Where-Object { -not $_.Ok -and $_.Required }).Count -gt 0) {
        Write-Note 'Обязательное пока не на месте — см. список "Чего не хватает" выше.'
    }
    exit 0
}

if (-not $python.Exe) {
    Write-Host ''
    if ($Uninstall) {
        Write-Warn2 'Python не найден — удалю файлы и плагин без остановки сервера.'
    }
    else {
        Write-Bad 'Без Python 3.11 или новее дальше нельзя: на нём работает сервер.'
        Write-Host ''
        Write-Host '  Поставьте Python одной командой и запустите установку снова:' -ForegroundColor White
        Write-Host '      winget install --id Python.Python.3.12 -e' -ForegroundColor Yellow
        Write-Host ''
        Write-Note 'Или скачайте с python.org и отметьте "Add python.exe to PATH".'
        Write-Note 'Или запустите этот скрипт с ключом -InstallDeps — тогда я поставлю сам.'
        exit 2
    }
}
if ($python.Exe) { Write-Note ('python:    ' + $python.Exe + ' (' + $python.Version + ')') }

# ── удаление ────────────────────────────────────────────────────────────────
if ($Uninstall) {
    Write-Step 'Останавливаю сервер'
    $killed = Stop-TeamRoom $HomeDir $DataDir
    if ($killed -gt 0) { Write-Ok ('остановлено процессов: ' + $killed) } else { Write-Ok 'сервер не запущен' }

    Write-Step 'Убираю автозапуск'
    $removed = Remove-Autostart $HomeDir
    if ($removed.Count -gt 0) { Write-Ok ($removed -join ', ') } else { Write-Ok 'автозапуска не было' }

    Write-Step 'Убираю плагин из Hermes Desktop'
    if (Remove-Plugin $HomeDir $HermesHome $python) {
        Write-Warn2 'папка плагина осталась - закройте Hermes Desktop и повторите'
    }
    else {
        Write-Ok ('плагина больше нет: ' + (Join-Path $HermesHome ('desktop-plugins\' + $PLUGIN_ID)))
    }

    Write-Step 'Убираю файлы программы'
    if (Test-Path $HomeDir) {
        foreach ($item in @(Get-ChildItem $HomeDir -Force -ErrorAction SilentlyContinue)) {
            if ($item.Name -eq 'data' -and -not $RemoveData) { continue }
            Remove-Item $item.FullName -Recurse -Force -ErrorAction SilentlyContinue
        }
        $left = @(Get-ChildItem $HomeDir -Force -ErrorAction SilentlyContinue)
        if ($left.Count -eq 0) { Remove-Item $HomeDir -Force -ErrorAction SilentlyContinue }
    }
    Write-Ok 'программа удалена'

    Write-Host ''
    if ($RemoveData) {
        Write-Host '  Данные тоже удалены.' -ForegroundColor White
    }
    else {
        Write-Host ('  Данные сохранены: ' + $DataDir) -ForegroundColor White
        Write-Note 'Комнаты, журнал и настройки остались. Удалить их совсем: -Uninstall -RemoveData'
    }
    Write-Host ''
    Write-Host '  Готово. Строка "Командные комнаты" исчезнет из Hermes после перезагрузки плагинов' -ForegroundColor White
    Write-Host '  (Ctrl+K - Reload desktop plugins).' -ForegroundColor White
    exit 0
}

# ── 1. исходники ────────────────────────────────────────────────────────────
$work = Join-Path ([System.IO.Path]::GetTempPath()) ('team-room-install-' + [Guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Path $work -Force | Out-Null

$label = 'local'
$sourcePath = ''
try {
    if ($SourceDir) {
        Write-Step 'Беру программу из папки'
        $sourcePath = Resolve-FullPath $SourceDir ''
        if (-not (Test-Path (Join-Path $sourcePath 'server\app.py'))) {
            Write-Bad ('В папке нет server\app.py: ' + $sourcePath)
            exit 2
        }
        $label = 'local'
        $tagFile = Join-Path $sourcePath 'docs\RELEASE-TAG.txt'
        if (Test-Path $tagFile) { $label = (Get-Content $tagFile -Raw -Encoding UTF8).Trim() }
    }
    else {
        Write-Step 'Скачиваю программу с GitHub'
        $archive = Get-Archive $Version $work
        $sourcePath = $archive.Path
        $label = $archive.Label
        Write-Ok ('скачано: ' + $archive.Url)
    }
}
catch {
    Write-Bad ('Не получилось получить программу: ' + $_.Exception.Message)
    Write-Note 'Проверьте интернет и запустите установку снова.'
    Write-Note ('Папка для ручной распаковки: ' + $work)
    exit 3
}

# ── 2. останавливаю прежний сервер ──────────────────────────────────────────
Write-Step 'Останавливаю прежний сервер, если он работал'
$killed = Stop-TeamRoom $HomeDir $DataDir
if ($killed -gt 0) { Write-Ok ('остановлено процессов: ' + $killed) } else { Write-Ok 'работающего сервера нет' }

# ── 3. копирую программу ────────────────────────────────────────────────────
Write-Step 'Ставлю файлы программы'
try {
    $ok = Install-Files $sourcePath $HomeDir $DataDir
}
catch {
    Write-Bad ('Не удалось скопировать файлы: ' + $_.Exception.Message)
    Write-Note 'Если сервер ещё работает - закройте его и запустите установку снова.'
    exit 4
}
if (-not $ok) {
    Write-Bad 'После копирования нет server\app.py - установка не удалась.'
    exit 4
}
Write-Ok ('файлы на месте: ' + $HomeDir)

# ── 4. плагин ──────────────────────────────────────────────────────────────
Write-Step 'Ставлю плагин в Hermes Desktop'
$plugin = Install-Plugin $HomeDir $HermesHome $Port $python
$pluginPath = Join-Path $HermesHome ('desktop-plugins\' + $PLUGIN_ID + '\plugin.js')
if ($plugin.Ok) {
    Write-Ok ('плагин на месте: ' + $pluginPath)
}
else {
    Write-Warn2 ('плагин не поставлен: ' + $plugin.Note)
    Write-Note 'Hermes Desktop не установлен или ещё не запускался. Приложение всё равно'
    Write-Note 'откроется в браузере; плагин добавим, когда Hermes появится.'
}

# ── 5. автозапуск ───────────────────────────────────────────────────────────
$autostartHow = 'выключен'
if ($NoAutostart) {
    Write-Step 'Автозапуск не включаю (ключ -NoAutostart)'
    $removed = Remove-Autostart $HomeDir
    if ($removed.Count -gt 0) { Write-Ok ('прежний автозапуск убран: ' + ($removed -join ', ')) }
    else { Write-Ok 'прежнего автозапуска нет' }
}
else {
    Write-Step 'Включаю автозапуск при входе в Windows'
    $auto = Set-Autostart $HomeDir
    if ($auto.Ok) {
        Write-Ok $auto.How
        $autostartHow = $auto.How
    }
    else {
        Write-Warn2 ('автозапуск не получился: ' + $auto.Note)
        Write-Note ('Сервер можно запускать вручную: ' + (Join-Path $HomeDir 'bin\team-room.cmd'))
    }
}

# ── 6. запуск ───────────────────────────────────────────────────────────────
Write-Step 'Запускаю сервер'
$runDir = Join-Path $DataDir 'run'
if (-not (Test-Path $runDir)) { New-Item -ItemType Directory -Path $runDir -Force | Out-Null }
# Лаунчеры bin\*.cmd берут порт, python и каталог зеркала отсюда — чтобы автозапуск
# работал ровно так же, как установка, без повторного разбора окружения.
Set-Content -Path (Join-Path $runDir 'port.txt') -Value "$Port" -Encoding ASCII -ErrorAction SilentlyContinue
Set-Content -Path (Join-Path $runDir 'python.txt') -Value $python.Exe -Encoding ASCII -ErrorAction SilentlyContinue
if ($env:TEAMROOM_VAULT_DIR) {
    Set-Content -Path (Join-Path $runDir 'vault.txt') -Value $env:TEAMROOM_VAULT_DIR -Encoding ASCII -ErrorAction SilentlyContinue
}
$run = $null
try {
    $run = Start-TeamRoom $HomeDir $DataDir $Port $python
}
catch {
    Write-Bad ('Не удалось запустить: ' + $_.Exception.Message)
    Write-Note ('Лог: ' + (Join-Path $DataDir 'logs\server.err.log'))
}

$url = 'http://127.0.0.1:' + $Port + '/'
$alive = Wait-Health $Port $WaitSeconds
if ($alive) {
    if ($run -and $run.Already) { Write-Ok 'сервер уже работал - второй не запускал' }
    else { Write-Ok ('сервер отвечает: ' + $url) }
}
else {
    Write-Warn2 ('сервер не ответил за ' + $WaitSeconds + ' секунд')
    Write-Note ('Логи: ' + (Join-Path $DataDir 'logs'))
}

# ── 7. запись о версии ──────────────────────────────────────────────────────
$record = [ordered]@{
    version      = $label
    port         = $Port
    home         = $HomeDir
    data         = $DataDir
    python       = $python.Exe
    pythonw      = $python.WindowExe
    installed_at = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
    autostart    = $autostartHow
}
try {
    $record | ConvertTo-Json -Depth 3 | Set-Content -Path $manifest -Encoding UTF8
}
catch { }

# ── 8. страница ────────────────────────────────────────────────────────────
if (-not $NoBrowser) {
    try { Start-Process $url | Out-Null } catch { Write-Note ('Откройте вручную: ' + $url) }
}

# ── итог ────────────────────────────────────────────────────────────────────
Write-Host ''
Write-Host ('  ' + ('-' * 46)) -ForegroundColor DarkGray
if ($alive) {
    Write-Host ('  Готово. Командные комнаты: ' + $url) -ForegroundColor Green
}
else {
    Write-Host '  Файлы установлены, но сервер пока не отвечает.' -ForegroundColor Yellow
    Write-Host ('  Запустить вручную: ' + (Join-Path $HomeDir 'bin\team-room.cmd')) -ForegroundColor White
}
Write-Host ''
Write-Host '  Как открыть в Hermes Desktop:' -ForegroundColor White
Write-Host '      левое меню - "Командные комнаты"  (или Ctrl+K - "командные")' -ForegroundColor Gray
if (-not $plugin.Ok) {
    Write-Host '      плагина пока нет - откройте адрес выше в браузере' -ForegroundColor Gray
}
Write-Host ''
Write-Host ('  Программа: ' + $HomeDir) -ForegroundColor Gray
Write-Host ('  Данные:    ' + $DataDir) -ForegroundColor Gray
Write-Host ('  Автозапуск: ' + $autostartHow) -ForegroundColor Gray
Write-Host ''
Write-Host '  Обновить - запустите эту же команду снова: данные сохранятся.' -ForegroundColor White
Write-Host '  Удалить:  install.ps1 -Uninstall   (данные останутся)' -ForegroundColor White
Write-Host ''

try {
    if (Test-Path $work) { Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue }
}
catch { }

if ($alive) { exit 0 }
exit 1

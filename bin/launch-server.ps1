<#
.SYNOPSIS
    Запускает сервер «Командных комнат» скрыто и ждёт, пока он ответит.
.DESCRIPTION
    Процесс создаётся через WMI (Win32_Process.Create): у него нет ни консольного
    окна, ни унаследованных ручек — поэтому лаунчер не «висит» и ничего не мигает.
    Если сервер уже отвечает на /api/health, второй экземпляр не запускается.
    Пишет data\run\server-<порт>.pid и строки в data\logs\launcher.log.
.PARAMETER Home
    Папка приложения (по умолчанию — родитель папки bin).
.PARAMETER Port
    Порт сервера (по умолчанию из data\run\port.txt, иначе 8777).
.PARAMETER WaitSeconds
    Сколько секунд ждать ответа сервера. По умолчанию 20.
#>
[CmdletBinding()]
param(
    [Alias('Home')]
    [string]$InstallHome = '',
    [int]$Port = 0,
    [int]$WaitSeconds = 20,
    [switch]$OpenBrowser
)

$ErrorActionPreference = 'Continue'
try { if ([Console]::IsOutputRedirected) { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } } catch { }

if ($InstallHome) {
    $AppDir = [System.IO.Path]::GetFullPath($InstallHome).TrimEnd('\')
}
else {
    $AppDir = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')).TrimEnd('\')
}
$DataDir = Join-Path $AppDir 'data'
$RunDir = Join-Path $DataDir 'run'
$LogDir = Join-Path $DataDir 'logs'
foreach ($dir in @($DataDir, $RunDir, $LogDir)) {
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
}
$Log = Join-Path $LogDir 'launcher.log'

function Write-Log([string]$text) {
    try { Add-Content -Path $Log -Value (('[{0}] {1}' -f (Get-Date).ToString('yyyy-MM-dd HH:mm:ss'), $text)) -Encoding UTF8 } catch { }
}

function Read-RunFile([string]$name) {
    $path = Join-Path $RunDir $name
    if (-not (Test-Path $path)) { return '' }
    try { return ((Get-Content $path -Raw -ErrorAction Stop) + '').Trim() } catch { return '' }
}

if (-not $Port) {
    $saved = Read-RunFile 'port.txt'
    if ($saved -match '^\d+$') { $Port = [int]$saved }
}
if (-not $Port) { $Port = 8777 }

$vault = Read-RunFile 'vault.txt'
if ($vault -and -not $env:TEAMROOM_VAULT_DIR) { $env:TEAMROOM_VAULT_DIR = $vault }

function Test-Health {
    try {
        $request = [System.Net.HttpWebRequest]::Create('http://127.0.0.1:' + $Port + '/api/health')
        $request.Proxy = $null
        $request.Timeout = 1500
        $request.ReadWriteTimeout = 1500
        $response = $request.GetResponse()
        $response.Close()
        return $true
    }
    catch { return $false }
}

$python = Read-RunFile 'python.txt'
if (-not $python -or -not (Test-Path $python)) {
    $python = ''
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source) { $python = $cmd.Source }
    if (-not $python) {
        $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
        if ($launcher) {
            $guess = & $launcher.Source '-3' '-c' 'import sys;print(sys.executable)' 2>$null
            if ($guess -and (Test-Path ([string]$guess).Trim())) { $python = ([string]$guess).Trim() }
        }
    }
}
if (-not $python) {
    Write-Host 'Team Room: Python 3.11+ не найден. Поставьте Python и запустите install.ps1 снова.'
    Write-Log 'python not found'
    exit 2
}

if (Test-Health) {
    Write-Log ('already running on port ' + $Port)
    if ($OpenBrowser) { try { Start-Process ('http://127.0.0.1:' + $Port + '/') | Out-Null } catch { } }
    exit 0
}

$pyw = $python
$candidate = Join-Path (Split-Path $python) 'pythonw.exe'
if (Test-Path $candidate) { $pyw = $candidate }

$script = Join-Path $AppDir 'server\app.py'
$commandLine = '"' + $pyw + '" "' + $script + '" --root "' + $AppDir + '" --home "' + $DataDir + '" --port ' + $Port

$started = $false
try {
    $result = Invoke-CimMethod -ClassName Win32_Process -MethodName Create `
        -Arguments @{ CommandLine = $commandLine; CurrentDirectory = $AppDir } -ErrorAction Stop
    if ($result -and $result.ReturnValue -eq 0) {
        $started = $true
        Write-Log ('started pid=' + $result.ProcessId + ' port=' + $Port)
        try { Set-Content -Path (Join-Path $RunDir ('server-' + $Port + '.pid')) -Value $result.ProcessId -Encoding ASCII } catch { }
    }
    else {
        Write-Log ('WMI start failed: return=' + $result.ReturnValue)
    }
}
catch {
    Write-Log ('WMI start failed: ' + $_.Exception.Message)
}

if (-not $started) {
    try {
        $proc = Start-Process -FilePath $pyw -ArgumentList @($script, '--root', $AppDir, '--home', $DataDir, '--port', "$Port") `
            -WorkingDirectory $AppDir -WindowStyle Hidden -PassThru -ErrorAction Stop
        if ($proc) {
            $started = $true
            Write-Log ('started via Start-Process pid=' + $proc.Id)
            try { Set-Content -Path (Join-Path $RunDir ('server-' + $Port + '.pid')) -Value $proc.Id -Encoding ASCII } catch { }
        }
    }
    catch {
        Write-Log ('start failed: ' + $_.Exception.Message)
    }
}

$deadline = (Get-Date).AddSeconds($WaitSeconds)
$alive = $false
while ((Get-Date) -lt $deadline) {
    if (Test-Health) { $alive = $true; break }
    Start-Sleep -Milliseconds 400
}

if ($alive) {
    Write-Log ('server is up on http://127.0.0.1:' + $Port + '/')
    if ($OpenBrowser) { try { Start-Process ('http://127.0.0.1:' + $Port + '/') | Out-Null } catch { } }
    exit 0
}
Write-Log ('server did not answer in ' + $WaitSeconds + 's')
exit 1

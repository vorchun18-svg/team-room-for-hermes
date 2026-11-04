<#
.SYNOPSIS
    Останавливает локальный сервер «Командных комнат».
.DESCRIPTION
    Берёт pid из data\run\server-*.pid и проверяет процессы Python, у которых
    в командной строке есть папка этого приложения и server\app.py. Чужой
    процесс на том же порту не трогает.
.EXAMPLE
    bin\team-room-stop.cmd
#>
[CmdletBinding()]
param(
    [Alias('Home')]
    [string]$InstallHome = ''
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

$port = ''
$portFile = Join-Path $RunDir 'port.txt'
if (Test-Path $portFile) { $port = ((Get-Content $portFile -Raw -ErrorAction SilentlyContinue) + '').Trim() }
if (-not $port) { $port = '8777' }

function Test-Health([string]$p) {
    try {
        $request = [System.Net.HttpWebRequest]::Create('http://127.0.0.1:' + $p + '/api/health')
        $request.Proxy = $null
        $request.Timeout = 1200
        $response = $request.GetResponse()
        $response.Close()
        return $true
    }
    catch { return $false }
}

$ids = @()
$pidFiles = @()
if (Test-Path $RunDir) {
    $pidFiles = @(Get-ChildItem $RunDir -Filter '*.pid' -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -like 'server-*' })
}
foreach ($file in $pidFiles) {
    $text = ''
    try { $text = Get-Content $file.FullName -Raw -ErrorAction Stop } catch { $text = '' }
    if ($text -and ($text + '').Trim() -match '^\d+$') { $ids += [int](($text + '').Trim()) }
}

try {
    $procs = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction Stop
    foreach ($proc in $procs) {
        $line = [string]$proc.CommandLine
        if (-not $line) { continue }
        if ($line.IndexOf($AppDir, [System.StringComparison]::OrdinalIgnoreCase) -lt 0) { continue }
        if ($line -notmatch 'app\.py') { continue }
        $ids += [int]$proc.ProcessId
    }
}
catch { }

$stopped = @()
foreach ($id in ($ids | Sort-Object -Unique)) {
    $proc = Get-Process -Id $id -ErrorAction SilentlyContinue
    if (-not $proc) { continue }
    if ($proc.ProcessName -notlike 'python*') { continue }
    Stop-Process -Id $id -Force -ErrorAction SilentlyContinue
    $stopped += $id
}
foreach ($file in $pidFiles) { Remove-Item $file.FullName -Force -ErrorAction SilentlyContinue }

Start-Sleep -Milliseconds 400
$alive = Test-Health $port

if ($stopped.Count -gt 0) {
    Write-Host ('Остановлено: ' + ($stopped -join ', ')) -ForegroundColor Green
}
else {
    Write-Host 'Сервер не запущен — останавливать нечего.' -ForegroundColor Gray
}
if ($alive) {
    Write-Host ('Порт ' + $port + ' всё ещё отвечает. Похоже, там работает другой сервер — его не трогаю.') -ForegroundColor Yellow
    Write-Host 'Свои процессы можно посмотреть так: Get-CimInstance Win32_Process -Filter "Name=''python.exe''"' -ForegroundColor Gray
    exit 1
}
Write-Host 'Готово.' -ForegroundColor Green
exit 0

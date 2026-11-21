# Запуск хостовых служб ГЛаДОС (голос, «Компьютер», питомец) и подключение их к ядру в контейнере.
# Ядро в контейнере само их не запускает (agent/peers.container.yaml), поэтому после каждого пересоздания ядра их надо поднимать заново.
# Запуск:  powershell -ExecutionPolicy Bypass -File tools\start_host_services.ps1
# Токен шины берётся из .env (строка GITS_TOKEN), в окно и журналы не выводится. Журналы служб: logs\host\<имя>.log
param(
    [string[]]$Only = @('voice', 'desktop', 'pet'),
    [string]$CoreUrl = 'ws://127.0.0.1:8766/ws'
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $root '.env'
$line = Get-Content $envFile | Where-Object { $_ -match '^GITS_TOKEN=' } | Select-Object -First 1
if (-not $line) { throw "В $envFile нет строки GITS_TOKEN (токен шины ядра)." }
$token = ($line -replace '^GITS_TOKEN=', '').Trim()

$python = (Get-Command python).Source
$pythonw = Join-Path (Split-Path $python) 'pythonw.exe'
if (-not (Test-Path $pythonw)) { $pythonw = $python }
$logDir = Join-Path $root 'logs\host'
New-Item -ItemType Directory -Force $logDir | Out-Null

$specs = @{
    voice   = @{ dir = 'services\voice';      exe = $python }
    desktop = @{ dir = 'workspaces\desktop';  exe = $python }
    pet     = @{ dir = 'services\pet';        exe = $pythonw }
}

foreach ($name in $Only) {
    $spec = $specs[$name]
    if (-not $spec) { Write-Warning "Неизвестная служба «$name»"; continue }
    $env:GITS_ID = $name
    $env:GITS_TOKEN = $token
    $env:GITS_CORE_URL = $CoreUrl
    $env:PYTHONPATH = Join-Path $root 'agent\interfaces\workspace'
    $env:PYTHONUNBUFFERED = '1'
    $out = Join-Path $logDir "$name.log"
    $err = Join-Path $logDir "$name.err.log"
    Start-Process -FilePath $spec.exe -ArgumentList @('-u', 'main.py') -WorkingDirectory (Join-Path $root $spec.dir) `
        -WindowStyle Hidden -RedirectStandardOutput $out -RedirectStandardError $err
    Write-Host "Запущено: $name (журнал: $out)"
}
Remove-Item Env:\GITS_TOKEN -ErrorAction SilentlyContinue
Write-Host 'Готово. Через несколько секунд службы появятся во вкладке «Сервисы» (кнопка ↻).'

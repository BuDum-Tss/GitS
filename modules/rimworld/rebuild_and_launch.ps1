# rebuild_and_launch.ps1
# Собирает AgentBridge мод и перезапускает RimWorld

$MOD_DIR   = Join-Path $PSScriptRoot "mod\AgentBridge"      # рядом со скриптом, где бы ни лежал репозиторий
$RIMWORLD  = "C:\GOG Games\RimWorld\RimWorldWin64.exe"

Write-Host "[1/3] Сборка мода..." -ForegroundColor Cyan
dotnet build "$MOD_DIR" -c Release --nologo -v q
if ($LASTEXITCODE -ne 0) {
    Write-Host "ОШИБКА сборки. Перезапуск отменён." -ForegroundColor Red
    exit 1
}
Write-Host "     OK" -ForegroundColor Green

# Правило 14: скрипт запускает игру, объявляем через ядро (тихо, если ядра нет: как game_launcher из скриптов)
try {
    $fact = "Ты сама делаешь это на ПК (не пользователь): перезапускаю RimWorld скриптом rebuild_and_launch: собран новый мод, игру закрываю и запускаю заново"
    $body = [Text.Encoding]::UTF8.GetBytes((@{ text = $fact; source = "rebuild_and_launch" } | ConvertTo-Json -Compress))
    Invoke-RestMethod -Uri "http://localhost:8766/api/notify" -Method Post -Body $body `
        -ContentType "application/json; charset=utf-8" -TimeoutSec 2 | Out-Null
} catch { }

Write-Host "[2/3] Закрываем RimWorld..." -ForegroundColor Cyan
$proc = Get-Process -Name "RimWorldWin64" -ErrorAction SilentlyContinue
if ($proc) {
    $proc | Stop-Process -Force
    Start-Sleep -Seconds 3
    Write-Host "     Закрыт" -ForegroundColor Green
} else {
    Write-Host "     Не был запущен" -ForegroundColor Yellow
}

Write-Host "[3/3] Запускаем RimWorld..." -ForegroundColor Cyan
Start-Process $RIMWORLD
Write-Host "     Готово! Новый AgentBridge.dll загрузится при старте." -ForegroundColor Green

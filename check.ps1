# Проверка перед коммитом: линтер и тесты.  Запуск: .\check.ps1
$ErrorActionPreference = "Continue"
Write-Host "== ruff ==" -ForegroundColor Cyan
python -m ruff check agent sdk services modules
$lint = $LASTEXITCODE
Write-Host "== pytest ==" -ForegroundColor Cyan
python -m pytest -q
$tests = $LASTEXITCODE
Write-Host "== pytest sdk (glados_module) ==" -ForegroundColor Cyan
python -m pytest -q sdk/tests
$sdktests = $LASTEXITCODE
# тесты каждого модуля идут отдельным запуском: у модулей одинаковые имена файлов (app.py, manifest.py, common.py)
Write-Host "== pytest modules/telegram (модуль Telegram) ==" -ForegroundColor Cyan
python -m pytest -q modules/telegram/tests
$modtests = $LASTEXITCODE
Write-Host "== pytest modules/voice (модуль голоса) ==" -ForegroundColor Cyan
python -m pytest -q modules/voice/tests
$voicetests = $LASTEXITCODE
if ($lint -ne 0 -or $tests -ne 0 -or $sdktests -ne 0 -or $modtests -ne 0 -or $voicetests -ne 0) { Write-Host "Есть замечания: линтер=$lint, тесты=$tests, тесты sdk=$sdktests, тесты telegram=$modtests, тесты voice=$voicetests" -ForegroundColor Yellow; exit 1 }
Write-Host "Всё в порядке" -ForegroundColor Green

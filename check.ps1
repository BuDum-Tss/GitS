# Проверка перед коммитом: линтер и тесты.  Запуск: .\check.ps1
$ErrorActionPreference = "Continue"
Write-Host "== ruff ==" -ForegroundColor Cyan
python -m ruff check agent sdk services
$lint = $LASTEXITCODE
Write-Host "== pytest ==" -ForegroundColor Cyan
python -m pytest -q
$tests = $LASTEXITCODE
Write-Host "== pytest sdk (glados_module) ==" -ForegroundColor Cyan
python -m pytest -q sdk/tests
$sdktests = $LASTEXITCODE
if ($lint -ne 0 -or $tests -ne 0 -or $sdktests -ne 0) { Write-Host "Есть замечания: линтер=$lint, тесты=$tests, тесты sdk=$sdktests" -ForegroundColor Yellow; exit 1 }
Write-Host "Всё в порядке" -ForegroundColor Green

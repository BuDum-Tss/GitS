# Проверка перед коммитом: линтер и тесты.  Запуск: .\check.ps1
# По умолчанию пропускаются медленные тесты (настоящий ffmpeg, slow, около трёх минут) и тесты с настоящей RimWorld (game: игра
# запускается и закрывается, пользователь слышит объявление); полный прогон: .\check.ps1 -All
param([switch]$All)
$ErrorActionPreference = "Continue"
Write-Host "== ruff ==" -ForegroundColor Cyan
python -m ruff check agent
$lint = $LASTEXITCODE
Write-Host "== pytest ==" -ForegroundColor Cyan
if ($All) { python -m pytest -q } else { python -m pytest -q -m "not slow and not game" }
$tests = $LASTEXITCODE
if ($lint -ne 0 -or $tests -ne 0) { Write-Host "Есть замечания: линтер=$lint, тесты=$tests" -ForegroundColor Yellow; exit 1 }
Write-Host "Всё в порядке" -ForegroundColor Green

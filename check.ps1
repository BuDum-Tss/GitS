# Проверка перед коммитом: линтер и тесты.  Запуск: .\check.ps1
$ErrorActionPreference = "Continue"
Write-Host "== ruff ==" -ForegroundColor Cyan
python -m ruff check agent workspaces services tools test
$lint = $LASTEXITCODE
Write-Host "== pytest ==" -ForegroundColor Cyan
python -m pytest -q
$tests = $LASTEXITCODE
Write-Host "== pytest sdk (gits_sdk) ==" -ForegroundColor Cyan
python -m pytest -q agent/interfaces/workspace/tests
$sdktests = $LASTEXITCODE
Write-Host "== pytest agent/interfaces/workspace/template (шаблон модуля) ==" -ForegroundColor Cyan
python -m pytest -q agent/interfaces/workspace/template/tests
if ($LASTEXITCODE -ne 0) { $sdktests = $LASTEXITCODE }
# тесты каждого модуля идут отдельным запуском: у модулей одинаковые имена файлов (app.py, manifest.py, common.py)
Write-Host "== pytest workspaces/telegram (модуль Telegram) ==" -ForegroundColor Cyan
python -m pytest -q workspaces/telegram/tests
$modtests = $LASTEXITCODE
Write-Host "== pytest workspaces/discord (окружение Discord) ==" -ForegroundColor Cyan
python -m pytest -q workspaces/discord/tests
$dctests = $LASTEXITCODE
Write-Host "== pytest workspaces/streamer (окружение «Стример»: Twitch и OBS) ==" -ForegroundColor Cyan
python -m pytest -q workspaces/streamer/tests
$twitchtests = $LASTEXITCODE
Write-Host "== pytest services/voice (модуль голоса) ==" -ForegroundColor Cyan
python -m pytest -q services/voice/tests
$voicetests = $LASTEXITCODE
Write-Host "== pytest services/pet (модуль питомца) ==" -ForegroundColor Cyan
python -m pytest -q services/pet/tests
$pettests = $LASTEXITCODE
Write-Host "== pytest workspaces/desktop (модуль компьютера) ==" -ForegroundColor Cyan
python -m pytest -q workspaces/desktop/tests
$desktoptests = $LASTEXITCODE
Write-Host "== pytest workspaces/rimworld (модуль RimWorld) ==" -ForegroundColor Cyan
Push-Location workspaces/rimworld
python -m pytest -q -m "not slow and not game"
$rimtests = $LASTEXITCODE
Pop-Location
Write-Host "== pytest workspaces/montage (модуль монтажа; полная проверка с Remotion: workspaces/montage/check.ps1) ==" -ForegroundColor Cyan
Push-Location workspaces/montage
$montagepy = if (Test-Path .venv\Scripts\python.exe) { ".venv\Scripts\python.exe" } else { "python" }
& $montagepy -m pytest -q
$montagetests = $LASTEXITCODE
Pop-Location
if ($lint -ne 0 -or $tests -ne 0 -or $sdktests -ne 0 -or $modtests -ne 0 -or $dctests -ne 0 -or $twitchtests -ne 0 -or $voicetests -ne 0 -or $pettests -ne 0 -or $desktoptests -ne 0 -or $rimtests -ne 0 -or $montagetests -ne 0) { Write-Host "Есть замечания: линтер=$lint, тесты=$tests, тесты sdk=$sdktests, тесты telegram=$modtests, тесты twitch=$twitchtests, тесты voice=$voicetests, тесты pet=$pettests, тесты desktop=$desktoptests, тесты rimworld=$rimtests, тесты montage=$montagetests" -ForegroundColor Yellow; exit 1 }
Write-Host "Всё в порядке" -ForegroundColor Green


# Проверка перед коммитом: линтер, тесты Python, типы и композиции Remotion.  Запуск: .\check.ps1
# Параметр -Render дополнительно рендерит короткое демо (долго, нужен Chromium Remotion): .\check.ps1 -Render
param([switch]$Render)
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

Write-Host "== ruff ==" -ForegroundColor Cyan
& $py -m ruff check agent ..\..\resources\GlaDOS\montage\assets
esources\GlaDOS\montagessets
$lint = $LASTEXITCODE

Write-Host "== pytest ==" -ForegroundColor Cyan
& $py -m pytest -q
$tests = $LASTEXITCODE

Push-Location (Join-Path $root "template")
Write-Host "== tsc (template) ==" -ForegroundColor Cyan
npx tsc --noEmit
$tsc = $LASTEXITCODE

Write-Host "== remotion compositions (template) ==" -ForegroundColor Cyan
npx remotion compositions src/index.ts
$comps = $LASTEXITCODE

$renderCode = 0
if ($Render) {
    Write-Host "== remotion render demo (vertical) ==" -ForegroundColor Cyan
    New-Item -ItemType Directory -Force -Path "demo/out" | Out-Null
    node demo/make-demo-media.mjs
    npx remotion render src/index.ts Main demo/out/demo_vertical_short.mp4 --props=demo/plan.vertical.short.json --scale=0.5 --crf=28
    $renderCode = $LASTEXITCODE
}
Pop-Location

if ($lint -ne 0 -or $tests -ne 0 -or $tsc -ne 0 -or $comps -ne 0 -or $renderCode -ne 0) {
    Write-Host "Есть замечания: линтер=$lint, тесты=$tests, tsc=$tsc, composition=$comps, render=$renderCode" -ForegroundColor Yellow
    exit 1
}
Write-Host "Всё в порядке" -ForegroundColor Green

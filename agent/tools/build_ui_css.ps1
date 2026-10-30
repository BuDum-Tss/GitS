# Собирает agent/static/ui/vendor/tailwind.css из классов, которые встречаются в index.html и architecture.js.
# Нужен Node.js и доступ к npm. Запуск: .\agent\tools\build_ui_css.ps1  (после правки классов в интерфейсе)
$ui = Join-Path $PSScriptRoot "..\static\ui" | Resolve-Path
$tmp = Join-Path $env:TEMP "gits_tw"
New-Item -ItemType Directory -Force $tmp | Out-Null
$cfg = @"
module.exports = { content: ["$($ui.Path.Replace('\','/'))/index.html", "$($ui.Path.Replace('\','/'))/architecture.js"], theme: { extend: {} }, plugins: [] };
"@
Set-Content -Path (Join-Path $tmp "tailwind.config.js") -Value $cfg -Encoding utf8
Set-Content -Path (Join-Path $tmp "in.css") -Value "@tailwind base;`n@tailwind components;`n@tailwind utilities;" -Encoding utf8
Push-Location $tmp
npx --yes tailwindcss@3.4.17 -c tailwind.config.js -i in.css -o (Join-Path $ui.Path "vendor\tailwind.css") --minify
Pop-Location

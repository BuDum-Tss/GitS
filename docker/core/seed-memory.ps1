<#
Кладёт память персонажа в именованный том Docker (gits-<id>-memory) для контейнера ядра, или выгружает её обратно.

  Загрузка:  .\docker\core\seed-memory.ps1 -CharacterId glados
  Выгрузка:  .\docker\core\seed-memory.ps1 -CharacterId glados -Export D:\backup\glados-memory

Загрузка: `python -m tools.character_home export` собирает память во временную папку (без журналов, кэшей и WAL, базы SQLite через
backup API), потом временный контейнер копирует её в том. Источник не меняется. Если в томе уже есть файлы, скрипт отказывает
(-Force: перезаписать совпадающие файлы, лишние остаются). Ядро на время копирования лучше остановить.
Выгрузка копирует память из тома в папку на хосте (ядро остановить, иначе базы SQLite могут оказаться на середине записи).
Содержимое файлов не печатается.
#>
[CmdletBinding()]
param(
    [string]$CharacterId = "glados",
    [string]$Volume = "",                       # по умолчанию gits-<CharacterId>-memory
    [string]$Image = "gits-core:dev",           # образ для временного контейнера (нужен только sh и cp)
    [string]$MemoryRoot = "",                   # корень памяти на хосте (по умолчанию agent\memory репозитория или MEMORY_DIR)
    [string]$ResourcesRoot = "",
    [string]$Export = "",                       # папка на хосте: выгрузить память из тома туда
    [switch]$Force
)
$ErrorActionPreference = "Stop"
if (-not $Volume) { $Volume = "gits-$CharacterId-memory" }
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path

function Invoke-Docker {
    & docker @args
    if ($LASTEXITCODE -ne 0) { throw "docker $($args -join ' ') завершился с кодом $LASTEXITCODE" }
}

if ($Export) {
    $target = New-Item -ItemType Directory -Force -Path $Export
    Invoke-Docker run --rm --user 0 -v "${Volume}:/src:ro" -v "$($target.FullName):/out" --entrypoint sh $Image -c "cp -a /src/. /out/"
    Write-Host "Память из тома $Volume выгружена в $($target.FullName)"
    return
}

$tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("gits-seed-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
New-Item -ItemType Directory -Path $tmp | Out-Null
try {
    $exportArgs = @("-m", "tools.character_home", "export", $CharacterId, $tmp)
    if ($MemoryRoot) { $exportArgs += @("--memory-root", $MemoryRoot) }
    if ($ResourcesRoot) { $exportArgs += @("--resources-root", $ResourcesRoot) }
    Push-Location (Join-Path $repo "agent")
    try { & python @exportArgs; if ($LASTEXITCODE -ne 0) { throw "tools.character_home export не удался" } } finally { Pop-Location }
    $mem = Join-Path $tmp "memory"
    if (-not (Test-Path $mem)) { throw "у персонажа $CharacterId нет памяти для переноса (нет папки memory в собранном томе)" }

    # том создаётся при первом использовании; проверяем, пуст ли он
    $existing = (& docker run --rm -v "${Volume}:/dst:ro" --entrypoint sh $Image -c "ls -A /dst | head -n 1")
    if ($LASTEXITCODE -ne 0) { throw "не удалось проверить том $Volume" }
    if ($existing -and -not $Force) { throw "том $Volume уже не пуст; -Force перезапишет совпадающие файлы" }

    Invoke-Docker run --rm --user 0 -v "${Volume}:/dst" -v "${mem}:/src:ro" --entrypoint sh $Image -c "cp -a /src/. /dst/ && chown -R 1000:1000 /dst"
    Write-Host "Память $CharacterId перенесена в том $Volume"
} finally {
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
}

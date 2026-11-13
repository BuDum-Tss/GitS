# diagnose.ps1 — единый отчёт о состоянии RimWorld + GLaDOS-агента.
# Запуск: .\diagnose.ps1              — полный отчёт
#         .\diagnose.ps1 -ErrorsOnly  — только ошибки (быстрая проверка "всё ли ок")
#         .\diagnose.ps1 -Tail 500    — глубина чтения лога агента (по умолчанию 300 строк)

param(
    [switch]$ErrorsOnly,
    [int]$Tail = 300
)

$ErrorActionPreference = "SilentlyContinue"
$AGENT_DIR  = Join-Path $PSScriptRoot "agent"      # рядом со скриптом, где бы ни лежал репозиторий
$MAIN_LOG   = "$AGENT_DIR\_main.err.log"
$PET_LOG    = "$AGENT_DIR\_pet.err.log"
$PLAYER_LOG = "$env:USERPROFILE\AppData\LocalLow\Ludeon Studios\RimWorld by Ludeon Studios\Player.log"
$MEMORY_DIR = "$AGENT_DIR\memory"

function Section($title) {
    Write-Host "`n=== $title ===" -ForegroundColor Cyan
}

# ── Процессы ──────────────────────────────────────────────────────────────────
if (-not $ErrorsOnly) {
    Section "ПРОЦЕССЫ"
    $rw = Get-Process -Name "RimWorldWin64" -ErrorAction SilentlyContinue
    Write-Host "RimWorld:       $(if ($rw) { "жив (PID $($rw.Id), с $($rw.StartTime))" } else { "НЕ ЗАПУЩЕНА" })"

    $mainProc = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*main.py*" }
    Write-Host "main.py:        $(if ($mainProc) { "жив (PID $($mainProc.ProcessId))" } else { "НЕ ЗАПУЩЕН" })"

    $petProc = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*desktop_pet*" }
    Write-Host "desktop_pet.py: $(if ($petProc) { "жив (PID $($petProc.ProcessId))" } else { "НЕ ЗАПУЩЕН" })"
}

# ── Состояние игры через мод ──────────────────────────────────────────────────
Section "СОСТОЯНИЕ ИГРЫ (мод HTTP)"
try {
    $state = Invoke-RestMethod "http://localhost:8765/state" -TimeoutSec 3
    if ($state.status) {
        Write-Host "Статус: $($state.status)" -ForegroundColor Yellow
    } else {
        Write-Host "Тик: $($state.tick)  Колония: $($state.colony.name)  Сейв: $($state.save_id)  Пауза: $($state.paused)  Скорость: $($state.time_speed)"
        Write-Host "Погода: $($state.weather)  Год: $($state.year)  Сезон: $($state.season)"
        if ($state.colony.pawns) {
            Write-Host "Пешки:"
            foreach ($p in $state.colony.pawns) {
                Write-Host ("  {0,-12} настроение={1,4:P0} здоровье={2,4:P0} работа={3}" -f $p.name, $p.mood_pct, $p.health_pct, $p.current_job)
            }
        }
        if ($state.threats -and $state.threats.Count -gt 0) {
            Write-Host "УГРОЗЫ: $($state.threats.Count)" -ForegroundColor Red
        }
        if ($state.colony.forbidden_items -and $state.colony.forbidden_items.Count -gt 0) {
            Write-Host "Запрещённые предметы: $($state.colony.forbidden_items.Count)" -ForegroundColor Yellow
        }
    }
} catch {
    Write-Host "МОД НЕ ОТВЕЧАЕТ на http://localhost:8765 — $_" -ForegroundColor Red
}

# ── Последние решения GLaDOS ──────────────────────────────────────────────────
if (-not $ErrorsOnly) {
    Section "ПОСЛЕДНИЕ РЕШЕНИЯ (лог агента, глубина $Tail)"
    Get-Content $MAIN_LOG -Tail $Tail |
        Select-String '\[GLaDOS\]|\[ACTIONS\]|\[PLANNING\]|\[LONG-TERM\]|\[REFLECTION\]|повтор подавлен|GLaDOS-меню' |
        Select-Object -Last 25 |
        ForEach-Object { Write-Host $_.Line }
}

# ── Ошибки агента (Python) ────────────────────────────────────────────────────
Section "ОШИБКИ АГЕНТА (Python)"
$agentErrors = Get-Content $MAIN_LOG -Tail 1000 |
    Select-String 'Traceback|Ошибка тика|Ошибка планирования|Ошибка рефлексии|Ошибка долгосрочного|ERROR'
if ($agentErrors) {
    $agentErrors | Select-Object -Last 20 | ForEach-Object { Write-Host $_.Line -ForegroundColor Red }
} else {
    Write-Host "Нет ошибок в последних 1000 строках." -ForegroundColor Green
}

# ── Ошибки мода (RimWorld Player.log) ─────────────────────────────────────────
Section "ОШИБКИ МОДА (RimWorld Player.log)"
if (Test-Path $PLAYER_LOG) {
    $modErrors = Get-Content $PLAYER_LOG -Tail 3000 | Select-String '\[AgentBridge\].*[Ee]rror'
    if ($modErrors) {
        $modErrors | Select-Object -Last 15 | ForEach-Object { Write-Host $_.Line -ForegroundColor Red }
    } else {
        Write-Host "Нет ошибок мода в последних 3000 строках Player.log." -ForegroundColor Green
    }
} else {
    Write-Host "Player.log не найден: $PLAYER_LOG" -ForegroundColor Yellow
}

# ── Цели / задачи / уроки ─────────────────────────────────────────────────────
if (-not $ErrorsOnly) {
    Section "АКТИВНЫЕ ЦЕЛИ (goals.json)"
    $goalsPath = "$MEMORY_DIR\goals.json"
    if (Test-Path $goalsPath) {
        try {
            $goals = Get-Content $goalsPath -Raw | ConvertFrom-Json
            $active = $goals | Where-Object { $_.status -eq "active" }
            if ($active) {
                foreach ($g in $active) {
                    Write-Host "[$($g.category)] $($g.title) — $($g.progress_pct)%  ($($g.success_criteria))"
                }
            } else {
                Write-Host "Нет активных целей."
            }
        } catch { Write-Host "Не удалось прочитать goals.json: $_" -ForegroundColor Yellow }
    }

    Section "ТЕКУЩИЕ ЗАДАЧИ (tasks.json)"
    $tasksPath = "$MEMORY_DIR\tasks.json"
    if (Test-Path $tasksPath) {
        try {
            $tasks = Get-Content $tasksPath -Raw | ConvertFrom-Json
            $pending = $tasks | Where-Object { $_.status -ne "done" }
            if ($pending) {
                foreach ($t in $pending) {
                    Write-Host "[$($t.status)] $($t.description)"
                }
            } else {
                Write-Host "Нет активных задач."
            }
        } catch { Write-Host "Не удалось прочитать tasks.json: $_" -ForegroundColor Yellow }
    }

    Section "ПОСЛЕДНИЕ УРОКИ (lessons.json)"
    $lessonsPath = "$MEMORY_DIR\lessons.json"
    if (Test-Path $lessonsPath) {
        try {
            $lessons = Get-Content $lessonsPath -Raw | ConvertFrom-Json
            $lessons | Select-Object -Last 5 | ForEach-Object { Write-Host "- $_" }
        } catch { Write-Host "Не удалось прочитать lessons.json: $_" -ForegroundColor Yellow }
    }
}

Write-Host "`n=== ГОТОВО ===" -ForegroundColor Cyan

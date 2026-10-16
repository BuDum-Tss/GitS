# montage-module

Модуль монтажа ГЛаДОС (`module_id: montage`): превращает исходники (ролик с человеком, запись забега RimWorld, её
закадр поверх графики, любые файлы) в готовый ролик в стиле Aperture Science. Работает по контракту модулей ядра GitS
(`C:\MyProjects\GitS\docs\tz\01-module-contract.md`), подключается к нему сам по WebSocket. Монтажом занимается агент на
Claude Agent SDK со скиллами Remotion; сборка и рендер идут в Remotion (`template/`).

Постановка: `C:\MyProjects\GitS\docs\tz\20-montage-module.md`, `21-aperture-style.md`, `22-editing-craft.md`.
Правила для агентов-исполнителей: `AGENTS.md`. История изменений: `CHANGELOG.md`.

## Состояние

Шаг 2 из ТЗ 20 готов: конвейер со стадиями (включая `story`), очередь задач, агент Claude со своими инструментами, песочница,
восстановление, пауза по батарейке, отмена, уборка, субтитры из журнала речи, контроль качества и режимы приёмки. Не сделано по
решению владельца: распознавание живой речи (WhisperX, MON-25…MON-27, MON-29, MON-38). Запуск: ядро GitS стартует модуль само
(`agent/modules.yaml`, переменная `MONTAGE_MODULE_DIR`); вручную `.venv\Scripts\python.exe agent\main.py` при работающем ядре.

## Установка

Всё ставится в папку репозитория; глобальные пакеты не нужны.

```powershell
# 1. Python-окружение модуля (Python 3.12: у бинарных пакетов будущих стадий, например RapidOCR, есть колёса)
C:\Users\coffe\AppData\Local\Python\pythoncore-3.12-64\python.exe -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r dev-requirements.txt
.\.venv\Scripts\python.exe -m pip install -e C:\MyProjects\GitS\sdk     # SDK модулей (пакет gits_sdk)

# 2. Шаблон Remotion (Node >= 18; проверено на v24.19.0). Первый запуск Remotion сам скачивает headless Chromium
cd template
npm ci
```

Первый запуск Remotion скачивает headless Chromium (около 120 МБ) в `template/node_modules/.remotion`; при обрыве сети команда
просто повторяется. Нужен ffmpeg в `PATH` (проверено: 9.0.1, сборка gyan.dev); функция поиска `agent/media.py: find_ffmpeg` та же, что в GitS.

Проверка: `.\check.ps1` (ruff, pytest, `npx tsc --noEmit`, `npx remotion compositions`). `.\check.ps1 -Render` дополнительно
рендерит короткое демо.

## Версии

| Что | Версия |
|---|---|
| Remotion (`remotion` и все `@remotion/*`) | 4.0.534, точная, без `^` |
| zod | 4.5.4 (ту версию, что требует `@remotion/studio` 4.0.534) |
| React / React DOM | 19.2.8 |
| TypeScript | 5.9.3 |
| Node.js / npm | v24.19.0 / 11.17.0 (минимум 18) |
| claude-agent-sdk (Python) | 0.2.157, как в GitS |
| Python окружения `.venv` | 3.12.10 |

Версия Remotion совпадает с версией скиллов (`4.0.534`, см. ниже). Обновление: `npx remotion upgrade` в `template/`
плюс обновление скиллов отдельной задачей (`AGENTS.md`: шаблон и скиллы меняются отдельно).

## Шаблон Remotion

`template/` содержит композицию `Main`, которая берёт размер, частоту кадров и длительность из входных свойств
(`plan.json`), пять сцен дизайн-системы v2 (ТЗ 23: интро, схема-чертёж, комната ГЛаДОС, отбивки-таблички, рамка HUD; субтитры только в вертикали) и схему `src/schema.ts` (zod). Компоненты, токены стиля, демо и команды рендера:
`docs/PIPELINE.md`. Демо-медиа (`npm run demo:media`) делает ffmpeg. Демо-планы для вертикали, горизонтали и квадрата: `template/demo/plan.*.json`; результаты рендера и
кадры складываются в `template/demo/out/` (в `.gitignore`).

Ассеты сцен лежат в `template/public/`: фон комнаты `scenes/glados-room.webp`, шесть эмоций питомца `pet/<эмоция>.png`, рамка HUD `hud/hud-frame.svg`. **Нужна версия питомца от 1000 px по высоте:** текущие стикеры 145×200 px и при показе на 42-62 % высоты кадра мылятся. Фон комнаты и надпись «APERTURE SCIENCE» в интро содержат знак Aperture (решение владельца; для публичных роликов права Valve взвесить отдельно).

## Скиллы Remotion (`.claude/skills/`)

Закреплённые копии 12 скиллов `remotion-*` из репозитория `remotion-dev/skills` (версия скиллов 4.0.534), установленных
в `C:\Users\coffe\.agents\skills` (ссылки из `C:\Users\coffe\.claude\skills`) 2026-10-09 по локальному времени
(в `.skill-lock.json`: 2026-10-08 23:34 UTC). Скопированы в этот репозиторий 2026-10-11 без изменений, с разыменованием
ссылок. Перед копированием прочитаны все `SKILL.md` и проверены файлы со скриптами; перечень исполняемого и
рекомендации по ним: `docs/SKILLS_AUDIT.md`. Обновлять скиллы только отдельной задачей.

## Шрифты и звуки

- `resources/<Имя>/montage/assets/fonts/`: IBM Plex, лицензия SIL OFL 1.1 (`OFL.txt`), источник Google Fonts (репозиторий `google/fonts`).
  Подробности, включая важную находку про кириллицу в статичной Plex Sans Condensed: `resources/<Имя>/montage/assets/fonts/README.md`.
- `resources/<Имя>/montage/assets/sfx/`: звуки синтезирует `resources/<Имя>/montage/assets/sfx/generate.py` (numpy + wave), без сэмплов.
- `resources/<Имя>/montage/assets/pictograms/`: 12 своих SVG-пиктограмм, не копии из игр Valve.

Права: Portal, Aperture Science и ГЛаДОС принадлежат Valve; здесь стилизация по мотивам без их материалов.

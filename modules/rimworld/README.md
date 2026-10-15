# rimworld-module

Модуль RimWorld для ядра ГЛаДОС ([GitS](../GitS)). Он играет в RimWorld: мод `mod/AgentBridge` (C#, Harmony), MCP-сервер
`agent/rimworld_mcp.py`, игровые контуры на Claude (ход, планирование, долгосрочные цели, рефлексия), режимы (наблюдает /
действует / планирует / тактика), журнал действий и задачи с подзадачами, комментатор для стрима, запись забегов на видео и
оверлей для OBS.

(Репозиторий раньше назывался `rimworld-agent`. Папка на диске может ещё называться так; ядро находит её по `RIMWORLD_MODULE_DIR`.)

Разговор, голос, питомец, Telegram и память-граф живут в ядре GitS. С ТЗ 11 этот процесс подключается к ядру как модуль `rimworld`
по контракту модулей (WebSocket `ws://127.0.0.1:8766/module/ws`, [руководство автора модулей](../GitS/docs/MODULES.md)):

| Направление | Как |
|---|---|
| модуль -> ядро | манифест (инструменты MCP-сервера и `order`), `state` раз в 5 с (`game_running`), события `rimworld.*` (набег, гибель, пожар, письмо, цель, итог забега, видео), реплики `say`, эмоции, расход Claude (`core.claude_usage`) |
| ядро -> модуль | вызовы инструментов (`tool.call`), приказ игрока (`order`), подграф для дашборда (`graph.get`), темы `presence`, `settings`, `profile`, `module` |
| пульт и OBS | `http://localhost:8767`: `/overlay`, `/api/state`, `/api/game`, `/api/graph`, `/api/appraisal_label`; мостики MCP-сервера к ядру `/api/core/tool`, `/api/core/web_search` |

Модуль запускает супервизор ядра (`GitS/agent/modules.yaml`, запись `rimworld`), включение и выключение в дашборде ядра (вкладка
«Модули»). Без ядра игровые контуры работают, но модуль молчит (реплики остаются в журнале событий). Общие настройки (режим
стримера, «не отвлекать», модель игрока, запись забегов, пост-итог в Telegram) приходят от ядра.

## Установка
Нужен Python 3.12+ (рабочий вариант: общий Python, которым запускается и ядро).
```powershell
pip install -r agent/requirements.txt
pip install -e C:\MyProjects\GitS\sdk      # пакет gits-sdk: контракт «ядро-модуль» (путь к GitS свой)
```
Отдельное виртуальное окружение модулю не нужно: ядро запускает его своим интерпретатором (`{PYTHON}` в `modules.yaml`). Если SDK не
установлен, `agent/main.py` ищет его в `GITS_SDK_DIR` или в соседней папке `GitS/sdk`, а супервизор добавляет `GitS/sdk` в `PYTHONPATH`.

## Запуск
Обычно ничего запускать не нужно: модуль стартует вместе с ядром. Вручную (ядро GitS при этом должно работать; токен берётся из файла ядра):
```powershell
cd agent
python main.py                  # игровые контуры + API на http://localhost:8767
# оверлей для OBS (источник «Браузер»): http://localhost:8767/overlay
```
Мод: `.\rebuild_and_launch.ps1` (собирает AgentBridge и перезапускает RimWorld). План мода: `docs/MOD_ROADMAP.md`.

Переменные окружения: `RIMWORLD_MODULE_DIR` (для ядра GitS: где лежит этот репозиторий; старое имя `RIMWORLD_AGENT_DIR` читается
как запасное одну версию), `RIMWORLD_MODULE_PORT` (порт API, по умолчанию 8767; старое `RIMWORLD_AGENT_PORT` запасное),
`RIMWORLD_MEMORY_DIR` (данные модуля, по умолчанию `agent/memory`), `GITS_CORE_URL`, `GITS_MODULE_TOKEN` (передаёт супервизор).

## Проверка
```powershell
.\check.ps1          # ruff и тесты (без медленных тестов с настоящим ffmpeg)
.\check.ps1 -All     # полный прогон, включая медленные
```

Правила работы и карта модулей: [AGENTS.md](AGENTS.md). Изменения: [CHANGELOG.md](CHANGELOG.md).

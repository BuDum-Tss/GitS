# GitS: ГЛаДОС, ассистентка на компьютере

Всегда включённая ассистентка с характером GLaDOS: разговор, зрение (экран), слух (системный звук и микрофон),
голос (Fish Speech, запасной edge-tts), Telegram (канал-дневник и её собственный аккаунт), память-граф (Graphiti +
FalkorDB), окружения с инструментами (управление ПК, приложения, MCP, вебхуки), питомец на рабочем столе, трей.

Игры подключаются отдельными агентами. Первый из них: [rimworld-agent](../rimworld-agent) (мод, MCP-сервер, игровые
контуры на Claude). Они общаются по localhost:

| Направление | Как |
|---|---|
| ассистент -> игра | приказы `POST :8767/api/order`, состояние `GET :8767/api/game` (`agent/game_bridge.py`) |
| игра -> ассистент | реплики `POST :8766/api/say`, эмоции `/api/emotion`, события `/api/event`, опрос `GET /api/speech_status` |
| MCP | окружение «RimWorld» (`environments/manifests/rimworld.json`) запускает `rimworld_mcp.py` из репозитория агента игры (`RIMWORLD_AGENT_DIR`) |

Без агента игры ассистентка работает как раньше, без игровой части.

## Запуск
```powershell
cd agent
pip install -r requirements.txt
copy secrets.env.example secrets.env     # токены Telegram и т.д. (не в git)
python main.py                           # ядро, дашборд на http://localhost:8766
pythonw desktop_pet.py                   # питомец
pythonw tray.py                          # трей: сам поднимает ядро, питомца и агента RimWorld (если он рядом)
```
Docker (необязательно): `docker compose up -d` поднимает граф памяти (FalkorDB), Fish Speech и WhisperX.
Локальная модель: LM Studio (`google/gemma-4-e4b` и эмбеддинги EmbeddingGemma).

Правила работы и карта модулей: [AGENTS.md](AGENTS.md). Изменения: [CHANGELOG.md](CHANGELOG.md).

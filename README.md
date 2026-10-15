# GitS: ГЛаДОС, ассистентка на компьютере

Всегда включённая ассистентка с характером GLaDOS: разговор, зрение (экран), голос и слух (модуль `modules/voice`: микрофон и
распознавание речи, узнавание голосов, синтез Fish Speech с запасным edge-tts, воспроизведение, слух системного звука),
Telegram (модуль `modules/telegram`: канал-дневник, бот и её собственный аккаунт), память-граф (Graphiti +
FalkorDB), окружения с инструментами (управление ПК, приложения, MCP, вебхуки), питомец на рабочем столе (модуль `modules/pet`), трей.

Игры подключаются модулями из папки `modules/`. Первый из них: [modules/rimworld](modules/rimworld) (мод, MCP-сервер, игровые
контуры на Claude; раньше отдельный репозиторий rimworld-agent). Модуль `rimworld` подключается к ядру по шине модулей
(WebSocket, контракт в [docs/MODULES.md](docs/MODULES.md)): его инструменты (из MCP-сервера игры) и приказ `order` видны планировщику,
пока профиль RimWorld активен, состояние игры (`game_running`, колония, цели) идёт в `state`, события игры (набег, гибель, пожар,
письмо, цель, итог забега) в менеджер внимания, реплики комментатора в очередь речи. Модуль запускает сам супервизор ядра
(`agent/modules.yaml`); папка модуля `modules/rimworld`. Без модуля игры ассистентка работает как раньше, без игровой части.

## Запуск
```powershell
cd agent
pip install -r requirements.txt -r ../modules/voice/requirements.txt    # ядро и звук (edge-tts, soundcard, faster-whisper, ormsgpack)
copy secrets.env.example secrets.env     # токены Telegram и т.д. (не в git)
python main.py                           # ядро, дашборд на http://localhost:8766; голос, питомец, Telegram и RimWorld запускает само ядро
pythonw tray.py                          # трей: поднимает только ядро (питомца больше не запускает); пункт «Показывать питомца» меняет pet_visible
```
Голос и питомец это модули (`agent/modules.yaml`, вкладка «Модули» дашборда): запускать их вручную не нужно, `desktop_pet.py` больше
нет. Без Fish Speech модуль `voice` говорит запасным голосом edge-tts (нужен интернет). Без WhisperX микрофон распознаёт речь
локально на процессоре (faster-whisper, модель `GLADOS_STT_MODEL`, по умолчанию small), если её веса уже скачаны; без узнавания
голосов и перебивания. Качать модель модуль сам не будет: это только по разрешению владельца.
Если модуль голоса выключен или не запущен, она отвечает только текстом в чате дашборда (и облачком питомца), модуль питомца при этом
работает. Звук без колонок и микрофона для проверки запуска: `GITS_VOICE_FAKE=1` (синтез и воспроизведение подменены тишиной),
питомец без окна: `GITS_PET_HEADLESS=1`. Замеры задержки голоса: `modules/voice/tools/measure_latency.py` (описание в самом файле).
Вручную (при разработке, ядро должно работать): `python modules/voice/main.py`, `python modules/pet/main.py` из своих папок.
SDK модулей (`sdk/`, пакет `glados-module`) ядро берёт напрямую из репозитория (`agent/main.py` добавляет `sdk` в `sys.path`),
ставить его не нужно. Для модуля в своём окружении: `pip install -e C:\MyProjects\GitS\sdk`.

## Модули
Ядро принимает модули по WebSocket (`ws://127.0.0.1:8766/module/ws`, токен в `%LOCALAPPDATA%\GLaDOS\module_token`) и само
запускает их по `agent/modules.yaml` (супервизор). Включаются и выключаются во вкладке «Модули» дашборда (там же настройки,
задачи и вопросы модулей). Профили поведения лежат в `agent/profiles/*.yaml`. Руководство автора модуля: [docs/MODULES.md](docs/MODULES.md),
ТЗ: [docs/tz/](docs/tz/README.md). Первый тестовый модуль, `echo` (`sdk/examples/echo_module.py`), выключен по умолчанию.

**Telegram** это модуль `modules/telegram` (запускается ядром сам). Токен бота и канал (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHANNEL`) и ключи её
аккаунта (`TELEGRAM_API_ID`, `TELEGRAM_API_HASH`) лежат в `agent/secrets.env`; свои настройки (Telegram-id владельца для разговора через
Telegram, частота и часы постов) задаются во вкладке «Модули». Вход в её аккаунт один раз вручную: `python modules/telegram/tg_login.py`
(телефон, код и пароль вводишь сам).

Docker (необязательно): `docker compose up -d` поднимает граф памяти (FalkorDB), Fish Speech и WhisperX.
Локальная модель: LM Studio (`google/gemma-4-e4b` и эмбеддинги EmbeddingGemma).

Правила работы и карта модулей: [AGENTS.md](AGENTS.md). Изменения: [CHANGELOG.md](CHANGELOG.md).

# 05. Персонажи: код без имени персонажа, несколько персонажей, память по персонажам

Этап 3, **первым** (до ТЗ 04). Репозитории: `GitS` (основной), `rimworld-module` и `montage-module`, если на момент
выполнения они уже существуют (переменные окружения и тексты в коде). Ветки: `tz/05-characters`.
Читать перед этим: `00-architecture.md`, `01-module-contract.md`, `02-core.md`, `03-attention.md`, `04-memory-knowledge.md`.

## 1. Цель

Решение владельца (2026-10-10):
- Проект называется **GitS**. Персонаж (сейчас ГЛаДОС) — данные: его имя, характер, голос, стиль живут только в
  файлах персонажа (промпты, YAML, референс голоса), а не в коде.
- Персонажей может быть несколько, между ними можно переключаться.
- **Память разделена по персонажам:** у каждого персонажа свой граф личной памяти и свой опыт; переключение персонажа
  переключает память. Справочник (вики, регламенты) общий.
- Существующие идентификаторы данных («glados» в графе Graphiti, имена файлов в `memory/`) **не переносятся**: это
  данные персонажа `glados`.

## 2. Переменные окружения и имена в коде

- **CHR-1 (ОБЯЗАТЕЛЬНО).** Префиксы (решение владельца): **`CHARACTER_`** — то, что относится к персонажу (быстрая модель
  речи, голос и его референс, карточка персонажа, модель её реплик); **`GITS_`** — инфраструктура (адреса, порты,
  токены, папки, модели служб, пути к программам). Таблица переименования (найти все: `rg -o "GLADOS_[A-Z_]+"` по
  `agent`, `modules`, `sdk`, `services` и по репозиториям модулей):

| Было | Стало |
|---|---|
| `GLADOS_FAST_MODEL`, `GLADOS_FAST_URL` | `CHARACTER_FAST_MODEL`, `CHARACTER_FAST_URL` |
| `GLADOS_ASSISTANT_MODEL`, `GLADOS_WEB_MODEL`, `GLADOS_ANALYSIS_MODEL` | `CHARACTER_ASSISTANT_MODEL`, `GITS_WEB_MODEL`, `GITS_ANALYSIS_MODEL` |
| `GLADOS_CORE_URL`, `GLADOS_MODULE_TOKEN`, `GLADOS_MODULE_ID`, `GLADOS_SECRETS_FILE`, `GLADOS_ASSISTANT_URL`, `GLADOS_DASHBOARD_HOST` | `GITS_CORE_URL`, `GITS_MODULE_TOKEN`, `GITS_MODULE_ID`, `GITS_SECRETS_FILE`, `GITS_CORE_HTTP_URL`, `GITS_DASHBOARD_HOST` |
| `GLADOS_LM_URL`, `GLADOS_GRAPH_*`, `GLADOS_DECIDER*`, `GLADOS_STT_MODEL` | `GITS_LM_URL`, `GITS_GRAPH_*`, `GITS_DECIDER*`, `GITS_STT_MODEL` |
| `GLADOS_RIMWORLD_*`, `GLADOS_RECORD_*`, `GLADOS_VIDEO_DIR`, `GLADOS_FFMPEG`, `GLADOS_LAUNCH_GAME`, `GLADOS_MODE`, `GLADOS_REFLEX_ACTIONS` | `GITS_` + тот же хвост |
| `GLADOS_VISION_BLOCK`, `GLADOS_HEARING_BLOCK_APPS`, `GLADOS_READ_ROOTS` | `GITS_` + тот же хвост |

  Переменные, которых нет в таблице, — по правилу префиксов; спорные — `GITS_` и пометка в отчёте.
- **CHR-2 (ОБЯЗАТЕЛЬНО).** Совместимость: общий помощник `env(name, default)` (`agent/envcompat.py` и копия в модулях)
  читает новое имя, а если его нет — старое `GLADOS_*` с предупреждением в журнал один раз за процесс. Удаление старых
  имён — через одну версию (запись в CHANGELOG «Deprecated»). `secrets.env.example` — только новые имена; исполнитель
  сам `secrets.env` не читает и не правит, а пишет в отчёте список переменных, которые пользователь должен переименовать.
- **CHR-3 (ОБЯЗАТЕЛЬНО).** SDK: пакет `glados-module` → **`gits-sdk`**, импорт `glados_module` → **`gits_sdk`**, журнал
  `gits_sdk`, файл токена `%LOCALAPPDATA%\GitS\module_token` (старый путь читается как запасной). Переустановить
  (`pip install -e C:\MyProjects\GitS\sdk`, старый пакет `pip uninstall glados-module`). Обновить все импорты, `docs/MODULES.md`,
  `docs/tz/*` (правка текста ТЗ: `glados_module` → `gits_sdk`, `GLADOS_*` → новые имена).
- **CHR-4 (ОБЯЗАТЕЛЬНО).** Имя персонажа в коде: найти `rg -n -i "glados|гладос|aperture"` в `*.py`, `*.cs` (мод не
  трогать, если упоминания только в комментариях), `*.ps1`, `static/`. Каждое вхождение — одно из:
  - текст для модели (промпт, персона, стиль) → в файлы персонажа (раздел 3), код подставляет из карточки;
  - имя в интерфейсе (дашборд, трей, питомец) → `character.name` из активного персонажа;
  - распознавание обращения («ГЛаДОС, ...», упоминание в группе Telegram) → список `character.aliases`;
  - идентификатор данных («glados» в Graphiti, `SELF_PERSON_ID`, имена файлов) → остаётся, но через
    `character.id` (у ГЛаДОС `id = "glados"` — данные совпадут);
  - комментарии и докстринги → нейтрально («персонаж», «ассистентка»).
  Имена модулей Python с персонажем (`rimworld-module/agent/glados_agent.py`) → нейтральные (`game_agent.py`), с
  обновлением импортов. Итог — в отчёте таблицей «файл — было — стало».

## 3. Персонажи

- **CHR-5 (ОБЯЗАТЕЛЬНО).** Папка персонажа `agent/characters/<id>/`:

```
agent/characters/glados/
  character.yaml       # id, name, aliases, gender, language, voice, emotions, defaults
  constitution.yaml    # перенос agent/resources/prompts/constitution.yaml (МЕНЯЕТ ТОЛЬКО ЧЕЛОВЕК)
  prompt.yaml          # перенос agent/resources/prompts/prompt.yaml
  persona.md           # тексты-персоны, которые сейчас зашиты в код (Telegram PERSONA и т.п.)
  voice/GlaDos.wav     # перенос agent/resources/tts/references/GlaDos.wav
  stickers/            # (после ТЗ 13) стикеры питомца
```

  `character.yaml`:

```yaml
id: glados                 # совпадает с идентификаторами данных (граф "glados")
name: ГЛаДОС
aliases: [ГЛаДОС, гладос, GLaDOS, глэдос]
gender: female             # для грамматики промптов
language: ru
voice: {reference: voice/GlaDos.wav, edge_voice: ru-RU-SvetlanaNeural, pitch: -25Hz, rate: -12%}
world: Aperture Science    # для промптов и стиля монтажа
```

  `constitution.yaml` переносится `git mv` без изменения содержимого (правило «меняет только человек» действует и для
  нового пути; обновить формулировку в `AGENTS.md`). Изменение файла — нарушение; проверка: хеш содержимого до и после.
- **CHR-6 (ОБЯЗАТЕЛЬНО).** `agent/character.py`: `Character` (поля из YAML + пути), `load(id)`, `active()`, `list()`,
  `switch(id)`. Активный — настройка `character` (по умолчанию `glados`). `prompts.py` собирает характер из папки
  активного персонажа. Модули получают персонажа через `core.persona` (карточка + `name`, `aliases`, `gender`, `voice`) и
  тему `character` (новая тема контракта: `{"id", "name", "aliases"}`, рассылается при переключении; добавить в ТЗ 01 и
  SDK `TOPICS`).
- **CHR-7 (ОБЯЗАТЕЛЬНО).** Переключение (`switch`): инструмент ядра `core.set_character` (`availability: always`,
  `risk: ask`), выбор в дашборде (вкладка «Состояние») и в трее. При переключении: дождаться конца текущей реплики,
  сбросить очередь речи и стек фокуса внимания, перечитать промпты, переключить память (раздел 4), разослать тему
  `character`, записать событие. Голосовой модуль меняет референс по `voice` персонажа.
- **CHR-8 (ЖЕЛАТЕЛЬНО).** Шаблон `agent/characters/_template/` с пустыми файлами и комментариями для нового
  персонажа; раздел «Как добавить персонажа» в `README.md`.

## 4. Память по персонажам

- **CHR-9 (ОБЯЗАТЕЛЬНО).** Личная память графа: `group_id` = `character.id` (у ГЛаДОС — прежний `glados`, данные не
  переносятся). Области ТЗ 04 — `"<character.id>.<scope>"`. FalkorDB: база = `character.id` (сейчас `database=GROUP`
  = "glados" — совпадает). Очередь графа — общая, но каждая запись помечена `character_id` на момент реплики.
- **CHR-10 (ОБЯЗАТЕЛЬНО).** Файлы памяти персонажа (диалог, факты, отношения, профиль голосов, правила внимания,
  `MemoryStore`): для `glados` — прежние пути в `memory/` (без переноса); для любого другого персонажа —
  `memory/characters/<id>/` с теми же именами файлов. Функция `character.memory_dir()` возвращает нужную папку; все места,
  где сейчас берутся пути этих файлов, — через неё. Общими остаются: `events.db`, `settings.json`, справочник
  `knowledge.db` (коллекции `reference` и `web`), скриншоты.
- **CHR-11 (ОБЯЗАТЕЛЬНО).** Опыт (ТЗ 04, коллекции `kind = experience`) — по персонажу: коллекция
  `<character.id>.<scope>.experience` (для `glados` прежние имена `rimworld.experience` и т.п. получают алиас — выбрать
  одно и описать в отчёте; если ТЗ 04 ещё не выполнено, ТЗ 04 сразу использует схему с персонажем).
- **CHR-12 (ОБЯЗАТЕЛЬНО).** Модули с собственной памятью персонажа (`rimworld-module`: `relationships` с колонистами,
  уроки; `voice`: профили голосов) получают `character.id` из темы `character` и хранят данные в подпапке персонажа
  по тому же правилу (для `glados` — прежние пути).

## 5. Тесты и приёмка

- **CHR-13 (ОБЯЗАТЕЛЬНО).** Тесты: совместимость переменных (новое, старое с предупреждением, отсутствие); загрузка
  персонажа и `list`; `switch` перечитывает промпты и переключает `group_id`, папку памяти, рассылает тему; хеш
  `constitution.yaml` не изменился; проверка, что в `agent/**/*.py` (кроме `characters/` и тестов) нет строк
  `глад`/`glados`/`aperture` без пометки `# id данных` (тест-«сторож» по исходникам).
- **Приёмка:** `.\check.ps1` зелёный во всех затронутых репозиториях; ядро стартует с персонажем `glados` и ведёт себя
  как раньше (тот же граф памяти, те же файлы); тестовый персонаж из шаблона переключается из дашборда, отвечает своим
  характером и не видит фактов графа `glados`; обратное переключение возвращает память ГЛаДОС.

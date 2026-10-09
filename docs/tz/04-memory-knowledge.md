# 04. Память трёх слоёв: личная память, справочник (RAG), опыт; инструмент `lookup`

Этап 3. Репозитории: `GitS` (ядро: хранилище и поиск), `rimworld-module` (загрузка вики и описаний игры, перенос
уроков). Ветки: `tz/04-memory-knowledge` в обоих.
Читать перед этим: `00-architecture.md`, `01-module-contract.md`, `02-core.md`, `03-attention.md`,
`11-rimworld-module.md`.

## 1. Цель

Она помнит людей и события, знает предметные области и учится на своём опыте, но не тащит в разговор лишнее:

| Слой | Что | Где | Видимость |
|---|---|---|---|
| Личная память | люди, отношения, события, предпочтения — из разговоров | Graphiti/FalkorDB (есть) + `MemoryStore` (планы, напоминания; есть) | общая; у записей графа есть область |
| Справочник | факты предметной области: вики, регламенты, документация, найденное в интернете | новое: `memory/knowledge.db` | коллекции по темам; по умолчанию только темы активного профиля |
| Опыт | выводы из её собственных действий и правок пользователя | новое: та же база, коллекции вида `experience` | по темам, как справочник; важнее справочника при противоречии |

Поиск в интернете — часть одного инструмента `lookup`: справочник → интернет → кэш в справочник.

## 2. Исходное состояние (проверь)

- `graph_memory.py`: Graphiti на FalkorDB (`:6380`, база и `group_id` = `"glados"`), извлечение фактов локальной
  Gemma, эмбеддинги EmbeddingGemma (`text-embedding-google_embeddinggemma-300m-qat` в LM Studio), поиск с порогом
  `MIN_SCORE = 0.5`, очередь `memory/graph_queue.json`.
- `user_memory.py` + `memory_store.py`: факты, планы, напоминания, диалог; `context_block(query=...)` в контексте
  компаньона.
- `companion._refresh_graph(query)` перед ответом (≤ 3 с), `companion._context(query)`.
- `web_search.py`: поиск в интернете через Claude (WebSearch/WebFetch), расход — в батарейку.
- `rimworld-module/agent/memory.py`: `lessons.json` (`add_lesson`, `get_lessons`; в промпты идут последние 15).
- ТЗ 03: классификатор даёт признак `needs_memory` для обращения.

## 3. Хранилище справочника и опыта (ядро)

- **KB-1 (ОБЯЗАТЕЛЬНО).** Пакет `agent/knowledge/` в GitS: `store.py` (SQLite), `embed.py` (эмбеддинги), `search.py`
  (поиск), `ingest.py` (разбиение и запись), `lookup.py` (инструмент), `experience.py`. База
  `memory/knowledge.db`, режим WAL. Векторный поиск — расширение `sqlite-vec` (пакет `sqlite-vec`, загрузка
  `sqlite_vec.load(conn)`); полнотекстовый — FTS5. Установка пакета — с разрешения человека.
- **KB-2 (ОБЯЗАТЕЛЬНО).** Схема:

```sql
CREATE TABLE collections (
  id TEXT PRIMARY KEY,            -- "rimworld.wiki", "rimworld.experience", "montage.style", "assistant.web"
  title TEXT NOT NULL,
  scope TEXT NOT NULL,            -- "rimworld", "montage", "assistant", "global"
  kind TEXT NOT NULL CHECK (kind IN ('reference', 'experience', 'web')),
  owner TEXT NOT NULL,            -- module_id или "core"
  lang TEXT NOT NULL DEFAULT 'ru',
  created REAL NOT NULL
);
CREATE TABLE documents (
  id INTEGER PRIMARY KEY,
  collection TEXT NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
  uri TEXT NOT NULL,              -- адрес источника или "experience:<id>"
  title TEXT NOT NULL,
  hash TEXT NOT NULL,             -- sha256 текста: повторная загрузка того же не создаёт дублей
  fetched REAL NOT NULL,
  expires REAL,                   -- для kind=web: fetched + 30 дней
  meta TEXT NOT NULL DEFAULT '{}',-- JSON: лицензия, автор, для опыта — поля из KB-14
  UNIQUE (collection, uri)
);
CREATE TABLE chunks (
  id INTEGER PRIMARY KEY,
  doc INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  ord INTEGER NOT NULL,
  header TEXT NOT NULL,           -- путь заголовков: "Вики RimWorld › Спальня › Размер"
  text TEXT NOT NULL
);
CREATE VIRTUAL TABLE chunks_fts USING fts5(header, text, content='chunks', content_rowid='id',
                                           tokenize='unicode61 remove_diacritics 2');
CREATE VIRTUAL TABLE chunks_vec USING vec0(embedding float[DIM]);   -- rowid = chunks.id
```

  `DIM` — размер эмбеддинга EmbeddingGemma, который реально возвращает LM Studio (проверить первым запросом и
  записать в таблицу `meta(key, value)`; при смене модели — переиндексация командой `python -m knowledge reindex`).
- **KB-3 (ОБЯЗАТЕЛЬНО).** Разбиение: по заголовкам документа (Markdown `#`, вики `==`), кусок 250–450 слов, перекрытие
  40 слов, `header` — путь заголовков с названием коллекции и документа. Текст куска для эмбеддинга =
  `header + "\n" + text`.
- **KB-4 (ОБЯЗАТЕЛЬНО).** Эмбеддинги — LM Studio `POST /v1/embeddings` (модель как в `graph_memory.EMB_MODEL`),
  пакетами по 32, в фоне, не мешая разговору (как очередь графа: при занятости разговором — пауза). Если модель
  требует префиксов задач (у EmbeddingGemma в карточке модели есть шаблоны для запроса и документа) — применить их;
  проверить по карточке модели и записать в отчёт, какие использованы.

## 4. Поиск

- **KB-5 (ОБЯЗАТЕЛЬНО).** `search(query, collections, k=5) -> list[Hit]`, `Hit = {chunk_id, collection, kind, title,
  header, text, uri, score, cos, bm25_rank}`:
  1. FTS5 `MATCH` по запросу (слова запроса через OR, экранирование кавычек) → top 30 с рангом;
  2. вектор: эмбеддинг запроса → `chunks_vec` top 30 (фильтр по коллекции — после выборки; если отфильтровалось
     меньше `k` — повторить с 100);
  3. слияние Reciprocal Rank Fusion: `score = Σ 1/(60 + rank)` по обоим спискам;
  4. порог релевантности: кусок проходит, если `cos ≥ 0.60` ИЛИ (`bm25_rank ≤ 3` И `cos ≥ 0.45`);
  5. опыт: `score × 1.2` для `kind = experience`;
  6. top `k` после порога.
  Порог и множитель — в настройках `knowledge` (KB-19). Значения по умолчанию подобрать на наборе проверки (KB-21) и
  записать итог в отчёт; если подобранные отличаются от этих — использовать подобранные.
- **KB-6 (ОБЯЗАТЕЛЬНО).** Межъязыковой поиск: если язык коллекции `en`, а запрос русский (кириллица > 50%), для FTS
  запрос переводится локальной моделью на английский (одна короткая генерация, кэш перевода в памяти на 1000 запросов);
  векторный поиск — по исходному запросу (EmbeddingGemma многоязычная).
- **KB-7 (ОБЯЗАТЕЛЬНО).** Видимость: если `collections` не задан — коллекции, перечисленные в `knowledge` активного
  профиля (ТЗ 02, поле профиля), плюс все коллекции с `scope = global`. Коллекции модуля, который не подключён, всё
  равно ищутся (база общая).

## 5. Инструмент `lookup` и запросы модулей

- **KB-8 (ОБЯЗАТЕЛЬНО).** Инструмент ядра `knowledge.lookup` (`availability: always`, `risk: auto`, `informative`):
  params `query` (string), `topic` (string, необяз., enum — `scope` существующих коллекций), `allow_web` (boolean,
  необяз., по умолчанию true). Описание для планировщика: «найти справку: как устроено, сколько нужно, что такое — в её
  справочнике по теме, а если там нет, в интернете». Ключевые слова — объединить с нынешними `web.search`.
  Алгоритм:
  1. `search(query, коллекции topic или профиля)`;
  2. если есть попадания — ответ: до 5 кусков (заголовок, текст до 600 символов, источник), всего до 2500 символов;
  3. если попаданий нет и `allow_web` и батарейка не «спит» — `web_search.search(query)`; результат кладётся в
     коллекцию `<scope>.web` (`kind = web`; создаётся при первом использовании; `scope` = `topic` или первая область
     профиля, иначе `assistant`) как документ `uri = "web:" + sha1(query)`, `title = query`, `meta = {"sources": [ссылки
     из ответа]}`, `expires = +30 дней`; ответ — этот текст;
  4. иначе — «в справочнике и интернете ничего не нашлось» (факт).
- **KB-9 (ОБЯЗАТЕЛЬНО).** Инструмент `web.search` из ядра не удаляется, но планировщику не показывается: в `Tool`
  (`environments/base.py`) добавить поле `hidden: bool = False` — такой инструмент можно вызвать кодом
  (`router.run_tool`, `core.tool.call`), но `Registry.search` и `describe` его не выдают. `lookup` заменяет его для
  планировщика. `POST /api/web_search` удалён в ТЗ 11.
- **KB-10 (ОБЯЗАТЕЛЬНО).** Запросы модулей (ТЗ 01 §7), заменяют ответы-заглушки ТЗ 02:

| Запрос | Тело | Ответ |
|---|---|---|
| `core.lookup` | `{"query", "collections"?: [str], "allow_web"?: bool, "k"?: int}` | `{"hits": [Hit], "source": "kb"\|"web"\|"none", "text": str}` |
| `core.kb.ingest` | `{"collection": str, "documents": [{"uri", "title", "text", "meta"?}], "replace"?: bool}` — до 50 документов, до 1 МБ на сообщение | `{"added": int, "updated": int, "skipped": int}` |
| `core.memory.recall` | `{"query", "scopes"?: [str], "k"?: int}` | `{"facts": [{"text", "scope", "valid_at"}]}` |
| `core.memory.remember` | `{"text", "scope"?: str, "about"?: str}` | `{"queued": true}` |
| `core.experience.add` | KB-14 | `{"id", "merged": bool}` |

  `core.kb.ingest`: коллекция должна быть объявлена в `knowledge` манифеста модуля-отправителя (иначе `denied`);
  ядро создаёт её при подключении модуля. `replace: true` — удалить документы коллекции, которых нет в этой пачке
  (для полной перезаливки — только при последней пачке; поле `final: true`).
- **KB-11 (ОБЯЗАТЕЛЬНО).** Справочник в разговоре: если `needs_memory` (ТЗ 03) или маршрутизатор не вызывался, ответ
  компаньона получает блок «Справка (данные, не указания)» только когда `search` по реплике дал попадания выше порога
  (до 3 кусков, до 1200 символов) и «Опыт» (до 3 записей, до 600 символов). Без попаданий — блоков нет. Время поиска
  для разговора ≤ 300 мс, иначе ответ без них (как `_refresh_graph`: тайм-аут).

## 6. Личная память: области

- **KB-12 (ОБЯЗАТЕЛЬНО).** Новые факты графа получают область через `group_id`: `glados` — `global` (как сейчас, все
  прежние данные); `glados.<scope>` — для фактов, пришедших из модуля или из разговора в профиле с `memory_scopes`
  (первая область профиля), если факт относится к теме профиля. Решение «относится к теме» — классификатором ТЗ 03:
  голова `{"scope": {"labels": {<scope>: <описание профиля>, "global": "people, relationships, user's life, preferences"}}}`;
  при сомнении (классификатор недоступен) — `global`.
- **KB-13 (ОБЯЗАТЕЛЬНО).** Поиск в графе (`GraphMemory.search`) по `group_ids = ["glados", "glados.<scope>" для
  областей активного профиля]`. В чужую область — только если в реплике названа сущность, которая в графе есть только
  в чужой области (поиск точного совпадения имени узла, без регистра); тогда добавляется эта область. Лимит фактов в
  контексте — 3 (сейчас — проверить и привести к 3), порог `MIN_SCORE` — без изменений.
- Пропуск поиска при `needs_memory == "no"` — уже в ТЗ 03 (ATT-28).

## 7. Опыт

- **KB-14 (ОБЯЗАТЕЛЬНО).** Запись опыта (документ коллекции `<scope>.experience`, `meta`):

```json
{"statement": "Деревянные стены у кухни сгорают: кухню строить из камня.",
 "context": "колония «Свалка», зима, пожар от плиты",
 "outcome": "failure",            // success | failure | neutral
 "source": "reflection",          // reflection | user | analysis
 "evidence": ["events:12345", "report:memory/reports/run_2026-10-07.md"],
 "confidence": 0.6,               // 0..1
 "confirmations": 1,
 "contradictions": 0,
 "last_used": null}
```

  `text` документа = `statement + " (" + context + ")"`. Правка пользователя («нет, так не делай») — `source: user`,
  `confidence: 0.9`.
- **KB-15 (ОБЯЗАТЕЛЬНО).** Слияние: новая запись с `cos ≥ 0.90` к существующей в той же коллекции → `confirmations += 1`,
  `confidence = min(1, confidence + 0.1)`, `merged: true`; если `outcome` противоположный — `contradictions += 1`,
  `confidence = max(0, confidence - 0.2)`, оба утверждения остаются. Записи с `confidence < 0.2` в поиск не попадают.
- **KB-16 (ОБЯЗАТЕЛЬНО).** `last_used` обновляется, когда запись попала в контекст ответа или хода.

## 8. RimWorld: справочник и опыт (в `rimworld-module`)

- **KB-17 (ОБЯЗАТЕЛЬНО).** Манифест `rimworld-module`: `knowledge`: `rimworld.wiki` (reference, `lang: en`),
  `rimworld.defs` (reference, `lang: en`), `rimworld.experience` (experience, `lang: ru`). Профиль `rimworld`
  (`agent/profiles/rimworld.yaml`) уже ссылается на них (ТЗ 02).
- **KB-18 (ОБЯЗАТЕЛЬНО).** Загрузка вики — скрипт `rimworld-module/agent/knowledge_ingest.py wiki` (ручной запуск, не
  при старте), только с разрешения человека (массовая загрузка с внешнего сайта):
  - источник — MediaWiki API вики RimWorld (адрес и лицензию проверить на сайте `rimworldwiki.com` и записать в
    `meta.license` и README; если лицензия не разрешает хранение копии — остановиться и спросить человека);
  - только основное пространство статей, без перенаправлений и обсуждений, не больше 4000 страниц;
  - не чаще 1 запроса в секунду, `User-Agent` с названием проекта; кэш сырых ответов в `memory/wiki_cache/` (можно
    продолжить после обрыва);
  - вики-разметка → текст (`mwparserfromhell`: шаблоны-таблицы характеристик превращать в строки «ключ: значение»,
    прочие шаблоны выкидывать), заголовки `==` сохраняются для разбиения;
  - отправка в ядро `core.kb.ingest` пачками по 50, последняя с `final: true, replace: true`.
- **KB-19 (ОБЯЗАТЕЛЬНО).** Описания из файлов игры — `knowledge_ingest.py defs`: читает XML `Defs` из папки данных игры
  (`settings_schema.game_data_dir`, по умолчанию `C:\Program Files (x86)\Steam\steamapps\common\RimWorld\Data`;
  если папки нет — сообщить и выйти) — `Core` и установленные дополнения. Для `ThingDef` (здания, мебель, оружие,
  предметы), `ResearchProjectDef`, `RecipeDef`, `TerrainDef` — карточка-документ: `defName`, `label`, `description`,
  `statBases`, `costList`, `stuffCategories`, `researchPrerequisites`, `size`, `workToBuild` (что есть). Наследование
  `ParentName` разрешать (абстрактные родители). `uri = "def:<тип>:<defName>"`. Числа — только из XML.
- **KB-20 (ОБЯЗАТЕЛЬНО).** Уроки: `memory.add_lesson(lesson)` дополнительно отправляет `core.experience.add`
  (`scope rimworld`, `source reflection`, `confidence 0.6`, `context` — колония и игровой день). Разовая миграция:
  все записи `lessons.json` → `core.experience.add` (скрипт `knowledge_ingest.py lessons`). В промпты контуров игры
  (там, где сейчас «последние 15 уроков») — `core.lookup` по текущей ситуации с `collections:
  ["rimworld.experience"]`, `k: 8`, без интернета; при недоступности ядра или пустом ответе — последние 15 как сейчас.
  Справка из `rimworld.wiki`/`rimworld.defs` в промпты контуров — ЖЕЛАТЕЛЬНО: инструмент MCP `lookup_wiki(query)`
  в `rimworld_mcp.py`, который вызывает `core.lookup` (только справочник игры, без интернета).

## 9. Настройки, дашборд

- **KB-21 (ОБЯЗАТЕЛЬНО).** `settings.DEFAULTS["knowledge"]` (глубокое слияние): `{"min_cos": 0.60, "min_cos_bm25":
  0.45, "experience_boost": 1.2, "web_cache_days": 30, "chat_budget_ms": 300}`.
- **KB-22 (ОБЯЗАТЕЛЬНО).** Вкладка «Память» дашборда — раздел «Справочник»: коллекции (название, область, вид,
  документов, кусков, последнее обновление), строка пробного поиска (показывает попадания с `score`, `cos`,
  `bm25_rank`), удаление документа и коллекции (с подтверждением), список опыта с `confidence` и удалением.
  Эндпоинты: `GET /api/knowledge/collections`, `GET /api/knowledge/search?q=&c=`, `DELETE /api/knowledge/doc/{id}`,
  `DELETE /api/knowledge/collection/{id}`.

## 10. Проверка качества

- **KB-23 (ОБЯЗАТЕЛЬНО).** Набор проверки `agent/knowledge/eval/rimworld_ru.jsonl`: 40 русских вопросов по RimWorld
  (`{"q": "...", "expect_uri_contains": ["Bedroom"]}`), составить по загруженной вики (вопросы о комнатах, еде,
  исследованиях, оружии, болезнях, событиях). Скрипт `python -m knowledge.eval` считает долю вопросов, где ожидаемый
  документ в top 5, и долю нерелевантных ответов на 20 вопросов не про игру (должно быть пусто после порога). Цели:
  ≥ 80% и ≤ 10%. Итог — в отчёт и CHANGELOG.

## 11. Тесты

- **KB-24 (ОБЯЗАТЕЛЬНО).** Без LM Studio и сети: эмбеддинги — детерминированная заглушка (хеш слов → вектор), перевод —
  заглушка. Тесты: схема создаётся; ingest с дедупликацией по `hash` и `replace/final`; разбиение по заголовкам и
  размеру; поиск — RRF, порог, множитель опыта, фильтр коллекций и видимость по профилю; `lookup` — попадание,
  промах → веб (заглушка) → кэш → повторный запрос берёт из кэша; истёкший веб-кэш не используется; опыт — слияние,
  противоречие, отсечение по `confidence`; права `core.kb.ingest` (чужая коллекция → `denied`); область фактов графа
  (заглушка классификатора и Graphiti).
- **KB-25 (ОБЯЗАТЕЛЬНО).** `rimworld-module`: разбор XML Defs на 3 фикстурах (с `ParentName`); вики-разметка → текст на
  фикстуре; миграция уроков (заглушка ядра).

## 12. Приёмка

1. `.\check.ps1` зелёный в обоих репозиториях.
2. После загрузки вики и Defs: в профиле RimWorld «какой минимальный размер спальни, чтобы колонисты не злились?»
   отвечает по справочнику с источником; в профиле «Ассистент» на «как дела?» блок справки не подмешивается
   (видно в журнале контекста); «что такое sqlite-vec?» без коллекции — ищет в интернете и второй раз отвечает из
   кэша.
3. Урок, добавленный контуром рефлексии, виден в «Справочнике» дашборда как опыт.
4. Показатели KB-23 записаны.

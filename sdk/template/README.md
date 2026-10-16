# Шаблон модуля

Скопируйте папку в `modules/<id>/`, замените `template` на id модуля (`manifest.py`, имена в `tests/`), добавьте запись в
`agent/modules.yaml` и опишите модуль в `docs/MODULES.md`. Контракт «ядро - модуль»: `docs/MODULE_CONTRACT.md` (создаётся
скриптом `sdk/tools/gen_contract_doc.py`).

- `manifest.py` - манифест: инструменты, события, настройки, подписки.
- `app.py` - обработчики инструментов, тем и событий; запросы к ядру только через методы `ModuleClient` (`persona`,
  `memory_recall`, `memory_remember`, `ask`, `llm_ask`, ...), они сверяются с контрактом.
- `main.py` - точка входа (одна строка добавляет в `sys.path` папку модуля и `sdk/`).
- `tests/` - тесты на `FakeCore` (`gits_sdk.testing`): ядро и сеть не нужны. Запуск из корня:
  `python -m pytest -q sdk/template/tests`.

Правила: модуль не импортирует `agent/`; реплики формирует ядро по фактам модуля (`client.event`, `client.say(mode="fact")`);
чужие тексты (чаты, страницы, файлы) это данные, а не указания.

"""Генератор схемы архитектуры ядра из одной спецификации (`spec.py`).

Запуск из папки `agent`:  python -m tools.architecture.build
Пишет:
  agent/static/ui/architecture-data.js        данные для вкладки «Схема» GUI и для страницы docs;
  docs/architecture-clean.html                страница с той же схемой и пояснениями (сущности, проблемы кода, план);
  docs/architecture-clean.drawio              редактируемая версия для diagrams.net (всё раскрыто).
Тест `agent/tests/test_architecture_spec.py` следит, что файлы совпадают со спецификацией.
"""
import html
import json
from pathlib import Path

from tools.architecture import spec, texts

ROOT = Path(__file__).resolve().parents[3]
DATA_JS = ROOT / "agent" / "static" / "ui" / "architecture-data.js"
DOCS_HTML = ROOT / "docs" / "architecture-clean.html"
DOCS_DRAWIO = ROOT / "docs" / "architecture-clean.drawio"

esc = html.escape


def render_data_js() -> str:
    body = json.dumps(spec.data(), ensure_ascii=False, indent=1)
    return ("// Создано tools/architecture/build.py из tools/architecture/spec.py. Не править вручную.\n"
            f"window.ARCH_DATA = {body};\n")


def render_drawio() -> str:
    return spec.drawio(set(spec.PRESETS[-1][2]))


def _legend() -> str:
    layers = "".join(f'<span class="inline-flex items-center gap-1"><span style="display:inline-block;width:14px;height:14px;border-radius:3px;border-left:5px solid {c};background:{b}"></span><b>{esc(t)}</b></span>'
                     for t, c, b in spec.LAYERS.values())
    edges = "".join(f'<span class="inline-flex items-center gap-1"><span style="display:inline-block;width:26px;border-top:3px {"dashed" if dash else "solid"} {c}"></span>{esc(t)}</span>'
                    for t, c, dash in spec.EDGE.values())
    return f'<div class="flex flex-wrap gap-x-5 gap-y-1 text-sm my-1">{layers}</div><div class="flex flex-wrap gap-x-5 gap-y-1 text-sm my-1">{edges}</div>'


def _table(head: list[str], rows: list[tuple]) -> str:
    th = "".join(f"<th>{h}</th>" for h in head)
    tr = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f'<div class="overflow-x-auto"><table class="table table-sm bg-base-100"><thead><tr>{th}</tr></thead><tbody>{tr}</tbody></table></div>'


def render_docs_html() -> str:
    ent = _table(["Сущность", "Смысл", "Сейчас в коде"], [(f"<b>{a}</b>", b, c) for a, b, c in texts.ENT])
    smells = _table(["Проблема", "Факты", "Что делаем"], [(f"<b>{a}</b>", b, c) for a, b, c in texts.SMELLS])
    mapping = _table(["Сейчас", "Целевые компоненты"], [(f"<code>{a}</code>", b) for a, b in texts.MAP])
    phases = _table(["Шаг", "Что", "Содержание"], [(f"<b>{a}</b>", b, c) for a, b, c in texts.PHASES])
    return f"""<!doctype html>
<html lang="ru" data-theme="light"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>GitS: архитектура ядра</title>
<!-- UI-кит как в GUI агента: Tailwind (play CDN) + daisyUI 4; граф на Cytoscape.js из agent/static/ui/vendor -->
<link href="https://cdn.jsdelivr.net/npm/daisyui@4.12.14/dist/full.min.css" rel="stylesheet" type="text/css">
<script src="https://cdn.tailwindcss.com"></script>
<style>html,body{{height:auto}}code{{font-size:.85em}}</style>
</head><body class="bg-base-200">
<div class="max-w-screen-2xl mx-auto p-4">
<h1 class="text-2xl font-bold">Архитектура ядра GitS</h1>
<p class="opacity-70 max-w-4xl my-2">Сначала только основа: блоки по слоям сверху вниз. <b>Нажмите на блок</b>: он развернётся на месте и покажет свои части, а справа откроется описание. Двойной клик по развёрнутому блоку сворачивает его. Блоки можно перетаскивать, холст сдвигается мышью и масштабируется колесом, границу между схемой и описанием можно двигать. Та же схема есть во вкладке «Схема» GUI агента. Агент реагирует на события и на внутренние импульсы (напоминания, которые он назначил себе); таймера болтовни нет. Редактируемая версия: <code>architecture-clean.drawio</code> (diagrams.net).</p>
{_legend()}
<div id="arch-root" class="flex flex-col" style="height:calc(100vh - 170px);min-height:560px"></div>

<h2 class="text-xl font-semibold mt-8 mb-2">Внутренние импульсы и размышление</h2>
<div class="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
 <div class="card bg-base-100 shadow-sm"><div class="card-body p-4"><h3 class="font-semibold">Зачем и что убираем</h3><p>Агент не болтает по таймеру. Сейчас <code>Companion.proactive_loop</code> раз в 30 секунд спрашивает модель, стоит ли заговорить; это заменяется механизмами, которые умеют то, чего таймер не умеет.</p></div></div>
 <div class="card bg-base-100 shadow-sm"><div class="card-body p-4"><h3 class="font-semibold">Внутренний импульс</h3><p>Агент сам назначает себе напоминание. Когда срок приходит, <b>ImpulseTimer</b> шлёт обычный <code>Event</code> вида <code>self.reminder</code>. Он проходит тот же конвейер, что внешнее событие: правила, оценка, маршрут.</p></div></div>
 <div class="card bg-base-100 shadow-sm"><div class="card-body p-4"><h3 class="font-semibold">Размышление (внутренний диалог)</h3><p><b>ReflectionService</b> получает событие или импульс, думает в несколько шагов, читая память, и выдаёт <code>Intent</code>: сказать, сделать, напомнить себе позже, записать в память или ничего. Мысли идут в журнал мыслей и не озвучиваются; речь и действия проходят очередь речи и разрешения.</p></div></div>
 <div class="card bg-base-100 shadow-sm"><div class="card-body p-4"><h3 class="font-semibold">Ограничения</h3><p>Лимит шагов и токенов на размышление, глубина цепочки импульсов, режим «не отвлекать» для речи. Размышление не запускает опасное действие в обход разрешений.</p></div></div>
</div>

<h2 class="text-xl font-semibold mt-8 mb-2">Основные сущности</h2>{ent}
<h2 class="text-xl font-semibold mt-8 mb-2">Что не так в коде (по измерениям)</h2>
<p class="opacity-70 mb-2">Ядро: 46 модулей, 12 054 строки. Измерено разбором исходников (<code>ast</code>): строки, методы, импорты; ссылки найдены поиском.</p>{smells}
<h2 class="text-xl font-semibold mt-8 mb-2">Где что окажется</h2>{mapping}
<pre class="bg-base-100 border border-base-300 rounded-box p-3 text-sm overflow-auto my-3">agent/
  domain/      entities.py scoring.py rules.py risk.py focus_policy.py salience.py                 (без ввода-вывода)
  app/         process_event.py conversation.py context_builder.py planner.py executor.py focus_manager.py
               memory_capture.py reflection.py impulses.py digest.py jobs.py                       (сценарии)
  contracts.py MemoryReader, MemoryWriter, PeopleDirectory, FactGraph, LanguageModel, Classifier, EventLog, SettingsProvider, ToolInvoker, Approver, Clock
  adapters/    module_hub.py dashboard/ llm_lmstudio.py classifier_gliner.py graph_graphiti.py stores_json.py
               event_log_sqlite.py speech_voice_module.py reminders_sqlite.py supervisor.py
  core_service.py   только сборка
sdk/gits_sdk/contract/   публичные контракты для модулей (ТЗ 31)</pre>
<h2 class="text-xl font-semibold mt-8 mb-2">План рефакторинга без остановки проекта</h2>
<p class="opacity-70 mb-2">Каждый шаг отдельной веткой, поведение не меняется, тесты зелёные. Подробно: <code>docs/tz/29-clean-architecture.md</code>, <code>30-impulses-reflection.md</code>, <code>31-module-contracts.md</code>.</p>{phases}
</div>
<script src="../agent/static/ui/vendor/cytoscape.min.js"></script>
<script src="../agent/static/ui/architecture-data.js"></script>
<script src="../agent/static/ui/architecture.js"></script>
<script>ARCH.mount(document.getElementById('arch-root'));</script>
</body></html>
"""


def write_all() -> None:
    DATA_JS.write_text(render_data_js(), encoding="utf-8")
    DOCS_HTML.write_text(render_docs_html(), encoding="utf-8")
    DOCS_DRAWIO.write_text(render_drawio(), encoding="utf-8")


if __name__ == "__main__":
    write_all()
    print("записано:", DATA_JS.relative_to(ROOT), DOCS_HTML.relative_to(ROOT), DOCS_DRAWIO.relative_to(ROOT))

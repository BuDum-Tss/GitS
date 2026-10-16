"""Генератор документа контракта (ТЗ 31, MC-9): строит `docs/MODULE_CONTRACT.md` и схемы `docs/contract/*.json` из описаний
`gits_sdk.contract`. Запуск из корня репозитория: `python sdk/tools/gen_contract_doc.py` (записать), `--check` (только сверить).
Тест `sdk/tests/test_contract_doc.py` проверяет, что файлы в репозитории совпадают с тем, что строит этот скрипт."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk"))

from gits_sdk import contract  # noqa: E402
from gits_sdk import protocol as P  # noqa: E402
from gits_sdk.contract import environment as E  # noqa: E402
from gits_sdk.contract import messages as M  # noqa: E402
from gits_sdk.contract import versions as V  # noqa: E402
from gits_sdk.contract.spec import Message  # noqa: E402

DOC = ROOT / "docs" / "MODULE_CONTRACT.md"
SCHEMA_DIR = ROOT / "docs" / "contract"


def _md(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _fields_table(fields) -> list[str]:
    if not fields:
        return ["Полей нет."]
    rows = ["| Поле | Тип | Обязательно | Ограничения | Описание |", "|---|---|---|---|---|"]
    for f in fields:
        lim = []
        if f.type == "string" and f.max is not None:
            lim.append(f"до {int(f.max)} симв.")
        if f.type in ("integer", "number"):
            if f.min is not None:
                lim.append(f"от {f.min:g}")
            if f.max is not None:
                lim.append(f"до {f.max:g}")
        if f.type == "array" and f.max is not None:
            lim.append(f"до {int(f.max)} элем.")
        if f.enum:
            lim.append("одно из " + ", ".join(f"`{e}`" for e in f.enum))
        if f.nonempty:
            lim.append("непустая")
        if f.default is not None:
            lim.append(f"по умолчанию `{f.default}`")
        typ = f.type + (f"[{f.items}]" if f.items else "")
        rows.append(f"| `{f.name}` | {typ} | {'да' if f.required else 'нет'} | {_md('; '.join(lim)) or '-'} | {_md(f.doc)} |")
    return rows


def _message_section(m: Message) -> list[str]:
    head = f"### `{m.name}`"
    out = [head, "", m.doc, ""]
    meta = [f"направление: {m.direction}", f"вид: {m.kind}", f"статус: **{m.status}**", f"с версии {m.since}"]
    if m.status == "planned" and m.planned_in:
        meta.append(f"появится в {m.planned_in}")
    if m.max_bytes:
        meta.append(f"размер тела до {m.max_bytes} байт")
    out += [", ".join(meta) + ".", ""]
    if m.errors:
        out += ["Коды ошибок: " + ", ".join(f"`{e}`" for e in m.errors) + ".", ""]
    if m.note:
        out += [m.note, ""]
    out += ["Тело:", ""] + _fields_table(m.request) + [""]
    if m.kind == "request":
        out += ["Ответ (`result.value`):", ""] + (_fields_table(m.response) if m.response else ["Произвольное JSON-значение или пустой объект."]) + [""]
    if m.valid:
        out += ["Пример:", "", "```json", json.dumps(m.valid[0], ensure_ascii=False, indent=2), "```", ""]
    return out


def _section(title: str, intro: str, msgs) -> list[str]:
    out = [f"## {title}", "", intro, ""]
    for m in msgs:
        out += _message_section(m)
    return out


def build_markdown() -> str:
    lines = [
        "# Контракт модулей GitS", "",
        "Файл создан скриптом `sdk/tools/gen_contract_doc.py` из `sdk/gits_sdk/contract/`. Руками не правится: меняйте описания в коде "
        "и запускайте скрипт. Тест `sdk/tests/test_contract_doc.py` сверяет файл с кодом. Руководство автора модуля: `docs/MODULES.md`, "
        "протокол: `docs/tz/01-module-contract.md`, ТЗ контракта: `docs/tz/31-module-contracts.md`.", "",
        f"Версия протокола `PROTOCOL = {P.PROTOCOL}`, версия контракта `{V.CONTRACT_VERSION}`. JSON-схемы тел лежат в `docs/contract/<имя>.json`.", "",
        "## Версии и совместимость", "",
    ]
    lines += [f"- {line}" for line in (V.__doc__ or "").strip().split("\n")[3:] if line.strip()]
    lines += ["", "История:", ""] + [f"- `{v}`: {d}" for v, d in V.HISTORY] + [""]
    lines += ["Устаревшее: " + ("; ".join(f"`{i}` до {d}, заменить на {r}" for i, d, r in V.DEPRECATED) or "нет."), ""]
    lines += _section("Запросы модуля к ядру", "Модуль шлёт запрос, ядро отвечает `result`. Клиент SDK: типизированные методы "
                      "`ModuleClient` (`tts`, `persona`, `llm_ask`, ...) или универсальный `request`.", M.CORE_REQUEST_MESSAGES)
    lines += _section("Сообщения модуля к ядру без ответа", "Одностороннее. Тело проверяет ядро; неверное отбрасывается с записью в журнал.",
                      M.MODULE_NOTE_MESSAGES)
    lines += _section("Запросы ядра к модулю", "Ядро шлёт запрос, модуль отвечает `result`. Обработчики модуля: `@client.tool`, "
                      "`@client.on_request` и т. д.", M.MODULE_REQUEST_MESSAGES)
    lines += _section("Сообщения ядра к модулю без ответа", "Одностороннее.", M.CORE_NOTE_MESSAGES)
    lines += _section("Темы", "Ядро рассылает `topic` с `{topic, data}` модулям, подписанным в манифесте (`subscribe`).", M.TOPIC_MESSAGES)
    lines += ["## Журнал ядра", "",
              "Запись: `client.log(kind, text, **data)` (сообщение `log`), допустимые виды: " + ", ".join(f"`{k}`" for k in M.LOG_KINDS) + ".",
              "Чтение: `core.events.tail`, допустимые виды: " + ", ".join(f"`{k}`" for k in M.LOG_READ_KINDS) + ".", ""]
    lines += ["## Окружение процесса модуля", "", "| Переменная | Кто задаёт | Обязательна | Описание | По умолчанию |", "|---|---|---|---|---|"]
    for v in E.ENV_VARS:
        lines.append(f"| `{v.name}` | {v.by} | {'да' if v.required else 'нет'} | {_md(v.doc)} | {_md(v.default) or '-'} |")
    lines += ["", "## Ресурсы персонажа", ""] + [f"- {r}" for r in E.RESOURCE_RULES] + [""]
    lines += ["## Что модулю запрещено", "",
              "Модули (`modules/*`, кроме тестов) импортируют только стандартную библиотеку, сторонние пакеты, `gits_sdk` и свои файлы. "
              "Журнал, локальная модель, путь к Claude, опасность действия и метки трассы берутся из SDK: `client.log`, `gits_sdk.host`, "
              "`gits_sdk.claude_path`, `gits_sdk.safety`, `gits_sdk.trace`. Тест: `agent/tests/test_architecture.py`.", ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def build_schemas() -> dict[str, str]:
    out: dict[str, str] = {}
    for m in contract.REGISTRY.values():
        doc = {"name": m.name, "direction": m.direction, "kind": m.kind, "status": m.status, "since": m.since,
               "errors": list(m.errors), "request": m.request_schema()}
        if m.kind == "request":
            doc["response"] = m.response_schema()
        out[f"{m.name}.json"] = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
    return out


def build() -> dict[Path, str]:
    files = {DOC: build_markdown()}
    for name, text in build_schemas().items():
        files[SCHEMA_DIR / name] = text
    return files


def main(argv: list[str]) -> int:
    files = build()
    if "--check" in argv:
        stale = [str(p.relative_to(ROOT)) for p, t in files.items()
                 if not p.exists() or p.read_text(encoding="utf-8").replace("\r\n", "\n") != t]
        extra = [p.name for p in SCHEMA_DIR.glob("*.json") if p not in files] if SCHEMA_DIR.is_dir() else []
        if stale or extra:
            print("устарели:", *stale, *extra, sep="\n  ")
            return 1
        print("документ контракта актуален")
        return 0
    SCHEMA_DIR.mkdir(parents=True, exist_ok=True)
    for p in SCHEMA_DIR.glob("*.json"):
        if p not in files:
            p.unlink()
    for p, t in files.items():
        p.write_text(t, encoding="utf-8", newline="\n")
    print(f"записано файлов: {len(files)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

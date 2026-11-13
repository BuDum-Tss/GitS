"""Разблокировка исследованиями (жалоба владельца: агент упирается в закрытую постройку или рецепт и не исследует нужное).

Что здесь:
  - признак закрытого объекта: `locked_error` (текст ошибки для модели; `is_locked_error` узнаёт его обратно);
  - цепочка исследований до нужного: `chain` (чистая функция: завершённые, доступные сейчас и каталог предпосылок);
  - очередь исследований `memory/research_queue.json`: срочные (их ждут шаги дерева задач) идут первыми;
  - шаги дерева задач, которые ждут исследование: `mark_waiting`, `mark_blocked`, `resume_waiting` (событие «исследование
    завершено» возвращает шаг в работу).

Ограничение текущего мода: предпосылки закрытого исследования и его стоимость он не отдаёт (нет команды описания исследования),
поэтому каталог `catalog` пуст, и цепочка видна только на один шаг (то, что уже доступно). Что нужно добавить в мод,
описано в docs/MOD_ROADMAP.md (раздел «Исследования»). Пока предпосылок не знаем, шаг помечается заблокированным с причиной,
а не обходится."""
from __future__ import annotations

import json
import time
from pathlib import Path

QUEUE_FILE = Path(__file__).parent / "memory" / "research_queue.json"
MAX_QUEUE = 20
WAITING = "waiting_research"       # статус шага дерева задач: ждём исследование
BLOCKED = "blocked"                # статус шага: нельзя выполнить, причина в blocked_reason
LOCKED_PREFIX = "ERROR: ЗАКРЫТО ИССЛЕДОВАНИЕМ"
UNAVAILABLE_PREFIX = "ERROR: НЕДОСТУПНО СЕЙЧАС"


def locked_error(name: str, research: list[str], what: str = "постройка") -> str:
    """Понятная ошибка для модели: что закрыто и какие исследования нужны (есть в описании объекта)."""
    need = ", ".join(research) if research else "неизвестно"
    return (f"{LOCKED_PREFIX}: {what} {name} откроется после исследования: {need}. Вызови unlock_for(\"{name}\", queue=true, "
            f"waiting_task=\"<шаг>\"), чтобы поставить исследование в очередь, а не обходиться другим объектом.")


def unavailable_error(recipe: str, workbench: str) -> str:
    """Рецепт не в списке доступных на верстаке: обычно закрыт исследованием, иногда имя неверное."""
    return (f"{UNAVAILABLE_PREFIX}: рецепт {recipe} сейчас не доступен на {workbench} (чаще всего нужно исследование; "
            f"проверь имя через list_recipes). Вызови unlock_for(\"{recipe}\", queue=true, waiting_task=\"<шаг>\").")


def is_locked_error(text: str) -> bool:
    return str(text).startswith((LOCKED_PREFIX, UNAVAILABLE_PREFIX))


def chain(target: str, completed: set[str], available: set[str], catalog: dict[str, dict]) -> tuple[list[str], int | None, str]:
    """Цепочка исследований до `target`: (шаги по порядку, суммарная стоимость или None, причина блокировки или '').

    completed: завершённые; available: можно начать сейчас; catalog: {имя: {"prereqs": [...], "cost": n}} (если мод отдаёт).
    Шаг без известных предпосылок и не доступный сейчас = цепочку не построить, это блокировка с причиной."""
    if target in completed:
        return [], 0, ""
    steps: list[str] = []
    seen: set[str] = set()

    def visit(name: str, path: tuple[str, ...]) -> str:
        if name in completed or name in seen:
            return ""
        if name in path:
            return f"цикл в предпосылках: {' -> '.join(path + (name,))}"
        info = catalog.get(name)
        if info is None or info.get("prereqs") is None:
            if name in available:
                info = {"prereqs": [], "cost": (info or {}).get("cost")}
            else:
                return f"не знаю предпосылок исследования «{name}» (мод не отдаёт описание исследований, см. MOD_ROADMAP)"
        for pre in info.get("prereqs") or []:
            err = visit(pre, path + (name,))
            if err:
                return err
        seen.add(name)
        steps.append(name)
        return ""

    err = visit(target, ())
    if err:
        return [], None, err
    costs = [(catalog.get(s) or {}).get("cost") for s in steps]
    total = sum(costs) if all(c is not None for c in costs) else None
    return steps, total, ""


# ── очередь исследований ───────────────────────────────────────────────────────

def load_queue(path: Path | None = None) -> list[dict]:
    try:
        data = json.loads((path or QUEUE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [i for i in data if isinstance(i, dict) and i.get("research")] if isinstance(data, list) else []


def _save(items: list[dict], path: Path | None = None) -> None:
    p = path or QUEUE_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def ordered(items: list[dict]) -> list[dict]:
    """Срочные (их ждут шаги дерева задач) впереди, внутри групп порядок добавления."""
    return sorted(items, key=lambda i: (not i.get("urgent"), i.get("added", 0)))


def enqueue(research: str, *, waiting_for: str = "", path: Path | None = None) -> str:
    """Поставить исследование в очередь (без дублей). Срочное, если его ждёт шаг дерева задач. Возвращает текст для модели."""
    items = load_queue(path)
    urgent = bool(waiting_for)
    for it in items:
        if it["research"] == research:
            it["urgent"] = bool(it.get("urgent")) or urgent
            if waiting_for:
                it["for"] = waiting_for
            _save(items, path)
            return f"исследование «{research}» уже в очереди (место {ordered(items).index(it) + 1})"
    if len(items) >= MAX_QUEUE:
        return f"ERROR: очередь исследований полна ({MAX_QUEUE})"
    items.append({"research": research, "for": waiting_for, "urgent": urgent, "added": time.time()})
    _save(items, path)
    place = [i["research"] for i in ordered(items)].index(research) + 1
    return f"исследование «{research}» в очереди, место {place}{' (срочно: ждёт шаг)' if urgent else ''}"


def next_research(path: Path | None = None) -> str:
    """Имя следующего исследования в очереди ('' если очередь пуста)."""
    items = ordered(load_queue(path))
    return items[0]["research"] if items else ""


def pop_done(research: str, path: Path | None = None) -> bool:
    items = load_queue(path)
    rest = [i for i in items if i["research"] != research]
    if len(rest) == len(items):
        return False
    _save(rest, path)
    return True


# ── шаги дерева задач ─────────────────────────────────────────────────────────

def mark_waiting(tasks: list[dict], step: str, research: str) -> bool:
    """Шаг ждёт исследование: статус waiting_research и поле waits_research. True, если шаг найден."""
    for t in tasks:
        if t.get("description") == step:
            t["status"] = WAITING
            t["waits_research"] = research
            t.pop("blocked_reason", None)
            return True
    return False


def mark_blocked(tasks: list[dict], step: str, reason: str) -> bool:
    for t in tasks:
        if t.get("description") == step:
            t["status"] = BLOCKED
            t["blocked_reason"] = reason[:300]
            return True
    return False


def resume_waiting(tasks: list[dict], research: str) -> list[str]:
    """Исследование завершено: шаги, которые его ждали, снова в работе (pending). Возвращает их описания."""
    resumed = []
    for t in tasks:
        if t.get("status") == WAITING and t.get("waits_research") == research:
            t["status"] = "pending"
            t.pop("waits_research", None)
            resumed.append(str(t.get("description")))
    return resumed


def waiting_research(tasks: list[dict]) -> set[str]:
    """Исследования, которых ждут шаги дерева задач (для приоритета очереди)."""
    return {str(t["waits_research"]) for t in tasks if t.get("status") == WAITING and t.get("waits_research")}


def resume_tasks_file(tasks_file: Path, research: str) -> list[str]:
    """Перечитать tasks.json, вернуть шаги, которые ждали `research`, и записать файл (если что-то вернулось)."""
    try:
        tasks = json.loads(tasks_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(tasks, list):
        return []
    resumed = resume_waiting(tasks, research)
    if resumed:
        tasks_file.write_text(json.dumps(tasks, ensure_ascii=False, indent=2), encoding="utf-8")
    return resumed

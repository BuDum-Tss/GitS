"""Стартовые цели и задачи на локальной модели (Gemma), когда Claude недоступен: чтобы у новой колонии не было пустого плана.

Цели и задачи Claude ставит сам (set_goal, set_task). Пока у него лимит, дашборд и ход оставались без главной цели. Здесь Gemma по
фактам состояния (возраст колонии, колонисты, запасы, еда, постройки) предлагает главную цель, 2-3 цели и 4-6 первых задач в том же
формате, что у Claude, и записывает их, ТОЛЬКО если плана ещё нет. Когда Claude вернётся, он продолжит с этих целей и может их менять.

Состояние для модели собирает код (никаких готовых формулировок целей в коде): модель пишет сами цели по фактам."""
import json
import logging
import time
from pathlib import Path

logger = logging.getLogger("local_planner")

_DIR = Path(__file__).parent / "memory"
CATEGORIES = ("defense", "economy", "research", "expansion", "survival", "population")
COOLDOWN = 600.0
_last_try = [0.0]

SYSTEM = (
    "Ты планируешь старт колонии в RimWorld для игрового агента. По фактам о колонии предложи: главную цель на всю игру "
    "(ambition), 2-3 ближайшие цели и 4-6 первых конкретных задач. Цели измеримые (число, срок, условие, которое видно в состоянии "
    "игры). Задачи короткие, выполнимые за час-два игры, привязаны к цели по номеру. Опирайся ТОЛЬКО на данные ниже, ничего не выдумывай. "
    "Ответь ТОЛЬКО JSON: "
    '{"ambition": {"title": "...", "success_criteria": "..."}, '
    '"goals": [{"title": "...", "category": "defense|economy|research|expansion|survival|population", "success_criteria": "..."}], '
    '"tasks": [{"description": "...", "goal": 0}]}. Тексты по-русски.')


def facts(state: dict) -> str:
    """Факты о колонии для модели (из состояния игры)."""
    colony = state.get("colony") or {}
    pawns = colony.get("pawns") or []
    res = colony.get("resources") or {}
    top = sorted(res.items(), key=lambda kv: -float(kv[1] or 0))[:8]
    lines = [f"День {float(state.get('tick') or 0) / 60000:.1f}, сезон {state.get('season')}, биом/погода: {state.get('weather')}",
             f"Колонистов: {len(pawns)}: " + ", ".join(f"{p.get('name')}" for p in pawns[:8]),
             f"Построек: {len(colony.get('buildings') or [])}, чертежей в очереди: {len(colony.get('pending_construction') or [])}",
             f"Запас еды на дней: {colony.get('food_days')}",
             "Ресурсы: " + (", ".join(f"{k} {v}" for k, v in top) or "почти ничего")]
    research = colony.get("research") or {}
    if research.get("current"):
        lines.append(f"Исследуется: {research['current']}")
    return "\n".join(lines)


def parse(raw: str) -> dict | None:
    """JSON плана из ответа модели с проверкой формы; None, если не годится."""
    a, b = raw.find("{"), raw.rfind("}")
    try:
        data = json.loads(raw[a:b + 1])
    except ValueError:
        return None
    amb = data.get("ambition") if isinstance(data, dict) else None
    if not isinstance(amb, dict) or len(str(amb.get("title") or "")) < 8 or len(str(amb.get("success_criteria") or "")) < 8:
        return None
    goals = [g for g in (data.get("goals") or []) if isinstance(g, dict) and len(str(g.get("title") or "")) >= 6][:3]
    tasks = [t for t in (data.get("tasks") or []) if isinstance(t, dict) and len(str(t.get("description") or "")) >= 8][:6]
    if not goals:
        return None
    return {"ambition": amb, "goals": goals, "tasks": tasks}


def build_files(plan: dict, now: float | None = None) -> tuple[list, list]:
    """(goals.json, tasks.json) в формате, который пишет Claude через set_goal и set_task."""
    now = now or time.time()
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(now))

    def goal(i, title, category, criteria, priority):
        return {"id": f"goal_{int(now)}{i}", "title": str(title)[:120], "category": category, "success_criteria": str(criteria)[:240],
                "priority": priority, "status": "active", "progress_pct": 0, "notes": "поставлено локальной моделью (Claude был недоступен)",
                "created": stamp, "updated": stamp}
    goals = [goal(0, plan["ambition"]["title"], "ambition", plan["ambition"]["success_criteria"], "high")]
    for i, g in enumerate(plan["goals"], 1):
        cat = g.get("category") if g.get("category") in CATEGORIES else "survival"
        goals.append(goal(i, g["title"], cat, g.get("success_criteria", ""), "normal"))
    tasks = []
    for t in plan["tasks"]:
        idx = t.get("goal")
        gid = goals[idx + 1]["id"] if isinstance(idx, int) and 0 <= idx < len(plan["goals"]) else goals[0]["id"]
        tasks.append({"description": str(t["description"])[:200], "goal_id": gid, "parent": "", "status": "pending", "created": stamp})
    return goals, tasks


async def seed(state: dict, ask=None) -> bool:
    """Записать стартовый план, если целей ещё нет. True, если записано. ask(system, user) -> текст (по умолчанию Gemma)."""
    goals_file, tasks_file = _DIR / "goals.json", _DIR / "tasks.json"
    try:
        if json.loads(goals_file.read_text(encoding="utf-8")):
            return False
    except (OSError, ValueError):
        pass
    if time.time() - _last_try[0] < COOLDOWN:
        return False
    _last_try[0] = time.time()
    if ask is None:
        import fast_brain

        async def ask(system, user):
            return await fast_brain._ask(system, user, timeout=90, max_tokens=700, temperature=0.4)
    try:
        plan = parse(await ask(SYSTEM, facts(state)))
    except Exception as e:
        logger.info(f"Локальный план не получен: {e}")
        return False
    if plan is None:
        logger.info("Локальная модель ответила не по формату: плана нет")
        return False
    goals, tasks = build_files(plan)
    _DIR.mkdir(parents=True, exist_ok=True)
    goals_file.write_text(json.dumps(goals, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        existing = json.loads(tasks_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        existing = []
    tasks_file.write_text(json.dumps(existing + tasks, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"Стартовый план поставлен локальной моделью: {goals[0]['title']} (+{len(goals) - 1} целей, {len(tasks)} задач)")
    return True

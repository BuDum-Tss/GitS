"""Статус модуля для ядра (ТЗ 18, FOC-1; docs/tz/01-module-contract.md, раздел 14, MOD-33): блок `data.status` в теме `state`.

Ядро по нему видит, что происходит: какая политика действует, можно ли безопасно её прервать, не ждёт ли она чего-то. По
`active_policy` фоновая политика (`afk`) означает, что задача кончилась и фокус свободен.

Поля:
  - `active_policy` - id текущей политики;
  - `active_goal` - главная цель игры (название) или пусто;
  - `urgency` - 0..1, срочность текущей политики (поле `urgency` в описании политики: бой 1, ручное 0.5, планирование 0.4);
  - `interruptible` - можно ли сейчас прервать: в бою нет и при критическом ремонте (постройка с HP ниже порога) нет;
    в наблюдении да; без фокуса (фоновая политика) прерывать нечего, поэтому да;
  - `estimated_remaining_s` - сколько осталось, если известно; пока источника оценки нет, всегда null;
  - `blocked` - строка с причиной, если политика ждёт (шаг заблокирован или ждёт исследование), иначе null;
  - `resources` - ресурсы политики (gpu, tokens), по модели, которая в ней думает.

Чистые функции: данные (политика, цель, дерево задач, постройки) передаёт контроллер, ничего не читается и не пишется."""
from __future__ import annotations

import research_unlock as ru

REPAIR_CRITICAL_HP = 0.3          # постройка с долей здоровья ниже этого критически повреждена: прерывать нельзя
MAX_REASON = 300


def critical_repair(buildings: list) -> list[str]:
    """Подписи построек с критическим повреждением (hp_pct ниже REPAIR_CRITICAL_HP). Пусто, если данных нет."""
    out: list[str] = []
    for b in buildings or []:
        if not isinstance(b, dict):
            continue
        hp = b.get("hp_pct")
        if isinstance(hp, bool) or not isinstance(hp, (int, float)):
            continue
        if hp < REPAIR_CRITICAL_HP:
            out.append(str(b.get("label") or b.get("def") or "постройка"))
    return out


def interruptible(policy, buildings: list) -> bool:
    """Можно ли безопасно прервать политику сейчас (см. модуль)."""
    if policy.focus != "active":
        return True                           # фоновая политика: фокуса нет, прерывать нечего
    if not policy.interruptible:
        return False                          # бой и другие политики без безопасной точки
    return not critical_repair(buildings)


def blocked_reason(policy, tasks: list) -> str | None:
    """Причина ожидания: первый заблокированный шаг дерева задач, иначе исследования, которых ждут шаги."""
    if policy.focus != "active":
        return None
    for t in tasks or []:
        if isinstance(t, dict) and t.get("status") == ru.BLOCKED:
            reason = str(t.get("blocked_reason") or "причина не указана")
            return f"шаг «{t.get('description')}»: {reason}"[:MAX_REASON]
    waiting = sorted(ru.waiting_research(tasks or []))
    if waiting:
        return ("ждём исследование: " + ", ".join(waiting))[:MAX_REASON]
    return None


def build(policy, *, goal: str, tasks: list, buildings: list) -> dict:
    """Блок `status` для сообщения `state` (ТЗ 18, MOD-33)."""
    return {
        "active_policy": policy.id,
        "active_goal": goal or "",
        "urgency": round(float(policy.urgency), 2),
        "interruptible": interruptible(policy, buildings),
        "estimated_remaining_s": None,
        "blocked": blocked_reason(policy, tasks),
        "resources": dict(policy.resources),
    }

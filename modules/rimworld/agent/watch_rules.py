"""Правила наблюдения, которые пишет сама модель (ТЗ 11e, POL-6): «жду исследование Орошение, чтобы строить фермы» →
правило: при завершении этого исследования повысить важность и перейти в планирование.

Модель не пишет код: правило проверяется строго. Вид события только из каталога (`policies/events.yaml`), фильтр по полю
только из `fields` этого вида, `boost` 1..3, срок 1..48 игровых часов, `switch_to` только из политик, в которые можно
перейти по просьбе (`Registry.requestable()`), не больше `MAX_RULES` правил. Невалидное правило отклоняется с причиной.

Срабатывание: событие того же вида с подходящим полем усиливает важность на `boost` (до 5) и, если задан `switch_to`, переводит
политику (`PolicyManager.rule_switch`). После срабатывания правило снимается. Срок истекает по игровому тику (2500 тиков в
игровом часе). Хранение: `memory/watch_rules.json`; его пишет MCP-сервер (инструменты `set_watch_rule`, `clear_watch_rule`),
читает и обновляет контроллер (перечитывает по времени изменения файла).

Замечание к ТЗ: POL-6 просит хранить правила в рабочей памяти (ТЗ 04c, `rimworld/scratch`). Слой рабочей памяти в ядре пока не
реализован (нет `working_memory.py`), поэтому правила лежат в файле модуля. Переезд — одна точка: `FILE`."""
from __future__ import annotations

import json
import os
import secrets
import time
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path

import policies

FILE = Path(__file__).parent / "memory" / "watch_rules.json"
MAX_RULES = 20
BOOST_RANGE = (1, 3)
TTL_H_RANGE = (1, 48)
TICKS_PER_HOUR = 2500
WHY_MAX = 200
FIRED_KEEP = 10


@dataclass
class WatchRule:
    id: str
    kind: str
    field: str
    value: str
    boost: int
    switch_to: str
    until_tick: int
    why: str
    created_tick: int

    def left_h(self, tick: int | None) -> float:
        if tick is None:
            return 0.0
        return max(0.0, round((self.until_tick - tick) / TICKS_PER_HOUR, 1))

    def public(self, tick: int | None) -> dict:
        return {**asdict(self), "hours_left": self.left_h(tick)}


def validate(args: dict, *, registry: policies.Registry | None, tick: int | None) -> tuple[WatchRule | None, str]:
    """Проверить правило из аргументов инструмента. (правило, '') или (None, причина отказа)."""
    reg = registry or policies.get()
    kind = str(args.get("kind") or "").strip()
    kd = reg.kind(kind)
    if kd is None:
        return None, f"нет вида события «{kind}». Виды: {', '.join(reg.kinds)}"
    field_name = str(args.get("field") or "").strip()
    value = str(args.get("value") or "").strip()
    if field_name and field_name not in kd.fields:
        return None, f"у вида «{kind}» нет поля «{field_name}». Поля: {', '.join(kd.fields) or 'нет'}"
    if field_name and not value:
        return None, f"для поля «{field_name}» нужно значение (value)"
    if value and not field_name:
        return None, "value без field: укажи поле, по которому искать"
    try:
        boost = int(args.get("boost", 1))
        ttl_h = int(args.get("ttl_h", 3))
    except (TypeError, ValueError):
        return None, "boost и ttl_h: целые числа"
    if not BOOST_RANGE[0] <= boost <= BOOST_RANGE[1]:
        return None, f"boost от {BOOST_RANGE[0]} до {BOOST_RANGE[1]}"
    if not TTL_H_RANGE[0] <= ttl_h <= TTL_H_RANGE[1]:
        return None, f"ttl_h (игровые часы) от {TTL_H_RANGE[0]} до {TTL_H_RANGE[1]}"
    switch_to = str(args.get("switch_to") or "").strip()
    if switch_to and switch_to not in reg.requestable():
        return None, f"switch_to: «{switch_to}» нельзя (можно: {', '.join(reg.requestable())})"
    why = str(args.get("why") or "").strip()[:WHY_MAX]
    if len(why) < 3:
        return None, "why: короткая причина (от 3 символов)"
    if tick is None:
        return None, "игра не отвечает: без текущего тика срок правила не посчитать"
    return WatchRule("w-" + secrets.token_hex(3), kind, field_name, value.lower(), boost, switch_to,
                     int(tick) + ttl_h * TICKS_PER_HOUR, why, int(tick)), ""


def matches(rule: WatchRule, kind: str, fields: dict) -> bool:
    if rule.kind != kind:
        return False
    if not rule.field:
        return True
    return rule.value in str(fields.get(rule.field, "")).lower()


class WatchRules:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path is not None else FILE
        self.rules: list[WatchRule] = []
        self.fired: deque = deque(maxlen=FIRED_KEEP)
        self._mtime: float | None = None
        self.load()

    def load(self) -> None:
        self.rules, self.fired = [], deque(maxlen=FIRED_KEEP)
        try:
            self._mtime = self.path.stat().st_mtime
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for item in (data.get("rules") or []) if isinstance(data, dict) else []:
            try:
                self.rules.append(WatchRule(**{k: item[k] for k in WatchRule.__dataclass_fields__}))
            except (KeyError, TypeError):
                continue
        self.rules = self.rules[:MAX_RULES]
        self.fired.extend((data.get("fired") or [])[-FIRED_KEEP:] if isinstance(data, dict) else [])

    def refresh(self) -> None:
        """Перечитать файл, если его изменил MCP-сервер."""
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            mtime = None
        if mtime != self._mtime:
            self.load()

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps({"rules": [asdict(r) for r in self.rules], "fired": list(self.fired)},
                                      ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
            self._mtime = self.path.stat().st_mtime
        except OSError:
            pass

    def purge(self, tick: int | None) -> list[WatchRule]:
        """Снять просроченные правила (по игровому тику). Возвращает снятые."""
        if tick is None:
            return []
        gone = [r for r in self.rules if r.until_tick <= tick]
        if gone:
            self.rules = [r for r in self.rules if r.until_tick > tick]
            self.save()
        return gone

    def match(self, kind: str, fields: dict) -> list[WatchRule]:
        return [r for r in self.rules if matches(r, kind, fields)]

    def consume(self, rule: WatchRule, text: str, tick: int | None) -> None:
        """Правило сработало: снять его и записать срабатывание для дашборда."""
        self.rules = [r for r in self.rules if r.id != rule.id]
        self.fired.append({"ts": round(time.time()), "tick": tick, "rule": rule.id, "why": rule.why, "text": text[:160],
                           "switch_to": rule.switch_to})
        self.save()

    def add(self, rule: WatchRule) -> str:
        """Добавить правило (или текст отказа)."""
        if len(self.rules) >= MAX_RULES:
            return f"ERROR: правил наблюдения уже {MAX_RULES} (максимум). Сними лишние: clear_watch_rule"
        self.refresh()
        self.rules.append(rule)
        self.save()
        return ""

    def remove(self, rule_id: str) -> bool:
        before = len(self.rules)
        self.rules = [r for r in self.rules if r.id != rule_id]
        if len(self.rules) != before:
            self.save()
            return True
        return False

    def public(self, tick: int | None) -> list[dict]:
        return [r.public(tick) for r in self.rules]

    def fired_public(self) -> list[dict]:
        return list(self.fired)

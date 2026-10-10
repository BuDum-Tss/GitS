"""Временные правила внимания (ТЗ 03, раздел 9): «пока играю, из телеги только мама», «час не трогай меня с чатами».

Правило сужает событие по модулю, шаблону вида (`fnmatch`) и подстроке имени отправителя; все заданные поля должны совпасть.
Эффекты: mute (совпавшие события уходят в сводку), boost (вес 2.0), only (совпавшие 1.5, несовпавшие события того же модуля
или всех модулей, если модуль не задан, 0.2), normal (отменить прежние правила для того же совпадения). Правила живут в
`memory/attention_rules.json` и переживают перезапуск; просроченные удаляются при загрузке и раз в минуту.
"""
import fnmatch
import json
import logging
import os
import secrets
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

logger = logging.getLogger("attention.rules")

EFFECTS = ("mute", "boost", "only", "normal")
W_BOOST, W_ONLY_MATCH, W_ONLY_OTHER = 2.0, 1.5, 0.2
FILE_NAME = "attention_rules.json"


@dataclass
class Rule:
    id: str                       # "r-" + 6 hex
    module: str | None
    kind: str | None              # шаблон fnmatch: "telegram.*"
    sender: str | None            # подстрока имени отправителя, без регистра
    effect: str                   # mute | boost | only | normal
    until: float
    created_from: str = ""        # реплика пользователя, из которой правило создано

    def matches(self, module: str, kind: str, sender_name: str | None) -> bool:
        """Подходит ли событие под все заданные поля правила."""
        if self.module and self.module != module:
            return False
        if self.kind and not fnmatch.fnmatchcase(kind, self.kind):
            return False
        if self.sender and self.sender.lower() not in (sender_name or "").lower():
            return False
        return True

    def covers(self, other: "Rule") -> bool:
        """Его выбор (модуль, вид, отправитель) не уже, чем у other: нужно для effect=normal."""
        if self.module and self.module != other.module:
            return False
        if self.kind and not (other.kind and fnmatch.fnmatchcase(other.kind, self.kind)):
            return False
        if self.sender and not (other.sender and self.sender.lower() in other.sender.lower()):
            return False
        return True

    def describe(self, now: float) -> str:
        parts = [p for p in (f"модуль {self.module}" if self.module else "", f"вид {self.kind}" if self.kind else "",
                             f"отправитель «{self.sender}»" if self.sender else "") if p]
        left = max(0, round((self.until - now) / 60))
        return f"{self.id}: {self.effect} ({', '.join(parts)}), ещё {left} мин"

    def public(self, now: float) -> dict:
        return {**asdict(self), "minutes_left": max(0, round((self.until - now) / 60))}


class RuleStore:
    def __init__(self, path: Path | str | None = None, *, clock: Callable[[], float] = time.time):
        self.path = Path(path) if path is not None else None
        self._clock = clock
        self.rules: list[Rule] = []
        self.load()

    # ── хранилище ────────────────────────────────────────────────────────────

    def load(self) -> None:
        """Прочитать файл; просроченные правила сразу отбрасываются. Битый файл не роняет ядро."""
        self.rules = []
        if self.path is None:
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            logger.warning(f"Правила внимания не прочитаны: {e}")
            return
        now = self._clock()
        for item in raw if isinstance(raw, list) else []:
            try:
                rule = Rule(id=str(item["id"]), module=item.get("module") or None, kind=item.get("kind") or None,
                            sender=item.get("sender") or None, effect=item["effect"], until=float(item["until"]),
                            created_from=str(item.get("created_from") or ""))
            except (KeyError, TypeError, ValueError):
                continue
            if rule.effect in EFFECTS and rule.effect != "normal" and rule.until > now:
                self.rules.append(rule)

    def save(self) -> None:
        """Атомарная запись (временный файл + замена). Блокирующая: звать через asyncio.to_thread."""
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps([asdict(r) for r in self.rules], ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as e:
            logger.warning(f"Правила внимания не сохранены: {e}")

    # ── правила ──────────────────────────────────────────────────────────────

    def purge(self) -> int:
        """Убрать просроченные. Возвращает сколько убрано."""
        now = self._clock()
        before = len(self.rules)
        self.rules = [r for r in self.rules if r.until > now]
        return before - len(self.rules)

    def active(self) -> list[Rule]:
        now = self._clock()
        return [r for r in self.rules if r.until > now]

    def add(self, *, module: str | None, kind: str | None, sender: str | None, effect: str, minutes: float,
            created_from: str = "") -> tuple[Rule | None, int]:
        """Создать правило. Для effect=normal правило не хранится: снимаются прежние правила с таким же или более узким
        совпадением. Возвращает (правило или None, сколько прежних правил снято)."""
        if effect not in EFFECTS:
            raise ValueError(f"effect: одно из {list(EFFECTS)}")
        rule = Rule("r-" + secrets.token_hex(3), module or None, kind or None, (sender or "").strip() or None, effect,
                    self._clock() + float(minutes) * 60, created_from[:200])
        removed = 0
        if effect == "normal":
            keep = [r for r in self.rules if not rule.covers(r)]
            removed = len(self.rules) - len(keep)
            self.rules = keep
            return None, removed
        self.rules.append(rule)
        return rule, removed

    def clear(self, rule_id: str | None = None) -> int:
        before = len(self.rules)
        self.rules = [r for r in self.rules if rule_id is not None and r.id != rule_id]
        return before - len(self.rules)

    # ── оценка события ───────────────────────────────────────────────────────

    def muted(self, module: str, kind: str, sender_name: str | None) -> Rule | None:
        for r in self.active():
            if r.effect == "mute" and r.matches(module, kind, sender_name):
                return r
        return None

    def weight(self, module: str, kind: str, sender_name: str | None) -> float:
        """w_rule: произведение весов действующих правил (mute здесь не участвует: R3 решает раньше)."""
        w = 1.0
        for r in self.active():
            if r.effect == "boost":
                if r.matches(module, kind, sender_name):
                    w *= W_BOOST
            elif r.effect == "only":
                if r.matches(module, kind, sender_name):
                    w *= W_ONLY_MATCH
                elif r.module is None or r.module == module:
                    w *= W_ONLY_OTHER
        return w

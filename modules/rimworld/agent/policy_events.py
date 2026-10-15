"""События для политик (ТЗ 11e, POL-3): превращение того, что происходит в игре, в события каталога `policies/events.yaml`.

Откуда берутся события:
  - очередь событий мода (`raid`, `death`, `fire`, `letter`, `game_loaded`, `colony_died`): `from_mod`;
  - важные строки разницы состояний (`statediff.compare`): `from_statediff`;
  - проверки контроллера (застрявшая задача, простой колонистов, просьба планировать): `make`.

Событие без важности (вид не из каталога или важность 0) отбрасывается до классификатора. Текстовые виды (`classify: true`)
уточняет классификатор GLiNER (`decider`): он выбирает вид из каталога по описанию; срочность по-прежнему берёт код из каталога,
потому что модель плохо различает порядковые оценки (см. ТЗ 03, ATT-4). Низкая уверенность или недоступность сервиса: вид остаётся.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import policies

CLASSIFY_MIN_CONF = 0.6      # ниже этой уверенности вид классификатора не берём
MAX_TEXT = 300

Decide = Callable[[str, dict], Awaitable["dict | None"]]


@dataclass
class Event:
    kind: str
    importance: int
    text: str
    fields: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
    classified: bool = False

    def describe(self) -> str:
        """Короткая строка для журнала и для подсказки хода."""
        return f"[{self.kind}, важность {self.importance}] {self.text[:MAX_TEXT]}"


def make(kind: str, text: str, fields: dict | None = None, reg: policies.Registry | None = None,
         importance: int | None = None) -> Event | None:
    """Событие по виду каталога. None, если вида нет или его важность 0 (такое событие не нужно никому)."""
    reg = reg or policies.get()
    k = reg.kind(kind)
    if k is None:
        return None
    imp = k.importance if importance is None else importance
    if imp <= 0:
        return None
    return Event(kind, min(5, max(0, imp)), (text or k.title)[:MAX_TEXT], dict(fields or {}))


def from_mod(evt: dict, reg: policies.Registry | None = None) -> Event | None:
    """Событие из очереди мода (`/events`). Вид и текст — факт, без реплики."""
    kind, d = evt.get("type"), evt.get("data") or {}
    if kind == "incident":
        label = d.get("label") or d.get("incident") or "угроза"
        return make("raid", f"Угроза колонии: {label}.", {"label": str(label)}, reg,
                    importance=5 if evt.get("priority") == "critical" else None)
    if kind == "pawn_died":
        name = d.get("name") or "?"
        return make("death", f"погиб колонист {name}", {"name": str(name), "cause": str(d.get("cause") or "")}, reg)
    if kind == "fire_started":
        return make("fire", "в колонии начался пожар", {}, reg)
    if kind == "letter":
        label = str(d.get("label") or "письмо")
        return make("letter", f"игровое письмо: {label}", {"label": label, "letter_def": str(d.get("letter_def") or "")}, reg)
    if kind == "game_loaded":
        return make("game_loaded", f"колония «{d.get('colony') or '?'}» загружена", {"colony": str(d.get("colony") or "")}, reg)
    if kind == "colony_died":
        return make("colony_died", f"вся колония «{d.get('colony') or '?'}» погибла", {"colony": str(d.get("colony") or "")}, reg)
    return None


def from_statediff(line: str, important: bool, reg: policies.Registry | None = None) -> Event | None:
    """Важная строка разницы состояний -> событие. Неважные строки и незнакомые по тексту не дают событий."""
    if not important:
        return None
    if line.startswith("появился враг:"):
        return make("raid", "на карте появился враг: " + line.split(":", 1)[1].strip(), {"label": "враг на карте"}, reg)
    if "упал(а) и лежит" in line:
        name = line.split(" упал", 1)[0]
        return make("pawn_downed", line, {"name": name}, reg)
    if line.startswith("исследование завершено:"):
        research = line.split(":", 1)[1].strip()
        return make("research_done", f"исследование завершено: {research}", {"research": research}, reg)
    if line.startswith("запас еды:"):
        return make("resource_out", line, {"resource": "food"}, reg)
    return None


def questions(reg: policies.Registry | None = None) -> dict:
    """Вопрос классификатору: какой это вид из каталога (метки английские, описания тоже)."""
    reg = reg or policies.get()
    return {"kind": {"type": "choice", "options": {k.id: k.describe for k in reg.kinds.values()},
                     "instructions": "Which kind of colony event is this?"}}


async def classify(ev: Event, decide: Decide | None = None, reg: policies.Registry | None = None) -> Event:
    """Уточнить вид текстового события классификатором. Без классификатора или при сбое возвращает событие как есть."""
    reg = reg or policies.get()
    k = reg.kind(ev.kind)
    if k is None or not k.classify or decide is None:
        return ev
    try:
        answers = await decide(ev.text[:MAX_TEXT], questions(reg))
    except Exception:
        return ev
    answer = (answers or {}).get("kind") if isinstance(answers, dict) else None
    if not isinstance(answer, dict):
        return ev
    label, conf = answer.get("answer"), float(answer.get("confidence") or 0.0)
    if label in reg.kinds and conf >= CLASSIFY_MIN_CONF and label != ev.kind:
        new = reg.kind(label)
        if new.importance <= 0:
            return ev
        return Event(label, new.importance, ev.text, {**ev.fields, "was": ev.kind}, ev.ts, classified=True)
    ev.classified = True
    return ev

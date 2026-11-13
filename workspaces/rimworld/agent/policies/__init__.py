"""Реестр политик игрового агента (ТЗ 11e, POL-2..POL-4, POL-11): каталог событий (`events.yaml`) и описания политик
(`<id>.yaml` рядом). Новая политика добавляется файлом, без правки контуров: реестр проверяет схему и ссылки (вид события,
политика, режим инструментов), и при ошибке в файле падает с понятным текстом, а не работает наполовину.

Политика описывает: когда входит (`enter`: виды событий и порог важности), когда и куда выходит (`exit`), куда её может
попросить переключиться сама модель (`switch_to`), как долго держится (`min_dwell_s`), каким временем игры управляет (`time`),
какая модель думает (`think`), какие режимы инструментов доступны (`modes`), бюджеты (`budget`). Приоритет: большее число
важнее (бой 4 > ручное 3 > планирование 2 > наблюдение 1 > вне игры 0). Ровно одна политика — `default` (вне игры)."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

import game_modes

DIR = Path(__file__).parent
FOCUS = ("background", "active")
LOOPS = ("passive", "single", "iterative")
THINK_MODELS = ("none", "local", "claude", "mixed")
EXIT_WHEN = ("never", "no_threats", "plan_done", "manual_done")
SOURCES = ("mod_event", "statediff", "monitor")
POLICY_KEYS = {"id", "title", "purpose", "priority", "default", "focus", "interruptible", "urgency", "enter", "exit",
               "switch_to", "min_dwell_s", "loop", "think", "modes", "time", "budget"}
KIND_KEYS = {"title", "describe", "source", "importance", "fields", "classify"}


class PolicyError(ValueError):
    """Ошибка в описании политики или каталоге событий: текст называет файл и поле."""


@dataclass(frozen=True)
class Kind:
    id: str
    title: str
    describe: str
    source: str
    importance: int
    fields: tuple
    classify: bool


@dataclass(frozen=True)
class Policy:
    id: str
    title: str
    purpose: str
    priority: int
    default: bool
    focus: str
    interruptible: bool
    enter_kinds: tuple
    enter_min: int
    exit_when: str
    exit_quiet_s: float
    exit_to: str
    switch_to: tuple
    min_dwell_s: float
    loop: str
    think: dict
    modes: tuple
    time: dict
    budget: dict
    urgency: float = 0.0                                          # срочность политики 0..1 (ТЗ 18, status.urgency)
    resources: dict = field(default_factory=dict)                 # ресурсы, которые тратит (gpu, tokens), 0..1


# Ресурсы политики выводятся из того, какая модель думает: локальная - видеокарта, Claude - токены (ТЗ 18, FOC-1).
_RESOURCES_BY_MODEL = {"local": {"gpu": 1}, "claude": {"tokens": 1}, "mixed": {"gpu": 1, "tokens": 1}, "none": {}}


def resources_of(think: dict) -> dict:
    return dict(_RESOURCES_BY_MODEL.get((think or {}).get("model"), {}))


class Registry:
    def __init__(self, kinds: dict[str, Kind], policies: dict[str, Policy]):
        self.kinds = kinds
        self.policies = policies
        defaults = [p.id for p in policies.values() if p.default]
        self.default: str = defaults[0]

    def kind(self, kind_id: str) -> Kind | None:
        return self.kinds.get(kind_id)

    def policy(self, policy_id: str) -> Policy | None:
        return self.policies.get(policy_id)

    def entering(self, kind: str, importance: int) -> Policy | None:
        """Политика, которую включает событие такого вида и важности (самая приоритетная). None, если никакая."""
        found = [p for p in self.policies.values() if kind in p.enter_kinds and importance >= p.enter_min]
        return max(found, key=lambda p: p.priority) if found else None

    def can_request(self, frm: str, to: str) -> bool:
        """Может ли модель попросить переход frm -> to (switch_to у исходной политики)."""
        p = self.policies.get(frm)
        return bool(p and to in p.switch_to)

    def requestable(self) -> tuple:
        """Политики, в которые можно перейти по просьбе (объединение switch_to)."""
        out: list[str] = []
        for p in self.policies.values():
            out.extend(t for t in p.switch_to if t not in out)
        return tuple(out)

    def modes_of(self, policy_id: str) -> tuple:
        """Режимы инструментов политики; пусто (вне игры) — режим наблюдения, чтобы ход не падал."""
        p = self.policies.get(policy_id)
        return (p.modes if p and p.modes else (game_modes.DEFAULT,))

    def transitions(self, policy_id: str) -> list[str]:
        """Переходы, которые ядро может запросить у политики (ТЗ 18, FOC-1, поле `transitions` манифеста). Из фоновой
        политики - только запрашиваемые по просьбе (`leave_afk`, см. `requestable`). Из активной - просьба модели
        (`switch_to`), уход в фоновую (`enter_afk`), условие выхода (`exit_to`) и политики, которые включают события
        не ниже по приоритету (события переводят так же, см. `policy_manager._may_enter`)."""
        p = self.policies[policy_id]
        if p.focus == "background":
            targets = set(self.requestable())
        else:
            targets = set(p.switch_to) | {self.default}
            if p.exit_to:
                targets.add(p.exit_to)
            targets |= {q.id for q in self.policies.values() if q.enter_kinds and q.priority >= p.priority}
        return [t for t in self.policies if t in targets and t != policy_id]

    def manifest(self) -> list[dict]:
        """Политики для манифеста модуля (ТЗ 18, FOC-1): формат проверяет SDK `validate_manifest`."""
        return [{"id": p.id, "title": p.title, "purpose": p.purpose, "focus": p.focus,
                 "transitions": self.transitions(p.id), "interruptible": p.interruptible,
                 "urgency": p.urgency, "resources": dict(p.resources)}
                for p in sorted(self.policies.values(), key=lambda p: p.priority)]

    def public(self) -> list[dict]:
        """Описание для дашборда и манифеста: id, название, фокус, переходы."""
        return [{"id": p.id, "title": p.title, "purpose": p.purpose, "focus": p.focus, "priority": p.priority,
                 "transitions": list(p.switch_to), "interruptible": p.interruptible, "default": p.default}
                for p in sorted(self.policies.values(), key=lambda p: p.priority)]


def _fail(where: str, msg: str):
    raise PolicyError(f"{where}: {msg}")


def _kind(raw_id: str, raw: dict, where: str) -> Kind:
    if not isinstance(raw, dict):
        _fail(where, "вид события должен быть объектом")
    unknown = set(raw) - KIND_KEYS
    if unknown:
        _fail(where, f"неизвестные поля {sorted(unknown)}")
    if raw.get("source") not in SOURCES:
        _fail(where, f"source должен быть одним из {SOURCES}")
    imp = raw.get("importance")
    if not isinstance(imp, int) or not 0 <= imp <= 5:
        _fail(where, "importance: целое от 0 до 5")
    fields = raw.get("fields") or []
    if not isinstance(fields, list) or not all(isinstance(f, str) for f in fields):
        _fail(where, "fields: список строк")
    return Kind(raw_id, str(raw.get("title") or raw_id), str(raw.get("describe") or raw_id), raw["source"], imp,
                tuple(fields), bool(raw.get("classify", False)))


def _policy(raw: dict, where: str, kinds: dict[str, Kind]) -> dict:
    """Проверка одной политики (без ссылок на другие политики: их проверяет load)."""
    unknown = set(raw) - POLICY_KEYS
    if unknown:
        _fail(where, f"неизвестные поля {sorted(unknown)}")
    for key in ("id", "title", "purpose", "priority", "focus", "enter", "exit", "switch_to", "min_dwell_s", "loop", "think", "modes"):
        if key not in raw:
            _fail(where, f"нет обязательного поля «{key}»")
    if not isinstance(raw["priority"], int) or raw["priority"] < 0:
        _fail(where, "priority: целое неотрицательное")
    if raw["focus"] not in FOCUS:
        _fail(where, f"focus: одно из {FOCUS}")
    if raw["loop"] not in LOOPS:
        _fail(where, f"loop: одно из {LOOPS}")
    enter = raw["enter"] or {}
    for k in enter.get("kinds") or []:
        if k not in kinds:
            _fail(where, f"enter.kinds: нет вида события «{k}» в events.yaml")
    if raw["exit"].get("when") not in EXIT_WHEN:
        _fail(where, f"exit.when: одно из {EXIT_WHEN}")
    think = raw["think"] or {}
    if think.get("model") not in THINK_MODELS:
        _fail(where, f"think.model: одно из {THINK_MODELS}")
    for mode in raw["modes"]:
        if mode not in game_modes.MODES:
            _fail(where, f"modes: нет режима инструментов «{mode}» (game_modes)")
    if not isinstance(raw["min_dwell_s"], (int, float)) or raw["min_dwell_s"] < 0:
        _fail(where, "min_dwell_s: число не меньше 0")
    urgency = raw.get("urgency", 0.0)
    if isinstance(urgency, bool) or not isinstance(urgency, (int, float)) or not 0 <= urgency <= 1:
        _fail(where, "urgency: число от 0 до 1")
    return raw


def _load_kinds(path: Path) -> dict[str, Kind]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw_kinds = data.get("kinds")
    if not isinstance(raw_kinds, dict) or not raw_kinds:
        _fail(str(path.name), "нужен непустой раздел kinds")
    return {kid: _kind(kid, raw, f"{path.name}:{kid}") for kid, raw in raw_kinds.items()}


def load(directory: Path = DIR) -> Registry:
    """Загрузить каталог и все политики из папки. Любая ошибка — PolicyError с именем файла и поля."""
    kinds = _load_kinds(directory / "events.yaml")
    policies: dict[str, Policy] = {}
    for path in sorted(directory.glob("*.yaml")):
        if path.name == "events.yaml":
            continue
        where = path.name
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            _fail(where, "файл политики должен быть объектом")
        _policy(raw, where, kinds)
        pid = raw["id"]
        if pid != path.stem:
            _fail(where, f"id «{pid}» должен совпадать с именем файла")
        if pid in policies:
            _fail(where, f"политика «{pid}» описана дважды")
        enter = raw["enter"] or {}
        exit_ = raw["exit"] or {}
        policies[pid] = Policy(
            id=pid, title=str(raw["title"]), purpose=str(raw["purpose"]), priority=raw["priority"],
            default=bool(raw.get("default", False)), focus=raw["focus"], interruptible=bool(raw.get("interruptible", True)),
            enter_kinds=tuple(enter.get("kinds") or []), enter_min=int(enter.get("min_importance", 0)),
            exit_when=exit_.get("when", "never"), exit_quiet_s=float(exit_.get("quiet_s", 0)), exit_to=str(exit_.get("to", "")),
            switch_to=tuple(raw["switch_to"] or []), min_dwell_s=float(raw["min_dwell_s"]), loop=raw["loop"],
            think=dict(raw["think"] or {}), modes=tuple(raw["modes"] or []), time=dict(raw.get("time") or {}),
            budget=dict(raw.get("budget") or {}), urgency=float(raw.get("urgency", 0.0)),
            resources=resources_of(dict(raw["think"] or {})))
    if not policies:
        _fail(directory.name, "нет ни одной политики")
    priorities = [p.priority for p in policies.values()]
    if len(set(priorities)) != len(priorities):
        _fail(directory.name, "приоритеты политик должны быть разными")
    if sum(p.default for p in policies.values()) != 1:
        _fail(directory.name, "ровно одна политика должна быть default")
    for p in policies.values():
        if p.exit_to and p.exit_to not in policies:
            _fail(p.id, f"exit.to: нет политики «{p.exit_to}»")
        for t in p.switch_to:
            if t not in policies:
                _fail(p.id, f"switch_to: нет политики «{t}»")
        if p.exit_when != "never" and not p.exit_to:
            _fail(p.id, "exit.to обязателен, если есть условие выхода")
    return Registry(kinds, policies)


_CACHE: list[Registry] = []


def get() -> Registry:
    """Реестр из папки политик (загружается один раз за процесс)."""
    if not _CACHE:
        _CACHE.append(load())
    return _CACHE[0]

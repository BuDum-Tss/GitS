"""Профили поведения (ТЗ 02, раздел 8): конфиги в agent/profiles/*.yaml и выбор активного профиля.

Профиль = тон, модули фокуса, области памяти и справочника, веса внимания и условия автовыбора. Активен ровно один;
включённых модулей может быть сколько угодно, а их фоновые задачи идут независимо от профиля. Инструмент модуля с
`availability: profile` виден планировщику, только когда модуль входит в `focus_modules` активного профиля.

Профиль это «состояние» (занятие), на котором она сфокусирована: ассистент, игра, сон. Из любого можно перейти в любое: само по
условиям или вручную. Сон такое же состояние: пока идёт разбор памяти (`sleeping()`), активен профиль `sleep`.

Выбор профиля (CORE-19), каждые 2 секунды и при смене состояния модулей:
  1. нет заряда Claude (`battery.sleeping`) или идёт сон памяти (`sleeping()`) -> `sleep` (сильнее ручного выбора);
  2. настройка `profile` не `auto`, такой профиль есть и он не `planned` -> он;
  3. иначе из профилей с выполненным условием `activate` берётся с наибольшим `priority` (при равенстве текущий, иначе
     первый по алфавиту);
  4. если подходящих нет, но текущий профиль перестал подходить меньше `sticky_s` назад -> остаётся текущий;
  5. иначе `assistant`.

Тон профиля (`tone`) добавляется к характеру в разговоре (`hint()`); добавка стримера (`_streamer_tone.txt`) идёт сверху,
когда включена настройка `streamer`. Сам характер (конституция) от профиля не зависит.

Менеджер не ходит в сеть и в систему сам: всё, что нужно для решения, ему дают функции-источники (`battery`,
`foreground_app`, `module_state`, `choice`), поэтому он проверяется тестами с фиктивными часами.
"""
import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

import yaml

import settings

logger = logging.getLogger("profiles")

PROFILES_DIR = Path(__file__).resolve().parent / "profiles"
HUB = "assistant"          # профиль по умолчанию (центр графа)
SLEEP = "sleep"            # особый профиль: нет заряда Claude
STREAMER_TONE_FILE = "_streamer_tone.txt"

# прежние значения настройки `profile` (до ТЗ 02) -> нынешние профили; stream профилем больше не является
LEGACY_CHOICES = {"game": "rimworld", "general": HUB, "work": "work"}

# Структура подграфа, пока модуль профиля не отвечает: войти в подграф можно всегда. Живой граф берётся у модуля.
DEFAULT_SUBGRAPHS = {
    "rimworld": {
        "nodes": [{"id": "agent", "label": "Агент RimWorld", "root": True, "tools": []},
                  {"id": "observe", "label": "Наблюдение и управление"}, {"id": "people", "label": "Колонисты: работы и режим дня"},
                  {"id": "build", "label": "Строительство"}, {"id": "economy", "label": "Хозяйство и ресурсы"},
                  {"id": "combat", "label": "Бой и тревога"}, {"id": "health", "label": "Здоровье и настроение"},
                  {"id": "explore", "label": "Разведка и знания"}, {"id": "show", "label": "Показ и камера"},
                  {"id": "screen", "label": "Экран (запасной интерфейс)"}],
        "edges": [{"from": "agent", "to": "observe", "label": "старт"}] +
                 [{"from": "observe", "to": m, "label": ""} for m in ("people", "build", "economy", "combat", "health", "explore", "show", "screen")],
    },
}


@dataclass(frozen=True)
class Profile:
    id: str
    title: str
    description: str = ""
    tone: str = ""                                   # добавка к характеру в разговоре
    focus_modules: tuple[str, ...] = ()              # инструменты availability=profile этих модулей видны
    memory_scopes: tuple[str, ...] = ()              # области памяти (к global всегда); используется в ТЗ 04
    knowledge: tuple[str, ...] = ()                  # коллекции справочника (ТЗ 04)
    attention: dict = field(default_factory=dict)    # {"weights": {шаблон kind: вес}} (ТЗ 03; здесь только хранится)
    activate: dict = field(default_factory=dict)     # {"foreground_apps": [...], "module_state": {модуль: {ключ: значение}}}
    priority: int = 0
    sticky_s: float = 0.0
    planned: bool = False                            # профиль ещё не написан: на графе виден, включить нельзя
    focus_quiet: tuple[str, ...] = ()                # модули, которым профиль не даёт переключать фокус (ТЗ 18, focus.quiet_modules)
    workspace: str = ""                              # среда агента (ТЗ 32): профиль включается, пока агент в этой среде


def _str_list(value, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise ValueError(f"{where}: должен быть списком строк")
    return tuple(value)


def parse_profile(data) -> Profile:
    """Профиль из словаря файла. ValueError с причиной, если формат неверный."""
    if not isinstance(data, dict):
        raise ValueError("профиль должен быть объектом")
    pid = data.get("id")
    if not isinstance(pid, str) or not pid.strip():
        raise ValueError("id: обязательная непустая строка")
    title = data.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("title: обязательная непустая строка")
    activate = data.get("activate") or {}
    if not isinstance(activate, dict):
        raise ValueError("activate: должен быть объектом")
    apps = [a.lower().removesuffix(".exe") for a in _str_list(activate.get("foreground_apps"), "activate.foreground_apps")]
    state = activate.get("module_state") or {}
    if not isinstance(state, dict) or not all(isinstance(v, dict) for v in state.values()):
        raise ValueError("activate.module_state: должен быть объектом {модуль: {ключ: значение}}")
    attention = data.get("attention") or {}
    if not isinstance(attention, dict):
        raise ValueError("attention: должен быть объектом")
    focus_cfg = data.get("focus") or {}
    if not isinstance(focus_cfg, dict):
        raise ValueError("focus: должен быть объектом")
    focus_quiet = _str_list(focus_cfg.get("quiet_modules"), "focus.quiet_modules")
    try:
        priority, sticky = int(data.get("priority", 0)), float(data.get("sticky_s", 0))
    except (TypeError, ValueError):
        raise ValueError("priority и sticky_s: числа")
    return Profile(
        id=pid.strip(), title=title.strip(), description=str(data.get("description") or "").strip(),
        tone=" ".join(str(data.get("tone") or "").split()),
        focus_modules=_str_list(data.get("focus_modules"), "focus_modules"),
        memory_scopes=_str_list(data.get("memory_scopes"), "memory_scopes"),
        knowledge=_str_list(data.get("knowledge"), "knowledge"), attention=attention,
        activate={"foreground_apps": apps, "module_state": state} if (apps or state) else {},
        priority=priority, sticky_s=sticky, planned=bool(data.get("planned", False)), focus_quiet=focus_quiet,
        workspace=str(data.get("workspace") or "").strip())


def load_profiles(directory: Path | str = PROFILES_DIR) -> dict[str, Profile]:
    """Все профили папки (*.yaml). Битый файл пропускается с предупреждением: ассистентка не должна падать из-за профиля.
    Профили `assistant` и `sleep` есть всегда (при отсутствии файла - минимальные)."""
    out: dict[str, Profile] = {}
    for path in sorted(Path(directory).glob("*.yaml")):
        try:
            profile = parse_profile(yaml.safe_load(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, yaml.YAMLError) as e:
            logger.warning(f"Профиль {path.name} пропущен: {e}")
            continue
        out[profile.id] = profile
    out.setdefault(HUB, Profile(HUB, "Ассистент", "Обычное состояние: разговор, память, управление компьютером."))
    out.setdefault(SLEEP, Profile(SLEEP, "Сон", "Токены Claude кончились: только лёгкий разговор."))
    return out


def load_streamer_tone(directory: Path | str = PROFILES_DIR) -> str:
    try:
        return " ".join((Path(directory) / STREAMER_TONE_FILE).read_text(encoding="utf-8").split())
    except OSError:
        return ""


def _norm_app(app) -> str:
    return str(app or "").lower().strip().removesuffix(".exe")


class ProfileManager:
    def __init__(self, profiles: dict[str, Profile] | None = None, *,
                 battery: Callable[[], dict] = lambda: {},
                 foreground_app: Callable[[], str | None] = lambda: None,
                 workspace_of: Callable[[], str] = lambda: "assistant",
                 module_state: Callable[[str], dict | None] = lambda module_id: None,
                 choice: Callable[[], str] | None = None,
                 streamer: Callable[[], bool] | None = None,
                 now: Callable[[], float] = time.time,
                 on_change: Callable[[str, str], None] | None = None,
                 streamer_tone: str | None = None,
                 subgraph: Callable[[Profile], Awaitable[dict | None]] | None = None):
        """battery() -> claude_battery.status(); foreground_app() -> имя процесса активного окна (модуль desktop, а до
        ТЗ 14: состояние модуля desktop); module_state(id) -> последний state.data модуля или None; choice() -> настройка
        profile; streamer() -> настройка streamer; on_change(старый, новый) вызывается при смене профиля;
        subgraph(profile) -> живой подграф профиля от его модуля (graph.get) или None."""
        self.profiles = profiles if profiles is not None else load_profiles()
        self._battery = battery
        self._foreground = foreground_app
        self._workspace = workspace_of
        self._module_state = module_state
        self._choice = choice or (lambda: str(settings.load().get("profile", "auto")))
        self._streamer = streamer or (lambda: bool(settings.load().get("streamer")))
        self._now = now
        self.on_change = on_change
        self._streamer_tone = load_streamer_tone() if streamer_tone is None else streamer_tone
        self.subgraph = subgraph
        self._current = HUB
        self._auto = HUB
        self._last_match: dict[str, float] = {}      # когда профиль в последний раз подходил по activate
        self._evaluated = False
        self.sleeping: Callable[[], bool] = lambda: False       # идёт ли сон памяти (ставит ядро)
        self.history: deque = deque(maxlen=40)       # переходы между состояниями: {ts, from, to, why}
        self._since = now()
        self._why = ""

    # ── выбор ───────────────────────────────────────────────────────────────

    def manual_choice(self) -> str | None:
        """Профиль, выбранный вручную (настройка profile), если он есть и написан; иначе None (auto)."""
        raw = str(self._choice() or "auto")
        raw = LEGACY_CHOICES.get(raw, raw)
        p = self.profiles.get(raw)
        return raw if p is not None and not p.planned and raw != SLEEP else None      # сон не выбирается настройкой: он бывает по делу

    def _matches(self, p: Profile) -> bool:
        act = p.activate
        if not act:
            return False
        apps = act.get("foreground_apps") or []
        if apps and _norm_app(self._foreground()) in apps:
            return True
        wanted = act.get("module_state") or {}
        if wanted:
            for module_id, pairs in wanted.items():
                data = self._module_state(module_id) or {}
                if not all(k in data and data[k] == v for k, v in pairs.items()):
                    return False
            return True
        return False

    def _pick_auto(self, now: float) -> str:
        # ТЗ 32, WS-5: профиль среды модуля (тон, видимые инструменты) включает сама среда; окно игры и состояние модуля нет
        ws = self._workspace()
        for p in self.profiles.values():
            if p.workspace and p.workspace == ws and not p.planned:
                return p.id
        matched = [p for p in self.profiles.values() if not p.planned and p.id != SLEEP and self._matches(p)]
        for p in matched:
            self._last_match[p.id] = now
        if matched:
            top = max(p.priority for p in matched)
            best = sorted(p.id for p in matched if p.priority == top)
            return self._current if self._current in best else best[0]
        cur = self.profiles.get(self._current)
        if (cur is not None and cur.id not in (HUB, SLEEP) and not cur.planned
                and now - self._last_match.get(cur.id, float("-inf")) < cur.sticky_s):
            return cur.id                                   # липкость: условия пропали недавно
        return HUB

    def evaluate(self) -> str:
        """Пересчитать активный профиль (CORE-19). Возвращает его id; при смене вызывает on_change."""
        now = self._now()
        battery_sleep = bool(self._battery().get("sleeping"))
        sleeping = battery_sleep or bool(self.sleeping())
        auto = SLEEP if sleeping else self._pick_auto(now)
        manual = None if sleeping else self.manual_choice()
        new = SLEEP if sleeping else (manual or auto)
        self._auto = auto
        old = self._current
        self._why = self._reason(new, manual, battery_sleep)
        if new != old or not self._evaluated:
            self._current, self._since = new, now
            if self._evaluated:
                logger.info(f"Профиль: {old} -> {new}" + (" (вручную)" if manual == new else ""))
                self.history.appendleft({"ts": now, "from": old, "to": new, "why": self._why})
                if self.on_change is not None:
                    try:
                        self.on_change(old, new)
                    except Exception as e:
                        logger.warning(f"Реакция на смену профиля: {e}")
            self._evaluated = True
        return new

    def _reason(self, new: str, manual: str | None, battery_sleep: bool) -> str:
        """Почему сейчас это состояние (для дашборда)."""
        if new == SLEEP:
            return "нет заряда Claude" if battery_sleep else "идёт разбор памяти"
        if manual == new:
            return "выбрано вручную"
        if new == HUB:
            return "по умолчанию: других занятий нет"
        return "сама: выполнены условия состояния"

    poke = evaluate      # событие смены окна или state модуля: пересчитать сразу

    async def run(self, interval: float = 2.0):
        """Пересчёт каждые `interval` секунд."""
        while True:
            try:
                self.evaluate()
            except Exception as e:
                logger.warning(f"Выбор профиля: {e}")
            await asyncio.sleep(interval)

    # ── чтение ──────────────────────────────────────────────────────────────

    @property
    def current_id(self) -> str:
        return self._current

    def current(self) -> Profile:
        return self.profiles.get(self._current) or self.profiles[HUB]

    def auto_id(self) -> str:
        return self._auto

    def focus_modules(self) -> set[str]:
        """Модули фокуса активного профиля: для Registry.focus."""
        return set(self.current().focus_modules)

    def info(self) -> dict:
        """Профиль для тем `profile` и `welcome`: {"id", "title", "focus_modules"}."""
        p = self.current()
        return {"id": p.id, "title": p.title, "focus_modules": list(p.focus_modules)}

    def hint(self) -> str:
        """Подсказка тона в промпт разговора (CORE-21): тон активного профиля и добавка стримера."""
        tone = self.current().tone
        if self._streamer() and self._streamer_tone:
            tone = f"{tone} {self._streamer_tone}".strip()
        # внутренняя настройка тона: о ней и о смене режима вслух не говорить, это не тема разговора
        return (f"[Внутренняя настройка тона, не тема разговора и не повод что-то сообщать: {tone} "
                "Никогда не упоминай режимы, профили и их переключение.]")

    def choices(self) -> list[str]:
        """Допустимые значения настройки profile: auto и все написанные профили."""
        return ["auto", *[p.id for p in self.profiles.values() if not p.planned]]

    def set_override(self, mode: str | None) -> str | None:
        """None или «auto»: вернуть автоматический выбор. Пишет настройку profile. Возвращает причину отказа или None."""
        if mode in (None, "", "auto"):
            settings.update(profile="auto")
            self.evaluate()
            return None
        mode = LEGACY_CHOICES.get(mode, mode)
        p = self.profiles.get(mode)
        if p is None:
            return f"нет такого профиля: {mode}"
        if p.planned:
            return f"профиль «{p.title}» ещё не написан"
        settings.update(profile=mode)
        self.evaluate()
        return None

    @property
    def override(self) -> str | None:
        return self.manual_choice()

    # ── состояния для дашборда ────────────────────────────────────────────────

    @staticmethod
    def _how(p: Profile) -> str:
        """Как наступает состояние (по условиям профиля): строка для карточки."""
        if p.planned:
            return "В планах, пока не написано"
        if p.id == SLEEP:
            return "Сама: простой, час расписания или нет заряда Claude. Или по кнопке «Уснуть сейчас»"
        if p.id == HUB:
            return "По умолчанию, когда нет других занятий"
        act, parts = p.activate or {}, []
        if act.get("foreground_apps"):
            parts.append("на экране: " + ", ".join(act["foreground_apps"]))
        for module_id, pairs in (act.get("module_state") or {}).items():
            parts.append(f"модуль {module_id}: " + ", ".join(f"{k} = {v}" for k, v in pairs.items()))
        return ("Сама, когда " + "; ".join(parts)) if parts else "Только вручную"

    def states(self) -> list[dict]:
        """Состояния (профили) для карточек: сначала ассистент, потом по приоритету, сон, затем ненаписанные."""
        cur, manual = self._current, self.manual_choice()

        def order(p: Profile):
            return (p.planned, p.id == SLEEP, p.id != HUB, -p.priority, p.id)
        return [{"id": p.id, "title": p.title, "description": p.description, "planned": p.planned, "current": p.id == cur,
                 "manual": p.id == manual, "how": self._how(p), "priority": p.priority, "modules": list(p.focus_modules)}
                for p in sorted(self.profiles.values(), key=order)]

    # ── граф для дашборда ─────────────────────────────────────────────────────

    async def graph(self) -> dict:
        """Узлы для дашборда (формат прежнего /api/modes): центр и профили; подграф профиля приходит от его модуля."""
        cur = self._current
        nodes = [{"id": p.id, "label": p.title, "title": p.description, "hub": p.id == HUB,
                  "planned": p.planned, "current": p.id == cur, "drill": True,
                  "modules": (["core", *p.focus_modules] if p.id == HUB else list(p.focus_modules))}
                 for p in self.profiles.values()]
        edges = [{"from": HUB, "to": p.id} for p in self.profiles.values() if p.id != HUB]
        lives = await asyncio.gather(*[self._live_subgraph(p) for p in self.profiles.values()])
        subs = {}
        for p, live in zip(self.profiles.values(), lives, strict=True):
            sub = live or DEFAULT_SUBGRAPHS.get(p.id)
            if sub:
                subs[p.id] = {**sub, "live": bool(live)}
        return {"current": cur, "auto": self._auto, "override": self.manual_choice(), "battery": self._battery(),
                "nodes": nodes, "edges": edges, "subgraphs": subs, "states": self.states(), "why": self._why,
                "since": self._since, "history": list(self.history)[:20]}

    async def _live_subgraph(self, profile: Profile) -> dict | None:
        if self.subgraph is None:
            return None
        try:
            data = await self.subgraph(profile)
        except Exception as e:
            logger.debug(f"Подграф профиля {profile.id}: {e}")
            return None
        return data if isinstance(data, dict) and data.get("nodes") is not None else None

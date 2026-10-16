"""Маршруты событий и жёсткие правила до модели (ТЗ 25, FAST-1, FAST-2). Документ: `docs/ROUTES.md`.

Вход события один: нормализация и дедупликация -> жёсткие правила (`HardRules`, YAML `routes/hard_rules.yaml`, без ML) ->
диспетчер (GLiNER и правила, Gemma только для неоднозначного, ТЗ 18) -> `routes[]`. Маршруты:
  reflex  мгновенное действие без разговора (оборвать речь, ответить на вопрос о разрешении, реакция D4); результат
          идёт в `already_done`, чтобы маршрут `chat` не повторял действие;
  chat    ответ или реплика (локальная модель, поток предложений в очередь речи);
  task    поручение: планировщик, модуль фокуса, приказ в игру (менеджер фокуса, ТЗ 18);
  ignore  ничего не говорить (сводка, отброшено);
  think   размышление (ТЗ 30).
Допустимы параллельные маршруты (`reflex` + `chat`). Решения внимания D0-D4 и правила R1-R4 отображаются таблицей
`DECISION_ROUTES`. Модуль не знает ни компаньона, ни настроек, ни часов.
"""
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

REFLEX, CHAT, TASK, IGNORE, THINK = "reflex", "chat", "task", "ignore", "think"
ROUTES = (REFLEX, CHAT, TASK, IGNORE, THINK)

# Решения менеджера внимания (ТЗ 03) и правила R1-R4 -> маршруты. D1 уходит в сводку без речи; D4 вдобавок к реплике
# даёт мгновенную реакцию (фокус reaction); R2 - ответ на вопрос; R3 - в сводку; R4 - срочное, объявленное модулем.
DECISION_ROUTES = {
    "D0": (IGNORE,), "D1": (IGNORE,), "D2": (CHAT,), "D3": (CHAT,), "D4": (REFLEX, CHAT),
    "R1": (CHAT,), "R2": (REFLEX,), "R3": (IGNORE,), "R4": (REFLEX, CHAT),
}

HARD_RULES_FILE = Path(__file__).with_name("routes") / "hard_rules.yaml"


@dataclass
class RouteDecision:
    """Итог входа события: маршруты, что уже сделано рефлексом, откуда решение."""
    routes: list[str] = field(default_factory=list)
    already_done: list[str] = field(default_factory=list)     # факты для chat: «речь остановлена» (код передаёт факт, не фразу)
    rule: str | None = None                                  # жёсткое правило или None
    echelon: str = "rules"                                   # rules / gliner / llm / hard
    reason: str = ""

    def has(self, route: str) -> bool:
        return route in self.routes

    def add(self, route: str) -> "RouteDecision":
        if route in ROUTES and route not in self.routes:
            self.routes.append(route)
        return self


def of_decision(decision: str | None, *, think: bool = False) -> list[str]:
    """Маршруты решения внимания (таблица `DECISION_ROUTES`); `think` добавляет размышление (ТЗ 30)."""
    out = list(DECISION_ROUTES.get(decision or "", (IGNORE,)))
    if think and THINK not in out:
        out.append(THINK)
    return out


def of_input(*, intent: str | None, looks_action: bool, flagged: bool = False, order_taken: bool = False,
             hard: "HardMatch | None" = None, has_router: bool = True) -> RouteDecision:
    """Маршруты обращения владельца. Жёсткое правило даёт `reflex` (+`chat`, если нужно ответить); поручение - `task`;
    остальное - `chat`. `flagged`: модель во время болтовни заподозрила просьбу (chat, затем task)."""
    d = RouteDecision(echelon="hard" if hard else ("gliner" if intent else "rules"))
    if hard is not None:
        d.rule, d.reason = hard.kind, hard.detail
        d.add(REFLEX)
        d.already_done.append(hard.fact)
        if hard.kind == "approval":
            return d                                         # ответ принадлежит вопросу: разговор не нужен
    if order_taken:
        return d.add(TASK)
    if intent == "about_self":
        return d.add(CHAT)
    if has_router and (looks_action or intent in ("action", "attention_rule")):
        return d.add(TASK)
    d.add(CHAT)
    if flagged and has_router:
        d.add(TASK)
    return d


# ── жёсткие правила ──────────────────────────────────────────────────────────

_WORD = re.compile(r"[a-zа-я0-9]+")


def normalize(text: str) -> str:
    """Нижний регистр, «ё» как «е», без знаков препинания."""
    return " ".join(_WORD.findall((text or "").lower().replace("ё", "е")))


@dataclass(frozen=True)
class HardMatch:
    kind: str          # stop | approval | emergency
    detail: str        # какое правило сработало (для журнала)
    fact: str          # факт для маршрута chat: что уже сделано (не готовая реплика)
    answer: str | None = None    # approval: yes | no


_STOP_FACT = "речь остановлена по просьбе владельца, генерация ответа прервана"


class HardRules:
    """Правила из YAML. `match(text, pending_question)` -> HardMatch или None. Не вызывает моделей и сеть."""

    def __init__(self, spec: dict):
        self.version = int(spec.get("version") or 0)
        stop = spec.get("stop") or {}
        self._stop_patterns = [re.compile(p) for p in stop.get("patterns") or []]
        self._stop_stems = tuple(stop.get("stems") or ())
        self._stop_words = int(stop.get("max_words") or 5)
        appr = spec.get("approval") or {}
        self._yes = tuple(normalize(x) for x in appr.get("accept") or [])
        self._no = tuple(normalize(x) for x in appr.get("decline") or [])
        self._appr_words = int(appr.get("max_words") or 3)
        emg = spec.get("emergency") or {}
        self._emg_stems = tuple(emg.get("stems") or ())
        self._emg_words = int(emg.get("max_words") or 6)

    @classmethod
    def load(cls, path: Path | None = None) -> "HardRules":
        spec = yaml.safe_load((path or HARD_RULES_FILE).read_text(encoding="utf-8")) or {}
        return cls(spec)

    @staticmethod
    def _stem_hit(words: list[str], stems: tuple[str, ...], truncated: bool = False) -> str | None:
        """Слово начинается с основы. truncated: подходит и обрыв основы не короче пяти букв и не короче основы без двух
        («замол» -> «замолч»); для экстренных слов обрыв не берётся: «помоги» - обычная просьба, а не «помогите»."""
        for w in words:
            for st in stems:
                if w.startswith(st) or (truncated and len(w) >= max(5, len(st) - 1) and st.startswith(w)):
                    return st
        return None

    def match(self, text: str, pending_question: bool = False) -> HardMatch | None:
        norm = normalize(text)
        if not norm:
            return None
        words = norm.split()
        if pending_question and len(words) <= self._appr_words:
            if norm in self._yes:
                return HardMatch("approval", f"yes:{norm}", "владелец ответил «да» на открытый вопрос о разрешении", "yes")
            if norm in self._no:
                return HardMatch("approval", f"no:{norm}", "владелец ответил «нет» на открытый вопрос о разрешении", "no")
        for rx in self._stop_patterns:
            if rx.match(norm):
                return HardMatch("stop", "pattern", _STOP_FACT)
        if len(words) <= self._stop_words:
            st = self._stem_hit(words, self._stop_stems, truncated=True)
            if st:
                return HardMatch("stop", f"stem:{st}", _STOP_FACT)
        if len(words) <= self._emg_words:
            st = self._stem_hit(words, self._emg_stems)
            if st:
                return HardMatch("emergency", f"stem:{st}", "сказано экстренное слово: речь остановлена, генерация прервана")
        return None


_cache: HardRules | None = None


def hard_rules() -> HardRules:
    """Правила из `routes/hard_rules.yaml` (читаются один раз; `reload()` перечитывает)."""
    global _cache
    if _cache is None:
        _cache = HardRules.load()
    return _cache


def reload() -> HardRules:
    global _cache
    _cache = HardRules.load()
    return _cache

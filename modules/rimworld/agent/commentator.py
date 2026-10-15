"""Комментатор игры: ГЛаДОС вслух говорит, что происходит, о чём думает и что планирует.

Работает на ЛОКАЛЬНОЙ модели (Gemma), отдельно от ходов Claude: ходы медленные и редкие, а комментарий должен идти
сразу и бесплатно. Два уровня:
  - обычный (по умолчанию в игре): реплика на заметное событие (колонист упал, налёт, постройка готова, враг пропал),
    не чаще раза в минуту для мелочей и быстрее для важного;
  - режим стримера (настройка «streamer»): плюс «живая» речь каждые ~35 секунд: что видит, мысли о колонии, планы,
    цель игры, задачи. Это генерация контента для стрима.
Тише воды, когда с ней разговаривают, она уже говорит, включён «не отвлекать» или игры нет. Повторы отсекаются.
"""
import json
import logging
import re
import time
from pathlib import Path

import events_log
import settings
from speech_text import clean_speech

logger = logging.getLogger("commentator")

BATTLE_GAP = 8             # сек между репликами в бою: опасность важнее всего
EVENT_GAP = 30            # сек между репликами при важном событии
CALM_GAP = 150            # сек между репликами по мелочам (обычный режим)
STREAM_GAP = 75           # сек между репликами в режиме стримера
CHAT_QUIET = 15           # сек тишины после реплики пользователя
STALE = 15                # сек: игра считается подключённой, если состояние обновлялось недавно
_TOPICS = ("цель игры и как к ней идёт колония", "план на ближайшие игровые часы", "что делают колонисты и кто чем занят",
           "наблюдение: что сейчас на экране и что в нём забавного или тревожного", "задачи, которые она себе поставила")
# Срочность события: 3 бой и угроза жизни (говорит почти сразу и только об этом), 2 заметное, 1 мелочь
_DANGER = re.compile(r"драк|дерёт|стреляет|враг|налёт|налет|рейд|угроз|упал|погиб|кровоточ|здоровье \d+% →|пожар|в бою|больше нет в колонии|"
                     r"схвач|тревога|срыв|на грани|осада|нападени", re.I)
_SILENT = re.compile(r"^\s*(<silent>|молчу|\(молчание\)|нечего сказать)\s*$", re.I)


class Commentator:
    def __init__(self, memory_dir: str, say, last_user_ts, is_speaking, ask=None, repeats=None):
        self._dir = Path(memory_dir)
        self._say = say                      # async (text): сказать через общий канал речи
        self._last_user_ts = last_user_ts    # () -> время последней реплики пользователя
        self._is_speaking = is_speaking      # () -> говорит ли она сейчас
        self._ask = ask                      # async (system, user) -> текст; по умолчанию локальная модель
        self._repeats = repeats or (lambda text: False)
        self.enabled = lambda: True          # ТЗ 11e POL-11: вне фокуса модуля комментатор молчит и не тратит модель
        self._pending: list[tuple[float, str, bool]] = []     # (когда, что произошло, важно ли)
        self._last_comment = 0.0
        self._topic = 0
        self._recent: list[str] = []
        self._history: list[tuple[float, str]] = []         # что уже происходило недавно: контекст для новых событий
        self.state_ts = 0.0                  # когда контроллер последний раз видел игру
        self.colony = ""
        self.colonists = ""                  # «Sean: работает; Vicky: бродит без дела»

    # ── вход от контроллера ───────────────────────────────────────────────────

    @staticmethod
    def severity(text: str, important: bool) -> int:
        base = 3 if _DANGER.search(text) else 2 if important else 1
        import appraisal
        ap = appraisal.recent(30)          # динамическая оценка событий может поднять важность (серия, нарастание)
        return max(base, 3 if ap and ap.importance >= 3 else 2 if ap and ap.importance >= 2 and important else 1)

    def feed(self, text: str, important: bool):
        if any(p[1] == text for p in self._pending):
            return                                            # то же самое уже ждёт своей очереди
        self._pending.append((time.time(), text, important))
        if important:
            self._history = [(t, x) for t, x in self._history if time.time() - t < 600 and x != text][-8:] + [(time.time(), text)]
        self._pending = self._pending[-30:]

    def update_state(self, colony: str, colonists: str):
        self.state_ts, self.colony, self.colonists = time.time(), colony, colonists

    # ── факты для реплики ────────────────────────────────────────────────────

    def _read(self, name, default):
        try:
            return json.loads((self._dir / name).read_text(encoding="utf-8"))
        except Exception:
            return default

    def _goals_block(self) -> str:
        goals = [g for g in self._read("goals.json", []) if g.get("status") == "active"]
        amb = [g for g in goals if g.get("category") == "ambition"]
        rest = [g for g in goals if g.get("category") != "ambition"][:3]
        lines = []
        if amb:
            lines.append(f"Главная цель игры: {amb[-1].get('title')} (прогресс {amb[-1].get('progress_pct', 0)}%)")
        lines += [f"Цель: {g.get('title')} ({g.get('progress_pct', 0)}%)" for g in rest]
        tasks = [t for t in self._read("tasks.json", []) if t.get("status") in ("in_progress", "pending")][:3]
        lines += [f"Задача: {t.get('description', '')[:110]}" for t in tasks]
        return "\n".join(lines)

    def _build_prompt(self, topic: str, events: list[str]) -> str:
        import agent_mode
        parts = [agent_mode.line()]
        if events:
            parts.append("Что только что произошло:\n" + "\n".join(f"- {e}" for e in events[-6:]))
        earlier = [x for t, x in self._history if x not in events and (_DANGER.search(x) or "опасность" not in topic)][-5:]
        if earlier:
            parts.append("Что было незадолго до этого (контекст: не называй происходящее «каким-то маневром», если это "
                         "следствие вот этого):\n" + ("\n".join(f"- {e}" for e in earlier)))
        if self.colonists:
            parts.append(f"Колония «{self.colony}», колонисты: {self.colonists}")
        goals = self._goals_block()
        if goals:
            parts.append(goals)
        import action_journal
        done = action_journal.recent_text(45, 8, speech=True)
        if done:
            parts.append("Что ты САМА недавно поставила и приказала (вся эта мебель, чертежи и пометки на карте твои: "
                         "не говори, что они появились сами или кто-то решил):\n" + done)
        import appraisal
        ap, mood = appraisal.recent(), appraisal.AFFECT.mood_hint()
        if ap:
            parts.append("Оценка быстрой модели (подстрой тон и длину под неё): " + ap.tone() + (chr(10) + mood if mood else ""))
        parts.append(f"Тема этой реплики: {topic}.")
        if self._recent:
            parts.append("Ты уже говорила (не повторяйся и не пересказывай то же):\n" + "\n".join(f"- {t}" for t in self._recent[-5:]))
        return "\n\n".join(parts)

    def _system(self, stream: bool) -> str:
        import prompts
        return prompts.streamer_persona() if stream else prompts.commentary_persona()

    def _said_before(self, text: str) -> bool:
        """Своя проверка повтора: слишком похоже на любую из последних её реплик в игре."""
        from memory_store import stems
        new = stems(text)
        if len(new) < 3:
            return False
        return any(old and len(new & old) / min(len(new), len(old)) >= 0.45 for old in (stems(t) for t in self._recent))

    # ── один шаг ──────────────────────────────────────────────────────────────

    def _may_speak(self, now: float) -> bool:
        if now - self.state_ts > STALE or settings.quiet_active():
            return False
        return not (now - self._last_user_ts() < CHAT_QUIET or self._is_speaking())

    async def step(self) -> str | None:
        """Решить, говорить ли сейчас, и сказать. Возвращает реплику или None."""
        now = time.time()
        if not self._may_speak(now):
            return None
        stream = bool(settings.load().get("streamer"))
        since = now - self._last_comment
        sev = {p[1]: self.severity(p[1], p[2]) for p in self._pending}
        top = max(sev.values(), default=0)
        important = [p for p in self._pending if p[2]]
        letters = [p[1] for p in self._pending if p[1].startswith("Письмо:")]
        if letters and top < 3 and since >= EVENT_GAP:
            topic, events = "реакция на письмо из игры (событие в колонии)", letters      # свадьба, ссора, гости: об этом и речь
        elif top >= 3 and since >= BATTLE_GAP:
            topic = "опасность и бой, реакция сразу и по делу"
            events = [p[1] for p in self._pending if sev[p[1]] >= 3]       # мелочи (стены, ресурсы) в бою не мешают
        elif important and since >= EVENT_GAP:
            topic, events = "реакция на только что случившееся", [p[1] for p in sorted(self._pending, key=lambda p: -sev[p[1]])]
        elif stream and since >= STREAM_GAP:
            topic, events = _TOPICS[self._topic % len(_TOPICS)], [p[1] for p in self._pending]
            self._topic += 1
        elif len(self._pending) >= 4 and since >= CALM_GAP:      # накопилось достаточно мелочей
            topic, events = "реакция на изменения в колонии", [p[1] for p in self._pending]
        else:
            return None
        import appraisal
        ap = appraisal.recent(60)
        if ap and ap.comment == "silent" and top < 3 and not stream and not letters:
            self._pending.clear()             # рутина по оценке модели: молчим
            self._last_comment = now - EVENT_GAP
            return None
        ask = self._ask
        if ask is None:
            import fast_brain

            async def ask(system, user):
                return await fast_brain._ask(system, user, timeout=60, max_tokens=220, temperature=0.9, background=True)
        try:
            text = (await ask(self._system(stream), self._build_prompt(topic, events))).strip()
        except Exception as e:
            logger.info(f"Комментатор: модель не ответила: {e}")
            self._last_comment = now - EVENT_GAP          # не долбить недоступную модель каждые пять секунд
            return None
        text = clean_speech(text)
        self._pending.clear()
        if not text or _SILENT.match(text) or self._repeats(text) or self._said_before(text):
            self._last_comment = now - EVENT_GAP
            return None
        self._recent = (self._recent + [text])[-8:]
        self._last_comment = time.time()
        await self._say(text)
        events_log.log("proactive", "комментарий к игре: " + text[:160], topic=topic, stream=stream)
        return text

    async def run(self):
        import asyncio
        while True:
            await asyncio.sleep(5)
            if not self.enabled():
                continue
            try:
                await self.step()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"Комментатор: {e}")

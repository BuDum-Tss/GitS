"""Менеджер внимания (ТЗ 03): что из происходящего заслуживает её слов, когда и насколько срочно.

С ТЗ 29 (ARC-4, шаг R3) класс `Attention` это тонкий фасад: логика лежит в компонентах пакета `app/`, формулы в `domain/`.

Каждое событие модуля, каждая речь модуля, вопрос, задача и собственная проактивная реплика проходят здесь:

  1. правила до классификатора (ATT-7, `app/process_event.py`): R1 обращение владельца -> разговор; R2 ответ на вопрос в том же
     канале; R3 временное правило mute -> в сводку; R4 критичное (urgency_hint 5 и объявлено в манифесте) -> срочность 5;
  2. срочность 0-5 (ATT-8, `app/event_classifier.py`): подсказка модуля и категория от классификатора (`decide.py`);
  3. приоритет 0-100 = срочность * 20 * вес профиля * вес отправителя * вес правил * вес состояния пользователя * свежесть
     (ATT-9, `app/scorer.py`, `domain/scoring.py`) и решение D0-D4 по порогам (ATT-10, `app/dispatcher.py`);
  4. очередь речи (`speech_queue.py`, `app/speech_coordinator.py`) и стек фокуса (`app/focus_stack.py`): ATT-13-ATT-23;
  5. временные правила, сводка (`app/digest.py`), быстрый путь «замолчи», классификация обращений пользователя (ATT-24-ATT-28);
  6. объявления о долгих запусках (`app/announce.py`, AN-1-AN-3): события `<модуль>.launch`, речь и `/api/notify` с пометкой
     `announce`, инструменты с `announce: true` идут мимо формулы важности отдельным видом `announce` (приоритет 75, срок 180 с,
     без `min_gap`): каждое объявление должно прозвучать;
  7. вопросы (`app/questions.py`) и ход задач модулей (`app/jobs.py`).

Классификатор только подсказывает: без него (сервис не запущен, настройка выключена, ошибка) всё работает по подсказкам
модуля. Он вызывается асинхронно с коротким тайм-аутом и не блокирует цикл событий (ATT-32). Реплики формирует модель по
факту (`companion.speak_about`); код передаёт только факты.

Интерфейс для шины и запросов ядра прежний (ТЗ 02 §9.4): `submit`, `submit_speech`, `submit_question`, `on_job_update`.
Люди и память читаются через контракты `PeopleDirectory` и `MemoryWriter` (`contracts.py`).
"""
import asyncio
import logging
import time
from typing import Any, Callable

import decide
from adapters.approver import ApprovalsModule
from adapters.classifier_gliner import GlinerClassifier
from adapters.event_log_sqlite import SqliteEventLog
from adapters.memory import writer_for_companion
from adapters.people import people_for_companion
from adapters.settings_provider import SettingsModule
from app.announce import Announcer, is_announce_event
from app.attention_core import (  # noqa: F401  (прежние имена модуля: их импортируют ядро, модули и тесты)
    ANNOUNCE_DEFAULT_URGENCY,
    CONVERSATION_FOCUS_P,
    CONVERSATION_P,
    DEFAULT_HALF_LIFE,
    DEFAULT_THRESHOLDS,
    FOCUS_DEPTH,
    PROACTIVE_P,
    QUESTION_ANNOUNCE_P,
    RULES_PURGE_S,
    SPEECH_DEFAULT_URGENCY,
    TEXT_LOG,
    TEXT_MAX,
    TICK_S,
    AttentionContext,
    Focus,
    Item,
    NullPresence,
    NullSpeech,
    Question,
    clamp,
)
from app.decision_log import DecisionLog
from app.digest import Digest
from app.dispatcher import Dispatcher
from app.event_classifier import EventClassifier
from app.focus_stack import FocusStack
from app.items import ItemStore, event_fact
from app.jobs import JobTracker
from app.normalizer import Normalizer
from app.process_event import EventPipeline, HardRules
from app.questions import QuestionService
from app.scorer import Scorer
from app.speech_coordinator import SpeechCoordinator
from attention_rules import FILE_NAME as RULES_FILE
from attention_rules import RuleStore
from domain import rules
from speech_queue import INTERRUPT_P, SpeechCandidate

import settings

logger = logging.getLogger("attention")
_clamp = clamp
_NullSpeech = NullSpeech
_NullPresence = NullPresence


class _Via:
    """Свойство фасада, которое читает и пишет поле компонента: `_Via("ctx", "hub")` -> `self.ctx.hub`."""

    def __init__(self, component: str, field: str):
        self.component, self.field = component, field

    def __get__(self, obj, owner=None):
        return self if obj is None else getattr(getattr(obj, self.component), self.field)

    def __set__(self, obj, value) -> None:
        setattr(getattr(obj, self.component), self.field, value)


class Attention:
    # поля, которые ядро и тесты читают и подставляют снаружи; живут в общем контексте или в компонентах
    companion = _Via("ctx", "companion")
    io = _Via("ctx", "io")
    decider = _Via("ctx", "decider")
    hub = _Via("ctx", "hub")                          # ModuleHub: соединения для доставки вопросов, ставит ядро
    modules = _Via("ctx", "modules")                  # ModuleRegistry: манифесты, включённость
    profiles = _Via("ctx", "profiles")                # ProfileManager: веса профиля
    rules = _Via("ctx", "rules")
    presence = _Via("ctx", "presence")
    kind_handlers = _Via("ctx", "kind_handlers")      # kind -> async (Item) -> None: служебные события (ТЗ 11, RW-9)
    kind_hooks = _Via("ctx", "kind_hooks")            # то же с побочным действием (ТЗ 12, VOICE-14)
    on_decided = _Via("ctx", "on_decided")            # ТЗ 18: решение (item, decision, priority, rule) -> менеджер фокуса
    policy_options = _Via("ctx", "policy_options")    # ТЗ 18, первый эшелон: варианты подсказки политики
    silent_kinds = _Via("ctx", "silent_kinds")        # виды, которые не пишутся в журнал module_event
    last_decision = _Via("ctx", "last_decision")      # для отладки и дашборда
    decisions = _Via("ctx", "decisions")
    _clock = _Via("ctx", "clock")
    _settings = _Via("ctx", "settings")
    _idle = _Via("ctx", "idle")
    _log = _Via("ctx", "log")
    _people = _Via("ctx", "people")
    _writer = _Via("ctx", "writer")
    _approver = _Via("ctx", "approver")
    _autopump = _Via("ctx", "autopump")
    _tasks = _Via("ctx", "tasks")
    _last_user_text = _Via("ctx", "last_user_text")
    speech = _Via("coordinator", "speech")             # SpeechQueue
    _items = _Via("store", "items")
    _by_key = _Via("store", "by_key")
    _digest = _Via("digest_", "buffers")
    _focus = _Via("focus_", "stack")
    _reaction_focus = _Via("focus_", "reaction_focus")
    _cls_pending = _Via("classifier", "pending")
    _job_states = _Via("jobs", "states")
    _announces = _Via("announcer", "recent")
    _question_channel = _Via("questions", "channel")   # куда доставлен вопрос (ответ принимается правилом R2)

    def __init__(self, companion, io, *, clock=time.time, decider=None,
                 settings_source: Callable[[], dict] | None = None, rules_file=None,
                 idle_source: Callable[[], float] | None = None, autopump: bool = True,
                 people=None, memory_writer=None, event_log=None, settings_provider=None, approver=None):
        """companion - Companion (say, speak_about, speech, presence); io - ConversationIO. decider - классификатор
        (Classifier; в тестах заглушка); settings_source - откуда брать настройки; rules_file - файл правил (по
        умолчанию memory/attention_rules.json); idle_source - секунды простоя ввода; autopump=False: планировщик речи
        запускается только вручную (`await attention.speech.pump()`), без фоновых задач.
        Контракты (ТЗ 29, ARC-3): people - PeopleDirectory, memory_writer - MemoryWriter, event_log - EventLog,
        settings_provider - SettingsProvider, approver - Approver. Без них берутся публичные свойства компаньона и адаптеры."""
        provider = settings_provider if settings_provider is not None else SettingsModule()
        ctx = self.ctx = AttentionContext(
            companion=companion, io=io, clock=clock, decider=decider if decider is not None else GlinerClassifier(),
            settings_provider=provider, settings_source=settings_source or provider.load,
            log=event_log if event_log is not None else SqliteEventLog(),
            approver=approver if approver is not None else ApprovalsModule(),
            people=people if people is not None else people_for_companion(companion),
            writer=memory_writer if memory_writer is not None else writer_for_companion(companion),
            idle=idle_source or self._default_idle, autopump=autopump,
            rules=RuleStore(rules_file if rules_file is not None else settings.memory_dir() / RULES_FILE, clock=clock),
            presence=getattr(companion, "presence", None) or NullPresence())
        self._loop_task: asyncio.Task | None = None
        self._last_purge = clock()
        # компоненты (ТЗ 29, ARC-4); связи между ними ставятся ниже, потому что они ссылаются друг на друга
        self.store = ItemStore(ctx)
        self.digest_ = Digest(ctx)
        self.focus_ = FocusStack(ctx)
        self.coordinator = SpeechCoordinator(ctx, self.store, self.digest_, self.focus_)
        self.store.speech = self.digest_.speech = self.focus_.speech = self.coordinator
        self.store.digest = self.focus_.digest = self.digest_
        self.decision_log = DecisionLog(ctx)
        self.scorer = Scorer(ctx, self.focus_)
        self.dispatcher = Dispatcher(ctx, self.scorer, self.digest_, self.coordinator, self.focus_, self.decision_log)
        self.classifier = EventClassifier(ctx, self.dispatcher)
        self.questions = QuestionService(ctx, self.coordinator, self.focus_)
        self.announcer = Announcer(ctx, self.store, self.digest_, self.coordinator, self.decision_log)
        self.normalizer = Normalizer(ctx)
        self.hard_rules = HardRules(ctx, self.dispatcher, self.decision_log, self.questions)
        self.pipeline = EventPipeline(ctx, self.normalizer, self.store, self.hard_rules, self.classifier, self.dispatcher,
                                      self.announcer, self.decision_log)
        self.jobs = JobTracker(ctx, self.store, self.dispatcher)

    # ── служебное ────────────────────────────────────────────────────────────

    @staticmethod
    def _default_idle() -> float:
        """Без источника простоя (ядро ставит его из state модуля desktop, ТЗ 14 DESK-7) пользователь считается за компьютером."""
        return 0.0

    def _spawn(self, coro) -> asyncio.Task:
        return self.ctx.spawn(coro)

    async def settle(self) -> None:
        """Дождаться фоновых задач (для тестов и остановки)."""
        while self._tasks or self.speech._tasks:
            await asyncio.gather(*list(self._tasks), *list(self.speech._tasks), return_exceptions=True)

    async def start(self) -> None:
        """Фоновый цикл: планировщик речи, срок жизни фокусов, чистка правил."""
        if self._loop_task is None:
            self._loop_task = asyncio.get_running_loop().create_task(self._loop())

    async def stop(self) -> None:
        if self._loop_task is not None:
            self._loop_task.cancel()
            self._loop_task = None
        for t in list(self._tasks):
            t.cancel()

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"Цикл внимания: {e}")
            await asyncio.sleep(TICK_S)

    async def tick(self) -> None:
        """Один проход цикла: сроки фокусов, чистка правил, сводка, планировщик речи."""
        now = self._clock()
        self._focus_tick(now)
        if now - self._last_purge >= RULES_PURGE_S:
            self._last_purge = now
            if self.rules.purge():
                await asyncio.to_thread(self.rules.save)
        await self.coordinator.pump()

    def _att(self, cfg: dict | None = None) -> dict:
        """Настройки attention с проверкой значений: испорченное заменяется значением по умолчанию."""
        return self.ctx.att(cfg)

    # ── события, речь модулей, объявления, задачи ───────────────────────────

    async def submit(self, module_id: str, event: dict) -> None:
        """Событие модуля (тело `event`, ТЗ 01 раздел 7.1)."""
        await self.pipeline.submit(module_id, event)

    async def submit_speech(self, module_id: str, say: dict) -> None:
        """Речь модуля (тело `say`, ТЗ 01 раздел 7.2): проходит ту же оценку важности, затем очередь речи."""
        await self.pipeline.submit_speech(module_id, say)

    @staticmethod
    def is_announce_event(kind: str, body: dict) -> bool:
        """Событие - объявление о запуске: вид `<модуль>.launch` или явная пометка `announce: true`."""
        return is_announce_event(kind, body)

    async def submit_announce(self, module_id: str, text: str, *, data: dict | None = None) -> None:
        """Объявление о запуске от самого ядра (инструмент модуля с `announce: true`, AN-3)."""
        await self.announcer.submit(module_id, text, data=data)

    async def on_job_update(self, module_id: str, job: dict) -> None:
        """Ход задачи модуля (тело `job.update`, ТЗ 01 раздел 9, ATT-12)."""
        await self.jobs.on_update(module_id, job)

    async def submit_question(self, q: Question) -> Any:
        """Задать вопрос пользователю (ATT-19). Возвращает (answer, by), как approvals.ask."""
        return await self.questions.submit(q)

    # ── сводка ───────────────────────────────────────────────────────────────

    def digest_counts(self) -> dict[str, int]:
        return self.digest_.counts()

    def take_digest(self, module: str | None = None) -> str:
        """Что накопилось в сводке (ATT-26): текст-факт; после выдачи буфер очищается."""
        return self.digest_.take(module)

    # ── правила (инструменты ядра) ───────────────────────────────────────────

    async def set_rule(self, *, module: str | None = None, kind: str | None = None, sender: str | None = None,
                       effect: str, minutes: float = 60) -> str:
        from environments.base import ToolError
        if not (module or kind or sender):
            raise ToolError("укажи хотя бы одно из: модуль, вид события, отправитель", "bad_request")
        if effect not in ("mute", "boost", "only", "normal"):
            raise ToolError("эффект: mute, boost, only или normal", "bad_request")
        minutes = clamp(float(minutes), 1, 1440)
        rule, removed = self.rules.add(module=module, kind=kind, sender=sender, effect=effect, minutes=minutes,
                                       created_from=self._last_user_text)
        await asyncio.to_thread(self.rules.save)
        self._log.log_nowait("attention", f"правило {effect}: {module or ''} {kind or ''} {sender or ''}".strip(),
                             rule=rule.id if rule else None, effect=effect, removed=removed, minutes=minutes)
        if rule is None:
            return f"Прежние правила для этого совпадения сняты: {removed}."
        return f"Правило создано: {rule.describe(self._clock())}."

    def rules_text(self) -> str:
        now = self._clock()
        active = self.rules.active()
        if not active:
            return "Временных правил внимания сейчас нет."
        return "Действующие правила внимания:\n" + "\n".join("- " + r.describe(now) for r in active)

    async def clear_rules(self, rule_id: str | None = None) -> str:
        n = self.rules.clear(rule_id)
        if n:
            await asyncio.to_thread(self.rules.save)
        return f"Снято правил: {n}."

    def snapshot(self) -> dict:
        """Для дашборда: правила, очередь речи, фокус, сводка, последние решения."""
        now = self._clock()
        return {"rules": [r.public(now) for r in self.rules.active()], "queue": self.speech.snapshot(),
                "focus": [{"id": f.id, "kind": f.kind, "topic": f.topic, "priority": f.priority} for f in self._focus],
                "digest": self.digest_counts(), "decisions": list(self.decisions)[-20:], "settings": self._att()}

    # ── обращения пользователя ───────────────────────────────────────────────

    @staticmethod
    def looks_like_stop(text: str) -> bool:
        """Короткая просьба замолчать: быстрый путь без модели (ATT-18). Правило в domain/rules.py."""
        return rules.looks_like_stop(text)

    async def reset_for_character(self) -> int:
        """Переключение персонажа (ТЗ 05, CHR-7): очередь речи очищается целиком, стек фокуса — до фонового состояния.
        Конец текущей реплики ждёт вызывающий код. Возвращает число снятых кандидатов."""
        n = await self.speech.stop_all(10_000)
        self.focus_.reset()
        self._log.log_nowait("attention", "очередь речи и фокус сброшены: смена персонажа", dropped=n)
        return n

    async def stop_speech(self, why: str = "stop") -> int:
        """«Замолчи» (ATT-18): речь обрывается немедленно, очередь очищается от кандидатов ниже 85."""
        n = await self.speech.stop_all(INTERRUPT_P)
        self._log.log_nowait("attention", "речь остановлена по просьбе пользователя", stop=True, why=why, dropped=n)
        return n

    async def on_user_input(self, text: str) -> None:
        """Обращение пользователя пришло (ATT-20): фокус conversation; «замолчи» срабатывает до любых вызовов модели."""
        self._last_user_text = text[:200]
        self.focus_.touch_conversation(text)
        if self.looks_like_stop(text):
            await self.stop_speech("phrase")

    async def classify_input(self, text: str) -> decide.InputClass | None:
        """ATT-28: намерение обращения до маршрутизатора. None - классификатора нет или он не уверен (всё как раньше)."""
        return await self.classifier.classify_input(text, self.stop_speech)

    def set_user_speaking(self, speaking: bool) -> None:
        """Модуль voice (событие `voice.user_speaking`, VOICE-8) сообщает, говорит ли пользователь сейчас: нужно условию 2
        очереди речи. Когда человек закончил говорить, обновляется и `last_user_ts`."""
        self.presence.update(user_speaking=bool(speaking))
        if not speaking:
            self.presence.update(last_user_ts=self._clock())

    # ── речь: вход для компаньона ────────────────────────────────────────────

    def enqueue_say(self, text: str, emotion: str | None = None, priority: int | None = None, *,
                    source: str | None = None) -> SpeechCandidate:
        """Готовая реплика компаньона -> очередь (ATT-13)."""
        return self.coordinator.enqueue_say(text, emotion, priority, source=source)

    def enqueue_fact(self, fact: str, user_text: str = "", must: bool = False, question: bool = False) -> SpeechCandidate:
        """Факт, по которому компаньон сам сформулирует реплику (`_speak_about`) -> очередь (ATT-13)."""
        return self.coordinator.enqueue_fact(fact, user_text, must, question)

    async def _speak_about(self, fact: str, user_text: str = "", must: bool = False, question: bool = False,
                           direct: bool = False):
        await self.coordinator.speak_about(fact, user_text, must, question, direct)

    # ── фокус ────────────────────────────────────────────────────────────────

    def top_focus(self) -> Focus:
        return self.focus_.top()

    def _push_focus(self, kind: str, topic: str, source: str, priority: int, resumable: bool) -> Focus:
        return self.focus_.push(kind, topic, source, priority, resumable)

    def _pop_focus(self, focus_id: str) -> None:
        self.focus_.pop(focus_id)

    def _focus_tick(self, now: float) -> None:
        self.focus_.tick(now)

    # ── прежние закрытые методы: ядро, инструменты и тесты зовут их напрямую ──

    def _make_item(self, module_id: str, ev: dict, now: float) -> Item:
        return self.normalizer.make_item(module_id, ev, now)

    def _note_sender(self, item: Item, *, skip_memory: bool) -> None:
        self.normalizer.note_sender(item, skip_memory=skip_memory)

    def _apply_classification(self, item: Item, cls: decide.EventClass | None, declared: int | None) -> None:
        self.classifier.apply(item, cls, declared)

    def _score(self, item: Item, cfg: dict, att: dict) -> dict:
        return self.scorer.score(item, cfg, att)

    def _decide(self, priority: float, att: dict, away: bool) -> str:
        return self.scorer.decide(priority, att, away)

    def _half_life(self, kind: str, att: dict) -> float:
        return self.scorer.half_life(kind, att)

    def _fact(self, item: Item) -> str:
        return event_fact(self.ctx, item)

    def _digest_add(self, item: Item) -> None:
        self.digest_.add(item)

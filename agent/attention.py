"""Менеджер внимания (ТЗ 03): что из происходящего заслуживает её слов, когда и насколько срочно.

Каждое событие модуля, каждая речь модуля, вопрос, задача и собственная проактивная реплика проходят здесь:

  1. правила до классификатора (ATT-7): R1 обращение владельца -> разговор; R2 ответ на вопрос в том же канале;
     R3 временное правило mute -> в сводку; R4 критичное (urgency_hint 5 и объявлено в манифесте) -> срочность 5;
  2. срочность 0-5 (ATT-8): подсказка модуля и категория от классификатора (`decide.py`, сервис `decider`);
  3. приоритет 0-100 = срочность * 20 * вес профиля * вес отправителя * вес правил * вес состояния пользователя * свежесть
     (ATT-9) и решение D0-D4 по порогам (ATT-10): выбросить, в сводку, сказать при случае, сказать скоро, прервать;
  4. очередь речи (`speech_queue.py`) и стек фокуса (conversation, reaction, question поверх idle): ATT-13-ATT-23;
  5. временные правила, сводка, быстрый путь «замолчи», классификация обращений пользователя (ATT-24-ATT-28);
  6. объявления о долгих запусках (AN-1-AN-3, правило 14 ТЗ): события `<модуль>.launch`, речь и `/api/notify` с пометкой
     `announce`, инструменты с `announce: true` идут мимо формулы важности отдельным видом `announce` (приоритет 75, срок 180 с,
     без `min_gap`): каждое объявление должно прозвучать.

Классификатор только подсказывает: без него (сервис не запущен, настройка выключена, ошибка) всё работает по подсказкам
модуля. Он вызывается асинхронно с коротким тайм-аутом и не блокирует цикл событий (ATT-32). Реплики формирует модель по
факту (`companion._speak_about`); код передаёт только факты.

Интерфейс для шины и запросов ядра прежний (ТЗ 02 §9.4): `submit`, `submit_speech`, `submit_question`, `on_job_update`.
"""
import asyncio
import fnmatch
import logging
import re
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import approvals
import decide
import events_log
import settings
from attention_rules import FILE_NAME as RULES_FILE
from attention_rules import RuleStore
from conversation_io import Channel, current_channel
from speech_queue import ANNOUNCE_P, INTERRUPT_P, SpeechCandidate, SpeechQueue

logger = logging.getLogger("attention")

TEXT_LOG = 200                       # сколько символов текста уходит в журнал (ATT-29)
TEXT_MAX = 1000
DEDUPE_S = 60.0                      # ATT-6
CLASSIFIER_QUEUE_MAX = 20            # ATT-32
TICK_S = 0.5
RULES_PURGE_S = 60.0
DIGEST_MAX = 50                      # ATT-26
DIGEST_SPEECH_MAX = 5                # ATT-27
DIGEST_MIN_ITEMS, DIGEST_MIN_PRIORITY, DIGEST_QUIET_S = 3, 25, 600.0
CONVERSATION_FOCUS_P, CONVERSATION_IDLE_S, RETURN_WINDOW_S, RETURN_P = 90, 60.0, 300.0, 40
FOCUS_DEPTH = 3                      # ATT-21
REACTION_MAX_AGE_S = 900.0
SPEECH_DEFAULT_URGENCY = 2           # речь модуля без urgency_hint
PROACTIVE_P = 30                     # проактивная реплика - «болтовня»: звучит, когда больше нечего сказать
CONVERSATION_P = 95                  # ATT-13
QUESTION_ANNOUNCE_P = 80             # формулировка вопроса разрешения (approvals.request), сказанная вне submit_question
ANNOUNCE_MERGE_S = 20.0              # AN-1: одинаковые объявления одного источника в этом окне склеиваются
ANNOUNCE_DEFAULT_URGENCY = 3
DEFAULT_HALF_LIFE = {"rimworld.*": 60, "telegram.*": 900, "desktop.*": 120, "*.job": 600, "*": 300}
DEFAULT_THRESHOLDS = (15, 35, 60, 85)
DECISION_NAMES = ("D0", "D1", "D2", "D3", "D4")
REPLY_CATEGORIES = ("question", "request_to_assistant")      # категории, в которых ответа ждут сами по себе

_STOP_RE = re.compile(r"^\W*(пожалуйста[\W]+)?(замолчи|замолчите|помолчи|помолчите|молчи|заткнись|тихо|"
                      r"хватит( болтать| говорить| трепаться)?|прекрати( говорить| болтать)?|стоп|stop|shut up)\W*"
                      r"(пожалуйста\W*)?$", re.I)


@dataclass
class Question:
    """Вопрос пользователю: от модуля (core.ask), из задачи модуля (waiting_user) или от самого ядра."""

    kind: str                                   # approval | choice | text
    question: str                               # факт: о чём спросить; формулировку вслух строит ядро
    options: list[str] | None = None
    timeout_s: float = 300
    source: str = "core"                        # id модуля или "core"
    context: dict = field(default_factory=dict)
    urgency_hint: int | None = None
    job_id: str | None = None                   # для вопросов задач
    question_id: str | None = None


@dataclass
class Item:
    """Событие внутри менеджера (ATT: раздел 4)."""

    id: str                                     # "a-" + 8 hex
    module: str
    kind: str                                   # "telegram.dm"
    text: str                                   # факт
    data: dict
    sender: dict | None
    reply_channel: dict | None
    addressed_to_me: bool | None
    urgency: int | None                         # 0..5 после правил и классификатора
    needs_reply: bool
    memorable: bool
    created: float
    expires: float | None
    dedupe_key: str | None
    count: int = 1                              # склеенные повторы
    urgency_hint: int | None = None
    updated: float = 0.0
    spoken: bool = False
    priority: float = 0.0
    decision: str = ""
    first_contact: bool = False                 # TG-14: профиль отправителя заведён именно этим событием (для веса «неизвестный»)
    memory_done: bool = False                   # TG-14: событие уже отдано в граф и память, в _act второй раз не отдаётся


@dataclass
class Focus:
    id: str
    kind: str                                   # "idle" | "conversation" | "reaction" | "question"
    topic: str                                  # факт: о чём
    source: str
    priority: int
    started: float
    last_active: float
    resumable: bool


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _longest_match(patterns: dict, kind: str, default=None):
    """Значение для самого длинного шаблона (fnmatch), совпавшего с видом события."""
    best, best_len = default, -1
    for pat, value in patterns.items():
        if len(pat) > best_len and fnmatch.fnmatchcase(kind, pat):
            best, best_len = value, len(pat)
    return best


class Attention:
    def __init__(self, companion, io, *, clock=time.time, decider: decide.Decider | None = None,
                 settings_source: Callable[[], dict] | None = None, rules_file=None,
                 idle_source: Callable[[], float] | None = None, autopump: bool = True):
        """companion - Companion (say, _speak_about, speech, presence, graph); io - ConversationIO. decider - клиент
        классификатора (в тестах заглушка); settings_source - откуда брать настройки; rules_file - файл правил (по
        умолчанию memory/attention_rules.json); idle_source - секунды простоя ввода; autopump=False: планировщик речи
        запускается только вручную (`await attention.speech.pump()`), без фоновых задач."""
        self.companion = companion
        self.io = io
        self._clock = clock
        self.decider = decider if decider is not None else decide.Decider()
        self._settings = settings_source or settings.load
        self._idle = idle_source or self._default_idle
        self._autopump = autopump
        self.hub = None                          # ModuleHub: соединения для доставки вопросов, ставит ядро
        self.modules = None                      # ModuleRegistry: манифесты, включённость
        self.profiles = None                     # ProfileManager: веса профиля
        self.rules = RuleStore(rules_file if rules_file is not None else settings.memory_dir() / RULES_FILE, clock=clock)
        self.presence = getattr(companion, "presence", None) or _NullPresence()
        self.speech = SpeechQueue(
            speech=getattr(companion, "speech", None) or _NullSpeech(), play=self._play, presence=self.presence.snapshot,
            chattiness=lambda: str(self._settings().get("chattiness", "normal")), question_pending=approvals.has_pending,
            repeats=lambda text: bool(getattr(companion, "_repeats_recent", lambda t: False)(text)),
            on_drop=self._speech_dropped, on_played=self._speech_played, clock=clock)
        # служебные события модулей, которые ядро обрабатывает само и не озвучивает: kind -> async (Item) -> None (ТЗ 11, RW-9:
        # rimworld.colonists обновляет отношения и граф памяти). Такое событие в журнал внимания и очередь речи не попадает.
        self.kind_handlers: dict[str, Callable[[Item], Awaitable[None]]] = {}
        # то же для событий, которым нужно ещё и побочное действие (ТЗ 12, VOICE-14: voice.heard пишется в память занятий), но
        # которые дальше идут обычным путём внимания: kind -> async (Item) -> None
        self.kind_hooks: dict[str, Callable[[Item], Awaitable[None]]] = {}
        # виды, которые не пишутся в журнал module_event: служебные и частые (voice.speech, voice.user_speaking)
        self.silent_kinds: set[str] = set()
        self._items: dict[str, Item] = {}
        self._by_key: dict[tuple[str, str], Item] = {}
        self._announces: dict[tuple[str, str], dict] = {}   # (модуль, текст) -> {item, cand, short} недавних объявлений (AN-1)
        self._digest: dict[str, deque] = {}
        self._cls_pending = 0
        self._job_states: dict[tuple[str, str], tuple] = {}
        self._tasks: set[asyncio.Task] = set()
        self._loop_task: asyncio.Task | None = None
        self._focus: list[Focus] = [Focus("f-idle", "idle", "", "", 0, clock(), clock(), False)]
        self._reaction_focus: dict[str, str] = {}      # id события -> id фокуса reaction
        self._question_channel: Channel | None = None  # куда доставлен вопрос (ответ принимается правилом R2)
        self._last_user_text = ""
        self._last_purge = clock()
        self._digest_offered = 0.0
        self.last_decision: dict | None = None         # для отладки и дашборда
        self.decisions: deque = deque(maxlen=100)
        self._publish_focus()

    # ── служебное ────────────────────────────────────────────────────────────

    @staticmethod
    def _default_idle() -> float:
        try:
            import perception
            return float(perception.idle_seconds())
        except Exception:
            return 0.0

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.warning(f"Фоновая задача внимания: {task.exception()!r}")

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
        await self.speech.pump(self._att()["interrupt_margin"])

    def _kick(self) -> None:
        if self._autopump:
            self._spawn(self.speech.pump(self._att()["interrupt_margin"]))

    # ── настройки ────────────────────────────────────────────────────────────

    def _att(self, cfg: dict | None = None) -> dict:
        """Настройки attention с проверкой значений: испорченное заменяется значением по умолчанию."""
        raw = (cfg if cfg is not None else self._settings()).get("attention")
        raw = raw if isinstance(raw, dict) else {}
        d = settings.DEFAULTS["attention"]
        out = {**d, **raw}
        th = out.get("thresholds")
        if not (isinstance(th, (list, tuple)) and len(th) == 4 and all(isinstance(x, (int, float)) for x in th)
                and list(th) == sorted(th)):
            out["thresholds"] = list(DEFAULT_THRESHOLDS)
        for key in ("interrupt_margin", "away_after_s", "min_conf_event", "min_conf_input"):
            if not isinstance(out.get(key), (int, float)) or isinstance(out.get(key), bool):
                out[key] = d[key]
        if not isinstance(out.get("half_life"), dict) or not out["half_life"]:
            out["half_life"] = dict(DEFAULT_HALF_LIFE)
        out["classifier"] = bool(out.get("classifier", True))
        return out

    # ── сборка события ───────────────────────────────────────────────────────

    def _make_item(self, module_id: str, ev: dict, now: float) -> Item:
        sender = ev.get("sender") if isinstance(ev.get("sender"), dict) else None
        reply = ev.get("reply_channel") if isinstance(ev.get("reply_channel"), dict) else None
        hint = ev.get("urgency_hint")
        hint = int(hint) if isinstance(hint, (int, float)) and not isinstance(hint, bool) else None
        ttl = ev.get("expires_in_s")
        return Item(id="a-" + secrets.token_hex(4), module=module_id, kind=str(ev.get("kind") or f"{module_id}.event"),
                    text=str(ev.get("text") or "")[:TEXT_MAX], data=ev.get("data") if isinstance(ev.get("data"), dict) else {},
                    sender=sender, reply_channel=reply,
                    addressed_to_me=ev["addressed_to_me"] if isinstance(ev.get("addressed_to_me"), bool) else None,
                    urgency=None, needs_reply=False, memorable=bool(ev.get("memorable")), created=now,
                    expires=now + float(ttl) if isinstance(ttl, (int, float)) and not isinstance(ttl, bool) and ttl > 0 else None,
                    dedupe_key=str(ev["dedupe_key"]) if ev.get("dedupe_key") else None, urgency_hint=hint, updated=now)

    def _emit_info(self, module_id: str, kind: str) -> dict | None:
        rec = self.modules.get(module_id) if self.modules is not None else None
        for e in ((rec.manifest or {}).get("emits") or []) if rec is not None else []:
            if isinstance(e, dict) and e.get("kind") == kind:
                return e
        return None

    def _module_title(self, module_id: str) -> str:
        rec = self.modules.get(module_id) if self.modules is not None else None
        return (rec.title if rec is not None and rec.title else module_id)

    @staticmethod
    def _sender_name(sender: dict | None) -> str | None:
        return (sender or {}).get("name") or (sender or {}).get("id")

    # ── события ──────────────────────────────────────────────────────────────

    async def submit(self, module_id: str, event: dict) -> None:
        """Событие модуля (тело `event`, ТЗ 01 раздел 7.1)."""
        now = self._clock()
        item = self._make_item(module_id, event, now)
        sender_name = self._sender_name(item.sender)
        if item.kind not in self.silent_kinds:
            events_log.log_nowait("module_event", item.text[:TEXT_LOG], module=module_id, event_kind=item.kind,
                           urgency_hint=item.urgency_hint, addressed_to_me=item.addressed_to_me, sender=sender_name)
        handler = self.kind_handlers.get(item.kind)
        if handler is not None:
            self._spawn(self._run_kind_handler(handler, item))
            return
        hook = self.kind_hooks.get(item.kind)
        if hook is not None:
            self._spawn(self._run_kind_handler(hook, item))               # побочное действие; событие идёт дальше обычным путём
        if self.is_announce_event(item.kind, event):
            self._register(item)                                           # AN-2: мимо формулы, но с записью решения
            await self._announce(item)
            return
        if self._merge_duplicate(item, now):
            return
        self._register(item)
        cfg = self._settings()
        att = self._att(cfg)
        to_conversation = self._is_owner_address(item)
        self._note_sender(item, skip_memory=to_conversation)

        # R1: обращение владельца к ней - это не событие внимания, а реплика разговору (CORE-24)
        if to_conversation:
            item.decision = "R1"
            self._spawn(self.io.from_module(item.text, item.reply_channel, item.sender))
            self._log_decision(item, att, rule="R1")
            return
        # R2: ответ владельца на вопрос, заданный в этом же канале
        if (approvals.has_pending() and item.sender and item.sender.get("is_owner") and item.reply_channel
                and self._question_channel is not None and item.reply_channel.get("module") == self._question_channel.module):
            if await approvals.try_answer(item.text):
                item.decision = "R2"
                self._log_decision(item, att, rule="R2")
                return
        # R3: временное правило mute - в сводку, классификатор не нужен
        muted = self.rules.muted(module_id, item.kind, sender_name)
        if muted is not None:
            item.urgency = item.urgency_hint if item.urgency_hint is not None else 1
            await self._finish(item, cfg, att, rule="R3", force="D1", rule_id=muted.id)
            return
        # R4: критичное событие, объявленное модулем как срочное, обходит классификатор
        info = self._emit_info(module_id, item.kind)
        declared = info.get("default_urgency") if info else None
        if item.urgency_hint == 5 and declared == 5:
            item.urgency = 5
            await self._finish(item, cfg, att, rule="R4")
            return

        if self._needs_classifier(item, att):
            if self._cls_pending >= CLASSIFIER_QUEUE_MAX:
                # ATT-32: очередь к классификатору длиннее 20 - новые события идут по подсказкам модуля (без подсказки urgency = 1)
                item.data = {**item.data, "classifier_skipped": "queue"}
                if item.urgency_hint is None:
                    item.urgency = 1
                self._apply_classification(item, None, declared)
                await self._finish(item, cfg, att)
                return
            # классификатор отвечает за сотни миллисекунд: ждём его в фоновой задаче, чтобы очередь событий модуля не стояла (ATT-32)
            self._cls_pending += 1
            self._spawn(self._classify_and_finish(item, cfg, att, declared))
            return
        self._apply_classification(item, None, declared)
        await self._finish(item, cfg, att)

    @staticmethod
    async def _run_kind_handler(handler, item: Item) -> None:
        try:
            await handler(item)
        except Exception as e:
            logger.warning(f"Обработчик события «{item.kind}»: {e}")

    @staticmethod
    def _is_owner_address(item: Item) -> bool:
        """R1: владелец обращается к ней (addressed_to_me или личка) и ему есть куда ответить."""
        return bool(item.sender and item.sender.get("is_owner") and item.reply_channel
                    and (item.addressed_to_me is True or item.kind.endswith(".dm")))

    def _note_sender(self, item: Item, *, skip_memory: bool) -> None:
        """TG-14: общее правило для любых модулей. Если у события есть sender.id и sender.name: человек заводится в
        relationships (ensure_person, source - модуль) и его реплика записывается (note). Если событие memorable: оно идёт в
        очередь графа и фактом-эпизодом в память о пользователе (как прежний _contact в core_service). Всё до решения, чтобы
        профиль и память не зависели от того, будет ли событие озвучено. Обращение владельца (R1) идёт разговором: его реплику
        в граф и память кладёт сам разговор, второй раз не пишем. Сбой записи событие не останавливает."""
        sender = item.sender
        if not sender or not sender.get("id") or not sender.get("name"):
            return
        ident, name = str(sender["id"]), str(sender["name"])
        try:
            rel = getattr(self.companion, "_rel", None)
            if rel is not None and hasattr(rel, "ensure_person"):
                item.first_contact = bool(rel.ensure_person(ident, name, source=item.module))
                rel.note(ident, item.text)
            if item.memorable and not skip_memory:
                mem = getattr(self.companion, "_mem", None)
                if mem is not None:
                    mem.add_fact(item.text[:300], kind="episode", importance=3)
                graph = getattr(self.companion, "graph", None)
                if graph is not None:
                    graph.enqueue(name, item.text)
                item.memory_done = True
        except Exception as e:
            logger.warning(f"Запись отправителя {ident} в relationships и память: {e}")

    async def _classify_and_finish(self, item: Item, cfg: dict, att: dict, declared: int | None) -> None:
        try:
            answers = await self.decider.classify(item.text, decide.EVENT_QUESTIONS, timeout=decide.EVENT_TIMEOUT_S)
        finally:
            self._cls_pending -= 1
        ms = self.decider.last_ms
        self._apply_classification(item, decide.parse_event(answers, att["min_conf_event"]), declared)
        await self._finish(item, cfg, att, ms=ms)

    def _needs_classifier(self, item: Item, att: dict) -> bool:
        """ATT-4a: подсказки модуля хватает, если есть и urgency_hint, и addressed_to_me, а текст не от человека."""
        if not att["classifier"] or not item.text.strip():
            return False
        return item.sender is not None or item.urgency_hint is None or item.addressed_to_me is None

    def _apply_classification(self, item: Item, cls: decide.EventClass | None, declared: int | None) -> None:
        """ATT-8: срочность = максимум из подсказки модуля и категории; spam даёт 0; без классификатора - подсказка или 1."""
        hint = item.urgency_hint if item.urgency_hint is not None else (declared if isinstance(declared, int) else None)
        if item.urgency is not None and cls is None:
            return                                        # уже назначена (очередь классификатора переполнена)
        if cls is not None and cls.category is not None:
            item.urgency = 0 if cls.category == "spam" else max(hint if hint is not None else 0, cls.urgency or 0)
            item.data = {**item.data, "category": cls.category, "category_conf": round(cls.category_conf, 3)}
        else:
            item.urgency = hint if hint is not None else 1
        if item.addressed_to_me is None and cls is not None:
            item.addressed_to_me = cls.addressed
        # «ждёт ответа»: да/нет-вопрос модели на живой проверке сильно смещён в «нет» (10 из 20 ответов нужных не нашла), поэтому
        # вопрос и просьба по категории тоже означают, что ответа ждут
        item.needs_reply = bool(cls is not None and (cls.needs_reply or cls.category in REPLY_CATEGORIES))

    # ── склейка повторов ─────────────────────────────────────────────────────

    def _register(self, item: Item) -> None:
        self._items[item.id] = item
        if item.dedupe_key:
            self._by_key[(item.module, item.dedupe_key)] = item
        if len(self._items) > 500:
            for old in sorted(self._items.values(), key=lambda i: i.created)[:100]:
                self._items.pop(old.id, None)
                if old.dedupe_key and self._by_key.get((old.module, old.dedupe_key)) is old:
                    del self._by_key[(old.module, old.dedupe_key)]

    def _merge_duplicate(self, item: Item, now: float) -> bool:
        """ATT-6: тот же dedupe_key за последние 60 с и ещё не озвучено - счётчик и новый текст, не новое событие."""
        if not item.dedupe_key:
            return False
        old = self._by_key.get((item.module, item.dedupe_key))
        if old is None or old.spoken or now - old.updated > DEDUPE_S:
            return False
        old.count += 1
        old.text, old.updated = item.text, now
        for cand in self.speech.items:
            if cand.item_id == old.id:
                cand.text = self._fact(old)
        for entry in self._digest.get(old.module, ()):
            if entry["item"] == old.id:
                entry["text"], entry["count"] = old.text, old.count
        events_log.log_nowait("attention", old.text[:TEXT_LOG], item=old.id, module=old.module, event_kind=old.kind, merged=True,
                       count=old.count)
        return True

    # ── вес и решение ────────────────────────────────────────────────────────

    def _w_profile(self, kind: str) -> float:
        weights = {}
        if self.profiles is not None:
            weights = (getattr(self.profiles.current(), "attention", None) or {}).get("weights") or {}
        w = _longest_match({k: v for k, v in weights.items() if isinstance(v, (int, float))}, kind, 1.0)
        return float(w)

    def _w_sender(self, sender: dict | None, first_contact: bool = False) -> float:
        if not sender:
            return 1.0
        if sender.get("is_owner"):
            return 1.5
        rel = getattr(self.companion, "_rel", None)
        rec = None
        if rel is not None and not first_contact:        # первое сообщение человека: профиль заведён только что, он ещё «неизвестный»
            for key in (sender.get("id"), sender.get("name")):
                if key:
                    rec = rel.get(str(key))
                    if rec is not None:
                        break
        if rec is None:
            return 0.8
        return 1.3 if float(rec.get("affinity", 0.0)) / 100.0 >= 0.6 else 1.0

    def _half_life(self, kind: str, att: dict) -> float:
        value = _longest_match({k: v for k, v in att["half_life"].items() if isinstance(v, (int, float)) and v > 0}, kind,
                               DEFAULT_HALF_LIFE["*"])
        return float(value)

    def _user_state(self, item: Item | None, urgency: int, addressed: bool | None, cfg: dict, att: dict) -> tuple[float, bool]:
        """(вес w_user, пользователь отошёл). «Не отвлекать» режет вес до 0.3, кроме срочного обращения к ней."""
        now = self._clock()
        quiet = bool(cfg.get("dnd")) or now < float(cfg.get("quiet_until") or 0.0)
        w = 1.0
        if quiet and not (urgency == 5 and addressed):
            w = 0.3
        away = self._idle() > att["away_after_s"]
        return w, away

    def _score(self, item: Item, cfg: dict, att: dict) -> dict:
        now = self._clock()
        urgency = int(item.urgency if item.urgency is not None else 1)
        w_user, away = self._user_state(item, urgency, item.addressed_to_me, cfg, att)
        parts = {"w_profile": self._w_profile(item.kind), "w_sender": self._w_sender(item.sender, item.first_contact),
                 "w_rule": self.rules.weight(item.module, item.kind, self._sender_name(item.sender)), "w_user": w_user,
                 "freshness": 0.5 ** (max(0.0, now - item.created) / self._half_life(item.kind, att))}
        priority = urgency * 20.0
        for v in parts.values():
            priority *= v
        parts["priority"] = _clamp(priority, 0.0, 100.0)
        parts["away"] = away
        return parts

    def _decide(self, priority: float, att: dict, away: bool) -> str:
        t = att["thresholds"]
        level = sum(1 for th in t if priority >= th)             # 0..4
        if level >= 2 and priority >= t[2]:
            top = self._focus[-1]
            if priority >= t[3] or (top.kind != "idle" and priority > top.priority + att["interrupt_margin"]):
                level = 4
        if away and level >= 2:
            level = 1                                             # сказать некому: в сводку
        return DECISION_NAMES[level]

    async def _finish(self, item: Item, cfg: dict, att: dict, *, rule: str | None = None, force: str | None = None,
                      ms: float | None = None, rule_id: str | None = None) -> None:
        """Приоритет, решение и действие по событию."""
        score = self._score(item, cfg, att)
        item.priority = score["priority"]
        decision = force or self._decide(item.priority, att, score["away"])
        item.decision = decision
        self._act(item, decision)
        self._log_decision(item, att, score=score, rule=rule, ms=ms, rule_id=rule_id)

    def _act(self, item: Item, decision: str) -> None:
        """Что происходит по решению (ATT-10)."""
        if decision == "D0":
            return
        if item.memorable and not item.memory_done:
            graph = getattr(self.companion, "graph", None)
            if graph is not None:
                graph.enqueue(self._sender_name(item.sender) or item.module, item.text)
        if decision == "D1":
            self._digest_add(item)
            return
        key = f"attention:{item.module}:{item.dedupe_key}" if item.dedupe_key else None
        now = self._clock()
        ttl = (item.expires - now) if item.expires is not None else 600.0
        if ttl <= 0:
            return
        self.speech.new(source=f"attention:{item.id}", mode="fact", text=self._fact(item),
                        priority=int(round(item.priority)), ttl=ttl, key=key, item_id=item.id)
        if decision == "D4":
            focus = self._push_focus("reaction", item.text[:80], f"attention:{item.id}", int(round(item.priority)), False)
            self._reaction_focus[item.id] = focus.id
        self._kick()

    def _fact(self, item: Item) -> str:
        """Факт для реплики (ATT-11): событие, повторы и, если можно ответить, через какой модуль."""
        parts = [f"Событие модуля «{self._module_title(item.module)}» ({item.kind}): {item.text}"]
        if item.count > 1:
            parts.append(f"(повторилось {item.count} раз)")
        if item.needs_reply and item.reply_channel:
            parts.append(f"На это можно ответить через модуль «{self._module_title(str(item.reply_channel.get('module')))}», "
                         "но только если пользователь попросит.")
        return " ".join(parts)

    def _log_decision(self, item: Item, att: dict, *, score: dict | None = None, rule: str | None = None,
                      ms: float | None = None, rule_id: str | None = None, **extra) -> None:
        """ATT-29: решение в журнале с множителями, приоритетом и временем классификатора."""
        score = score or {}
        record = {"item": item.id, "module": item.module, "event_kind": item.kind, "urgency": item.urgency,
                  "w_profile": score.get("w_profile"), "w_sender": score.get("w_sender"), "w_rule": score.get("w_rule"),
                  "w_user": score.get("w_user"), "freshness": score.get("freshness"),
                  "priority": round(item.priority, 1) if score else None, "decision": item.decision,
                  "classifier_ms": ms, "rule": rule, "rule_id": rule_id, "count": item.count,
                  "category": item.data.get("category") if isinstance(item.data, dict) else None, **extra}
        self.last_decision = record
        self.decisions.append({**record, "text": item.text[:TEXT_LOG], "ts": item.created})
        events_log.log_nowait("attention", item.text[:TEXT_LOG], **{k: (round(v, 3) if isinstance(v, float) else v)
                                                              for k, v in record.items()})

    # ── речь модулей ─────────────────────────────────────────────────────────

    async def submit_speech(self, module_id: str, say: dict) -> None:
        """Речь модуля (тело `say`, ТЗ 01 раздел 7.2): проходит ту же оценку важности, затем очередь речи."""
        text = str(say.get("text", "")).strip()
        if not text:
            return
        now = self._clock()
        mode = "verbatim" if say.get("mode", "fact") == "verbatim" else "fact"
        hint = say.get("urgency_hint")
        hint = int(hint) if isinstance(hint, (int, float)) and not isinstance(hint, bool) else SPEECH_DEFAULT_URGENCY
        ttl = say.get("expires_in_s")
        ttl = float(ttl) if isinstance(ttl, (int, float)) and not isinstance(ttl, bool) and ttl > 0 else None
        item = Item(id="a-" + secrets.token_hex(4), module=module_id, kind=f"{module_id}.say", text=text[:TEXT_MAX], data={},
                    sender=None, reply_channel=None, addressed_to_me=None, urgency=hint, needs_reply=False,
                    memorable=False, created=now, expires=None, dedupe_key=None, urgency_hint=hint, updated=now)
        if say.get("announce") is True:
            self._register(item)
            await self._announce(item, mode=mode, emotion=say.get("emotion"), ttl=ttl)      # AN-2
            return
        cfg = self._settings()
        att = self._att(cfg)
        if self.rules.muted(module_id, item.kind, None) is not None:
            item.priority = 0.0
            item.decision = "D1"
            self._digest_add(item)
            self._log_decision(item, att, rule="R3")
            return
        score = self._score(item, cfg, att)
        item.priority = score["priority"]
        decision = self._decide(item.priority, att, score["away"])
        item.decision = decision
        if decision == "D1":
            self._digest_add(item)
        elif decision != "D0":
            key = say.get("key")
            self.speech.new(source=f"module:{module_id}", mode=mode, text=text[:TEXT_MAX] if mode == "fact" else text,
                            emotion=say.get("emotion"), priority=int(round(item.priority)), ttl=ttl,
                            key=f"module:{module_id}:{key}" if key else None, item_id=item.id)
            if decision == "D4":
                focus = self._push_focus("reaction", text[:80], f"module:{module_id}", int(round(item.priority)), False)
                self._reaction_focus[item.id] = focus.id
            self._kick()
        self._log_decision(item, att, score=score)

    # ── объявления о запусках (AN-1, AN-2) ───────────────────────────────────

    @staticmethod
    def is_announce_event(kind: str, body: dict) -> bool:
        """Событие - объявление о запуске: вид `<модуль>.launch` или явная пометка `announce: true`."""
        return body.get("announce") is True or kind.endswith(".launch")

    async def submit_announce(self, module_id: str, text: str, *, data: dict | None = None) -> None:
        """Объявление о запуске от самого ядра (инструмент модуля с `announce: true`, AN-3): тот же путь, что у событий `.launch`."""
        text = str(text).strip()
        if not text:
            return
        now = self._clock()
        item = Item(id="a-" + secrets.token_hex(4), module=module_id, kind=f"{module_id}.launch", text=text[:TEXT_MAX],
                    data=dict(data or {}), sender=None, reply_channel=None, addressed_to_me=False,
                    urgency=ANNOUNCE_DEFAULT_URGENCY, needs_reply=False, memorable=False, created=now, expires=None,
                    dedupe_key=None, urgency_hint=ANNOUNCE_DEFAULT_URGENCY, updated=now)
        self._register(item)
        await self._announce(item)

    async def _announce(self, item: Item, *, mode: str = "fact", emotion: str | None = None, ttl: float | None = None) -> None:
        """Объявление о долгом запуске (AN-1, AN-2): формула важности, правила и пауза между репликами не применяются.
        Пользователь просил не отвлекать - объявление звучит коротко; пользователь отошёл - уходит в сводку (и в журнал).
        Одинаковые объявления одного источника в пределах 20 с склеиваются: остаётся одно, в факте счётчик."""
        now = self._clock()
        cfg = self._settings()
        att = self._att(cfg)
        key = (item.module, " ".join(item.text.split()).casefold())
        self._announces = {k: v for k, v in self._announces.items() if now - v["item"].created <= ANNOUNCE_MERGE_S}
        old = self._announces.get(key)
        if old is not None:
            self._merge_announce(old, now)
            return
        quiet = bool(cfg.get("dnd")) or now < float(cfg.get("quiet_until") or 0.0)
        away = self._idle() > att["away_after_s"]
        short = quiet and mode == "fact"                              # готовую реплику модуля сократить нельзя
        item.urgency = item.urgency if item.urgency is not None else ANNOUNCE_DEFAULT_URGENCY
        item.priority, item.decision = float(ANNOUNCE_P), "ANNOUNCE"
        entry = {"item": item, "cand": None, "short": short, "mode": mode}
        self._announces[key] = entry
        if away:
            route = "digest"                                          # сказать некому: сводка, не Telegram
            self._digest_add(item)
        else:
            route = "short" if short else "speech"
            fact = self._announce_fact(item, short) if mode == "fact" else item.text
            entry["cand"] = self.speech.new(source=f"announce:{item.id}", mode=mode, text=fact, emotion=emotion,
                                            priority=ANNOUNCE_P, ttl=ttl, must=True, item_id=item.id)
            self._kick()
        self._log_decision(item, att, score={"w_user": 0.3 if quiet else 1.0, "priority": float(ANNOUNCE_P), "away": away},
                           rule="ANNOUNCE", route=route)

    def _merge_announce(self, entry: dict, now: float) -> None:
        old: Item = entry["item"]
        old.count += 1
        old.updated = now
        cand = entry["cand"]
        if cand is not None and cand in self.speech.items and entry["mode"] == "fact":
            cand.text = self._announce_fact(old, entry["short"])        # ещё не звучало: счётчик в факте
        for rec in self._digest.get(old.module, ()):
            if rec["item"] == old.id:
                rec["count"] = old.count
        events_log.log_nowait("attention", old.text[:TEXT_LOG], item=old.id, module=old.module, event_kind=old.kind,
                              merged=True, count=old.count, decision="ANNOUNCE")

    def _announce_fact(self, item: Item, short: bool) -> str:
        """Факт для реплики: что запускается и зачем (формулирует модель); пометка «коротко» при «не отвлекать»."""
        parts = [f"Объявление о запуске (модуль «{self._module_title(item.module)}»): {item.text}",
                 "Пользователь должен об этом узнать."]
        if item.count > 1:
            parts.append(f"(одно и то же объявление пришло {item.count} раз подряд, скажи один раз)")
        if short:
            parts.append("Коротко: пользователь просил не отвлекать, одна короткая фраза.")
        return " ".join(parts)

    # ── задачи модулей ───────────────────────────────────────────────────────

    async def on_job_update(self, module_id: str, job: dict) -> None:
        """Ход задачи модуля (тело `job.update`, ТЗ 01 раздел 9, ATT-12). Вопрос задачи (waiting_user) здесь не задаётся:
        шина уже вызывает submit_question с защитой от повторов по question_id (MOD-20) и сама отправляет job.answer."""
        key, state, stage = (module_id, str(job.get("job_id"))), job.get("state"), job.get("stage")
        before = self._job_states.get(key)
        self._job_states[key] = (state, stage)
        if len(self._job_states) > 500:                     # старые задачи забываем: словарь не растёт без конца
            self._job_states.pop(next(iter(self._job_states)))
        if before == (state, stage) or state == "waiting_user":
            return
        title = job.get("title") or job.get("job_id")
        if state == "done":
            urgency, text = 3, f"Задача «{title}» (модуль {module_id}) выполнена."
        elif state == "failed":
            reason = (job.get("error") or {}).get("message") or "причина не названа"
            urgency, text = 4, f"Задача «{title}» (модуль {module_id}) не удалась: {reason}."
        elif state == "cancelled":
            urgency, text = 1, f"Задача «{title}» (модуль {module_id}) отменена."
        else:
            urgency = 1
            text = f"Задача «{title}» (модуль {module_id}): состояние {state}" + (f", стадия {stage}." if stage else ".")
        if job.get("text"):
            text += f" Подробности: {str(job['text'])[:300]}"
        now = self._clock()
        item = Item(id="a-" + secrets.token_hex(4), module=module_id, kind=f"{module_id}.job", text=text[:TEXT_MAX],
                    data={"job_id": job.get("job_id"), "state": state, "stage": stage}, sender=None, reply_channel=None,
                    addressed_to_me=False, urgency=urgency, needs_reply=False, memorable=False, created=now, expires=None,
                    dedupe_key=f"job:{module_id}:{job.get('job_id')}:{state}:{stage}", urgency_hint=urgency, updated=now)
        if self._merge_duplicate(item, now):
            return
        self._register(item)
        cfg = self._settings()
        att = self._att(cfg)
        muted = self.rules.muted(module_id, item.kind, None)
        await self._finish(item, cfg, att, **({"rule": "R3", "force": "D1", "rule_id": muted.id} if muted else {}))

    # ── сводка ───────────────────────────────────────────────────────────────

    def _digest_add(self, item: Item) -> None:
        buf = self._digest.setdefault(item.module, deque(maxlen=DIGEST_MAX))
        buf.append({"item": item.id, "kind": item.kind, "text": item.text, "priority": round(item.priority, 1),
                    "count": item.count, "ts": self._clock(), "offered": False})

    def digest_counts(self) -> dict[str, int]:
        return {m: len(buf) for m, buf in self._digest.items() if buf}

    def take_digest(self, module: str | None = None) -> str:
        """Что накопилось в сводке (ATT-26): текст-факт; после выдачи буфер очищается."""
        modules = [module] if module else list(self._digest)
        lines: list[tuple[float, str]] = []
        for m in modules:
            buf = self._digest.pop(m, None)
            for e in buf or ():
                times = f" (×{e['count']})" if e["count"] > 1 else ""
                lines.append((e["priority"], f"- {self._module_title(m)}: {e['text']}{times}"))
        if not lines:
            return "В сводке пусто: ничего заметного не накопилось."
        lines.sort(key=lambda x: -x[0])
        return f"Накопилось в сводке ({len(lines)}):\n" + "\n".join(t for _, t in lines)

    def _maybe_offer_digest(self) -> None:
        """ATT-27: фокус вернулся в idle после разговора или реакции; накопилось >= 3 записей с priority >= 25; за 10 минут
        не было самостоятельных реплик - в очередь речи факт «накопилось» (не больше 5 самых важных)."""
        now = self._clock()
        if now - self.speech.last_initiative < DIGEST_QUIET_S or now - self._digest_offered < DIGEST_QUIET_S:
            return
        fresh = [(e, m) for m, buf in self._digest.items() for e in buf if not e["offered"] and e["priority"] >= DIGEST_MIN_PRIORITY]
        if len(fresh) < DIGEST_MIN_ITEMS:
            return
        fresh.sort(key=lambda x: -x[0]["priority"])
        top = fresh[:DIGEST_SPEECH_MAX]
        for e, _ in top:
            e["offered"] = True
        facts = "; ".join(f"{self._module_title(m)}: {e['text']}" for e, m in top)
        self._digest_offered = now
        self.speech.new(source="attention:digest", mode="fact", text=f"Накопилось: {facts}", priority=40, ttl=600.0,
                        key="attention:digest")
        self._kick()

    # ── правила (инструменты ядра) ───────────────────────────────────────────

    async def set_rule(self, *, module: str | None = None, kind: str | None = None, sender: str | None = None,
                       effect: str, minutes: float = 60) -> str:
        from environments.base import ToolError
        if not (module or kind or sender):
            raise ToolError("укажи хотя бы одно из: модуль, вид события, отправитель", "bad_request")
        if effect not in ("mute", "boost", "only", "normal"):
            raise ToolError("эффект: mute, boost, only или normal", "bad_request")
        minutes = _clamp(float(minutes), 1, 1440)
        rule, removed = self.rules.add(module=module, kind=kind, sender=sender, effect=effect, minutes=minutes,
                                       created_from=self._last_user_text)
        await asyncio.to_thread(self.rules.save)
        events_log.log_nowait("attention", f"правило {effect}: {module or ''} {kind or ''} {sender or ''}".strip(),
                       rule=rule.id if rule else None, effect=effect, removed=removed, minutes=minutes)
        if rule is None:
            return f"Прежние правила для этого совпадения сняты: {removed}."
        return f"Правило создано: {rule.describe(self._clock())}."

    def rules_text(self) -> str:
        now = self._clock()
        rules = self.rules.active()
        if not rules:
            return "Временных правил внимания сейчас нет."
        return "Действующие правила внимания:\n" + "\n".join("- " + r.describe(now) for r in rules)

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
        """Короткая просьба замолчать: быстрый путь без модели (ATT-18)."""
        return len(text) <= 40 and bool(_STOP_RE.match(text.strip()))

    async def stop_speech(self, why: str = "stop") -> int:
        """«Замолчи» (ATT-18): речь обрывается немедленно, очередь очищается от кандидатов ниже 85."""
        n = await self.speech.stop_all(INTERRUPT_P)
        events_log.log_nowait("attention", "речь остановлена по просьбе пользователя", stop=True, why=why, dropped=n)
        return n

    async def on_user_input(self, text: str) -> None:
        """Обращение пользователя пришло (ATT-20): фокус conversation; «замолчи» срабатывает до любых вызовов модели."""
        self._last_user_text = text[:200]
        self._touch_conversation(text)
        if self.looks_like_stop(text):
            await self.stop_speech("phrase")

    async def classify_input(self, text: str) -> decide.InputClass | None:
        """ATT-28: намерение обращения до маршрутизатора. None - классификатора нет или он не уверен (всё как раньше)."""
        att = self._att()
        if not att["classifier"] or not text.strip():
            return None
        answers = await self.decider.classify(text, decide.INPUT_QUESTIONS, timeout=decide.INPUT_TIMEOUT_S)
        cls = decide.parse_input(answers, att["min_conf_input"])
        events_log.log_nowait("attention", text[:TEXT_LOG], input=True, intent=cls.intent if cls else None,
                       needs_memory=cls.needs_memory if cls else None, classifier_ms=self.decider.last_ms,
                       available=answers is not None)
        if cls is not None and cls.intent == "stop":
            await self.stop_speech("classifier")
        return cls

    def set_user_speaking(self, speaking: bool) -> None:
        """Модуль voice (событие `voice.user_speaking`, VOICE-8) сообщает, говорит ли пользователь сейчас: нужно условию 2
        очереди речи. Когда человек закончил говорить, обновляется и `last_user_ts`."""
        self.presence.update(user_speaking=bool(speaking))
        if not speaking:
            self.presence.update(last_user_ts=self._clock())

    # ── речь: вход для компаньона ────────────────────────────────────────────

    def enqueue_say(self, text: str, emotion: str | None = None, priority: int | None = None, *,
                    source: str | None = None) -> SpeechCandidate:
        """Готовая реплика компаньона -> очередь (ATT-13). Источник по контексту: в формулировке вопроса - question,
        в обработке обращения - conversation (приоритет 95), иначе проактивная (приоритет «болтовни»)."""
        if approvals.announcing.get():
            cand = self.speech.new(source="question", mode="verbatim", text=text, emotion=emotion,
                                   priority=priority or QUESTION_ANNOUNCE_P)
        elif current_channel.get() is not None:
            self._touch_conversation()
            cand = self.speech.new(source="conversation", mode="verbatim", text=text, emotion=emotion,
                                   priority=CONVERSATION_P, channel=current_channel.get())
        else:
            cand = self.speech.new(source=source or "proactive", mode="verbatim", text=text, emotion=emotion,
                                   priority=priority if priority is not None else PROACTIVE_P)
        self._kick()
        return cand

    def enqueue_fact(self, fact: str, user_text: str = "", must: bool = False, question: bool = False) -> SpeechCandidate:
        """Факт, по которому компаньон сам сформулирует реплику (`_speak_about`) -> очередь (ATT-13)."""
        if question or approvals.announcing.get():
            cand = self.speech.new(source="question", mode="fact", text=fact, priority=QUESTION_ANNOUNCE_P, must=must,
                                   question=True)
        elif current_channel.get() is not None:
            self._touch_conversation()
            cand = self.speech.new(source="conversation", mode="fact", text=fact, priority=CONVERSATION_P,
                                   user_text=user_text, must=must, question=question, channel=current_channel.get())
        else:
            cand = self.speech.new(source="proactive", mode="fact", text=fact, priority=PROACTIVE_P, user_text=user_text,
                                   must=must)
        self._kick()
        return cand

    async def _play(self, cand: SpeechCandidate) -> str | None:
        """Сказать кандидата: готовая реплика - `companion.say`, факт - `companion._speak_about` (формулирует модель)."""
        before = getattr(self.companion, "last_utterance_id", None)
        token = current_channel.set(cand.channel)
        try:
            if cand.mode == "verbatim":
                await self.companion.say(cand.text, cand.emotion, cand.priority, direct=True)
            else:
                await self.companion._speak_about(cand.text, cand.user_text, cand.must, cand.question, direct=True)
        finally:
            current_channel.reset(token)
        uid = getattr(self.companion, "last_utterance_id", None)
        return uid if uid != before else None

    def _speech_played(self, cand: SpeechCandidate) -> None:
        if cand.item_id:
            item = self._items.get(cand.item_id)
            if item is not None:
                item.spoken = True
            self._pop_reaction(cand.item_id)
        if cand.family == "conversation":
            self._touch_conversation()

    def _speech_dropped(self, cand: SpeechCandidate, reason: str) -> None:
        events_log.log_nowait("attention", cand.text[:TEXT_LOG], speech_dropped=reason, source=cand.source, priority=cand.priority)
        if cand.item_id:
            item = self._items.get(cand.item_id)
            if item is not None and ((reason in ("expired", "overflow") and cand.family in ("attention", "announce"))
                                     or (reason == "stopped" and cand.family == "announce")):
                self._digest_add(item)                        # ATT-26: устаревшее нерассказанное попадает в сводку
            self._pop_reaction(cand.item_id)

    # ── вопросы ──────────────────────────────────────────────────────────────

    def _chat_module(self) -> str | None:
        """Подключённый включённый модуль-канал (chat.channel), куда можно отправить вопрос владельцу."""
        if self.hub is None:
            return None
        found = [c.module_id for c in self.hub.connections()
                 if "chat.channel" in (c.manifest or {}).get("capabilities", [])
                 and (self.modules is None or self.modules.is_enabled(c.module_id))]
        return "telegram" if "telegram" in found else (found[0] if found else None)

    async def submit_question(self, q: Question) -> Any:
        """Задать вопрос пользователю (ATT-19). Возвращает (answer, by), как approvals.ask. Вопрос ставится в очередь речи
        (приоритет urgency_hint*20 в пределах 40-90, по умолчанию 60); заданным он считается, когда прозвучал; если пользователь
        отошёл и есть модуль-канал, вопрос уходит владельцу туда."""
        att = self._att()
        hint = q.urgency_hint if isinstance(q.urgency_hint, (int, float)) and not isinstance(q.urgency_hint, bool) else 3
        priority = int(_clamp(hint * 20, 40, 90))
        away_module = self._chat_module() if self._idle() > att["away_after_s"] else None
        focus = self._push_focus("question", q.question[:80], f"question:{q.source}", priority, True)

        async def announce(fact: str) -> None:
            if away_module is not None:
                await self._announce_in_module(fact, away_module)
                return
            cand = self.speech.new(source="question", mode="fact", text=fact, priority=priority, must=True, question=True)
            self._kick()
            try:
                await cand.done
            except asyncio.CancelledError:
                self.speech.discard(cand, "cancelled")        # ответ пришёл раньше, чем вопрос озвучили
                raise

        try:
            return await approvals.ask(q.kind, q.question, q.options, q.timeout_s, q.source, announce=announce,
                                       context=q.context)
        finally:
            self._question_channel = None
            self._pop_focus(focus.id)

    async def _announce_in_module(self, fact: str, module: str) -> None:
        """Вопрос владельцу в модуль-канал: формулировку строит модель, доставка - обычным путём ответа в канал модуля."""
        channel = Channel("module", module, "owner")
        self._question_channel = channel
        token = current_channel.set(channel)
        try:
            await self.companion._speak_about(fact, must=True, question=True, direct=True)
        finally:
            current_channel.reset(token)

    # ── фокус ────────────────────────────────────────────────────────────────

    def top_focus(self) -> Focus:
        return self._focus[-1]

    def _publish_focus(self) -> None:
        top = self._focus[-1]
        self.presence.update(focus=top.kind if top.kind == "idle" else f"{top.kind}: {top.topic}", busy=top.kind != "idle")

    def _push_focus(self, kind: str, topic: str, source: str, priority: int, resumable: bool) -> Focus:
        now = self._clock()
        f = Focus("f-" + secrets.token_hex(3), kind, topic, source, priority, now, now, resumable)
        self._focus.append(f)
        while len(self._focus) - 1 > FOCUS_DEPTH:                   # ATT-21: без учёта дна стека
            victims = [x for x in self._focus[1:-1] if x.resumable] or self._focus[1:-1]
            if not victims:
                break
            self._focus.remove(victims[0])
        self._publish_focus()
        return f

    def _touch_conversation(self, topic: str | None = None) -> None:
        now = self._clock()
        conv = next((f for f in self._focus if f.kind == "conversation"), None)
        if conv is None:
            self._push_focus("conversation", (topic or "")[:80], "user", CONVERSATION_FOCUS_P, True)
            return
        conv.last_active = now
        if topic:
            conv.topic = topic[:80]
            self._publish_focus()

    def _pop_reaction(self, item_id: str) -> None:
        fid = self._reaction_focus.pop(item_id, None)
        if fid:
            self._pop_focus(fid)

    def _pop_focus(self, focus_id: str) -> None:
        idx = next((i for i, f in enumerate(self._focus) if f.id == focus_id and i > 0), None)
        if idx is None:
            return
        was_top = idx == len(self._focus) - 1
        gone = self._focus.pop(idx)
        top = self._focus[-1]
        if was_top and gone.kind in ("reaction", "question") and top.kind == "conversation" \
                and self._clock() - top.last_active < RETURN_WINDOW_S:
            # ATT-22: разговор прервался; возвращаться ли к теме, решает модель (может промолчать)
            self.speech.new(source="attention:return", mode="fact", priority=RETURN_P, ttl=120.0, key="attention:return",
                            text=f"Разговор о «{top.topic}» прервался из-за «{gone.topic}». Можно вернуться к теме или промолчать.")
            self._kick()
        self._publish_focus()
        if top.kind == "idle" and gone.kind in ("conversation", "reaction"):
            self._maybe_offer_digest()

    def _focus_tick(self, now: float) -> None:
        """Сроки (ATT-20): conversation снимается через 60 с без обращений и ответов, зависшая reaction - через 15 минут."""
        for f in list(self._focus[1:]):
            if (f.kind == "conversation" and now - f.last_active > CONVERSATION_IDLE_S) or \
                    (f.kind == "reaction" and now - f.started > REACTION_MAX_AGE_S):
                for item_id, fid in list(self._reaction_focus.items()):
                    if fid == f.id:
                        del self._reaction_focus[item_id]
                self._pop_focus(f.id)


class _NullSpeech:
    """Вывод речи, которого нет (тесты без компаньона)."""

    async def speak(self, utterance_id, text, emotion, priority) -> None: ...
    async def stop(self, utterance_id=None) -> None: ...
    def is_speaking(self) -> bool:
        return False
    def current_segment(self) -> str:
        return ""


class _NullPresence:
    def __init__(self):
        self.state = {"speaking": False, "user_speaking": False, "last_user_ts": 0.0, "focus": "idle", "busy": False}

    def snapshot(self) -> dict:
        return dict(self.state)

    def update(self, **fields) -> bool:
        self.state.update(fields)
        return True

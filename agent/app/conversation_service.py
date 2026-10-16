"""ConversationService: путь одного обращения владельца (ТЗ 29, ARC-5): вход, проверки (поправка, ответ на вопрос о
разрешении, приказ в игру), выбор ветки (болтовня / просьба / «что умеешь»), журнал маршрута.
Ответ формулирует ReplyGenerator, память пишет MemoryCapture, остальное зовётся через фасад `Companion`."""
import asyncio
import contextvars
import re
import time
import logging

import fast_brain
import journal
import routes
import latency_trace
import salience
from conversation_io import DASHBOARD, VOICE, UserInput, current_channel

from app.instant import current_turn
from app.memory_capture import SPEAKER_TAG

logger = logging.getLogger("companion")

# отправитель текущего обращения (узнанный голос: новый человек даёт яркость, ТЗ 24 TRC-4)
_sender_ctx: contextvars.ContextVar = contextvars.ContextVar("companion_sender", default=None)

WRITE_WAIT_S = 0.05      # сколько ответ ждёт запись реплики в диалог (ТЗ 25, FAST-7); дальше запись доходит в фоне
_EXPLICIT_MEMORY = re.compile(r"\b(запомни\w*|не забудь\w*|запиши)\b", re.IGNORECASE)


class ConversationService:
    def __init__(self, host):
        self._h = host

    async def on_voice(self, text: str, speaker: dict, trace=None):
        """Реплика, сказанная голосом: узнанный владелец идёт как обычное сообщение, остальным ставится пометка,
        кто говорит (имя или «незнакомый голос»), и заводится профиль человека."""
        h = self._h
        pid, name = speaker.get("id"), speaker.get("name")
        if speaker.get("owner") or not speaker.get("has_owner"):
            prefix = ""
        elif name:
            prefix = f"[говорит {name}] "
        else:
            prefix = "[незнакомый голос] "
        if pid and not speaker.get("owner") and speaker.get("has_owner"):
            h._people.ensure_person(f"voice:{pid}", name or f"голос {str(pid)[-4:]}", source="voice")
            h._people.note(f"voice:{pid}", text)
        await h.on_input(UserInput(prefix + text, VOICE, sender=speaker, ts=time.time(), trace=trace))

    async def on_user_message(self, text: str, channel=None):
        """Обращение текстом (чат дашборда, строка питомца); channel по умолчанию - дашборд."""
        await self._h.on_input(UserInput(text, channel or DASHBOARD))

    async def on_input(self, ui: UserInput):
        """Единый вход обращений (CORE-24): ответ на него уходит в канал обращения (current_channel)."""
        h = self._h
        token = current_channel.set(ui.channel)
        t = ui.trace if ui.trace is not None else latency_trace.start(ui.channel.kind)   # трасса задержки (ТЗ 24, TRC-1)
        trace_token = latency_trace.bind(t)
        if h.attention is None:
            h.presence.update(busy=True)         # с менеджером внимания занятость определяет стек фокуса (ATT-23)
        try:
            sender_token = _sender_ctx.set(ui.sender)
            try:
                await h._handle_text(ui.text)
            finally:
                _sender_ctx.reset(sender_token)
        finally:
            latency_trace.unbind(trace_token)
            latency_trace.finish_core(t)
            current_channel.reset(token)
            if h.attention is None:
                h.presence.update(busy=False)

    async def handle_text(self, text: str):
        """Единый вход обращения (ТЗ 25, FAST-1): жёсткие правила до моделей -> параллельно классификатор, приказ в игру, поиск
        в памяти и запись реплики -> маршруты (`routes.py`) -> ответ потоком, поручение или то и другое."""
        h = self._h
        text = text.strip()
        if not text:
            return
        hard = routes.hard_rules().match(text, pending_question=h._approver.has_pending())
        if hard is not None and hard.kind in ("stop", "emergency"):
            await self._reflex(text, hard)
        # запись реплики (диалог, очередь графа) уходит в поток и идёт параллельно разбору; ответ её дожидается (FAST-7)
        sal, reasons = salience.score(salience.factors_of(memorable=bool(_EXPLICIT_MEMORY.search(text)),
                                                          novelty=bool((_sender_ctx.get() or {}).get("new"))))
        writes = asyncio.ensure_future(asyncio.to_thread(self._write_user, text))
        try:
            await self._handle(text, hard, writes, sal, reasons)
        finally:
            await asyncio.gather(writes, return_exceptions=True)

    def _write_user(self, text: str) -> None:
        h = self._h
        h._memory_writer.add_dialogue("user", text)
        h._queue_graph(text)

    async def _reflex(self, text: str, hard) -> None:
        """Рефлекс жёсткого правила (FAST-2, FAST-5): оборвать генерацию ответа и речь сразу, без вызовов моделей."""
        h = self._h
        h.cancel_reply(hard.kind)
        if h.attention is not None:
            if not h.attention.looks_like_stop(text):          # «замолчи» остановит on_user_input; остальное - здесь
                await h.attention.stop_speech(f"hard_rule:{hard.kind}")
        else:
            await h.speech.stop(None)

    async def _handle(self, text: str, hard, writes, sal, reasons) -> None:
        h = self._h
        h._log.log_nowait("user", text, salience=sal, reasons=reasons)   # без ожидания коммита (ТЗ 24: трасса)
        h._pending_memory += 1
        h._last_user_ts = time.time()
        h.presence.update(last_user_ts=h._last_user_ts)
        fast_brain.publish("chat", {"emotion": "thinking", "action": "none"})   # сразу «задумалась», без вызова модели

        if h.attention is not None:
            await h.attention.on_user_input(text)          # фокус «разговор»; «замолчи» обрывает речь до любых вызовов модели
        h._check_correction(text)                           # поправка пользователя (ТЗ 24, TRC-5)
        if h._approver.has_pending() and await h._answer_approval(text):
            await writes
            self.log_decision(text, routes.of_input(intent=None, looks_action=False, hard=hard or routes.HardMatch(
                "approval", "pending_question", "владелец ответил на открытый вопрос о разрешении")), None)
            return
        # классификатор, приказ в игру и поиск в памяти идут вместе (FAST-6); поиск ограничен дедлайном при ответе
        likely_task = h.router is not None and h.router.looks_like_action(text)
        retrieval = None if (likely_task or hard is not None) else h.context_builder.start_retrieval(text)   # поручению память не нужна
        cls, taken = await asyncio.gather(self._classify(text), self._game_order(text))
        latency_trace.mark("classify_done")
        if taken:
            self._cancel(retrieval)
            await writes
            self.log_decision(text, routes.of_input(intent=None, looks_action=False, order_taken=True, hard=hard), cls)
            asyncio.create_task(h._speak_about(
                "Игрок только что дал тебе срочное указание по игре, ты прямо сейчас берёшься выполнять его "
                "(действия в игре идут отдельно). Скажи одной короткой фразой в своём характере, что принимаешь.",
                text, must=True))
            asyncio.create_task(fast_brain.react_chat(text, h._people))
            return
        intent = cls.intent if cls is not None else None
        # needs_memory классификатора пока смещён в «нет» (замеры ТЗ 03): по нему память не отключаем, только пишем в журнал.
        # Поиск в графе пропускается лишь там, где память заведомо не нужна; вернуть needs_memory после дообучения.
        use_memory = intent not in ("stop", "approval") and not (hard is not None and hard.kind == "stop")
        decision = routes.of_input(intent=intent, looks_action=h.router is not None and h.router.looks_like_action(text),
                                   hard=hard, has_router=h.router is not None)
        route, routed = "chat", False          # routed: вызывался ли планировщик (ТЗ 24, TRC-6)
        done = decision.already_done
        await asyncio.wait({writes}, timeout=WRITE_WAIT_S)   # обычно запись уже готова и контекст видит реплику; медленный диск ответ не держит
        turn = h.instant.begin(text, task=decision.has(routes.TASK) and intent != "about_self")
        try:
            async with h._busy:
                if intent == "about_self":
                    route = "about_self"
                    latency_trace.mark("route")
                    await h._converse(text, about_self=True, use_memory=use_memory, retrieval=retrieval, already_done=done)
                elif decision.has(routes.TASK):
                    route, routed = "task", True
                    self._cancel(retrieval)
                    latency_trace.mark("route")
                    await h._act(text)          # просьба: планировщик, ответ после плана
                else:
                    latency_trace.mark("route")
                    flagged = await h._converse(text, use_memory=use_memory, retrieval=retrieval, already_done=done)   # болтовня
                    if flagged and h.router is not None:
                        route, routed = "task", True
                        decision.add(routes.TASK)
                        await h._act(text, after_chat=True)   # модель заподозрила просьбу, которую не распознали глаголы
        finally:
            turn.end()
        self.log_route(text, route, routed, intent, cls, decision)
        h.arm_idle_consolidation()
        asyncio.create_task(fast_brain.react_chat(text, h._people))   # эмоция и отношение: уже после ответа
        if h._pending_memory >= 6:
            asyncio.create_task(h._consolidate())                  # осмыслить беседу и запомнить важное

    @staticmethod
    def _cancel(retrieval) -> None:
        if retrieval is not None:
            retrieval.cancel()

    async def _classify(self, text: str):
        """Намерение обращения (ATT-28); None - классификатора нет, всё как раньше."""
        h = self._h
        return await h.attention.classify_input(text) if h.attention is not None else None

    async def _game_order(self, text: str) -> bool:
        h = self._h
        if h.game_order is None:
            return False
        try:
            return bool(await h.game_order(text))
        except Exception as e:
            h._log.log("error", f"приказ в игру не принят: {e}")
            return False

    def check_correction(self, text: str) -> None:
        """Поправка «нет, не так» (ТЗ 24, TRC-5): по шаблону сразу; неясное начало проверяет модель в фоне."""
        h = self._h
        said = SPEAKER_TAG.sub("", text)
        level = journal.correction_level(said)
        if level == "no":
            return
        ref, prev = h._last_say, h._last_say_text[:200]
        if level == "yes":
            journal.mistake("correction", said[:200], refs=[ref], previous=prev)
        else:
            task = asyncio.create_task(h._confirm_correction(said, ref, prev))
            h._bg_tasks.add(task)                 # сильная ссылка: иначе задачу может собрать сборщик мусора
            task.add_done_callback(h._bg_tasks.discard)

    async def confirm_correction(self, text: str, ref: float, prev: str) -> None:
        system = ("Пользователь ответил на твою прошлую реплику. Определи, поправляет ли он тебя: говорит ли, что ты "
                  'ошиблась или сказала неверно. Ответь ТОЛЬКО JSON: {"correction": <true или false>}')
        try:
            data = fast_brain._json_of(await fast_brain._ask(system, f"Твоя реплика: {prev}\nОтвет: {text[:300]}",
                                                             timeout=20, max_tokens=20, temperature=0.0, background=True))
        except Exception as e:
            logger.info(f"Проверка поправки не выполнена: {e}")
            return
        if data and data.get("correction") is True:
            journal.mistake("correction", text[:200], refs=[ref], previous=prev, checked="llm")

    def log_decision(self, text: str, decision, cls) -> None:
        """Решение без разговора (ответ на вопрос о разрешении, приказ в игру): маршруты и правило в журнал решений."""
        journal.decision(decision.routes[-1] if decision.routes else routes.IGNORE, "conversation", text,
                         confidence=cls.intent_conf if cls is not None else None, echelon=decision.echelon, routes=decision.routes,
                         already_done=decision.already_done, hard_rule=decision.rule)

    def log_route(self, text: str, route: str, routed: bool, intent, cls, decision=None) -> None:
        """Решение маршрута разговора (ТЗ 24, TRC-6; ТЗ 25, FAST-1: список маршрутов) и несовпадение классификатора с путём
        (TRC-5: misroute)."""
        h = self._h
        conf = cls.intent_conf if cls is not None else None
        extra = ({"routes": decision.routes, "already_done": decision.already_done, "hard_rule": decision.rule}
                 if decision is not None else {})
        journal.decision(route, "conversation", text, confidence=conf, echelon="gliner" if cls is not None else "rules",
                         intent=intent, **extra)
        if intent == "chat" and routed and h.router is not None and h.router.last_kind == "done":
            journal.mistake("misroute", text[:200], refs=[h._last_say], source="classifier", intent=intent)
        elif intent == "action" and not routed and h.router is not None:
            journal.mistake("misroute", text[:200], refs=[h._last_say], source="classifier", intent=intent)

    async def act(self, text: str, after_chat: bool = False):
        """Просьба действовать: маршрутизатор решает «могу или нет», выполняет, а итог говорится вслух."""
        h = self._h
        turn = current_turn.get()
        # «принято» мгновенного уровня уже сказано: объявление плана (say_first) было бы вторым подтверждением (ТЗ 25, FAST-3)
        announce = (lambda: not after_chat and not (turn is not None and turn.acked))
        outcome = await h.router.handle(text, announce=announce)
        if after_chat and h.router.last_kind == "cannot":
            return   # на ответ-болтовню уже сказано всё, что можно; не повторяем «не могу» вторым сообщением
        if outcome:
            # итог действия или честное «не могу»: пользователь должен узнать результат
            await h._speak_about(outcome, text, must=True)
        elif outcome is None and not after_chat:
            await h._converse(text)    # планировщик решил, что это всё-таки разговор

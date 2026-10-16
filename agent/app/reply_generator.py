"""ReplyGenerator: ответ локальной модели в разговоре и формулировка реплики по факту (ТЗ 29, ARC-5).
Страховки стиля (`declutter`, потоковая `StreamDeclutter`) здесь же.

Стриминг (ТЗ 25, FAST-4, FAST-5): если вывод речи умеет голос и модель отдаёт поток (`ask_stream`), ответ идёт так: токены ->
`SentenceSplitter` -> `StreamDeclutter` (держит последнее предложение до прихода следующего) -> `Companion.say_part` (прямо в
вывод речи мимо условий ожидания очереди; следующие предложения встают в очередь модуля голоса, а не заменяют звучащее) ->
TTS. Маркер [ДЕЙСТВИЕ] разбирается до первого предложения. `cancel()` («замолчи», перебивание, экстренное слово) обрывает
запрос к модели, остаток предложений и ещё не начатое предложение в синтезе. Нет потока или канала голоса: прежний путь целиком.
"""
import asyncio
import logging
import re

import fast_brain
import latency_trace
import prompts
import reply_trace
import speech_out
from conversation_io import current_channel
from speech_text import SentenceSplitter, clean_speech

logger = logging.getLogger("companion")


ACTION_MARKER = "[ДЕЙСТВИЕ]"
BACKPRESSURE_LIVE = 3          # сколько предложений может быть у модуля голоса в работе, прежде чем ждём (его очередь: звучащее + три)


class StreamSession:
    """Один потоковый ответ: отмена (событие), что уже ушло в речь."""

    def __init__(self):
        self.cancelled = False
        self.reason = ""
        self.event = asyncio.Event()
        self.sentences: list[str] = []          # ушедшие в речь предложения (то, что она успела сказать)
        self.utterances: list[str] = []

    def cancel(self, reason: str) -> None:
        if not self.cancelled:
            self.cancelled, self.reason = True, reason
            self.event.set()


class StreamDeclutter:
    """`ReplyGenerator.declutter` для потока: первое предложение уходит сразу (кроме похожего на обещание действия: его
    судьба ясна, когда придёт второе), каждое следующее держится до прихода очередного (задержка на одно предложение),
    последнее проверяется на хвост-предложение помощи и третий вопрос подряд при `finish()`. Результат тот же, что у
    `declutter` по целой реплике."""

    def __init__(self, gen: "ReplyGenerator"):
        self._g = gen
        self._first_held: str | None = None     # первое предложение, похожее на обещание: ждём второго
        self._held: str | None = None
        self._released = 0
        self._count = 0

    def push(self, sentence: str) -> list[str]:
        g, out = self._g, []
        self._count += 1
        if self._count == 1:
            if g.promise_like(sentence):
                self._first_held = sentence
                return []
            self._released = 1
            return [sentence]
        if self._first_held is not None:
            self._first_held = None            # второе предложение пришло: обещание без вызова инструмента отбрасывается
        elif self._held is not None:
            out.append(self._held)
            self._released += 1
        self._held = sentence
        return out

    def finish(self) -> list[str]:
        out = []
        if self._first_held is not None:        # предложение всего одно: как и раньше, ничего не отбрасывается
            out.append(self._first_held)
        if self._held is not None:
            if self._released >= 1 and self._g.tail_unwanted(self._held):
                pass
            else:
                out.append(self._held)
        self._first_held = self._held = None
        return out


class ReplyGenerator:
    def __init__(self, host):
        self._h = host
        self._session: StreamSession | None = None

    _OFFER = re.compile(r"^(ты )?(хочешь|не хочешь|хотела бы|хотел бы)(,? чтобы я)?|^если (хочешь|нужно)|^могу ли я|"
                        r"^нужна ли тебе|^чем (ещё )?(могу )?помочь|^давай я", re.I)

    _PROMISE = re.compile(r"^(сейчас |щас |хорошо, )?(опубликую|отправлю|запущу|включу|выключу|запощу|напишу в)", re.I)

    def promise_like(self, sentence: str) -> bool:
        """Похоже на обещание действия, а инструмент не вызывался (router.last_kind != "done")."""
        h = self._h
        return (h.router is None or h.router.last_kind != "done") and bool(self._PROMISE.match(sentence.strip(" «\"")))

    def tail_unwanted(self, sentence: str) -> bool:
        """Хвост-предложение помощи («Хочешь, чтобы я…?») или третий вопрос подряд."""
        recent = [m["text"].rstrip().endswith("?") for m in self._h._memory_reader.recent_dialogue(10)
                  if m["role"] != "user"][-2:]
        return bool(self._OFFER.match(sentence.strip(" «\""))
                    or (sentence.rstrip().endswith("?") and len(recent) == 2 and all(recent)))

    def declutter(self, reply: str) -> str:
        """Страховка от навязчивости: убирает хвост-предложение помощи («Хочешь, чтобы я…?») и
        третий вопрос подряд в конце реплики. Применяется, только если останется хотя бы одно предложение."""
        h = self._h
        sents = [x for x in re.split(r"(?<=[.!?…])\s+", reply.strip()) if x]
        if len(sents) < 2:
            return reply
        # обещание действия без вызова инструмента (router.last_kind == "done" значит, что он был)
        if (h.router is None or h.router.last_kind != "done") and self._PROMISE.match(sents[0].strip(" «\"")):
            sents = sents[1:]
            reply = " ".join(sents)
            if len(sents) < 2:
                return reply
        last = sents[-1].strip(" «\"")
        recent = [m["text"].rstrip().endswith("?") for m in h._memory_reader.recent_dialogue(10) if m["role"] != "user"][-2:]
        if self._OFFER.match(last) or (sents[-1].rstrip().endswith("?") and len(recent) == 2 and all(recent)):
            return " ".join(sents[:-1])
        return reply

    # ── настройки и отмена ─────────────────────────────────────────────────────

    def _cfg(self) -> dict:
        try:
            cfg = self._h._settings.load().get("fast_contour")
        except Exception:
            cfg = None
        return cfg if isinstance(cfg, dict) else {}

    def _reply_tokens(self) -> int:
        """Потолок длины ответа в токенах (`fast_contour.reply_tokens`, 40-300): локальная модель выдаёт ~12 токенов в секунду,
        а синтез речи тоже растёт с длиной, поэтому длинная реплика стоит секунд до звука."""
        value = self._cfg().get("reply_tokens", 90)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return 90
        return int(min(300, max(40, value)))

    def _stream_ok(self) -> bool:
        """Потоковый путь: включён в настройках, ответ не в канал модуля (там не речь), модель умеет поток, есть голос."""
        h = self._h
        if not self._cfg().get("stream", True) or not hasattr(h._llm, "ask_stream"):
            return False
        channel = current_channel.get()
        if channel is not None and channel.kind == "module":
            return False
        return speech_out.supports_streaming(h.speech)

    def cancel(self, reason: str = "stop") -> bool:
        """Оборвать идущий потоковый ответ (FAST-5): запрос к модели, остаток предложений. Звучащее останавливает вывод речи
        («замолчи», перебивание). Возвращает True, если было что обрывать."""
        s = self._session
        if s is None or s.cancelled:
            return False
        s.cancel(reason)
        return True

    @property
    def streaming(self) -> bool:
        return self._session is not None and not self._session.cancelled

    async def quick_fact(self, fact: str, user_text: str) -> str | None:
        """Реплика мгновенного уровня по факту (подтверждение, филлер, ТЗ 25 FAST-3): модель отвечает текстом реплики; вызов
        мимо блокировки модели. Нет модели или ответа - None (фраз в коде нет)."""
        h = self._h
        system = prompts.companion_persona() + " " + fact + " Ответь ТОЛЬКО текстом реплики, без пояснений."
        user = f"Пользователь сказал: {user_text}" if user_text else "Пользователь ждёт."
        try:
            try:
                reply = await h._llm.ask(system, user, timeout=15, max_tokens=40, temperature=0.7, parallel=True)
            except TypeError:
                reply = await h._llm.ask(system, user, timeout=15, max_tokens=40, temperature=0.7)
        except Exception as e:
            logger.info(f"Реплика мгновенного уровня не сформулирована: {e}")
            return None
        reply_trace.begin("quick", system, user, max_tokens=40, temperature=0.7)
        return clean_speech(reply.strip()) or None

    # ── ответ ──────────────────────────────────────────────────────────────────

    async def converse(self, text: str, about_self: bool = False, use_memory: bool = True, retrieval=None,
                       already_done=()) -> bool:
        """Обычный ответ обычным текстом, без JSON: так он приходит вдвое быстрее (локальная
        модель генерирует ~10-15 токенов в секунду, лишние поля стоят секунд).
        retrieval: поиск в памяти, запущенный раньше (ТЗ 25, FAST-6); already_done: что рефлекс уже сделал (FAST-1).
        Возвращает True, если модель пометила сообщение как просьбу действовать."""
        h = self._h
        system = (prompts.companion_persona() + " " + h.profile_hint() + " Если пользователь просит что-то СДЕЛАТЬ (включить, выключить, найти, написать, "
                  "опубликовать, посмотреть), начни ответ с маркера [ДЕЙСТВИЕ] и коротко скажи, что сейчас "
                  "посмотришь, что можешь, БЕЗ выдуманного результата. Иначе просто ответь.")
        if use_memory:
            deadline = self._cfg().get("memory_deadline_ms", 300)
            await h.context_builder.use_retrieval(retrieval, text, deadline / 1000 if deadline else None)
            await h.context_builder.refresh_knowledge(text)      # ТЗ 04, KB-11: справка и опыт, если включено (knowledge.chat)
        else:
            h.context_builder.clear_knowledge()
            if retrieval is not None:
                retrieval.cancel()
            h._self_block = ""
            h._graph_block = ""          # классификатор: для этой реплики факты о людях и прошлом не нужны (ATT-28)
        latency_trace.mark("retrieve_done")
        user = f"{h._context(query=text)}\n\nПользователь говорит: {text}"
        if already_done:
            user += "\n\nУже сделано без тебя (не повторяй и не обещай заново): " + "; ".join(already_done) + "."
        if about_self and h.router is not None:
            user += ("\n\nПользователь спрашивает о твоих возможностях. Отвечай строго по этому перечню, ничего не выдумывая:\n"
                     + h.router.registry.describe())
        stream = self._stream_ok()
        reply_trace.begin("converse", system, user, max_tokens=self._reply_tokens(), temperature=0.7, stream=stream)
        if stream:
            action_request, escalate = await self._converse_stream(system, user)
        else:
            action_request, escalate = await self._converse_whole(system, user)
        if escalate:
            await h._escalate(text)
        if len(text) >= 15:
            asyncio.create_task(h._learn(text))   # фоном, уже после ответа
        return action_request

    async def _converse_whole(self, system: str, user: str) -> tuple[bool, bool]:
        """Прежний путь: весь ответ целиком, потом реплика. (просьба действовать, нужна эскалация к Claude)."""
        h = self._h
        reply, escalate, action_request = "", False, False   # escalate: только если локальная модель недоступна
        try:
            reply = (await h._llm.ask(system, user, timeout=60, max_tokens=self._reply_tokens(), temperature=0.7)).strip()
            # без потоковой передачи первый токен приходит вместе с концом ответа
            latency_trace.mark("llm_first_token")
            latency_trace.mark("llm_done")
            if reply.startswith(ACTION_MARKER):
                action_request, reply = True, reply[len(ACTION_MARKER):].strip()
        except Exception as e:
            logger.warning(f"Локальная модель не ответила: {e}")
            reply_trace.drop()
            escalate = True    # без локальной модели пусть отвечает Claude
        if reply:
            latency_trace.mark("first_sentence_ready")
            await h.say(h._declutter(reply))
        return action_request, escalate

    async def _converse_stream(self, system: str, user: str) -> tuple[bool, bool]:
        """Потоковый путь (FAST-4, FAST-5). Эскалация к Claude, только если модель не дала ни одного предложения из-за сбоя."""
        h = self._h
        session = self._session = StreamSession()
        splitter, decl = SentenceSplitter(), StreamDeclutter(self)
        head, head_done, action_request, failed = "", False, False, False
        first_token = False
        gen = h._llm.ask_stream(system, user, timeout=60, max_tokens=self._reply_tokens(), temperature=0.7)
        stopper = asyncio.ensure_future(session.event.wait())
        nxt: asyncio.Future | None = None

        merge_rest = bool(self._cfg().get("merge_rest", True))   # первое предложение сразу, остальное одним куском: один шов
        held: list[str] = []

        async def deliver(sentences: list[str]) -> None:
            for sent in sentences:
                if session.cancelled:
                    return
                if merge_rest and session.sentences:
                    held.append(sent)                 # первое уже ушло в синтез: остальные ждут конца генерации
                    continue
                await self._emit(session, sent)

        async def flush_held() -> None:
            if held and not session.cancelled:
                text = " ".join(held)
                held.clear()
                await self._emit(session, text)

        try:
            while True:
                nxt = asyncio.ensure_future(gen.__anext__())
                await asyncio.wait({nxt, stopper}, return_when=asyncio.FIRST_COMPLETED)
                if session.cancelled:
                    nxt.cancel()
                    await asyncio.gather(nxt, return_exceptions=True)
                    break
                try:
                    delta = nxt.result()
                except StopAsyncIteration:
                    break
                if not first_token:
                    first_token = True
                    latency_trace.mark("llm_first_token")
                if not head_done:                              # маркер [ДЕЙСТВИЕ] разбирается до первого предложения
                    head += delta
                    probe = head.lstrip()
                    if probe.startswith(ACTION_MARKER):
                        action_request, delta = True, probe[len(ACTION_MARKER):]
                    elif ACTION_MARKER.startswith(probe):
                        continue                              # пока не ясно, маркер это или начало слова
                    else:
                        delta = head
                    head, head_done = "", True
                for sent in splitter.feed(delta):
                    await deliver(decl.push(sent))
                    if session.cancelled:
                        break
                if session.cancelled:
                    break
            if not session.cancelled:
                if head and not head_done:                     # поток кончился на обрывке, похожем на маркер
                    for sent in splitter.feed(head):
                        await deliver(decl.push(sent))
                for sent in splitter.flush():
                    await deliver(decl.push(sent))
                await deliver(decl.finish())
                await flush_held()
        except asyncio.CancelledError:
            session.cancel("task_cancelled")
            raise
        except Exception as e:
            logger.warning(f"Локальная модель не ответила: {e}")
            failed = True
            if session.sentences and not session.cancelled:   # часть ответа уже прозвучала: договаривать нечем, но и звать Claude поздно
                await deliver(decl.finish())
                await flush_held()
        finally:
            latency_trace.mark("llm_done")
            stopper.cancel()
            if nxt is not None and not nxt.done():
                nxt.cancel()
            try:
                await gen.aclose()
            except Exception:
                pass
            self._session = None
        if not session.sentences:
            reply_trace.drop()
        if session.sentences:
            h.commit_reply(" ".join(session.sentences))      # в память и журнал уходит то, что она успела сказать
        return action_request, failed and not session.sentences and not session.cancelled

    async def _emit(self, session: StreamSession, sentence: str) -> None:
        """Одно готовое предложение: в вывод речи (первое заменяет звучащее, следующие встают в очередь модуля голоса)."""
        h = self._h
        text = clean_speech(sentence)
        if not text or session.cancelled:
            return
        if not session.sentences:
            latency_trace.mark("first_sentence_ready")
        live = getattr(h.speech, "live_count", None)
        waited = 0.0
        while live is not None and live() >= BACKPRESSURE_LIVE and not session.cancelled and waited < 30.0:
            await asyncio.sleep(0.05)                         # модуль голоса держит в очереди не больше трёх: старое не вытесняем
            waited += 0.05
        if session.cancelled:
            return
        utterance = await h.say_part(text, first=not session.sentences)
        if utterance:
            session.utterances.append(utterance)
            session.sentences.append(text)
            if session.cancelled:                             # отмена пришла, пока предложение уходило: остановить и его
                await h.speech.stop(utterance)

    async def speak_about(self, fact: str, user_text: str, must: bool, question: bool, direct: bool):
        """Нет заготовленных фраз: ей сообщают факт, а говорить ли и что — решает сама.
        must=True для итога прямой просьбы пользователя: промолчать нельзя, он должен
        узнать, что получилось (иначе непонятно, сработало ли).
        question=True: факт - вопрос к пользователю (core.ask, вопрос задачи), его нужно задать своими словами.
        direct=False: факт кладётся в очередь речи внимания, реплику сформулирует планировщик, когда придёт очередь;
        direct=True: планировщик уже выбрал этот факт - реплика формулируется и говорится сразу."""
        h = self._h
        if h._queued(direct):
            await h._wait_spoken(h.attention.enqueue_fact(fact, user_text, must, question))
            return
        if question:
            rule = ("Пользователю нужно задать вопрос, который описан в факте: задай его своими словами коротко, "
                    "ничего не выдумывая и не отвечая за него. Промолчать нельзя. "
                    'Ответь ТОЛЬКО JSON: {"say": true, "text": "<реплика>"}')
        elif must:
            rule = ("Пользователь сам об этом попросил, поэтому обязательно коротко сообщи ему, "
                    "что получилось. Упомяни суть результата, не выдумывай деталей. "
                    'Ответь ТОЛЬКО JSON: {"say": true, "text": "<реплика>"}')
        else:
            rule = ("Сама реши, нужно ли что-то сказать: можно промолчать. Не пересказывай факт "
                    "дословно и не выдумывай деталей. Ответь ТОЛЬКО JSON: "
                    '{"say": <true или false>, "text": "<реплика>"}')
        system = prompts.companion_persona() + " Ниже факт о том, что только что произошло. " + rule
        user = (f"{h._context(query=user_text or fact)}\n\nФакт: {fact}"
                + (f"\nПользователь сказал: {user_text}" if user_text else ""))
        try:
            data = fast_brain._json_of(await h._llm.ask(system, user, timeout=60,
                                                        max_tokens=160, temperature=0.7))
        except Exception as e:
            logger.warning(f"Не удалось решить, что сказать: {e}")
            return
        if data and data.get("say") and data.get("text"):
            reply_trace.begin("fact", system, user, max_tokens=160, temperature=0.7, must=must, question=question)
            await h.say(str(data["text"]), direct=True)

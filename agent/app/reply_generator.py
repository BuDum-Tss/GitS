"""ReplyGenerator: ответ локальной модели в разговоре и формулировка реплики по факту (ТЗ 29, ARC-5).
Страховки стиля (`declutter`) здесь же. Точка для стриминга ответа (ТЗ 25)."""
import asyncio
import logging
import re

import fast_brain
import latency_trace
import prompts

logger = logging.getLogger("companion")


class ReplyGenerator:
    def __init__(self, host):
        self._h = host

    _OFFER = re.compile(r"^(ты )?(хочешь|не хочешь|хотела бы|хотел бы)(,? чтобы я)?|^если (хочешь|нужно)|^могу ли я|"
                        r"^нужна ли тебе|^чем (ещё )?(могу )?помочь|^давай я", re.I)

    _PROMISE = re.compile(r"^(сейчас |щас |хорошо, )?(опубликую|отправлю|запущу|включу|выключу|запощу|напишу в)", re.I)

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

    async def converse(self, text: str, about_self: bool = False, use_memory: bool = True) -> bool:
        """Обычный ответ обычным текстом, без JSON: так он приходит вдвое быстрее (локальная
        модель генерирует ~10-15 токенов в секунду, лишние поля стоят секунд).
        Возвращает True, если модель пометила сообщение как просьбу действовать."""
        h = self._h
        system = (prompts.companion_persona() + " " + h.profile_hint() + " Если пользователь просит что-то СДЕЛАТЬ (включить, выключить, найти, написать, "
                  "опубликовать, посмотреть), начни ответ с маркера [ДЕЙСТВИЕ] и коротко скажи, что сейчас "
                  "посмотришь, что можешь, БЕЗ выдуманного результата. Иначе просто ответь.")
        if use_memory:
            await h._refresh_graph(text)
        else:
            h._self_block = ""
            h._graph_block = ""          # классификатор: для этой реплики факты о людях и прошлом не нужны (ATT-28)
        latency_trace.mark("retrieve_done")
        user = f"{h._context(query=text)}\n\nПользователь говорит: {text}"
        if about_self and h.router is not None:
            user += ("\n\nПользователь спрашивает о твоих возможностях. Отвечай строго по этому перечню, ничего не выдумывая:\n"
                     + h.router.registry.describe())
        reply, escalate, action_request = "", False, False   # escalate: только если локальная модель недоступна
        try:
            reply = (await h._llm.ask(system, user, timeout=60, max_tokens=140, temperature=0.7)).strip()
            # ТЗ 24, TRC-1: без потоковой передачи первый токен приходит вместе с концом ответа (стриминг - ТЗ 25)
            latency_trace.mark("llm_first_token")
            latency_trace.mark("llm_done")
            if reply.startswith("[ДЕЙСТВИЕ]"):
                action_request, reply = True, reply[len("[ДЕЙСТВИЕ]"):].strip()
        except Exception as e:
            logger.warning(f"Локальная модель не ответила: {e}")
            escalate = True    # без локальной модели пусть отвечает Claude
        if reply:
            latency_trace.mark("first_sentence_ready")
            await h.say(h._declutter(reply))
        if escalate:
            await h._escalate(text)
        if len(text) >= 15:
            asyncio.create_task(h._learn(text))   # фоном, уже после ответа
        return action_request

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
            await h.say(str(data["text"]), direct=True)

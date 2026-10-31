"""ReplyGenerator: реплика по факту и страховки стиля (ТЗ 29, ARC-5).

Разговор с владельцем ведёт единый агент (`app/resident.py`). Здесь остаётся то, что нужно очереди речи: `speak_about`
формулирует реплику по факту (объявление, итог, вопрос модуля) и `declutter` убирает навязчивый хвост («Хочешь, чтобы я…?»,
третий вопрос подряд).
"""
import logging
import re

import fast_brain
import prompts
import reply_trace

logger = logging.getLogger("companion")


class ReplyGenerator:
    def __init__(self, host):
        self._h = host

    _OFFER = re.compile(r"^(ты )?(хочешь|не хочешь|хотела бы|хотел бы)(,? чтобы я)?|^если (хочешь|нужно)|^могу ли я|"
                        r"^нужна ли тебе|^чем (ещё )?(могу )?помочь|^давай я", re.I)

    def declutter(self, reply: str) -> str:
        """Страховка от навязчивости: убирает хвост-предложение помощи («Хочешь, чтобы я…?») и
        третий вопрос подряд в конце реплики. Применяется, только если останется хотя бы одно предложение."""
        h = self._h
        sents = [x for x in re.split(r"(?<=[.!?…])\s+", reply.strip()) if x]
        if len(sents) < 2:
            return reply
        last = sents[-1].strip(" «\"")
        recent = [m["text"].rstrip().endswith("?") for m in h._memory_reader.recent_dialogue(10) if m["role"] != "user"][-2:]
        if self._OFFER.match(last) or (sents[-1].rstrip().endswith("?") and len(recent) == 2 and all(recent)):
            return " ".join(sents[:-1])
        return reply

    def _is_repeat(self, text: str) -> bool:
        """Новая реплика по факту почти повторяет одну из её последних шести реплик."""
        from domain import repeat
        try:
            own = [d["text"] for d in self._h._memory_reader.recent_dialogue(12) if d.get("role") != "user"][-6:]
        except Exception:
            return False
        return repeat.is_repeat(text, own)

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
        system = (prompts.companion_persona() + " Ниже факт о том, что только что произошло. " + prompts.action_rule() + " " + rule
                  + (" Свои недавние реплики ты видишь в контексте: не повторяй их ни дословно, ни по смыслу, скажи иначе или промолчи."
                     if not must and not question else ""))
        if not user_text:
            h._graph_block = h._self_block = ""          # факт о событии: память прошлой реплики (блоки остались от неё) тут не нужна
        user = (f"{h._context(query=user_text or fact, owner=True if user_text else False, role='fact')}\n\nФакт: {fact}"
                + (f"\nПользователь сказал: {user_text}" if user_text else ""))
        try:
            data = fast_brain._json_of(await h._llm.ask(system, user, timeout=60,
                                                        max_tokens=160, temperature=0.7))
        except Exception as e:
            logger.warning(f"Не удалось решить, что сказать: {e}")
            return
        if data and data.get("say") and data.get("text"):
            if not must and not question and self._is_repeat(str(data["text"])):
                logger.info("Реплика почти повторяет недавнюю: промолчала")
                return
            reply_trace.begin("fact", system, user, max_tokens=160, temperature=0.7, must=must, question=question)
            await h.say(str(data["text"]), direct=True)

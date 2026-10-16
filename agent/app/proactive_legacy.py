"""Временный сценарий проактивности (ТЗ 29, ARC-5): опрос себя раз в 30 с, как было в `Companion`.

По решению владельца агент не говорит по таймеру: сценарий заменили внутренние импульсы и размышление (ТЗ 30). Опрос
выключен настройкой `proactive_tick` (по умолчанию False); файл удаляется через одну версию."""
import asyncio
import json
import logging
import time

import fast_brain
import gamenet
import prompts

logger = logging.getLogger("companion")

MIN_GAP = {"quiet": 30 * 60, "normal": 10 * 60, "chatty": 3 * 60}   # не говорить сама чаще
CONSULT = {"quiet": 10 * 60, "normal": 4 * 60, "chatty": 90}        # как часто спрашивать себя


class ProactiveLegacy:
    def __init__(self, host):
        self._h = host

    async def loop(self):
        h = self._h
        while True:
            await asyncio.sleep(30)
            try:
                if not h._settings.load().get("proactive_tick"):
                    continue                  # ТЗ 30, IMP-8: болтовни по таймеру нет, пока её не включили явно
                await h._maybe_speak()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"Проактивность: {e}")
                h._log.log("error", f"проактивность: {e}")

    def digest(self, silent_mins: int) -> str:
        """Дайджест для решения «заговорить ли»: время, молчание, занятия за последние минуты."""
        h = self._h
        lines = [f"Сейчас {time.strftime('%H:%M')}. Ты молчишь уже {silent_mins} мин."]
        if h._settings.load()["vision"]:
            acts = h._memory_reader.recent_activity(5)
            if acts:
                lines.append("Что ты видела на экране (от старого к новому):")
                for a in acts:
                    mins = int((time.time() - a["ts"]) / 60)
                    lines.append(f"  {mins} мин назад: {a['summary']}")
        else:
            lines.append("Зрение выключено, на экран ты не смотришь.")
        plans = h._memory_reader.plans_to_nudge()[:2]
        if plans:
            lines.append("Скоро у пользователя (напомни ОДИН раз, коротко, и больше к этому не возвращайся):")
            lines += [f"  - {p['text']}" for p in plans]
        return "\n".join(lines)

    def repeats_recent(self, text: str) -> bool:
        """Не повторять недавние темы: сильное пересечение по словам с её последними репликами."""
        from memory_store import stems
        new = stems(text)
        if len(new) < 3:
            return False
        for m in [m for m in self._h._memory_reader.recent_dialogue(10) if m["role"] != "user"][-5:]:
            old = stems(m["text"])
            if old and len(new & old) / min(len(new), len(old)) >= 0.5:
                return True
        return False

    async def maybe_speak(self):
        h = self._h
        if h._busy.locked() or h._approver.has_pending():
            return
        if h._pending_memory >= 2 and time.time() - h._last_user_ts > 8 * 60:
            asyncio.create_task(h._consolidate())   # пауза в разговоре: самое время осмыслить
        now = time.time()
        idle = h.idle_seconds()
        # Пользователь отошёл / вернулся
        if idle > 600:
            if h._away_since is None:
                h._away_since = now - idle
            return
        returned_after = None
        if h._away_since is not None:
            away_mins = (now - h._away_since) / 60
            h._away_since = None
            if away_mins >= 15:
                returned_after = int(away_mins)

        cfg = h._settings.load()
        if h._settings.quiet_active(cfg):
            return   # пользователь просил не отвлекать (смотрит, работает)
        level = cfg["chattiness"]
        gap = now - h._last_say
        if now - h._last_user_ts < 120:
            return   # только что разговаривали: сама не вклинивается
        if gamenet.port_open() and gap < 45 * 60:
            return   # идёт игра: про игру говорит игровой комментатор, а тут сама не лезет
        cur = h.screen_activity() if cfg["vision"] else None
        if (cur and cur["category"] in ("media", "game") and now - cur["ts"] < 600
                and gap < 45 * 60):
            return   # смотрит видео или играет: сама не лезет (если не слишком долго молчала)
        if returned_after is not None and gap > 120:
            situation = f"Пользователь вернулся к компьютеру после перерыва в {returned_after} мин."
        else:
            if gap < MIN_GAP.get(level, MIN_GAP["normal"]):
                return
            if now - h._last_consult < CONSULT.get(level, CONSULT["normal"]):
                return
            situation = h._digest(int(gap / 60))
        h._last_consult = now

        system = (prompts.companion_persona() + "\n" + h.profile_hint() + "\nСама решай, стоит ли сейчас вмешаться. Говори, когда есть что-то уместное: "
                  "наблюдение о том, что видишь, шутка про занятие, забота (долго сидит, поздно), вопрос. "
                  "Не навязывайся без повода: если пользователь сейчас занят (игра, работа, видео) или сказать "
                  "нечего, ответь say=false. Не возвращайся к темам, которые уже поднимала в последних "
                  "репликах. Ответь ТОЛЬКО JSON: "
                  '{"say": <true или false>, "text": "<реплика>", "emotion": <одно из '
                  + json.dumps(fast_brain.EMOTIONS) + ">}")
        user = f"{h._context()}\n\n{situation}"
        raw = await h._llm.ask(system, user, timeout=60, max_tokens=200, temperature=0.8,
                               background=True)
        data = fast_brain._json_of(raw)
        if data and data.get("say") and data.get("text") and h._repeats_recent(str(data["text"])):
            h._log.log("proactive", situation, said=False, why="повтор недавней темы")
            h._last_say = now - MIN_GAP.get(level, MIN_GAP["normal"]) / 2
        elif data and data.get("say") and data.get("text"):
            emo = data.get("emotion") if data.get("emotion") in fast_brain.EMOTIONS else None
            await h.say(str(data["text"]), emo, source="proactive")
            h._log.log("proactive", situation, said=True)
        else:
            h._log.log("proactive", situation, said=False)

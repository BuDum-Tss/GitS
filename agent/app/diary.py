"""Суточная сводка и дневник от первого лица (ТЗ 27, EPI-5, EPI-8).

Оба текста пишет локальная модель по фактам суток (резюме эпизодов, счётчик спокойного); готовых фраз в коде нет. Имена и числа
проверяются по тем же источникам, что и резюме эпизодов (`domain.episodes.unsupported`); при замечаниях одна повторная
попытка, потом текст не пишется (повторится следующим сном). Записи индексируются для поиска: коллекции `episodes`
(ключ `digest:<день>`) и `diary` (ключ - день)."""
import asyncio
import logging
from typing import Callable

from app.reflection import parse_step
from contracts import LanguageModel
from domain import episodes as dom

logger = logging.getLogger("episodes")

ASK_TIMEOUT = 45.0


def day_material(episodes: list[dict], digest: dict | None) -> str:
    """Факты суток для сводки и дневника: резюме эпизодов по времени и сколько было спокойного."""
    lines = [f"- {e['title']}: {e['summary']}" for e in episodes if e.get("summary")]
    calm = (digest or {}).get("calm") or {}
    if calm.get("n"):
        lines.append(f"Спокойных событий без эпизодов: {calm['n']}.")
    return "\n".join(lines)


async def _write(llm: LanguageModel, prompt: dict, material: str, head: str, allow, source: str) -> tuple[str | None, int, bool]:
    """(текст или None, потрачено токенов, модель недоступна)."""
    system = f"{prompt.get('system', '')}\n{prompt.get('format', '')}".strip()
    remark = ""
    spent = 0
    for _ in (1, 2):
        user = f"{head}\n<<<\n{material}\n>>>" + (f"\n\nЗамечания к прошлому ответу, исправь их: {remark}" if remark else "")
        try:
            raw = await llm.ask(system, user, timeout=ASK_TIMEOUT, max_tokens=500, temperature=0.6, background=True) or ""
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.info(f"Сводка или дневник: модель недоступна: {e}")
            raw = ""
        spent += (len(system) + len(user) + len(raw)) // 3
        if not raw.strip():
            return None, spent, True
        data = parse_step(raw)
        text = " ".join(str((data or {}).get("text") or "").split())
        problems = [] if text else ["нет поля text"]
        if text:
            problems += dom.unsupported(text, source, allow)
        if not problems:
            return text[:2500], spent, False
        remark = "; ".join(problems)[:300]
    return None, spent, False


class DayWriter:
    def __init__(self, *, llm: LanguageModel, store, search, digest_prompt: Callable[[], dict], diary_prompt: Callable[[], dict],
                 allow: Callable[[], list[str]] = lambda: [], character: Callable[[], str] = lambda: ""):
        self.llm, self.store, self.search = llm, store, search
        self.digest_prompt, self.diary_prompt, self.allow, self.character = digest_prompt, diary_prompt, allow, character

    async def digest(self, day: str) -> tuple[bool, int, bool]:
        """Сводка суток, если эпизодов стало больше, чем в прошлой версии. (записана ли, токены, модель недоступна)."""
        eps = await asyncio.to_thread(self.store.episodes_of_day, day)
        dig = await asyncio.to_thread(self.store.digest, day)
        if not eps or (dig and dig.get("text") and dig.get("episodes", 0) >= len(eps)):
            return False, 0, False
        material = day_material(eps, dig)
        text, spent, down = await _write(self.llm, self.digest_prompt(), material, f"Сутки {day}. Эпизоды:", self.allow(), material)
        if text is None:
            return False, spent, down
        await asyncio.to_thread(self.store.set_digest, day, text, len(eps))
        if self.search is not None:
            await self.search.add("episodes", f"digest:{day}", text, ts=eps[-1]["t_end"], meta={"digest": True, "day": day})
        return True, spent, False

    async def diary(self, day: str) -> tuple[bool, int, bool]:
        eps = await asyncio.to_thread(self.store.episodes_of_day, day)
        if not eps:
            return False, 0, False
        dig = await asyncio.to_thread(self.store.digest, day)
        material = day_material(eps, dig) + (f"\nИтог суток: {dig['text']}" if dig and dig.get("text") else "")
        text, spent, down = await _write(self.llm, self.diary_prompt(), material, f"Запись дневника за {day}. Факты дня:",
                                         self.allow(), material)
        if text is None:
            return False, spent, down
        await asyncio.to_thread(self.store.put_diary, day, text, self.character(), [e["id"] for e in eps])
        if self.search is not None:
            await self.search.add("diary", day, text, ts=eps[-1]["t_end"], meta={"day": day})
        return True, spent, False

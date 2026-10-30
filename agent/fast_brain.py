"""Локальная модель (LM Studio): один вызов `_ask`, потоковый `_ask_stream`, оценка тона сообщения игрока.

Модель одна, вызовы идут по очереди, срочные (ответ пользователю, план) вытесняют фоновые (запоминание, зрение, оценка
тона). Реплику здесь не пишут и эмоцию не выбирают: теги стикеров ставит сама модель в тексте ответа (`domain/stickers.py`),
а решения принимает классификатор (`decide.py`).
"""
import asyncio
import json
import logging
import re
import time

import httpx

import events_log
import flow_trace
import salience  # ТЗ 24, TRC-4: изменение отношений меняет яркость события
from envcompat import env

logger = logging.getLogger("fast_brain")

LM_URL   = env("CHARACTER_FAST_URL", "http://127.0.0.1:1234/v1/chat/completions")
LM_MODEL = env("CHARACTER_FAST_MODEL", "google/gemma-4-e4b")
TIMEOUT  = 8.0
WARMUP_TIMEOUT = 90.0   # холодный старт: LM Studio грузит модель ~20 с
_lock: asyncio.Lock | None = None

_CHAT_SYSTEM = (
    "Ты — рефлекторный слой {name}. Игрок написал сообщение. Оцени, как оно "
    "меняет её отношение к игроку (delta от -10 грубость, до +10 тепло и "
    "уважение; 0 — нейтрально). Ответь ТОЛЬКО JSON: "
    '{"delta": <целое от -10 до 10>}'
)

def _json_of(txt: str) -> dict | None:
    """Достать JSON из ответа модели. Небольшие модели иногда пишут текстовое
    поле без кавычек ("text": Привет, мир, "x": ...), поэтому при сбое
    строгого разбора значения чинятся: что не JSON, то считается строкой."""
    a, b = txt.find("{"), txt.rfind("}")
    body = txt[a:b + 1]
    try:
        return json.loads(body)
    except ValueError:
        pass
    keys = list(re.finditer(r'"(\w+)"\s*:\s*', body))
    if not keys:
        return None
    out = {}
    for i, m in enumerate(keys):
        end = keys[i + 1].start() if i + 1 < len(keys) else len(body)
        raw = body[m.end():end].strip()
        if i + 1 == len(keys):
            raw = raw.rstrip("}").strip()
        raw = raw.rstrip(",").strip()
        try:
            out[m.group(1)] = json.loads(raw)
        except ValueError:
            out[m.group(1)] = raw.strip('"').strip()
    return out


class Preempted(Exception):
    """Фоновый вызов вытеснен срочным (ответ пользователю важнее анализа экрана и т.п.)."""


def _use_claude() -> bool:
    """Быстрая модель на Claude (настройка fast_llm, по умолчанию claude) или локальная (LM Studio)."""
    try:
        import settings
        return settings.load().get("fast_llm", "claude") != "local"
    except Exception:
        return False


_bg_calls: set = set()   # идущие фоновые HTTP-вызовы: срочный запрос отменяет их
_fg_waiting = 0          # сколько срочных запросов сейчас ждёт или идёт


async def _post(body: dict, timeout: float):
    async with httpx.AsyncClient(timeout=timeout) as c:
        return await c.post(LM_URL, json=body)


async def _ask(system: str, user: str, timeout: float, max_tokens: int = 40,
               image_b64: str | None = None, temperature: float = 0.3,
               background: bool = False, parallel: bool = False) -> str:
    """Один вызов локальной модели. Модель одна, поэтому вызовы идут по очереди, но срочные
    (ответ пользователю, план) вытесняют фоновые (запоминание, зрение, оценка тона).
    background=True: вызов уступает срочным, при вытеснении повторяется, а после трёх
    неудач бросает Preempted.
    image_b64 — JPEG в base64 (модель должна уметь картинки, Gemma 4 умеет).
    parallel=True (ТЗ 25, FAST-3): короткий вызов мгновенного уровня (подтверждение, филлер) идёт мимо блокировки и не
    вытесняет других: иначе фраза «принято» ждала бы план, который она и должна опередить."""
    global _lock, _fg_waiting
    _t_llm = time.perf_counter()
    if _use_claude():
        import claude_llm
        out = await claude_llm.ask(system, user, max(timeout, 30.0), image_b64=image_b64)
        flow_trace.step(None, "gemma", "planner", "gemma", "llm", ms=(time.perf_counter() - _t_llm) * 1000, size=len(system) + len(str(user)),
                        label="claude")
        return out
    if _lock is None:
        _lock = asyncio.Lock()
    content = user
    if image_b64:
        content = [{"type": "text", "text": user},
                   {"type": "image_url",
                    "image_url": {"url": "data:image/jpeg;base64," + image_b64}}]
    body = {
        "model": LM_MODEL,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": content}],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "reasoning_effort": "none",   # иначе Gemma «размышляет» и съедает все токены
    }
    if parallel:
        r = await _post(body, timeout)
    elif not background:
        _fg_waiting += 1
        try:
            for task in list(_bg_calls):
                task.cancel()          # освобождаем модель для срочного запроса
            async with _lock:
                r = await _post(body, timeout)
        finally:
            _fg_waiting -= 1
    else:
        r = None
        for attempt in range(3):
            while _fg_waiting:
                await asyncio.sleep(0.2)      # срочное идёт: уступаем
            async with _lock:
                task = asyncio.ensure_future(_post(body, timeout))
                _bg_calls.add(task)
                try:
                    r = await task
                    break
                except asyncio.CancelledError:
                    if asyncio.current_task().cancelling():
                        raise                 # отменили нас самих, а не только вызов
                finally:
                    _bg_calls.discard(task)
            await asyncio.sleep(1.0)          # вытеснили: подождать и повторить
        if r is None:
            raise Preempted("фоновый вызов вытеснен срочными")
    flow_trace.step(None, "gemma", "planner", "gemma", "llm", ms=(time.perf_counter() - _t_llm) * 1000, size=len(system) + len(str(user)),
                    label="background" if background else "gemma")
    data = r.json()
    if "choices" not in data:   # LM Studio вернула ошибку (модель грузится, перегрузка): объясняем, а не падаем с KeyError
        raise RuntimeError(str(data.get("error") or data)[:200])
    return data["choices"][0]["message"].get("content") or ""


def _stream_piece(line: str) -> str | None:
    """Кусок текста из строки потока LM Studio (`data: {...}`); None - строка без текста или конец потока."""
    line = line.strip()
    if not line.startswith("data:"):
        return None
    payload = line[5:].strip()
    if not payload or payload == "[DONE]":
        return None
    try:
        data = json.loads(payload)
        return (data["choices"][0].get("delta") or {}).get("content") or None
    except (ValueError, KeyError, IndexError, TypeError):
        return None


async def _ask_stream(system: str, user: str, timeout: float, max_tokens: int = 140, temperature: float = 0.7):
    """Ответ локальной модели потоком (ТЗ 25, FAST-4): асинхронный генератор кусков текста. Срочный вызов, как `_ask` без
    background: вытесняет фоновые и держит блокировку, пока идёт поток. Закрытие генератора (`aclose`) обрывает запрос.
    Ошибка LM Studio или сети - исключение до первого куска или посреди потока."""
    global _lock, _fg_waiting
    if _use_claude():
        import claude_llm
        async for piece in claude_llm.ask_stream(system, user, max(timeout, 30.0)):
            yield piece
        return
    if _lock is None:
        _lock = asyncio.Lock()
    body = {
        "model": LM_MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "reasoning_effort": "none",
        "stream": True,
    }
    _fg_waiting += 1
    try:
        for task in list(_bg_calls):
            task.cancel()
        async with _lock:
            async with httpx.AsyncClient(timeout=timeout) as c:
                async with c.stream("POST", LM_URL, json=body) as r:
                    if r.status_code != 200:
                        raise RuntimeError((await r.aread()).decode("utf-8", "replace")[:200])
                    async for line in r.aiter_lines():
                        piece = _stream_piece(line)
                        if piece:
                            yield piece
    finally:
        _fg_waiting -= 1


async def react_chat(text: str, rel=None) -> dict | None:
    """Реакция на сообщение игрока: сдвиг отношения к игроку."""
    t0 = time.perf_counter()
    try:
        j = _json_of(await _ask(_CHAT_SYSTEM, text[:300], TIMEOUT, background=True))
        delta = max(-10, min(10, int(j.get("delta", 0)))) if j else 0
    except Exception as e:
        logger.warning(f"Не удалось оценить сообщение игрока: {e}")
        return None
    res = {"source": "llm", "delta": delta, "ms": round((time.perf_counter() - t0) * 1000)}
    sal, reasons = salience.score(salience.factors_of(delta=delta))
    events_log.log("reflex", "оценка тона сообщения пользователя", delta=delta, ms=res["ms"],
                   salience=sal, reasons=reasons)
    if rel is not None and delta:
        from relationships import PLAYER
        rel.adjust(PLAYER, delta, f"написал: {text[:60]}")
    logger.info(f"[fast] чат → отношение к игроку {delta:+d} ({res['ms']} мс)")
    return res


async def warmup():
    """Загрузить модель в LM Studio заранее, чтобы первый ответ не ждал ~20 с (для Claude прогрев не нужен)."""
    if _use_claude():
        return
    t0 = time.perf_counter()
    try:
        await _ask("Ответь одним словом.", "Привет", WARMUP_TIMEOUT, max_tokens=4)
        logger.info(f"[fast] прогрев: {round((time.perf_counter() - t0) * 1000)} мс")
    except Exception as e:
        logger.warning(f"[fast] прогрев не удался: {e}")

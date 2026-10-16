"""Быстрый «рефлекторный» мозг: локальная LLM (LM Studio) → эмоция + команда.

Реплику НЕ генерирует — комментарий по-прежнему пишет Claude (run_turn).
Задача здесь одна: за секунду-две решить, какую эмоцию показать и нужна ли
простая команда игре. Результат пишется в memory/emotion.json, откуда его
берёт dashboard (/api/state → "emotion") и читает модуль pet (через presence: emotion и emotion_ts, PET-9).

Простые команды (draft_colonists / undraft) исполняет reflex_actions — с
проверкой по реальному состоянию игры. Отношения к колонистам и игроку
(relationships.py) подмешиваются в подсказку, поэтому смерть любимого и
нелюбимого колониста вызывает разную эмоцию.
Если LM Studio недоступна или ответ не разобрать — работает таблица правил.
"""
import asyncio
import json
import logging
import re
import time
from pathlib import Path

import httpx

import character
import events_log
import journal
import reflex_actions
import salience  # ТЗ 24, TRC-4: изменение отношений меняет яркость события
from envcompat import env

logger = logging.getLogger("fast_brain")

LM_URL   = env("CHARACTER_FAST_URL", "http://127.0.0.1:1234/v1/chat/completions")
LM_MODEL = env("CHARACTER_FAST_MODEL", "google/gemma-4-e4b")
TIMEOUT  = 8.0
WARMUP_TIMEOUT = 90.0   # холодный старт: LM Studio грузит модель ~20 с
_lock: asyncio.Lock | None = None

EMOTIONS = ["idle", "action", "angry", "question", "looking",
            "sitting", "thinking", "happy", "threat", "resting",
            "sad", "scared", "proud", "surprised"]
ACTIONS  = ["none", "draft_colonists", "undraft"]

_SYSTEM = (
    "Ты — рефлекторный слой {name} (колония в RimWorld). Получив событие, выбери "
    "эмоцию стикера и простую команду. draft_colonists — только если колонии "
    "угрожают враги (рейд); undraft — когда опасность миновала; в остальных "
    "случаях none. Учитывай отношения, если они указаны: смерть того, к кому "
    "есть привязанность, — sad, смерть нелюбимого — idle или angry. Ответь "
    'ТОЛЬКО JSON без пояснений: {"emotion": <одно из ' + json.dumps(EMOTIONS) + '>, '
    '"action": <одно из ' + json.dumps(ACTIONS) + '>, '
    '"intensity": <целое от 0 до 3: насколько сильна эмоция, 0 почти нет, 3 очень сильна>}'
)

_CHAT_SYSTEM = (
    "Ты — рефлекторный слой {name}. Игрок написал сообщение. Оцени, как оно "
    "меняет её отношение к игроку (delta от -10 грубость, до +10 тепло и "
    "уважение; 0 — нейтрально) и выбери эмоцию стикера. Ответь ТОЛЬКО JSON: "
    '{"delta": <целое от -10 до 10>, "emotion": <одно из ' + json.dumps(EMOTIONS) + '>, '
    '"intensity": <целое от 0 до 3: насколько сильна эмоция>}'
)

# Запасные правила, если локальная модель недоступна
_FALLBACK = {
    "colony_died":  ("sad", "none"),
    "pawn_died":    ("sad", "none"),
    "fire_started": ("scared", "none"),
    "game_loaded":  ("looking", "none"),
}

_EMOTION_FILE: Path | None = None


def init(memory_dir: str):
    global _EMOTION_FILE
    _EMOTION_FILE = Path(memory_dir) / "emotion.json"


_TAGS = re.compile(r"<[^>]+>|\(\*[^)]*\)|\(/[^)]*\)")


def clean_tags(value) -> str:
    """Текст письма игры: мод отдаёт TaggedString как словарь {'RawText': ...} и с разметкой <color>, (*Name)."""
    if isinstance(value, dict):
        value = value.get("RawText") or ""
    text = str(value)
    m = re.match(r"^\{'RawText': '(.*?)', 'Length'", text)
    if m:
        text = m.group(1)
    return " ".join(_TAGS.sub("", text).split())


def describe(evt: dict, rel=None) -> str:
    """Короткое текстовое описание события для модели (с отношением, если есть)."""
    d = evt.get("data", {}) or {}
    t = evt.get("type", "?")
    if t == "pawn_died":
        name = d.get("name", "?")
        extra = rel.describe(name) if rel else ""
        return (f"Колонист {name} умер: {d.get('cause', 'неизвестно')}."
                + (f" ({extra})" if extra else ""))
    if t == "colony_died":
        return "Вся колония погибла."
    if t == "fire_started":
        return "Начался пожар на базе."
    if t == "game_loaded":
        return f"Загружена колония {d.get('colony', '?')}."
    if t == "letter":
        text = clean_tags(d.get("text") or "")[:200]
        return f"Письмо: {clean_tags(d.get('label', '?'))}. {text}".strip()
    return f"Событие {t}."


def _json_of(txt: str) -> dict | None:
    """Достать JSON из ответа модели. Небольшие модели иногда пишут текстовое
    поле без кавычек ("text": Привет, мир, "emotion": ...), поэтому при сбое
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


def _parse(txt: str) -> dict | None:
    j = _json_of(txt)
    if not j or j.get("emotion") not in EMOTIONS:
        return None
    if j.get("action") not in ACTIONS:
        j["action"] = "none"
    return {"emotion": j["emotion"], "action": j["action"], "intensity": journal.clamp_intensity(j.get("intensity"))}


class Preempted(Exception):
    """Фоновый вызов вытеснен срочным (ответ пользователю важнее анализа экрана и т.п.)."""


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


async def classify(evt: dict, rel=None, timeout: float = TIMEOUT) -> dict:
    """Вернуть {"emotion", "action", "source", "ms"}. Никогда не бросает исключений."""
    t0 = time.perf_counter()
    result, source = None, "rules"
    try:
        result = _parse(await _ask(character.fill(_SYSTEM), describe(evt, rel), timeout))
        if result:
            source = "llm"
    except Exception as e:
        logger.warning(f"Локальная LLM недоступна: {e}")
    if result is None:
        emo, act = _FALLBACK.get(evt.get("type"), ("looking", "none"))
        if evt.get("type") == "letter" and evt.get("priority") == "critical":
            emo, act = "threat", "draft_colonists"   # исполнится только при реальных врагах
        result = {"emotion": emo, "action": act, "intensity": 1}
    result["source"] = source
    result["ms"] = round((time.perf_counter() - t0) * 1000)
    return result


_listeners: list = []
_affect = None          # AffectService (ТЗ 26): оценка событий и интенсивность реплик; None - состояния эмоций нет


def set_affect(service) -> None:
    global _affect
    _affect = service


def add_listener(callback):
    """Подписка на эмоции (PET-9): callback(emotion, ts) вызывается при каждой публикации. Ядро кладёт их в presence, а
    шина модулей рассылает тему presence (питомец показывает стикер). Возвращает callback (для remove_listener)."""
    _listeners.append(callback)
    return callback


def remove_listener(callback) -> None:
    if callback in _listeners:
        _listeners.remove(callback)


def publish(evt_name: str, res: dict):
    """Записать эмоцию для дашборда и сообщить подписчикам (presence для модулей) и в журнал (ТЗ 24, TRC-3)."""
    ts = time.time()
    intensity = res.get("intensity", 1)
    if _affect is not None:
        if evt_name == "say":                   # эмоция реплики: интенсивность из состояния (ТЗ 26, AFF-5)
            intensity = _affect.reply_intensity(res["emotion"])
        else:
            _affect.on_emotion(evt_name, {**res, "intensity": intensity})
    journal.emotion(res["emotion"], intensity, reason=evt_name, character=character.active().id)
    for callback in list(_listeners):
        try:
            callback(res["emotion"], ts)
        except Exception as e:
            logger.warning(f"Подписчик эмоций: {e}")
    if _EMOTION_FILE is None:
        return
    try:
        _EMOTION_FILE.write_text(json.dumps({
            "emotion": res["emotion"], "action": res.get("action", "none"),
            "event": evt_name, "ts": ts,
            "source": res.get("source"), "ms": res.get("ms"),
        }, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.warning(f"Не удалось записать emotion.json: {e}")


async def react(evt: dict, state: dict | None = None, rel=None) -> dict:
    """Реакция на событие игры: эмоция + (если нужно и можно) команда игре."""
    res = await classify(evt, rel)
    publish(evt.get("type", "?"), res)
    outcome = ""
    if res["action"] != "none" and state is not None:
        outcome = " | команда: " + await reflex_actions.execute(res["action"], state)
    logger.info(f"[fast] {evt.get('type')} → {res['emotion']} / {res['action']} "
                f"({res['source']}, {res['ms']} мс){outcome}")
    events_log.log("reflex", describe(evt, rel), emotion=res["emotion"], action=res["action"],
                   source=res["source"], ms=res["ms"], result=outcome.removeprefix(" | команда: "))
    return res


async def react_chat(text: str, rel=None) -> dict | None:
    """Реакция на сообщение игрока: эмоция и сдвиг отношения к игроку."""
    t0 = time.perf_counter()
    try:
        j = _json_of(await _ask(_CHAT_SYSTEM, text[:300], TIMEOUT, background=True))
        emo = j.get("emotion") if j else None
        delta = max(-10, min(10, int(j.get("delta", 0)))) if j else 0
        intensity = journal.clamp_intensity(j.get("intensity")) if j else 1
    except Exception as e:
        logger.warning(f"Не удалось оценить сообщение игрока: {e}")
        return None
    if emo not in EMOTIONS:
        emo = "looking"
    res = {"emotion": emo, "action": "none", "source": "llm", "intensity": intensity, "delta": delta,
           "ms": round((time.perf_counter() - t0) * 1000)}
    publish("chat", res)
    sal, reasons = salience.score(salience.factors_of(delta=delta))
    events_log.log("reflex", "оценка тона сообщения пользователя", emotion=emo, delta=delta, ms=res["ms"],
                   salience=sal, reasons=reasons)
    if rel is not None and delta:
        from relationships import PLAYER
        rel.adjust(PLAYER, delta, f"написал: {text[:60]}")
    logger.info(f"[fast] чат → {emo}, отношение к игроку {delta:+d} ({res['ms']} мс)")
    return res


async def warmup():
    """Загрузить модель в LM Studio заранее, чтобы первое событие не ждало ~20 с."""
    res = await classify({"type": "game_loaded", "data": {"colony": "прогрев"}},
                         timeout=WARMUP_TIMEOUT)
    logger.info(f"[fast] прогрев: {res['source']}, {res['ms']} мс")

"""Помощники плана монтажа: озвучка реплик, подгонка закадра, текстовый вид истории для сайта (ТЗ 20, раздел 6).

Команды окружения (`voiceover`) и сайт берут отсюда: логика одна и та же.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
from pathlib import Path

from gits_sdk import ToolError

from core_api import CoreAPI
from jobs import Job

VOICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
MAX_VOICE_CHARS = 400
VOICE_GAP_MS = 300                      # пауза между репликами закадра (регламент §5: 250-400 мс)


# ---------------------------------------------------------------------------------------------------- озвучка

def voice_cache_path(job: Job) -> Path:
    return job.plan_dir / "voiceover_cache.json"


def load_voice_cache(job: Job) -> dict:
    try:
        return json.loads(voice_cache_path(job).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def voice_digest(text: str, emotion: str | None) -> str:
    return hashlib.sha1(f"{emotion or 'neutral'}|{text}".encode()).hexdigest()


async def synthesize_to(core: CoreAPI, job: Job, voice_id: str, text: str, emotion: str) -> dict:
    """Синтез одной реплики её голосом: core.tts -> work/public/vo/<id>.wav; запись в кэш (id -> хэш текста и эмоции, длительность)."""
    if not VOICE_ID_RE.match(voice_id or ""):
        raise ToolError("id реплики: буквы, цифры, _ и - (до 32 символов)", code="invalid")
    text = (text or "").strip()
    if not text or len(text) > MAX_VOICE_CHARS:
        raise ToolError(f"текст реплики {voice_id}: от 1 до {MAX_VOICE_CHARS} символов", code="invalid")
    value = await core.tts(text, emotion)
    target = job.work / "public" / "vo" / f"{voice_id}.wav"
    target.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(shutil.copyfile, value["path"], target)
    cache = load_voice_cache(job)
    cache[voice_id] = {"hash": voice_digest(text, emotion), "duration_ms": int(value["duration_ms"])}
    await asyncio.to_thread(voice_cache_path(job).write_text, json.dumps(cache, ensure_ascii=False, indent=2), "utf-8")
    return {"id": voice_id, "duration_ms": int(value["duration_ms"]), "path": f"public/vo/{voice_id}.wav"}


def fit_voiceover(plan: dict) -> dict:
    """Реплики закадра не наезжают друг на друга: следующая сдвигается, если начинается раньше конца предыдущей плюс пауза
    (регламент §5). Реплика с `duck: true` намеренно идёт поверх. Ролик без исходного видео удлиняется до конца закадра."""
    vos = sorted(plan.get("voiceover") or [], key=lambda v: v["at_ms"])
    shifted = []
    cursor = 0
    for v in vos:
        dur = int(v.get("duration_ms") or 0)
        if not v.get("duck") and v["at_ms"] < cursor + (VOICE_GAP_MS if cursor else 0):
            new = cursor + VOICE_GAP_MS
            shifted.append({"id": v["id"], "from": v["at_ms"], "to": new})
            v["at_ms"] = new
        cursor = max(cursor, v["at_ms"] + dur)
    overflow = max(0, cursor - plan["duration_ms"])
    extended = False
    if overflow and not plan.get("segments"):
        plan["duration_ms"] = cursor + 600
        extended = True
        overflow = 0
    plan["voiceover"] = vos
    return {"shifted": shifted, "overflow_ms": overflow, "duration_extended": extended}


# ---------------------------------------------------------------------------------------------------- тексты для сайта

def story_markdown(story: dict) -> str:
    out = ["# История ролика", "", f"**Логлайн.** {story['logline']}", "", f"**Крючок.** {story['hook']['what']} ({story['hook']['why']})", "",
           f"**Обещание.** {story['promise']}", "", "## Биты"]
    for b in story["beats"]:
        out.append(f"- {b['id']} [{b['role']}] {b['t_from_ms'] / 1000:.1f}–{b['t_to_ms'] / 1000:.1f} с: {b['purpose']}")
    for sp in story.get("setups_payoffs") or []:
        out.append(f"- подготовка {sp['setup']} → отдача {sp['payoff']}: {sp.get('what', '')}")
    return "\n".join(out) + "\n"

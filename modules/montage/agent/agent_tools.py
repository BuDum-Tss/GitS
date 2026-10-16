"""Свои инструменты агента-монтажёра (ТЗ 20, раздел 8.2): логика без привязки к SDK.

`AgentTools` — обычные асинхронные методы над окружением задачи (`JobEnv`). `claude_runner` оборачивает их в SDK MCP
(`create_sdk_mcp_server`), а тесты и заглушка агента вызывают напрямую.
"""
from __future__ import annotations

import json
import re
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from gits_sdk import ToolError

import transcribe
from core_api import CoreAPI
from jobs import Job, JobStore
from layout import Layout

VOICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
MAX_ASKS = {"clarify": 2, "build": 1}                      # ТЗ 20 §6 и §8.2
MAX_VOICE_CHARS = 400


@dataclass
class JobEnv:
    """Всё, что инструменты агента знают о задаче."""
    job: Job
    store: JobStore
    core: CoreAPI
    layout: Layout
    stage: str = ""
    # вопрос пользователю через задачу (waiting_user): (вопрос, вид, варианты) -> ответ; ставит конвейер
    ask: Callable[[str, str, list[str] | None], Awaitable[str | None]] | None = None
    # ход задачи: (прогресс 0..1 внутри стадии, текст)
    progress: Callable[[float, str], Awaitable[None]] | None = None
    asks: dict[str, int] = field(default_factory=dict)       # сколько вопросов задано на каждой стадии


class AgentTools:
    def __init__(self, env: JobEnv):
        self.env = env

    # -------------------------------------------------------------------------------------------- ask_user
    async def ask_user(self, question: str, kind: str = "choice", options: list[str] | None = None) -> str:
        env = self.env
        limit = MAX_ASKS.get(env.stage)
        if limit is None:
            raise ToolError("вопрос пользователю можно задавать только на стадиях clarify и build", code="denied")
        if env.asks.get(env.stage, 0) >= limit:
            raise ToolError(f"на стадии {env.stage} уже задано {limit} вопр.: дальше решай сама", code="denied")
        if kind not in ("choice", "text"):
            raise ToolError("kind: choice или text", code="bad_request")
        if kind == "choice" and not options:
            raise ToolError("для choice нужен непустой список options", code="bad_request")
        if env.ask is None:
            raise ToolError("вопросы пользователю сейчас недоступны", code="unavailable")
        env.asks[env.stage] = env.asks.get(env.stage, 0) + 1
        answer = await env.ask(question, kind, options)
        return "" if answer is None else str(answer)

    # -------------------------------------------------------------------------------------------- progress
    async def progress(self, progress: float, text: str) -> str:
        if self.env.progress is not None:
            await self.env.progress(max(0.0, min(1.0, float(progress))), str(text)[:300])
        return "ok"

    # -------------------------------------------------------------------------------------------- synthesize_voice
    async def synthesize_voice(self, id: str, text: str, emotion: str = "neutral") -> dict:
        env = self.env
        if not VOICE_ID_RE.match(id or ""):
            raise ToolError("id: буквы, цифры, _ и - (до 32 символов)", code="bad_request")
        text = (text or "").strip()
        if not text or len(text) > MAX_VOICE_CHARS:
            raise ToolError(f"text: от 1 до {MAX_VOICE_CHARS} символов", code="bad_request")
        out = await synthesize_to(env, id, text, emotion or "neutral")
        return out

    # -------------------------------------------------------------------------------------------- transcript_words
    async def transcript_words(self, from_ms: int, to_ms: int) -> list[dict]:
        path = self.env.job.transcript / "words.json"
        try:
            words = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return transcribe.words_between(words, int(from_ms), int(to_ms))


def voice_cache_path(job: Job) -> Path:
    return job.plan_dir / "voiceover_cache.json"


def load_voice_cache(job: Job) -> dict:
    try:
        return json.loads(voice_cache_path(job).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


async def synthesize_to(env: JobEnv, voice_id: str, text: str, emotion: str) -> dict:
    """Синтез одной реплики: core.tts -> work/public/vo/<id>.wav; запись в кэш (id -> текст, эмоция, длительность)."""
    import asyncio
    import hashlib
    value = await env.core.tts(text, emotion)
    target = env.job.work / "public" / "vo" / f"{voice_id}.wav"
    target.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(shutil.copyfile, value["path"], target)
    cache = load_voice_cache(env.job)
    cache[voice_id] = {"hash": hashlib.sha1(f"{emotion}|{text}".encode()).hexdigest(), "duration_ms": int(value["duration_ms"])}
    await asyncio.to_thread(voice_cache_path(env.job).write_text, json.dumps(cache, ensure_ascii=False, indent=2), "utf-8")
    return {"id": voice_id, "duration_ms": int(value["duration_ms"]), "path": f"public/vo/{voice_id}.wav"}

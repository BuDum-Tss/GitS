"""Заглушки для тестов конвейера: ядро, агент Claude, Remotion и контроль качества. Без сети, Claude и Chromium; ffmpeg
настоящий там, где стадии его вызывают (loudnorm, проверка файлов), файлы крошечные."""
from __future__ import annotations

import asyncio
import copy
import json
import shutil
import tempfile
import wave
from collections.abc import Awaitable, Callable
from pathlib import Path


import media
import qa as qa_mod
from claude_runner import StageResult
from layout import Layout


def write_silence_wav(path: Path, seconds: float, rate: int = 8000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(seconds * rate))


_SAMPLES: dict[tuple, Path] = {}


async def sample_video(w: int, h: int, dur: float) -> Path:
    """Крошечный ролик со звуком, один раз на размер и длительность за весь прогон тестов (ffmpeg на этой машине стартует долго)."""
    key = (w, h, round(dur, 2))
    if key not in _SAMPLES or not _SAMPLES[key].is_file():
        path = Path(tempfile.gettempdir()) / f"montage_sample_{w}x{h}_{int(dur * 100)}.mp4"
        if not path.is_file():
            res = await media.ffmpeg(["-f", "lavfi", "-i", f"testsrc2=s={w}x{h}:r=30:d={dur}", "-f", "lavfi", "-i",
                                      f"sine=frequency=300:duration={dur}", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                                      "-c:a", "aac", str(path)])
            assert res.ok, res.err
        _SAMPLES[key] = path
    return _SAMPLES[key]


async def fast_loudnorm(src, dst, **_kw):
    shutil.copyfile(src, dst)
    return {}


async def fast_extract_frame(video, t_s, out, *, width=None):
    out.write_bytes(b"png")


def fast_prepare_work(job_dir: Path, layout) -> Path:
    """Рабочая папка без копирования шаблона и junction (отдельный тест workspace проверяет настоящую)."""
    work = job_dir / "work"
    for sub in ("public/media", "public/vo", "public/transcript", "src/custom"):
        (work / sub).mkdir(parents=True, exist_ok=True)
    (work / "src" / "custom" / "index.ts").write_text("export const customComponents = {\n\tJournalLog,\n};\n", encoding="utf-8")
    return work


class StubCore:
    """Ядро для конвейера: записывает вызовы, синтез — тишина нужной длины (0,06 с на символ)."""

    def __init__(self, tmp: Path, settings: dict | None = None):
        self.tmp = tmp
        self.settings = dict(settings or {})
        self.sleeping = False
        self.updates: list[dict] = []
        self.events: list[dict] = []
        self.usage: list[tuple] = []
        self.tts_calls: list[str] = []
        self.persona_card = "Сухая учтивость, научный протокол."

    async def tts(self, text, emotion="neutral"):
        self.tts_calls.append(text)
        path = self.tmp / f"tts_{len(self.tts_calls)}.wav"
        seconds = max(0.8, len(text) * 0.06)
        write_silence_wav(path, seconds)
        return {"path": str(path), "duration_ms": int(seconds * 1000)}

    async def persona(self):
        return {"name": "ГЛаДОС", "card": self.persona_card}

    async def claude_usage(self, tokens, source, limit_text=None):
        self.usage.append((tokens, source, limit_text))

    async def job_update(self, job_id, **fields):
        self.updates.append({"job_id": job_id, **fields})
        return True

    async def event(self, kind, text, **fields):
        self.events.append({"kind": kind, "text": text, **fields})
        return True

    async def resend_jobs(self):
        return 0

    def states(self, job_id: str) -> list[tuple]:
        return [(u.get("state"), u.get("stage")) for u in self.updates if u["job_id"] == job_id]


STORY = {"logline": "Подопытный объясняет правило, ГЛаДОС фиксирует нарушение.",
         "hook": {"t_ms": 0, "what": "ГЛаДОС сообщает о нарушении", "why": "интрига"}, "promise": "Правило и вывод",
         "beats": [{"id": "b1", "role": "hook", "t_from_ms": 0, "t_to_ms": 3000, "purpose": "зацепить", "emotion": "интерес"},
                   {"id": "b2", "role": "rise", "t_from_ms": 3000, "t_to_ms": 9000, "purpose": "объяснить правило"},
                   {"id": "b3", "role": "button", "t_from_ms": 9000, "t_to_ms": 12000, "purpose": "вывод и призыв"}],
         "setups_payoffs": [{"setup": "b1", "payoff": "b3", "what": "нарушение и итог"}], "target_duration_ms": 12000}


def make_plan(*, platform="shorts", duration=12000, voiceover=True, captions=True, segments: list | None = None) -> dict:
    vertical = platform != "youtube"
    plan = {
        "version": 1, "format": {"width": 1080 if vertical else 1920, "height": 1920 if vertical else 1080, "fps": 30},
        "duration_ms": duration, "title": "Тест", "segments": segments or [],
        "captions": {"enabled": captions and vertical, "source": "voiceover", "style": "aperture_default", "overrides": [],
                     "words_file": "transcript/words.json"},
        "voiceover": [], "inserts": [
            {"id": "i1", "kind": "chamber_sign", "at_ms": 200, "dur_ms": 3000, "beat": "b1",
             "props": {"big": "07", "small": "07/19 · колония · день 1", "done": 7, "total": 19, "active": ["colonists", "build"],
                       "title": "Правило безопасности"}},
            {"id": "i2", "kind": "result_sign", "at_ms": 6800, "dur_ms": 2600, "beat": "b3",
             "props": {"big": "ПРОЙДЕНО", "small": "потерь 0 · дней 12", "done": 19, "total": 19, "active": ["colonists"],
                       "title": "Правило принято"}}],
        "blackout": [], "sfx": [{"at_ms": 200, "name": "chime_up"}, {"at_ms": 6800, "name": "chime_up"}], "music": None,
        "cta": {"platform": platform, "at_ms": 10000 if platform != "youtube" else duration - 4000, "text": "Следующее испытание скоро", "beat": "b3"}
        if duration > 10000 else None,
    }
    if voiceover:
        plan["voiceover"] = [{"id": "vo1", "at_ms": 300, "text": "Протокол нарушен.", "emotion": "neutral", "tts_path": None,
                              "duration_ms": None, "beat": "b1", "duck": False},
                             {"id": "vo2", "at_ms": 4000, "text": "Правило простое: не трогать то, что не твоё.", "emotion": "neutral",
                              "tts_path": None, "duration_ms": None, "beat": "b2", "duck": False}]
    return plan


Responder = Callable[..., Awaitable[StageResult] | StageResult]


class FakeRunner:
    """Агент без Claude: на каждую стадию пишет ожидаемые файлы. `override[stage]` — функция(env, prompt, call_no) вместо
    стандартного поведения; `calls` — журнал вызовов."""

    def __init__(self, plan_factory: Callable[[], dict] = make_plan, story: dict | None = None):
        self.plan_factory = plan_factory
        self.story = story or STORY
        self.calls: list[dict] = []
        self.override: dict[str, Callable] = {}
        self.review_replies: list[dict] = []
        self.tokens = 1000
        self.fail_stage: dict[str, int] = {}              # стадия -> сколько раз подряд упасть

    async def run(self, env, stage, prompt, *, model, max_turns, readonly=False, fresh=False):
        n = sum(1 for c in self.calls if c["stage"] == stage) + 1
        self.calls.append({"stage": stage, "model": model, "prompt": prompt, "readonly": readonly, "fresh": fresh, "n": n})
        if self.fail_stage.get(stage, 0) > 0:
            self.fail_stage[stage] -= 1
            from claude_runner import StageFailed
            raise StageFailed("заглушка: агент упал")
        text = "готово"
        if stage in self.override:
            r = self.override[stage](env, prompt, n)
            r = await r if asyncio.iscoroutine(r) else r
            if isinstance(r, StageResult):
                return r
            text = r or text
        elif stage == "clarify":
            (env.job.plan_dir / "clarify.json").write_text(json.dumps({"platform": "shorts", "duration_s": 12}), encoding="utf-8")
        elif stage == "story":
            (env.job.plan_dir / "story.json").write_text(json.dumps(self.story, ensure_ascii=False), encoding="utf-8")
            (env.job.plan_dir / "story.md").write_text("# История\n", encoding="utf-8")
        elif stage == "plan":
            plan = self.plan_factory()
            plan["voiceover"] = plan["voiceover"] if env.job.request.get("voiceover") in ("glados", "character") else []
            (env.job.plan_dir / "plan.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
            (env.job.plan_dir / "plan.md").write_text("# План\n", encoding="utf-8")
        elif stage == "review":
            reply = self.review_replies.pop(0) if self.review_replies else {
                "criteria": {"readability": {"score": 5, "issues": []}}, "overall": 5, "verdict": "pass"}
            text = json.dumps(reply, ensure_ascii=False)
        return StageResult(text=text, tokens=self.tokens, session_id="sess-1", turns=3)


class FakeRenderer:
    """Remotion без Chromium: настоящий крошечный ролик нужной длительности (ffmpeg), если не сказано иначе."""

    def __init__(self):
        self.tsc_errors: list[list[str]] = []
        self.plan_errors: list[list[str]] = []
        self.renders: list[dict] = []
        self.stills: list[int] = []
        self.render_error: Exception | None = None
        self.block: asyncio.Event | None = None

    async def tsc(self, work):
        return self.tsc_errors.pop(0) if self.tsc_errors else []

    async def validate_plan(self, work, plan):
        return self.plan_errors.pop(0) if self.plan_errors else []

    async def render(self, work, props, out, *, scale, crf, on_progress=None):
        if self.render_error:
            raise self.render_error
        if self.block is not None:
            await self.block.wait()
        plan = json.loads(Path(props).read_text(encoding="utf-8"))
        self.renders.append({"scale": scale, "crf": crf, "out": Path(out).name})
        dur = plan["duration_ms"] / 1000
        w = round(plan["format"]["width"] * (scale or 1) / 10)
        h = round(plan["format"]["height"] * (scale or 1) / 10)
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(await sample_video(w - w % 2, h - h % 2, dur), out)
        if on_progress:
            on_progress(1.0)

    async def still(self, work, props, frame, out):
        self.stills.append(frame)
        out.write_bytes(b"png")


class StubQA:
    """Контроль качества со сценарием: список ответов `technical` по порядку вызовов (по умолчанию чисто)."""

    def __init__(self, script: list[list[qa_mod.Finding]] | None = None):
        self.script = script or []
        self.calls: list[dict] = []

    async def technical(self, job, plan, words, story, video, *, draft, scale, platform):
        self.calls.append({"draft": draft, "video": Path(video).name})
        return (self.script.pop(0) if self.script else []), {"video": Path(video).name, "draft": draft}


def layout_for(tmp: Path) -> Layout:
    from layout import ROOT
    return Layout(root=ROOT, jobs=tmp / "jobs")


def copy_plan(plan: dict) -> dict:
    return copy.deepcopy(plan)

"""Конструктор монтажного листа по сценам.

Агент описывает ролик сценами: вид, что на картинке, фразы, материалы. Времена в миллисекундах он не считает: после озвучки
фраз длины известны, и `build_plan` раскладывает сцены встык, ставит реплики внутрь своей сцены, растягивает сцену на всю её
речь и собирает `plan.json` по правилам `plan_schema`. Описание сцен (`plan/scenes.json`) остаётся источником правды: сайт
показывает из него «Описание» и «Материалы».
"""
from __future__ import annotations

from plan_schema import CAPTION_PLATFORMS, MAX_VOICEOVER, NO_CTA_UNDER_MS, YOUTUBE_CTA_TAIL_MS, PlanContext

SCENES_FILE = "scenes.json"
MAX_SCENES = 30
MAX_PHRASES = 8
MAX_DESCRIPTION = 240
MAX_ASSETS = 12
LEAD_MS = 400                 # пауза от начала сцены до первой реплики
GAP_MS = 300                  # пауза между репликами (регламент §5)
TAIL_MS = 500                 # пауза после последней реплики до конца сцены
INTRO_FULL_MS = 7100
# сколько держится сцена без речи и пределы для табличек: (по умолчанию, минимум, максимум)
HOLD = {"intro": (INTRO_FULL_MS, 3400, INTRO_FULL_MS), "chamber_sign": (3000, 2500, 3500), "result_sign": (3000, 2500, 3500),
        "blueprint": (5000, 3000, 10 ** 9), "glados_room": (4000, 1000, 10 ** 9), "custom": (4000, 1000, 10 ** 9)}
FORMATS = {"horizontal": (1920, 1080), "vertical": (1080, 1920), "square": (1080, 1080)}
SCENE_KEYS = {"kind", "description", "props", "say", "emotion", "hold_ms", "assets", "mood", "beat"}


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def parse_scenes(spec, ctx: PlanContext, beat_ids: set[str] | None = None) -> tuple[list[dict], list[str]]:
    """Сцены из того, что прислал агент: (чистый список, ошибки)."""
    errors: list[str] = []
    if not isinstance(spec, list) or not spec:
        return [], ["scenes: непустой список сцен"]
    if len(spec) > MAX_SCENES:
        return [], [f"scenes: не больше {MAX_SCENES} сцен"]
    out: list[dict] = []
    for n, raw in enumerate(spec, 1):
        where = f"scenes[{n}]"
        if not isinstance(raw, dict):
            errors.append(f"{where}: должна быть сцена-объект")
            continue
        extra = set(raw) - SCENE_KEYS
        if extra:
            errors.append(f"{where}: лишние поля {sorted(extra)}; допустимы {sorted(SCENE_KEYS)}")
        kind = raw.get("kind")
        if kind not in HOLD or kind not in ctx.insert_kinds:
            errors.append(f"{where}.kind: одно из {', '.join(k for k in HOLD if k in ctx.insert_kinds)} (получено «{kind}»)")
            continue
        description = _text(raw.get("description"))
        if not description or len(description) > MAX_DESCRIPTION:
            errors.append(f"{where}.description: что на картинке, 1–{MAX_DESCRIPTION} символов")
        say = raw.get("say") or []
        if not isinstance(say, list) or len(say) > MAX_PHRASES or not all(_text(s) and len(_text(s)) <= MAX_VOICEOVER for s in say):
            errors.append(f"{where}.say: список фраз (до {MAX_PHRASES}), каждая 1–{MAX_VOICEOVER} символов, одна фраза на предложение")
            say = []
        if kind == "intro" and say:
            errors.append(f"{where}: заставка идёт со своим звуком, фраз в ней нет: перенеси их в следующую сцену")
        if n > 1 and kind == "intro":
            errors.append(f"{where}: заставка только первой сценой")
        hold = raw.get("hold_ms")
        if hold is not None and not (isinstance(hold, int) and not isinstance(hold, bool) and 500 <= hold <= 120_000):
            errors.append(f"{where}.hold_ms: целое число мс от 500 до 120000 (сколько держать сцену без речи)")
            hold = None
        assets = raw.get("assets") or []
        if not isinstance(assets, list) or len(assets) > MAX_ASSETS or not all(_text(a) for a in assets):
            errors.append(f"{where}.assets: список строк (до {MAX_ASSETS}): какие материалы ты берёшь")
            assets = []
        props = raw.get("props")
        if props is not None and not isinstance(props, dict):
            errors.append(f"{where}.props: объект параметров вставки")
            props = None
        mood = raw.get("mood")
        if mood is not None and mood not in ctx.moods:
            errors.append(f"{where}.mood: одно из {', '.join(ctx.moods)}")
            mood = None
        beat = raw.get("beat")
        if beat_ids is not None and beat not in beat_ids:
            errors.append(f"{where}.beat: у ролика есть story.json, укажи бит из: {', '.join(sorted(beat_ids))}")
        out.append({"kind": kind, "description": description, "props": props or {}, "say": [_text(s) for s in say],
                    "emotion": _text(raw.get("emotion")) or "neutral", "hold_ms": hold, "assets": [_text(a) for a in assets],
                    "mood": mood, "beat": beat if isinstance(beat, str) and beat else None})
    return out, errors


def phrase_id(scene_n: int, phrase_n: int) -> str:
    return f"s{scene_n}p{phrase_n}"


def build_plan(scenes: list[dict], durations: dict[str, int], *, title: str, fmt: str, fps: int, platform: str,
               cta_text: str | None, ctx: PlanContext) -> tuple[dict, list[dict]]:
    """Монтажный лист из сцен и длин фраз (`durations`: id фразы -> мс). Возвращает (plan, строки раскладки)."""
    width, height = FORMATS.get(fmt, FORMATS["horizontal"])
    inserts: list[dict] = []
    voiceover: list[dict] = []
    layout: list[dict] = []
    cursor = 0
    for n, scene in enumerate(scenes, 1):
        kind = scene["kind"]
        default, low, high = HOLD[kind]
        start = cursor
        phrases = scene["say"]
        speech_end = start
        if phrases:
            at = start + LEAD_MS
            for k, text in enumerate(phrases, 1):
                pid = phrase_id(n, k)
                dur = int(durations[pid])
                voiceover.append({"id": pid, "at_ms": at, "text": text, "emotion": scene["emotion"], "tts_path": None,
                                  "duration_ms": dur, "beat": scene["beat"] or f"b{n}", "duck": False})
                scene.setdefault("_spans", []).append((at, at + dur))
                at += dur + GAP_MS
            speech_end = at - GAP_MS
            dur_ms = max(scene["hold_ms"] or 0, speech_end - start + TAIL_MS, low)
        else:
            dur_ms = scene["hold_ms"] or default
        dur_ms = max(low, min(high, dur_ms)) if not phrases else dur_ms
        props = dict(scene["props"])
        if kind == "intro":
            props.setdefault("subtitle", title[:60])
        elif kind == "glados_room":
            props.setdefault("moods", [{"from_ms": start, "to_ms": start + dur_ms, "mood": scene["mood"] or ctx.moods[0]}])
            props.setdefault("speech", [{"from_ms": a, "to_ms": b} for a, b in scene.get("_spans", [])])
        inserts.append({"id": f"s{n}", "kind": kind, "at_ms": start, "dur_ms": dur_ms, "beat": scene["beat"] or f"b{n}", "props": props})
        layout.append({"id": f"s{n}", "kind": kind, "at_ms": start, "dur_ms": dur_ms, "phrases": len(phrases)})
        cursor = start + dur_ms
    total = cursor
    vertical = height > width
    plan: dict = {
        "version": 1, "format": {"width": width, "height": height, "fps": fps}, "duration_ms": total, "title": title, "segments": [],
        "captions": {"enabled": bool(vertical and platform in CAPTION_PLATFORMS and voiceover), "source": "voiceover",
                     "style": "aperture_default", "overrides": [], "words_file": "transcript/words.json"},
        "voiceover": voiceover, "inserts": inserts, "blackout": [], "sfx": [], "music": None,
    }
    if cta_text and platform != "none" and total > NO_CTA_UNDER_MS:
        plan["cta"] = {"platform": platform, "at_ms": max(0, total - YOUTUBE_CTA_TAIL_MS + 500), "text": cta_text,
                       "beat": scenes[-1]["beat"] or f"b{len(scenes)}"}
    for scene in scenes:
        scene.pop("_spans", None)
    return plan, layout

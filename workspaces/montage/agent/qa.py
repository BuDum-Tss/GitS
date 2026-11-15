"""Контроль качества (ТЗ 20, раздел 12a, MON-QA-1, MON-QA-4; ТЗ 22, CRAFT-4).

Технические проверки черновика и финала — только ffmpeg/ffprobe и код, без модели (`run_technical`); их вызывает команда
окружения `check` (и `final` для готового файла). Смысловую оценку кадров по рубрике навыка `montage-regulation/qa-rubric.md` делает
сама ГЛаДОС. Итог пишется в отчёт `qa/report.json` и `qa/report.md`.

Что и как проверяется (честно о границах):
  - формат и (в финале) громкость: ffprobe/ffmpeg по отрендеренному файлу;
  - субтитры: страницы строятся тем же алгоритмом, что в шаблоне (порт `template/src/timeline.ts`), и сравниваются со
    словами `words.json` (расхождение времени > 150 мс — ошибка, слово без страницы — ошибка). Появление субтитров
    в самом видео по пикселям не проверяется; текст на кадрах проверяет OCR, когда он установлен;
  - зоны вставок и субтитров: из `template/qa_zones.json` (экспорт токенов шаблона);
  - OCR: RapidOCR; если в окружении модуля его нет, проверка пропускается с предупреждением (ничего не скачивается).
"""
from __future__ import annotations

import difflib
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

import media
from layout import Layout

LUFS_TARGET, LUFS_TOL, PEAK_MAX = -14.0, 1.0, -1.0           # регламент §5
DURATION_TOL_S = 0.5
CAPTION_TOL_MS = 150
SPEECH_OVERLAP_MS = 300
OCR_MIN_SIMILARITY = 0.85
OCR_MAX_FRAMES = 14
RHYTHM_S = {"shorts": (1.2, 2.5), "reels": (1.2, 2.5), "telegram": (2.0, 3.0), "youtube": (3.0, 6.0)}   # ТЗ 22 §3.3
JUMP_CUT_MAX_MS = 1000


@dataclass
class Finding:
    check: str
    severity: str                 # error | warning
    message: str
    t_ms: int | None = None

    def line(self) -> str:
        at = f" на {self.t_ms / 1000:.1f} с" if self.t_ms is not None else ""
        return f"[{self.check}]{at}: {self.message}"


def errors_of(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if f.severity == "error"]


# ---------------------------------------------------------------------------------------------------- геометрия

def load_zones(layout: Layout) -> dict:
    return json.loads((layout.template / "qa_zones.json").read_text(encoding="utf-8"))


def orientation_of(width: int, height: int) -> str:
    return "vertical" if height > width else "horizontal" if width > height else "square"


def _rects_overlap(a: dict, b: dict, min_share: float = 0.03) -> bool:
    w = min(a["x"] + a["w"], b["x"] + b["w"]) - max(a["x"], b["x"])
    h = min(a["y"] + a["h"], b["y"] + b["h"]) - max(a["y"], b["y"])
    if w <= 0 or h <= 0:
        return False
    return w * h / max(1e-9, min(a["w"] * a["h"], b["w"] * b["h"])) > min_share


def insert_zone_kind(ins: dict) -> str | None:
    """Порт `insertZoneKind` из timeline.ts."""
    kind = ins.get("kind")
    if kind in ("chamber_sign", "result_sign", "glados_room"):
        return kind
    if kind == "custom":
        comp = (ins.get("props") or {}).get("component")
        return "journal" if comp == "JournalLog" else "specimen" if comp == "FontSpecimen" else None
    return None


def avoidance_ratio(zones: dict, o: str, base: float, zone: dict) -> float | None:
    """Порт `avoidanceRatio`: куда сместить субтитры, чтобы не перекрыть зону; None, если места нет."""
    half = zones["captions"]["height_ratio"][o] / 2
    gap = 0.015
    top, bottom = zone["y"], zone["y"] + zone["h"]
    if base + half <= top or base - half >= bottom:
        return base
    safe = zones["safe_zones"][o]
    below = bottom + gap + half
    if below + half <= 1 - safe["bottom"]:
        return below
    above = top - gap - half
    if above - half >= safe["top"]:
        return above
    return None


def caption_y_ratio(plan: dict, zones: dict, t_ms: float) -> tuple[float, bool]:
    """Порт `captionYRatio`; второе значение False, если субтитрам негде разместиться рядом со вставкой."""
    w, h = plan["format"]["width"], plan["format"]["height"]
    o = orientation_of(w, h)
    base = ((plan.get("captions") or {}).get("position") or {}).get("y_ratio", zones["captions"]["default_y_ratio"][o])
    ramp = 250
    y, best, ok = base, 0.0, True
    for ins in plan.get("inserts") or []:
        kind = insert_zone_kind(ins)
        spec = zones["components"].get(kind) if kind else None
        if not spec or not spec.get("avoid_captions"):
            continue
        lo, hi = ins["at_ms"], ins["at_ms"] + ins["dur_ms"]
        if t_ms < lo - ramp or t_ms > hi + ramp:
            continue
        target = avoidance_ratio(zones, o, base, spec[o])
        if target is None:
            if lo <= t_ms <= hi:
                ok = False
            continue
        if target == base:
            continue
        p = min(1.0, max(0.0, (t_ms - (lo - ramp)) / ramp), min(1.0, max(0.0, (hi + ramp - t_ms) / ramp)))
        if p > best:
            best = p
            y = base + (target - base) * (p * p * (3 - 2 * p))
    return y, ok


# ---------------------------------------------------------------------------------------------------- субтитры (порт timeline.ts)

def _strip_commas(s: str) -> str:
    return re.sub(r"[,，、]", "", s).strip()


def human_words(plan: dict, words: list[dict]) -> list[dict]:
    overrides = {o["word_index"]: o for o in (plan.get("captions") or {}).get("overrides") or []}
    out = []
    for w in words:
        ov = overrides.get(w["i"])
        text = _strip_commas(ov["text"] if ov and "text" in ov else w["text"])
        if not text:
            continue
        seg = next((s for s in plan["segments"] if (w.get("source") in (None, s["source"]))
                    and s["in_ms"] <= w["start_ms"] < s["out_ms"]), None)
        if seg is None or seg.get("volume", 1) == 0:
            continue
        start = seg["at_ms"] + (w["start_ms"] - seg["in_ms"])
        end = seg["at_ms"] + (min(w["end_ms"], seg["out_ms"]) - seg["in_ms"])
        out.append({"text": text, "start": start, "end": max(end, start + 80), "voice": False, "i": w["i"]})
    return out


def voiceover_words(vo: list[dict]) -> list[dict]:
    out = []
    for v in vo:
        parts = [p for p in (_strip_commas(x) for x in v["text"].split()) if p]
        if not parts:
            continue
        total = sum(len(p) + 1 for p in parts)
        duration = v.get("duration_ms") or max(1000, total * 62)
        cursor = float(v["at_ms"])
        for p in parts:
            d = duration * (len(p) + 1) / total
            out.append({"text": p, "start": round(cursor), "end": round(cursor + d), "voice": True, "i": None})
            cursor += d
    return out


def _tiktok(captions: list[dict], combine_ms: int) -> list[dict]:
    pages: list[dict] = []
    cur_text, cur_tokens, cur_from, cur_to = "", [], 0, 0

    def add():
        pages.append({"start": cur_from, "tokens": list(cur_tokens)})

    for index, item in enumerate(captions):
        text = item["text"]
        exceeds = cur_to - cur_from > combine_ms
        if text.startswith(" ") and exceeds:
            if cur_text != "":
                add()
            cur_text = text.lstrip()
            cur_tokens = [{"from": item["start"], "to": item["end"]}] if cur_text else []
            cur_from, cur_to = item["start"], item["end"]
        else:
            if cur_text == "":
                cur_from = item["start"]
            cur_text = (cur_text + text).lstrip()
            if text.strip():
                cur_tokens.append({"from": item["start"], "to": item["end"]})
            cur_to = item["end"]
        if item["break_after"] and cur_text != "":
            add()
            cur_text, cur_tokens = "", []
        if index == len(captions) - 1 and cur_text != "":
            add()
    return pages


def caption_pages(plan: dict, words: list[dict], zones: dict) -> list[dict]:
    """Страницы субтитров на шкале ролика: {start, end, voice, words:[слова]}. Порт `buildPages` (createTikTokStyleCaptions,
    550 мс, до 28 символов, разрыв после конца предложения)."""
    cap = plan.get("captions") or {}
    if not cap.get("enabled"):
        return []
    source = cap.get("source", "transcript")
    allw: list[dict] = []
    if source != "voiceover":
        allw += human_words(plan, words)
    if source != "transcript":
        allw += voiceover_words(plan.get("voiceover") or [])
    allw.sort(key=lambda w: w["start"])
    pages: list[dict] = []
    max_chars = zones["captions"]["max_chars"]
    for voice in (False, True):
        stream = [w for w in allw if w["voice"] == voice]
        if not stream:
            continue
        line = 0
        breaks = []
        for w in stream:
            add = len(w["text"]) + (1 if line > 0 else 0)
            brk = line > 0 and line + add > max_chars
            line = len(w["text"]) if brk else line + add
            breaks.append(brk)
        caps = []
        for k, w in enumerate(stream):
            caps.append({"text": ("" if k == 0 else " ") + w["text"], "start": w["start"], "end": w["end"],
                         "break_after": (k + 1 < len(stream) and breaks[k + 1]) or bool(re.search(r"[.?!…]$", w["text"]))})
        idx = 0
        for page in _tiktok(caps, 550):
            ws = stream[idx: idx + len(page["tokens"])]
            idx += len(page["tokens"])
            last = page["tokens"][-1]["to"] if page["tokens"] else page["start"]
            pages.append({"start": page["start"], "end": max(last, page["start"] + 200), "voice": voice, "words": ws})
    pages.sort(key=lambda p: p["start"])
    return pages


# ---------------------------------------------------------------------------------------------------- интервалы

def overlap(a: tuple[float, float], b: tuple[float, float]) -> float:
    return max(0.0, min(a[1], b[1]) - max(a[0], b[0]))


def _voice_span(v: dict) -> tuple[int, int]:
    return v["at_ms"], v["at_ms"] + (v.get("duration_ms") or max(1000, len(v["text"]) * 62))


# ---------------------------------------------------------------------------------------------------- проверки ремесла по плану (CRAFT-4)

def craft_plan_checks(plan: dict, story: dict | None, words: list[dict], platform: str) -> list[Finding]:
    """Проверки ремесла, которым не нужен рендер: первый бит, ритм, перебивки, закадр поверх речи, концовка."""
    out: list[Finding] = []
    w, h = plan["format"]["width"], plan["format"]["height"]
    beats = (story or {}).get("beats") or []
    if beats:
        first = beats[0]
        if h > w and first["t_to_ms"] - first["t_from_ms"] > 3000:
            out.append(Finding("craft.first_beat", "error", f"первый бит длится {(first['t_to_ms'] - first['t_from_ms']) / 1000:.1f} с: в "
                               "вертикальном ролике он не длиннее 3 секунд", first["t_from_ms"]))
        if beats[-1].get("role") not in ("button", "cta"):
            out.append(Finding("craft.ending", "error", f"последний бит «{beats[-1].get('role')}»: ролик заканчивается «кнопкой» "
                               "(button) или призывом (cta), а не обрывом", beats[-1]["t_from_ms"]))
    segs = sorted(plan["segments"], key=lambda s: s["at_ms"])
    if segs and platform in RHYTHM_S:
        avg = sum(s["out_ms"] - s["in_ms"] for s in segs) / len(segs) / 1000
        lo, hi = RHYTHM_S[platform]
        if not lo <= avg <= hi:
            out.append(Finding("craft.rhythm", "warning", f"средняя длина плана {avg:.1f} с, для площадки {platform} ориентир {lo}–{hi} с"))
    for a, b in zip(segs, segs[1:]):
        if a["source"] != b["source"] or a.get("intent") == "jump_cut" or b.get("intent") == "jump_cut":
            continue
        if abs(a["at_ms"] + (a["out_ms"] - a["in_ms"]) - b["at_ms"]) > 100:
            continue                                        # между ними пауза или вставка, это не склейка встык
        gap = b["in_ms"] - a["out_ms"]
        if gap != 0 and abs(gap) < JUMP_CUT_MAX_MS:
            out.append(Finding("craft.jump_cut", "error", f"склейка встык {a['id']}→{b['id']} одного исходника со сдвигом {gap} мс: это "
                               "случайная перебивка; удлини или объедини планы, либо пометь intent «jump_cut»", b["at_ms"]))
    live = [(x["start"], x["end"]) for x in human_words(plan, words)]
    for v in plan.get("voiceover") or []:
        if v.get("duck"):
            continue
        span = _voice_span(v)
        shared = sum(overlap(span, s) for s in live)
        if shared > SPEECH_OVERLAP_MS:
            out.append(Finding("craft.voice_overlap", "error", f"закадр {v['id']} перекрывает живую речь на {shared / 1000:.1f} с "
                               "(допустимо 0,3 с; для намеренного наложения поставь duck: true)", v["at_ms"]))
    return out


# ---------------------------------------------------------------------------------------------------- технические проверки (MON-QA-1)

OcrFn = Callable[[Path], Awaitable[str]]


def ocr_available() -> bool:
    try:
        import rapidocr_onnxruntime  # noqa: F401
        return True
    except ImportError:
        try:
            import rapidocr  # noqa: F401
            return True
        except ImportError:
            return False


def _norm_text(s: str) -> str:
    return re.sub(r"[\W_]+", "", s.casefold())


def text_recall(expected: str, seen: str) -> float:
    """Какая доля ожидаемого текста найдена в распознанном (по совпадающим блокам, без регистра и знаков)."""
    e, s = _norm_text(expected), _norm_text(seen)
    if not e:
        return 1.0
    blocks = difflib.SequenceMatcher(None, e, s, autojunk=False).get_matching_blocks()
    return sum(b.size for b in blocks) / len(e)


def expected_texts(plan: dict, words: list[dict], zones: dict, max_pages: int = 6) -> list[tuple[int, str, str]]:
    """Кадры для OCR: (время мс, ожидаемый текст, откуда) — середина каждой текстовой вставки и каждая 10-я страница субтитров."""
    out: list[tuple[int, str, str]] = []
    for ins in plan.get("inserts") or []:
        p = ins.get("props") or {}
        text = (p.get("title") if ins["kind"] in ("chamber_sign", "result_sign", "blueprint") else p.get("subtitle") if ins["kind"] == "intro" else None)
        if text:
            out.append((ins["at_ms"] + ins["dur_ms"] // 2, str(text), f"вставка {ins['id']}"))
    pages = caption_pages(plan, words, zones)
    for k in range(9, len(pages), 10):
        pg = pages[k]
        out.append(((pg["start"] + pg["end"]) // 2, " ".join(w["text"] for w in pg["words"]), f"субтитры, страница {k + 1}"))
    if not any(o[2].startswith("субтитры") for o in out) and pages:
        pg = pages[len(pages) // 2]
        out.append(((pg["start"] + pg["end"]) // 2, " ".join(w["text"] for w in pg["words"]), "субтитры, средняя страница"))
    return sorted(out)[:OCR_MAX_FRAMES]


async def run_technical(plan: dict, words: list[dict], story: dict | None, video: Path, layout: Layout, *, draft: bool,
                        scale: float = 1.0, platform: str = "none", ocr: OcrFn | None = None,
                        stills: Callable[[int, Path], Awaitable[None]] | None = None, workdir: Path | None = None) -> tuple[list[Finding], dict]:
    """Все технические проверки одного рендера. Возвращает (находки, сведения о том, что проверено)."""
    zones = load_zones(layout)
    f: list[Finding] = []
    checked: dict = {"video": video.name, "draft": draft}
    D = plan["duration_ms"]

    # ---- формат
    info = await media.probe(video)
    exp_w, exp_h = round(plan["format"]["width"] * scale), round(plan["format"]["height"] * scale)
    checked["format"] = {"width": info.width, "height": info.height, "fps": round(info.fps, 2), "vcodec": info.vcodec,
                         "acodec": info.acodec, "duration_s": round(info.duration_s, 2)}
    if (info.width, info.height) != (exp_w, exp_h):
        f.append(Finding("format", "error", f"размер {info.width}×{info.height}, по плану {exp_w}×{exp_h}"))
    if abs(info.fps - plan["format"]["fps"]) > 0.5:
        f.append(Finding("format", "error", f"{info.fps:.1f} кадров в секунду, по плану {plan['format']['fps']}"))
    if info.vcodec != "h264":
        f.append(Finding("format", "error", f"видеокодек {info.vcodec}, нужен h264"))
    if info.has_audio and info.acodec != "aac":
        f.append(Finding("format", "error", f"звуковой кодек {info.acodec}, нужен aac"))
    if abs(info.duration_s - D / 1000) > DURATION_TOL_S:
        f.append(Finding("format", "error", f"длительность {info.duration_s:.2f} с, по плану {D / 1000:.2f} с (допуск 0,5 с)"))

    # ---- громкость: только в финале (там звук выравнивается); догадки по тишине и неподвижности кадра убраны —
    # заставка со звуком и намеренные паузы давали ложные отказы, смысл ролика агент проверяет сам по кадрам
    if info.has_audio and not draft:
        loud = await media.loudness(video)
        checked["loudness"] = loud
        if loud["integral"] is not None and abs(loud["integral"] - LUFS_TARGET) > LUFS_TOL:
            f.append(Finding("loudness", "error", f"интегральная громкость {loud['integral']:.1f} LUFS, нужно {LUFS_TARGET:.0f} ± {LUFS_TOL:.0f}"))
        if loud["true_peak"] is not None and loud["true_peak"] > PEAK_MAX:
            f.append(Finding("loudness", "error", f"пик {loud['true_peak']:.1f} dBTP выше {PEAK_MAX:.0f}"))

    # ---- субтитры: страницы против слов, зоны
    pages = caption_pages(plan, words, zones)
    checked["caption_pages"] = len(pages)
    cap = plan.get("captions") or {}
    if cap.get("enabled") and cap.get("source", "transcript") != "voiceover":
        mapped = {w["i"]: w for w in human_words(plan, words)}
        page_starts = [(p["start"], w) for p in pages if not p["voice"] for w in p["words"]]
        for start, w in page_starts:
            if w["i"] in mapped and abs(mapped[w["i"]]["start"] - w["start"]) > CAPTION_TOL_MS:
                f.append(Finding("captions", "error", f"слово «{w['text']}» на странице субтитров смещено относительно расшифровки", int(start)))
        shown = {w["i"] for _, w in page_starts}
        for i, w in mapped.items():
            if i not in shown:
                f.append(Finding("captions", "error", f"слово «{w['text']}» из расшифровки не получило субтитра", int(w["start"])))
        for w in mapped.values():
            if w["start"] > D:
                f.append(Finding("captions", "error", f"слово «{w['text']}» вне ролика ({w['start'] / 1000:.1f} с)", int(w["start"])))
    f += zone_findings(plan, pages, zones)

    # ---- текст на кадрах (OCR)
    if ocr is not None and stills is not None and workdir is not None:
        checked["ocr"] = {"available": True, "frames": 0}
        for t_ms, text, origin in expected_texts(plan, words, zones):
            png = workdir / f"ocr_{t_ms}.png"
            try:
                await stills(round(t_ms / 1000 * plan["format"]["fps"]), png)
                seen = await ocr(png)
            except Exception as e:                                # кадр или распознавание не получились: предупреждение, не ошибка
                f.append(Finding("ocr", "warning", f"{origin}: проверить текст на кадре не удалось ({type(e).__name__})", t_ms))
                continue
            checked["ocr"]["frames"] += 1
            share = text_recall(text, seen)
            if share < OCR_MIN_SIMILARITY:
                f.append(Finding("ocr", "error", f"{origin}: на кадре найдено {share:.0%} задуманного текста «{text[:40]}» "
                                 f"(видно: «{seen[:60]}»): глифов нет, текст обрезан или перекрыт", t_ms))
    else:
        checked["ocr"] = {"available": False}

    f += craft_plan_checks(plan, story, words, platform)
    return f, checked


def zone_findings(plan: dict, pages: list[dict], zones: dict) -> list[Finding]:
    """Вставки и субтитры не пересекаются по времени и зонам; текст вне небезопасных зон площадки (ТЗ 21 §8)."""
    out: list[Finding] = []
    w, h = plan["format"]["width"], plan["format"]["height"]
    o = orientation_of(w, h)
    safe = zones["safe_zones"][o]
    D = plan["duration_ms"]
    placed = []
    for ins in plan.get("inserts") or []:
        kind = insert_zone_kind(ins)
        if kind is None:
            continue
        r = zones["components"][kind][o]
        placed.append((ins, kind, r))
        if r["y"] < safe["top"] - 1e-6 or r["y"] + r["h"] > 1 - safe["bottom"] + 1e-6:
            out.append(Finding("safe_zone", "error", f"вставка {ins['id']} ({kind}) выходит в небезопасную зону площадки", ins["at_ms"]))
    cta = plan.get("cta")
    if cta:
        r = zones["components"]["cta"][o]
        placed.append(({"id": "cta", "at_ms": cta["at_ms"], "dur_ms": D - cta["at_ms"], "kind": "cta"}, "cta", r))
        if r["y"] < safe["top"] - 1e-6 or r["y"] + r["h"] > 1 - safe["bottom"] + 1e-6:
            out.append(Finding("safe_zone", "error", "призыв выходит в небезопасную зону площадки", cta["at_ms"]))
    for k, (a, ka, ra) in enumerate(placed):
        for b, kb, rb in placed[k + 1:]:
            if overlap((a["at_ms"], a["at_ms"] + a["dur_ms"]), (b["at_ms"], b["at_ms"] + b["dur_ms"])) > 100 and _rects_overlap(ra, rb):
                out.append(Finding("overlap", "error", f"вставки {a['id']} ({ka}) и {b['id']} ({kb}) пересекаются по времени и по зоне",
                                   max(a["at_ms"], b["at_ms"])))
    half = zones["captions"]["height_ratio"][o] / 2
    for p in pages:
        mid = (p["start"] + p["end"]) / 2
        y, ok = caption_y_ratio(plan, zones, mid)
        if not ok:
            out.append(Finding("overlap", "error", "субтитры негде разместить рядом со вставкой: нет места ни выше, ни ниже", int(mid)))
            continue
        rect = {"x": 0.04, "y": y - half, "w": 0.92, "h": 2 * half}
        if rect["y"] < safe["top"] - 0.01 or rect["y"] + rect["h"] > 1 - safe["bottom"] + 0.01:
            out.append(Finding("safe_zone", "error", "субтитры в небезопасной зоне площадки (y_ratio слишком высоко или низко)", int(mid)))
        for ins, kind, r in placed:
            active = ins["at_ms"] <= mid <= ins["at_ms"] + ins["dur_ms"]
            if active and (kind == "cta" or zones["components"][kind].get("avoid_captions")) and _rects_overlap(rect, r):
                out.append(Finding("overlap", "error", f"субтитры перекрывают {'призыв' if kind == 'cta' else 'вставку ' + str(ins['id'])}", int(mid)))
    return out


# ---------------------------------------------------------------------------------------------------- отчёт (MON-QA-4)

@dataclass
class QAOutcome:
    findings: list[Finding] = field(default_factory=list)
    checked: dict = field(default_factory=dict)

    @property
    def errors(self) -> list[Finding]:
        return errors_of(self.findings)

    @property
    def passed(self) -> bool:
        return not self.errors

    def summary(self) -> dict:
        return {"technical_errors": len(self.errors), "technical_warnings": len(self.findings) - len(self.errors),
                "passed": self.passed}


def write_report(qa_dir: Path, job_id: str, outcome: QAOutcome, *, draft: bool, stem: str = "report") -> tuple[Path, Path]:
    qa_dir.mkdir(parents=True, exist_ok=True)
    doc = {"job_id": job_id, "draft": draft, "summary": outcome.summary(), "checked": outcome.checked,
           "findings": [asdict(f) for f in outcome.findings]}
    jp, mp = qa_dir / f"{stem}.json", qa_dir / f"{stem}.md"
    jp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    s = outcome.summary()
    md = [f"# Отчёт контроля качества ({'черновик' if draft else 'финал'}), задача {job_id}", "",
          f"Итог: {'проверки пройдены' if s['passed'] else 'остались замечания'}. Ошибок {s['technical_errors']}, предупреждений "
          f"{s['technical_warnings']}.", ""]
    md += ["## Технические проверки"]
    errs = outcome.errors
    warns = [f for f in outcome.findings if f.severity != "error"]
    md += [*(f"- ОШИБКА {f.line()}" for f in errs), *(f"- предупреждение {f.line()}" for f in warns)] or ["- замечаний нет"]
    chk = outcome.checked
    if chk:
        md += ["", "Проверено: " + ", ".join(f"{k}" for k in chk if k not in ("video", "draft"))]
    mp.write_text("\n".join(md) + "\n", encoding="utf-8")
    return jp, mp


# ---------------------------------------------------------------------------------------------------- служба проверок

class QAService:
    """Технические проверки для команд окружения: собирает OCR и кадры Remotion, если OCR установлен."""

    def __init__(self, layout: Layout, renderer=None, ocr: OcrFn | None = None):
        self.layout = layout
        self.renderer = renderer
        self.ocr = ocr if ocr is not None else self._default_ocr()

    @staticmethod
    def _default_ocr() -> OcrFn | None:
        if not ocr_available():
            return None
        import asyncio

        def run(path: Path) -> str:
            try:
                from rapidocr_onnxruntime import RapidOCR
            except ImportError:
                from rapidocr import RapidOCR
            engine = RapidOCR()
            result = engine(str(path))
            rows = result[0] if isinstance(result, tuple) else getattr(result, "txts", None)
            if rows and isinstance(rows, (list, tuple)) and rows and isinstance(rows[0], (list, tuple)):
                return " ".join(str(r[1]) for r in rows)
            return " ".join(str(r) for r in rows or [])

        async def ocr(path: Path) -> str:
            return await asyncio.to_thread(run, path)
        return ocr

    async def technical(self, job, plan: dict, words: list[dict], story: dict | None, video: Path, *, draft: bool, scale: float,
                        platform: str) -> tuple[list[Finding], dict]:
        stills = None
        if self.ocr is not None and self.renderer is not None:
            async def stills(frame: int, out: Path) -> None:
                await self.renderer.still(job.work, job.plan_dir / "plan.json", frame, out)
        qa_dir = job.qa / "ocr"
        qa_dir.mkdir(parents=True, exist_ok=True)
        return await run_technical(plan, words, story, video, self.layout, draft=draft, scale=scale, platform=platform,
                                   ocr=self.ocr, stills=stills, workdir=qa_dir)

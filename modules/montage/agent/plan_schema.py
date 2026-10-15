"""Проверка `plan.json` и `story.json` (ТЗ 20, раздел 7, MON-13; ТЗ 22, CRAFT-2).

Проверка ручная, без новых зависимостей (pydantic в requirements.txt нет): исполнитель выбрал явные правила и понятные
сообщения для агента. Ошибки возвращаются агенту списком строк «путь: что не так» (до 3 попыток исправления). Форма плана
совпадает с zod-схемой шаблона `template/src/schema.ts` (источник правды о форме): тест сверяет эту проверку с
`template/plan.schema.json` на наборе верных и неверных планов, а конвейер дополнительно гоняет план через
`template/scripts/validate-plan.mjs` (та же схема, что в `calculateMetadata`).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

PLATFORMS = ("shorts", "reels", "telegram", "youtube", "none")
# запасной список видов вставок; настоящий берётся из template/plan.schema.json (единый источник: новые компоненты шаблона
# добавляются без правки Python), подробные правила props есть только у перечисленных ниже видов
INSERT_KINDS = ("chamber_card", "hazard_banner", "pixel_transition", "test_result", "lower_third", "custom")
CAPTION_PLATFORMS = ("shorts", "reels", "telegram", "none")      # субтитры вжигаются только в вертикальных коротких роликах
BEAT_ROLES = ("hook", "setup", "inciting", "rise", "turn", "climax", "payoff", "button", "cta")
MAX_ON_SCREEN = 60          # тексты на экране (ТЗ 20 MON-13), заголовки 40 (регламент §6), CTA 80
MAX_TITLE = 40
MAX_CTA = 80
MAX_VOICEOVER = 400
MAX_PIXEL_TRANSITIONS = 2   # регламент §9
SFX_MIN_GAP_MS = 1000       # регламент §5: не больше одного эффекта в секунду
PIXEL_MS = (400, 600)       # регламент §4.5

# площадка -> (ширина, высота, fps, предельная длительность, мс); None: без ограничения (ТЗ 21 §8)
PLATFORM_FORMAT = {
    "shorts": (1080, 1920, 30, 60_000), "reels": (1080, 1920, 30, 90_000), "telegram": (1080, 1920, 30, 120_000),
    "youtube": (1920, 1080, 30, 600_000), "none": None,
}
DURATION_SLACK_MS = 1500    # цель «≤ 60 с» не строгий предел: допуск на концовку
NO_CTA_UNDER_MS = 10_000    # ролик или фрагмент до 10 секунд — без CTA
YOUTUBE_CTA_TAIL_MS = 5500  # youtube: призыв в последних 5 секундах


@dataclass
class PlanContext:
    """Всё, что проверке нужно знать о задаче: папка (файлы `source`), площадка, биты истории, длительности исходников."""
    job_dir: Path | None = None
    platform: str = "none"
    beat_ids: set[str] | None = None                 # id битов story.json; None: истории нет, поле `beat` проверяется только как строка
    pictograms: set[str] = field(default_factory=set)
    sfx: set[str] = field(default_factory=set)
    source_ms: dict[str, int] = field(default_factory=dict)     # длительность исходников в мс по пути `sources/…` (если известна)
    insert_kinds: tuple[str, ...] = INSERT_KINDS               # допустимые kind вставок (из plan.schema.json шаблона)

    @classmethod
    def from_layout(cls, layout, **kw) -> PlanContext:
        pict = {p.stem for p in (layout.assets / "pictograms").glob("*.svg")}
        sfx = {p.stem for p in (layout.assets / "sfx").glob("*.wav")}
        return cls(pictograms=pict, sfx=sfx, insert_kinds=insert_kinds_from_schema(layout), **kw)


def insert_kinds_from_schema(layout) -> tuple[str, ...]:
    """Допустимые `kind` вставок из `template/plan.schema.json` (создаётся `npm run gen:schema` из zod-схемы шаблона)."""
    try:
        doc = json.loads((layout.template / "plan.schema.json").read_text(encoding="utf-8"))
        variants = doc["properties"]["inserts"]["items"]
        variants = variants.get("oneOf") or variants.get("anyOf") or []
        kinds = tuple(v["properties"]["kind"]["const"] for v in variants)
    except (OSError, ValueError, KeyError, TypeError):
        return INSERT_KINDS
    return kinds or INSERT_KINDS


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


class _Check:
    def __init__(self):
        self.problems: list[str] = []

    def add(self, where: str, msg: str) -> None:
        self.problems.append(f"{where}: {msg}")

    def obj(self, where: str, value, allowed: set[str], required: set[str] = frozenset()) -> bool:
        if not isinstance(value, dict):
            self.add(where, "должен быть объектом")
            return False
        for key in sorted(set(value) - allowed):
            self.add(f"{where}.{key}", "лишнее поле (схема плана закрытая)")
        for key in sorted(set(required) - set(value)):
            self.add(f"{where}.{key}", "обязательное поле отсутствует")
        return True

    def int_field(self, where: str, d: dict, key: str, lo: int | None = 0, hi: int | None = None, required: bool = True):
        if key not in d:
            return None
        v = d[key]
        if not _is_int(v):
            self.add(f"{where}.{key}", "должно быть целым числом (миллисекунды)" if key.endswith("_ms") else "должно быть целым числом")
            return None
        if lo is not None and v < lo:
            self.add(f"{where}.{key}", f"не меньше {lo}")
        if hi is not None and v > hi:
            self.add(f"{where}.{key}", f"не больше {hi}")
        return v

    def text(self, where: str, d: dict, key: str, limit: int, required: bool = True) -> str | None:
        if key not in d:
            return None
        v = d[key]
        if not isinstance(v, str) or not v.strip():
            self.add(f"{where}.{key}", "должна быть непустая строка")
            return None
        if len(v) > limit:
            self.add(f"{where}.{key}", f"{len(v)} символов, предел {limit}")
        return v


def _source_ok(c: _Check, where: str, source, ctx: PlanContext) -> None:
    if not isinstance(source, str) or not source.strip():
        c.add(where, "должна быть непустая строка")
        return
    if not source.startswith("sources/") or "\\" in source or ".." in source.split("/"):
        c.add(where, f"путь в виде «sources/имя_файла» внутри папки задачи (получено «{source}»)")
        return
    if ctx.job_dir is not None and not (ctx.job_dir / source).is_file():
        c.add(where, f"файла «{source}» в папке задачи нет")


def validate_plan(plan, ctx: PlanContext) -> list[str]:
    """Список ошибок плана (пустой: план верен). Правила MON-13, CRAFT-2 и регламента стиля, которые видны по плану."""
    c = _Check()
    top = {"version", "format", "duration_ms", "title", "segments", "captions", "voiceover", "inserts", "blackout", "sfx",
           "music", "cta"}
    if not c.obj("plan", plan, top, {"version", "format", "duration_ms", "title", "segments", "captions"}):
        return c.problems
    if plan.get("version") != 1:
        c.add("version", "должно быть 1")
    fmt = plan.get("format")
    if c.obj("format", fmt, {"width", "height", "fps"}, {"width", "height", "fps"}):
        w = c.int_field("format", fmt, "width", 16)
        h = c.int_field("format", fmt, "height", 16)
        c.int_field("format", fmt, "fps", 1, 60)
        want = PLATFORM_FORMAT.get(ctx.platform)
        if want and w is not None and h is not None and (w, h) != want[:2]:
            c.add("format", f"для площадки {ctx.platform} нужен кадр {want[0]}×{want[1]} (получено {w}×{h})")
        if want and _is_int(fmt.get("fps")) and fmt["fps"] != want[2]:
            c.add("format.fps", f"для площадки {ctx.platform} нужно {want[2]} кадров в секунду")
        if (w and w % 2) or (h and h % 2):
            c.add("format", "ширина и высота должны быть чётными (H.264)")
    duration = c.int_field("plan", plan, "duration_ms", 100)
    if duration is not None:
        want = PLATFORM_FORMAT.get(ctx.platform)
        if want and duration > want[3] + DURATION_SLACK_MS:
            c.add("duration_ms", f"для площадки {ctx.platform} ролик не длиннее {want[3] // 1000} с (получено {duration / 1000:.1f} с)")
    if not isinstance(plan.get("title"), str):
        c.add("title", "должна быть строка")
    D = duration if duration is not None else 10 ** 9
    beats = ctx.beat_ids
    used_beats: set[str] = set()

    def beat_ref(where: str, d: dict, required: bool) -> None:
        b = d.get("beat")
        if b is None:
            if required:
                c.add(f"{where}.beat", "обязательное поле: id бита из story.json (CRAFT-2)")
            return
        if not isinstance(b, str) or not b:
            c.add(f"{where}.beat", "должна быть непустая строка (id бита)")
            return
        used_beats.add(b)
        if beats is not None and b not in beats:
            c.add(f"{where}.beat", f"бита «{b}» нет в story.json (есть: {', '.join(sorted(beats))})")

    # ---- segments
    ids: set[str] = set()
    spans: list[tuple[int, int, str]] = []
    segs = plan.get("segments")
    if not isinstance(segs, list):
        c.add("segments", "должен быть массивом")
        segs = []
    for k, s in enumerate(segs):
        where = f"segments[{k}]"
        if not c.obj(where, s, {"id", "source", "in_ms", "out_ms", "at_ms", "volume", "crop", "zoom", "beat", "intent"},
                     {"id", "source", "in_ms", "out_ms", "at_ms", "beat"}):
            continue
        sid = s.get("id")
        if not isinstance(sid, str) or not sid:
            c.add(f"{where}.id", "должна быть непустая строка")
        elif sid in ids:
            c.add(f"{where}.id", f"id «{sid}» повторяется")
        else:
            ids.add(sid)
        _source_ok(c, f"{where}.source", s.get("source"), ctx)
        a = c.int_field(where, s, "in_ms", 0)
        b = c.int_field(where, s, "out_ms", 0)
        at = c.int_field(where, s, "at_ms", 0)
        if a is not None and b is not None:
            if b <= a:
                c.add(f"{where}.out_ms", f"должно быть больше in_ms ({a})")
            src_len = ctx.source_ms.get(s.get("source"))
            if src_len and b > src_len + 100:
                c.add(f"{where}.out_ms", f"исходник длится {src_len} мс, а out_ms = {b}")
            if at is not None:
                if at + (b - a) > D:
                    c.add(where, f"сегмент заканчивается на {at + b - a} мс, ролик длится {D} мс")
                if b > a:
                    spans.append((at, at + b - a, sid or str(k)))
        if "volume" in s and not (_is_num(s["volume"]) and 0 <= s["volume"] <= 2):
            c.add(f"{where}.volume", "число от 0 до 2")
        if "zoom" in s and not (_is_num(s["zoom"]) and 0 <= s["zoom"] <= 0.04):
            c.add(f"{where}.zoom", "число от 0 до 0.04 (наезд не больше 4 %, регламент §9)")
        if "crop" in s and not (isinstance(s["crop"], dict) and set(s["crop"]) == {"mode"} and s["crop"]["mode"] in ("center", "face")):
            c.add(f"{where}.crop", "{\"mode\": \"center\"} или {\"mode\": \"face\"}")
        if "intent" in s and s["intent"] != "jump_cut":
            c.add(f"{where}.intent", "единственное допустимое значение: jump_cut")
        beat_ref(where, s, True)
    spans.sort()
    for (a0, a1, aid), (b0, b1, bid) in zip(spans, spans[1:]):
        if b0 < a1:
            c.add("segments", f"сегменты {aid} и {bid} пересекаются по at_ms ({a0}–{a1} и {b0}–{b1})")

    # ---- captions
    cap = plan.get("captions")
    if c.obj("captions", cap, {"enabled", "source", "style", "position", "overrides", "words_file"}, {"enabled"}):
        if not isinstance(cap.get("enabled"), bool):
            c.add("captions.enabled", "true или false")
        if "source" in cap and cap["source"] not in ("transcript", "voiceover", "both"):
            c.add("captions.source", "transcript, voiceover или both")
        if "style" in cap and cap["style"] != "aperture_default":
            c.add("captions.style", "единственный стиль: aperture_default")
        if "position" in cap and not (isinstance(cap["position"], dict) and set(cap["position"]) == {"y_ratio"}
                                      and _is_num(cap["position"]["y_ratio"]) and 0.05 <= cap["position"]["y_ratio"] <= 0.95):
            c.add("captions.position", "{\"y_ratio\": число от 0.05 до 0.95}")
        if "words_file" in cap and not isinstance(cap["words_file"], str):
            c.add("captions.words_file", "должна быть строка")
        for k, o in enumerate(cap.get("overrides") or []):
            # MON-28: исправлять можно только текст слова; времена берутся из расшифровки и менять их нельзя
            where = f"captions.overrides[{k}]"
            if isinstance(o, dict) and ({"start_ms", "end_ms", "at_ms", "start", "end"} & set(o)):
                c.add(where, "времена слов менять нельзя (MON-28): допустимы только text и highlight")
                continue
            if c.obj(where, o, {"word_index", "text", "highlight"}, {"word_index"}):
                c.int_field(where, o, "word_index", 0)
                if "text" in o and not isinstance(o["text"], str):
                    c.add(f"{where}.text", "должна быть строка")
                if "highlight" in o and not isinstance(o["highlight"], bool):
                    c.add(f"{where}.highlight", "true или false")
        if cap.get("enabled") is True:
            fmt_ = plan.get("format") if isinstance(plan.get("format"), dict) else {}
            vertical = _is_int(fmt_.get("width")) and _is_int(fmt_.get("height")) and fmt_["height"] > fmt_["width"]
            if ctx.platform not in CAPTION_PLATFORMS or not vertical:
                c.add("captions.enabled", "субтитры вжигаются только в вертикальные короткие ролики (shorts, reels, telegram); "
                      "в горизонтальных (youtube) captions.enabled = false, конвейер выдаст файл .srt рядом с финалом")
        if cap.get("enabled") and cap.get("source", "transcript") != "voiceover" and not segs:
            c.add("captions.source", "субтитры по расшифровке без сегментов исходника невозможны: используй source «voiceover»")

    # ---- voiceover
    vo = plan.get("voiceover", [])
    vids: set[str] = set()
    if not isinstance(vo, list):
        c.add("voiceover", "должен быть массивом")
        vo = []
    for k, v in enumerate(vo):
        where = f"voiceover[{k}]"
        if not c.obj(where, v, {"id", "at_ms", "text", "emotion", "tts_path", "duration_ms", "beat", "duck"}, {"id", "at_ms", "text"}):
            continue
        vid = v.get("id")
        if not isinstance(vid, str) or not vid:
            c.add(f"{where}.id", "должна быть непустая строка")
        elif not all(ch.isalnum() or ch in "_-" for ch in vid):
            c.add(f"{where}.id", "только буквы, цифры, _ и - (id становится именем файла vo/<id>.wav)")
        elif vid in vids:
            c.add(f"{where}.id", f"id «{vid}» повторяется")
        else:
            vids.add(vid)
        at = c.int_field(where, v, "at_ms", 0)
        c.text(where, v, "text", MAX_VOICEOVER)
        dur = c.int_field(where, v, "duration_ms", 0) if v.get("duration_ms") is not None else None
        if at is not None and at > D:
            c.add(f"{where}.at_ms", f"{at} мс за пределами ролика ({D} мс)")
        if at is not None and dur is not None and at + dur > D + 50:
            c.add(f"{where}.duration_ms", f"реплика заканчивается на {at + dur} мс, ролик длится {D} мс")
        for key in ("duck",):
            if key in v and not isinstance(v[key], bool):
                c.add(f"{where}.{key}", "true или false")
        beat_ref(where, v, False)

    # ---- inserts
    iids: set[str] = set()
    inserts = plan.get("inserts", [])
    if not isinstance(inserts, list):
        c.add("inserts", "должен быть массивом")
        inserts = []
    pixel = 0
    for k, ins in enumerate(inserts):
        where = f"inserts[{k}]"
        if not c.obj(where, ins, {"id", "kind", "at_ms", "dur_ms", "props", "beat"}, {"id", "kind", "at_ms", "dur_ms", "props", "beat"}):
            continue
        iid = ins.get("id")
        if not isinstance(iid, str) or not iid:
            c.add(f"{where}.id", "должна быть непустая строка")
        elif iid in iids:
            c.add(f"{where}.id", f"id «{iid}» повторяется")
        else:
            iids.add(iid)
        kind = ins.get("kind")
        if kind not in ctx.insert_kinds:
            c.add(f"{where}.kind", f"одно из {', '.join(ctx.insert_kinds)} (получено «{kind}»)")
        at = c.int_field(where, ins, "at_ms", 0)
        dur = c.int_field(where, ins, "dur_ms", 1)
        if at is not None and dur is not None and at + dur > D + 50:
            c.add(where, f"вставка заканчивается на {at + dur} мс, ролик длится {D} мс")
        beat_ref(where, ins, True)
        _check_insert_props(c, where, kind, ins.get("props"), ctx)
        if kind == "pixel_transition":
            pixel += 1
            if dur is not None and not PIXEL_MS[0] <= dur <= PIXEL_MS[1]:
                c.add(f"{where}.dur_ms", f"переход «пиксели» длится {PIXEL_MS[0]}–{PIXEL_MS[1]} мс (регламент §4.5)")
    if pixel > MAX_PIXEL_TRANSITIONS:
        c.add("inserts", f"переходов «пиксели» {pixel}, допустимо не больше {MAX_PIXEL_TRANSITIONS} (регламент §9)")

    # ---- blackout
    bl = plan.get("blackout", [])
    if not isinstance(bl, list):
        c.add("blackout", "должен быть массивом")
        bl = []
    for k, b in enumerate(bl):
        where = f"blackout[{k}]"
        if c.obj(where, b, {"from_ms", "to_ms", "opacity"}, {"from_ms", "to_ms"}):
            a = c.int_field(where, b, "from_ms", 0)
            z = c.int_field(where, b, "to_ms", 0)
            if a is not None and z is not None and z <= a:
                c.add(f"{where}.to_ms", f"должно быть больше from_ms ({a})")
            if z is not None and z > D:
                c.add(f"{where}.to_ms", f"{z} мс за пределами ролика ({D} мс)")
            if "opacity" in b and not (_is_num(b["opacity"]) and 0 <= b["opacity"] <= 1):
                c.add(f"{where}.opacity", "число от 0 до 1")

    # ---- sfx
    sfx = plan.get("sfx", [])
    if not isinstance(sfx, list):
        c.add("sfx", "должен быть массивом")
        sfx = []
    times: list[int] = []
    for k, s in enumerate(sfx):
        where = f"sfx[{k}]"
        if c.obj(where, s, {"at_ms", "name", "volume"}, {"at_ms", "name"}):
            at = c.int_field(where, s, "at_ms", 0, D)
            if at is not None:
                times.append(at)
            if ctx.sfx and s.get("name") not in ctx.sfx:
                c.add(f"{where}.name", f"нет такого звука: «{s.get('name')}» (есть: {', '.join(sorted(ctx.sfx))})")
            if "volume" in s and not (_is_num(s["volume"]) and 0 <= s["volume"] <= 1):
                c.add(f"{where}.volume", "число от 0 до 1")
    times.sort()
    for a, b in zip(times, times[1:]):
        if b - a < SFX_MIN_GAP_MS:
            c.add("sfx", f"эффекты на {a} и {b} мс ближе секунды друг к другу (регламент §5: не больше одного в секунду)")

    # ---- music
    music = plan.get("music")
    if music is not None:
        if c.obj("music", music, {"src", "volume"}, {"src"}):
            src = music.get("src")
            if not isinstance(src, str) or not src:
                c.add("music.src", "должна быть непустая строка")
            elif ctx.job_dir is not None and not (ctx.job_dir / src).is_file():
                c.add("music.src", f"файла «{src}» нет в папке задачи: музыка только из исходников или от владельца (регламент §5)")

    # ---- cta
    cta = plan.get("cta")
    if cta is not None:
        if c.obj("cta", cta, {"platform", "at_ms", "text", "beat"}, {"platform", "at_ms", "text"}):
            if cta.get("platform") not in PLATFORMS:
                c.add("cta.platform", f"одно из {', '.join(PLATFORMS)}")
            elif cta["platform"] != ctx.platform:
                c.add("cta.platform", f"площадка задачи «{ctx.platform}», а в cta «{cta['platform']}»")
            at = c.int_field("cta", cta, "at_ms", 0, D)
            c.text("cta", cta, "text", MAX_CTA)
            beat_ref("cta", cta, False)
            if ctx.platform == "none":
                c.add("cta", "для площадки none призыва нет (ТЗ 21 §8)")
            elif D <= NO_CTA_UNDER_MS:
                c.add("cta", "ролик до 10 секунд идёт без призыва (ТЗ 21 §8)")
            elif ctx.platform == "youtube" and at is not None and at < D - YOUTUBE_CTA_TAIL_MS:
                c.add("cta.at_ms", f"на youtube призыв в последних 5 секундах: не раньше {D - YOUTUBE_CTA_TAIL_MS} мс")
    elif ctx.platform not in ("none",) and D > NO_CTA_UNDER_MS and D < 10 ** 9:
        c.add("cta", f"для площадки {ctx.platform} нужен призыв (cta): ролик длиннее 10 секунд (ТЗ 21 §8)")

    # ---- биты (CRAFT-2): бит без материала это ошибка
    if beats is not None:
        orphans = sorted(beats - used_beats)
        if orphans:
            c.add("beats", f"в плане нет материала для битов: {', '.join(orphans)} (у сегмента, вставки, реплики или призыва должен быть beat)")
    return c.problems


def _check_insert_props(c: _Check, where: str, kind, props, ctx: PlanContext) -> None:
    w = f"{where}.props"
    if kind == "chamber_card":
        if not c.obj(w, props, {"chamber", "of", "title", "pictograms"}, {"chamber", "title", "pictograms"}):
            return
        c.int_field(w, props, "chamber", 0, 999)
        if "of" in props:
            c.int_field(w, props, "of", 1, 99)
        c.text(w, props, "title", MAX_TITLE)
        pics = props.get("pictograms")
        if not isinstance(pics, list) or not 2 <= len(pics) <= 5:
            c.add(f"{w}.pictograms", "список из 2–5 пиктограмм")
        else:
            for j, p in enumerate(pics):
                name = p if isinstance(p, str) else (p.get("name") if isinstance(p, dict) else None)
                if isinstance(p, dict):
                    c.obj(f"{w}.pictograms[{j}]", p, {"name", "active", "label"}, {"name"})
                    if isinstance(p.get("label"), str) and len(p["label"]) > 24:
                        c.add(f"{w}.pictograms[{j}].label", "не длиннее 24 символов")
                if ctx.pictograms and name not in ctx.pictograms:
                    c.add(f"{w}.pictograms[{j}]", f"нет пиктограммы «{name}» (есть: {', '.join(sorted(ctx.pictograms))})")
    elif kind == "hazard_banner":
        if c.obj(w, props, {"text"}, {"text"}):
            c.text(w, props, "text", MAX_ON_SCREEN)
    elif kind == "pixel_transition":
        if c.obj(w, props, {"direction"}, {"direction"}) and props.get("direction") not in ("in", "out"):
            c.add(f"{w}.direction", "in или out")
    elif kind == "test_result":
        if c.obj(w, props, {"result", "label", "text"}, {"result", "text"}):
            if props.get("result") not in ("pass", "fail"):
                c.add(f"{w}.result", "pass или fail")
            if "label" in props:
                c.text(w, props, "label", MAX_TITLE)
            c.text(w, props, "text", MAX_ON_SCREEN)
    elif kind == "lower_third":
        if c.obj(w, props, {"text", "sub"}, {"text"}):
            c.text(w, props, "text", MAX_ON_SCREEN)
            if "sub" in props:
                c.text(w, props, "sub", MAX_ON_SCREEN)
    elif kind not in ("custom",):
        if not isinstance(props, dict):
            c.add(w, "должен быть объектом")                       # новый вид вставки: форму props проверяет схема шаблона (zod)
    elif kind == "custom":
        if not isinstance(props, dict):
            c.add(w, "должен быть объектом")
        elif not isinstance(props.get("component"), str) or not props["component"]:
            c.add(f"{w}.component", "обязательная строка: имя компонента из work/src/custom/index.ts")
        else:
            for key, val in props.items():
                if key == "component":
                    continue
                strings = [val] if isinstance(val, str) else [x for x in val if isinstance(x, str)] if isinstance(val, list) else []
                for s in strings:
                    if len(s) > MAX_ON_SCREEN:
                        c.add(f"{w}.{key}", f"текст на экране {len(s)} символов, предел {MAX_ON_SCREEN}")


# ------------------------------------------------------------------------------------------------ story.json (CRAFT-2)

def validate_story(story) -> list[str]:
    """Проверка `plan/story.json`: логлайн, крючок, обещание, биты с ролями, пары подготовка-отдача, длительность."""
    c = _Check()
    top = {"logline", "hook", "promise", "beats", "setups_payoffs", "runner", "vo_role", "target_duration_ms"}
    if not c.obj("story", story, top, {"logline", "hook", "promise", "beats", "target_duration_ms"}):
        return c.problems
    for key in ("logline", "promise"):
        if not isinstance(story.get(key), str) or not story[key].strip():
            c.add(key, "должна быть непустая строка")
    hook = story.get("hook")
    if c.obj("hook", hook, {"t_ms", "what", "why"}, {"what", "why"}):
        for key in ("what", "why"):
            if not isinstance(hook.get(key), str) or not hook[key].strip():
                c.add(f"hook.{key}", "должна быть непустая строка")
        if "t_ms" in hook:
            c.int_field("hook", hook, "t_ms", 0)
    target = c.int_field("story", story, "target_duration_ms", 1000)
    beats = story.get("beats")
    ids: list[str] = []
    if not isinstance(beats, list) or not beats:
        c.add("beats", "нужен непустой массив битов")
        beats = []
    last_end = -1
    for k, b in enumerate(beats):
        where = f"beats[{k}]"
        if not c.obj(where, b, {"id", "role", "source", "t_from_ms", "t_to_ms", "emotion", "purpose"}, {"id", "role", "t_from_ms", "t_to_ms", "purpose"}):
            continue
        bid = b.get("id")
        if not isinstance(bid, str) or not bid:
            c.add(f"{where}.id", "должна быть непустая строка")
        elif bid in ids:
            c.add(f"{where}.id", f"id «{bid}» повторяется")
        else:
            ids.append(bid)
        if b.get("role") not in BEAT_ROLES:
            c.add(f"{where}.role", f"одно из {', '.join(BEAT_ROLES)}")
        a = c.int_field(where, b, "t_from_ms", 0)
        z = c.int_field(where, b, "t_to_ms", 0)
        if a is not None and z is not None:
            if z <= a:
                c.add(f"{where}.t_to_ms", f"должно быть больше t_from_ms ({a})")
            if a < last_end - 1:
                c.add(f"{where}.t_from_ms", "биты идут по времени подряд, без нахлёста")
            last_end = max(last_end, z)
        if target is not None and z is not None and z > target + 2000:
            c.add(f"{where}.t_to_ms", f"бит заканчивается на {z} мс, а target_duration_ms = {target}")
        if not isinstance(b.get("purpose"), str) or not b["purpose"].strip():
            c.add(f"{where}.purpose", "зачем этот бит: непустая строка")
    for k, sp in enumerate(story.get("setups_payoffs") or []):
        where = f"setups_payoffs[{k}]"
        if c.obj(where, sp, {"setup", "payoff", "what"}, {"setup", "payoff"}):
            for key in ("setup", "payoff"):
                if sp.get(key) not in ids:
                    c.add(f"{where}.{key}", f"бита «{sp.get(key)}» нет среди beats")
    return c.problems


def story_beat_ids(story: dict) -> set[str]:
    return {b["id"] for b in story.get("beats") or [] if isinstance(b, dict) and isinstance(b.get("id"), str)}

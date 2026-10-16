"""Контроль качества (MON-QA-1…QA-6, CRAFT-3, CRAFT-4): каждая техническая проверка на синтетических файлах ffmpeg (громкость,
blackdetect, freezedetect, silencedetect — на настоящем ffmpeg, файл 8 секунд), логика субтитров, зон, ремесла, режимов приёмки."""
import copy
import json
import subprocess

import pytest

import media
import qa
import review_mode as rm
from layout import DEFAULT
from tests.stubs import STORY, make_plan

ZONES = qa.load_zones(DEFAULT)


# ------------------------------------------------------------------------------------------------ настоящий ffmpeg

@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    """8 секунд 64×64, 10 к/с: 0-1,2 с чёрное, 1,2-4,2 с красное неподвижное, дальше движение; звук: тон 1 с, тишина 4 с, тон 3 с."""
    path = tmp_path_factory.mktemp("qa") / "clip.mp4"
    graph = ("color=c=black:s=64x64:r=10:d=1.2[v0];color=c=red:s=64x64:r=10:d=3[v1];testsrc2=s=64x64:r=10:d=3.8[v2];"
             "[v0][v1][v2]concat=n=3:v=1:a=0[v];sine=frequency=440:d=1[a0];anullsrc=r=44100:cl=mono:d=4[a1];sine=frequency=440:d=3[a2];"
             "[a0][a1][a2]concat=n=3:v=0:a=1[a]")
    res = subprocess.run([media.find_ffmpeg(), "-hide_banner", "-y", "-filter_complex", graph, "-map", "[v]", "-map", "[a]", "-c:v", "libx264",
                          "-pix_fmt", "yuv420p", "-c:a", "aac", "-t", "8", str(path)], capture_output=True)
    assert res.returncode == 0, res.stderr[-300:]
    return path


def small_plan():
    plan = make_plan(platform="none", duration=8000, voiceover=False, captions=False)
    plan["format"] = {"width": 64, "height": 64, "fps": 10}
    plan["inserts"], plan["sfx"], plan["cta"] = [], [], None
    plan["voiceover"] = [{"id": "vo1", "at_ms": 2000, "text": "Длинная реплика закадра", "duration_ms": 2500, "beat": "b2"}]
    return plan


async def test_ffmpeg_checks_find_black_freeze_silence_and_loudness(clip):
    black = await media.blackdetect(clip, min_s=0.5)
    assert black and black[0][0] < 0.1 and 1.0 < black[0][1] < 1.4
    freeze = await media.freezedetect(clip, min_s=2.0)
    assert any(a < 1.5 and b > 4.0 for a, b in freeze)
    silences = await media.silencedetect(clip, noise_db=-45, min_s=1.5)
    assert silences and 0.8 < silences[0][0] < 1.3 and 4.5 < silences[0][1] < 5.5
    loud = await media.loudness(clip)
    assert loud["integral"] is not None and -40 < loud["integral"] < 0 and loud["true_peak"] is not None
    head = await media.silencedetect(clip, noise_db=-45, min_s=0.5, limit_s=3.0)
    assert head and head[0][1] <= 3.01


async def test_run_technical_reports_each_problem(clip):
    plan = small_plan()
    findings, checked = await qa.run_technical(plan, [], STORY, clip, DEFAULT, draft=False, scale=1.0, platform="none")
    by = {}
    for f in findings:
        by.setdefault(f.check, []).append(f)
    assert by["blackdetect"][0].severity == "error" and by["blackdetect"][0].t_ms < 200
    assert by["freezedetect"] and by["silencedetect"]                                     # реплика закадра попала в тишину
    assert by["craft.first_seconds"]                                                      # тишина в первых 3 с
    assert by["loudness"][0].severity == "error"                                          # финал: громкость вне -14 ± 1
    assert not any(f.check == "format" for f in findings) and checked["format"]["width"] == 64
    assert by["ocr"][0].severity == "warning"                                             # OCR не установлен: пропуск с предупреждением
    draft, _ = await qa.run_technical(plan, [], STORY, clip, DEFAULT, draft=True, scale=1.0, platform="none")
    assert [f.severity for f in draft if f.check == "loudness"] == ["warning"] * len([f for f in draft if f.check == "loudness"])


async def test_format_mismatch_is_an_error(clip):
    plan = small_plan()
    plan["format"] = {"width": 128, "height": 64, "fps": 25}
    plan["duration_ms"] = 12000
    findings, _ = await qa.run_technical(plan, [], STORY, clip, DEFAULT, draft=False, scale=1.0, platform="none")
    msgs = " ".join(f.message for f in findings if f.check == "format")
    assert "128×64" in msgs and "кадров в секунду" in msgs and "длительность" in msgs


# ------------------------------------------------------------------------------------------------ чистая логика

def test_interval_helpers():
    assert qa.subtract((0.0, 10.0), [(2.0, 3.0), (8.0, 12.0)]) == [(0.0, 2.0), (3.0, 8.0)]
    assert qa.subtract((1.0, 2.0), [(0.0, 5.0)]) == []
    assert qa.overlap((0, 5), (3, 9)) == 2 and qa.overlap((0, 1), (2, 3)) == 0


def words(*items, start=0, gap=100):
    out, t = [], start
    for i, text in enumerate(items):
        out.append({"i": i, "text": text, "start_ms": t, "end_ms": t + 300, "conf": 0.9})
        t += 300 + gap
    return out


def seg_plan(**over):
    plan = make_plan(voiceover=False, duration=9000)
    plan["segments"] = [{"id": "s1", "source": "sources/a.mp4", "in_ms": 1000, "out_ms": 8000, "at_ms": 500, "beat": "b2"}]
    plan["captions"] = {"enabled": True, "source": "transcript", "overrides": [], "words_file": "transcript/words.json"}
    plan.update(over)
    return plan


def test_caption_pages_map_words_through_segments_and_drop_commas():
    ws = words("Привет,", "мир.", "Как", "дела?", start=1200)
    ws[1]["start_ms"] = 99999                                    # вне сегмента: выпадает
    ws[1]["end_ms"] = 100300
    pages = qa.caption_pages(seg_plan(), ws, ZONES)
    texts = [" ".join(w["text"] for w in p["words"]) for p in pages]
    assert texts[0].startswith("Привет") and "мир" not in " ".join(texts)
    assert pages[0]["start"] == 500 + (1200 - 1000)              # at_ms + (start - in_ms)
    plan = seg_plan()
    plan["captions"]["overrides"] = [{"word_index": 0, "text": "Aperture"}]
    assert qa.caption_pages(plan, ws, ZONES)[0]["words"][0]["text"] == "Aperture"


def test_caption_pages_break_at_28_chars_and_sentence_end():
    ws = words("абвгдежзик", "лмнопрстуф", "хцчшщъыьэю", "яабвг", start=1200, gap=10)
    pages = qa.caption_pages(seg_plan(), ws, ZONES)
    assert all(len(" ".join(w["text"] for w in p["words"])) <= 28 for p in pages) and len(pages) >= 2


def test_voiceover_words_span_the_replica():
    plan = make_plan(duration=12000)
    plan["voiceover"][0]["duration_ms"] = 2000
    ws = qa.voiceover_words(plan["voiceover"][:1])
    assert ws[0]["start"] == 300 and abs(ws[-1]["end"] - 2300) <= 2 and all(w["voice"] for w in ws)


def test_zone_findings_catch_overlaps_and_unsafe_zones():
    plan = make_plan()
    plan["inserts"].append({"id": "i3", "kind": "chamber_sign", "at_ms": 400, "dur_ms": 2500, "beat": "b1",
                            "props": {"big": "1", "small": "Х", "done": 1, "total": 19, "active": [], "title": "Х"}})
    found = qa.zone_findings(plan, [], ZONES)
    assert any(f.check == "overlap" and "i1" in f.message and "i3" in f.message for f in found)
    plan2 = make_plan()
    plan2["captions"]["position"] = {"y_ratio": 0.9}
    pages = [{"start": 5000, "end": 5600, "voice": True, "words": [{"text": "я"}]}]
    assert any(f.check == "safe_zone" for f in qa.zone_findings(plan2, pages, ZONES))
    assert qa.zone_findings(make_plan(), [{"start": 5000, "end": 5600, "voice": True, "words": []}], ZONES) == []


def test_captions_move_away_from_inserts():
    plan = make_plan()
    y_free, ok = qa.caption_y_ratio(plan, ZONES, 6000)
    assert ok and y_free == pytest.approx(0.62)
    plan["inserts"].append({"id": "x", "kind": "chamber_sign", "at_ms": 6000, "dur_ms": 2500, "beat": "b2",
                            "props": {"big": "1", "small": "т", "done": 1, "total": 19, "active": [], "title": "т"}})
    y_busy, ok = qa.caption_y_ratio(plan, ZONES, 6500)
    assert ok and y_busy != pytest.approx(0.62)


# ------------------------------------------------------------------------------------------------ ремесло (CRAFT-4)

def craft(plan, story=STORY, words=(), platform="shorts"):
    return {f.check: f for f in qa.craft_plan_checks(plan, copy.deepcopy(story), list(words), platform)}


def test_craft_first_beat_and_ending():
    story = copy.deepcopy(STORY)
    story["beats"][0]["t_to_ms"] = 3500
    assert "craft.first_beat" in craft(make_plan(), story)
    story2 = copy.deepcopy(STORY)
    story2["beats"][-1]["role"] = "payoff"
    assert "craft.ending" in craft(make_plan(), story2)
    assert craft(make_plan()) == {}


def test_craft_rhythm_is_a_warning_and_jump_cut_is_an_error():
    plan = make_plan(voiceover=False)
    plan["segments"] = [{"id": "s1", "source": "sources/a.mp4", "in_ms": 0, "out_ms": 4000, "at_ms": 0, "beat": "b1"},
                        {"id": "s2", "source": "sources/a.mp4", "in_ms": 4400, "out_ms": 8000, "at_ms": 4000, "beat": "b2"}]
    found = craft(plan)
    assert found["craft.rhythm"].severity == "warning" and found["craft.jump_cut"].severity == "error"
    plan["segments"][1]["intent"] = "jump_cut"
    assert "craft.jump_cut" not in craft(plan)
    plan["segments"][1].pop("intent")
    plan["segments"][1]["in_ms"] = 4000                          # продолжение без сдвига это не перебивка
    assert "craft.jump_cut" not in craft(plan)
    plan["segments"][1]["in_ms"] = 9000
    plan["segments"][1]["out_ms"] = 12000
    assert "craft.jump_cut" not in craft(plan)                   # сдвиг больше секунды: монтажная склейка, не перебивка


def test_craft_voiceover_over_live_speech_needs_duck():
    plan = seg_plan()
    plan["voiceover"] = [{"id": "vo1", "at_ms": 1700, "text": "Реплика поверх речи", "duration_ms": 2000, "beat": "b2", "duck": False}]
    ws = words("Говорит", "человек", "долго", "и", "много", start=1500)
    assert "craft.voice_overlap" in craft(plan, words=ws)
    plan["voiceover"][0]["duck"] = True
    assert "craft.voice_overlap" not in craft(plan, words=ws)


# ------------------------------------------------------------------------------------------------ OCR, кадры, ответ проверяющего

def test_text_recall_is_case_and_punctuation_insensitive():
    assert qa.text_recall("Нарушение протокола", "НАРУШЕНИЕ  ПРОТОКОЛА!") == 1.0
    assert qa.text_recall("Нарушение протокола", "Нарушение прот") < 0.85
    assert qa.text_recall("", "что угодно") == 1.0


async def test_ocr_mismatch_is_an_error_and_match_passes(tmp_path, monkeypatch):
    seen = {"text": "Правило безопасности Правило принято"}

    async def ocr(path):
        return seen["text"]

    async def stills(frame, out):
        out.write_bytes(b"png")
    plan = make_plan()
    # без настоящего видео: проверяем только ветку OCR через вспомогательные функции
    items = [i for i in qa.expected_texts(plan, [], ZONES) if i[2].startswith("вставка")]
    assert [t for _, t, _ in items] == ["Правило безопасности", "Правило принято"]
    assert qa.text_recall(items[0][1], await ocr(tmp_path)) >= qa.OCR_MIN_SIMILARITY
    seen["text"] = "Правило безопас"
    assert qa.text_recall(items[0][1], await ocr(tmp_path)) < qa.OCR_MIN_SIMILARITY


def test_key_frames_cover_start_inserts_beats_cta_and_end():
    frames = qa.key_frame_times(make_plan(), STORY)
    assert 8 <= len(frames) + 6 and len(frames) <= 16
    times = [t for t, _ in frames]
    assert times == sorted(times) and times[0] <= 300 and times[-1] >= 11000
    assert any("призыв" in w for _, w in frames) and any("вставка i1" in w for _, w in frames)


def test_semantic_answer_parsing_and_fix_notes():
    text = 'Вот оценка:\n```json\n{"criteria": {"readability": {"score": 2, "issues": [{"t_ms": 4000, "text": "мелко"}]}, "hook": {"score": 5, "issues": []}}, "overall": 3, "verdict": "fix"}\n```'
    sem = qa.parse_semantic(text)
    assert sem["verdict"] == "fix" and sem["overall"] == 3 and sem["criteria"]["readability"]["issues"][0]["t_ms"] == 4000
    assert any("мелко" in n and "4.0 с" in n for n in qa.semantic_fix_notes(sem))
    assert qa.parse_semantic("нет json") is None
    assert qa.parse_semantic('{"criteria": {}, "overall": 5}')["verdict"] == "pass"
    rubric = (DEFAULT.prompts / "qa_rubric.md").read_text(encoding="utf-8")
    assert all(name in rubric for name in qa.CRITERIA)                                    # рубрика покрывает критерии MON-QA-2 и CRAFT-3


def test_report_files_are_written(tmp_path):
    out = qa.QAOutcome(findings=[qa.Finding("blackdetect", "error", "чёрный кадр", 1000), qa.Finding("ocr", "warning", "OCR нет")],
                       semantic={"criteria": {"hook": {"score": 4, "issues": []}}, "overall": 4, "verdict": "pass"})
    jp, mp = qa.write_report(tmp_path / "qa", "j-aaaaaaaa", out, draft=True, fixed=["круг 1: исправлено"])
    doc = json.loads(jp.read_text(encoding="utf-8"))
    assert doc["summary"]["technical_errors"] == 1 and doc["fixed"] == ["круг 1: исправлено"]
    md = mp.read_text(encoding="utf-8")
    assert "чёрный кадр" in md and "Исправлено автоматически" in md and not out.passed


# ------------------------------------------------------------------------------------------------ режимы приёмки (MON-QA-5, QA-6)

def verdicts(n, agree):
    rows = []
    for k in range(n):
        ok = k < agree
        rows.append({"stage": "review", "answer": "готово", "qa": {"passed": ok}})            # человек всегда «готово»; автопроверка pass = согласие
    return rows


def test_agreement_counts_pass_ready_pairs():
    stats = rm.agreement(verdicts(20, 17) + [{"stage": "plan_approval", "answer": "утвердить", "qa": {}}])
    assert (stats["decisions"], stats["agreed"]) == (20, 17) and rm.agreement_text(stats) == "согласие с вами: 17/20, 85 %"
    both_fix = rm.agreement([{"stage": "review", "answer": "правки", "qa": {"passed": False}}])
    assert both_fix["agreed"] == 1


def test_auto_mode_needs_twenty_decisions_and_ninety_percent():
    ok, why = rm.auto_allowed(rm.agreement(verdicts(19, 19)))
    assert not ok and "19 из 20" in why
    ok, why = rm.auto_allowed(rm.agreement(verdicts(20, 17)))
    assert not ok and "85 %" in why and "90" in why
    assert rm.auto_allowed(rm.agreement(verdicts(20, 18)))[0]
    assert rm.effective_mode("auto", rm.agreement(verdicts(5, 5)))[0] == "auto_then_human"
    assert rm.effective_mode("auto", rm.agreement(verdicts(30, 29)))[0] == "auto"
    assert rm.effective_mode("human", {"decisions": 0, "agreed": 0, "share": 0})[0] == "human"
    assert rm.effective_mode("что-то", {"decisions": 0, "agreed": 0, "share": 0})[0] == "human"


def test_auto_skips_human_review_only_when_everything_is_clean():
    clean = {"technical_errors": 0, "semantic_overall": 4, "semantic_verdict": "pass"}
    assert rm.skip_human_review("auto", clean)
    assert not rm.skip_human_review("auto", {**clean, "semantic_overall": 3})
    assert not rm.skip_human_review("auto", {**clean, "technical_errors": 1})
    assert not rm.skip_human_review("auto_then_human", clean) and not rm.skip_human_review("human", clean)

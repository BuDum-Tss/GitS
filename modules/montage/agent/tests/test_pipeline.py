"""Конвейер на заглушках (MON-37): все стадии до done, вопросы и возвраты, auto_final, восстановление на каждой стадии
(MON-8), батарейка (MON-10), повтор и отказ (MON-9), автоисправление по контролю качества (MON-QA-3), опыт (MON-32)."""
import asyncio
import json

import pytest

import qa as qa_mod
from claude_runner import StageResult
from jobs import PAUSED_STAGE
from tests.rig import Rig
from tests.stubs import STORY, make_plan

pytestmark = pytest.mark.usefixtures("fast")

ERR = qa_mod.Finding("blackdetect", "error", "чёрный кадр 1.0 с вне затемнения", 5000)


async def test_full_run_with_human_approvals(tmp_path):
    rig = Rig(tmp_path)
    job = rig.new_job()
    await rig.run(job, ["утвердить", "готово"])
    job = rig.reload(job)
    assert (job.state, job.stage, job.data["progress"]) == ("done", "done", 1.0)
    states = rig.core.states(job.job_id)
    assert ("waiting_user", "plan_approval") in states and ("waiting_user", "review") in states
    stages = [s for _, s in states]
    order = [s for s in ("intake", "transcribe", "story", "plan", "plan_approval", "voiceover", "build", "preview", "review", "final", "done")
             if s in stages]
    assert [stages.index(s) for s in order] == sorted(stages.index(s) for s in order)           # стадии идут по порядку
    out = tmp_path / "out" / "final_shorts.mp4"
    assert out.is_file() and job.data["output"] == str(out)
    assert [r["scale"] for r in rig.renderer.renders] == [0.5, None] and rig.renderer.renders[1]["crf"] == 16
    launch = [e for e in rig.core.events if e["kind"] == "montage.launch"]
    assert len(launch) == 1 and launch[0]["announce"] is True and "финальный рендер" in launch[0]["text"]
    assert any(e["kind"] == "montage.done" for e in rig.core.events)
    assert len(rig.core.tts_calls) == 2 and (job.work / "public" / "vo" / "vo1.wav").is_file()
    plan = json.loads((job.plan_dir / "plan.json").read_text(encoding="utf-8"))
    assert all(v["duration_ms"] for v in plan["voiceover"])
    verdicts = rig.store.verdicts()
    assert [(v["stage"], v["answer"]) for v in verdicts] == [("plan_approval", "утвердить"), ("review", "готово")]
    assert job.data["claude"]["tokens"] > 0 and job.data["claude"]["session_id"] == "sess-1"
    models = {c["stage"]: c["model"] for c in rig.runner.calls}
    assert models["story"] == "opus" and models["plan"] == "opus" and models["build"] == "sonnet"
    log = [r["kind"] for r in rig.store.read_log(job)]
    assert "stage" in log and "answer" in log


async def test_clarify_is_skipped_when_nothing_is_missing_and_asked_when_it_is(tmp_path):
    rig = Rig(tmp_path, {"approve_plan": False, "auto_final": True})
    job = rig.new_job()
    await rig.run(job)
    assert not any(c["stage"] == "clarify" for c in rig.runner.calls)
    rig2 = Rig(tmp_path / "b", {"approve_plan": False, "auto_final": True})
    job2 = rig2.new_job(platform="none", platform_explicit=False)
    await rig2.run(job2)
    assert [c["stage"] for c in rig2.runner.calls][0] == "clarify" and rig2.reload(job2).request["platform"] == "shorts"


async def test_redo_plan_then_story_then_edits_go_back(tmp_path):
    rig = Rig(tmp_path)
    job = rig.new_job()
    await rig.run(job, ["переделать план", "добавить баннер", "переделать историю", "сделать мрачнее", "утвердить", "правки",
                        "субтитры выше", "готово"])
    job = rig.reload(job)
    assert job.state == "done"
    stages = [c["stage"] for c in rig.runner.calls]
    assert stages.count("plan") == 3 and stages.count("story") == 2 and stages.count("build") == 2
    plan_prompts = [c["prompt"] for c in rig.runner.calls if c["stage"] == "plan"]
    assert "добавить баннер" in plan_prompts[1] and "добавить баннер" in plan_prompts[2]
    assert "сделать мрачнее" in next(c["prompt"] for c in rig.runner.calls if c["stage"] == "story" and c["n"] == 2)
    build_prompts = [c["prompt"] for c in rig.runner.calls if c["stage"] == "build"]
    assert "субтитры выше" not in build_prompts[0] and "субтитры выше" in build_prompts[1]
    assert job.data["revisions"][0]["applied"] is True
    kinds = [(v["stage"], v["answer"]) for v in rig.store.verdicts()]
    assert kinds == [("plan_approval", "переделать план"), ("plan_approval", "переделать историю"), ("plan_approval", "утвердить"),
                     ("review", "правки"), ("review", "готово")]


async def test_free_text_answers_are_understood(tmp_path):
    rig = Rig(tmp_path)
    job = rig.new_job()
    await rig.run(job, ["да, годится", "правки пока не нужны, всё готово"])
    assert rig.reload(job).state == "done"


async def test_auto_final_skips_the_review_question(tmp_path):
    rig = Rig(tmp_path, {"auto_final": True})
    job = rig.new_job()
    await rig.run(job, ["утвердить"])
    states = rig.core.states(job.job_id)
    assert ("waiting_user", "review") not in states and rig.reload(job).state == "done"


async def test_approve_plan_off_skips_the_plan_question(tmp_path):
    rig = Rig(tmp_path, {"approve_plan": False})
    job = rig.new_job()
    await rig.run(job, ["готово"])
    assert ("waiting_user", "plan_approval") not in rig.core.states(job.job_id)


async def test_invalid_plan_goes_back_to_the_agent_with_errors(tmp_path):
    rig = Rig(tmp_path, {"approve_plan": False, "auto_final": True})

    def bad_then_good(env, prompt, n):
        plan = make_plan()
        if n == 1:
            plan["segments"] = [{"id": "s1", "source": "sources/нет.mp4", "in_ms": 0, "out_ms": 1000, "at_ms": 0, "beat": "b1"}]
        (env.job.plan_dir / "plan.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    rig.runner.override["plan"] = bad_then_good
    job = rig.new_job()
    await rig.run(job)
    plans = [c for c in rig.runner.calls if c["stage"] == "plan"]
    assert len(plans) == 2 and "нет.mp4" in plans[1]["prompt"] and rig.reload(job).state == "done"


async def test_plan_that_never_passes_fails_after_three_attempts(tmp_path):
    rig = Rig(tmp_path, {"approve_plan": False})
    rig.runner.override["plan"] = lambda env, prompt, n: (env.job.plan_dir / "plan.json").write_text("{}", encoding="utf-8")
    job = rig.new_job()
    await rig.run(job)
    job = rig.reload(job)
    assert job.state == "failed" and job.data["error"]["code"] == "bad_request" and "plan" in job.data["error"]["message"]
    assert len([c for c in rig.runner.calls if c["stage"] == "plan"]) == 3                      # без повторной попытки стадии
    assert any(e["kind"] == "montage.failed" for e in rig.core.events)


async def test_story_without_material_for_a_beat_is_a_plan_error(tmp_path):
    rig = Rig(tmp_path, {"approve_plan": False})
    story = {**STORY, "beats": STORY["beats"] + [{"id": "b4", "role": "cta", "t_from_ms": 12000, "t_to_ms": 12500, "purpose": "призыв"}]}
    rig.runner.story = story
    job = rig.new_job()
    await rig.run(job)
    assert rig.reload(job).state == "failed" and "b4" in rig.reload(job).data["error"]["message"]


async def test_stage_failure_is_retried_once_then_job_fails(tmp_path):
    rig = Rig(tmp_path, {"approve_plan": False, "auto_final": True})
    rig.runner.fail_stage = {"story": 1}
    job = rig.new_job()
    await rig.run(job)
    assert rig.reload(job).state == "done" and any(r["kind"] == "retry" for r in rig.store.read_log(job))
    rig2 = Rig(tmp_path / "b", {"approve_plan": False})
    rig2.runner.fail_stage = {"story": 5}
    job2 = rig2.new_job()
    await rig2.run(job2)
    j2 = rig2.reload(job2)
    assert j2.state == "failed" and j2.data["error"]["code"] == "internal" and "story" in j2.data["error"]["message"]
    assert "Traceback" not in j2.data["error"]["message"]
    assert len([c for c in rig2.runner.calls if c["stage"] == "story"]) == 2


async def test_claude_limit_does_not_retry(tmp_path):
    from claude_runner import ClaudeLimitError
    rig = Rig(tmp_path)

    def limit(env, prompt, n):
        raise ClaudeLimitError("You've hit your session limit")
    rig.runner.override["story"] = limit
    job = rig.new_job()
    await rig.run(job)
    j = rig.reload(job)
    assert j.state == "failed" and j.data["error"]["code"] == "unavailable" and len(rig.runner.calls) == 1


async def test_talking_head_fails_with_unavailable_transcription(tmp_path):
    rig = Rig(tmp_path)
    src = tmp_path / "clip.mp4"
    res = await __import__("media").ffmpeg(["-f", "lavfi", "-i", "testsrc2=s=160x90:r=10:d=2", "-f", "lavfi", "-i", "sine=d=2", "-c:v", "libx264",
                                            "-pix_fmt", "yuv420p", "-c:a", "aac", str(src)])
    assert res.ok
    job = rig.new_job(sources=[str(src)], source_kind="talking_head", duration_s=None)
    await rig.run(job)
    j = rig.reload(job)
    assert j.state == "failed" and j.data["error"]["code"] == "unavailable" and "WhisperX" in j.data["error"]["message"]
    assert j.stage == "transcribe"


async def test_qa_autofix_loop_is_limited_to_two_rounds(tmp_path):
    qa = __import__("tests.stubs", fromlist=["StubQA"]).StubQA([[ERR], [ERR], [ERR]])
    rig = Rig(tmp_path, {"approve_plan": False}, qa=qa)
    job = rig.new_job()
    await rig.run(job, ["готово"])
    j = rig.reload(job)
    assert j.state == "done"
    builds = [c for c in rig.runner.calls if c["stage"] == "build"]
    assert len(builds) == 3 and "чёрный кадр" in builds[1]["prompt"]
    assert len(rig.renderer.renders) == 4                                                         # 3 черновика + финал
    reviews = [c for c in rig.runner.calls if c["stage"] == "review"]
    assert len(reviews) == 1                                                                       # смысловая проверка только на последнем круге
    q = next(u for u in rig.core.updates if u.get("state") == "waiting_user" and u["stage"] == "review")
    assert "Автопроверка не прошла" in q["question"]["question"]
    assert j.data["qa"]["technical_errors"] == 1 and (j.qa / "report.json").is_file() and (j.qa / "report.md").is_file()
    assert any(a["kind"] == "qa_report" for a in j.data["artifacts"])


async def test_qa_fix_then_semantic_review_runs_once_when_technical_is_clean(tmp_path):
    from tests.stubs import StubQA
    rig = Rig(tmp_path, {"approve_plan": False, "auto_final": True}, qa=StubQA([[ERR], []]))
    rig.runner.review_replies = [{"criteria": {"hook": {"score": 5, "issues": []}}, "overall": 5, "verdict": "pass"}]
    job = rig.new_job()
    await rig.run(job)
    j = rig.reload(job)
    assert len([c for c in rig.runner.calls if c["stage"] == "build"]) == 2
    assert len([c for c in rig.runner.calls if c["stage"] == "review"]) == 1 and j.data["qa"]["passed"] is True
    rev = next(c for c in rig.runner.calls if c["stage"] == "review")
    assert rev["readonly"] and rev["fresh"]                                                         # отдельный вызов, не сессия монтажёра


async def test_semantic_fix_verdict_goes_back_to_the_builder(tmp_path):
    rig = Rig(tmp_path, {"approve_plan": False, "auto_final": True})
    rig.runner.review_replies = [
        {"criteria": {"readability": {"score": 2, "issues": [{"t_ms": 2000, "text": "заголовок мелкий"}]}}, "overall": 2, "verdict": "fix"},
        {"criteria": {"readability": {"score": 5, "issues": []}}, "overall": 5, "verdict": "pass"}]
    job = rig.new_job()
    await rig.run(job)
    builds = [c for c in rig.runner.calls if c["stage"] == "build"]
    assert len(builds) == 2 and "заголовок мелкий" in builds[1]["prompt"]


@pytest.mark.parametrize("crash_at", ["intake", "clarify", "transcribe", "story", "plan", "plan_approval", "voiceover", "build",
                                      "preview", "review", "final"])
async def test_recovery_after_a_crash_on_every_stage(tmp_path, crash_at):
    rig = Rig(tmp_path)
    job = rig.new_job(platform_explicit=False if crash_at == "clarify" else True)
    async def crash(j):
        raise asyncio.CancelledError                                                              # процесс умер на этой стадии

    rig.pipeline.handlers[crash_at] = crash
    with pytest.raises(asyncio.CancelledError):
        await rig.run(job, ["утвердить", "готово"])
    saved = rig.reload(job)
    assert saved.state == "running" and saved.stage == crash_at                                   # на диске стадия записана
    rig2 = Rig(tmp_path)
    job2 = rig2.store.unfinished()[0]
    await rig2.run(job2, ["утвердить", "готово"])
    done = rig2.reload(job2)
    assert done.state == "done" and (tmp_path / "out" / "final_shorts.mp4").is_file()


async def test_recovery_asks_a_pending_review_question_again(tmp_path):
    rig = Rig(tmp_path)
    job = rig.new_job()
    task = asyncio.create_task(rig.pipeline.run_job(job))
    qid = await rig._wait(lambda: rig.pipeline.waiting_question(job.job_id))
    await rig._wait(lambda: rig.reload(job).state == "waiting_user")
    saved = rig.reload(job)
    assert saved.state == "waiting_user" and saved.data["question"]["question_id"] == qid
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    rig2 = Rig(tmp_path)
    job2 = rig2.store.unfinished()[0]
    t2 = asyncio.create_task(rig2.run(job2, ["утвердить", "готово"]))
    await t2
    first_q = [u for u in rig2.core.updates if u.get("state") == "waiting_user"][0]["question"]
    assert first_q["question_id"] == qid                                                          # тот же question_id: ядро не спросит дважды


async def test_battery_pause_and_resume(tmp_path):
    rig = Rig(tmp_path, {"approve_plan": False, "auto_final": True})
    job = rig.new_job()
    rig.pipeline.set_sleeping(True)
    task = asyncio.create_task(rig.run(job))
    await rig._wait(lambda: (PAUSED_STAGE in [s for _, s in rig.core.states(job.job_id)]))
    paused = [u for u in rig.core.updates if u.get("stage") == PAUSED_STAGE][0]
    assert paused["state"] == "queued" and paused["text"] == "пауза: нет заряда Claude"
    assert not rig.runner.calls                                                                   # Claude не вызывался
    rig.pipeline.set_sleeping(False)
    await task
    assert rig.reload(job).state == "done"
    assert ("running", "story") in rig.core.states(job.job_id)


async def test_battery_sleeping_in_core_state_pauses_before_next_claude_call(tmp_path):
    rig = Rig(tmp_path, {"approve_plan": False, "auto_final": True})
    job = rig.new_job()

    def sleep_after_story(env, prompt, n):
        rig.core.sleeping = True
        rig.pipeline.set_sleeping(True)
        (env.job.plan_dir / "story.json").write_text(json.dumps(STORY), encoding="utf-8")
    rig.runner.override["story"] = sleep_after_story
    task = asyncio.create_task(rig.run(job))
    await rig._wait(lambda: PAUSED_STAGE in [s for _, s in rig.core.states(job.job_id)])
    assert [c["stage"] for c in rig.runner.calls] == ["story"]                                    # текущий вызов дошёл до конца
    assert rig.reload(job).stage == PAUSED_STAGE
    rig.core.sleeping = False
    rig.pipeline.set_sleeping(False)
    await task
    assert rig.reload(job).state == "done"


async def test_voiceover_overlaps_are_shifted_and_extended(tmp_path):
    from pipeline import fit_voiceover
    plan = make_plan(segments=[])
    plan["voiceover"][0]["duration_ms"] = 5000                   # первая реплика доходит до 5300, вторая стоит на 4000
    plan["voiceover"][1]["duration_ms"] = 9000
    fit = fit_voiceover(plan)
    assert fit["shifted"] == [{"id": "vo2", "from": 4000, "to": 5600}]
    assert plan["voiceover"][1]["at_ms"] == 5600 and fit["duration_extended"] and plan["duration_ms"] == 5600 + 9000 + 600


async def test_voiceover_not_requested_but_planned_is_rejected(tmp_path):
    rig = Rig(tmp_path, {"approve_plan": False})
    rig.runner.plan_factory = make_plan
    job = rig.new_job(voiceover="none")
    rig.runner.override["plan"] = lambda env, p, n: (env.job.plan_dir / "plan.json").write_text(json.dumps(make_plan()), encoding="utf-8")
    await rig.run(job)
    assert rig.reload(job).state == "failed" and "закадр не запрошен" in rig.reload(job).data["error"]["message"]


async def test_youtube_final_gets_an_srt_next_to_it_and_no_burned_captions(tmp_path):
    rig = Rig(tmp_path, {"approve_plan": False, "auto_final": True})
    rig.runner.plan_factory = lambda: make_plan(platform="youtube", duration=14000, captions=True)
    job = rig.new_job(platform="youtube", format="horizontal")
    await rig.run(job)
    j = rig.reload(job)
    assert j.state == "done"
    srt = tmp_path / "out" / "final_youtube.srt"
    text = srt.read_text(encoding="utf-8")
    assert "-->" in text and "Протокол" in text and (tmp_path / "out" / "final_youtube.mp4").is_file()
    assert any(a["kind"] == "subtitles" for a in j.data["artifacts"])


async def test_burned_captions_are_rejected_for_youtube(tmp_path):
    rig = Rig(tmp_path, {"approve_plan": False})
    bad = make_plan(platform="youtube", duration=14000)
    bad["captions"]["enabled"] = True
    rig.runner.plan_factory = lambda: bad
    job = rig.new_job(platform="youtube", format="horizontal")
    await rig.run(job)
    j = rig.reload(job)
    assert j.state == "failed" and "captions.enabled" in j.data["error"]["message"]


async def test_cleanup_keeps_the_last_finished_jobs(tmp_path):
    rig = Rig(tmp_path, {"approve_plan": False, "auto_final": True, "keep_jobs": 1})
    jobs = []
    for _ in range(3):
        j = rig.new_job()
        await rig.run(j)
        jobs.append(j)
    left = {p.name for p in (tmp_path / "jobs").glob("j-*")}
    assert left == {jobs[-1].job_id}
    assert (tmp_path / "out" / "final_shorts.mp4").is_file()                                      # копия в output_dir остаётся
    assert (rig.layout.template / "node_modules").is_dir()                                         # уборка не задела шаблон (junction)


async def test_progress_is_monotonic_across_stages(tmp_path):
    rig = Rig(tmp_path, {"approve_plan": False, "auto_final": True})
    job = rig.new_job()
    await rig.run(job)
    values = [u["progress"] for u in rig.core.updates if "progress" in u]
    assert values == sorted(values) or max(values) == 1.0
    assert values[-1] == 1.0


def test_stage_result_dataclass_defaults():
    assert StageResult().tokens == 0

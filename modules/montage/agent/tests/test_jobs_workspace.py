"""Хранилище задач (MON-8, MON-11), рабочая копия шаблона (MON-1), файл субтитров .srt."""
import json
import threading

from jobs import JobStore, PAUSED_STAGE, STAGES
from layout import DEFAULT
from tests.stubs import make_plan
import srt as srt_mod
import qa as qa_mod
import workspace


def request():
    return {"sources": [], "brief": "Шортс про куб", "platform": "shorts"}


def test_job_folder_layout_and_job_json(tmp_path):
    store = JobStore(tmp_path / "jobs")
    job = store.create(request())
    assert job.job_id.startswith("j-") and len(job.job_id) == 10
    for sub in ("sources", "transcript", "plan", "work", "renders", "qa"):
        assert (job.folder / sub).is_dir()
    data = json.loads((job.folder / "job.json").read_text(encoding="utf-8"))
    assert {"job_id", "title", "created", "updated", "state", "stage", "progress", "request", "question", "answers", "revisions", "claude",
            "artifacts", "error"} <= set(data)
    assert data["state"] == "queued" and data["stage"] == "intake" and data["claude"] == {"session_id": None, "tokens": 0}
    assert STAGES.index("story") == STAGES.index("transcribe") + 1 and STAGES.index("plan") == STAGES.index("story") + 1
    assert PAUSED_STAGE == "paused_battery"


def test_save_is_atomic_and_thread_safe(tmp_path):
    store = JobStore(tmp_path / "jobs")
    job = store.create(request())

    def worker(k):
        for i in range(30):
            job.data["progress"] = (k * 30 + i) / 1000
            store.save(job)
            store.log(job, "tick", k=k, i=i)
    threads = [threading.Thread(target=worker, args=(k,)) for k in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert json.loads((job.folder / "job.json").read_text(encoding="utf-8"))["job_id"] == job.job_id
    assert not (job.folder / "job.json.tmp").exists() and len(store.read_log(job, 1000)) == 120


def test_unfinished_jobs_are_found_after_restart(tmp_path):
    store = JobStore(tmp_path / "jobs")
    a, b, c = store.create(request()), store.create(request()), store.create(request())
    b.data["state"] = "done"
    c.data["state"] = "waiting_user"
    store.save(b)
    store.save(c)
    fresh = JobStore(tmp_path / "jobs")
    assert {j.job_id for j in fresh.unfinished()} == {a.job_id, c.job_id}


def test_cleanup_keeps_recent_finished_jobs_and_does_not_follow_the_junction(tmp_path):
    store = JobStore(tmp_path / "jobs")
    target = tmp_path / "template_node_modules"
    target.mkdir()
    (target / "keep.txt").write_text("шаблон", encoding="utf-8")
    jobs = [store.create(request()) for _ in range(4)]
    for k, j in enumerate(jobs):
        j.data["state"] = "done" if k < 3 else "running"
        j.data["created"] = 1000 + k
        store.save(j)
        workspace.make_dir_link(j.folder / "work" / "node_modules", target)
    removed = store.cleanup(keep=1)
    assert set(removed) == {jobs[0].job_id, jobs[1].job_id}
    assert (target / "keep.txt").read_text(encoding="utf-8") == "шаблон"        # junction снят, цель цела
    assert jobs[2].folder.is_dir() and jobs[3].folder.is_dir()                  # свежая завершённая и незавершённая остались


def test_verdict_log_survives_cleanup(tmp_path):
    store = JobStore(tmp_path / "jobs")
    store.append_verdict({"job_id": "j-1", "stage": "review"})
    j = store.create(request())
    j.data["state"] = "done"
    store.save(j)
    store.cleanup(keep=1)
    assert store.verdicts() == [{"job_id": "j-1", "stage": "review"}] and (tmp_path / "jobs" / "qa_verdicts.jsonl").is_file()


def test_prepare_work_copies_the_template_without_demo_and_links_node_modules(tmp_path):
    store = JobStore(tmp_path / "jobs")
    job = store.create(request())
    (job.sources / "clip_h264.mp4").write_bytes(b"video")
    (job.transcript / "words.json").write_text("[]", encoding="utf-8")
    work = workspace.prepare_work(job.folder, DEFAULT)
    for rel in ("src/Main.tsx", "src/custom/index.ts", "scripts/validate-plan.mjs", "public/fonts/OFL.txt", "public/sfx/chime_up.wav",
                "package.json", "qa_zones.json", "plan.schema.json", ".claude/skills/remotion-best-practices/SKILL.md",
                "public/media/clip_h264.mp4", "public/transcript/words.json"):
        assert (work / rel).exists(), rel
    assert not (work / "demo").exists() and not (work / "public" / "demo").exists()
    assert (work / "node_modules" / "zod").exists()                            # junction на template/node_modules
    (work / "src" / "custom" / "Mine.tsx").write_text("// моя вставка", encoding="utf-8")
    workspace.prepare_work(job.folder, DEFAULT)                                  # повтор: правки агента на месте
    assert (work / "src" / "custom" / "Mine.tsx").exists()
    assert workspace.custom_components(work) == {"JournalLog", "FontSpecimen", "PictogramSheet"}


def test_srt_for_a_non_burned_video_comes_from_the_same_words():
    zones = qa_mod.load_zones(DEFAULT)
    plan = make_plan(platform="youtube", duration=14000, captions=False)
    plan["voiceover"][0]["duration_ms"] = 1500
    plan["voiceover"][1]["duration_ms"] = 3500
    text = srt_mod.build_srt(plan, [], zones)
    blocks = text.strip().split("\n\n")
    assert blocks[0].splitlines()[0] == "1" and "00:00:00,300 -->" in blocks[0] and "Протокол" in blocks[0]
    assert len(blocks) >= 2 and all("-->" in b for b in blocks)
    empty = make_plan(platform="youtube", duration=14000, voiceover=False, captions=False)
    assert srt_mod.build_srt(empty, [], zones) == ""

"""Remotion Studio задач и пространство «Монтаж» (ТЗ 15, TAB-4, TAB-9): один Studio на задачу, гашение с задачей и по
простою, объявление запуска событием montage.launch, кнопки одобрения, блоки панели. Процессы и сеть подменены."""
import asyncio
import json
import os

import pytest
from gits_sdk import ModuleClient, ToolError

import main as main_mod
from manifest import build_manifest
from studio import StudioError, StudioPool
from tests.stubs import FakeRenderer, FakeRunner, StubQA, layout_for


class FakeProc:
    _pid = 100

    def __init__(self):
        FakeProc._pid += 1
        self.pid = FakeProc._pid
        self.returncode = None

    async def wait(self):
        if self.returncode is None:
            self.returncode = -9
        return self.returncode

    def die(self, code=1):
        self.returncode = code


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def make_pool(clock=None, ready_ok=True, idle_s=1800):
    spawned = []
    ports = iter(range(41000, 41100))

    async def spawn(npx, work, port):
        proc = FakeProc()
        spawned.append((npx, str(work), port, proc))
        return proc

    async def ready(url, proc):
        if not ready_ok:
            raise StudioError("не ответил")

    pool = StudioPool(npx=lambda: "npx", clock=clock or Clock(), idle_s=idle_s, spawn=spawn, ready=ready,
                      port=lambda: next(ports))
    return pool, spawned


async def test_one_studio_per_job_and_its_props(tmp_path):
    pool, spawned = make_pool()
    work = tmp_path / "work"
    work.mkdir()
    url, started = await pool.open("j-00000001", work)
    again, started2 = await pool.open("j-00000001", work)
    assert started is True and started2 is False and url == again
    assert url.startswith("http://127.0.0.1:") and len(spawned) == 1
    npx, cwd, port, _proc = spawned[0]
    assert cwd == str(work) and url.endswith(str(port))
    other, _ = await pool.open("j-00000002", work)
    assert other != url and len(spawned) == 2                      # у каждой задачи свой порт


async def test_missing_work_dir_is_refused(tmp_path):
    pool, spawned = make_pool()
    with pytest.raises(StudioError):
        await pool.open("j-00000001", tmp_path / "нет-такой")
    assert spawned == []


async def test_studio_that_does_not_start_is_killed_and_forgotten(tmp_path):
    pool, spawned = make_pool(ready_ok=False)
    work = tmp_path
    with pytest.raises(StudioError):
        await pool.open("j-00000001", work)
    assert pool.running() == [] and spawned[0][3].returncode is not None


async def test_stop_on_job_end_and_unknown_job(tmp_path):
    pool, spawned = make_pool()
    await pool.open("j-00000001", tmp_path)
    assert await pool.stop("j-00000001") is True
    assert spawned[0][3].returncode is not None and pool.running() == []
    assert await pool.stop("j-00000001") is False


async def test_idle_studio_is_stopped_by_reaper(tmp_path):
    clock = Clock()
    pool, spawned = make_pool(clock=clock, idle_s=60)
    await pool.open("j-00000001", tmp_path)
    clock.now += 30
    await pool.open("j-00000002", tmp_path)                       # вторая задача позже
    clock.now += 45                                               # первая простаивает 75 с, вторая 45 с
    assert await pool.reap() == ["j-00000001"]
    assert pool.running() == ["j-00000002"]


async def test_dead_studio_is_forgotten_by_reaper(tmp_path):
    pool, spawned = make_pool()
    await pool.open("j-00000001", tmp_path)
    spawned[0][3].die(0)
    assert await pool.reap() == ["j-00000001"]


async def test_stop_all_clears_everything(tmp_path):
    pool, _ = make_pool()
    await pool.open("j-00000001", tmp_path)
    await pool.open("j-00000002", tmp_path)
    await pool.stop_all()
    assert pool.running() == []


# ---------------------------------------------------------------- пространство «Монтаж» на стенде менеджера


def manager_for(tmp_path):
    layout = layout_for(tmp_path)
    client = ModuleClient(build_manifest())
    client, manager = main_mod.build(layout, client=client, runner=FakeRunner(), renderer=FakeRenderer(), qa=StubQA())
    return client, manager


def make_job(manager, title="Ролик про куб", state="queued", plan=False):
    job = manager.store.create({"brief": title, "sources": []}, title=title)
    job.data["state"] = state
    if plan:
        (job.plan_dir / "plan.json").write_text(json.dumps({"scenes": []}), encoding="utf-8")
    manager.store.save(job)
    return job


async def test_panel_lists_jobs_with_studio_and_approve_buttons(tmp_path):
    client, manager = manager_for(tmp_path)
    queued = make_job(manager, "Первый", "queued", plan=True)
    asking = make_job(manager, "Второй", "waiting_user", plan=True)
    blocks = manager.panel_blocks()
    assert [b["type"] for b in blocks][:3] == ["stats", "table", "text"]
    rows = {r["cells"][0]: r for r in blocks[1]["rows"]}
    assert [a["id"] for a in rows["Первый"]["actions"]] == ["open_studio"]     # есть план, ждать ответа не надо
    assert [a["id"] for a in rows["Второй"]["actions"]] == ["open_studio"]     # ждёт ответа, но не одобрен: без кнопки «Одобрить»
    assert queued.job_id != asking.job_id


async def test_open_studio_needs_a_plan_and_announces_the_first_start(tmp_path, monkeypatch):
    client, manager = manager_for(tmp_path)
    no_plan = make_job(manager, "Без плана", "queued", plan=False)
    with pytest.raises(ToolError):
        await manager.open_studio(no_plan.job_id)
    job = make_job(manager, "С планом", "queued", plan=True)
    events = []

    async def record(kind, text, **fields):
        events.append((kind, text, fields))
        return True

    async def fake_open(job_id, work):
        return ("http://127.0.0.1:41234", job_id == job.job_id and not any(e for e in events))

    monkeypatch.setattr(manager.core, "event", record)
    monkeypatch.setattr(manager.studios, "open", fake_open)
    first = await manager.open_studio(job.job_id)
    assert first["url"] == "http://127.0.0.1:41234"
    assert events and events[0][0] == "montage.launch" and events[0][2]["announce"] is True
    assert "Remotion Studio" in events[0][1] and "рендера не будет" in events[0][1]
    await manager.open_studio(job.job_id)                          # повторный вызов: адрес тот же, без нового объявления
    assert len(events) == 1                                        # объявлено один раз


async def test_done_job_shows_video_and_report_excerpt(tmp_path):
    client, manager = manager_for(tmp_path)
    job = make_job(manager, "Готовый", "done", plan=True)
    final = job.renders / "final.mp4"
    final.write_bytes(b"0" * 10)
    job.qa.mkdir(parents=True, exist_ok=True)
    report = job.qa / "report_final.md"
    report.write_text("# Отчёт\nзамечаний нет " * 10, encoding="utf-8")
    job.add_artifact(final, "final")
    job.add_artifact(report, "qa_report_final")
    manager.store.save(job)
    blocks = manager.panel_blocks()
    video = next(b for b in blocks if b["type"] == "video")
    assert video["artifact"] == {"job": job.job_id, "name": "final.mp4"}
    report_block = next(b for b in blocks if b.get("title") == "Отчёт проверки")
    assert report_block["text"].startswith("# Отчёт") and len(report_block["text"]) <= 2500


def test_state_data_carries_folders_for_panel_links(tmp_path):
    """publish_state отдаёт jobs_dir и output_dir: по ним ядро открывает папки из пространства (path_from)."""
    client, manager = manager_for(tmp_path)
    captured = {}

    async def grab(summary, data):
        captured.update(data)

    client.state = grab
    asyncio.run(manager.publish_state())
    assert captured["jobs_dir"] == os.path.abspath(str(tmp_path / "jobs"))
    assert captured["output_dir"]

"""Модуль целиком против FakeCore из SDK (MON-5, MON-7, MON-8, MON-10, MON-12, MON-31, MON-33): настоящий ModuleClient, настоящий
WebSocket; агент, Remotion и контроль качества — заглушки."""
import asyncio
import json
import types

import pytest
from glados_module import ToolError
from glados_module.testing import FakeCore

import main as main_mod
from layout import DEFAULT
from manifest import build_manifest
from tests.stubs import STORY, FakeRenderer, FakeRunner, StubQA, layout_for, write_silence_wav

pytestmark = pytest.mark.usefixtures("fast")


class Env:
    def __init__(self, tmp, core: FakeCore, **kw):
        self.tmp = tmp
        self.core = core
        self.layout = layout_for(tmp)
        self.runner = kw.pop("runner", None) or FakeRunner()
        self.renderer = kw.pop("renderer", None) or FakeRenderer()
        from glados_module import ModuleClient
        self.client = ModuleClient(build_manifest(), url=core.url, token=core.token)
        self.client, self.manager = main_mod.build(self.layout, client=self.client, runner=self.runner, renderer=self.renderer, qa=StubQA())
        self.task = None

    async def __aenter__(self):
        self.task = asyncio.create_task(main_mod.run(self.client, self.manager))
        await self.core.wait_connected()
        await self.core.wait_for(lambda: self.core.states.get("montage") is not None, 10)
        return self

    async def __aexit__(self, *exc):
        await self.client.stop()
        await asyncio.wait_for(self.task, 20)

    def jobs(self):
        return self.core.messages_of("job.update")


def core_with_handlers(tmp, **kw):
    core = FakeCore(module_settings={"output_dir": str(tmp / "out")}, **kw)
    kb = []

    @core.on_request("core.persona")
    def persona(body):
        return {"name": "ГЛаДОС", "card": "Сухая учтивость."}

    @core.on_request("core.tts")
    def tts(body):
        seconds = max(0.8, len(body["text"]) * 0.06)
        path = tmp / f"tts_{len(kb)}.wav"
        kb.append(1)
        write_silence_wav(path, seconds)
        return {"path": str(path), "duration_ms": int(seconds * 1000)}

    core.ingested = []

    @core.on_request("core.kb.ingest")
    def ingest(body):
        core.ingested.append(body)
        return {"added": len(body["documents"]), "updated": 0, "skipped": 0}

    core.experience = []

    @core.on_request("core.experience.add")
    def exp(body):
        core.experience.append(body)
        return {"id": "e1", "merged": False}

    @core.on_request("core.claude_usage")
    def usage(body):
        return {}
    return core


START = {"sources": [], "brief": "20 секунд: ГЛаДОС объясняет правило", "source_kind": "narrated", "platform": "shorts", "duration_s": 12}


def last_update(core, job_id):
    return [m.body for m in core.messages_of("job.update") if m.body["job_id"] == job_id][-1]


async def answer_when_asked(core, job_id, answers, seen=()):
    """Отвечает на вопросы задачи: берёт только текущий вопрос (последний job.update в состоянии waiting_user)."""
    seen = set(seen)
    for answer in answers:
        def current():
            ups = [m.body for m in core.messages_of("job.update") if m.body["job_id"] == job_id]
            last = ups[-1] if ups else None
            if last and last.get("state") == "waiting_user" and last["question"]["question_id"] not in seen:
                return last
            return None
        q = await core.wait_for(current, 60)
        seen.add(q["question"]["question_id"])
        await core.job_answer(job_id, q["question"]["question_id"], answer)


async def test_job_through_the_real_protocol(tmp_path):
    core = core_with_handlers(tmp_path)
    async with core, Env(tmp_path, core):
        value = await core.call_tool("start", START)
        job_id = value["job_id"]
        assert job_id.startswith("j-")
        await answer_when_asked(core, job_id, ["утвердить", "готово"])
        await core.wait_for(lambda: last_update(core, job_id)["state"] == "done", 120)
        final = last_update(core, job_id)
        assert final["progress"] == 1.0 and any(a["kind"] == "final" for a in final["artifacts"])
        assert (tmp_path / "out" / "final_shorts.mp4").is_file()
        await core.wait_for(lambda: any(e["kind"] == "montage.done" for e in core.events), 10)
        kinds = [e["kind"] for e in core.events]
        assert "montage.launch" in kinds and "montage.done" in kinds
        launch = next(e for e in core.events if e["kind"] == "montage.launch")
        assert launch["announce"] is True and "финальный рендер" in launch["text"]
        # MON-31: регламент ушёл в справочник один раз
        await core.wait_for(lambda: core.ingested, 10)
        assert core.ingested[0]["collection"] == "montage.style" and core.ingested[0]["replace"] and core.ingested[0]["final"]
        assert {d["uri"] for d in core.ingested[0]["documents"]} == {"montage:style", "montage:craft"}
        # MON-33: state
        await core.wait_for(lambda: core.states["montage"]["data"]["current"] is None and "ничего не выполняется" in core.states["montage"]["summary"], 10)
        status = await core.call_tool("status", {})
        assert job_id in status["text"] and status["jobs"][0]["state"] == "done"
        assert [j["job_id"] for j in await core.job_list()] == [job_id]


async def test_start_checks(tmp_path, monkeypatch):
    core = core_with_handlers(tmp_path)
    async with core, Env(tmp_path, core) as env:
        with pytest.raises(ToolError) as e:
            await core.call_tool("start", {**START, "sources": [str(tmp_path / "нет.mp4")]})
        assert e.value.code == "bad_request" and "нет такого файла" in e.value.message
        with pytest.raises(ToolError) as e:
            await core.call_tool("start", {**START, "sources": ["relative/path.mp4"]})
        assert "абсолютным" in e.value.message
        with pytest.raises(ToolError) as e:
            await core.call_tool("start", {**START, "brief": "  "})
        assert e.value.code == "bad_request"
        with pytest.raises(ToolError) as e:
            await core.call_tool("start", {**START, "platform": "tiktok"})
        assert e.value.code == "bad_request"
        # место на диске: нужно 5 × размер исходников
        src = tmp_path / "big.bin"
        src.write_bytes(b"x" * 1000)
        monkeypatch.setattr("manager.shutil.disk_usage", lambda p: types.SimpleNamespace(free=3000, total=10 ** 9, used=0))
        with pytest.raises(ToolError) as e:
            await core.call_tool("start", {**START, "sources": [str(src)], "source_kind": "generic"})
        assert e.value.code == "unavailable" and "мало места" in e.value.message
        monkeypatch.undo()
        # шрифты (MON-24)
        env.manager.layout = types.SimpleNamespace(missing_fonts=lambda: ["IBMPlexMono-Regular.ttf"], assets=DEFAULT.assets)
        with pytest.raises(ToolError) as e:
            await core.call_tool("start", START)
        assert e.value.code == "unavailable" and "нет шрифта IBMPlexMono-Regular.ttf" in e.value.message
        assert not await core.job_list()                                                   # ни одна задача не поставлена


async def test_start_is_refused_when_claude_is_asleep(tmp_path):
    core = core_with_handlers(tmp_path, battery={"percent": 0, "sleeping": True})
    async with core, Env(tmp_path, core):
        with pytest.raises(ToolError) as e:
            await core.call_tool("start", START)
        assert e.value.code == "unavailable" and e.value.message == "нет заряда Claude"


async def test_cancel_stops_the_running_job_and_queue_goes_on(tmp_path):
    core = core_with_handlers(tmp_path)
    renderer = FakeRenderer()
    renderer.block = asyncio.Event()                                       # рендер «висит»
    async with core, Env(tmp_path, core, renderer=renderer) as env:
        s = {**START}
        a = (await core.call_tool("start", s))["job_id"]
        b = (await core.call_tool("start", s))["job_id"]
        await answer_when_asked(core, a, ["утвердить"])
        await core.wait_for(lambda: last_update(core, a)["stage"] == "preview", 60)
        assert last_update(core, b)["state"] == "queued"                    # одна задача выполняется, остальные ждут
        await core.wait_for(lambda: "задач в очереди 1" in core.states["montage"]["summary"], 10)
        await core.job_cancel(a)
        await core.wait_for(lambda: last_update(core, a)["state"] == "cancelled", 30)
        renderer.block.set()
        await core.wait_for(lambda: last_update(core, b)["state"] == "waiting_user", 60)   # очередь пошла дальше
        await core.job_cancel(b)
        await core.wait_for(lambda: last_update(core, b)["state"] == "cancelled", 30)
        assert (env.layout.jobs / a).is_dir()                                # папка остаётся до уборки
        res = await core.call_tool("cancel", {"job_id": a})
        assert res == {"cancelled": True}


async def test_revise_during_review_and_after_done(tmp_path):
    core = core_with_handlers(tmp_path)
    async with core, Env(tmp_path, core) as env:
        job_id = (await core.call_tool("start", START))["job_id"]
        await answer_when_asked(core, job_id, ["утвердить"])
        await core.wait_for(lambda: last_update(core, job_id)["state"] == "waiting_user" and last_update(core, job_id)["stage"] == "review", 90)
        old = last_update(core, job_id)["question"]["question_id"]
        await core.call_tool("revise", {"job_id": job_id, "notes": "субтитры крупнее"})
        await answer_when_asked(core, job_id, ["готово"], seen={old})                    # после правок снова черновик
        await core.wait_for(lambda: last_update(core, job_id)["state"] == "done", 120)
        builds = [c for c in env.runner.calls if c["stage"] == "build"]
        assert len(builds) == 2 and "субтитры крупнее" in builds[1]["prompt"]
        # правки к готовому: задача возвращается на сборку
        res = await core.call_tool("revise", {"job_id": job_id, "notes": "убрать плашку"})
        assert res["accepted"] is True
        old = last_update(core, job_id)
        await core.wait_for(lambda: last_update(core, job_id)["stage"] == "review" and last_update(core, job_id)["state"] == "waiting_user", 120)
        await answer_when_asked(core, job_id, ["готово"])
        await core.wait_for(lambda: last_update(core, job_id)["state"] == "done", 120)
        assert len([c for c in env.runner.calls if c["stage"] == "build"]) == 3
        with pytest.raises(ToolError) as e:
            await core.call_tool("revise", {"job_id": job_id, "notes": " "})
        assert e.value.code == "bad_request"
        with pytest.raises(ToolError) as e:
            await core.call_tool("revise", {"job_id": "j-00000000", "notes": "х"})
        assert e.value.code == "unknown"


async def test_battery_topic_pauses_between_claude_calls(tmp_path):
    core = core_with_handlers(tmp_path)
    async with core, Env(tmp_path, core) as env:
        async def slow_story(e, prompt, n):
            await asyncio.sleep(0.6)                                          # тема battery придёт во время вызова Claude
            (e.job.plan_dir / "story.json").write_text(json.dumps(STORY, ensure_ascii=False), encoding="utf-8")
        env.runner.override["story"] = slow_story
        job_id = (await core.call_tool("start", START))["job_id"]
        await core.wait_for(lambda: last_update(core, job_id).get("stage") == "story", 30)
        await core.topic("battery", {"percent": 0, "sleeping": True})
        await core.wait_for(lambda: last_update(core, job_id).get("stage") == "paused_battery", 60)
        paused = last_update(core, job_id)
        assert paused["state"] == "queued" and paused["text"] == "пауза: нет заряда Claude"
        calls_at_pause = len(env.runner.calls)
        await asyncio.sleep(0.3)
        assert len(env.runner.calls) == calls_at_pause                       # пока Claude спит, вызовов нет
        await core.topic("battery", {"percent": 70, "sleeping": False})
        await answer_when_asked(core, job_id, ["утвердить", "готово"])
        await core.wait_for(lambda: last_update(core, job_id)["state"] == "done", 120)


async def test_unfinished_jobs_are_recovered_on_start(tmp_path):
    core = core_with_handlers(tmp_path)
    layout = layout_for(tmp_path)
    from tests.rig import Rig
    rig = Rig(tmp_path)
    job = rig.new_job(voiceover="glados")                                   # intake до падения уже выбрал закадр
    job.data.update(state="running", stage="story")
    rig.store.save(job)
    async with core, Env(tmp_path, core) as env:
        await core.wait_for(lambda: any(m.body["job_id"] == job.job_id for m in core.messages_of("job.update")), 30)
        await answer_when_asked(core, job.job_id, ["утвердить", "готово"])
        await core.wait_for(lambda: last_update(core, job.job_id)["state"] == "done", 120)
        assert layout.jobs == env.layout.jobs


async def test_kb_sync_is_skipped_when_the_style_did_not_change(tmp_path):
    core = core_with_handlers(tmp_path)
    async with core, Env(tmp_path, core):
        await core.wait_for(lambda: core.ingested, 10)
        await core.wait_for(lambda: (tmp_path / "jobs" / "kb_state.json").is_file(), 10)
    core2 = core_with_handlers(tmp_path)
    async with core2, Env(tmp_path, core2):
        await asyncio.sleep(0.5)
        assert core2.ingested == []                                          # тот же хеш: повторной загрузки нет

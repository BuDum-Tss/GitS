"""Живой прогон модуля монтажа против FakeCore из SDK: настоящий Claude (по подписке пользователя), настоящий Remotion и ffmpeg.

Ядра GitS здесь нет: синтез голоса (`core.tts`) подменён тоном нужной длины через ffmpeg, персонаж и справочник отвечают
заглушками, вопросы (утвердить план, готово) закрываются автоматически. Нужен для проверки всей цепочки на одном коротком ролике.

    .venv\\Scripts\\python.exe tools\\live_run.py            # прогон «narrated» 20 секунд, площадка shorts
    .venv\\Scripts\\python.exe tools\\live_run.py --smoke    # только проверка запуска агента (несколько токенов)

Результат в `jobs/live/` (папка в .gitignore): `jobs/live/<id>/`, готовые файлы в `jobs/live/live_out/`, сводка `jobs/live/live_summary.json`.
"""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "agent")]

from glados_module import ModuleClient  # noqa: E402
from glados_module.testing import FakeCore  # noqa: E402

import main as main_mod  # noqa: E402
import media  # noqa: E402
from layout import Layout  # noqa: E402
from manifest import build_manifest  # noqa: E402

BRIEF = "20 секунд: ГЛаДОС объясняет подопытному правило безопасности лаборатории"
CARD = ("Ты ГЛаДОС: сухая учтивость, научный протокол, пассивная агрессия, точные наблюдения вместо общих слов. Шутка через поворот "
        "факта, без грубости. Говоришь коротко и по-русски.")


async def run(smoke: bool, models: str, resume: str | None = None) -> int:
    layout = Layout(root=ROOT, jobs=ROOT / "jobs" / "live")           # своя папка: чужие задачи из jobs/ не подхватываются
    out_dir = layout.jobs / "live_out"
    out_dir.mkdir(parents=True, exist_ok=True)
    usage: list[dict] = []
    tts_count = [0]
    core = FakeCore(module_settings={"output_dir": str(out_dir), "plan_model": models, "build_model": models})

    @core.on_request("core.persona")
    def persona(body):
        return {"name": "ГЛаДОС", "card": CARD}

    @core.on_request("core.tts")
    async def tts(body):
        tts_count[0] += 1
        seconds = max(1.0, len(body["text"]) * 0.065)
        path = out_dir / f"tts_{tts_count[0]}.wav"
        await media.synth_tone(path, seconds, freq=180 + 40 * tts_count[0])
        return {"path": str(path), "duration_ms": int(seconds * 1000)}

    @core.on_request("core.claude_usage")
    def claude_usage(body):
        usage.append(body)
        return {}

    @core.on_request("core.kb.ingest")
    def ingest(body):
        return {"added": len(body["documents"]), "updated": 0, "skipped": 0}

    @core.on_request("core.experience.add")
    def experience(body):
        return {"id": "e", "merged": False}

    t0 = time.time()
    async with core:
        client = ModuleClient(build_manifest(), url=core.url, token=core.token)
        client, manager = main_mod.build(layout, client=client)
        task = asyncio.create_task(main_mod.run(client, manager))
        await core.wait_connected()
        if smoke:
            return await smoke_test(manager, layout, usage)
        if resume:                                                           # продолжить упавшую задачу с её стадии (экономит лимит Claude)
            job_id = resume
        else:
            job_id = (await core.call_tool("start", {"sources": [], "brief": BRIEF, "source_kind": "narrated", "platform": "shorts",
                                                     "duration_s": 20}))["job_id"]
        print("задача", job_id, flush=True)
        answered: set[str] = set()
        last = None
        while True:
            await asyncio.sleep(1.0)
            ups = [m.body for m in core.messages_of("job.update") if m.body["job_id"] == job_id]
            cur = ups[-1] if ups else None
            if cur is None:
                continue
            tag = (cur.get("state"), cur.get("stage"), cur.get("text"))
            if tag != last:
                last = tag
                print(f"[{time.time() - t0:6.0f} с] {cur['state']:12s} {cur['stage']:14s} {round((cur.get('progress') or 0) * 100):3d} %  {cur.get('text') or ''}", flush=True)
            if cur["state"] == "waiting_user" and cur["question"]["question_id"] not in answered:
                q = cur["question"]
                answered.add(q["question_id"])
                answer = q["options"][0] if q.get("options") else "ничего"
                print(f"    вопрос: {q['question'][:200]}\n    ответ: {answer}", flush=True)
                await core.job_answer(job_id, q["question_id"], answer)
            if cur["state"] in ("done", "failed", "cancelled"):
                break
        await asyncio.sleep(2)
        job = manager.store.load(job_id)
        summary = {"job_id": job_id, "state": job.state, "seconds": round(time.time() - t0), "tokens": job.data["claude"]["tokens"],
                   "usage_calls": len(usage), "error": job.data.get("error"), "artifacts": job.data.get("artifacts"),
                   "qa": job.data.get("qa"), "output": job.data.get("output"), "folder": str(job.folder)}
        (layout.jobs / "live_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        await client.stop()
        await asyncio.wait_for(task, 30)
        return 0 if job.state == "done" else 1


async def smoke_test(manager, layout, usage) -> int:
    """Один короткий вызов агента: проверка параметров SDK, песочницы и учёта расхода."""
    from agent_tools import JobEnv
    from claude_runner import SdkRunner
    job = manager.store.create({"sources": [], "brief": "smoke", "platform": "none"})
    (job.work / "src").mkdir(parents=True, exist_ok=True)
    (job.folder / "persona.json").write_text(json.dumps({"card": CARD}, ensure_ascii=False), encoding="utf-8")
    env = JobEnv(job=job, store=manager.store, core=manager.core, layout=layout, stage="story")
    res = await SdkRunner(layout).run(env, "story", "Прочитай файл ../../../docs/APERTURE_STYLE.md (первые 20 строк) и ответь одним "
                                      "предложением, как называется стиль. Больше ничего не делай.", model="sonnet", max_turns=6)
    print("ответ:", res.text[:300], "| токены:", res.tokens, "| сессия:", res.session_id, "| ходов:", res.turns)
    print("решения песочницы:", [(r["tool"], r["allowed"]) for r in manager.store.read_log(job) if r["kind"] == "sandbox"])
    print("расход ушёл в ядро:", usage)
    job.data["state"] = "cancelled"                                       # проверочная задача не должна подхватываться как незавершённая
    manager.store.save(job)
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--models", default="sonnet", help="модель для всех стадий (по умолчанию sonnet: экономия лимита)")
    ap.add_argument("--resume", help="id упавшей задачи в jobs/live: вернуть её в очередь и продолжить")
    a = ap.parse_args()
    if a.resume:
        path = ROOT / "jobs" / "live" / a.resume / "job.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data.update(state="queued", error=None)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    sys.exit(asyncio.run(run(a.smoke, a.models, a.resume)))

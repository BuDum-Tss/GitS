"""Процесс модуля: main.py стартует (GITS_VOICE_FAKE=1: звука нет), подключается к FakeCore, озвучивает и выходит по shutdown."""
import asyncio
import os
import sys
from pathlib import Path

from gits_sdk.testing import FakeCore

MODULE_DIR = Path(__file__).resolve().parent.parent
GITS = MODULE_DIR.parent.parent


async def test_main_process_speaks_fake_and_shuts_down(tmp_path):
    mem = tmp_path / "memory"
    mem.mkdir(exist_ok=True)
    async with FakeCore(settings={"voice_muted": False, "volume": 100, "mic": False, "hearing": False,
                                  "dnd": False, "quiet_until": 0.0}) as core:
        env = dict(os.environ)
        env.update(GITS_CORE_URL=core.url, GITS_MODULE_TOKEN=core.token, GITS_MODULE_ID="voice", MEMORY_DIR=str(mem),
                   GITS_SECRETS_FILE=str(tmp_path / "нет-secrets.env"), LOCALAPPDATA=str(tmp_path / "la"),
                   GITS_VOICE_TMP=str(tmp_path / "vtmp"), GITS_VOICE_FAKE="1", GITS_VOICE_FAKE_SEGMENT_MS="150",
                   PYTHONUNBUFFERED="1", PYTHONPATH=str(GITS / "sdk"))
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-u", "main.py", cwd=str(MODULE_DIR), env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            await core.wait_connected(timeout=30)
            hello = core.hellos[0]
            assert hello["module_id"] == "voice" and hello["manifest"]["capabilities"] == ["voice.output", "voice.input"]
            state = (await core.wait_message("state", timeout=30)).body
            assert state["data"]["fake"] is True and state["data"]["mic"] is False
            assert proc.returncode is None

            await core.send("speak", {"utterance_id": "u-aaaa0001", "text": "Первое. Второе."})
            await core.wait_message("event", lambda b: b["kind"] == "voice.speech" and b["data"]["phase"] == "ended", timeout=15)
            phases = [e["data"]["phase"] for e in core.events if e["kind"] == "voice.speech"]
            assert phases == ["started", "segment", "ended"]

            await core.send("speak", {"utterance_id": "u-aaaa0002", "text": "Раз. Два. Три. Четыре."})
            await core.wait_message("event", lambda b: b["kind"] == "voice.speech" and b["data"]["utterance_id"] == "u-aaaa0002"
                                    and b["data"]["phase"] == "started", timeout=15)
            await core.send("speak.stop", {"utterance_id": "u-aaaa0002", "at": "segment_end"})
            end = (await core.wait_message("event", lambda b: b["kind"] == "voice.speech" and b["data"]["utterance_id"] == "u-aaaa0002"
                                           and b["data"]["phase"] == "ended", timeout=15)).body["data"]
            assert end["completed"] is False and end["reason"] == "stop" and end["spoken_upto"] >= 1

            res = await core.request("tts.synthesize", {"text": "Проверка", "format": "wav"}, timeout=15)
            assert res["path"].startswith(str(tmp_path / "vtmp")) and res["duration_ms"] > 0 and os.path.exists(res["path"])

            await core.shutdown("тест")
            code = await asyncio.wait_for(proc.wait(), 15)
            assert code == 0
        finally:
            if proc.returncode is None:
                proc.kill()
            out = (await proc.stdout.read()).decode("utf-8", "replace") if proc.stdout else ""
    assert "Traceback" not in out, out

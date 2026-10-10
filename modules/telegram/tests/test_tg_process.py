"""Процесс модуля: main.py стартует, подключается к ядру (FakeCore), честно сообщает «не настроен» и выходит по shutdown.

Секреты не читаются: файл секретов заменён несуществующим, переменных TELEGRAM_* нет, Telegram не затрагивается.
"""
import asyncio
import os
import sys
from pathlib import Path

from glados_module.testing import FakeCore

MODULE_DIR = Path(__file__).resolve().parent.parent
GITS = MODULE_DIR.parent.parent


async def test_main_process_connects_reports_not_configured_and_shuts_down(tmp_path):
    mem = tmp_path / "memory"
    mem.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("TELEGRAM_")}
    async with FakeCore() as core:
        @core.on_request("core.privacy_terms")
        def terms(body):
            return {"terms": ["Анна"]}

        env.update(GLADOS_CORE_URL=core.url, GLADOS_MODULE_TOKEN=core.token, GLADOS_MODULE_ID="telegram",
                   MEMORY_DIR=str(mem), GLADOS_SECRETS_FILE=str(tmp_path / "нет-secrets.env"),
                   LOCALAPPDATA=str(tmp_path / "la"), PYTHONUNBUFFERED="1", PYTHONPATH=str(GITS / "sdk"))
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-u", "main.py", cwd=str(MODULE_DIR), env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            await core.wait_connected(timeout=30)
            hello = core.hellos[0]
            assert hello["module_id"] == "telegram" and hello["manifest"]["capabilities"] == ["chat.channel"]
            state = (await core.wait_message("state", timeout=30)).body
            assert state["summary"] == "канал: не настроен, аккаунт: нет ключей, последний пост: ещё не было"
            assert state["data"] == {"channel_ok": False, "account_ok": False, "last_post_ts": 0.0, "unread": 0}
            await core.wait_for(lambda: core.messages_of("core.privacy_terms"), timeout=30)    # список личных слов запрошен
            assert proc.returncode is None                                                      # не упал
            danger = await core.check_tool("send", {"chat": "Маша", "text": "привет, Анна"})
            assert danger is not None
            try:
                await core.call_tool("post", {})
            except Exception as e:                                                              # честный отказ, а не сбой
                assert "не настроен" in str(e)
            else:
                raise AssertionError("post без настройки должен отказать")
            await core.shutdown("тест")
            code = await asyncio.wait_for(proc.wait(), 15)
            assert code == 0
        finally:
            if proc.returncode is None:
                proc.kill()
            out = (await proc.stdout.read()).decode("utf-8", "replace") if proc.stdout else ""
    assert "Traceback" not in out, out

"""`main.build` и `main.run` в режиме аренды: `lease_host.serve` вызывает их правильно, процесс модуля целиком берёт и отпускает аренду.
Без игры и Docker: ядро это `FakeCore` из SDK."""
import asyncio
import os
import socket
import sys
from pathlib import Path

import pytest
from gits_sdk.control import LeaseClient
from gits_sdk.testing import FakeCore

import gamenet
import lease_host

AGENT = Path(__file__).resolve().parent.parent
CONTROL_TOKEN = "control-token-test"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_serve_calls_build_with_lease_args_and_run_with_its_result():
    calls = []

    def build_fn(url=None, token=None):
        calls.append(("build", url, token))
        return "client", "ctx"

    async def run_fn(client, ctx):
        calls.append(("run", client, ctx))
        await asyncio.sleep(30)

    class Stub:                                              # клиент шины, у которого нечего ждать
        pass
    stub = Stub()
    stub.on_disconnect = lambda fn: fn
    stub.stop = lambda: asyncio.sleep(0)
    stub.wait_connected = lambda timeout: asyncio.sleep(0)

    def build_stub(url=None, token=None):
        calls.append(("build", url, token))
        return stub, "ctx"

    port = free_port()
    env = {"GITS_CONTROL_TOKEN": CONTROL_TOKEN, "GITS_CONTROL_PORT": str(port)}
    server = asyncio.create_task(lease_host.serve(build_stub, run_fn, manifest={"id": "rimworld"}, env=env))
    lc = LeaseClient(f"http://127.0.0.1:{port}", CONTROL_TOKEN)
    for _ in range(100):
        try:
            await lc.info()
            break
        except Exception:
            await asyncio.sleep(0.05)
    got = await lc.acquire(character="glados", core_url="ws://core.test/ws", token="core-secret", purpose="play")
    assert got["ok"]
    await asyncio.sleep(0.1)
    assert calls[0] == ("build", "ws://core.test/ws", "core-secret") and calls[1] == ("run", stub, "ctx")
    assert (await lc.release(got["lease_id"]))["released"] is True
    server.cancel()
    await asyncio.gather(server, return_exceptions=True)


@pytest.mark.skipif(gamenet.port_open(), reason="RimWorld запущена: процесс модуля начал бы играть по-настоящему")
async def test_module_process_in_lease_mode_connects_on_acquire_and_survives_release(tmp_path):
    own_memory, core_memory = tmp_path / "module-memory", tmp_path / "core-memory"
    own_memory.mkdir()
    core_memory.mkdir()
    port = free_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("RIMWORLD_", "GLADOS_"))}
    env["GITS_NO_LAUNCH"] = "1"
    async with FakeCore(settings={"streamer": False, "dnd": False, "quiet_until": 0.0, "voice_muted": False,
                                  "record_runs": False}) as core:
        env.update(GITS_CONTROL_TOKEN=CONTROL_TOKEN, GITS_RIMWORLD_CONTAINER="1", GITS_CONTROL_PORT=str(port),
                   MEMORY_DIR=str(core_memory), RIMWORLD_MEMORY_DIR=str(own_memory), RIMWORLD_MODULE_PORT=str(free_port()),
                   CHARACTER_FAST_URL="http://127.0.0.1:9/v1/chat/completions", GITS_DECIDER_URL="http://127.0.0.1:9",
                   PYTHONUNBUFFERED="1", LOCALAPPDATA=str(tmp_path / "la"))
        env.pop("GITS_TOKEN", None)
        proc = await asyncio.create_subprocess_exec(sys.executable, "-u", "main.py", cwd=str(AGENT), env=env,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            lc = LeaseClient(f"http://127.0.0.1:{port}", CONTROL_TOKEN)
            for _ in range(600):
                try:
                    info = await lc.info()
                    break
                except Exception:
                    assert proc.returncode is None
                    await asyncio.sleep(0.2)
            assert info["id"] == "rimworld" and core.connections == 0           # без аренды к ядру не подключается
            got = await lc.acquire(character="glados", core_url=core.url, token=core.token, purpose="play")
            assert got["ok"]
            await core.wait_connected(timeout=60)
            assert core.hellos[0]["id"] == "rimworld"
            state = (await core.wait_message("state", timeout=30)).body
            assert state["data"]["game_running"] is False
            assert (await lc.release(got["lease_id"]))["released"] is True
            await asyncio.sleep(1.0)
            assert proc.returncode is None                                        # сервис жив и ждёт следующей аренды
            assert (await lc.status())["state"] == "free"
        finally:
            if proc.returncode is None:
                proc.terminate()
            try:
                await asyncio.wait_for(proc.communicate(), 20)
            except asyncio.TimeoutError:
                proc.kill()

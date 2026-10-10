"""Супервизор процессов модулей (ТЗ 02, CORE-12…CORE-17): запуск, перезапуск с задержками, остановка после сбоев, выключение."""
import asyncio
import os
import sys
import textwrap

import psutil
import pytest

import supervisor
from supervisor import ModuleSpec, Supervisor, load_specs, redact

TOKEN = "ab" * 32            # 64 hex, как настоящий токен ядра

# «хороший» модуль: печатает окружение, запускает потомка и работает до флага остановки
OK = textwrap.dedent('''
    import os, subprocess, sys, time
    from pathlib import Path
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    Path("child.pid").write_text(str(child.pid))
    print("module_id=" + os.environ["GLADOS_MODULE_ID"], "url=" + os.environ["GLADOS_CORE_URL"],
          "token=" + ("yes" if len(os.environ.get("GLADOS_MODULE_TOKEN", "")) == 64 else "no"),
          "unbuffered=" + os.environ.get("PYTHONUNBUFFERED", ""), "sdk=" + str("sdk" in os.environ.get("PYTHONPATH", "")))
    while not Path("stop.flag").exists():
        time.sleep(0.05)
    child.terminate()
''')
CRASH = textwrap.dedent('''
    import sys
    print("не получилось подключиться")
    print("Authorization: Bearer " + "f" * 20)
    print("token=" + "a1" * 32)
    sys.exit(1)
''')
EXIT0 = 'print("всё сделал")'
STUBBORN = textwrap.dedent('''
    import subprocess, sys, time
    from pathlib import Path
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    Path("child.pid").write_text(str(child.pid))
    print("упрямый запущен", flush=True)
    time.sleep(120)
''')


def make(tmp_path, script, *, restart="on-failure", command="{PYTHON}", cwd=None, enabled=True, **kw):
    mod = tmp_path / "mod"
    mod.mkdir(exist_ok=True)
    (mod / "main.py").write_text(script, encoding="utf-8")

    async def shutdown(module_id):
        (mod / "stop.flag").write_text("1")
        return True
    spec = ModuleSpec(id="fake", cwd=cwd or str(mod), command=command, args=["-u", "main.py"], restart=restart)
    flags = {"enabled": enabled, "connected": False}
    sup = Supervisor([spec], token=TOKEN, core_url="ws://127.0.0.1:1/module/ws", enabled=lambda i: flags["enabled"],
                     is_connected=lambda i: flags["connected"], send_shutdown=kw.pop("send_shutdown", shutdown),
                     gits_root=tmp_path / "gits", agent_dir=tmp_path, logs_dir=tmp_path / "logs", **kw)
    return sup, mod, flags


async def wait_for(predicate, timeout=10.0):
    async def waiter():
        while True:
            if predicate():
                return
            await asyncio.sleep(0.02)
    await asyncio.wait_for(waiter(), timeout)


def log_text(sup):
    return sup.log_path("fake").read_text(encoding="utf-8")


async def test_start_passes_environment_writes_log_and_stops_on_shutdown(tmp_path):
    sup, mod, _ = make(tmp_path, OK)
    await sup.start()
    await wait_for(lambda: (mod / "child.pid").exists() and "token=yes" in (sup.log_path("fake").read_text(encoding="utf-8") if sup.log_path("fake").exists() else ""))
    st = sup.status("fake")
    assert st["status"] == "running" and st["pid"] and st["configured"]
    text = log_text(sup)
    assert "module_id=fake" in text and "url=ws://127.0.0.1:1/module/ws" in text and "unbuffered=1" in text
    assert "sdk=True" in text                         # SDK виден процессу через PYTHONPATH (до установки пакета)
    assert TOKEN not in text                          # токен в журнал не попадает
    pid = st["pid"]
    await sup.stop("fake")                            # shutdown -> сам вышел (флаг) -> не убит
    assert sup.status("fake")["status"] == "stopped" and not psutil.pid_exists(pid)


async def test_crash_restarts_with_backoff_then_gives_up_after_five(tmp_path):
    delays = []

    async def fake_sleep(s):
        delays.append(s)
    ticks = iter(range(0, 1000, 10))                  # 10 с между сбоями: все в одном десятиминутном окне
    sup, mod, _ = make(tmp_path, CRASH, sleep=fake_sleep, clock=lambda: next(ticks))
    import events_log
    await sup.start()
    await wait_for(lambda: sup.status("fake")["status"] == "crashed")
    assert delays == [2, 5, 15, 60]                   # CORE-15; пятое падение - конец
    st = sup.status("fake")
    assert st["restarts"] == 4 and st["text"] == "остановлен после сбоев"
    assert "не получилось подключиться" in st["last_error"]
    assert "Bearer" not in st["last_error"] and ("a1" * 32) not in st["last_error"]      # похожее на токен вырезано
    assert "Bearer" not in log_text(sup) and ("a1" * 32) not in log_text(sup) and "***" in log_text(sup)
    assert any("остановлен после" in e["text"] for e in events_log.tail(50, {"module"}))
    # «перезапустить» сбрасывает счётчик и запускает заново
    delays.clear()
    await sup.restart("fake")
    assert sup.status("fake")["restarts"] == 0
    await wait_for(lambda: len(delays) == 4 and sup.status("fake")["status"] == "crashed")
    assert delays == [2, 5, 15, 60]
    await sup.stop_all()


async def test_failures_older_than_ten_minutes_do_not_count(tmp_path):
    delays = []
    holder = {}
    clock_t = [0.0]

    def clock():
        clock_t[0] += 700                              # между сбоями больше окна 600 с
        return clock_t[0]

    async def fake_sleep(s):
        delays.append(s)
        if len(delays) >= 7:
            holder["sup"].state["fake"].desired = False
    sup, mod, _ = make(tmp_path, CRASH, sleep=fake_sleep, clock=clock)
    holder["sup"] = sup
    await sup.start()
    await wait_for(lambda: len(delays) >= 7 and sup.state["fake"].task.done())
    assert delays == [2] * 7 and sup.status("fake")["status"] != "crashed"


async def _runs_once_and_stops(tmp_path, policy, script):
    sup, mod, _ = make(tmp_path, script, restart=policy)
    await sup.start()
    await wait_for(lambda: sup.state["fake"].task.done())
    return sup.status("fake")


async def test_restart_policy_never_and_clean_exit_are_not_restarted(tmp_path):
    for policy, script in (("never", CRASH), ("on-failure", EXIT0)):
        st = await _runs_once_and_stops(tmp_path, policy, script)
        assert st["status"] == "stopped" and st["restarts"] == 0
    sup, mod, _ = make(tmp_path, EXIT0, restart="always", sleep=lambda s: asyncio.sleep(0))
    await sup.start()
    await wait_for(lambda: sup.status("fake")["status"] == "crashed")                   # always: даже чистый выход - перезапуск
    assert sup.status("fake")["restarts"] == 4


async def test_stubborn_process_tree_is_killed_after_wait(tmp_path):
    async def no_shutdown(module_id):
        return True                                    # shutdown послан, модуль его игнорирует
    sup, mod, _ = make(tmp_path, STUBBORN, send_shutdown=no_shutdown, shutdown_wait=0.4)
    await sup.start()
    await wait_for(lambda: (mod / "child.pid").exists() and "упрямый запущен" in (log_text(sup) if sup.log_path("fake").exists() else ""))
    pid, child = sup.status("fake")["pid"], int((mod / "child.pid").read_text())
    assert psutil.pid_exists(child)
    t0 = asyncio.get_running_loop().time()
    await sup.stop("fake")
    assert asyncio.get_running_loop().time() - t0 < 5
    await wait_for(lambda: not psutil.pid_exists(pid) and not psutil.pid_exists(child))   # убито дерево, потомок тоже
    assert sup.status("fake")["status"] == "stopped"


async def test_disable_and_enable_via_apply(tmp_path):
    sup, mod, flags = make(tmp_path, OK)
    await sup.start()
    await wait_for(lambda: sup.status("fake")["status"] == "running")
    flags["enabled"] = False
    await sup.apply("fake")                           # CORE-14: выключили -> shutdown -> процесс завершён
    assert sup.status("fake")["status"] == "stopped" and sup.status("fake")["pid"] is None
    (mod / "stop.flag").unlink()
    flags["enabled"] = True
    await sup.apply("fake")
    await wait_for(lambda: sup.status("fake")["status"] == "running")
    await sup.stop_all()                              # CORE-17: остановка ядра
    assert sup.status("fake")["status"] == "stopped"


async def test_disabled_module_is_not_started_at_boot(tmp_path):
    sup, mod, _ = make(tmp_path, OK, enabled=False)
    await sup.start()
    assert sup.status("fake")["status"] == "stopped" and sup.state["fake"].task is None
    await sup.restart("fake")                          # выключенный не запускается и по «перезапустить»
    assert sup.state["fake"].task is None


async def test_manually_started_module_blocks_second_instance(tmp_path):
    sup, mod, flags = make(tmp_path, OK, poll_manual=0.05)
    flags["connected"] = True
    await sup.start()
    await wait_for(lambda: sup.status("fake")["status"] == "manual")
    assert sup.status("fake")["pid"] is None and not (mod / "child.pid").exists()
    flags["connected"] = False                        # ручной экземпляр отключился -> супервизор запускает свой
    await wait_for(lambda: sup.status("fake")["status"] == "running")
    await sup.stop_all()


async def test_missing_variable_marks_not_configured(tmp_path):
    sup, mod, _ = make(tmp_path, OK, cwd="{RIMWORLD_MODULE_DIR_X}/agent", env={})
    await sup.start()
    await wait_for(lambda: sup.state["fake"].task.done())
    st = sup.status("fake")
    assert st["status"] == "not_configured" and not st["configured"] and "RIMWORLD_MODULE_DIR_X" in st["reason"]
    assert "не настроен" in st["text"]
    sup2, mod2, _ = make(tmp_path, OK, cwd="{GITS}/нет-такой-папки")
    assert "папка модуля не найдена" in sup2.resolve("fake")
    sup3, _, _ = make(tmp_path, OK, cwd="{MY_DIR}", env={"MY_DIR": str(tmp_path / "mod")})
    cmd, cwd = sup3.resolve("fake")
    assert cwd == tmp_path / "mod" and cmd == [sys.executable, "-u", "main.py"]


async def test_missing_command_falls_back_to_core_python_with_warning(tmp_path, caplog):
    sup, mod, _ = make(tmp_path, EXIT0, command=str(tmp_path / "нет" / "python.exe"))
    cmd, _cwd = sup.resolve("fake")
    assert cmd[0] == sys.executable
    assert any("запускаю тем же Python" in r.message for r in caplog.records)


async def test_relative_cwd_is_relative_to_agent_dir(tmp_path):
    (tmp_path / "sub").mkdir()
    sup, mod, _ = make(tmp_path, EXIT0, cwd="sub")
    assert sup.resolve("fake")[1] == tmp_path / "sub"


def test_load_specs_and_defaults(tmp_path):
    f = tmp_path / "modules.yaml"
    f.write_text(textwrap.dedent('''
        modules:
          - id: a
            cwd: "{GITS}/x"
            command: "{PYTHON}"
            args: ["-u", "main.py"]
          - id: b
            cwd: .
            command: py
            restart: sometimes
          - {nonsense: 1}
          - id: c
            cwd: .
            command: py
            autostart: false
            enabled: false
            restart: never
    '''), encoding="utf-8")
    specs = {s.id: s for s in load_specs(f)}
    assert set(specs) == {"a", "c"}                                       # неверные записи пропущены
    assert specs["a"].restart == "on-failure" and specs["a"].autostart and specs["a"].enabled
    assert not specs["c"].autostart and not specs["c"].enabled and specs["c"].restart == "never"
    assert load_specs(tmp_path / "нет.yaml") == []
    echo = {s.id: s for s in load_specs()}["echo"]                        # настоящий agent/modules.yaml
    assert echo.enabled is False and echo.args == ["-u", "echo_module.py"] and echo.cwd.endswith("sdk/examples")


def test_redact_hides_token_like_text():
    assert redact("ключ " + "ab" * 32 + " конец") == "ключ *** конец"
    assert redact("Authorization: Bearer abc.def") == "Authorization: ***"
    assert redact("обычная строка 12345") == "обычная строка 12345"


async def test_real_echo_module_command_resolves_inside_repo():
    sup = Supervisor(load_specs(), token=TOKEN, core_url="ws://x")
    cmd, cwd = sup.resolve("echo")
    assert cmd == [sys.executable, "-u", "echo_module.py"] and (cwd / "echo_module.py").is_file()
    env = sup._process_env("echo")
    assert env["GLADOS_MODULE_ID"] == "echo" and env["GLADOS_MODULE_TOKEN"] == TOKEN
    assert os.path.join(str(supervisor.GITS_ROOT), "sdk") in env["PYTHONPATH"]


@pytest.fixture(autouse=True)
def _cleanup_children():
    yield
    for p in psutil.Process().children(recursive=True):               # на случай упавшего теста: не оставлять процессы
        try:
            p.kill()
        except psutil.Error:
            pass

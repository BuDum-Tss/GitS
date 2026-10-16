"""Окружение модулей и общие функции SDK (ТЗ 31, MC-6, MC-4): супервизор передаёт то, что записано в контракте;
функции SDK, заменившие внутренности ядра, совпадают с ядром."""
from pathlib import Path

import latency_trace
import safety
from gits_sdk import safety as sdk_safety
from gits_sdk import trace as sdk_trace
from gits_sdk.contract import environment as E
from supervisor import Supervisor, load_specs


def test_supervisor_passes_every_declared_variable(tmp_path):
    specs = [s for s in load_specs() if s.kind != "service"][:1]
    sup = Supervisor(specs, token="t" * 64, core_url="ws://127.0.0.1:1/module/ws", env={"GITS_CHARACTER_ID": "glados"},
                     gits_root=tmp_path, memory_dir=tmp_path / "mem")
    env = sup._process_env(specs[0].id)
    for name in E.SUPERVISOR_VARS:
        assert env.get(name), f"супервизор не передал {name}"
    assert env["GITS_MODULE_ID"] == specs[0].id and env["GITS_CORE_URL"] == "ws://127.0.0.1:1/module/ws"
    assert Path(env["MEMORY_DIR"]).is_absolute()


def test_environment_table_has_no_duplicates_and_known_origins():
    names = [v.name for v in E.ENV_VARS]
    assert len(names) == len(set(names))
    assert {v.by for v in E.ENV_VARS} <= {E.SUPERVISOR, E.USER, E.INHERITED}


def test_sdk_safety_is_the_core_safety():
    assert safety.danger_of is sdk_safety.danger_of
    root = Path.cwd()
    assert sdk_safety.danger_of("Bash", {"command": "rm -rf /"}, root)
    assert sdk_safety.danger_of("Bash", {"command": "ls"}, root) is None


def test_sdk_trace_wire_matches_latency_trace():
    wire = sdk_trace.wire_new("voice", marks={"mic_end": 12.34567})
    core_wire = latency_trace.wire_new("voice", marks={"mic_end": 12.34567})
    assert set(wire) == set(core_wire) == {"id", "source", "marks"}
    assert wire["marks"] == core_wire["marks"] and wire["source"] == core_wire["source"]
    assert set(sdk_trace.STAGES) == set(latency_trace.STAGES)
    t = latency_trace.from_bus(wire, "voice")                       # ядро принимает трассу модуля
    assert t is not None and t.marks["mic_end"] == 12.346

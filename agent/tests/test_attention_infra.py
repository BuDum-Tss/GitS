"""Окружение менеджера внимания (ТЗ 03): сервисы в супервизоре (ATT-2), инструменты `attention.*` (ATT-24), настройки
(ATT-30), остановка речи питомца, признак «пользователь говорит», дашборд, выгрузка для оценки (ATT-5), запись журнала без
ожидания диска. Процессы настоящие, но игрушечные; сеть и модель не нужны."""
import asyncio
import json
import textwrap
import time
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import web
from test_attention import Rig
from test_supervisor import wait_for

import dashboard
import events_log
import module_registry
import settings
from environments import builtin
from environments.base import ToolError, validate_args
from environments.registry import Registry
from supervisor import ModuleSpec, Supervisor, load_specs

# ── сервис в супервизоре (ATT-2) ──────────────────────────────────────────────────────────────────

SERVICE = textwrap.dedent('''
    import os, time
    print("сервис запущен token=" + ("есть" if os.environ.get("GLADOS_MODULE_TOKEN") else "нет"), flush=True)
    time.sleep(120)
''')
SERVICE_CRASH = 'import sys\nprint("упал", flush=True)\nsys.exit(1)'


class Health:
    """Проверка health_url в памяти: что ответит и сколько раз спросили."""

    def __init__(self, ok=False):
        self.ok, self.calls = ok, 0

    async def __call__(self, url):
        self.calls += 1
        return self.ok


def make_service(tmp_path, script, health, *, command="{PYTHON}", restart="on-failure", **kw):
    mod = tmp_path / "svc"
    mod.mkdir(exist_ok=True)
    (mod / "main.py").write_text(script, encoding="utf-8")
    spec = ModuleSpec(id="decider", cwd=str(mod), command=command, args=["-u", "main.py"], restart=restart,
                      kind="service", health_url="http://127.0.0.1:8768/health")
    sup = Supervisor([spec], token="ab" * 32, core_url="ws://127.0.0.1:1/module/ws", gits_root=tmp_path, agent_dir=tmp_path,
                     logs_dir=tmp_path / "logs", probe=health, health_interval=0.03, **kw)
    return sup


def test_load_specs_reads_kind_and_health_url_and_validates(tmp_path):
    path = tmp_path / "modules.yaml"
    path.write_text(textwrap.dedent('''
        modules:
          - {id: plain, command: x}
          - {id: svc, kind: service, command: x, health_url: "http://127.0.0.1:1/health"}
          - {id: nohealth, kind: service, command: x}
          - {id: strange, kind: daemon, command: x, health_url: "http://127.0.0.1:1/health"}
    '''), encoding="utf-8")
    specs = {s.id: s for s in load_specs(path)}
    assert set(specs) == {"plain", "svc"}                                # неверные записи пропущены
    assert specs["plain"].kind == "module" and specs["plain"].health_url == ""
    assert specs["svc"].kind == "service" and specs["svc"].health_url.endswith("/health")


def test_shipped_modules_yaml_declares_the_decider_service():
    spec = next(s for s in load_specs() if s.id == "decider")
    assert (spec.kind, spec.health_url, spec.autostart, spec.restart) == ("service", "http://127.0.0.1:8768/health", True, "on-failure")
    assert spec.command == "{GITS}/services/decider/.venv/Scripts/python.exe" and spec.cwd == "{GITS}/services/decider"
    assert spec.args == ["decider_server.py"]


async def test_service_is_running_only_when_health_answers_and_gets_no_core_token(tmp_path):
    health = Health(False)
    sup = make_service(tmp_path, SERVICE, health)
    await sup.start()
    await wait_for(lambda: sup.status("decider")["pid"] and "сервис запущен" in (
        sup.log_path("decider").read_text(encoding="utf-8") if sup.log_path("decider").exists() else ""))
    st = sup.status("decider")
    assert st["status"] == "starting" and st["kind"] == "service" and st["healthy"] is False
    await wait_for(lambda: health.calls >= 2)                            # опрос идёт, пока процесс жив
    health.ok = True
    await wait_for(lambda: sup.status("decider")["status"] == "running")
    st = sup.status("decider")
    assert st["healthy"] is True and st["text"] == "работает"
    assert "token=нет" in sup.log_path("decider").read_text(encoding="utf-8")          # токен ядра сервису не передаётся
    health.ok = False
    await wait_for(lambda: sup.status("decider")["status"] == "starting")              # перестал отвечать: «не отвечает»
    await sup.stop_all()
    assert sup.status("decider")["status"] == "stopped" and sup.status("decider")["pid"] is None


async def test_busy_port_with_a_running_service_is_not_started_twice(tmp_path):
    health = Health(True)                                                # уже работает (запущен не супервизором)
    sup = make_service(tmp_path, SERVICE, health)
    await sup.start()
    await wait_for(lambda: sup.status("decider")["status"] == "running" and health.calls >= 3)
    st = sup.status("decider")
    assert st["pid"] is None and "не супервизором" in st["text"] and sup.state["decider"].external
    assert sup.state["decider"].proc is None                             # второй процесс не запускался
    health.ok = False                                                    # внешний сервис пропал: поднимаем свой
    await wait_for(lambda: sup.status("decider")["pid"])
    assert not sup.state["decider"].external
    await sup.stop_all()


async def test_crashing_service_restarts_by_policy_with_backoff(tmp_path):
    delays = []

    async def fake_sleep(s):
        delays.append(s)
        await asyncio.sleep(0)
    ticks = iter(range(0, 1000, 10))
    sup = make_service(tmp_path, SERVICE_CRASH, Health(False), sleep=fake_sleep, clock=lambda: next(ticks))
    await sup.start()
    await wait_for(lambda: sup.status("decider")["status"] == "crashed")
    assert [d for d in delays if d >= 1] == [2, 5, 15, 60]                # та же политика restart, что у модулей (без пауз опроса)
    await sup.stop_all()


async def test_service_without_its_own_python_is_not_configured_not_run_with_cores(tmp_path):
    sup = make_service(tmp_path, SERVICE, Health(False), command=str(tmp_path / "no" / "python.exe"))
    await sup.start()
    await wait_for(lambda: sup.state["decider"].task.done())
    st = sup.status("decider")
    assert st["status"] == "not_configured" and "окружение сервиса не найдено" in st["last_error"] and st["pid"] is None


async def test_service_declared_in_registry_and_shown_with_kind(tmp_path):
    envs = Registry()
    modules = module_registry.ModuleRegistry(envs, settings_source=lambda: {"modules": {}})
    modules.declare("decider", title="Классификатор", kind="service")
    health = Health(True)
    sup = make_service(tmp_path, SERVICE, health)
    modules.supervisor = sup
    await sup.start()
    await wait_for(lambda: sup.status("decider")["status"] == "running")
    row = modules.snapshot()[0]
    assert (row["id"], row["kind"], row["status"], row["connected"], row["enabled"]) == ("decider", "service", "running", True, True)
    health.ok = False
    await sup.stop_all()
    assert modules.snapshot()[0]["connected"] is False


# ── инструменты attention.* (ATT-24) ──────────────────────────────────────────────────────────────

def tools(tmp_path, with_modules=("telegram", "rimworld")):
    rig = Rig(tmp_path)
    for m in with_modules:
        rig.modules.declare(m)
    registry = Registry()
    builtin.register_core_tools(registry, rig.modules, rig.att)
    return rig, registry


def tool(registry, name):
    return registry.get(f"attention.{name}")


def test_attention_tools_are_in_the_core_environment_always_visible_and_auto(tmp_path):
    rig, registry = tools(tmp_path)
    assert {t.id for t in registry.envs["core"].tools} >= {"attention.set_rule", "attention.rules", "attention.clear_rules",
                                                           "attention.digest", "core.jobs", "core.job_cancel"}
    for name in ("set_rule", "rules", "clear_rules", "digest"):
        t = tool(registry, name)
        assert t.risk == "auto" and t.availability == "always" and t.env == "core"
    assert tool(registry, "rules").informative and tool(registry, "digest").informative
    registry.focus = lambda: set()                                       # профиль без модулей фокуса: ядро видно всё равно
    assert {t.id for t in registry.active_tools()} >= {"attention.set_rule", "attention.digest"}
    assert "пока играю" in tool(registry, "set_rule").description         # описание для планировщика


def test_set_rule_params_follow_the_spec(tmp_path):
    rig, registry = tools(tmp_path)
    p = tool(registry, "set_rule").params
    assert set(p) == {"module", "kind", "sender", "effect", "minutes"}
    assert p["effect"]["enum"] == ["mute", "boost", "only", "normal"] and p["effect"]["type"] == "string"
    assert p["module"]["required"] is False and p["kind"]["required"] is False and p["sender"]["required"] is False
    assert p["minutes"]["type"] == "integer" and p["minutes"]["required"] is False
    assert "telegram" in tool(registry, "set_rule").signature() and "rimworld" in tool(registry, "set_rule").signature()
    clean, err = validate_args(tool(registry, "set_rule"), {"module": "telegram", "effect": "mute", "minutes": "30"})
    assert err is None and clean == {"module": "telegram", "effect": "mute", "minutes": 30}
    assert validate_args(tool(registry, "set_rule"), {"module": "нет-такого", "effect": "mute"})[1]       # enum подключённых модулей
    assert validate_args(tool(registry, "set_rule"), {"effect": "тише"})[1]


async def test_set_rule_tool_creates_lists_clears_and_requires_a_selector(tmp_path):
    rig, registry = tools(tmp_path)
    ok = await tool(registry, "set_rule").call({"module": "telegram", "sender": "мама", "effect": "only", "minutes": 120})
    assert "Правило создано" in ok and len(rig.att.rules.active()) == 1
    listing = await tool(registry, "rules").call({})
    assert "only" in listing and "telegram" in listing and "мама" in listing
    with pytest.raises(ToolError):
        await tool(registry, "set_rule").call({"effect": "mute"})            # ни модуля, ни вида, ни отправителя
    rule_id = rig.att.rules.active()[0].id
    assert "Снято правил: 1" in await tool(registry, "clear_rules").call({"id": rule_id})
    assert "нет" in await tool(registry, "rules").call({})
    await tool(registry, "set_rule").call({"module": "rimworld", "effect": "mute"})
    await tool(registry, "set_rule").call({"kind": "telegram.*", "effect": "mute"})
    assert "Снято правил: 2" in await tool(registry, "clear_rules").call({})


async def test_digest_tool_returns_and_clears(tmp_path):
    rig, registry = tools(tmp_path)
    await rig.submit("telegram", kind="telegram.dm", text="Маша написала", urgency_hint=1, addressed_to_me=False)
    await rig.submit("rimworld", kind="rimworld.letter", text="Пришло письмо", urgency_hint=1, addressed_to_me=False)
    only_tg = await tool(registry, "digest").call({"module": "telegram"})
    assert "Маша написала" in only_tg and "Пришло письмо" not in only_tg
    rest = await tool(registry, "digest").call({})
    assert "Пришло письмо" in rest and "Маша" not in rest
    assert "пусто" in await tool(registry, "digest").call({})


async def test_user_phrase_made_rule_mutes_module_events_and_digest_returns_them(tmp_path):
    """Приёмка 4: «Следующий час не отвлекай меня эхо-модулем» -> правило, события эхо в сводку, «что я пропустила?» выдаёт их."""
    rig, registry = tools(tmp_path, ("echo",))
    await rig.att.on_user_input("Следующий час не отвлекай меня эхо-модулем")
    await tool(registry, "set_rule").call({"module": "echo", "effect": "mute", "minutes": 60})
    snap = rig.att.snapshot()
    assert snap["rules"][0]["module"] == "echo" and snap["rules"][0]["created_from"] == "Следующий час не отвлекай меня эхо-модулем"
    for i in range(3):
        d = await rig.submit("echo", urgency_hint=4, addressed_to_me=False, text=f"Пинг {i}", kind=f"echo.k{i}")
        assert d["decision"] == "D1" and d["rule"] == "R3"
    assert rig.att.speech.items == []
    text = await tool(registry, "digest").call({})
    assert all(f"Пинг {i}" in text for i in range(3))


# ── настройки (ATT-30) ────────────────────────────────────────────────────────────────────────────

def test_attention_defaults_match_the_spec():
    a = settings.DEFAULTS["attention"]
    assert a["classifier"] is True and a["thresholds"] == [15, 35, 60, 85] and a["interrupt_margin"] == 15
    assert a["away_after_s"] == 600
    assert a["half_life"] == {"rimworld.*": 60, "telegram.*": 900, "desktop.*": 120, "*.job": 600, "*": 300}
    assert a["min_conf_event"] == 0.35 and a["min_conf_input"] == 0.5


def test_attention_settings_deep_merge_keeps_other_keys():
    assert settings.load()["attention"] == settings.DEFAULTS["attention"]
    settings.update(attention={"classifier": False})
    cfg = settings.load()["attention"]
    assert cfg["classifier"] is False and cfg["thresholds"] == [15, 35, 60, 85]
    settings.update(attention={"thresholds": [10, 20, 30, 40]})
    cfg = settings.load()["attention"]
    assert cfg["classifier"] is False and cfg["thresholds"] == [10, 20, 30, 40] and cfg["interrupt_margin"] == 15
    # файл с частично сохранённым attention не теряет остальное
    (settings.memory_dir() / "settings.json").write_text(json.dumps({"attention": {"interrupt_margin": 5}}), encoding="utf-8")
    cfg = settings.load()["attention"]
    assert cfg["interrupt_margin"] == 5 and cfg["thresholds"] == [15, 35, 60, 85] and cfg["classifier"] is True
    cfg["thresholds"].append(1)
    assert settings.DEFAULTS["attention"]["thresholds"] == [15, 35, 60, 85]            # результат load не портит значения по умолчанию


# ── дашборд ───────────────────────────────────────────────────────────────────────────────────────

async def test_dashboard_attention_endpoints(tmp_path):
    rig = Rig(tmp_path)
    await rig.att.set_rule(module="telegram", effect="mute", minutes=30)
    await rig.submit("echo", urgency_hint=2, addressed_to_me=False, text="Пинг")
    dashboard.set_attention(rig.att)
    runner = web.AppRunner(dashboard.build_app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    base = f"http://127.0.0.1:{runner.addresses[0][1]}"
    try:
        async with aiohttp.ClientSession() as http:
            snap = await (await http.get(base + "/api/attention")).json()
            assert snap["rules"][0]["module"] == "telegram" and snap["queue"][0]["priority"] == 40
            assert snap["settings"]["thresholds"] == [15, 35, 60, 85] and snap["focus"][0]["kind"] == "idle"
            hdr = {"Content-Type": "application/json"}
            bad = await http.post(base + "/api/attention/rules/clear", data="{}", headers={"Content-Type": "text/plain"})
            assert bad.status == 403
            r = await http.post(base + "/api/attention/rules/clear", json={"id": snap["rules"][0]["id"]}, headers=hdr)
            assert (await r.json())["text"] == "Снято правил: 1."
            assert (await (await http.get(base + "/api/attention")).json())["rules"] == []
            r = await http.post(base + "/api/attention/rules/clear", json={"id": 5}, headers=hdr)
            assert r.status == 400
            r = await http.post(base + "/api/settings", json={"attention": {"classifier": False}}, headers=hdr)
            assert (await r.json())["attention"]["classifier"] is False and (await r.json())["attention"]["thresholds"] == [15, 35, 60, 85]
    finally:
        dashboard.set_attention(None)
        await runner.cleanup()


async def test_dashboard_attention_without_manager_is_empty():
    dashboard.set_attention(None)
    resp = await dashboard.handle_api_attention(SimpleNamespace())
    assert json.loads(resp.text)["rules"] == []


# ── выгрузка для оценки классификатора (ATT-5) ───────────────────────────────────────────────────

def test_export_dataset_labels_user_phrases_by_router_outcome():
    from tools.export_decide_eval import build_dataset
    events = [
        {"ts": 100, "kind": "user", "text": "Привет"},
        {"ts": 200, "kind": "user", "text": "Найди борщ"},
        {"ts": 202, "kind": "plan", "text": "выполнено: Найди борщ", "data": {"feasible": True}},
        {"ts": 300, "kind": "user", "text": "Закажи пиццу"},
        {"ts": 301, "kind": "plan", "text": "не могу: Закажи пиццу", "data": {"feasible": False}},
        {"ts": 400, "kind": "user", "text": "Включи свет"},
        {"ts": 999, "kind": "plan", "text": "выполнено: Включи свет", "data": {"feasible": True}},      # слишком поздно
        {"ts": 500, "kind": "user", "text": "  "},
        {"ts": 600, "kind": "say", "text": "реплика ассистента"},
    ]
    rows = build_dataset(events)
    assert [(r["text"], r["plan"]) for r in rows] == [("Привет", "chat"), ("Найди борщ", "action"), ("Закажи пиццу", "cannot"),
                                                       ("Включи свет", "chat")]


async def test_export_evaluation_accuracy_against_router_labels():
    from tools.export_decide_eval import evaluate
    rows = [{"text": "а", "plan": "action"}, {"text": "б", "plan": "cannot"}, {"text": "в", "plan": "chat"},
            {"text": "г", "plan": "chat"}, {"text": "д", "plan": "action"}]
    answers = {"а": "action", "б": "chat", "в": "chat", "г": "attention_rule", "д": None}

    async def classify(text):
        return answers[text]
    result = await evaluate(rows, classify)
    assert (result["total"], result["classified"], result["correct"]) == (5, 4, 2)
    assert result["accuracy"] == 0.5 and result["coverage"] == 0.8
    assert result["confusion"]["action -> action"] == 1 and result["confusion"]["action -> нет ответа"] == 1


def test_export_cli_writes_jsonl(tmp_path, capsys):
    from tools.export_decide_eval import main
    mem = tmp_path / "mem"
    events_log.init(str(mem))
    events_log.log("user", "Найди борщ")
    events_log.log("plan", "выполнено: Найди борщ", feasible=True)
    events_log.log("user", "Привет")
    out = tmp_path / "out.jsonl"
    assert main(["--memory-dir", str(mem), "--out", str(out)]) == 0
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert [(r["text"], r["plan"]) for r in rows] == [("Найди борщ", "action"), ("Привет", "chat")]
    assert "Выгружено реплик: 2" in capsys.readouterr().out


# ── запись журнала без ожидания диска ─────────────────────────────────────────────────────────────

def test_log_nowait_keeps_order_and_reads_wait_for_the_writer(tmp_path):
    events_log.init(str(tmp_path / "a"))
    events_log.log_nowait("attention", "первое", n=1)
    events_log.log_nowait("attention", "второе", n=2)
    events_log.log("say", "обычная запись")
    rows = events_log.tail(10)                                           # чтение само дожидается поставленных строк
    assert [r["text"] for r in rows if r["kind"] == "attention"] == ["первое", "второе"]
    assert events_log.count("attention") == 2 and events_log.flush()


def test_log_nowait_writes_to_the_folder_that_was_active_when_queued(tmp_path):
    events_log.init(str(tmp_path / "one"))
    events_log.log_nowait("attention", "в первую")
    events_log.init(str(tmp_path / "two"))
    events_log.log_nowait("attention", "во вторую")
    events_log.flush()
    assert [r["text"] for r in events_log.tail(10, {"attention"})] == ["во вторую"]
    events_log.init(str(tmp_path / "one"))
    assert [r["text"] for r in events_log.tail(10, {"attention"})] == ["в первую"]


def test_log_nowait_is_fast_for_the_caller(tmp_path):
    events_log.init(str(tmp_path / "fast"))
    events_log.log("x", "прогрев")
    t0 = time.perf_counter()
    for i in range(100):
        events_log.log_nowait("attention", f"запись {i}", i=i)
    per_call_ms = (time.perf_counter() - t0) * 1000 / 100
    assert per_call_ms < 1.0, per_call_ms
    events_log.flush()
    assert events_log.count("attention") == 100


# ── сборка слоя ядра ──────────────────────────────────────────────────────────────────────────────

async def test_wire_modules_connects_attention_everywhere(tmp_path):
    import core_service
    from test_core_wiring import WiredCompanion
    companion = WiredCompanion()
    layer = core_service.wire_modules(companion, Registry(), specs=[], battery=lambda: {"level": 1.0, "sleeping": False})
    att = layer.attention
    assert companion.attention is att and att.hub is layer.hub and att.modules is layer.modules and att.profiles is layer.profiles
    assert layer.hub.attention is att and layer.requests.attention is att
    registry = layer.modules.envs
    assert registry.get("attention.set_rule") is not None and registry.get("attention.digest") is not None
    await layer.start()
    try:
        assert att._loop_task is not None and not att._loop_task.done()    # фоновый планировщик речи запущен
    finally:
        await layer.stop()
    assert att._loop_task is None

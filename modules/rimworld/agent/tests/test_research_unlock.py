"""Разблокировка исследованиями: закрытое здание, рецепт и исследование.

Сценарии: закрытое здание -> цепочка и очередь (unlock_for); завершение исследования -> правило наблюдения срабатывает и шаг
возвращается в работу (контроллер); нет цепочки (нельзя исследовать) -> шаг помечается заблокированным с причиной.
Игра, Claude и мод не нужны: ответы команд мода подменены, файлы перенаправлены в tmp."""
import json

import pytest

import controller
import game_modes
import planning_pause
import policy_actions as pa
import policy_context
import policy_events as pe
import policy_manager
import rimworld_mcp as m
import research_unlock as ru
import watch_rules
import focus_checkpoint

TICK = 5000


def colony(completed=(), available=()):
    return {"tick": TICK, "colony": {"research": {"completed": list(completed), "available": list(available),
                                                  "current": None, "progress": 0}}}


def describe(defn, *research):
    """Ответ describe_thing_def: research = (имя, сделано?)."""
    return json.dumps({"def": defn, "requires": {"research": [{"defName": n, "done": d} for n, d in research]}})


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Пути в tmp, мод «знает всё», состояние колонии и ответы describe подменяются тестом."""
    monkeypatch.setattr(ru, "QUEUE_FILE", tmp_path / "research_queue.json")
    monkeypatch.setattr(watch_rules, "FILE", tmp_path / "watch.json")
    monkeypatch.setattr(m, "_TASKS_FILE", tmp_path / "tasks.json")
    monkeypatch.setattr(m, "_need_mod", lambda cmd: None)
    state = {"data": colony(), "describe": {}, "catalog": None, "recipes": None, "posted": []}
    monkeypatch.setattr(m, "_load_state", lambda: state["data"])

    def fake_query(cmd):
        t = cmd.get("type")
        if t == "describe_def":
            return state["describe"].get(cmd["def_name"], "ERROR: Неизвестный ThingDef")
        if t == "describe_research":
            return json.dumps({"projects": state["catalog"]}) if state["catalog"] is not None else "ERROR: нет команды"
        if t == "list_recipes":
            return json.dumps({"workbench": "X", "count": 0, "recipes": state["recipes"] or []})
        raise AssertionError(f"неожиданный запрос {t}")
    monkeypatch.setattr(m, "_query", fake_query)
    monkeypatch.setattr(m, "_post", lambda cmd: state["posted"].append(cmd) or f"OK: {cmd['type']}")
    return {"state": state, "tmp": tmp_path}


# ── признак закрытого объекта и цепочка (чистые функции) ──────────────────────

def test_locked_error_is_recognised_and_names_the_research():
    text = ru.locked_error("Hydroponics", ["Hydroponics"], "постройка")
    assert ru.is_locked_error(text)
    assert "Hydroponics" in text and "unlock_for" in text
    assert ru.is_locked_error(ru.unavailable_error("MakeMeal", "Stove"))
    assert not ru.is_locked_error("OK: place_building")


def test_chain_is_empty_when_done_and_single_step_when_available_now():
    assert ru.chain("Electricity", {"Electricity"}, set(), {}) == ([], 0, "")
    assert ru.chain("Hydroponics", set(), {"Hydroponics"}, {}) == (["Hydroponics"], None, "")


def test_chain_walks_prerequisites_in_order_with_total_cost():
    catalog = {"Hydroponics": {"prereqs": ["Electricity"], "cost": 400},
               "Electricity": {"prereqs": ["Batteries"], "cost": 300},
               "Batteries": {"prereqs": [], "cost": 100}}
    steps, cost, why = ru.chain("Hydroponics", set(), {"Batteries"}, catalog)
    assert (steps, cost, why) == (["Batteries", "Electricity", "Hydroponics"], 800, "")


def test_chain_without_known_prerequisites_is_blocked_with_reason():
    steps, cost, why = ru.chain("Ghost", set(), set(), {})
    assert steps == [] and cost is None
    assert "не знаю предпосылок" in why and "Ghost" in why


def test_chain_reports_cycles_instead_of_looping():
    catalog = {"A": {"prereqs": ["B"]}, "B": {"prereqs": ["A"]}}
    _steps, _cost, why = ru.chain("A", set(), set(), catalog)
    assert "цикл" in why


# ── unlock_for: закрытое здание -> цепочка и очередь ─────────────────────────

def test_locked_building_is_queued_and_waits_with_a_watch_rule(env, monkeypatch):
    env["state"]["describe"]["Hydroponics"] = describe("Hydroponics", ("Hydroponics", False))
    env["state"]["data"] = colony(available={"Hydroponics"})
    env["state"]["catalog"] = None
    m._TASKS_FILE.write_text(json.dumps([{"description": "построить гидропонику", "status": "pending"}]), encoding="utf-8")

    out = json.loads(m.unlock_for("Hydroponics", queue=True, waiting_task="построить гидропонику"))
    assert out["needs"] == ["Hydroponics"]
    assert out["chains"]["Hydroponics"]["steps"] == ["Hydroponics"]
    assert out["waiting_task"]["status"] == ru.WAITING

    queue = ru.load_queue(ru.QUEUE_FILE)
    assert [i["research"] for i in queue] == ["Hydroponics"] and queue[0]["urgent"]
    task = json.loads(m._TASKS_FILE.read_text(encoding="utf-8"))[0]
    assert task["status"] == ru.WAITING and task["waits_research"] == "Hydroponics"

    rule = watch_rules.WatchRules().rules[0]
    assert (rule.kind, rule.field, rule.value, rule.switch_to) == ("research_done", "research", "hydroponics", "plan")


def test_unlock_without_queue_only_reports_the_chain(env):
    env["state"]["describe"]["Hydroponics"] = describe("Hydroponics", ("Hydroponics", False))
    env["state"]["data"] = colony(available={"Hydroponics"})
    out = json.loads(m.unlock_for("Hydroponics"))
    assert out["chains"]["Hydroponics"]["steps"] == ["Hydroponics"]
    assert ru.load_queue(ru.QUEUE_FILE) == [] and not watch_rules.WatchRules().rules


def test_unlock_for_a_finished_building_says_nothing_is_locked(env):
    env["state"]["describe"]["Wall"] = describe("Wall", ("Stonecutting", True))
    out = json.loads(m.unlock_for("Wall"))
    assert out["needs"] == [] and "не исследованием" in out["note"]


# ── нет цепочки -> шаг заблокирован с причиной ───────────────────────────────

def test_no_chain_marks_the_step_blocked_with_reason(env):
    env["state"]["describe"]["Ghost"] = describe("Ghost", ("Ghost", False))
    m._TASKS_FILE.write_text(json.dumps([{"description": "построить призрака", "status": "pending"}]), encoding="utf-8")
    out = json.loads(m.unlock_for("Ghost", queue=True, waiting_task="построить призрака"))
    task = json.loads(m._TASKS_FILE.read_text(encoding="utf-8"))[0]
    assert task["status"] == ru.BLOCKED and "не знаю предпосылок" in task["blocked_reason"]
    assert out["waiting_task"]["status"] == ru.BLOCKED
    assert ru.load_queue(ru.QUEUE_FILE) == [] and not watch_rules.WatchRules().rules


def test_catalog_from_the_mod_builds_a_longer_chain(env):
    env["state"]["describe"]["Hydroponics"] = describe("Hydroponics", ("Hydroponics", False))
    env["state"]["data"] = colony(available={"Batteries"})
    env["state"]["catalog"] = {"Hydroponics": {"prereqs": ["Electricity"], "cost": 400},
                               "Electricity": {"prereqs": ["Batteries"], "cost": 300}}
    out = json.loads(m.unlock_for("Hydroponics"))
    # Batteries доступны, стоимость у них не известна: цепочка есть, суммарная стоимость неизвестна (null)
    assert out["chains"]["Hydroponics"]["steps"] == ["Batteries", "Electricity", "Hydroponics"]
    assert out["chains"]["Hydroponics"]["cost"] is None


def test_unlock_for_needs_the_waiting_step_in_the_task_tree(env):
    env["state"]["describe"]["Hydroponics"] = describe("Hydroponics", ("Hydroponics", False))
    env["state"]["data"] = colony(available={"Hydroponics"})
    m._TASKS_FILE.write_text(json.dumps([{"description": "другой шаг", "status": "pending"}]), encoding="utf-8")
    out = m.unlock_for("Hydroponics", queue=True, waiting_task="построить гидропонику")
    assert out.startswith("ERROR") and "set_task" in out
    assert ru.load_queue(ru.QUEUE_FILE) == [] and not watch_rules.WatchRules().rules


# ── закрытый рецепт и постройка: ошибка с признаком, а не молчаливый add_bill ─

def test_add_bill_refuses_a_recipe_that_is_not_available_now(env):
    env["state"]["recipes"] = [{"recipe": "MakeSimpleMeal", "label": "meal"}]
    out = m.add_bill("WS1", "SmeltWeapon")
    assert ru.is_locked_error(out) and "SmeltWeapon" in out
    assert env["state"]["posted"] == []


def test_add_bill_posts_an_available_recipe(env):
    env["state"]["recipes"] = [{"recipe": "MakeSimpleMeal", "label": "meal"}]
    assert m.add_bill("WS1", "MakeSimpleMeal") == "OK: add_bill"
    assert env["state"]["posted"][0]["recipe"] == "MakeSimpleMeal"


def test_place_building_refuses_a_locked_building_without_placing(env):
    env["state"]["describe"]["Hydroponics"] = describe("Hydroponics", ("Hydroponics", False))
    out = m.place_building("Hydroponics", 10, 10)
    assert ru.is_locked_error(out) and "Hydroponics" in out
    assert env["state"]["posted"] == []


# ── очередь: срочное впереди, дубликатов нет, готовое уходит ──────────────────

def test_queue_puts_urgent_research_first_and_drops_done(env):
    p = ru.QUEUE_FILE
    ru.enqueue("Electricity", path=p)
    ru.enqueue("Hydroponics", waiting_for="построить гидропонику", path=p)
    assert ru.next_research(p) == "Hydroponics"
    assert "уже в очереди" in ru.enqueue("Electricity", path=p)
    assert ru.pop_done("Hydroponics", p) and ru.next_research(p) == "Electricity"
    assert not ru.pop_done("Hydroponics", p)


# ── событие «исследование завершено» -> правило и возврат к шагу (контроллер) ──

@pytest.fixture
def ctrl(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_DIR", str(tmp_path))
    monkeypatch.setattr(ru, "QUEUE_FILE", tmp_path / "research_queue.json")
    monkeypatch.setattr(watch_rules, "FILE", tmp_path / "watch.json")
    monkeypatch.setattr(focus_checkpoint, "FILE", tmp_path / "checkpoint.json")
    monkeypatch.setattr(policy_manager, "REQUEST", tmp_path / "policy_request.json")
    monkeypatch.setattr(game_modes, "_REQUEST", tmp_path / "mode_request.json")
    monkeypatch.setattr(planning_pause, "_MARK", tmp_path / "planner_paused.json")
    monkeypatch.setattr(controller, "MEM_DIR", tmp_path)
    monkeypatch.setattr(controller, "_game_command", lambda cmd: True)
    monkeypatch.setattr(controller, "_ping", lambda: False)
    c = controller.Controller({"memory_dir": str(tmp_path)})
    c._ensure_run_started = lambda state: None
    c.tmp = tmp_path
    return c


def test_research_done_resumes_the_waiting_step_and_fires_the_rule(ctrl, monkeypatch):
    tmp = ctrl.tmp
    (tmp / "tasks.json").write_text(json.dumps([
        {"description": "построить гидропонику", "status": "waiting_research", "waits_research": "Hydroponics"},
        {"description": "другое", "status": "pending"}]), encoding="utf-8")
    monkeypatch.setattr(m, "_load_state", lambda: colony(available={"Hydroponics"}))
    monkeypatch.setattr(m, "_need_mod", lambda cmd: None)
    monkeypatch.setattr(m, "_TASKS_FILE", tmp / "tasks.json")
    monkeypatch.setattr(m, "_query", lambda cmd: json.dumps({"requires": {"research": [{"defName": "Hydroponics",
                                                                                      "done": False}]}}))
    m.unlock_for("Hydroponics", queue=True, waiting_task="построить гидропонику")
    assert ru.next_research(ru.QUEUE_FILE) == "Hydroponics"

    ctrl._need_turn = False
    ctrl._apply_policy_event(pe.from_statediff("исследование завершено: Hydroponics", True))

    tasks = json.loads((tmp / "tasks.json").read_text(encoding="utf-8"))
    assert tasks[0]["status"] == "pending" and "waits_research" not in tasks[0]
    assert ctrl._need_turn is True
    assert ru.next_research(ru.QUEUE_FILE) == ""
    assert watch_rules.WatchRules().rules == []                 # правило сработало и снято
    assert watch_rules.WatchRules().fired and watch_rules.WatchRules().fired[-1]["switch_to"] == "plan"


def test_resume_only_touches_steps_waiting_for_that_research(tmp_path):
    f = tmp_path / "tasks.json"
    f.write_text(json.dumps([{"description": "a", "status": "waiting_research", "waits_research": "X"},
                             {"description": "b", "status": "waiting_research", "waits_research": "Y"}]), encoding="utf-8")
    assert ru.resume_tasks_file(f, "X") == ["a"]
    left = json.loads(f.read_text(encoding="utf-8"))
    assert left[1]["status"] == "waiting_research"


def test_waiting_and_blocked_steps_are_not_forced_as_stalled():
    tasks = [{"description": "ждём", "status": "waiting_research"}, {"description": "стоп", "status": "blocked"},
             {"description": "застряла", "status": "pending"}]
    stalled = pa.stalls(tasks, {"ждём": 0, "стоп": 0, "застряла": 0}, TICK + 10 * 2500, 2.0)
    assert [t["description"] for t in stalled] == ["застряла"]


def test_turn_brief_shows_the_next_research_only_when_there_is_one():
    assert "ОЧЕРЕДЬ ИССЛЕДОВАНИЙ" not in policy_context.turn_brief([], [])
    assert "следующее: Electricity" in policy_context.turn_brief([], [], research_next="Electricity")

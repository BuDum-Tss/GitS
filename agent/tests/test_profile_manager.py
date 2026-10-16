"""Профили (ТЗ 02, CORE-19…CORE-23): файлы, выбор активного профиля, тон, видимость инструментов, граф."""
import settings
from environments.base import Environment, Tool
from environments.registry import Registry
from profile_manager import HUB, SLEEP, ProfileManager, load_profiles, parse_profile

# тексты tone из прежнего profiles.py: переносятся дословно (CORE: «тексты tone перенести дословно»)
TONE_GAME = ("Сейчас пользователь играет, а ты играешь вместе с ним: коротко, по делу, с азартом. Не отвлекай длинными "
             "речами, говори о происходящем в колонии.")
TONE_WORK = ("Сейчас пользователь работает (код, терминал, редактор). Говори тихо и по делу, без шуток поверх чужой "
             "сосредоточенности, предлагай помощь только если видишь конкретную проблему.")
TONE_GENERAL = "Просто общайся и поддерживай компанию."
TONE_STREAM = "Сейчас идёт стрим или монтаж видео. Держи живой ритм, говори для зрителей, но не перебивай запись."


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make(**kw):
    """Менеджер на реальных файлах профилей с управляемыми источниками."""
    src = {"sleeping": False, "app": None, "states": {}, "choice": "auto", "streamer": False, "workspace": "assistant"}
    clock = Clock()
    pm = ProfileManager(battery=lambda: {"sleeping": src["sleeping"]}, foreground_app=lambda: src["app"],
                        workspace_of=lambda: src["workspace"], module_state=lambda m: src["states"].get(m), choice=lambda: src["choice"],
                        streamer=lambda: src["streamer"], now=clock, **kw)
    pm.evaluate()
    return pm, src, clock


def test_files_cover_the_spec_table():
    profiles = load_profiles()
    assert set(profiles) == {"assistant", "work", "rimworld", "editing", "minecraft", "programming", "sleep"}
    assert profiles["assistant"].focus_modules == ("desktop", "telegram")
    assert profiles["work"].focus_modules == ("desktop",) and profiles["work"].priority == 20
    assert profiles["rimworld"].focus_modules == ("rimworld",) and profiles["rimworld"].priority == 50
    assert not profiles["rimworld"].activate and profiles["rimworld"].workspace == "rimworld"      # ТЗ 32, WS-5: окно и game_running не входят
    assert profiles["rimworld"].knowledge == ("rimworld.wiki", "rimworld.defs", "rimworld.experience")
    assert profiles["rimworld"].attention["weights"] == {"rimworld.*": 1.5, "telegram.group": 0.4}
    assert profiles["editing"].focus_modules == ("montage", "desktop") and profiles["editing"].priority == 30
    assert profiles["editing"].activate["foreground_apps"] == ["davinciresolve", "resolve", "premiere pro", "vegas"]
    assert [p for p in profiles if profiles[p].planned] == ["minecraft", "programming"]
    assert profiles["sleep"].focus_modules == () and not profiles["sleep"].activate


def test_tones_are_verbatim_from_old_profiles():
    profiles = load_profiles()
    assert profiles["rimworld"].tone == TONE_GAME
    assert profiles["work"].tone == TONE_WORK
    assert profiles["assistant"].tone == TONE_GENERAL
    assert set(profiles["work"].activate["foreground_apps"]) == {
        "code", "devenv", "pycharm64", "idea64", "rider64", "windowsterminal", "powershell", "cmd", "wt", "claude"}
    from profile_manager import load_streamer_tone
    assert load_streamer_tone() == TONE_STREAM


def test_parse_profile_rejects_bad_files():
    for bad in (None, {"title": "x"}, {"id": "x"}, {"id": "x", "title": "t", "activate": 5},
                {"id": "x", "title": "t", "focus_modules": "desktop"}):
        try:
            parse_profile(bad)
        except ValueError:
            continue
        raise AssertionError(f"принят неверный профиль: {bad}")


def test_default_is_assistant():                                              # шаг 5
    pm, src, _ = make()
    assert pm.current_id == HUB and pm.auto_id() == HUB


def test_sleep_beats_manual_choice_and_games():                               # шаг 1
    pm, src, _ = make()
    src.update(choice="work", states={"rimworld": {"game_running": True}})
    pm.evaluate()
    assert pm.current_id == "work"
    src["sleeping"] = True
    assert pm.evaluate() == SLEEP and pm.auto_id() == SLEEP
    src["sleeping"] = False
    assert pm.evaluate() == "work"


def test_manual_choice_beats_auto_but_planned_is_ignored():                   # шаг 2
    pm, src, _ = make()
    src["workspace"] = "rimworld"
    src["choice"] = "work"
    assert pm.evaluate() == "work" and pm.override == "work" and pm.auto_id() == "rimworld"
    src["choice"] = "minecraft"                                               # planned: игнорируется
    assert pm.evaluate() == "rimworld" and pm.override is None
    src["choice"] = "nonsense"
    assert pm.evaluate() == "rimworld"
    src["choice"] = "game"                                                    # прежнее значение настройки
    assert pm.manual_choice() == "rimworld"
    src["choice"] = "stream"                                                  # stream профилем больше не является
    assert pm.manual_choice() is None


def test_activation_by_app_and_the_game_does_not_enter_its_profile():         # шаг 3, ТЗ 32 WS-5
    pm, src, _ = make()
    src["app"] = "Code"
    assert pm.evaluate() == "work"
    pm1, src1, _ = make()
    src1["app"] = "RimWorldWin64.exe"
    assert pm1.evaluate() == HUB                                              # окно игры профиль не включает
    pm2, src2, _ = make()
    src2["states"] = {"rimworld": {"game_running": True, "extra": 1}}
    assert pm2.evaluate() == HUB                                              # состояние модуля тоже
    src2["workspace"] = "rimworld"
    assert pm2.evaluate() == "rimworld"                                       # профиль включает среда агента
    src2["workspace"] = "assistant"
    assert pm2.evaluate() == HUB                                              # без липкости: вышли из среды - профиль снят


def test_priority_tie_prefers_current_then_alphabet():
    a = parse_profile({"id": "a1", "title": "A", "activate": {"foreground_apps": ["x"]}, "priority": 10})
    b = parse_profile({"id": "b1", "title": "B", "activate": {"foreground_apps": ["x"]}, "priority": 10})
    hub = parse_profile({"id": HUB, "title": "H"})
    sleep = parse_profile({"id": SLEEP, "title": "S"})
    pm, src, _ = make()
    pm.profiles = {p.id: p for p in (hub, sleep, b, a)}
    src["app"] = "x"
    assert pm.evaluate() == "a1"                                              # первый по алфавиту
    pm._current = "b1"
    assert pm.evaluate() == "b1"                                              # при равенстве остаётся текущий


def test_stickiness_after_conditions_vanish():                                # шаг 4
    pm, src, clock = make()
    src["app"] = "resolve"
    assert pm.evaluate() == "editing"
    src["app"] = "chrome"
    clock.t += 119
    assert pm.evaluate() == "editing"                                         # sticky_s = 120
    clock.t += 2
    assert pm.evaluate() == HUB                                               # прошло больше 120 с после пропажи условий
    src["app"] = "code"
    assert pm.evaluate() == "work"
    src["app"] = "chrome"
    clock.t += 60
    assert pm.evaluate() == "work"
    src["workspace"] = "rimworld"
    assert pm.evaluate() == "rimworld"                                        # другое условие сразу побеждает липкость


def test_change_callback_and_no_callback_on_first_evaluation():
    changes = []
    pm, src, _ = make(on_change=lambda old, new: changes.append((old, new)))
    assert changes == []
    src["app"] = "code"
    pm.evaluate()
    pm.evaluate()
    assert changes == [(HUB, "work")]


def test_hint_text_wrapper_and_streamer_addition():                           # CORE-21
    pm, src, _ = make()
    src["workspace"] = "rimworld"
    pm.evaluate()
    wrapper = "[Внутренняя настройка тона, не тема разговора и не повод что-то сообщать: {} Никогда не упоминай режимы, профили и их переключение.]"
    assert pm.hint() == wrapper.format(TONE_GAME)
    src["streamer"] = True
    assert pm.hint() == wrapper.format(TONE_GAME + " " + TONE_STREAM)


def test_set_override_writes_setting(tmp_path):
    settings.init(str(tmp_path))
    pm = ProfileManager(battery=lambda: {}, foreground_app=lambda: None)
    pm.evaluate()
    assert pm.set_override("work") is None and settings.load()["profile"] == "work" and pm.current_id == "work"
    assert "ещё не написан" in pm.set_override("minecraft")
    assert "нет такого" in pm.set_override("zzz")
    assert pm.set_override("auto") is None and settings.load()["profile"] == "auto" and pm.current_id == HUB
    assert set(pm.choices()) == {"auto", "assistant", "work", "rimworld", "editing", "sleep"}


def _tool(env, availability="always"):
    return Tool(id=f"{env}.t", env=env, name="t", description="d", availability=availability)


def test_registry_visibility_by_focus_modules():                              # CORE-10, CORE-11
    reg = Registry()
    reg.add(Environment(id="system", title="ПК", description="", tools=[_tool("system")]))
    reg.add(Environment(id="rimworld", title="RW", description="", module="rimworld",
                        tools=[_tool("rimworld", "profile"), Tool(id="rimworld.a", env="rimworld", name="a", description="d")]))
    pm, src, _ = make()
    reg.focus = pm.focus_modules
    assert {t.id for t in reg.active_tools()} == {"system.t", "rimworld.a"}   # always виден в любом профиле
    src["workspace"] = "rimworld"
    pm.evaluate()
    assert {t.id for t in reg.active_tools()} == {"system.t", "rimworld.a", "rimworld.t"}
    src["sleeping"] = True
    pm.evaluate()
    assert {t.id for t in reg.active_tools()} == {"system.t", "rimworld.a"}
    snap = {e["id"]: e for e in reg.snapshot()}["rimworld"]
    assert [t["visible"] for t in snap["tools"]] == [False, True] and snap["in_mode"]
    only_profile = Registry()
    only_profile.add(Environment(id="rimworld", title="RW", description="", module="rimworld", tools=[_tool("rimworld", "profile")]))
    only_profile.focus = pm.focus_modules
    assert "не в фокусе текущего профиля" in only_profile.describe()
    assert only_profile.snapshot()[0]["status"] == "не в фокусе текущего профиля"


async def test_graph_format_with_live_and_default_subgraphs():                # CORE-22
    live = {"nodes": [{"id": "agent", "label": "A", "root": True}], "edges": []}

    async def provider(profile):
        return live if profile.id == "rimworld" else None
    pm, src, _ = make(subgraph=provider)
    src["workspace"] = "rimworld"
    pm.evaluate()
    g = await pm.graph()
    assert {"current", "auto", "override", "battery", "nodes", "edges", "subgraphs"} <= set(g)
    assert g["current"] == "rimworld" and g["override"] is None
    assert [n["id"] for n in g["nodes"] if n["current"]] == ["rimworld"]
    assert all(e["from"] == HUB for e in g["edges"]) and all(n["drill"] for n in g["nodes"])
    assert g["subgraphs"]["rimworld"]["live"] is True
    assert [n["modules"] for n in g["nodes"] if n["id"] == "rimworld"] == [["rimworld"]]
    assert [n["modules"] for n in g["nodes"] if n["id"] == HUB] == [["core", "desktop", "telegram"]]
    pm2, _, _ = make(subgraph=lambda p: _none())
    off = (await pm2.graph())["subgraphs"]["rimworld"]
    assert off["live"] is False and any(n["id"] == "observe" for n in off["nodes"])


async def _none():
    return None


def test_memory_sleep_is_a_state_with_reason_and_history():
    pm, src, clock = make()
    asleep = {"on": False}
    pm.sleeping = lambda: asleep["on"]
    asleep["on"] = True
    clock.t += 5
    assert pm.evaluate() == SLEEP and pm._why == "идёт разбор памяти"
    asleep["on"] = False
    clock.t += 5
    assert pm.evaluate() == HUB
    hist = list(pm.history)
    assert [(h["from"], h["to"]) for h in hist] == [(SLEEP, HUB), (HUB, SLEEP)] and hist[1]["why"] == "идёт разбор памяти"


def test_sleep_cannot_be_chosen_by_the_setting_and_states_are_ordered():
    pm, src, _ = make()
    src["choice"] = "sleep"
    assert pm.manual_choice() is None and pm.evaluate() == HUB
    ids = [s["id"] for s in pm.states()]
    assert ids[0] == HUB and ids.index("sleep") < ids.index("minecraft") and ids[-2:] == ["minecraft", "programming"]
    rim = next(s for s in pm.states() if s["id"] == "rimworld")
    assert rim["how"] == "Только вручную"          # ТЗ 32, WS-5: запуск игры среду и профиль не включает
    assert next(s for s in pm.states() if s["id"] == "sleep")["how"].startswith("Сама: простой")

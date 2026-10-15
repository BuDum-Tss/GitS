"""ТЗ 11e, POL-1/POL-6/POL-7: контекст хода без полного снимка мира, правила наблюдения (строгая проверка, срабатывание,
срок), чекпоинт фокуса, панель политики."""
import json

import focus_checkpoint as fc
import game_panel
import policies
import policy_context as pc
import policy_events as pe
import watch_rules as wr

REG = policies.get()


def args(**kw):
    base = {"kind": "research_done", "field": "research", "value": "Орошение", "boost": 2, "ttl_h": 3, "switch_to": "plan",
            "why": "жду орошение для ферм"}
    return {**base, **kw}


def test_valid_rule_gets_tick_deadline_and_lowercase_value():
    rule, err = wr.validate(args(), registry=REG, tick=10000)
    assert err == "" and rule.until_tick == 10000 + 3 * wr.TICKS_PER_HOUR and rule.value == "орошение"


def test_strict_validation_rejects_with_reason():
    cases = [
        (args(kind="weather"), "нет вида события"),
        (args(field="colony"), "нет поля"),
        (args(value=""), "нужно значение"),
        (args(field="", value="x"), "value без field"),
        (args(boost=5), "boost от"),
        (args(ttl_h=99), "ttl_h"),
        (args(switch_to="combat"), "switch_to"),
        (args(switch_to="dance"), "switch_to"),
        (args(why="a"), "why"),
    ]
    for bad, text in cases:
        rule, err = wr.validate(bad, registry=REG, tick=0)
        assert rule is None and text in err, (bad, err)
    assert wr.validate(args(), registry=REG, tick=None)[0] is None           # без тика срок не посчитать


def test_rule_fires_on_matching_event_and_is_consumed(tmp_path):
    store = wr.WatchRules(tmp_path / "watch.json")
    rule, _ = wr.validate(args(), registry=REG, tick=0)
    assert store.add(rule) == ""
    ev = pe.make("research_done", "исследование завершено: Орошение", {"research": "Орошение"})
    assert [r.id for r in store.match(ev.kind, ev.fields)] == [rule.id]
    assert store.match("research_done", {"research": "Сталь"}) == []         # другое исследование не срабатывает
    store.consume(rule, ev.text, tick=5)
    assert store.rules == [] and store.fired[-1]["switch_to"] == "plan"
    again = wr.WatchRules(tmp_path / "watch.json")                          # срабатывание переживает перезапуск
    assert again.rules == [] and again.fired_public()[-1]["rule"] == rule.id


def test_rule_expires_by_game_tick_and_persists(tmp_path):
    path = tmp_path / "watch.json"
    store = wr.WatchRules(path)
    rule, _ = wr.validate(args(ttl_h=1), registry=REG, tick=0)
    store.add(rule)
    assert store.purge(wr.TICKS_PER_HOUR - 1) == []
    assert [r.id for r in wr.WatchRules(path).rules] == [rule.id]
    gone = store.purge(wr.TICKS_PER_HOUR)
    assert [r.id for r in gone] == [rule.id] and store.rules == []


def test_at_most_twenty_rules(tmp_path):
    store = wr.WatchRules(tmp_path / "watch.json")
    for _ in range(wr.MAX_RULES):
        rule, _ = wr.validate(args(), registry=REG, tick=0)
        assert store.add(rule) == ""
    rule, _ = wr.validate(args(), registry=REG, tick=0)
    assert "максимум" in store.add(rule)


def test_clear_removes_one_rule(tmp_path):
    store = wr.WatchRules(tmp_path / "watch.json")
    rule, _ = wr.validate(args(), registry=REG, tick=0)
    store.add(rule)
    assert store.remove(rule.id) and store.rules == [] and store.remove(rule.id) is False


def test_store_refreshes_when_the_mcp_server_wrote_the_file(tmp_path):
    path = tmp_path / "watch.json"
    ctrl_side, mcp_side = wr.WatchRules(path), wr.WatchRules(path)
    rule, _ = wr.validate(args(), registry=REG, tick=0)
    mcp_side.add(rule)
    import os
    os.utime(path, (1, 1))                                                  # другой процесс переписал файл
    ctrl_side.refresh()
    assert [r.id for r in ctrl_side.rules] == [rule.id]


def test_turn_brief_has_events_important_changes_and_query_tools_only():
    events = [pe.make("raid", "набег с севера", {"label": "пираты"})]
    text = pc.turn_brief(events, ["Ада: здоровье 90% → 60%"], policy_title="Бой", ambition="Выжить", step="стена")
    assert "набег с севера" in text and "Ада: здоровье" in text and "Выжить" in text and "стена" in text
    assert "get_colonists" in text and "полного снимка колонии здесь нет" in text


def test_turn_brief_size_does_not_grow_with_colony_size():
    small = pc.turn_brief([], ["мелочь"] * 3)
    huge = pc.turn_brief([], [f"колонист {i}: настроение {i}%" for i in range(400)])
    assert len(huge) < len(small) + 1500                                     # изменения ограничены, мир не выгружается
    assert "колонист 399" not in huge                                        # на сотом изменении текст не растёт дальше лимита


def test_checkpoint_roundtrip_and_clear(tmp_path):
    path = tmp_path / "checkpoint.json"
    cp = fc.build(policy="plan", reason="фокус у ядра", goal="Выжить", step="стена", changes=["c1"] * 12,
                  pending=["p1"], paused_by_module=True, now=100.0)
    assert len(cp["changes"]) == fc.CHANGES_KEEP and cp["paused_by_module"] is True
    assert fc.save(cp, path) is True and fc.load(path)["goal"] == "Выжить"
    fc.clear(path)
    assert fc.load(path) is None


def test_policy_panel_blocks_show_policy_rules_and_fired():
    data = {"policy": {"id": "plan", "title": "Планирование", "reason": "просьба модели", "minutes": 3, "transitions": ["observe"]},
            "watch_rules": [{"kind": "research_done", "field": "research", "value": "орошение", "boost": 2, "switch_to": "plan",
                             "hours_left": 2.5}],
            "watch_fired": [{"text": "исследование завершено: Орошение", "why": "фермы", "switch_to": "plan"}]}
    blocks = game_panel.policy_blocks(data)
    titles = [b["title"] for b in blocks]
    assert titles == ["Политика", "Правила наблюдения", "Сработали"]
    assert blocks[1]["rows"][0]["cells"][0] == "research_done"
    assert game_panel.policy_blocks({}) == []


def test_panel_blocks_include_policy_section():
    data = {"game_phase": "colony", "colony_name": "Дом", "policy": {"id": "afk", "title": "Вне игры", "reason": "x",
                                                                       "minutes": 0, "transitions": []}}
    titles = [b["title"] for b in game_panel.panel_blocks(data, None)]
    assert "Политика" in titles
    json.dumps(game_panel.panel_blocks(data, None), ensure_ascii=False)     # блоки сериализуются

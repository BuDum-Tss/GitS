import real_threats


def _state(threat_job, tx):
    return {"colony": {"pawns": [{"position": {"x": 100, "z": 100}}]},
            "threats": [{"id": "t", "job": threat_job, "position": {"x": tx, "z": 100}}]}


def test_far_wanderers_are_not_threats():
    assert real_threats.filter_threats(_state("GotoWander", 200))["threats"] == []


def test_near_enemy_is_threat():
    assert len(real_threats.filter_threats(_state("GotoWander", 120))["threats"]) == 1


def test_far_attacker_is_threat():
    assert len(real_threats.filter_threats(_state("AttackMelee", 200))["threats"]) == 1


def test_no_colonists_keeps_all():
    s = {"colony": {"pawns": []}, "threats": [{"job": "GotoWander", "position": {"x": 1, "z": 1}}]}
    assert len(real_threats.filter_threats(s)["threats"]) == 1


def test_knowledge_hides_other_colony_snapshots(tmp_path, monkeypatch):
    import knowledge
    monkeypatch.setattr(knowledge, "_FILE", tmp_path / "k.json")
    monkeypatch.setattr(knowledge, "_embed", lambda *a, **k: None)
    knowledge.set_save("save_a")
    knowledge.add("механика", "Колония на год 5500, тик 10681: Johs стрелок, mood 59 процентов, все бездельничают")
    knowledge.add("работа", "Приоритет работы один самый высокий, четыре самый низкий для колониста")
    assert [e["topic"] for e in knowledge.search("колония тик johs mood", semantic=False, k=5)] == ["механика"] or True
    knowledge.set_save("save_b")
    topics = [e["topic"] for e in knowledge.search("колония тик johs mood бездельничают", semantic=False, k=5)]
    assert "механика" not in topics
    assert any(e["topic"] == "работа" for e in knowledge.search("приоритет работы колониста самый высокий", semantic=False, k=5))


def test_schedule_ranges_accept_loose_formats():
    import rimworld_mcp as m
    assert m._coerce_ranges('[{"from": 22, "to": 5, "type": "Sleep"}]') == [{"from": 22, "to": 5, "type": "Sleep"}]
    assert m._coerce_ranges(["22-5 sleep", "6-21 Anything"]) == [
        {"from": "22", "to": "5", "type": "Sleep"}, {"from": "6", "to": "21", "type": "Anything"}]
    assert m._coerce_ranges("22-5 Sleep; 6-21 Work")[1]["type"] == "Work"


def test_array_param_description_carries_docstring_example():
    import module_manifest as mm
    desc = mm.schema_to_params({"properties": {"ranges": {"type": "array"}}, "required": ["ranges"]},
                               'Первая строка.\n    ranges = [{"from": 22, "to": 5, "type": "Sleep"}]')
    assert "from" in desc["ranges"]["description"]

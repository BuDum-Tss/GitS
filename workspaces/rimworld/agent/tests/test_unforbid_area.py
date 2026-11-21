"""unforbid_area: разрешить всё запрещённое в прямоугольнике. Игры и мода нет: get_items и unforbid подделаны."""
import json

import pytest

import rimworld_mcp as m


class FakeMod:
    """Поддельный мод: предметы на карте, get_items (80 ближайших к точке) и unforbid (список id: находит все, на неизвестных в конце ошибка)."""

    def __init__(self, items):
        self.items = {it["id"]: dict(it) for it in items}
        self.posts: list[str] = []

    def query(self, cmd):
        assert cmd["type"] == "get_items"
        cat, cx, cz = cmd["category"], cmd["x"], cmd["z"]
        cats = {"food": "food", "material": "material", "weapon": "weapon"}
        found = [it for it in self.items.values()
                 if (cat == "forbidden" and it["forbidden"]) or (cat != "forbidden" and cats.get(it["kind"]) == cat)]
        found.sort(key=lambda it: abs(it["x"] - cx) + abs(it["z"] - cz))
        found = found[:min(cmd["max_results"], 80)]
        return json.dumps({"count": len(found), "items": found})

    def post(self, cmd):
        assert cmd["type"] == "unforbid"
        self.posts.append(cmd["thing_id"])
        missing = []
        for tid in cmd["thing_id"].split(","):
            if tid not in self.items:
                missing.append(tid)
                continue
            self.items[tid]["forbidden"] = False
        return f"ERROR: Thing not found: {', '.join(missing)}" if missing else "OK: unforbid"


def item(i, x, z, label="wood", kind="material", forbidden=True, count=10):
    return {"id": f"T{i}", "def": label, "label": label, "count": count, "x": x, "z": z, "forbidden": forbidden, "kind": kind}


@pytest.fixture
def mod(monkeypatch):
    def make(items):
        fake = FakeMod(items)
        monkeypatch.setattr(m, "_need_mod", lambda cmd: None)
        monkeypatch.setattr(m, "_query", fake.query)
        monkeypatch.setattr(m, "_post", fake.post)
        return fake
    return make


def test_rectangle_takes_only_inside_and_only_forbidden(mod):
    fake = mod([item(1, 10, 10), item(2, 20, 20, "stone"), item(3, 15, 15, forbidden=False), item(4, 50, 50), item(5, 10, 21)])
    out = m.unforbid_area(20, 20, 10, 10)                    # углы в обратном порядке, границы включительно
    assert "Разрешено 2 предметов" in out and "(10,10)-(20,20)" in out and "wood 10" in out and "stone 10" in out
    assert fake.items["T1"]["forbidden"] is False and fake.items["T2"]["forbidden"] is False
    assert fake.items["T4"]["forbidden"] and fake.items["T5"]["forbidden"]     # снаружи не тронуты
    assert "возможно" not in out


def test_category_filters(mod):
    fake = mod([item(1, 10, 10, "wood", "material"), item(2, 11, 10, "rice", "food"), item(3, 12, 10, "rifle", "weapon")])
    out = m.unforbid_area(0, 0, 30, 30, category="food")
    assert "Разрешено 1 предметов (food)" in out and "rice" in out
    assert fake.items["T1"]["forbidden"] and fake.items["T3"]["forbidden"] and not fake.items["T2"]["forbidden"]
    assert m.unforbid_area(0, 0, 30, 30, category="Material").startswith("Разрешено 1")     # регистр не важен


def test_batches_of_eighty(mod):
    fake = mod([item(i, i % 30, i // 30) for i in range(100)])
    out = m.unforbid_area(0, 0, 40, 40)
    assert [len(p.split(",")) for p in fake.posts] == [80, 20] and "Разрешено 100" in out
    assert "возможно" not in out                                # первая выдача упёрлась в 80, но второй раунд добрал остальное


def test_second_round_collects_beyond_limit_without_warning(mod):
    fake = mod([item(i, i % 30, i // 30) for i in range(100)])
    out = m.unforbid_area(0, 0, 40, 40)
    assert all(not it["forbidden"] for it in fake.items.values()) and "Разрешено 100" in out


def test_partial_error_is_reported(mod, monkeypatch):
    fake = mod([item(1, 5, 5), item(2, 6, 5), item(3, 7, 5)])
    real_post = fake.post

    def flaky(cmd):                                              # T2 мод не находит и обрывает пачку
        if "T2" in cmd["thing_id"].split(","):
            ids = cmd["thing_id"].split(",")
            for tid in ids[:ids.index("T2")]:
                fake.items[tid]["forbidden"] = False
            return "ERROR: Thing not found: T2"
        return real_post(cmd)
    monkeypatch.setattr(m, "_post", flaky)
    out = m.unforbid_area(0, 0, 10, 10)
    assert "Разрешено 2 предметов" in out and "Не удалось 1" in out and "Thing not found: T2" in out
    assert not fake.items["T1"]["forbidden"] and not fake.items["T3"]["forbidden"]


def test_empty_area(mod):
    mod([item(1, 50, 50)])
    out = m.unforbid_area(0, 0, 10, 10)
    assert "запрещённого" in out and "нет" in out and "Разрешено" not in out


def test_limit_hit_with_nothing_in_area_warns(mod):
    mod([item(i, 100 + i % 10, 100 + i // 10) for i in range(120)])        # 80 ближайших к центру области лежат снаружи
    out = m.unforbid_area(0, 0, 20, 20)
    assert "нет" in out and "возможно" in out and "80" in out


def test_invalid_parameters(mod):
    fake = mod([item(1, 5, 5)])
    assert m.unforbid_area(1.5, 0, 5, 5).startswith("ERROR: x1 должен быть целым")
    assert m.unforbid_area(0, "a", 5, 5).startswith("ERROR: z1 должен быть целым")
    assert m.unforbid_area(True, 0, 5, 5).startswith("ERROR")
    assert m.unforbid_area(-1, 0, 5, 5).startswith("ERROR") and "отрицательными" in m.unforbid_area(-1, 0, 5, 5)
    assert "слишком большая" in m.unforbid_area(0, 0, 300, 5)           # 301 клетка по x
    assert m.unforbid_area(0, 0, 299, 299).startswith("Разрешено")           # ровно 300x300 допустимо
    assert m.unforbid_area(0, 0, 5, 5, category="ружья").startswith("ERROR: category")
    assert fake.posts == ["T1"]                                  # поддельный мод звали только допустимым вызовом 300x300


def test_no_mod(monkeypatch):
    monkeypatch.setattr(m, "_need_mod", lambda cmd: f"ERROR: мод старой версии, команда «{cmd}»")
    assert m.unforbid_area(0, 0, 5, 5).startswith("ERROR: мод старой версии")


def test_error_from_get_items_is_passed_through(monkeypatch):
    monkeypatch.setattr(m, "_need_mod", lambda cmd: None)
    monkeypatch.setattr(m, "_query", lambda cmd: "ERROR: игра не отвечает")
    assert m.unforbid_area(0, 0, 5, 5) == "ERROR: игра не отвечает"

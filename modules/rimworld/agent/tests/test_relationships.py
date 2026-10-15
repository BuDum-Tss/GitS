"""Отношения: действующий состав из игры, выбывшие, давние записи."""
import time

import relationships as rl
from relationships import Relationships


def pawns(*names):
    return [{"name": n} for n in names]


def test_new_colonists_added_and_missing_marked_gone(tmp_path):
    r = Relationships(str(tmp_path))
    r.sync_colonists(pawns("Boone", "Fell"))
    assert r.get("Boone")["alive"] and r.get("Fell")["alive"]
    r.sync_colonists(pawns("Boone"))                       # Fell ушёл
    assert r.get("Boone")["alive"] and not r.get("Fell")["alive"]
    assert r.get("Fell")["reasons"][-1] == "ушёл из колонии"


def test_empty_list_means_no_game_not_everyone_gone(tmp_path):
    r = Relationships(str(tmp_path))
    r.sync_colonists(pawns("Boone"))
    r.sync_colonists([])
    assert r.get("Boone")["alive"]


def test_returning_colonist_is_alive_again_but_dead_stay_dead(tmp_path):
    r = Relationships(str(tmp_path))
    r.sync_colonists(pawns("Boone", "Fell"))
    r.sync_colonists(pawns("Boone"))
    r.sync_colonists(pawns("Boone", "Fell"))               # загрузили прежний сейв
    assert r.get("Fell")["alive"]
    r.mark_dead("Fell", "кровь")
    r.sync_colonists(pawns("Boone", "Fell"))
    assert not r.get("Fell")["alive"]                      # погибший не воскресает от того, что имя ещё в списке


def test_old_dead_are_pruned_and_hidden_from_context(tmp_path):
    r = Relationships(str(tmp_path))
    r.sync_colonists(pawns("Boone", "Old", "Recent"))
    r.mark_dead("Old", "кровь")
    r.mark_dead("Recent", "кровь")
    data = r._load()
    data["Old"]["ts"] = time.time() - (rl.DEAD_KEEP_DAYS + 1) * 86400
    data["Recent"]["ts"] = time.time() - 3 * 3600          # погиб три часа назад: в контексте уже не нужен
    r._save(data)
    assert "Old" in r._load() and "Old" not in r.summary()
    r.sync_colonists(pawns("Boone"))
    assert "Old" not in r._load() and "Recent" in r._load()
    assert "Recent" not in r.summary()


def test_stale_alive_pawns_from_old_colonies_are_hidden(tmp_path):
    r = Relationships(str(tmp_path))
    r.sync_colonists(pawns("Cassie", "Boone"))
    data = r._load()
    data["Cassie"]["ts"] = data["Cassie"]["seen"] = time.time() - (rl.STALE_HOURS + 1) * 3600   # игра её давно не видела
    r._save(data)
    r.adjust("Cassie", 10, "было дело")
    r.adjust("Boone", 10, "было дело")
    data = r._load()
    data["Cassie"]["ts"] = time.time() - (rl.STALE_HOURS + 1) * 3600
    r._save(data)
    assert "Cassie" not in r.summary() and "Boone" in r.summary()
    assert rl.is_hidden("Cassie", r._load()["Cassie"], time.time()) and not rl.is_hidden("игрок", {"alive": True}, time.time())

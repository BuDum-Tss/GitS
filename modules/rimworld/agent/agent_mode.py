"""Режим игрового агента: наблюдает, действует (ход) или планирует. Нужен боту (понятный контекст) и зрителю стрима
(дашборд и оверлей показывают, что она сейчас делает и почему). Ничего не запрещает: только отражает происходящее."""
import time

MODES = {"observe": "наблюдает", "act": "действует", "plan": "планирует", "tactic": "в тактике (бой)"}
_state = {"mode": "observe", "reason": "", "since": time.time(), "game_mode": "observe"}
_history: list[dict] = []


def set_mode(mode: str, reason: str = ""):
    if mode not in MODES:
        return
    if mode == _state["mode"] and reason == _state["reason"]:
        return
    _history.append({**_state, "until": time.time()})
    del _history[:-8]
    _state.update(mode=mode, reason=reason[:160], since=time.time())


def set_game_mode(mode_id: str) -> None:
    """Режим хода игрового агента (game_modes): для дашборда, где у каждого режима свой набор инструментов."""
    _state["game_mode"] = mode_id


def get() -> dict:
    return {**_state, "label": MODES[_state["mode"]], "recent": [
        {"mode": h["mode"], "label": MODES[h["mode"]], "reason": h["reason"], "seconds": round(h["until"] - h["since"])}
        for h in reversed(_history[-5:])]}


def line() -> str:
    """Одна фраза для подсказки (комментатор): что она делает сейчас."""
    s = get()
    return f"Сейчас она {s['label']}" + (f": {s['reason']}" if s["reason"] else "") + "."


def graph() -> dict:
    """Подграф профиля RimWorld для дашборда: режимы хода (game_modes) с их инструментами, переходы через switch_mode, текущий режим подсвечен."""
    import game_modes
    cur = _state.get("game_mode") or game_modes.DEFAULT
    nodes = [{"id": "agent", "label": "Агент RimWorld", "root": True, "current": False,
              "title": "Базовые инструменты есть в любом режиме", "tools": list(game_modes.CORE), "kind": "core"}]
    for m in game_modes.MODES.values():
        nodes.append({"id": m.id, "label": m.title, "title": m.purpose, "current": m.id == cur, "tools": list(m.tools), "kind": "mode"})
    # переходы: наблюдение это центр, из него в любой режим и обратно (switch_mode); бой и тревога достижимы из любого режима
    edges = [{"from": "agent", "to": game_modes.DEFAULT, "label": "старт"}]
    for m in game_modes.MODES:
        if m != game_modes.DEFAULT:
            edges.append({"from": game_modes.DEFAULT, "to": m, "label": ""})
    edges.append({"from": "build", "to": "observe", "label": "готово"})
    return {"nodes": nodes, "edges": edges, "current": cur}

"""Презентеры дашборда (ТЗ 29, ARC-8): из данных в JSON-ответ. Чистые функции: ни `request`, ни хранилищ, ни настроек;
данные приходят аргументами, читает их роутер. Формат ответов прежний."""
import logging
import os
import time

logger = logging.getLogger("dashboard")


def screen_state(modules) -> dict | None:
    """Состояние модуля desktop для вкладки «Что она видит» (DESK-15): None, если модуль не подключён."""
    if modules is None:
        return None
    rec = modules.get("desktop")
    if rec is None or not rec.connected or not rec.state:
        return None
    data = rec.state.get("data") or {}
    return {"summary": rec.state.get("summary"), "foreground_app": data.get("foreground_app"),
            "sensitive": data.get("sensitive"), "idle_s": data.get("idle_s"), "activity": data.get("activity")}


def relations_view(rel: dict, now: float | None = None) -> list[dict]:
    """Люди из relationships.json: до 12 не колонистов и до 12 колонистов, по убыванию |симпатии|."""
    from relationships import is_hidden, kind_of, label
    now = time.time() if now is None else now
    rows = sorted(
        ({"name": r.get("name", n), "affinity": r.get("affinity", 0), "label": label(r.get("affinity", 0)),
          "alive": r.get("alive", True), "kind": kind_of(n, r), "source": r.get("source", ""),
          "last": (r.get("notes") or [""])[-1]} for n, r in rel.items()
         if not is_hidden(n, r, now)),
        key=lambda x: -abs(x["affinity"]))
    return [x for x in rows if x["kind"] != "pawn"][:12] + [x for x in rows if x["kind"] == "pawn"][:12]


def approval_view(pending: bool, text, info: dict | None) -> dict:
    return {"pending": pending, "text": text,
            **{k: v for k, v in (info or {}).items() if k in ("kind", "options", "source", "queued")}}


def core_payload(*, dialogue: list, activity: list, screen, memories: list, relations: list, approval: dict) -> dict:
    return {"dialogue": dialogue[-40:], "activity": activity[-5:], "screen": screen,
            "memories": sorted(memories, key=lambda i: -i["ts"])[:150], "relations": relations, "approval": approval}


def state_payload(*, game: dict, emotion, settings_dict: dict, character: dict, presence, modules: list) -> dict:
    """GET /api/state: состояние модуля игры (пусто, если он не подключён) и общие поля ядра."""
    connected = bool(game.get("connected"))
    # Реплика ассистентки (PET-10): файла реплики и полей say / last_decision больше нет; питомец и другие модули получают речь толчком,
    # темой presence (speaking, segment, last_say), а не опросом этого эндпоинта.
    return {
        "connected":        connected,
        "colony_name":      game.get("colony_name"),
        "tick":             game.get("tick"),
        "pawns":            game.get("pawns", []),
        "resources":        game.get("resources", {}),
        "research":         game.get("research", {"current": None, "progress": 0.0}),
        "game_decision":    game.get("game_decision") if connected else None,
        "recent_decisions": game.get("recent_decisions", []),
        "ambition":         game.get("ambition"),
        "goals":            game.get("goals", []),
        "tasks":            game.get("tasks", []),
        "mode":             game.get("mode"),
        "pawn_fates":       game.get("pawn_fates", {}),
        "emotion":          emotion,
        "settings":         settings_dict,
        "character":        character,
        "presence":         presence,                                    # как тема presence (CORE-39)
        "modules":          [{"id": m["id"], "connected": m["connected"], "enabled": m["enabled"]} for m in modules],
    }


def graph_view(snap: dict, *, status: str, pending) -> dict:
    edges = snap.get("edges", [])
    return {
        "enabled": True, "status": status, "pending": pending, "nodes": snap.get("entities", []),
        "current": [e for e in edges if not e["invalid_at"]], "outdated": [e for e in edges if e["invalid_at"]],
        "entities": len(snap.get("entities", [])), "error": snap.get("error"),
    }


def jobs_view(jobs: list[dict]) -> list[dict]:
    keys = ("module", "job_id", "title", "state", "stage", "progress", "text", "question", "artifacts", "updated")
    return [{k: j.get(k) for k in keys} for j in jobs]


def panel_view(rec) -> dict:
    stored = rec.panel_blocks or {}
    return {"id": rec.id, "panel": rec.panel, "blocks": stored.get("blocks", []),
            "ts": stored.get("ts"), "connected": rec.connected}


def check_setting_value(spec: dict, value):
    """Значение настройки по схеме модуля (ТЗ 01, 6.3): (значение, ошибка)."""
    kind = spec.get("type")
    if kind == "boolean":
        ok = isinstance(value, bool)
    elif kind == "integer":
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif kind == "number":
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    else:
        ok = isinstance(value, str)
    if not ok:
        return None, f"значение должно быть типа {kind}"
    if kind in ("integer", "number"):
        if "min" in spec and value < spec["min"]:
            return None, f"значение меньше {spec['min']}"
        if "max" in spec and value > spec["max"]:
            return None, f"значение больше {spec['max']}"
    if spec.get("enum") and value not in spec["enum"]:
        return None, f"значение должно быть одним из {spec['enum']}"
    return value, None


def rimworld_url() -> str:
    """Адрес API модуля RimWorld (пульт отметок оценок): RIMWORLD_MODULE_URL, запасная старая RIMWORLD_AGENT_URL (ТЗ 11, RW-14b)."""
    if os.getenv("RIMWORLD_MODULE_URL"):
        return os.environ["RIMWORLD_MODULE_URL"]
    if os.getenv("RIMWORLD_AGENT_URL"):
        logger.warning("Переменная RIMWORLD_AGENT_URL устарела: переименуй в RIMWORLD_MODULE_URL")
        return os.environ["RIMWORLD_AGENT_URL"]
    return "http://127.0.0.1:8767"

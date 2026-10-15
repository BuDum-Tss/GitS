"""Что агент должен знать о положении колонии и карты: возраст, новое ли поселение, разведана ли карта.

Чистые функции (без игры и сети): строят блоки для подсказки хода из состояния игры и сохранённого обзора карты.
Обзор карты (`survey_map`) хранится в memory/map_survey.json вместе с save_id: чужой обзор из прошлой колонии не показывается."""
import json
import time
from pathlib import Path

TICKS_PER_DAY = 60000
NEW_COLONY_DAYS = 5          # до этого возраста поселение считается новым и ещё не обжитым
_FILE = Path(__file__).parent / "memory" / "map_survey.json"


def load_survey(save_id: str) -> dict | None:
    try:
        data = json.loads(_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if data.get("save_id") == save_id else None


def save_survey(save_id: str, survey: dict) -> None:
    try:
        _FILE.parent.mkdir(parents=True, exist_ok=True)
        _FILE.write_text(json.dumps({"save_id": save_id, "ts": time.time(), "survey": survey}, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def age_days(state: dict) -> float:
    return float(state.get("tick") or 0) / TICKS_PER_DAY


def is_new_colony(state: dict) -> bool:
    colony = state.get("colony") or {}
    return age_days(state) < NEW_COLONY_DAYS or len(colony.get("buildings") or []) < 4


def age_block(state: dict) -> str:
    """Возраст колонии и, если поселение новое, чёткое «это новое место»: прежние координаты и планы к нему не относятся."""
    if not state:
        return ""
    colony = state.get("colony") or {}
    days, pawns, buildings = age_days(state), len(colony.get("pawns") or []), len(colony.get("buildings") or [])
    lines = ["=== ВОЗРАСТ КОЛОНИИ ===", f"Идёт день {days:.1f} (тик {state.get('tick')}), колонистов {pawns}, построек {buildings}."]
    if is_new_colony(state):
        lines.append(
            "ЭТО НОВОЕ ПОСЕЛЕНИЕ: колонисты только что высадились на НОВУЮ незнакомую карту. Всё, что ты помнила про прошлые колонии "
            "(координаты, места построек, цели, потери), относится к другим картам и здесь НЕ действует. Опирайся на то, что видишь сейчас. "
            "Начни с разведки (survey_map, analyze_area у лагеря), потом расставь приоритеты работ и выбери место для первой комнаты.")
    return "\n".join(lines) + "\n"


def survey_block(state: dict, loaded: dict | None) -> str:
    """Короткая сводка обзора карты для подсказки; если обзора нет у новой колонии, требование сделать его."""
    if loaded and loaded.get("survey"):
        sv = loaded["survey"]
        out = ["=== КАРТА (обзор survey_map, ближайшее к колонии) ==="]
        m = sv.get("map") or {}
        out.append(f"Карта {m.get('w')}x{m.get('h')}, биом {m.get('biome')}, колония около ({(sv.get('colony_center') or {}).get('x')},{(sv.get('colony_center') or {}).get('z')}).")
        for key, title in (("water", "вода"), ("forest", "лес"), ("fertile", "плодородная земля"), ("ruins", "руины")):
            items = sv.get(key) or []
            if items:
                out.append(f"- {title}: " + "; ".join(f"({i['x']},{i['z']}) ~{i['dist']} кл." for i in items[:3]))
        for ore, items in (sv.get("ores") or {}).items():
            if items:
                out.append(f"- {ore}: " + "; ".join(f"({i['x']},{i['z']}) ~{i['dist']} кл." for i in items[:2]))
        return "\n".join(out) + "\nПодробности и схема: survey_map. Руины рядом: analyze_area(x, z).\n"
    if state and is_new_colony(state):
        return "=== КАРТА НЕ РАЗВЕДАНА ===\nСделай survey_map() и analyze_area(x, z) вокруг лагеря, прежде чем размечать постройки.\n"
    return ""

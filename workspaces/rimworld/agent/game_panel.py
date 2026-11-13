"""Пространство модуля RimWorld в дашборде ядра (ТЗ 15, TAB-5 и TAB-9): ссылки на оверлей OBS и пульт оценок, блоки состояния
игры, колонистов, последних решений, оценок и записи забега.

Блоки собирает чистая функция `panel_blocks` из того же состояния, что уходит ядру в `state` (`game_api.game_snapshot`),
поэтому здесь нет ни сети, ни окна. Тексты от игры (имена колонистов, решения модели) выводятся дашбордом экранированными.
"""
from __future__ import annotations

import copy
import os
import time

OVERLAY_URL = "http://127.0.0.1:8767/overlay"          # оверлей OBS (game_api.py)
SCORES_URL = "http://127.0.0.1:8766/"                   # пульт отметок оценок: вкладка «Состояние» дашборда ядра

PANEL = {
    "title": "RimWorld",
    "icon": "gamepad",
    "order": 20,
    "links": [
        {"id": "overlay", "label": "Оверлей OBS", "kind": "url", "url": OVERLAY_URL},
        {"id": "scores", "label": "Пульт оценок", "kind": "url", "url": SCORES_URL},
    ],
    "actions": ["take_focus", "release_focus"],        # кнопки фокуса (правка fix/focus-bootstrap, см. module_manifest.attach)
}

PHASE_LABEL = {"none": "не запущена", "menu": "меню", "colony": "колония загружена"}
MAX_PAWNS, MAX_DECISIONS, MAX_APPRAISALS = 20, 8, 8
MAX_RULES, MAX_FIRED = 20, 8


def _clip(value, n: int) -> str:
    return " ".join(str(value if value is not None else "").split())[:n]


def recording_info(rec) -> dict | None:
    """Статус записи забега из RunRecorder.current (файл и длительность), или None, если записи нет."""
    if rec is None:
        return None
    started = getattr(rec, "started", None)
    raw = getattr(rec, "raw", None)
    return {"file": os.path.basename(str(raw)) if raw else "", "minutes": int((time.time() - started) // 60) if started else 0,
            "colony": _clip(getattr(rec, "colony", ""), 80)}


def policy_blocks(data: dict) -> list[dict]:
    """ТЗ 11e, POL-7: политика игрового агента, правила наблюдения и что сработало (тексты экранирует дашборд)."""
    pol = data.get("policy") or {}
    if not pol:
        return []
    focus = data.get("focus") or {}
    items = [
        {"label": "Сейчас", "value": _clip(pol.get("title") or pol.get("id"), 60)},
        {"label": "Почему", "value": _clip(pol.get("reason"), 160)},
        {"label": "Минут в ней", "value": str(int(pol.get("minutes") or 0))},
        {"label": "Можно перейти", "value": _clip(", ".join(pol.get("transitions") or []) or "никуда", 80)}]
    if focus:                                       # правка fix/focus-bootstrap: режим фокуса и причина, почему агент молчит
        items.append({"label": "Режим фокуса", "value": _clip(focus.get("mode_label"), 120)})
        if focus.get("why_afk"):
            items.append({"label": "Почему вне фокуса", "value": _clip(focus.get("why_afk"), 200)})
    out: list[dict] = [{"type": "stats", "title": "Политика", "items": items}]
    if focus:
        out.append({"type": "buttons", "title": "Фокус", "items": [
            {"action": "take_focus", "label": "Взять фокус"},
            {"action": "release_focus", "label": "Отпустить фокус"}]})
    rules = data.get("watch_rules") or []
    if rules:
        rows = [{"cells": [_clip(r.get("kind"), 40), _clip(f"{r.get('field')}={r.get('value')}" if r.get("field") else "любое", 60),
                           f"+{int(r.get('boost') or 0)}", _clip(r.get("switch_to") or "-", 20),
                           str(r.get("hours_left", ""))]} for r in rules[:MAX_RULES]]
        out.append({"type": "table", "title": "Правила наблюдения", "columns": ["Событие", "Фильтр", "Усиление", "Политика",
                                                                               "Осталось, ч"], "rows": rows})
    else:
        out.append({"type": "text", "title": "Правила наблюдения", "text": "Правил наблюдения нет."})
    fired = (data.get("watch_fired") or [])[-MAX_FIRED:]
    if fired:
        out.append({"type": "list", "title": "Сработали", "items": [
            {"text": _clip(f.get("text"), 200), "sub": _clip(f.get("why"), 200), "badge": _clip(f.get("switch_to") or "", 20)}
            for f in reversed(fired)]})
    return out


def panel_blocks(data: dict, recording: dict | None = None) -> list[dict]:
    """Блоки пространства по состоянию игры (`data` из state: connected, colony_name, pawns, ambition, mode, game_phase, ...)."""
    phase = str(data.get("game_phase") or "none")
    ambition = (data.get("ambition") or {}).get("title")
    mode = (data.get("mode") or {}).get("label") or ""
    blocks: list[dict] = [
        {"type": "stats", "title": "Сейчас", "items": [
            {"label": "Игра", "value": PHASE_LABEL.get(phase, phase)},
            {"label": "Колония", "value": _clip(data.get("colony_name") or "нет", 80)},
            {"label": "Режим агента", "value": _clip(mode or "нет", 60)},
            {"label": "Цель", "value": _clip(ambition or "не поставлена", 120)}]},
    ]
    blocks.extend(policy_blocks(data))
    pawns = data.get("pawns") or []
    if pawns:
        rows = []
        for p in pawns[:MAX_PAWNS]:
            health = p.get("health_pct")
            health_text = f"{round(float(health) * 100)} %" if isinstance(health, (int, float)) else ""
            rows.append({"cells": [_clip(p.get("name"), 60), _clip(p.get("job"), 120),
                                   _clip(p.get("mood_label"), 40), health_text]})
        blocks.append({"type": "table", "title": "Колонисты", "columns": ["Имя", "Занятие", "Настроение", "Здоровье"],
                       "rows": rows})
    else:
        blocks.append({"type": "text", "title": "Колонисты", "text": "Колонистов пока нет (колония не загружена)."})
    decisions = (data.get("recent_decisions") or [])[-MAX_DECISIONS:]
    if decisions:
        blocks.append({"type": "list", "title": "Последние решения", "items": [
            {"text": _clip(d.get("directive") or d.get("text") or "решение", 200),
             "sub": _clip(", ".join(str(a) for a in (d.get("actions") or [])), 200),
             "badge": _clip(d.get("tick") or "", 20)} for d in reversed(decisions)]})
    appraisals = (data.get("appraisals") or [])[:MAX_APPRAISALS]
    if appraisals:
        blocks.append({"type": "list", "title": "Оценки событий", "items": [
            {"text": _clip(a.get("text") or a.get("event") or a.get("kind") or "оценка", 200),
             "sub": _clip(a.get("outcome") or "", 200),
             "badge": _clip(a.get("label") or "", 20)} for a in appraisals]})
    if recording:
        blocks.append({"type": "text", "title": "Запись забега",
                       "text": f"Идёт запись: {recording.get('file') or 'файл'}, {int(recording.get('minutes') or 0)} мин."})
    else:
        blocks.append({"type": "text", "title": "Запись забега", "text": "Запись сейчас не ведётся."})
    return blocks


def panel_spec() -> dict:
    """Свежая копия поля `panel` манифеста."""
    return copy.deepcopy(PANEL)

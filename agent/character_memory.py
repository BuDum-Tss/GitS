"""Воспоминания персонажа о себе (ТЗ 05, CHR-15, CHR-19): проверка memories.yaml, отбор в контекст, блок для промпта.

Чистые функции, без графа и модели: их проверяют тесты. Хранение и поиск по смыслу в графе (graph_memory.GraphMemory).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from character import CharacterError

TYPES = ("biography", "self", "opinion", "relationship", "habit", "gag", "taboo")   # CHR-15
NUDGE_TYPES = ("habit", "opinion")     # CHR-19: «напоминание» о привычке или мнении, когда ничего не подошло
NUDGE_EVERY = 5                        # CHR-19: раз в N реплик
TEXT_MAX = 400
N_PICK = 3                             # CHR-19: до трёх воспоминаний к реплике


def load_memories(path: Path) -> list[dict[str, Any]]:
    """Прочитать и проверить memories.yaml. Нет файла — пустой список. Ошибки — CharacterError с указанием записи."""
    if not path.is_file():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    except (OSError, yaml.YAMLError) as e:
        raise CharacterError(f"{path}: не читается ({e})") from e
    if not isinstance(data, list):
        raise CharacterError(f"{path}: ожидается список записей")
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for i, item in enumerate(data, 1):
        where = f"{path.name}, запись {i}"
        if not isinstance(item, dict):
            raise CharacterError(f"{where}: ожидается словарь полей")
        mem_id = str(item.get("id") or "").strip()
        if not mem_id:
            raise CharacterError(f"{where}: нужен id")
        if mem_id in seen:
            raise CharacterError(f"{where}: id «{mem_id}» повторяется")
        seen.add(mem_id)
        mtype = str(item.get("type") or "")
        if mtype not in TYPES:
            raise CharacterError(f"{where}: type «{mtype}» не из {list(TYPES)}")
        text = " ".join(str(item.get("text") or "").split())
        if not text or len(text) > TEXT_MAX:
            raise CharacterError(f"{where}: text пустой или длиннее {TEXT_MAX} символов")
        about = item.get("about", ["self"])
        about = [about] if isinstance(about, str) else list(about or [])
        if not about or not all(str(a).strip() for a in about):
            raise CharacterError(f"{where}: about не может быть пустым")
        try:
            weight = float(item.get("weight", 0.5))
        except (TypeError, ValueError) as e:
            raise CharacterError(f"{where}: weight должен быть числом от 0 до 1") from e
        if not 0.0 <= weight <= 1.0:
            raise CharacterError(f"{where}: weight {weight} вне диапазона 0..1")
        out.append({"id": mem_id, "type": mtype, "text": text, "about": [str(a).strip() for a in about],
                    "tags": [str(t) for t in (item.get("tags") or [])], "weight": weight})
    return out


def pick(cands: list[dict[str, Any]], scores: list[float], *, n: int = N_PICK, min_score: float,
         nudge: bool = False) -> tuple[list[dict[str, Any]], bool]:
    """Отбор воспоминаний к реплике (CHR-19). Порог по смыслу как у фактов памяти; порядок relevance × (0.5 + weight).

    Если ни одно не прошло порог и наступил черёд «напоминания» (nudge), берём одно самое весомое мнение или привычку,
    чтобы характер не выцветал. Возвращает (отобранные, было ли напоминание)."""
    ranked = []
    for c, s in zip(cands, scores, strict=False):
        if s >= min_score:
            ranked.append(dict(c, score=round(float(s), 3)))
    ranked.sort(key=lambda c: -(c["score"] * (0.5 + float(c.get("weight") or 0))))
    if ranked:
        return ranked[:n], False
    if nudge:
        habits = [c for c in cands if c.get("mtype") in NUDGE_TYPES]
        if habits:
            best = max(habits, key=lambda c: float(c.get("weight") or 0))
            return [dict(best, score=0.0, nudge=True)], True
    return [], False


def block(items: list[dict[str, Any]]) -> str:
    """Блок для промпта: «Твои воспоминания (это о тебе)». Пусто, если отбора нет."""
    if not items:
        return ""
    return "Твои воспоминания (это о тебе):\n" + "\n".join(f"- {c['fact']}" for c in items)

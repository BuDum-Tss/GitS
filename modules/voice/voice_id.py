"""Узнавание людей по голосу, локально (без Neo4j): сырой голосовой эмбеддинг → стабильный person_id.

Диаризация WhisperX даёт метки SPEAKER_00, которые заново назначаются на каждом фрагменте: по ним человека не
узнать. Эмбеддинг говорящего (pyannote wespeaker) стабилен, поэтому его сравнивают с уже известными голосами
(косинусное сходство); достаточно похожий считается тем же человеком, иначе заводится новый person-xxxxxxxx.
Центр голоса уточняется с каждым узнаванием. Голос самой ГЛаДОС (через колонки) хранится под «self»: так она не
отвечает на своё эхо. Хранилище: memory/voices.json (эмбеддинги это биометрия, файл не в git).
Идея и порог взяты из GitS (src/memory/voice_id.py), хранение заменено на локальный файл.
"""
import json
import time
import uuid
from pathlib import Path

import numpy as np

SELF_PERSON_ID = "self"
MATCH_THRESHOLD = 0.75      # косинусное сходство, с которого голос считается знакомым (калибровать на реальных голосах)
_MAX_WEIGHT = 40            # центр голоса перестаёт «тяжелеть»: голос может плавно меняться


class VoiceIdentity:
    def __init__(self, memory_dir: str, threshold: float = MATCH_THRESHOLD):
        self._file = Path(memory_dir) / "voices.json"
        self._thr = threshold
        Path(memory_dir).mkdir(parents=True, exist_ok=True)

    # ── хранилище ────────────────────────────────────────────────────────────

    def rebind(self, memory_dir: str) -> None:
        """Переключение персонажа (ТЗ 05, CHR-12): профили голосов в папке этого персонажа."""
        self._file = Path(memory_dir) / "voices.json"
        Path(memory_dir).mkdir(parents=True, exist_ok=True)

    def _load(self) -> dict:
        try:
            d = json.loads(self._file.read_text(encoding="utf-8"))
        except Exception:
            d = {}
        d.setdefault("people", {})
        d.setdefault("owner", None)
        return d

    def _save(self, d: dict):
        self._file.write_text(json.dumps(d), encoding="utf-8")

    @staticmethod
    def _cos(a: np.ndarray, b: np.ndarray) -> float:
        na, nb = np.linalg.norm(a), np.linalg.norm(b)
        return float(a @ b / (na * nb)) if na and nb else 0.0

    # ── узнавание ────────────────────────────────────────────────────────────

    def resolve(self, embedding: list[float] | None) -> tuple[str | None, bool]:
        """Вернуть (person_id, новый_ли). (None, False), если эмбеддинга нет или он негодный."""
        if not embedding:
            return None, False
        vec = np.asarray(embedding, dtype=np.float32)
        if vec.ndim != 1 or not np.isfinite(vec).all() or not vec.any():
            return None, False
        d = self._load()
        best, best_pid = -1.0, None
        for pid, rec in d["people"].items():
            c = np.asarray(rec["embedding"], dtype=np.float32)
            if c.shape != vec.shape:
                continue
            s = self._cos(vec, c)
            if s > best:
                best, best_pid = s, pid
        now = time.time()
        if best_pid is not None and best >= self._thr:
            rec = d["people"][best_pid]
            if best_pid != SELF_PERSON_ID:      # голос агента не «плывёт»: он фиксирован синтезатором
                w = min(rec.get("count", 1), _MAX_WEIGHT)
                rec["embedding"] = ((np.asarray(rec["embedding"], dtype=np.float32) * w + vec) / (w + 1)).tolist()
                rec["count"] = rec.get("count", 1) + 1
            rec["last_seen"] = now
            self._save(d)
            return best_pid, False
        pid = f"person-{uuid.uuid4().hex[:8]}"
        d["people"][pid] = {"embedding": vec.tolist(), "count": 1, "name": None, "first_seen": now, "last_seen": now}
        self._save(d)
        return pid, True

    # ── имена, владелец, свой голос ──────────────────────────────────────────

    def set_name(self, person_id: str, name: str) -> bool:
        d = self._load()
        if person_id not in d["people"]:
            return False
        d["people"][person_id]["name"] = name.strip()
        self._save(d)
        return True

    def name_of(self, person_id: str | None) -> str | None:
        return self._load()["people"].get(person_id or "", {}).get("name")

    def names(self) -> dict[str, str]:
        return {p: r["name"] for p, r in self._load()["people"].items() if r.get("name")}

    def set_owner(self, person_id: str) -> bool:
        d = self._load()
        if person_id not in d["people"] or person_id == SELF_PERSON_ID:
            return False
        d["owner"] = person_id
        self._save(d)
        return True

    def owner(self) -> str | None:
        return self._load().get("owner")

    def has_self(self) -> bool:
        return SELF_PERSON_ID in self._load()["people"]

    def register_self(self, embedding: list[float]):
        d = self._load()
        d["people"][SELF_PERSON_ID] = {"embedding": list(map(float, embedding)), "count": 1, "name": "ГЛаДОС",
                                       "first_seen": time.time(), "last_seen": time.time()}
        self._save(d)

    def people(self) -> list[dict]:
        d = self._load()
        return [{"id": p, "name": r.get("name"), "count": r.get("count", 1), "last_seen": r.get("last_seen"),
                 "owner": p == d["owner"]} for p, r in d["people"].items() if p != SELF_PERSON_ID]

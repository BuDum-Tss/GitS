"""Отношения персонажа к колонистам и к игроку.

Хранятся в memory/relationships.json: для каждого субъекта (имя колониста
или "игрок") — симпатия от -100 до +100 и несколько последних причин.
Отношения попадают в контекст Claude (build_context_block) и в подсказку
быстрого слоя, поэтому влияют и на реплики, и на решения.

Меняются тремя способами:
  1. автоматически по событиям (смерть, сообщения игрока);
  2. самим Claude через инструмент set_opinion;
  3. медленно затухают к нейтральному — обиды и симпатии не вечны.
"""
import json
import time
from pathlib import Path

PLAYER = "игрок"
_MIN, _MAX = -100.0, 100.0
_DECAY_PER_HOUR = 0.5      # пунктов к нулю за реальный час
_MAX_REASONS = 5
DEAD_SHOWN_HOURS = 2         # погибшего помним в контексте и интерфейсе столько часов
STALE_HOURS = 12              # персонаж игры, которого игра давно не видела, считается устаревшим (колония сменилась)
DEAD_KEEP_DAYS = 3           # записи погибших и ушедших старше этого срока удаляются

_LABELS = [
    (-60, "презирает"), (-25, "раздражает"), (-8, "недолюбливает"),
    (8, "нейтрально"), (25, "терпит с интересом"), (60, "симпатия"),
    (101, "привязанность"),
]


def is_hidden(name: str, rec: dict, now: float) -> bool:
    """Не показывать в контексте и интерфейсе: давно погибшие и ушедшие, а также «живые» из прежних колоний,
    которых игра уже давно не видела."""
    t = rec.get("ts", now)
    if not rec.get("alive", True):
        return now - t > DEAD_SHOWN_HOURS * 3600
    return kind_of(name, rec) == "pawn" and now - max(rec.get("seen", 0), t) > STALE_HOURS * 3600


def kind_of(name: str, rec: dict) -> str:
    """player — пользователь, person — живой собеседник (Telegram), pawn — персонаж игры."""
    return rec.get("kind") or ("player" if name == PLAYER else "pawn")


def label(affinity: float) -> str:
    for limit, text in _LABELS:
        if affinity < limit:
            return text
    return _LABELS[-1][1]


class Relationships:
    def __init__(self, memory_dir: str = "./memory"):
        self._file = Path(memory_dir) / "relationships.json"
        Path(memory_dir).mkdir(parents=True, exist_ok=True)

    # ── хранилище ────────────────────────────────────────────────────────────

    def rebind(self, memory_dir) -> None:
        """Переключение персонажа (ТЗ 05, CHR-7, CHR-10): отношения этого персонажа в его папке памяти."""
        self._file = Path(memory_dir) / "relationships.json"
        Path(memory_dir).mkdir(parents=True, exist_ok=True)

    def _load(self) -> dict:
        try:
            return json.loads(self._file.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save(self, data: dict):
        self._file.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                              encoding="utf-8")

    @staticmethod
    def _decayed(rec: dict, now: float) -> float:
        hours = max(0.0, (now - rec.get("ts", now)) / 3600)
        aff = rec.get("affinity", 0.0)
        step = _DECAY_PER_HOUR * hours
        if aff > 0:
            return max(0.0, aff - step)
        return min(0.0, aff + step)

    # ── API ──────────────────────────────────────────────────────────────────

    def sync_colonists(self, pawns: list[dict]):
        """Действующий состав берётся из игры: новые колонисты заводятся (нейтральные), а персонажи игры, которых в
        нынешней колонии нет (колония сменилась, ушли), помечаются выбывшими. Давние выбывшие удаляются."""
        data = self._load()
        changed = False
        now = time.time()
        present = set()
        for p in pawns:
            name = p.get("name")
            if not name or p.get("is_prisoner"):
                continue
            present.add(name)
            rec = data.get(name)
            if rec is not None and now - rec.get("seen", 0) > 600:
                rec["seen"] = now                      # «игра видела его только что»
                changed = True
            if rec is None:
                data[name] = {"affinity": 0.0, "reasons": [], "ts": now, "alive": True, "kind": "pawn"}
                changed = True
            elif not rec.get("alive", True) and rec.get("kind", "pawn") == "pawn" and rec.get("reasons", [""])[-1:] == ["ушёл из колонии"]:
                rec.update({"alive": True, "ts": now})        # вернулся (например, загрузили прежний сейв)
                changed = True
        if present:      # пустой список значит «игры нет», а не «все ушли»
            for name, rec in data.items():
                if kind_of(name, rec) == "pawn" and rec.get("alive", True) and name not in present:
                    rec.update({"alive": False, "ts": now, "reasons": (rec.get("reasons", []) + ["ушёл из колонии"])[-_MAX_REASONS:]})
                    changed = True
        stale = [n for n, r in data.items() if kind_of(n, r) == "pawn" and not r.get("alive", True)
                 and now - r.get("ts", now) > DEAD_KEEP_DAYS * 86400]
        for n in stale:
            del data[n]
            changed = True
        if changed:
            self._save(data)

    def ensure_person(self, key: str, name: str, source: str = "", **extra) -> bool:
        """Завести профиль живого человека (например, написавшего в Telegram-бот). True, если он новый."""
        data = self._load()
        if key in data:
            return False
        now = time.time()
        data[key] = {"kind": "person", "name": name, "source": source, "affinity": 0.0, "reasons": [],
                     "ts": now, "first_seen": now, "alive": True, "notes": [], **extra}
        self._save(data)
        return True

    def people_terms(self) -> list[str]:
        """Имена и ники живых собеседников (kind person): личные слова, которые нельзя публиковать (core.privacy_terms)."""
        out = []
        for name, rec in self._load().items():
            if kind_of(name, rec) == "person":
                out += [str(v) for v in (rec.get("name"), rec.get("handle")) if v]
        return out

    def note(self, key: str, text: str):
        """Запомнить последнюю реплику человека (хранится до 5 штук)."""
        data = self._load()
        rec = data.get(key)
        if rec is None:
            return
        rec["notes"] = (rec.get("notes", []) + [text[:200]])[-5:]
        rec["last_seen"] = time.time()
        self._save(data)

    def adjust(self, subject: str, delta: float, reason: str) -> float:
        """Сдвинуть симпатию. Возвращает новое значение."""
        data = self._load()
        now = time.time()
        rec = data.get(subject, {"affinity": 0.0, "reasons": [], "alive": True})
        aff = self._decayed(rec, now) + delta
        aff = max(_MIN, min(_MAX, aff))
        reasons = (rec.get("reasons", []) + [reason[:100]])[-_MAX_REASONS:]
        data[subject] = {**rec, "affinity": round(aff, 1), "reasons": reasons, "ts": now}
        self._save(data)
        return aff

    def mark_dead(self, subject: str, cause: str = ""):
        data = self._load()
        rec = data.get(subject, {"affinity": 0.0, "reasons": []})
        rec.update({"alive": False, "ts": time.time()})
        if cause:
            rec["reasons"] = (rec.get("reasons", []) + [f"умер: {cause}"[:100]])[-_MAX_REASONS:]
        data[subject] = rec
        self._save(data)

    def get(self, subject: str) -> dict | None:
        rec = self._load().get(subject)
        if rec is None:
            return None
        return {**rec, "affinity": round(self._decayed(rec, time.time()), 1)}

    def describe(self, subject: str) -> str:
        """Короткая фраза об отношении — для подсказки быстрого слоя."""
        rec = self.get(subject)
        if rec is None:
            return ""
        return f"отношение к {subject}: {label(rec['affinity'])} ({rec['affinity']:+.0f})"

    def summary(self, limit: int = 8) -> str:
        """Блок для контекста Claude: самые сильные отношения."""
        data = self._load()
        now = time.time()
        rows = []
        for name, rec in data.items():
            if is_hidden(name, rec, now):
                continue        # давно погибших, ушедших и забытых игрой не вспоминаем
            aff = self._decayed(rec, now)
            rows.append((abs(aff), name, aff, rec))
        rows.sort(reverse=True)
        lines = []
        for _, name, aff, rec in rows[:limit]:
            if abs(aff) < 3 and rec.get("alive", True) and kind_of(name, rec) != "person":
                continue
            state = "" if rec.get("alive", True) else " [мёртв]"
            why = rec.get("reasons", [])
            tail = f" — {why[-1]}" if why else ""
            shown = rec.get("name", name) if kind_of(name, rec) == "person" else name
            who = "человек в Telegram" if kind_of(name, rec) == "person" else (
                "персонаж игры" if kind_of(name, rec) == "pawn" else "")
            lines.append(f"  {shown}{state}{f' ({who})' if who else ''}: {label(aff)} ({aff:+.0f}){tail}")
        if not lines:
            return ""
        return ("\nТвои личные отношения (влияют на тон реплик и на то, кого ты "
                "бережёшь, а кем готова пожертвовать):\n" + "\n".join(lines))

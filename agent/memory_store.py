"""Долговременная память ассистентки (memory/memories.json).

Виды записей:
  user     - устойчивые факты о пользователе (имя, привычки, предпочтения);
  self     - о ней самой: что пообещала, какое мнение сложилось, пожелания к её поведению,
             общие шутки («рассказывать больше шуток»);
  plan     - планы пользователя со сроком («завтра курс», «сделать MCP-сервер»);
  episode  - заметные события и итоги бесед.

Запись: id, kind, text, importance (1-5), ts, last_used, uses, due (срок планов, unix или None).
Дубли сливаются (похожие по основам слов), память ограничена и вытесняет наименее ценное.
Поиск по теме разговора: пересечение основ слов + важность + свежесть.
Всё хранится локально; сырой текст экрана и звука сюда не попадает.
"""
import json
import math
import re
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

KINDS = ("user", "self", "plan", "episode")
_MAX = 300
_DUP = 0.6               # доля общих основ, начиная с которой записи считаются дублем
_WORD = re.compile(r"[а-яёa-z0-9]{3,}", re.I)
_STOP = {"что", "как", "это", "про", "мне", "меня", "или", "для", "все", "всё", "обо", "мой", "моя",
         "его", "ему", "она", "они", "при", "над", "под", "без", "через", "тоже", "когда", "если",
         "пользователь", "пользователя", "пользователю", "ассистентка"}


def _stop_words() -> set[str]:
    """Служебные слова и имя с алиасами активного персонажа (ТЗ 05, CHR-4): его имя не бывает темой запоминания."""
    import character
    try:
        return _STOP | {a.lower() for a in character.active().aliases}
    except Exception:
        return _STOP


_WEEKDAYS = [("понедельник", 0), ("вторник", 1), ("сред", 2), ("четверг", 3), ("пятниц", 4),
             ("суббот", 5), ("воскресень", 6)]
_UNDATED_PLAN_DAYS = 14   # план без срока устаревает через столько дней, иначе он не исчезал бы никогда


def _end_of_day(ts: float, plus_days: int) -> float:
    d = datetime.fromtimestamp(ts) + timedelta(days=plus_days)
    return d.replace(hour=23, minute=59, second=0, microsecond=0).timestamp()


def effective_due(it: dict) -> float | None:
    """Срок плана: заданный, иначе по словам в тексте («завтра», «в субботу»), иначе по возрасту."""
    if it.get("due"):
        return it["due"]
    low = it["text"].lower()
    ts = it["ts"]
    if "послезавтра" in low:
        return _end_of_day(ts, 2)
    if "завтра" in low:
        return _end_of_day(ts, 1)
    if "сегодня" in low:
        return _end_of_day(ts, 0)
    for name, wd in _WEEKDAYS:
        if name in low:
            ahead = (wd - datetime.fromtimestamp(ts).weekday()) % 7 or 7
            return _end_of_day(ts, ahead)
    return ts + _UNDATED_PLAN_DAYS * 86400


def _stem(w: str) -> str:
    """Основа слова: отрезаем до двух букв (окончание), но не короче 4 и не длиннее 6 букв.
    Короткие четыре буквы склеивали разные слова («посетить» и «поселенцев»)."""
    w = w.lower()
    return w[:min(6, max(4, len(w) - 2))]


def stems(text: str) -> set[str]:
    stop = _stop_words()
    return {_stem(w) for w in _WORD.findall(text) if w.lower() not in stop}


def _similar(a: str, b: str) -> bool:
    sa, sb = stems(a), stems(b)
    if not sa or not sb:
        return a.strip().lower() == b.strip().lower()
    return len(sa & sb) / min(len(sa), len(sb)) >= _DUP


class MemoryStore:
    def __init__(self, memory_dir: str = "./memory"):
        self._dir = Path(memory_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._file = self._dir / "memories.json"
        self._legacy = self._dir / "user_profile.json"

    # ── хранилище ────────────────────────────────────────────────────────────

    def _load(self) -> dict:
        try:
            data = json.loads(self._file.read_text(encoding="utf-8"))
            data.setdefault("items", [])
            data.setdefault("meta", {})
            return data
        except Exception:
            data = {"items": [], "meta": {}}
        self._migrate(data)
        return data

    def _save(self, data: dict):
        self._file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _migrate(self, data: dict):
        """Перенос старого плоского списка фактов с грубой разметкой видов."""
        try:
            old = json.loads(self._legacy.read_text(encoding="utf-8")).get("facts", [])
        except Exception:
            return
        for f in old:
            text = str(f.get("text", "")).strip()
            low = text.lower()
            kind = "user"
            if re.search(r"планир|завтра|нужно будет|надо будет", low):
                kind = "plan"
            elif re.search(r"рассказывать|шутк|говори|отвечай|не называй", low):
                kind = "self"
            self._add(data, kind, text, 3, f.get("ts", time.time()), None)
        data["meta"]["migrated"] = True
        self._save(data)

    # ── запись ────────────────────────────────────────────────────────────────

    def _add(self, data: dict, kind: str, text: str, importance: int, ts: float, due) -> bool:
        text = re.sub(r"\s+", " ", str(text)).strip()[:300]
        if not text or kind not in KINDS:
            return False
        for it in data["items"]:
            if it["kind"] == kind and _similar(it["text"], text):
                it["importance"] = max(it["importance"], importance)
                it["ts"] = max(it["ts"], ts)
                if len(text) > len(it["text"]):
                    it["text"] = text
                if due and not it.get("due"):
                    it["due"] = due
                return False   # дубль: слито
        data["items"].append({"id": uuid.uuid4().hex[:8], "kind": kind, "text": text,
                              "importance": max(1, min(5, int(importance))), "ts": ts,
                              "last_used": 0.0, "uses": 0, "due": due})
        if len(data["items"]) > _MAX:
            data["items"].sort(key=self._worth)
            data["items"] = data["items"][len(data["items"]) - _MAX:]
        return True

    def add(self, kind: str, text: str, importance: int = 3, due_days: float | None = None) -> bool:
        data = self._load()
        due = time.time() + due_days * 86400 if due_days else None
        added = self._add(data, kind, text, importance, time.time(), due)
        self._save(data)
        return added

    @staticmethod
    def _worth(it: dict) -> float:
        """Ценность записи: важность и свежесть (эпизоды выветриваются быстрее фактов)."""
        age_days = (time.time() - it["ts"]) / 86400
        half = 14 if it["kind"] == "episode" else 90
        return it["importance"] + 2 * math.exp(-age_days / half)

    # ── чтение ────────────────────────────────────────────────────────────────

    def all(self) -> list[dict]:
        return self._load()["items"]

    def kind(self, kind: str) -> list[dict]:
        return [i for i in self.all() if i["kind"] == kind]

    def active_plans(self) -> list[dict]:
        """Планы без срока или со сроком, который не истёк (неделя на «просрочку» даём)."""
        now = time.time()
        return [i for i in self.kind("plan") if effective_due(i) > now - 12 * 3600]

    def plans_to_nudge(self, mark: bool = True) -> list[dict]:
        """Планы, о которых пора напомнить: срок в ближайшие сутки (или только что прошёл) и
        напоминание ещё не было за последние 12 часов. Каждое напоминание помечается."""
        data = self._load()
        now = time.time()
        due_soon = []
        for it in data["items"]:
            if it["kind"] != "plan":
                continue
            due = effective_due(it)
            if due - now > 24 * 3600 or due < now - 2 * 3600:
                continue
            if now - it.get("nudged", 0) < 12 * 3600:
                continue
            due_soon.append(it)
        if mark and due_soon:
            for it in due_soon:
                it["nudged"] = now
            self._save(data)
        return due_soon

    def relevant(self, query: str = "", k: int = 8) -> list[dict]:
        """Записи для контекста: по теме разговора, плюс самое важное всегда."""
        data = self._load()
        q = stems(query)
        now = time.time()
        scored = []
        for it in data["items"]:
            overlap = len(q & stems(it["text"]))
            if it["kind"] == "plan" and (overlap == 0 or effective_due(it) < now - 12 * 3600):
                continue   # планы подмешиваются только по теме разговора и пока не устарели
            scored.append((overlap * 3 + self._worth(it), it))
        scored.sort(key=lambda x: -x[0])
        picked = [it for _, it in scored[:k]]
        for it in sorted((i for i in data["items"] if i["importance"] >= 5 and i["kind"] in ("user", "self")),
                         key=lambda i: -i["ts"])[:2]:
            if it not in picked:
                picked.append(it)
        ids = {i["id"] for i in picked}
        for it in data["items"]:
            if it["id"] in ids:
                it["last_used"], it["uses"] = now, it.get("uses", 0) + 1
        self._save(data)
        return picked

    def search(self, query: str, k: int = 6) -> list[dict]:
        """Что она помнит по теме (для инструмента recall): только реально совпавшие."""
        q = stems(query)
        hits = [(len(q & stems(i["text"])), i) for i in self.all()]
        hits = [h for h in hits if h[0] > 0] if q else []
        hits.sort(key=lambda h: (-h[0], -h[1]["importance"]))
        return [i for _, i in hits[:k]]

    # ── забывание ─────────────────────────────────────────────────────────────

    def forget_matching(self, text: str, strict: bool = False) -> int:
        """Забыть записи (всех видов). Нестрогий режим: «забудь что я люблю чай» находит
        «Любит: чай»; совпадать должна большая часть значимых слов (по трёхбуквенным основам)."""
        data = self._load()
        if strict:
            kept = [i for i in data["items"] if text.lower() not in i["text"].lower()]
        else:
            stop = _stop_words()
            ws = [w.lower() for w in _WORD.findall(text) if w.lower() not in stop]
            keys = [w[:3] if len(w) > 3 else w for w in ws]
            if not keys:
                return 0
            need = max(1, -(-len(keys) * 6 // 10))
            kept = [i for i in data["items"] if sum(k in i["text"].lower() for k in keys) < need]
        removed = len(data["items"]) - len(kept)
        data["items"] = kept
        self._save(data)
        return removed

    def delete(self, item_id: str) -> bool:
        data = self._load()
        n = len(data["items"])
        data["items"] = [i for i in data["items"] if i["id"] != item_id]
        self._save(data)
        return len(data["items"]) < n

    # ── метаданные (когда последний раз осмысляли беседу) ─────────────────────

    def meta(self, key: str, default=None):
        return self._load()["meta"].get(key, default)

    def set_meta(self, key: str, value):
        data = self._load()
        data["meta"][key] = value
        self._save(data)

    # ── текст для подсказки ───────────────────────────────────────────────────

    def context_text(self, query: str = "") -> str:
        items = self.relevant(query)
        if not items:
            return ""
        now = time.time()
        titles = {"user": "Что ты помнишь о пользователе:",
                  "self": "Что ты помнишь о себе (обещания, мнения, пожелания к тебе):",
                  "plan": "Планы и дела пользователя:",
                  "episode": "Из недавнего:"}
        lines = []
        for kind in KINDS:
            group = [i for i in items if i["kind"] == kind]
            if not group:
                continue
            lines.append(titles[kind])
            for i in group:
                tail = ""
                if kind == "episode":
                    tail = f" ({max(0, int((now - i['ts']) / 86400))} дн. назад)"
                elif kind == "plan":
                    days = round((effective_due(i) - now) / 86400)
                    when = "сегодня" if days == 0 else (f"через {days} дн." if days > 0 else f"{-days} дн. назад")
                    tail = f" (срок: {when})"
                lines.append(f"  - {i['text']}{tail}")
        return "\n".join(lines)

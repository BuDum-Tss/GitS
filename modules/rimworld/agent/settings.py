"""Общие настройки ГЛаДОС (memory/settings.json).

Читают и пишут все процессы: ядро, дашборд, питомец. Глушилка голоса
(voice_muted) и выключатель зрения (vision) живут здесь, поэтому
переживают перезапуск и работают одинаково из кнопки питомца, горячей
клавиши, чата и дашборда.
"""
import json
import os
import time
from pathlib import Path

import events_log

DEFAULTS = {
    "voice_muted": False,       # глушилка: не озвучивать реплики (текст остаётся)
    "vision": True,             # смотреть на экран (локально)
    "game_model": "haiku",      # модель игровых ходов и планирования: haiku (быстро, дёшево) / sonnet (думает глубже, дороже)
    "profile": "auto",          # режим поведения: auto (по активному окну) / game / work / stream / general
    "streamer": False,          # режим стримера: в игре она вслух говорит мысли, планы и цель (контент)
    "mic": False,               # слушать микрофон и разговаривать голосом (нужен docker compose up whisperx)
    "hearing": False,           # слушать системный звук (речь в колонках); по умолчанию выключено
    "auto_accept": False,       # авто-разрешать ВСЕ действия (кроме опасных, см. safety.py); включает только пользователь
    "record_runs": False,       # записывать каждый забег на видео (ffmpeg): сценарий, монтаж и вертикальные клипы в папку
    "telegram": False,          # публиковать мысли в Telegram-канал
    "telegram_after_run": False,   # писать пост-итог в канал после каждого забега (гибель колонии)
    "telegram_voice": False,       # посты в канал голосовым сообщением (текст идёт подписью)
    "telegram_confirm": False,  # спрашивать разрешение перед каждым постом (False: публикует сама)
    "graph_memory": True,        # память-граф (SQLite graph.db): факты из разговора; без моделей LM Studio тихо отключается
    "claude_usage": "normal",   # расход Claude: frugal / normal / generous (темп игровых контуров, см. claude_budget.py)
    "chattiness": "normal",     # quiet / normal / chatty — как часто говорит сама
    "pet_visible": True,        # показывать окно питомца (голос при этом работает)
    "volume": 100,              # громкость озвучки, 0-100
    "hotkey_input": "Ctrl+Shift+Space",   # глобальная клавиша строки быстрого ввода
    "dnd": False,               # «не отвлекать» до выключения (включает пользователь)
    "quiet_until": 0.0,         # режим «не отвлекать»: до какого времени (unix) молчит сама
    "pet_pinned": False,        # закрепить питомца: нельзя сдвинуть мышью
    "pet_x": None,              # запомненная позиция окна питомца (None — угол по умолчанию)
    "pet_y": None,
}
_dir = Path(os.getenv("MEMORY_DIR", "./memory"))


def quiet_active(cfg: dict | None = None) -> bool:
    """Идёт ли режим «не отвлекать»: включён вручную или по таймеру («буду смотреть сериал»)."""
    cfg = cfg or load()
    return bool(cfg["dnd"]) or time.time() < cfg["quiet_until"]


def init(memory_dir: str):
    global _dir
    _dir = Path(memory_dir)
    events_log.init(memory_dir)


def _file() -> Path:
    return _dir / "settings.json"


def load() -> dict:
    try:
        data = json.loads(_file().read_text(encoding="utf-8"))
    except Exception:
        data = {}
    return {**DEFAULTS, **{k: v for k, v in data.items() if k in DEFAULTS}}


def update(**changes) -> dict:
    data = load()
    before = dict(data)
    for k, v in changes.items():
        if k in DEFAULTS:
            data[k] = v
    diff = {k: v for k, v in data.items()
            if before.get(k) != v and k not in ("pet_x", "pet_y", "quiet_until")}
    if diff:
        events_log.log("setting", ", ".join(f"{k} = {v}" for k, v in diff.items()))
    try:
        _dir.mkdir(parents=True, exist_ok=True)
        tmp = _file().with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, _file())
    except OSError:
        pass
    return data

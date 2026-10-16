"""Общие настройки ядра GitS (memory/settings.json).

Файл читают и пишут ядро, дашборд и трей; модули (голос, питомец, ...) получают значения темой `settings` и меняют их
через HTTP-API дашборда (`POST /api/settings`). Глушилка голоса
(voice_muted) и выключатель зрения (vision) живут здесь, поэтому
переживают перезапуск и работают одинаково из кнопки питомца, горячей
клавиши, чата и дашборда.
"""
import copy
import json
import os
import time
from pathlib import Path

import events_log

DEFAULTS = {
    "voice_muted": False,       # глушилка: не озвучивать реплики (текст остаётся)
    "vision": True,             # смотреть на экран (локально)
    "game_model": "haiku",      # модель игровых ходов и планирования: haiku (быстро, дёшево) / sonnet (думает глубже, дороже)
    "profile": "auto",          # профиль: auto (сама выбирает) или id профиля из agent/profiles/ (ТЗ 02)
    "character": "glados",      # id данных; персонаж: папка resources/<Имя> (id в character.yaml) (ТЗ 05); переключает ядро (core.set_character), не правьте вручную
    "streamer": False,          # режим стримера: в игре она вслух говорит мысли, планы и цель (контент)
    "mic": False,               # слушать микрофон и разговаривать голосом (нужен docker compose up whisperx)
    "context": {"mode": "push", "budget_tokens": {}},   # ТЗ 04: push/pull память; бюджет токенов на роль и блок
    "hearing": False,           # слушать системный звук (речь в колонках); по умолчанию выключено
    "auto_accept": False,       # авто-разрешать ВСЕ действия (кроме опасных, см. safety.py); включает только пользователь
    "telegram": False,          # публиковать мысли в Telegram-канал
    "telegram_after_run": False,   # писать пост-итог в канал после каждого забега (гибель колонии)
    "record_runs": False,       # записывать каждый забег RimWorld на видео (общая настройка для модуля rimworld, ТЗ 11)
    "telegram_voice": False,       # посты в канал голосовым сообщением (текст идёт подписью)
    "telegram_confirm": False,  # спрашивать разрешение перед каждым постом (False: публикует сама)
    "graph_memory": True,        # память-граф (SQLite graph.db): факты из разговора; без моделей LM Studio тихо отключается
    "claude_usage": "normal",   # расход Claude: frugal / normal / generous (темп игровых контуров, см. claude_budget.py)
    "claude_token_budget": 3000000,   # ёмкость «батарейки»: токенов Claude за окно лимита (5 часов), см. claude_battery.py
    "chattiness": "normal",     # quiet / normal / chatty — как часто говорит сама
    # размышление (ТЗ 30, IMP-7): шагов и токенов на мысль, размышлений в час, глубина цепочки напоминаний
    "reflection": {"max_steps": 4, "max_tokens": 1500, "max_per_hour": 6, "max_depth": 3},
    # эпизоды и режим сна (ТЗ 27): сон при простое, расписании или «догоняющий»; нарезка `cut`; в контекст ответа эпизоды идут,
    # если context=True (по умолчанию выключено); raw_keep_days > 0 удаляет сырые события уже записанных эпизодов
    "episodes": {"enabled": True, "idle_min": 15, "min_gap_min": 20, "schedule_hours": [3], "catchup_after_h": 12,
                 "catchup_budget_tokens": 1500, "notify_salience": 0.5, "notify_max": 3, "diary_hour": 22,
                 "compress_after_days": 30, "raw_keep_days": 0, "related_cos": 0.82, "heavy_salience": 0, "context": False,
                 "cut": {"pause_s": 600, "peak": 0.5, "dense_events": 8, "max_events": 40, "max_tokens": 1500}},
    # быстрый контур (ТЗ 25): stream - стриминг ответа по предложениям; memory_deadline_ms - дедлайн поиска в памяти (FAST-6);
    # ack_ms - через сколько без реплики на поручении уходит факт «принято» (FAST-3); filler_ms - факт-филлер при затянутом ответе
    "fast_contour": {"stream": True, "merge_rest": True, "reply_tokens": 90, "memory_deadline_ms": 300, "ack_ms": 300, "filler_ms": 4000},
    # бюджеты задержки по этапам трассы, мс (ТЗ 25, FAST-8): шаг от предыдущей метки до метки этапа; end_to_sound - от конца
    # фразы до начала звука. Превышение пишется в журнал (`budget_exceeded`) и видно в отчёте /api/latency
    "latency_budget": {"stt_done": 1500, "core_in": 400, "classify_done": 400, "route": 100, "retrieve_done": 450,
                       "llm_first_token": 2500, "first_sentence_ready": 1200, "llm_done": 6000, "tts_first_audio": 1500,
                       "play_start": 400, "end_to_sound": 4500},
    # уроки и убеждения (ТЗ 28): разбор ошибок во сне; context=True кладёт убеждения и уроки по ситуации в контекст ответа (по
    # умолчанию выключено, поведение прежнее); K подтверждений из M разных эпизодов делают урок убеждением, не больше
    # belief_per_day в сутки и beliefs_max активных; expire_repeats повторов после применения устаревляют урок
    "lessons": {"enabled": True, "context": False, "max_context": 3, "merge_cos": 0.90, "merge_sim": 0.6, "group_sim": 0.25,
                "promote_k": 3, "promote_m": 2, "belief_per_day": 2, "beliefs_max": 12, "belief_chars": 600,
                "expire_repeats": 2, "stale_days": 60, "batch": 200},
    # справочники (ТЗ 04, KB-21): пороги поиска, справка в ответе разговора (chat, по умолчанию выключено: поведение прежнее),
    # перевод запроса для поиска по иноязычному справочнику (translate: код языка или "" - выключено, KB-6), области графа (scopes)
    "knowledge": {"min_cos": 0.60, "min_cos_bm25": 0.45, "experience_boost": 1.2, "chat": False, "chat_budget_ms": 300,
                  "chat_k": 3, "chat_chars": 1200, "chat_experience": 3, "chat_experience_chars": 600,
                  "lookup_k": 5, "lookup_chunk_chars": 600, "lookup_chars": 2500, "translate": "", "translate_cache": 1000,
                  "scopes": True},
    "dev_mode": False,          # режим разработчика: во вкладке «Модули» видны примеры для разработчиков (модуль echo)
    "pet_visible": True,        # показывать окно питомца (модуль pet при этом остаётся подключённым)
    "volume": 100,              # громкость озвучки, 0-100
    "tts_speed": 100,           # скорость речи в процентах (50-150, 100 - обычная); ESpeech и edge-tts
    "hotkey_input": "Ctrl+Shift+Space",   # глобальная клавиша строки быстрого ввода
    "dnd": False,               # «не отвлекать» до выключения (включает пользователь)
    "quiet_until": 0.0,         # режим «не отвлекать»: до какого времени (unix) молчит сама
    "only_core": False,         # режим «только ядро» (ТЗ 17, FLOW-1): супервизор не запускает модули (применяется при запуске ядра)
    "pet_pinned": False,        # закрепить питомца: нельзя сдвинуть мышью
    "pet_bubble": True,         # показывать облачко с текстом речи над питомцем
    "pet_bubble_secs": 25,      # сколько секунд облачко держится после конца речи (3-120)
    "pet_x": None,              # запомненная позиция окна питомца (None — угол по умолчанию)
    "pet_y": None,
    # модули (ТЗ 02, CORE-40): id -> {"enabled": bool, <имя собственной настройки>: значение}; нет записи — по умолчанию
    # (включён; для модулей из modules.yaml берётся поле enabled оттуда)
    "modules": {},
    # менеджер внимания (ТЗ 03, ATT-30): классификатор, пороги решений D0-D4, запас прерывания, когда пользователь считается
    # отошедшим, период полураспада свежести по шаблону вида события; min_conf_* - порог уверенности классификатора
    "attention": {
        "classifier": True,
        "thresholds": [15, 35, 60, 85],
        "interrupt_margin": 15,
        "away_after_s": 600,
        "half_life": {"rimworld.*": 60, "telegram.*": 900, "desktop.*": 120, "*.job": 600, "*": 300},
        "min_conf_event": 0.35,
        "min_conf_input": 0.5,
    },
}
# словари, которые update() сливает на один уровень вглубь (modules.<id> и attention.<ключ> целиком не затираются)
_DEEP_KEYS = ("modules", "attention", "context", "reflection", "episodes", "fast_contour", "latency_budget", "lessons", "knowledge")
_dir = Path(os.getenv("MEMORY_DIR", "./memory"))


def quiet_active(cfg: dict | None = None) -> bool:
    """Идёт ли режим «не отвлекать»: включён вручную или по таймеру («буду смотреть сериал»)."""
    cfg = cfg or load()
    return bool(cfg["dnd"]) or time.time() < cfg["quiet_until"]


def init(memory_dir: str):
    global _dir
    _dir = Path(memory_dir)
    events_log.init(memory_dir)


def memory_dir() -> Path:
    """Папка памяти ядра (там же лежат settings.json, правила внимания и др.)."""
    return _dir


def _file() -> Path:
    return _dir / "settings.json"


def load() -> dict:
    try:
        data = json.loads(_file().read_text(encoding="utf-8"))
    except Exception:
        data = {}
    merged = {**DEFAULTS, **{k: v for k, v in data.items() if k in DEFAULTS}}
    for k in _DEEP_KEYS:
        if not isinstance(merged.get(k), dict):
            merged[k] = copy.deepcopy(DEFAULTS[k])
        else:
            # недостающие ключи берутся из значений по умолчанию (частично сохранённый attention не теряет остальное);
            # изменение результата не должно портить DEFAULTS
            merged[k] = {**copy.deepcopy(DEFAULTS[k]), **copy.deepcopy(merged[k])}
    return merged


def _merge_deep(old: dict, new: dict) -> dict:
    """Слияние на один уровень: словари второго уровня (modules.<id>) дополняются, а не заменяются."""
    out = dict(old)
    for key, value in new.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = {**out[key], **value}
        else:
            out[key] = value
    return out


def update(**changes) -> dict:
    data = load()
    before = dict(data)
    for k, v in changes.items():
        if k in DEFAULTS:
            if k in _DEEP_KEYS and isinstance(v, dict) and isinstance(data.get(k), dict):
                data[k] = _merge_deep(data[k], v)
            else:
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

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

from services.tracing import events_log

DEFAULTS = {
    "voice_muted": False,       # глушилка: не озвучивать реплики (текст остаётся)
    "vision": True,             # смотреть на экран (локально)
    "pi_model": "gpt-6-luna",   # модель среды pi
    "pi_thinking": "minimal",   # глубина размышления среды pi: minimal / low / medium / high
    "pi_model_heavy": "gpt-6-sol",   # модель среды pi для тяжёлой работы с кодом (помощник на ПК, монтаж)
    "game_model": "haiku",      # модель игровых ходов и планирования: haiku (быстро, дёшево) / sonnet (думает глубже, дороже)
    "character": "glados",      # id данных; персонаж: папка resources/<Имя> (id в character.yaml) (ТЗ 05); один персонаж на контейнер, во время работы не меняется
    "streamer": False,          # режим стримера: в игре она вслух говорит мысли, планы и цель (контент)
    "mic": False,               # слушать микрофон и разговаривать голосом (распознавание локальное, GigaAM)
    "context": {"mode": "push", "budget_tokens": {"reply": {"dialogue": 500, "capabilities": 350, "total": 1800}}},   # ТЗ 04: push/pull память; бюджет токенов на роль и блок
    "hearing": False,           # слушать системный звук (речь в колонках); по умолчанию выключено
    "auto_accept": False,       # авто-разрешать ВСЕ действия (кроме опасных, см. services/tools/safety.py); включает только пользователь
    "record_runs": True,        # записывать каждый забег RimWorld на видео (общая настройка для модуля rimworld, ТЗ 11)
    "chattiness": "normal",     # quiet / normal / chatty — как часто говорит сама
    # размышление (ТЗ 30, IMP-7): шагов и токенов на мысль, размышлений в час, глубина цепочки напоминаний
    "reflection": {"max_steps": 4, "max_tokens": 1500, "max_per_hour": 6, "max_depth": 3},
    # эпизоды и режим сна (ТЗ 27): сон при простое, расписании или «догоняющий»; выбор ярких моментов `moments` (порог пика, суточный бюджет, склейка, окно контекста); writer - кто пишет эпизоды от первого лица: fork (копия её сессии pi, при недоступности heavy), heavy (pi_model_heavy, при сбое быстрая) или fast;
    # live - живая запись эпизодов, пока она бодрствует (условия форка, паузы, лимиты, очередь); в контекст ответа эпизоды идут,
    # если context=True (по умолчанию выключено); raw_keep_days > 0 удаляет сырые события уже записанных эпизодов
    "episodes": {"enabled": True, "idle_min": 15, "min_gap_min": 20, "schedule_hours": [3], "catchup_after_h": 12,
                 "catchup_budget_tokens": 1500, "notify_salience": 0.5, "notify_max": 3, "diary_hour": 22,
                 "compress_after_days": 30, "raw_keep_days": 0, "related_cos": 0.82, "writer": "fork", "context": False,
                 "moments": {"threshold": 0.5, "per_day": 10, "merge_s": 300, "before_s": 240, "after_s": 120, "max_events": 30,
                             "max_tokens": 1500},
                 "live": {"enabled": True, "tick_s": 30, "min_gap_s": 45, "per_hour": 8, "max_pending": 20, "instant_score": 0.85,
                          "instant_after_s": 20, "fork_after_events": 6, "context_share": 0.5, "max_wait_s": 1800,
                          "max_per_fork": 4}},
    # быстрый контур (ТЗ 25): stream - стриминг ответа по предложениям; memory_deadline_ms - дедлайн поиска в памяти (FAST-6);
    # ack_ms - через сколько без реплики на поручении уходит факт «принято» (FAST-3); filler_ms - факт-филлер при затянутом ответе
    # бюджеты задержки по этапам трассы, мс (ТЗ 25, FAST-8): шаг от предыдущей метки до метки этапа; end_to_sound - от конца
    # фразы до начала звука. Превышение пишется в журнал (`budget_exceeded`) и видно в отчёте /api/latency
    "latency_budget": {"stt_done": 1500, "core_in": 400, "classify_done": 400, "route": 100, "retrieve_done": 450,
                       "llm_first_token": 2500, "first_sentence_ready": 1200, "llm_done": 6000, "tts_first_audio": 1500,
                       "play_start": 400, "end_to_sound": 4500},
    "dev_mode": False,          # режим разработчика: во вкладке «Модули» видны примеры для разработчиков (модуль echo)
    "volume": 100,              # громкость озвучки, 0-100
    "tts_speed": 100,           # скорость речи в процентах (50-150, 100 - обычная); ESpeech и edge-tts
    "tts_fx": "off",            # фильтр под голос персонажа после синтеза: off или пресет из services/voice/voice_fx.py
    "autoplay": False,          # ТЗ 33: единый агент сам продолжает работу в среде автономного модуля (игра), раз в work_interval_s модуля
    "dnd": False,               # «не отвлекать» до выключения (включает пользователь)
    "quiet_until": 0.0,         # режим «не отвлекать»: до какого времени (unix) молчит сама
    "only_core": False,         # режим «только ядро» (ТЗ 17, FLOW-1): супервизор не запускает модули (применяется при запуске ядра)
    # участники шины (ТЗ 02, CORE-40): id -> {"enabled": bool,
    # <имя собственной настройки>: значение}; нет записи — по умолчанию (включён; для записей из peers.yaml берётся поле enabled)
    "peers": {},
    # менеджер внимания (ТЗ 03, ATT-30): классификатор обращений (decide), запас прерывания, когда пользователь считается
    # отошедшим, min_conf_input - порог уверенности классификатора; inbox - входящая очередь событий (min_interval_s: не чаще,
    # чем раз в столько секунд отдавать накопившееся ей на обдумывание)
    "attention": {
        "classifier": True,
        "interrupt_margin": 15,
        "away_after_s": 600,
        "min_conf_input": 0.5,
        "inbox": {"min_interval_s": 60},
    },
}
# словари, которые update() сливает на один уровень вглубь (peers.<id> и attention.<ключ> целиком не затираются)
_DEEP_KEYS = ("peers", "attention", "context", "reflection", "episodes", "latency_budget")
def default_memory_dir() -> str:
    """MEMORY_DIR главнее; иначе CHARACTER_HOME/memory (том); иначе resources/<персонаж>/memory (папка персонажа)."""
    from gits_sdk import resources
    return str(resources.default_memory_dir())


_dir = Path(default_memory_dir())


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


def peer_entry(cfg: dict | None, peer_id: str) -> dict | None:
    """Запись участника: `peers.<id>` или None."""
    peers = (cfg or {}).get("peers")
    entry = peers.get(peer_id) if isinstance(peers, dict) else None
    return entry if isinstance(entry, dict) else None


def _write(data: dict) -> None:
    try:
        _dir.mkdir(parents=True, exist_ok=True)
        tmp = _file().with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, _file())
    except OSError:
        pass


# Миграции уже существующих settings.json (убрать после 2026-12-01): `modules` -> `peers`, `twitch`/`obs` -> `streamer`,
# настройки питомца из общих в `peers.pet`, настройки Telegram из общих в `peers.telegram`.
def _migrate_modules_key(data: dict) -> bool:
    """Миграция: ключ `modules` (участники шины) -> `peers`. True, если файл надо переписать."""
    if "modules" not in data:
        return False
    legacy = data.pop("modules")
    if "peers" not in data and isinstance(legacy, dict):
        data["peers"] = legacy
    return True


_STREAMER_LEGACY = ("twitch", "obs")


def _migrate_streamer_peers(data: dict) -> bool:
    """Одноразовая миграция фазы 3b: записи `peers.twitch` и `peers.obs` -> одна `peers.streamer` (окружения слиты). Если есть
    старые записи, а `streamer` нет: `enabled` = включён любой из них (нет поля - по умолчанию включён), значения настроек из
    обоих (у twitch первыми, затем obs), старые ключи удаляются. Если `streamer` уже есть, старые ключи просто удаляются.
    True, если файл надо переписать."""
    peers = data.get("peers")
    if not isinstance(peers, dict) or not any(k in peers for k in _STREAMER_LEGACY):
        return False
    old = {k: peers.pop(k) for k in _STREAMER_LEGACY if k in peers}
    if "streamer" not in peers:
        entries = [e for e in old.values() if isinstance(e, dict)]
        merged: dict = {}
        for entry in entries:
            merged.update({k: v for k, v in entry.items() if k != "enabled"})
        if any("enabled" in e for e in entries):
            merged["enabled"] = any(bool(e.get("enabled", True)) for e in entries)
        if merged:
            peers["streamer"] = merged
    return True


_PET_LEGACY = {"pet_visible": "visible", "pet_pinned": "pinned", "pet_bubble": "bubble", "pet_bubble_secs": "bubble_secs",
               "pet_x": "x", "pet_y": "y", "hotkey_input": "hotkey_input"}


def _migrate_pet_settings(data: dict) -> bool:
    """Миграция: настройки питомца раньше лежали в общих настройках ядра (`pet_visible`, `pet_x`, `hotkey_input`...), теперь
    это собственные настройки участника в `peers.pet` (`visible`, `x`, `hotkey_input`...). Значение из общих настроек
    переносится, если собственное ещё не задано; пустая позиция (None) пропускается. True, если файл надо переписать."""
    old = {k: data.pop(k) for k in _PET_LEGACY if k in data}
    if not old:
        return False
    peers = data.setdefault("peers", {})
    if not isinstance(peers, dict):
        return True
    entry = peers.setdefault("pet", {})
    if isinstance(entry, dict):
        for legacy, name in _PET_LEGACY.items():
            if legacy in old and old[legacy] is not None and name not in entry:
                entry[name] = old[legacy]
    return True


_TELEGRAM_LEGACY = {"telegram": "publish", "telegram_after_run": "after_run", "telegram_voice": "voice",
                    "telegram_confirm": "confirm"}


def _migrate_telegram_settings(data: dict) -> bool:
    """Миграция: настройки Telegram раньше лежали в общих настройках ядра (`telegram`, `telegram_voice`...), теперь это
    собственные настройки окружения в `peers.telegram` (`publish`, `voice`, `confirm`, `after_run`). Значение из общих
    настроек переносится, если собственное ещё не задано. True, если файл надо переписать."""
    old = {k: data.pop(k) for k in _TELEGRAM_LEGACY if k in data}
    if not old:
        return False
    peers = data.setdefault("peers", {})
    if not isinstance(peers, dict):
        return True
    entry = peers.setdefault("telegram", {})
    if isinstance(entry, dict):
        for legacy, name in _TELEGRAM_LEGACY.items():
            if legacy in old and isinstance(old[legacy], bool) and name not in entry:
                entry[name] = old[legacy]
    return True


def load() -> dict:
    try:
        data = json.loads(_file().read_text(encoding="utf-8"))
    except Exception:
        data = {}
    if isinstance(data, dict) and (_migrate_modules_key(data) | _migrate_streamer_peers(data) | _migrate_pet_settings(data)
                                  | _migrate_telegram_settings(data)):
        _write(data)
    elif not isinstance(data, dict):
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
    """Слияние на один уровень: словари второго уровня (peers.<id>) дополняются, а не заменяются."""
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
            if before.get(k) != v and k != "quiet_until"}
    if diff:
        events_log.log("setting", ", ".join(f"{k} = {v}" for k, v in diff.items()))
    _write(data)
    return data

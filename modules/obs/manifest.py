"""Манифест модуля OBS: переключение сцен, трансляция, запись, субтитры в потоке (CEA-608) через obs-websocket.

Начало трансляции публичное (`risk="public"`), остановка с вопросом (`ask`), сцены и запись без вопросов."""
import copy

_GENERIC = ("obs", "обс", "стрим", "трансляция", "сцена", "эфир")


def _tool(name, description, params, *, risk="auto", keywords=(), informative=False, timeout_s=30):
    tool = {"name": name, "description": description, "params": params, "risk": risk, "availability": "always",
            "timeout_s": timeout_s, "keywords": list(dict.fromkeys([*keywords, *_GENERIC]))}
    if informative:
        tool["informative"] = True
    return tool


TOOLS = [
    _tool("status", "Состояние OBS: на связи ли, какая сцена, идёт ли трансляция и запись.", {}, informative=True,
          keywords=("идёт ли стрим", "что в obs")),
    _tool("scenes", "Список сцен OBS и текущая сцена.", {}, informative=True, keywords=("какие сцены",)),
    _tool("switch_scene", "Переключить сцену OBS по названию (например кабинет, AFK, игра).",
          {"scene": {"type": "string", "description": "название сцены из списка scenes"}}, keywords=("переключи", "включи сцену")),
    _tool("start_stream", "Начать трансляцию из OBS (идёт в эфир на площадку, которая настроена в OBS).", {}, risk="public",
          keywords=("начни стрим", "выйди в эфир", "запусти трансляцию")),
    _tool("stop_stream", "Закончить трансляцию из OBS.", {}, risk="ask", keywords=("останови стрим", "закончи стрим")),
    _tool("start_record", "Начать запись в OBS.", {}, keywords=("запись", "запиши")),
    _tool("stop_record", "Остановить запись в OBS и вернуть путь к файлу.", {}, keywords=("останови запись",)),
    _tool("caption", "Отправить субтитр в поток (закрытые субтитры CEA-608, зритель включает их кнопкой CC в плеере Twitch).",
          {"text": {"type": "string", "description": "текст субтитра, до 200 символов"}}, keywords=("субтитры",)),
]

MANIFEST = {
    "id": "obs",
    "title": "OBS",
    "description": "OBS Studio: переключает сцены, начинает и заканчивает трансляцию и запись, шлёт субтитры в поток "
                   "(obs-websocket, адрес и пароль в .env: OBS_WEBSOCKET_URL, OBS_WEBSOCKET_PASSWORD).",
    "version": "0.1.0",
    "capabilities": [],
    "subscribe": ["settings", "module", "character"],
    "shared_settings": [],
    "settings_schema": {},
    "tools": TOOLS,
    "emits": [
        {"kind": "obs.stream_state", "description": "Трансляция в OBS началась или закончилась", "default_urgency": 3},
        {"kind": "obs.record_state", "description": "Запись в OBS началась или закончилась", "default_urgency": 2},
        {"kind": "obs.scene", "description": "Сцена OBS сменилась", "default_urgency": 1},
    ],
    "state": True,
    "policies": [
        {"id": "available", "title": "Доступен", "focus": "background", "transitions": [], "interruptible": True,
         "purpose": "управление OBS по просьбе"},
    ],
    "default_policy": "available",
    "panel": {"title": "OBS", "icon": "video", "order": 63},
}


def build_manifest() -> dict:
    """Свежая копия манифеста."""
    return copy.deepcopy(MANIFEST)

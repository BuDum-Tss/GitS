"""Манифест подсистемы OBS (часть окружения «Стример»): переключение сцен, трансляция, запись, субтитры в потоке (CEA-608) через obs-websocket.

Начало трансляции публичное (`risk="public"`), остановка с вопросом (`ask`), сцены и запись без вопросов."""
PREFIX = "obs_"                       # имена инструментов подсистемы: streamer.obs_<имя>

_GENERIC = ("obs", "обс", "стрим", "трансляция", "сцена", "эфир")


def _tool(name, description, params, *, risk="auto", keywords=(), informative=False, timeout_s=30, read_only=False):
    tool = {"name": PREFIX + name, "description": description, "params": params, "risk": risk, "availability": "always",
            "timeout_s": timeout_s, "keywords": list(dict.fromkeys([*keywords, *_GENERIC]))}
    if informative:
        tool["informative"] = True
    if read_only:
        tool["read_only"] = True          # явное чтение: журнал действий хода агента не считает его действием с эффектом
    return tool


TOOLS = [
    _tool("status", "Состояние OBS: на связи ли, какая сцена, идёт ли трансляция и запись.", {}, informative=True, read_only=True,
          keywords=("идёт ли стрим", "что в obs")),
    _tool("scenes", "Список сцен OBS и текущая сцена.", {}, informative=True, read_only=True, keywords=("какие сцены",)),
    _tool("switch_scene", "Переключить сцену OBS по названию (например кабинет, AFK, игра).",
          {"scene": {"type": "string", "description": "название сцены из списка obs_scenes"}}, keywords=("переключи", "включи сцену")),
    _tool("start_stream", "Начать трансляцию из OBS (идёт в эфир на площадку, которая настроена в OBS).", {}, risk="public",
          keywords=("начни стрим", "выйди в эфир", "запусти трансляцию")),
    _tool("stop_stream", "Закончить трансляцию из OBS.", {}, risk="ask", keywords=("останови стрим", "закончи стрим")),
    _tool("start_record", "Начать запись в OBS.", {}, keywords=("запись", "запиши")),
    _tool("stop_record", "Остановить запись в OBS и вернуть путь к файлу.", {}, keywords=("останови запись",)),
    _tool("caption", "Отправить субтитр в поток (закрытые субтитры CEA-608, зритель включает их кнопкой CC в плеере Twitch).",
          {"text": {"type": "string", "description": "текст субтитра, до 200 символов"}}, keywords=("субтитры",)),
]

EMITS = [
    {"kind": "obs.stream_state", "description": "Трансляция в OBS началась или закончилась", "default_urgency": 3},
    {"kind": "obs.record_state", "description": "Запись в OBS началась или закончилась", "default_urgency": 2},
    {"kind": "obs.scene", "description": "Сцена OBS сменилась", "default_urgency": 1},
]

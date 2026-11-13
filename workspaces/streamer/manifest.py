"""Манифест окружения «Стример»: один процесс, один манифест на двух подсистем, `twitch` (чат и события канала, модерация) и
`obs` (сцены, трансляция, запись, субтитры). Фаза 3b: прежние отдельные модули twitch и obs слиты в одно окружение.

Инструменты подсистем получают префиксы (`streamer.twitch_chat_say`, `streamer.obs_switch_scene`). Виды событий остаются прежними
(`twitch.chat`, `obs.scene`): правила внимания привязаны к ним, поэтому манифест объявляет `event_prefixes`."""
import copy

from obs.obs_manifest import EMITS as OBS_EMITS
from obs.obs_manifest import TOOLS as OBS_TOOLS
from twitch.twitch_manifest import EMITS as TWITCH_EMITS
from twitch.twitch_manifest import SETTINGS_SCHEMA as TWITCH_SETTINGS
from twitch.twitch_manifest import TOOLS as TWITCH_TOOLS

MANIFEST = {
    "id": "streamer",
    "role": "workspace",
    "ownership": "owned",
    "title": "Стример",
    "description": "Ведение трансляции: чат и события канала Twitch, ответы и модерация; в OBS сцены, трансляция, запись и "
                   "субтитры в поток.",
    "version": "0.2.0",
    "capabilities": ["chat.channel"],
    "subscribe": ["settings", "presence", "module", "character"],
    "shared_settings": ["quiet_until", "dnd"],
    "event_prefixes": ["twitch", "obs"],
    "settings_schema": {**TWITCH_SETTINGS},
    "tools": [*TWITCH_TOOLS, *OBS_TOOLS],
    "emits": [*TWITCH_EMITS, *OBS_EMITS],
    "state": True,
    # среда агента: в неё входят, когда ведут трансляцию (чат, события канала, сцены, запись, субтитры)
    "workspace": {"id": "streamer", "title": "Стример",
                  "purpose": "ведение трансляции: чат и события канала Twitch, сцены и запись в OBS, субтитры",
                  "entry": ["user_message", "self_impulse", "critical"], "interruptible": True,
                  "safe_points": ["между действиями в эфире"], "signals_in": ["twitch.*", "obs.*"],
                  "done_when": "трансляция закончилась или о ней больше не просят",
                  "when_to_use": "когда идёт трансляция или о ней просят: ответить в чате, сменить название или сцену, "
                                 "начать или закончить стрим и запись, отправить субтитр, посмотреть события канала",
                  "keywords": ["стрим", "трансляция", "твич", "twitch", "obs", "обс", "чат", "сцена", "эфир", "запись"]},
    # одна панель: разделы Twitch и OBS (блоки присылает модуль через panel.set, если они понадобятся)
    "panel": {"title": "Стример", "icon": "video", "order": 62},
}


def build_manifest(parts: tuple[str, ...] = ("twitch", "obs")) -> dict:
    """Свежая копия манифеста. `parts` оставляет только инструменты и события названных подсистем (для тестов одной подсистемы
    на общем клиенте: клиент требует обработчик для каждого инструмента манифеста)."""
    manifest = copy.deepcopy(MANIFEST)
    if set(parts) != {"twitch", "obs"}:
        manifest["tools"] = [t for t in manifest["tools"] if t["name"].split("_", 1)[0] in parts]
        manifest["emits"] = [e for e in manifest["emits"] if e["kind"].split(".", 1)[0] in parts]
    return manifest

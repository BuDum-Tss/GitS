"""Коллекция сцен OBS для стрима: «Кабинет» (персонаж говорит со зрителями), «AFK» (раздумья над чертежами) и «Игра» (захват игры,
рамка и персонаж). Страницы сцен лежат в `agent/static/stream/` и раздаются дашбордом ядра (источники «Браузер»).

Запуск из папки `agent` (OBS можно не закрывать, коллекция появится в меню «Коллекция сцен» при открытии меню):
    python -m tools.obs_scenes              # записать Стрим.json в папку сцен OBS (APPDATA/obs-studio/basic/scenes)
    python -m tools.obs_scenes --out файл   # записать в другое место
Звук: общие устройства (весь звук ПК, микрофон) в коллекцию не входят, в эфир идёт звук игры и голос (захват звука приложений: игра и ffplay модуля голоса). Ядро должно работать (страницы идут с :8766).
"""
import argparse
import json
import os
import uuid
from pathlib import Path

NAME = "Стрим"
BASE_URL = "http://localhost:8766/static/stream"
NS = uuid.UUID("6f1c2b6e-3d0a-4f5e-9d2a-5a7c0b1e4d10")
GAME_WINDOW = "RimWorld by Ludeon Studios:UnityWndClass:RimWorldWin64.exe"       # заголовок:класс:exe окна игры
SCENES = (("Кабинет", ("browser:office", "audio:voice")), ("AFK", ("browser:afk", "audio:voice")),
          ("Игра", ("game", "audio:game", "audio:voice", "browser:game")))
VOICE_WINDOW = "GladosVoice:SDL_app:ffplay.exe"      # окно ffplay модуля голоса (modules/voice/player.py, FFPLAY_TITLE)


def _uuid(name: str) -> str:
    return str(uuid.uuid5(NS, name))


def _source(name: str, kind: str, settings: dict, canvas: str | None) -> dict:
    src = {"prev_ver": 553648129, "name": name, "uuid": _uuid(name), "id": kind, "versioned_id": kind, "settings": settings,
           "mixers": 255 if kind in ("browser_source", "wasapi_process_output_capture") else 0, "sync": 0, "flags": 0, "volume": 1.0, "balance": 0.5, "enabled": True,
           "muted": False, "push-to-mute": False, "push-to-mute-delay": 0, "push-to-talk": False, "push-to-talk-delay": 0,
           "hotkeys": {}, "deinterlace_mode": 0, "deinterlace_field_order": 0, "monitoring_type": 0, "private_settings": {}}
    if canvas:
        src["canvas_uuid"] = canvas
    return src


def _item(name: str, item_id: int) -> dict:
    return {"name": name, "source_uuid": _uuid(name), "visible": True, "locked": False, "rot": 0.0, "pos": {"x": 0.0, "y": 0.0},
            "scale": {"x": 1.0, "y": 1.0}, "align": 5, "bounds_type": 0, "bounds_align": 0, "bounds": {"x": 0.0, "y": 0.0},
            "crop_left": 0, "crop_top": 0, "crop_right": 0, "crop_bottom": 0, "id": item_id, "group_item_backup": False,
            "scale_filter": "disable", "blend_method": "default", "blend_type": "normal", "show_in_multiview": True,
            "private_settings": {}}


def build(template: dict | None = None, base_url: str = BASE_URL) -> dict:
    """Коллекция сцен как словарь формата OBS. template: существующая коллекция (звук, холст, версия)."""
    template = template or {}
    canvas = next((s.get("canvas_uuid") for s in template.get("sources", []) if s.get("canvas_uuid")), None)
    sources: list[dict] = []
    defs = {
        "browser:office": ("Кабинет: страница", "browser_source", {"url": f"{base_url}/office.html"}),
        "browser:afk": ("AFK: страница", "browser_source", {"url": f"{base_url}/afk.html"}),
        "browser:game": ("Игра: рамка и персонаж", "browser_source", {"url": f"{base_url}/game.html"}),
        "audio:voice": ("Звук: голос", "wasapi_process_output_capture", {"window": VOICE_WINDOW, "priority": 2}),     # только голос
        "audio:game": ("Звук: игра", "wasapi_process_output_capture", {"window": GAME_WINDOW, "priority": 2}),     # звук только игры
        "game": ("Игра: захват", "game_capture", {"capture_mode": "window", "window": GAME_WINDOW, "priority": 1,
                                                  "capture_cursor": False}),
    }
    for name, kind, settings in defs.values():
        if kind == "browser_source":
            settings = {**settings, "width": 1920, "height": 1080, "fps": 30, "css": "", "shutdown": False, "restart_when_active": False}
        sources.append(_source(name, kind, settings, canvas))
    for scene, keys in SCENES:
        items = [_item(defs[k][0], i + 1) for i, k in enumerate(keys)]
        sc = _source(scene, "scene", {"id_counter": len(items), "custom_size": False, "items": items}, canvas)
        sc["hotkeys"] = {"OBSBasic.SelectScene": []}
        sources.append(sc)
    names = [s for s, _ in SCENES]
    out = {"name": NAME, "sources": sources, "groups": [], "scene_order": [{"name": n} for n in names],
           "current_scene": names[0], "current_program_scene": names[0], "current_transition": template.get("current_transition", "Fade"),
           "transition_duration": 300, "transitions": template.get("transitions", []), "quick_transitions": [],
           "saved_projectors": [], "preview_locked": False, "scaling_enabled": False, "scaling_level": 0,
           "scaling_off_x": 0.0, "scaling_off_y": 0.0, "modules": template.get("modules", {}), "canvases": template.get("canvases", []),
           "version": 2}
    return out                                                   # общих устройств (звук рабочего стола, микрофон) нет: в эфир идёт только звук игры


def obs_scenes_dir() -> Path:
    return Path(os.path.expandvars("%APPDATA%")) / "obs-studio" / "basic" / "scenes"


def _find_template(folder: Path) -> dict | None:
    for path in sorted(folder.glob("*.json")):
        if path.stem != NAME:
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", help="куда записать (по умолчанию папка сцен OBS)")
    a = ap.parse_args(argv)
    folder = obs_scenes_dir()
    target = Path(a.out) if a.out else folder / f"{NAME}.json"
    data = build(_find_template(folder) if folder.exists() else None)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=4), encoding="utf-8")
    print(f"записано {target}: сцены {', '.join(s for s, _ in SCENES)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

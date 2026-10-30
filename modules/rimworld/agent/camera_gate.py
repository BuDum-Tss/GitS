"""Переключатель камеры: кто ею управляет, агент или игрок.

Включено (по умолчанию): камеру двигает агент (режиссёр камеры, инструменты `camera_*` в ходах Claude). Выключено: камера у
игрока, агент команд камеры не шлёт, а инструменты отвечают отказом. Состояние лежит в файле `camera_agent.json` в папке
памяти модуля: его читают сразу три процесса (контроллер модуля, MCP-сервер ходов Claude, тесты), у MCP-сервера общей памяти с
контроллером нет. Значение берётся из настройки модуля `camera_agent` (дашборд, вкладка «Модули»); `controller` переписывает файл,
когда она меняется. Чтение камеры (`camera_view`) не блокируется: она ничего не двигает."""
import json
import os
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
READ_ONLY = {"camera_view"}
REFUSAL = ("Камера сейчас у игрока: переключатель «Камерой управляет агент» выключен. Камеру не двигай, играй дальше без неё "
           "(постройки, колонисты и приказы от этого не зависят).")
_CACHE_S = 1.0
_cache = {"at": 0.0, "value": True}


def _path() -> Path:
    return Path(os.getenv("RIMWORLD_MEMORY_DIR") or HERE / "memory") / "camera_agent.json"


def agent_has_camera() -> bool:
    """Двигает ли камеру агент. Нет файла или он испорчен: да (прежнее поведение)."""
    now = time.monotonic()
    if now - _cache["at"] < _CACHE_S:
        return _cache["value"]
    value = True
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
        value = bool(data.get("agent", True)) if isinstance(data, dict) else True
    except (OSError, ValueError):
        value = True
    _cache.update(at=now, value=value)
    return value


def set_agent_camera(on: bool) -> None:
    """Записать состояние переключателя (атомарно: читатель не увидит половину файла)."""
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"agent": bool(on), "ts": time.time()}), encoding="utf-8")
    os.replace(tmp, path)
    _cache.update(at=0.0, value=bool(on))


def blocks(command_type: str) -> bool:
    """Нужно ли отказать команде мода: это команда камеры, она что-то двигает, а камера у игрока."""
    t = str(command_type or "")
    return (t.startswith("camera_") or t == "set_auto_camera") and t not in READ_ONLY and not agent_has_camera()

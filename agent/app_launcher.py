"""Запуск установленных приложений по названию, как в PowerToys Run (Alt+Пробел).

Каталог = ярлыки меню «Пуск» (все пользователи и текущий) + магазинные приложения Windows (Get-StartApps) + свои записи
из memory/apps.json (RimWorld с ожиданием мода и то, что добавлено инструментом add_app). Запускается только то, что есть
в каталоге, без аргументов и произвольных команд. Из каталога исключены деинсталляторы, консоли (cmd, PowerShell,
терминал) и редактор реестра; при необходимости такое можно добавить в apps.json вручную.
Поиск как в Run: точное совпадение, начало названия, вхождение, похожее написание; если подходит несколько и явного
лидера нет, она переспрашивает.
"""
import difflib
import json
import os
import re
import subprocess
import time
from pathlib import Path

import psutil

import game_launcher

_ALLOWED_EXT = {".exe", ".lnk", ".bat", ".cmd"}
_BLOCK = re.compile(r"uninstall|деинсталл|удалить|удаление|remove|regedit|редактор реестра|command prompt|командная строка|"
                    r"powershell|windows terminal|терминал windows|^cmd$|^терминал$", re.I)
_CACHE = {"at": 0.0, "items": []}
_TTL = 600
_RU = {"хром": "chrome", "гугл хром": "chrome", "телеграм": "telegram", "телега": "telegram", "докер": "docker",
       "эдж": "edge", "блокнот": "notepad", "дискорд": "discord", "стим": "steam", "спотифай": "spotify", "ворд": "word",
       "эксель": "excel", "лм студио": "lm studio", "обс": "obs", "проводник": "explorer", "настройки": "settings"}


def _file() -> Path:
    base = os.getenv("MEMORY_DIR") or str(Path(__file__).resolve().parent / "memory")
    return Path(base) / "apps.json"


def _start_roots() -> list[str]:
    return [os.path.expandvars(r"%ProgramData%\Microsoft\Windows\Start Menu\Programs"),
            os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs")]


def _uwp_apps() -> list[dict]:
    """Магазинные приложения Windows: [{'Name', 'AppID'}]; без PowerShell или при сбое пусто."""
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                              "Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress"],
                             capture_output=True, timeout=30, creationflags=0x08000000).stdout.decode("utf-8", "ignore")
        data = json.loads(out) if out.strip() else []
        return data if isinstance(data, list) else [data]
    except Exception:
        return []


def _curated() -> list[dict]:
    f = _file()
    try:
        return json.loads(f.read_text(encoding="utf-8"))["apps"]
    except Exception:
        apps = []
        rw = game_launcher.find_exe()
        if rw:
            apps.append({"id": "rimworld", "title": "RimWorld", "path": rw, "kind": "rimworld",
                         "aliases": ["римворлд", "рим ворлд", "rimworld", "игра", "игру", "колония"]})
        _save(apps)
        return apps


def _save(apps: list[dict]):
    f = _file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"apps": apps}, ensure_ascii=False, indent=2), encoding="utf-8")


def catalog(refresh: bool = False) -> list[dict]:
    """Все приложения, которые можно запустить: свои записи, меню «Пуск», магазинные. Кэш на 10 минут."""
    if not refresh and _CACHE["items"] and time.time() - _CACHE["at"] < _TTL:
        return _CACHE["items"]
    items = [dict(a, source="свой список") for a in _curated()]
    seen = {a["title"].lower() for a in items}
    for root in _start_roots():
        for p in Path(root).rglob("*.lnk") if Path(root).is_dir() else []:
            title = p.stem
            if title.lower() in seen or _BLOCK.search(title):
                continue
            seen.add(title.lower())
            items.append({"title": title, "path": str(p), "kind": "lnk", "aliases": [], "source": "меню «Пуск»"})
    for u in _uwp_apps():
        title, app_id = str(u.get("Name", "")), str(u.get("AppID", ""))
        if title and app_id and title.lower() not in seen and not _BLOCK.search(title):
            seen.add(title.lower())
            items.append({"title": title, "path": app_id, "kind": "uwp", "aliases": [], "source": "магазин Windows"})
    _CACHE.update(at=time.time(), items=items)
    return items


def _norm(s: str) -> str:
    return " ".join(s.lower().replace("ё", "е").split())


def search(query: str, limit: int = 5) -> list[tuple[int, dict]]:
    q0 = _norm(query)
    queries = {q0} | {v for k, v in _RU.items() if k in q0}          # «открой хром» ищет и «chrome»
    scored = []
    for app in catalog():
        best = 0
        for key in [app["title"], app.get("id", ""), *app.get("aliases", [])]:
            k = _norm(key)
            if not k:
                continue
            for q in queries:
                if q == k:
                    s = 100
                elif k.startswith(q) or q.startswith(k):
                    s = 90
                elif q in k or (len(k) > 3 and k in q):
                    s = 75
                else:
                    r = difflib.SequenceMatcher(None, q, k).ratio()
                    s = int(r * 80) if r >= 0.75 else 0
                best = max(best, s)
        if best:
            scored.append((best, app))
    scored.sort(key=lambda x: (-x[0], len(x[1]["title"])))
    return scored[:limit]


def find(name: str) -> dict:
    """Лучшее совпадение, как в Run. ValueError с вариантами, если ничего нет или непонятно, какое из нескольких."""
    hits = search(name)
    if not hits:
        raise ValueError(f"приложения «{name}» не нашла среди установленных")
    top = hits[0]
    if len(hits) == 1 or top[0] >= 100 or top[0] - hits[1][0] >= 10:
        return top[1]
    raise ValueError(f"под «{name}» подходит несколько: " + ", ".join(a["title"] for _, a in hits[:4]) + ". Уточни, какое")


def is_running(app: dict) -> bool:
    if app.get("kind") not in ("exe", "rimworld"):
        return False                     # у ярлыков и магазинных приложений имя процесса заранее неизвестно
    exe = Path(app["path"]).name.lower()
    return any((p.info["name"] or "").lower() == exe for p in psutil.process_iter(["name"]))


def launch(name: str) -> str:
    """Запустить приложение по названию. Возвращает человеческий итог; бросает ValueError/RuntimeError с причиной."""
    app = find(name)
    kind, path = app.get("kind"), app["path"]
    if kind == "rimworld":
        return game_launcher.launch()
    if kind == "uwp":
        subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{path}"], close_fds=True)
    else:
        if not Path(path).is_file():
            raise RuntimeError(f"файла приложения «{app['title']}» больше нет: {path}")
        if is_running(app):
            return f"«{app['title']}» уже запущено"
        if path.lower().endswith(".lnk"):
            os.startfile(path)   # noqa: S606  (путь из каталога установленных приложений)
        else:
            game_launcher.spawn_detached(path, str(Path(path).parent))      # не потомок бота: перезапуск бота программу не закроет
    return f"«{app['title']}» запущено"


def titles(query: str = "", limit: int = 15) -> str:
    items = [a for _, a in search(query, limit)] if query.strip() else catalog()[:limit]
    return ", ".join(a["title"] for a in items) or "ничего не нашла"


def add(title: str, path: str, aliases: list[str] | None = None) -> str:
    """Добавить в свой список (вызывается только после разрешения пользователя)."""
    p = Path(os.path.expandvars(path.strip().strip('"')))
    if not p.is_file() or p.suffix.lower() not in _ALLOWED_EXT:
        raise ValueError(f"нужен существующий файл .exe, .lnk, .bat или .cmd, а не «{path}»")
    apps = _curated()
    if any(a["path"].lower() == str(p).lower() for a in apps):
        raise ValueError("это приложение уже в списке")
    apps.append({"id": p.stem.lower().replace(" ", "_"), "title": title.strip() or p.stem, "path": str(p),
                 "kind": "exe" if p.suffix.lower() == ".exe" else "lnk",
                 "aliases": [x.strip().lower() for x in (aliases or []) if x.strip()]})
    _save(apps)
    _CACHE["at"] = 0.0
    return f"добавила «{title}» в список приложений"

"""Пространство модуля на стороне ядра (TZ 15): раздача файлов артефактов, открытие папок, проверка адресов и ответов
действий. Пути берутся только из каталогов, которые модуль объявил в `panel.serve_dirs`; запрос путь не задаёт (только
задача и имя файла из её артефактов). Всё, что приходит от модуля, - данные: адреса и текст проверяются здесь.
"""
import os
from pathlib import Path
from urllib.parse import urlsplit

LOCAL_HOSTS = ("127.0.0.1", "localhost")          # адреса действий (open_url) без разрешения модуля
ACTION_MESSAGE_MAX = 300
ARTIFACT_TYPES = {                                 # тип по расширению; остальное - просто файл
    ".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime",
    ".srt": "text/plain; charset=utf-8", ".vtt": "text/vtt; charset=utf-8", ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8", ".json": "application/json", ".png": "image/png",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif",
}


def _inside(base: str, target: str) -> bool:
    try:
        return os.path.commonpath([base, target]) == base
    except ValueError:                             # разные диски
        return False


def _has_dotdot(raw: str) -> bool:
    return ".." in Path(raw).parts


def served_file(serve_dirs, raw) -> Path | None:
    """Файл внутри каталогов модуля или None. `..` в пути и симлинк, ведущий наружу, отклоняются: сравнение по realpath."""
    if not isinstance(raw, str) or not raw or not os.path.isabs(raw) or _has_dotdot(raw):
        return None
    real = os.path.realpath(raw)
    if not os.path.isfile(real):
        return None
    for d in serve_dirs or []:
        base = os.path.realpath(d)
        if real != base and _inside(base, real):
            return Path(real)
    return None


def served_dir(serve_dirs, raw) -> Path | None:
    """Папка, которую можно открыть в проводнике: сам каталог из serve_dirs или его подпапка; иначе None."""
    if not isinstance(raw, str) or not raw or not os.path.isabs(raw) or _has_dotdot(raw):
        return None
    real = os.path.realpath(raw)
    if not os.path.isdir(real):
        return None
    for d in serve_dirs or []:
        base = os.path.realpath(d)
        if _inside(base, real):
            return Path(real)
    return None


IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")


def served_image(serve_dirs, name) -> Path | None:
    """Файл-картинка по имени прямо в каталоге из serve_dirs (блок image с полем file, без задачи): только имя без путей и
    только расширения картинок; дальше те же проверки, что у served_file (никаких `..` и симлинков наружу)."""
    if not isinstance(name, str) or bad_name(name) or not name.lower().endswith(IMAGE_EXTS):
        return None
    for d in serve_dirs or []:
        found = served_file(serve_dirs, str(Path(d) / name))
        if found is not None:
            return found
    return None


def secret_actions(blocks) -> set[str]:
    """Идентификаторы действий, которые стоят за секретными полями ввода (input с secret) в блоках панели, включая вкладки.
    Значение таких действий нигде не пишется и вырезается из ответов."""
    found: set[str] = set()
    for b in blocks if isinstance(blocks, list) else []:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "input" and b.get("secret") is True and isinstance(b.get("action"), str):
            found.add(b["action"])
        for tab in b.get("tabs") or []:
            if isinstance(tab, dict):
                found |= secret_actions(tab.get("blocks"))
    return found


def mask_secret(text, secret) -> str:
    """Убрать секретное значение (пароль из поля ввода) из текста: сообщения модуля и ошибок, прежде чем они пойдут в ответ или журнал."""
    text = str(text)
    if isinstance(secret, str) and secret:
        text = text.replace(secret, "***")
    return text


def artifact_content_type(path: Path) -> str:
    return ARTIFACT_TYPES.get(path.suffix.lower(), "application/octet-stream")


def open_url_allowed(url, allowed_hosts) -> bool:
    """Адрес, который дашборд откроет в новой вкладке по ответу действия: http(s) на 127.0.0.1 или localhost либо
    узел из panel.allowed_hosts."""
    if not isinstance(url, str) or len(url) > 2000:
        return False
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    return parts.hostname in LOCAL_HOSTS or parts.hostname in set(allowed_hosts or [])


def action_result(value, allowed_hosts, secret: str | None = None) -> dict:
    """Ответ модуля на `panel.action` (`{ok, message?, open_url?}`) -> ответ дашборду. Адрес, которого нет в
    разрешённых, не отдаётся: вместо него в сообщении пишется причина."""
    if not isinstance(value, dict) or not isinstance(value.get("ok"), bool):
        return {"ok": False, "message": "модуль вернул ответ не по форме"}
    out: dict = {"ok": value["ok"]}
    message = value.get("message")
    if isinstance(message, str) and message:
        out["message"] = mask_secret(message, secret)[:ACTION_MESSAGE_MAX]
    url = value.get("open_url")
    if url is not None:
        if open_url_allowed(url, allowed_hosts):
            out["open_url"] = url
        else:
            note = "адрес не открыт: хост не разрешён"
            out["message"] = f"{out['message']}; {note}" if out.get("message") else note
    return out


def find_artifact(jobs: dict, job_id: str, name: str) -> str | None:
    """Путь артефакта задачи по имени файла (basename), если такой есть среди её `artifacts`."""
    job = jobs.get(job_id) if isinstance(jobs, dict) else None
    if not job:
        return None
    for art in job.get("artifacts") or []:
        path = art.get("path") if isinstance(art, dict) else None
        if isinstance(path, str) and os.path.basename(path) == name:
            return path
    return None


def bad_name(name: str) -> bool:
    return not name or name in (".", "..") or "/" in name or "\\" in name or ".." in name


def reveal(path: Path) -> None:
    """Открыть папку в проводнике (Windows). Блокирующий вызов: зовётся через asyncio.to_thread."""
    if not hasattr(os, "startfile"):
        raise RuntimeError("открытие папки поддержано только в Windows")
    os.startfile(str(path))            # noqa: S606 - путь уже проверен served_dir()

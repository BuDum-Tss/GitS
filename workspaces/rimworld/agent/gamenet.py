"""Адрес мода RimWorld: определяется автоматически.

Мод слушает только IPv6-петлю ([::1]:8765): 127.0.0.1 (IPv4) он не принимает, а запрос с
IPv6-адресом в заголовке Host отвергает («Bad Request»). Отвечает он только на имя localhost.
Поэтому в URL остаётся localhost, а проверка «игра запущена» пробует оба адреса. Проверка
занимает доли секунды и нужна, потому что подключение к закрытому порту в Windows висит секунды.

Контейнер (docs/RIMWORLD_CONTAINER.md): мод слушает только [::1] хоста, из контейнера он напрямую недоступен. Модуль ходит к нему
через прокси хостового помощника: RIMWORLD_API=`<GITS_RIMWORLD_HOST_URL>/game-api`, токен помощника в заголовке (`auth_headers`).
Не локальное имя в RIMWORLD_API включает этот режим: «игра запущена» тогда значит «`/ping` через прокси отвечает 200».
Без переменной всё как раньше (localhost:8765).
"""
import os
import socket
import urllib.request
from urllib.parse import urlsplit

PORT = 8765
_CANDIDATES = ("::1", "127.0.0.1")
_last_ok: str | None = None


def _try(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


_LOCAL_NAMES = ("localhost", "127.0.0.1", "::1", "[::1]", "")


def configured_url() -> str | None:
    """Адрес мода из RIMWORLD_API (без косой черты в конце); пусто: переменная не задана."""
    value = (os.getenv("RIMWORLD_API") or "").strip().rstrip("/")
    if not value:
        helper = (os.getenv("GITS_RIMWORLD_HOST_URL") or "").strip().rstrip("/")      # контейнер: мод только через прокси помощника
        value = f"{helper}/game-api" if helper else ""
    return value or None


def is_proxy() -> bool:
    """RIMWORLD_API указывает не на петлю этого компьютера (контейнер): запросы к моду идут через прокси помощника."""
    url = configured_url()
    if not url:
        return False
    return (urlsplit(url if "//" in url else "//" + url).hostname or "") not in _LOCAL_NAMES


def auth_headers() -> dict:
    """Заголовок токена помощника для запросов к моду через прокси (в локальном режиме пусто)."""
    token = os.getenv("GITS_RIMWORLD_HOST_TOKEN") or ""
    return {"Authorization": f"Bearer {token}"} if token and is_proxy() else {}


def _proxy_ping(timeout: float) -> bool:
    try:
        req = urllib.request.Request(f"{configured_url()}/ping", headers=auth_headers())
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def find_host(port: int = PORT, timeout: float = 0.25) -> str | None:
    """Адрес, на котором игра сейчас принимает соединения, либо None."""
    global _last_ok
    if is_proxy():
        return urlsplit(configured_url()).hostname if _proxy_ping(max(timeout, 2.0)) else None
    order = ([_last_ok] if _last_ok else []) + [h for h in _CANDIDATES if h != _last_ok]
    for host in order:
        if _try(host, port, timeout):
            _last_ok = host
            return host
    return None


def port_open(port: int = PORT) -> bool:
    return find_host(port) is not None


def base_url(port: int = PORT) -> str:
    """URL для HTTP-запросов к моду: RIMWORLD_API, если задана, иначе имя localhost (именно его мод принимает в заголовке Host)."""
    return configured_url() or f"http://localhost:{port}"

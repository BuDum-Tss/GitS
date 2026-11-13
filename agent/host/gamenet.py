"""Адрес мода RimWorld: определяется автоматически.

Мод слушает только IPv6-петлю ([::1]:8765): 127.0.0.1 (IPv4) он не принимает, а запрос с
IPv6-адресом в заголовке Host отвергает («Bad Request»). Отвечает он только на имя localhost.
Поэтому в URL остаётся localhost, а проверка «игра запущена» пробует оба адреса. Проверка
занимает доли секунды и нужна, потому что подключение к закрытому порту в Windows висит секунды.
"""
import socket

PORT = 8765
_CANDIDATES = ("::1", "127.0.0.1")
_last_ok: str | None = None


def _try(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def find_host(port: int = PORT, timeout: float = 0.25) -> str | None:
    """Адрес, на котором игра сейчас принимает соединения, либо None."""
    global _last_ok
    order = ([_last_ok] if _last_ok else []) + [h for h in _CANDIDATES if h != _last_ok]
    for host in order:
        if _try(host, port, timeout):
            _last_ok = host
            return host
    return None


def port_open(port: int = PORT) -> bool:
    return find_host(port) is not None


def base_url(port: int = PORT) -> str:
    """URL для HTTP-запросов к моду: имя localhost (именно его мод принимает в заголовке Host)."""
    return f"http://localhost:{port}"

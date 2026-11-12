"""Инструменты поиска для агента pi: `search` (выдача без ключа) и `fetch` (текст страницы). Исполняет ядро.

Страница, которую читает модель, недоверенная: она может подсунуть адрес дашборда ядра (`http://127.0.0.1:8766/...`) или
внутренней сети. Поэтому `fetch` берёт только http/https, проверяет адреса, в которые имя разрешается (публичные, не петля,
не частные и не служебные), и каждый шаг перенаправления (не больше 3); размер ответа и время ограничены, тип только
текстовый. Сеть и разрешение имён подменяются в тестах (`TRANSPORT`, `resolve`).
"""
import asyncio
import html as html_lib
import ipaddress
import logging
import re
import socket
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import httpx

logger = logging.getLogger("web_tools")

SEARCH_URL = "https://html.duckduckgo.com/html/"
MAX_RESULTS = 8
MAX_TEXT = 6000
MAX_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 3
FETCH_TIMEOUT_S = 15.0
USER_AGENT = "Mozilla/5.0 (compatible; GitS-search/1.0)"
TEXT_TYPES = ("text/", "application/xhtml+xml", "application/xml", "application/json")
BLOCKED_HOSTS = ("localhost", "localhost.localdomain")

TRANSPORT: httpx.AsyncBaseTransport | None = None          # тесты подставляют httpx.MockTransport


class FetchError(Exception):
    """Страницу брать нельзя или не удалось: текст идёт модели как ошибка инструмента."""


async def resolve(host: str) -> list[str]:
    """Адреса, в которые разрешается имя (блокирующий getaddrinfo уходит в поток)."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return sorted({str(i[4][0]).split("%")[0] for i in infos})


def _public(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not (ip.is_multicast or ip.is_loopback or ip.is_private or ip.is_link_local
                                 or ip.is_reserved or ip.is_unspecified)


async def check_url(url: str) -> None:
    """FetchError, если адрес нельзя открывать: не http/https, локальное имя или непубличный адрес."""
    parts = urlparse(url.strip())
    if parts.scheme not in ("http", "https"):
        raise FetchError("разрешены только адреса http и https")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise FetchError("в адресе нет имени узла")
    if host in BLOCKED_HOSTS or host.endswith(".localhost") or host.endswith(".local") or host.endswith(".internal"):
        raise FetchError("локальные адреса запрещены")
    try:                                    # сокращённые и числовые записи (127.1, 2130706433, 0x7f.0.0.1) читает и ОС
        addrs = [socket.inet_ntoa(socket.inet_aton(host))]
    except OSError:
        addrs = []
    try:
        addrs = addrs or await resolve(host)
    except Exception:
        raise FetchError("адрес не разрешился") from None
    if not addrs:
        raise FetchError("адрес не разрешился")
    if not all(_public(a) for a in addrs):
        raise FetchError("адрес ведёт в закрытую сеть: запрещено")


def html_to_text(raw: str) -> str:
    """Текст страницы без тегов, скриптов и стилей."""
    raw = re.sub(r"(?is)<(script|style|noscript|template|svg|head)\b.*?</\1\s*>", " ", raw)
    raw = re.sub(r"(?s)<!--.*?-->", " ", raw)
    raw = re.sub(r"(?i)<(br|/p|/div|/li|/tr|/h[1-6])\b[^>]*>", "\n", raw)
    raw = re.sub(r"(?s)<[^>]*>", " ", raw)
    raw = html_lib.unescape(raw)
    lines = [re.sub(r"[ \t\r\f\v ]+", " ", ln).strip() for ln in raw.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(ln for ln in lines if ln)).strip()


async def fetch(url: str) -> str:
    """Текст страницы (до MAX_TEXT символов). FetchError с причиной при отказе."""
    try:
        async with asyncio.timeout(FETCH_TIMEOUT_S):
            return await _fetch(url)
    except TimeoutError:
        raise FetchError("страница не ответила за 15 секунд") from None


async def _fetch(url: str) -> str:
    async with httpx.AsyncClient(transport=TRANSPORT, follow_redirects=False, timeout=FETCH_TIMEOUT_S,
                                 headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain,*/*;q=0.5"}) as client:
        current = url.strip()
        for hop in range(MAX_REDIRECTS + 1):
            await check_url(current)
            try:
                async with client.stream("GET", current) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308):
                        target = resp.headers.get("location")
                        if not target:
                            raise FetchError("перенаправление без адреса")
                        if hop == MAX_REDIRECTS:
                            raise FetchError("слишком много перенаправлений")
                        current = urljoin(current, target)
                        continue
                    if resp.status_code >= 400:
                        raise FetchError(f"страница ответила кодом {resp.status_code}")
                    ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                    if not ctype.startswith(TEXT_TYPES):
                        raise FetchError(f"тип содержимого не текстовый: {ctype or 'неизвестный'}")
                    body = bytearray()
                    async for chunk in resp.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_BYTES:
                            break
                    raw = bytes(body[:MAX_BYTES]).decode(resp.encoding or "utf-8", "replace")
            except httpx.HTTPError as e:
                raise FetchError(f"сеть: {type(e).__name__}") from None
            text = html_to_text(raw) if ("html" in ctype or "xml" in ctype) else raw.strip()
            return text[:MAX_TEXT] or "(страница пуста)"
    raise FetchError("слишком много перенаправлений")


# ── выдача поисковика ──────────────────────────────────────────────

_LINK = re.compile(r'(?is)<a\b[^>]*class="[^"]*\bresult__a\b[^"]*"[^>]*>(.*?)</a>')
_HREF = re.compile(r'(?is)\bhref="([^"]*)"')
_SNIPPET = re.compile(r'(?is)<(?:a|div|td)\b[^>]*class="[^"]*\bresult__snippet\b[^"]*"[^>]*>(.*?)</(?:a|div|td)>')


def _clean(fragment: str) -> str:
    return re.sub(r"\s+", " ", html_lib.unescape(re.sub(r"(?s)<[^>]*>", "", fragment))).strip()


def _real_url(href: str) -> str:
    href = html_lib.unescape(href)
    if href.startswith("//"):
        href = "https:" + href
    parts = urlparse(href)
    if parts.netloc.endswith("duckduckgo.com") and parts.path.startswith("/l/"):
        target = parse_qs(parts.query).get("uddg", [""])[0]
        return unquote(target)
    return href


def parse_results(page: str, limit: int = MAX_RESULTS) -> list[dict]:
    """Результаты из HTML-версии DuckDuckGo: список {title, url, snippet}. Чистая функция."""
    out: list[dict] = []
    links = list(_LINK.finditer(page))
    for i, m in enumerate(links):
        tag = m.group(0)[: m.group(0).find(">") + 1]
        href = _HREF.search(tag)
        url = _real_url(href.group(1)) if href else ""
        if not url.startswith(("http://", "https://")):
            continue
        end = links[i + 1].start() if i + 1 < len(links) else len(page)
        snip = _SNIPPET.search(page, m.end(), end)
        out.append({"title": _clean(m.group(1)), "url": url, "snippet": _clean(snip.group(1)) if snip else ""})
        if len(out) >= limit:
            break
    return out


def format_results(results: list[dict]) -> str:
    return "\n\n".join(f"{i}. {r['title']}\n{r['url']}\n{r['snippet']}".rstrip() for i, r in enumerate(results, 1))


async def search(query: str) -> str:
    """Выдача поисковика: до 8 строк «заголовок, адрес, сниппет». FetchError при сбое сети."""
    query = (query or "").strip()[:300]
    if not query:
        raise FetchError("пустой запрос")
    try:
        async with asyncio.timeout(FETCH_TIMEOUT_S):
            async with httpx.AsyncClient(transport=TRANSPORT, follow_redirects=False, timeout=FETCH_TIMEOUT_S,
                                         headers={"User-Agent": USER_AGENT}) as client:
                resp = await client.post(SEARCH_URL, data={"q": query})
                if resp.status_code != 200:
                    raise FetchError(f"поисковик ответил кодом {resp.status_code}")
                page = resp.text[:MAX_BYTES]
    except TimeoutError:
        raise FetchError("поисковик не ответил за 15 секунд") from None
    except httpx.HTTPError as e:
        raise FetchError(f"сеть: {type(e).__name__}") from None
    results = parse_results(page)
    return format_results(results) if results else "Ничего не найдено."


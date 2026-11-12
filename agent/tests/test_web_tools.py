"""Инструменты поиска для pi (`web_tools.py`) и поиск агентом pi (`web_search.py`): сеть и разрешение имён подменены."""
import asyncio

import httpx
import pytest

import settings
import web_search
import web_tools
from contracts import EndEvent, TextEvent, ToolEvent

PUBLIC = "93.184.216.34"
DDG_PAGE = """
<html><body><div class="results">
<div class="result results_links"><h2 class="result__title">
<a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwiki.example.org%2FRoof%3Fa%3D1%26b%3D2&amp;rut=abc">Roof <b>support</b> &amp; walls</a></h2>
<a class="result__snippet" href="//duckduckgo.com/l/?uddg=x">Крыша держится на <b>опорах</b> &quot;3x3&quot;.</a></div>
<div class="result"><a class="result__a" href="https://other.example.com/page">Второй результат</a>
<a class="result__snippet" href="#">Второй сниппет</a></div>
<div class="result"><a class="result__a" href="/relative/skip">Относительная ссылка</a></div>
</div></body></html>
"""


@pytest.fixture(autouse=True)
def _net(monkeypatch):
    """Имена разрешаются в публичный адрес, кроме особых; сеть возвращает то, что положит тест в `web_tools.TRANSPORT`."""
    table = {"evil.example.com": ["10.0.0.5"], "mixed.example.com": [PUBLIC, "127.0.0.1"], "v6.example.com": ["::1"],
             "mapped.example.com": ["::ffff:127.0.0.1"], "meta.example.com": ["169.254.169.254"]}

    async def resolve(host):
        if host in table:
            return table[host]
        try:
            import ipaddress
            ipaddress.ip_address(host)
            return [host]
        except ValueError:
            return [PUBLIC]

    monkeypatch.setattr(web_tools, "resolve", resolve)
    yield
    monkeypatch.setattr(web_tools, "TRANSPORT", None)


def _serve(handler):
    web_tools.TRANSPORT = httpx.MockTransport(handler)


# ── разбор выдачи и страниц ────────────────────────────────────────

def test_parse_results_from_saved_html():
    results = web_tools.parse_results(DDG_PAGE)
    assert results == [
        {"title": "Roof support & walls", "url": "https://wiki.example.org/Roof?a=1&b=2",
         "snippet": 'Крыша держится на опорах "3x3".'},
        {"title": "Второй результат", "url": "https://other.example.com/page", "snippet": "Второй сниппет"},
    ]
    text = web_tools.format_results(results)
    assert text.startswith("1. Roof support & walls\nhttps://wiki.example.org/Roof?a=1&b=2\n")
    assert web_tools.parse_results("<html>ничего</html>") == []
    many = "".join(f'<a class="result__a" href="https://e.com/{i}">t{i}</a>' for i in range(20))
    assert len(web_tools.parse_results(many)) == 8


def test_html_to_text_drops_tags_scripts_styles():
    page = "<html><head><title>T</title><style>p{}</style></head><body><script>alert(1)</script><h1>Заголовок</h1><p>Один &amp; два</p><!-- x --></body></html>"
    assert web_tools.html_to_text(page) == "Заголовок\nОдин & два"


async def test_search_posts_query_and_parses():
    seen = {}

    def handler(request):
        seen["body"] = request.content.decode()
        seen["method"] = request.method
        return httpx.Response(200, text=DDG_PAGE)

    _serve(handler)
    out = await web_tools.search("rimworld roof")
    assert seen["method"] == "POST" and "q=rimworld+roof" in seen["body"]
    assert "https://wiki.example.org/Roof?a=1&b=2" in out
    _serve(lambda r: httpx.Response(200, text="<html></html>"))
    assert await web_tools.search("ничего нет") == "Ничего не найдено."
    _serve(lambda r: httpx.Response(503))
    with pytest.raises(web_tools.FetchError, match="503"):
        await web_tools.search("сломано")


# ── безопасность fetch ─────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "ftp://example.com/x", "gopher://example.com", "javascript:alert(1)", "data:text/html,hi", "http:///nohost",
    "http://localhost:8766/api/state", "http://LOCALHOST./x", "http://sub.localhost/x", "http://127.0.0.1:8766/api/core",
    "http://127.1/", "http://2130706433/", "http://0x7f.0.0.1/", "http://0.0.0.0/", "http://[::1]:8766/", "http://[::ffff:127.0.0.1]/",
    "http://10.1.2.3/", "http://192.168.0.1/", "http://172.16.5.5/", "http://169.254.169.254/latest/meta-data",
    "http://100.64.0.1/", "http://evil.example.com/", "http://mixed.example.com/", "http://v6.example.com/",
    "http://mapped.example.com/", "http://meta.example.com/", "http://printer.local/", "http://db.internal/",
])
async def test_fetch_refuses_unsafe_urls(url):
    hits = []
    _serve(lambda r: hits.append(r) or httpx.Response(200, text="секрет", headers={"content-type": "text/plain"}))
    with pytest.raises(web_tools.FetchError):
        await web_tools.fetch(url)
    assert hits == []                          # до сети дело не дошло


async def test_fetch_ok_text_and_trim():
    _serve(lambda r: httpx.Response(200, text="<html><body><p>" + "слово " * 3000 + "</p></body></html>",
                                    headers={"content-type": "text/html; charset=utf-8"}))
    out = await web_tools.fetch("https://wiki.example.org/page")
    assert out.startswith("слово слово") and len(out) == web_tools.MAX_TEXT
    _serve(lambda r: httpx.Response(200, text='{"a": 1}', headers={"content-type": "application/json"}))
    assert await web_tools.fetch("https://api.example.org/x") == '{"a": 1}'


async def test_fetch_refuses_non_text_and_errors():
    _serve(lambda r: httpx.Response(200, content=b"\x89PNG", headers={"content-type": "image/png"}))
    with pytest.raises(web_tools.FetchError, match="не текстовый"):
        await web_tools.fetch("https://example.org/a.png")
    _serve(lambda r: httpx.Response(200, content=b"MZ", headers={"content-type": "application/octet-stream"}))
    with pytest.raises(web_tools.FetchError, match="не текстовый"):
        await web_tools.fetch("https://example.org/a.exe")
    _serve(lambda r: httpx.Response(404, text="нет", headers={"content-type": "text/html"}))
    with pytest.raises(web_tools.FetchError, match="404"):
        await web_tools.fetch("https://example.org/none")


async def test_fetch_caps_response_size():
    big = b"a" * (5 * 1024 * 1024)
    _serve(lambda r: httpx.Response(200, content=big, headers={"content-type": "text/plain"}))
    out = await web_tools.fetch("https://example.org/big")
    assert 0 < len(out) <= web_tools.MAX_TEXT


async def test_redirect_to_private_address_is_refused():
    hits = []

    def handler(request):
        hits.append(str(request.url))
        if request.url.host == "start.example.org":
            return httpx.Response(302, headers={"location": "http://127.0.0.1:8766/api/settings"})
        return httpx.Response(200, text="внутренности", headers={"content-type": "text/plain"})

    _serve(handler)
    with pytest.raises(web_tools.FetchError, match="закрытую сеть"):
        await web_tools.fetch("https://start.example.org/")
    assert hits == ["https://start.example.org/"]
    _serve(lambda r: httpx.Response(302, headers={"location": "file:///etc/passwd"}))
    with pytest.raises(web_tools.FetchError, match="http"):
        await web_tools.fetch("https://start.example.org/")


async def test_redirects_limited_to_three_and_followed():
    def chain(n_hops):
        def handler(request):
            step = int(request.url.path.strip("/") or 0)
            if step < n_hops:
                return httpx.Response(301, headers={"location": f"/{step + 1}"})
            return httpx.Response(200, text="конец", headers={"content-type": "text/plain"})
        return handler

    _serve(chain(3))
    assert await web_tools.fetch("https://example.org/0") == "конец"
    _serve(chain(4))
    with pytest.raises(web_tools.FetchError, match="перенаправлений"):
        await web_tools.fetch("https://example.org/0")


async def test_fetch_time_limit(monkeypatch):
    async def slow(request):
        await asyncio.sleep(5)
        return httpx.Response(200, text="поздно", headers={"content-type": "text/plain"})

    monkeypatch.setattr(web_tools, "FETCH_TIMEOUT_S", 0.2)
    _serve(slow)
    with pytest.raises(web_tools.FetchError, match="15 секунд"):
        await web_tools.fetch("https://example.org/slow")


# ── поиск агентом pi ───────────────────────────────────────────────

class FakeRuntime:
    """Подделка PiRuntime: проигрывает сценарий и вызывает инструменты ядра."""
    instances = []

    def __init__(self, script):
        self.script, self.closed, self.tools, self.system, self.results = script, False, {}, "", []
        FakeRuntime.instances.append(self)

    async def start(self, system, tools):
        self.system, self.tools = system, {t.name: t for t in tools}

    async def run(self, prompt):
        for step in self.script:
            if step[0] == "text":
                yield TextEvent(step[1])
            elif step[0] == "tool":
                yield ToolEvent(step[1], step[2])
                self.results.append(await self.tools[step[1]].handler(step[2]))
            elif step[0] == "end":
                yield EndEvent(step[1], step[2] if len(step) > 2 else None, {})
            elif step[0] == "hang":
                await asyncio.sleep(60)

    async def close(self):
        self.closed = True


def _use(monkeypatch, script):
    FakeRuntime.instances = []
    settings.update(agent_runtime="pi")
    monkeypatch.setattr(web_search, "_pi_runtime", lambda: FakeRuntime(script))


async def test_pi_search_runs_tools_and_closes(monkeypatch):
    _use(monkeypatch, [("tool", "search", {"query": "roof"}), ("tool", "fetch", {"url": "http://127.0.0.1:8766/x"}),
                       ("text", "Факт 1.\nИсточники: https://wiki.example.org"), ("end", "ok")])
    _serve(lambda r: httpx.Response(200, text=DDG_PAGE))
    out = await web_search.search("rimworld roof support")
    rt = FakeRuntime.instances[0]
    assert out.startswith("Факт 1.") and rt.closed and len(FakeRuntime.instances) == 1
    assert "WebSearch" not in rt.system and "search" in rt.system and "fetch" in rt.system
    assert set(rt.tools) == {"search", "fetch"}
    assert rt.results[0][0] is True and "wiki.example.org" in rt.results[0][1]
    assert rt.results[1][0] is False and "закрытую сеть" in rt.results[1][1]      # дашборд ядра модели недоступен


async def test_pi_search_does_not_use_claude_or_its_quota(monkeypatch):
    import claude_quota
    monkeypatch.setattr(claude_quota, "blocked", lambda: True)
    _use(monkeypatch, [("text", "Ответ."), ("end", "ok")])
    assert await web_search.search("rimworld roof support") == "Ответ."


async def test_pi_search_errors(monkeypatch):
    _use(monkeypatch, [("end", "error", "нет входа")])
    assert await web_search.search("rimworld roof support") == "Поиск не удался."
    assert FakeRuntime.instances[0].closed
    monkeypatch.setattr(web_search, "TIMEOUT", 0.2)
    _use(monkeypatch, [("hang",)])
    assert "не уложился" in await web_search.search("rimworld roof support")
    assert FakeRuntime.instances[0].closed
    _use(monkeypatch, [("end", "ok")])
    assert await web_search.search("rimworld roof support") == "Ничего не нашла."


def test_pi_runtime_without_history(monkeypatch):
    from adapters.runtime_pi import PiRuntime
    rt = PiRuntime(persist=False)
    rt._system, rt._tools = "с", {}
    assert rt._init_message()["session_file"] is None
    assert PiRuntime()._init_message()["session_file"]

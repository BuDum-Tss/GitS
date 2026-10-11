"""POST /api/notify: объявление от внешнего скрипта или теста идёт в менеджер внимания как факт для персонажа."""
import aiohttp
from aiohttp import web
from test_core_wiring import JSON, free_port

import dashboard


class FakeAttention:
    def __init__(self):
        self.speech = []

    async def submit_speech(self, module_id, say):
        self.speech.append((module_id, say))


async def serve():
    port = free_port()
    att = FakeAttention()
    dashboard.set_attention(att)
    runner = web.AppRunner(dashboard.build_app())
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    return f"http://127.0.0.1:{port}", att, runner


async def test_notify_submits_an_announcement():
    base, att, runner = await serve()
    try:
        async with aiohttp.ClientSession() as http:
            async with http.post(base + "/api/notify", json={"text": " Запускаю RimWorld: тест ", "source": "rimworld-tests"},
                                 headers=JSON) as r:
                assert r.status == 200 and (await r.json()) == {"ok": True}
        assert att.speech == [("rimworld-tests", {"text": "Запускаю RimWorld: тест", "mode": "fact", "urgency_hint": 3,
                                                  "announce": True})]            # AN-2: объявление, а не обычная речь
    finally:
        dashboard.set_attention(None)
        await runner.cleanup()


async def test_notify_rejects_bad_requests():
    base, att, runner = await serve()
    try:
        async with aiohttp.ClientSession() as http:
            async def post(body, status, **kw):
                async with http.post(base + "/api/notify", json=body, headers=kw.get("headers", JSON)) as r:
                    assert r.status == status, (body, r.status, await r.text())

            await post({"text": "а" * 301, "source": "t"}, 400)             # длиннее 300
            await post({"text": "  ", "source": "t"}, 400)
            await post({"text": 5, "source": "t"}, 400)
            await post({"text": "ок", "source": "плохое имя"}, 400)
            await post({"text": "ок"}, 400)
            await post([1], 400)
            await post({"text": "ок", "source": "t"}, 403, headers={"Origin": "http://evil.example"})     # чужая страница
            async with http.post(base + "/api/notify", data="text=ok&source=t") as r:                     # не JSON
                assert r.status == 403
        assert att.speech == []
        dashboard.set_attention(None)
        async with aiohttp.ClientSession() as http:
            async with http.post(base + "/api/notify", json={"text": "ок", "source": "t"}, headers=JSON) as r:
                assert r.status == 503                                        # внимание не подключено
    finally:
        await runner.cleanup()


async def test_notify_refuses_non_local_peers():
    class Req:
        remote = "192.168.1.5"
    resp = await dashboard.handle_api_notify(Req())
    assert resp.status == 403

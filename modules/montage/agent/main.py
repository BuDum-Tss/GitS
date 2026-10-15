"""Точка входа модуля монтажа (ТЗ 20): `python main.py` из папки agent/. Ядро GitS запускает модуль само (modules.yaml).

Подключается к ядру по контракту модулей (SDK `glados_module`), объявляет инструменты `start`, `status`, `revise`, `cancel`,
ведёт очередь задач и конвейер. Ничего не публикует (закон ТЗ 20): публикует ядро по просьбе пользователя.
"""
import asyncio
import logging
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE)]

from glados_module import ModuleClient  # noqa: E402

import approval_panel as ap  # noqa: E402
from claude_runner import SdkRunner  # noqa: E402
from core_api import ClientCore  # noqa: E402
from jobs import JobStore  # noqa: E402
from layout import DEFAULT, Layout  # noqa: E402
from manager import Manager  # noqa: E402
from manifest import build_manifest, serve_dirs  # noqa: E402
from pipeline import Pipeline  # noqa: E402

log = logging.getLogger("montage")


def wire(client: ModuleClient, manager: Manager) -> None:
    """Подключить обработчики инструментов, ответов, отмены и тем ядра к менеджеру."""

    @client.tool("start")
    async def start(args):
        return await manager.start(args)

    @client.tool("open_studio")
    async def open_studio(args):
        return await manager.open_studio(str(args.get("job_id") or ""))

    @client.panel_action("open_studio")
    async def panel_open_studio(args):
        value = await manager.open_studio(str(args.get("job_id") or ""))
        return {"ok": True, "open_url": value["url"], "message": "Studio открыт"}

    # одобрение по этапам (TZ 15b): каждая кнопка - свой id действия, проверка вопроса в менеджере
    def decision(action: str):
        async def handler(args):
            return await manager.panel_decide(action, args if isinstance(args, dict) else {})
        return handler

    for action in ap.ACTIONS:
        client.panel_action(action)(decision(action))

    @client.tool("status")
    async def status(args):
        return await manager.status(args)

    @client.tool("revise")
    async def revise(args):
        return await manager.revise(args)

    @client.tool("cancel")
    async def cancel(args):
        await manager.cancel(str(args.get("job_id") or ""))
        return {"cancelled": True}

    @client.on_job_answer
    async def answer(job_id, question_id, value):
        manager.answer(job_id, question_id, value)

    @client.on_job_cancel
    async def job_cancel(job_id):
        await manager.cancel(str(job_id))

    @client.on_topic("battery")
    async def battery(data):
        manager.pipeline.set_sleeping(bool(data.get("sleeping")))

    @client.on_topic("settings")
    async def settings(_data):
        await manager.publish_state()

    @client.on_reconnect
    async def reconnect():
        await client.resend_jobs()
        await manager.publish_state()

    @client.on_shutdown
    async def shutdown(_reason):
        await manager.stop()


def build(layout: Layout = DEFAULT, *, client: ModuleClient | None = None, runner=None, renderer=None, qa=None):
    """Собрать модуль: (клиент, менеджер). Параметры позволяют подставить заглушки (тесты, живой прогон против FakeCore)."""
    client = client or ModuleClient(build_manifest(serve_dirs(layout.jobs)))
    core = ClientCore(client)
    store = JobStore(layout.jobs)
    if renderer is None:
        from render import RemotionRenderer
        renderer = RemotionRenderer()
    pipeline = Pipeline(store, core, runner or SdkRunner(layout), layout, renderer, qa)
    manager = Manager(store, core, pipeline, layout)
    wire(client, manager)
    return client, manager


async def run(client: ModuleClient, manager: Manager) -> int:
    """Подключение к ядру и работа: восстановление задач, очередь, загрузка регламента в справочник."""
    manager.start_worker()
    background: list[asyncio.Task] = []

    async def after_connect():
        await client.wait_connected()
        manager.pipeline.set_sleeping(bool((client.battery or {}).get("sleeping")))
        await manager.recover()
        await manager.publish_state()
        await manager.kb_loop()
    background.append(asyncio.create_task(after_connect()))
    try:
        return await client.run()
    finally:
        for t in background:
            t.cancel()
        await manager.stop()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s", datefmt="%H:%M:%S")
    client, manager = build()
    try:
        return asyncio.run(run(client, manager))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())

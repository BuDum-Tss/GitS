"""Точка входа окружения «Монтаж» (ТЗ 20): `python main.py` из папки agent/. Ядро GitS запускает модуль само (peers.yaml).

Подключается к ядру по контракту модулей (SDK `gits_sdk`) и даёт ГЛаДОС окружение (docs/WORKSPACE_CONTRACT.md): инструменты
`skills`, `files`, `run`, `work`, `site`, `resources`, навыки, рабочую область, команды и сайт. Своих агентов не запускает:
монтирует сама ГЛаДОС. Ничего не публикует (закон ТЗ 20): публикует ядро по просьбе пользователя.
"""
import asyncio
import logging
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE)]

from gits_sdk import WorkspaceClient  # noqa: E402

from commands import Commands  # noqa: E402
from core_api import ClientCore  # noqa: E402
from jobs import JobStore  # noqa: E402
from layout import DEFAULT, Layout  # noqa: E402
from manager import Manager  # noqa: E402
from manifest import build_manifest, serve_dirs  # noqa: E402

log = logging.getLogger("montage")


def wire(client: WorkspaceClient, manager: Manager) -> None:
    """Подключить обработчики инструментов, окружения, отмены и тем ядра к менеджеру."""

    manager.tools.attach(client)                 # skills, files, run: песочница, навыки и команды из манифеста

    @client.tool("work")
    async def work(args):
        return await manager.journal.audited("work", args, manager.work)

    @client.tool("site")
    async def site(args):
        return await manager.journal.audited("site", args, manager.site_tool)

    @client.tool("resources")
    async def resources(args):
        async def call(_args):
            return {"text": manager.resources_text()}
        return await manager.journal.audited("resources", args, call)

    # окружение агента (контракт 3.2, раздел 7): запросы workspace.enter / afk / exit / resume
    @client.on_request("workspace.enter")
    async def ws_enter(body):
        return await manager.enter_workspace(body or {})

    @client.on_request("workspace.afk")
    async def ws_afk(body):
        return await manager.afk_workspace(body or {})

    @client.on_request("workspace.exit")
    async def ws_exit(body):
        return await manager.exit_workspace(body or {})

    @client.on_request("workspace.resume")
    async def ws_resume(body):
        return await manager.enter_workspace({**(body or {}), "restored": True})

    @client.on_job_cancel
    async def job_cancel(job_id):
        await manager.cancel(str(job_id))

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


def build(layout: Layout = DEFAULT, *, client: WorkspaceClient | None = None, renderer=None, qa=None,
          url: str | None = None, token: str | None = None, site_port: int | None = None):
    """Собрать модуль: (клиент, менеджер). Параметры позволяют подставить заглушки (тесты, живой прогон против FakeCore);
    url и token: адрес и токен ядра (при аренде они приходят в acquire, иначе берутся из окружения)."""
    client = client or WorkspaceClient(build_manifest(serve_dirs(layout.jobs)), url=url, token=token)
    core = ClientCore(client, volume=lambda: layout.character.parent)     # папка персонажа: общий том с ядром и голосом
    store = JobStore(layout.jobs)
    if renderer is None:
        from render import RemotionRenderer
        renderer = RemotionRenderer()
    commands = Commands(store, core, layout, renderer, qa)
    manager = Manager(store, core, commands, layout, site_port=site_port)
    wire(client, manager)
    return client, manager


async def run(client: WorkspaceClient, manager: Manager) -> int:
    """Подключение к ядру и работа: восстановление единиц, сводка. Сайт поднимается при входе в окружение (`workspace.enter`)."""
    manager.start_worker()
    background: list[asyncio.Task] = []

    async def after_connect():
        await client.wait_connected()
        await manager.recover()
        await manager.publish_state()
    background.append(asyncio.create_task(after_connect()))
    try:
        return await client.run()
    finally:
        for t in background:
            t.cancel()
        await manager.stop()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s", datefmt="%H:%M:%S")
    import lease_host
    if lease_host.enabled():                      # контейнер: сервис ждёт аренды, к ядру хозяина подключается по acquire
        try:
            return asyncio.run(lease_host.serve(build, run))
        except KeyboardInterrupt:
            return 0
    client, manager = build()
    try:
        return asyncio.run(run(client, manager))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())

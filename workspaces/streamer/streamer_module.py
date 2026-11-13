"""Окружение «Стример»: склейка подсистем `twitch` и `obs` в одном процессе.

`StreamerModule(client, memory_dir)` создаёт обе подсистемы на ОДНОМ клиенте ядра (инструменты `twitch_*` и `obs_*`, события с
прежними видами `twitch.*` и `obs.*`, доставка ответов `deliver`), запускает их фоновые задачи и отправляет общее состояние
окружения: сводку обеих подсистем одним сообщением. Сами подсистемы друг друга не знают."""
import asyncio
import logging
import time

from gits_sdk import PeerClient
from obs.obs_module import ObsModule
from twitch.common import scrub
from twitch.twitch_module import TwitchModule

log = logging.getLogger("streamer")

STATE_EVERY_S = 30.0                  # состояние повторяется не реже раза в 30 с
STATE_POLL_S = 2.0                    # как часто проверяется, изменилась ли сводка


class StreamerModule:
    def __init__(self, client: PeerClient, memory_dir: str, obs_url: str | None = None, obs_password: str | None = None):
        self.client = client
        self.memory_dir = memory_dir
        self.twitch = TwitchModule(client, memory_dir)
        self.obs = ObsModule(client, obs_url, obs_password)
        self.tasks: list[asyncio.Task] = []
        self.state_task: asyncio.Task | None = None

    def start(self) -> list[asyncio.Task]:
        loop = asyncio.get_running_loop()
        self.tasks += self.twitch.start() + self.obs.start()
        self.state_task = loop.create_task(self.run_state())
        self.tasks.append(self.state_task)
        return self.tasks

    async def stop(self) -> None:
        if self.state_task is not None:
            self.state_task.cancel()
            await asyncio.gather(self.state_task, return_exceptions=True)
        await self.twitch.stop()
        await self.obs.stop()
        self.tasks.clear()

    def snapshot(self) -> tuple[str, dict]:
        """Общая сводка: строки подсистем через «; », данные по подсистемам в `twitch` и `obs`."""
        t_summary, t_data = self.twitch.snapshot()
        o_summary, o_data = self.obs.snapshot()
        return f"{t_summary}; {o_summary}", {"twitch": t_data, "obs": o_data}

    async def run_state(self) -> None:
        last_key, sent_at = None, 0.0
        while True:
            try:
                await self.obs.prepare_state()
                summary, data = self.snapshot()
                key = (summary, tuple(data["twitch"]["failed_subscriptions"]))
                if key != last_key or time.time() - sent_at >= STATE_EVERY_S:
                    await self.client.state(summary, data)
                    last_key, sent_at = key, time.time()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("состояние не отправлено: %s", scrub(str(e)))
            await asyncio.sleep(STATE_POLL_S)

"""Исходящие команды шины модулей (ТЗ 29, ARC-7): темы одному и всем, присутствие с ограничением частоты, ответы на
вопросы задач. Класс не знает ни внимания, ни моделей: ему нужна только шина (соединения, включённость, фоновые задачи)."""
import asyncio
import logging

import flow_trace

logger = logging.getLogger("hub")

PRESENCE_MIN_INTERVAL_S = 0.1          # CORE-5: тема presence не чаще 10 раз в секунду


class Outbound:
    def __init__(self, hub):
        """hub - ModuleHub: connection/connections, _enabled, _spawn, _monotonic."""
        self._hub = hub
        self._presence_latest: dict | None = None
        self._presence_last = float("-inf")
        self._presence_timer: asyncio.TimerHandle | None = None

    def stop(self) -> None:
        if self._presence_timer is not None:
            self._presence_timer.cancel()

    async def safe_send_topic(self, conn, topic: str, data: dict) -> None:
        from module_hub import ModuleError
        try:
            await conn.send("topic", {"topic": topic, "data": data})
        except ModuleError:
            pass

    async def send_topic(self, module_id: str, topic: str, data: dict) -> None:
        """Тема одному модулю (если подключён)."""
        conn = self._hub.connection(module_id)
        if conn is not None:
            await self.safe_send_topic(conn, topic, data)

    async def broadcast(self, topic: str, data: dict) -> None:
        """Тема всем подключённым и включённым модулям, подписанным на неё (CORE-5)."""
        targets = [c for c in self._hub.connections() if topic in c.spec.subscribe and self._hub._enabled(c)]
        await asyncio.gather(*[self.safe_send_topic(c, topic, data) for c in targets])

    def notify(self, topic: str, data: dict) -> None:
        """Как broadcast, но из синхронного кода (смена профиля, присутствие): рассылка уходит фоновой задачей."""
        coro = self.broadcast(topic, data)
        try:
            self._hub._spawn(coro)
        except RuntimeError:
            coro.close()                               # цикла событий нет (старт или тесты): некому рассылать; корутину закрываем

    def broadcast_presence(self, data: dict) -> None:
        """Тема presence не чаще 10 раз в секунду; в паузе побеждает последнее значение (CORE-5)."""
        self._presence_latest = data
        if self._presence_timer is not None:
            return
        wait = PRESENCE_MIN_INTERVAL_S - (self._hub._monotonic() - self._presence_last)
        if wait <= 0:
            self._flush_presence()
            return
        try:
            self._presence_timer = asyncio.get_running_loop().call_later(wait, self._flush_presence)
        except RuntimeError:
            pass

    def _flush_presence(self) -> None:
        self._presence_timer = None
        data, self._presence_latest = self._presence_latest, None
        if data is not None:
            self._presence_last = self._hub._monotonic()
            self.notify("presence", data)
            try:
                if self._hub.is_connected("pet"):                        # ТЗ 17: питомец получает присутствие, это его «вывод»
                    flow_trace.step(None, "out_pet", "speech_queue", "out_pet", "presence", label="presence")
            except Exception:
                pass

    async def send_job_answer(self, module_id: str, job_id: str, question_id: str, answer) -> bool:
        """Ответ на вопрос задачи (MOD-20) уходит модулю как job.answer. False: модуль не подключён (отправим при
        следующем job.update этой задачи после переподключения) или отправка не удалась."""
        from module_hub import ModuleError
        conn = self._hub.connection(module_id)
        if conn is None:
            return False
        try:
            await conn.send("job.answer", {"job_id": job_id, "question_id": question_id, "answer": answer})
            return True
        except ModuleError as e:
            logger.warning(f"job.answer для {module_id}/{job_id} не отправлен: {e.message}")
            return False

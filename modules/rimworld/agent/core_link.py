"""Связь модуля RimWorld с ядром ГЛаДОС (ТЗ 11, RW-6): замена прежнего speech_link.py.

Внешний интерфейс тот же, что был у SpeechLink, чтобы контроллер и комментатор не переписывать: `online`, `last_user_ts()`,
`is_speaking()`, `repeats(text)`, `say(text, emotion)`, `emotion(event, emotion)`, `event(kind, data)`, `run()`. Разница в том,
что вместо HTTP к :8766 работает клиент модуля (`glados_module.ModuleClient`, WebSocket к шине ядра):
  - реплики идут в ядро как `say` (verbatim): речь одна, очередью и разрешением говорить распоряжается менеджер внимания;
  - присутствие (говорит ли она, когда говорил пользователь, что сказала) приходит темой `presence`;
  - общие настройки (стример, «не отвлекать», модель игрока и др.) приходят в `welcome` и темой `settings` и пишутся в
    `settings.json` модуля;
  - события игры уходят в ядро как события модуля `rimworld.*` (RW-9): ядро решает, важны ли они сейчас;
  - итог забега: при настройке `telegram_after_run` модуль сам просит ядро вызвать `telegram.post` (RW-12).
"""
import asyncio
import logging
from collections import deque

from glados_module import ToolError

import events_log
import settings

logger = logging.getLogger("core_link")

SETTINGS_SYNC_S = 1.0           # как часто сверять общие настройки ядра с файлом настроек модуля
SAY_EXPIRES_S = 20              # реплика комментатора устаревает за столько секунд (ядро не озвучит запоздавшую)
POST_TIMEOUT_S = 150            # telegram.post может ждать разрешения; ядро само ограничивает инструмент 120 с
RECENT_KEEP = 10                # сколько последних реплик ассистентки помнить для проверки повторов

# Короткий вид события, как его зовёт игровой код -> (вид для ядра, срочность по умолчанию). Срочность и объявление в
# `emits` манифеста должны совпадать (module_manifest.EMITS берёт значения отсюда).
EVENTS = {
    "colonists": ("rimworld.colonists", 0),     # служебное: ядро обновляет отношения и граф памяти, не озвучивает
    "run_ended": ("rimworld.run_ended", 3),
    "raid": ("rimworld.raid", 5),
    "death": ("rimworld.death", 4),
    "fire": ("rimworld.fire", 4),
    "letter": ("rimworld.letter", 2),
    "goal": ("rimworld.goal", 3),
    "run_video": ("rimworld.run_video", 2),
}
# сколько секунд событие остаётся актуальным (после этого ядро его не озвучивает)
EXPIRES_S = {"rimworld.raid": 180, "rimworld.fire": 180, "rimworld.death": 300, "rimworld.letter": 300,
             "rimworld.goal": 600, "rimworld.run_ended": 1800, "rimworld.run_video": 3600}


class CoreLink:
    def __init__(self, client):
        self.client = client
        self._presence: dict = {}
        self._recent: deque = deque(maxlen=RECENT_KEEP)
        self._last_say_seen = ""
        self._applied: dict | None = None            # какие общие настройки уже записаны в файл настроек модуля
        self._background: set[asyncio.Task] = set()
        client.on_topic("presence")(self._on_presence)
        client.on_topic("settings")(self._on_settings)

    # ── то, что она делает сейчас: синхронные ответы из последних тем ядра ──

    @property
    def online(self) -> bool:
        return bool(self.client.connected and self.client.enabled)

    @property
    def module_settings(self) -> dict:
        """Собственные настройки модуля из дашборда (settings_schema манифеста)."""
        return self.client.module_settings

    def last_user_ts(self) -> float:
        return float(self._presence.get("last_user_ts") or 0.0)

    def is_speaking(self) -> bool:
        return bool(self._presence.get("speaking"))

    def repeats(self, text: str) -> bool:
        """Слишком похоже на недавнюю реплику ассистентки."""
        from memory_store import stems
        new = stems(text)
        if len(new) < 3:
            return False
        for old in (stems(t) for t in self._recent):
            if old and len(new & old) / min(len(new), len(old)) >= 0.5:
                return True
        return False

    async def _on_presence(self, data: dict) -> None:
        self._presence = dict(data)
        said = str(data.get("last_say") or "").strip()
        if said and said != self._last_say_seen:
            self._last_say_seen = said
            self._recent.append(said)

    async def _on_settings(self, data: dict) -> None:
        await asyncio.to_thread(self.apply_settings)

    # ── общие настройки ядра -> settings.json модуля ──

    def apply_settings(self) -> bool:
        """Записать общие настройки ядра в файл настроек модуля, если они изменились. True, если что-то записано."""
        shared = {k: v for k, v in dict(self.client.settings).items() if k in settings.DEFAULTS}
        if shared == self._applied:
            return False
        self._applied = dict(shared)
        if shared and any(settings.load().get(k) != v for k, v in shared.items()):
            settings.update(**shared)           # общие настройки живут в ядре
            return True
        return False

    async def _settings_loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.apply_settings)
            except Exception as e:
                logger.debug(f"Сверка настроек: {e}")
            await asyncio.sleep(SETTINGS_SYNC_S)

    # ── исходящие ──

    async def say(self, text: str, emotion: str | None = None) -> None:
        if not self.online:
            logger.info(f"[ядро недоступно, реплика не озвучена] {text[:120]}")
            return
        try:
            await self.client.say(text, mode="verbatim", emotion=emotion, key="rimworld.comment",
                                  expires_in_s=SAY_EXPIRES_S)
        except Exception as e:
            logger.info(f"say не дошёл: {e}")

    async def announce(self, text: str) -> bool:
        """Объявить пользователю действие модуля (REC-7: запуск игры): факт для персонажа, срочность 3. Ядро недоступно:
        False (вызывающий пробует другой путь)."""
        if not self.online:
            return False
        try:
            await self.client.say(text, mode="fact", urgency_hint=3, announce=True)   # всегда звучит (ядро: AN-1)
            return True
        except Exception as e:
            logger.info(f"объявление не дошло: {e}")
            return False

    async def emotion(self, event: str, emotion: str) -> None:
        try:
            await self.client.emotion(emotion, cause=event)
        except Exception as e:
            logger.debug(f"emotion не дошла: {e}")

    async def event(self, kind: str, data: dict | None = None, text: str | None = None,
                    urgency: int | None = None) -> None:
        """Событие игры для ядра (RW-9). `kind` короткий (`raid`, `death`, `run_ended`...) или полный (`rimworld.raid`).
        `text` это факт по-русски (не реплика для озвучки); `urgency` заменяет срочность по умолчанию.
        Игровым событиям `sender` не указывается: люди из игры не должны попадать в отношения ядра (правило TG-14)."""
        data = dict(data or {})
        full, default = EVENTS.get(kind) or (kind if kind.startswith("rimworld.") else f"rimworld.{kind}", 1)
        fields: dict = {"data": data, "urgency_hint": default if urgency is None else int(urgency),
                        "addressed_to_me": False}
        if full in EXPIRES_S:
            fields["expires_in_s"] = EXPIRES_S[full]
        try:
            await self.client.event(full, text or _default_text(full, data), **fields)
        except Exception as e:
            logger.info(f"событие {full} не дошло: {e}")
        if full == "rimworld.run_ended":
            self._after_run(data)

    # ── пост-итог в Telegram (RW-12) ──

    def _after_run(self, data: dict) -> None:
        prompt = str(data.get("prompt") or "").strip()
        if not prompt or not self.online or not self.client.settings.get("telegram_after_run"):
            return
        task = asyncio.get_running_loop().create_task(self._post_run_summary(prompt))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _post_run_summary(self, prompt: str) -> bool:
        """Попросить ядро вызвать telegram.post с темой из итога забега (публикует модуль Telegram, разрешения и текст
        поста его). Сбой только в журнал: забегу он не мешает."""
        try:
            await self.client.request("core.tool.call", {"tool": "telegram.post", "args": {"topic": prompt}},
                                      timeout=POST_TIMEOUT_S)
            return True
        except (ToolError, asyncio.TimeoutError) as e:
            events_log.log("telegram", f"пост-итог после забега не опубликован: {getattr(e, 'message', None) or e!r}")
            return False

    # ── жизнь ──

    async def run(self) -> int:
        """Подключение к ядру и обслуживание до shutdown. Возвращает код выхода процесса (0, или 2 при отказе ядра)."""
        sync = asyncio.get_running_loop().create_task(self._settings_loop())
        try:
            return await self.client.run()
        finally:
            sync.cancel()
            for task in list(self._background):
                task.cancel()


def _default_text(kind: str, data: dict) -> str:
    """Факт для события, у которого вызывающий не передал текст."""
    if kind == "rimworld.colonists":
        pawns = data.get("pawns") or []
        return f"Колония «{data.get('colony') or 'колония'}»: колонистов {len(pawns)}."
    return kind

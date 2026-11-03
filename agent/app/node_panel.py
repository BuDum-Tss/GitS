"""Панель узла рабочего места (ТЗ 33, `docs/WORKSPACE_MODULES.md`, разделы 4.4 и 4.5).

Пока агент в среде модуля, у которого объявлено дерево узлов (`nodes`) и модуль сообщил текущий узел (`state.node`), ядро
вызывает запросы из `nodes[].context` и кладёт результат в контекст агента блоком «Рабочее место». Модель не вызывается: факты
берёт запрос модуля, ядро только укладывает их в бюджет и показывает изменения (`on_change`: целиком при входе в узел и когда
ответ изменился, иначе строка «без изменений»). Выводы из чисел делает сам агент.

`refresh()` асинхронный (запросы к модулю, тайм-аут `TIMEOUT_S`), `block()` синхронный и отдаёт результат последнего `refresh()`:
контекст собирается синхронно, поэтому агент зовёт `refresh()` перед сборкой запроса к себе.
"""
import asyncio
import hashlib
import logging
import time
from typing import Awaitable, Callable

logger = logging.getLogger("node_panel")

TIMEOUT_S = 1.5
TTL_S = 4.0                 # свежее этого повторно не запрашивается
TOTAL_MAX = 1500            # на весь блок узла, символов
DEFAULT_MODE = "on_change"
DEFAULT_CHARS = 400


class NodePanel:
    def __init__(self, *, env_of: Callable[[], str | None], manifest_of: Callable[[str], dict | None],
                 node_of: Callable[[str], str | None], run_tool: Callable[[str, dict], Awaitable[tuple[bool, str, str | None]]],
                 clock: Callable[[], float] = time.monotonic, timeout: float = TIMEOUT_S):
        """env_of() - текущая среда (id модуля или assistant); manifest_of(id) - манифест модуля; node_of(id) - узел, который
        сообщил модуль; run_tool(tool_id, args) - `Router.run_tool_ex`."""
        self._env_of, self._manifest_of, self._node_of, self._run = env_of, manifest_of, node_of, run_tool
        self._clock, self._timeout = clock, timeout
        self._key: tuple[str, str] | None = None            # (модуль, узел) последнего refresh
        self._at = -1e9
        self._hash: dict[tuple[str, str, str], str] = {}    # (модуль, узел, инструмент) -> хэш последнего показанного ответа
        self._text = ""

    def target(self) -> tuple[str, dict] | None:
        """(id модуля, описание узла) для текущей среды или None: среда не модуль, узлов нет или узел неизвестен."""
        module = self._env_of()
        manifest = self._manifest_of(module) if module else None
        node = self._node_of(module) if manifest else None
        if not node:
            return None
        for n in manifest.get("nodes") or []:
            if isinstance(n, dict) and n.get("path") == node and n.get("context"):
                return module, n
        return None

    async def refresh(self) -> None:
        """Обновить блок: запросы панели текущего узла. Сбой одного запроса блок не ломает."""
        target = self.target()
        if target is None:
            self._key, self._text = None, ""
            return
        module, node = target
        key = (module, node["path"])
        now = self._clock()
        if key == self._key and now - self._at < TTL_S:
            return
        entered = key != self._key
        self._key, self._at = key, now
        lines: list[str] = []
        for item in node.get("context") or []:
            tool, mode = item.get("tool"), item.get("mode", DEFAULT_MODE)
            limit = int(item.get("max_chars", DEFAULT_CHARS))
            try:
                ok, text, _code = await asyncio.wait_for(self._run(f"{module}.{tool}", dict(item.get("args") or {})), self._timeout)
            except Exception as e:                       # модуль не ответил: панель без этой строки
                logger.info(f"Панель узла {node['path']}: {tool}: {type(e).__name__}")
                continue
            if not ok or not str(text).strip():
                continue
            text = " ".join(str(text).split())[:limit]
            digest = hashlib.sha1(text.encode("utf-8")).hexdigest()
            slot = (module, node["path"], tool)
            changed = self._hash.get(slot) != digest
            self._hash[slot] = digest
            if mode == "on_enter" and not entered:
                continue
            if mode == "on_change" and not (changed or entered):
                lines.append(f"- {tool}: без изменений")
                continue
            lines.append(f"- {tool}: {text}")
        title = node.get("title") or node["path"]
        self._text = (f"Рабочее место: {module}/{node['path']} ({title})\n" + "\n".join(lines))[:TOTAL_MAX] if lines else ""

    def block(self) -> str:
        return self._text

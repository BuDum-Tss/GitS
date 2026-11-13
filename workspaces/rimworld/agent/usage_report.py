"""Расход Claude игровым агентом -> «батарейка» ядра ГЛаДОС (запрос `core.claude_usage`, ТЗ 11, RW-15).

Токены считает ядро (claude_battery): у подписки один лимит на всех. Отправка не ждёт ответа и не стоит на критическом пути
хода: запрос уходит в цикл событий модуля отдельной задачей. Нет связи с ядром: расход просто не учитывается.
`push` можно звать из любого потока (claude_quota.note вызывается и из потоков), вызывающий код не меняется.
Поля те же, что были у HTTP-версии: `tokens`, `source` (теперь с префиксом `rimworld.`), `limit_text` (текст исчерпанного
лимита: ядро вызывает claude_quota.note, как нынешний /api/claude_usage)."""
import asyncio
import logging

from gits_sdk import ToolError

logger = logging.getLogger("usage_report")

SOURCE_PREFIX = "rimworld."
SEND_TIMEOUT_S = 10.0

_client = None      # ModuleClient
_loop: asyncio.AbstractEventLoop | None = None


def attach(client, loop: asyncio.AbstractEventLoop) -> None:
    """Подключить клиента модуля (main.py, когда цикл событий уже запущен)."""
    global _client, _loop
    _client, _loop = client, loop


def source_name(source: str = "game") -> str:
    """`turn` -> `rimworld.turn`; уже с префиксом остаётся как есть."""
    source = (source or "game").strip()
    return source if source.startswith(SOURCE_PREFIX) else SOURCE_PREFIX + source


def body_of(tokens: int = 0, source: str = "game", limit_text: str = "") -> dict:
    body = {"tokens": max(0, int(tokens)), "source": source_name(source)}
    if limit_text:
        body["limit_text"] = str(limit_text)
    return body


async def _send(body: dict) -> None:
    try:
        await _client.request("core.claude_usage", body, timeout=SEND_TIMEOUT_S)
    except (ToolError, asyncio.TimeoutError) as e:
        logger.debug(f"расход Claude не учтён: {getattr(e, 'message', None) or e!r}")
    except Exception as e:
        logger.debug(f"расход Claude не учтён: {e!r}")


def push(tokens: int = 0, source: str = "game", limit_text: str = ""):
    if tokens <= 0 and not limit_text:
        return
    client, loop = _client, _loop
    if client is None or loop is None or loop.is_closed():
        return
    try:
        asyncio.run_coroutine_threadsafe(_send(body_of(tokens, source, limit_text)), loop)
    except RuntimeError:
        pass                                              # цикл закрывается: модуль останавливается


def push_result(message, source: str = "game"):
    """По ResultMessage Claude Agent SDK: вход, выход и запись в кэш (чтение кэша не считается)."""
    u = getattr(message, "usage", None) or {}
    push(sum(int(u.get(k) or 0) for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens")), source)

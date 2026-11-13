"""Интерфейс управления workspace (контракт 1.11): аренда общего окружения (`ownership: shared`).

HTTP, не WebSocket: сообщения `Message` для него не подходят, поэтому описан отдельным списком `ENDPOINTS`. Его читают сервер
и клиент аренды (`gits_sdk.control`) и генератор `docs/MODULE_CONTRACT.md` (раздел «Интерфейс управления») вместе со схемами
`docs/contract/control.<имя>.json`. Авторизация: заголовок `Authorization: Bearer <GITS_CONTROL_TOKEN>` у каждого запроса.
"""
from __future__ import annotations

from dataclasses import dataclass

from .spec import Field, schema_of

STATE_OFFLINE = "offline"      # окружение недоступно (игра не запущена и т. п.)
STATE_FREE = "free"            # доступно, хозяина нет
STATE_BUSY = "busy"            # занято хозяином (одна аренда за раз)
STATES = (STATE_OFFLINE, STATE_FREE, STATE_BUSY)

F = Field

HOLDER_FIELDS = (
    F("character", "string", True, "персонаж-хозяин", nonempty=True),
    F("core_url", "string", True, "адрес ядра хозяина, к которому подключён workspace (ws:// или wss://)", nonempty=True),
    F("since", "number", True, "когда аренда началась, unix-время в секундах"),
    F("purpose", "string", False, "зачем взято (например `play`, `montage`); пусто, если не указано"),
    F("releasing", "boolean", False, "агент вышел, но у workspace ещё есть работа: аренда держится до её конца (только при true)"),
    F("jobs", "integer", False, "сколько активных задач осталось (есть при `releasing`)"),
)


@dataclass(frozen=True)
class Endpoint:
    name: str
    method: str
    path: str
    doc: str
    request: tuple[Field, ...] = ()
    response: tuple[Field, ...] = ()
    errors: tuple[tuple[int, str], ...] = ()
    example_request: dict | None = None
    example_response: dict | None = None
    note: str = ""
    since: str = "1.11"

    def request_schema(self) -> dict:
        return schema_of(self.request, title=f"{self.method} {self.path}", description=self.doc)

    def response_schema(self) -> dict:
        return schema_of(self.response, title=f"{self.method} {self.path} (ответ 200)")


_UNAUTH = (401, "нет или неверен токен управления: `{\"ok\": false, \"error\": \"unauthorized\"}`")
_BAD = (400, "тело неверно: `{\"ok\": false, \"error\": \"bad_request\", \"message\": ...}`")

STATUS = Endpoint(
    "status", "GET", "/status",
    "Состояние окружения. Допустим у любого участника с интерфейсом управления: у workspace с `ownership: owned` и у service "
    "аренды нет, `state` равен `free` или `offline`, `holder` пуст, `active` считает текущие подключения.",
    response=(F("id", "string", True, "id участника"),
              F("role", "string", True, "роль", enum=("workspace", "service")),
              F("ownership", "string", False, "режим владения (у workspace)", enum=("shared", "owned")),
              F("state", "string", True, "состояние", enum=STATES),
              F("holder", "object", True, "хозяин аренды (поля: " + ", ".join(f.name for f in HOLDER_FIELDS) +
                ") или null, если свободно"),
              F("active", "integer", True, "сколько хозяев или подключений сейчас (для shared 0 или 1)")),
    errors=(_UNAUTH,),
    example_response={"id": "rimworld", "role": "workspace", "ownership": "shared", "state": "busy",
                      "holder": {"character": "glados", "core_url": "ws://127.0.0.1:8766/module/ws", "since": 1760000000.0,
                                 "purpose": "play"}, "active": 1},
)

INFO = Endpoint(
    "info", "GET", "/info",
    "Сведения о workspace до аренды: для чего он нужен и когда в него переходить. Источник: манифест (поле `workspace`, описания "
    "инструментов). Допустим у любого участника с интерфейсом управления; агент читает его, пока workspace не подключён к ядру.",
    response=(F("id", "string", True, "id участника"),
              F("role", "string", True, "роль", enum=("workspace", "service")),
              F("title", "string", True, "название"),
              F("purpose", "string", False, "для чего нужен"),
              F("when_to_use", "string", False, "когда в него переходить"),
              F("keywords", "array", False, "слова, по которым его стоит вспомнить"),
              F("entry", "array", False, "что включает среду (`user_message`, `self_impulse`, `critical`)"),
              F("interruptible", "boolean", False, "можно ли выйти в любой момент"),
              F("safe_points", "array", False, "когда выход безопасен"),
              F("done_when", "string", False, "когда дело считается законченным"),
              F("tools", "array", True, "инструменты: `[{name, description}]`"),
              F("state", "string", True, "состояние аренды", enum=STATES),
              F("holder", "object", True, "хозяин аренды или null")),
    errors=(_UNAUTH,),
    example_response={"id": "montage", "role": "workspace", "title": "Монтаж", "purpose": "монтаж роликов",
                      "when_to_use": "когда нужно смонтировать ролик из файлов", "keywords": ["монтаж", "ролик"],
                      "entry": ["user_message"], "interruptible": True, "safe_points": ["между стадиями"],
                      "done_when": "задачи готовы", "tools": [{"name": "start", "description": "смонтировать видео"}],
                      "state": "free", "holder": None},
)

ACQUIRE = Endpoint(
    "acquire", "POST", "/acquire",
    "Взять окружение в аренду. При успехе workspace вызывает обработчик `on_acquire(lease)` и подключается к ядру хозяина "
    "(`core_url`, `token` из тела). Одна аренда за раз, срока нет. Повторный запрос того же `character` с тем же `purpose` "
    "идемпотентен (тот же `lease_id`); если `core_url` или `token` изменились, прежняя аренда закрывается и открывается новая.",
    request=(F("character", "string", True, "персонаж-хозяин", nonempty=True),
             F("core_url", "string", True, "адрес ядра хозяина", nonempty=True),
             F("token", "string", True, "токен подключения к ядру хозяина (в журнал не пишется, в `holder` не отдаётся)", nonempty=True),
             F("purpose", "string", False, "зачем взято")),
    response=(F("ok", "boolean", True, "true"), F("lease_id", "string", True, "id аренды для release")),
    errors=(_BAD, _UNAUTH,
            (404, "у этого участника нет аренды (`ownership` не `shared`)"),
            (409, "занято другим: `{\"ok\": false, \"state\": \"busy\", \"holder\": {...}}`"),
            (502, "`on_acquire` не смог подключиться: `{\"ok\": false, \"error\": \"acquire_failed\", \"message\": ...}`, аренда не выдана"),
            (503, "окружение недоступно: `{\"ok\": false, \"state\": \"offline\"}`")),
    example_request={"character": "glados", "core_url": "ws://127.0.0.1:8766/module/ws", "token": "<токен ядра>",
                     "purpose": "play"},
    example_response={"ok": True, "lease_id": "l-0123456789ab"},
)

RELEASE = Endpoint(
    "release", "POST", "/release",
    "Вернуть окружение: workspace вызывает `on_release(lease)`, отключается от ядра хозяина и забывает его. Так же аренда "
    "кончается при обрыве WebSocket участника. Идемпотентно: "
    "если аренды уже нет, ответ 200 с `released: false`.",
    request=(F("lease_id", "string", True, "id аренды", nonempty=True),),
    response=(F("ok", "boolean", True, "true"), F("released", "boolean", True, "была ли аренда закрыта этим вызовом"),
              F("pending", "string", False, "`jobs`: аренда не закрыта, у workspace есть незаконченные задачи (`holder.releasing`); "
                "она закроется сама, когда они кончатся")),
    errors=(_BAD, _UNAUTH),
    example_request={"lease_id": "l-0123456789ab"},
    example_response={"ok": True, "released": True},
)

ENDPOINTS = (STATUS, INFO, ACQUIRE, RELEASE)

RULES = (
    "Выход с незаконченной работой: `on_release` может вернуть `False`, тогда `POST /release` отвечает "
    "`{\"ok\": true, \"released\": false, \"pending\": \"jobs\"}`, аренда остаётся (`state: busy`, `holder.releasing: true`, "
    "`holder.jobs`), другой `character` получает 409 с этим `holder`, тот же `character` возвращает аренду запросом `acquire` "
    "(тот же `lease_id`, `releasing` снимается). Когда работа кончилась, workspace сам закрывает аренду (`finish_release`: "
    "`on_release`, `state: free`). Связь с ядром хозяина в это время остаётся. Обрыв WebSocket закрывает аренду сразу.",
    "У аренды нет срока и нет продления: она держится, пока хозяин не вызовет `release` или пока не оборвётся WebSocket участника "
    "с ядром хозяина (ядро упало или отключило участника): тогда workspace сам отпускает аренду и вызывает `on_release`.",
    "Один хозяин за раз; другой `character` получает 409 с описанием хозяина (без токена).",
    "Состояние аренды хранит сам workspace; ядро его не дублирует и читает запросом `GET /status`.",
    "`GET /info` описывает workspace до аренды и до подключения к ядру: для чего он и когда в него переходить (из манифеста: поле `workspace`).",
    "Сервер управления не стартует без токена управления (`GITS_CONTROL_TOKEN`); токен общий для всех вызывающих.",
    "Токен ядра хозяина из `acquire` живёт только в памяти workspace на время аренды: ни в журналы, ни в `status` он не попадает.",
    "Клиентская часть для ядра: `gits_sdk.control.LeaseClient(base_url, token)` с методами `status`, `info`, `acquire`, `release`.",
)

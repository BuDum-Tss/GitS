"""Интерфейс управления workspace (контракт 1.11): аренда общего окружения (`ownership: shared`).

HTTP, не WebSocket: сообщения `Message` для него не подходят, поэтому описан отдельным списком `ENDPOINTS`. Его читают сервер
и клиент аренды (`gits_sdk.control`) и генератор `docs/MODULE_CONTRACT.md` (раздел «Интерфейс управления») вместе со схемами
`docs/contract/control.<имя>.json`. Авторизация: заголовок `Authorization: Bearer <GITS_CONTROL_TOKEN>` у каждого запроса.
"""
from __future__ import annotations

from dataclasses import dataclass

from .spec import Field, schema_of

LEASE_TTL_DEFAULT_S = 300
LEASE_TTL_MAX_S = 3600

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
    F("expires", "number", True, "когда аренда истекает, unix-время в секундах"),
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
              F("ttl_s", "number", True, f"срок аренды по умолчанию, секунд ({LEASE_TTL_DEFAULT_S}; наибольший {LEASE_TTL_MAX_S})"),
              F("active", "integer", True, "сколько хозяев или подключений сейчас (для shared 0 или 1)")),
    errors=(_UNAUTH,),
    example_response={"id": "rimworld", "role": "workspace", "ownership": "shared", "state": "busy",
                      "holder": {"character": "glados", "core_url": "ws://127.0.0.1:8766/module/ws", "since": 1760000000.0,
                                 "purpose": "play", "expires": 1760000300.0},
                      "ttl_s": LEASE_TTL_DEFAULT_S, "active": 1},
)

ACQUIRE = Endpoint(
    "acquire", "POST", "/acquire",
    "Взять окружение в аренду. При успехе workspace вызывает обработчик `on_acquire(lease)` и подключается к ядру хозяина "
    "(`core_url`, `token` из тела). Одна аренда за раз. Повторный запрос того же `character` с тем же `purpose` продлевает "
    "аренду (идемпотентно: тот же `lease_id`); если `core_url` или `token` изменились, прежняя аренда закрывается и открывается новая.",
    request=(F("character", "string", True, "персонаж-хозяин", nonempty=True),
             F("core_url", "string", True, "адрес ядра хозяина", nonempty=True),
             F("token", "string", True, "токен подключения к ядру хозяина (в журнал не пишется, в `holder` не отдаётся)", nonempty=True),
             F("purpose", "string", False, "зачем взято"),
             F("ttl_s", "number", False, f"срок аренды, секунд; больше {LEASE_TTL_MAX_S} обрезается", min=1,
               max=LEASE_TTL_MAX_S, default=LEASE_TTL_DEFAULT_S)),
    response=(F("ok", "boolean", True, "true"), F("lease_id", "string", True, "id аренды для renew и release"),
              F("expires", "number", True, "когда истекает, unix-время в секундах"),
              F("ttl_s", "number", True, "выданный срок, секунд")),
    errors=(_BAD, _UNAUTH,
            (404, "у этого участника нет аренды (`ownership` не `shared`)"),
            (409, "занято другим: `{\"ok\": false, \"state\": \"busy\", \"holder\": {...}}`"),
            (502, "`on_acquire` не смог подключиться: `{\"ok\": false, \"error\": \"acquire_failed\", \"message\": ...}`, аренда не выдана"),
            (503, "окружение недоступно: `{\"ok\": false, \"state\": \"offline\"}`")),
    example_request={"character": "glados", "core_url": "ws://127.0.0.1:8766/module/ws", "token": "<токен ядра>",
                     "purpose": "play", "ttl_s": 300},
    example_response={"ok": True, "lease_id": "l-0123456789ab", "expires": 1760000300.0, "ttl_s": 300},
)

RENEW = Endpoint(
    "renew", "POST", "/renew", "Продлить аренду: новый срок отсчитывается от текущего момента.",
    request=(F("lease_id", "string", True, "id аренды", nonempty=True),
             F("ttl_s", "number", False, "новый срок, секунд", min=1, max=LEASE_TTL_MAX_S, default=LEASE_TTL_DEFAULT_S)),
    response=(F("ok", "boolean", True, "true"), F("lease_id", "string", True, "id аренды"),
              F("expires", "number", True, "когда истекает"), F("ttl_s", "number", True, "выданный срок, секунд")),
    errors=(_BAD, _UNAUTH, (404, "аренды нет, она истекла или закрыта: `{\"ok\": false, \"error\": \"unknown_lease\"}`")),
    example_request={"lease_id": "l-0123456789ab", "ttl_s": 300},
    example_response={"ok": True, "lease_id": "l-0123456789ab", "expires": 1760000600.0, "ttl_s": 300},
)

RELEASE = Endpoint(
    "release", "POST", "/release",
    "Вернуть окружение: workspace вызывает `on_release(lease)`, отключается от ядра хозяина и забывает его. Идемпотентно: "
    "если аренды уже нет, ответ 200 с `released: false`.",
    request=(F("lease_id", "string", True, "id аренды", nonempty=True),),
    response=(F("ok", "boolean", True, "true"), F("released", "boolean", True, "была ли аренда закрыта этим вызовом")),
    errors=(_BAD, _UNAUTH),
    example_request={"lease_id": "l-0123456789ab"},
    example_response={"ok": True, "released": True},
)

ENDPOINTS = (STATUS, ACQUIRE, RENEW, RELEASE)

RULES = (
    f"Срок аренды истекает сам: `ttl_s` по умолчанию {LEASE_TTL_DEFAULT_S} с, наибольший {LEASE_TTL_MAX_S} с; истёк срок: то же, "
    "что `release` (вызывается `on_release`). Хозяин продлевает аренду запросом `renew` (например раз в треть срока).",
    "Один хозяин за раз; другой `character` получает 409 с описанием хозяина (без токена).",
    "Сервер управления не стартует без токена управления (`GITS_CONTROL_TOKEN`); токен общий для всех вызывающих.",
    "Токен ядра хозяина из `acquire` живёт только в памяти workspace на время аренды: ни в журналы, ни в `status` он не попадает.",
    "Клиентская часть для ядра: `gits_sdk.control.LeaseClient(base_url, token)` с методами `status`, `acquire`, `renew`, `release`.",
)

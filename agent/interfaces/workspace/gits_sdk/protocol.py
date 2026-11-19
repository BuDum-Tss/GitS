"""Протокол «ядро ↔ модуль» (ТЗ 01, docs/MODULE_CONTRACT.md): константы, конверт, манифест, проверки.

Единственный источник правды о форме сообщений. Этот файл импортируют и SDK модуля, и ядро GitS (не копируют),
поэтому он зависит только от стандартной библиотеки и ничего не знает ни о сети, ни о настройках ядра.

Проверки (`validate_*`) возвращают список проблем по-русски; пустой список = всё верно. Классы (`Manifest`, `Event`,
`Say`, `JobUpdate`) собираются из словарей через `from_dict` / `from_body` и бросают `ProtocolError` с перечнем проблем.
"""
from __future__ import annotations

import json
import math
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from .envcompat import env

# ---------------------------------------------------------------- транспорт (MOD-1…MOD-7)

PROTOCOL = 3
WS_PATH = "/ws"                        # единственный адрес входа
DEFAULT_CORE_URL = "ws://127.0.0.1:8766" + WS_PATH
ENV_CORE_URL = "GITS_CORE_URL"
ENV_TOKEN = "GITS_TOKEN"               # токен подключения к ядру
ENV_ID = "GITS_ID"                     # id участника
ENV_CONTROL_TOKEN = "GITS_CONTROL_TOKEN"   # общий токен интерфейса управления (аренда workspace, `gits_sdk.control`)
ENV_CONTAINER = "GITS_CONTAINER"       # 1: ядро работает в Docker-контейнере (том персонажа, токен из переменной, без трея)
ENV_HUB_ALLOW = "GITS_HUB_ALLOW"       # сети CIDR через запятую, которым шина модулей открыта помимо петли (контейнер: шлюз Docker)
MAX_MESSAGE_BYTES = 1024 * 1024        # MOD-6: файлы передаются путями, не внутри сообщений
HEARTBEAT_S = 10                       # MOD-5: встроенный пинг WebSocket (aiohttp heartbeat)
PEER_TIMEOUT_S = 30                    # нет ответа столько секунд: соединение разорвано
RECONNECT_DELAYS = (1, 2, 4, 8, 16, 30)  # MOD-7: дальше без конца по 30 с
CLOSE_REPLACED = 4000                  # MOD-13: код закрытия «вытеснен новым подключением»
EXIT_REJECTED = 2                      # MOD-12: код завершения процесса модуля после reject про протокол/манифест
CORE_HOST_ALLOWED = ("127.0.0.1", "::1")   # MOD-3: ядро принимает только петлю


def is_container(environ=None) -> bool:
    """Ядро запущено в контейнере (`GITS_CONTAINER=1`)."""
    value = (os.environ if environ is None else environ).get(ENV_CONTAINER, "")
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def env_token() -> str | None:
    """Токен подключения из окружения (`GITS_TOKEN`)."""
    return os.environ.get(ENV_TOKEN) or None


def env_id() -> str | None:
    """id участника из окружения (`GITS_ID`)."""
    return os.environ.get(ENV_ID) or None


def core_url() -> str:
    """Адрес ядра: GITS_CORE_URL или стандартный."""
    return env(ENV_CORE_URL) or DEFAULT_CORE_URL


def _local_root() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    return Path(base) if base else Path.home() / ".local" / "share"


def token_path() -> Path:
    """Файл токена модулей (MOD-4, ТЗ 05 CHR-3): %LOCALAPPDATA%\\GitS\\module_token. Его пишет ядро."""
    return _local_root() / "GitS" / "module_token"


def legacy_token_path() -> Path:
    """Прежний путь файла токена (до переименования в GitS): читается как запасной."""
    return _local_root() / "GLaDOS" / "module_token"


def new_token() -> str:
    """Новый токен ядра: 32 случайных байта в hex (ядро делает это при каждом старте)."""
    return secrets.token_hex(32)


def write_token_file(token: str, path: Path | None = None) -> Path:
    """Записать токен в файл (для ядра); каталог создаётся. Токен нигде не печатается."""
    path = path or token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token, encoding="utf-8")
    return path


def read_token_file(path: Path | None = None) -> str | None:
    """Прочитать токен из файла; None, если файла нет или он пуст. Без пути: новый файл, затем прежний (запасной)."""
    candidates = [path] if path is not None else [token_path(), legacy_token_path()]
    for candidate in candidates:
        try:
            token = candidate.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if token:
            return token
    return None


def reconnect_delay(attempt: int) -> float:
    """Задержка перед попыткой номер attempt (с нуля): 1, 2, 4, 8, 16, 30, 30, ..."""
    return float(RECONNECT_DELAYS[min(max(attempt, 0), len(RECONNECT_DELAYS) - 1)])


# ---------------------------------------------------------------- коды ошибок (MOD-9)

ERROR_CODES = ("bad_request", "unknown", "denied", "unavailable", "timeout", "cancelled", "internal",
               "busy", "invalid")   # busy и invalid: контракт 3.2, инструменты workspace (операция занята, неверные аргументы)


class ProtocolError(ValueError):
    """Нарушение формата сообщения или манифеста; `problems` перечисляет все найденные проблемы."""

    def __init__(self, message: str, problems: list[str] | None = None):
        super().__init__(message)
        self.problems = list(problems or [message])


class ManifestError(ProtocolError):
    """Манифест не прошёл проверку."""


class ToolError(Exception):
    """Ожидаемый отказ (понятная причина, а не сбой кода): уходит в `result` как `ok: false`.

    Бросается обработчиками инструментов и клиентом (`request`, `ask`) при `ok: false` от ядра.
    `code` один из ERROR_CODES (MOD-9); неизвестный код заменяется на `internal`.
    """

    def __init__(self, message: str = "", code: str = "unavailable"):
        super().__init__(message)
        self.message = message
        self.code = code if code in ERROR_CODES else "internal"


# ---------------------------------------------------------------- типы сообщений

# модуль -> ядро
MODULE_TO_CORE_NOTES = ("hello", "event", "say", "emotion", "state", "job.update", "result", "panel.set",
                        "log")                 # log: ТЗ 31, запись в журнал ядра вместо прямого events_log
CORE_REQUESTS = (
    "core.ask", "core.tts", "core.tool.call", "core.memory.recall", "core.memory.remember",
    "core.persona", "core.privacy_terms",
    "core.web_search",
    # ТЗ 31 (контракт 1.1): быстрая модель, чтение журнала
    "core.llm.ask", "core.events.tail",
    # ТЗ 27 (контракт 1.2): эпизоды памяти
    "core.memory.episodes",
    # ТЗ 32 (контракт 1.9): цель и задачи среды
    "core.tasks",
)
MODULE_TO_CORE = frozenset(MODULE_TO_CORE_NOTES) | frozenset(CORE_REQUESTS)

# ядро -> модуль
CORE_TO_MODULE_REQUESTS = ("tool.call", "tool.check", "job.cancel", "job.list", "graph.get", "deliver",
                           "tts.synthesize", "panel.action",
                           # среды агента (ТЗ 32, WS-4)
                           "workspace.enter", "workspace.exit", "workspace.afk", "workspace.resume")
CORE_TO_MODULE_NOTES = ("welcome", "reject", "job.answer", "speak", "speak.stop", "topic", "shutdown", "result")
CORE_TO_MODULE = frozenset(CORE_TO_MODULE_REQUESTS) | frozenset(CORE_TO_MODULE_NOTES)

REQUEST_TYPES = frozenset(CORE_REQUESTS) | frozenset(CORE_TO_MODULE_REQUESTS)


def is_request(msg_type: str) -> bool:
    """Ждёт ли сообщение такого типа ответа `result` (MOD-8)."""
    return msg_type in REQUEST_TYPES


TOPICS = ("presence", "settings", "profile", "module", "character")   # character: ТЗ 05, CHR-6
CAPABILITIES = ("voice.output", "voice.input", "presence.view", "screen.observe", "chat.channel", "game")
RISKS = ("auto", "ask", "public")
AVAILABILITIES = ("always", "profile")
PARAM_TYPES = ("string", "integer", "number", "boolean", "array", "object")
SETTING_TYPES = ("boolean", "integer", "number", "string")
CORE_FILLS = ("context",)                  # что ядро умеет заполнять само (MOD-15, поле core_fills)
SAY_MODES = ("fact", "verbatim")
ASK_KINDS = ("approval", "choice", "text")
JOB_STATES = ("queued", "running", "waiting_user", "done", "failed", "cancelled")
JOB_FINISHED = ("done", "failed", "cancelled")

# ---------------------------------------------------------------- ограничения

# роли участника шины: workspace (окружение с инструментами, событиями и рабочим местом) и service
ROLE_WORKSPACE = "workspace"
ROLE_SERVICE = "service"
ROLES = (ROLE_WORKSPACE, ROLE_SERVICE)
# режим владения workspace: shared (общий, эксклюзивный, берётся в аренду) и owned (живёт внутри ядра персонажа)
OWNERSHIP_SHARED = "shared"
OWNERSHIP_OWNED = "owned"
OWNERSHIPS = (OWNERSHIP_SHARED, OWNERSHIP_OWNED)


def role_of(manifest: Any) -> str:
    """Роль по манифесту (`role` обязательно); для неверного манифеста пустая строка."""
    if isinstance(manifest, dict) and isinstance(manifest.get("role"), str):
        return manifest["role"]
    return ""


def ownership_of(manifest: Any) -> str | None:
    """Режим владения workspace (`shared` или `owned`); у service None."""
    if role_of(manifest) != ROLE_WORKSPACE:
        return None
    return manifest.get("ownership") if isinstance(manifest, dict) else None


PEER_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,47}$")
SETTING_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
PARAM_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
JOB_ID_RE = re.compile(r"^j-[0-9a-f]{8}$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
KIND_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}\.[a-z0-9_][a-z0-9_.]*$")
WORKSPACE_ENTRIES = ("user_message", "self_impulse", "schedule", "critical", "fallback")   # ТЗ 32, 4.1: как можно войти в среду
WORKSPACE_ENTRY_DEFAULT = ("user_message", "self_impulse", "critical")

DESCRIPTION_MAX = 300
EVENT_TEXT_MAX = 1000
STATE_SUMMARY_MAX = 300
TIMEOUT_TOOL_MIN, TIMEOUT_TOOL_MAX, TIMEOUT_TOOL_DEFAULT = 1, 120, 30
ASK_TIMEOUT_DEFAULT, ASK_TIMEOUT_MAX = 300, 86400
URGENCY_MIN, URGENCY_MAX = 0, 5
# ТЗ 33: рабочее место (дерево узлов), контекст узла и сдвиг срочности событий по теме
NODE_PATH_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}(/[a-z0-9_]{1,32}){0,3}$")      # «architect/zone»: глубина до 4
NODE_MAX = 64
NODE_CONTEXT_MAX = 6
NODE_CONTEXT_MODES = ("on_enter", "on_change", "every_turn")
NODE_CONTEXT_MODE_DEFAULT = "on_change"
NODE_CONTEXT_CHARS_MIN, NODE_CONTEXT_CHARS_MAX, NODE_CONTEXT_CHARS_DEFAULT = 50, 2000, 400
WATCH_SHIFT_MIN, WATCH_SHIFT_MAX = -2, 2
TOPIC_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")

# пространство модуля в дашборде (TZ 15, поле panel и сообщение panel.set)
PANEL_TITLE_MAX, PANEL_ICON_MAX = 60, 40
PANEL_ORDER_DEFAULT = 100
PANEL_LINKS_MAX, PANEL_ACTIONS_MAX, PANEL_HOSTS_MAX, PANEL_DIRS_MAX = 12, 40, 10, 10
PANEL_LINK_KINDS = ("url", "tool", "reveal_path")
PANEL_ARGS_FROM = ("job",)
PANEL_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")          # id ссылок, действий и вкладок
PANEL_BLOCK_TYPES = ("stats", "table", "list", "text", "progress", "buttons", "link", "image", "video", "log", "tabs",
                     "input", "cards", "split")
PANEL_BUTTON_STYLES = ("default", "primary", "danger")
PANEL_SET_MAX_BYTES = 64 * 1024              # panel.set: тело целиком (ТЗ 15, раздел 3)
PANEL_SET_MIN_INTERVAL_S = 1.0               # не чаще раза в секунду на модуль (ядро отбрасывает лишнее)
PANEL_BLOCKS_MAX, PANEL_ITEMS_MAX, PANEL_ROWS_MAX, PANEL_LINES_MAX = 40, 50, 200, 200
PANEL_TABS_MAX, PANEL_DEPTH_MAX = 6, 2       # вложенность блоков «tabs»: не глубже двух уровней
PANEL_TEXT_MAX, PANEL_CELL_MAX, PANEL_LINE_MAX = 4000, 500, 500
PANEL_LABEL_MAX = 80
PANEL_INPUT_VALUE_MAX = 4096                 # значение поля ввода (блок input): уходит действием как args.value
PANEL_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")      # image.file: только картинки из panel.serve_dirs
PANEL_ACTION_RESULT_MESSAGE_MAX = 300
PANEL_CARDS_MAX = 50                         # карточек в блоке cards и вложенных карточек у одной карточки (children)
PANEL_SELECT_OPTIONS_MAX = 8                 # пунктов выпадающего списка карточки
PANEL_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def new_id() -> str:
    """Id сообщения: `m-` + 16 hex."""
    return "m-" + secrets.token_hex(8)


def new_job_id() -> str:
    """Id задачи (MOD-19): `j-` + 8 hex."""
    return "j-" + secrets.token_hex(4)


def new_instance() -> str:
    """Id процесса модуля для `hello.instance`."""
    return secrets.token_hex(4)


def clip(text: str, limit: int) -> str:
    """Обрезать текст до limit символов, оставив многоточие."""
    return text if len(text) <= limit else text[: max(limit - 1, 0)] + "…"


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_str_list(v: Any) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


# ---------------------------------------------------------------- конверт (раздел 4)

@dataclass
class Envelope:
    """Конверт сообщения: {"v", "type", "id", "re", "ts", "body"}."""

    type: str
    body: dict = field(default_factory=dict)
    id: str = field(default_factory=new_id)
    re: str | None = None
    ts: float = field(default_factory=time.time)
    v: int = PROTOCOL

    def to_dict(self) -> dict:
        return {"v": self.v, "type": self.type, "id": self.id, "re": self.re, "ts": self.ts, "body": self.body}

    def dumps(self) -> str:
        """JSON-текст кадра. Не больше MAX_MESSAGE_BYTES (MOD-6), иначе ProtocolError."""
        try:
            text = json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError) as e:
            raise ProtocolError(f"сообщение «{self.type}» не сериализуется в JSON: {e}") from e
        if len(text.encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise ProtocolError(f"сообщение «{self.type}» больше {MAX_MESSAGE_BYTES} байт")
        return text

    @classmethod
    def from_dict(cls, data: Any) -> Envelope:
        if not isinstance(data, dict):
            raise ProtocolError("сообщение должно быть JSON-объектом")
        problems = []
        if data.get("v") != PROTOCOL or isinstance(data.get("v"), bool):
            problems.append(f"версия протокола {data.get('v')!r} не поддерживается (нужна {PROTOCOL})")
        msg_type = data.get("type")
        if not isinstance(msg_type, str) or not msg_type:
            problems.append("поле type должно быть непустой строкой")
        msg_id = data.get("id")
        if not isinstance(msg_id, str) or not msg_id:
            problems.append("поле id должно быть непустой строкой")
        re_ = data.get("re")
        if re_ is not None and not isinstance(re_, str):
            problems.append("поле re должно быть строкой или null")
        if not _is_num(data.get("ts")):
            problems.append("поле ts должно быть числом")
        body = data.get("body", {})
        if body is None:
            body = {}
        if not isinstance(body, dict):
            problems.append("поле body должно быть объектом")
        if problems:
            raise ProtocolError("; ".join(problems), problems)
        return cls(type=msg_type, body=body, id=msg_id, re=re_, ts=float(data["ts"]), v=data["v"])

    @classmethod
    def loads(cls, raw: str | bytes) -> Envelope:
        if isinstance(raw, (bytes, bytearray)):
            if len(raw) > MAX_MESSAGE_BYTES:
                raise ProtocolError(f"сообщение больше {MAX_MESSAGE_BYTES} байт")
            try:
                raw = bytes(raw).decode("utf-8")
            except UnicodeDecodeError as e:
                raise ProtocolError("кадр не в UTF-8") from e
        elif len(raw.encode("utf-8", errors="replace")) > MAX_MESSAGE_BYTES:
            raise ProtocolError(f"сообщение больше {MAX_MESSAGE_BYTES} байт")
        try:
            data = json.loads(raw)
        except ValueError as e:
            raise ProtocolError(f"кадр не JSON: {e}") from e
        return cls.from_dict(data)


def result_ok(re: str, value: Any = None) -> Envelope:
    """Успешный ответ на запрос `re` (MOD-8)."""
    return Envelope("result", {"ok": True, "value": value}, re=re)


def result_error(re: str, code: str, message: str) -> Envelope:
    """Ответ-ошибка на запрос `re`; код из ERROR_CODES (MOD-9), message по-русски и без секретов."""
    if code not in ERROR_CODES:
        code = "internal"
    return Envelope("result", {"ok": False, "error": {"code": code, "message": message}}, re=re)


def unwrap_result(env: Envelope) -> Any:
    """Достать `value` из `result` или бросить ToolError с кодом ошибки."""
    body = env.body
    if body.get("ok"):
        return body.get("value")
    err = body.get("error") if isinstance(body.get("error"), dict) else {}
    raise ToolError(str(err.get("message") or "ядро вернуло ошибку без пояснения"), code=str(err.get("code") or "internal"))


# ---------------------------------------------------------------- манифест (раздел 6)

def _check_params(where: str, params: Any, problems: list[str]) -> None:
    if not isinstance(params, dict):
        problems.append(f"{where}.params: должен быть объектом (имя параметра -> схема)")
        return
    for pname, spec in params.items():
        w = f"{where}.params.{pname}"
        if not isinstance(pname, str) or not PARAM_NAME_RE.match(pname):
            problems.append(f"{w}: недопустимое имя параметра")
        if not isinstance(spec, dict):
            problems.append(f"{w}: схема параметра должна быть объектом")
            continue
        if spec.get("type") not in PARAM_TYPES:
            problems.append(f"{w}.type: одно из {list(PARAM_TYPES)}")
        if "description" in spec and not isinstance(spec["description"], str):
            problems.append(f"{w}.description: должна быть строкой")
        if "required" in spec and not isinstance(spec["required"], bool):
            problems.append(f"{w}.required: должно быть true или false")
        if "enum" in spec and (not isinstance(spec["enum"], list) or not spec["enum"]):
            problems.append(f"{w}.enum: должен быть непустым списком")


def _check_tool(i: int, tool: Any, problems: list[str], seen: set[str]) -> None:
    where = f"tools[{i}]"
    if not isinstance(tool, dict):
        problems.append(f"{where}: инструмент должен быть объектом")
        return
    name = tool.get("name")
    if not isinstance(name, str) or not TOOL_NAME_RE.match(name):
        problems.append(f"{where}.name: должно соответствовать {TOOL_NAME_RE.pattern}")
    else:
        where = f"tools[{name}]"
        if name in seen:
            problems.append(f"{where}: имя инструмента повторяется")
        seen.add(name)
    desc = tool.get("description")
    if not isinstance(desc, str) or not desc.strip():
        problems.append(f"{where}.description: обязательная непустая строка")
    elif len(desc) > DESCRIPTION_MAX:
        problems.append(f"{where}.description: длиннее {DESCRIPTION_MAX} символов")
    if "params" not in tool:
        problems.append(f"{where}.params: обязательное поле (объект, может быть пустым)")
    else:
        _check_params(where, tool["params"], problems)
    if tool.get("risk") not in RISKS:
        problems.append(f"{where}.risk: одно из {list(RISKS)}")
    if tool.get("availability") not in AVAILABILITIES:
        problems.append(f"{where}.availability: одно из {list(AVAILABILITIES)}")
    for flag in ("informative", "self_confirming", "danger_check", "job", "fallback", "announce", "sends", "read_only"):
        if flag in tool and not isinstance(tool[flag], bool):
            problems.append(f"{where}.{flag}: должно быть true или false")
    if "timeout_s" in tool:
        t = tool["timeout_s"]
        if not _is_int(t) or not TIMEOUT_TOOL_MIN <= t <= TIMEOUT_TOOL_MAX:
            problems.append(f"{where}.timeout_s: целое {TIMEOUT_TOOL_MIN}–{TIMEOUT_TOOL_MAX}")
    if "node" in tool and (not isinstance(tool["node"], str) or not NODE_PATH_RE.match(tool["node"])):
        problems.append(f"{where}.node: путь узла вида «architect/zone» (строчные латинские, глубина до 4)")
    if "keywords" in tool and not _is_str_list(tool["keywords"]):
        problems.append(f"{where}.keywords: должен быть списком строк")
    if "core_fills" in tool:
        fills = tool["core_fills"]
        if not _is_str_list(fills):
            problems.append(f"{where}.core_fills: должен быть списком строк")
        else:
            params = tool.get("params") if isinstance(tool.get("params"), dict) else {}
            for fill in fills:
                if fill not in CORE_FILLS:
                    problems.append(f"{where}.core_fills: поддерживается только {list(CORE_FILLS)}, получено «{fill}»")
                elif fill not in params:
                    problems.append(f"{where}.core_fills: параметр «{fill}» не описан в params")


def _check_setting(name: Any, spec: Any, problems: list[str]) -> None:
    where = f"settings_schema.{name}"
    if not isinstance(name, str) or not SETTING_NAME_RE.match(name):
        problems.append(f"{where}: недопустимое имя настройки")
    if not isinstance(spec, dict):
        problems.append(f"{where}: схема настройки должна быть объектом")
        return
    kind = spec.get("type")
    if kind not in SETTING_TYPES:
        problems.append(f"{where}.type: одно из {list(SETTING_TYPES)}")
    title = spec.get("title")
    if not isinstance(title, str) or not title.strip():
        problems.append(f"{where}.title: обязательная непустая строка")
    if spec.get("secret"):
        problems.append(f"{where}.secret: секретные настройки запрещены, секреты модуль читает сам из .env")
    if "secret" in spec and not isinstance(spec["secret"], bool):
        problems.append(f"{where}.secret: должно быть true или false")
    if "default" not in spec:
        problems.append(f"{where}.default: обязательное поле")
        return
    if kind not in SETTING_TYPES:
        return

    def fits(v: Any) -> bool:
        return {"boolean": isinstance(v, bool), "integer": _is_int(v), "number": _is_num(v),
                "string": isinstance(v, str)}[kind]

    default = spec["default"]
    if not fits(default):
        problems.append(f"{where}.default: не соответствует типу {kind}")
        return
    numeric = kind in ("integer", "number")
    lo, hi = spec.get("min"), spec.get("max")
    for key, bound in (("min", lo), ("max", hi)):
        if key in spec:
            if not numeric:
                problems.append(f"{where}.{key}: только для integer и number")
            elif not _is_num(bound):
                problems.append(f"{where}.{key}: должно быть числом")
    if numeric and _is_num(lo) and _is_num(hi) and lo > hi:
        problems.append(f"{where}: min больше max")
    if numeric and _is_num(lo) and default < lo:
        problems.append(f"{where}.default: меньше min")
    if numeric and _is_num(hi) and default > hi:
        problems.append(f"{where}.default: больше max")
    if "enum" in spec:
        enum = spec["enum"]
        if not isinstance(enum, list) or not enum or not all(fits(x) for x in enum):
            problems.append(f"{where}.enum: непустой список значений типа {kind}")
        elif default not in enum:
            problems.append(f"{where}.default: нет среди enum")


def kind_allowed(kind: str, peer_id: str, extra_prefixes: Any = ()) -> bool:
    """Вид события допустим, если начинается с id модуля или с одного из `event_prefixes` манифеста (окружение, слитое из
    нескольких подсистем, сохраняет прежние виды `twitch.chat`, `obs.scene`)."""
    return any(kind.startswith(p + ".") for p in (peer_id, *(extra_prefixes or ())))


def validate_manifest(manifest: Any, warnings: list[str] | None = None) -> list[str]:
    """Проверить манифест по MOD-15/MOD-16 и разделу 6.1. Возвращает список проблем (пустой = верно).

    Поле `panel` (TZ 15) не отклоняет модуль: его проблемы уходят в `warnings`, если список передан (модуль работает без
    пространства). Без `warnings` проблемы panel не возвращаются вовсе."""
    if not isinstance(manifest, dict):
        return ["манифест должен быть объектом"]
    problems: list[str] = []

    mid = manifest.get("id")
    id_ok = isinstance(mid, str) and bool(PEER_ID_RE.match(mid))
    if not id_ok:
        problems.append(f"id: должен соответствовать {PEER_ID_RE.pattern}")
    title = manifest.get("title")
    if not isinstance(title, str) or not title.strip():
        problems.append("title: обязательная непустая строка")
    desc = manifest.get("description")
    if not isinstance(desc, str) or not desc.strip():
        problems.append("description: обязательная непустая строка")
    elif len(desc) > DESCRIPTION_MAX:
        problems.append(f"description: длиннее {DESCRIPTION_MAX} символов")
    ver = manifest.get("version")
    if not isinstance(ver, str) or not SEMVER_RE.match(ver):
        problems.append("version: строка semver вида 1.2.3")

    for key, allowed in (("capabilities", CAPABILITIES), ("subscribe", TOPICS)):
        if key not in manifest:
            continue
        val = manifest[key]
        if not _is_str_list(val):
            problems.append(f"{key}: должен быть списком строк")
            continue
        for item in val:
            if item not in allowed:
                problems.append(f"{key}: неизвестное значение «{item}», допустимо {list(allowed)}")
    if "shared_settings" in manifest and not _is_str_list(manifest["shared_settings"]):
        problems.append("shared_settings: должен быть списком строк")

    if "settings_schema" in manifest:
        schema = manifest["settings_schema"]
        if not isinstance(schema, dict):
            problems.append("settings_schema: должен быть объектом")
        else:
            for name, spec in schema.items():
                _check_setting(name, spec, problems)

    if "tools" in manifest:
        tools = manifest["tools"]
        if not isinstance(tools, list):
            problems.append("tools: должен быть списком")
        else:
            seen: set[str] = set()
            for i, tool in enumerate(tools):
                _check_tool(i, tool, problems, seen)

    extra_prefixes: list[str] = []
    if "event_prefixes" in manifest:
        raw_prefixes = manifest["event_prefixes"]
        if not _is_str_list(raw_prefixes) or not all(PEER_ID_RE.match(x) for x in raw_prefixes):
            problems.append("event_prefixes: список префиксов видов событий (как id модуля)")
        else:
            extra_prefixes = list(raw_prefixes)
    if "emits" in manifest:
        emits = manifest["emits"]
        if not isinstance(emits, list):
            problems.append("emits: должен быть списком")
        else:
            kinds: set[str] = set()
            for i, emit in enumerate(emits):
                where = f"emits[{i}]"
                if not isinstance(emit, dict):
                    problems.append(f"{where}: должен быть объектом")
                    continue
                kind = emit.get("kind")
                if not isinstance(kind, str) or not KIND_RE.match(kind):
                    problems.append(f"{where}.kind: вид «<модуль>.<что>» из латинских строчных букв")
                else:
                    where = f"emits[{kind}]"
                    if id_ok and not kind_allowed(kind, mid, extra_prefixes):
                        problems.append(f"{where}: kind должен начинаться с «{mid}.»"
                                        + (" или с одного из event_prefixes" if extra_prefixes else ""))
                    if kind in kinds:
                        problems.append(f"{where}: kind повторяется")
                    kinds.add(kind)
                if not isinstance(emit.get("description"), str) or not emit["description"].strip():
                    problems.append(f"{where}.description: обязательная непустая строка")
                urgency = emit.get("default_urgency")
                if not _is_int(urgency) or not URGENCY_MIN <= urgency <= URGENCY_MAX:
                    problems.append(f"{where}.default_urgency: целое {URGENCY_MIN}–{URGENCY_MAX}")
                if "urgency_unfocused" in emit and (not _is_int(emit["urgency_unfocused"])
                                                    or not URGENCY_MIN <= emit["urgency_unfocused"] <= URGENCY_MAX):
                    problems.append(f"{where}.urgency_unfocused: целое {URGENCY_MIN}–{URGENCY_MAX}")
                if "topics" in emit and (not _is_str_list(emit["topics"])
                                         or not all(TOPIC_RE.match(t) for t in emit["topics"])):
                    problems.append(f"{where}.topics: список тем из строчных латинских букв")
                if "follow_up" in emit and not _is_str_list(emit["follow_up"]):
                    problems.append(f"{where}.follow_up: список имён инструментов")

    for flag in ("graph", "state"):
        if flag in manifest and not isinstance(manifest[flag], bool):
            problems.append(f"{flag}: должно быть true или false")
    _check_role(manifest, problems)
    _check_workspace(manifest, problems)
    _check_nodes(manifest, problems)
    if "panel" in manifest and warnings is not None:
        warnings.extend(parse_panel(manifest)[1])
    if warnings is not None and isinstance(manifest.get("workspace"), dict):
        warnings.extend(parse_workspace_spec(manifest["workspace"])[1])
    return problems


def _check_role(manifest: dict, problems: list[str]) -> None:
    """Поля `role` (обязательно: workspace | service) и `ownership` (обязательно у workspace: shared | owned; у service нельзя)."""
    role = manifest.get("role")
    if "role" not in manifest:
        problems.append(f"role: обязательное поле, одно из {list(ROLES)}")
        role = None
    elif not isinstance(role, str) or role not in ROLES:
        problems.append(f"role: одно из {list(ROLES)}")
        role = None
    if role == ROLE_SERVICE:
        if "ownership" in manifest:
            problems.append("ownership: только у workspace")
    elif role == ROLE_WORKSPACE:
        if "ownership" not in manifest:
            problems.append(f"ownership: обязательное поле у workspace, одно из {list(OWNERSHIPS)}")
        elif not isinstance(manifest["ownership"], str) or manifest["ownership"] not in OWNERSHIPS:
            problems.append(f"ownership: одно из {list(OWNERSHIPS)}")
    elif "ownership" in manifest and (not isinstance(manifest["ownership"], str) or manifest["ownership"] not in OWNERSHIPS):
        problems.append(f"ownership: одно из {list(OWNERSHIPS)}")
    if role == ROLE_SERVICE and "workspace" in manifest:
        problems.append("workspace: у service описания среды быть не может")


def _check_nodes(manifest: dict, problems: list[str]) -> None:
    """ТЗ 33: дерево узлов рабочего места (поле `nodes`) и ссылки на него. Необязательно: без него модуль работает как раньше
    (плоский список инструментов, один неявный узел)."""
    tool_names = {t["name"] for t in manifest.get("tools") or [] if isinstance(t, dict) and isinstance(t.get("name"), str)}
    paths: set[str] = set()
    nodes = manifest.get("nodes")
    if nodes is not None:
        if not isinstance(nodes, list):
            problems.append("nodes: должен быть списком")
            nodes = []
        elif len(nodes) > NODE_MAX:
            problems.append(f"nodes: не больше {NODE_MAX} узлов")
        for i, node in enumerate(nodes):
            where = f"nodes[{i}]"
            if not isinstance(node, dict):
                problems.append(f"{where}: должен быть объектом")
                continue
            path = node.get("path")
            if not isinstance(path, str) or not NODE_PATH_RE.match(path):
                problems.append(f"{where}.path: путь вида «architect/zone» (строчные латинские, глубина до 4)")
                continue
            where = f"nodes[{path}]"
            if path in paths:
                problems.append(f"{where}: путь повторяется")
            paths.add(path)
            if not isinstance(node.get("title"), str) or not node["title"].strip():
                problems.append(f"{where}.title: обязательная непустая строка")
            if "tab" in node and not isinstance(node["tab"], str):
                problems.append(f"{where}.tab: строка")
            context = node.get("context", [])
            if not isinstance(context, list) or len(context) > NODE_CONTEXT_MAX:
                problems.append(f"{where}.context: список не длиннее {NODE_CONTEXT_MAX}")
                context = []
            for j, item in enumerate(context):
                cw = f"{where}.context[{j}]"
                if not isinstance(item, dict):
                    problems.append(f"{cw}: должен быть объектом")
                    continue
                if item.get("tool") not in tool_names:
                    problems.append(f"{cw}.tool: имя инструмента-запроса из tools")
                if "args" in item and not isinstance(item["args"], dict):
                    problems.append(f"{cw}.args: должен быть объектом")
                if item.get("mode", NODE_CONTEXT_MODE_DEFAULT) not in NODE_CONTEXT_MODES:
                    problems.append(f"{cw}.mode: одно из {list(NODE_CONTEXT_MODES)}")
                chars = item.get("max_chars", NODE_CONTEXT_CHARS_DEFAULT)
                if not _is_int(chars) or not NODE_CONTEXT_CHARS_MIN <= chars <= NODE_CONTEXT_CHARS_MAX:
                    problems.append(f"{cw}.max_chars: целое {NODE_CONTEXT_CHARS_MIN}–{NODE_CONTEXT_CHARS_MAX}")
            watch = node.get("watch", {})
            if not isinstance(watch, dict):
                problems.append(f"{where}.watch: объект «тема: сдвиг срочности»")
            else:
                for topic, shift in watch.items():
                    if not isinstance(topic, str) or not TOPIC_RE.match(topic):
                        problems.append(f"{where}.watch: тема «{topic}» из строчных латинских букв")
                    if not _is_int(shift) or not WATCH_SHIFT_MIN <= shift <= WATCH_SHIFT_MAX:
                        problems.append(f"{where}.watch[{topic}]: целое {WATCH_SHIFT_MIN}–{WATCH_SHIFT_MAX}")
        for path in sorted(paths):
            parent = path.rpartition("/")[0]
            if parent and parent not in paths:
                problems.append(f"nodes[{path}]: нет родительского узла «{parent}»")
    for tool in manifest.get("tools") or []:
        if isinstance(tool, dict) and isinstance(tool.get("node"), str) and paths and tool["node"] not in paths:
            problems.append(f"tools[{tool.get('name')}].node: узла «{tool['node']}» нет в nodes")
    for emit in manifest.get("emits") or []:
        if isinstance(emit, dict) and isinstance(emit.get("follow_up"), list):
            for name in emit["follow_up"]:
                if isinstance(name, str) and name not in tool_names:
                    problems.append(f"emits[{emit.get('kind')}].follow_up: инструмента «{name}» нет в tools")


def _check_workspace(manifest: dict, problems: list[str]) -> None:
    """ТЗ 32, WS-4: среда агента, которую объявляет модуль (поле `workspace`). Необязательно: без него модуль не среда.
    Среда модуля входит в фокус только по импульсу: `process_started` и подобные условия запрещены."""
    if "workspace" not in manifest:
        return
    ws = manifest["workspace"]
    if not isinstance(ws, dict):
        problems.append("workspace: должен быть объектом")
        return
    if ws.get("id") != manifest.get("id"):
        problems.append("workspace.id: должен совпадать с id модуля")
    for key in ("title", "purpose", "done_when", "when_to_use"):
        if key in ws and (not isinstance(ws[key], str) or not ws[key].strip()):
            problems.append(f"workspace.{key}: непустая строка")
    if "title" not in ws:
        problems.append("workspace.title: обязательная непустая строка")
    entry = ws.get("entry", list(WORKSPACE_ENTRY_DEFAULT))
    if not isinstance(entry, list) or not entry or not all(isinstance(e, str) for e in entry):
        problems.append("workspace.entry: непустой список строк")
    else:
        for e in entry:
            if e not in WORKSPACE_ENTRIES:
                problems.append(f"workspace.entry: «{e}» неизвестно, допустимо {list(WORKSPACE_ENTRIES)} "
                                "(запуск процесса и окно среду не включают)")
    if "interruptible" in ws and not isinstance(ws["interruptible"], bool):
        problems.append("workspace.interruptible: должно быть true или false")
    for key in ("safe_points", "signals_in", "keywords"):
        if key in ws and (not isinstance(ws[key], list) or not all(isinstance(x, str) and x.strip() for x in ws[key])):
            problems.append(f"workspace.{key}: список непустых строк")
    if "default_goal" in ws and (not isinstance(ws["default_goal"], str) or not ws["default_goal"].strip() or len(ws["default_goal"]) > 300):
        problems.append("workspace.default_goal: непустая строка не длиннее 300 символов")
    if "autonomous" in ws and not isinstance(ws["autonomous"], bool):
        problems.append("workspace.autonomous: должно быть true или false")
    if "self_goals" in ws and not isinstance(ws["self_goals"], bool):
        problems.append("workspace.self_goals: должно быть true или false")
    if "work_interval_s" in ws and (not _is_int(ws["work_interval_s"]) or not 30 <= ws["work_interval_s"] <= 3600):
        problems.append("workspace.work_interval_s: целое 30–3600 секунд")
    extra = set(ws) - {"id", "title", "purpose", "entry", "interruptible", "safe_points", "signals_in", "done_when",
                       "autonomous", "work_interval_s", "default_goal", "self_goals", "when_to_use", "keywords",
                       "brief", "skills", "area", "commands", "site"}
    if extra:
        problems.append(f"workspace: неизвестные поля {sorted(extra)}")


# ---------------------------------------------------------------- расширение блока workspace (контракт 3.2)

WS_BRIEF_MAX = 400
WS_SKILLS_MAX = 40
WS_COMMANDS_MAX = 40
WS_AREA_PATHS_MAX = 40
WS_TEXT_MAX = 300
WS_SKILL_NAME_RE = re.compile(r"^[a-z0-9_-]{1,64}$")
WS_COMMAND_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
WS_TIMEOUT_MAX_S = 86400
WS_AREA_KEYS = ("root", "writable", "readonly", "hidden")


def _rel_path_problem(where: str, value: Any) -> str | None:
    """Путь области: непустая относительная строка без `..`, без диска и абсолютного начала."""
    if not isinstance(value, str) or not value.strip() or len(value) > 300:
        return f"{where}: непустая строка до 300 символов"
    norm = value.replace("\\", "/")
    if norm.startswith("/") or re.match(r"^[A-Za-z]:", norm) or os.path.isabs(value):
        return f"{where}: путь должен быть относительным"
    if ".." in norm.split("/"):
        return f"{where}: «..» в пути запрещено"
    return None


def workspace_field_problems(ws: dict) -> dict[str, list[str]]:
    """Проблемы необязательных полей workspace 3.2 (`brief`, `skills`, `area`, `commands`, `site`): поле -> список проблем."""
    out: dict[str, list[str]] = {}

    def add(key: str, text: str | None) -> None:
        if text:
            out.setdefault(key, []).append(text)

    if "brief" in ws and (not isinstance(ws["brief"], str) or not ws["brief"].strip() or len(ws["brief"]) > WS_BRIEF_MAX):
        add("brief", f"workspace.brief: непустая строка до {WS_BRIEF_MAX} символов")
    if "skills" in ws:
        skills = ws["skills"]
        if not isinstance(skills, list) or len(skills) > WS_SKILLS_MAX:
            add("skills", f"workspace.skills: список до {WS_SKILLS_MAX} навыков")
        else:
            names: set[str] = set()
            for i, sk in enumerate(skills):
                where = f"workspace.skills[{i}]"
                if not isinstance(sk, dict):
                    add("skills", f"{where}: объект {{name, description, path}}")
                    continue
                name = sk.get("name")
                if not isinstance(name, str) or not WS_SKILL_NAME_RE.match(name):
                    add("skills", f"{where}.name: строчные латинские, цифры, «_» и «-»")
                elif name in names:
                    add("skills", f"{where}.name: «{name}» повторяется")
                else:
                    names.add(name)
                if not isinstance(sk.get("description"), str) or not sk["description"].strip() or len(sk["description"]) > WS_TEXT_MAX:
                    add("skills", f"{where}.description: непустая строка до {WS_TEXT_MAX} символов")
                add("skills", _rel_path_problem(f"{where}.path", sk.get("path")))
    if "area" in ws:
        area = ws["area"]
        if not isinstance(area, dict):
            add("area", "workspace.area: должен быть объектом")
        else:
            extra = set(area) - set(WS_AREA_KEYS)
            if extra:
                add("area", f"workspace.area: неизвестные поля {sorted(extra)}")
            if "root" in area:
                add("area", _rel_path_problem("workspace.area.root", area["root"]))
            for key in ("writable", "readonly", "hidden"):
                if key not in area:
                    continue
                items = area[key]
                if not isinstance(items, list) or len(items) > WS_AREA_PATHS_MAX:
                    add("area", f"workspace.area.{key}: список путей до {WS_AREA_PATHS_MAX}")
                    continue
                for i, item in enumerate(items):
                    add("area", _rel_path_problem(f"workspace.area.{key}[{i}]", item))
    if "commands" in ws:
        cmds = ws["commands"]
        if not isinstance(cmds, list) or len(cmds) > WS_COMMANDS_MAX:
            add("commands", f"workspace.commands: список до {WS_COMMANDS_MAX} команд")
        else:
            ids: set[str] = set()
            for i, c in enumerate(cmds):
                where = f"workspace.commands[{i}]"
                if not isinstance(c, dict):
                    add("commands", f"{where}: объект {{id, title}}")
                    continue
                cid = c.get("id")
                if not isinstance(cid, str) or not WS_COMMAND_ID_RE.match(cid):
                    add("commands", f"{where}.id: идентификатор «[a-z][a-z0-9_]*»")
                elif cid in ids:
                    add("commands", f"{where}.id: «{cid}» повторяется")
                else:
                    ids.add(cid)
                if not isinstance(c.get("title"), str) or not c["title"].strip() or len(c["title"]) > WS_TEXT_MAX:
                    add("commands", f"{where}.title: непустая строка до {WS_TEXT_MAX} символов")
                for flag in ("background", "irreversible"):
                    if flag in c and not isinstance(c[flag], bool):
                        add("commands", f"{where}.{flag}: true или false")
                if "timeout_s" in c and (not _is_int(c["timeout_s"]) or not 1 <= c["timeout_s"] <= WS_TIMEOUT_MAX_S):
                    add("commands", f"{where}.timeout_s: целое 1–{WS_TIMEOUT_MAX_S}")
    if "site" in ws:
        site = ws["site"]
        if not isinstance(site, dict) or not _is_loopback_url(site.get("url")):
            add("site", "workspace.site: объект {url} с http(s) адресом на 127.0.0.1 или localhost")
    return out


@dataclass
class WorkspaceSkill:
    """Навык окружения: папка с `SKILL.md` внутри рабочей области или образа."""

    name: str
    description: str
    path: str

    def to_dict(self) -> dict:
        return {"name": self.name, "description": self.description, "path": self.path}


@dataclass
class WorkspaceArea:
    """Рабочая область окружения: где агент читает и пишет (пути относительны корню окружения)."""

    root: str = ""
    writable: list[str] = field(default_factory=list)
    readonly: list[str] = field(default_factory=list)
    hidden: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        out: dict = {}
        if self.root:
            out["root"] = self.root
        for key in ("writable", "readonly", "hidden"):
            if getattr(self, key):
                out[key] = list(getattr(self, key))
        return out


@dataclass
class WorkspaceCommand:
    """Разрешённое действие запуска (`run`): произвольной оболочки нет."""

    id: str
    title: str
    background: bool = False
    timeout_s: int | None = None
    irreversible: bool = False

    def to_dict(self) -> dict:
        out: dict = {"id": self.id, "title": self.title}
        if self.background:
            out["background"] = True
        if self.timeout_s is not None:
            out["timeout_s"] = self.timeout_s
        if self.irreversible:
            out["irreversible"] = True
        return out


@dataclass
class WorkspaceSpec:
    """Необязательные поля блока `workspace` контракта 3.2. Пустые поля в `to_dict` не выводятся."""

    brief: str = ""
    skills: list[WorkspaceSkill] = field(default_factory=list)
    area: WorkspaceArea | None = None
    commands: list[WorkspaceCommand] = field(default_factory=list)
    site_url: str = ""

    def command(self, command_id: str) -> WorkspaceCommand | None:
        return next((c for c in self.commands if c.id == command_id), None)

    def skill(self, name: str) -> WorkspaceSkill | None:
        return next((s for s in self.skills if s.name == name), None)

    def to_dict(self) -> dict:
        out: dict = {}
        if self.brief:
            out["brief"] = self.brief
        if self.skills:
            out["skills"] = [s.to_dict() for s in self.skills]
        if self.area is not None and self.area.to_dict():
            out["area"] = self.area.to_dict()
        if self.commands:
            out["commands"] = [c.to_dict() for c in self.commands]
        if self.site_url:
            out["site"] = {"url": self.site_url}
        return out


def parse_workspace_spec(ws: Any) -> tuple[WorkspaceSpec | None, list[str]]:
    """Разобрать поля 3.2 блока `workspace`. Неверное поле отбрасывается с предупреждением, остальные остаются
    (окружение не отклоняется). Нет ни одного поля: (None, [])."""
    if not isinstance(ws, dict):
        return None, []
    problems = workspace_field_problems(ws)
    warnings = [f"поле workspace.{key} отброшено: " + "; ".join(items) for key, items in problems.items()]
    spec = WorkspaceSpec()
    if "brief" in ws and "brief" not in problems:
        spec.brief = ws["brief"].strip()
    if "skills" in ws and "skills" not in problems:
        spec.skills = [WorkspaceSkill(s["name"], s["description"], s["path"]) for s in ws["skills"]]
    if "area" in ws and "area" not in problems:
        a = ws["area"]
        spec.area = WorkspaceArea(root=a.get("root", ""), writable=list(a.get("writable") or []),
                                  readonly=list(a.get("readonly") or []), hidden=list(a.get("hidden") or []))
    if "commands" in ws and "commands" not in problems:
        spec.commands = [WorkspaceCommand(c["id"], c["title"], bool(c.get("background", False)), c.get("timeout_s"),
                                          bool(c.get("irreversible", False))) for c in ws["commands"]]
    if "site" in ws and "site" not in problems:
        spec.site_url = ws["site"]["url"]
    if not spec.to_dict():
        return None, warnings
    return spec, warnings


@dataclass
class ToolSpec:
    """Инструмент из манифеста (раздел 6.1)."""

    name: str
    description: str
    params: dict
    risk: str
    availability: str
    informative: bool = False
    self_confirming: bool = False
    timeout_s: int = TIMEOUT_TOOL_DEFAULT
    keywords: list[str] = field(default_factory=list)
    danger_check: bool = False
    job: bool = False
    fallback: bool = False
    core_fills: list[str] = field(default_factory=list)
    announce: bool = False          # долгое или заметное действие на ПК: ядро объявляет запуск до вызова (AN-3)
    node: str | None = None         # ТЗ 33: узел рабочего места, в котором инструмент виден; None - виден всегда
    sends: bool = False             # контракт 3.4: инструмент отправляет текст людям (сообщение в чат): ядро пишет исходящее событие `module_out`
    read_only: bool = False         # контракт 3.5: явное чтение без побочного эффекта; по умолчанию эффект есть (журнал действий хода)

    def full_id(self, peer_id: str) -> str:
        return f"{peer_id}.{self.name}"

    @classmethod
    def from_dict(cls, d: dict) -> ToolSpec:
        return cls(
            name=d["name"], description=d["description"], params=dict(d["params"]), risk=d["risk"],
            availability=d["availability"], informative=bool(d.get("informative", False)),
            self_confirming=bool(d.get("self_confirming", False)),
            timeout_s=int(d.get("timeout_s", TIMEOUT_TOOL_DEFAULT)), keywords=list(d.get("keywords") or []),
            danger_check=bool(d.get("danger_check", False)), job=bool(d.get("job", False)),
            fallback=bool(d.get("fallback", False)), core_fills=list(d.get("core_fills") or []),
            announce=bool(d.get("announce", False)), node=d.get("node"),
            sends=bool(d.get("sends", False)), read_only=bool(d.get("read_only", False)))

    def to_dict(self) -> dict:
        return {"name": self.name, "description": self.description, "params": self.params, "risk": self.risk,
                "availability": self.availability, "informative": self.informative,
                "self_confirming": self.self_confirming, "timeout_s": self.timeout_s, "keywords": self.keywords,
                "danger_check": self.danger_check, "job": self.job, "fallback": self.fallback,
                "core_fills": self.core_fills, "announce": self.announce,
                **({"node": self.node} if self.node else {}), **({"sends": True} if self.sends else {}),
                **({"read_only": True} if self.read_only else {})}


@dataclass
class EmitSpec:
    """Событие, которое объявляет модуль."""

    kind: str
    description: str
    default_urgency: int
    urgency_unfocused: int | None = None        # ТЗ 33: срочность, когда модуль не в фокусе (по умолчанию как default_urgency)
    topics: list[str] = field(default_factory=list)      # темы события: по ним узел сдвигает срочность (nodes[].watch)
    follow_up: list[str] = field(default_factory=list)   # какие инструменты-запросы вызвать после события

    @classmethod
    def from_dict(cls, e: dict) -> EmitSpec:
        return cls(e["kind"], e["description"], e["default_urgency"], urgency_unfocused=e.get("urgency_unfocused"),
                   topics=list(e.get("topics") or []), follow_up=list(e.get("follow_up") or []))

    def to_dict(self) -> dict:
        out = {"kind": self.kind, "description": self.description, "default_urgency": self.default_urgency}
        if self.urgency_unfocused is not None:
            out["urgency_unfocused"] = self.urgency_unfocused
        if self.topics:
            out["topics"] = list(self.topics)
        if self.follow_up:
            out["follow_up"] = list(self.follow_up)
        return out


# ---------------------------------------------------------------- пространство модуля (TZ 15)

HOST_RE = re.compile(r"^[A-Za-z0-9.-]{1,253}$")


def _is_http_url(v: Any) -> bool:
    if not isinstance(v, str) or len(v) > 2000:
        return False
    parts = urlsplit(v)
    return parts.scheme in ("http", "https") and bool(parts.hostname)


def _is_loopback_url(v: Any) -> bool:
    return _is_http_url(v) and urlsplit(v).hostname in ("127.0.0.1", "localhost")


def panel_problems(panel: Any, tools: set[str]) -> list[str]:
    """Проверка поля `panel` манифеста (TZ 15, раздел 3). Пустой список = верно. `tools` - имена инструментов манифеста."""
    if not isinstance(panel, dict):
        return ["panel: должен быть объектом"]
    problems: list[str] = []
    title = panel.get("title")
    if title is not None and (not isinstance(title, str) or not title.strip() or len(title) > PANEL_TITLE_MAX):
        problems.append(f"panel.title: строка 1–{PANEL_TITLE_MAX} символов")
    icon = panel.get("icon")
    if icon is not None and (not isinstance(icon, str) or len(icon) > PANEL_ICON_MAX):
        problems.append(f"panel.icon: строка до {PANEL_ICON_MAX} символов")
    if panel.get("order") is not None and not _is_int(panel["order"]):
        problems.append("panel.order: целое число")
    if panel.get("embed_url") is not None and not _is_loopback_url(panel["embed_url"]):
        problems.append("panel.embed_url: http(s) адрес на 127.0.0.1 или localhost")
    dirs = panel.get("serve_dirs")
    if dirs is not None and (not isinstance(dirs, list) or len(dirs) > PANEL_DIRS_MAX
                             or not all(isinstance(d, str) and os.path.isabs(d) for d in dirs)):
        problems.append(f"panel.serve_dirs: список абсолютных путей, до {PANEL_DIRS_MAX}")
    hosts = panel.get("allowed_hosts")
    if hosts is not None and (not isinstance(hosts, list) or len(hosts) > PANEL_HOSTS_MAX
                              or not all(isinstance(h, str) and HOST_RE.match(h) for h in hosts)):
        problems.append(f"panel.allowed_hosts: список имён узлов без адреса, до {PANEL_HOSTS_MAX}")
    actions = panel.get("actions")
    if actions is not None:
        if not isinstance(actions, list) or len(actions) > PANEL_ACTIONS_MAX \
                or not all(isinstance(a, str) and PANEL_ID_RE.match(a) for a in actions):
            problems.append(f"panel.actions: список идентификаторов «[a-z][a-z0-9_]*», до {PANEL_ACTIONS_MAX}")
        elif len(set(actions)) != len(actions):
            problems.append("panel.actions: идентификатор повторяется")
    links = panel.get("links")
    if links is not None:
        if not isinstance(links, list) or len(links) > PANEL_LINKS_MAX:
            problems.append(f"panel.links: список до {PANEL_LINKS_MAX} ссылок")
        else:
            ids: set[str] = set()
            for i, link in enumerate(links):
                problems.extend(_link_problems(f"panel.links[{i}]", link, tools, ids))
    return problems


def _link_problems(where: str, link: Any, tools: set[str], ids: set[str]) -> list[str]:
    if not isinstance(link, dict):
        return [f"{where}: должен быть объектом"]
    problems: list[str] = []
    lid = link.get("id")
    if not isinstance(lid, str) or not PANEL_ID_RE.match(lid):
        problems.append(f"{where}.id: идентификатор «[a-z][a-z0-9_]*»")
    elif lid in ids:
        problems.append(f"{where}.id: «{lid}» повторяется")
    else:
        ids.add(lid)
    label = link.get("label")
    if not isinstance(label, str) or not label.strip() or len(label) > PANEL_LABEL_MAX:
        problems.append(f"{where}.label: строка 1–{PANEL_LABEL_MAX} символов")
    kind = link.get("kind")
    if kind not in PANEL_LINK_KINDS:
        return problems + [f"{where}.kind: одно из {list(PANEL_LINK_KINDS)}"]
    if kind == "url" and not _is_http_url(link.get("url")):
        problems.append(f"{where}.url: http(s) адрес")
    if kind == "tool":
        tool = link.get("tool")
        if not isinstance(tool, str) or tool not in tools:
            problems.append(f"{where}.tool: имя инструмента из tools этого манифеста")
        if link.get("args_from") is not None and link["args_from"] not in PANEL_ARGS_FROM:
            problems.append(f"{where}.args_from: одно из {list(PANEL_ARGS_FROM)}")
        if link.get("open_result_url") is not None and not isinstance(link["open_result_url"], bool):
            problems.append(f"{where}.open_result_url: true или false")
    if kind == "reveal_path":
        path, path_from = link.get("path"), link.get("path_from")
        if (path is None) == (path_from is None):
            problems.append(f"{where}: нужно ровно одно из path (абсолютный путь) или path_from (имя поля state.data)")
        elif path is not None and (not isinstance(path, str) or not os.path.isabs(path)):
            problems.append(f"{where}.path: абсолютный путь")
        elif path_from is not None and (not isinstance(path_from, str) or not PANEL_ID_RE.match(path_from)):
            problems.append(f"{where}.path_from: имя поля state.data")
    return problems


def parse_panel(manifest: dict) -> tuple[PanelSpec | None, list[str]]:
    """Разобрать `panel` манифеста. Неверный panel отбрасывается целиком: модуль работает без пространства (TZ 15)."""
    raw = manifest.get("panel")
    if raw is None:
        return None, []
    tools = {t["name"] for t in manifest.get("tools") or [] if isinstance(t, dict) and isinstance(t.get("name"), str)}
    problems = panel_problems(raw, tools)
    if problems:
        return None, ["пространство модуля отброшено, модуль работает без него: " + "; ".join(problems)]
    links = [PanelLink(id=l["id"], label=l["label"], kind=l["kind"], url=l.get("url") or "",
                       tool=l.get("tool") or "", args_from=l.get("args_from") or "",
                       open_result_url=bool(l.get("open_result_url", False)), path=l.get("path") or "",
                       path_from=l.get("path_from") or "") for l in raw.get("links") or []]
    order = raw.get("order")
    spec = PanelSpec(title=raw.get("title") or "", icon=raw.get("icon") or "",
                     order=PANEL_ORDER_DEFAULT if order is None else order, links=links,
                     embed_url=raw.get("embed_url") or "", serve_dirs=list(raw.get("serve_dirs") or []),
                     actions=list(raw.get("actions") or []), allowed_hosts=list(raw.get("allowed_hosts") or []))
    return spec, []


@dataclass
class PanelLink:
    """Ссылка или вызов на странице модуля (`panel.links`, раздел 3 ТЗ 15)."""

    id: str
    label: str
    kind: str                       # url | tool | reveal_path
    url: str = ""
    tool: str = ""
    args_from: str = ""             # "job": аргументы берутся из задачи, для которой нарисована ссылка
    open_result_url: bool = False
    path: str = ""
    path_from: str = ""

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if not (v == "" or v is False)}


@dataclass
class PanelSpec:
    """Поле `panel` манифеста (TZ 15, раздел 3). Пустые поля в `to_dict` не выводятся, кроме `order`."""

    title: str = ""
    icon: str = ""
    order: int = PANEL_ORDER_DEFAULT
    links: list[PanelLink] = field(default_factory=list)
    embed_url: str = ""
    serve_dirs: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    allowed_hosts: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        out: dict = {"order": self.order}
        for key, value in (("title", self.title), ("icon", self.icon), ("embed_url", self.embed_url),
                           ("serve_dirs", self.serve_dirs), ("actions", self.actions),
                           ("allowed_hosts", self.allowed_hosts)):
            if value:
                out[key] = list(value) if isinstance(value, list) else value
        if self.links:
            out["links"] = [link.to_dict() for link in self.links]
        return out


# блоки панели (panel.set, раздел 2 и 3 ТЗ 15)

def _cell_ok(v: Any) -> bool:
    if v is None or isinstance(v, bool):
        return True
    if isinstance(v, float):
        return math.isfinite(v)
    if _is_num(v):
        return True
    return isinstance(v, str) and len(v) <= PANEL_CELL_MAX


def _text_problem(where: str, item: dict, key: str, limit: int, *, required: bool = True) -> str | None:
    value = item.get(key)
    if value is None:
        return f"{where}.{key}: обязательная строка" if required else None
    if not isinstance(value, str) or len(value) > limit:
        return f"{where}.{key}: строка до {limit} символов"
    return None


def _artifact_problems(where: str, ref: Any) -> list[str]:
    if not isinstance(ref, dict):
        return [f"{where}.artifact: объект {{job, name}}"]
    problems = []
    job = ref.get("job")
    if not isinstance(job, str) or not JOB_ID_RE.match(job):
        problems.append(f"{where}.artifact.job: идентификатор задачи j-xxxxxxxx")
    name = ref.get("name")
    if not isinstance(name, str) or not name or len(name) > 200 or name in (".", "..") or "/" in name or "\\" in name:
        problems.append(f"{where}.artifact.name: имя файла без путей")
    return problems


def _action_problems(where: str, item: Any, actions: set[str] | None) -> list[str]:
    """Действие строки или кнопки: id (из panel.actions, если список известен), подпись, args - объект."""
    if not isinstance(item, dict):
        return [f"{where}: объект {{id, label}}"]
    problems = []
    aid = item.get("id", item.get("action"))
    if not isinstance(aid, str) or not PANEL_ID_RE.match(aid):
        problems.append(f"{where}.id: идентификатор действия")
    elif actions is not None and aid not in actions:
        problems.append(f"{where}.id: действие «{aid}» не объявлено в panel.actions")
    problems += [p for p in [_text_problem(where, item, "label", PANEL_LABEL_MAX)] if p]
    if item.get("args") is not None and not isinstance(item["args"], dict):
        problems.append(f"{where}.args: объект")
    return problems


def _check_stats(where: str, b: dict, actions, depth: int) -> list[str]:
    items = b.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= PANEL_ITEMS_MAX:
        return [f"{where}.items: список 1–{PANEL_ITEMS_MAX}"]
    problems = []
    for j, it in enumerate(items):
        w = f"{where}.items[{j}]"
        if not isinstance(it, dict):
            problems.append(f"{w}: объект")
            continue
        problems += [p for p in [_text_problem(w, it, "label", PANEL_LABEL_MAX)] if p]
        if "value" not in it or not _cell_ok(it["value"]):
            problems.append(f"{w}.value: строка, число или логическое значение")
    return problems


def _check_table(where: str, b: dict, actions, depth: int) -> list[str]:
    cols = b.get("columns")
    if not isinstance(cols, list) or not 1 <= len(cols) <= 12 or not all(
            isinstance(c, str) and len(c) <= PANEL_LABEL_MAX for c in cols):
        return [f"{where}.columns: список 1–12 подписей"]
    rows = b.get("rows", [])
    if not isinstance(rows, list) or len(rows) > PANEL_ROWS_MAX:
        return [f"{where}.rows: список до {PANEL_ROWS_MAX} строк"]
    problems = []
    for r, row in enumerate(rows):
        w = f"{where}.rows[{r}]"
        if not isinstance(row, dict) or not isinstance(row.get("cells"), list) or len(row["cells"]) != len(cols):
            problems.append(f"{w}: объект с cells той же длины, что columns")
            continue
        problems += [f"{w}.cells[{c}]: строка, число или логическое значение"
                     for c, cell in enumerate(row["cells"]) if not _cell_ok(cell)]
        acts = row.get("actions")
        if acts is not None:
            if not isinstance(acts, list) or len(acts) > 6:
                problems.append(f"{w}.actions: список до 6 действий")
            else:
                for k, act in enumerate(acts):
                    problems += _action_problems(f"{w}.actions[{k}]", act, actions)
    return problems


def _check_list(where: str, b: dict, actions, depth: int) -> list[str]:
    items = b.get("items")
    if not isinstance(items, list) or len(items) > PANEL_ROWS_MAX:
        return [f"{where}.items: список до {PANEL_ROWS_MAX} строк"]
    problems = []
    for j, it in enumerate(items):
        w = f"{where}.items[{j}]"
        if not isinstance(it, dict):
            problems.append(f"{w}: объект")
            continue
        problems += [p for p in [_text_problem(w, it, "text", PANEL_CELL_MAX),
                                 _text_problem(w, it, "sub", 300, required=False),
                                 _text_problem(w, it, "badge", 40, required=False)] if p]
    return problems


def _check_text(where: str, b: dict, actions, depth: int) -> list[str]:
    return [p for p in [_text_problem(where, b, "text", PANEL_TEXT_MAX)] if p]


def _check_progress(where: str, b: dict, actions, depth: int) -> list[str]:
    problems = [p for p in [_text_problem(where, b, "label", 120, required=False)] if p]
    value = b.get("value")
    if not _is_num(value) or not 0 <= value <= 1:
        problems.append(f"{where}.value: число 0–1")
    return problems


def _check_buttons(where: str, b: dict, actions, depth: int) -> list[str]:
    items = b.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= 20:
        return [f"{where}.items: список 1–20 кнопок"]
    problems = []
    for j, it in enumerate(items):
        w = f"{where}.items[{j}]"
        if not isinstance(it, dict):
            problems.append(f"{w}: объект")
            continue
        has_action, has_url = "action" in it, "url" in it
        if has_action == has_url:
            problems.append(f"{w}: нужно ровно одно из action (id действия) или url (http(s))")
        if has_action:
            problems += _action_problems(w, {"id": it["action"], "label": it.get("label"), "args": it.get("args")},
                                         actions)
        if has_url:
            if not _is_http_url(it["url"]):
                problems.append(f"{w}.url: http(s) адрес")
            problems += [p for p in [_text_problem(w, it, "label", PANEL_LABEL_MAX)] if p]
        if it.get("style") is not None and it["style"] not in PANEL_BUTTON_STYLES:
            problems.append(f"{w}.style: одно из {list(PANEL_BUTTON_STYLES)}")
    return problems


def _check_link(where: str, b: dict, actions, depth: int) -> list[str]:
    problems = [p for p in [_text_problem(where, b, "label", PANEL_LABEL_MAX)] if p]
    if not _is_http_url(b.get("url")):
        problems.append(f"{where}.url: http(s) адрес")
    return problems


def _check_image(where: str, b: dict, actions, depth: int) -> list[str]:
    """Картинка: либо artifact {job, name} (файл задачи), либо file - имя файла-картинки прямо в panel.serve_dirs (без задачи:
    так показывают QR-код входа и подобное). Ровно одно из двух."""
    if ("artifact" in b) == ("file" in b):
        return [f"{where}: нужно ровно одно из artifact ({{job, name}}) или file (имя файла)"]
    if "file" in b:
        problems = [] if _image_name_ok(b["file"]) else [
            f"{where}.file: имя файла без путей, расширение из {list(PANEL_IMAGE_EXTS)}"]
    else:
        problems = _artifact_problems(where, b.get("artifact"))
    return problems + [p for p in [_text_problem(where, b, "alt", 200, required=False)] if p]


def _check_video(where: str, b: dict, actions, depth: int) -> list[str]:
    return _artifact_problems(where, b.get("artifact"))


def _check_log(where: str, b: dict, actions, depth: int) -> list[str]:
    lines = b.get("lines")
    if not isinstance(lines, list) or len(lines) > PANEL_LINES_MAX or not all(
            isinstance(x, str) and len(x) <= PANEL_LINE_MAX for x in lines):
        return [f"{where}.lines: список до {PANEL_LINES_MAX} строк по {PANEL_LINE_MAX} символов"]
    return []


def _check_input(where: str, b: dict, actions, depth: int) -> list[str]:
    """Поле ввода с кнопкой: значение уходит действием `action` как args.value (вместе с args блока). secret: поле пароля;
    его значение ядро нигде не пишет и вырезает из ответов, начальное значение (value) у такого поля запрещено."""
    problems = _action_problems(where, {"id": b.get("action"), "label": b.get("submit", "Отправить"), "args": b.get("args")},
                                actions)
    problems += [p for p in [_text_problem(where, b, "label", PANEL_LABEL_MAX), _text_problem(where, b, "placeholder", 120, required=False),
                             _text_problem(where, b, "value", PANEL_INPUT_VALUE_MAX, required=False)] if p]
    if b.get("secret") is not None and not isinstance(b["secret"], bool):
        problems.append(f"{where}.secret: true или false")
    if b.get("secret") is True and b.get("value"):
        problems.append(f"{where}.value: у секретного поля начального значения быть не может")
    if b.get("multiline") is not None and not isinstance(b["multiline"], bool):
        problems.append(f"{where}.multiline: true или false")
    return problems


def _image_name_ok(name: Any) -> bool:
    """Имя файла-картинки прямо в panel.serve_dirs: без путей, расширение из PANEL_IMAGE_EXTS."""
    return (isinstance(name, str) and bool(name) and len(name) <= 200 and name not in (".", "..") and "/" not in name
            and "\\" not in name and name.lower().endswith(PANEL_IMAGE_EXTS))


def _select_problems(where: str, sel: Any, actions: set[str] | None) -> list[str]:
    """Выпадающий список карточки: options [{value, label}], value (выбранный), action и args (как у кнопки), arg: под каким
    именем выбранное значение уходит в args действия (по умолчанию «value»), hint: подсказка при наведении."""
    if not isinstance(sel, dict):
        return [f"{where}: объект {{options, value, action}}"]
    problems = _action_problems(where, {"id": sel.get("action"), "label": "x", "args": sel.get("args")}, actions)
    opts = sel.get("options")
    values: list = []
    if not isinstance(opts, list) or not 1 <= len(opts) <= PANEL_SELECT_OPTIONS_MAX:
        problems.append(f"{where}.options: список 1–{PANEL_SELECT_OPTIONS_MAX} пунктов {{value, label}}")
    else:
        for k, o in enumerate(opts):
            w = f"{where}.options[{k}]"
            if not isinstance(o, dict):
                problems.append(f"{w}: объект {{value, label}}")
                continue
            values.append(o.get("value"))
            problems += [p for p in [_text_problem(w, o, "value", 40), _text_problem(w, o, "label", PANEL_LABEL_MAX)] if p]
            if isinstance(o.get("value"), str) and not o["value"]:
                problems.append(f"{w}.value: непустая строка")
    if sel.get("value") is not None and (not isinstance(sel["value"], str) or (values and sel["value"] not in values)):
        problems.append(f"{where}.value: значение одного из пунктов")
    problems += [p for p in [_text_problem(where, sel, "hint", 200, required=False)] if p]
    arg = sel.get("arg")
    if arg is not None and (not isinstance(arg, str) or not PANEL_ID_RE.match(arg)):
        problems.append(f"{where}.arg: имя параметра «[a-z][a-z0-9_]*»")
    return problems


def _card_problems(where: str, c: Any, actions: set[str] | None, nested: bool) -> list[str]:
    """Карточка списка: title (обязательно), subtitle, badge (число непрочитанных), tag (маленькая серая метка), avatar (имя
    файла-картинки в serve_dirs; нет: кружок с буквой), icon (короткий значок вместо буквы) и color (#RRGGBB кружка), select,
    action с args (клик), selected, muted, expanded и children (вложенные карточки, не глубже одного уровня)."""
    if not isinstance(c, dict):
        return [f"{where}: объект"]
    problems = [p for p in [_text_problem(where, c, "title", 120), _text_problem(where, c, "subtitle", 300, required=False),
                            _text_problem(where, c, "badge", 40, required=False), _text_problem(where, c, "tag", 40, required=False),
                            _text_problem(where, c, "icon", 16, required=False)] if p]
    if c.get("avatar") is not None and not _image_name_ok(c["avatar"]):
        problems.append(f"{where}.avatar: имя файла без путей, расширение из {list(PANEL_IMAGE_EXTS)}")
    if c.get("color") is not None and not (isinstance(c["color"], str) and PANEL_COLOR_RE.match(c["color"])):
        problems.append(f"{where}.color: цвет #RRGGBB")
    for key in ("selected", "muted", "expanded"):
        if c.get(key) is not None and not isinstance(c[key], bool):
            problems.append(f"{where}.{key}: true или false")
    if c.get("action") is not None:
        problems += _action_problems(f"{where}.action", {"id": c["action"], "label": "x", "args": c.get("args")}, actions)
    elif c.get("args") is not None:
        problems.append(f"{where}.args: без action не нужны")
    if c.get("select") is not None:
        problems += _select_problems(f"{where}.select", c["select"], actions)
    children = c.get("children")
    if children is not None:
        if nested:
            problems.append(f"{where}.children: вложенные карточки не глубже одного уровня")
        elif not isinstance(children, list) or len(children) > PANEL_CARDS_MAX:
            problems.append(f"{where}.children: список до {PANEL_CARDS_MAX} карточек")
        else:
            for k, child in enumerate(children):
                problems += _card_problems(f"{where}.children[{k}]", child, actions, True)
    return problems


def _check_cards(where: str, b: dict, actions, depth: int) -> list[str]:
    """Список карточек (диалоги, темы): до PANEL_CARDS_MAX карточек, у каждой до PANEL_CARDS_MAX вложенных."""
    items = b.get("items")
    if not isinstance(items, list) or len(items) > PANEL_CARDS_MAX:
        return [f"{where}.items: список до {PANEL_CARDS_MAX} карточек"]
    problems: list[str] = []
    for j, c in enumerate(items):
        problems += _card_problems(f"{where}.items[{j}]", c, actions, False)
    return problems


def _check_split(where: str, b: dict, actions, depth: int) -> list[str]:
    """Две колонки: left (список) и right (содержимое) - списки блоков; на узком экране колонки идут друг под другом."""
    if depth >= PANEL_DEPTH_MAX:
        return [f"{where}: колонки вложены глубже {PANEL_DEPTH_MAX} уровней"]
    problems: list[str] = []
    for side in ("left", "right"):
        problems += [f"{where}.{side}.{p}" for p in validate_panel_blocks(b.get(side), actions, depth=depth + 1)]
    return problems


def _check_tabs(where: str, b: dict, actions, depth: int) -> list[str]:
    if depth >= PANEL_DEPTH_MAX:
        return [f"{where}: вкладки вложены глубже {PANEL_DEPTH_MAX} уровней"]
    tabs = b.get("tabs")
    if not isinstance(tabs, list) or not 1 <= len(tabs) <= PANEL_TABS_MAX:
        return [f"{where}.tabs: список 1–{PANEL_TABS_MAX} вкладок"]
    problems = []
    for k, tab in enumerate(tabs):
        w = f"{where}.tabs[{k}]"
        if not isinstance(tab, dict):
            problems.append(f"{w}: объект")
            continue
        problems += [p for p in [_text_problem(w, tab, "label", PANEL_LABEL_MAX)] if p]
        problems += [f"{w}.{p}" for p in validate_panel_blocks(tab.get("blocks"), actions, depth=depth + 1)]
    return problems


_BLOCK_CHECKERS = {
    "stats": _check_stats, "table": _check_table, "list": _check_list, "text": _check_text,
    "progress": _check_progress, "buttons": _check_buttons, "link": _check_link, "image": _check_image,
    "video": _check_video, "log": _check_log, "tabs": _check_tabs, "input": _check_input,
    "cards": _check_cards, "split": _check_split,
}


def _check_block(where: str, b: Any, actions: set[str] | None, depth: int) -> list[str]:
    if not isinstance(b, dict):
        return [f"{where}: блок должен быть объектом"]
    kind = b.get("type")
    if kind not in PANEL_BLOCK_TYPES:
        return [f"{where}.type: одно из {list(PANEL_BLOCK_TYPES)}"]
    problems = [p for p in [_text_problem(where, b, "title", 120, required=False)] if p]
    if b.get("collapsed") is not None and not isinstance(b["collapsed"], bool):
        problems.append(f"{where}.collapsed: true или false")
    return problems + _BLOCK_CHECKERS[kind](where, b, actions, depth)


def validate_panel_blocks(blocks: Any, actions: set[str] | None = None, *, depth: int = 0) -> list[str]:
    """Проверить список блоков панели (TZ 15, раздел 2). `actions` - допустимые id действий из panel.actions
    (None - не проверять id). Текст только строками: модуль не присылает HTML, дашборд экранирует всё."""
    if not isinstance(blocks, list):
        return ["blocks: список блоков"]
    if len(blocks) > PANEL_BLOCKS_MAX:
        return [f"blocks: не больше {PANEL_BLOCKS_MAX} блоков"]
    problems: list[str] = []
    for i, block in enumerate(blocks):
        problems += _check_block(f"blocks[{i}]", block, actions, depth)
    return problems


def validate_panel_set(body: Any, actions: set[str] | None = None) -> list[str]:
    """Проверить тело `panel.set`: {"blocks": [...]}, не больше PANEL_SET_MAX_BYTES (TZ 15, раздел 3)."""
    if not isinstance(body, dict) or not isinstance(body.get("blocks"), list):
        return ['panel.set: тело {"blocks": [...]}']
    try:
        size = len(json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    except (TypeError, ValueError):
        return ["panel.set: блоки должны быть JSON-значениями"]
    problems = [f"panel.set: больше {PANEL_SET_MAX_BYTES} байт"] if size > PANEL_SET_MAX_BYTES else []
    return problems + validate_panel_blocks(body["blocks"], actions)


@dataclass
class Manifest:
    """Проверенный манифест модуля. Собирается через `Manifest.from_dict` (бросает ManifestError)."""

    id: str
    title: str
    description: str
    version: str
    capabilities: list[str] = field(default_factory=list)
    subscribe: list[str] = field(default_factory=list)
    shared_settings: list[str] = field(default_factory=list)
    settings_schema: dict = field(default_factory=dict)
    tools: list[ToolSpec] = field(default_factory=list)
    emits: list[EmitSpec] = field(default_factory=list)
    graph: bool = False
    state: bool = False
    panel: PanelSpec | None = None                      # пространство модуля (TZ 15); None - вкладки нет
    panel_warnings: list[str] = field(default_factory=list)   # почему panel отброшен (для журнала ядра)
    workspace: dict | None = None                       # ТЗ 32, WS-4: среда агента, которую объявляет модуль
    workspace_spec: WorkspaceSpec | None = None         # контракт 3.2: разобранные brief, skills, area, commands, site
    workspace_warnings: list[str] = field(default_factory=list)   # какие поля workspace 3.2 отброшены и почему
    role: str = ""                                      # workspace | service (обязательно в манифесте)
    ownership: str | None = None                        # shared | owned (обязательно у workspace; у service None)
    nodes: list[dict] = field(default_factory=list)     # ТЗ 33: дерево узлов рабочего места (пусто: плоский список)
    event_prefixes: list[str] = field(default_factory=list)   # дополнительные префиксы видов событий (кроме id)

    @classmethod
    def from_dict(cls, d: Any) -> Manifest:
        warnings: list[str] = []
        problems = validate_manifest(d, warnings)
        if problems:
            raise ManifestError("манифест не прошёл проверку: " + "; ".join(problems), problems)
        panel, panel_warnings = parse_panel(d)
        ws_spec, ws_warnings = parse_workspace_spec(d["workspace"]) if isinstance(d.get("workspace"), dict) else (None, [])
        return cls(
            id=d["id"], title=d["title"], description=d["description"], version=d["version"],
            capabilities=list(d.get("capabilities") or []), subscribe=list(d.get("subscribe") or []),
            shared_settings=list(d.get("shared_settings") or []), settings_schema=dict(d.get("settings_schema") or {}),
            tools=[ToolSpec.from_dict(t) for t in d.get("tools") or []],
            emits=[EmitSpec.from_dict(e) for e in d.get("emits") or []],
            nodes=[dict(n) for n in d.get("nodes") or []],
            graph=bool(d.get("graph", False)), state=bool(d.get("state", False)),
            panel=panel, panel_warnings=panel_warnings,
            workspace=dict(d["workspace"]) if d.get("workspace") else None,
            workspace_spec=ws_spec, workspace_warnings=ws_warnings,
            role=d.get("role"), ownership=d.get("ownership"), event_prefixes=list(d.get("event_prefixes") or []))

    @property
    def effective_role(self) -> str:
        return self.role

    @property
    def effective_ownership(self) -> str | None:
        """Режим владения: у workspace shared или owned, у service None."""
        return None if self.role != ROLE_WORKSPACE else self.ownership

    def to_dict(self) -> dict:
        out = {"id": self.id, "title": self.title, "description": self.description, "version": self.version,
               "capabilities": self.capabilities, "subscribe": self.subscribe,
               "shared_settings": self.shared_settings, "settings_schema": self.settings_schema,
               "tools": [t.to_dict() for t in self.tools],
               "emits": [e.to_dict() for e in self.emits],
               "graph": self.graph, "state": self.state}
        if self.event_prefixes:
            out["event_prefixes"] = list(self.event_prefixes)
        out["role"] = self.role
        if self.ownership is not None:
            out["ownership"] = self.ownership
        if self.panel is not None:
            out["panel"] = self.panel.to_dict()
        if self.workspace:
            out["workspace"] = dict(self.workspace)
        if self.nodes:
            out["nodes"] = [dict(n) for n in self.nodes]
        return out

    def tool(self, name: str) -> ToolSpec | None:
        return next((t for t in self.tools if t.name == name), None)

    def setting_defaults(self) -> dict:
        """Значения собственных настроек по умолчанию (для ядра: `modules.<id>.<имя>`)."""
        return {name: spec["default"] for name, spec in self.settings_schema.items()}


# ---------------------------------------------------------------- рукопожатие (раздел 5)

def hello_body(manifest: dict, instance: str, pid: int | None = None) -> dict:
    """Тело `hello`: `id`, `role` (как в манифесте), `instance`, `pid`, `protocol`, `manifest`."""
    return {"id": manifest["id"], "role": role_of(manifest), "instance": instance,
            "pid": os.getpid() if pid is None else pid, "protocol": PROTOCOL, "manifest": manifest}


def validate_hello(body: Any) -> list[str]:
    """Проверить тело `hello` вместе с манифестом (для ядра: при проблемах отвечает `reject`).

    `id` и `role` обязательны; `role` совпадает с ролью манифеста; удалённое поле `module_id` не принимается."""
    if not isinstance(body, dict):
        return ["hello: тело должно быть объектом"]
    problems = []
    if body.get("protocol") != PROTOCOL or isinstance(body.get("protocol"), bool):
        problems.append(f"protocol {body.get('protocol')!r} не поддерживается (нужен {PROTOCOL})")
    mid = body.get("id")
    if not isinstance(mid, str) or not PEER_ID_RE.match(mid):
        problems.append(f"id: должен соответствовать {PEER_ID_RE.pattern}")
    if "module_id" in body:
        problems.append("module_id: поле удалено в контракте 2.0, нужно id")
    if not isinstance(body.get("instance"), str) or not body["instance"]:
        problems.append("instance: обязательная непустая строка")
    if not _is_int(body.get("pid")):
        problems.append("pid: целое число")
    manifest = body.get("manifest")
    problems.extend(validate_manifest(manifest))
    if isinstance(manifest, dict) and manifest.get("id") != mid:
        problems.append("id не совпадает с manifest.id")
    if body.get("role") not in ROLES:
        problems.append(f"role: одно из {list(ROLES)}")
    elif isinstance(manifest, dict) and body["role"] != role_of(manifest):
        problems.append(f"role {body['role']!r} не совпадает с manifest.role")
    return problems


# ---------------------------------------------------------------- события, речь, вопросы, задачи

def _check_urgency(where: str, body: dict, problems: list[str]) -> None:
    if "urgency_hint" in body:
        u = body["urgency_hint"]
        if not _is_int(u) or not URGENCY_MIN <= u <= URGENCY_MAX:
            problems.append(f"{where}urgency_hint: целое {URGENCY_MIN}–{URGENCY_MAX}")


def _check_expires(where: str, body: dict, problems: list[str]) -> None:
    if "expires_in_s" in body:
        e = body["expires_in_s"]
        if not _is_num(e) or e <= 0:
            problems.append(f"{where}expires_in_s: положительное число секунд")


def validate_event(body: Any) -> list[str]:
    """Проверить тело `event` (раздел 7.1)."""
    if not isinstance(body, dict):
        return ["event: тело должно быть объектом"]
    problems: list[str] = []
    kind = body.get("kind")
    if not isinstance(kind, str) or not KIND_RE.match(kind):
        problems.append("kind: вид «<модуль>.<что>» из латинских строчных букв")
    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        problems.append("text: обязательная непустая строка")
    elif len(text) > EVENT_TEXT_MAX:
        problems.append(f"text: длиннее {EVENT_TEXT_MAX} символов")
    if "data" in body and not isinstance(body["data"], dict):
        problems.append("data: должен быть объектом")
    _check_urgency("", body, problems)
    for flag in ("addressed_to_me", "memorable", "announce"):
        if flag in body and not isinstance(body[flag], bool):
            problems.append(f"{flag}: должно быть true или false")
    if "sender" in body:
        s = body["sender"]
        if not isinstance(s, dict) or not isinstance(s.get("id"), str) or not s["id"]:
            problems.append("sender: объект с непустой строкой id")
        else:
            if "name" in s and not isinstance(s["name"], str):
                problems.append("sender.name: должна быть строкой")
            if "is_owner" in s and not isinstance(s["is_owner"], bool):
                problems.append("sender.is_owner: должно быть true или false")
    if "reply_channel" in body:
        r = body["reply_channel"]
        if not isinstance(r, dict) or not isinstance(r.get("module"), str) or not isinstance(r.get("target"), str):
            problems.append("reply_channel: объект со строками module и target")
    if "dedupe_key" in body and not isinstance(body["dedupe_key"], str):
        problems.append("dedupe_key: должна быть строкой")
    if "trace" in body:                                   # ТЗ 24: необязательная трасса задержки, ядро её не разбирает
        t = body["trace"]
        if not isinstance(t, dict) or not isinstance(t.get("id"), str) or not t["id"]:
            problems.append("trace: объект с непустой строкой id")
    _check_expires("", body, problems)
    return problems


def validate_say(body: Any) -> list[str]:
    """Проверить тело `say` (раздел 7.2)."""
    if not isinstance(body, dict):
        return ["say: тело должно быть объектом"]
    problems: list[str] = []
    if not isinstance(body.get("text"), str) or not body["text"].strip():
        problems.append("text: обязательная непустая строка")
    if body.get("mode", "fact") not in SAY_MODES:
        problems.append(f"mode: одно из {list(SAY_MODES)}")
    for key in ("emotion", "key"):
        if key in body and not isinstance(body[key], str):
            problems.append(f"{key}: должна быть строкой")
    if "announce" in body and not isinstance(body["announce"], bool):
        problems.append("announce: должно быть true или false")
    _check_urgency("", body, problems)
    _check_expires("", body, problems)
    return problems


def validate_ask(body: Any, *, question_id: bool = False) -> list[str]:
    """Проверить тело `core.ask` (раздел 7.3); с question_id=True проверяет вопрос задачи (MOD-20)."""
    if not isinstance(body, dict):
        return ["вопрос должен быть объектом"]
    problems: list[str] = []
    kind = body.get("kind")
    if kind not in ASK_KINDS:
        problems.append(f"kind: одно из {list(ASK_KINDS)}")
    if not isinstance(body.get("question"), str) or not body["question"].strip():
        problems.append("question: обязательная непустая строка")
    options = body.get("options")
    if kind == "choice" and (not _is_str_list(options) or not options):
        problems.append("options: для choice нужен непустой список строк")
    elif options is not None and not _is_str_list(options):
        problems.append("options: должен быть списком строк")
    if "timeout_s" in body:
        t = body["timeout_s"]
        if not _is_num(t) or not 1 <= t <= ASK_TIMEOUT_MAX:
            problems.append(f"timeout_s: число секунд 1–{ASK_TIMEOUT_MAX}")
    if body.get("context") is not None and not isinstance(body["context"], dict):
        problems.append("context: должен быть объектом")
    _check_urgency("", body, problems)
    if question_id and (not isinstance(body.get("question_id"), str) or not body["question_id"]):
        problems.append("question_id: обязательная непустая строка")
    return problems


def validate_job_update(body: Any) -> list[str]:
    """Проверить тело `job.update` (раздел 9, MOD-19…MOD-21)."""
    if not isinstance(body, dict):
        return ["job.update: тело должно быть объектом"]
    problems: list[str] = []
    job_id = body.get("job_id")
    if not isinstance(job_id, str) or not JOB_ID_RE.match(job_id):
        problems.append("job_id: вид j-xxxxxxxx (8 hex)")
    state = body.get("state")
    if state not in JOB_STATES:
        problems.append(f"state: одно из {list(JOB_STATES)}")
    for key in ("title", "stage", "text"):
        if key in body and body[key] is not None and not isinstance(body[key], str):
            problems.append(f"{key}: должна быть строкой")
    if body.get("progress") is not None:
        p = body["progress"]
        if not _is_num(p) or not 0 <= p <= 1:
            problems.append("progress: число 0–1")
    if state == "waiting_user":
        q = body.get("question")
        if not isinstance(q, dict):
            problems.append("question: в состоянии waiting_user обязателен вопрос (MOD-20)")
        else:
            problems.extend(f"question.{p}" for p in validate_ask(q, question_id=True))
    if body.get("artifacts") is not None:
        arts = body["artifacts"]
        if not isinstance(arts, list) or not all(
                isinstance(a, dict) and isinstance(a.get("path"), str) and a["path"] for a in arts):
            problems.append("artifacts: список объектов с непустой строкой path")
    if state == "failed":
        err = body.get("error")
        if not isinstance(err, dict) or err.get("code") not in ERROR_CODES or not isinstance(err.get("message"), str):
            problems.append("error: для failed обязателен {code (MOD-9), message} (MOD-21)")
    return problems


def _drop_none(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


@dataclass
class Event:
    """Событие модуля (раздел 7.1)."""

    kind: str
    text: str
    data: dict | None = None
    urgency_hint: int | None = None
    addressed_to_me: bool | None = None
    sender: dict | None = None
    reply_channel: dict | None = None
    dedupe_key: str | None = None
    expires_in_s: float | None = None
    memorable: bool | None = None
    announce: bool | None = None      # объявление о долгом запуске (AN-2)

    @classmethod
    def from_body(cls, body: Any) -> Event:
        problems = validate_event(body)
        if problems:
            raise ProtocolError("event: " + "; ".join(problems), problems)
        return cls(**{k: body.get(k) for k in cls.__dataclass_fields__})

    def to_body(self) -> dict:
        return _drop_none({k: getattr(self, k) for k in self.__dataclass_fields__})


@dataclass
class Say:
    """Просьба модуля сказать (раздел 7.2)."""

    text: str
    mode: str = "fact"
    emotion: str | None = None
    urgency_hint: int | None = None
    expires_in_s: float | None = None
    key: str | None = None
    announce: bool | None = None      # объявление о долгом запуске: звучит всегда (AN-2)

    @classmethod
    def from_body(cls, body: Any) -> Say:
        problems = validate_say(body)
        if problems:
            raise ProtocolError("say: " + "; ".join(problems), problems)
        return cls(**{k: body.get(k, getattr(cls, k, None)) for k in cls.__dataclass_fields__})

    def to_body(self) -> dict:
        return _drop_none({k: getattr(self, k) for k in self.__dataclass_fields__})


@dataclass
class JobUpdate:
    """Ход задачи (раздел 9); он же JobStatus в ответе на `job.list`."""

    job_id: str
    state: str
    title: str | None = None
    stage: str | None = None
    progress: float | None = None
    text: str | None = None
    question: dict | None = None
    artifacts: list | None = None
    error: dict | None = None

    @property
    def finished(self) -> bool:
        return self.state in JOB_FINISHED

    @classmethod
    def from_body(cls, body: Any) -> JobUpdate:
        problems = validate_job_update(body)
        if problems:
            raise ProtocolError("job.update: " + "; ".join(problems), problems)
        return cls(**{k: body.get(k) for k in cls.__dataclass_fields__})

    def to_body(self) -> dict:
        return _drop_none({k: getattr(self, k) for k in self.__dataclass_fields__})

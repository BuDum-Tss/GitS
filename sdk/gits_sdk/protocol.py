"""Протокол «ядро ↔ модуль» (ТЗ 01, docs/tz/01-module-contract.md): константы, конверт, манифест, проверки.

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

PROTOCOL = 1
DEFAULT_CORE_URL = "ws://127.0.0.1:8766/module/ws"
ENV_CORE_URL = "GITS_CORE_URL"
ENV_TOKEN = "GITS_MODULE_TOKEN"
MAX_MESSAGE_BYTES = 1024 * 1024        # MOD-6: файлы передаются путями, не внутри сообщений
HEARTBEAT_S = 10                       # MOD-5: встроенный пинг WebSocket (aiohttp heartbeat)
PEER_TIMEOUT_S = 30                    # нет ответа столько секунд: соединение разорвано
RECONNECT_DELAYS = (1, 2, 4, 8, 16, 30)  # MOD-7: дальше без конца по 30 с
CLOSE_REPLACED = 4000                  # MOD-13: код закрытия «вытеснен новым подключением»
EXIT_REJECTED = 2                      # MOD-12: код завершения процесса модуля после reject про протокол/манифест
CORE_HOST_ALLOWED = ("127.0.0.1", "::1")   # MOD-3: ядро принимает только петлю


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

ERROR_CODES = ("bad_request", "unknown", "denied", "unavailable", "timeout", "cancelled", "internal")


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
MODULE_TO_CORE_NOTES = ("hello", "event", "say", "emotion", "state", "job.update", "result", "panel.set")
CORE_REQUESTS = (
    "core.ask", "core.tts", "core.tool.call", "core.lookup", "core.memory.recall", "core.memory.remember",
    "core.experience.add", "core.kb.ingest", "core.persona", "core.privacy_terms", "core.claude_usage",
    "core.web_search",
)
MODULE_TO_CORE = frozenset(MODULE_TO_CORE_NOTES) | frozenset(CORE_REQUESTS)

# ядро -> модуль
CORE_TO_MODULE_REQUESTS = ("tool.call", "tool.check", "job.cancel", "job.list", "graph.get", "deliver",
                           "tts.synthesize", "panel.action",
                           # менеджер фокуса (ТЗ 18, FOC-4): переходы между политиками модуля
                           "policy.activate", "focus.release", "focus.resume")
CORE_TO_MODULE_NOTES = ("welcome", "reject", "job.answer", "speak", "speak.stop", "topic", "shutdown", "result")
CORE_TO_MODULE = frozenset(CORE_TO_MODULE_REQUESTS) | frozenset(CORE_TO_MODULE_NOTES)

REQUEST_TYPES = frozenset(CORE_REQUESTS) | frozenset(CORE_TO_MODULE_REQUESTS)


def is_request(msg_type: str) -> bool:
    """Ждёт ли сообщение такого типа ответа `result` (MOD-8)."""
    return msg_type in REQUEST_TYPES


TOPICS = ("presence", "settings", "profile", "module", "battery", "character")   # character: ТЗ 05, CHR-6
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

MODULE_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,47}$")
SETTING_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
PARAM_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
JOB_ID_RE = re.compile(r"^j-[0-9a-f]{8}$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
KIND_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}\.[a-z0-9_][a-z0-9_.]*$")
POLICY_FOCUS = ("active", "background")        # ТЗ 18, FOC-1: active держит фокус, background - фон (вне фокуса)
POLICY_RESOURCES = ("gpu", "cpu", "tokens")    # ресурсы политики (необязательные числа >= 0)

DESCRIPTION_MAX = 300
EVENT_TEXT_MAX = 1000
STATE_SUMMARY_MAX = 300
TIMEOUT_TOOL_MIN, TIMEOUT_TOOL_MAX, TIMEOUT_TOOL_DEFAULT = 1, 120, 30
ASK_TIMEOUT_DEFAULT, ASK_TIMEOUT_MAX = 300, 86400
URGENCY_MIN, URGENCY_MAX = 0, 5

# пространство модуля в дашборде (TZ 15, поле panel и сообщение panel.set)
PANEL_TITLE_MAX, PANEL_ICON_MAX = 60, 40
PANEL_ORDER_DEFAULT = 100
PANEL_LINKS_MAX, PANEL_ACTIONS_MAX, PANEL_HOSTS_MAX, PANEL_DIRS_MAX = 12, 40, 10, 10
PANEL_LINK_KINDS = ("url", "tool", "reveal_path")
PANEL_ARGS_FROM = ("job",)
PANEL_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")          # id ссылок, действий и вкладок
PANEL_BLOCK_TYPES = ("stats", "table", "list", "text", "progress", "buttons", "link", "image", "video", "log", "tabs")
PANEL_BUTTON_STYLES = ("default", "primary", "danger")
PANEL_SET_MAX_BYTES = 64 * 1024              # panel.set: тело целиком (ТЗ 15, раздел 3)
PANEL_SET_MIN_INTERVAL_S = 1.0               # не чаще раза в секунду на модуль (ядро отбрасывает лишнее)
PANEL_BLOCKS_MAX, PANEL_ITEMS_MAX, PANEL_ROWS_MAX, PANEL_LINES_MAX = 40, 50, 200, 200
PANEL_TABS_MAX, PANEL_DEPTH_MAX = 6, 2       # вложенность блоков «tabs»: не глубже двух уровней
PANEL_TEXT_MAX, PANEL_CELL_MAX, PANEL_LINE_MAX = 4000, 500, 500
PANEL_LABEL_MAX = 80
PANEL_ACTION_RESULT_MESSAGE_MAX = 300


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
    for flag in ("informative", "self_confirming", "danger_check", "job", "fallback", "announce"):
        if flag in tool and not isinstance(tool[flag], bool):
            problems.append(f"{where}.{flag}: должно быть true или false")
    if "timeout_s" in tool:
        t = tool["timeout_s"]
        if not _is_int(t) or not TIMEOUT_TOOL_MIN <= t <= TIMEOUT_TOOL_MAX:
            problems.append(f"{where}.timeout_s: целое {TIMEOUT_TOOL_MIN}–{TIMEOUT_TOOL_MAX}")
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
        problems.append(f"{where}.secret: секретные настройки запрещены, секреты модуль читает сам из secrets.env")
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


def validate_manifest(manifest: Any, warnings: list[str] | None = None) -> list[str]:
    """Проверить манифест по MOD-15/MOD-16 и разделу 6.1. Возвращает список проблем (пустой = верно).

    Поле `panel` (TZ 15) не отклоняет модуль: его проблемы уходят в `warnings`, если список передан (модуль работает без
    пространства). Без `warnings` проблемы panel не возвращаются вовсе."""
    if not isinstance(manifest, dict):
        return ["манифест должен быть объектом"]
    problems: list[str] = []

    mid = manifest.get("id")
    id_ok = isinstance(mid, str) and bool(MODULE_ID_RE.match(mid))
    if not id_ok:
        problems.append(f"id: должен соответствовать {MODULE_ID_RE.pattern}")
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
                    if id_ok and not kind.startswith(mid + "."):
                        problems.append(f"{where}: kind должен начинаться с «{mid}.»")
                    if kind in kinds:
                        problems.append(f"{where}: kind повторяется")
                    kinds.add(kind)
                if not isinstance(emit.get("description"), str) or not emit["description"].strip():
                    problems.append(f"{where}.description: обязательная непустая строка")
                urgency = emit.get("default_urgency")
                if not _is_int(urgency) or not URGENCY_MIN <= urgency <= URGENCY_MAX:
                    problems.append(f"{where}.default_urgency: целое {URGENCY_MIN}–{URGENCY_MAX}")

    if "knowledge" in manifest:
        knowledge = manifest["knowledge"]
        if not isinstance(knowledge, list):
            problems.append("knowledge: должен быть списком")
        else:
            names: set[str] = set()
            for i, coll in enumerate(knowledge):
                where = f"knowledge[{i}]"
                if not isinstance(coll, dict):
                    problems.append(f"{where}: должен быть объектом")
                    continue
                for key in ("collection", "title", "scope", "kind"):
                    if not isinstance(coll.get(key), str) or not coll[key].strip():
                        problems.append(f"{where}.{key}: обязательная непустая строка")
                if isinstance(coll.get("collection"), str):
                    if coll["collection"] in names:
                        problems.append(f"{where}.collection: «{coll['collection']}» повторяется")
                    names.add(coll["collection"])

    for flag in ("graph", "state"):
        if flag in manifest and not isinstance(manifest[flag], bool):
            problems.append(f"{flag}: должно быть true или false")
    _check_policies(manifest, problems)
    if "panel" in manifest and warnings is not None:
        warnings.extend(parse_panel(manifest)[1])
    return problems


def _check_policies(manifest: dict, problems: list[str]) -> None:
    """ТЗ 18, FOC-1: политики модуля. Без `policies` модуль работает как раньше (режим совместимости, без фокуса).
    С `policies` обязательны `default_policy` (фоновая политика) и минимум одна политика с focus background."""
    if "policies" not in manifest:
        if "default_policy" in manifest:
            problems.append("default_policy: задано без policies")
        return
    policies = manifest["policies"]
    if not isinstance(policies, list) or not policies:
        problems.append("policies: непустой список политик")
        return
    ids: list[str] = []
    for i, pol in enumerate(policies):
        where = f"policies[{i}]"
        if not isinstance(pol, dict):
            problems.append(f"{where}: должна быть объектом")
            continue
        pid = pol.get("id")
        if not isinstance(pid, str) or not MODULE_ID_RE.match(pid):
            problems.append(f"{where}.id: строка по шаблону {MODULE_ID_RE.pattern}")
        elif pid in ids:
            problems.append(f"{where}.id: «{pid}» повторяется")
        else:
            ids.append(pid)
        if not isinstance(pol.get("title"), str) or not pol["title"].strip():
            problems.append(f"{where}.title: обязательная непустая строка")
        if pol.get("focus") not in POLICY_FOCUS:
            problems.append(f"{where}.focus: одно из {list(POLICY_FOCUS)}")
        for key in ("transitions", "safe_points"):
            if key in pol and not _is_str_list(pol[key]):
                problems.append(f"{where}.{key}: список строк")
        for key in ("interruptible", "quiet"):
            if key in pol and not isinstance(pol[key], bool):
                problems.append(f"{where}.{key}: true или false")
        resources = pol.get("resources", {})
        if not isinstance(resources, dict):
            problems.append(f"{where}.resources: объект")
        else:
            for key, val in resources.items():
                if key not in POLICY_RESOURCES or not _is_num(val) or val < 0:
                    problems.append(f"{where}.resources.{key}: допустимы {list(POLICY_RESOURCES)} с числом от 0")
    for i, pol in enumerate(policies):
        if isinstance(pol, dict) and _is_str_list(pol.get("transitions", [])):
            for target in pol.get("transitions", []):
                if target not in ids:
                    problems.append(f"policies[{i}].transitions: политики «{target}» нет в policies")
    if not any(isinstance(p, dict) and p.get("focus") == "background" for p in policies):
        problems.append("policies: нужна минимум одна политика с focus background (ТЗ 18, FOC-1)")
    default = manifest.get("default_policy")
    if not isinstance(default, str) or default not in ids:
        problems.append("default_policy: id одной из политик в policies")
    else:
        spec = next(p for p in policies if isinstance(p, dict) and p.get("id") == default)
        if spec.get("focus") != "background":
            problems.append("default_policy: политика по умолчанию должна быть фоновой (focus background)")


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

    def full_id(self, module_id: str) -> str:
        return f"{module_id}.{self.name}"

    @classmethod
    def from_dict(cls, d: dict) -> ToolSpec:
        return cls(
            name=d["name"], description=d["description"], params=dict(d["params"]), risk=d["risk"],
            availability=d["availability"], informative=bool(d.get("informative", False)),
            self_confirming=bool(d.get("self_confirming", False)),
            timeout_s=int(d.get("timeout_s", TIMEOUT_TOOL_DEFAULT)), keywords=list(d.get("keywords") or []),
            danger_check=bool(d.get("danger_check", False)), job=bool(d.get("job", False)),
            fallback=bool(d.get("fallback", False)), core_fills=list(d.get("core_fills") or []),
            announce=bool(d.get("announce", False)))

    def to_dict(self) -> dict:
        return {"name": self.name, "description": self.description, "params": self.params, "risk": self.risk,
                "availability": self.availability, "informative": self.informative,
                "self_confirming": self.self_confirming, "timeout_s": self.timeout_s, "keywords": self.keywords,
                "danger_check": self.danger_check, "job": self.job, "fallback": self.fallback,
                "core_fills": self.core_fills, "announce": self.announce}


@dataclass
class EmitSpec:
    """Событие, которое объявляет модуль."""

    kind: str
    description: str
    default_urgency: int


@dataclass
class KnowledgeSpec:
    """Коллекция справочника, которой владеет модуль (ТЗ 04)."""

    collection: str
    title: str
    scope: str
    kind: str


@dataclass
class PolicySpec:
    """Политика модуля (ТЗ 18, FOC-1). `quiet` - в политике обычные события не переключают фокус (режим «не беспокоить»);
    `transitions` пустой - переходы не ограничены."""

    id: str
    title: str
    focus: str
    purpose: str = ""
    transitions: list[str] = field(default_factory=list)
    interruptible: bool = False
    safe_points: list[str] = field(default_factory=list)
    resources: dict = field(default_factory=dict)
    quiet: bool = False

    @classmethod
    def from_dict(cls, d: dict) -> PolicySpec:
        return cls(id=d["id"], title=d["title"], focus=d["focus"], purpose=str(d.get("purpose") or ""),
                   transitions=list(d.get("transitions") or []), interruptible=bool(d.get("interruptible", False)),
                   safe_points=list(d.get("safe_points") or []), resources=dict(d.get("resources") or {}),
                   quiet=bool(d.get("quiet", False)))

    def to_dict(self) -> dict:
        out = {"id": self.id, "title": self.title, "focus": self.focus, "transitions": self.transitions,
               "interruptible": self.interruptible, "safe_points": self.safe_points, "resources": self.resources}
        if self.purpose:
            out["purpose"] = self.purpose
        if self.quiet:
            out["quiet"] = True
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
    "video": _check_video, "log": _check_log, "tabs": _check_tabs,
}


def _check_block(where: str, b: Any, actions: set[str] | None, depth: int) -> list[str]:
    if not isinstance(b, dict):
        return [f"{where}: блок должен быть объектом"]
    kind = b.get("type")
    if kind not in PANEL_BLOCK_TYPES:
        return [f"{where}.type: одно из {list(PANEL_BLOCK_TYPES)}"]
    problems = [p for p in [_text_problem(where, b, "title", 120, required=False)] if p]
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
    knowledge: list[KnowledgeSpec] = field(default_factory=list)
    graph: bool = False
    state: bool = False
    panel: PanelSpec | None = None                      # пространство модуля (TZ 15); None - вкладки нет
    panel_warnings: list[str] = field(default_factory=list)   # почему panel отброшен (для журнала ядра)
    policies: list[PolicySpec] = field(default_factory=list)  # ТЗ 18, FOC-1; пусто - режим совместимости (без фокуса)
    default_policy: str | None = None

    @classmethod
    def from_dict(cls, d: Any) -> Manifest:
        warnings: list[str] = []
        problems = validate_manifest(d, warnings)
        if problems:
            raise ManifestError("манифест не прошёл проверку: " + "; ".join(problems), problems)
        panel, panel_warnings = parse_panel(d)
        return cls(
            id=d["id"], title=d["title"], description=d["description"], version=d["version"],
            capabilities=list(d.get("capabilities") or []), subscribe=list(d.get("subscribe") or []),
            shared_settings=list(d.get("shared_settings") or []), settings_schema=dict(d.get("settings_schema") or {}),
            tools=[ToolSpec.from_dict(t) for t in d.get("tools") or []],
            emits=[EmitSpec(e["kind"], e["description"], e["default_urgency"]) for e in d.get("emits") or []],
            knowledge=[KnowledgeSpec(k["collection"], k["title"], k["scope"], k["kind"])
                       for k in d.get("knowledge") or []],
            graph=bool(d.get("graph", False)), state=bool(d.get("state", False)),
            panel=panel, panel_warnings=panel_warnings,
            policies=[PolicySpec.from_dict(p) for p in d.get("policies") or []],
            default_policy=d.get("default_policy"))

    def to_dict(self) -> dict:
        out = {"id": self.id, "title": self.title, "description": self.description, "version": self.version,
               "capabilities": self.capabilities, "subscribe": self.subscribe,
               "shared_settings": self.shared_settings, "settings_schema": self.settings_schema,
               "tools": [t.to_dict() for t in self.tools],
               "emits": [{"kind": e.kind, "description": e.description, "default_urgency": e.default_urgency}
                         for e in self.emits],
               "knowledge": [{"collection": k.collection, "title": k.title, "scope": k.scope, "kind": k.kind}
                             for k in self.knowledge],
               "graph": self.graph, "state": self.state}
        if self.panel is not None:
            out["panel"] = self.panel.to_dict()
        if self.policies:
            out["policies"] = [p.to_dict() for p in self.policies]
            out["default_policy"] = self.default_policy
        return out

    def tool(self, name: str) -> ToolSpec | None:
        return next((t for t in self.tools if t.name == name), None)

    def policy(self, pid: str) -> PolicySpec | None:
        return next((p for p in self.policies if p.id == pid), None)

    def setting_defaults(self) -> dict:
        """Значения собственных настроек по умолчанию (для ядра: `modules.<id>.<имя>`)."""
        return {name: spec["default"] for name, spec in self.settings_schema.items()}


# ---------------------------------------------------------------- рукопожатие (раздел 5)

def hello_body(manifest: dict, instance: str, pid: int | None = None) -> dict:
    """Тело `hello`."""
    return {"module_id": manifest["id"], "instance": instance, "pid": os.getpid() if pid is None else pid,
            "protocol": PROTOCOL, "manifest": manifest}


def validate_hello(body: Any) -> list[str]:
    """Проверить тело `hello` вместе с манифестом (для ядра: при проблемах отвечает `reject`)."""
    if not isinstance(body, dict):
        return ["hello: тело должно быть объектом"]
    problems = []
    if body.get("protocol") != PROTOCOL or isinstance(body.get("protocol"), bool):
        problems.append(f"protocol {body.get('protocol')!r} не поддерживается (нужен {PROTOCOL})")
    mid = body.get("module_id")
    if not isinstance(mid, str) or not MODULE_ID_RE.match(mid):
        problems.append(f"module_id: должен соответствовать {MODULE_ID_RE.pattern}")
    if not isinstance(body.get("instance"), str) or not body["instance"]:
        problems.append("instance: обязательная непустая строка")
    if not _is_int(body.get("pid")):
        problems.append("pid: целое число")
    manifest = body.get("manifest")
    problems.extend(validate_manifest(manifest))
    if isinstance(manifest, dict) and manifest.get("id") != mid:
        problems.append("module_id не совпадает с manifest.id")
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

"""Супервизор процессов участников шины (ТЗ 02, раздел 7): запускает записи из `agent/peers.yaml` (разделы `workspaces:` и
`services:`) и следит за ними.

Участник включён, если пользователь его не выключил (настройка `peers.<id>.enabled`); включённый с
`autostart: true` запускается вместе с ядром. Падение процесса перезапускается по политике `restart` с задержками
2, 5, 15, 60 с; пять падений за десять минут - и супервизор перестаёт, помечая модуль «остановлен после сбоев» (с хвостом
вывода без токенов). Выключение: модулю уходит `shutdown`, через 10 с процесс убивается вместе с потомками. Вручную
запущенный модуль (при разработке) принимается как обычный: пока он подключён, второй экземпляр не запускается.

Внешний сервис (ТЗ 03, ATT-2): запись `kind: service` с `health_url` - процесс без связи по WebSocket (первый: `decider`, классификатор
внимания). Супервизор считает его работающим, пока `GET health_url` отвечает 200 (опрос раз в 10 с), перезапускает по той же
политике `restart`, а если порт уже занят работающим сервисом (запущен не супервизором), второй экземпляр не запускает и только
следит за здоровьем. Токен ядра сервису не передаётся.

Вывод процесса (stdout и stderr вместе) пишется в `agent/logs/modules/<id>.log` (5 МБ, три файла). Токен ядра процессу
передаётся в `GITS_TOKEN`, id в `GITS_ID`, в журналы он не попадает: похожие на токены места в выводе вырезаются. Модулю также
передаётся `MEMORY_DIR`: абсолютный путь к папке памяти ядра (модули хранят свои данные там же, ТЗ 10).
"""
import asyncio
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Awaitable, Callable

import psutil
import yaml

import events_log

logger = logging.getLogger("supervisor")

AGENT_DIR = Path(__file__).resolve().parent
GITS_ROOT = AGENT_DIR.parent
PEERS_YAML = AGENT_DIR / "peers.yaml"
ENV_PEERS_FILE = "GITS_PEERS_FILE"           # другой файл участников (контейнер: agent/peers.container.yaml); относительный путь от корня репозитория
LOGS_DIR = AGENT_DIR / "logs" / "modules"

RESTART_DELAYS = (2, 5, 15, 60)       # CORE-15
CRASH_LIMIT, CRASH_WINDOW_S = 5, 600  # пять падений за десять минут - перестаём
SHUTDOWN_WAIT_S = 10.0                # CORE-14, CORE-17
TAIL_LINES = 20
LOG_BYTES, LOG_FILES = 5 * 1024 * 1024, 3
POLL_MANUAL_S = 5.0                   # как часто смотрим, не отключился ли вручную запущенный модуль
HEALTH_INTERVAL_S = 10.0              # ATT-2: опрос health_url сервиса
HEALTH_TIMEOUT_S = 2.0
KINDS = ("service",)                    # kind в записи (необязателен): service - внешний процесс без WebSocket (health_url)
OWNERSHIPS = ("shared", "owned")
SECTIONS = (("workspaces", "workspace"), ("services", "service"))   # раздел файла -> роль
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
RESTART_POLICIES = ("always", "on-failure", "never")

_VAR = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
# похожее на токен: длинный hex (токен ядра - 64 hex) или Bearer-заголовок
_SECRET = re.compile(r"[0-9a-fA-F]{32,}|Bearer\s+\S+", re.I)


@dataclass
class PeerSpec:
    id: str
    cwd: str
    command: str
    args: list[str] = field(default_factory=list)
    autostart: bool = True
    restart: str = "on-failure"
    enabled: bool = True             # включён ли, пока в настройках нет явного выбора
    title: str = ""
    role: str = "workspace"          # workspace | service (раздел файла; манифест подключившегося участника главнее)
    ownership: str | None = None     # shared | owned (только у workspace; манифест главнее)
    external: bool = False           # внешний сервис без WebSocket (`kind: service`): жив по health_url (ATT-2)
    health_url: str = ""
    description: str = ""            # подпись для дашборда: перекрывает описание из манифеста подключившегося модуля
    dev: bool = False                # для разработчиков: вкладки показывают запись только при настройке dev_mode
    unmanaged: bool = False          # workspace запускает не супервизор (compose: контейнер); `external: true` в peers.yaml
    control_url: str = ""            # общее пространство с интерфейсом управления: адрес (http://montage:8780), аренда вместо запуска
    control_token_env: str = "GITS_CONTROL_TOKEN"      # имя переменной окружения с токеном управления (по умолчанию GITS_CONTROL_TOKEN)
    skip_when_env: dict[str, list[str]] = field(default_factory=dict)   # не запускать, если переменная окружения равна одному из значений


@dataclass
class PlannedPeer:
    """Участник, которого ещё нет (раздел `planned:` в peers.yaml): дашборд показывает серую карточку, супервизор не трогает."""
    id: str
    title: str
    tz: str = ""


def peers_file(env: dict | None = None, gits_root: Path = GITS_ROOT) -> Path:
    """Файл участников: `GITS_PEERS_FILE` (относительный путь считается от
    корня репозитория) или `agent/peers.yaml`."""
    source = os.environ if env is None else env
    raw = str(source.get(ENV_PEERS_FILE) or "").strip()
    if not raw:
        return PEERS_YAML
    path = Path(raw)
    return path if path.is_absolute() else Path(gits_root) / path


def load_planned(path: Path | str | None = None) -> list[PlannedPeer]:
    """Запланированные участники из peers.yaml. Неверная запись пропускается с предупреждением."""
    path = peers_file() if path is None else path
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        logger.warning(f"peers.yaml не прочитан: {e}")
        return []
    rows: list[PlannedPeer] = []
    for i, raw in enumerate((data.get("planned") if isinstance(data, dict) else None) or []):
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not raw.get("title"):
            logger.warning(f"peers.yaml, planned, запись {i + 1}: нужен объект с id и title")
            continue
        rows.append(PlannedPeer(id=raw["id"], title=str(raw["title"]), tz=str(raw.get("tz") or "")))
    return rows


def _skip_when_env(raw) -> dict[str, list[str]]:
    """`skip_when_env: {ИМЯ: [значения]}` в peers.yaml: запись не запускается, если переменная равна одному из значений."""
    if raw is None:
        return {}
    if not isinstance(raw, dict) or not all(isinstance(v, list) for v in raw.values()):
        raise ValueError("skip_when_env: объект {ИМЯ: [значения]}")
    return {str(k): [str(x).strip().lower() for x in v] for k, v in raw.items()}


def _entry(raw: dict, role_hint: str, section: str) -> PeerSpec:
    """Одна запись файла -> PeerSpec. Роль задаёт раздел (`workspaces:` / `services:`). Внешний сервис без WebSocket это `kind: service` с `health_url`."""
    restart = raw.get("restart", "on-failure")
    if restart not in RESTART_POLICIES:
        raise ValueError(f"restart: одно из {list(RESTART_POLICIES)}")
    args = raw.get("args") or []
    if not isinstance(args, list):
        raise ValueError("args: список строк")
    kind = raw.get("kind")
    if kind is not None and kind not in KINDS:
        raise ValueError(f"kind: одно из {list(KINDS)}")
    external = kind == "service"
    health_url = str(raw.get("health_url") or "")
    if external and not health_url.startswith(("http://", "https://")):
        raise ValueError("для kind: service нужен health_url (http:// или https://)")
    role = role_hint
    ownership = raw.get("ownership")
    if role == "workspace":
        ownership = ownership or None
        if ownership is not None and ownership not in OWNERSHIPS:
            raise ValueError(f"ownership: одно из {list(OWNERSHIPS)}")
    elif ownership is not None:
        raise ValueError("ownership допустим только у workspace")
    unmanaged = False
    control_url = str(raw.get("control_url") or "")
    if control_url and not control_url.startswith(("http://", "https://")):
        raise ValueError("control_url: адрес http:// или https://")
    if role == "workspace" and raw.get("external") is True:
        unmanaged = True
        if ownership != "shared":
            raise ValueError("external: true допустим только у workspace с ownership: shared")
    if control_url and ownership != "shared":
        raise ValueError("control_url допустим только у workspace с ownership: shared")
    if "command" not in raw and not unmanaged:
        raise KeyError("command")
    return PeerSpec(id=raw["id"], cwd=str(raw.get("cwd") or "."), command=str(raw.get("command") or ""),
                    unmanaged=unmanaged, control_url=control_url,
                    control_token_env=str(raw.get("control_token_env") or "GITS_CONTROL_TOKEN"),
                    args=[str(a) for a in args], autostart=bool(raw.get("autostart", True)),
                    restart=restart, enabled=bool(raw.get("enabled", True)),
                    title=str(raw.get("title") or ""), role=role, ownership=ownership, external=external,
                    health_url=health_url, description=str(raw.get("description") or ""), dev=bool(raw.get("dev", False)),
                    skip_when_env=_skip_when_env(raw.get("skip_when_env")))


def load_specs(path: Path | str | None = None) -> list[PeerSpec]:
    """Участники из peers.yaml (разделы `workspaces:` и `services:`). Неверная запись
    пропускается с предупреждением."""
    path = peers_file() if path is None else path
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        logger.warning(f"peers.yaml не прочитан: {e}")
        return []
    specs: list[PeerSpec] = []
    seen: set[str] = set()
    for section, role_hint in SECTIONS:
        for i, raw in enumerate((data.get(section) if isinstance(data, dict) else None) or []):
            try:
                if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
                    raise ValueError("нужен объект с id")
                if raw["id"] in seen:
                    raise ValueError(f"id «{raw['id']}» уже есть в файле")
                specs.append(_entry(raw, role_hint, section))
                seen.add(raw["id"])
            except (KeyError, ValueError) as e:
                logger.warning(f"peers.yaml, {section}, запись {i + 1}: {e}")
    return specs


def redact(line: str) -> str:
    return _SECRET.sub("***", line)


def kill_tree(pid: int) -> None:
    """Остановить процесс вместе с потомками (как tray._kill_tree), оставшихся добить."""
    try:
        parent = psutil.Process(pid)
        procs = parent.children(recursive=True) + [parent]
    except psutil.Error:
        return
    for p in procs:
        try:
            p.terminate()
        except psutil.Error:
            pass
    _gone, alive = psutil.wait_procs(procs, timeout=3)
    for p in alive:
        try:
            p.kill()
        except psutil.Error:
            pass


class _LogSink:
    """Файл вывода модуля с ротацией; пишется пачками через поток, чтобы не блокировать цикл событий."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._handler = RotatingFileHandler(path, maxBytes=LOG_BYTES, backupCount=LOG_FILES - 1, encoding="utf-8")
        self._buffer: list[str] = []

    def add(self, line: str) -> None:
        self._buffer.append(line)

    def _flush(self, lines: list[str]) -> None:
        for line in lines:
            self._handler.handle(logging.LogRecord("module", logging.INFO, "", 0, line, None, None))

    async def flush(self) -> None:
        if self._buffer:
            lines, self._buffer = self._buffer, []
            await asyncio.to_thread(self._flush, lines)

    def close(self) -> None:
        self._handler.close()


class _Proc:
    """Состояние одного модуля у супервизора."""

    def __init__(self, spec: PeerSpec):
        self.spec = spec
        self.status = "stopped"         # stopped | starting | running | backoff | crashed | not_configured | manual
        self.text = "остановлен"
        self.proc: asyncio.subprocess.Process | None = None
        self.task: asyncio.Task | None = None
        self.desired = False
        self.failures: deque[float] = deque()
        self.restarts = 0
        self.last_error = ""
        self.reason = ""
        self.tail: deque[str] = deque(maxlen=TAIL_LINES)
        self.sink: _LogSink | None = None
        self.healthy = False            # сервис: последний опрос health_url дал 200
        self.external = False           # сервис работает, но запущен не супервизором

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None


class Supervisor:
    _only_core = staticmethod(lambda: False)         # настройка «только ядро»; в __init__ заменяется на источник из ядра

    def __init__(self, specs: list[PeerSpec], *, token: str, core_url: str,
                 enabled: Callable[[str], bool] = lambda peer_id: True,
                 is_connected: Callable[[str], bool] = lambda peer_id: False,
                 send_shutdown: Callable[[str], Awaitable[bool]] | None = None,
                 gits_root: Path = GITS_ROOT, agent_dir: Path = AGENT_DIR, logs_dir: Path = LOGS_DIR,
                 python: str = sys.executable, env: dict | None = None,
                 sleep: Callable[[float], Awaitable] = asyncio.sleep, clock: Callable[[], float] = time.monotonic,
                 shutdown_wait: float = SHUTDOWN_WAIT_S, poll_manual: float = POLL_MANUAL_S,
                 pythonpath: list[str] | None = None, memory_dir: Path | str | None = None,
                 probe: Callable[[str], Awaitable[bool]] | None = None, health_interval: float = HEALTH_INTERVAL_S,
                 announce: Callable[[str, str], Awaitable[None]] | None = None,
                 only_core: Callable[[], bool] = lambda: False):
        """enabled(id) - включён ли модуль (PeerRegistry.is_enabled); is_connected(id) - подключён ли он сейчас;
        send_shutdown(id) - послать модулю shutdown; sleep и clock подменяются в тестах (задержки перезапуска);
        pythonpath - что добавить в PYTHONPATH процессов (до установки пакета gits-sdk это GitS/sdk);
        memory_dir - папка памяти ядра: модулям уходит как MEMORY_DIR (абсолютным путём; их рабочая папка другая);
        probe(url) -> отвечает ли сервис 200 (в тестах подменяется); health_interval - период опроса сервисов;
        announce(id, text) - объявление о запуске процесса модуля (правило 14: включили, перезапуск по кнопке, перезапуск
        после сбоя); текст - факт, фразу строит модель. Без него объявлений нет (тесты, скрипты)."""
        self.specs = {s.id: s for s in specs}
        self._only_core = only_core                 # настройка «только ядро» (ТЗ 17, FLOW-1); как GITS_NO_MODULES=1, при старте
        self._announce = announce
        self.token, self.core_url = token, core_url
        self._enabled, self._is_connected, self._send_shutdown = enabled, is_connected, send_shutdown
        self.gits_root, self.agent_dir, self.logs_dir, self.python = Path(gits_root), Path(agent_dir), Path(logs_dir), python
        self._env = dict(os.environ if env is None else env)
        self._sleep, self._clock = sleep, clock
        self.shutdown_wait, self.poll_manual = shutdown_wait, poll_manual
        self.pythonpath = list(pythonpath if pythonpath is not None else [str(self.gits_root / "sdk")])
        self._probe = probe or self._http_probe
        self.health_interval = health_interval
        self.memory_dir = Path(memory_dir).resolve() if memory_dir is not None else None
        self.state = {s.id: _Proc(s) for s in specs}
        for m in self.state.values():
            if m.spec.unmanaged:
                m.status, m.text = "manual", "запускается отдельно (контейнер)"

    @staticmethod
    async def _http_probe(url: str) -> bool:
        """GET url отвечает 200 (ATT-2)."""
        import aiohttp
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=HEALTH_TIMEOUT_S)) as http:
                async with http.get(url) as r:
                    return r.status == 200
        except Exception:
            return False

    # ── разбор записи ───────────────────────────────────────────────────────

    def pythonw(self) -> str:
        """{PYTHONW}: pythonw.exe из того же окружения (окна консоли нет; питомцу, PET-11), а если его нет - сам интерпретатор."""
        exe = Path(self.python)
        w = exe.with_name("pythonw.exe")
        return str(w if w.exists() else exe)

    def _subst(self, text: str, missing: list[str]) -> str:
        def one(m: re.Match) -> str:
            name = m.group(1)
            if name == "GITS":
                return str(self.gits_root)
            if name == "PYTHON":
                return self.python
            if name == "PYTHONW":
                return self.pythonw()
            if name in self._env and self._env[name]:
                return self._env[name]
            missing.append(name)
            return m.group(0)
        return _VAR.sub(one, text)

    def resolve(self, peer_id: str) -> tuple[list[str], Path] | str:
        """(команда с аргументами, рабочая папка) или причина «не настроен» (CORE-12)."""
        spec = self.specs[peer_id]
        missing: list[str] = []
        command = self._subst(spec.command, missing)
        args = [self._subst(a, missing) for a in spec.args]
        cwd = Path(self._subst(spec.cwd, missing))
        if missing:
            return "не задана переменная окружения " + ", ".join(sorted(set(missing)))
        if not cwd.is_absolute():
            cwd = self.agent_dir / cwd
        if not cwd.is_dir():
            return f"папка модуля не найдена: {cwd}"
        if spec.external and not Path(command).is_file() and not shutil.which(command):
            return f"окружение сервиса не найдено: {command} (см. README сервиса)"      # чужой Python сервису не годится
        if spec.command.strip() not in ("{PYTHON}", "{PYTHONW}") and not Path(command).is_file() and not shutil.which(command):
            logger.warning(f"Модуль {peer_id}: команда {command} не найдена, запускаю тем же Python, что и ядро")
            command = self.python
        return [command, *args], cwd

    def _process_env(self, peer_id: str) -> dict:
        env = dict(self._env)
        if self.specs[peer_id].external:
            env["PYTHONUNBUFFERED"] = "1"           # внешний сервис не участник шины: токен ядра и адрес шины ему не нужны
            return env
        env.update(GITS_TOKEN=self.token, GITS_CORE_URL=self.core_url, GITS_ID=peer_id, PYTHONUNBUFFERED="1")
        if self.memory_dir is not None:
            env["MEMORY_DIR"] = str(self.memory_dir)
        if os.environ.get("CHARACTER_HOME"):          # том персонажа: модули берут ресурсы из него (gits_sdk.resources)
            env["CHARACTER_HOME"] = os.environ["CHARACTER_HOME"]
        if "GITS_CHARACTER_ID" not in env:         # ресурсы модуля: resources/<Имя>/<модуль>/ активного персонажа (gits_sdk.resources)
            try:
                import character
                env["GITS_CHARACTER_ID"] = character.configured_id()
            except Exception as e:                  # настройки не читаются: модуль возьмёт персонажа по умолчанию
                logger.warning(f"Модуль {peer_id}: персонаж для ресурсов не определён ({e})")
        if self.pythonpath:            # до установки пакета gits-sdk модули находят SDK через PYTHONPATH
            env["PYTHONPATH"] = os.pathsep.join([*self.pythonpath, *([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])])
        return env

    # ── состояние ───────────────────────────────────────────────────────────

    def status(self, peer_id: str) -> dict | None:
        m = self.state.get(peer_id)
        if m is None:
            return None
        return {"status": m.status, "text": m.text, "pid": m.proc.pid if m.alive else None, "restarts": m.restarts,
                "last_error": m.last_error or (m.reason if m.status == "not_configured" else ""),
                "configured": m.status != "not_configured", "reason": m.reason, "role": m.spec.role, "external": m.spec.external,
                "healthy": m.healthy}

    def log_path(self, peer_id: str) -> Path:
        return self.logs_dir / f"{peer_id}.log"

    def should_run(self, peer_id: str) -> bool:
        if getattr(self.specs[peer_id], "unmanaged", False):            # контейнер запускает compose, супервизор его не трогает
            return False
        if os.environ.get("GITS_NO_MODULES") == "1" or self._only_core():     # ТЗ 17, FLOW-1: режим «только ядро», службы ядра (decider) остаются
            spec = self.specs[peer_id]
            if not spec.external or os.environ.get("GITS_NO_DECIDER") == "1":
                return False
        for name, values in self.specs[peer_id].skip_when_env.items():
            if os.environ.get(name, "").strip().lower() in values:        # например TTS_ENGINE=fish: ESpeech не нужен
                return False
        return self.specs[peer_id].autostart and self._enabled(peer_id)

    # ── управление ──────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Запуск при старте ядра: все включённые модули с autostart."""
        for peer_id in self.specs:
            if self.should_run(peer_id):
                self._launch(peer_id)

    def _launch(self, peer_id: str) -> None:
        m = self.state[peer_id]
        if m.task is not None and not m.task.done():
            return
        m.desired = True
        m.task = asyncio.get_running_loop().create_task(self._supervise(m))

    async def apply(self, peer_id: str) -> None:
        """Привести процесс в соответствие с настройкой: включили - запустить, выключили - остановить (CORE-14)."""
        if peer_id not in self.specs:
            return
        if self.should_run(peer_id):
            fresh = self.state[peer_id].task is None or self.state[peer_id].task.done()   # без надзора до сих пор
            self._launch(peer_id)
            if fresh:
                await self._announce_launch(peer_id, "его включили в настройках")
        else:
            await self.stop(peer_id)

    async def restart(self, peer_id: str) -> None:
        """Кнопка «перезапустить»: счётчик сбоев сбрасывается; выключенный модуль не запускается."""
        if peer_id not in self.specs:
            return
        await self.stop(peer_id)
        m = self.state[peer_id]
        m.failures.clear()
        m.restarts, m.last_error, m.reason = 0, "", ""
        m.status, m.text = "stopped", "остановлен"
        if self._enabled(peer_id):
            self._launch(peer_id)
            await self._announce_launch(peer_id, "перезапуск по кнопке в дашборде")

    async def _announce_launch(self, peer_id: str, why: str) -> None:
        """Объявление о запуске процесса модуля (правило 14): факт «что запускается и зачем», фразу строит модель.
        Не объявляется: сервисы (decider - внутренняя часть ядра), старт при загрузке ядра, модуль без настроек
        (он не запустится, см. resolve)."""
        spec = self.specs[peer_id]
        if self._announce is None or spec.external or not isinstance(self.resolve(peer_id), tuple):
            return
        try:
            await self._announce(peer_id, f"Запускаю модуль «{spec.title or peer_id}»: {why}.")
        except Exception as e:                       # объявление не должно мешать запуску
            logger.warning(f"Объявление запуска модуля {peer_id}: {e}")

    async def stop(self, peer_id: str) -> None:
        """Остановить процесс, запущенный супервизором: shutdown, ожидание 10 с, затем убить дерево."""
        m = self.state.get(peer_id)
        if m is None or m.spec.unmanaged:
            return
        m.desired = False
        task = m.task
        if m.alive:
            await self._terminate(m)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        m.task = None
        m.healthy = m.external = False
        if m.status not in ("crashed", "not_configured"):
            m.status, m.text = "stopped", "остановлен"

    async def stop_all(self) -> None:
        """Остановка ядра (CORE-17): shutdown всем одновременно, через 10 с убить оставшихся."""
        await asyncio.gather(*[self.stop(mid) for mid in self.specs], return_exceptions=True)

    async def _terminate(self, m: _Proc) -> None:
        proc = m.proc
        if proc is None or proc.returncode is not None:
            return
        if m.spec.external:                    # у сервиса нет канала shutdown: останавливаем процесс вместе с потомками
            await asyncio.to_thread(kill_tree, proc.pid)
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except asyncio.TimeoutError:
                pass
            return
        if self._send_shutdown is not None:
            try:
                await self._send_shutdown(m.spec.id)
            except Exception as e:
                logger.debug(f"shutdown для {m.spec.id}: {e}")
        try:
            await asyncio.wait_for(proc.wait(), self.shutdown_wait)
        except asyncio.TimeoutError:
            logger.warning(f"Модуль {m.spec.id} не вышел за {self.shutdown_wait:g} с, процесс убит")
            events_log.log("module", f"модуль {m.spec.id} не завершился сам, процесс убит", module=m.spec.id)
            await asyncio.to_thread(kill_tree, proc.pid)
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except asyncio.TimeoutError:
                pass

    # ── цикл надзора ────────────────────────────────────────────────────────

    async def _supervise(self, m: _Proc) -> None:
        spec = m.spec
        try:
            while m.desired:
                resolved = self.resolve(spec.id)
                if isinstance(resolved, str):
                    m.status, m.text, m.reason = "not_configured", f"не настроен: {resolved}", resolved
                    events_log.log("module", f"модуль {spec.id} не запущен: {resolved}", module=spec.id)
                    return
                m.reason = ""
                if spec.external:
                    m.healthy = await self._probe(spec.health_url)
                    if m.healthy:                       # ATT-2: порт занят работающим сервисом - второй не запускаем, следим
                        m.external = True
                        m.status, m.text = "running", "работает (запущен не супервизором)"
                        await self._sleep(self.health_interval)
                        continue
                    m.external = False
                elif self._is_connected(spec.id):       # CORE-16: запущен вручную, второй экземпляр не нужен
                    m.status, m.text = "manual", "запущен вручную"
                    await self._sleep(self.poll_manual)
                    continue
                code = await self._run_once(m, *resolved)
                if not m.desired:
                    return
                if not await self._after_exit(m, code):
                    return
        except asyncio.CancelledError:
            if m.alive:
                await asyncio.shield(self._terminate(m))
            raise
        finally:
            if m.sink is not None:
                m.sink.close()
                m.sink = None

    async def _run_once(self, m: _Proc, command: list[str], cwd: Path) -> int:
        spec = m.spec
        m.tail.clear()
        if m.sink is None:
            m.sink = await asyncio.to_thread(_LogSink, self.log_path(spec.id))
        try:
            m.proc = await asyncio.create_subprocess_exec(
                *command, cwd=str(cwd), env=self._process_env(spec.id), stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, creationflags=NO_WINDOW,
                limit=1 << 20)
        except OSError as e:
            m.tail.append(f"не удалось запустить: {e}")
            logger.warning(f"Модуль {spec.id}: не удалось запустить: {e}")
            m.status, m.text = "backoff", "не удалось запустить"
            return 1
        service = spec.external
        m.status, m.text = ("starting", "запускается") if service else ("running", "запущен")
        m.healthy = False
        events_log.log("module", f"запущен процесс {'сервиса' if service else 'модуля'} {spec.id} (pid {m.proc.pid})",
                       module=spec.id)
        pump = asyncio.get_running_loop().create_task(self._pump(m, m.proc))
        health = asyncio.get_running_loop().create_task(self._watch_health(m)) if service else None
        try:
            code = await m.proc.wait()
        finally:
            if health is not None:
                health.cancel()
            m.healthy = False
        try:
            await asyncio.wait_for(pump, 3)             # дочитать хвост вывода
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pump.cancel()
        await m.sink.flush()
        return code

    async def _watch_health(self, m: _Proc) -> None:
        """Пока процесс сервиса жив: опрос health_url раз в 10 с; 200 - «работает», иначе «не отвечает» (ATT-2)."""
        while True:
            ok = await self._probe(m.spec.health_url)
            if ok != m.healthy:
                events_log.log("module", f"сервис {m.spec.id}: {'работает' if ok else 'не отвечает'}", module=m.spec.id,
                               healthy=ok)
            m.healthy = ok
            m.status, m.text = ("running", "работает") if ok else ("starting", "запущен, не отвечает на health")
            await self._sleep(self.health_interval)

    async def _pump(self, m: _Proc, proc: asyncio.subprocess.Process) -> None:
        last_flush = time.monotonic()
        try:
            while True:
                try:
                    raw = await asyncio.wait_for(proc.stdout.readline(), 0.5)
                except asyncio.TimeoutError:
                    await m.sink.flush()                # вывода нет: то, что накопилось, всё равно уходит в файл
                    last_flush = time.monotonic()
                    continue
                except (ValueError, asyncio.LimitOverrunError):
                    continue                            # слишком длинная строка: пропускаем
                if not raw:
                    return
                line = raw.decode("utf-8", errors="replace").rstrip()
                clean = redact(line)
                m.sink.add(clean)
                if clean == line:                       # строки, похожие на токены, в хвост не попадают (CORE-15)
                    m.tail.append(line)
                if len(m.sink._buffer) >= 50 or time.monotonic() - last_flush > 0.5:
                    await m.sink.flush()
                    last_flush = time.monotonic()
        finally:
            await m.sink.flush()

    async def _after_exit(self, m: _Proc, code: int) -> bool:
        """Что делать после выхода процесса. True - перезапустить, False - прекратить надзор."""
        spec = m.spec
        tail = "\n".join(m.tail)
        logger.info(f"Процесс модуля {spec.id} завершился с кодом {code}")
        events_log.log("module", f"процесс модуля {spec.id} завершился (код {code})", module=spec.id, code=code)
        if spec.restart == "never" or (spec.restart == "on-failure" and code == 0):
            m.status, m.text = "stopped", "остановлен" if code == 0 else f"завершился с кодом {code}"
            if code != 0:
                m.last_error = tail
            return False
        now = self._clock()
        m.failures.append(now)
        while m.failures and now - m.failures[0] > CRASH_WINDOW_S:
            m.failures.popleft()
        m.last_error = tail
        if len(m.failures) >= CRASH_LIMIT:
            m.status, m.text = "crashed", "остановлен после сбоев"
            events_log.log("module", f"модуль {spec.id} остановлен после {CRASH_LIMIT} сбоев за {CRASH_WINDOW_S // 60} минут",
                           module=spec.id, tail=tail)
            logger.warning(f"Модуль {spec.id} остановлен после сбоев")
            return False
        delay = RESTART_DELAYS[min(len(m.failures) - 1, len(RESTART_DELAYS) - 1)]
        m.status, m.text = "backoff", f"перезапуск через {delay} с"
        await self._sleep(delay)
        if not m.desired:
            return False
        m.restarts += 1
        await self._announce_launch(spec.id, f"процесс упал с кодом {code}, перезапускаю (сбой {len(m.failures)} из "
                                             f"{CRASH_LIMIT} за {CRASH_WINDOW_S // 60} минут)")
        return True

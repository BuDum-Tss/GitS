"""Супервизор процессов модулей (ТЗ 02, раздел 7): запускает модули из `agent/modules.yaml` и следит за ними.

Модуль включён, если пользователь его не выключил (настройка `modules.<id>.enabled`); включённый модуль с
`autostart: true` запускается вместе с ядром. Падение процесса перезапускается по политике `restart` с задержками
2, 5, 15, 60 с; пять падений за десять минут - и супервизор перестаёт, помечая модуль «остановлен после сбоев» (с хвостом
вывода без токенов). Выключение: модулю уходит `shutdown`, через 10 с процесс убивается вместе с потомками. Вручную
запущенный модуль (при разработке) принимается как обычный: пока он подключён, второй экземпляр не запускается.

Сервис (ТЗ 03, ATT-2): запись `kind: service` с `health_url` - процесс без связи по WebSocket (первый: `decider`, классификатор
внимания). Супервизор считает его работающим, пока `GET health_url` отвечает 200 (опрос раз в 10 с), перезапускает по той же
политике `restart`, а если порт уже занят работающим сервисом (запущен не супервизором), второй экземпляр не запускает и только
следит за здоровьем. Токен ядра сервису не передаётся.

Вывод процесса (stdout и stderr вместе) пишется в `agent/logs/modules/<id>.log` (5 МБ, три файла). Токен ядра процессу
передаётся в `GLADOS_MODULE_TOKEN`, в журналы он не попадает: похожие на токены места в выводе вырезаются. Модулю также
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
MODULES_YAML = AGENT_DIR / "modules.yaml"
LOGS_DIR = AGENT_DIR / "logs" / "modules"

RESTART_DELAYS = (2, 5, 15, 60)       # CORE-15
CRASH_LIMIT, CRASH_WINDOW_S = 5, 600  # пять падений за десять минут - перестаём
SHUTDOWN_WAIT_S = 10.0                # CORE-14, CORE-17
TAIL_LINES = 20
LOG_BYTES, LOG_FILES = 5 * 1024 * 1024, 3
POLL_MANUAL_S = 5.0                   # как часто смотрим, не отключился ли вручную запущенный модуль
HEALTH_INTERVAL_S = 10.0              # ATT-2: опрос health_url сервиса
HEALTH_TIMEOUT_S = 2.0
KINDS = ("module", "service")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
RESTART_POLICIES = ("always", "on-failure", "never")

_VAR = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
# похожее на токен: длинный hex (токен ядра - 64 hex) или Bearer-заголовок
_SECRET = re.compile(r"[0-9a-fA-F]{32,}|Bearer\s+\S+", re.I)


@dataclass
class ModuleSpec:
    id: str
    cwd: str
    command: str
    args: list[str] = field(default_factory=list)
    autostart: bool = True
    restart: str = "on-failure"
    enabled: bool = True             # включён ли, пока в настройках нет явного выбора
    title: str = ""
    kind: str = "module"             # module: подключается по WebSocket; service: проверяется по health_url (ATT-2)
    health_url: str = ""
    description: str = ""            # подпись для дашборда: перекрывает описание из манифеста подключившегося модуля
    dev: bool = False                # для разработчиков: вкладка «Модули» показывает запись только при настройке dev_mode


@dataclass
class PlannedModule:
    """Модуль, которого ещё нет (раздел `planned:` в modules.yaml): дашборд показывает серую карточку, супервизор не трогает."""
    id: str
    title: str
    tz: str = ""


def load_planned(path: Path | str = MODULES_YAML) -> list[PlannedModule]:
    """Запланированные модули из modules.yaml. Неверная запись пропускается с предупреждением."""
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        logger.warning(f"modules.yaml не прочитан: {e}")
        return []
    rows: list[PlannedModule] = []
    for i, raw in enumerate((data.get("planned") if isinstance(data, dict) else None) or []):
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not raw.get("title"):
            logger.warning(f"modules.yaml, planned, запись {i + 1}: нужен объект с id и title")
            continue
        rows.append(PlannedModule(id=raw["id"], title=str(raw["title"]), tz=str(raw.get("tz") or "")))
    return rows


def load_specs(path: Path | str = MODULES_YAML) -> list[ModuleSpec]:
    """Модули из modules.yaml. Неверная запись пропускается с предупреждением."""
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        logger.warning(f"modules.yaml не прочитан: {e}")
        return []
    specs: list[ModuleSpec] = []
    for i, raw in enumerate((data.get("modules") if isinstance(data, dict) else None) or []):
        try:
            if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
                raise ValueError("нужен объект с id")
            restart = raw.get("restart", "on-failure")
            if restart not in RESTART_POLICIES:
                raise ValueError(f"restart: одно из {list(RESTART_POLICIES)}")
            args = raw.get("args") or []
            if not isinstance(args, list):
                raise ValueError("args: список строк")
            kind = raw.get("kind", "module")
            if kind not in KINDS:
                raise ValueError(f"kind: одно из {list(KINDS)}")
            health_url = str(raw.get("health_url") or "")
            if kind == "service" and not health_url.startswith(("http://", "https://")):
                raise ValueError("для kind: service нужен health_url (http:// или https://)")
            specs.append(ModuleSpec(id=raw["id"], cwd=str(raw.get("cwd") or "."), command=str(raw["command"]),
                                    args=[str(a) for a in args], autostart=bool(raw.get("autostart", True)),
                                    restart=restart, enabled=bool(raw.get("enabled", True)),
                                    title=str(raw.get("title") or ""), kind=kind, health_url=health_url,
                                    description=str(raw.get("description") or ""), dev=bool(raw.get("dev", False))))
        except (KeyError, ValueError) as e:
            logger.warning(f"modules.yaml, запись {i + 1}: {e}")
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

    def __init__(self, spec: ModuleSpec):
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
    def __init__(self, specs: list[ModuleSpec], *, token: str, core_url: str,
                 enabled: Callable[[str], bool] = lambda module_id: True,
                 is_connected: Callable[[str], bool] = lambda module_id: False,
                 send_shutdown: Callable[[str], Awaitable[bool]] | None = None,
                 gits_root: Path = GITS_ROOT, agent_dir: Path = AGENT_DIR, logs_dir: Path = LOGS_DIR,
                 python: str = sys.executable, env: dict | None = None,
                 sleep: Callable[[float], Awaitable] = asyncio.sleep, clock: Callable[[], float] = time.monotonic,
                 shutdown_wait: float = SHUTDOWN_WAIT_S, poll_manual: float = POLL_MANUAL_S,
                 pythonpath: list[str] | None = None, memory_dir: Path | str | None = None,
                 probe: Callable[[str], Awaitable[bool]] | None = None, health_interval: float = HEALTH_INTERVAL_S,
                 announce: Callable[[str, str], Awaitable[None]] | None = None):
        """enabled(id) - включён ли модуль (ModuleRegistry.is_enabled); is_connected(id) - подключён ли он сейчас;
        send_shutdown(id) - послать модулю shutdown; sleep и clock подменяются в тестах (задержки перезапуска);
        pythonpath - что добавить в PYTHONPATH процессов (до установки пакета glados-module это GitS/sdk);
        memory_dir - папка памяти ядра: модулям уходит как MEMORY_DIR (абсолютным путём; их рабочая папка другая);
        probe(url) -> отвечает ли сервис 200 (в тестах подменяется); health_interval - период опроса сервисов;
        announce(id, text) - объявление о запуске процесса модуля (правило 14: включили, перезапуск по кнопке, перезапуск
        после сбоя); текст - факт, фразу строит модель. Без него объявлений нет (тесты, скрипты)."""
        self.specs = {s.id: s for s in specs}
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

    def resolve(self, module_id: str) -> tuple[list[str], Path] | str:
        """(команда с аргументами, рабочая папка) или причина «не настроен» (CORE-12)."""
        spec = self.specs[module_id]
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
        if spec.kind == "service" and not Path(command).is_file() and not shutil.which(command):
            return f"окружение сервиса не найдено: {command} (см. README сервиса)"      # чужой Python сервису не годится
        if spec.command.strip() not in ("{PYTHON}", "{PYTHONW}") and not Path(command).is_file() and not shutil.which(command):
            logger.warning(f"Модуль {module_id}: команда {command} не найдена, запускаю тем же Python, что и ядро")
            command = self.python
        return [command, *args], cwd

    def _process_env(self, module_id: str) -> dict:
        env = dict(self._env)
        if self.specs[module_id].kind == "service":
            env["PYTHONUNBUFFERED"] = "1"           # сервис не модуль: токен ядра и адрес шины ему не нужны
            return env
        env.update(GLADOS_MODULE_TOKEN=self.token, GLADOS_CORE_URL=self.core_url, GLADOS_MODULE_ID=module_id,
                   PYTHONUNBUFFERED="1")
        if self.memory_dir is not None:
            env["MEMORY_DIR"] = str(self.memory_dir)
        if self.pythonpath:            # до установки пакета glados-module модули находят SDK через PYTHONPATH
            env["PYTHONPATH"] = os.pathsep.join([*self.pythonpath, *([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])])
        return env

    # ── состояние ───────────────────────────────────────────────────────────

    def status(self, module_id: str) -> dict | None:
        m = self.state.get(module_id)
        if m is None:
            return None
        return {"status": m.status, "text": m.text, "pid": m.proc.pid if m.alive else None, "restarts": m.restarts,
                "last_error": m.last_error or (m.reason if m.status == "not_configured" else ""),
                "configured": m.status != "not_configured", "reason": m.reason, "kind": m.spec.kind,
                "healthy": m.healthy}

    def log_path(self, module_id: str) -> Path:
        return self.logs_dir / f"{module_id}.log"

    def should_run(self, module_id: str) -> bool:
        return self.specs[module_id].autostart and self._enabled(module_id)

    # ── управление ──────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Запуск при старте ядра: все включённые модули с autostart."""
        for module_id in self.specs:
            if self.should_run(module_id):
                self._launch(module_id)

    def _launch(self, module_id: str) -> None:
        m = self.state[module_id]
        if m.task is not None and not m.task.done():
            return
        m.desired = True
        m.task = asyncio.get_running_loop().create_task(self._supervise(m))

    async def apply(self, module_id: str) -> None:
        """Привести процесс в соответствие с настройкой: включили - запустить, выключили - остановить (CORE-14)."""
        if module_id not in self.specs:
            return
        if self.should_run(module_id):
            fresh = self.state[module_id].task is None or self.state[module_id].task.done()   # без надзора до сих пор
            self._launch(module_id)
            if fresh:
                await self._announce_launch(module_id, "его включили в настройках")
        else:
            await self.stop(module_id)

    async def restart(self, module_id: str) -> None:
        """Кнопка «перезапустить»: счётчик сбоев сбрасывается; выключенный модуль не запускается."""
        if module_id not in self.specs:
            return
        await self.stop(module_id)
        m = self.state[module_id]
        m.failures.clear()
        m.restarts, m.last_error, m.reason = 0, "", ""
        m.status, m.text = "stopped", "остановлен"
        if self._enabled(module_id):
            self._launch(module_id)
            await self._announce_launch(module_id, "перезапуск по кнопке в дашборде")

    async def _announce_launch(self, module_id: str, why: str) -> None:
        """Объявление о запуске процесса модуля (правило 14): факт «что запускается и зачем», фразу строит модель.
        Не объявляется: сервисы (decider - внутренняя часть ядра), старт при загрузке ядра, модуль без настроек
        (он не запустится, см. resolve)."""
        spec = self.specs[module_id]
        if self._announce is None or spec.kind == "service" or not isinstance(self.resolve(module_id), tuple):
            return
        try:
            await self._announce(module_id, f"Запускаю модуль «{spec.title or module_id}»: {why}.")
        except Exception as e:                       # объявление не должно мешать запуску
            logger.warning(f"Объявление запуска модуля {module_id}: {e}")

    async def stop(self, module_id: str) -> None:
        """Остановить процесс, запущенный супервизором: shutdown, ожидание 10 с, затем убить дерево."""
        m = self.state.get(module_id)
        if m is None:
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
        if m.spec.kind == "service":                    # у сервиса нет канала shutdown: останавливаем процесс вместе с потомками
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
                if spec.kind == "service":
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
        service = spec.kind == "service"
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

"""Рабочие единицы монтажа на диске (ролики; ТЗ 20 раздел 6, docs/WORKSPACE_CONTRACT.md раздел 4): `job.json`, журнал `log.jsonl`,
уборка.

Папка единицы `jobs/<id>/`: job.json, sources/, transcript/, plan/, work/, renders/, qa/, log.jsonl. Состояния: `new`, `working`,
`waiting_user`, `done`, `failed`, `cancelled`. Файлы пишутся атомарно (временный файл и замена): падение процесса посреди записи
не портит job.json. Запись небольшая, но правило репозитория (всё асинхронно) соблюдается вызывающим кодом через `asyncio.to_thread`.
Результат последней проверки `check` (хэш плана и итог) лежит в `data["check"]`: по нему `final` решает, пускать ли финальный рендер.
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import stat
import threading
import time
from pathlib import Path

from gits_sdk import protocol as P

STATES = ("new", "working", "waiting_user", "done", "failed", "cancelled")
FINISHED = ("done", "failed", "cancelled")
CLOSED = ("done", "cancelled")                      # единица закрыта: открытые (new, working, waiting_user, failed) ещё в работе
SUBDIRS = ("sources", "transcript", "plan", "work", "renders", "qa")


class Job:
    """Задача: словарь `data` (ровно то, что лежит в job.json) и пути её папки."""

    def __init__(self, folder: Path, data: dict):
        self.folder = Path(folder)
        self.data = data

    @property
    def job_id(self) -> str:
        return self.data["job_id"]

    @property
    def state(self) -> str:
        return self.data["state"]

    @property
    def request(self) -> dict:
        return self.data["request"]

    @property
    def finished(self) -> bool:
        return self.state in FINISHED

    @property
    def open(self) -> bool:
        return self.state not in CLOSED

    def path(self, *parts: str) -> Path:
        return self.folder.joinpath(*parts)

    @property
    def sources(self) -> Path:
        return self.folder / "sources"

    @property
    def transcript(self) -> Path:
        return self.folder / "transcript"

    @property
    def plan_dir(self) -> Path:
        return self.folder / "plan"

    @property
    def work(self) -> Path:
        return self.folder / "work"

    @property
    def renders(self) -> Path:
        return self.folder / "renders"

    @property
    def qa(self) -> Path:
        return self.folder / "qa"

    @property
    def log_path(self) -> Path:
        return self.folder / "log.jsonl"

    def artifact(self, kind: str) -> str | None:
        for a in reversed(self.data.get("artifacts") or []):
            if a.get("kind") == kind:
                return a["path"]
        return None

    def add_artifact(self, path: Path | str, kind: str) -> None:
        arts = [a for a in self.data.get("artifacts") or [] if a.get("kind") != kind]
        arts.append({"path": str(path), "kind": kind})
        self.data["artifacts"] = arts

    def snapshot(self) -> dict:
        return copy.deepcopy(self.data)


def default_title(request: dict) -> str:
    brief = " ".join(str(request.get("brief") or "").split())
    return (brief[:60] or "Монтаж").rstrip(" .,;:")


class JobStore:
    def __init__(self, jobs_dir: Path):
        self.dir = Path(jobs_dir)
        self._lock = threading.Lock()                  # save зовут из потоков (to_thread) и из фоновых задач прогресса
        self.dir.mkdir(parents=True, exist_ok=True)

    # ---- создание и чтение

    def create(self, request: dict, title: str | None = None) -> Job:
        job_id = P.new_job_id()
        while (self.dir / job_id).exists():
            job_id = P.new_job_id()
        folder = self.dir / job_id
        for sub in SUBDIRS:
            (folder / sub).mkdir(parents=True, exist_ok=True)
        now = time.time()
        data = {"job_id": job_id, "title": title or default_title(request), "created": now, "updated": now,
                "state": "new", "request": dict(request), "artifacts": [], "runs": {}, "check": None, "error": None,
                "now": None, "step_name": None}
        job = Job(folder, data)
        self.save(job)
        return job

    def load(self, job_id: str) -> Job | None:
        path = self.dir / job_id / "job.json"
        try:
            return Job(path.parent, json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return None

    def all(self) -> list[Job]:
        jobs = []
        for folder in sorted(self.dir.glob("j-*")):
            job = self.load(folder.name)
            if job is not None:
                jobs.append(job)
        return sorted(jobs, key=lambda j: j.data.get("created", 0))

    def unfinished(self) -> list[Job]:
        """Открытые единицы: не закрытые (`done`, `cancelled`)."""
        return [j for j in self.all() if j.open]

    def save(self, job: Job) -> None:
        with self._lock:
            job.data["updated"] = time.time()
            target = job.folder / "job.json"
            tmp = job.folder / "job.json.tmp"
            tmp.write_text(json.dumps(job.data, ensure_ascii=False, indent=2), encoding="utf-8")
            for attempt in range(5):                         # Windows: антивирус или индексатор иногда держат файл долю секунды
                try:
                    os.replace(tmp, target)
                    return
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(0.05 * (attempt + 1))

    # ---- журнал единицы (MON-9, MON-17): запуски команд, ошибки; без содержимого файлов

    def log(self, job: Job, kind: str, **fields) -> None:
        row = {"ts": round(time.time(), 3), "kind": kind, **fields}
        with self._lock, open(job.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def read_log(self, job: Job, limit: int = 200) -> list[dict]:
        try:
            lines = job.log_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        rows = []
        for line in lines[-limit:]:
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
        return rows

    def read_actions(self, job: Job, limit: int = 300) -> list[dict]:
        """Последние `limit` записей-действий (`kind` = `action`) единицы, по времени от старых к новым."""
        try:
            lines = job.log_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        return _actions_of(lines, limit)

    # ---- общий журнал окружения: действия, у которых нет единицы (до `work create`)

    @property
    def env_log_path(self) -> Path:
        return self.dir / "env_log.jsonl"

    def log_env(self, kind: str, **fields) -> None:
        row = {"ts": round(time.time(), 3), "kind": kind, **fields}
        with self._lock, open(self.env_log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def read_env_actions(self, limit: int = 300) -> list[dict]:
        try:
            lines = self.env_log_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        return _actions_of(lines, limit)

    # ---- уборка (MON-11)

    def cleanup(self, keep: int) -> list[str]:
        """Оставить последние `keep` завершённых задач; остальные папки удалить целиком (копии в output_dir лежат вне
        jobs/ и не затрагиваются). Возвращает id удалённых."""
        finished = [j for j in self.all() if j.finished]
        removed = []
        for job in finished[:-keep] if keep > 0 else finished:
            _rmtree(job.folder)
            removed.append(job.job_id)
        return removed


def _actions_of(lines: list[str], limit: int) -> list[dict]:
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("kind") == "action":
            rows.append(row)
    return rows[-limit:]


def _rmtree(folder: Path) -> None:
    """Удалить папку задачи. `work/node_modules` это junction на шаблон: его снимают, не заходя внутрь (иначе удалится
    сам шаблон)."""
    link = folder / "work" / "node_modules"
    if link.exists() or link.is_symlink():
        _unlink_dir(link)

    def onerror(func, path, _exc):
        try:
            os.chmod(path, stat.S_IWRITE)
            func(path)
        except OSError:
            pass
    shutil.rmtree(folder, onerror=onerror)


def _unlink_dir(path: Path) -> None:
    try:
        os.unlink(path)                       # символьная ссылка и junction на Windows снимаются unlink
    except OSError:
        try:
            os.rmdir(path)
        except OSError:
            pass

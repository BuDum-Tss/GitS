"""Сайт монтажа: одна страница на рабочую единицу (ролик). Показывает хозяина аренды (чьи это ролики), сценарий, монтажный лист
(из plan.json), план, все файлы единицы (исходники, расшифровка, рабочая копия, рендеры, QA) и кнопку «Remotion Studio». Только
чтение: файлы не меняются, рендера и запуска команд нет. Поднимается при входе в окружение (`workspace.enter`) и работает в AFK;
адрес отдаёт инструмент `site`.
"""
from __future__ import annotations

import html
import json
import logging
import mimetypes
import os
from pathlib import Path
from urllib.parse import quote, urlsplit

from aiohttp import web

from plan_tools import plan_markdown, story_markdown

log = logging.getLogger("montage.site")

SITE_PORT_ENV = "MONTAGE_SITE_PORT"       # порт сайта в контейнере (compose публикует его на хост)
DEFAULT_SITE_PORT = 8790                  # без переменной тот же порт: ссылка в манифесте должна быть известна заранее


def site_port() -> int:
    raw = os.environ.get(SITE_PORT_ENV, "").strip()
    return int(raw) if raw.isdigit() else DEFAULT_SITE_PORT


def site_url() -> str:
    return f"http://127.0.0.1:{site_port()}/"
TEXT_LIMIT = 400_000                      # больше этого текст не показывается целиком
SKIP_DIRS = {"node_modules", ".git", "__pycache__", ".claude"}        # .claude: сотни файлов навыков Remotion, не работа задачи
TEXT_EXT = {".md", ".txt", ".json", ".jsonl", ".ts", ".tsx", ".js", ".css", ".html", ".srt", ".yaml", ".yml", ".svg", ".sh",
            ".ps1", ".py", ".lock", ".config"}
SECTIONS = (("Сценарий", "story"), ("Монтажный лист", "sheet"), ("План", "plan"), ("Файлы", "files"))
STYLE = """body{font:15px/1.5 system-ui,sans-serif;margin:0;background:#f4f4f2;color:#1b1b1b}
header{background:#111;color:#fff;padding:14px 24px;display:flex;gap:24px;align-items:baseline;flex-wrap:wrap}
header a{color:#fff;text-decoration:none}main{max-width:1100px;margin:0 auto;padding:20px 24px}
nav a{margin-right:16px;color:#c55a00;text-decoration:none}nav a.on{font-weight:700;border-bottom:2px solid #c55a00}
table{border-collapse:collapse;width:100%;background:#fff}td,th{border:1px solid #d8d8d4;padding:6px 10px;text-align:left;vertical-align:top}
th{background:#ececE8}pre{background:#fff;border:1px solid #d8d8d4;padding:12px;overflow:auto;white-space:pre-wrap}
.btn{display:inline-block;background:#c55a00;color:#fff;padding:8px 16px;text-decoration:none;border-radius:4px}
.muted{color:#6b6b66}.bar{height:10px;background:#c55a00;border-radius:2px;min-width:2px}video,img{max-width:100%}"""


def _e(value) -> str:
    return html.escape(str(value if value is not None else ""))


def _page(title: str, owner: str, body: str) -> web.Response:
    owner_text = f"хозяин: {_e(owner)}" if owner else "хозяина нет"
    text = (f"<!doctype html><html lang=ru><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{_e(title)}</title><style>{STYLE}</style><header><a href='/'><b>Монтаж</b></a>"
            f"<span>{owner_text}</span></header><main>{body}</main></html>")
    return web.Response(text=text, content_type="text/html")


def _ms(value) -> str:
    try:
        total = max(0, int(value)) / 1000
    except (TypeError, ValueError):
        return "—"
    return f"{int(total // 60)}:{total % 60:05.2f}"


def list_files(root: Path, limit: int = 2000) -> list[Path]:
    """Файлы задачи (без node_modules и служебного) относительно её папки, по алфавиту."""
    out: list[Path] = []
    for base, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(files):
            out.append(Path(base, name).relative_to(root))
            if len(out) >= limit:
                return out
    return out


def safe_file(root: Path, rel: str) -> Path | None:
    """Файл внутри папки задачи: пути вверх, симлинки наружу и node_modules не открываются."""
    parts = [p for p in rel.replace("\\", "/").split("/") if p]
    if not parts or ".." in parts or any(p in SKIP_DIRS for p in parts):
        return None
    path = root.joinpath(*parts)
    try:
        resolved = path.resolve()
        resolved.relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    return path if path.is_file() else None


def sheet_rows(plan: dict) -> list[dict]:
    """Монтажный лист: все дорожки плана одной таблицей по времени (начало, длительность, дорожка, что, текст)."""
    rows: list[dict] = []
    for seg in plan.get("segments") or []:
        rows.append({"at": seg.get("at_ms", seg.get("start_ms")), "dur": seg.get("dur_ms"), "track": "видео",
                     "what": seg.get("id") or seg.get("source") or "", "text": seg.get("note") or seg.get("source") or ""})
    for ins in plan.get("inserts") or []:
        props = ins.get("props") or {}
        text = props.get("subtitle") or props.get("wordmark") or props.get("title") or ""
        rows.append({"at": ins.get("at_ms"), "dur": ins.get("dur_ms"), "track": "вставка",
                     "what": f"{ins.get('kind', '')} {ins.get('id', '')}".strip(), "text": text})
    for vo in plan.get("voiceover") or []:
        rows.append({"at": vo.get("at_ms"), "dur": vo.get("duration_ms"), "track": "голос",
                     "what": f"{vo.get('id', '')} · {vo.get('emotion', '')}", "text": vo.get("text", "")})
    for sfx in plan.get("sfx") or []:
        rows.append({"at": sfx.get("at_ms"), "dur": None, "track": "звук", "what": sfx.get("name", ""),
                     "text": f"громкость {sfx.get('volume', '')}"})
    for blk in plan.get("blackout") or []:
        rows.append({"at": blk.get("at_ms"), "dur": blk.get("dur_ms"), "track": "затемнение", "what": "", "text": ""})
    cta = plan.get("cta")
    if cta:
        rows.append({"at": cta.get("at_ms"), "dur": None, "track": "призыв", "what": cta.get("platform", ""),
                     "text": cta.get("text", "")})
    return sorted(rows, key=lambda r: (r["at"] if isinstance(r["at"], (int, float)) else 10 ** 12))


def _read_text(path: Path) -> str:
    try:
        raw = path.read_bytes()[:TEXT_LIMIT]
    except OSError:
        return ""
    return raw.decode("utf-8", errors="replace")


class Site:
    """HTTP-сервер сайта. `manager` даёт задачи (`store`), хозяина (`layout.owner`) и запуск Studio (`launch_studio`)."""

    def __init__(self, manager, *, host: str | None = None, port: int | None = None):
        self.manager = manager
        raw = os.environ.get(SITE_PORT_ENV, "").strip()
        self.port = port if port is not None else site_port()
        self.host = host or ("0.0.0.0" if raw else "127.0.0.1")        # в контейнере порт публикует compose
        self._runner: web.AppRunner | None = None
        self.app = web.Application()
        self.app.add_routes([web.get("/", self.index), web.get("/job/{job}", self.job),
                             web.get("/job/{job}/studio", self.studio),
                             web.get("/job/{job}/raw/{path:.*}", self.raw), web.get("/job/{job}/view/{path:.*}", self.view)])

    @property
    def owner(self) -> str:
        return getattr(self.manager.layout, "owner", "") or ""

    def url(self, job_id: str = "") -> str:
        return f"http://127.0.0.1:{self.port}" + (f"/job/{quote(job_id)}" if job_id else "/")

    async def start(self) -> None:
        if self._runner is not None:
            return
        runner = web.AppRunner(self.app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, self.host, self.port)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        self._runner = runner
        log.info("сайт монтажа: %s", self.url())

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # ── страницы ──

    def _job(self, request: web.Request):
        job = self.manager.store.load(request.match_info["job"])
        if job is None:
            raise web.HTTPNotFound(text="единицы нет")
        return job

    async def index(self, request: web.Request) -> web.Response:
        rows = "".join(
            f"<tr><td><a href='/job/{quote(j.job_id)}'>{_e(j.data.get('title') or j.job_id)}</a></td><td>{_e(j.job_id)}</td>"
            f"<td>{_e(j.state)}</td></tr>" for j in self.manager.store.all())
        body = ("<h1>Ролики</h1><table><tr><th>Ролик</th><th>Единица</th><th>Состояние</th></tr>"
                f"{rows or '<tr><td colspan=3>Роликов пока нет.</td></tr>'}</table>")
        return _page("Монтаж", self.owner, body)

    async def job(self, request: web.Request) -> web.Response:
        job = self._job(request)
        section = request.query.get("s", "story")
        jid = quote(job.job_id)
        nav = "".join(f"<a class='{'on' if key == section else ''}' href='/job/{jid}?s={key}'>{label}</a>"
                      for label, key in SECTIONS)
        studio = ""
        if (job.plan_dir / "plan.json").is_file():
            studio = f" <a class=btn href='/job/{jid}/studio'>Открыть в Remotion Studio</a>"
        head = (f"<h1>{_e(job.data.get('title') or job.job_id)}</h1><p class=muted>{_e(job.job_id)} · "
                f"{_e(job.state)}{studio}</p><nav>{nav}</nav><hr>")
        return _page(str(job.data.get("title") or job.job_id), self.owner, head + getattr(self, f"_s_{section}", self._s_story)(job))

    def _doc(self, job, name: str, empty: str) -> str:
        path = job.plan_dir / name
        return f"<pre>{_e(_read_text(path))}</pre>" if path.is_file() else f"<p class=muted>{empty}</p>"

    def _doc_or_json(self, job, name: str, json_name: str, render, empty: str) -> str:
        """Текст из `<имя>.md`, а если его нет (агент пишет только json), то человекочитаемый вид из `<имя>.json`."""
        if (job.plan_dir / name).is_file():
            return self._doc(job, name, empty)
        try:
            doc = json.loads((job.plan_dir / json_name).read_text(encoding="utf-8"))
            return f"<pre>{_e(render(doc))}</pre>"
        except (OSError, ValueError, KeyError, TypeError):
            return f"<p class=muted>{empty}</p>"

    def _s_story(self, job) -> str:
        return self._doc_or_json(job, "story.md", "story.json", story_markdown, "Сценария пока нет.")

    def _s_plan(self, job) -> str:
        return self._doc_or_json(job, "plan.md", "plan.json", plan_markdown, "Плана пока нет.")

    def _s_sheet(self, job) -> str:
        path = job.plan_dir / "plan.json"
        try:
            plan = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return "<p class=muted>Монтажного листа пока нет: план не готов.</p>"
        total = float(plan.get("duration_ms") or 0) or 1.0
        fmt = plan.get("format") or {}
        out = [f"<p>{_e(fmt.get('width'))}×{_e(fmt.get('height'))}, {_e(fmt.get('fps'))} к/с, "
               f"длительность {_ms(plan.get('duration_ms'))}</p><table><tr><th>Время</th><th>Длит.</th><th>Дорожка</th>"
               "<th>Что</th><th>Текст</th><th>Шкала</th></tr>"]
        for r in sheet_rows(plan):
            at = r["at"] if isinstance(r["at"], (int, float)) else 0
            width = (r["dur"] or 0) / total * 100 if isinstance(r["dur"], (int, float)) else 0
            bar = f"<div class=bar style='margin-left:{at / total * 100:.1f}%;width:{max(width, 0.5):.1f}%'></div>"
            out.append(f"<tr><td>{_ms(r['at'])}</td><td>{_ms(r['dur']) if r['dur'] else ''}</td><td>{_e(r['track'])}</td>"
                       f"<td>{_e(r['what'])}</td><td>{_e(r['text'])}</td><td style='width:220px'>{bar}</td></tr>")
        out.append("</table>")
        return "".join(out)

    def _s_files(self, job) -> str:
        jid = quote(job.job_id)
        items = []
        for rel in list_files(job.folder):
            posix = rel.as_posix()
            q = quote(posix)
            kind = mimetypes.guess_type(posix)[0] or ""
            viewable = rel.suffix.lower() in TEXT_EXT or kind.startswith(("video/", "image/", "audio/"))
            link = f"/job/{jid}/view/{q}" if viewable else f"/job/{jid}/raw/{q}"
            try:
                size = f"{(job.folder / rel).stat().st_size / 1024:.0f} КБ"
            except OSError:
                size = ""
            items.append(f"<tr><td><a href='{link}'>{_e(posix)}</a></td><td class=muted>{size}</td></tr>")
        return f"<table><tr><th>Файл</th><th>Размер</th></tr>{''.join(items)}</table>"

    async def studio(self, request: web.Request) -> web.Response:
        job = self._job(request)
        value = await self.manager.open_studio(job.job_id)
        port = urlsplit(value["url"]).port
        raise web.HTTPFound(f"http://{request.url.host}:{port}")

    async def view(self, request: web.Request) -> web.Response:
        job = self._job(request)
        rel = request.match_info["path"]
        path = safe_file(job.folder, rel)
        if path is None:
            raise web.HTTPNotFound(text="файла нет")
        raw = f"/job/{quote(job.job_id)}/raw/{quote(rel)}"
        kind = mimetypes.guess_type(rel)[0] or ""
        if kind.startswith("video/"):
            body = f"<video controls src='{raw}'></video>"
        elif kind.startswith("audio/"):
            body = f"<audio controls src='{raw}'></audio>"
        elif kind.startswith("image/") and path.suffix.lower() != ".svg":
            body = f"<img src='{raw}'>"
        else:
            body = f"<pre>{_e(_read_text(path))}</pre>"
        back = f"/job/{quote(job.job_id)}?s=files"
        return _page(rel, self.owner, f"<p><a href='{back}'>← файлы</a> · <a href='{raw}'>скачать</a></p><h2>{_e(rel)}</h2>{body}")

    async def raw(self, request: web.Request):
        job = self._job(request)
        path = safe_file(job.folder, request.match_info["path"])
        if path is None:
            raise web.HTTPNotFound(text="файла нет")
        return web.FileResponse(path)

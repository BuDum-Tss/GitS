"""Сайт монтажа: одна страница на рабочую единицу (ролик). Показывает хозяина аренды (чьи это ролики), сценарий, монтажный лист
(из plan.json), план, все файлы единицы (исходники, расшифровка, рабочая копия, рендеры, QA) и кнопку «Remotion Studio». Только
чтение: файлы не меняются, рендера и запуска команд нет. Поднимается при входе в окружение (`workspace.enter`) и работает в AFK;
адрес отдаёт инструмент `site`.
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import mimetypes
import os
from pathlib import Path
from urllib.parse import quote, urlsplit

from aiohttp import web
from gits_sdk import ToolError

from journal import active_task, hms, plan_steps
from plan_tools import story_markdown

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
SECTIONS = (("Монтажный лист", "sheet"), ("Действия", "actions"), ("Файлы", "files"), ("Дизайн", "design"))
DEFAULT_SECTION = "sheet"
ACTIONS_SHOWN = 300                       # сколько последних действий на вкладке
LIVE_EVERY_MS = 3000                      # автообновление вкладки «Действия» и плана действий
PREVIEW_CSP = ("sandbox allow-scripts; default-src 'none'; "
               "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
               "img-src 'self' data: blob:; media-src 'self' blob:; font-src 'self' data:; "
               "connect-src 'none'; base-uri 'none'; form-action 'none'")
FONT_EXT = {".ttf", ".otf", ".woff", ".woff2"}
INTRO_MEDIA = {
    "Intro": ("529b6ede200adc229d8cc149e3962526", "Aperture_Boot_dark.mp4"),
    "IntroLight": ("feab921e68fdee2b3b4e5187f09c4256", "Aperture_Boot_light.mp4"),
    "IntroVertical": ("5fb907f9c7bca416514b1ea768a38ab8", "Aperture_Boot_vertical_dark.mp4"),
    "IntroVerticalLight": ("be853705e93c450ae74b3d3a7615ad65", "Aperture_Boot_vertical_light.mp4"),
}
STYLE = """main{max-width:1440px;margin:0 auto;padding:20px 24px}
h1,h2,h3{font-weight:700;margin:1rem 0 .65rem}
h1{font-size:1.75rem}h2{font-size:1.35rem}h3{font-size:1.15rem}
nav a.on{font-weight:700;border-bottom:2px solid currentColor}
main table{border-collapse:collapse;width:100%}main td,main th{border-bottom:1px solid oklch(var(--b3));padding:8px 12px;text-align:left;vertical-align:top}
pre{background:oklch(var(--b1));border:1px solid oklch(var(--b3));padding:12px;overflow:auto;white-space:pre-wrap}
.now{background:#fff3e6;border-left:4px solid #c55a00;padding:8px 12px;margin:8px 0}.warn{background:#fff0c2;border:1px solid #d4a800;padding:8px 12px;font-weight:700}
.plan{list-style:none;padding:0;margin:6px 0}.plan li{padding:2px 0}.plan .active{font-weight:700;color:#c55a00}.plan .done{color:#4a7a4a}.plan .skipped{color:#8a8a85}.bad{color:#b00020;font-weight:700}
.muted{opacity:.7}.bar{height:10px;background:oklch(var(--p));border-radius:2px;min-width:2px}video,img{max-width:100%}
.browser{display:grid;grid-template-columns:minmax(190px,1fr) minmax(0,3fr);gap:20px;margin-top:20px}
.file-nav{max-height:75vh;overflow:auto;background:white;padding:12px;border:1px solid #d8d8d4}
.file-nav a{display:block;margin:0;padding:6px;overflow-wrap:anywhere}.file-nav a[aria-current]{background:#fff3e6}
.preview{min-width:0;background:white;border:1px solid #d8d8d4;padding:16px;overflow-wrap:anywhere}
.preview iframe{width:100%;height:65vh;border:0}.preview img,.preview video{max-height:65vh;object-fit:contain}
.font-sample{font-size:clamp(20px,4vw,42px)}
.design-table{table-layout:fixed;overflow-wrap:anywhere}.design-table th:first-child{width:24%}
.design-table th:nth-child(2){width:26%}.design-scroll{overflow-x:auto}.design-table{min-width:630px}
.inline-preview{height:180px;min-height:90px;max-height:80vh;resize:vertical;overflow:auto;border:1px solid oklch(var(--b3));border-radius:.5rem}
.inline-preview iframe,.inline-preview img,.inline-preview video{display:block;width:100%;height:100%;border:0;object-fit:contain}
.inline-preview audio{max-width:100%}.inline-preview pre{margin:0}.inline-preview .font-sample{padding:.6rem}
@media(max-width:650px){.browser{grid-template-columns:1fr}.file-nav{max-height:25vh}}"""
WORKSPACE_STYLE = """
main:has(.editor){max-width:none;padding:0}
.editor{display:flex;flex-direction:column;height:calc(100dvh - 64px);min-height:440px}
.editor-head{padding:.45rem .8rem;border-bottom:1px solid oklch(var(--b3))}
.editor-head h1{font-size:1rem;margin:0}.editor-main,.editor-bottom{display:flex;min-height:0}
.editor-main{flex:1}.editor-bottom{height:34%;min-height:130px;border-top:1px solid oklch(var(--b3))}
.editor-bottom:not(:has(.editor-panel:not([hidden]))){height:4.7rem;min-height:0}
.editor-panel[hidden]{display:none}.editor-panel{min-width:0;overflow:auto;padding:.55rem}
.editor-side{width:clamp(160px,25vw,480px);border-inline:1px solid oklch(var(--b3))}
.editor-bottom .editor-panel{flex:1}.editor-rail{display:flex;flex-direction:column;flex:none;background:oklch(var(--b1));border-inline:1px solid oklch(var(--b3))}
.editor-rail button{writing-mode:vertical-rl;padding:.55rem .35rem;border-radius:0;border:0;min-height:0;height:auto}
.editor-rail button[aria-expanded=true]{background:oklch(var(--b3));box-shadow:inset 2px 0 oklch(var(--p))}
.editor-bottom .editor-rail{flex-direction:row;align-self:flex-start}
.editor-bottom .editor-rail button{padding:.3rem .45rem}
.editor-studio{display:flex;flex:1;min-width:0;background:#171717}.editor-studio iframe{flex:1;width:100%;border:0}
.editor-studio p{margin:auto;color:#eee}.editor-table{overflow:auto}
.agent-tree,.agent-tree ul{padding:0;margin:0;list-style:none}
.agent-tree ul{margin-left:.65rem;padding-left:.65rem;border-left:1px solid oklch(var(--b3))}
.agent-tree summary,.agent-tree a{display:block;padding:.2rem .4rem;overflow-wrap:anywhere}
.agent-tree summary{cursor:pointer;list-style:none}.agent-tree summary::-webkit-details-marker{display:none}
.agent-tree summary::before{content:'›';display:inline-block;width:1rem}
.agent-tree details[open]>summary::before{transform:rotate(90deg)}
.agent-tree a{padding-left:1.4rem}.agent-tree a:hover,.agent-tree summary:hover{background:oklch(var(--b2))}
@media(max-width:800px){.editor-side{width:29vw;min-width:110px}.editor-panel{padding:.35rem}}
"""
WORKSPACE_SCRIPT = """<script>
for (const rail of document.querySelectorAll('[data-panel-group]')) {
  const tabs = Array.from(rail.querySelectorAll('[data-panel]'));
  for (const tab of tabs) {
    tab.onclick = () => {
      const opening = tab.getAttribute('aria-expanded') === 'false';
      for (const button of tabs) {
        const active = opening && button === tab;
        button.setAttribute('aria-expanded', String(active));
        document.getElementById(button.getAttribute('aria-controls')).hidden = !active;
      }
      if (opening && tab.dataset.panel === 'files') loadBranch('.', document.getElementById('agent-files'));
    };
  }
}

async function loadBranch(path, list) {
  if (list.dataset.loading || list.dataset.loaded) return;
  list.dataset.loading = 'true';
  const message = document.createElement('li');
  message.textContent = 'Загрузка…';
  list.replaceChildren(message);
  try {
    const response = await fetch('/workspace-files?' + new URLSearchParams({path}));
    if (!response.ok) throw new Error('workspace-files');
    const data = await response.json();
    list.replaceChildren();
    for (const entry of data.entries) {
      const row = document.createElement('li');
      const child = path === '.' ? entry.name : path + '/' + entry.name;
      if (entry.type === 'dir') {
        const folder = document.createElement('details');
        const label = document.createElement('summary');
        const branch = document.createElement('ul');
        label.textContent = entry.name;
        folder.append(label, branch);
        folder.ontoggle = () => { if (folder.open) loadBranch(child, branch); };
        row.append(folder);
      } else {
        const link = document.createElement('a');
        link.href = '/workspace-file?' + new URLSearchParams({path: child});
        link.textContent = entry.name;
        link.target = '_blank';
        link.rel = 'noopener';
        row.append(link);
      }
      list.append(row);
    }
    if (data.truncated || !data.entries.length) {
      message.textContent = data.truncated ? 'Показан тот же ограниченный список, что и у агента' : 'Папка пуста';
      list.append(message);
    }
    list.dataset.loaded = 'true';
  } catch {
    message.textContent = 'Не удалось прочитать папку';
    list.replaceChildren(message);
  } finally {
    delete list.dataset.loading;
  }
}
if (!document.getElementById('panel-files').hidden) loadBranch('.', document.getElementById('agent-files'));
document.getElementById('refresh-agent-files').onclick = () => {
  const list = document.getElementById('agent-files');
  delete list.dataset.loaded;
  loadBranch('.', list);
};
</script>"""
DASHBOARD_CSS = Path(__file__).resolve().parents[3] / "agent/clients/gui/vendor/tailwind.css"


def _e(value) -> str:
    return html.escape(str(value if value is not None else ""))


def _preview_markup(path: Path, raw: str, *, inline: bool = False) -> str:
    kind = mimetypes.guess_type(path.name)[0] or ""
    suffix = path.suffix.lower()
    if kind.startswith("video/"):
        return f"<video controls playsinline preload='none' src='{raw}'></video>"
    if kind.startswith("audio/"):
        return f"<audio controls preload='none' src='{raw}'></audio>"
    if kind.startswith("image/"):
        return f"<img loading='lazy' src='{raw}' alt='{_e(path.name)}'>"
    if suffix == ".html":
        return (f"<iframe loading='lazy' sandbox='allow-scripts' referrerpolicy='no-referrer' "
                f"title='{_e(path.name)}' src='{raw}'></iframe>")
    if suffix in FONT_EXT:
        family = "PreviewFont" + hashlib.sha256(raw.encode()).hexdigest()[:12]
        return (f"<style>@font-face{{font-family:{family};src:url('{raw}')}}</style>"
                f"<p class='font-sample' style='font-family:{family},sans-serif' contenteditable='true' "
                "aria-label='Образец шрифта'>Лаборатория Aperture · The quick brown fox<br>Аа Бб Вв Дд Яя · 0123456789</p>")
    if inline and suffix not in TEXT_EXT:
        return "<p class='muted'>Предпросмотр недоступен</p>"
    if inline:
        with path.open(encoding="utf-8", errors="replace") as file:
            snippet = file.read(1201)
        return f"<pre>{_e(snippet[:1200])}{'…' if len(snippet) > 1200 else ''}</pre>"
    return f"<pre>{_e(_read_text(path))}</pre>"


def _page(title: str, owner: str, body: str) -> web.Response:
    owner_text = f"хозяин: {_e(owner)}" if owner else "хозяина нет"
    text = (f"<!doctype html><html lang=ru data-theme=light><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            "<link rel='stylesheet' href='https://cdn.jsdelivr.net/npm/daisyui@4.12.14/dist/full.min.css'>"
            "<link rel='stylesheet' href='/static/ui/vendor/tailwind.css'>"
            "<link rel='stylesheet' href='/design/components/bundle.css'>"
            f"<title>{_e(title)}</title><style>{STYLE}{WORKSPACE_STYLE}</style><body class='bg-base-200'>"
            "<div class='navbar bg-base-100 border-b border-base-300 px-4'>"
            "<a href='/' class='btn btn-ghost text-lg'>Монтаж</a>"
            f"<span class='badge badge-outline'>{owner_text}</span></div><main>{body}</main></body></html>")
    return web.Response(text=text, content_type="text/html")


def _ms(value) -> str:
    try:
        total = max(0, int(value)) / 1000
    except (TypeError, ValueError):
        return "—"
    return f"{int(total // 60)}:{total % 60:05.2f}"


def list_files(root: Path, limit: int = 2000, *, technical: bool = False) -> list[Path]:
    """Файлы задачи (без node_modules и служебного) относительно её папки, по алфавиту."""
    out: list[Path] = []
    for base, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        folder = Path(base).relative_to(root)
        if not technical and folder == Path("."):
            dirs[:] = [d for d in dirs if d in {"plan", "sources", "renders", "transcript", "qa", "work"}]
            continue
        if not technical and folder == Path("work"):
            dirs[:] = [d for d in dirs if d == "public"]
            continue
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


def _now_text(job, *, with_label: bool = False) -> str:
    """Строка «Сейчас» единицы (из `now` в job.json), экранированная."""
    now = job.data.get("now") if isinstance(job.data.get("now"), dict) else None
    if not now or not now.get("text"):
        text = "ждёт ответа пользователя" if job.state == "waiting_user" else "действий пока нет"
        return f"Сейчас: {_e(text)}" if with_label else _e(text)
    body = f"{_e(now.get('text'))} <span class=muted>({_e(hms(now.get('at')))})</span>"
    return f"Сейчас: {body}" if with_label else body


def _action_rows(rows: list[dict]) -> str:
    """Строки таблицы действий (свежие сверху, как переданы): время по часам, инструмент, текст, значок ok или ошибка."""
    out = []
    for r in rows:
        ok = r.get("ok", True)
        icon = "<span title='ok'>&#10003;</span>" if ok else f"<span class=bad title='{_e(r.get('error') or 'ошибка')}'>&#10007;</span>"
        tool = f"{r.get('tool') or ''}" + (f" {r.get('op')}" if r.get("op") and r.get("op") != r.get("tool") else "")
        out.append(f"<tr><td>{_e(hms(r.get('ts')))}</td><td>{_e(tool)}</td><td>{_e(r.get('text'))}</td><td>{icon}</td></tr>")
    return "".join(out)


def _plan_block(task: dict | None, error: str | None) -> str:
    """«План действий» чек-листом: ✓ сделано, ▶ идёт, ○ впереди, – пропущено. Плана нет: заметная пометка."""
    steps = plan_steps(task)
    if not steps:
        note = "Агент ещё не завёл план действий" + (f" (план сейчас не читается: {error})" if error else "")
        return f"<h3>План действий</h3><p class=warn>{_e(note)}</p>"
    icons = {"done": "&#10003;", "active": "&#9654;", "pending": "&#9675;", "skipped": "&ndash;"}
    items = "".join(f"<li class={s['status']}>{icons[s['status']]} {_e(s['text'])}</li>" for s in steps)
    goal = f" <span class=muted>{_e(task.get('goal'))}</span>" if task and task.get("goal") else ""
    return f"<h3>План действий{goal}</h3><ul class=plan>{items}</ul>"


def _live_script(job_id: str) -> str:
    """Небольшой скрипт автообновления (без библиотек); без него страница работает по перезагрузке. Данные экранирует сервер."""
    return ("<script>(function(){var u='/job/" + quote(job_id) + "/live';function g(i){return document.getElementById(i)}"
            "function t(){fetch(u).then(function(r){return r.json()}).then(function(d){"
            "if(g('now'))g('now').innerHTML=d.now;if(g('plan'))g('plan').innerHTML=d.plan;"
            "if(g('rows'))g('rows').innerHTML=d.rows;"
            "if(g('sheet')&&d.sheet!==g('sheet').dataset.html){g('sheet').innerHTML=d.sheet;g('sheet').dataset.html=d.sheet;}"
            "if(d.has_plan&&g('studio-placeholder')){const frame=document.createElement('iframe');"
            "frame.src=u.replace(/\\/live$/,'/studio');frame.title='Remotion Studio';"
            "frame.allow='fullscreen';g('studio-placeholder').replaceWith(frame);}}).catch(function(){})}"
            f"setInterval(t,{LIVE_EVERY_MS});}})();</script>")


class Site:
    """HTTP-сервер сайта. `manager` даёт задачи (`store`), хозяина (`layout.owner`) и запуск Studio (`launch_studio`)."""

    def __init__(self, manager, *, host: str | None = None, port: int | None = None, persistent: bool = False):
        self.manager = manager
        raw = os.environ.get(SITE_PORT_ENV, "").strip()
        self.port = port if port is not None else site_port()
        self.host = host or ("0.0.0.0" if raw else "127.0.0.1")        # в контейнере порт публикует compose
        self._runner: web.AppRunner | None = None
        self.persistent = persistent

        @web.middleware
        async def lease_required(request, handler):
            if self.manager is None and request.path != "/static/ui/vendor/tailwind.css":
                return _page("Монтаж свободен", "", "<div class='alert'>Сейчас модуль монтажа никто не использует.</div>")
            return await handler(request)

        self.app = web.Application(middlewares=[lease_required])
        self.app.add_routes([web.get("/", self.index), web.get("/job/{job}", self.job),
                             web.get("/job/{job}/studio", self.studio), web.get("/job/{job}/live", self.live),
                             web.get("/workspace-files", self.workspace_files), web.get("/workspace-file", self.workspace_file),
                             web.get("/workspace-raw/{path:.*}", self.workspace_file),
                             web.get("/job/{job}/raw/{path:.*}", self.raw), web.get("/job/{job}/view/{path:.*}", self.view),
                             web.get("/design/{path:.*}", self.design),
                             web.get("/static/ui/vendor/tailwind.css", self.dashboard_css)])

    async def dashboard_css(self, request):
        return web.FileResponse(DASHBOARD_CSS)

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
        if not self.persistent:
            await self.close()

    async def close(self) -> None:
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
            f"<td>{_e(j.state)}</td><td>{_now_text(j)}</td></tr>" for j in self.manager.store.all())
        body = ("<h1>Ролики</h1><table><tr><th>Ролик</th><th>Единица</th><th>Состояние</th><th>Сейчас</th></tr>"
                f"{rows or '<tr><td colspan=4>Роликов пока нет.</td></tr>'}</table>")
        env = self.manager.store.read_env_actions(20) if hasattr(self.manager.store, "read_env_actions") else []
        if env:
            body += "<h2>Действия вне роликов</h2><table>" + _action_rows(list(reversed(env))) + "</table>"
        return _page("Монтаж", self.owner, body)

    async def job(self, request: web.Request) -> web.Response:
        manager = self.manager
        job = self._job(request)
        section = request.query.get("s", DEFAULT_SECTION)
        jid = quote(job.job_id)
        studio = "<p id='studio-placeholder'>Remotion Studio появится после сохранения монтажного листа.</p>"
        if (job.plan_dir / "plan.json").is_file():
            studio = f"<iframe src='/job/{jid}/studio' title='Remotion Studio' allow='fullscreen'></iframe>"
        plan_html = await self._plan_html()
        if self.manager is not manager:
            raise web.HTTPServiceUnavailable(text="Аренда монтажа изменилась")
        bottom = section if section in ("files", "design") else "files"
        left = "actions" if section == "actions" else "sheet"
        actions, sheet, design = await asyncio.to_thread(
            lambda: (self._s_actions(job), self._s_sheet(job), self._design_sections(job)))
        sheet = f"<div id='sheet'>{sheet}</div>"
        if self.manager is not manager:
            raise web.HTTPServiceUnavailable(text="Аренда монтажа изменилась")
        if section in ("idea", "story"):
            sheet += await asyncio.to_thread(getattr(self, f"_s_{section}"), job)
        elif not (job.plan_dir / "plan.json").is_file():
            sheet += await asyncio.to_thread(self._s_idea, job)
        labels = dict((key, title) for title, key in SECTIONS)
        labels["tasks"] = "Задачи"

        def rail(group, keys, active):
            buttons = "".join(f"<button class='btn btn-ghost' data-panel='{key}' aria-controls='panel-{key}' "
                              f"aria-expanded='{str(key == active).lower()}'>{labels[key]}</button>" for key in keys)
            return f"<nav class='editor-rail' data-panel-group='{group}'>{buttons}</nav>"

        def panel(key, body, active, css=""):
            hidden = "" if key == active else " hidden"
            return f"<section id='panel-{key}' class='editor-panel {css}'{hidden}>{body}</section>"

        tasks = f"<p class=now id=now>{_now_text(job, with_label=True)}</p><div id=plan>{plan_html}</div>"
        files = ("<h2>Файлы</h2><button id='refresh-agent-files' class='btn btn-ghost btn-xs'>Обновить</button>"
                 "<ul id='agent-files' class='agent-tree'></ul>")
        title = str(job.data.get("title") or job.job_id)
        body = (f"<div class='editor'><div class='editor-head'><h1>{_e(title)}</h1></div>"
                f"<div class='editor-main'>{rail('left', ('sheet', 'actions'), left)}"
                f"{panel('sheet', sheet, left, 'editor-side editor-table')}{panel('actions', actions, left, 'editor-side')}"
                f"<section class='editor-studio'>{studio}</section>{panel('tasks', tasks, 'tasks', 'editor-side')}"
                f"{rail('right', ('tasks',), 'tasks')}</div>"
                f"<div class='editor-bottom'>{rail('bottom', ('files', 'design'), bottom)}"
                f"{panel('files', files, bottom)}{panel('design', design, bottom)}</div></div>")
        return _page(title, self.owner, body + _live_script(job.job_id) + WORKSPACE_SCRIPT)

    async def _plan_html(self) -> str:
        """Блок «План действий»: активная задача и её шаги из `core.tasks` (план хранит ядро, не окружение)."""
        journal = getattr(self.manager, "journal", None)
        if journal is None:
            return _plan_block(None, None)
        data = await journal.plan()
        return _plan_block(active_task(data), journal.plan_error if data is None else None)

    async def live(self, request: web.Request) -> web.Response:
        """Данные для автообновления страницы единицы (раз в 3 секунды): строка «Сейчас», план действий, таблица действий."""
        job = self._job(request)
        rows = _action_rows(list(reversed(self.manager.store.read_actions(job, ACTIONS_SHOWN))))
        sheet = await asyncio.to_thread(self._s_sheet, job)
        return web.json_response({"now": _now_text(job, with_label=True), "plan": await self._plan_html(), "rows": rows,
                                  "sheet": sheet, "has_plan": (job.plan_dir / "plan.json").is_file()})

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

    def _s_idea(self, job) -> str:
        return self._doc(job, "idea.md", "Идеи пока нет: агент её ещё не написала.")

    def _s_actions(self, job) -> str:
        rows = _action_rows(list(reversed(self.manager.store.read_actions(job, ACTIONS_SHOWN))))
        return ("<table><thead><tr><th>Время</th><th>Инструмент</th><th>Действие</th><th></th></tr></thead>"
                f"<tbody id=rows>{rows or '<tr><td colspan=4 class=muted>Действий пока нет.</td></tr>'}</tbody></table>")

    def _s_story(self, job) -> str:
        return self._doc_or_json(job, "story.md", "story.json", story_markdown, "Сценария пока нет.")

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

    def _design_sections(self, job) -> str:
        root = self.manager.layout.character / "design"
        sections: dict[str, list[Path]] = {}
        for path in list_files(root, technical=True):
            if path.name.casefold() == "readme.md":
                continue
            section = path.parts[0] if len(path.parts) > 1 else "Обзор"
            sections.setdefault(section, []).append(path)
        titles = {"assets": "Медиа", "components": "Компоненты", "fonts": "Шрифты"}
        links, panels = [], []
        for index, (section, files) in enumerate(sections.items()):
            label = titles.get(section, section)
            anchor = f"design-section-{index}"
            links.append(f"<li><a href='#{anchor}'>{_e(label)}</a></li>")
            groups: dict[Path, list[Path]] = {}
            for path in files:
                groups.setdefault(path.parent, []).append(path)
            rows = []
            for folder, entries in groups.items():
                readme = safe_file(root, (folder / "README.md").as_posix())
                description = _read_text(readme).strip().split("\n\n", 1)[0][:360] if readme else ""
                if folder != Path(".") and section == "assets":
                    rows.append(f"<tr><th colspan='3'>{_e(folder.name)}</th></tr>")
                for path in entries:
                    rel = path.as_posix()
                    url = f"/job/{quote(job.job_id)}/view/design/{quote(rel)}"
                    kind = mimetypes.guess_type(rel)[0] or path.suffix.lstrip(".").upper()
                    name = folder.name if path.name == "preview.html" else path.name
                    target = safe_file(root, rel)
                    if target is None:
                        continue
                    try:
                        preview = _preview_markup(target, f"/design/{quote(rel)}", inline=True)
                    except OSError:
                        preview = "<p class='muted'>Файл недоступен</p>"
                    rows.append(f"<tr><td><a class='link' href='{url}'>{_e(name)}</a>"
                                f"<div class='muted'>{_e(kind)}</div></td><td>{_e(description) or '—'}</td>"
                                f"<td><div class='inline-preview'>{preview}</div></td></tr>")
            panels.append(f"<section id='{anchor}'><h3>{_e(label)}</h3><div class='design-scroll'>"
                          "<table class='table table-sm design-table'>"
                          "<thead><tr><th>Название</th><th>Описание</th><th>Просмотр</th></tr></thead>"
                          f"<tbody>{''.join(rows)}</tbody></table></div></section>")
        return (f"<h2>Библиотека Aperture</h2><div class='browser'><nav aria-label='Разделы дизайна'>"
                f"<ul class='menu bg-base-100 rounded-box'>{''.join(links)}</ul></nav>"
                f"<div class='card bg-base-100'><div class='card-body'>{''.join(panels)}</div></div></div>") if panels else ""

    def _file_links(self, job, scope: str, selected: str = "") -> str:
        if scope == "design":
            root = self.manager.layout.character / "design"
            files = list_files(root, technical=True)
            prefix = "design/"
            title = "Библиотека Aperture"
        else:
            files = list_files(job.folder)
            prefix = ""
            title = "Файлы проекта"
        links = []
        for file in files:
            rel = prefix + file.as_posix()
            current = " aria-current='page'" if rel == selected else ""
            links.append(f"<a href='/job/{quote(job.job_id)}/view/{quote(rel)}'{current}>{_e(file.as_posix())}</a>")
        return f"<nav class='file-nav' aria-label='{title}'><strong>{title}</strong>{''.join(links)}</nav>" if links else ""

    async def studio(self, request: web.Request) -> web.Response:
        job = self._job(request)
        try:
            value = await self.manager.open_studio(job.job_id)
        except ToolError as exc:
            return web.Response(text=str(exc), status=503)
        port = urlsplit(value["url"]).port
        raise web.HTTPFound(f"http://{request.url.host}:{port}")

    async def workspace_files(self, request: web.Request) -> web.Response:
        try:
            result = await asyncio.to_thread(self.manager.tools._op_list, {"path": request.query.get("path", ".")})
        except ToolError as exc:
            raise web.HTTPNotFound(text=str(exc)) from exc
        return web.json_response(result)

    async def workspace_file(self, request: web.Request) -> web.StreamResponse:
        raw_path = request.match_info.get("path")
        name = raw_path if raw_path is not None else request.query.get("path", "")
        try:
            path = await asyncio.to_thread(self.manager.tools.sandbox.resolve, name)
        except ToolError as exc:
            raise web.HTTPNotFound(text=str(exc)) from exc
        if not path.is_file():
            raise web.HTTPNotFound(text="файла нет")
        if raw_path is not None or request.query.get("raw") == "1":
            return self._asset_response(path)
        raw = "/workspace-raw/" + quote(name)
        body = await asyncio.to_thread(_preview_markup, path, raw)
        return _page(path.name, self.owner, f"<h1>{_e(name)}</h1><section class='preview'>{body}</section>")

    async def view(self, request: web.Request) -> web.Response:
        job = self._job(request)
        rel = request.match_info["path"]
        design = rel.startswith("design/")
        path = safe_file(self.manager.layout.character / "design", rel.removeprefix("design/")) if design else safe_file(job.folder, rel)
        if path is None:
            raise web.HTTPNotFound(text="файла нет")
        raw = "/" + quote(rel) if design else f"/job/{quote(job.job_id)}/raw/{quote(rel)}"
        body = await asyncio.to_thread(_preview_markup, path, raw)
        back = f"/job/{quote(job.job_id)}?s=files"
        links = await asyncio.to_thread(self._file_links, job, "design" if design else "project", rel)
        return _page(rel, self.owner, f"<p><a href='{back}'>← файлы</a> · <a href='{raw}' download>скачать</a></p>"
                     f"<div class='browser'>{links}<section class='preview'><h2>{_e(rel)}</h2>{body}</section></div>")

    async def raw(self, request: web.Request):
        job = self._job(request)
        path = safe_file(job.folder, request.match_info["path"])
        if path is None:
            raise web.HTTPNotFound(text="файла нет")
        return self._asset_response(path)

    async def design(self, request: web.Request):
        rel = request.match_info["path"]
        path = safe_file(self.manager.layout.character / "design", rel)
        if path is None:
            raise web.HTTPNotFound(text="файла нет")
        if rel.startswith("components/") and path.name == "preview.html":
            markup = await asyncio.to_thread(_read_text, path)
            if path.parent.name in INTRO_MEDIA:
                blob, filename = INTRO_MEDIA[path.parent.name]
                markup = markup.replace(f"/_blob/{blob}", f"/design/assets/Intro/{filename}")
            return web.Response(
                text=("<!doctype html><meta charset=utf-8>"
                      "<meta name=viewport content='width=device-width,initial-scale=1'>"
                      "<link rel='stylesheet' href='/design/components/bundle.css'>" + markup),
                content_type="text/html",
                headers={"Content-Security-Policy": PREVIEW_CSP, "X-Content-Type-Options": "nosniff"},
            )
        return self._asset_response(path)

    @staticmethod
    def _asset_response(path: Path):
        headers = {"Content-Security-Policy": PREVIEW_CSP, "X-Content-Type-Options": "nosniff"}
        if path.suffix.lower() in FONT_EXT:
            headers["Access-Control-Allow-Origin"] = "null"
        return web.FileResponse(path, headers=headers)

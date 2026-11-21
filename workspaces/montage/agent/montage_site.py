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
from overview import overview_html
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
STATIC_DIR = Path(__file__).resolve().parent / "static"          # montage-ui.css и montage-ui.js: оформление в стиле shadcn/ui, без CDN
UI_FILES = {"montage-ui.css": "text/css", "montage-ui.js": "text/javascript"}
THEME_INIT = ("<script>try{var t=localStorage.getItem('montage-theme');"
              "var d=t?t==='dark':matchMedia('(prefers-color-scheme: dark)').matches;"
              "document.documentElement.classList.toggle('dark',d)}catch(e){}</script>")     # в <head>: тема ставится до отрисовки, без мигания
ICON_SUN = ("<svg class='i-sun' viewBox='0 0 24 24' aria-hidden='true'><circle cx='12' cy='12' r='4'/><path d='M12 2v2M12 20v2M4.9 4.9l1.4 1.4"
            "M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4'/></svg>")
ICON_MOON = "<svg class='i-moon' viewBox='0 0 24 24' aria-hidden='true'><path d='M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z'/></svg>"
ICON_FULLSCREEN = ("<svg viewBox='0 0 24 24' aria-hidden='true'><path d='M8 3H5a2 2 0 0 0-2 2v3M21 8V5a2 2 0 0 0-2-2h-3"
                   "M3 16v3a2 2 0 0 0 2 2h3M16 21h3a2 2 0 0 0 2-2v-3'/></svg>")
ICON_CLOSE = "<svg viewBox='0 0 24 24' aria-hidden='true'><path d='M18 6 6 18M6 6l12 12'/></svg>"
PREVIEW_STYLE = ("<style>html,body{margin:0;height:100%;overflow:hidden}#stage{position:fixed;inset:0;overflow:hidden}"
                 "#scene{position:absolute;left:0;top:0;width:max-content;transform-origin:0 0}</style>")
# Сцена компонента нарисована в фиксированном размере (1920x1080, вертикаль и т.п.): вписываем её в окно с сохранением пропорций
PREVIEW_SCALE = ("<script>(function(){var st=document.getElementById('stage'),sc=document.getElementById('scene');"
                 "function fit(){var w=sc.offsetWidth,h=sc.offsetHeight,W=st.clientWidth,H=st.clientHeight;if(!w||!h||!W||!H)return;"
                 "var k=Math.min(W/w,H/h);sc.style.transform='translate('+(W-w*k)/2+'px,'+(H-h*k)/2+'px) scale('+k+')'}"
                 "if(window.ResizeObserver){var ro=new ResizeObserver(fit);ro.observe(st);ro.observe(sc)}"
                 "else addEventListener('resize',fit);addEventListener('load',fit);fit()})();</script>")
WORKSPACE_SCRIPT = """<script>
const editor = document.querySelector('.editor');
for (const splitter of editor.querySelectorAll('[data-resize]')) {
  const name = splitter.dataset.resize;
  const horizontal = name === 'bottom';
  const direction = name === 'left' ? 1 : -1;
  const panel = () => splitter.parentElement.querySelector('.editor-panel:not([hidden])');
  const size = () => horizontal ? splitter.parentElement.offsetHeight : panel().offsetWidth;
  const setSize = (value) => {
    const max = horizontal ? editor.clientHeight * .72 : editor.clientWidth * .38;
    editor.style.setProperty('--' + name, Math.min(max, Math.max(horizontal ? 130 : 110, value)) + 'px');
  };
  splitter.onpointerdown = (event) => {
    if (event.button !== 0) return;
    event.preventDefault();
    const start = horizontal ? event.clientY : event.clientX;
    const initial = size();
    splitter.setPointerCapture(event.pointerId);
    editor.classList.add('resizing');
    splitter.onpointermove = (move) => {
      const point = horizontal ? move.clientY : move.clientX;
      setSize(initial + direction * (point - start));
    };
    splitter.onlostpointercapture = () => {
      splitter.onpointermove = null;
      editor.classList.remove('resizing');
    };
  };
  splitter.onkeydown = (event) => {
    const keys = horizontal ? ['ArrowUp', 'ArrowDown'] : ['ArrowLeft', 'ArrowRight'];
    const step = keys.indexOf(event.key);
    if (step < 0) return;
    event.preventDefault();
    setSize(size() + direction * (step === 0 ? -20 : 20));
  };
}
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

PREVIEW_DIALOG = (
    "<dialog id='preview-dialog' class='dialog' aria-labelledby='preview-title'>"
    "<div class='dialog-head'><h3 id='preview-title' class='dialog-title'>Предпросмотр</h3>"
    f"<button type='button' class='btn btn-outline btn-icon btn-sm' data-fullscreen='#preview-stage' data-tip='На весь экран (Esc выход)' "
    f"aria-label='На весь экран'>{ICON_FULLSCREEN}</button>"
    f"<button type='button' class='btn btn-ghost btn-icon btn-sm' data-close data-tip='Закрыть (Esc)' aria-label='Закрыть'>{ICON_CLOSE}</button></div>"
    "<div id='preview-stage' class='dialog-stage'><iframe id='preview-frame' title='Предпросмотр компонента' sandbox='allow-scripts' "
    "referrerpolicy='no-referrer' src='about:blank'></iframe></div></dialog>")


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
    text = (f"<!doctype html><html lang=ru><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"{THEME_INIT}<link rel='stylesheet' href='/static/montage-ui.css'>"
            "<link rel='stylesheet' href='/design/components/bundle.css'>"
            f"<title>{_e(title)}</title><body>"
            "<header class='app-header'><a href='/' class='brand'>Монтаж</a>"
            f"<span class='badge'>{owner_text}</span><span class='spacer'></span>"
            "<button type='button' id='theme-toggle' class='btn btn-ghost btn-icon btn-sm theme-toggle' data-tip='Тема: светлая / тёмная' "
            f"aria-label='Переключить тему'>{ICON_SUN}{ICON_MOON}</button></header>"
            f"<main>{body}</main><script src='/static/montage-ui.js'></script></body></html>")
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
            dirs[:] = [d for d in dirs if d in {"plan", "sources", "renders", "clips", "transcript", "qa", "work"}]
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


def sheet_rows(plan: dict, scenes: dict | None = None) -> list[dict]:
    """Монтажный лист по времени: «картинка» (`what` — что происходит на экране) и «голос» (`text` — что говорят)."""
    rows: list[dict] = []

    def add(at, dur, track, what="", text="", assets=()):
        rows.append({"at": at, "dur": dur, "track": track, "what": what, "text": text, "assets": list(dict.fromkeys(assets))})

    for seg in plan.get("segments") or []:
        source = str(seg.get("source") or "")
        add(seg.get("at_ms", seg.get("start_ms")), seg.get("dur_ms"), "картинка",
            seg.get("note") or f"Исходное видео {source}".strip(), assets=[source] if source else [])
    for ins in plan.get("inserts") or []:
        own = (scenes or {}).get(ins.get("id")) or {}           # сцена агента: её описание и материалы важнее догадок по параметрам
        add(ins.get("at_ms"), ins.get("dur_ms"), "картинка", own.get("description") or _insert_description(ins),
            assets=[*own.get("assets", []), *_insert_assets(ins)])
    cta = plan.get("cta")
    if cta:
        add(cta.get("at_ms"), None, "картинка", f"Призыв на экране: {cta.get('text', '')}".rstrip(": "))
    for vo in plan.get("voiceover") or []:
        who = vo.get("speaker") or vo.get("character") or ""
        add(vo.get("at_ms"), vo.get("duration_ms"), "голос", text=f"{who}: {vo.get('text', '')}" if who else vo.get("text", ""))
    for sfx in plan.get("sfx") or []:
        add(sfx.get("at_ms"), None, "звук", assets=[f"sfx/{sfx.get('name')}"] if sfx.get("name") else [])
    music = plan.get("music")
    if isinstance(music, dict) and music.get("src"):
        add(music.get("at_ms", 0), None, "звук", assets=[str(music["src"])])
    return sorted(rows, key=lambda r: (r["at"] if isinstance(r["at"], (int, float)) else 10 ** 12))


# Названия известных видов вставок; неизвестный вид (плейбук может выбрать и другие) показывается как есть
_INSERT_TITLES = {"intro": "Заставка Aperture Science", "glados_room": "Комната ГЛаДОС", "chamber_sign": "Табличка камеры",
                  "result_sign": "Итог испытания", "blueprint": "Схема на чертёжной сетке", "custom": "Своя сцена"}
_ASSET_SUFFIXES = (".mp4", ".mov", ".webm", ".mkv", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".wav", ".mp3", ".m4a", ".ogg")


def _insert_description(ins: dict) -> str:
    kind = ins.get("kind", "")
    props = ins.get("props") or {}
    title = _INSERT_TITLES.get(kind, kind)
    detail = props.get("title") or props.get("subtitle") or props.get("component") or ""
    return f"{title}: {detail}" if detail else title


def _insert_assets(ins: dict) -> list[str]:
    """Материалы вставки без закрытого списка: готовое видео заставки, компонент своей сцены и любые строки props с файлами."""
    kind, props = ins.get("kind"), ins.get("props") or {}
    found = ["Aperture_Boot (видео заставки)"] if kind == "intro" else []
    if props.get("component"):
        found.append(f"компонент {props['component']}")

    def walk(value) -> None:
        if isinstance(value, str) and value.lower().endswith(_ASSET_SUFFIXES):
            found.append(value)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(props)
    return found


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
            if self.manager is None and not request.path.startswith("/static/montage-ui."):
                return _page("Монтаж свободен", "", overview_html()
                             + "<div class='alert'>Сейчас модуль монтажа никто не использует.</div>")
            return await handler(request)

        self.app = web.Application(middlewares=[lease_required])
        self.app.add_routes([web.get("/", self.index), web.get("/job/{job}", self.job),
                             web.get("/job/{job}/studio", self.studio), web.get("/job/{job}/live", self.live),
                             web.get("/workspace-files", self.workspace_files), web.get("/workspace-file", self.workspace_file),
                             web.get("/workspace-raw/{path:.*}", self.workspace_file),
                             web.get("/job/{job}/raw/{path:.*}", self.raw), web.get("/job/{job}/view/{path:.*}", self.view),
                             web.get("/design/{path:.*}", self.design),
                             web.get("/static/{name}", self.static)])

    async def static(self, request):
        """Оформление сайта (css и js): отдаётся из agent/static, доступно и без аренды."""
        name = request.match_info["name"]
        if name not in UI_FILES:
            raise web.HTTPNotFound(text="файла нет")
        return web.FileResponse(STATIC_DIR / name, headers={"Content-Type": UI_FILES[name] + "; charset=utf-8"})

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
        body = (overview_html() + "<h2>Ролики</h2><table><tr><th>Ролик</th><th>Единица</th><th>Состояние</th><th>Сейчас</th></tr>"
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
            buttons = "".join(f"<button class='tabs-trigger' data-panel='{key}' aria-controls='panel-{key}' "
                              f"aria-expanded='{str(key == active).lower()}'>{labels[key]}</button>" for key in keys)
            return f"<nav class='editor-rail tabs-list' data-panel-group='{group}'>{buttons}</nav>"

        def panel(key, body, active, css=""):
            hidden = "" if key == active else " hidden"
            return f"<section id='panel-{key}' class='editor-panel {css}'{hidden}>{body}</section>"

        def splitter(target, label, axis="vertical"):
            return (f"<div class='editor-split' role='separator' tabindex='0' aria-orientation='{axis}' "
                    f"aria-label='{label}' data-resize='{target}'></div>")

        tasks = f"<p class=now id=now>{_now_text(job, with_label=True)}</p><div id=plan>{plan_html}</div>"
        files = ("<h2>Файлы</h2><button id='refresh-agent-files' class='btn btn-outline btn-sm'>Обновить</button>"
                 "<ul id='agent-files' class='agent-tree'></ul>")
        title = str(job.data.get("title") or job.job_id)
        body = (f"<div class='editor'><div class='editor-head'><h1>{_e(title)}</h1></div>"
                f"<div class='editor-main'><div class='editor-left'>{rail('left', ('sheet', 'actions'), left)}"
                f"{panel('sheet', sheet, left, 'editor-side editor-table')}{panel('actions', actions, left, 'editor-side')}"
                f"{splitter('left', 'Ширина левой панели')}</div><section class='editor-studio' id='editor-studio'>"
                f"<button type='button' class='btn btn-icon btn-sm fs-btn' data-fullscreen='#editor-studio' data-tip='На весь экран (Esc выход)' "
                f"aria-label='На весь экран'>{ICON_FULLSCREEN}</button>{studio}</section>"
                f"<div class='editor-right'>{splitter('right', 'Ширина правой панели')}{panel('tasks', tasks, 'tasks', 'editor-side')}"
                f"{rail('right', ('tasks',), 'tasks')}</div></div>"
                f"<div class='editor-bottom'>{splitter('bottom', 'Высота нижней панели', 'horizontal')}"
                f"{panel('files', files, bottom)}{panel('design', design, bottom)}"
                f"{rail('bottom', ('files', 'design'), bottom)}</div></div>")
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
               f"длительность {_ms(plan.get('duration_ms'))}</p><table><tr><th>Время</th><th>Длит.</th>"
               "<th>Описание</th><th>Фразы</th><th>Материалы</th><th>Шкала</th></tr>"]
        scenes = {}
        try:
            spec = json.loads((job.plan_dir / "scenes.json").read_text(encoding="utf-8"))
            scenes = {f"s{n}": s for n, s in enumerate(spec.get("scenes") or [], 1) if isinstance(s, dict)}
        except (OSError, ValueError, AttributeError):
            pass
        for r in sheet_rows(plan, scenes):
            at = r["at"] if isinstance(r["at"], (int, float)) else 0
            width = (r["dur"] or 0) / total * 100 if isinstance(r["dur"], (int, float)) else 0
            bar = f"<div class=bar style='margin-left:{at / total * 100:.1f}%;width:{max(width, 0.5):.1f}%'></div>"
            out.append(f"<tr><td>{_ms(r['at'])}</td><td>{_ms(r['dur']) if r['dur'] else ''}</td>"
                       f"<td>{_e(r['what'])}</td><td>{_e(r['text'])}</td><td>{'<br>'.join(_e(a) for a in r['assets'])}</td><td style='width:220px'>{bar}</td></tr>")
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
                    popup = ""
                    if path.suffix.lower() == ".html":          # предпросмотр в попапе: страница сама вписывает сцену в окно
                        popup = (f"<div><button type='button' class='btn btn-outline btn-sm' data-preview='/design/{quote(rel)}' "
                                 f"data-title='{_e(name)}'>Просмотр</button></div>")
                    rows.append(f"<tr><td><a href='{url}'>{_e(name)}</a>"
                                f"<div class='muted'>{_e(kind)}</div>{popup}</td><td>{_e(description) or '—'}</td>"
                                f"<td><div class='inline-preview'>{preview}</div></td></tr>")
            panels.append(f"<section id='{anchor}'><h3>{_e(label)}</h3><div class='design-scroll'>"
                          "<table class='design-table'>"
                          "<thead><tr><th>Название</th><th>Описание</th><th>Просмотр</th></tr></thead>"
                          f"<tbody>{''.join(rows)}</tbody></table></div></section>")
        return (f"<h2>Библиотека Aperture</h2><div class='browser'><nav aria-label='Разделы дизайна'>"
                f"<ul class='design-menu'>{''.join(links)}</ul></nav>"
                f"<div class='card'><div class='card-content'>{''.join(panels)}</div></div></div>{PREVIEW_DIALOG}") if panels else ""

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
                      "<link rel='stylesheet' href='/design/components/bundle.css'>" + PREVIEW_STYLE
                      + f"<div id=stage><div id=scene>{markup}</div></div>" + PREVIEW_SCALE),
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

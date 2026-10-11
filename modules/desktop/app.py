"""Модуль desktop (ТЗ 14): экран, приложения, мышь и клавиатура, Claude-помощник на этом компьютере.

Модуль владеет зрением и вводом; ядро получает от него события «чем занят пользователь» (desktop.*) и состояние
(`state`: активное приложение, простой, текущий вывод анализа) для выбора профиля и проактивности.

Разрешения: опасное и «ask»-инструменты спрашиваются через ядро (`client.ask`); при `auto_accept` обычные действия
разрешаются сразу (как раньше в approvals.request). Claude-помощник (`claude_task`) - задача (job): ответ сразу, итог
приходит `job.update`. Расход Claude уходит ядру (`core.claude_usage`), лимит ядро проверяет до запуска.

Тестовый режим `GITS_DESKTOP_FAKE=1` (проверка запуска без экрана): опрос окна и ввода выключен, инструменты, которые
трогают экран, компьютер или Claude, отвечают `unavailable`. Манифест, состояние и подключение работают как обычно.
"""
import asyncio
import logging
import os
import time
from pathlib import Path

import app_launcher
import assistant_agent
import pc_control
import perception
import screen_operator
import screenshots
from glados_module import ModuleClient, ToolError
from glados_module.protocol import new_job_id
from watch import FocusWatch

import events_log

log = logging.getLogger("desktop")

FAKE_ENV = "GITS_DESKTOP_FAKE"
TICK_S = 2.0                  # опрос окна и простоя
STATE_EVERY_S = 30.0          # состояние без изменений шлётся раз в 30 с
STATE_MIN_GAP_S = 2.0         # при изменении - не чаще раза в 2 с
IDLE_BUCKET_S = 10            # простой в состоянии округляется до 10 с: иначе оно менялось бы каждый тик
APPROVAL_TIMEOUT_S = 300
CLAUDE_TEXT_MAX = 1500        # итог задачи для её реплики (ТЗ 14, DESK-8)
USAGE_TIMEOUT_S = 20
MENU_BUTTONS = ("быстрый запуск", "quick start", "продолжить", "continue", "загрузить", "load")   # кнопки главного меню
MENU_IDLE_MIN_S = 120         # меню жмём только когда пользователь отошёл (как раньше в дашборде)
FAKE_MSG = "тестовый режим (GITS_DESKTOP_FAKE): экран, ввод и Claude не трогаются"


def fake_mode() -> bool:
    return os.getenv(FAKE_ENV, "").strip().lower() in ("1", "true", "yes", "on")


class DesktopModule:
    def __init__(self, client: ModuleClient, *, fake: bool | None = None, clock=time.time,
                 idle_fn=None, foreground_fn=None, analyze_fn=None):
        """idle_fn() -> секунды простоя, foreground_fn() -> активное окно (по умолчанию из perception); подменяются в тестах."""
        self.client = client
        self.fake = fake_mode() if fake is None else fake
        self._clock = clock
        self._idle_fn = idle_fn or perception.idle_seconds
        self._fg_fn = foreground_fn or perception.foreground
        self.watch = FocusWatch()
        self.screen = perception.ScreenWatcher(self._on_activity, analyze_fn=analyze_fn, clock=clock)
        self._tasks: list[asyncio.Task] = []
        self._jobs: dict[str, asyncio.Task] = {}     # job_id -> задача claude_task (для job.cancel)
        self._last_key = None
        self._last_state_at = 0.0
        self._obs: tuple = (None, 0.0, False)        # последнее наблюдение опроса: (окно, простой, чувствительное)
        self._last_screen_at = 0.0
        self._register()

    # ── запуск и остановка ───────────────────────────────────────────────────

    def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._tasks.append(loop.create_task(self._loop()))

    async def stop(self) -> None:
        for t in self._tasks + list(self._jobs.values()):
            t.cancel()
        await asyncio.gather(*self._tasks, *self._jobs.values(), return_exceptions=True)
        self._tasks.clear()
        self._jobs.clear()

    # ── разрешения и учёт ───────────────────────────────────────────────────

    def _vision(self) -> bool:
        return bool(self.client.settings.get("vision")) and self.client.enabled and not self.fake

    def _live(self) -> None:
        """Тестовый режим: действия с экраном, вводом, запуском программ и Claude запрещены."""
        if self.fake:
            raise ToolError(FAKE_MSG, code="unavailable")

    async def _ask(self, question: str, danger: str | None = None, job_id: str | None = None) -> bool:
        """Разрешение пользователя через ядро (client.ask). Обычное действие при auto_accept разрешается сразу;
        опасное спрашивается всегда (ТЗ 14, DESK-9). Тайм-аут и отказ - False."""
        auto = bool(self.client.settings.get("auto_accept"))
        if auto and not danger:
            events_log.log("approval", "авто-разрешено: " + question)
            return True
        text = question if not danger else f"{question} Внимание: {danger}." + (
            " Спрашиваю, даже несмотря на авто-разрешение." if auto else "")
        context = {"job_id": job_id} if job_id else None
        try:
            answer = await self.client.ask(text, kind="approval", timeout_s=APPROVAL_TIMEOUT_S, context=context)
        except (asyncio.TimeoutError, ToolError) as e:
            log.warning(f"разрешение не получено: {e}")
            return False
        return answer is True

    async def _usage(self, tokens: int, limit_text: str | None) -> None:
        """DESK-10: расход Claude ядру (батарейка и исчерпанный лимит). Сбой учёта не ломает задачу."""
        body = {"tokens": int(tokens), "source": "desktop"}
        if limit_text:
            body["limit_text"] = limit_text
        try:
            await self.client.request("core.claude_usage", body, timeout=USAGE_TIMEOUT_S)
        except Exception as e:
            log.warning(f"расход Claude не учтён: {e}")

    async def _say_fact(self, text: str, announce: bool = False) -> None:
        """Факт для её реплики (правило 14: объявления о долгом запуске - announce)."""
        extra = {"announce": True} if announce else {}
        await self.client.say(text, mode="fact", **extra)

    # ── события и состояние ─────────────────────────────────────────────────

    async def _on_activity(self, entry: dict) -> None:
        """Новый вывод анализа экрана: событие desktop.activity (ядро пишет его в память занятий, DESK-3)."""
        await self.client.event("desktop.activity", f"{entry['app']}: {entry['summary']}",
                                data={"app": entry["app"], "category": entry["category"],
                                      "summary": entry["summary"], "since": entry["since"]})

    async def _loop(self) -> None:
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning(f"опрос экрана: {type(e).__name__}: {e}")
            await asyncio.sleep(TICK_S)

    async def _tick(self) -> None:
        now = self._clock()
        if self.fake:
            win, idle, sensitive = None, 0.0, False
        else:
            win = await asyncio.to_thread(self._fg_fn)
            idle = await asyncio.to_thread(self._idle_fn)
            sensitive = bool(win and perception.is_sensitive(win))
            for ev in self.watch.tick(now, win, idle, sensitive):
                await self.client.event(ev["kind"], ev["text"], data=ev["data"])
            if self._vision() and now - self._last_screen_at >= perception.CHECK_EVERY_SECS:
                self._last_screen_at = now
                await self.screen.observe(win, idle, True)
        self._obs = (win, idle, sensitive)
        await self._send_state(now)

    async def _send_state(self, now: float, force: bool = False) -> None:
        """`state` (ТЗ 14, DESK-6): при изменении не чаще раза в 2 с, без изменений - раз в 30 с.
        Берёт последнее наблюдение опроса (self._obs), поэтому вызывается и из обработчиков тем."""
        win, idle, sensitive = self._obs
        app = str(win["app"]) if win else None
        activity = self.screen.current
        category = activity["category"] if activity else None
        key = (app, sensitive, category, int(idle // IDLE_BUCKET_S))
        changed = key != self._last_key
        since = now - self._last_state_at
        if not force and not ((changed and since >= STATE_MIN_GAP_S) or since >= STATE_EVERY_S):
            return
        self._last_key, self._last_state_at = key, now
        summary = (f"{app or 'активного окна нет'}, {category or 'анализа пока нет'}; простой {int(idle)} с")
        data = {"foreground_app": app, "sensitive": sensitive, "idle_s": round(idle, 1), "activity": activity}
        await self.client.state(summary, data)

    # ── обработчики ─────────────────────────────────────────────────────────

    def _register(self) -> None:
        c = self.client

        @c.on_topic("settings")
        async def on_settings(data):
            await self._send_state(self._clock(), force=True)

        @c.on_topic("module")
        async def on_module(data):
            await self._send_state(self._clock(), force=True)

        @c.on_reconnect
        async def reconnected():
            self._last_key = None
            await c.resend_jobs()                      # MOD-22: незавершённые задачи шлём заново

        @c.on_job_cancel
        async def cancel_job(job_id):
            task = self._jobs.get(job_id)
            if task is not None:
                task.cancel()

        # ── экран ───────────────────────────────────────────────────────────

        @c.tool("take_screenshot")
        async def take_screenshot(args):
            self._live()
            target = args.get("target") or "game"
            focus = bool(args.get("focus"))
            if target == "screen":
                await self._say_fact("Снимаю весь экран.", announce=True)
            try:
                if target == "screen":
                    path = await asyncio.to_thread(screenshots.grab_screen)
                else:
                    path = await asyncio.to_thread(screenshots.grab_game, focus)
            except screenshots.ShotError as e:
                raise ToolError(str(e), code="unavailable")
            size = await asyncio.to_thread(_image_size, path)
            return (f"снимок {'экрана' if target == 'screen' else 'игры'} сохранён: {path.name} ({size}). "
                    "Его можно приложить к черновику поста (draft_attach).")

        @c.check("take_screenshot")
        async def check_screenshot(args):
            return ("Она хочет сфотографировать весь экран: на снимок могут попасть чужие окна"
                    if args.get("target") == "screen" else None)

        @c.tool("describe_screen")
        async def describe_screen(args):
            self._live()
            seen = await perception.describe_now(self._vision())
            status = seen["status"]
            if status == "off":
                raise ToolError("зрение выключено, она ничего не видит", code="unavailable")
            if status == "sensitive":
                raise ToolError("сейчас открыто окно с личным содержимым (пароли, банк, приватный режим), смотреть она не будет",
                                code="denied")
            if status != "ok":
                raise ToolError("локальная модель зрения не ответила", code="unavailable")
            return seen["text"]

        @c.tool("last_screenshot")
        async def last_screenshot(args):
            path = await asyncio.to_thread(screenshots.last)
            return {"path": str(path) if path else None}

        @c.tool("read_screen")
        async def read_screen(args):
            self._live()
            return await screen_operator.read_screen()

        @c.tool("operate_screen")
        async def operate_screen(args):
            self._live()
            return await screen_operator.operate(str(args["goal"]), self._ask, int(args.get("max_steps") or 8))

        # ── мышь, клавиатура, окна (pc_control) ─────────────────────────────

        @c.tool("list_windows")
        async def list_windows(args):
            self._live()
            return await asyncio.to_thread(pc_control._list_windows)

        @c.tool("focus_window")
        async def focus_window(args):
            self._live()
            return await asyncio.to_thread(pc_control._focus_window, args["title"])

        @c.tool("click_text")
        async def click_text(args):
            self._live()
            return await asyncio.to_thread(pc_control._click_text, args["text"], args.get("button") or "left",
                                           bool(args.get("double")))

        @c.tool("click")
        async def click(args):
            self._live()
            return await asyncio.to_thread(pc_control._click_xy, int(args["x"]), int(args["y"]),
                                           args.get("button") or "left", bool(args.get("double")))

        @c.tool("type_text")
        async def type_text(args):
            self._live()
            return await asyncio.to_thread(pc_control._type, args["text"])

        @c.check("type_text")
        async def check_type(args):
            return await asyncio.to_thread(pc_control._danger_type, {"text": args.get("text", "")})

        @c.tool("press_key")
        async def press_key(args):
            self._live()
            return await asyncio.to_thread(pc_control._press, args["keys"])

        @c.check("press_key")
        async def check_key(args):
            return pc_control._danger_key({"keys": args.get("keys", "")})

        @c.tool("scroll")
        async def scroll(args):
            self._live()
            return await asyncio.to_thread(pc_control._scroll, int(args["amount"]), args.get("x"), args.get("y"))

        @c.tool("menu_click")
        async def menu_click(args):
            self._live()
            text = str(args.get("text") or "").strip()
            if text.lower() not in MENU_BUTTONS:
                raise ToolError("такой кнопки в списке разрешённых нет", code="denied")
            if self._idle_fn() < MENU_IDLE_MIN_S:
                raise ToolError("пользователь сейчас за компьютером: жду, пока отойдёт", code="unavailable")

            def work():
                pc_control._focus_window("RimWorld")
                time.sleep(0.6)
                return pc_control._click_text(text, "left", False)
            return await asyncio.to_thread(work)

        # ── приложения ──────────────────────────────────────────────────────

        @c.tool("launch_app")
        async def launch_app(args):
            self._live()
            try:
                return await asyncio.to_thread(app_launcher.launch, args["name"])
            except (ValueError, RuntimeError) as e:
                raise ToolError(str(e), code="unavailable")

        @c.tool("list_apps")
        async def list_apps(args):
            self._live()
            return "Подходящие приложения: " + await asyncio.to_thread(app_launcher.titles, args.get("query") or "")

        @c.tool("add_app")
        async def add_app(args):
            self._live()
            try:
                return await asyncio.to_thread(app_launcher.add, args["title"], args["path"], args.get("aliases") or [])
            except ValueError as e:
                raise ToolError(str(e), code="bad_request")

        # ── Claude-помощник (задача) ────────────────────────────────────────

        @c.tool("claude_task")
        async def claude_task(args):
            self._live()
            task = str(args.get("task") or "").strip()
            if not task:
                raise ToolError("нужна просьба", code="bad_request")
            job_id = new_job_id()
            self._jobs[job_id] = asyncio.get_running_loop().create_task(
                self._run_claude(job_id, task, str(args.get("context") or "")))
            return {"job_id": job_id}

    async def _run_claude(self, job_id: str, task: str, context: str) -> None:
        """Задача claude_task (DESK-8): running → done (text - итог) или failed (error)."""
        c = self.client
        try:
            await c.job_update(job_id, title=f"Задача на компьютере: {task[:60]}", state="running",
                               text="Claude взялся за задачу.")
            answer = await assistant_agent.run_task(task, context, lambda q, d: self._ask(q, d, job_id), self._usage)
            if answer is None:
                await c.job_update(job_id, state="failed", text="Агент-помощник не смог выполнить задачу.",
                                   error={"code": "internal", "message": "сбой агента-помощника"})
            else:
                await c.job_update(job_id, state="done", text=answer[:CLAUDE_TEXT_MAX])
        except asyncio.CancelledError:
            await c.job_update(job_id, state="cancelled", text="Задачу отменили.")
            raise
        except Exception as e:
            log.exception("claude_task")
            await c.job_update(job_id, state="failed", text="Задача сорвалась.",
                               error={"code": "internal", "message": str(e)[:200]})
        finally:
            self._jobs.pop(job_id, None)


def _image_size(path: Path) -> str:
    from PIL import Image
    with Image.open(path) as im:
        return f"{im.width}x{im.height}"

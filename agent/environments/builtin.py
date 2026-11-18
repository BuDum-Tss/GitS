"""Встроенные окружения (Python-плагины): сама ассистентка, система ПК.

Каждое действие ассистентки, которое раньше было отдельной веткой кода (замолчи, посмотри на
экран, напиши пост), теперь описано как инструмент и доступно через общий маршрутизатор.
Внешние окружения (RimWorld, Minecraft, вебхуки) подключаются манифестами в
environments/manifests/.
"""
import asyncio
import os
import re
import time

from services.tracing import events_log
from host import settings
from services.conversation.stems import stems
from services.tasks.service import TaskError
from services.tasks import model as task_dom
from .base import Environment, Tool, ToolError
from .bug_report import repair_status, report_bug
from gits_sdk.resources import shared_catalog


# настройки, которые модули меняют инструментом core.set_setting (TG-13): белый список, остальное только человек.
STEP_CHARS = 120                              # длина шага плана, который агент задаёт сам
MODULE_SETTINGS = ("autoplay",)       # настройки Telegram стали собственными настройками окружения Telegram (peers.telegram)


def _tool(env, name, description, params, call, risk="auto", keywords=(), self_confirming=False,
          informative=False, danger=None, read_only=False):
    return Tool(id=f"{env}.{name}", env=env, name=name, description=description, params=params,
                risk=risk, keywords=tuple(keywords), call=call, self_confirming=self_confirming,
                informative=informative, danger=danger, read_only=read_only)


def register(registry, companion, memory):
    """Собрать и добавить встроенные окружения. Все объекты живут в одном процессе ядра."""

    # ── сама ассистентка: её настройки и память ───────────────────────────────

    async def set_voice(a):
        settings.update(voice_muted=a["muted"])
        return "озвучка выключена, текст остаётся в чате" if a["muted"] else "озвучка включена"

    async def set_vision(a):
        settings.update(vision=a["enabled"])
        return "зрение включено" if a["enabled"] else "зрение выключено, на экран больше не смотрит"

    async def set_streamer(a):
        settings.update(streamer=a["enabled"])
        return ("режим стримера включён: в игре будет вслух говорить мысли, планы и цель" if a["enabled"]
                else "режим стримера выключен, в игре комментирует только заметные события")

    async def set_hearing(a):
        settings.update(hearing=a["enabled"])
        return ("слух включён (звонки и мессенджеры пропускаются)" if a["enabled"]
                else "слух выключен, системный звук не слушается")

    async def set_chattiness(a):
        settings.update(chattiness=a["level"])
        return {"quiet": "будет заговаривать сама редко", "normal": "будет заговаривать сама обычно",
                "chatty": "будет заговаривать сама часто"}[a["level"]]

    # ── самоаудит ───────────────────────────────────────────────────────────

    _CLAIM = re.compile(r"\b(опубликую|опубликовал[аи]?|отправлю|отправил[аи]?|запущу|запустил[аи]|включу|включил[аи]|"
                        r"выключу|выключил[аи]|запощу|напишу в)\b", re.I)

    def _unbacked_claims(events: list[dict]) -> list[str]:
        """Реплики, где она обещала или заявила действие, но в журнале нет ни выполненного плана, ни публикации."""
        out = []
        for e in events:
            if e["kind"] != "say" or not _CLAIM.search(e["text"]):
                continue
            window = [x for x in events if e["ts"] - 240 <= x["ts"] <= e["ts"] + 5]
            backed = any((x["kind"] == "plan" and x["text"].startswith("выполнено")) or
                         (x["kind"] == "telegram" and x["text"].startswith("опубликовано")) for x in window)
            if not backed:
                out.append(e["text"][:110])
        return out

    _analysis_running = {"on": False}

    async def deep_analysis(a):
        """Запуск глубокого анализа в фоне: чат не блокируется, итог она проговорит сама, когда он будет готов."""
        if _analysis_running["on"]:
            raise ToolError("предыдущий анализ ещё идёт, дождись его итога")
        question = (a.get("question") or "проанализируй свою работу и протокол событий").strip()
        hours = float(a.get("hours") or 6)
        now = time.time()
        claims = _unbacked_claims(events_log.tail(limit=4000, since=now - hours * 3600))
        cfg = settings.load()
        extra = "\n".join([
            "## Инструменты и окружения сейчас", registry.describe(),
            f"\n## Настройки\nголос {'выкл' if cfg['voice_muted'] else 'вкл'}, зрение {'вкл' if cfg['vision'] else 'выкл'}, "
            f"слух {'вкл' if cfg['hearing'] else 'выкл'}.",
            "\n## Обещала или заявила действие, но в журнале его нет\n" + ("\n".join(f"- {c}" for c in claims) or "нет"),
        ])

        async def job():
            from services.runtime import analysis
            _analysis_running["on"] = True
            try:
                res = await analysis.analyze(question, hours, extra)
                events_log.log("audit", f"анализ готов: {question[:80]}",
                               report=res["report_path"], fixes=res["fixes"], conclusions=res["conclusions"])
                parts = [res["summary"] or "разбор готов", f"Подробный отчёт лежит здесь: {res['report_path']}"]
                if res["fixes"]:
                    parts.append("Что стоит поправить: " + "; ".join(res["fixes"]))
                await companion._speak_about("Анализ закончен. " + "\n".join(parts), question, must=True)
            except Exception as e:
                events_log.log("error", f"анализ не удался: {e}")
                await companion._speak_about(f"Анализ не удался: {e}. Сказать честно, что именно сломалось.", question, must=True)
            finally:
                _analysis_running["on"] = False

        asyncio.create_task(job())
        return ("анализ запущен в фоне (агент читает журналы и код, это займёт несколько минут); "
                "результат она проговорит сама, когда будет готов")

    async def forget(a):
        n = companion.memory_writer.forget_matching(a["text"])
        try:                                                        # планы это задачи: совпавшие открытые закрываются как отменённые
            svc = _tasks()
            want = stems(a["text"])
            for t in await svc.list_tasks("*", kinds=(task_dom.KIND_PLAN,)):
                if want and len(want & stems(t.goal)) * 10 >= len(want) * 6:
                    await svc.close(t.id, status=task_dom.CANCELLED, note="забыто по просьбе")
                    n += 1
        except Exception:
            pass
        if not n:
            raise ToolError("ничего такого в памяти не нашлось")
        return f"стёрто записей из памяти: {n}"

    async def remember(a):
        stored = await companion.remember_text(a["text"], a.get("kind"))
        return "запомнила: " + "; ".join(stored) if stored else "это она уже помнила (запись обновлена)"

    async def remind_self(a):
        impulses = getattr(companion, "impulses", None)
        if impulses is None:
            raise ToolError("напоминания себе ещё не подключены")
        text = str(a.get("text") or "").strip()
        try:
            minutes = float(a.get("in_minutes"))
        except (TypeError, ValueError):
            raise ToolError("не указано, через сколько минут напомнить")
        if not text or minutes <= 0:
            raise ToolError("нужен текст и срок больше нуля минут")
        if minutes > 60 * 24 * 30:
            raise ToolError("напоминание можно назначить не дальше чем на 30 суток")
        source = {"kind": "conversation"}
        if a.get("priority") not in (None, ""):                      # важность для внимания v2 (core.timer); зажимается потолком вида
            from services.attention_v2.event import parse_priority
            try:
                raw = a["priority"]
                if isinstance(raw, str):
                    try:
                        raw = float(raw.replace(",", "."))
                    except ValueError:
                        pass
                source["priority"] = parse_priority(raw)
            except ValueError as e:
                raise ToolError(str(e), "bad_request")
        await impulses.timer.add(time.time() + minutes * 60, text, source=source)
        return f"напомнит себе через {int(minutes)} мин: она сама решит, сказать ли об этом, когда срок придёт"

    def _tasks():
        svc = getattr(companion, "tasks", None)
        if svc is None:
            raise ToolError("общие задачи ещё не подключены")
        return svc

    async def _task_call(coro):
        try:
            return await coro
        except TaskError as e:
            raise ToolError(str(e))

    def _task_line(t):
        done, total = task_dom.progress(t)
        bits = [f"{t.id} [{t.status}] {t.goal}"]
        if total:
            bits.append(f"шаги {done}/{total}")
            nxt = task_dom.next_step(t)
            if nxt is not None:
                bits.append(f"дальше: {nxt.text}")
        if t.due is not None:
            bits.append("срок " + time.strftime("%d.%m %H:%M", time.localtime(t.due)))
        return "; ".join(bits)

    async def list_tasks(a):
        svc = _tasks()
        ws = str(a.get("workspace") or "").strip() or None
        found = await svc.list_tasks(ws, only_open=not a.get("all"), kinds=(task_dom.KIND_TASK, task_dom.KIND_PLAN))
        if not found:
            return "в этой среде открытых задач нет" if not a.get("all") else "задач нет"
        return "\n".join(_task_line(t) for t in found)

    async def create_task(a):
        svc = _tasks()
        due = None
        if a.get("in_minutes") not in (None, ""):
            try:
                due = time.time() + float(a["in_minutes"]) * 60
            except (TypeError, ValueError):
                raise ToolError("срок в минутах должен быть числом")
        t = await _task_call(svc.create(str(a.get("goal") or ""), origin=task_dom.USER if a.get("origin") == "user" else task_dom.SELF,
                                        priority=a.get("priority") or task_dom.PRIORITY_DEFAULT, due=due, plan=bool(a.get("plan", True)),
                                        steps=_step_texts(a.get("steps"))))
        return "задача создана: " + _task_line(t)

    def _step_texts(raw):
        if raw in (None, ""):
            return ()
        if isinstance(raw, str):
            raw = [p for p in raw.splitlines()]
        if not isinstance(raw, (list, tuple)):
            raise ToolError("steps должен быть списком строк")
        return [str(s).strip()[:STEP_CHARS] for s in raw if str(s).strip()][:task_dom.STEPS_MAX]

    async def task_step(a):
        svc = _tasks()
        ref, add = a.get("step"), str(a.get("add") or "").strip()
        status = str(a.get("status") or task_dom.STEP_DONE)
        t, idx = await _task_call(svc.update_step(str(a.get("id") or ""), ref, status=status, add=add[:STEP_CHARS]))
        total, step = len(t.steps), t.steps[idx]
        label = {task_dom.STEP_DONE: "сделан", task_dom.STEP_SKIPPED: "пропущен", task_dom.STEP_PENDING: "снова в плане"}[step.status]
        if add and ref in (None, ""):
            line = f"Шаг {idx + 1} из {total} «{step.text}»: добавлен"
        else:
            line = f"Шаг {idx + 1} из {total} «{step.text}»: {label}"
        nxt = task_dom.next_step(t)
        if nxt is not None:
            line += f"; дальше: «{nxt.text}»"
        elif total:
            line += "; все шаги отмечены, закрой задачу assistant.close_task"
        if a.get("note"):
            line += f" ({str(a['note'])[:120]})"
        return line

    async def close_task(a):
        svc = _tasks()
        status = str(a.get("status") or task_dom.DONE)
        t = await _task_call(svc.close(str(a.get("id") or ""), status=status, note=str(a.get("note") or "")))
        return f"задача {t.id} закрыта ({t.status}): {t.goal}"

    async def recall(a):
        parts = []
        episodes = getattr(companion, "episodes", None)
        if episodes is not None:                                   # единая память: эпизоды (жизнь, канон, заметки) и дневник; id разворачивает recall_episode
            for h in await episodes.recall.find(a["query"], k=6):
                label = {"episode": "эпизод " + h["id"], "note": "заметка", "canon": "о себе"}.get(h["kind"], h["kind"])
                parts.append(f"{label} ({h['when']}): {h['text'][:300]}")
        parts = list(dict.fromkeys(parts))
        if not parts:
            raise ToolError("по этой теме в памяти ничего нет")
        return "; ".join(parts)

    async def recall_episode(a):
        episodes = getattr(companion, "episodes", None)
        if episodes is None:
            raise ToolError("эпизоды ещё не подключены")
        text = await episodes.recall.recall_episode(str(a.get("id") or ""))
        if not text:
            raise ToolError("такого эпизода нет")
        return text

    async def quiet_mode(a):
        companion._set_quiet(a["minutes"])
        return f"режим «не отвлекать» на {a['minutes']} мин: сама не заговаривает"

    bool_p = lambda d: {"type": "boolean", "description": d}
    registry.add(Environment(
        id="assistant", title="Сама ассистентка",
        description="Её собственные настройки и память: голос, зрение, слух, разговорчивость.",
        tools=[
            _tool("assistant", "set_voice", "Заглушить или вернуть озвучку ответов на колонках ПК (десктопный голос). Не относится к голосовым "
                  "каналам Discord: там говорят инструментом voice.say, заглушка ПК ему не мешает. Заглушку ставит владелец; "
                  "снять её можно только с его разрешения.",
                  {"muted": bool_p("true - выключить озвучку на ПК, false - включить (спросит владельца)")}, set_voice,
                  keywords=("озвучка колонки", "заглуши колонки", "без звука на ПК", "замолчи", "молчи", "тихо"),
                  danger=lambda a: None if a.get("muted") else "включить озвучку на колонках ПК: она выключена по решению владельца"),
            _tool("assistant", "set_vision", "Включить или выключить зрение: слежение за активным окном на экране.",
                  {"enabled": bool_p("true - смотреть на экран, false - не смотреть")}, set_vision,
                  keywords=("зрение", "экран", "смотри", "отвернись", "подглядывай")),
            _tool("assistant", "set_streamer", "Включить или выключить РЕЖИМ СТРИМЕРА: в игре она вслух ведёт стрим (что видит, "
                  "о чём думает, планы, цель, задачи). Для просьб «начинай стрим», «комментируй игру», «режим стримера», «хватит стримить».",
                  {"enabled": bool_p("true - включить режим стримера, false - выключить")}, set_streamer,
                  keywords=("стрим", "стример", "стримить", "комментируй", "озвучивай", "расскажи что происходит")),
            _tool("assistant", "set_hearing", "Включить или выключить слух: распознавание системного звука (речи в колонках).",
                  {"enabled": bool_p("true - слушать системный звук, false - не слушать")}, set_hearing,
                  keywords=("слух", "слушай", "звук", "колонки", "подслушивай")),
            _tool("assistant", "set_chattiness", "Как часто она САМА начинает разговор первой (не про голос). Для просьб «говори реже», «болтай больше», «не приставай».",
                  {"level": {"type": "string", "enum": ["quiet", "normal", "chatty"],
                             "description": "quiet - редко, normal - обычно, chatty - часто"}}, set_chattiness,
                  keywords=("болтай", "реже", "чаще", "приставай", "разговорчивость")),
            _tool("assistant", "deep_analysis", "ГЛУБОКИЙ АНАЛИЗ силами агента: разбор протокола событий, причин гибели колонии "
                  "или провала, своей работы и ошибок. Для просьб «проанализируй себя / протокол / свою работу», «почему погибла "
                  "колония», «что пошло не так», «сделай выводы». Идёт в фоне несколько минут: агент читает журналы и код, "
                  "пишет подробный отчёт, выводы сохраняются в память. Для таких просьб берут ЭТО, а не пересказ по памяти.",
                  {"question": {"type": "string", "description": "что именно разобрать, своими словами пользователя"},
                   "hours": {"type": "integer", "required": False, "description": "за сколько последних часов брать журнал, по умолчанию 6"}},
                  deep_analysis, informative=True,
                  keywords=("анализ", "проанализируй", "разбери", "причины", "выводы", "протокол", "аудит", "самоанализ",
                            "почему", "ошибки", "гибель", "погибла", "что пошло не так")),
            _tool("assistant", "forget", "Стереть из её памяти эпизоды и заметки, где встречается фраза (воспоминания персонажа о себе из канона не стираются).",
                  {"text": {"type": "string", "description": "слово или фраза из записи"}}, forget,
                  keywords=("забудь", "забыть", "стереть", "память")),
            _tool("assistant", "remember", "Запомнить что-то надолго по просьбе («запомни, что…»). "
                  "kind: user - факт о пользователе или его пожелание к её поведению (self то же самое), "
                  "plan - план или дело пользователя (due_days - через сколько дней; станет задачей).",
                  {"text": {"type": "string", "description": "что запомнить, одной фразой"},
                   "kind": {"type": "string", "enum": ["user", "self", "plan"], "required": False,
                            "description": "вид записи, по умолчанию user"},
                   "due_days": {"type": "integer", "required": False, "description": "срок плана в днях"}},
                  remember, keywords=("запомни", "запиши", "помни", "запомнить", "важно")),
            _tool("assistant", "remind_self", "Назначить САМОЙ СЕБЕ напоминание на потом, чтобы вернуться к теме позже (например, "
                  "когда человек сказал «напомни», «вернись к этому позже», «проверь через час»). В срок придёт внутреннее "
                  "событие, она подумает и решит, сказать, сделать или промолчать.",
                  {"text": {"type": "string", "description": "о чём вспомнить, одной фразой"},
                   "in_minutes": {"type": "number", "description": "через сколько минут напомнить себе"},
                   "priority": {"type": "string", "required": False,
                                "description": "важность: BACKGROUND, NORMAL, NOTABLE (по умолчанию), IMPORTANT или число 0-100"}},
                  remind_self, keywords=("напомни", "напоминание", "вернись", "позже", "через", "проверь", "не забудь")),
            _tool("assistant", "tasks", "Показать открытые задачи текущей среды (цель, статус, шаги, срок): что она сейчас делает и что "
                  "осталось. Для вопросов «чем ты занята», «что в планах», «какие у тебя задачи».",
                  {"workspace": {"type": "string", "required": False, "description": "среда (assistant, rimworld, montage), по умолчанию текущая; * - все"},
                   "all": {"type": "boolean", "required": False, "description": "показать и закрытые"}},
                  list_tasks, informative=True, read_only=True, keywords=("задачи", "задача", "планы", "дела", "чем занята", "список дел")),
            _tool("assistant", "create_task", "Завести задачу в текущей среде: цель, при желании срок. Если шагов нет, цель разобьётся на шаги "
                  "сама (планировщик). Для долгих дел из нескольких шагов; короткое одно действие делай сразу, без задачи.",
                  {"goal": {"type": "string", "description": "цель одной фразой"},
                   "in_minutes": {"type": "number", "required": False, "description": "срок через сколько минут"},
                   "priority": {"type": "integer", "required": False, "description": "важность 1-5, по умолчанию 3"},
                   "steps": {"type": "array", "items": {"type": "string"}, "required": False,
                             "description": "свои шаги, коротко (до 12 по 120 символов); если даны, планировщик не вызывается"},
                   "plan": {"type": "boolean", "required": False, "description": "разбить на шаги (по умолчанию да)"},
                   "origin": {"type": "string", "enum": ["user", "self"], "required": False,
                              "description": "user - просьба пользователя, self - её собственное дело"}},
                  create_task, keywords=("задача", "заведи", "запланируй", "сделай по шагам", "цель", "разбей")),
            _tool("assistant", "task_step", "Отметить шаг задачи по ходу работы (пользователь видит план): done (сделан), skipped "
                  "(пропущен) или pending (вернуть в план; это не «в работе»). Отмечай шаг сразу, как он сделан. Новый шаг в конец: add. "
                  "Когда все шаги сделаны, задача остаётся открытой: закрой её assistant.close_task.",
                  {"id": {"type": "string", "description": "id задачи"},
                   "step": {"type": "string", "required": False, "description": "номер шага с 1 или его точный текст"},
                   "status": {"type": "string", "enum": ["done", "skipped", "pending"], "required": False, "description": "по умолчанию done"},
                   "add": {"type": "string", "required": False, "description": "новый шаг в конец плана (до 12 шагов)"},
                   "note": {"type": "string", "required": False, "description": "коротко, что получилось"}},
                  task_step, keywords=("шаг", "отметь шаг", "план", "задача", "сделано", "пропусти шаг")),
            _tool("assistant", "close_task","Закрыть задачу по id: done (выполнена), failed (не вышло) или cancelled (отменена).",
                  {"id": {"type": "string", "description": "id задачи из списка задач"},
                   "status": {"type": "string", "enum": ["done", "failed", "cancelled"], "required": False, "description": "по умолчанию done"},
                   "note": {"type": "string", "required": False, "description": "коротко почему"}},
                  close_task, keywords=("задача", "закрой", "отмени задачу", "выполнено", "готово")),
            _tool("assistant", "recall", "Вспомнить, что она помнит по теме или о ком-то (для вопросов «что ты помнишь про…»).",
                  {"query": {"type": "string", "description": "тема или слова для поиска в памяти"}},
                  recall, informative=True, read_only=True, keywords=("помнишь", "вспомни", "вспомнить", "память", "знаешь")),
            _tool("assistant", "recall_episode", "Развернуть эпизод из памяти по его id (из ответа recall «эпизод e12-30»): текст эпизода и "
                  "события-источники по порядку. Для просьб «расскажи подробнее, как это было», «что именно там было сказано».",
                  {"id": {"type": "string", "description": "id эпизода, например e120-135"}},
                  recall_episode, informative=True, read_only=True, keywords=("подробнее", "как это было", "эпизод", "помнишь как", "вспомни как")),
            _tool("assistant", "quiet_mode", "Не отвлекать пользователя заданное число минут (она сама не заговаривает).",
                  {"minutes": {"type": "integer", "description": "на сколько минут, до 240"}}, quiet_mode,
                  keywords=("отвлекай", "мешай", "занят", "смотрю", "работаю", "минут", "тишина")),
        ]))

    async def web_search_tool(a):
        from services.runtime import web_search
        return await web_search.search(a["query"])

    # общий для всех профилей: поиск нужен и ассистенту, и игре, и монтажу (module=core: включён всегда)
    registry.add(Environment(
        id="web", title="Интернет",
        description="Поиск в интернете: вики, документация, форумы. Общий инструмент всех профилей.",
        tools=[_tool("web", "search", "Найти в интернете ответ на вопрос и вернуть выжимку фактов со ссылками на источники. "
                     "Для просьб «поищи», «узнай, как», «что такое», справки по играм и программам. Читает только страницы, ничего не меняет.",
                     {"query": {"type": "string", "description": "что искать; для игр и программ лучше на английском"}},
                     web_search_tool, informative=True, read_only=True,
                     keywords=("поищи", "найди", "интернет", "гугл", "google", "вики", "wiki", "узнай", "как работает", "что такое"))]))



class _ModuleIds(list):
    """Допустимые значения параметра «модуль»: подключённые и объявленные в peers.yaml модули на момент вызова.
    Список динамический: модули приходят и уходят, а описание инструмента строится один раз."""

    def __init__(self, modules):
        super().__init__()
        self._modules = modules

    def _ids(self) -> list[str]:
        return sorted(self._modules.records) if self._modules is not None else []

    def __iter__(self):
        return iter(self._ids())

    def __contains__(self, item):
        return item in self._ids()

    def __len__(self):
        return len(self._ids())

    def __bool__(self):
        return bool(self._ids())

    def __repr__(self):
        return repr(self._ids())


class _WorkspaceIds(list):
    """Допустимые значения параметра «среда»: среды, которые знает менеджер сред на момент вызова (модульные приходят и уходят)."""

    def __init__(self, workspaces, modules=None):
        super().__init__()
        self._ws = workspaces
        self._modules = modules

    def _ids(self) -> list[str]:
        if self._ws is None:
            return []
        self._ws.sync()
        ids = [s.id for s in self._ws.specs.values() if s.id != "sleep"]        # сон включает только планировщик сна
        for rec in (getattr(self._modules, "records", None) or {}).values():    # заявленные модули без подключения: ошибка скажет причину
            if rec.role == "workspace" and not rec.external and rec.id not in ids and rec.id != "sleep":
                ids.append(rec.id)
        return ids

    def __iter__(self):
        return iter(self._ids())

    def __contains__(self, item):
        return item in self._ids()

    def __len__(self):
        return len(self._ids())

    def __bool__(self):
        return bool(self._ids())

    def __repr__(self):
        return repr(self._ids())


def _why_no_workspace(workspaces, modules, ws: str) -> str:
    """Почему среды нет, человеческими словами: модуль выключен, не запущен или среды не объявляет."""
    rec = (getattr(modules, "records", None) or {}).get(ws)
    if rec is None:
        return f"среды «{ws}» не существует"
    title = rec.title or ws
    if not modules.is_enabled(ws):
        return f"модуль «{title}» выключен: его надо включить во вкладке «Модули»"
    if not getattr(rec, "manifest", None):
        return f"модуль «{title}» ещё не подключился к ядру (не запущен или упал при старте); смотри журнал logs/modules/{ws}.log"
    return f"у модуля «{title}» нет своей среды"


def _entry_text(entry) -> str:
    """Вводная модуля при входе (brief, навыки, единицы, сайт) для ответа агенту; пусто, если её нет."""
    if not isinstance(entry, dict):
        return ""
    lines = []
    if entry.get("brief"):
        lines.append(str(entry["brief"]))
    skills = [s for s in entry.get("skills") or [] if isinstance(s, dict) and s.get("name")]
    if skills:
        lines.append("Навыки (читай инструментом skills, name — точное имя): "
                     + "; ".join(f"{s['name']} — {s.get('description') or ''}".rstrip(" —") for s in skills))
    units = [u for u in entry.get("units") or [] if isinstance(u, dict) and u.get("id")]
    if units:
        lines.append("Открытые единицы: " + "; ".join(
            f"{u['id']} «{u.get('title') or ''}»" + (f" ({u['state']})" if u.get("state") else "") for u in units))
    if entry.get("site"):
        lines.append(f"Сайт: {entry['site']}")
    return "\n".join(lines)


def _workspace_tools(workspaces, modules=None) -> list[Tool]:
    """ТЗ 32, WS-5/WS-10/WS-16 и docs/WORKSPACE_CONTRACT.md (раздел 7): `workspace.enter` (занять окружение), `workspace.afk`
    (отойти, не отпуская), `workspace.exit` (выйти и отпустить) и `workspace.status`. Вход в среду модуля идёт только по импульсу:
    по просьбе пользователя (риск auto, причина `user`) или по самоимпульсу из размышления (причина `self`, перед входом обычное
    разрешение). Фокус один, закреплённых за агентом окружений может быть несколько (остальные в AFK)."""
    from services.reflection import origin as impulse_origin
    from services.spaces import model as dom

    def reason() -> str:
        return dom.Reason.SELF if impulse_origin.current() == impulse_origin.SELF else dom.Reason.USER

    def title_of(ws: str) -> str:
        spec = workspaces.specs.get(ws)
        return spec.title if spec is not None else ws

    async def enter(a):
        ws = str(a["workspace"])
        workspaces.sync()
        if ws == dom.ASSISTANT:
            raise ToolError("assistant это обычная среда без окружения: отойти из окружения - workspace.afk, закончить в нём - workspace.exit",
                            code="bad_request")
        if ws not in workspaces.specs:
            raise ToolError(_why_no_workspace(workspaces, modules, ws), code="unavailable")
        res = await workspaces.enter(ws, reason(), str(a.get("goal") or "")[:200])
        if not res.get("ok"):
            raise ToolError(str(res.get("error") or "не удалось войти в среду"), code="unavailable")
        spec = workspaces.specs[ws]
        text = f"сейчас в среде «{spec.title}»" + (" (уже была за ней, аренда прежняя)" if res.get("already") else "")
        intro = _entry_text(res.get("entry"))
        return f"{text}\n{intro}"[:1800] if intro else text

    def target(a) -> str:
        """Окружение из аргумента или текущее; в обычной среде без окружения указывать нечего."""
        ws = str(a.get("workspace") or "").strip() or workspaces.current
        if ws == dom.ASSISTANT:
            raise ToolError("сейчас ты не в окружении: выходить некуда (закреплённые за тобой смотри в workspace.status)", code="bad_request")
        return ws

    async def exit_(a):
        ws = target(a)
        res = await workspaces.exit(ws, reason())
        if not res.get("ok"):
            raise ToolError(str(res.get("error") or "не удалось выйти из среды"), code="unavailable")
        if res.get("releasing"):
            return (f"вышла из среды «{title_of(ws)}»; в ней ещё идут операции" + (f" ({res['jobs']})" if res.get("jobs") else "") + ", аренда вернётся, когда они закончатся, "
                    "новые начинать нельзя")
        return f"вышла из среды «{title_of(ws)}», аренда возвращена"

    async def afk(a):
        ws = target(a)
        res = await workspaces.afk(ws, reason())
        if not res.get("ok"):
            raise ToolError(str(res.get("error") or "не удалось отойти из среды"), code="unavailable")
        if res.get("already"):
            return f"среда «{title_of(ws)}» уже в AFK: за тобой, фокуса на ней нет"
        return (f"отошла из среды «{title_of(ws)}» (AFK): аренда и рабочая область остаются за тобой, операции идут дальше, "
                f"вернуться: workspace.enter({ws})")

    def danger(a) -> str | None:
        workspaces.sync()
        spec = workspaces.specs.get(str(a.get("workspace")))
        if reason() == dom.Reason.SELF and spec is not None and spec.kind == dom.KIND_MODULE:
            return f"войти в среду «{spec.title}» по собственному импульсу"
        return None

    def lease_text(lease: dict | None) -> str:
        """Состояние аренды общего пространства фактом (его хранит сам сервис пространства)."""
        if not lease:
            return ""
        state, holder = lease.get("state"), lease.get("holder") or {}
        if state == "free":
            return "свободно"
        if state == "mine":
            return f"моё, дорабатывает задачи ({lease.get('jobs', 0)})" if lease.get("releasing") else "занято мной"
        if state == "busy":
            goal = f", цель: {holder['purpose']}" if holder.get("purpose") else ""
            return f"занято персонажем {holder.get('character') or '?'}{goal}"
        return "недоступно" + (f" ({lease['reason']})" if lease.get("reason") else "")

    async def status(a):
        refresh = getattr(workspaces, "refresh_leases", None)
        if refresh is not None:
            await refresh()                                   # состояние аренды читается у сервисов, ядро его не хранит
        snap = workspaces.snapshot()
        held = {h["id"]: h for h in snap.get("held", [])}
        if snap["current"] in held:
            line = f"Фокус: среда «{snap['title']}» (причина: {snap['reason']})" + (f", цель: {snap['goal']}" if snap["goal"] else "") + "."
        else:
            line = f"Фокус: среда «{snap['title']}» (окружения нет, обычный разговор)."
        afk_rows, free_rows, busy_rows = [], [], []
        for x in snap["available"]:
            if x["id"] in (snap["current"], "sleep", dom.ASSISTANT):
                continue
            lease = x.get("lease")
            parts = [f"«{x['title']}» ({x['id']})"]
            if x["id"] in held:                                # закреплено за ней, но фокуса нет (AFK)
                if held[x["id"]].get("goal"):
                    parts.append(f"цель: {held[x['id']]['goal']}")
                parts.append(lease_text(lease) or "аренда за мной")
                parts.append("вернуться: workspace.enter, закончить: workspace.exit")
                afk_rows.append("; ".join(p for p in parts if p))
                continue
            if x.get("purpose"):
                parts.append(f"для чего: {x['purpose']}")
            if x.get("when_to_use"):
                parts.append(f"когда переходить: {x['when_to_use']}")
            state = lease_text(lease)
            parts.append(state or ("" if x["connected"] else "не подключена"))
            row = "; ".join(p for p in parts if p)
            (busy_rows if lease and lease.get("state") == "busy" else free_rows).append(row)
        out = [line]
        for title, rows in (("Закреплены за тобой, в AFK (аренда и работа продолжаются)", afk_rows),
                            ("Свободные среды", free_rows), ("Заняты другими", busy_rows)):
            if rows:
                out.append(title + ":" + "".join("\n- " + r for r in rows))
        return "\n".join(out)

    ids = _WorkspaceIds(workspaces, modules)
    ws_param = {"type": "string", "enum": ids, "required": False,
                "description": "id окружения; без него текущее (закреплённые за тобой смотри в workspace.status)"}
    return [
        Tool(id="workspace.enter", env="core", name="enter",
             description="Занять окружение и начать в нём работать: войти в игру («зайди в колонию», «давай поиграем») или в монтаж "
                         "(«давай монтировать»). Берёт его в аренду, переводит на него фокус; повторный вход в окружение, которое уже за тобой "
                         "(после workspace.afk), возвращает в него без новой аренды. Прежнее окружение не отпускается: уходит в AFK. "
                         "Какие есть пространства, для чего они и свободны ли, показывает workspace.status. В окружение входят только по просьбе "
                         "или собственному решению, запуск игры сам его не включает. Если модуль не подключён или выключен, инструмент вернёт "
                         "причину: перескажи её как есть.",
             params={"workspace": {"type": "string", "enum": ids, "description": "id окружения: rimworld, montage"},
                     "goal": {"type": "string", "required": False, "description": "цель входа одной фразой"}},
             risk="auto", danger=danger, call=enter, availability="always",
             keywords=("зайди", "заходи", "войди", "давай поиграем", "поиграем", "в колонию", "в игру", "давай монтировать",
                       "вернись", "среда")),
        Tool(id="workspace.exit", env="core", name="exit",
             description="Закончить работу в окружении совсем: выйти и ОТПУСТИТЬ аренду. Если там ещё идут операции, ждёт их конца (аренда "
                         "вернётся сама, новые начинать нельзя). Не для долгой операции, которую ты ждёшь: тогда workspace.afk.",
             params={"workspace": ws_param}, risk="auto", call=exit_, availability="always",
             keywords=("выйди", "выходи", "закончила", "хватит играть", "закончили монтаж", "вернись", "среда")),
        Tool(id="workspace.afk", env="core", name="afk",
             description="Отойти из окружения, не отпуская: фокус снят, аренда и рабочая область остаются за тобой, операции идут (например, "
                         "долгий рендер). Итог придёт событием, вернуться можно workspace.enter. Закончила совсем: workspace.exit.",
             params={"workspace": ws_param}, risk="auto", call=afk, availability="always",
             keywords=("отойди", "afk", "пока рендерится", "жди рендер", "среда")),
        Tool(id="workspace.status", env="core", name="status",
             description="В какой среде фокус агента, какие окружения закреплены за ней (в AFK), какие свободны и какие заняты другими. "
                         "Для вопросов «чем ты занята», «где ты сейчас».",
             params={}, risk="auto", call=status, informative=True, read_only=True, availability="always",
             keywords=("чем занята", "где ты", "в какой среде", "что делаешь", "среда")),
    ]


def register_core_tools(registry, modules, attention=None, workspaces=None):
    """Инструменты ядра про задачи окружений (окружение core, всегда видны, ТЗ 02 раздел 11) и про внимание (ТЗ 03, ATT-24).
    modules - PeerRegistry (окружения и сервисы): jobs_all() и async cancel_job(module, job_id); attention - менеджер внимания (attention.py)."""

    async def jobs(a):
        rows = modules.jobs_all()
        if not rows:
            return "задач у окружений сейчас нет"
        lines = []
        for j in rows:
            pct = f", {round(j['progress'] * 100)}%" if isinstance(j.get("progress"), (int, float)) else ""
            stage = f", стадия {j['stage']}" if j.get("stage") else ""
            lines.append(f"- {j['module']} {j['job_id']}: {j.get('title') or 'без названия'}, {j['state']}{stage}{pct}")
        return "Задачи модулей:\n" + "\n".join(lines)

    async def modules_status(a):
        report = modules.status_report()
        ids = []
        if workspaces is not None:
            workspaces.sync()
            ids = [s.id for s in workspaces.specs.values() if s.kind == "module" and s.id != workspaces.current]
        if ids:                                               # «не подключён» у модуля с окружением значит «ты в него не входила»
            report += ("\nМодули с окружением (" + ", ".join(ids) + ") подключаются, когда ты в них входишь: "
                       "workspace.enter(workspace=<id>, goal=<цель>), «не подключён» тут не значит «сломан».")
        return report

    async def module_events(a):
        return modules.events_report(a.get("module"), a.get("limit") or 15)

    async def job_cancel(a):
        return await modules.cancel_job(a["module"], a["job_id"])

    async def set_setting(a):
        key = a["key"]
        if key not in MODULE_SETTINGS:
            raise ToolError(f"настройку «{key}» модули менять не могут", code="denied")
        if not isinstance(a["value"], bool):
            raise ToolError("значение должно быть true или false", code="bad_request")
        settings.update(**{key: a["value"]})
        return f"настройка {key}: {'включена' if a['value'] else 'выключена'}"

    async def set_rule(a):
        return await attention.set_rule(module=a.get("module"), kind=a.get("kind"), sender=a.get("sender"),
                                        effect=a["effect"], minutes=a.get("minutes") or 60)

    async def rules(a):
        return attention.rules_text()

    async def clear_rules(a):
        return await attention.clear_rules(a.get("id"))

    async def digest(a):
        return attention.take_digest(a.get("module"))

    def attention_tool(name, description, params, call, informative=False, keywords=(), read_only=False):
        # id attention.<имя>, а окружение core: Tool.id не обязан начинаться с id окружения
        return Tool(id=f"attention.{name}", env="core", name=name, description=description, params=params, risk="auto",
                    keywords=tuple(keywords), call=call, informative=informative, availability="always", read_only=read_only)

    attention_tools = [] if attention is None else [
        attention_tool(
            "set_rule", "Временное правило внимания: пользователь просит не отвлекать, заглушить, выделить кого-то или что-то на "
            "время: «пока играю, из телеги только мама», «час не трогай меня с чатами». Нужно указать хотя бы одно из: модуль, "
            "вид события, отправитель.",
            {"module": {"type": "string", "required": False, "enum": _ModuleIds(modules),
                        "description": "id модуля, на который распространяется правило"},
             "kind": {"type": "string", "required": False, "description": "шаблон вида события, например telegram.*"},
             "sender": {"type": "string", "required": False, "description": "часть имени отправителя, например мама"},
             "effect": {"type": "string", "enum": ["mute", "boost", "only", "normal"],
                        "description": "mute - молчать (в сводку), boost - выделить, only - только это, остальное тише, "
                                       "normal - снять прежние правила"},
             "minutes": {"type": "integer", "required": False, "description": "на сколько минут, 1-1440, по умолчанию 60"}},
            set_rule, keywords=("отвлекай", "не трогай", "заглуши", "выдели", "только", "на время", "час", "пока играю", "правило")),
        attention_tool("rules", "Какие временные правила внимания действуют сейчас.", {}, rules, informative=True, read_only=True,
                       keywords=("правила", "что заглушено", "не отвлекать")),
        attention_tool("clear_rules", "Снять временные правила внимания: одно по номеру или все сразу.",
                       {"id": {"type": "string", "required": False, "description": "номер правила вида r-xxxxxx; без него снимаются все"}},
                       clear_rules, keywords=("сними", "отмени правило", "снова отвлекай")),
        attention_tool("digest", "Что накопилось и не было сказано, пока не отвлекала: сводка событий модулей. Для просьб "
                       "«что я пропустила», «что там было», «что накопилось». После выдачи сводка очищается.",
                       {"module": {"type": "string", "required": False, "enum": _ModuleIds(modules),
                                   "description": "id модуля; без него по всем"}},
                       digest, informative=True, keywords=("пропустила", "накопилось", "сводка", "что было", "пока не отвлекала")),
    ]

    workspace_tools = [] if workspaces is None else _workspace_tools(workspaces, modules)     # ТЗ 32: среды агента
    async def resources(a):
        found = await asyncio.to_thread(shared_catalog, a.get("query") or "")
        offset = max(0, a.get("offset") or 0)
        page = found[offset:offset + 5]
        if offset + len(page) < len(found):
            page.append(f"Следующая страница: offset={offset + len(page)} с тем же query.")
        return "\n".join(page) or "Подходящих файлов нет."

    repair_tools = []
    if os.environ.get("GITS_REPAIR_QUEUE"):
        repair_tools.append(_tool(
            "core", "bug_report",
            "Поставить подтверждённый баг GitS в очередь автономного исправления. "
            "Можно вызывать из любого окружения. Передай симптомы, воспроизведение, ожидаемое и фактическое "
            "поведение. Убери секреты и личные данные. Используй для наблюдаемого сбоя или сообщения владельца.",
            {"summary": {"type": "string", "description": "Компонент и суть бага"},
             "steps": {"type": "string", "description": "Подробные шаги воспроизведения и исходные условия"},
             "expected": {"type": "string", "description": "Как система должна была повести себя"},
             "actual": {"type": "string", "description": "Что произошло, включая релевантные ошибки"},
             "screenshot": {"type": "string", "required": False,
                            "description": "Имя снимка из desktop.take_screenshot без личных данных"}},
            report_bug, informative=True, keywords=("баг", "ошибка", "сломалось", "исправь", "почини"),
        ))
        repair_tools.append(_tool(
            "core", "bug_status", "Статус исправления бага, ссылка на GitHub Issue и итог передеплоя.",
            {"request_id": {"type": "string", "description": "Номер из core.bug_report"}},
            repair_status, informative=True, keywords=("баг", "статус", "исправление"),
        ))
    registry.add(Environment(
        id="core", title="Ядро: задачи окружений и внимание",
        description="Долгие задачи окружений (монтаж, рендер и т.п.): посмотреть список и отменить. Внимание: временные правила "
                    "«не отвлекай / выдели» и сводка накопившегося. Общие настройки Telegram для модулей.",
        tools=attention_tools + workspace_tools + repair_tools + [
            _tool("core", "resources", "Рабочие файлы персонажа, включая дизайн и готовые ролики монтажа. Доступно в любом окружении. "
                  "Идентификатор MP4 можно передать в telegram.message (files) после перехода в Telegram.",
                  {"query": {"type": "string", "required": False, "description": "часть имени файла или папки"},
                   "offset": {"type": "integer", "required": False, "description": "позиция следующей страницы"}},
                  resources, informative=True, read_only=True, keywords=("ресурсы", "файлы", "ролик", "видео", "экспорт")),
            _tool("core", "jobs", "Перечень задач всех окружений: окружение, название, состояние, стадия, прогресс. "
                  "Для вопросов «что сейчас делается», «как там рендер», «какие задачи идут».",
                  {}, jobs, informative=True, read_only=True, keywords=("задачи", "задача", "рендер", "прогресс", "идёт", "готово")),
            _tool("core", "modules_status", "Состояние модулей: какие подключены, включены ли и что у каждого сейчас (игра, Telegram, "
                  "голос, питомец, компьютер, монтаж). В контекст состояние не кладётся: узнавай им, когда спрашивают или нужно знать. "
                  "Для вопросов «что с модулями», «запущена ли игра», «работает ли телеграм».",
                  {}, modules_status, informative=True, read_only=True, keywords=("модули", "модуль", "статус", "состояние", "подключён", "работает")),
            _tool("core", "module_events", "Недавние события модулей (что происходило в игре RimWorld, Telegram, на компьютере): "
                  "в контекст они не кладутся, читай их этим инструментом, когда спрашивают «что было в игре», «что нового» "
                  "или нужно знать, что произошло.",
                  {"module": {"type": "string", "required": False, "enum": _ModuleIds(modules), "description": "id модуля; без него все"},
                   "limit": {"type": "integer", "required": False, "description": "сколько событий, по умолчанию 15"}},
                  module_events, informative=True, read_only=True, keywords=("события", "что было", "что произошло", "что нового", "в игре", "колония")),
            _tool("core", "set_setting", "Включить или выключить общую настройку ядра: "
                  "autoplay (играть самой: рабочие циклы в среде игры; включай, когда пользователь просит начать стрим или играть сама, выключай, когда просит закончить).",
                  {"key": {"type": "string", "enum": list(MODULE_SETTINGS), "description": "какая настройка"},
                   "value": {"type": "boolean", "description": "true - включить, false - выключить"}},
                  set_setting, keywords=("настройка",)),
            _tool("core", "job_cancel", "Отменить задачу окружения (по окружению и номеру задачи из перечня задач). "
                  "Спрашивать разрешения не нужно.",
                  {"module": {"type": "string", "description": "id окружения, например montage"},
                   "job_id": {"type": "string", "description": "номер задачи вида j-xxxxxxxx"}},
                  job_cancel, risk="auto", informative=True, keywords=("отмени", "останови", "задачу", "рендер")),
        ]))

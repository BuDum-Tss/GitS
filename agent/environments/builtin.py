"""Встроенные окружения (Python-плагины): сама ассистентка, система ПК.

Каждое действие ассистентки, которое раньше было отдельной веткой кода (замолчи, посмотри на
экран, напиши пост), теперь описано как инструмент и доступно через общий маршрутизатор.
Внешние окружения (RimWorld, Minecraft, вебхуки) подключаются манифестами в
environments/manifests/.
"""
import asyncio
import re
import time

import events_log
import settings
from .base import Environment, Tool, ToolError


# настройки, которые модули меняют инструментом core.set_setting (TG-13): белый список, остальное только человек.
# pet_visible добавлена по решению владельца (TZ 15b): кнопки «Показать» и «Скрыть» в пространстве модуля pet
MODULE_SETTINGS = ("telegram", "telegram_after_run", "telegram_voice", "telegram_confirm", "pet_visible")


def _tool(env, name, description, params, call, risk="auto", keywords=(), self_confirming=False,
          informative=False):
    return Tool(id=f"{env}.{name}", env=env, name=name, description=description, params=params,
                risk=risk, keywords=tuple(keywords), call=call, self_confirming=self_confirming,
                informative=informative)


def register(registry, companion, memory, profiles=None):
    """Собрать и добавить встроенные окружения. Все объекты живут в одном процессе ядра.
    profiles - ProfileManager (profile_manager.py): нужен инструменту assistant.set_profile."""

    # ── сама ассистентка: её настройки и память ───────────────────────────────

    async def set_voice(a):
        settings.update(voice_muted=a["muted"])
        return "озвучка выключена, текст остаётся в чате" if a["muted"] else "озвучка включена"

    async def set_vision(a):
        settings.update(vision=a["enabled"])
        return "зрение включено" if a["enabled"] else "зрение выключено, на экран больше не смотрит"

    profile_ids = profiles.choices() if profiles is not None else ["auto"]

    async def set_profile(a):
        if profiles is None:
            raise ToolError("профили не подключены")
        err = profiles.set_override(a["mode"])
        if err:
            raise ToolError(err)
        return "профиль выбирается автоматически" if a["mode"] == "auto" else f"профиль: {profiles.current().title}"

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
            f"слух {'вкл' if cfg['hearing'] else 'выкл'}, Telegram-посты {'вкл' if cfg['telegram'] else 'выкл'}.",
            "\n## Обещала или заявила действие, но в журнале его нет\n" + ("\n".join(f"- {c}" for c in claims) or "нет"),
        ])

        async def job():
            import analysis
            _analysis_running["on"] = True
            try:
                res = await analysis.analyze(question, hours, extra)
                saved = sum(bool(memory.add_fact(c, kind="self", importance=4)) for c in res["conclusions"])
                events_log.log("audit", f"анализ готов: {question[:80]} (выводов в память: {saved})",
                               report=res["report_path"], fixes=res["fixes"], conclusions=res["conclusions"])
                parts = [res["summary"] or "разбор готов", f"Подробный отчёт лежит здесь: {res['report_path']}"]
                if res["fixes"]:
                    parts.append("Что стоит поправить: " + "; ".join(res["fixes"]))
                if res["conclusions"]:
                    parts.append(f"Выводов сохранено в память: {saved}.")
                await companion._speak_about("Анализ закончен. " + "\n".join(parts), question, must=True)
            except Exception as e:
                events_log.log("error", f"анализ не удался: {e}")
                await companion._speak_about(f"Анализ не удался: {e}. Сказать честно, что именно сломалось.", question, must=True)
            finally:
                _analysis_running["on"] = False

        asyncio.create_task(job())
        return ("анализ запущен в фоне (Claude читает журналы и код, это займёт несколько минут); "
                "результат она проговорит сама, когда будет готов")

    async def forget(a):
        n = memory.forget_matching(a["text"])
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
        await impulses.timer.add(time.time() + minutes * 60, text, source={"kind": "conversation"})
        return f"напомнит себе через {int(minutes)} мин: она сама решит, сказать ли об этом, когда срок придёт"

    async def recall(a):
        parts = [i["text"] for i in memory.store.search(a["query"])]
        graph = getattr(companion, "graph", None)
        if graph is not None:
            parts += [f["fact"] for f in await graph.search(a["query"], limit=6)]     # память-граф: факты о людях и жизни
        episodes = getattr(companion, "episodes", None)
        if episodes is not None:                                   # ТЗ 27: эпизоды и дневник; id разворачивает recall_episode
            for h in await episodes.recall.find(a["query"], k=3):
                parts.append(f"{'эпизод ' + h['id'] if h['kind'] == 'episode' else h['kind']} ({h['when']}): {h['text'][:300]}")
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
            _tool("assistant", "set_voice", "Заглушить или вернуть её ГОЛОС (озвучку ответов вслух). Для просьб «замолчи», «выключи голос», «без звука», «говори вслух».",
                  {"muted": bool_p("true - выключить озвучку, false - включить")}, set_voice,
                  keywords=("голос", "озвучка", "звук", "замолчи", "молчи", "тихо", "заглуши", "говори", "вслух")),
            _tool("assistant", "set_vision", "Включить или выключить зрение: слежение за активным окном на экране.",
                  {"enabled": bool_p("true - смотреть на экран, false - не смотреть")}, set_vision,
                  keywords=("зрение", "экран", "смотри", "отвернись", "подглядывай")),
            _tool("assistant", "set_profile", "Выбрать профиль (чем она сейчас занята): " + " / ".join(profile_ids) + ". "
                  "auto: сама определяет по окну и играм. Для просьб «я работаю, веди себя тише», «режим игры», «определяй сама».",
                  {"mode": {"type": "string", "description": " / ".join(profile_ids), "enum": profile_ids}},
                  set_profile, keywords=("режим", "работаю", "работа", "играю", "тише", "профиль")),
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
            _tool("assistant", "deep_analysis", "ГЛУБОКИЙ АНАЛИЗ силами Claude: разбор протокола событий, причин гибели колонии "
                  "или провала, своей работы и ошибок. Для просьб «проанализируй себя / протокол / свою работу», «почему погибла "
                  "колония», «что пошло не так», «сделай выводы». Идёт в фоне несколько минут: Claude читает журналы и код, "
                  "пишет подробный отчёт, выводы сохраняются в память. Для таких просьб берут ЭТО, а не пересказ по памяти.",
                  {"question": {"type": "string", "description": "что именно разобрать, своими словами пользователя"},
                   "hours": {"type": "integer", "required": False, "description": "за сколько последних часов брать журнал, по умолчанию 6"}},
                  deep_analysis, informative=True,
                  keywords=("анализ", "проанализируй", "разбери", "причины", "выводы", "протокол", "аудит", "самоанализ",
                            "почему", "ошибки", "гибель", "погибла", "что пошло не так")),
            _tool("assistant", "forget", "Стереть из её долговременной памяти записи, где встречается фраза.",
                  {"text": {"type": "string", "description": "слово или фраза из записи"}}, forget,
                  keywords=("забудь", "забыть", "стереть", "память")),
            _tool("assistant", "remember", "Запомнить что-то надолго по просьбе («запомни, что…»). "
                  "kind: user - факт о пользователе, self - о ней самой или пожелание к её поведению, "
                  "plan - план или дело пользователя (due_days - через сколько дней), episode - событие.",
                  {"text": {"type": "string", "description": "что запомнить, одной фразой"},
                   "kind": {"type": "string", "enum": ["user", "self", "plan", "episode"], "required": False,
                            "description": "вид записи, по умолчанию user"},
                   "due_days": {"type": "integer", "required": False, "description": "срок плана в днях"}},
                  remember, keywords=("запомни", "запиши", "помни", "запомнить", "важно")),
            _tool("assistant", "remind_self", "Назначить САМОЙ СЕБЕ напоминание на потом, чтобы вернуться к теме позже (например, "
                  "когда человек сказал «напомни», «вернись к этому позже», «проверь через час»). В срок придёт внутреннее "
                  "событие, она подумает и решит, сказать, сделать или промолчать.",
                  {"text": {"type": "string", "description": "о чём вспомнить, одной фразой"},
                   "in_minutes": {"type": "number", "description": "через сколько минут напомнить себе"}},
                  remind_self, keywords=("напомни", "напоминание", "вернись", "позже", "через", "проверь", "не забудь")),
            _tool("assistant", "recall", "Вспомнить, что она помнит по теме или о ком-то (для вопросов «что ты помнишь про…»).",
                  {"query": {"type": "string", "description": "тема или слова для поиска в памяти"}},
                  recall, informative=True, keywords=("помнишь", "вспомни", "вспомнить", "память", "знаешь")),
            _tool("assistant", "recall_episode", "Развернуть эпизод из памяти по его id (из ответа recall «эпизод e12-30»): резюме, "
                  "цитаты и события-источники по порядку. Для просьб «расскажи подробнее, как это было», «что именно там было сказано».",
                  {"id": {"type": "string", "description": "id эпизода, например e120-135"}},
                  recall_episode, informative=True, keywords=("подробнее", "как это было", "эпизод", "помнишь как", "вспомни как")),
            _tool("assistant", "quiet_mode", "Не отвлекать пользователя заданное число минут (она сама не заговаривает).",
                  {"minutes": {"type": "integer", "description": "на сколько минут, до 240"}}, quiet_mode,
                  keywords=("отвлекай", "мешай", "занят", "смотрю", "работаю", "минут", "тишина")),
        ]))

    async def web_search_tool(a):
        import web_search
        return await web_search.search(a["query"])

    async def knowledge_lookup(a):
        kb = getattr(companion, "knowledge", None)
        if kb is None:
            raise ToolError("справочник ещё не подключён")
        query = str(a.get("query") or "").strip()
        if not query:
            raise ToolError("нужен запрос")
        text = await kb.tool_lookup(query, str(a.get("topic") or ""))
        if not text:
            raise ToolError("в справочнике по этой теме ничего не нашлось")
        return text

    # справочник (ТЗ 04, KB-8): без интернета и каскада; для сети есть отдельный web.search
    registry.add(Environment(
        id="knowledge", title="Справочник",
        description="Справочные материалы и накопленный опыт по темам (вики игр, документация): как устроено, сколько нужно, что такое.",
        tools=[_tool("knowledge", "lookup", "Найти справку в её справочнике по теме (вики и описания игр, регламенты, накопленный опыт): "
                     "как устроено, сколько нужно, что такое, как лучше. Возвращает куски текста с названием источника. "
                     "Если в справочнике нет, для сети есть отдельный поиск в интернете.",
                     {"query": {"type": "string", "description": "что узнать, как вопрос или ключевые слова"},
                      "topic": {"type": "string", "required": False, "description": "область справочника, например rimworld (необязательно: без неё ищет по справочнику профиля)"}},
                     knowledge_lookup, informative=True,
                     keywords=("справочник", "справка", "вики", "wiki", "как устроено", "сколько нужно", "что такое", "регламент",
                               "руководство", "документация"))]))

    # общий для всех профилей: поиск нужен и ассистенту, и игре, и монтажу (module=core: включён всегда)
    registry.add(Environment(
        id="web", title="Интернет",
        description="Поиск в интернете: вики, документация, форумы. Общий инструмент всех профилей.",
        tools=[_tool("web", "search", "Найти в интернете ответ на вопрос и вернуть выжимку фактов со ссылками на источники. "
                     "Для просьб «поищи», «узнай, как», «что такое», справки по играм и программам. Читает только страницы, ничего не меняет.",
                     {"query": {"type": "string", "description": "что искать; для игр и программ лучше на английском"}},
                     web_search_tool, informative=True,
                     keywords=("поищи", "найди", "интернет", "гугл", "google", "вики", "wiki", "узнай", "как работает", "что такое"))]))



class _ModuleIds(list):
    """Допустимые значения параметра «модуль»: подключённые и объявленные в modules.yaml модули на момент вызова.
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


def _focus_tool(focus, modules) -> Tool:
    """ТЗ 18, FOC-6: команды фокуса в чате и голосом - закрепить, снять закрепление, «не беспокоить», разовое исключение.
    Это просьба самого пользователя, поэтому риск auto; фокус модулям по-прежнему передают только правила менеджера."""
    async def control(a):
        action, module = a["action"], a.get("module")
        if action in ("pin", "allow_once") and not module:
            raise ToolError("назови модуль, для которого это нужно", code="bad_request")
        if action == "pin":
            res = await focus.pin(module)
            if not res.get("ok"):
                raise ToolError("фокус не закреплён", code="internal")
            return f"фокус закреплён за модулем «{module}»"
        if action == "unpin":
            await focus.pin(None)
            return "закрепление фокуса снято"
        if action in ("dnd_on", "dnd_off"):
            target = module or "all"
            await focus.set_dnd(target, action == "dnd_on")
            where = "для всего агента" if target == "all" else f"для модуля «{target}»"
            return f"«не беспокоить» {where}: {'включено' if action == 'dnd_on' else 'выключено'}"
        await focus.allow_once(module)
        return f"разовое исключение из режима «не беспокоить» для модуля «{module}»"

    return Tool(id="focus.control", env="core", name="control",
                description="Фокус агента по просьбе пользователя: закрепить фокус за модулем («держи фокус на игре»), снять "
                            "закрепление, включить или выключить «не беспокоить» (для всего агента или для модуля), разрешить "
                            "одно сообщение модуля сквозь «не беспокоить».",
                params={"action": {"type": "string", "enum": ["pin", "unpin", "dnd_on", "dnd_off", "allow_once"],
                                   "description": "что сделать"},
                        "module": {"type": "string", "required": False, "enum": _ModuleIds(modules),
                                   "description": "id модуля; для dnd без него - для всего агента"}},
                risk="auto",
                keywords=("закрепи", "держи фокус", "фокус", "не беспокоить", "не отвлекай", "разреши один раз"),
                call=control, informative=True, availability="always")


class _WorkspaceIds(list):
    """Допустимые значения параметра «среда»: среды, которые знает менеджер сред на момент вызова (модульные приходят и уходят)."""

    def __init__(self, workspaces):
        super().__init__()
        self._ws = workspaces

    def _ids(self) -> list[str]:
        if self._ws is None:
            return []
        self._ws.sync()
        return [s.id for s in self._ws.specs.values() if s.id != "sleep"]       # сон включает только планировщик сна

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


def _workspace_tools(workspaces) -> list[Tool]:
    """ТЗ 32, WS-5/WS-10: сменить среду агента и узнать текущую. Вход в среду модуля идёт только по импульсу: по просьбе
    пользователя (риск auto, причина `user`) или по самоимпульсу из размышления (причина `self`, перед входом обычное разрешение)."""
    from app import impulse_origin
    from domain import workspace as dom

    def reason() -> str:
        return dom.Reason.SELF if impulse_origin.current() == impulse_origin.SELF else dom.Reason.USER

    async def switch(a):
        ws = str(a["workspace"])
        res = await workspaces.enter(ws, reason(), str(a.get("goal") or "")[:200])
        if not res.get("ok"):
            raise ToolError(str(res.get("error") or "не удалось сменить среду"), code="unavailable")
        spec = workspaces.specs[ws]
        return f"сейчас в среде «{spec.title}»" + (" (уже была в ней)" if res.get("already") else "")

    def danger(a) -> str | None:
        workspaces.sync()
        spec = workspaces.specs.get(str(a.get("workspace")))
        if reason() == dom.Reason.SELF and spec is not None and spec.kind == dom.KIND_MODULE:
            return f"войти в среду «{spec.title}» по собственному импульсу"
        return None

    async def status(a):
        snap = workspaces.snapshot()
        sig = snap["signals"]
        line = f"Сейчас в среде «{snap['title']}» (причина: {snap['reason']})" + (f", цель: {snap['goal']}" if snap["goal"] else "") + "."
        others = ", ".join(f"«{x['title']}»" + ("" if x["connected"] else " (не подключена)")
                           for x in snap["available"] if x["id"] != snap["current"] and x["id"] != "sleep")
        return line + (f" Другие среды: {others}." if others else "") + (
            f" Сигналов из чужих сред: {sig['notify']} уведомлений, {sig['digest']} в сводке." if sig["notify"] or sig["digest"] else "")

    ids = _WorkspaceIds(workspaces)
    return [
        Tool(id="workspace.switch", env="core", name="switch",
             description="Сменить среду агента: войти в игру («зайди в колонию», «давай поиграем»), в монтаж («давай монтировать») "
                         "или вернуться в обычную среду assistant. В среду модуля входят только по просьбе или собственному решению, "
                         "запуск игры сам её не включает.",
             params={"workspace": {"type": "string", "enum": ids, "description": "id среды: assistant, rimworld, montage"},
                     "goal": {"type": "string", "required": False, "description": "цель входа одной фразой"}},
             risk="auto", danger=danger, call=switch, availability="always",
             keywords=("зайди", "заходи", "войди", "давай поиграем", "поиграем", "в колонию", "в игру", "давай монтировать",
                       "вернись", "среда")),
        Tool(id="workspace.status", env="core", name="status",
             description="В какой среде сейчас агент, почему и какие есть другие. Для вопросов «чем ты занята», «где ты сейчас».",
             params={}, risk="auto", call=status, informative=True, availability="always",
             keywords=("чем занята", "где ты", "в какой среде", "что делаешь", "среда")),
    ]


def register_core_tools(registry, modules, attention=None, focus=None, switch=None, workspaces=None):
    """Инструменты ядра про задачи модулей (окружение core, всегда видны, ТЗ 02 раздел 11) и про внимание (ТЗ 03, ATT-24).
    modules - ModuleRegistry: jobs_all() и async cancel_job(module, job_id); attention - менеджер внимания (attention.py);
    focus - менеджер фокуса (focus.py, ТЗ 18): закрепить фокус, «не беспокоить», разовое исключение голосом."""

    async def jobs(a):
        rows = modules.jobs_all()
        if not rows:
            return "задач у модулей сейчас нет"
        lines = []
        for j in rows:
            pct = f", {round(j['progress'] * 100)}%" if isinstance(j.get("progress"), (int, float)) else ""
            stage = f", стадия {j['stage']}" if j.get("stage") else ""
            lines.append(f"- {j['module']} {j['job_id']}: {j.get('title') or 'без названия'}, {j['state']}{stage}{pct}")
        return "Задачи модулей:\n" + "\n".join(lines)

    async def job_cancel(a):
        return await modules.cancel_job(a["module"], a["job_id"])

    async def set_character(a):
        if switch is None:
            raise ToolError("смена персонажа пока недоступна", code="unavailable")
        return await switch(str(a["id"]))

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

    def attention_tool(name, description, params, call, informative=False, keywords=()):
        # id attention.<имя>, а окружение core: Tool.id не обязан начинаться с id окружения
        return Tool(id=f"attention.{name}", env="core", name=name, description=description, params=params, risk="auto",
                    keywords=tuple(keywords), call=call, informative=informative, availability="always")

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
        attention_tool("rules", "Какие временные правила внимания действуют сейчас.", {}, rules, informative=True,
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

    focus_tools = [] if focus is None else [_focus_tool(focus, modules)]     # ТЗ 18: независимо от внимания
    focus_tools += [] if workspaces is None else _workspace_tools(workspaces)     # ТЗ 32: среды агента
    registry.add(Environment(
        id="core", title="Ядро: задачи модулей и внимание",
        description="Долгие задачи модулей (монтаж, рендер и т.п.): посмотреть список и отменить. Внимание: временные правила "
                    "«не отвлекай / выдели» и сводка накопившегося. Общие настройки Telegram для модулей.",
        tools=attention_tools + focus_tools + [
            _tool("core", "jobs", "Перечень задач всех модулей: модуль, название, состояние, стадия, прогресс. "
                  "Для вопросов «что сейчас делается», «как там рендер», «какие задачи идут».",
                  {}, jobs, informative=True, keywords=("задачи", "задача", "рендер", "прогресс", "идёт", "готово")),
            _tool("core", "set_setting", "Включить или выключить общую настройку для модулей: telegram (посты-мысли), "
                  "telegram_after_run (пост-итог после забега), telegram_voice (посты голосом), telegram_confirm (спрашивать "
                  "перед публикацией), pet_visible (показывать окно питомца).",
                  {"key": {"type": "string", "enum": list(MODULE_SETTINGS), "description": "какая настройка"},
                   "value": {"type": "boolean", "description": "true - включить, false - выключить"}},
                  set_setting, keywords=("настройка",)),
            _tool("core", "set_character", "Сменить персонажа на другого из папки characters (id персонажа). Вместе с персонажем переключается его память и голос. Для просьб «смени персонажа», «переключись на ...».",
                  {"id": {"type": "string", "description": "id персонажа из папки characters (список в состоянии дашборда)"}},
                  set_character, risk="ask", keywords=("персонаж", "смени", "переключись", "переключи")),
            _tool("core", "job_cancel", "Отменить задачу модуля (по модулю и номеру задачи из перечня задач).",
                  {"module": {"type": "string", "description": "id модуля, например montage"},
                   "job_id": {"type": "string", "description": "номер задачи вида j-xxxxxxxx"}},
                  job_cancel, risk="ask", informative=True, keywords=("отмени", "останови", "задачу", "рендер")),
        ]))

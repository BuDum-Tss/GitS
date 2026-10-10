"""Встроенные окружения (Python-плагины): сама ассистентка, система ПК, соцсети.

Каждое действие ассистентки, которое раньше было отдельной веткой кода (замолчи, посмотри на
экран, напиши пост), теперь описано как инструмент и доступно через общий маршрутизатор.
Внешние окружения (RimWorld, Minecraft, вебхуки) подключаются манифестами в
environments/manifests/.
"""
import asyncio
import re
import time

import events_log
import perception
import screenshots
import settings
from .base import Environment, Tool, ToolError
from . import pc_control


def _tool(env, name, description, params, call, risk="auto", keywords=(), self_confirming=False,
          informative=False):
    return Tool(id=f"{env}.{name}", env=env, name=name, description=description, params=params,
                risk=risk, keywords=tuple(keywords), call=call, self_confirming=self_confirming,
                informative=informative)


def register(registry, companion, hearing, telegram, memory, tg_user=None, profiles=None):
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

    async def set_telegram_periodic(a):
        settings.update(telegram=a["enabled"])
        return ("периодические посты в Telegram включены" if a["enabled"]
                else "периодические посты в Telegram выключены")

    async def set_telegram_after_run(a):
        settings.update(telegram_after_run=a["enabled"])
        return ("после каждого забега (гибели колонии) будет писать пост-итог в канал" if a["enabled"]
                else "постов-итогов после забега больше не будет")

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

    async def name_voice(a):
        vid = getattr(companion, "voice_id", None)
        last = getattr(companion, "last_voice", None)
        if vid is None or not last or not last.get("id") or str(last["id"]).startswith("SPEAKER"):
            raise ToolError("голос не узнан: микрофон выключен, сервер WhisperX не запущен или голоса пока не было")
        name = a["name"].strip()
        if not vid.set_name(last["id"], name):
            raise ToolError("такого голоса нет в памяти")
        if a.get("is_owner"):
            vid.set_owner(last["id"])
            memory.add_fact(f"Имя: {name}", kind="user", importance=5)
            return f"запомнила: этот голос принадлежит тебе, тебя зовут {name}"
        rel = getattr(companion, "_rel", None)
        if rel is not None:
            rel.ensure_person(f"voice:{last['id']}", name, source="voice")
        return f"запомнила: этот голос принадлежит человеку по имени {name}"

    async def forget(a):
        n = memory.forget_matching(a["text"])
        if not n:
            raise ToolError("ничего такого в памяти не нашлось")
        return f"стёрто записей из памяти: {n}"

    async def remember(a):
        stored = await companion.remember_text(a["text"], a.get("kind"))
        return "запомнила: " + "; ".join(stored) if stored else "это она уже помнила (запись обновлена)"

    async def recall(a):
        parts = [i["text"] for i in memory.store.search(a["query"])]
        graph = getattr(companion, "graph", None)
        if graph is not None:
            parts += [f["fact"] for f in await graph.search(a["query"], limit=6)]     # память-граф: факты о людях и жизни
        parts = list(dict.fromkeys(parts))
        if not parts:
            raise ToolError("по этой теме в памяти ничего нет")
        return "; ".join(parts)

    async def launch_app(a):
        import app_launcher
        try:
            return await asyncio.to_thread(app_launcher.launch, a["name"])
        except (ValueError, RuntimeError) as e:
            raise ToolError(str(e))

    async def list_apps(a):
        import app_launcher
        return "Подходящие приложения: " + await asyncio.to_thread(app_launcher.titles, a.get("query") or "")

    async def add_app(a):
        import app_launcher
        try:
            return await asyncio.to_thread(app_launcher.add, a["title"], a["path"], a.get("aliases") or [])
        except ValueError as e:
            raise ToolError(str(e))

    async def quiet_mode(a):
        companion._set_quiet(a["minutes"])
        return f"режим «не отвлекать» на {a['minutes']} мин: сама не заговаривает"

    bool_p = lambda d: {"type": "boolean", "description": d}
    registry.add(Environment(
        id="assistant", title="Сама ассистентка",
        description="Её собственные настройки и память: голос, зрение, слух, разговорчивость, публикации.",
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
            _tool("assistant", "set_telegram_periodic", "Включить или выключить периодические посты-мысли в Telegram-канал.",
                  {"enabled": bool_p("true - публиковать периодически, false - не публиковать")}, set_telegram_periodic,
                  keywords=("телеграм", "канал", "публикуй", "мысли", "посты")),
            _tool("assistant", "set_telegram_after_run", "Включить или выключить пост-итог в Telegram-канал после каждого "
                  "забега (когда колония погибла). Для просьб «после каждого забега пиши пост».",
                  {"enabled": bool_p("true - писать итог после забега, false - не писать")}, set_telegram_after_run,
                  keywords=("забег", "после", "итог", "колония", "пост", "канал", "телеграм")),
            _tool("assistant", "deep_analysis", "ГЛУБОКИЙ АНАЛИЗ силами Claude: разбор протокола событий, причин гибели колонии "
                  "или провала, своей работы и ошибок. Для просьб «проанализируй себя / протокол / свою работу», «почему погибла "
                  "колония», «что пошло не так», «сделай выводы». Идёт в фоне несколько минут: Claude читает журналы и код, "
                  "пишет подробный отчёт, выводы сохраняются в память. Для таких просьб берут ЭТО, а не пересказ по памяти.",
                  {"question": {"type": "string", "description": "что именно разобрать, своими словами пользователя"},
                   "hours": {"type": "integer", "required": False, "description": "за сколько последних часов брать журнал, по умолчанию 6"}},
                  deep_analysis, informative=True,
                  keywords=("анализ", "проанализируй", "разбери", "причины", "выводы", "протокол", "аудит", "самоанализ",
                            "почему", "ошибки", "гибель", "погибла", "что пошло не так")),
            _tool("assistant", "name_voice", "Запомнить, чей это голос: тот, что говорил только что (через микрофон). "
                  "is_owner=true, если это сам пользователь («запомни мой голос, я Аня»). Для просьб «запомни мой голос», "
                  "«это голос Васи».",
                  {"name": {"type": "string", "description": "имя человека"},
                   "is_owner": {"type": "boolean", "required": False, "description": "true: это сам пользователь"}},
                  name_voice, keywords=("голос", "запомни", "узнавай", "это", "меня", "зовут", "микрофон")),
            _tool("assistant", "launch_app", "Запустить любое установленное приложение по названию, как в Alt+Пробел "
                  "(меню «Пуск» и магазин Windows). RimWorld запускается с ожиданием мода. Для просьб «запусти римворлд», "
                  "«пойдём играть», «открой хром», «включи телеграм». Консоли, реестр и деинсталляторы недоступны.",
                  {"name": {"type": "string", "description": "название приложения, как сказал пользователь"}},
                  launch_app, informative=True,
                  keywords=("запусти", "запускай", "открой", "включи", "включай", "игру", "римворлд", "rimworld", "поиграем",
                            "играть", "приложение", "программу", "хром", "докер", "телеграм", "блокнот")),
            _tool("assistant", "list_apps", "Найти установленные приложения по части названия (или назвать несколько).",
                  {"query": {"type": "string", "required": False, "description": "часть названия"}}, list_apps, informative=True,
                  keywords=("приложения", "программы", "что можешь запустить", "что установлено")),
            _tool("assistant", "add_app", "Добавить приложение в свой список для запуска: нужен путь к .exe, .lnk, .bat или .cmd "
                  "(для программ вне меню «Пуск»). Каждый раз спрашивает разрешение.",
                  {"title": {"type": "string", "description": "как называть приложение"},
                   "path": {"type": "string", "description": "полный путь к файлу запуска"},
                   "aliases": {"type": "array", "required": False, "description": "другие названия"}},
                  add_app, risk="ask", informative=True, keywords=("добавь", "приложение", "программу", "список")),
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
            _tool("assistant", "recall", "Вспомнить, что она помнит по теме или о ком-то (для вопросов «что ты помнишь про…»).",
                  {"query": {"type": "string", "description": "тема или слова для поиска в памяти"}},
                  recall, informative=True, keywords=("помнишь", "вспомни", "вспомнить", "память", "знаешь")),
            _tool("assistant", "quiet_mode", "Не отвлекать пользователя заданное число минут (она сама не заговаривает).",
                  {"minutes": {"type": "integer", "description": "на сколько минут, до 240"}}, quiet_mode,
                  keywords=("отвлекай", "мешай", "занят", "смотрю", "работаю", "минут", "тишина")),
        ]))

    # ── система ПК ────────────────────────────────────────────────────────────

    async def take_screenshot(a):
        target = a.get("target") or "game"
        focus = bool(a.get("focus"))
        try:
            if target == "screen":
                path = await asyncio.to_thread(screenshots.grab_screen)
            else:
                path = await asyncio.to_thread(screenshots.grab_game, focus)
        except screenshots.ShotError as e:
            raise ToolError(str(e))
        from PIL import Image
        with Image.open(path) as im:
            size = f"{im.width}x{im.height}"
        return (f"снимок {'экрана' if target == 'screen' else 'игры'} сохранён: {path.name} ({size}). "
                "Его можно приложить к черновику поста (draft_attach).")

    def _shot_danger(a):
        return "Она хочет сфотографировать весь экран: на снимок могут попасть чужие окна" if a.get("target") == "screen" else None

    async def describe_screen(a):
        seen = await perception.describe_now()
        status = seen["status"]
        if status == "off":
            raise ToolError("зрение выключено, она ничего не видит")
        if status == "sensitive":
            raise ToolError("сейчас открыто окно с личным содержимым (пароли, банк, приватный режим), смотреть она не будет")
        if status != "ok":
            raise ToolError("локальная модель зрения не ответила")
        return seen["text"]

    async def recent_audio(a):
        if hearing is None or not settings.load()["hearing"]:
            raise ToolError("слух выключен, системный звук не слушается")
        text = hearing.recent_text(300)
        if not text:
            raise ToolError("за последние минуты речи в звуке не было")
        return text

    async def ask_claude(a):
        result = await companion.run_claude(a["task"])
        if result is None:
            raise ToolError("агент-помощник на Claude сбойнул")
        return result or "готово, добавить нечего"

    async def web_search_tool(a):
        import web_search
        return await web_search.search(a["query"])

    # общий для всех профилей: поиск нужен и ассистенту, и игре, и монтажу (module=core: включён всегда)
    registry.add(Environment(
        id="web", title="Интернет",
        description="Поиск в интернете: вики, документация, форумы. Общий инструмент всех профилей.",
        tools=[_tool("web", "search", "Найти в интернете ответ на вопрос и вернуть выжимку фактов со ссылками на источники. "
                     "Для просьб «поищи», «узнай, как», «что такое», справки по играм и программам. Читает только страницы, ничего не меняет.",
                     {"query": {"type": "string", "description": "что искать; для игр и программ лучше на английском"}},
                     web_search_tool, informative=True,
                     keywords=("поищи", "найди", "интернет", "гугл", "google", "вики", "wiki", "узнай", "как работает", "что такое"))]))

    system_env = Environment(
        id="system", title="Компьютер пользователя",
        description="Экран, звук, файлы, команды и интернет на этом компьютере, а также мышь, клавиатура и окна.",
        tools=[
            _tool("system", "take_screenshot", "Сделать скриншот и сохранить файлом. target=game: окно RimWorld "
                  "(без вопросов; игра должна быть на переднем плане, либо focus=true: она сама переключится на неё); "
                  "target=screen: весь экран (всегда спрашивает разрешение).",
                  {"target": {"type": "string", "enum": ["game", "screen"], "required": False,
                              "description": "game - окно игры (по умолчанию), screen - весь экран"},
                   "focus": {"type": "boolean", "required": False, "description": "переключиться на игру перед снимком"}},
                  take_screenshot, informative=True,
                  keywords=("скриншот", "снимок", "сфотографируй", "скрин", "фото", "сними", "картинка")),
            _tool("system", "describe_screen", "Посмотреть на экран прямо сейчас и описать, что на нём (приложения, чем занят пользователь).",
                  {}, describe_screen, informative=True,
                  keywords=("экран", "видишь", "посмотри", "окно", "делаю", "открыто")),
            _tool("system", "recent_audio", "Пересказать, что недавно звучало в колонках (речь в фильме, видео), если слух включён.",
                  {}, recent_audio, informative=True, keywords=("слышишь", "услышала", "звучит", "говорят", "серия", "фильм", "видео")),
            _tool("system", "ask_claude", "Сложные задачи на компьютере: работа с файлами и кодом проекта, команды, поиск в интернете, "
                  "анализ. Выполняет Claude и сам спрашивает разрешение на каждое изменение.",
                  {"task": {"type": "string", "description": "что нужно сделать, своими словами пользователя"}}, ask_claude, informative=True,
                  keywords=("файл", "код", "команда", "найди", "интернет", "проект", "скрипт", "поиск", "папка", "почини", "исправь")),
        ])
    for t in system_env.tools:
        if t.name == "ask_claude":
            t.always = True
        if t.name == "take_screenshot":
            t.danger = _shot_danger
    system_env.tools += pc_control.make_tools(_tool)   # мышь, клавиатура, окна (всё с разрешением)

    async def read_screen_tool(a):
        import screen_operator
        return await screen_operator.read_screen()

    async def operate_screen_tool(a):
        import screen_operator
        return await screen_operator.operate(a["goal"], companion.say, int(a.get("max_steps") or 8))

    system_env.tools += [
        _tool("system", "read_screen", "Запасной уровень (по экрану): прочитать надписи на экране (OCR) с координатами в активном окне. "
              "Для вопросов «что там написано» и чтобы выбрать, на что нажать.",
              {}, read_screen_tool, informative=True, keywords=("прочитай", "написано", "экран", "надпись", "текст на экране")),
        _tool("system", "operate_screen", "Запасной уровень (по экрану): выполнить цель в ЛЮБОЙ программе, нажимая мышью и клавиатурой "
              "шаг за шагом по надписям на экране (ведёт локальная модель). Используй, только если у программы нет прямого инструмента.",
              {"goal": {"type": "string", "description": "цель своими словами: «открой настройки звука и убавь громкость»"},
               "max_steps": {"type": "integer", "required": False, "description": "не больше шагов (по умолчанию 8)"}},
              operate_screen_tool, risk="ask", informative=True,
              keywords=("открой", "нажми", "настрой", "включи", "выключи", "в программе", "в приложении", "кнопку", "меню")),
    ]
    registry.add(system_env)

    # ── соцсети ────────────────────────────────────────────────────────────────

    async def telegram_post(a):
        status, text = await telegram.post_on_request(a.get("topic") or "приветственный пост", a.get("voice"))
        reasons = {
            "not_configured": "Telegram не настроен: нет токена бота или канала в agent/secrets.env",
            "not_verified": "связь с каналом не прошла проверку: токен неверный или бот не администратор канала",
            "unsafe": "черновик не прошёл проверку приватности, ничего не опубликовано",
            "declined": "пользователь не разрешил публикацию",
            "failed": "Telegram вернул ошибку, пост не опубликован",
        }
        if status != "ok":
            raise ToolError(reasons.get(status, "публикация не удалась"))
        return f"опубликован пост: «{text}»"

    # черновик поста: писать, править, прикладывать снимки, публиковать
    async def draft_show(a):
        return telegram.draft.preview()

    async def draft_write(a):
        text = (a.get("text") or "").strip()
        if not text:
            text = await telegram.compose(a.get("topic") or "приветственный пост", telegram.draft.get()["text"])
        if not text:
            raise ToolError("текст не получился или не прошёл проверку приватности")
        telegram.draft.set_text(text)
        events_log.log("telegram", "черновик написан: " + text)
        return "черновик написан:\n" + text

    async def draft_append(a):
        text = (a.get("text") or "").strip()
        if not text:
            raise ToolError("нечего добавлять: текст пуст")
        telegram.draft.append(text)
        return "добавлено в конец черновика"

    async def draft_edit(a):
        current = telegram.draft.get()["text"]
        if not current:
            raise ToolError("черновик пуст, сначала его нужно написать")
        text = await telegram.compose(a["instruction"], current)
        if not text:
            raise ToolError("правка не получилась или не прошла проверку приватности, черновик не изменён")
        telegram.draft.set_text(text)
        events_log.log("telegram", "черновик отредактирован: " + text)
        return "черновик переписан:\n" + text

    async def draft_replace(a):
        if not telegram.draft.replace(a["find"], a["replace"]):
            raise ToolError("такого фрагмента в черновике нет")
        return "заменено"

    async def draft_attach(a):
        try:
            name = telegram.draft.attach(a.get("shot") or "last")
        except ValueError as e:
            raise ToolError(str(e))
        return f"снимок {name} приложен к черновику"

    async def draft_detach(a):
        n = telegram.draft.detach(a.get("shot") or "all")
        return f"снято снимков: {n}"

    async def draft_clear(a):
        telegram.draft.clear()
        return "черновик очищен"

    async def draft_publish(a):
        d = telegram.draft.get()
        photos = [screenshots.resolve(n) for n in d["photos"]]
        status, text = await telegram.publish(d["text"], [p for p in photos if p], a.get("voice"))
        reasons = {
            "not_configured": "Telegram не настроен: нет токена бота или канала в agent/secrets.env",
            "not_verified": "связь с каналом не прошла проверку: токен неверный или бот не администратор канала",
            "unsafe": "текст не прошёл проверку приватности, ничего не опубликовано",
            "declined": "пользователь не разрешил публикацию",
            "failed": "Telegram вернул ошибку, пост не опубликован",
            "empty": "черновик пуст: ни текста, ни снимков",
        }
        if status != "ok":
            raise ToolError(reasons.get(status, "публикация не удалась"))
        telegram.draft.clear()
        return "опубликовано из черновика" + (f" (со снимками: {len(photos)})" if photos else "") + (f": «{text}»" if text else "")

    # её собственный аккаунт в Telegram (Telethon): читать чаты и писать как пользователь
    async def tg_chats(a):
        return await _tg_call(tg_user.chats(int(a.get("limit") or 20)))

    async def tg_read(a):
        return await _tg_call(tg_user.read(a["chat"], int(a.get("limit") or 15)))

    async def tg_unread(a):
        return tg_user.unread(int(a.get("limit") or 15))

    async def tg_send(a):
        return await _tg_call(tg_user.send(a["chat"], a["text"]))

    async def _tg_call(coro):
        try:
            return await coro
        except (ValueError, RuntimeError) as e:
            raise ToolError(str(e))
        except Exception as e:
            raise ToolError(f"Telegram-аккаунт: {str(e)[:160]}")

    def tg_account_state():
        return tg_user.state() if tg_user is not None else (False, "модуль не подключён")

    if tg_user is not None:
        registry.add(Environment(
            id="telegram_account", title="Её аккаунт в Telegram",
            description="Её собственный аккаунт (не пользователя): читать чаты и писать как обычный человек.",
            status=tg_account_state,
            tools=[
                _tool("telegram_account", "tg_chats", "Список диалогов её Telegram-аккаунта (личные, группы, каналы) с числом непрочитанных.",
                      {"limit": {"type": "integer", "required": False, "description": "сколько диалогов, по умолчанию 20"}},
                      tg_chats, informative=True, keywords=("телеграм", "чаты", "диалоги", "переписка", "аккаунт", "тг")),
                _tool("telegram_account", "tg_read", "Прочитать последние сообщения чата её Telegram-аккаунта (чат по названию, @имени или id).",
                      {"chat": {"type": "string", "description": "название чата, @имя или id"},
                       "limit": {"type": "integer", "required": False, "description": "сколько сообщений, по умолчанию 15"}},
                      tg_read, informative=True, keywords=("прочитай", "переписка", "сообщения", "чат", "телеграм", "тг")),
                _tool("telegram_account", "tg_unread", "Что писали в её Telegram-аккаунт недавно (входящие, которые она заметила).",
                      {"limit": {"type": "integer", "required": False, "description": "сколько последних, по умолчанию 15"}},
                      tg_unread, informative=True, keywords=("новые", "входящие", "написали", "сообщения", "телеграм", "тг")),
                _tool("telegram_account", "tg_send", "Написать сообщение с её Telegram-аккаунта в чат (по названию, @имени или id). "
                      "Каждый раз спрашивает разрешение.",
                      {"chat": {"type": "string", "description": "название чата, @имя или id"},
                       "text": {"type": "string", "description": "текст сообщения"}},
                      tg_send, risk="ask", informative=True, keywords=("напиши", "отправь", "ответь", "сообщение", "телеграм", "тг")),
            ]))

    def telegram_state():
        if telegram is None or not telegram.configured():
            return False, "не настроен: нет токена бота или канала в agent/secrets.env"
        return True, "канал " + (f"«{telegram.title}»" if telegram.title else telegram._channel)

    registry.add(Environment(
        id="social", title="Соцсети",
        description="Публикации в Telegram-канал.", status=telegram_state,
        tools=[
            _tool("social", "draft_show", "Показать текущий черновик поста для канала (текст и приложенные снимки).",
                  {}, draft_show, informative=True, keywords=("черновик", "драфт", "пост", "покажи", "канал")),
            _tool("social", "draft_write", "Написать черновик поста для Telegram-канала (заменяет текущий текст). "
                  "Можно дать готовый text или тему topic: тогда она напишет сама, с учётом событий игры.",
                  {"text": {"type": "string", "required": False, "description": "готовый текст поста"},
                   "topic": {"type": "string", "required": False, "description": "о чём написать"}},
                  draft_write, informative=True, keywords=("черновик", "драфт", "напиши", "пост", "заметка", "канал", "тг")),
            _tool("social", "draft_edit", "Переписать текущий черновик по указанию («короче», «добавь шутку», «убери вторую фразу»).",
                  {"instruction": {"type": "string", "description": "что изменить в черновике"}},
                  draft_edit, informative=True, keywords=("отредактируй", "поправь", "перепиши", "короче", "добавь", "убери", "черновик", "драфт")),
            _tool("social", "draft_append", "Дописать готовый текст в конец черновика.",
                  {"text": {"type": "string", "description": "что дописать"}}, draft_append,
                  keywords=("допиши", "добавь", "черновик")),
            _tool("social", "draft_replace", "Точная замена фрагмента в черновике: первое вхождение find меняется на replace.",
                  {"find": {"type": "string", "description": "что заменить"}, "replace": {"type": "string", "description": "на что"}},
                  draft_replace, keywords=("замени", "исправь", "черновик")),
            _tool("social", "draft_attach", "Приложить скриншот к черновику: shot — имя файла или last (последний снимок, по умолчанию).",
                  {"shot": {"type": "string", "required": False, "description": "имя файла снимка или last"}},
                  draft_attach, keywords=("приложи", "вставь", "скриншот", "снимок", "картинка", "черновик")),
            _tool("social", "draft_detach", "Убрать снимок из черновика (shot — имя файла или all).",
                  {"shot": {"type": "string", "required": False, "description": "имя файла или all"}},
                  draft_detach, keywords=("убери", "сними", "скриншот", "черновик")),
            _tool("social", "draft_clear", "Выбросить черновик целиком (текст и снимки).",
                  {}, draft_clear, keywords=("очисти", "выброси", "удали", "черновик")),
            _tool("social", "draft_publish", "Опубликовать черновик в Telegram-канал (текст и приложенные снимки).",
                  {"voice": {"type": "boolean", "required": False,
                             "description": "true: голосовым сообщением её голосом; по умолчанию как в настройках"}},
                  draft_publish, risk="public", self_confirming=True, informative=True,
                  keywords=("опубликуй", "выложи", "запости", "отправь", "черновик", "канал", "тг")),
            _tool("social", "telegram_post", "Написать и опубликовать пост в Telegram-канал на заданную тему "
                  "(если тема не названа, приветственный пост).",
                  {"topic": {"type": "string", "description": "о чём пост, своими словами пользователя", "required": False},
                   "voice": {"type": "boolean", "required": False,
                             "description": "true: голосовым сообщением её голосом; по умолчанию как в настройках"}},
                  telegram_post, risk="public", self_confirming=True, informative=True,
                  keywords=("пост", "телеграм", "канал", "опубликуй", "запости", "выложи", "тг")),
        ]))


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


def register_core_tools(registry, modules, attention=None):
    """Инструменты ядра про задачи модулей (окружение core, всегда видны, ТЗ 02 раздел 11) и про внимание (ТЗ 03, ATT-24).
    modules - ModuleRegistry: jobs_all() и async cancel_job(module, job_id); attention - менеджер внимания (attention.py)."""

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

    registry.add(Environment(
        id="core", title="Ядро: задачи модулей и внимание",
        description="Долгие задачи модулей (монтаж, рендер и т.п.): посмотреть список и отменить. Внимание: временные правила "
                    "«не отвлекай / выдели» и сводка накопившегося.",
        tools=attention_tools + [
            _tool("core", "jobs", "Перечень задач всех модулей: модуль, название, состояние, стадия, прогресс. "
                  "Для вопросов «что сейчас делается», «как там рендер», «какие задачи идут».",
                  {}, jobs, informative=True, keywords=("задачи", "задача", "рендер", "прогресс", "идёт", "готово")),
            _tool("core", "job_cancel", "Отменить задачу модуля (по модулю и номеру задачи из перечня задач).",
                  {"module": {"type": "string", "description": "id модуля, например montage"},
                   "job_id": {"type": "string", "description": "номер задачи вида j-xxxxxxxx"}},
                  job_cancel, risk="ask", informative=True, keywords=("отмени", "останови", "задачу", "рендер")),
        ]))

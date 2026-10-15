"""Спецификация схемы архитектуры ядра (единственный источник): группы, компоненты, связи, слои. Из неё строятся данные для GUI и docs/architecture-clean.drawio (build.py)."""
import html
import textwrap

LAYERS = {
    "D": ("Домен", "#7a5ac8", "#efe9fb"),
    "A": ("Сценарии", "#2b6fba", "#e6f0fb"),
    "I": ("Адаптеры", "#1f9d55", "#e3f5ea"),
    "X": ("Внешнее", "#6b7683", "#eceff3"),
}
EDGE = {
    "flow": ("поток события и данных", "#7a5ac8", False),
    "use": ("вызов сценария, контракта", "#2b6fba", False),
    "w": ("запись в память", "#1f9d55", False),
    "r": ("чтение из памяти", "#c2308a", False),
    "ext": ("обращение к внешней службе", "#d9822b", False),
    "ctl": ("команда, управление", "#c2412d", True),
}
NW, GW = 268, 292
COLX = {"A": 30, "B": 372, "C": 714, "D": 1056, "E": 1398, "F": 1740}
TOP = 40


def N(id, t, tag, do, out, now, new=False):
    return dict(id=id, t=t, tag=tag, do=do, out=out, now=now, new=new)


# summary: что показываем, пока группа свёрнута
GROUPS = [
    dict(id="g_src", col="A", title="Источники событий", tag="I",
         summary="Модули (голос, Telegram, экран, игра, монтаж) и чат дашборда. Присылают события и выполняют команды.", out="Event", nodes=[
        N("voice", "voice", "I", "микрофон, VAD, распознавание речи (WhisperX), узнавание голоса; озвучка (Fish Speech)", "Event voice.utterance", "modules/voice"),
        N("desk", "desktop", "I", "экран, активность, мышь и клавиатура, Claude-помощник", "Event desktop.*, job.update", "modules/desktop"),
        N("tg", "telegram", "I", "бот, её аккаунт, посты канала", "Event telegram.*", "modules/telegram"),
        N("rim", "rimworld", "I", "игровой агент: ходы, комментатор, запись забега", "Event raid, death, colonists", "modules/rimworld"),
        N("mont", "montage", "I", "конвейер роликов: Remotion + ffmpeg, агент Claude", "job.update, вопросы", "modules/montage"),
        N("dash", "Дашборд и чат", "I", "чат, настройки, пульт, оценки", "UserInput как Event", "dashboard.py: 863 строки, 0 классов, ходит в хранилища напрямую"),
    ]),
    dict(id="g_imp", col="A", title="Внутренние импульсы", tag="A", new=True,
         summary="Напоминания, которые агент назначил себе; сработавший срок становится событием. Болтовни по таймеру нет.", out="Event self.*", nodes=[
        N("reminders", "ReminderStore", "I", "напоминания, назначенные самим агентом: срок, текст, откуда пришла мысль", "срок наступил", "нет: сейчас есть только план в MemoryStore с сроком", True),
        N("timer", "ImpulseTimer", "I", "следит только за сроками напоминаний; когда срок пришёл, шлёт событие. Не тик болтовни", "Event self.reminder", "нет: сейчас proactive_loop раз в 30 с", True),
    ]),
    dict(id="g_in", col="B", title="Приём", tag="I",
         summary="Принимает событие из любого источника, проверяет, приводит к единому виду, склеивает повторы, узнаёт отправителя.", out="Event", nodes=[
        N("hub", "ModuleHub (входящие)", "I", "WebSocket :8766: рукопожатие, проверка формата, очередь входящих; запросы core.*", "Event", "module_hub.py: 683 строки; импортирует attention и fast_brain, то есть транспорт знает домен"),
        N("event", "Event", "D", "единый конверт: kind, text, sender, hint, data, trace, ts; источник внешний или внутренний (self)", "сущность", "нет: dict, Item, UserInput и SpeechCandidate по отдельности", True),
        N("norm", "Normalizer", "A", "нормализация, дедупликация за 60 с, склейка повторов, TTL", "Event", "attention.submit и _merge_duplicate"),
        N("people", "PeopleDirectory", "A", "sender → Person, первый контакт, заметки о человеке", "Person", "attention._note_sender + relationships; лезет в companion._rel через getattr"),
    ]),
    dict(id="g_dec", col="C", title="Решение", tag="A",
         summary="Правила до моделей, быстрая классификация, оценка важности и выбор маршрута: сказать, ответить, сделать, сменить фокус, подумать, отложить.", out="Decision + routes", nodes=[
        N("rules", "HardRules", "D", "до моделей: R1 владелец → разговор, R2 ответ на вопрос, R3 mute, R4 критичное, «замолчи»", "RuleResult", "середина attention.submit; стоп-фразы и «похоже на просьбу» ещё и в router"),
        N("cls", "Classifier (контракт)", "A", "категория, срочность, адресат, нужен ли фокус; реализация: служба GLiNER :8768; тайм-аут 1 с", "Classification", "decide.py + services/decider"),
        N("score", "PriorityScorer", "D", "срочность × 20 × веса (профиль, отправитель, правила, пользователь) × свежесть", "priority 0..100", "attention._score"),
        N("disp", "Dispatcher", "A", "решение D0–D4 и routes[]: say, chat, task, focus, think, digest, ignore; пишет решение в журнал", "Decision", "attention._decide, _act, _finish"),
    ]),
    dict(id="g_conv", col="D", title="Разговор", tag="A",
         summary="Ведёт беседу: собирает контекст из памяти, пишет ответ быстрой моделью.", out="ReplyCandidate", nodes=[
        N("conv", "ConversationService", "A", "обращение владельца: проверки → ветка (болтовня / просьба / «что умеешь») → ответ", "Reply или Task", "companion._handle_text и _converse"),
        N("ctx", "ContextBuilder", "A", "собирает контекст: характер, память, диалог, состояние; бюджет токенов на роль", "Context", "нет: companion._context склеивает 6 строк без бюджета", True),
        N("reply", "ReplyGenerator", "A", "LanguageModel(fast) → LM Studio (Gemma): ответ; страховки стиля", "ReplyCandidate", "companion._converse, _declutter + fast_brain"),
    ]),
    dict(id="g_think", col="D", title="Размышление", tag="A", new=True,
         summary="Внутренний диалог агента: получает событие или импульс, думает, читая память, и решает: сказать, сделать, напомнить себе или записать.", out="Intent", nodes=[
        N("reflect", "ReflectionService", "A", "внутренний диалог в несколько шагов по событию или импульсу; читает память; лимиты шагов и токенов", "Intent: сказать, сделать, напомнить, записать, ничего", "нет: сейчас только proactive_loop (опрос себя раз в 30 с)", True),
        N("thoughts", "Журнал мыслей", "I", "внутренние рассуждения: не озвучиваются; основа дневника и эпизодов", "Thought", "нет", True),
    ]),
    dict(id="g_act", col="E", title="Дела", tag="A",
         summary="Строит план из доступных инструментов, спрашивает разрешение на опасное, выполняет в модулях, ведёт долгие задачи.", out="ToolResult", nodes=[
        N("reg", "ToolRegistry", "A", "инструменты ядра и модулей, видимость по профилю, отбор по смыслу", "Tool[]", "environments/registry.py, tool_search.py"),
        N("plan", "Planner", "A", "LanguageModel → LM Studio (Gemma): план шагов или «не могу»", "Plan", "router._plan"),
        N("risk", "RiskPolicy + Approver", "D", "риск auto / ask / public / danger; вопрос пользователю, ответ да/нет", "Approval", "router._step + approvals.py: 301 строка"),
        N("exec", "Executor (ToolInvoker)", "A", "вызов инструмента в ядре или модуле, коды ошибок, итог-факт", "ToolResult", "router._step и _run_step"),
        N("jobs", "JobTracker", "A", "долгие задачи: прогресс, вопросы, итог; Claude-помощник (Agent SDK)", "JobState", "module_registry + attention.on_job_update + desktop.assistant_agent"),
    ]),
    dict(id="g_foc", col="E", title="Фокус", tag="A",
         summary="Решает, кому отдать главную роль (например, игре в бою), и когда её вернуть.", out="FocusDecision", nodes=[
        N("fmgr", "FocusManager", "A", "один слот, очередь, переключение, дребезг, checkpoint", "FocusDecision", "focus.py: 1061 строка, 60 методов в одном классе"),
        N("fpol", "FocusPolicy", "D", "чистые правила: приоритет, запас, критичное, старение, конфликты", "FocusDecision", "внутри FocusManager: _gate, _near, _claimant", True),
        N("fdisp", "FocusDispatch (контракт)", "A", "Gemma только для неоднозначного; ответ строго проверяется", "Op", "focus_dispatch.py: 288 строк"),
    ]),
    dict(id="g_out", col="F", title="Вывод", tag="I",
         summary="Очередь речи выбирает момент и приоритет, затем реплика уходит каналом: голос, чат, Telegram, питомец. Команды модулям уходят отсюда же.", out="Utterance, команды", nodes=[
        N("queue", "SpeechQueue", "A", "приоритеты, условия «не перебиваю», интервалы, прерывание, TTL; чистую политику выбора выделить в домен позже", "Utterance", "speech_queue.py: 296 строк, асинхронный координатор (asyncio-задачи), в домен целиком не переносится"),
        N("digest", "Digest", "A", "сводка событий D1 и предложение рассказать", "SpeechCandidate", "attention._digest_*"),
        N("out", "SpeechOutput и каналы (контракты)", "I", "голос (speak), чат, Telegram (deliver), питомец", "доставка", "speech_out.py, conversation_io.py"),
        N("aff", "Presence и эмоции", "A", "говорит ли, эмоция, присутствие; (план) настроение", "PresenceState", "PresenceTracker + fast_brain.publish"),
        N("hubout", "ModuleHub (исходящие)", "I", "доставка команд модулям: tool.call, speak, policy.activate, job.answer", "ToolResult, события", "тот же module_hub.py, смешан с приёмом"),
    ]),
    dict(id="g_mem", col="BOTTOM", title="Память", tag="A",
         summary="Запись и чтение памяти: диалог, факты, профили, воспоминания о себе. Источник правды — журнал событий.", out="MemoryRecord, MemoryHit", nodes=[
        N("mw", "MemoryWriter", "A", "сразу: диалог и журнал; в паузе: факты (Gemma + эмбеддинги); раз в 6 реплик: осмысление; (план) эпизоды и сон", "MemoryRecord", "companion._learn, _consolidate + graph_memory.step"),
        N("mr", "MemoryReader", "A", "граф + воспоминания о себе + записи; (план) гибридный поиск", "MemoryHit[]", "graph.search, self_memories, MemoryStore.relevant (два поиска)"),
        N("elog", "EventLog", "I", "SQLite events.db: журнал событий, трасса, решения", "контракт журнала", "events_log.py: глобальные функции, 16 импортов"),
        N("stores", "Диалог, записи, люди", "I", "JSON: диалог, MemoryStore (планы, напоминания), отношения", "репозитории", "user_memory.py, memory_store.py, relationships.py"),
        N("graph", "FactGraph (контракт)", "I", "Graphiti: факты с интервалами валидности", "контракт графа", "graph_memory.py: 701 строка"),
        N("char", "CharacterRepo", "I", "resources/<Имя>: конституция, prompt, канон, голос", "Character", "character.py + resources/"),
    ]),
    dict(id="g_ext", col="BOTTOM2", title="Внешний слой и сборка", tag="X",
         summary="Модели и службы: LM Studio, GLiNER, FalkorDB, Fish Speech, WhisperX, Claude. Сборка: core_service.", out="", nodes=[
        N("lms", "LM Studio :1234", "X", "Gemma (речь, план, факты) и EmbeddingGemma", "", ""),
        N("gli", "GLiNER decider :8768", "X", "классификатор событий и обращений", "", ""),
        N("falk", "FalkorDB :6380", "X", "база графа фактов", "", ""),
        N("fish", "Fish Speech / edge-tts", "X", "синтез речи", "", ""),
        N("wx", "WhisperX / Whisper", "X", "распознавание речи", "", ""),
        N("cl", "Claude Agent SDK", "X", "долгие и сложные задачи", "", ""),
        N("sup", "Supervisor", "I", "запуск и перезапуск процессов модулей", "", "supervisor.py: 573 строки"),
        N("root", "Composition root", "I", "только собирает объекты и подставляет контракты; бизнес-логики быть не должно", "", "core_service.py: 22 импорта, внутри обработчики voice, colonists, run_lost"),
    ]),
]

OWNER = {n["id"]: g["id"] for g in GROUPS for n in g["nodes"]}
BYID = {g["id"]: g for g in GROUPS}

EDGES = [
    ("voice", "hub", "flow", "Event", None), ("desk", "hub", "flow", None, None), ("tg", "hub", "flow", None, None),
    ("rim", "hub", "flow", None, None), ("mont", "hub", "flow", "job.update", None), ("dash", "hub", "flow", "UserInput", None),
    ("reminders", "timer", "use", "срок", None), ("timer", "norm", "flow", "self.reminder", None),
    ("hub", "norm", "flow", "Event", 2), ("norm", "people", "use", "sender", None), ("norm", "rules", "flow", "Event", 3),
    ("rules", "cls", "flow", "не решено правилами", 4), ("cls", "score", "flow", "Classification", 5), ("score", "disp", "flow", "priority", 6),
    ("rules", "conv", "flow", "R1: владелец", None),
    ("disp", "conv", "flow", "route chat", 7), ("disp", "plan", "flow", "route task", 7), ("disp", "fmgr", "flow", "route focus", 7),
    ("disp", "reflect", "flow", "route think", 7), ("disp", "queue", "flow", "route say: факт", 7), ("disp", "digest", "flow", "D1", 7),
    ("conv", "ctx", "use", None, 8), ("ctx", "mr", "r", "запрос", 9), ("ctx", "reply", "use", "Context", 10),
    ("reply", "queue", "flow", "ReplyCandidate", 11), ("conv", "plan", "flow", "просьба", None),
    ("reflect", "mr", "r", "вспоминает", None), ("reflect", "thoughts", "w", "мысль", None),
    ("reflect", "queue", "flow", "хочет сказать", None), ("reflect", "plan", "flow", "хочет сделать", None),
    ("reflect", "reminders", "w", "напомнить себе", None), ("reflect", "mw", "w", "записать", None),
    ("reg", "plan", "use", "Tool[]", None), ("plan", "risk", "flow", "Plan", 12), ("risk", "exec", "flow", "разрешённый шаг", 13),
    ("exec", "hubout", "flow", "tool.call", 14), ("exec", "jobs", "use", None, None), ("jobs", "queue", "flow", "итог", None),
    ("exec", "queue", "flow", "итог-факт", 15),
    ("fmgr", "fpol", "use", "правила", None), ("fmgr", "fdisp", "use", "неоднозначно", None), ("fmgr", "hubout", "ctl", "policy.activate", None),
    ("queue", "out", "flow", "Utterance", 16), ("out", "aff", "use", None, None), ("out", "hubout", "flow", "speak", 17), ("digest", "queue", "flow", None, None),
    ("conv", "mw", "w", "после ответа", None), ("disp", "elog", "w", "Decision", None),
    ("mw", "graph", "w", None, None), ("mw", "stores", "w", None, None),
    ("mr", "graph", "r", None, None), ("mr", "stores", "r", None, None), ("ctx", "char", "r", "характер", None),
    ("graph", "falk", "ext", None, None),
]
# ссылки макроуровня (когда конец свёрнут): номер шага
MACRO_STEPS = {
    ("g_src", "g_in"): 1, ("g_imp", "g_in"): 1, ("g_in", "g_dec"): 2,
    ("g_dec", "g_conv"): 3, ("g_dec", "g_act"): 3, ("g_dec", "g_foc"): 3, ("g_dec", "g_think"): 3, ("g_dec", "g_out"): 3,
    ("g_conv", "g_out"): 4, ("g_act", "g_out"): 4, ("g_think", "g_out"): 4, ("g_think", "g_act"): 4,
    ("g_out", "g_src"): 5,
}
MACRO_ONLY = []
PRESETS = [
    ("p1", "1. Основа", []),
    ("p2", "2. + Приём и решение", ["g_in", "g_dec"]),
    ("p3", "3. + Разговор и вывод", ["g_in", "g_dec", "g_conv", "g_out"]),
    ("p4", "4. + Дела, фокус, размышление", ["g_in", "g_dec", "g_conv", "g_out", "g_act", "g_foc", "g_think", "g_imp"]),
    ("p5", "5. + Память", ["g_in", "g_dec", "g_conv", "g_out", "g_act", "g_foc", "g_think", "g_imp", "g_mem"]),
    ("p6", "6. Всё вместе", ["g_in", "g_dec", "g_conv", "g_out", "g_act", "g_foc", "g_think", "g_imp", "g_mem", "g_ext", "g_src"]),
]

CW = 6.3


def wrap(text, w):
    return textwrap.wrap(text, max(12, int(w / CW))) or [""]


def node_lines(n, w):
    inner = w - 20
    out = [("do", s) for s in wrap(n["do"], inner)]
    if n["out"]:
        out += [("out", s) for s in wrap("→ " + n["out"], inner)]
    if n["now"]:
        out += [("now", s) for s in wrap("сейчас: " + n["now"], inner)]
    return out


def macro_node(g):
    parts = ", ".join(n["t"] for n in g["nodes"])
    return dict(id=g["id"], t=g["title"], tag=g["tag"], do=g["summary"], out=g["out"], now="", new=g.get("new", False),
                parts="внутри: " + parts)


def macro_lines(n, w):
    inner = w - 20
    out = [("do", s) for s in wrap(n["do"], inner)]
    if n["out"]:
        out += [("out", s) for s in wrap("→ " + n["out"], inner)]
    out += [("now", s) for s in wrap(n["parts"], inner)]
    return out


class Box:
    def __init__(self, id, x, y, w, h):
        self.id, self.x, self.y, self.w, self.h = id, x, y, w, h


def build(expand):
    """раскладка для набора раскрытых групп; возвращает (nodes_list, boxes, groupboxes, W, H, edges)"""
    boxes, gboxes, nodes = {}, {}, {}
    colbottom = {}
    order = [g for g in GROUPS if g["col"] in COLX]
    for g in order:
        x = COLX[g["col"]]
        y = colbottom.get(g["col"], TOP)
        if g["id"] in expand:
            cy = y + 44
            for n in g["nodes"]:
                h = 30 + len(node_lines(n, NW)) * 14.5 + 8
                boxes[n["id"]] = Box(n["id"], x + 12, cy, NW, h)
                nodes[n["id"]] = (n, False)
                cy += h + 14
            gboxes[g["id"]] = Box(g["id"], x, y, GW, cy - y)
            colbottom[g["col"]] = cy + 22
        else:
            mn = macro_node(g)
            h = 30 + len(macro_lines(mn, NW)) * 14.5 + 8
            boxes[g["id"]] = Box(g["id"], x + 12, y + 4, NW, h)
            nodes[g["id"]] = (mn, True)
            colbottom[g["col"]] = y + 4 + h + 26
    top_bottom = max(colbottom.values())
    # низ: память и внешний слой
    y = top_bottom + 10
    mem = BYID["g_mem"]
    mx = COLX["C"]
    if "g_mem" in expand:
        cy = y + 44
        rows = [mem["nodes"][:3], mem["nodes"][3:]]
        for r in rows:
            rh = max(30 + len(node_lines(n, NW)) * 14.5 + 8 for n in r)
            for i, n in enumerate(r):
                boxes[n["id"]] = Box(n["id"], mx + 12 + i * (NW + 28), cy, NW, rh)
                nodes[n["id"]] = (n, False)
            cy += rh + 14
        gboxes["g_mem"] = Box("g_mem", mx, y, 3 * NW + 2 * 28 + 24, cy - y)
        y = cy + 22
    else:
        mn = macro_node(mem)
        h = 30 + len(macro_lines(mn, NW)) * 14.5 + 8
        boxes["g_mem"] = Box("g_mem", mx + 12 + (NW + 28), y + 4, NW, h)
        nodes["g_mem"] = (mn, True)
        y = y + 4 + h + 26
    ext = BYID["g_ext"]
    if "g_ext" in expand:
        ih = max(30 + len(node_lines(n, 226)) * 14.5 + 8 for n in ext["nodes"])
        for i, n in enumerate(ext["nodes"]):
            boxes[n["id"]] = Box(n["id"], 30 + 12 + i * (226 + 14), y + 44, 226, ih)
            nodes[n["id"]] = (n, False)
        gboxes["g_ext"] = Box("g_ext", 30, y, 12 + 8 * (226 + 14) + 6, ih + 44 + 14)
        y = y + ih + 44 + 14 + 30
    else:
        y += 0
    W = COLX["F"] + GW + 30
    H = y + 20
    # рёбра
    def rep(nid):
        g = OWNER[nid]
        return nid if g in expand or (g == "g_src" and "g_src" in expand) else g
    def vis(nid):
        g = OWNER[nid]
        if g in expand:
            return nid
        return g
    edges, seen = [], {}
    for a, b, k, label, step in EDGES:
        ra, rb = vis(a), vis(b)
        if ra == rb:
            continue
        if ra not in boxes or rb not in boxes:
            continue
        both_exp = (OWNER[a] in expand) and (OWNER[b] in expand)
        if len(expand) == 0:
            st = MACRO_STEPS.get((OWNER[a], OWNER[b]))
        else:
            st = step if both_exp else None
            if st is None and not both_exp:
                st = None
        key = (ra, rb, k)
        if key in seen:
            if seen[key]["step"] is None and st is not None:
                seen[key]["step"] = st
            if not seen[key]["label"] and label and both_exp:
                seen[key]["label"] = label
            continue
        e = dict(a=ra, b=rb, kind=k, label=label if both_exp or len(expand) == 0 else (label if label and len(label) < 22 else None), step=st)
        seen[key] = e
        edges.append(e)
    if len(expand) == 0:                              # основа: чистим и подписываем простыми словами
        lab = {("g_dec", "g_conv"): "ответить", ("g_dec", "g_act"): "сделать", ("g_dec", "g_foc"): "сменить фокус", ("g_dec", "g_think"): "подумать",
               ("g_dec", "g_out"): "просто сказать", ("g_conv", "g_out"): "реплика", ("g_conv", "g_act"): "просьба", ("g_think", "g_out"): "хочет сказать",
               ("g_think", "g_act"): "хочет сделать", ("g_act", "g_out"): "итог дела", ("g_foc", "g_out"): "режим модулю", ("g_conv", "g_mem"): "читает и пишет",
               ("g_think", "g_mem"): "читает и пишет", ("g_think", "g_imp"): "напомнить себе", ("g_src", "g_in"): "Event", ("g_imp", "g_in"): "Event self.*"}
        keep, seen2 = [], set()
        for e in edges:
            key = (e["a"], e["b"])
            if key == ("g_dec", "g_mem") or key in seen2:
                continue
            seen2.add(key)
            if key in lab:
                e["label"] = lab[key]
            if key == ("g_conv", "g_mem") or key == ("g_think", "g_mem"):
                e["kind"] = "r"
            keep.append(e)
        edges = keep
    if "g_src" not in expand:                       # речь и команды уходят модулям-источникам
        src = "hubout" if "g_out" in expand else "g_out"
        edges.append(dict(a=src, b="g_src", kind="ctl", label="речь, команды, режим", step=5 if len(expand) == 0 else None))
    return nodes, boxes, gboxes, W, H, edges


def route(boxes, edges):
    items = []
    for e in edges:
        a, b = boxes[e["a"]], boxes[e["b"]]
        acx, bcx = a.x + a.w / 2, b.x + b.w / 2
        if b.y > a.y + a.h + 40 and b.y - a.y > 240:
            sa, sb = "b", "t"
        elif b.x > a.x + a.w - 4 and abs(bcx - acx) > 40:
            sa, sb = "r", "l"
        elif b.x + b.w < a.x + 4 and abs(bcx - acx) > 40:
            sa, sb = "l", "r"
        elif b.y >= a.y:
            sa, sb = "b", "t"
        else:
            sa, sb = "t", "b"
        items.append(dict(e=e, a=a, b=b, sa=sa, sb=sb))
    slots = {}
    for it in items:
        slots.setdefault((it["e"]["a"], it["sa"]), []).append((it, "a"))
        slots.setdefault((it["e"]["b"], it["sb"]), []).append((it, "b"))
    for (nid, side), lst in slots.items():
        def key(p, side=side):
            it, end = p
            o = it["b"] if end == "a" else it["a"]
            return o.y + o.h / 2 if side in ("l", "r") else o.x + o.w / 2
        lst.sort(key=key)
        k = len(lst)
        for i, (it, end) in enumerate(lst):
            it["off" + end] = (i - (k - 1) / 2) * min(14, 70 / max(1, k))

    def pt(bx, side, off):
        if side == "r":
            return bx.x + bx.w, bx.y + bx.h / 2 + off
        if side == "l":
            return bx.x, bx.y + bx.h / 2 + off
        if side == "b":
            return bx.x + bx.w / 2 + off, bx.y + bx.h
        return bx.x + bx.w / 2 + off, bx.y

    out = []
    for it in items:
        p0 = pt(it["a"], it["sa"], it.get("offa", 0))
        p3 = pt(it["b"], it["sb"], it.get("offb", 0))
        if it["sa"] in ("r", "l"):
            c = max(40, abs(p3[0] - p0[0]) * 0.5)
            d = 1 if it["sa"] == "r" else -1
            p1, p2 = (p0[0] + d * c, p0[1]), (p3[0] - d * c, p3[1])
        else:
            c = max(30, abs(p3[1] - p0[1]) * 0.45)
            d = 1 if it["sa"] == "b" else -1
            p1, p2 = (p0[0], p0[1] + d * c), (p3[0], p3[1] - d * c)
        u = 0.5
        mid = tuple((1 - u) ** 3 * p0[i] + 3 * (1 - u) ** 2 * u * p1[i] + 3 * (1 - u) * u * u * p2[i] + u ** 3 * p3[i] for i in (0, 1))
        out.append(dict(e=it["e"], p0=p0, p1=p1, p2=p2, p3=p3, mid=mid))
    return out


def esc(s):
    return html.escape(s, quote=True)


def board_html(pid, expand):
    nodes, boxes, gboxes, W, H, edges = build(expand)
    paths = route(boxes, edges)
    parts = []
    for g in GROUPS:
        if g["id"] in gboxes:
            b = gboxes[g["id"]]
            parts.append(f'<div class="grp" style="left:{b.x}px;top:{b.y}px;width:{b.w}px;height:{b.h}px"><div class="gt">{esc(g["title"])}</div></div>')
    for nid, (n, macro) in nodes.items():
        b = boxes[nid]
        fg, bg = LAYERS[n["tag"]][1], LAYERS[n["tag"]][2]
        ls = "".join(f'<div class="{k}">{esc(s)}</div>' for k, s in (macro_lines(n, b.w) if macro else node_lines(n, b.w)))
        chip = f'<span class="tg" style="background:{fg}">{LAYERS[n["tag"]][0]}</span>'
        new = '<span class="nw">нужно выделить</span>' if n["new"] else ""
        cls = "nd" + (" new" if n["new"] else "") + (" macro" if macro else "")
        parts.append(f'<div class="{cls}" data-id="{nid}" style="left:{b.x}px;top:{b.y}px;width:{b.w}px;height:{b.h}px;border-left-color:{fg};background:{bg}">'
                     f'<div class="ti">{esc(n["t"])} {chip}{new}</div>{ls}</div>')
    svg = [f'<svg class="w" width="{W}" height="{H}" viewBox="0 0 {W} {H}"><defs>']
    for k, (_, col, _) in EDGE.items():
        svg.append(f'<marker id="{pid}-ar-{k}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0L10,5L0,10z" fill="{col}"/></marker>')
    svg.append('</defs>')
    badges = []
    steps = set()
    for p in paths:
        e = p["e"]
        col, dash = EDGE[e["kind"]][1], EDGE[e["kind"]][2]
        d = f'M{p["p0"][0]:.1f},{p["p0"][1]:.1f} C{p["p1"][0]:.1f},{p["p1"][1]:.1f} {p["p2"][0]:.1f},{p["p2"][1]:.1f} {p["p3"][0]:.1f},{p["p3"][1]:.1f}'
        svg.append(f'<path class="e" data-a="{e["a"]}" data-b="{e["b"]}" d="{d}" stroke="{col}" fill="none" stroke-width="1.9"'
                   + (' stroke-dasharray="6 4"' if dash else '') + f' marker-end="url(#{pid}-ar-{e["kind"]})"/>')
        if e["step"] is not None:
            steps.add(e["step"])
            badges.append(f'<g class="bd" data-a="{e["a"]}" data-b="{e["b"]}"><circle cx="{p["mid"][0]:.1f}" cy="{p["mid"][1]:.1f}" r="10" fill="{col}"/>'
                          f'<text x="{p["mid"][0]:.1f}" y="{p["mid"][1]:.1f}">{e["step"]}</text></g>')
        if e["label"]:
            badges.append(f'<text class="lb" data-a="{e["a"]}" data-b="{e["b"]}" x="{p["mid"][0]:.1f}" y="{p["mid"][1] + (24 if e["step"] is not None else -6):.1f}">{esc(e["label"])}</text>')
    svg.append('</svg>')
    top = f'<svg class="w top" width="{W}" height="{H}" viewBox="0 0 {W} {H}">' + "".join(badges) + '</svg>'
    inner = "".join(svg) + "".join(parts) + top
    return inner, W, H, steps, boxes, nodes


def drawio(expand):
    from xml.sax.saxutils import escape as xe
    nodes, boxes, gboxes, W, H, edges = build(expand)
    cells = ['<mxCell id="0"/>', '<mxCell id="1" parent="0"/>']
    for g in GROUPS:
        if g["id"] in gboxes:
            b = gboxes[g["id"]]
            cells.append(f'<mxCell id="{g["id"]}" value="{xe(g["title"])}" style="swimlane;whiteSpace=wrap;html=1;startSize=34;fillColor=#f7f8fa;strokeColor=#b6bdc8;fontStyle=1;fontSize=13;align=left;spacingLeft=8;" vertex="1" parent="1">'
                         f'<mxGeometry x="{b.x}" y="{b.y}" width="{b.w}" height="{b.h}" as="geometry"/></mxCell>')
    for nid, (n, macro) in nodes.items():
        b = boxes[nid]
        fg, bg = LAYERS[n["tag"]][1], LAYERS[n["tag"]][2]
        lines = "".join(f'&lt;br&gt;{xe(s)}' for _, s in (macro_lines(n, b.w) if macro else node_lines(n, b.w)))
        val = f'&lt;b&gt;{xe(n["t"])}&lt;/b&gt; [{LAYERS[n["tag"]][0]}]' + ('&lt;i&gt; нужно выделить&lt;/i&gt;' if n["new"] else '') + lines
        parent = OWNER.get(nid) if (not macro and OWNER.get(nid) in gboxes) else None
        if parent:
            gb = gboxes[parent]
            geo = f'x="{b.x - gb.x}" y="{b.y - gb.y}"'
            par = parent
        else:
            geo = f'x="{b.x}" y="{b.y}"'
            par = "1"
        dashed = "dashed=1;" if n["new"] else ""
        cells.append(f'<mxCell id="{nid}" value="{val}" style="rounded=1;whiteSpace=wrap;html=1;align=left;verticalAlign=top;spacingLeft=6;fontSize=10;fillColor={bg};strokeColor={fg};{dashed}" vertex="1" parent="{par}">'
                     f'<mxGeometry {geo} width="{b.w}" height="{b.h}" as="geometry"/></mxCell>')
    k = 0
    for e in edges:
        k += 1
        col, dash = EDGE[e["kind"]][1], EDGE[e["kind"]][2]
        label = (f'{e["step"]}. ' if e["step"] is not None else '') + (e["label"] or '')
        cells.append(f'<mxCell id="e{k}" value="{xe(label)}" style="edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;endArrow=block;strokeColor={col};' + ('dashed=1;' if dash else '')
                     + f'fontSize=9;" edge="1" parent="1" source="{e["a"]}" target="{e["b"]}"><mxGeometry relative="1" as="geometry"/></mxCell>')
    return ('<mxfile host="app.diagrams.net"><diagram name="Архитектура ядра GitS: всё вместе" id="arch2"><mxGraphModel dx="1600" dy="1000" grid="1" gridSize="10" guides="1" tooltips="1" '
            f'connect="1" arrows="1" fold="1" page="1" pageScale="1" pageWidth="{int(W)}" pageHeight="{int(H)}" math="0" shadow="0"><root>' + "".join(cells) + '</root></mxGraphModel></diagram></mxfile>')


# ── строки раскладки (сверху вниз) и подписи стрелок между свёрнутыми блоками ──
ROW = {"g_src": 0, "g_imp": 0, "g_in": 1, "g_dec": 2, "g_conv": 3, "g_think": 3, "g_act": 3, "g_foc": 3, "g_out": 4, "g_mem": 5, "g_ext": 5}
MACRO_LABELS = {
    ("g_dec", "g_conv"): "ответить", ("g_dec", "g_act"): "сделать", ("g_dec", "g_foc"): "сменить фокус", ("g_dec", "g_think"): "подумать",
    ("g_dec", "g_out"): "просто сказать", ("g_conv", "g_out"): "реплика", ("g_conv", "g_act"): "просьба", ("g_think", "g_out"): "хочет сказать",
    ("g_think", "g_act"): "хочет сделать", ("g_act", "g_out"): "итог дела", ("g_foc", "g_out"): "режим модулю", ("g_conv", "g_mem"): "читает и пишет",
    ("g_think", "g_mem"): "читает и пишет", ("g_think", "g_imp"): "напомнить себе", ("g_src", "g_in"): "Event", ("g_imp", "g_in"): "Event self.*",
    ("g_in", "g_dec"): "Event", ("g_dec", "g_mem"): "журнал решений", ("g_act", "g_src"): "команды",
}


def data() -> dict:
    """Данные для GUI и страницы docs: группы, компоненты, связи, слои, виды стрелок."""
    return dict(
        groups=[dict(id=g["id"], row=ROW[g["id"]], title=g["title"], tag=g["tag"], new=bool(g.get("new")), summary=g["summary"], out=g["out"],
                     nodes=[dict(id=n["id"], t=n["t"], tag=n["tag"], new=n["new"], do=n["do"], out=n["out"], now=n["now"]) for n in g["nodes"]]) for g in GROUPS],
        edges=[[a, b, k, label] for a, b, k, label, _ in EDGES],
        macroLabels=[[a, b, label] for (a, b), label in MACRO_LABELS.items()],
        layers={k: [v[0], v[1], v[2]] for k, v in LAYERS.items()},
        edgeKinds={k: [v[0], v[1], v[2]] for k, v in EDGE.items()},
    )

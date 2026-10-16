"""Топология потока данных (ТЗ 17, FLOW-6, раздел 3): узлы, рёбра, слои и поведенческий граф хода.

Всё описано данными: код дашборда про конкретные узлы ничего не знает. Узлы модулей создаются по реестру (`topology`).
Ребро называется `<откуда>-><куда>`: так же пишет шаги `flow_trace.step`."""
from flow_trace import edge_id

LAYERS = {
    "modules": ("Модули", "#3b82f6"),
    "attention": ("Внимание", "#f59e0b"),
    "thinking": ("Мышление", "#8b5cf6"),
    "tools": ("Инструменты", "#14b8a6"),
    "wm": ("Память кратковременная", "#22c55e"),
    "ltm": ("Память долговременная", "#16a34a"),
    "output": ("Речь и вывод", "#ec4899"),
}

# (id, подпись, слой)
NODES = [
    ("bus", "Шина событий", "attention"),
    ("gliner", "Классификатор GLiNER", "attention"),
    ("attention", "Внимание D0-D4", "attention"),
    ("focus", "Фокус", "attention"),
    ("speech_queue", "Очередь речи", "output"),
    ("planner", "Выбор инструментов", "thinking"),
    ("gemma", "Gemma (речь, план)", "thinking"),
    ("claude", "Claude (долгие задачи)", "thinking"),
    ("tools", "Инструменты", "tools"),
    ("working_memory", "Рабочая память", "wm"),
    ("graph_memory", "Личная память (граф)", "ltm"),
    ("kb", "Справочник (RAG)", "ltm"),
    ("experience", "Опыт", "ltm"),
    ("out_voice", "Голос", "output"),
    ("out_pet", "Питомец", "output"),
    ("out_telegram", "Telegram", "output"),
    ("out_dashboard", "Дашборд", "output"),
]

EDGES = [
    ("bus", "gliner"), ("gliner", "attention"), ("attention", "focus"), ("attention", "working_memory"),
    ("attention", "planner"), ("attention", "speech_queue"), ("planner", "gemma"), ("planner", "claude"),
    ("planner", "tools"), ("planner", "graph_memory"), ("gemma", "speech_queue"), ("gemma", "working_memory"),
    ("gemma", "graph_memory"), ("working_memory", "graph_memory"), ("graph_memory", "kb"), ("graph_memory", "experience"),
    ("speech_queue", "out_voice"), ("speech_queue", "out_pet"), ("speech_queue", "out_telegram"),
    ("speech_queue", "out_dashboard"), ("claude", "speech_queue"),
]

# Состояния хода и узел, который переводит в состояние (поведенческий граф без LangGraph)
STATES = [
    ("received", "принято", "bus"), ("classified", "классифицировано", "gliner"), ("decided", "решено", "attention"),
    ("memory", "память", "graph_memory"), ("llm", "LLM", "gemma"), ("tool", "инструмент", "tools"),
    ("answered", "ответ", "speech_queue"), ("silent", "молчание", "attention"),
]
TRANSITIONS = [
    ("received", "classified", ""), ("classified", "decided", ""), ("decided", "silent", "D0, D1 или D2"),
    ("decided", "memory", "нужна память"), ("decided", "llm", "нужна реплика или план"),
    ("memory", "llm", ""), ("llm", "tool", "план требует действия"), ("tool", "llm", "цикл"),
    ("llm", "answered", ""), ("decided", "answered", "готовая реплика"),
]

OUTPUT_OF_MODULE = {"voice": "out_voice", "pet": "out_pet", "telegram": "out_telegram"}


def module_node(module_id: str) -> str:
    return f"module:{module_id}"


def topology(module_ids=()) -> dict:
    """Узлы, рёбра, слои, состояния. Каждому модулю: узел `module:<id>` и рёбра в шину и из инструментов."""
    nodes = [{"id": i, "label": t, "layer": layer} for i, t, layer in NODES]
    edges = [{"id": edge_id(a, b), "from": a, "to": b} for a, b in EDGES]
    for mid in sorted(set(module_ids)):
        n = module_node(mid)
        nodes.append({"id": n, "label": mid, "layer": "modules"})
        edges.append({"id": edge_id(n, "bus"), "from": n, "to": "bus"})
        edges.append({"id": edge_id("tools", n), "from": "tools", "to": n})
    return {"layers": [{"id": k, "title": v[0], "color": v[1]} for k, v in LAYERS.items()], "nodes": nodes, "edges": edges,
            "states": [{"id": i, "label": t, "node": n} for i, t, n in STATES],
            "transitions": [{"from": a, "to": b, "when": w} for a, b, w in TRANSITIONS]}

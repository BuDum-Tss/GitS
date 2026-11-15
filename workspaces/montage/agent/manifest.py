"""Манифест окружения «Монтаж» (ТЗ 20, раздел 5; docs/WORKSPACE_CONTRACT.md 3.2). Монтирует сама ГЛаДОС: окружение даёт ей
инструменты (`skills`, `files`, `run`, `work`, `site`, `resources`), навыки, рабочую область, команды и сайт. Описания инструментов
читает планировщик ядра."""
import copy
import os

from gits_sdk import Sandbox
from gits_sdk.protocol import WorkspaceCommand

import area
from layout import ROOT
from montage_site import site_url

SOURCE_KINDS = ("talking_head", "rimworld_run", "narrated", "generic")
PLATFORMS = ("shorts", "reels", "youtube", "none")
FORMATS = ("vertical", "horizontal")
VOICEOVERS = ("none", "character", "keep")
VOICEOVER_ALIASES = {"glados": "character"}     # прежнее имя режима (ТЗ 05, CHR-4): старые задания читаются так же


def normalize_voiceover(value) -> str:
    """Режим закадра: прежнее значение glados читается как character; неизвестное возвращается как есть."""
    v = str(value or "")
    return VOICEOVER_ALIASES.get(v, v)

WINDOWS_OUTPUT_DIR = r"%USERPROFILE%\Videos\GLaDOS"
ENV_OUT_DIR = "MONTAGE_OUT_DIR"          # папка готовых роликов; в контейнере /data/montage/out (том персонажа)


def default_output_dir() -> str:
    """Папка готовых роликов по умолчанию: `MONTAGE_OUT_DIR`, иначе Видео пользователя (Windows)."""
    return os.environ.get(ENV_OUT_DIR, "").strip() or WINDOWS_OUTPUT_DIR


def resolve_output_dir(value: str | None = None) -> str:
    """Настройка `output_dir` с подстановкой переменных. Если задан `MONTAGE_OUT_DIR`, а в настройке осталось
    прежнее значение по умолчанию (путь Windows, в контейнере он бессмыслен), берётся `MONTAGE_OUT_DIR`."""
    value = str(value or "").strip()
    env_dir = os.environ.get(ENV_OUT_DIR, "").strip()
    if env_dir and (not value or value == WINDOWS_OUTPUT_DIR):
        return env_dir
    return os.path.expandvars(value or default_output_dir())


_KEYWORDS = ("монтаж", "смонтируй", "ролик", "видео", "шортс", "рилс", "клип", "субтитры", "нарезка")

BRIEF = ("Сначала montage.skills(name=montage-regulation, file=START.md). Ролик ищи через montage.work list/status, "
         "новую единицу создавай только для нового. Пути в files начинаются с id ролика. "
         "Ролик собирай сценами: work op=scenes (без времён), затем run compose. "
         "Задачи: assistant.tasks найди открытую; сделанный шаг сразу отмечай assistant.task_step (done); в конце close_task.")

# разрешённые команды (`run`): исчерпывающий список, оболочки нет; первый аргумент каждой это id единицы (ролика)
COMMANDS = [
    WorkspaceCommand("intake", "Приём исходников единицы: копирование, проверка, перекодирование", background=True, timeout_s=3600),
    WorkspaceCommand("transcribe", "Слова для субтитров (журнал речи забега)", timeout_s=600),
    WorkspaceCommand("compose", "Монтажный лист из сцен (work op=scenes): озвучка фраз и раскладка по времени, пишет plan.json",
                     background=True, timeout_s=1800),
    WorkspaceCommand("voiceover", "Озвучка реплик готового plan.json её голосом", background=True, timeout_s=1800),
    WorkspaceCommand("render", "Черновик ролика (половинный размер, renders/preview.mp4)", background=True, timeout_s=10800),
    WorkspaceCommand("still", "Кадр плана: renders/still_<кадр>.png (аргумент: номер кадра)", timeout_s=300),
    WorkspaceCommand("compositions", "Список композиций проекта Remotion", timeout_s=300),
    WorkspaceCommand("check", "Проверка плана и кадров черновика перед финалом (отчёт qa/report.md)", timeout_s=600),
    WorkspaceCommand("final", "Финальный рендер в выходную папку (без свежей проверки check отказ)", background=True, timeout_s=10800,
                     irreversible=True),
]

_RUN_NOTE = " Первый аргумент: id единицы (инструмент work); у still вторым номер кадра."


def _standard_tools() -> list[dict]:
    """Инструменты `skills`, `files`, `run` из SDK (описания и риски общие у всех окружений)."""
    tools = area.MontageTools(Sandbox(ROOT, readonly=["."]), commands=COMMANDS).declare_tools()
    for t in tools:
        if t["name"] == "run":
            t["description"] += _RUN_NOTE
            t["params"]["args"]["description"] = "аргументы: id единицы, затем (для still) номер кадра"
        if t["name"] == "files":
            t["description"] = ("Чтение файлов и запись идеи, сценария и plan.json. В плане время в миллисекундах: "
                                "20 секунд = duration_ms: 20000. Перед записью прочитай пример из template/demo: "
                                "plan.horizontal.json или plan.vertical.json.")
            t["params"]["content"]["description"] = (
                "plan.json: version: 1, title, format {width,height,fps}, duration_ms, segments, captions. "
                "Звук: отдельные поля voiceover, music, sfx."
            )
        t["keywords"] = [*t.get("keywords", []), *_KEYWORDS]
    return tools


def _tools() -> list[dict]:
    return [
        *_standard_tools(),
        {
            "name": "work",
            "description": "Ролики монтажа: create (бриф, исходники, площадка), list, status, update (режим закадра), "
                           "scenes (описать ролик сценами без времён; лист соберёт run compose), cancel.",
            "params": {
                "op": {"type": "string", "enum": ["create", "list", "status", "update", "scenes", "cancel"], "required": True,
                       "description": "действие"},
                "scenes": {"type": "array", "required": False,
                           "description": "scenes: список сцен по порядку. Сцена: kind (intro, glados_room, chamber_sign, result_sign, "
                                          "blueprint, custom), description (что на картинке), say (список фраз закадра), assets (какие "
                                          "материалы берёшь), props (параметры вставки), mood, emotion, hold_ms, beat. Времён нет."},
                "cta": {"type": "string", "required": False, "description": "scenes: текст призыва в конце (если площадка его требует)"},
                "id": {"type": "string", "required": False, "description": "id единицы (status, update, cancel)"},
                "brief": {"type": "string", "required": False, "description": "create: что за ролик, своими словами пользователя"},
                "title": {"type": "string", "required": False, "description": "create: название (по умолчанию из брифа)"},
                "sources": {"type": "array", "required": False,
                            "description": "create: пути файлов или папок с исходниками: абсолютные или resources/... (свои ресурсы, как их отдаёт инструмент resources)"},
                "source_kind": {"type": "string", "enum": list(SOURCE_KINDS), "required": False,
                                "description": "create: вид исходников; если не задан, определяется по файлам"},
                "format": {"type": "string", "enum": list(FORMATS), "required": False,
                           "description": "create: ориентация кадра; точные размеры задаются в монтажном листе"},
                "duration_s": {"type": "integer", "required": False,
                               "description": "create: целевая длительность в секундах. В plan.json используй duration_ms."},
                "voiceover": {"type": "string", "enum": [*VOICEOVERS, *VOICEOVER_ALIASES], "required": False,
                              "description": "create/update: закадр: none, character (голос персонажа) или keep (оставить голос исходника)"},
                "set": {"type": "string", "enum": ["working", "waiting_user"], "required": False,
                        "description": "status: пометить единицу (waiting_user: жду ответа пользователя)"},
            },
            "risk": "auto",
            "availability": "always",
            "timeout_s": 60,
            "keywords": list(_KEYWORDS),
        },
        {
            "name": "site",
            "description": "Адрес сайта монтажа (ролики, сценарий, монтажный лист, файлы, кнопка Remotion Studio) и страница ролика: "
                           "ссылка для пользователя.",
            "params": {"id": {"type": "string", "required": False, "description": "id единицы; без него адрес сайта"}},
            "risk": "auto",
            "availability": "always",
            "informative": True,
            "keywords": [*_KEYWORDS, "сайт", "ссылка", "студия", "studio", "remotion"],
        },
        {
            "name": "resources",
            "description": "Её ресурсы для монтажа (интро, комната, питомец, рамки, чертежи, шрифты, звуки) с путями resources/... "
                           "(годятся в files и в sources work). Сначала вызови без параметров: это оглавление; потом group=<имя> за списком.",
            "params": {"group": {"type": "string", "required": False,
                                 "description": "имя группы из оглавления (Комната, Питомец, Интро...): полный список её файлов с размерами"}},
            "risk": "auto",
            "availability": "always",
            "informative": True,
            "keywords": [*_KEYWORDS, "ресурсы", "заставка", "интро", "материалы", "что у тебя есть"],
        },
    ]


def _workspace_block() -> dict:
    return {"id": "montage", "title": "Монтаж", "purpose": "монтаж роликов: приём, план, озвучка, черновик, проверка, финал, правки",
            "entry": ["user_message", "self_impulse", "critical"], "interruptible": True,
            "safe_points": ["между командами окружения"], "signals_in": ["telegram.*"],
            "done_when": "агент вышел из окружения (workspace.exit) и операций не осталось",
            "when_to_use": "когда просят смонтировать ролик, шортс или клип из видеофайлов, наложить субтитры и графику, "
                           "озвучить закадровым голосом или поправить готовый монтаж",
            "keywords": ["монтаж", "ролик", "шортс", "рилс", "клип", "субтитры", "видео"],
            "brief": BRIEF,
            "skills": [s.to_dict() for s in area.discover_skills(ROOT / ".claude" / "skills")],
            "area": {"root": area.AREA_ROOT, "writable": list(area.WRITABLE), "readonly": list(area.READONLY),
                     "hidden": list(area.HIDDEN)},
            "commands": [c.to_dict() for c in COMMANDS],
            "site": {"url": site_url()}}


MANIFEST = {
    "id": "montage",
    "role": "workspace",
    "ownership": "shared",
    "title": "Монтаж",
    "description": "Монтаж роликов из файлов: рабочая область, навыки Remotion и регламент, приём, расшифровка, её закадровый голос, "
                   "черновик, проверка и финальный рендер. Монтирует сама ГЛаДОС; долгие команды идут в фоне, итог приходит позже.",
    "version": "0.2.0",
    "subscribe": ["settings", "module"],
    "shared_settings": ["dnd", "quiet_until"],
    "settings_schema": {
        "output_dir": {"type": "string", "title": "Папка готовых роликов", "default": default_output_dir()},
        "keep_jobs": {"type": "integer", "title": "Сколько завершённых единиц хранить", "default": 10, "min": 1, "max": 100},
    },
    "tools": _tools(),
    "emits": [
        {"kind": "montage.launch", "description": "Запущен долгий рендер (факт: что рендерится и оценка времени)",
         "default_urgency": 2},
        {"kind": "montage.done", "description": "Операция монтажа закончена (черновик, озвучка, финальный ролик)", "default_urgency": 3},
        {"kind": "montage.failed", "description": "Операция монтажа не удалась", "default_urgency": 3},
    ],
    "state": True,
    # окружение агента (ТЗ 32, WS-4; контракт 3.2): вход по сообщению пользователя, по самоимпульсу или по критическому сигналу
    "workspace": _workspace_block(),
    # пространство в дашборде (TZ 15, TAB-4, TAB-9): только сводка и ссылка на сайт; блоки присылает менеджер (panel.set)
    "panel": {
        "title": "Монтаж",
        "icon": "film",
        "order": 40,
        "links": [
            {"id": "site", "label": "Сайт монтажа", "kind": "url", "url": site_url()},
        ],
    },
}


def serve_dirs(jobs_dir, output_dir: str | None = None) -> list[str]:
    """Каталоги, из которых ядро раздаёт файлы единиц и открывает папки (panel.serve_dirs, TZ 15). Выбираются при
    запуске: если пользователь потом сменит `output_dir`, модуль перезапускают (манифест принимается один раз)."""
    out = resolve_output_dir(output_dir)
    return [os.path.abspath(str(jobs_dir)), os.path.abspath(out)]


def build_manifest(serve: list[str] | None = None) -> dict:
    """Свежая копия манифеста (клиент тоже копирует, но тестам удобно менять свою). `serve` - serve_dirs для panel."""
    m = copy.deepcopy(MANIFEST)
    if serve:
        m["panel"]["serve_dirs"] = list(serve)
    return m

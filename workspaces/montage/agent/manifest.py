"""Манифест модуля монтажа (ТЗ 20, раздел 5: MON-5, MON-6). Описания инструментов читает планировщик ядра."""
import copy
import os

from montage_site import site_url

SOURCE_KINDS = ("talking_head", "rimworld_run", "narrated", "generic")
PLATFORMS = ("shorts", "reels", "telegram", "youtube", "none")
FORMATS = ("vertical", "horizontal", "square")
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

MANIFEST = {
    "id": "montage",
    "role": "workspace",
    "ownership": "shared",
    "title": "Монтаж",
    "description": "Монтаж роликов из файлов: расшифровка, план, её закадровый голос, графика Aperture, субтитры, "
                   "черновик и финальный рендер. Задачи долгие, результат приходит позже.",
    "version": "0.1.0",
    "subscribe": ["settings", "module"],
    "shared_settings": ["dnd", "quiet_until", "pi_model_heavy", "pi_thinking"],
    "settings_schema": {
        "approve_plan": {"type": "boolean", "title": "Спрашивать утверждение плана монтажа", "default": True},
        "auto_final": {"type": "boolean", "title": "Рендерить финал без вопроса «готово / правки»", "default": False},
        "plan_model": {"type": "string", "title": "Модель для истории и плана", "default": "opus",
                       "enum": ["opus", "sonnet"]},
        "build_model": {"type": "string", "title": "Модель для сборки проекта Remotion", "default": "sonnet",
                        "enum": ["opus", "sonnet"]},
        "output_dir": {"type": "string", "title": "Папка готовых роликов", "default": default_output_dir()},
        "keep_jobs": {"type": "integer", "title": "Сколько завершённых задач хранить", "default": 10, "min": 1,
                      "max": 100},
        "max_turns_stage": {"type": "integer", "title": "Лимит шагов агента на стадию", "default": 80, "min": 10,
                            "max": 300},
        "review_mode": {"type": "string", "title": "Режим приёмки черновика (auto только при ≥ 20 решениях и согласии ≥ 90 %)",
                        "default": "human", "enum": ["human", "auto_then_human", "auto"]},
    },
    "tools": [
        {
            "name": "start",
            "description": "Смонтировать видео, ролик, шортс или клип из файлов: субтитры, графика, её закадровый голос. "
                           "Долго, результат придёт позже.",
            "params": {
                "sources": {"type": "array", "description": "абсолютные пути файлов или папок с исходниками; свои ресурсы: пути из инструмента resources"},
                "brief": {"type": "string", "description": "что за ролик, своими словами пользователя"},
                "source_kind": {"type": "string", "enum": list(SOURCE_KINDS), "required": False,
                                "description": "вид исходников; если не задан, определяется по файлам"},
                "platform": {"type": "string", "enum": list(PLATFORMS), "required": False,
                             "description": "площадка; по умолчанию none"},
                "format": {"type": "string", "enum": list(FORMATS), "required": False,
                           "description": "формат кадра; по умолчанию по площадке"},
                "duration_s": {"type": "integer", "required": False, "description": "целевая длительность, секунд"},
                "voiceover": {"type": "string", "enum": [*VOICEOVERS, *VOICEOVER_ALIASES], "required": False,
                              "description": "закадр: none, character (голос персонажа) или keep (оставить голос исходника)"},
            },
            "risk": "ask",
            "availability": "always",
            "timeout_s": 30,
            "keywords": list(_KEYWORDS),
            "job": True,
            "announce": True,
        },
        {
            "name": "status",
            "description": "Состояние задач монтажа: что в очереди, что выполняется, на какой стадии.",
            "params": {"job_id": {"type": "string", "required": False, "description": "id задачи; без него все"}},
            "risk": "auto",
            "availability": "always",
            "informative": True,
            "keywords": list(_KEYWORDS),
        },
        {
            "name": "resources",
            "description": "Какие у неё есть ресурсы для монтажа (интро-заставки, комната, стикеры питомца, рамки, чертежи, шрифты, "
                           "звуки) и их пути: годятся в sources инструмента start.",
            "params": {},
            "risk": "auto",
            "availability": "always",
            "informative": True,
            "keywords": [*_KEYWORDS, "ресурсы", "заставка", "интро", "материалы", "что у тебя есть"],
        },
        {
            "name": "revise",
            "description": "Правки к черновику или готовому ролику: задача возвращается на стадию сборки.",
            "params": {"job_id": {"type": "string", "description": "id задачи"},
                       "notes": {"type": "string", "description": "что изменить, своими словами пользователя"}},
            "risk": "auto",
            "availability": "always",
            "keywords": list(_KEYWORDS),
        },
        {
            "name": "cancel",
            "description": "Отменить задачу монтажа.",
            "params": {"job_id": {"type": "string", "description": "id задачи"}},
            "risk": "auto",
            "availability": "always",
            "keywords": list(_KEYWORDS),
        },
        {
            "name": "open_studio",
            "description": "Открыть сайт монтажа задачи: сценарий, монтажный лист, план, все файлы и кнопка Remotion Studio "
                           "(просмотр без рендера, гаснет через 30 минут без обращения). Для просьб «покажи монтаж», «открой студию».",
            "params": {"job_id": {"type": "string", "description": "id задачи"}},
            "risk": "auto",
            "availability": "always",
            "timeout_s": 120,
            "keywords": list(_KEYWORDS) + ["студия", "studio", "remotion"],
        },
    ],
    "emits": [
        {"kind": "montage.launch", "description": "Запущен долгий рендер (факт: что рендерится и оценка времени)",
         "default_urgency": 2},
        {"kind": "montage.done", "description": "Ролик готов", "default_urgency": 3},
        {"kind": "montage.failed", "description": "Задача монтажа не удалась", "default_urgency": 3},
    ],
    "state": True,
    # среда агента (ТЗ 32, WS-4): в неё входят по сообщению пользователя («давай монтировать»), по самоимпульсу или по
    # критическому сигналу; закончена, когда задачи, начатые при ней, готовы
    "workspace": {"id": "montage", "title": "Монтаж", "purpose": "монтаж роликов: нарезка, склейка, озвучка, выгрузка, правки",
                  "entry": ["user_message", "self_impulse", "critical"], "interruptible": True,
                  "safe_points": ["между стадиями задачи"], "signals_in": ["telegram.*"],
                  "done_when": "задачи, начатые при среде, готовы или отменены",
                  "when_to_use": "когда просят смонтировать ролик, шортс или клип из видеофайлов, наложить субтитры и графику, "
                                 "озвучить закадровым голосом или поправить готовый монтаж",
                  "keywords": ["монтаж", "ролик", "шортс", "рилс", "клип", "субтитры", "видео"]},
    # пространство в дашборде (TZ 15, TAB-4, TAB-9): ссылки и кнопки; блоки присылает менеджер (panel.set)
    "panel": {
        "title": "Монтаж",
        "icon": "film",
        "order": 40,
        # у среды свой сайт (сценарий, монтажный лист, файлы, Studio); в дашборде только сводка и ссылка на него
        "links": [
            {"id": "site", "label": "Сайт монтажа", "kind": "url", "url": site_url()},
        ],
        # одобрение по этапам (TZ 15b): кнопки этапа истории и плана, черновика и вопросы агента (answer_question)
        "actions": ["approve_plan", "rework_plan", "reject_plan", "approve_draft", "rework_draft",
                    "answer_question"],
    },
}

def serve_dirs(jobs_dir, output_dir: str | None = None) -> list[str]:
    """Каталоги, из которых ядро раздаёт файлы задач и открывает папки (panel.serve_dirs, TZ 15). Выбираются при
    запуске: если пользователь потом сменит `output_dir`, модуль перезапускают (манифест принимается один раз)."""
    out = resolve_output_dir(output_dir)
    return [os.path.abspath(str(jobs_dir)), os.path.abspath(out)]


def build_manifest(serve: list[str] | None = None) -> dict:
    """Свежая копия манифеста (клиент тоже копирует, но тестам удобно менять свою). `serve` - serve_dirs для panel."""
    m = copy.deepcopy(MANIFEST)
    if serve:
        m["panel"]["serve_dirs"] = list(serve)
    return m

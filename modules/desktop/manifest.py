"""Манифест модуля desktop (ТЗ 14, разделы 4-5). Описания инструментов перенесены из прежних мест ядра
(environments/builtin.py, environments/pc_control.py, сам tool-список app_launcher) без изменения смысла: их читает планировщик.

Инструменты с `announce: true` (README, правило 14): launch_app, operate_screen, claude_task. take_screenshot объявляет
себя сам только для target=screen (announce в манифесте не ставится: флаг относится ко всему инструменту)."""
import copy

_BACKUP_NOTE = "Запасной уровень (по экрану): используй, только если у программы нет прямого инструмента или адаптера. "
_BTN = {"type": "string", "enum": ["left", "right"], "required": False, "description": "кнопка мыши, по умолчанию left"}
_DBL = {"type": "boolean", "required": False, "description": "двойной щелчок"}

TOOLS = [
    {"name": "take_screenshot",
     "description": "Сделать скриншот и сохранить файлом. target=game: окно RimWorld (без вопросов; игра должна быть на "
                    "переднем плане, либо focus=true: она сама переключится на неё); target=screen: весь экран (всегда "
                    "спрашивает разрешение).",
     "params": {"target": {"type": "string", "enum": ["game", "screen"], "required": False,
                           "description": "game - окно игры (по умолчанию), screen - весь экран"},
                "focus": {"type": "boolean", "required": False, "description": "переключиться на игру перед снимком"}},
     "risk": "auto", "availability": "always", "informative": True, "danger_check": True,
     "keywords": ["скриншот", "снимок", "сфотографируй", "скрин", "фото", "сними", "картинка"]},
    {"name": "describe_screen",
     "description": "Посмотреть на экран прямо сейчас и описать, что на нём (приложения, чем занят пользователь).",
     "params": {}, "risk": "auto", "availability": "always", "informative": True,
     "keywords": ["экран", "видишь", "посмотри", "окно", "делаю", "открыто"]},
    {"name": "read_screen",
     "description": _BACKUP_NOTE + "Прочитать надписи на экране (OCR) с координатами в активном окне. Для вопросов «что там "
                    "написано» и чтобы выбрать, на что нажать.",
     "params": {}, "risk": "auto", "availability": "always", "informative": True,
     "keywords": ["прочитай", "написано", "экран", "надпись", "текст на экране"]},
    {"name": "operate_screen",
     "description": "Запасной уровень (по экрану): выполнить цель в ЛЮБОЙ программе, нажимая мышью и клавиатурой "
                    "шаг за шагом по надписям на экране (ведёт локальная модель). Используй, только если у программы "
                    "нет прямого инструмента.",
     "params": {"goal": {"type": "string", "description": "цель своими словами: «открой настройки звука и убавь громкость»"},
                "max_steps": {"type": "integer", "required": False, "description": "не больше шагов (по умолчанию 8)"}},
     "risk": "ask", "availability": "always", "informative": True, "timeout_s": 120, "job": False, "announce": True,
     "keywords": ["открой", "нажми", "настрой", "включи", "выключи", "в программе", "в приложении", "кнопку", "меню"]},
    {"name": "list_windows",
     "description": "Перечислить открытые окна на компьютере (заголовки).",
     "params": {}, "risk": "auto", "availability": "always", "informative": True,
     "keywords": ["окна", "окно", "открыто", "программы", "приложения"]},
    {"name": "focus_window",
     "description": _BACKUP_NOTE + "Переключиться на окно по части заголовка.",
     "params": {"title": {"type": "string", "description": "часть заголовка окна"}},
     "risk": "ask", "availability": "always",
     "keywords": ["окно", "переключись", "открой", "перейди", "свернуто"]},
    {"name": "click_text",
     "description": _BACKUP_NOTE + "Нажать мышью на надпись на экране (кнопку, ссылку, пункт меню): находит текст по экрану "
                    "и кликает в его центр.",
     "params": {"text": {"type": "string", "description": "надпись, на которую нажать"}, "button": _BTN, "double": _DBL},
     "risk": "ask", "availability": "always",
     "keywords": ["нажми", "кликни", "клик", "кнопка", "кнопку", "нажать", "ссылка", "пункт", "меню"]},
    {"name": "click",
     "description": _BACKUP_NOTE + "Нажать мышью в точку экрана по координатам (пиксели от левого верхнего угла).",
     "params": {"x": {"type": "integer", "description": "x"}, "y": {"type": "integer", "description": "y"},
                "button": _BTN, "double": _DBL},
     "risk": "ask", "availability": "always",
     "keywords": ["нажми", "кликни", "координаты", "точка", "клик"]},
    {"name": "type_text",
     "description": _BACKUP_NOTE + "Набрать текст на клавиатуре в активном окне (в том числе русский). Перед этим нужно, "
                    "чтобы курсор стоял в нужном поле.",
     "params": {"text": {"type": "string", "description": "что набрать (до 500 символов)"}},
     "risk": "ask", "availability": "always", "danger_check": True,
     "keywords": ["набери", "напечатай", "введи", "впиши", "текст", "клавиатура"]},
    {"name": "press_key",
     "description": _BACKUP_NOTE + "Нажать клавишу или сочетание клавиш, например enter, esc, f5, ctrl+s, alt+tab.",
     "params": {"keys": {"type": "string", "description": "клавиши через плюс: ctrl+s"}},
     "risk": "ask", "availability": "always", "danger_check": True,
     "keywords": ["нажми", "клавиша", "enter", "сохрани", "сочетание", "горячая"]},
    {"name": "scroll",
     "description": _BACKUP_NOTE + "Прокрутить колесо мыши (положительное число вверх, отрицательное вниз).",
     "params": {"amount": {"type": "integer", "description": "на сколько щелчков"},
                "x": {"type": "integer", "required": False, "description": "x, где прокрутить"},
                "y": {"type": "integer", "required": False, "description": "y, где прокрутить"}},
     "risk": "ask", "availability": "always",
     "keywords": ["прокрути", "скролл", "вниз", "вверх", "колесо"]},
    {"name": "launch_app",
     "description": "Запустить любое установленное приложение по названию, как в Alt+Пробел (меню «Пуск» и магазин "
                    "Windows). RimWorld запускается с ожиданием мода. Для просьб «запусти римворлд», «пойдём играть», "
                    "«открой хром», «включи телеграм». Консоли, реестр и деинсталляторы недоступны.",
     "params": {"name": {"type": "string", "description": "название приложения, как сказал пользователь"}},
     "risk": "auto", "availability": "always", "informative": True, "timeout_s": 120, "announce": True,
     "keywords": ["запусти", "запускай", "открой", "включи", "включай", "игру", "римворлд", "rimworld", "поиграем",
                  "играть", "приложение", "программу", "хром", "докер", "телеграм", "блокнот"]},
    {"name": "list_apps",
     "description": "Найти установленные приложения по части названия (или назвать несколько).",
     "params": {"query": {"type": "string", "required": False, "description": "часть названия"}},
     "risk": "auto", "availability": "always", "informative": True,
     "keywords": ["приложения", "программы", "что можешь запустить", "что установлено"]},
    {"name": "add_app",
     "description": "Добавить приложение в свой список для запуска: нужен путь к .exe, .lnk, .bat или .cmd (для программ "
                    "вне меню «Пуск»). Каждый раз спрашивает разрешение.",
     "params": {"title": {"type": "string", "description": "как называть приложение"},
                "path": {"type": "string", "description": "полный путь к файлу запуска"},
                "aliases": {"type": "array", "required": False, "description": "другие названия"}},
     "risk": "ask", "availability": "always", "informative": True,
     "keywords": ["добавь", "приложение", "программу", "список"]},
    {"name": "claude_task",
     "description": "Сложные задачи на компьютере: работа с файлами и кодом проекта, команды, поиск в интернете, анализ. "
                    "Выполняет Claude и сам спрашивает разрешение на каждое изменение.",
     "params": {"task": {"type": "string", "description": "что нужно сделать, своими словами пользователя"},
                "context": {"type": "string", "required": False,
                            "description": "краткий контекст разговора (заполняет ядро, планировщик его не видит)"}},
     "risk": "auto", "availability": "always", "informative": True, "job": True, "fallback": True, "announce": True,
     "core_fills": ["context"],
     "keywords": ["файл", "код", "команда", "найди", "интернет", "проект", "скрипт", "поиск", "папка", "почини", "исправь"]},
    {"name": "menu_click",
     "description": "Нажать кнопку главного меню RimWorld без Claude и без вопроса (OCR по окну игры). Только надписи из "
                    "белого списка («Быстрый запуск», «Продолжить», «Загрузить») и только когда пользователь не за компьютером.",
     "params": {"text": {"type": "string", "description": "надпись кнопки меню"}},
     "risk": "auto", "availability": "always",
     "keywords": ["римворлд", "меню", "быстрый запуск", "продолжить", "загрузить"]},
    {"name": "last_screenshot",
     "description": "Путь к последнему сохранённому снимку экрана или игры, чтобы приложить его к черновику поста.",
     "params": {}, "risk": "auto", "availability": "always", "informative": True,
     "keywords": ["снимок", "скриншот", "фото", "черновик", "последний"]},
]

EMITS = [
    {"kind": "desktop.app_changed", "description": "Сменилось активное приложение (служебное: не озвучивается)",
     "default_urgency": 0},
    {"kind": "desktop.activity", "description": "Итог анализа экрана: чем занят пользователь", "default_urgency": 1},
    {"kind": "desktop.user_idle", "description": "Простой ввода (клавиатура и мышь) 10 минут и больше", "default_urgency": 1},
    {"kind": "desktop.user_back", "description": "Пользователь вернулся к компьютеру после простоя", "default_urgency": 2},
]

MANIFEST = {
    "id": "desktop",
    "title": "Компьютер",
    "description": "Экран, приложения, мышь и клавиатура на этом компьютере: что на экране, снимки, запуск программ, "
                   "управление по экрану и Claude-помощник для сложных задач.",
    "version": "1.0.0",
    "capabilities": ["screen.observe"],
    "subscribe": ["settings", "module"],
    "shared_settings": ["vision", "dnd", "quiet_until", "auto_accept"],
    "tools": TOOLS,
    "emits": EMITS,
    "state": True,
    # пространство в дашборде (TZ 15, TAB-6/TAB-9): активное окно, разрешения, задачи помощника Claude и их остановка
    "panel": {"title": "Компьютер", "icon": "monitor", "order": 50, "actions": ["cancel_task", "cancel_all"]},
}


def build_manifest() -> dict:
    """Свежая копия манифеста (тестам удобно менять свою)."""
    return copy.deepcopy(MANIFEST)

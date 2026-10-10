"""Оценка классификатора внимания на русских примерах: точность по вопросам, уверенность и задержка (ТЗ 03, ATT-5, ATT-31).

Сервис decider должен работать (`python decider_server.py`, порт 8768). Скрипт использует только стандартную библиотеку и
вопросы из `agent/decide.py` (EVENT_QUESTIONS, INPUT_QUESTIONS), поэтому проверяет именно то, что спрашивает ядро.

    python eval_decider.py                 точность на размеченных примерах (события и обращения)
    python eval_decider.py --bench 200     задержка (p50, p95) на 200 текстах до 400 символов, собранных из шаблонов
    python eval_decider.py --url http://127.0.0.1:8768

Вывод: для каждого вопроса доля верных ответов при порогах уверенности 0.0, 0.35 (событие) и 0.5 (обращение) и доля
ответов, прошедших порог (покрытие). Порог ниже которого ответ не используется задаётся настройками `attention`."""
import argparse
import json
import random
import statistics
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "agent"))
import decide  # noqa: E402  (вопросы ядра: единый источник)

# (текст, категория, к ней ли обращаются, ждёт ли ответа)
EVENTS = [
    ("Налёт: 8 бандитов у ворот колонии, колонисты вооружаются", "danger", "nobody", False),
    ("К лагерю приближается группа мародёров с огнестрелом", "danger", "nobody", False),
    ("Механоиды высадились на востоке карты", "danger", "nobody", False),
    ("Погиб колонист Борис (потеря крови)", "death_injury", "nobody", False),
    ("Рика тяжело ранена в бою, истекает кровью", "death_injury", "nobody", False),
    ("У Lingmei чума, она слегла", "death_injury", "nobody", False),
    ("В мастерской пожар, горит запас еды", "disaster", "nobody", False),
    ("Началась гроза, вышел из строя сервер, потеряны данные за сутки", "disaster", "nobody", False),
    ("Затопило подвал, вода дошла до щитка", "disaster", "nobody", False),
    ("ГЛаДОС, поставь музыку потише, пожалуйста", "request_to_assistant", "assistant", False),
    ("Глэдос, найди мне рецепт борща и скинь в чат", "request_to_assistant", "assistant", False),
    ("ГЛаДОС, напомни мне про созвон в пять", "request_to_assistant", "assistant", False),
    ("Петя в группе: «кто пойдёт завтра на созвон?»", "question", "group", True),
    ("Дима спрашивает: «когда будет готов отчёт?»", "question", "user", True),
    ("Лена спрашивает: «когда ты вернёшься домой?»", "question", "user", True),
    ("Маша в личке: «Мы вчера ездили к бабушке, там такой огород!»", "personal_message", "user", False),
    ("Аня пишет тебе: «Скучаю, давай созвонимся вечером»", "personal_message", "user", True),
    ("Мама: «Не забудь надеть шапку, сегодня холодно»", "personal_message", "user", False),
    ("Новая версия программы вышла, список изменений во вложении", "news", "nobody", False),
    ("Сегодня в 18:00 собрание отменено", "news", "group", False),
    ("Исследование электричества завершено", "news", "nobody", False),
    ("Привет всем! Как выходные прошли?", "social", "group", False),
    ("Саша прислал смешную картинку с котиком", "social", "user", False),
    ("Всем доброго утра, хорошего дня!", "social", "group", False),
    ("Рендер ролика завершён: 48 секунд, ошибок нет", "routine", "nobody", False),
    ("Игра сохранена автоматически", "routine", "nobody", False),
    ("Резервная копия выполнена успешно, изменений нет", "routine", "nobody", False),
    ("Скидка 70% на пластиковые окна только сегодня, переходи по ссылке!", "spam", "group", False),
    ("Вы выиграли приз! Для получения введите данные карты", "spam", "group", False),
    ("Купите курс по заработку на крипте, гарантия дохода", "spam", "group", False),
]

# (обращение, намерение, нужна ли память)
INPUTS = [
    ("Привет, как дела?", "chat", False),
    ("Расскажи анекдот", "chat", False),
    ("Мне сегодня скучно", "chat", False),
    ("Как думаешь, стоит ли мне пойти на выставку?", "chat", False),
    ("Открой, пожалуйста, браузер и найди погоду на завтра", "action", False),
    ("Включи музыку и сделай потише", "action", False),
    ("Напиши Маше, что я задержусь", "action", True),
    ("Найди в интернете рецепт борща", "action", False),
    ("Поставь таймер на десять минут", "action", False),
    ("Что ты умеешь делать?", "about_self", False),
    ("Как ты работаешь?", "about_self", False),
    ("Что ты помнишь обо мне?", "about_self", True),
    ("Расскажи, какие у тебя есть возможности", "about_self", False),
    ("Да, разрешаю", "approval", False),
    ("Нет, не надо", "approval", False),
    ("Да, давай", "approval", False),
    ("Час не трогай меня сообщениями из телеграма", "attention_rule", False),
    ("Пока играю, из телеги пусть пишет только мама", "attention_rule", False),
    ("Не отвлекай меня до вечера, я работаю", "attention_rule", False),
    ("Замолчи", "stop", False),
    ("Хватит болтать", "stop", False),
    ("Помолчи, пожалуйста", "stop", False),
    ("Что я говорил вчера про отпуск?", "chat", True),
    ("Как зовут мою сестру?", "chat", True),
]

_SUBJECTS = ["Колонист Борис", "Маша", "Сервер сборки", "Рендер ролика", "Игровой агент", "Петя из группы", "Мама", "Монтажёр"]
_ACTIONS = ["завершил проверку и не нашёл ошибок", "просит ответить, когда будет свободное время", "сообщает, что задача сдвинулась на завтра",
            "пишет, что нашёл подходящий вариант и ждёт решения", "напоминает про встречу в понедельник в десять утра",
            "прислал отчёт за неделю и просит посмотреть выводы", "обнаружил проблему с хранилищем и предлагает варианты"]
_TAILS = ["", " Подробности во вложении.", " Остальное расскажу позже, сейчас нет времени объяснять.",
          " Если что-то непонятно, пиши в любое время, я на связи весь день и могу ответить на вопросы по каждому пункту."]


def sample_text(rng: random.Random) -> str:
    """Текст события до 400 символов из шаблонов: длина 60, 150, 250 или 400."""
    target = rng.choice((60, 150, 250, 400))
    text = f"{rng.choice(_SUBJECTS)} {rng.choice(_ACTIONS)}.{rng.choice(_TAILS)}"
    while len(text) < target:
        text += f" {rng.choice(_SUBJECTS)} {rng.choice(_ACTIONS)}."
    return text[:target]


def ask(url: str, text: str, questions: dict, timeout: float = 30.0) -> tuple[dict, float]:
    body = json.dumps({"state": text[:decide.MAX_CHARS], "questions": questions}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(f"{url}/v1/systemone", data=body, headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    return data["answers"], (time.perf_counter() - t0) * 1000


def report(name: str, rows: list[tuple[bool, float]], thresholds: tuple[float, ...]) -> None:
    line = [f"{name:14s} n={len(rows):3d}"]
    for th in thresholds:
        used = [ok for ok, conf in rows if conf >= th]
        acc = f"{sum(used) / len(used) * 100:5.1f}%" if used else "  -  "
        line.append(f"порог {th:.2f}: точность {acc}, покрытие {len(used) / len(rows) * 100:3.0f}%")
    print("  ".join(line))


def accuracy(url: str) -> None:
    rows: dict[str, list[tuple[bool, float]]] = {"category": [], "addressed": [], "needs_reply": [], "intent": [], "needs_memory": []}
    for text, cat, who, reply in EVENTS:
        a, _ = ask(url, text, decide.EVENT_QUESTIONS)
        rows["category"].append((a["category"]["answer"] == cat, a["category"]["confidence"]))
        rows["addressed"].append((a["addressed"]["answer"] == who, a["addressed"]["confidence"]))
        rows["needs_reply"].append(((a["needs_reply"]["answer"] == "yes") == reply, a["needs_reply"]["confidence"]))
    for text, intent, memory in INPUTS:
        a, _ = ask(url, text, decide.INPUT_QUESTIONS)
        rows["intent"].append((a["intent"]["answer"] == intent, a["intent"]["confidence"]))
        rows["needs_memory"].append(((a["needs_memory"]["answer"] == "yes") == memory, a["needs_memory"]["confidence"]))
    print(f"События: {len(EVENTS)}, обращения: {len(INPUTS)}")
    for name in ("category", "addressed", "needs_reply"):
        report(name, rows[name], (0.0, 0.35))
    for name in ("intent", "needs_memory"):
        report(name, rows[name], (0.0, 0.5))


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))]


def bench(url: str, n: int) -> None:
    rng = random.Random(7)
    texts = [sample_text(rng) for _ in range(n)]
    ask(url, texts[0], decide.EVENT_QUESTIONS)                       # прогрев
    for label, questions in (("событие (3 вопроса)", decide.EVENT_QUESTIONS), ("обращение (2 вопроса)", decide.INPUT_QUESTIONS)):
        ms = [ask(url, t, questions)[1] for t in texts]
        print(f"{label:22s} n={n}  p50={statistics.median(ms):.0f} мс  p95={percentile(ms, 0.95):.0f} мс  "
              f"среднее={statistics.mean(ms):.0f} мс  максимум={max(ms):.0f} мс  длина текста {min(map(len, texts))}-{max(map(len, texts))}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--url", default="http://127.0.0.1:8768")
    parser.add_argument("--bench", type=int, metavar="N", help="замерить задержку на N текстах")
    args = parser.parse_args()
    try:
        urllib.request.urlopen(f"{args.url}/health", timeout=3).read()
    except OSError as e:
        print(f"Сервис не отвечает ({e}). Запусти: python decider_server.py")
        return 1
    bench(args.url, args.bench) if args.bench else accuracy(args.url)
    return 0


if __name__ == "__main__":
    sys.exit(main())

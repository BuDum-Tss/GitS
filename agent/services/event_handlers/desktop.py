"""События модуля «Компьютер» (ТЗ 14; вынесено из core_service в ТЗ 29, ARC-7, поведение прежнее)."""
import asyncio


def desktop_state(modules) -> dict:
    """state.data модуля desktop (ТЗ 14, DESK-7): активное приложение, простой, текущая активность экрана.
    Пусто, если модуль не подключён: тогда профиль по окну не выбирается, простоя нет, активности нет."""
    return modules.state_data("desktop") or {}


def make_activity_hook(companion):
    """desktop.activity (ТЗ 14, DESK-3): итог анализа экрана пишется в память занятий (activity.json); событие само
    идёт обычным путём внимания (urgency 1, как voice.heard)."""
    async def on_activity(item) -> None:
        data = item.data or {}
        mem = getattr(companion, "_mem", None)
        if mem is None or not data.get("app") or not data.get("summary"):
            return
        await asyncio.to_thread(mem.add_activity, str(data["app"]), str(data.get("category") or "other"), str(data["summary"]))
    return on_activity

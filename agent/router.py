"""Маршрутизатор просьб: сначала «есть ли у меня такая возможность», потом план или честное «не могу».

Любое сообщение пользователя проходит один путь:
  1. из реестра берутся подходящие инструменты всех окружений (RimWorld, система ПК,
     соцсети, внешние MCP и вебхуки…);
  2. локальная модель-планировщик видит ТОЛЬКО этот перечень и решает: это разговор
     (kind=chat) или просьба действовать (kind=action); если действие, то выполнимо ли оно
     перечисленными инструментами (feasible) и какие вызовы по порядку (steps);
  3. шаги проверяются по схемам инструментов и выполняются с учётом риска
     (auto - сразу, ask - после разрешения, public - по флагу telegram_confirm);
  4. итог возвращается фактом, а сказать пользователю его ассистентка решает уже сама.

handle() возвращает None, если это обычный разговор; пустую строку, если простое действие
выполнено и сказать больше нечего; иначе текст-факт о результате.
"""
import asyncio
import json
import logging
import re

import approvals
import events_log
import fast_brain
import settings
from environments.base import ToolError, validate_args

logger = logging.getLogger("router")

MAX_STEPS = 4

# Глаголы-просьбы: по ним решается, нужен ли планировщик вообще. Болтовня идёт мимо него,
# чтобы отвечать сразу, без лишнего вызова модели.
_VERBS = ("напиш", "включ", "выключ", "найд", "сдела", "покаж", "запомн", "забуд", "отправ", "откр",
          "закр", "замолч", "помолч", "запост", "опублик", "посмотр", "провер", "удал", "созд",
          "скач", "установ", "запуст", "запуск", "стартуй", "поигра", "останов", "постав", "заказ", "купи", "переключ", "отключ",
          "поруч", "клод", "claude", "попроси", "отвлека", "нажм", "кликн", "набер", "напечата", "прокрут", "вспомн", "помниш", "запиши", "закаж", "оплат", "забронир", "отмен", "перезапуст", "обнови", "загруз", "видиш", "слыши", "подсматр", "подслуш", "слушай", "прочит", "сохран", "измени", "поменяй")
_VERB_RE = re.compile(r"\b(" + "|".join(_VERBS) + ")", re.I)
RESULT_CHARS = 600

PLANNER = (
    "Ты — планировщик действий ассистентки на компьютере пользователя. Ниже ПОЛНЫЙ перечень "
    "инструментов, которые у неё есть прямо сейчас. Других возможностей нет.\n\n"
    "ИНСТРУМЕНТЫ:\n{tools}\n\n"
    "Реши по последнему сообщению пользователя:\n"
    "1. kind=\"action\", если он просит что-то СДЕЛАТЬ или узнать то, для чего нужен инструмент "
    "(включить, выключить, написать, посмотреть, найти, опубликовать, запомнить, забыть). "
    "kind=\"chat\", если он просто разговаривает, делится, шутит, спрашивает мнение или спрашивает "
    "о её возможностях. Если сомневаешься, выбирай \"chat\". Рассказ пользователя о СВОИХ действиях "
    "(«я запустила игру», «я включил музыку», «я проверила почту») и приглашения вроде «сыграем?» — это "
    "всегда \"chat\": просьбы к ней тут нет.\n"
    "2. Для action: feasible=true, только если это можно сделать ПЕРЕЧИСЛЕННЫМИ инструментами. "
    "Иначе feasible=false, а в reason коротко: чего не хватает.\n"
    "3. reason заполняй ТОЛЬКО при feasible=false (коротко), иначе пустая строка. steps: вызовы по порядку, не больше %d. В args только параметры из описания, значения "
    "берёшь из просьбы; ничего не выдумывай и не придумывай несуществующих инструментов.\n\n"
    "ПРЕДПОЧТЕНИЕ: сначала выбирай узкие инструменты (их выполняет локальная модель и экран, это быстро и бесплатно); "
    "claude_task только если узких инструментов для просьбы нет.\n"
    "claude_task (если он есть в списке) это универсальный помощник на Claude Code для ЛЮБЫХ задач на компьютере: "
    "установка программ и библиотек, файлы, код, команды, интернет, анализ. Он сам спросит разрешение на каждое "
    "изменение. Если просьба про компьютер, программы, файлы или код, а узкого инструмента нет, выбирай kind=action "
    "и claude_task с task, в котором просьба своими словами пользователя; feasible=false только если не подходит "
    "и claude_task. Незнакомое слово может быть названием программы или библиотеки (например «телетон» это "
    "Telethon, библиотека Python): не отвечай, что такого нет. Если пользователь просит поручить что-то Клоду, "
    "Claude или помощнику, задачу возьми из его предыдущих реплик.\n\n"
    "Ответ строго JSON без пояснений:\n"
    '{"kind": "chat" или "action", "feasible": true или false, "reason": "...", '
    '"say_first": "...", "steps": [{"tool": "id инструмента", "args": {}}]}\n'
    "say_first: очень короткая фраза (до 6 слов) от первого лица (по-русски, женский род), которую "
    "ты скажешь, пока выполняешь, например «Сейчас посмотрю» или «Хорошо, выключаю». Для kind=chat и "
    "feasible=false оставь пустую строку."
) % MAX_STEPS


class Router:
    def __init__(self, registry, say, dialogue_provider):
        self.registry = registry
        self._say = say                          # как озвучить вопрос о разрешении
        self._dialogue = dialogue_provider       # () -> короткий текст последних реплик
        self.last_kind = "chat"                  # итог последнего handle: chat / cannot / done

    @staticmethod
    def looks_like_action(text: str) -> bool:
        """Быстрая проверка без модели: похоже ли сообщение на просьбу что-то сделать."""
        return bool(_VERB_RE.search(text.lower()))

    # ── планирование ─────────────────────────────────────────────────────────

    @staticmethod
    def _parse(raw: str) -> dict | None:
        a, b = raw.find("{"), raw.rfind("}")
        try:
            plan = json.loads(raw[a:b + 1])
        except ValueError:
            return None
        if not isinstance(plan, dict) or plan.get("kind") not in ("chat", "action"):
            return None
        plan["steps"] = [s for s in (plan.get("steps") or []) if isinstance(s, dict)][:MAX_STEPS]
        return plan

    async def _plan(self, text: str, tools) -> dict | None:
        system = PLANNER.replace("{tools}", "\n".join("- " + t.signature() for t in tools))
        user = f"{self._dialogue()}\n\nПоследнее сообщение пользователя: {text}"
        for attempt in range(2):
            try:
                raw = await fast_brain._ask(system + ("" if attempt == 0 else
                                            "\nПредыдущий ответ не был валидным JSON. Ответь ТОЛЬКО валидным JSON."),
                                            user, timeout=90, max_tokens=420, temperature=0.1)
            except Exception as e:
                logger.warning(f"Планировщик недоступен: {e}")
                return None
            plan = self._parse(raw)
            if plan:
                return plan
        return None

    # ── исполнение ────────────────────────────────────────────────────────────

    async def _step(self, step: dict, candidates: dict) -> tuple[bool, str, str, str | None]:
        """(успех, название, результат/причина, код ошибки по MOD-9 или None)."""
        tool = candidates.get(str(step.get("tool", "")))
        if tool is None:
            return False, str(step.get("tool", "?")), "такого инструмента у неё нет", "unknown"
        args, err = validate_args(tool, step.get("args"))
        if err:
            return False, tool.name, f"неверные параметры: {err}", "bad_request"
        danger = tool.danger(args) if tool.danger else None
        if tool.danger_check and tool.check is not None and not danger:
            danger = await tool.check(args)       # модуль сам называет опасность (tool.check, CORE-35)
        needs_ask = bool(danger) or tool.risk == "ask" or (
            tool.risk == "public" and not tool.self_confirming and settings.load()["telegram_confirm"])
        if needs_ask:
            shown = ", ".join(f"{k}={str(v)[:60]}" for k, v in args.items())
            question = f"Хочу выполнить действие «{tool.name}» ({tool.description.rstrip('.')})"
            if shown:
                question += f" с параметрами: {shown[:200]}"
            if not await approvals.request(question + ".", self._say, danger=danger):
                return False, tool.name, "пользователь не разрешил", "denied"
        try:
            # запас над timeout_s: модульный вызов сам вернёт понятную причину тайм-аута раньше (CORE-34)
            result = await asyncio.wait_for(tool.call(args), tool.timeout_s + 2)
        except ToolError as e:
            return False, tool.name, str(e), getattr(e, "code", "unavailable")
        except asyncio.TimeoutError:
            return False, tool.name, "не уложилась по времени", "timeout"
        except Exception as e:
            logger.warning(f"Инструмент {tool.id}: {e}")
            return False, tool.name, f"сбой: {e}", "internal"
        return True, tool.name, str(result)[:RESULT_CHARS], None

    async def _run_step(self, step: dict, candidates: dict) -> tuple[bool, str, str]:
        """(успех, название, результат/причина)."""
        ok, name, info, _code = await self._step(step, candidates)
        return ok, name, info

    # ── главный вход ───────────────────────────────────────────────────────────

    async def run_tool_ex(self, tool_id: str, args: dict) -> tuple[bool, str, str | None]:
        """Как run_tool, но третьим значением возвращает код ошибки (MOD-9: unknown, bad_request, denied, unavailable,
        timeout, internal) или None при успехе: так отвечает ядро на core.tool.call от модулей."""
        tool = self.registry.get(tool_id)
        if tool is None:
            return False, "такого инструмента нет", "unknown"
        ok, _name, result, code = await self._step({"tool": tool_id, "args": args}, {tool_id: tool})
        return ok, result, code

    async def run_tool(self, tool_id: str, args: dict) -> tuple[bool, str]:
        """Выполнить один инструмент по id с обычным контролем: проверка параметров, вопрос о разрешении, опасное всегда спрашивается.
        Для других агентов (игра) через дашборд. Возвращает (успех, результат или причина)."""
        ok, result, _code = await self.run_tool_ex(tool_id, args)
        return ok, result

    async def handle(self, text: str, announce: bool = True) -> str | None:
        tools = await self.registry.search_async(text)
        if not tools:
            return None
        plan = await self._plan(text, tools)
        if plan is None:
            events_log.log("plan", "планировщик не дал разбираемого ответа, это обычный разговор")
            return None
        if plan["kind"] == "chat":
            self.last_kind = "chat"
            return None

        if plan.get("feasible") and not plan["steps"]:
            self.last_kind = "chat"      # «выполнимо», но делать нечего: это просто разговор
            return None
        if not plan.get("feasible"):
            self.last_kind = "cannot"
            reason = str(plan.get("reason") or "подходящего инструмента нет")[:200]
            events_log.log("plan", f"не могу: {text}", feasible=False, reason=reason)
            return (f"Пользователь просит: «{text}». Среди твоих инструментов подходящего нет "
                    f"(причина: {reason}). Вот что у тебя есть:\n{self.registry.describe()}\n"
                    "Честно скажи, что этого не можешь. Если предлагаешь альтернативу, называй ТОЛЬКО то, что есть "
                    "в списке выше; ничего другого не обещай (напоминаний, заказов и т.п. нет, если их нет в списке).")

        self.last_kind = "done"
        say_first = str(plan.get("say_first") or "").strip()
        announced = bool(announce and say_first)
        if announced:
            await self._say(say_first)   # пока ищет и делает, пользователь видит, что она занята
        candidates = {t.id: t for t in tools}
        # модель часто пишет имя без окружения («claude_task» вместо «desktop.claude_task»): приводим к id
        by_name: dict = {}
        for t in tools:
            by_name.setdefault(t.name, []).append(t.id)
        for st in plan["steps"]:
            name = str(st.get("tool", ""))
            if name not in candidates and len(by_name.get(name.split(".")[-1], [])) == 1:
                st["tool"] = by_name[name.split(".")[-1]][0]
        done, steps_log = [], []
        for step in plan["steps"]:
            ok, name, info = await self._run_step(step, candidates)
            done.append((ok, name, info))
            steps_log.append({"tool": name, "ok": ok, "result": info[:120]})
            if not ok:
                break   # дальше по плану идти нельзя
        events_log.log("plan", f"выполнено: {text}", feasible=True, steps=steps_log)
        # простое действие прошло, и она уже сказала «сейчас сделаю»: итог молчаливый
        if announced and all(ok for ok, _, _ in done) and not any(
                candidates[str(st.get("tool"))].informative for st in plan["steps"]
                if str(st.get("tool")) in candidates):
            return ""
        lines = [f"- {'сделано' if ok else 'НЕ получилось'}: {name} → {info}" for ok, name, info in done]
        return (f"Пользователь просил: «{text}». Ты действовала по плану:\n" + "\n".join(lines) +
                "\nСообщи пользователю итог своими словами, строго по этим результатам.")

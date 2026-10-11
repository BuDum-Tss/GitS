"""Реестр окружений: загрузка, поиск инструментов, описание для самой ассистентки."""
import asyncio
import json
import logging
import re
from pathlib import Path

import gamenet
from .base import Environment, Tool
from . import sources

logger = logging.getLogger("environments")

_WORD = re.compile(r"[a-zа-яё0-9]{3,}", re.I)


def _port_open(port: int) -> bool:
    return gamenet.port_open(int(port))   # оба адреса петли: мод слушает только IPv6


def _stems(text: str) -> set[str]:
    return {w.lower()[:5] for w in _WORD.findall(text)}


# Прежние id инструментов экрана (ядро, до ТЗ 14) → новые в модуле desktop. Модуль RimWorld вызывает их через core.tool.call.
_DESKTOP_NAMES = ("take_screenshot", "describe_screen", "read_screen", "operate_screen", "list_windows", "focus_window",
                  "click_text", "click", "type_text", "press_key", "scroll", "launch_app", "list_apps", "add_app")
LEGACY_TOOL_IDS = {f"system.{n}": f"desktop.{n}" for n in _DESKTOP_NAMES if n not in ("launch_app", "list_apps", "add_app")}
LEGACY_TOOL_IDS.update({f"assistant.{n}": f"desktop.{n}" for n in ("launch_app", "list_apps", "add_app")})
LEGACY_TOOL_IDS["system.ask_claude"] = "desktop.claude_task"
_LEGACY_WARNED: set[str] = set()


class Registry:
    def __init__(self):
        self.envs: dict[str, Environment] = {}
        self.load_errors: dict[str, str] = {}
        # () -> модули фокуса активного профиля (profile_manager); None: фокуса нет, видно всё (тесты)
        self.focus = None
        self.embedder = None   # tool_search.ToolEmbeddings: отбор по смыслу (CORE-36); None - только по основам слов

    def _focus_modules(self) -> set[str] | None:
        return None if self.focus is None else set(self.focus())

    def tool_visible(self, env: Environment, tool: Tool, focus: set[str] | None = None) -> bool:
        """Виден ли инструмент планировщику (CORE-10): always - всегда; profile - когда модуль окружения в фокусе профиля."""
        if self.focus is None or tool.availability == "always":
            return True
        return env.module in (focus if focus is not None else self._focus_modules())

    def add(self, env: Environment):
        self.envs[env.id] = env

    # ── манифесты ────────────────────────────────────────────────────────────

    async def load_manifests(self, directory: Path):
        for path in sorted(Path(directory).glob("*.json")):
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
            except ValueError as e:
                self.load_errors[path.name] = f"неверный JSON: {e}"
                continue
            await self._load_manifest(path.name, manifest)

    async def _load_manifest(self, fname: str, m: dict):
        env = Environment(id=m["id"], title=m.get("title", m["id"]),
                          description=m.get("description", ""), enabled=m.get("enabled", True),
                          module=m.get("module", "core"))
        problems = []
        if env.enabled:
            for src in m.get("sources", []):
                try:
                    if src["type"] == "mcp":
                        env.tools += await asyncio.wait_for(sources.mcp_tools(env.id, src),
                                                            sources.DISCOVER_TIMEOUT + 15)
                    elif src["type"] == "webhook_out":
                        env.tools.append(sources.webhook_tool(env.id, src))
                    else:
                        problems.append(f"неизвестный тип источника {src['type']!r}")
                except Exception as e:
                    problems.append(f"{src.get('type')}: {e}")
            if env.module != "core":
                for t in env.tools:       # окружение чужого модуля: инструменты видны только в фокусе профиля (CORE-10)
                    t.availability = "profile"
        port = m.get("requires_port")
        if port:
            env.status = lambda port=port: (_port_open(port), "приложение не запущено (порт %s закрыт)" % port)
        if problems:
            self.load_errors[fname] = "; ".join(problems)
            env.status = lambda p="; ".join(problems): (False, p)
        self.add(env)

    # ── доступ ───────────────────────────────────────────────────────────────

    def active_tools(self) -> list[Tool]:
        focus = self._focus_modules()
        return [t for e in self.envs.values() if e.state()[0] for t in e.tools if self.tool_visible(e, t, focus)]

    def get(self, tool_id: str) -> Tool | None:
        for e in self.envs.values():
            for t in e.tools:
                if t.id == tool_id:
                    return t
        target = LEGACY_TOOL_IDS.get(tool_id)                # ТЗ 14, DESK-x: прежние id экранных инструментов
        if target is not None:
            if tool_id not in _LEGACY_WARNED:
                _LEGACY_WARNED.add(tool_id)
                logger.warning(f"инструмент «{tool_id}» переехал в модуль desktop: используется «{target}»")
            return self.get(target)
        return None

    def search(self, text: str, k: int = 14) -> list[Tool]:
        """Подходящие инструменты по просьбе. Если инструментов немного, возвращаются все:
        планировщик сам решит. Иначе отбор по пересечению основ слов."""
        tools = self.active_tools()
        if len(tools) <= k:
            return tools
        query = _stems(text)
        scored = []
        for t in tools:
            doc = _stems(f"{t.name} {t.description} {' '.join(t.keywords)} "
                         f"{self.envs[t.env].title} {self.envs[t.env].description}")
            scored.append((len(query & doc), t))
        scored.sort(key=lambda x: -x[0])
        picked = [t for _, t in scored[:k]]
        picked += [t for t in tools if t.always and t not in picked]   # запасной путь не должен отсеиваться
        return picked

    async def search_async(self, text: str, k: int = 14) -> list[Tool]:
        """Как search, но сначала по смыслу (эмбеддинги описаний); недоступны или кэш ещё не собран - по основам слов."""
        tools = self.active_tools()
        if len(tools) <= k or self.embedder is None:
            return self.search(text, k)
        picked = await self.embedder.rank(text, tools, self.envs, k)
        if picked is None:
            return self.search(text, k)
        return picked + [t for t in tools if t.always and t not in picked]    # запасной путь не должен отсеиваться

    # ── описание ──────────────────────────────────────────────────────────────

    def describe(self) -> str:
        """Честный перечень окружений и их состояния, для ответа «что ты умеешь / не могу»."""
        lines = []
        focus = self._focus_modules()
        for e in self.envs.values():
            ok, why = e.state()
            shown = [t for t in e.tools if self.tool_visible(e, t, focus)]
            if ok and e.tools and not shown:
                ok, why = False, "не в фокусе текущего профиля (включится, когда начнётся нужное занятие)"
            names = ", ".join(t.name for t in (shown if ok else e.tools)[:8]) or "инструментов нет"
            lines.append(f"- {e.title}: {'доступно' if ok else 'недоступно (' + why + ')'}. {e.description} "
                         f"Умеет: {names}.")
        return "\n".join(lines)

    def snapshot(self) -> list[dict]:
        """Для дашборда."""
        out = []
        focus = self._focus_modules()
        for e in self.envs.values():
            ok, why = e.state()
            in_mode = not e.tools or any(self.tool_visible(e, t, focus) for t in e.tools)
            if not in_mode:
                ok, why = False, "не в фокусе текущего профиля"
            out.append({"module": e.module, "in_mode": in_mode, "id": e.id, "title": e.title, "description": e.description,
                        "enabled": e.enabled, "ok": ok, "status": why,
                        "tools": [{"id": t.id, "name": t.name, "description": t.description, "risk": t.risk,
                                   "availability": t.availability, "visible": self.tool_visible(e, t, focus)}
                                  for t in e.tools]})
        return out

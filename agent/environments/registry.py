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


class Registry:
    def __init__(self):
        self.envs: dict[str, Environment] = {}
        self.load_errors: dict[str, str] = {}
        self.gate = None   # (модуль) -> включён ли в текущем режиме; None: всё включено

    def _in_mode(self, env: Environment) -> bool:
        return self.gate is None or self.gate(env.module)

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
        port = m.get("requires_port")
        if port:
            env.status = lambda port=port: (_port_open(port), "приложение не запущено (порт %s закрыт)" % port)
        if problems:
            self.load_errors[fname] = "; ".join(problems)
            env.status = lambda p="; ".join(problems): (False, p)
        self.add(env)

    # ── доступ ───────────────────────────────────────────────────────────────

    def active_tools(self) -> list[Tool]:
        return [t for e in self.envs.values() if self._in_mode(e) and e.state()[0] for t in e.tools]

    def get(self, tool_id: str) -> Tool | None:
        for e in self.envs.values():
            for t in e.tools:
                if t.id == tool_id:
                    return t
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

    # ── описание ──────────────────────────────────────────────────────────────

    def describe(self) -> str:
        """Честный перечень окружений и их состояния, для ответа «что ты умеешь / не могу»."""
        lines = []
        for e in self.envs.values():
            ok, why = e.state()
            if not self._in_mode(e):
                ok, why = False, "не включено в текущем режиме (включится, когда начнётся игра или нужная среда)"
            names = ", ".join(t.name for t in e.tools[:8]) or "инструментов нет"
            lines.append(f"- {e.title}: {'доступно' if ok else 'недоступно (' + why + ')'}. {e.description} "
                         f"Умеет: {names}.")
        return "\n".join(lines)

    def snapshot(self) -> list[dict]:
        """Для дашборда."""
        out = []
        for e in self.envs.values():
            ok, why = e.state()
            in_mode = self._in_mode(e)
            if not in_mode:
                ok, why = False, "не включено в текущем режиме"
            out.append({"module": e.module, "in_mode": in_mode, "id": e.id, "title": e.title, "description": e.description,
                        "enabled": e.enabled, "ok": ok, "status": why,
                        "tools": [{"id": t.id, "name": t.name, "description": t.description,
                                   "risk": t.risk} for t in e.tools]})
        return out

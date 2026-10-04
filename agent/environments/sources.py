"""Источники инструментов окружения: MCP-серверы и исходящие вебхуки.

Описываются декларативно в манифестах (environments/manifests/*.json), без кода.

Безопасность:
  - секреты в манифесте только по имени: {TELEGRAM_BOT_TOKEN}; значения берутся из
    переменных окружения (agent/secrets.env) и нигде не логируются;
  - внешнему MCP-серверу передаётся МИНИМАЛЬНОЕ окружение (безопасный набор из mcp)
    плюс только те переменные, что явно перечислены в манифесте;
  - описания инструментов из чужих MCP-серверов - недоверенный текст: они попадают
    в подсказку планировщику только как описание вызова, а не как указания;
  - риск по умолчанию у внешних инструментов: ask (с разрешением).
"""
import asyncio
import fnmatch
import logging
import os
import re
import sys
from pathlib import Path

import httpx

from .base import Tool

logger = logging.getLogger("environments")

_SUBST = re.compile(r"\{(\w+)\}")
DISCOVER_TIMEOUT = 30
CALL_TIMEOUT = 120


class SourceError(Exception):
    pass


def _subst(template, params: dict):
    """{имя} -> значение параметра, иначе переменной окружения. Структуры обходятся рекурсивно."""
    if isinstance(template, dict):
        return {k: _subst(v, params) for k, v in template.items()}
    if isinstance(template, list):
        return [_subst(v, params) for v in template]
    if not isinstance(template, str):
        return template

    def repl(m):
        name = m.group(1)
        if name in params:
            return str(params[name])
        if name in os.environ:
            return os.environ[name]
        raise SourceError(f"не задана переменная {name}")
    return _SUBST.sub(repl, template)


def _pick_risk(name: str, src: dict) -> str:
    for pattern, risk in (src.get("risk_overrides") or {}).items():
        if fnmatch.fnmatch(name, pattern):
            return risk
    return src.get("risk_default", "ask")


# ── MCP ───────────────────────────────────────────────────────────────────────

def _server_params(src: dict):
    from mcp import StdioServerParameters
    from mcp.client.stdio import get_default_environment
    env = get_default_environment()
    for key, value in (src.get("env") or {}).items():
        env[key] = _subst(value, {})
    command = sys.executable if src["command"] in ("python", "python3") else src["command"]
    return StdioServerParameters(command=command, args=_subst(src.get("args", []), {}), env=env,
                                 cwd=str(Path(__file__).resolve().parent.parent))   # папка agent/


def _schema_to_params(schema: dict) -> dict:
    required = set((schema or {}).get("required", []))
    out = {}
    for name, p in ((schema or {}).get("properties") or {}).items():
        item = {"type": p.get("type", "string") if isinstance(p.get("type"), str) else "string",
                "description": str(p.get("description", p.get("title", "")))[:160],
                "required": name in required}
        if p.get("enum"):
            item["enum"] = p["enum"]
        out[name] = item
    return out


async def mcp_tools(env_id: str, src: dict) -> list[Tool]:
    """Подключиться к MCP-серверу, получить список инструментов (подключение закрывается)."""
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client
    params = _server_params(src)
    tools = []
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), DISCOVER_TIMEOUT)
            listed = await asyncio.wait_for(session.list_tools(), DISCOVER_TIMEOUT)
    for t in listed.tools:
        tool_name = t.name
        if any(fnmatch.fnmatch(tool_name, pat) for pat in src.get("exclude", [])):
            continue   # инструмент принадлежит другому окружению (например, ввод мышью и клавиатурой)

        async def call(args: dict, _n=tool_name):
            from mcp import ClientSession as CS
            from mcp.client.stdio import stdio_client as sc
            async with sc(_server_params(src)) as (r, w):
                async with CS(r, w) as s:
                    await s.initialize()
                    res = await asyncio.wait_for(s.call_tool(_n, args), CALL_TIMEOUT)
            text = " ".join(getattr(c, "text", "") for c in res.content).strip()
            if res.isError:
                raise SourceError(text or "инструмент вернул ошибку")
            return text[:2000]

        tools.append(Tool(
            id=f"{env_id}.{tool_name}", env=env_id, name=tool_name,
            description=(t.description or tool_name).strip().split("\n")[0][:200],
            params=_schema_to_params(t.inputSchema), risk=_pick_risk(tool_name, src),
            keywords=tuple(src.get("keywords", ())), call=call))
    return tools


# ── исходящий вебхук ────────────────────────────────────────────────────────────

def webhook_tool(env_id: str, src: dict) -> Tool:
    async def call(args: dict) -> str:
        url = _subst(src["url"], args)
        headers = _subst(src.get("headers", {}), args)
        body = _subst(src.get("body", {}), args)
        method = src.get("method", "POST").upper()
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.request(method, url, headers=headers,
                                json=body if method != "GET" else None,
                                params=body if method == "GET" else None)
        if r.status_code >= 400:
            raise SourceError(f"ответ {r.status_code}")   # тело и URL не показываем: там могут быть секреты
        return f"ответ {r.status_code}"

    return Tool(id=f"{env_id}.{src['name']}", env=env_id, name=src["name"],
                description=src.get("description", src["name"]), params=src.get("params", {}),
                risk=src.get("risk", "ask"), keywords=tuple(src.get("keywords", ())), call=call)

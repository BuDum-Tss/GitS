"""Инструменты ядра для голосовых каналов: `voice.say`, `voice.sessions`, `voice.stop`, `voice.leave`; `discord.say` это явная дорожка речи в Discord (то же, что `voice.say`, но только по каналам адаптера discord).

Сказанное участником голосового канала приходит агенту событием `voicechat.utterance`; ответ это вызов `voice.say`, а не
реплика диалога. Инструмент только ставит реплику в очередь и сразу возвращает uid: о конце речи ядро узнаёт отдельным событием,
вызов не ждёт синтеза и звука.
"""
import asyncio

from environments.base import Environment, Tool, ToolError

from .engine import VoiceEngine
from .protocol import encode
from .session import VoiceSession

ENV_ID = "voicechat"
LEAVE_TIMEOUT_S = 3.0
DISCORD_ADAPTER = "discord"


def _pick(engine: VoiceEngine, session: str | None, adapter: str | None = None) -> VoiceSession:
    """Сессия по id; без id единственная открытая (при `adapter` среди каналов этого адаптера)."""
    if session:
        found = engine.session(session)
        if found is None or (adapter and found.adapter != adapter):
            raise ToolError(f"голосового канала «{session}» нет; открытые: {_ids(engine, adapter)}", "bad_request")
        return found
    sessions = [s for s in engine.sessions_manager.all() if not adapter or s.adapter == adapter]
    if not sessions:
        raise ToolError(f"сейчас нет ни одного открытого голосового канала{' ' + adapter if adapter else ''}", "unavailable")
    if len(sessions) > 1:
        raise ToolError(f"открыто несколько голосовых каналов, укажи session: {_ids(engine, adapter)}", "bad_request")
    return sessions[0]


def _ids(engine: VoiceEngine, adapter: str | None = None) -> str:
    return ", ".join(s.id for s in engine.sessions_manager.all() if not adapter or s.adapter == adapter) or "нет"


def build_tools(engine: VoiceEngine) -> list[Tool]:
    async def say(a: dict, adapter: str | None = None) -> str:
        sess = _pick(engine, a.get("session"), adapter)
        try:
            ticket = engine.speak(sess.id, str(a["text"]), replace=bool(a.get("replace")))
        except ValueError as e:
            raise ToolError(str(e), "bad_request") from None
        where = "звучит сразу" if ticket.position == 0 else f"в очереди, перед ней реплик: {ticket.position}"
        return f"поставлено в голосовой канал «{sess.id}» ({where}), uid {ticket.uid}; когда договорит, придёт событие"

    async def discord_say(a: dict) -> str:
        return await say(a, DISCORD_ADAPTER)

    async def sessions(a: dict) -> str:
        rows = engine.sessions()
        if not rows:
            return "открытых голосовых каналов нет"
        lines = []
        for s in rows:
            who = ", ".join(sp["name"] + (" (владелец)" if sp["is_owner"] else "") for sp in s["speakers"]) or "никого"
            state = "говорит" if s["speaking"] else "молчит"
            name = f"«{s['title']}» ({s['id']}, {s['adapter']})" if s.get("title") else f"{s['id']} ({s['adapter']})"
            lines.append(f"- {name}: сейчас {state}, в очереди {s['queue']}; в канале: {who}; "
                         f"перебивание: {s['barge_in']}")
        return "Голосовые каналы:\n" + "\n".join(lines)

    async def stop(a: dict) -> str:
        sess = _pick(engine, a.get("session"))
        engine.stop(sess.id, "now", None, "stop")
        return f"речь в канале «{sess.id}» остановлена, очередь очищена"

    async def leave(a: dict) -> str:
        sess = _pick(engine, a.get("session"))
        engine.stop(sess.id, "now", None, "stop")
        try:
            await asyncio.wait_for(sess.transport.send_control(encode("leave", reason="requested")), LEAVE_TIMEOUT_S)
        except (ConnectionError, RuntimeError, asyncio.TimeoutError):
            raise ToolError("адаптер не отвечает, выйти из канала не удалось", "unavailable") from None
        return f"попросила адаптер выйти из канала «{sess.id}»"

    async def transcript(a: dict) -> str:
        sess = _pick(engine, a.get("session"))
        rows = engine.transcript(sess.id, int(a.get("n") or 10))
        if not rows:
            return f"в канале «{sess.id}» пока никто ничего не говорил"
        now = engine.clock.time()
        lines = [f"- {int(now - r.ts)} с назад, {'ты' if r.own else r.name}: {r.text}" for r in rows]
        return f"Последние фразы канала «{sess.id}»:\n" + "\n".join(lines)

    session_param = {"type": "string", "required": False, "description": "id голосового канала; можно опустить, если он один"}
    return [
        Tool(id="voice.say", env=ENV_ID, name="say",
             description="Сказать вслух в голосовом канале (Discord, колонки): реплика ставится в очередь и озвучивается, вызов "
                         "не ждёт конца речи. Так отвечают на то, что сказали в голосовом канале.",
             params={"text": {"type": "string", "description": "что сказать, живой устной речью, коротко"},
                     "session": session_param,
                     "replace": {"type": "boolean", "required": False,
                                 "description": "true: оборвать звучащее и очередь и сказать это сразу"}},
             risk="auto", call=say, keywords=("скажи", "ответь", "голосом", "вслух", "войс"), availability="always"),
        Tool(id="discord.say", env=ENV_ID, name="say",
             description="Сказать вслух в голосовом канале Discord (явная дорожка речи): реплика ставится в очередь, вызов не ждёт "
                         "конца речи; итог придёт фоном. Канал можно опустить, если открыт один.",
             params={"text": {"type": "string", "description": "что сказать, живой устной речью, коротко"},
                     "session": session_param,
                     "replace": {"type": "boolean", "required": False,
                                 "description": "true: оборвать звучащее и очередь и сказать это сразу"}},
             risk="auto", call=discord_say, keywords=("скажи", "ответь", "голосом", "вслух", "дискорд", "войс"), availability="always"),
        Tool(id="voice.sessions", env=ENV_ID, name="sessions",
             description="Какие голосовые каналы открыты сейчас и кто в них: id, адаптер, говорит ли она, участники.",
             params={}, risk="auto", call=sessions, informative=True, read_only=True, keywords=("голосовой", "канал", "кто в канале", "войс"),
             availability="always"),
        Tool(id="voice.transcript", env=ENV_ID, name="transcript",
             description="Последние фразы голосового канала: что говорили люди (в том числе не тебе) и ты сама.",
             params={"session": session_param,
                     "n": {"type": "integer", "required": False, "description": "сколько фраз, по умолчанию 10"}},
             risk="auto", call=transcript, informative=True, read_only=True, keywords=("о чём говорили", "что говорили", "в канале", "расшифровка"),
             availability="always"),
        Tool(id="voice.stop", env=ENV_ID, name="stop",
             description="Замолчать в голосовом канале: оборвать речь и очистить очередь реплик.",
             params={"session": session_param}, risk="auto", call=stop, keywords=("замолчи", "хватит", "тихо"),
             availability="always"),
        Tool(id="voice.leave", env=ENV_ID, name="leave",
             description="Выйти из голосового канала: адаптер отключается от канала, сессия закрывается.",
             params={"session": session_param}, risk="auto", call=leave, keywords=("выйди", "уйди из канала", "отключись"),
             availability="always"),
    ]


def register_voice_tools(registry, engine: VoiceEngine) -> Environment:
    """Добавить окружение с инструментами в реестр ядра (окружение `module=core`: видно в любой среде)."""
    env = Environment(id=ENV_ID, title="Голосовые каналы",
                      description="Голосовые каналы (Discord, колонки ПК): сказать вслух, кто в канале, замолчать, выйти. "
                                  "Сказанное участниками приходит событиями voicechat.utterance.",
                      tools=build_tools(engine))
    registry.add(env)
    return env

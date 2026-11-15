"""Чужие сообщения из каналов модулей в чате дашборда подписаны именем собеседника, а не «Ты» (только показ).

Роль в диалоге остаётся `user` (память, эпизоды и контекст агента не меняются), в запись добавляется `who`."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from services.conversation.companion import Companion
from services.conversation.conversation_io import ConversationIO
from services.conversation.service import _channel_sender_label
from services.conversation.conversation_io import Channel

HTML = (Path(__file__).resolve().parents[1] / "agent" / "clients" / "gui" / "index.html").read_text(encoding="utf-8")
NODE = shutil.which("node")
DC = Channel("module", "discord", "channel:7")


def test_label_for_any_module_channel_sender_owner_included():
    assert _channel_sender_label({"id": "d:5", "name": "NektoKtoto", "is_owner": False}, DC) == "NektoKtoto · Discord"
    assert _channel_sender_label({"id": "d:1", "name": "BaDumTss", "is_owner": True}, DC) == "BaDumTss · Discord"    # владелец тоже
    assert _channel_sender_label({"id": "d:5"}, DC) == "d:5 · Discord"
    assert _channel_sender_label(None, DC) == "Discord" and _channel_sender_label({"name": ""}, DC) == "Discord"
    assert _channel_sender_label({"name": "X"}, Channel("dashboard")) is None           # «Ты» только у дашборда
    assert _channel_sender_label({"name": "X"}, Channel("voice")) is None               # голос не меняется


async def test_dialogue_and_journal_carry_who_but_role_and_text_stay(tmp_path):
    c = Companion(str(tmp_path / "memory"), rel=None)
    logged = []
    c._log = type("L", (), {"log_nowait": staticmethod(lambda kind, text="", **d: logged.append((kind, text, d))),
                            "log": staticmethod(lambda *a, **k: None)})()

    async def stop_after_write(text, hard, writes, sal, reasons, who=None):
        c._log.log_nowait("user", text, salience=sal, reasons=reasons, **({"who": who} if who else {}))
        await writes
    c.conversation._handle = stop_after_write
    io = ConversationIO(c)
    await io.from_module("привет", {"module": "discord", "target": "channel:7"}, {"id": "d:5", "name": "NektoKtoto", "is_owner": False})
    await io.from_module("и я", {"module": "discord", "target": "channel:7"}, {"id": "d:1", "name": "Аня", "is_owner": True})
    first, second = [m for m in c._mem.recent_dialogue(5) if m["role"] == "user"]
    assert first["who"] == "NektoKtoto · Discord" and first["text"] == "привет"        # роль и текст прежние
    assert second["who"] == "Аня · Discord" and second["text"] == "и я"                 # владелец из канала тоже подписан
    assert logged[0][2]["who"] == "NektoKtoto · Discord" and logged[1][2]["who"] == "Аня · Discord"
    await c.on_user_message("из дашборда")                                              # дашборд: подписи нет, «Ты»
    assert "who" not in [m for m in c._mem.recent_dialogue(5) if m["role"] == "user"][-1]


@pytest.mark.skipif(NODE is None, reason="нет node: рендер дашборда проверить нечем")
def test_chat_renders_other_sender_as_incoming_message():
    esc = re.search(r"^const esc = .*$", HTML, re.M).group(0)
    region = HTML[HTML.index("function renderChat("):HTML.index("const CTX_KINDS")]
    dialogue = [{"role": "user", "text": "привет", "ts": 1}, {"role": "user", "text": "от них <b>", "ts": 2, "who": "NektoKtoto · Discord"},
                {"role": "glados", "text": "ответ", "ts": 3}]
    script = (f"{esc}\nlet chatMsgs = [], stick = false; const el = {{dataset: {{}}, innerHTML: '', scrollTop: 0, scrollHeight: 0}};\n"
              f"const $ = id => el;\n{region}\nrenderChat({json.dumps(dialogue, ensure_ascii=False)}, false, []);\n"
              "process.stdout.write(el.innerHTML);")
    done = subprocess.run([NODE, "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert done.returncode == 0, done.stderr
    html = done.stdout
    bubbles = html.split('<div class="chat ')[1:]
    assert 'chat-end' in bubbles[0] and ">Ты " in bubbles[0] and "chat-bubble-neutral" in bubbles[0]          # владелец как раньше
    assert bubbles[1].startswith("chat-start") and "NektoKtoto · Discord" in bubbles[1]                      # чужой: входящий, с именем
    assert "chat-bubble-neutral" not in bubbles[1] and "&lt;b&gt;" in bubbles[1] and "контекст" not in bubbles[1]
    assert "Ассистент" in bubbles[2]

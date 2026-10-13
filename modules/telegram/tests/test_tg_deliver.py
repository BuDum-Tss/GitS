"""Доставка ответов ядра в Telegram: deliver (ТЗ 10: TG-17..TG-20)."""
from pathlib import Path

import pytest
from tg_rig import Rig

from gits_sdk import ToolError

OWNER = 777


async def deliver(rig, **body):
    return await rig.core.request("deliver", body)


async def test_deliver_to_bot_chat_sends_text_from_the_bot(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        assert await deliver(rig, target="bot:555", text="Всё под контролем.") == {"delivered": True}
        sends = rig.api_calls("sendMessage")
    assert sends == [{"chat_id": 555, "text": "Всё под контролем."}]
    assert rig.tl.sent == []                                                  # аккаунт не использовался


async def test_deliver_to_user_chat_sends_from_her_account(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True) as rig:
        assert await deliver(rig, target="user:-100", text="Отвечаю в группе.") == {"delivered": True}
    assert rig.tl.sent == [(("entity", -100), "Отвечаю в группе.")]
    assert rig.api_calls("sendMessage") == []


async def test_deliver_user_target_without_session_is_unavailable(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        with pytest.raises(ToolError) as e:
            await deliver(rig, target="user:5", text="привет")
    assert e.value.code == "unavailable"


async def test_owner_target_goes_to_the_owners_private_bot_chat(tmp_path, monkeypatch):
    """TG-19: вопросы ядра, когда пользователь отошёл, приходят с target «owner»."""
    async with Rig(tmp_path, monkeypatch, module_settings={"owner_tg_id": OWNER}) as rig:
        await deliver(rig, target="owner", text="Можно удалить черновые рендеры?")
        sends = rig.api_calls("sendMessage")
    assert sends == [{"chat_id": OWNER, "text": "Можно удалить черновые рендеры?"}]


async def test_owner_target_without_owner_id_is_unavailable(tmp_path, monkeypatch):
    """TG-20."""
    async with Rig(tmp_path, monkeypatch) as rig:
        with pytest.raises(ToolError) as e:
            await deliver(rig, target="owner", text="вопрос")
        assert rig.api_calls("sendMessage") == []
    assert e.value.code == "unavailable" and e.value.message == "Telegram-id владельца не задан"


async def test_voice_path_sends_voice_with_caption_when_enabled(tmp_path, monkeypatch):
    voice = tmp_path / "ответ.ogg"
    voice.write_bytes(b"OggS-reply")
    async with Rig(tmp_path, monkeypatch, settings={"telegram_voice": True}) as rig:
        await deliver(rig, target="bot:555", text="Голосом.", voice_path=str(voice))
        uploads = rig.uploads
    assert [(m, d["chat_id"], d["caption"], f["voice"][1]) for m, d, f in uploads] == \
        [("sendVoice", 555, "Голосом.", b"OggS-reply")]
    assert rig.api_calls("sendMessage") == []                                 # текст ушёл подписью


async def test_voice_path_is_ignored_without_telegram_voice(tmp_path, monkeypatch):
    voice = tmp_path / "ответ.ogg"
    voice.write_bytes(b"OggS-reply")
    async with Rig(tmp_path, monkeypatch, settings={"telegram_voice": False}) as rig:
        await deliver(rig, target="bot:555", text="Текстом.", voice_path=str(voice))
    assert rig.uploads == [] and rig.api_calls("sendMessage") == [{"chat_id": 555, "text": "Текстом."}]


async def test_wav_voice_is_not_converted_here_and_goes_as_text(tmp_path, monkeypatch):
    """ТЗ 12: перекодирования в OGG в модуле Telegram больше нет (его делает модуль голоса, core.tts вернёт OGG)."""
    wav = tmp_path / "ответ.wav"
    wav.write_bytes(b"RIFF")
    async with Rig(tmp_path, monkeypatch, settings={"telegram_voice": True}) as rig:
        await deliver(rig, target="bot:555", text="Из wav.", voice_path=str(wav))
    assert rig.uploads == [] and rig.api_calls("sendMessage") == [{"chat_id": 555, "text": "Из wav."}]
    assert not (Path(__file__).resolve().parent.parent / "opus_encoder.py").exists()


async def test_voice_failure_falls_back_to_text(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, settings={"telegram_voice": True}) as rig:
        await deliver(rig, target="bot:555", text="Всё равно скажу.", voice_path=str(tmp_path / "нет-файла.ogg"))
    assert rig.uploads == [] and rig.api_calls("sendMessage") == [{"chat_id": 555, "text": "Всё равно скажу."}]


async def test_voice_to_her_account_uses_voice_note(tmp_path, monkeypatch):
    voice = tmp_path / "в.ogg"
    voice.write_bytes(b"OggS-x")
    async with Rig(tmp_path, monkeypatch, telethon=True, settings={"telegram_voice": True}) as rig:
        await deliver(rig, target="user:5", text="Привет.", voice_path=str(voice))
    f = rig.tl.files[0]
    assert (f["voice_note"], f["caption"], f["data"]) == (True, "Привет.", b"OggS-x") and rig.tl.sent == []


async def test_long_reply_is_split_into_messages(tmp_path, monkeypatch):
    text = ("слово " * 1500).strip()                                           # 8999 символов
    async with Rig(tmp_path, monkeypatch) as rig:
        await deliver(rig, target="bot:5", text=text)
        parts = [p["text"] for p in rig.api_calls("sendMessage")]
    assert len(parts) == 3 and all(len(p) <= 4096 for p in parts) and " ".join(parts).split() == text.split()


async def test_bad_targets_and_bodies(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        for body in ({"target": "tg:123", "text": "x"}, {"target": "bot:abc", "text": "x"}, {"target": "bot:1", "text": " "},
                     {"text": "x"}):
            with pytest.raises(ToolError) as e:
                await deliver(rig, **body)
            assert e.value.code == "bad_request"


async def test_bot_api_error_becomes_unavailable_without_token(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        async def broken(method, _timeout=15, **params):
            raise RuntimeError("Forbidden: bot was blocked https://api.telegram.org/bot123:SECRET-TOKEN/sendMessage")
        rig.mod.channel._call = broken
        with pytest.raises(ToolError) as e:
            await deliver(rig, target="bot:5", text="привет")
    assert e.value.code == "unavailable" and "SECRET-TOKEN" not in e.value.message


async def test_deliver_without_bot_token_is_unavailable(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, token=None) as rig:
        with pytest.raises(ToolError) as e:
            await deliver(rig, target="bot:5", text="привет")
    assert e.value.code == "unavailable"

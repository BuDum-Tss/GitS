"""Companion разрезан на компоненты agent/app (ТЗ 29, ARC-5): фасад сохраняет интерфейс, компоненты работают на заглушках."""
from types import SimpleNamespace
from companion import Companion


class Speech:
    def __init__(self):
        self.spoken = []

    async def speak(self, utterance_id, text, emotion, priority):
        self.spoken.append(text)

    async def stop(self, utterance_id=None):
        pass

    def is_speaking(self):
        return False

    def current_segment(self):
        return ""


class ScriptedLLM:
    """LanguageModel-заглушка: отвечает по очереди заготовленными строками."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    async def ask(self, system, user, **kw):
        self.calls.append((system, user))
        return self.answers.pop(0) if self.answers else ""


def make(tmp_path, *answers):
    llm = ScriptedLLM(*answers)
    c = Companion(str(tmp_path / "memory"), rel=None, speech=Speech(), llm=llm)
    c.graph = SimpleNamespace(remember=lambda text, about, importance, subject="": True, status_text=lambda: "граф")      # профиль в графе (запись без модели)
    return c, llm


def test_facade_keeps_components_and_public_interface(tmp_path):
    c, _ = make(tmp_path)
    for name in ("conversation", "context_builder", "reply_generator", "memory_capture"):
        assert getattr(c, name) is not None
    for name in ("on_voice", "on_user_message", "on_input", "say", "remember_text", "speak_about",
                 "run_claude", "note_heard", "recent_heard", "rebind_memory", "is_speaking"):
        assert callable(getattr(c, name))
    for name in ("_mem", "_rel", "graph", "router", "attention", "presence", "speech", "io", "game_order"):
        assert hasattr(c, name)


def test_context_builder_joins_blocks(tmp_path):
    c, _ = make(tmp_path)
    c._graph_block = "ГРАФ"
    c._self_block = "СЕБЯ"
    c.note_heard("шум")
    text = c.context_builder.build(query="x")
    assert "ГРАФ" in text and "СЕБЯ" in text
    assert "Справка о твоих возможностях" in text
    assert c._context(query="x") == text


async def test_reply_generator_converse_says_declutted_reply(tmp_path):
    c, llm = make(tmp_path, "Готово. Хочешь, чтобы я ещё что-нибудь сделала?")
    flagged = await c._converse("привет")
    assert flagged is False
    assert c.speech.spoken == ["Готово."]


async def test_reply_generator_marks_action_request(tmp_path):
    c, _ = make(tmp_path, "[ДЕЙСТВИЕ] Сейчас посмотрю.")
    assert await c._converse("включи свет") is True
    assert c.speech.spoken == ["Сейчас посмотрю."]


async def test_memory_capture_remember_text_stores_parsed_fact(tmp_path):
    c, _ = make(tmp_path, '{"memories": [{"kind": "user", "text": "Любимый цвет пользователя - зелёный", "importance": 4}]}')
    stored = await c.remember_text("запомни: люблю зелёный")
    assert stored == ["Любимый цвет пользователя - зелёный"]


async def test_memory_capture_remember_text_falls_back_to_raw_text(tmp_path):
    c, _ = make(tmp_path, "не json")
    assert await c.remember_text("запомни это дело") == ["запомни это дело"]


async def test_no_timer_driven_chatter_left_on_the_facade(tmp_path):
    """ТЗ 30: опроса «стоит ли заговорить» у компаньона больше нет, остались только импульсы."""
    c, llm = make(tmp_path)
    assert not any(hasattr(c, n) for n in ("proactive_loop", "_maybe_speak", "_digest", "proactive_legacy"))
    assert c._repeats_recent("совсем новая тема про погоду на неделе") is False and llm.calls == []

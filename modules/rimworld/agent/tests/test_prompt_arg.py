import glados_agent as g


def test_short_prompt_is_inline_and_long_goes_to_file(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_DIR", str(tmp_path))
    assert g.prompt_arg("коротко") == "коротко"
    long = "я" * (g.PROMPT_INLINE_MAX + 1)
    arg = g.prompt_arg(long, "t")
    assert arg["type"] == "file" and open(arg["path"], encoding="utf-8").read() == long

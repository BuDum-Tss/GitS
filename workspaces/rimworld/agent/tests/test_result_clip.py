"""Ответы инструментов MCP-сервера обрезаются: огромный результат раздувает контекст хода («Prompt is too long»)."""
import asyncio

import rimworld_mcp


def test_short_result_is_untouched_and_long_one_is_cut_with_a_note():
    assert rimworld_mcp.clip_result("коротко") == "коротко"
    long = "x" * (rimworld_mcp.RESULT_MAX_CHARS + 500)
    out = rimworld_mcp.clip_result(long)
    assert out.startswith("x" * rimworld_mcp.RESULT_MAX_CHARS) and "ответ обрезан" in out and str(len(long)) in out
    assert len(out) < len(long)


def test_registered_tools_keep_their_names_and_schemas():
    tools = asyncio.run(rimworld_mcp.mcp.list_tools())
    by_name = {t.name: t for t in tools}
    assert "get_colony_state" in by_name and "survey_map" in by_name
    assert "set_task" in by_name and "description" in by_name["set_task"].inputSchema["properties"]

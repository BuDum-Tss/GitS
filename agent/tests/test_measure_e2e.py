"""Сквозной замер на заглушках (ТЗ 24, TRC-7): настоящее ядро, модель и речь подменены; у каждой реплики есть метки по порядку,
сводка считает p50 и p95, а журнал получает по одной записи trace на трассу. Внешнего ничего не запускается."""
from tools.measure_e2e import run_stub, to_markdown


async def test_stub_run_writes_one_trace_per_turn_with_percentiles(tmp_path):
    rep = await run_stub(3, str(tmp_path / "memory"))
    assert rep["mode"] == "stub"
    assert rep["traces"] == 6                                   # 3 чата и 3 голоса
    assert rep["by_source"] == {"dashboard": 3, "voice": 3}
    for stage in ("core_in", "route", "retrieve_done", "llm_done", "first_sentence_ready"):
        assert stage in rep["at"], stage
    assert rep["steps"]["llm_done"]["n"] >= 3
    assert rep["steps"]["llm_done"]["p50"] is not None and rep["steps"]["llm_done"]["p95"] >= rep["steps"]["llm_done"]["p50"]
    assert rep["end_to_sound"]["n"] == 3                         # у голоса есть начало звука, у чата нет
    md = to_markdown(rep)
    assert "| до звука |" in md and "p95" in md


async def test_stub_run_does_not_leave_patches_behind(tmp_path):
    import fast_brain
    before = fast_brain._ask
    await run_stub(1, str(tmp_path / "memory"))
    assert fast_brain._ask is before

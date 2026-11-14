import json

import pytest

from environments.base import ToolError
from environments.bug_report import repair_status, report_bug


async def test_bug_report_queues_structured_evidence(tmp_path, monkeypatch):
    queue = tmp_path / "queue"
    queue.mkdir()
    monkeypatch.setenv("GITS_REPAIR_QUEUE", str(queue))
    monkeypatch.setenv("GITS_REPAIR_SCREENSHOTS", str(tmp_path))
    name = "shot_20261114_220001_screen.png"
    (tmp_path / name).write_bytes(b"evidence")
    report = {"summary": "Сбой", "steps": "Открыть монтаж", "expected": "Редактор", "actual": "Ошибка"}
    with pytest.raises(ToolError):
        await report_bug({**report, "screenshot": "../" + name})
    assert not list(queue.iterdir())
    result = await report_bug({**report, "screenshot": name})
    request_id = result["request_id"]
    assert json.loads((queue / f"{request_id}.json").read_text(encoding="utf-8")) == {
        **report, "attachment": f"{request_id}.png",
    }
    assert (queue / f"{request_id}.png").read_bytes() == b"evidence"
    status_args = {"request_id": request_id}
    assert await repair_status(status_args) == {"state": "queued"}
    issue = "https://github.com/example/repo/issues/1"
    (queue / f"{request_id}.issue").write_text(issue, encoding="utf-8")
    (queue / f"{request_id}.result").write_text("Deployed", encoding="utf-8")
    assert await repair_status(status_args) == {"state": "finished", "issue": issue, "result": "Deployed"}


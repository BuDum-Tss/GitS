"""Очередь самоисправлений. Её читает исполнитель на хосте."""
import asyncio
import json
import os
import re
from pathlib import Path
from uuid import uuid4

from .base import ToolError


def enqueue(queue: Path, report: dict, shot_name: str = "") -> str:
    request_id = uuid4().hex
    if shot_name:
        source = Path(os.environ["GITS_REPAIR_SCREENSHOTS"]) / shot_name
        if source.resolve().parent != source.parent.resolve():
            raise ToolError("Снимок должен находиться в папке скриншотов.", code="bad_request")
        with source.open("rb") as image:
            content = image.read(8 * 1024 * 1024 + 1)
        if len(content) > 8 * 1024 * 1024:
            raise ToolError("Снимок превышает 8 МиБ.", code="bad_request")
        attachment = f"{request_id}{source.suffix}"
        (queue / attachment).write_bytes(content)
        report["attachment"] = attachment
    temporary = queue / f"{request_id}.tmp"
    temporary.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    temporary.replace(queue / f"{request_id}.json")
    return request_id


async def report_bug(args):
    report = {key: args[key].strip() for key in ("summary", "steps", "expected", "actual")}
    if any(not value or len(value) > 4000 for value in report.values()):
        raise ToolError("Заполни все поля репорта. Каждое поле допускает до 4000 символов.", code="bad_request")
    shot_name = args.get("screenshot", "")
    if shot_name and (not os.environ.get("GITS_REPAIR_SCREENSHOTS")
                      or not re.fullmatch(r"shot_\d{8}_\d{6}_(game|screen)\.(png|jpg)", shot_name)):
        raise ToolError("Укажи имя доступного снимка desktop.take_screenshot.", code="bad_request")
    try:
        request_id = await asyncio.to_thread(enqueue, Path(os.environ["GITS_REPAIR_QUEUE"]), report, shot_name)
    except OSError as exc:
        raise ToolError("Очередь ремонта недоступна.", code="unavailable") from exc
    return {"request_id": request_id, "state": "queued"}


async def repair_status(args):
    request_id = args["request_id"]
    if not re.fullmatch(r"[a-f0-9]{32}", request_id):
        raise ToolError("Нужен номер из core.bug_report.", code="bad_request")
    queue = Path(os.environ["GITS_REPAIR_QUEUE"])

    def read_status():
        if not (queue / f"{request_id}.json").is_file():
            return {"state": "unknown"}
        status = {"state": "queued"}
        issue = queue / f"{request_id}.issue"
        result = queue / f"{request_id}.result"
        if issue.is_file():
            status.update(state="running", issue=issue.read_text(encoding="utf-8"))
        if result.is_file():
            status.update(state="finished", result=result.read_text(encoding="utf-8"))
        return status

    return await asyncio.to_thread(read_status)


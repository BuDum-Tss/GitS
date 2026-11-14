import json

from services.tools.router import format_tool_result


def test_montage_file_page_resumes_on_utf8_byte_boundary():
    content = "a" + "я" * 2500
    result = json.dumps({"path": "schema.ts", "content": content}, ensure_ascii=False)
    page = format_tool_result("montage.files", {"op": "read", "offset": 10}, result)
    assert page.startswith("schema.ts (offset=10)\n" + "a" + "я" * 2047)
    assert "offset=4105" in page
    assert "\ufffd" not in page


def test_skill_page_preserves_start_guide_beyond_generic_limit():
    args = {"name": "montage-regulation", "file": "START.md"}
    result = {"path": "START.md", "content": "x" * 4200}
    page = format_tool_result("montage.skills", args, result)
    assert "x" * 4096 in page
    assert "montage.skills с тем же name и file и offset=4096" in page


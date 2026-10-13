"""Пространство модуля (TZ 15): поле `panel` манифеста и блоки `panel.set` (TAB-1, TAB-8)."""
import copy

import pytest

from gits_sdk import protocol as P
from gits_sdk.protocol import Manifest

BASE = {
    "id": "montage",
    "title": "Монтаж",
    "description": "Монтаж роликов по сценарию.",
    "version": "1.0.0",
    "tools": [
        {"name": "open_studio", "description": "Открыть Studio задачи.", "params": {"job_id": {"type": "string"}},
         "risk": "auto", "availability": "always"},
    ],
}

PANEL = {
    "title": "Монтаж",
    "icon": "film",
    "order": 40,
    "links": [
        {"id": "studio", "label": "Открыть в Remotion Studio", "kind": "tool", "tool": "open_studio",
         "args_from": "job", "open_result_url": True},
        {"id": "folder", "label": "Папка роликов", "kind": "reveal_path", "path": "C:\\MyProjects\\jobs"},
        {"id": "from_state", "label": "Папка задач", "kind": "reveal_path", "path_from": "jobs_dir"},
        {"id": "docs", "label": "Документация", "kind": "url", "url": "https://example.com/docs"},
    ],
    "embed_url": "http://127.0.0.1:3000",
    "serve_dirs": ["C:\\MyProjects\\jobs"],
    "actions": ["approve_plan", "open_studio"],
    "allowed_hosts": ["example.com"],
}


def manifest(panel=None, **changes):
    m = copy.deepcopy(BASE)
    if panel is not None:
        m["panel"] = panel
    m.update(changes)
    return m


def panel_with(**changes):
    p = copy.deepcopy(PANEL)
    for k, v in changes.items():
        if v is KeyError:
            p.pop(k, None)
        else:
            p[k] = v
    return p


# ---------------------------------------------------------------- поле panel манифеста (TAB-1)

def test_valid_panel_is_accepted_and_parsed():
    m = manifest(PANEL)
    warnings: list[str] = []
    assert P.validate_manifest(m, warnings) == []
    assert warnings == []
    spec = Manifest.from_dict(m)
    assert spec.panel is not None
    assert spec.panel.title == "Монтаж" and spec.panel.order == 40 and spec.panel.icon == "film"
    assert [link.kind for link in spec.panel.links] == ["tool", "reveal_path", "reveal_path", "url"]
    assert spec.panel.links[0].args_from == "job" and spec.panel.links[0].open_result_url is True
    assert spec.panel.actions == ["approve_plan", "open_studio"]
    assert spec.panel_warnings == []


def test_manifest_without_panel_has_no_panel():
    spec = Manifest.from_dict(manifest())
    assert spec.panel is None and spec.panel_warnings == []
    assert "panel" not in spec.to_dict()


def test_panel_roundtrip_through_to_dict():
    spec = Manifest.from_dict(manifest(PANEL))
    again = Manifest.from_dict(spec.to_dict())
    assert again.panel == spec.panel
    assert P.validate_manifest(spec.to_dict()) == []


def test_panel_defaults_order_when_absent():
    spec = Manifest.from_dict(manifest({"links": []}))
    assert spec.panel is not None and spec.panel.order == P.PANEL_ORDER_DEFAULT


BAD_PANELS = [
    ("not an object", "панель", "panel: должен быть объектом"),
    ({"links": "x"}, "panel.links", "список до"),
    (panel_with(links=[{"id": "Bad-Id", "label": "x", "kind": "url", "url": "https://e.com"}]), "id", "идентификатор"),
    (panel_with(links=[{"id": "a", "label": "x", "kind": "url", "url": "ftp://e.com"}]), "url", "http(s)"),
    (panel_with(links=[{"id": "a", "label": "x", "kind": "shell", "url": "https://e.com"}]), "kind", "одно из"),
    (panel_with(links=[{"id": "a", "label": "x", "kind": "tool", "tool": "missing"}]), "tool", "tools этого манифеста"),
    (panel_with(links=[{"id": "a", "label": "x", "kind": "tool", "tool": "open_studio", "args_from": "user"}]),
     "args_from", "одно из"),
    (panel_with(links=[{"id": "a", "label": "x", "kind": "reveal_path"}]), "ровно одно", "ровно одно"),
    (panel_with(links=[{"id": "a", "label": "x", "kind": "reveal_path", "path": "jobs", "path_from": "x"}]),
     "ровно одно", "ровно одно"),
    (panel_with(links=[{"id": "a", "label": "x", "kind": "reveal_path", "path": "relative\\dir"}]), "path", "абсолютный"),
    (panel_with(links=[{"id": "a", "label": "x", "kind": "url", "url": "https://e.com"},
                       {"id": "a", "label": "y", "kind": "url", "url": "https://e.com"}]), "повторяется", "повторяется"),
    (panel_with(embed_url="https://example.com/app"), "embed_url", "127.0.0.1"),
    (panel_with(serve_dirs=["relative"]), "serve_dirs", "абсолютных"),
    (panel_with(actions=["Bad Action"]), "actions", "идентификатор"),
    (panel_with(title=""), "title", "строка 1"),
    (panel_with(order="high"), "order", "целое"),
    (panel_with(allowed_hosts=["https://example.com"]), "allowed_hosts", "имён узлов"),
]


@pytest.mark.parametrize("panel,_needle,text", BAD_PANELS)
def test_bad_panel_does_not_reject_module(panel, _needle, text):
    """TAB-1: неверный panel не отклоняет модуль, его проблемы идут в предупреждения."""
    m = manifest(panel)
    warnings: list[str] = []
    assert P.validate_manifest(m, warnings) == []        # модуль принят
    assert any(text in w for w in warnings), warnings
    spec = Manifest.from_dict(m)                          # и собирается без вкладки
    assert spec.panel is None
    assert spec.panel_warnings and "отброшено" in spec.panel_warnings[0]


def test_panel_problems_are_silent_without_warnings_list():
    assert P.validate_manifest(manifest({"links": "x"})) == []


def test_panel_with_null_is_treated_as_absent():
    spec = Manifest.from_dict(manifest(None))
    assert spec.panel is None


# ---------------------------------------------------------------- блоки panel.set (TAB-8, проверка)

ACTIONS = {"approve_plan", "open_studio"}


def blocks_ok():
    return [
        {"type": "stats", "title": "Сводка", "items": [{"label": "Роликов", "value": 3}, {"label": "Готово", "value": "2"}]},
        {"type": "table", "columns": ["Задача", "Стадия"],
         "rows": [{"cells": ["j-1a2b3c4d", "монтаж"], "actions": [{"id": "open_studio", "label": "Студия"}]}]},
        {"type": "list", "items": [{"text": "Ролик", "sub": "черновик", "badge": "новое"}]},
        {"type": "text", "text": "<b>не HTML</b> и <script>x</script>"},
        {"type": "progress", "label": "Рендер", "value": 0.4},
        {"type": "buttons", "items": [{"label": "Одобрить", "action": "approve_plan", "style": "primary",
                                       "args": {"job_id": "j-1a2b3c4d"}},
                                      {"label": "Документы", "url": "https://example.com"}]},
        {"type": "link", "label": "Сайт", "url": "https://example.com"},
        {"type": "image", "artifact": {"job": "j-1a2b3c4d", "name": "frame.png"}, "alt": "кадр"},
        {"type": "video", "artifact": {"job": "j-1a2b3c4d", "name": "preview.mp4"}},
        {"type": "log", "lines": ["строка 1", "строка 2"]},
        {"type": "tabs", "tabs": [{"label": "Ролики", "blocks": [{"type": "text", "text": "вложено"}]}]},
    ]


def test_all_block_types_are_accepted():
    assert P.validate_panel_set({"blocks": blocks_ok()}, ACTIONS) == []


def test_empty_blocks_list_is_valid():
    assert P.validate_panel_set({"blocks": []}, ACTIONS) == []


def _problems(block, actions=ACTIONS):
    return P.validate_panel_set({"blocks": [block]}, actions)


BAD_BLOCKS = [
    ({"type": "html", "text": "x"}, "type"),
    ({"type": "text"}, "text"),
    ({"type": "text", "text": "x" * (P.PANEL_TEXT_MAX + 1)}, "text"),
    ({"type": "stats", "items": []}, "items"),
    ({"type": "stats", "items": [{"label": "a"}]}, "value"),
    ({"type": "table", "columns": ["a", "b"], "rows": [{"cells": ["only one"]}]}, "cells"),
    ({"type": "table", "columns": ["a"], "rows": [{"cells": [{"nested": 1}]}]}, "cells"),
    ({"type": "table", "columns": ["a"], "rows": [{"cells": ["x"], "actions": [{"id": "drop_db", "label": "x"}]}]},
     "не объявлено"),
    ({"type": "progress", "value": 1.5}, "value"),
    ({"type": "buttons", "items": [{"label": "x"}]}, "ровно одно"),
    ({"type": "buttons", "items": [{"label": "x", "action": "approve_plan", "url": "https://e.com"}]}, "ровно одно"),
    ({"type": "buttons", "items": [{"label": "x", "action": "approve_plan", "style": "neon"}]}, "style"),
    ({"type": "buttons", "items": [{"label": "x", "url": "javascript:alert(1)"}]}, "http(s)"),
    ({"type": "link", "label": "x", "url": "file:///C:/secret"}, "url"),
    ({"type": "image", "artifact": {"job": "j-1a2b3c4d", "name": "../secret.png"}}, "имя файла"),
    ({"type": "image", "artifact": {"job": "job-1", "name": "a.png"}}, "j-xxxxxxxx"),
    ({"type": "video", "artifact": {"job": "j-1a2b3c4d", "name": "dir/x.mp4"}}, "имя файла"),
    ({"type": "log", "lines": ["x" * (P.PANEL_LINE_MAX + 1)]}, "lines"),
    ({"type": "list", "items": [{"sub": "no text"}]}, "text"),
    ({"type": "tabs", "tabs": [{"label": "a", "blocks": [
        {"type": "tabs", "tabs": [{"label": "b", "blocks": [
            {"type": "tabs", "tabs": [{"label": "c", "blocks": []}]}]}]}]}]}, "глубже"),
    ({"type": "text", "text": "x", "title": "y" * 200}, "title"),
]


@pytest.mark.parametrize("block,needle", BAD_BLOCKS)
def test_bad_blocks_are_rejected(block, needle):
    problems = _problems(block)
    assert problems, block
    assert any(needle in p for p in problems), problems


def test_action_outside_declared_actions_is_rejected():
    problems = P.validate_panel_set({"blocks": [
        {"type": "buttons", "items": [{"label": "x", "action": "shutdown_pc"}]}]}, {"approve_plan"})
    assert any("shutdown_pc" in p for p in problems)


def test_action_ids_are_not_checked_when_actions_unknown():
    assert P.validate_panel_set({"blocks": [
        {"type": "buttons", "items": [{"label": "x", "action": "anything"}]}]}, None) == []


def test_too_many_blocks_is_rejected():
    blocks = [{"type": "text", "text": "x"}] * (P.PANEL_BLOCKS_MAX + 1)
    assert any("блоков" in p for p in P.validate_panel_set({"blocks": blocks}))


def test_body_over_64_kib_is_rejected():
    big = {"blocks": [{"type": "log", "lines": ["y" * 500] * 200}] * 4}
    assert any("байт" in p for p in P.validate_panel_set(big))


def test_non_json_values_are_rejected():
    body = {"blocks": [{"type": "progress", "value": float("nan")}]}
    assert P.validate_panel_set(body)


def test_body_shape_is_checked():
    assert P.validate_panel_set({"blocks": "x"})
    assert P.validate_panel_set([])


def test_html_text_is_only_a_string():
    """Текст блока - строка без разметки: проверка её не превращает в HTML, экранирование делает дашборд."""
    assert P.validate_panel_set({"blocks": [{"type": "text", "text": "<img src=x onerror=alert(1)>"}]}) == []

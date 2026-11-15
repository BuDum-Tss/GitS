"""Дашборд: рендер блоков пространства окружения (input, image с file, collapsed) и сворачиваемые «Настройки».

Код страницы лежит в agent/clients/gui/index.html одним скриптом. Здесь из него вынимаются чистые функции рендера
(от `const MODTAB` до `function modEventRows`) и запускаются в node с подставными localStorage и $; браузера нет, поэтому проверяется
разметка строкой. Без node проверки рендера пропускаются, статические остаются."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

HTML = (Path(__file__).resolve().parents[1] / "agent" / "clients" / "gui" / "index.html").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _region() -> str:
    esc = re.search(r"^const esc = .*$", HTML, re.M).group(0)
    start, end = HTML.index("const MODTAB ="), HTML.index("function modEventRows")
    return esc + "\n" + HTML[start:end]


def render(blocks, module="telegram", store=None):
    """HTML блоков и содержимое localStorage после рендера."""
    script = (
        "const mem = %s; const localStorage = {getItem: k => (k in mem ? mem[k] : null), setItem: (k, v) => { mem[k] = String(v); }};\n"
        "const $ = id => ({classList: {toggle() {}}, innerHTML: ''});\n"
        "%s\n"
        "const blocks = %s;\n"
        "process.stdout.write(JSON.stringify({html: panelBlocks('%s', blocks, ''), mem}));\n"
        % (json.dumps(store or {}), _region(), json.dumps(blocks, ensure_ascii=False), module))
    done = subprocess.run([NODE, "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


needs_node = pytest.mark.skipif(NODE is None, reason="нет node: рендер дашборда проверить нечем")


@needs_node
def test_input_block_renders_a_form_and_escapes_everything():
    html = render([{"type": "input", "action": "send", "label": "<b>Имя</b>", "placeholder": "\"x\" onfocus=\"alert(1)",
                    "submit": "Ок", "args": {"chat": 5}}])["html"]
    assert 'data-panel-input="send"' in html and 'type="text"' in html and "<b>Имя</b>" not in html
    assert "&lt;b&gt;Имя&lt;/b&gt;" in html and 'onfocus="alert' not in html and "&quot;x&quot;" in html
    assert 'data-args="{&quot;chat&quot;:5}"' in html and "data-secret" not in html


@needs_node
def test_secret_input_is_a_password_field_without_value_in_markup():
    html = render([{"type": "input", "action": "login_password", "label": "Пароль", "secret": True}])["html"]
    assert 'type="password"' in html and 'data-secret="1"' in html and "value=" not in html


@needs_node
def test_image_file_points_to_the_file_endpoint_and_artifact_to_the_job():
    html = render([{"type": "image", "file": "qr 1.png", "alt": "QR"},
                   {"type": "image", "artifact": {"job": "j-1a2b3c4d", "name": "a.png"}}])["html"]
    assert "/api/artifact?module=telegram&amp;file=qr%201.png" in html
    assert "job=j-1a2b3c4d&amp;name=a.png" in html


@needs_node
def test_collapsed_blocks_are_details_closed_by_default_and_remembered():
    block = [{"type": "text", "title": "Канал", "text": "готов", "collapsed": True},
             {"type": "text", "title": "Открытый", "text": "x", "collapsed": False},
             {"type": "text", "title": "Обычный", "text": "y"}]
    out = render(block)
    sections = out["html"].split("<section>")[1:]
    assert len(sections) == 2 and 'data-collapse="panel:telegram:0"' in sections[0] and " open" not in sections[0].split(">")[0]
    assert 'data-collapse="panel:telegram:1"' in sections[1] and "open" in sections[1].split(">")[0]
    assert "<details" not in out["html"].split("Обычный")[0].split("</details>")[-1]          # без collapsed: просто заголовок
    remembered = render(block, store={"gits.collapsed": json.dumps({"panel:telegram:0": True, "panel:telegram:1": False})})["html"]
    first, second = remembered.split("<details")[1:3]
    assert first.split(">")[0].endswith("open") or " open" in first.split(">")[0]
    assert " open" not in second.split(">")[0]                       # сохранённое состояние сильнее значения по умолчанию


@needs_node
def test_text_stays_escaped_in_every_new_block():
    html = render([{"type": "text", "title": "<i>t</i>", "text": "<script>alert(1)</script>", "collapsed": True}])["html"]
    assert "<script>" not in html and "&lt;script&gt;" in html and "&lt;i&gt;t&lt;/i&gt;" in html


def test_settings_of_every_workspace_are_collapsible_and_closed_by_default():
    """Настройки окружений: и в карточке «Обзора», и на странице окружения это сворачиваемая секция, по умолчанию свёрнутая."""
    assert "function collapsible(" in HTML and "gits.collapsed" in HTML
    assert "collapsible('settings:' + m.id, 'Настройки'" in HTML                       # страница окружения
    assert "collapseOpen('settings:' + m.id, true)" in HTML                            # карточка в «Обзоре»
    assert re.search(r"document\.addEventListener\('toggle'.*collapseSave", HTML, re.S)   # состояние запоминается


def test_secret_field_value_never_goes_to_storage_or_dom_attributes():
    submit = HTML[HTML.index("$('mod-page').addEventListener('submit'"):]
    submit = submit[:submit.index("});") + 3]
    assert "localStorage" not in submit and "field.value = ''" in submit and "if (secret) body.secret = true" in submit

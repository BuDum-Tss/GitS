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


@needs_node
def test_cards_render_avatar_letter_badge_select_and_nested_children():
    card = {"title": "Клуб <b>x</b>", "subtitle": "Вы: привет", "badge": "3", "tag": "темы: 2", "avatar": "av_m1_2.jpg",
            "action": "open_chat", "args": {"chat": -1}, "expanded": True, "selected": True,
            "select": {"options": [{"value": "ignore", "label": "Игнорировать"}, {"value": "read", "label": "Читать"}],
                       "value": "read", "action": "set_mode", "args": {"chat": -1}, "arg": "mode"},
            "children": [{"title": "ПРОГРЕСС", "color": "#A0B0C0", "icon": "#", "action": "open_chat",
                          "args": {"chat": -1, "topic": 5}, "badge": "1"}]}
    plain = {"title": "маша", "color": "red\" onmouseover=\"x"}                 # нет аватарки, цвет не по шаблону
    html = render([{"type": "cards", "items": [card, plain]}])["html"]
    assert "/api/artifact?module=telegram&amp;file=av_m1_2.jpg" in html and "Клуб &lt;b&gt;x&lt;/b&gt;" in html and "<b>x" not in html
    assert 'data-panel-action="open_chat"' in html and 'data-args="{&quot;chat&quot;:-1}"' in html and "pn-sel" in html
    assert 'data-panel-select="set_mode"' in html and 'data-arg="mode"' in html and '<option value="read" selected>' in html
    assert html.count("<option") == 2 and "pn-nested" in html and "ПРОГРЕСС" in html and "background:#A0B0C0" in html
    assert ">М<" in html and "onmouseover" not in html and "background:hsl(" in html                 # буква-заглушка, цвет только по шаблону
    assert "темы: 2" in html and ">3<" in html and "▾" in html


@needs_node
def test_collapsed_card_hides_children_and_split_has_two_columns_with_own_paths():
    cards = {"type": "cards", "items": [{"title": "Форум", "expanded": False, "action": "toggle_topics",
                                          "children": [{"title": "Тема"}]}]}
    html = render([{"type": "split", "left": [cards], "right": [{"type": "text", "text": "справа <i>"}]}])["html"]
    assert "Тема" not in html and "▸" in html and "pn-left" in html and "pn-right" in html and "справа &lt;i&gt;" in html
    assert html.index("pn-left") < html.index("Форум") < html.index("pn-right") < html.index("справа")
    assert 'data-keep-scroll="telegram:0"' in html


@needs_node
def test_input_draft_key_depends_on_args_so_drafts_do_not_leak_between_chats():
    def key(chat):
        html = render([{"type": "input", "action": "send", "label": "x", "args": {"chat": chat}}])["html"]
        return re.search(r'data-draft-key="([^"]*)"', html).group(1)
    assert key(1) != key(2) and key(1) == key(1)


def test_card_controls_are_wired_in_the_page_script():
    """Список режима отправляет действие без открытия карточки; сохраняется позиция прокрутки левой колонки."""
    assert "data-panel-select" in HTML and "if (e.target.closest('[data-panel-select]')) return;" in HTML
    assert "el.blur()" in HTML and "data-keep-scroll" in HTML and "container-type: inline-size" in HTML


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

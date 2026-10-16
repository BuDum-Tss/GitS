"""Шаблон Remotion без запуска Chromium (MON-19..MON-22, STY-4, STY-5): версии, токены против регламента, нет русских
констант в коде, демо-планы проходят схему zod, экспортные файлы (qa_zones.json, plan.schema.json) не устарели."""
from layout import DEFAULT
import json
import re
import shutil
import subprocess

import pytest

NODE = shutil.which("node")


@pytest.fixture(scope="module")
def tpl(root):
    return root / "template"


def _strip_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(^|[^:\"'`])//[^\n]*", r"\1", src)


def test_remotion_packages_pinned_to_one_exact_version(tpl):
    pkg = json.loads((tpl / "package.json").read_text(encoding="utf-8"))
    deps = {**pkg["dependencies"], **pkg["devDependencies"]}
    remotion = {n: v for n, v in deps.items() if n == "remotion" or n.startswith("@remotion/")}
    assert {"remotion", "@remotion/cli", "@remotion/captions", "@remotion/fonts", "@remotion/media-utils",
            "@remotion/zod-types"} <= set(remotion)
    assert len(set(remotion.values())) == 1                                  # одна версия на все пакеты Remotion
    assert all(re.fullmatch(r"\d+\.\d+\.\d+", v) for v in deps.values()), "версии точные, без ^ и ~"
    assert "zod" in deps


def test_installed_versions_match(tpl):
    if not (tpl / "node_modules").is_dir():
        pytest.skip("npm ci не выполнен")
    lock = json.loads((tpl / "package-lock.json").read_text(encoding="utf-8"))
    pkgs = lock["packages"]
    assert pkgs["node_modules/remotion"]["version"] == pkgs["node_modules/@remotion/cli"]["version"]


def test_colour_tokens_equal_the_regulation(root, tpl):
    style = DEFAULT.style.read_text(encoding="utf-8")
    table = dict(re.findall(r"\| `([\w-]+)` \| `(#[0-9A-Fa-f]{6})` \|", style))
    assert set(table) == {"chamberDark", "labWhite", "labGrey", "portalBlue", "portalOrange", "sign-paper", "sign-ink", "sign-muted",
                          "sign-off", "bp-center", "bp-mid", "bp-edge", "bp-line"}
    tokens = (tpl / "src" / "style" / "tokens.ts").read_text(encoding="utf-8")
    for name, hex_ in table.items():
        assert re.search(rf"(?:'{name}'|\b{name}): '{hex_}'", tokens, re.I), f"токен {name} не равен {hex_}"
    for gone in ("hazardYellow", "alertRed"):                                      # красный и жёлтый убраны из стиля (SC-1)
        assert gone not in tokens and gone not in style.split("## 4.")[0].split("## 3.")[1]


@pytest.mark.skipif(NODE is None, reason="нужен Node.js")
def test_tokens_equal_the_design_system_json_and_scene_math_works(tpl):
    """SC-1 и SC-2: tokens.ts равен template/design/tokens.json (дизайн-система v2); математика сцен (ирис, интро, эмоции,
    огибающая голоса, выноски, пути чертежа, таблички) проверяется без браузера."""
    r = _node(tpl, "scripts/check-scenes.mjs")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "check-scenes: ok" in r.stdout


def test_scene_assets_exist_in_the_template(tpl):
    """SC-5: фон комнаты, 6 эмоций питомца, рамка HUD; копии попадают в рабочую папку задачи."""
    from workspace import PUBLIC_DIRS
    assert (tpl / "public" / "scenes" / "glados-room.webp").stat().st_size > 10_000
    for mood in ("idle", "thinking", "angry", "happy", "question", "surprised"):
        png = (tpl / "public" / "pet" / f"{mood}.png").read_bytes()
        assert png[:8] == b"\x89PNG\r\n\x1a\n", mood
    hud = (tpl / "public" / "hud" / "hud-frame.svg").read_text(encoding="utf-8")
    assert "<svg" in hud and 'viewBox="0 0 1920 1080"' in hud and "fill-opacity" in hud
    assert {"scenes", "pet", "hud"} <= set(PUBLIC_DIRS)


def test_removed_styles_are_gone_from_the_template(tpl):
    """SC-1 и SC-3: нет компонентов и видов вставок, которые опирались на жёлтый и красный."""
    for name in ("ChamberCard", "HazardBanner", "TestResult", "PixelTransition", "LowerThird"):
        assert not (tpl / "src" / "components" / f"{name}.tsx").exists(), name
    for name in ("Intro", "Blueprint", "GladosRoom", "Sign", "FootageFrame", "Icon"):
        assert (tpl / "src" / "components" / f"{name}.tsx").is_file(), name
    schema = (tpl / "src" / "schema.ts").read_text(encoding="utf-8")
    for old in ("chamber_card", "hazard_banner", "test_result", "pixel_transition", "lower_third"):
        assert old not in schema, old
    for kind in ("intro", "blueprint", "glados_room", "chamber_sign", "result_sign"):
        assert f"'{kind}'" in schema, kind
    assert "frame: z.enum(['hud'])" in schema


def test_captions_are_not_drawn_outside_vertical(tpl):
    """SC-4: компонент субтитров сам не рисует их в горизонтали и квадрате; .srt остаётся за конвейером."""
    code = (tpl / "src" / "components" / "Captions.tsx").read_text(encoding="utf-8")
    assert "captionsBurned(width, height)" in code


def test_components_take_colours_only_from_tokens(tpl):
    for path in (tpl / "src").rglob("*.ts*"):
        if path.name in {"tokens.ts", "pictograms.generated.ts"}:
            continue
        code = _strip_comments(path.read_text(encoding="utf-8"))
        assert not re.search(r"#[0-9a-fA-F]{3,8}\b", code), f"{path.name}: цвет не из токенов"
        assert not re.search(r"\brgba?\(", code), f"{path.name}: rgba вне токенов (используй withAlpha)"
        assert "linear-gradient" not in code and "radial-gradient" not in code, f"{path.name}: градиенты запрещены"


def test_no_russian_text_constants_in_template(tpl):
    """STY-5: все тексты на экране из plan.json; в коде шаблона кириллицы нет вне комментариев."""
    for path in (tpl / "src").rglob("*.ts*"):
        code = _strip_comments(path.read_text(encoding="utf-8"))
        # сообщения об ошибках для разработчика (throw/Error) допустимы
        code = re.sub(r"(throw new Error|new Error)\([^;]*\);?", "", code)
        assert not re.search(r"[А-Яа-яЁё]", code), f"{path.relative_to(tpl)}: русская константа в коде"


def test_pictogram_module_is_in_sync_with_svg(root, tpl):
    generated = (tpl / "src" / "style" / "pictograms.generated.ts").read_text(encoding="utf-8")
    names = re.findall(r"^\t(\w+): \{strokeWidth", generated, re.M)
    svgs = sorted((DEFAULT.assets / "pictograms").glob("*.svg"))
    assert names == [p.stem for p in svgs]
    for p in svgs:
        inner = re.search(r"<svg[^>]*>(.*)</svg>", p.read_text(encoding="utf-8"), re.S).group(1)
        inner = re.sub(r"\s*\n\s*", "", inner).strip()
        assert json.dumps(inner, ensure_ascii=False) in generated, f"{p.name}: запусти node scripts/sync-assets.mjs"


def test_demo_plans_exist_for_each_format(tpl):
    """SC-7: шортс (интро, комната, схема, игра с субтитрами, итог) и YouTube (интро, отбивка, комната, схема, игра в рамке, итог)."""
    sizes = {"vertical": (1080, 1920), "horizontal": (1920, 1080), "square": (1080, 1080)}
    for kind, (w, h) in sizes.items():
        plan = json.loads((tpl / "demo" / f"plan.{kind}.json").read_text(encoding="utf-8"))
        assert (plan["format"]["width"], plan["format"]["height"]) == (w, h)
        assert plan["duration_ms"] > 0 and plan["inserts"] and plan["voiceover"] and plan["sfx"]
        order = [i["kind"] for i in sorted(plan["inserts"], key=lambda i: i["at_ms"])]
        assert all(s.get("beat") for s in plan["segments"]) and all(i.get("beat") for i in plan["inserts"])
        old = {"chamber_card", "hazard_banner", "pixel_transition", "test_result", "lower_third"}
        assert not old & set(order)
        if kind == "vertical":
            assert order == ["intro", "glados_room", "blueprint", "result_sign"] and plan["segments"] and plan["captions"]["enabled"]
        if kind == "horizontal":
            assert order == ["intro", "chamber_sign", "glados_room", "blueprint", "result_sign"]
            assert not plan["captions"]["enabled"] and all(s.get("frame") == "hud" for s in plan["segments"])
        if kind == "square":
            assert not plan["captions"]["enabled"] and {"intro", "glados_room", "blueprint", "result_sign", "custom"} <= set(order)
    short = json.loads((tpl / "demo" / "plan.vertical.short.json").read_text(encoding="utf-8"))
    assert short["duration_ms"] <= 10000 and short["cta"] is None            # ролик до 10 с без CTA
    assert [i["kind"] for i in short["inserts"]] == ["chamber_sign", "glados_room", "result_sign"]


def _node(tpl, *args):
    return subprocess.run([NODE, "--disable-warning=MODULE_TYPELESS_PACKAGE_JSON", *args], cwd=tpl, capture_output=True,
                          text=True, encoding="utf-8", timeout=120)


@pytest.mark.skipif(NODE is None, reason="нужен Node.js")
def test_demo_plans_pass_the_zod_schema(tpl):
    plans = sorted(str(p.relative_to(tpl)) for p in (tpl / "demo").glob("plan.*.json"))
    assert len(plans) >= 5
    r = _node(tpl, "scripts/validate-plan.mjs", *plans)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.skipif(NODE is None, reason="нужен Node.js")
def test_schema_rejects_broken_plans(tpl, tmp_path):
    plan = json.loads((tpl / "demo" / "plan.vertical.json").read_text(encoding="utf-8"))
    broken = {
        "no_beat_on_segment": lambda p: p["segments"][0].pop("beat"),
        "intro_subtitle_over_60": lambda p: p["inserts"][0]["props"].__setitem__("subtitle", "Я" * 61),
        "sign_title_over_30": lambda p: p["inserts"][3]["props"].__setitem__("title", "Я" * 31),
        "sign_big_over_12": lambda p: p["inserts"][3]["props"].__setitem__("big", "Я" * 13),
        "sign_hold_too_short": lambda p: p["inserts"][3].__setitem__("dur_ms", 1000),
        "unknown_icon": lambda p: p["inserts"][3]["props"]["active"].append("костёр"),
        "five_callouts": lambda p: p["inserts"][2]["props"].__setitem__("callouts", [{"x": 0.1, "y": 0.1, "label": "а"}] * 5),
        "callout_label_over_40": lambda p: p["inserts"][2]["props"]["callouts"][0].__setitem__("label", "Я" * 41),
        "bad_drawing_type": lambda p: p["inserts"][2]["props"]["drawing"].append({"type": "spiral"}),
        "bad_path_characters": lambda p: p["inserts"][2]["props"]["drawing"].append({"type": "path", "d": "M 0 0 L foo(1)"}),
        "bad_mood": lambda p: p["inserts"][1]["props"].__setitem__("moods", [{"from_ms": 0, "to_ms": 100, "mood": "furious"}]),
        "bad_frame_value": lambda p: p["segments"][0].__setitem__("frame", "border"),
        "cta_over_80": lambda p: p["cta"].__setitem__("text", "Я" * 81),
        "unknown_sfx": lambda p: p["sfx"][0].__setitem__("name", "vine_boom"),
        "removed_kind": lambda p: p["inserts"][0].__setitem__("kind", "hazard_banner"),
        "unknown_kind": lambda p: p["inserts"][0].__setitem__("kind", "confetti"),
        "unknown_field": lambda p: p.__setitem__("extra", 1),
        "negative_time": lambda p: p["segments"][0].__setitem__("at_ms", -1),
        "zoom_over_4_percent": lambda p: p["segments"][0].__setitem__("zoom", 0.1),
    }
    for name, mutate in broken.items():
        bad = json.loads(json.dumps(plan))
        mutate(bad)
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
        r = _node(tpl, "scripts/validate-plan.mjs", str(path))
        assert r.returncode == 1, f"{name}: схема пропустила неверный план"


@pytest.mark.skipif(NODE is None, reason="нужен Node.js")
def test_exported_json_files_are_current(tpl, tmp_path):
    zones = tmp_path / "qa_zones.json"
    schema = tmp_path / "plan.schema.json"
    assert _node(tpl, "scripts/export-qa-zones.mjs", str(zones)).returncode == 0
    assert _node(tpl, "scripts/export-schema.mjs", str(schema)).returncode == 0
    assert zones.read_text(encoding="utf-8") == (tpl / "qa_zones.json").read_text(encoding="utf-8"), "npm run gen:qa-zones"
    assert schema.read_text(encoding="utf-8") == (tpl / "plan.schema.json").read_text(encoding="utf-8"), "npm run gen:schema"


def test_qa_zones_content(tpl):
    z = json.loads((tpl / "qa_zones.json").read_text(encoding="utf-8"))
    assert z["safe_zones"]["vertical"] == {"top": 0.12, "bottom": 0.2}
    comps = z["components"]
    assert set(comps) == {"chamber_sign", "result_sign", "glados_room", "cta", "journal", "specimen", "blackout"}
    for kind in comps:
        assert set(comps[kind]) >= {"vertical", "horizontal", "square", "avoid_captions"}
    for kind, entry in comps.items():
        for fmt in ("vertical", "horizontal", "square"):
            r = entry[fmt]
            assert 0 <= r["x"] and 0 <= r["y"] and r["x"] + r["w"] <= 1.0001 and r["y"] + r["h"] <= 1.0001, (kind, fmt)
    # вертикаль: важное вне верхних 12 % и нижних 20 % (регламент §8): табличка, питомец, призыв, журнал
    for kind in ("chamber_sign", "result_sign", "glados_room", "cta", "journal"):
        r = comps[kind]["vertical"]
        assert r["y"] >= 0.12 and r["y"] + r["h"] <= 0.8, kind
    assert comps["glados_room"]["vertical"]["h"] == 0.42 and comps["glados_room"]["horizontal"]["h"] == 0.62     # высота питомца
    assert comps["chamber_sign"]["avoid_captions"] and comps["glados_room"]["avoid_captions"]
    cz = z["captions"]["zone_at_default"]["vertical"]
    assert cz["y"] >= 0.12 and cz["y"] + cz["h"] <= 0.8

"""Шаблон Remotion без запуска Chromium (MON-19..MON-22, STY-4, STY-5): версии, токены против регламента, нет русских
констант в коде, демо-планы проходят схему zod, экспортные файлы (qa_zones.json, plan.schema.json) не устарели."""
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
    style = (root / "docs" / "APERTURE_STYLE.md").read_text(encoding="utf-8")
    table = dict(re.findall(r"\| `(\w+)` \| `(#[0-9A-Fa-f]{6})` \|", style))
    assert set(table) == {"labWhite", "labGrey", "chamberDark", "portalBlue", "portalOrange", "hazardYellow", "alertRed"}
    tokens = (tpl / "src" / "style" / "tokens.ts").read_text(encoding="utf-8")
    for name, hex_ in table.items():
        assert re.search(rf"{name}: '{hex_}'", tokens, re.I), f"токен {name} не равен {hex_}"


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
    svgs = sorted((root / "assets" / "pictograms").glob("*.svg"))
    assert names == [p.stem for p in svgs]
    for p in svgs:
        inner = re.search(r"<svg[^>]*>(.*)</svg>", p.read_text(encoding="utf-8"), re.S).group(1)
        inner = re.sub(r"\s*\n\s*", "", inner).strip()
        assert json.dumps(inner, ensure_ascii=False) in generated, f"{p.name}: запусти node scripts/sync-assets.mjs"


def test_demo_plans_exist_for_each_format(tpl):
    sizes = {"vertical": (1080, 1920), "horizontal": (1920, 1080), "square": (1080, 1080)}
    for kind, (w, h) in sizes.items():
        plan = json.loads((tpl / "demo" / f"plan.{kind}.json").read_text(encoding="utf-8"))
        assert (plan["format"]["width"], plan["format"]["height"]) == (w, h)
        assert plan["duration_ms"] > 0 and plan["segments"] and plan["inserts"]
        kinds = {i["kind"] for i in plan["inserts"]}
        assert {"chamber_card", "hazard_banner", "pixel_transition", "test_result", "lower_third", "custom"} <= kinds
        assert plan["cta"] and plan["blackout"] and plan["voiceover"] and plan["sfx"]
        assert all(s.get("beat") for s in plan["segments"]) and all(i.get("beat") for i in plan["inserts"])
    short = json.loads((tpl / "demo" / "plan.vertical.short.json").read_text(encoding="utf-8"))
    assert short["duration_ms"] <= 10000 and short["cta"] is None            # ролик до 10 с без CTA


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
        "title_over_40": lambda p: p["inserts"][0]["props"].__setitem__("title", "Я" * 41),
        "text_over_60": lambda p: p["inserts"][2]["props"].__setitem__("text", "Я" * 61),
        "cta_over_80": lambda p: p["cta"].__setitem__("text", "Я" * 81),
        "unknown_sfx": lambda p: p["sfx"][0].__setitem__("name", "vine_boom"),
        "unknown_kind": lambda p: p["inserts"][0].__setitem__("kind", "confetti"),
        "unknown_field": lambda p: p.__setitem__("extra", 1),
        "one_pictogram": lambda p: p["inserts"][0]["props"].__setitem__("pictograms", ["cube"]),
        "bad_pictogram": lambda p: p["inserts"][0]["props"].__setitem__("pictograms", ["cube", "portal"]),
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
    for kind in ("chamber_card", "hazard_banner", "test_result", "lower_third", "cta", "journal", "blackout"):
        assert set(comps[kind]) >= {"vertical", "horizontal", "square", "avoid_captions"}
    for kind, entry in comps.items():
        for fmt in ("vertical", "horizontal", "square"):
            r = entry[fmt]
            assert 0 <= r["x"] and 0 <= r["y"] and r["x"] + r["w"] <= 1.0001 and r["y"] + r["h"] <= 1.0001, (kind, fmt)
    # вертикаль: важный текст вне верхних 12 % и нижних 20 % (регламент §8); баннер и подпись внутри безопасной области
    for kind in ("chamber_card", "hazard_banner", "test_result", "lower_third", "cta", "journal"):
        r = comps[kind]["vertical"]
        assert r["y"] >= 0.12 and r["y"] + r["h"] <= 0.8, kind
    assert comps["hazard_banner"]["vertical"]["h"] == 0.12                    # 12 % кадра
    assert comps["chamber_card"]["vertical"]["h"] == 0.5                      # половина высоты
    cz = z["captions"]["zone_at_default"]["vertical"]
    assert cz["y"] >= 0.12 and cz["y"] + cz["h"] <= 0.8

"""Песочница (MON-17, MON-18): таблица разрешённых и запрещённых вызовов, включая «..», &&, |, npm i, curl, чужую задачу."""
import json

import pytest

from layout import DEFAULT
from sandbox import Sandbox, tokenize, Denied


@pytest.fixture()
def box(tmp_path):
    job = tmp_path / "jobs" / "j-aaaaaaaa"
    for sub in ("work/scripts", "work/src/custom", "work/public/media", "plan", "renders", "sources"):
        (job / sub).mkdir(parents=True)
    (tmp_path / "jobs" / "j-bbbbbbbb" / "work").mkdir(parents=True)
    (job / "work" / "scripts" / "validate-plan.mjs").write_text((DEFAULT.template / "scripts" / "validate-plan.mjs").read_text(encoding="utf-8"),
                                                              encoding="utf-8")
    (job / "work" / "scripts" / "evil.mjs").write_text("import cp from 'node:child_process'; cp.exec('calc')", encoding="utf-8")
    (job / "work" / "scripts" / "mine.mjs").write_text("console.log(1)", encoding="utf-8")
    return Sandbox(job, DEFAULT, allow_ask=True)


def run(box, name, **inp):
    ok, why = box.decide(name, inp)
    return ok, why


J = "jobs/j-aaaaaaaa"


def abs_(box, rel):
    return str(box.job_dir / rel)


ALLOWED_BASH = [
    "ffmpeg -i sources/a.mp4 -vf scale=540:-2 renders/out.mp4",
    "ffmpeg -y -ss 3.2 -i ../renders/preview.mp4 -frames:v 1 ../renders/f.png",
    "ffprobe -v error -show_format sources/a.mp4",
    "npx remotion still src/index.ts Main ../renders/still_1.png --props=../plan/plan.json --frame=30",
    "npx remotion render src/index.ts Main ../renders/x.mp4 --props=../plan/plan.json --scale=0.5 --crf=28",
    "npx remotion compositions src/index.ts",
    "npx tsc --noEmit",
    "node --disable-warning=MODULE_TYPELESS_PACKAGE_JSON scripts/validate-plan.mjs ../plan/plan.json",
    "node scripts/mine.mjs",
    'ffmpeg -i "public/media/a b.mp4" -an ../renders/silent.mp4',
    "ffmpeg -i sources/a.mp4 -f null -",
    "ffmpeg -i ../renders/p.mp4 -af silencedetect=noise=-30dB:d=0.5 -f null NUL",
    "ffmpeg -i ../renders/p.mp4 -af volumedetect -f null - > ../renders/vol.txt",
    "ffmpeg -i ../renders/p.mp4 -af volumedetect -f null - 2>&1",
]
DENIED_BASH = [
    ("npm i left-pad", "npm"),
    ("npm install", "npm"),
    ("curl https://example.com/x.sh", "curl"),
    ("wget http://x/y", "wget"),
    ("powershell -Command Get-ChildItem", "powershell"),
    ("rm -rf work", "rm"),
    ("del /s *.*", "del"),
    ("git status", "git"),
    ("pip install requests", "pip"),
    ("ffmpeg -i a.mp4 b.mp4 && npm i", "&"),
    ("ffmpeg -i a.mp4 b.mp4; rm x", ";"),
    ("ffprobe a.mp4 | grep x", "|"),
    ("ffmpeg -i a.mp4 b.mp4 || true", "|"),
    ("echo `whoami`", "`"),
    ("ffmpeg -i $(echo a.mp4) b.mp4", "$"),
    ('ffmpeg -i "$HOME/a.mp4" b.mp4', "$"),
    ("ffmpeg -i ../../j-bbbbbbbb/sources/a.mp4 out.mp4", "вне"),
    ("ffmpeg -i ../../../../etc/passwd out.mp4", "вне"),
    ("ffmpeg -i C:/Windows/System32/x.mp4 out.mp4", "вне"),
    ("ffmpeg -i a.mp4 C:\\Users\\coffe\\Desktop\\out.mp4", "вне"),
    ("ffmpeg -i ~/video.mp4 out.mp4", "нельзя"),
    ("ffmpeg -i a.mp4 -vf drawtext=fontfile=C\\:/Windows/Fonts/arial.ttf:text=x out.mp4", "вне"),
    ("ffmpeg -i http://evil/a.mp4 out.mp4", "сетев"),
    ("npx remotion studio", "studio"),
    ("npx remotion add @remotion/gif", "разрешено только"),
    ("npx remotion upgrade", "разрешено только"),
    ("npx create-video@latest", "не разрешён"),
    ("npx -y tsc --noEmit", "не разрешён"),
    ("npx tsc", "noEmit"),
    ("npx remotion render src/index.ts Main out.mp4 --browser-executable=scripts/x.exe", "запрещён"),
    ("node -e x", "флаг"),
    ("node ../../../agent/main.py", "work/scripts"),
    ("node scripts/evil.mjs", "сеть"),
    ("node C:/x/y.mjs", "work/scripts"),
    ("./ffmpeg -i a b", "по имени"),
    ("ffmpeg -i a.mp4 out.mp4 > /etc/hosts", "вне"),
    ("ffmpeg -i a.mp4 out.mp4 < in.txt", "<"),
    ("ls -la", "не разрешена"),
    ("cat ../sources/info.json", "не разрешена"),
    ("", "пустая"),
]


@pytest.mark.parametrize("cmd", ALLOWED_BASH)
def test_allowed_bash(box, cmd):
    ok, why = run(box, "Bash", command=cmd)
    assert ok, why


@pytest.mark.parametrize(("cmd", "needle"), DENIED_BASH)
def test_denied_bash(box, cmd, needle):
    ok, why = run(box, "Bash", command=cmd)
    assert not ok and needle.lower() in why.lower(), (cmd, why)


def test_read_inside_and_outside(box):
    for rel in ("plan/plan.json", "work/src/Main.tsx", "sources/info.json", "renders/still_1.png"):
        assert run(box, "Read", file_path=abs_(box, rel))[0]
    assert run(box, "Read", file_path=str(DEFAULT.template / "demo" / "plan.vertical.json"))[0]
    assert run(box, "Read", file_path=str(DEFAULT.docs / "APERTURE_STYLE.md"))[0]
    assert run(box, "Read", file_path=str(DEFAULT.assets / "fonts" / "OFL.txt"))[0]
    assert run(box, "Read", file_path="src/Main.tsx")[0]                                                   # относительный путь от work/
    assert run(box, "Read", file_path=str(DEFAULT.skills / "remotion-best-practices" / "SKILL.md"))[0]
    for bad in (str(box.job_dir.parent / "j-bbbbbbbb" / "job.json"), "C:/Windows/win.ini", "../../j-bbbbbbbb/job.json",
                "C:/Users/coffe/.claude/settings.json", str(DEFAULT.root / "agent" / "main.py"), str(DEFAULT.root / ".git" / "config")):
        ok, why = run(box, "Read", file_path=bad)
        assert not ok and "читать можно только" in why, bad


def test_glob_and_grep(box):
    assert run(box, "Glob", pattern="src/**/*.tsx")[0]
    assert run(box, "Glob", pattern="*.json", path=abs_(box, "plan"))[0]
    assert not run(box, "Glob", pattern="../../**/*")[0]
    assert not run(box, "Glob", pattern="C:/**/*.ini")[0]
    assert not run(box, "Glob", pattern="*", path="C:/Windows")[0]
    assert run(box, "Grep", pattern="Captions", path=abs_(box, "work/src"))[0]
    assert not run(box, "Grep", pattern="password", path="C:/Users")[0]


def test_write_and_edit_only_in_work_and_plan(box):
    assert run(box, "Write", file_path=abs_(box, "work/src/custom/Blueprint.tsx"))[0]
    assert run(box, "Edit", file_path=abs_(box, "plan/plan.json"))[0]
    assert run(box, "Write", file_path="src/custom/New.tsx")[0]
    for bad in ("work/node_modules/x.js", "sources/a.mp4", "renders/x.mp4", "job.json", "log.jsonl", "../j-bbbbbbbb/work/x.ts"):
        ok, why = run(box, "Write", file_path=abs_(box, bad))
        assert not ok, bad
    assert not run(box, "Write", file_path=str(DEFAULT.template / "src" / "Main.tsx"))[0]              # шаблон менять нельзя
    assert not run(box, "Edit", file_path="C:/Users/coffe/.bashrc")[0]
    assert not run(box, "Write", file_path=str(DEFAULT.docs / "APERTURE_STYLE.md"))[0]
    assert not run(box, "Write", file_path="../../../agent/sandbox.py")[0]


def test_tools_outside_the_policy_are_denied(box):
    for name in ("WebFetch", "WebSearch", "Task", "Agent", "NotebookEdit", "mcp__other__tool", "mcp__montage__delete_everything"):
        ok, why = run(box, name, url="https://x")
        assert not ok, name
    for name in ("Skill", "TodoWrite", "mcp__montage__progress", "mcp__montage__synthesize_voice", "mcp__montage__kb_lookup",
                 "mcp__montage__transcript_words", "mcp__montage__ask_user"):
        assert run(box, name)[0], name


def test_ask_user_is_only_for_clarify_and_build(tmp_path):
    job = tmp_path / "j-cccccccc"
    (job / "work").mkdir(parents=True)
    closed = Sandbox(job, DEFAULT, allow_ask=False)
    assert not closed.decide("mcp__montage__ask_user", {})[0] and closed.decide("mcp__montage__progress", {})[0]


def test_readonly_reviewer_only_reads(tmp_path):
    job = tmp_path / "j-dddddddd"
    (job / "work").mkdir(parents=True)
    ro = Sandbox(job, DEFAULT, readonly=True)
    assert ro.decide("Read", {"file_path": str(job / "qa" / "frames" / "f.png")})[0] and ro.decide("Glob", {"pattern": "*.png"})[0]
    for name in ("Bash", "Write", "Edit", "Skill", "mcp__montage__progress", "mcp__montage__ask_user"):
        assert not ro.decide(name, {"command": "ffmpeg -i a b", "file_path": str(job / "work" / "x")})[0], name


async def test_decisions_are_logged_without_file_contents(box):
    rows = []
    box.log = rows.append
    r1 = await box.can_use_tool("Bash", {"command": "ffmpeg -i a.mp4 b.mp4"}, None)
    r2 = await box.can_use_tool("Bash", {"command": "curl http://x"}, None)
    r3 = await box.can_use_tool("Write", {"file_path": abs_(box, "work/a.ts"), "content": "СЕКРЕТНОЕ СОДЕРЖИМОЕ"}, None)
    assert type(r1).__name__ == "PermissionResultAllow" and type(r2).__name__ == "PermissionResultDeny" and r2.message
    assert [(r["tool"], r["allowed"]) for r in rows] == [("Bash", True), ("Bash", False), ("Write", True)]
    assert "СЕКРЕТНОЕ" not in json.dumps(rows, ensure_ascii=False) and type(r3).__name__ == "PermissionResultAllow"


def test_tokenize_basics():
    assert tokenize('ffmpeg -i "a b.mp4" out.mp4') == ["ffmpeg", "-i", "a b.mp4", "out.mp4"]
    assert tokenize("echo '$HOME'") == ["echo", "$HOME"]
    assert tokenize("ffmpeg -i a b > out.txt") == ["ffmpeg", "-i", "a", "b", ">", "out.txt"]
    with pytest.raises(Denied):
        tokenize("a && b")
    with pytest.raises(Denied):
        tokenize("a\nb")
    with pytest.raises(Denied):
        tokenize('echo "unclosed')


def test_junction_to_template_is_not_a_write_hole(box):
    # work/node_modules ведёт в template/: писать туда нельзя ни через имя ссылки, ни напрямую
    assert not run(box, "Write", file_path=abs_(box, "work/node_modules/zod/index.js"))[0]
    assert not run(box, "Write", file_path=str(DEFAULT.template / "node_modules" / "zod" / "index.js"))[0]

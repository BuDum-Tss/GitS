"""Запуск приложений как в Alt+Пробел: каталог из «Пуска» и магазина, поиск, запреты, добавление."""
import pytest

import app_launcher as al


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_DIR", str(tmp_path))
    start = tmp_path / "Start"
    for name in ["Google Chrome", "Microsoft Edge", "Microsoft Word", "LM Studio", "Uninstall LM Studio", "Windows PowerShell",
                 "Telegram", "Telegram Desktop Helper"]:
        (start / "Programs").mkdir(parents=True, exist_ok=True)
        (start / "Programs" / f"{name}.lnk").write_bytes(b"")
    monkeypatch.setattr(al, "_start_roots", lambda: [str(start)])
    monkeypatch.setattr(al, "_uwp_apps", lambda: [{"Name": "Калькулятор", "AppID": "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"},
                                                  {"Name": "Terminal", "AppID": "x!y"}])
    monkeypatch.setattr(al.game_launcher, "find_exe", lambda: str(tmp_path / "RimWorldWin64.exe"))
    al._CACHE.update(at=0.0, items=[])
    return tmp_path


def test_catalog_has_start_menu_uwp_and_curated_but_not_dangerous(env):
    titles = {a["title"] for a in al.catalog()}
    assert {"Google Chrome", "LM Studio", "Калькулятор", "Telegram"} <= titles
    assert not any(t in titles for t in ("Uninstall LM Studio", "Windows PowerShell"))      # деинсталлятор и консоль исключены


def test_search_like_run(env):
    assert al.find("chrome")["title"] == "Google Chrome"
    assert al.find("lm studio")["title"] == "LM Studio"
    assert al.find("калькулятор")["kind"] == "uwp"
    assert al.find("телеграм")["title"] == "Telegram" and al.find("открой хром")["title"] == "Google Chrome"
    assert al.find("Telegram")["title"] == "Telegram"                     # точное совпадение побеждает «Telegram Desktop Helper»


def test_unknown_and_ambiguous(env):
    with pytest.raises(ValueError, match="не нашла"):
        al.find("зюзябликс")
    with pytest.raises(ValueError, match="несколько"):
        al.find("micro")                                                  # Microsoft Edge и Microsoft Word: лидера нет


def test_launch_start_menu_and_store_apps(env, monkeypatch):
    started = []
    monkeypatch.setattr(al.os, "startfile", lambda p: started.append(("lnk", p)), raising=False)
    monkeypatch.setattr(al.subprocess, "Popen", lambda args, **k: started.append(("popen", args)))
    assert "запущено" in al.launch("chrome") and started[-1][0] == "lnk"
    assert "запущено" in al.launch("калькулятор")
    assert started[-1][1][0] == "explorer.exe" and "Microsoft.WindowsCalculator" in started[-1][1][1]
    with pytest.raises(ValueError):
        al.launch("powershell")                                           # консоль не запускается


def test_rimworld_goes_through_game_launcher(env, monkeypatch):
    monkeypatch.setattr(al.game_launcher, "launch", lambda: "игра запущена, мод ответил")
    assert al.launch("римворлд") == "игра запущена, мод ответил" and al.launch("игру") == "игра запущена, мод ответил"


def test_add_app_validates_and_extends_catalog(env, tmp_path):
    new = tmp_path / "Other.exe"
    new.write_bytes(b"")
    assert "добавила" in al.add("Другая программа", str(new), ["другая"])
    assert al.find("другая")["title"] == "Другая программа"
    with pytest.raises(ValueError, match="уже в списке"):
        al.add("Ещё раз", str(new))
    with pytest.raises(ValueError, match="нужен существующий"):
        al.add("Скрипт", str(tmp_path / "evil.ps1"))

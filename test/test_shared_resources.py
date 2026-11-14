from gits_sdk.resources import shared_catalog, shared_entries, shared_file


def test_montage_exports_are_shared_without_a_workspace_lease(tmp_path, monkeypatch):
    monkeypatch.setenv("CHARACTER_HOME", str(tmp_path))
    out = tmp_path / "montage" / "out"
    out.mkdir(parents=True)
    video = out / "final.mp4"
    video.write_bytes(b"video")
    (out / ".final.tmp").write_bytes(b"incomplete")
    assert shared_catalog("mp4") == ["montage/out/final.mp4"]
    assert shared_file("montage/final.mp4") == video
    assert shared_file("montage/../private.mp4") is None
    assert shared_file("montage/.final.tmp") is None
    subfolder = out / "episode"
    subfolder.mkdir()
    (subfolder / "clip.mp4").write_bytes(b"clip")
    assert "montage/out/episode/clip.mp4" in shared_catalog("clip")
    assert shared_entries("montage/out/episode") == [
        {"name": "clip.mp4", "path": "montage/out/episode/clip.mp4", "directory": False},
    ]
    assert shared_entries("montage/../shared") is None

    design = tmp_path / "montage" / "design"
    design.mkdir()
    (design / "tokens.json").write_text("{}")
    (design / "settings.json").write_text("{}")
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "prompt.yaml").write_text("private")
    assert shared_catalog("design") == ["montage/design/tokens.json"]
    assert shared_entries("core") is None
    assert shared_file("montage/design/settings.json") is None
    assert shared_file("core/prompt.yaml") is None


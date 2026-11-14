from gits_sdk.resources import shared_catalog, shared_file


def test_montage_exports_are_shared_without_a_workspace_lease(tmp_path, monkeypatch):
    monkeypatch.setenv("CHARACTER_HOME", str(tmp_path))
    out = tmp_path / "montage" / "out"
    out.mkdir(parents=True)
    video = out / "final.mp4"
    video.write_bytes(b"video")
    (out / ".final.tmp").write_bytes(b"incomplete")
    assert shared_catalog("mp4") == ["montage/final.mp4"]
    assert shared_file("montage/final.mp4") == video
    assert shared_file("montage/../private.mp4") is None
    assert shared_file("montage/.final.tmp") is None


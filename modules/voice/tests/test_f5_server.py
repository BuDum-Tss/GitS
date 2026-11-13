"""Разбор референса в сервисе ESpeech (services/f5-tts/f5_server.py): без CUDA, fastapi и torch, тяжёлые импорты подменены."""
import base64
import importlib.util
import sys
import types
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[3] / "services" / "f5-tts" / "f5_server.py"


@pytest.fixture(scope="module")
def f5():
    class _App:
        def get(self, *a, **k):
            return lambda f: f

        post = get

    stubs = {
        "soundfile": types.ModuleType("soundfile"),
        "uvicorn": types.ModuleType("uvicorn"),
        "fastapi": types.SimpleNamespace(FastAPI=lambda: _App(), Response=object),
        "fastapi.responses": types.SimpleNamespace(JSONResponse=object),
        "pydantic": types.SimpleNamespace(BaseModel=object),
    }
    saved = {k: sys.modules.get(k) for k in stubs}
    sys.modules.update(stubs)
    try:
        spec = importlib.util.spec_from_file_location("f5_server_under_test", SERVER)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
    return mod


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def test_b64_reference_is_written_to_cache_by_content_hash(f5, tmp_path):
    path = f5.resolve_ref("", _b64(b"RIFFwav"), tmp_path)
    assert Path(path).parent == tmp_path and Path(path).read_bytes() == b"RIFFwav"
    assert f5.resolve_ref("", _b64(b"RIFFwav"), tmp_path) == path               # то же содержимое: тот же файл
    assert f5.resolve_ref("", _b64(b"other"), tmp_path) != path


def test_b64_reference_is_not_rewritten_on_every_request(f5, tmp_path):
    cache = tmp_path / "cache"
    path = Path(f5.resolve_ref("", _b64(b"RIFFwav"), cache))
    stamp = path.stat().st_mtime_ns
    f5.resolve_ref("", _b64(b"RIFFwav"), cache)
    assert path.stat().st_mtime_ns == stamp and [p.name for p in cache.iterdir()] == [path.name]


def test_b64_wins_over_path_and_path_still_works(f5, tmp_path):
    ref = tmp_path / "ref.wav"
    ref.write_bytes(b"path-ref")
    cache = tmp_path / "cache"
    assert f5.resolve_ref(str(ref), _b64(b"b64-ref"), cache) != str(ref)
    assert f5.resolve_ref(str(ref), "", cache) == str(ref)


def test_missing_or_broken_reference_is_none(f5, tmp_path):
    assert f5.resolve_ref("", "", tmp_path) is None
    assert f5.resolve_ref(str(tmp_path / "нет.wav"), "", tmp_path) is None
    assert f5.resolve_ref("", "это не base64!", tmp_path) is None
    ref = tmp_path / "ref.wav"
    ref.write_bytes(b"x")
    assert f5.resolve_ref(str(ref), "это не base64!", tmp_path) == str(ref)     # битый b64: падаем на путь

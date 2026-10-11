"""Узнавание людей по голосу (перенесено из тестов ядра, VOICE-2: данные остаются в MEMORY_DIR под прежними именами)."""
import numpy as np

import voice_id as vid


def test_voice_identity_recognises_and_separates(tmp_path):
    v = vid.VoiceIdentity(str(tmp_path))
    rng = np.random.default_rng(1)
    a, b = rng.normal(size=192), rng.normal(size=192)
    pa, new = v.resolve(list(a))
    assert new and pa.startswith("person-")
    again, new2 = v.resolve(list(a + rng.normal(scale=0.05, size=192)))       # тот же голос с шумом
    assert again == pa and not new2
    pb, new3 = v.resolve(list(b))
    assert pb != pa and new3
    assert v.resolve(None) == (None, False) and v.resolve([float("nan")] * 4) == (None, False)


def test_voice_names_owner_and_self(tmp_path):
    v = vid.VoiceIdentity(str(tmp_path))
    rng = np.random.default_rng(2)
    e = rng.normal(size=64)
    pid, _ = v.resolve(list(e))
    assert v.set_name(pid, "Аня") and v.name_of(pid) == "Аня" and v.set_owner(pid) and v.owner() == pid
    assert not v.has_self()
    v.register_self(list(rng.normal(size=64)))
    assert v.has_self() and not v.set_owner(vid.SELF_PERSON_ID)
    assert all(p["id"] != vid.SELF_PERSON_ID for p in v.people())
    # состояние переживает перезапуск
    assert vid.VoiceIdentity(str(tmp_path)).name_of(pid) == "Аня"


def test_data_file_keeps_its_old_name(tmp_path):
    v = vid.VoiceIdentity(str(tmp_path))
    v.resolve(list(np.random.default_rng(5).normal(size=16)))
    assert (tmp_path / "voices.json").exists()

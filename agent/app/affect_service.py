"""Эмоции персонажа: оркестрация (ТЗ 26). Состояние хранится по персонажу в `<папка памяти персонажа>/affect.json`,
чистая логика в `domain/affect.py`. Часы и запись подставляются, поэтому в тестах время управляемое.

Источники сдвига: реплика пользователя и реакции на события (`fast_brain.publish` зовёт `on_emotion`), метки ошибок
(`journal.mistake` зовёт `on_mistake`), изменение отношений (поле `delta` реакции на реплику). Собственные реплики состояние
не двигают (иначе эмоция подкрепляла бы сама себя): их интенсивность берётся из состояния (`reply_intensity`).
Настроение не меняет факты, конституцию, разрешения и безопасность: наружу уходят только факт для контекста, подпись
питомца, ограниченный сдвиг голоса и сдвиг порогов внимания.
"""
from __future__ import annotations

import json
import logging
import time
from collections import deque
from pathlib import Path
from typing import Callable

from domain import affect as dom

log = logging.getLogger("affect")

FILE_NAME = "affect.json"
HISTORY_S = 24 * 3600.0
SAMPLE_EVERY_S = 300.0
SAVE_EVERY_S = 30.0
REPLY_CATEGORY = "say"            # собственные реплики: состояние не двигают
CHAT_CATEGORY = "chat"


def category_of(event_name: str) -> str | None:
    """Категория чувствительности по имени события; None: событие состояние не двигает."""
    name = str(event_name or "")
    if name == REPLY_CATEGORY:
        return None
    if name == "chat":
        return "chat"
    return "game"


class AffectService:
    def __init__(self, folder: Path, params: dom.Params | None = None, *, clock: Callable[[], float] = time.time,
                 on_change: Callable[[dict], None] | None = None):
        self.params = params or dom.Params()
        self.clock = clock
        self.on_change = on_change              # снимок для presence; синхронный
        self.folder = Path(folder)
        self.state = dom.initial(self.params, clock())
        self.history: deque = deque()           # [ts, valence, mood, arousal]
        self.warnings: list[str] = []
        self._saved_at = 0.0
        self.load()

    # ── хранение ────────────────────────────────────────────────────────────

    @property
    def path(self) -> Path:
        return self.folder / FILE_NAME

    def load(self) -> None:
        """Восстановить состояние (AFF-1) и сразу пересчитать затухание по часам (AFF-4: пока «спала»)."""
        now = self.clock()
        raw = None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        self.state = dom.State.from_dict(raw.get("state") if isinstance(raw, dict) else None, self.params, now)
        self.history.clear()
        for row in (raw.get("history") if isinstance(raw, dict) else None) or []:
            if isinstance(row, list) and len(row) == 4 and now - row[0] < HISTORY_S:
                self.history.append(row)
        self.state = dom.decay(self.state, self.params, now)

    def save(self) -> None:
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            data = {"state": self.state.to_dict(), "history": list(self.history)}
            self.path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            self._saved_at = self.clock()
        except OSError as e:
            log.warning("affect.json не записан: %s", e)

    def stop(self) -> None:
        self._refresh()
        self.save()

    # ── обновление ──────────────────────────────────────────────────────────

    def _refresh(self) -> dom.State:
        now = self.clock()
        self.state = dom.decay(self.state, self.params, now)
        return self.state

    def _record(self, force: bool = False) -> None:
        now = self.state.ts
        while self.history and now - self.history[0][0] > HISTORY_S:
            self.history.popleft()
        if force or not self.history or now - self.history[-1][0] >= SAMPLE_EVERY_S:
            self.history.append([round(now, 1), round(self.state.valence, 3), round(self.state.mood, 3),
                                 round(self.state.arousal, 3)])

    def appraise(self, label: str, intensity, category: str, *, valence_push: float = 0.0, reason: str = "") -> None:
        now = self.clock()
        self.state, warns = dom.appraise(self.state, self.params, now, label=label, intensity=intensity,
                                         category=category, valence_push=valence_push, reason=reason)
        for w in warns:
            log.warning("%s", w)
        self.warnings = (self.warnings + warns)[-5:]
        self._record(force=bool(warns) or abs(valence_push) > 0)
        if now - self._saved_at >= SAVE_EVERY_S:
            self.save()
        self._notify()

    def on_emotion(self, event_name: str, res: dict) -> None:
        """Из `fast_brain.publish`: эмоция события с интенсивностью 0..3. Не бросает наружу."""
        try:
            cat = category_of(event_name)
            if cat is None:
                return
            delta = res.get("delta")
            push = max(-10.0, min(10.0, float(delta))) / 10.0 if isinstance(delta, (int, float)) and not isinstance(delta, bool) else 0.0
            self.appraise(str(res.get("emotion") or "idle"), res.get("intensity", 1), cat, reason=str(event_name))
            if push:
                self.appraise("idle", 0, "relation", valence_push=push, reason="отношения с игроком")
        except Exception as e:
            log.warning("оценка эмоции: %s", e)

    def on_mistake(self, kind: str) -> None:
        try:
            self.appraise("sad", 1, "mistake", reason=f"ошибка: {kind}")
        except Exception as e:
            log.warning("оценка ошибки: %s", e)

    def reply_intensity(self, label: str) -> int:
        """Интенсивность эмоции реплики 0..3 из состояния: сила подписи среди быстрых эмоций, иначе от возбуждения."""
        st = self._refresh()
        power = st.emotions.get(label)
        if power is None:
            power = max(0.0, st.arousal - self.params.baseline_arousal) * 0.5
        return max(0, min(dom.INTENSITY_MAX, round(power * dom.INTENSITY_MAX)))

    # ── выдача ──────────────────────────────────────────────────────────────

    def mood_block(self) -> str:
        return dom.mood_fact(self._refresh(), self.params, self.clock())

    def emotion_label(self) -> str:
        return dom.dominant(self._refresh())

    def tone(self) -> dict:
        return dom.voice_tone(self._refresh(), self.params)

    def proactivity_offset(self) -> float:
        return dom.proactivity_offset(self._refresh(), self.params)

    def presence_fields(self) -> dict:
        st = self._refresh()
        return {"valence": round(st.valence, 3), "arousal": round(st.arousal, 3), "mood": round(st.mood, 3),
                "mood_emotion": dom.dominant(st)}

    def snapshot(self, with_history: bool = False) -> dict:
        st = self._refresh()
        out = {**self.presence_fields(), "mood_word": dom.mood_word(st), "emotions": {k: round(v, 3) for k, v in st.emotions.items()},
               "reason": st.reason, "tone": self.tone(), "proactivity_offset": self.proactivity_offset(),
               "baseline": {"valence": self.params.baseline_valence, "arousal": self.params.baseline_arousal},
               "warnings": list(self.warnings), "ts": st.ts}
        if with_history:
            self._record()
            out["history"] = list(self.history)
        return out

    def _notify(self) -> None:
        if self.on_change is not None:
            try:
                self.on_change(self.presence_fields())
            except Exception as e:
                log.warning("рассылка настроения: %s", e)

    # ── управление ──────────────────────────────────────────────────────────

    def reset(self) -> None:
        """Кнопка сброса в дашборде: базовый уровень, история очищается."""
        self.state = dom.initial(self.params, self.clock())
        self.history.clear()
        self.warnings = []
        self._record(force=True)
        self.save()
        self._notify()

    def switch(self, folder: Path, params: dom.Params) -> None:
        """Переключение персонажа (AFF-8): сохранить своё состояние, загрузить состояние нового."""
        self.stop()
        self.folder, self.params = Path(folder), params
        self.warnings = []
        self.load()
        self._notify()

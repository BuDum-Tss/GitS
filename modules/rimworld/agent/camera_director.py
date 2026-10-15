"""Режиссёр камеры: решает, на что смотреть, чтобы в видео попадало важное. Плавность и слежение делает мод (CameraDirector.cs).

Правила, от важного к обычному:
  1. событие (гибель, налёт, пожар, раненый колонист): крупный кадр на место события, держится несколько секунд;
  2. бой (на карте враги): кадр на середину схватки, масштаб по разбросу участников;
  3. обычное время: колонист за делом (следим за ним), время от времени общий план колонии.
Приказы самого агента (колонист, клетка стройки) мод выделяет сам в режиме auto, они важнее кадров из пункта 3.
Модуль чистый: `update` и `on_event` возвращают список команд камеры, отправляет их контроллер.
"""
import random
import time

SHOT_EVERY_S = 14          # как часто менять кадр в обычное время
WIDE_EVERY = 4             # каждый N-й кадр: общий план
FIGHT_EVERY_S = 4          # как часто пересчитывать кадр боя
_BORING_JOBS = {None, "", "Wait_Wander", "GotoWander", "Wait", "Wait_MaintainPosture", "Wait_SafeTemperature"}


def _xz(obj: dict | None) -> tuple[float, float] | None:
    pos = (obj or {}).get("position") or {}
    return (float(pos["x"]), float(pos["z"])) if "x" in pos and "z" in pos else None


def _center(points: list[tuple[float, float]]) -> tuple[float, float, float]:
    xs, zs = [p[0] for p in points], [p[1] for p in points]
    spread = max(max(xs) - min(xs), max(zs) - min(zs))
    return sum(xs) / len(xs), sum(zs) / len(zs), spread


class CameraDirector:
    def __init__(self, rng: random.Random | None = None):
        self._rng = rng or random.Random()
        self._shots = 0
        self._next_shot = 0.0
        self._next_fight = 0.0
        self._last_pos: dict[str, tuple[float, float]] = {}    # где колонист был в последний раз (нужно, когда он погиб)
        self.mode_sent = False

    def reset(self):
        """Новый забег или перезапуск игры: режим камеры в моде нужно задать заново."""
        self.mode_sent = False
        self._next_shot = 0.0

    def _goto(self, x, z, size, hold, smooth=0.8, soft=False) -> dict:
        return {"type": "camera_goto", "x": round(x), "z": round(z), "size": size, "hold": hold, "smooth": smooth,
                "soft": soft}

    def on_event(self, evt: dict, state: dict, now: float | None = None) -> list[dict]:
        now = time.time() if now is None else now
        kind, d = evt.get("type"), evt.get("data") or {}
        cmds: list[dict] = []
        if kind == "pawn_died":
            at = self._last_pos.get(d.get("pawn_id"))
            if at:
                cmds.append(self._goto(*at, 10, 7, 0.6))
        elif kind == "fire_started" and "x" in d:
            cmds.append(self._goto(d["x"], d["z"], 12, 6, 0.7))
        elif kind == "pawn_downed" and d.get("pawn_id"):
            cmds.append({"type": "camera_track", "pawn_id": d["pawn_id"], "size": 11, "seconds": 7, "smooth": 0.5,
                         "soft": False})
        elif kind == "incident":
            cmds += self._fight_shot(state, hold=8)
        if cmds:
            self._next_shot = now + SHOT_EVERY_S
        return cmds

    def _fight_shot(self, state: dict, hold: float = 5) -> list[dict]:
        pts = [p for p in (_xz(t) for t in state.get("threats") or []) if p]
        if not pts:
            return []
        pts += [p for p in (_xz(c) for c in (state.get("colony") or {}).get("pawns", []) if c.get("is_drafted")) if p]
        x, z, spread = _center(pts)
        return [self._goto(x, z, max(14, min(34, 10 + spread * 0.6)), hold, 1.0)]

    def update(self, state: dict, now: float | None = None) -> list[dict]:
        """Вызывается каждый тик контроллера. Возвращает команды камеры (часто пустой список)."""
        now = time.time() if now is None else now
        cmds: list[dict] = []
        if not self.mode_sent:
            cmds.append({"type": "camera_mode", "mode": "auto"})
            self.mode_sent = True
        pawns = (state.get("colony") or {}).get("pawns") or []
        for p in pawns:
            at = _xz(p)
            if at and p.get("id"):
                self._last_pos[p["id"]] = at
        if state.get("threats"):
            if now >= self._next_fight:
                self._next_fight = now + FIGHT_EVERY_S
                cmds += self._fight_shot(state)
                self._next_shot = now + SHOT_EVERY_S
            return cmds
        if now < self._next_shot:
            return cmds
        self._next_shot = now + SHOT_EVERY_S
        self._shots += 1
        points = [p for p in (_xz(c) for c in pawns) if p]
        busy = [p for p in pawns if p.get("current_job") not in _BORING_JOBS and not p.get("is_downed") and _xz(p)]
        if points and (self._shots % WIDE_EVERY == 0 or not busy):
            x, z, spread = _center(points)
            cmds.append(self._goto(x, z, max(24, min(36, 18 + spread * 0.5)), SHOT_EVERY_S - 1, 1.8, soft=True))
        elif busy:
            who = self._rng.choice(busy)
            cmds.append({"type": "camera_track", "pawn_id": who["id"], "size": 13, "seconds": SHOT_EVERY_S - 1,
                         "smooth": 0.9, "soft": True})
        return cmds

"""Наблюдатель простоя: сколько колонисты подряд бродят или ждут без дела.

Агент, которому нечего делать, склонен ждать. Наблюдатель на каждом игровом тике получает снимок (`statediff.snapshot`) и текущее
время (часы подставляются снаружи) и говорит, когда пора напомнить ему о простаивающих колонистах: событие `rimworld.idle_colonists`.
Не считается простоем: игра на паузе, угроза или бой на карте, мобилизованный, лежащий или пленный колонист.
"""
import statediff

IDLE_GRACE_S = 45        # столько секунд подряд колонист без дела, прежде чем считать его простаивающим (настройка idle_grace_s)
IDLE_REPEAT_S = 90       # не чаще этого повторяем событие, пока простой продолжается
_WAIT_JOBS = {"Wait", "Wait_MaintainPosture", "Wait_SafeTemperature"}


def _label(job: str | None) -> str:
    return "ждёт" if job in _WAIT_JOBS else "бродит"


def _duration(seconds: float) -> str:
    return f"{round(seconds / 60)} мин" if seconds >= 90 else f"{max(1, round(seconds))} с"


class IdleWatch:
    def __init__(self, grace_s: float = IDLE_GRACE_S, repeat_s: float = IDLE_REPEAT_S):
        self.grace_s = grace_s
        self.repeat_s = repeat_s
        self._since: dict = {}          # id колониста -> с какого момента он простаивает подряд
        self._reported: set = set()     # о ком уже сообщали в текущей серии
        self._last_emit = 0.0

    def reset(self) -> None:
        self._since.clear()
        self._reported.clear()
        self._last_emit = 0.0

    def update(self, snap: dict, now: float, grace_s: float | None = None) -> dict | None:
        """Один тик. Возвращает {"text", "colonists": [{id, name, job, idle_s}]}, когда событие пора слать, иначе None."""
        grace = self.grace_s if grace_s is None else grace_s
        pawns = snap.get("pawns") or {}
        if snap.get("paused") or snap.get("threats") or any(p["job"] in statediff._FIGHT_JOBS for p in pawns.values()):
            self.reset()                # пауза и бой рвут «подряд»: после них отсчёт заново
            return None
        idle = {pid for pid, p in pawns.items() if p["job"] in statediff.IDLE_JOBS and not p["drafted"]
                and not p["downed"] and not p["prisoner"]}
        for pid in list(self._since):
            if pid not in idle:
                del self._since[pid]
        for pid in idle:
            self._since.setdefault(pid, now)
        if not self._since:
            self.reset()                # все заняты: серия кончилась
            return None
        ready = [pid for pid in self._since if now - self._since[pid] >= grace]
        self._reported &= set(ready)
        if not ready:
            return None
        if not (set(ready) - self._reported) and now - self._last_emit < self.repeat_s:
            return None
        self._reported = set(ready)
        self._last_emit = now
        colonists = [{"id": pid, "name": pawns[pid]["name"], "job": pawns[pid]["job"], "idle_s": round(now - self._since[pid])}
                     for pid in sorted(ready, key=lambda i: (self._since[i], str(i)))]
        who = ", ".join(f"{c['name']} ({_label(c['job'])}, {_duration(c['idle_s'])})" for c in colonists)
        return {"text": f"Простаивают колонисты: {who}. Игра идёт, дела у них нет.", "colonists": colonists}

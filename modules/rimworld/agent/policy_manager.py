"""Машина состояний политик игрового агента (ТЗ 11e, POL-4, POL-5, POL-11, POL-14).

Что решает. Вход: событие (вид, важность) от мира, просьба модели (`switch_policy`), просьба ядра (`policy.activate`,
`focus.release`), условие выхода (`exit`). Выход: действующая политика с причиной. Правила:
  - приоритет: политика с большим приоритетом прерывает меньший (бой 4 > ручное 3 > планирование 2 > наблюдение 1 > вне игры 0);
  - дребезг: политика с меньшим или равным приоритетом сменяется, только когда прошло `min_dwell_s` с её начала;
  - просьба модели идёт только по `switch_to` исходной политики; просьба ядра выводит из `afk` в любую активную политику;
  - в `afk` игровые события ничего не включают (игровых контуров нет), их отдают ядру (см. контроллер);
  - каждый переход пишется в журнал (через `log`) и в историю, которую читает дашборд.

Просьбы модели идут файлом `memory/policy_request.json` (пишет MCP-сервер, отдельный процесс; читает контроллер), как просьбы
о режиме в `game_modes.py`."""
from __future__ import annotations

import json
import time
from collections import deque
from pathlib import Path
from typing import Callable

import policies

REQUEST = Path(__file__).parent / "memory" / "policy_request.json"
REQUEST_MAX_AGE_S = 900
HISTORY = 12


def _no_log(kind: str, text: str) -> None:
    return None


class PolicyManager:
    def __init__(self, registry: policies.Registry | None = None, *, clock: Callable[[], float] = time.time,
                 log: Callable[[str, str], None] = _no_log):
        self.reg = registry or policies.get()
        self._clock = clock
        self._log = log
        self.current: str = self.reg.default
        self.since: float = clock()
        self.reason: str = "по умолчанию: вне игры"
        self.history: deque = deque(maxlen=HISTORY)

    @property
    def policy(self) -> policies.Policy:
        return self.reg.policies[self.current]

    @property
    def in_focus(self) -> bool:
        """Модуль в фокусе: действует политика с активным фокусом (не afk)."""
        return self.policy.focus == "active"

    def dwell_left_s(self, now: float | None = None) -> float:
        now = self._clock() if now is None else now
        return max(0.0, self.policy.min_dwell_s - (now - self.since))

    def _switch(self, to: str, reason: str, source: str) -> None:
        frm = self.current
        now = self._clock()
        self.history.append({"ts": round(now), "from": frm, "to": to, "reason": reason[:160], "source": source})
        self.current, self.since, self.reason = to, now, reason[:160]
        self._log("game", f"политика: {frm} → {to} ({source}): {reason[:120]}")

    def _may_enter(self, target: policies.Policy) -> bool:
        cur = self.policy
        if target.priority > cur.priority:
            return True                                    # приоритетнее: прерывает сразу, дребезг не действует
        if target.priority == cur.priority:
            return self.dwell_left_s() <= 0
        return False                                       # вниз по приоритету: только условие выхода (finish) или просьба

    def on_event(self, kind: str, importance: int, reason: str = "") -> bool:
        """Событие включает политику по `enter`. True, если политика сменилась. В afk игровые события не включают ничего."""
        if not self.in_focus:
            return False
        target = self.reg.entering(kind, importance)
        if target is None or target.id == self.current or not self._may_enter(target):
            return False
        self._switch(target.id, reason or f"событие: {kind}", "event")
        return True

    def rule_switch(self, to: str, reason: str) -> bool:
        """Правило наблюдения, которое создала модель, переводит в политику `to` (без проверки `switch_to`: правило
        проверено при создании). Приоритет и дребезг те же, что у события."""
        target = self.reg.policy(to)
        if target is None or target.id == self.current or not self.in_focus or not self._may_enter(target):
            return False
        self._switch(to, reason, "rule")
        return True

    def request(self, to: str, reason: str) -> tuple[bool, str]:
        """Просьба модели (switch_policy). (успех, причина отказа или «ok»)."""
        if self.reg.policy(to) is None:
            return False, f"нет политики «{to}». Есть: {', '.join(self.reg.policies)}"
        if to == self.current:
            return False, f"уже действует «{to}»"
        if not self.reg.can_request(self.current, to):
            return False, f"из «{self.current}» нельзя перейти в «{to}» по просьбе (можно: {', '.join(self.policy.switch_to) or 'никуда'})"
        if self.dwell_left_s() > 0:
            return False, f"слишком рано: политика «{self.current}» ещё не отработала {round(self.dwell_left_s())} с"
        self._switch(to, reason or "просьба модели", "request")
        return True, "ok"

    def finish(self, reason: str) -> bool:
        """Условие выхода выполнено (план разложен, угроза ушла): вернуться к `exit.to`."""
        cur = self.policy
        if cur.exit_when == "never" or not cur.exit_to or self.current == cur.exit_to:
            return False
        self._switch(cur.exit_to, reason, "exit")
        return True

    def leave_afk(self, to: str, reason: str, source: str = "core") -> bool:
        """Фокус выдан модулю (`source`: core - policy.activate от ядра; auto и button - правка fix/focus-bootstrap):
        из afk в активную политику `to`. Бой по просьбе не включают."""
        target = self.reg.policy(to)
        if target is None or to not in self.reg.requestable() or to == self.current:
            return False                                      # ядро возвращает только в наблюдение или планирование, не в бой
        self._switch(to, reason or "фокус возвращён", source)
        return True

    def enter_afk(self, reason: str, source: str = "core") -> bool:
        """Фокус забран (`source`: core - focus.release от ядра; auto, button - игра закрыта или кнопка «Отпустить»):
        в afk, игровые контуры не держим."""
        if not self.in_focus:
            return False
        self._switch(self.reg.default, reason or "фокус у другой задачи", source)
        return True

    def coerce_mode(self, mode: str) -> str:
        """Режим инструментов внутри политики: если режима нет в её наборе, первый режим политики."""
        modes = self.reg.modes_of(self.current)
        return mode if mode in modes else modes[0]

    def snapshot(self, now: float | None = None) -> dict:
        now = self._clock() if now is None else now
        return {"id": self.current, "title": self.policy.title, "focus": self.policy.focus, "reason": self.reason,
                "minutes": int((now - self.since) // 60), "dwell_left_s": round(self.dwell_left_s(now)),
                "transitions": list(self.policy.switch_to),
                "history": list(self.history)[-5:]}


# ── просьба модели (файл) ─────────────────────────────────────────────────────────────────────

def write_request(policy: str, reason: str, registry: policies.Registry | None = None, path: Path | None = None) -> str | None:
    """Записать просьбу сменить политику (MCP-сервер). Возвращает текст ошибки или None."""
    path = Path(path) if path is not None else REQUEST
    reg = registry or policies.get()
    if reg.policy(policy) is None:
        return f"нет политики «{policy}». Есть: {', '.join(reg.policies)}"
    if policy not in reg.requestable():
        return f"политику «{policy}» нельзя запросить: её включают только события игры"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"policy": policy, "reason": reason[:200], "ts": time.time()}, ensure_ascii=False),
                        encoding="utf-8")
    except OSError as e:
        return str(e)
    return None


def take_request(path: Path | None = None, max_age: float = REQUEST_MAX_AGE_S) -> dict | None:
    """Забрать (и удалить) свежую просьбу о политике."""
    path = Path(path) if path is not None else REQUEST
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        path.unlink()
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and time.time() - data.get("ts", 0) < max_age and data.get("policy") else None

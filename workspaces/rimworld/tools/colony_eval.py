"""Замер автономной игры RimWorld: python workspaces/rimworld/tools/colony_eval.py [--active-seconds 1800] [--poll 5] [--watch] [--once].

Раз в --poll секунд читает состояние мода (/state), копит АКТИВНОЕ время (игра не на паузе, тики идут) и считает критерии владельца:
здание создано и обустроено, атака отбита, работает не меньше половины, потери не больше одного, срывов нет. Логика в agent/colony_metrics.py.

Подключение: по умолчанию как у модуля (RIMWORLD_API, GITS_RIMWORLD_HOST_URL + GITS_RIMWORLD_HOST_TOKEN, иначе http://localhost:8765).
--api URL задаёт адрес мода, --host-url URL задаёт адрес хостового помощника (запросы идут на <URL>/game-api с токеном из переменной
окружения --host-token-env). Токен нигде не печатается.

Выход: 0 все критерии PASS при набранном активном времени, 1 FAIL, 2 прогон прерван (игра закрылась или не отвечает).
Отчёт: консоль и agent/memory/eval/colony_eval_<время>.json. Игру инструмент не запускает и не закрывает."""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent / "agent")]

import colony_metrics as cm  # noqa: E402

EVAL_DIR = HERE.parent / "agent" / "memory" / "eval"
_LOCAL = ("localhost", "127.0.0.1", "::1", "[::1]", "")


class ModError(Exception):
    """Мод не ответил или ответил ошибкой."""


class ModClient:
    """HTTP-клиент мода: прямой (localhost) или через прокси хостового помощника."""

    def __init__(self, base: str, token: str = "", timeout: float = 6.0):
        self.base = base.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _call(self, path: str, body: dict | None = None, timeout: float | None = None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, headers=self._headers(), method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read().decode("utf-8"))
            except Exception:
                raise ModError(f"HTTP {e.code}")
        except Exception as e:
            raise ModError(str(e))

    def get_state(self) -> dict:
        return self._call("/state")

    def command(self, cmd: dict) -> tuple[bool, str]:
        """(успех, результат или текст ошибки)."""
        data = self._call("/command", cmd, timeout=25)
        if data.get("status") == "ok":
            return True, str(data.get("result", ""))
        return False, str(data.get("message") or data.get("error") or data)


def make_client(args, environ=None) -> ModClient:
    env = os.environ if environ is None else environ
    if args.host_url:
        token = env.get(args.host_token_env, "")
        return ModClient(args.host_url.rstrip("/") + "/game-api", token)
    if args.api:
        host = args.api.split("//", 1)[-1].split("/", 1)[0].rsplit(":", 1)[0]
        token = env.get(args.host_token_env, "") if host not in _LOCAL else ""
        return ModClient(args.api, token)
    api = (env.get("RIMWORLD_API") or "").strip()
    helper = (env.get("GITS_RIMWORLD_HOST_URL") or "").strip().rstrip("/")
    if not api and helper:
        api = helper + "/game-api"
    if api:
        host = api.split("//", 1)[-1].split("/", 1)[0].rsplit(":", 1)[0]
        return ModClient(api, env.get(args.host_token_env, "") if host not in _LOCAL else "")
    return ModClient("http://localhost:8765")


def probe_rooms(client, cells, metrics: cm.ColonyMetrics) -> dict | None:
    """Опросить мод о помещениях в клетках. None: команды room_info в моде нет (включается запасной режим)."""
    rooms = {}
    for xz in cells:
        ok, text = client.command({"type": "room_info", "x": xz[0], "z": xz[1]})
        if not ok:
            if "unknown command" in text.lower():
                metrics.set_room_support(False)
                return None
            continue
        try:
            rooms[xz] = json.loads(text)
        except ValueError:
            continue
    metrics.set_room_support(True)
    return rooms


def inject_raid(client, args, metrics: cm.ColonyMetrics, out) -> bool:
    """Инъекция рейда (тестовое оборудование сценария). True: больше не пытаться."""
    cmd = {"type": "dev_trigger_incident", "incident": "RaidEnemy"}
    if args.raid_points:
        cmd["points"] = args.raid_points
    try:
        ok, text = client.command(cmd)
    except ModError as e:
        ok, text = False, str(e)
    if ok:
        metrics.note_injection(True, True, text)
        out("Рейд инъектирован командой dev_trigger_incident (атака в отчёте помечена как инъекция).")
        return True
    if "unknown command" in text.lower():
        metrics.note_injection(False, False, text)
        out("ПРЕДУПРЕЖДЕНИЕ: мод не знает dev_trigger_incident (нужна пересборка мода), рейд не вызван; замер продолжается.")
        return True
    metrics.note_injection(False, True, text)
    out(f"ПРЕДУПРЕЖДЕНИЕ: рейд не вызван: {text}")
    return len([i for i in metrics.injections if not i["ok"]]) >= 3


def run(cfg: cm.Config, client, *, poll: float = 5.0, once: bool = False, watch: bool = False, raid_at: float | None = None,
        raid_points: float | None = None, stop_on_fail: bool = False, abort_after: float = 180.0, room_every: float = 30.0, max_real: float = 0.0,
        now=time.monotonic, sleep=time.sleep, out=print) -> tuple[int, dict]:
    """Главный цикл. Возвращает (код возврата, отчёт). Часы, сон и вывод подменяются в тестах."""
    metrics = cm.ColonyMetrics(cfg)
    start = now()
    last_ok = start
    next_probe = 0.0
    next_watch = start + 60.0
    inject_done = raid_at is None
    stall_told = False
    args = argparse.Namespace(raid_points=raid_points)
    aborted, reason = False, ""
    while True:
        t = now()
        try:
            state = client.get_state()
        except ModError as e:
            state = None
            metrics.note_miss(t)
            if t - last_ok >= abort_after:
                aborted, reason = True, f"мод не отвечает {t - last_ok:.0f} с ({e})"
        if state is not None:
            rooms = None
            if cm._loaded(state):
                if metrics.want_room_probe() and t >= next_probe:
                    next_probe = t + room_every
                    try:
                        rooms = probe_rooms(client, metrics.pending_probe_cells(state), metrics)
                    except ModError:
                        rooms = None
                        metrics.note_miss(t)
                metrics.update(state, t, rooms)
                last_ok = t
            else:
                metrics.update(state, t)
                if t - last_ok >= abort_after:
                    aborted, reason = True, f"игра не загружена (меню или нет колонии) {t - last_ok:.0f} с"
        if metrics.stall["failed"] and not stall_told:
            stall_told = True
            sl = metrics.stall
            out(f"ПРОВАЛ: игра на паузе {sl['paused_s']:.0f} с и ничего не менялось (на {sl['at_real_s']} с прогона; последнее изменение: {sl['last_change']}).")
        if stall_told and stop_on_fail:
            rep = metrics.report(aborted=False, abort_reason="остановлено по --stop-on-fail", wall_seconds=now() - start)
            rep["exit_code"] = 1
            return 1, rep
        if not inject_done and metrics.active_seconds >= raid_at:
            inject_done = inject_raid(client, args, metrics, out)
        if once:
            out(cm.format_instant(metrics.instant))
            code = 0 if metrics.instant.get("loaded") else 2
            return code, metrics.report(aborted=code == 2, abort_reason="нет снимка состояния" if code == 2 else "", wall_seconds=now() - start)
        if watch and t >= next_watch:
            next_watch = t + 60.0
            out(f"[{metrics.active_seconds:.0f}/{cfg.active_seconds:.0f} с активной игры] " + cm.format_instant(metrics.instant))
            for c in metrics._criteria(False):
                out(f"    {c['status']:<8}{c['title']}: {c['value']}")
        if metrics.done:
            break
        if not aborted and max_real and t - start >= max_real:
            aborted, reason = True, f"истёк лимит реального времени {max_real:.0f} с"
        if aborted:
            break
        sleep(poll)
    rep = metrics.report(aborted=aborted, abort_reason=reason, wall_seconds=now() - start)
    return rep["exit_code"], rep


def save_report(rep: dict, directory: Path = EVAL_DIR, stamp: str | None = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"colony_eval_{stamp or datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps(rep, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def build_parser() -> argparse.ArgumentParser:
    d = cm.Config()
    ap = argparse.ArgumentParser(description="Замер автономной игры RimWorld по критериям владельца")
    ap.add_argument("--poll", type=float, default=5.0, help="период опроса состояния, с")
    ap.add_argument("--active-seconds", type=float, default=d.active_seconds, help="сколько секунд АКТИВНОЙ игры замерять")
    ap.add_argument("--api", default="", help="адрес мода (по умолчанию как у модуля: RIMWORLD_API или http://localhost:8765)")
    ap.add_argument("--host-url", default="", help="адрес хостового помощника (запросы идут на <URL>/game-api)")
    ap.add_argument("--host-token-env", default="GITS_RIMWORLD_HOST_TOKEN", help="имя переменной окружения с токеном помощника")
    ap.add_argument("--once", action="store_true", help="разовый снимок метрик без ожидания (отладка)")
    ap.add_argument("--watch", action="store_true", help="раз в минуту печатать промежуточное состояние")
    ap.add_argument("--raid-at", type=float, default=None, help="через сколько активных секунд вызвать рейд командой dev_trigger_incident")
    ap.add_argument("--raid-points", type=float, default=0, help="сила рейда (0: по умолчанию игры)")
    ap.add_argument("--stall-seconds", type=float, default=d.stall_seconds, help="пауза без изменений колонии дольше стольких секунд: провал (0: выключено)")
    ap.add_argument("--stop-on-fail", action="store_true", help="при провале на зависании сразу завершить с кодом 1")
    ap.add_argument("--abort-after", type=float, default=180.0, help="прервать, если нет состояния столько секунд")
    ap.add_argument("--max-real", type=float, default=0.0, help="предел реального времени прогона, с (0: без предела)")
    ap.add_argument("--room-every", type=float, default=30.0, help="как часто опрашивать помещения командой room_info, с")
    ap.add_argument("--out-dir", default=str(EVAL_DIR), help="куда писать JSON")
    ap.add_argument("--min-beds", type=int, default=None, help="кроватей в помещении (по умолчанию ceil(колонистов/2), не меньше 1)")
    ap.add_argument("--min-furniture", type=int, default=d.min_furniture)
    ap.add_argument("--min-new-structures", type=int, default=d.min_new_structures, help="новых стен и дверей за прогон")
    ap.add_argument("--min-attackers", type=int, default=d.min_attackers)
    ap.add_argument("--clear-polls", type=int, default=d.clear_polls)
    ap.add_argument("--min-repelled", type=int, default=d.min_repelled)
    ap.add_argument("--work-min-share", type=float, default=d.work_min_share)
    ap.add_argument("--work-mean-min", type=float, default=d.work_mean_min)
    ap.add_argument("--work-samples-min", type=float, default=d.work_samples_min)
    ap.add_argument("--strict-work", action="store_true", help="сон и отдых считать неработой")
    ap.add_argument("--max-losses", type=int, default=d.max_losses)
    ap.add_argument("--hp-min", type=float, default=d.health_hp_min)
    ap.add_argument("--mood-min", type=float, default=d.health_mood_min)
    ap.add_argument("--ok-share", type=float, default=d.health_ok_share)
    ap.add_argument("--max-health-violations", type=int, default=d.max_health_violations)
    ap.add_argument("--break-mood", type=float, default=d.break_mood, help="настроение ниже этого считается срывом (приближение)")
    return ap


def config_from_args(a) -> cm.Config:
    return cm.Config(active_seconds=a.active_seconds, max_gap=max(30.0, a.poll * 6), min_beds=a.min_beds, min_furniture=a.min_furniture,
                     min_new_structures=a.min_new_structures, min_attackers=a.min_attackers, clear_polls=a.clear_polls,
                     min_repelled=a.min_repelled, work_min_share=a.work_min_share, work_mean_min=a.work_mean_min,
                     work_samples_min=a.work_samples_min, strict_work=a.strict_work, max_losses=a.max_losses,
                     health_hp_min=a.hp_min, health_mood_min=a.mood_min, health_ok_share=a.ok_share,
                     max_health_violations=a.max_health_violations, break_mood=a.break_mood, stall_seconds=a.stall_seconds)


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    cfg = config_from_args(a)
    client = make_client(a)
    print(f"Замер: {cfg.active_seconds:.0f} с активной игры, опрос раз в {a.poll:g} с, мод {client.base}" + (" (через прокси помощника)" if client.token else ""))
    try:
        code, rep = run(cfg, client, poll=a.poll, once=a.once, watch=a.watch, raid_at=a.raid_at, raid_points=a.raid_points or None, stop_on_fail=a.stop_on_fail,
                        abort_after=a.abort_after, room_every=a.room_every, max_real=a.max_real)
    except KeyboardInterrupt:
        print("Прервано с клавиатуры.")
        return 2
    if not a.once:
        print(cm.format_report(rep))
        print("Отчёт:", save_report(rep, Path(a.out_dir)))
    return code


if __name__ == "__main__":
    sys.exit(main())

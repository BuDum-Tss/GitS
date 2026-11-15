"""Яркие моменты журнала (`memory_service/moments.py`, `moment_selector.py`): оценка событий, пики, склейка, контекст, лимиты,
суточный бюджет, закрытие, отметка с конца журнала, backfill. Журнал и часы: заглушки."""
import sqlite3

from services.memory_service import moments as mom
from services.memory_service.episode_recall import EpisodeRecall
from services.memory_service.episode_store import EpisodeStore
from services.memory_service.moment_selector import MomentSelector

T0 = 1_800_000_000.0
CFG = mom.MomentCfg()


def row(i, ts, kind, text, **data):
    return {"id": i, "ts": ts, "kind": kind, "text": text, "data": data}


def att(i, ts, text, decision, urgency=0, routes=("chat",), salience=0.0, **extra):
    return row(i, ts, "attention", text, decision=decision, urgency=urgency, routes=list(routes), salience=salience, **extra)


STRONG = "запомни, мне нравится физика, экзамен завтра"          # user: 0.15 + 0.35 + 0.2 = 0.7


class Journal:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def after(self, after_id=0, limit=500, kinds=None):
        return [r for r in self.rows if r["id"] > after_id and (not kinds or r["kind"] in kinds)][:limit]

    def last_id(self):
        return self.rows[-1]["id"] if self.rows else 0

    def delete_ids(self, ids):
        return 0


# ── оценка событий ───────────────────────────────────────────────────────────────────────────

def test_noise_is_always_zero_even_with_salience():
    for kind in ("focus", "decision", "trace", "budget_exceeded", "setting", "profile", "error", "vision"):
        assert mom.event_score(row(1, T0, kind, "что-то", salience=0.9)) == (0.0, [])
    assert mom.event_score(row(1, T0, "module_event", "окно", event_kind="desktop.app_changed"))[0] == 0.0
    assert mom.event_score(row(1, T0, "module_event", "активность", event_kind="desktop.activity", salience=0.0))[0] == 0.0


def test_table_weights():
    sc = lambda kind, text="текст", **d: mom.event_score(row(1, T0, kind, text, **d))[0]          # noqa: E731
    assert sc("mistake") == 0.7 and sc("approval") == 0.4 and sc("memory") == 0.3 and sc("say") == 0.1
    assert sc("telegram") == 0.5 and sc("hearing") == 0.2 and sc("game") == 0.3 and sc("game", salience=0.6) == 0.6
    assert sc("task.status", status="done") == 0.5 and sc("task.status", status="failed") == 0.65 and sc("task.status", status="active") == 0
    assert sc("emotion", "happy", intensity=1, bg=False) == mom.EMOTION_MAX
    assert sc("emotion", "thinking", intensity=1, bg=True) == 0 and sc("emotion", "idle", intensity=0, bg=False) == 0
    assert sc("user", "привет") == mom.USER_BASE and sc("неизвестный") == 0


def test_module_event_fallback_table():
    sc = lambda ek: mom.event_score(row(1, T0, "module_event", "событие", event_kind=ek))[0]          # noqa: E731
    assert sc("rimworld.death") == 0.9 and sc("montage.failed") == 0.7 and sc("montage.done") == 0.6
    assert sc("telegram.message") == 0.5 and sc("voice.heard") == 0.35 and sc("rimworld.colonists") == 0


def test_user_text_signs():
    sc = lambda t: mom.event_score(row(1, T0, "user", t))                                           # noqa: E731
    assert sc("ок")[0] == 0.15
    assert sc("а" * 81)[0] == 0.25
    assert sc("запомни это")[0] >= 0.5 and "просьба запомнить" in sc("запомни это")[1]
    for text, why in (("я ненавижу утро", "сильные чувства"), ("большое спасибо", "благодарность"), ("прости меня", "извинение"),
                      ("это неправильно", "критика"), ("меня зовут Аня", "личное")):
        assert why in sc(text)[1], text
    assert sc("запомни, я ненавижу, спасибо, прости, неправильно, меня зовут " + "а" * 90)[0] == 1.0     # не больше 1.0


def test_attention_score_by_verdict():
    d3 = att(1, T0, "Лусти на грани срыва", "D3", urgency=4, routes=["chat"])
    score, why = mom.attention_score(d3)
    assert score >= 0.75 and score >= CFG.threshold and why
    assert mom.attention_score(att(1, T0, "окно", "D1", routes=["ignore"], event_kind="desktop.app_changed"))[0] == 0
    assert mom.attention_score(att(1, T0, "окно", "D0", routes=["ignore"], salience=0.9))[0] == 0     # ignore + D0/D1: не пик
    assert mom.attention_score(att(1, T0, "x", "D2", routes=["speech"]))[0] == 0.45 < CFG.threshold
    assert mom.attention_score(att(1, T0, "x", "ANNOUNCE", routes=["speech"]))[0] == 0.4
    assert mom.attention_score(att(1, T0, "x", "D2", routes=["speech"], salience=0.6))[0] == 0.6        # не меньше salience внимания
    assert mom.attention_score(att(1, T0, "x", "D4", urgency=5, routes=["think"]))[0] == 1.0             # обрезка до 1.0
    assert mom.attention_score(row(1, T0, "attention", "без решения"))[0] == 0


# ── моменты ──────────────────────────────────────────────────────────────────────────────────

def test_close_peaks_merge_and_far_ones_do_not():
    rows = [row(1, T0, "user", STRONG), row(2, T0 + 100, "user", STRONG), row(3, T0 + 1000, "user", STRONG)]
    ms = mom.select_moments(rows, CFG, T0 + 5000)
    assert len(ms) == 2 and ms[0].peaks == [1, 2] and ms[1].peaks == [3]
    assert ms[0].id == "m1-2" and ms[0].salience == 0.7 and ms[0].closed


def test_context_takes_all_streams_and_scene_as_background():
    rows = [row(1, T0 - 900, "vision", "Пользователь смотрит видео"), row(2, T0 - 700, "module_event", "Окно: браузер",
                                                                         event_kind="desktop.app_changed"),
            row(3, T0 - 600, "vision", "Пользователь читает чат"), row(4, T0 - 100, "game", "изменение: пауза"),
            row(5, T0 - 50, "telegram", "пришло сообщение"), row(6, T0 - 40, "focus", "шум", focus_type="switch"),
            row(7, T0, "user", STRONG), row(8, T0 + 30, "say", "хорошо"), row(9, T0 + 200, "user", "это уже вне окна")]
    (m,) = mom.select_moments(rows, CFG, T0 + 5000)
    ids = [e.id for e in m.events]
    assert ids == [2, 3, 4, 5, 7, 8]                                  # сцена (две последние), игра и телеграм, фокус и поздняя реплика нет
    assert [e.id for e in m.events if e.scene] == [2, 3] and m.peak_id == 7


def test_attention_event_can_be_a_peak_and_its_module_event_is_not_doubled():
    text = "Монтаж: черновик отрендерен"
    rows = [row(1, T0, "module_event", text, module="montage", event_kind="montage.done"), att(2, T0 + 1, text, "D3", urgency=4)]
    (m,) = mom.select_moments(rows, CFG, T0 + 5000)
    assert [e.id for e in m.events] == [2] and m.peak_id == 2
    rows = rows[:1]                                                    # без записи внимания работает запасная таблица
    (m,) = mom.select_moments(rows, CFG, T0 + 5000)
    assert m.peak_id == 1 and m.salience == 0.6


def test_ignored_attention_never_makes_a_moment():
    rows = [att(i, T0 + i, "окно сменилось", "D1", routes=["ignore"], event_kind="desktop.app_changed") for i in range(1, 6)]
    assert mom.select_moments(rows, CFG, T0 + 5000) == []


def test_limits_keep_events_closest_to_the_brightest_peak():
    rows = [row(i, T0 - 200 + i, "say", f"реплика {i}") for i in range(1, 41)] + [row(100, T0, "user", STRONG)]
    (m,) = mom.select_moments(rows, mom.MomentCfg(max_events=7), T0 + 5000)
    assert len(m.events) == 7 and 100 in [e.id for e in m.events] and {e.id for e in m.events} >= {37, 38, 39, 40}
    (m,) = mom.select_moments(rows, mom.MomentCfg(max_tokens=60), T0 + 5000)
    assert 100 in [e.id for e in m.events] and sum(len(e.text) // 3 + 9 for e in m.events) <= 60 + 40


def test_per_day_budget_keeps_the_brightest_closed_moments():
    rows = []
    for k in range(5):
        rows.append(row(k + 1, T0 + k * 3600, "user", STRONG if k != 3 else "запомни, мне нравится это, я ненавижу, спасибо"))
    ms = mom.select_moments(rows, mom.MomentCfg(per_day=2), T0 + 86400)
    assert len(ms) == 2 and 4 in [m.peak_id for m in ms]              # самый яркий остался
    assert len(mom.select_moments(rows, mom.MomentCfg(per_day=2), T0 + 86400, taken={mom.day_of(T0): 1})) == 1


def test_moment_waits_until_after_s_and_instant_peak_closes_fast():
    rows = [row(1, T0, "user", STRONG)]
    assert not mom.select_moments(rows, CFG, T0 + 100)[0].closed
    assert not mom.select_moments(rows, CFG, T0 + 200)[0].closed        # окно вышло, но пики ещё могут склеиться (merge_s)
    assert mom.select_moments(rows, CFG, T0 + 400)[0].closed
    assert mom.select_moments(rows, CFG, T0 + 1, final=True)[0].closed
    bright = [att(1, T0, "Колонист погиб", "D4", urgency=5, routes=["think"])]
    assert not mom.select_moments(bright, CFG, T0 + 10)[0].closed
    assert mom.select_moments(bright, CFG, T0 + 25)[0].closed          # instant_after_s = 20


def test_cfg_from_settings_ignores_junk():
    cfg = mom.MomentCfg.from_settings({"threshold": 0.6, "per_day": "много", "merge_s": -5, "max_events": 12})
    assert cfg.threshold == 0.6 and cfg.per_day == 10 and cfg.merge_s == 300.0 and cfg.max_events == 12
    assert mom.MomentCfg.from_settings(None) == CFG


# ── селектор ─────────────────────────────────────────────────────────────────────────────────

def make_selector(tmp_path, rows, now, **kw):
    store = EpisodeStore(tmp_path / "episodes.db")
    j = Journal(rows)
    return j, store, MomentSelector(source=j, store=store, settings=lambda: kw.pop("settings", {}), clock=lambda: now,
                                    character=lambda: "glados", **kw)


async def test_selector_without_mark_starts_at_the_end_of_journal(tmp_path):
    rows = [row(i, T0 + i, "user", STRONG) for i in range(1, 4)]
    j, store, sel = make_selector(tmp_path, rows, T0 + 9999)
    assert (await sel.run()) == {"episodes": 0, "calm": 0, "upto": 3} and store.get_meta("moments_upto") == 3
    j.rows.append(row(4, T0 + 5000, "user", STRONG))
    sel.clock = lambda: T0 + 9999
    assert (await sel.run())["episodes"] == 1                          # новое после отметки обрабатывается


async def test_selector_does_not_duplicate_and_keeps_peak_unique(tmp_path):
    rows = [att(5, T0, "Колонист погиб", "D4", urgency=5, routes=["think"])]
    j, store, sel = make_selector(tmp_path, rows, T0 + 30, first_upto=lambda: 0)
    assert (await sel.run())["episodes"] == 1                          # instant: закрыт через 20 с
    j.rows.append(row(6, T0 + 60, "say", "позже"))                      # контекст вырос: id момента был бы другим
    sel.clock = lambda: T0 + 700
    assert (await sel.run())["episodes"] == 0 and len(store.select()) == 1
    ep = store.select()[0]
    assert ep["stream"] == "moment" and ep["status"] == "pending" and ep["peak_id"] == 5 and ep["module"] == ""


async def test_backfill_takes_top_k_and_does_not_move_the_mark(tmp_path):
    rows = [row(1, T0 - 10 * 86400, "user", STRONG)]                    # старше since_ts
    for k in range(4):
        rows.append(row(10 + k, T0 + k * 3600, "user", STRONG))
    rows.append(row(20, T0 + 5 * 3600, "mistake", "инструмент упал"))      # 0.7: так же ярко
    rows.append(att(30, T0 + 8 * 3600, "Колонист погиб", "D4", urgency=5, routes=["think"]))   # самый яркий
    j, store, sel = make_selector(tmp_path, rows, T0 + 86400)
    store.set_meta("moments_upto", 77)
    assert await sel.backfill(T0 - 3600, 3) == 3
    eps = store.select(oldest_first=True)
    assert len(eps) == 3 and 30 in [e["peak_id"] for e in eps] and 1 not in [e["first_id"] for e in eps]
    assert store.get_meta("moments_upto") == 77
    assert await sel.backfill(T0 - 3600, 3) == 0                        # повтор не дублирует


# ── хранилище и чтение ───────────────────────────────────────────────────────────────────────

def test_old_database_gets_new_columns(tmp_path):
    path = tmp_path / "episodes.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE episodes (id TEXT PRIMARY KEY, t_start REAL NOT NULL, t_end REAL NOT NULL, first_id INTEGER NOT NULL,"
               " last_id INTEGER NOT NULL, stream TEXT NOT NULL DEFAULT '', module TEXT NOT NULL DEFAULT '',"
               " participants TEXT NOT NULL DEFAULT '[]', peak_id INTEGER, salience REAL NOT NULL DEFAULT 0,"
               " emotions TEXT NOT NULL DEFAULT '[]', title TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '',"
               " outcome TEXT NOT NULL DEFAULT '', quotes TEXT NOT NULL DEFAULT '[]', facts TEXT NOT NULL DEFAULT '[]',"
               " related TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'pending', version INTEGER NOT NULL DEFAULT 0,"
               " character TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',"
               " facts_done INTEGER NOT NULL DEFAULT 0, notified INTEGER NOT NULL DEFAULT 0, compressed INTEGER NOT NULL DEFAULT 0,"
               " raw_deleted INTEGER NOT NULL DEFAULT 0, day TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL)")
    db.execute("INSERT INTO episodes (id, t_start, t_end, first_id, last_id, created, updated) VALUES ('e1-2', 1, 2, 1, 2, 1, 1)")
    db.commit()
    db.close()
    store = EpisodeStore(path)
    old = store.get("e1-2")
    assert old["attitude"] == "" and old["noticed"] == []
    store.update("e1-2", summary="рассказ", attitude="мне это важно", noticed=["деталь"])
    got = store.get("e1-2")
    assert got["attitude"] == "мне это важно" and got["noticed"] == ["деталь"] and got["version"] == 1
    EpisodeStore(path)                                                  # повторное открытие не ломается


async def test_recall_gives_attitude_and_noticed(tmp_path):
    store = EpisodeStore(tmp_path / "episodes.db")
    store.add_episode({"id": "m1-2", "t_start": T0, "t_end": T0 + 1, "first_id": 1, "last_id": 2, "day": "2026-01-01"},
                      [{"id": 1, "ts": T0, "kind": "user", "text": "привет"}])
    store.update("m1-2", title="Встреча", summary="Я встретила Аню.", attitude="Мне было приятно.", noticed=["она улыбалась"])
    rec = EpisodeRecall(store=store, search=None)
    ep = await rec.expand("m1-2")
    assert ep["attitude"] == "Мне было приятно." and ep["noticed"] == ["она улыбалась"]
    text = await rec.recall_episode("m1-2")
    assert "Мне было приятно." in text and "она улыбалась" in text
    assert (await rec.listing())[0]["attitude"] == "Мне было приятно."

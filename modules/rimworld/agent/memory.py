"""Persistent memory for GLaDOS across sessions."""
import json
from datetime import datetime
from pathlib import Path


class Memory:
    def __init__(self, memory_dir: str = "./memory"):
        self._dir = Path(memory_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._experiment_log = self._dir / "experiment_log.json"
        self._lessons        = self._dir / "lessons.json"
        self._pawn_records   = self._dir / "pawn_records.json"
        self._current_run    = self._dir / "current_run.json"

    def _load(self, path: Path, default):
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        return default

    def _save(self, path: Path, data):
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    # Current run ----------------------------------------------------------

    def start_run(self, colony_name: str, save_id: str = ""):
        run = {
            "id": f"colony_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
            "colony_name": colony_name,
            "save_id": save_id,
            "started": datetime.now().isoformat(),
            "key_decisions": [],
            "pawn_fates": {}
        }
        self._save(self._current_run, run)
        return run

    def get_current_run(self) -> dict | None:
        return self._load(self._current_run, None)

    def log_decision(self, tick: int, directive: str, actions: list[str], event_type: str = "proactive"):
        run = self.get_current_run()
        if not run:
            return
        run["key_decisions"].append({
            "tick": tick,
            "event": event_type,
            "directive": directive[:300],
            "actions": actions[:10]
        })
        # Keep only last 50 decisions in current run file
        run["key_decisions"] = run["key_decisions"][-50:]
        self._save(self._current_run, run)

    def record_pawn_fate(self, pawn_id: str, name: str, fate: str, cause: str = ""):
        run = self.get_current_run()
        if not run:
            return
        run["pawn_fates"][pawn_id] = {"name": name, "fate": fate, "cause": cause}
        self._save(self._current_run, run)

    def end_run(self, outcome: str, notes: str = ""):
        run = self.get_current_run()
        if not run:
            return
        run["ended"] = datetime.now().isoformat()
        run["outcome"] = outcome
        run["notes"] = notes

        log = self._load(self._experiment_log, {"colony_runs": []})
        log["colony_runs"].append(run)
        self._save(self._experiment_log, log)
        self._current_run.unlink(missing_ok=True)

    # Idle comment (вне активного забега — главное меню и т.п.) ------------

    def set_idle_comment(self, text: str):
        self._save(self._dir / "idle_comment.json",
                  {"text": text, "ts": datetime.now().isoformat()})

    def get_idle_comment(self) -> dict | None:
        return self._load(self._dir / "idle_comment.json", None)

    # Lessons --------------------------------------------------------------

    def add_lesson(self, lesson: str):
        lessons = self._load(self._lessons, [])
        if lesson not in lessons:
            lessons.append(lesson)
            self._save(self._lessons, lessons)

    def get_lessons(self) -> list[str]:
        return self._load(self._lessons, [])

    # Pawn records (cross-run) ---------------------------------------------

    def upsert_pawn(self, pawn_id: str, data: dict):
        records = self._load(self._pawn_records, {})
        records[pawn_id] = {**records.get(pawn_id, {}), **data}
        self._save(self._pawn_records, records)

    def get_pawn(self, pawn_id: str) -> dict | None:
        return self._load(self._pawn_records, {}).get(pawn_id)

    # Context for LLM prompt -----------------------------------------------

    def build_context_block(self) -> str:
        lines = ["=== GLaDOS EXPERIMENT ARCHIVE ==="]

        run = self.get_current_run()
        if run:
            lines.append(f"\nCurrent experiment: {run['id']} — {run['colony_name']}")
            lines.append(f"Started: {run['started']}")
            recent = run["key_decisions"][-6:]
            if recent:
                lines.append("\nRecent decisions (last 10):")
                for d in recent:
                    if d["directive"] and d["directive"] != "(молчание)":
                        lines.append(f"  [tick {d['tick']}] {d['event']}: {d['directive'][:120]}")

        lessons = self.get_lessons()
        if lessons:
            lines.append("\nLessons learned from previous experiments:")
            for i, l in enumerate(lessons[-15:], 1):
                lines.append(f"  {i}. {l}")

        log = self._load(self._experiment_log, {"colony_runs": []})
        past_runs = log["colony_runs"][-5:]
        if past_runs:
            lines.append(f"\nPast experiments ({len(log['colony_runs'])} total):")
            for r in past_runs:
                outcome = r.get("outcome", "unknown")
                notes   = r.get("notes", "")[:100]
                lines.append(f"  [{r['id']}] {r.get('colony_name','?')} — {outcome}. {notes}")

        return "\n".join(lines)

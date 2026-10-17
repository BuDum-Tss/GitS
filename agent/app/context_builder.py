"""ContextBuilder: контекст ответа (ТЗ 29, ARC-5). Пока в точности прежняя склейка шести источников:
память и диалог, справка о возможностях, услышанное, воспоминания о себе, факты графа, отношение к игроку.
Состояние (`_graph_block`, `_self_block`, `_heard`) живёт на фасаде `Companion`.
"""
import asyncio
import logging
import time

from app.memory_capture import SPEAKER_TAG

logger = logging.getLogger("companion")

PREFETCH_TTL_S = 600.0     # найденное с опозданием годится для следующей реплики не дольше этого (ТЗ 25, FAST-6)


class ContextBuilder:
    def __init__(self, host):
        self._h = host
        self.mood_of = None        # ТЗ 26: callable() -> факт о настроении; ядро ставит AffectService.mood_block

    def claude_note(self) -> str:
        import claude_quota
        return (f"Claude (игровые ходы, глубокий анализ, помощник) сейчас недоступен: исчерпан лимит до "
                f"{claude_quota.until_text()}. Если просят то, что требует Claude, честно скажи это.") if claude_quota.blocked() else ""

    def capabilities(self) -> str:
        """Честный список того, что у неё сейчас включено: без него она выдумывает возможности."""
        h = self._h
        cfg = h._settings.load()
        lines = ["Справка о твоих возможностях и их состоянии (для тебя, а не тема разговора: упоминай их, только если спросили; говори о них только по этим данным):",
                 f"- Зрение: {'включено' if cfg['vision'] else 'выключено'}.",
                 f"- Слух (системный звук): {'включён' if cfg['hearing'] else 'выключен'}.",
                 f"- Голос: {'заглушён' if cfg['voice_muted'] else 'включён'}."]
        lines += ["- " + note for note in h.module_notes()]
        lines.append("- Помощник Claude (Клод, Claude Code): ты можешь поручить ему сложные задачи на компьютере: "
                     "установку программ и библиотек, файлы, код, команды, интернет, анализ. Он сам спрашивает "
                     "разрешение на каждое изменение. Просьбы, которые тебе не по силам, уходят к нему.")
        status = h._memory_reader.graph_status()
        if status is not None:
            lines.append("- " + status)
        note = h._claude_note()
        if note:
            lines.append("- " + note)
        return "\n".join(lines)

    def _settings_context(self) -> dict:
        try:
            cfg = self._h._settings.load().get("context")
        except Exception:
            cfg = None
        return cfg if isinstance(cfg, dict) else {}

    def build(self, include_hearing: bool = True, query: str = "", needs_memory: str | None = None,
              role: str = "reply", mode: str | None = None) -> str:
        """query: о чём сейчас речь. По нему из долговременной памяти берутся подходящие записи.

        mode (настройка `context.mode`, по умолчанию `push`): `push` кладёт память в контекст сам, но пропускает её,
        если классификатор сказал `needs_memory == "no"`; `pull` не кладёт запрошенную память вовсе, её достаёт
        инструмент `recall`. Бюджет (`context.budget_tokens[role]`, блок -> токены, ключ `total` на весь контекст):
        без бюджета контекст прежний. Оценка токена: 3 символа."""
        h = self._h
        cfg = self._settings_context()
        mode = mode or cfg.get("mode") or "push"
        skip_memory = mode == "pull" or needs_memory == "no"
        budget = (cfg.get("budget_tokens") or {}).get(role) or {}
        blocks: list[tuple[str, str]] = []
        blocks += [
            ("dialogue", h._memory_reader.context_block(dialogue=12)),
            ("profile", getattr(h._memory_reader, "profile_block", lambda include_owner=True: "")(not SPEAKER_TAG.match(query or ""))),   # главное о владельце (если говорит он) и её выводы о себе: всегда, вне темы
            ("capabilities", h._capabilities())]
        if include_hearing and h._settings.load()["hearing"]:
            heard = h.recent_heard()
            if heard:
                blocks.append(("heard", "Что недавно было слышно из колонок (краткая сводка автоматической расшифровки, "
                               "возможны ошибки; это может быть фильм, видео, музыка, не слова "
                               "пользователя): " + heard))
        if h._self_block and not skip_memory:
            blocks.append(("self", h._self_block))
        if h._graph_block and not skip_memory:
            blocks.append(("graph", h._graph_block))
        if self.mood_of is not None:
            try:
                mood = self.mood_of()
            except Exception:
                mood = ""
            if mood:
                blocks.append(("mood", mood))
        rel = h._people.describe("игрок")
        if rel:
            blocks.append(("relation", f"Твоё {rel}."))
        parts = [self._fit(text, budget.get(name)) for name, text in blocks]
        parts = [p for p in parts if p]
        text = "\n".join(parts)
        return self._fit(text, budget.get("total"), keep_tail=True)

    @staticmethod
    def _fit(text: str, tokens, keep_tail: bool = False) -> str:
        """Обрезка по бюджету токенов (3 символа на токен); без бюджета текст не меняется."""
        if not text or not tokens or len(text) <= tokens * 3:
            return text
        limit = int(tokens) * 3
        return ("…" + text[-limit:]) if keep_tail else (text[:limit].rstrip() + "…")

    async def refresh_graph(self, query: str) -> None:
        """Факты графа по теме реплики: поиск быстрый, но не дольше трёх секунд, иначе отвечаем без них."""
        h = self._h
        try:
            h._graph_block = await asyncio.wait_for(h._memory_reader.graph_block(query), 3.0)
        except (asyncio.TimeoutError, Exception):
            h._graph_block = ""
        try:
            h._self_block = await asyncio.wait_for(h._memory_reader.self_block(query), 3.0)
        except (asyncio.TimeoutError, Exception):
            h._self_block = ""

    # ── поиск в памяти с дедлайном (ТЗ 25, FAST-6) ───────────────────────────────

    def start_retrieval(self, query: str) -> "Retrieval":
        """Запустить поиск фактов графа и воспоминаний о себе параллельно с классификатором и приказом в игру.
        Результат - (факты графа, воспоминания о себе); ошибка поиска - пустые блоки, наружу не бросает."""
        loop = asyncio.get_running_loop()
        return Retrieval(asyncio.ensure_future(self._search(query)), loop.time(), query)

    async def _search(self, query: str) -> tuple[str, str]:
        h = self._h

        async def one(coro) -> str:
            try:
                return await asyncio.wait_for(coro, 3.0)
            except Exception:
                return ""
        graph, own = await asyncio.gather(one(h._memory_reader.graph_block(query)), one(h._memory_reader.self_block(query)))
        return graph, own

    async def use_retrieval(self, retrieval: "Retrieval | None", query: str, deadline_s: float | None) -> bool:
        """Положить результат поиска в контекст ответа. Дедлайн отсчитывается от запуска поиска. Не успел: ответ без памяти
        (блоки пустые либо найденное раньше с опозданием для предыдущей реплики), а когда поиск закончит, результат ждёт
        следующей реплики (предзагрузка). Возвращает True, если блоки свежие."""
        h = self._h
        retrieval = retrieval or self.start_retrieval(query)
        task = retrieval.task
        try:
            if deadline_s is None:
                graph, own = await task
            else:
                left = deadline_s - (asyncio.get_running_loop().time() - retrieval.t0)
                graph, own = await asyncio.wait_for(asyncio.shield(task), max(0.0, left))
        except asyncio.TimeoutError:
            task.add_done_callback(lambda t, q=retrieval.query: self._prefetched(t, q))
            fresh = getattr(h, "_prefetch", None)
            if fresh and time.time() - fresh["ts"] <= PREFETCH_TTL_S:
                h._graph_block, h._self_block = fresh["graph"], fresh["self"]
                h._prefetch = None
            else:
                h._graph_block = h._self_block = ""
            return False
        except Exception as e:
            logger.info(f"Поиск в памяти не удался: {e}")
            h._graph_block = h._self_block = ""
            return False
        h._graph_block, h._self_block = graph, own
        h._prefetch = None
        return True

    def _prefetched(self, task: "asyncio.Task", query: str) -> None:
        """Поиск закончился после дедлайна: результат кладётся на следующую реплику."""
        if task.cancelled() or task.exception() is not None:
            return
        graph, own = task.result()
        if graph or own:
            self._h._prefetch = {"query": query, "graph": graph, "self": own, "ts": time.time()}


class Retrieval:
    """Идущий поиск в памяти: задача, момент запуска (от него считается дедлайн) и запрос."""

    __slots__ = ("task", "t0", "query")

    def __init__(self, task: "asyncio.Task", t0: float, query: str):
        self.task, self.t0, self.query = task, t0, query

    def cancel(self) -> None:
        self.task.cancel()

"""ContextBuilder: контекст ответа (ТЗ 29, ARC-5). Пока в точности прежняя склейка шести источников:
память и диалог, справка о возможностях, услышанное, воспоминания о себе, факты графа, отношение к игроку.
Состояние (`_graph_block`, `_self_block`, `_heard`) живёт на фасаде `Companion`."""
import asyncio


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

    def build(self, include_hearing: bool = True, query: str = "") -> str:
        """query: о чём сейчас речь. По нему из долговременной памяти берутся подходящие записи."""
        h = self._h
        parts = [h._memory_reader.context_block(dialogue=12, query=query), h._capabilities()]
        if include_hearing and h._settings.load()["hearing"]:
            heard = h.recent_heard()
            if heard:
                parts.append("Что недавно было слышно из колонок (краткая сводка автоматической расшифровки, "
                             "возможны ошибки; это может быть фильм, видео, музыка, не слова "
                             "пользователя): " + heard)
        if h._self_block:
            parts.append(h._self_block)
        if h._graph_block:
            parts.append(h._graph_block)
        if self.mood_of is not None:
            try:
                parts.append(self.mood_of())
            except Exception:
                pass
        rel = h._people.describe("игрок")
        if rel:
            parts.append(f"Твоё {rel}.")
        return "\n".join(p for p in parts if p)

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

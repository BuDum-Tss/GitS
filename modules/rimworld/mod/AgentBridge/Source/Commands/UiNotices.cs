using System;
using System.Linq;
using Newtonsoft.Json.Linq;
using HarmonyLib;
using RimWorld;
using Verse;

namespace AgentBridge
{
    /// Уведомления (письма справа), задания (вкладка «Задания»), фауна и режим дня: состояние и действия теми же шагами, что у игрока.
    public static partial class CommandExecutor
    {
        private static string Plain(string t) => System.Text.RegularExpressions.Regex.Replace(t ?? "", @"\(\*?/?\w+\)", "");

        private static object LettersState()
        {
            return Find.LetterStack.LettersListForReading.Select((l, i) => new
            {
                index = i, label = Plain(l.Label.ToString()), kind = l.def.defName,
                has_choices = l is ChoiceLetter c && c.Choices != null && c.Choices.Any(),
                dismissable = l.CanDismissWithRightClick,
                age_s = (Find.TickManager.TicksGame - l.arrivalTick) / 60,
            }).ToList();
        }

        private static Letter LetterAt(JObject cmd)
        {
            var list = Find.LetterStack.LettersListForReading;
            int i = cmd["index"]?.Value<int>() ?? throw new Exception("нужен index письма (из ui_state.letters)");
            if (i < 0 || i >= list.Count) throw new Exception($"письма с index {i} нет, всего {list.Count}");
            return list[i];
        }

        /// ui_letter {index, action: read|choose|dismiss, choice}: read открывает письмо как клик игрока, choose выбирает кнопку,
        /// dismiss убирает письмо (если игра разрешает).
        private static string UiLetter(JObject cmd)
        {
            if (!InGame) throw new Exception("игра не запущена");
            var letter = LetterAt(cmd);
            var action = cmd["action"]?.Value<string>() ?? "read";
            var label = Plain(letter.Label.ToString());
            if (action == "dismiss")
            {
                if (!letter.CanDismissWithRightClick) throw new Exception($"«{label}» нельзя убрать, пока не прочитано и не выбрано решение.");
                Find.LetterStack.RemoveLetter(letter);
            }
            else if (action == "read")
            {
                if (!(letter is ChoiceLetter)) letter.OpenLetter();
                else letter.OpenLetter();
            }
            else if (action == "choose")
            {
                var cl = letter as ChoiceLetter ?? throw new Exception("у этого письма нет выбора");
                var choice = cmd["choice"]?.Value<string>() ?? throw new Exception("нужен choice (текст кнопки)");
                var opt = cl.Choices.FirstOrDefault(o => OptText(o).StartsWith(choice, StringComparison.OrdinalIgnoreCase))
                    ?? throw new Exception("нет такой кнопки. Есть: " + string.Join(" | ", cl.Choices.Select(o => OptText(o))));
                opt.action?.Invoke();
                if (opt.resolveTree == false) { }
            }
            else throw new Exception("action: read | choose | dismiss");
            return UiState(new JObject { ["action"] = "letter_" + action, ["letter"] = label });
        }

        private static string OptText(DiaOption o) => Traverse.Create(o).Field("text").GetValue<string>() ?? "";

        private static object QuestsState()
        {
            return Find.QuestManager.QuestsListForReading.Where(q => !q.hiddenInUI && !q.Historical).Take(ListMax).Select(q => new
            {
                id = q.id, name = q.name, state = q.State.ToString(),
                can_accept = q.State == QuestState.NotYetAccepted,
            }).ToList();
        }

        /// ui_quest {id, action: accept|dismiss, pawn_id}: принять задание (с исполнителем, если требует) или отклонить.
        private static string UiQuest(JObject cmd)
        {
            if (!InGame) throw new Exception("игра не запущена");
            int id = cmd["id"]?.Value<int>() ?? throw new Exception("нужен id задания (из ui_state.quests)");
            var q = Find.QuestManager.QuestsListForReading.FirstOrDefault(x => x.id == id) ?? throw new Exception("задания нет");
            var action = cmd["action"]?.Value<string>() ?? "accept";
            if (action == "accept")
            {
                if (q.State != QuestState.NotYetAccepted) throw new Exception("задание уже не ждёт принятия: " + q.State);
                var pawn = GetPawnById(cmd["pawn_id"]?.Value<string>() ?? "") ?? Find.CurrentMap.mapPawns.FreeColonists.FirstOrDefault();
                q.Accept(pawn);
            }
            else if (action == "dismiss") Traverse.Create(q).Field("dismissed").SetValue(true);
            else throw new Exception("action: accept | dismiss");
            return UiState(new JObject { ["action"] = "quest_" + action, ["quest"] = q.name });
        }

        private static object WildlifeState()
        {
            var map = Find.CurrentMap;
            return map.mapPawns.AllPawnsSpawned.Where(p => p.RaceProps.Animal && p.Faction == null).Take(ListMax).Select(a => new
            {
                id = a.ThingID, label = a.LabelShort, kind = a.def.label, x = a.Position.x, z = a.Position.z,
                tame_marked = map.designationManager.DesignationOn(a, DesignationDefOf.Tame) != null,
                hunt_marked = map.designationManager.DesignationOn(a, DesignationDefOf.Hunt) != null,
            }).ToList();
        }

        private static object ScheduleState()
        {
            return Find.CurrentMap.mapPawns.FreeColonists.Select(p => new
            {
                id = p.ThingID, name = p.LabelShort,
                // 24 часа: S сон, W работа, A всё что угодно, J радость
                hours = p.timetable == null ? "" : new string(p.timetable.times.Select(t => char.ToUpper(t.defName[0])).ToArray()),
            }).ToList();
        }
    }
}

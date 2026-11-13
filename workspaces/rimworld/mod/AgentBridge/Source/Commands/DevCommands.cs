using System;
using System.Linq;
using Newtonsoft.Json.Linq;
using RimWorld;
using RimWorld.QuestGen;
using Verse;

namespace AgentBridge
{
    /// Только для проверки шагов интерфейса в тестовой колонии: создаёт то, чего в новой колонии ещё нет
    /// (кровать, письмо с выбором, задание). В обычной игре не вызывается.
    public static partial class CommandExecutor
    {
        private static string DevTestSetup()
        {
            if (!InGame) throw new Exception("игра не запущена");
            var map = Find.CurrentMap;
            var pawn = map.mapPawns.FreeColonists.First();
            var result = new JObject();

            var cell = CellFinder.RandomClosewalkCellNear(pawn.Position, map, 6);
            var bed = (Building_Bed)ThingMaker.MakeThing(ThingDefOf.Bed, ThingDefOf.WoodLog);
            bed.SetFaction(Faction.OfPlayer);
            GenSpawn.Spawn(bed, cell, map);
            result["bed"] = bed.ThingID;

            var letter = (ChoiceLetter_GrowthMoment)null;
            var plain = LetterMaker.MakeLetter("Проверка уведомлений", "Тестовое письмо для проверки шагов интерфейса.", LetterDefOf.NeutralEvent);
            Find.LetterStack.ReceiveLetter(plain);
            result["letter"] = true;

            try
            {
                var def = DefDatabase<QuestScriptDef>.GetNamedSilentFail("OpportunitySite_ItemStash") ?? DefDatabase<QuestScriptDef>.AllDefsListForReading.FirstOrDefault(d => d.IsRootAny && d.defName.StartsWith("Opportunity"));
                if (def != null) { var q = QuestUtility.GenerateQuestAndMakeAvailable(def, 300f); result["quest"] = q?.name; }
            }
            catch (Exception e) { result["quest_error"] = e.Message; }
            return result.ToString(Newtonsoft.Json.Formatting.None);
        }
    }
}

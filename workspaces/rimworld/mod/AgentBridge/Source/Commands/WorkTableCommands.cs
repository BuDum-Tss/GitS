using System.Linq;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using RimWorld;
using Verse;

namespace AgentBridge
{
    /// Таблица работ как в окне «Работа»: у каждого колониста приоритет каждой работы цифрой (1 высший ... 4 низкий, 0 выкл,
    /// -1 недоступна), навыки и страсть. Чтобы агент решал по цифрам и видел, кто на что способен.
    public static partial class CommandExecutor
    {
        private static string GetWorkTable(JObject cmd)
        {
            var map = Find.CurrentMap;
            var types = DefDatabase<WorkTypeDef>.AllDefsListForReading.Where(w => w.visible).OrderByDescending(w => w.naturalPriority).ToList();
            var pawns = map.mapPawns.FreeColonistsSpawned.Select(p => new
            {
                id = p.ThingID,
                name = p.LabelShort,
                priorities = types.ToDictionary(w => w.defName,
                    w => p.WorkTypeIsDisabled(w) ? -1 : p.workSettings.GetPriority(w)),
                skills = types.Where(w => !p.WorkTypeIsDisabled(w) && w.relevantSkills.Count > 0).ToDictionary(w => w.defName, w =>
                {
                    var sk = p.skills.GetSkill(w.relevantSkills[0]);
                    return new { skill = w.relevantSkills[0].defName, level = sk.Level, passion = sk.passion.ToString() };
                }),
            }).ToList();
            return JsonConvert.SerializeObject(new
            {
                manual_priorities = Current.Game.playSettings.useWorkPriorities,
                legend = "1 высший ... 4 низкий, 0 выключено, -1 работа недоступна пешке; без ручных приоритетов (manual_priorities=false) работы только вкл/выкл",
                work_types = types.Select(w => w.defName).ToList(),
                pawns,
            });
        }
    }
}

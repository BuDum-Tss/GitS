using System;
using System.Collections.Generic;
using System.Linq;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using RimWorld;
using Verse;

namespace AgentBridge
{
    /// Проверочная команда для оценки модуля: запускает игровой инцидент (налёт, бешеные животные, ...) в текущей колонии,
    /// чтобы проверить оборонительные рефлексы без ожидания рассказчика. Только по явному confirm="eval".
    public static partial class CommandExecutor
    {
        private static string DevTriggerIncident(JObject cmd)
        {
            if (!InGame) throw new Exception("игра не запущена");
            if (cmd["confirm"]?.Value<string>() != "eval")
                throw new Exception("dev_trigger_incident запускает настоящий инцидент в колонии: нужен confirm=\"eval\" (только для проверки)");
            var name = cmd["incident"]?.Value<string>() ?? throw new ArgumentException("Missing 'incident' (defName инцидента: RaidEnemy, AnimalInsanityMass, ...)");
            var def = DefDatabase<IncidentDef>.GetNamedSilentFail(name)
                ?? throw new Exception($"нет инцидента с defName «{name}» (примеры: RaidEnemy, AnimalInsanityMass, ManhunterPack)");

            var map = Find.CurrentMap;
            var parms = StorytellerUtility.DefaultParmsNow(def.category, map);
            if (cmd["points"] != null) parms.points = cmd["points"].Value<float>();

            var before = new HashSet<string>(HostileIds(map));
            bool ok = def.Worker.TryExecute(parms);
            var fresh = map.mapPawns.AllPawnsSpawned
                .Where(p => !p.Dead && p.HostileTo(Faction.OfPlayer) && !before.Contains(p.ThingID))
                .ToList();

            return JsonConvert.SerializeObject(new
            {
                defName = def.defName,
                success = ok,
                points = Math.Round(parms.points, 1),
                faction = parms.faction?.Name,
                new_threats = fresh.Count,
                threats = fresh.GroupBy(p => p.def.defName).ToDictionary(g => g.Key, g => g.Count()),
                note = ok ? "инцидент выполнен" : "рассказчик отказал: инцидент сейчас невозможен (нет подходящего места, фракции или условий)",
            });
        }

        private static IEnumerable<string> HostileIds(Map map) =>
            map.mapPawns.AllPawnsSpawned.Where(p => !p.Dead && p.HostileTo(Faction.OfPlayer)).Select(p => p.ThingID);
    }
}

using System;
using System.Collections.Generic;
using System.Linq;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using RimWorld;
using Verse;

namespace AgentBridge
{
    /// Цена и объём работы стройки: сколько материалов нужно и есть ли они на складе, сколько часов работы у строителей
    /// (с учётом чертежей, которые уже стоят в очереди). Без этого она размечала здания больше, чем колония потянет.
    public static partial class CommandExecutor
    {
        private const float TicksPerHour = 2500f;

        private class CostLine { public ThingDef Def; public ThingDef Stuff; public int Count; }

        /// Нужные материалы и работа для списка (постройка, материал, сколько штук).
        private static (Dictionary<ThingDef, int> need, float work) CostOf(IEnumerable<CostLine> lines)
        {
            var need = new Dictionary<ThingDef, int>();
            float work = 0f;
            foreach (var l in lines.Where(l => l.Count > 0))
            {
                foreach (var cost in l.Def.CostListAdjusted(l.Stuff))
                    need[cost.thingDef] = (need.TryGetValue(cost.thingDef, out var n) ? n : 0) + cost.count * l.Count;
                work += l.Def.GetStatValueAbstract(StatDefOf.WorkToBuild, l.Stuff) * l.Count;
            }
            return (need, work);
        }

        /// Скорость стройки колонии: число строителей и суммарная скорость (1.0 = обычный строитель).
        private static (int builders, float speed) BuilderPower(Map map)
        {
            var list = map.mapPawns.FreeColonistsSpawned
                .Where(p => !p.Downed && !p.WorkTypeIsDisabled(WorkTypeDefOf.Construction)).ToList();
            return (list.Count, list.Sum(p => p.GetStatValue(StatDefOf.ConstructionSpeed)));
        }

        /// Работа, которая уже висит в очереди: чертежи и каркасы, ещё не достроенные.
        private static float BacklogWork(Map map)
        {
            float work = 0f;
            foreach (var t in map.listerThings.AllThings)
            {
                if (t is Frame f) work += f.WorkLeft;
                else if (t is Blueprint_Build bp && bp.def.entityDefToBuild is ThingDef def)
                    work += def.GetStatValueAbstract(StatDefOf.WorkToBuild, bp.stuffToUse);
            }
            return work;
        }

        /// Итог для ответа: материалы (нужно/есть), часы работы, перегрузка. problems дополняется при нехватке или перегрузе.
        private static object CostReport(Map map, IEnumerable<CostLine> lines, List<string> problems)
        {
            var (need, work) = CostOf(lines);
            var materials = need.Select(kv => new { def = kv.Key.defName, need = kv.Value, have = map.resourceCounter.GetCount(kv.Key) }).ToList();
            foreach (var m in materials.Where(m => m.need > m.have))
                problems?.Add($"не хватает {m.def}: нужно {m.need}, на складе {m.have}. Уменьши постройку или сначала добудь ресурсы.");

            var (builders, speed) = BuilderPower(map);
            float backlog = BacklogWork(map);
            float hoursThis = speed > 0.05f ? work / (speed * TicksPerHour) : float.PositiveInfinity;
            float hoursBacklog = speed > 0.05f ? backlog / (speed * TicksPerHour) : 0f;
            float limit = 12f + 8f * builders;      // что колония потянет: полдня плюс по 8 часов на строителя
            bool heavy = hoursThis + hoursBacklog > limit;
            if (heavy)
                problems?.Add($"слишком большой объём: {hoursThis:0.#} ч работы + {hoursBacklog:0.#} ч уже в очереди при {builders} строителях " +
                              $"(разумно до {limit:0} ч). Построй меньше (комната меньшего размера) или сначала достройте очередь.");
            return new
            {
                materials,
                work_hours = float.IsInfinity(hoursThis) ? -1f : (float)Math.Round(hoursThis, 1),
                queue_hours = (float)Math.Round(hoursBacklog, 1),
                builders, limit_hours = limit,
                affordable = materials.All(m => m.need <= m.have),
                heavy,
            };
        }

        /// estimate_build: сколько стоит и сколько делать N штук постройки (до расстановки).
        private static string EstimateBuild(JObject cmd)
        {
            var map = Find.CurrentMap;
            var def = GetThingDefOrThrow(cmd["def_name"].Value<string>());
            var count = Math.Max(1, cmd["count"]?.Value<int>() ?? 1);
            var stuff = ResolveStuff(def, cmd);
            var problems = new List<string>();
            var report = CostReport(map, new[] { new CostLine { Def = def, Stuff = stuff, Count = count } }, problems);
            return JsonConvert.SerializeObject(new { def = def.defName, count, cost = report, problems });
        }

        /// describe_def: что нужно для постройки и какие у неё характеристики (стоимость, работа, материалы со складом, прочность,
        /// горючесть, красота, требования: исследование, навык, питание).
        private static string DescribeDef(JObject cmd)
        {
            var map = Find.CurrentMap;
            var def = GetThingDefOrThrow(cmd["def_name"].Value<string>());
            var stuffs = def.MadeFromStuff
                ? GenStuff.AllowedStuffsFor(def).Take(14).Select(st => new
                {
                    stuff = st.defName,
                    in_stock = map.resourceCounter.GetCount(st),
                    work = (int)def.GetStatValueAbstract(StatDefOf.WorkToBuild, st),
                    hp = (int)def.GetStatValueAbstract(StatDefOf.MaxHitPoints, st),
                    flammability = Math.Round(def.GetStatValueAbstract(StatDefOf.Flammability, st), 2),
                    beauty = Math.Round(def.GetStatValueAbstract(StatDefOf.Beauty, st), 1),
                    cost = def.CostListAdjusted(st).Select(c => new { c.thingDef.defName, c.count }).ToList(),
                }).ToList<object>()
                : new List<object>();
            var plain = def.MadeFromStuff ? null : new
            {
                cost = def.CostList?.Select(c => new { c.thingDef.defName, c.count, in_stock = map.resourceCounter.GetCount(c.thingDef) }).ToList(),
                work = (int)def.GetStatValueAbstract(StatDefOf.WorkToBuild),
                hp = (int)def.GetStatValueAbstract(StatDefOf.MaxHitPoints),
                flammability = Math.Round(def.GetStatValueAbstract(StatDefOf.Flammability), 2),
                beauty = Math.Round(def.GetStatValueAbstract(StatDefOf.Beauty), 1),
            };
            var research = def.researchPrerequisites?.Select(r => new { r.defName, done = r.IsFinished }).ToList();
            var power = def.comps?.OfType<CompProperties_Power>().FirstOrDefault();
            return JsonConvert.SerializeObject(new
            {
                def = def.defName, label = def.label,
                description = (def.description ?? "").Length > 220 ? def.description.Substring(0, 220) + "…" : def.description,
                size = new { x = def.size.x, z = def.size.z },
                category = def.designationCategory?.defName,
                made_from_stuff = def.MadeFromStuff,
                stuff_categories = def.stuffCategories?.Select(c => c.defName).ToList(),
                materials = stuffs,
                single_material = plain,
                requires = new
                {
                    research,
                    construction_skill = def.constructionSkillPrerequisite,
                    power_watts = power != null ? (float?)power.PowerConsumption : null,
                    passability = def.passability.ToString(),
                    holds_roof = def.holdsRoof,
                },
                note = "materials: из чего можно строить (запас на складе, часы работы, прочность, горючесть, красота). Камень это БЛОКИ Blocks*.",
            });
        }
    }
}

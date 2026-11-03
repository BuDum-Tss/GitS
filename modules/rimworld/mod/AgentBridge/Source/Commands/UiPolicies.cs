using System;
using System.Collections.Generic;
using System.Linq;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using RimWorld;
using Verse;

namespace AgentBridge
{
    /// Политики питания, лекарств и одежды (вкладка «Назначения», кнопка «Управление политиками») и кровати (владельцы).
    ///
    /// ui_policy {kind: food|drugs|apparel, action, name, ...}:
    ///   show, create, rename (new_name), delete, allow / deny (item: предмет или категория по defName или названию; food и apparel),
    ///   set_drug (drug, allow_addiction?, allow_joy?, scheduled?, mood_below?, joy_below?; drugs).
    /// ui_bed {action: assign|unassign, bed_id, pawn_id}: владелец кровати.
    public static partial class CommandExecutor
    {
        private static ThingFilter PolicyFilter(string kind, string name, out string label)
        {
            var g = Current.Game;
            if (kind == "food")
            {
                var p = g.foodRestrictionDatabase.AllFoodRestrictions.FirstOrDefault(x => x.label == name) ?? throw new Exception($"нет политики питания «{name}»");
                label = p.label; return p.filter;
            }
            if (kind == "apparel")
            {
                var p = g.outfitDatabase.AllOutfits.FirstOrDefault(x => x.label == name) ?? throw new Exception($"нет политики одежды «{name}»");
                label = p.label; return p.filter;
            }
            throw new Exception("kind: food, drugs или apparel");
        }

        private static string Lower(string s) => (s ?? "").Trim().ToLowerInvariant();

        /// Предметы или категории по строке: defName или название. Категория (например «мясо») затрагивает все её предметы.
        private static (List<ThingDef> things, List<ThingCategoryDef> cats) ResolveItems(string item)
        {
            var key = Lower(item);
            var things = DefDatabase<ThingDef>.AllDefsListForReading.Where(d => Lower(d.defName) == key || Lower(d.label) == key).ToList();
            var cats = DefDatabase<ThingCategoryDef>.AllDefsListForReading.Where(d => Lower(d.defName) == key || Lower(d.label) == key).ToList();
            if (things.Count == 0 && cats.Count == 0) throw new Exception($"нет предмета или категории «{item}» (defName или название)");
            return (things, cats);
        }

        private static object FilterSummary(ThingFilter f)
        {
            var allowed = f.AllowedThingDefs.ToList();
            return new
            {
                allowed_count = allowed.Count,
                allowed_sample = allowed.Take(25).Select(d => d.label).ToList(),
                categories = f.DisplayRootCategory?.catDef?.childCategories?.Select(c => new { id = c.defName, label = c.label,
                    allowed = f.Allows(c.DescendantThingDefs.FirstOrDefault() ?? allowed.FirstOrDefault()) }).Take(20).ToList(),
            };
        }

        private static object DrugEntries(DrugPolicy p)
        {
            var rows = new List<object>();
            for (int i = 0; i < p.Count; i++)
            {
                var e = p[i];
                rows.Add(new { drug = e.drug.defName, label = e.drug.label, allow_addiction = e.allowedForAddiction, allow_joy = e.allowedForJoy,
                               scheduled = e.allowScheduled, mood_below = e.onlyIfMoodBelow, joy_below = e.onlyIfJoyBelow });
            }
            return rows;
        }

        private static string UiPolicy(JObject cmd)
        {
            if (!InGame) throw new Exception("игра не запущена");
            var kind = cmd["kind"]?.Value<string>() ?? "";
            var action = cmd["action"]?.Value<string>() ?? "show";
            var name = cmd["name"]?.Value<string>() ?? "";
            var g = Current.Game;
            object detail = null;

            if (action == "create")
            {
                if (kind == "food") g.foodRestrictionDatabase.MakeNewFoodRestriction().label = name;
                else if (kind == "drugs") g.drugPolicyDatabase.MakeNewDrugPolicy().label = name;
                else if (kind == "apparel") g.outfitDatabase.MakeNewOutfit().label = name;
                else throw new Exception("kind: food, drugs или apparel");
                return UiState(new JObject { ["action"] = "policy_create", ["kind"] = kind, ["name"] = name });
            }

            if (kind == "drugs")
            {
                var p = g.drugPolicyDatabase.AllPolicies.FirstOrDefault(x => x.label == name) ?? throw new Exception($"нет политики лекарств «{name}»");
                if (action == "rename") p.label = cmd["new_name"]?.Value<string>() ?? throw new Exception("нужен new_name");
                else if (action == "delete") { var r = g.drugPolicyDatabase.TryDelete(p); if (!r.Accepted) throw new Exception(r.Reason); }
                else if (action == "set_drug")
                {
                    var key = Lower(cmd["drug"]?.Value<string>());
                    DrugPolicyEntry entry = null;
                    for (int i = 0; i < p.Count; i++) if (Lower(p[i].drug.defName) == key || Lower(p[i].drug.label) == key) entry = p[i];
                    if (entry == null) throw new Exception("нет такого препарата. Есть: " + string.Join(", ", Enumerable.Range(0, p.Count).Select(i => p[i].drug.label)));
                    if (cmd["allow_addiction"] != null) entry.allowedForAddiction = cmd["allow_addiction"].Value<bool>();
                    if (cmd["allow_joy"] != null) entry.allowedForJoy = cmd["allow_joy"].Value<bool>();
                    if (cmd["scheduled"] != null) entry.allowScheduled = cmd["scheduled"].Value<bool>();
                    if (cmd["mood_below"] != null) { entry.onlyIfMoodBelow = cmd["mood_below"].Value<float>(); }
                    if (cmd["joy_below"] != null) { entry.onlyIfJoyBelow = cmd["joy_below"].Value<float>(); }
                }
                else if (action != "show") throw new Exception("для drugs: show, create, rename, delete, set_drug");
                detail = new { policy = p.label, entries = DrugEntries(p) };
            }
            else
            {
                var filter = PolicyFilter(kind, name, out var label);
                if (action == "rename")
                {
                    var nn = cmd["new_name"]?.Value<string>() ?? throw new Exception("нужен new_name");
                    if (kind == "food") g.foodRestrictionDatabase.AllFoodRestrictions.First(x => x.label == name).label = nn;
                    else g.outfitDatabase.AllOutfits.First(x => x.label == name).label = nn;
                    label = nn;
                }
                else if (action == "delete")
                {
                    AcceptanceReport r = kind == "food"
                        ? g.foodRestrictionDatabase.TryDelete(g.foodRestrictionDatabase.AllFoodRestrictions.First(x => x.label == name))
                        : g.outfitDatabase.TryDelete(g.outfitDatabase.AllOutfits.First(x => x.label == name));
                    if (!r.Accepted) throw new Exception(r.Reason);
                }
                else if (action == "allow" || action == "deny")
                {
                    bool on = action == "allow";
                    var (things, cats) = ResolveItems(cmd["item"]?.Value<string>() ?? throw new Exception("нужен item"));
                    foreach (var c in cats) filter.SetAllow(c, on);
                    foreach (var t in things) filter.SetAllow(t, on);
                }
                else if (action != "show") throw new Exception("действия: show, create, rename, delete, allow, deny");
                if (action != "delete") detail = new { policy = label, filter = FilterSummary(filter) };
            }
            return UiState(new JObject { ["action"] = "policy_" + action, ["kind"] = kind, ["name"] = name, ["detail"] = detail == null ? null : JToken.FromObject(detail) });
        }

        // ── кровати ──────────────────────────────────────────────────────────

        private static object BedsState()
        {
            return Find.CurrentMap.listerBuildings.AllBuildingsColonistOfClass<Building_Bed>().Take(ListMax).Select(b => new
            {
                id = b.ThingID, label = b.LabelShort, x = b.Position.x, z = b.Position.z, medical = b.Medical,
                slots = b.TotalSleepingSlots,
                owners = b.CompAssignableToPawn?.AssignedPawnsForReading.Select(p => p.LabelShort).ToList(),
            }).ToList();
        }

        private static string UiBed(JObject cmd)
        {
            if (!InGame) throw new Exception("игра не запущена");
            var id = cmd["bed_id"]?.Value<string>() ?? throw new Exception("нужен bed_id (из ui_state.beds)");
            var bed = Find.CurrentMap.listerBuildings.AllBuildingsColonistOfClass<Building_Bed>().FirstOrDefault(b => b.ThingID == id) ?? throw new Exception("кровать не найдена");
            var pawn = GetPawnById(cmd["pawn_id"]?.Value<string>() ?? "") ?? throw new Exception("пешка не найдена");
            var comp = bed.CompAssignableToPawn ?? throw new Exception("эту кровать нельзя назначить");
            var action = cmd["action"]?.Value<string>() ?? "assign";
            if (action == "assign")
            {
                if (!comp.AssignedPawnsForReading.Contains(pawn) && comp.AssignedPawnsForReading.Count >= comp.MaxAssignedPawnsCount && comp.MaxAssignedPawnsCount > 0)
                    comp.TryUnassignPawn(comp.AssignedPawnsForReading[0]);   // как в игре: занято, прежний владелец освобождает место
                comp.TryAssignPawn(pawn);
            }
            else if (action == "unassign") comp.TryUnassignPawn(pawn);
            else throw new Exception("action: assign | unassign");
            return UiState(new JObject { ["action"] = "bed_" + action, ["bed"] = bed.LabelShort, ["pawn"] = pawn.LabelShort,
                                         ["owners"] = JToken.FromObject(comp.AssignedPawnsForReading.Select(p => p.LabelShort).ToList()) });
        }
    }
}

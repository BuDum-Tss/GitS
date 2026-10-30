using System;
using System.Linq;
using Newtonsoft.Json.Linq;
using RimWorld;
using Verse;

namespace AgentBridge
{
    /// Области и питомцы теми же шагами, что у игрока: области в «Режиме дня» (колонисты) и «Питомцах» (животные),
    /// хозяин, «следовать», дрессировка. Курсор едет к ячейке таблицы (TableCursor.TableSpot), правка после клика.
    public static partial class CommandExecutor
    {
        private static Area FindArea(Map map, string label)
        {
            if (string.IsNullOrEmpty(label) || label.Equals("none", StringComparison.OrdinalIgnoreCase) || label == "нет") return null;
            return map.areaManager.AllAreas.FirstOrDefault(a => a.Label == label)
                ?? throw new Exception($"нет области «{label}». Есть: " + string.Join(", ", map.areaManager.AllAreas.Select(a => a.Label)));
        }

        private static Pawn UiPawn(JObject cmd)
        {
            var id = cmd["pawn_id"]?.Value<string>() ?? cmd["animal_id"]?.Value<string>() ?? "";
            return GetPawnById(id) ?? throw new Exception("пешка или животное не найдены");
        }

        /// ui_area {pawn_id, area}: разрешённая область пешки (вкладка «Режим дня») или животного (вкладка «Питомцы»). area "none": без ограничения.
        private static string UiArea(JObject cmd)
        {
            if (!InGame) throw new Exception("игра не запущена");
            var p = UiPawn(cmd);
            var area = FindArea(Find.CurrentMap, cmd["area"]?.Value<string>());
            if (p.playerSettings == null) throw new Exception($"{p.LabelShort}: области не настраиваются");
            p.playerSettings.AreaRestrictionInPawnCurrentMap = area;
            return UiState(new JObject { ["action"] = "area", ["pawn"] = p.LabelShort, ["area"] = area?.Label });
        }

        /// ui_animal {animal_id, master?: pawn_id|"none", follow_drafted?, follow_fieldwork?, train?: Obedience|Release|Rescue|Haul, value?: bool}.
        private static string UiAnimal(JObject cmd)
        {
            if (!InGame) throw new Exception("игра не запущена");
            var a = UiPawn(cmd);
            if (a.Faction != Faction.OfPlayer || !a.RaceProps.Animal) throw new Exception($"{a.LabelShort}: это не ваш питомец");
            var done = "";
            if (cmd["master"] != null)
            {
                var m = cmd["master"].Value<string>();
                if (m == "none" || string.IsNullOrEmpty(m)) a.playerSettings.Master = null;
                else
                {
                    var master = GetPawnById(m) ?? throw new Exception("хозяин не найден");
                    if (a.training != null && !a.training.HasLearned(TrainableDefOf.Obedience))
                        throw new Exception($"{a.LabelShort} не выучил «послушание»: хозяина назначить нельзя, сначала дрессировка Obedience.");
                    a.playerSettings.Master = master;
                }
                done += "master ";
            }
            if (cmd["follow_drafted"] != null) { a.playerSettings.followDrafted = cmd["follow_drafted"].Value<bool>(); done += "follow_drafted "; }
            if (cmd["follow_fieldwork"] != null) { a.playerSettings.followFieldwork = cmd["follow_fieldwork"].Value<bool>(); done += "follow_fieldwork "; }
            if (cmd["train"] != null)
            {
                var def = DefDatabase<TrainableDef>.GetNamedSilentFail(cmd["train"].Value<string>())
                    ?? throw new Exception("train: " + string.Join(", ", DefDatabase<TrainableDef>.AllDefs.Select(d => d.defName)));
                var acc = a.training.CanAssignToTrain(def);
                if (!acc.Accepted) throw new Exception($"{a.LabelShort}: нельзя тренировать {def.label}: {acc.Reason}");
                a.training.SetWantedRecursive(def, cmd["value"]?.Value<bool>() ?? true);
                done += "train ";
            }
            if (done == "") throw new Exception("нужно одно из: master, follow_drafted, follow_fieldwork, train");
            return UiState(new JObject { ["action"] = "animal", ["animal"] = a.LabelShort, ["changed"] = done.Trim() });
        }

        private static object PetsState()
        {
            return Find.CurrentMap.mapPawns.SpawnedColonyAnimals.Take(ListMax).Select(a => new
            {
                id = a.ThingID, label = a.LabelShort, kind = a.def.label,
                master = a.playerSettings?.Master?.LabelShort,
                area = a.playerSettings?.AreaRestrictionInPawnCurrentMap?.Label,
                follow_drafted = a.playerSettings?.followDrafted, follow_fieldwork = a.playerSettings?.followFieldwork,
                training = a.training == null ? null : DefDatabase<TrainableDef>.AllDefsListForReading.Where(d => a.training.CanAssignToTrain(d).Accepted)
                    .ToDictionary(d => d.defName, d => a.training.HasLearned(d) ? "выучено" : a.training.GetWanted(d) ? "учится" : "нет"),
                slaughter = Find.CurrentMap.designationManager.DesignationOn(a, DesignationDefOf.Slaughter) != null,
            }).ToList();
        }
    }
}

using System;
using System.Collections.Generic;
using System.Linq;
using HarmonyLib;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using RimWorld;
using UnityEngine;
using Verse;

namespace AgentBridge
{
    /// Состояние интерфейса и шаги меню «Архитектор» (ui_state, ui_category, ui_designator, ui_place, ui_drag, ui_cancel).
    ///
    /// Идея: агент видит ровно то, что видит игрок, и действует теми же шагами. `ui_state` отдаёт текст (не картинку): какая вкладка
    /// открыта и что на ней; для строительства: категории, постройки выбранной категории, выбранный дизайнатор. Шаги строительства
    /// идут настоящими вызовами игры (дизайнаторы), поэтому правила (можно ли поставить, цена, занято ли) решает сама игра и
    /// причина отказа приходит её словами. Для остальных областей (работа и приоритеты, технологии, питомцы, области, ручное
    /// управление пешкой) состояние тоже здесь, а действия уже есть командами, привязанными к вкладкам (`set_work_priority`, `research`,
    /// `order_tame`, `set_zone_home`, `select`, `draft`, `move`, `attack`...).
    public static partial class CommandExecutor
    {
        private const int ListMax = 30;

        // ── состояние ────────────────────────────────────────────────────────

        private static string UiState(JObject last = null)
        {
            if (!InGame) throw new Exception("игра не запущена");
            var tab = Find.MainTabsRoot.OpenTab;
            var state = new JObject
            {
                ["open_tab"] = tab?.defName,
                ["last"] = last,
                ["tabs"] = Section(() => DefDatabase<MainButtonDef>.AllDefsListForReading.Where(d => d.Worker.Visible)
                    .Select(d => new { id = d.defName, label = d.LabelCap.ToString() }).ToList()),
                ["agent_camera"] = UserCamera.AgentHasCamera,
                ["paused"] = Find.TickManager.Paused,
                ["selection"] = Section(SelectionState),
                ["designator"] = Section(DesignatorState),
            };
            var name = tab?.defName ?? "";
            if (name == "Architect") state["architect"] = Section(ArchitectState);
            else if (name == "Work") state["work"] = Section(WorkState);
            else if (name == "Research") state["research"] = Section(ResearchState);
            else if (name == "Animals") state["animals"] = Section(PetsState);
            else if (name == "Restrict" || name == "Schedule") state["areas"] = Section(AreasState);
            else if (name == "Assign") state["assign"] = Section(AssignState);
            if (name == "Assign") state["beds"] = Section(BedsState);
            if (name == "Quests") state["quests"] = Section(QuestsState);
            else if (name == "Wildlife") state["wildlife"] = Section(WildlifeState);
            if (name == "Restrict" || name == "Schedule") state["schedule"] = Section(ScheduleState);
            state["letters"] = Section(LettersState);
            state["pawns"] = Section(PawnsState);
            state["threats"] = Section(ThreatsState);
            return state.ToString(Formatting.None);
        }

        private static JToken Section(Func<object> f)
        {
            try { var v = f(); return v == null ? null : JToken.FromObject(v); }
            catch (Exception e) { return new JObject { ["error"] = e.Message }; }
        }

        private static object SelectionState()
        {
            return Find.Selector.SelectedObjects.Take(12).Select(o => o is Thing t
                ? new { id = t.ThingID, label = t.LabelShort, kind = t is Pawn ? "pawn" : "thing", x = t.Position.x, z = t.Position.z }
                : new { id = "", label = o.ToString(), kind = "object", x = 0, z = 0 }).ToList();
        }

        private static object DesignatorState()
        {
            var d = Find.DesignatorManager.SelectedDesignator;
            if (d == null) return null;
            var build = d as Designator_Build;
            return new
            {
                id = build?.PlacingDef?.defName ?? d.GetType().Name,
                label = d.Label,
                kind = build != null ? "build" : d.GetType().Name,
                stuff = (build?.StuffDef)?.defName,
                rotation = build != null ? Traverse.Create(build).Field("placingRot").GetValue<Rot4>().ToStringHuman() : null,
            };
        }

        private static MainTabWindow_Architect ArchitectWindow() => MainButtonDefOf.Architect.TabWindow as MainTabWindow_Architect;

        private static List<ArchitectCategoryTab> ArchitectTabs(MainTabWindow_Architect w) =>
            Traverse.Create(w).Field("desPanelsCached").GetValue<List<ArchitectCategoryTab>>() ?? new List<ArchitectCategoryTab>();

        private static ArchitectCategoryTab SelectedArchitectTab(MainTabWindow_Architect w) =>
            Traverse.Create(w).Field("selectedDesPanel").GetValue<ArchitectCategoryTab>();

        private static IEnumerable<Designator> FlatDesignators(DesignationCategoryDef cat)
        {
            foreach (var d in cat.ResolvedAllowedDesignators)
            {
                if (d is Designator_Dropdown dd) foreach (var e in dd.Elements) yield return e;
                else yield return d;
            }
        }

        private static string DesignatorId(Designator d) => (d as Designator_Build)?.PlacingDef?.defName ?? d.GetType().Name;

        private static object ArchitectState()
        {
            var w = ArchitectWindow();
            if (w == null) return null;
            var sel = SelectedArchitectTab(w);
            var cats = ArchitectTabs(w).Select(t => new { id = t.def.defName, label = t.def.label, selected = t == sel }).ToList();
            object designators = null;
            if (sel != null)
                designators = FlatDesignators(sel.def).Take(ListMax * 2).Select(d => new
                {
                    id = DesignatorId(d), label = d.Label, kind = d is Designator_Build ? "build" : d.GetType().Name,
                }).ToList();
            return new { categories = cats, category = sel?.def.defName, designators };
        }

        private static object WorkState()
        {
            var works = DefDatabase<WorkTypeDef>.AllDefsListForReading.Where(d => d.visible).OrderByDescending(d => d.naturalPriority).ToList();
            var rows = Find.CurrentMap.mapPawns.FreeColonists.Select(p => new
            {
                id = p.ThingID, name = p.LabelShort,
                priority = works.Where(wt => !p.WorkTypeIsDisabled(wt)).ToDictionary(wt => wt.defName, wt => p.workSettings.GetPriority(wt)),
            }).ToList();
            return new { manual_priorities = Current.Game.playSettings.useWorkPriorities, pawns = rows };
        }

        private static object ResearchState()
        {
            var rm = Find.ResearchManager;
            var cur = rm.GetProject();
            var avail = DefDatabase<ResearchProjectDef>.AllDefsListForReading.Where(p => !p.IsFinished && p.CanStartNow)
                .OrderBy(p => p.CostApparent).Take(ListMax)
                .Select(p => new { id = p.defName, label = p.label, cost = Mathf.RoundToInt(p.CostApparent) }).ToList();
            return new
            {
                current = cur == null ? null : new { id = cur.defName, label = cur.label, progress = Mathf.RoundToInt(cur.ProgressPercent * 100f) },
                available = avail,
                finished = DefDatabase<ResearchProjectDef>.AllDefsListForReading.Count(p => p.IsFinished),
            };
        }

        private static object AnimalsState()
        {
            var map = Find.CurrentMap;
            return map.mapPawns.SpawnedColonyAnimals.Take(ListMax).Select(a => new
            {
                id = a.ThingID, label = a.LabelShort, kind = a.def.label,
                master = a.playerSettings?.Master?.LabelShort,
                area = a.playerSettings?.AreaRestrictionInPawnCurrentMap?.Label,
                slaughter = map.designationManager.DesignationOn(a, DesignationDefOf.Slaughter) != null,
            }).ToList();
        }

        private static object AreasState()
        {
            var map = Find.CurrentMap;
            return new
            {
                areas = map.areaManager.AllAreas.Select(a => a.Label).ToList(),
                pawns = map.mapPawns.FreeColonists.Select(p => new
                {
                    id = p.ThingID, name = p.LabelShort, area = p.playerSettings?.AreaRestrictionInPawnCurrentMap?.Label,
                }).ToList(),
            };
        }

        // ── настройка пешки: питание, лекарства, одежда, уход ─────────────────

        private static object AssignState()
        {
            var g = Current.Game;
            return new
            {
                food = g.foodRestrictionDatabase.AllFoodRestrictions.Select(p => p.label).ToList(),
                drugs = g.drugPolicyDatabase.AllPolicies.Select(p => p.label).ToList(),
                apparel = g.outfitDatabase.AllOutfits.Select(p => p.label).ToList(),
                medical = Enum.GetNames(typeof(MedicalCareCategory)),
                pawns = Find.CurrentMap.mapPawns.FreeColonists.Select(p => new
                {
                    id = p.ThingID, name = p.LabelShort,
                    food = p.foodRestriction?.CurrentFoodPolicy?.label,
                    drugs = p.drugs?.CurrentPolicy?.label,
                    apparel = p.outfits?.CurrentApparelPolicy?.label,
                    medical = p.playerSettings?.medCare.ToString(),
                }).ToList(),
            };
        }

        /// ui_assign {pawn_id, kind: food|drugs|apparel|medical, value: название политики или категории ухода}.
        private static string UiAssign(JObject cmd)
        {
            if (!InGame) throw new Exception("игра не запущена");
            if (Find.MainTabsRoot.OpenTab?.defName != "Assign")
                throw new Exception("Сначала открой вкладку «Назначения» (open_tab tab=Assign), как это делает игрок.");
            var pawn = GetPawnById(cmd["pawn_id"]?.Value<string>() ?? "") ?? throw new Exception("пешка не найдена");
            var kind = cmd["kind"]?.Value<string>() ?? "";
            var value = cmd["value"]?.Value<string>() ?? throw new Exception("нужно value");
            var g = Current.Game;
            switch (kind)
            {
                case "food":
                    pawn.foodRestriction.CurrentFoodPolicy = g.foodRestrictionDatabase.AllFoodRestrictions.FirstOrDefault(p => p.label == value)
                        ?? throw new Exception($"нет политики питания «{value}»"); break;
                case "drugs":
                    pawn.drugs.CurrentPolicy = g.drugPolicyDatabase.AllPolicies.FirstOrDefault(p => p.label == value)
                        ?? throw new Exception($"нет политики лекарств «{value}»"); break;
                case "apparel":
                    pawn.outfits.CurrentApparelPolicy = g.outfitDatabase.AllOutfits.FirstOrDefault(p => p.label == value)
                        ?? throw new Exception($"нет политики одежды «{value}»"); break;
                case "medical":
                    if (!Enum.TryParse(value, true, out MedicalCareCategory care)) throw new Exception($"нет категории ухода «{value}»");
                    pawn.playerSettings.medCare = care; break;
                default: throw new Exception("kind: food, drugs, apparel или medical");
            }
            return UiState();
        }

        /// Ручное управление: кто призван и чем занят, оружие, здоровье.
        private static object PawnsState()
        {
            return Find.CurrentMap.mapPawns.FreeColonists.Take(12).Select(p => new
            {
                id = p.ThingID, name = p.LabelShort, x = p.Position.x, z = p.Position.z,
                drafted = p.Drafted, job = p.CurJob?.def?.defName,
                weapon = p.equipment?.Primary?.LabelShort,
                health = Mathf.RoundToInt(p.health.summaryHealth.SummaryHealthPercent * 100f),
                downed = p.Downed,
            }).ToList();
        }

        /// Враги в кадре и рядом: расстояние до ближайшего колониста.
        private static object ThreatsState()
        {
            var map = Find.CurrentMap;
            var colonists = map.mapPawns.FreeColonists.ToList();
            if (colonists.Count == 0) return null;
            return map.mapPawns.AllPawnsSpawned.Where(p => p.HostileTo(Faction.OfPlayer) && !p.Downed && !p.Dead).Take(12).Select(p => new
            {
                id = p.ThingID, label = p.LabelShort, x = p.Position.x, z = p.Position.z,
                dist = Mathf.RoundToInt(colonists.Min(c => c.Position.DistanceTo(p.Position))),
                weapon = p.equipment?.Primary?.LabelShort,
            }).ToList();
        }

        // ── шаги «Архитектора» ───────────────────────────────────────────────

        private static MainTabWindow_Architect RequireArchitect()
        {
            if (!InGame) throw new Exception("игра не запущена");
            if (Find.MainTabsRoot.OpenTab != MainButtonDefOf.Architect)
                throw new Exception("Сначала открой вкладку «Архитектор» (open_tab tab=Architect), как это делает игрок.");
            return ArchitectWindow() ?? throw new Exception("окно «Архитектор» недоступно");
        }

        private static string UiCategory(JObject cmd)
        {
            var w = RequireArchitect();
            var id = cmd["id"]?.Value<string>() ?? throw new Exception("нужен id категории (см. ui_state: architect.categories)");
            var tab = ArchitectTabs(w).FirstOrDefault(t => t.def.defName.Equals(id, StringComparison.OrdinalIgnoreCase)
                                                           || t.def.label.Equals(id, StringComparison.OrdinalIgnoreCase))
                ?? throw new Exception($"нет категории «{id}». Есть: {string.Join(", ", ArchitectTabs(w).Select(t => t.def.defName))}");
            Traverse.Create(w).Field("selectedDesPanel").SetValue(tab);
            Find.DesignatorManager.Deselect();
            return UiState();
        }

        private static string UiDesignator(JObject cmd)
        {
            var w = RequireArchitect();
            var sel = SelectedArchitectTab(w) ?? throw new Exception("Сначала выбери категорию (ui_category).");
            var id = cmd["id"]?.Value<string>() ?? throw new Exception("нужен id постройки (см. ui_state: architect.designators)");
            var d = FlatDesignators(sel.def).FirstOrDefault(x => DesignatorId(x).Equals(id, StringComparison.OrdinalIgnoreCase)
                                                                  || x.Label.Equals(id, StringComparison.OrdinalIgnoreCase))
                ?? throw new Exception($"в категории «{sel.def.defName}» нет «{id}». Есть: " +
                                       string.Join(", ", FlatDesignators(sel.def).Select(DesignatorId).Take(ListMax)));
            if (d is Designator_Build build)
            {
                var stuffName = cmd["stuff"]?.Value<string>();
                if (!string.IsNullOrEmpty(stuffName))
                {
                    var stuff = DefDatabase<ThingDef>.GetNamedSilentFail(stuffName) ?? throw new Exception($"нет материала «{stuffName}»");
                    build.SetStuffDef(stuff);
                }
                var rot = cmd["rot"]?.Value<string>();
                if (!string.IsNullOrEmpty(rot))
                    Traverse.Create(build).Field("placingRot").SetValue(rot.ToLowerInvariant() switch
                    {
                        "east" => Rot4.East, "south" => Rot4.South, "west" => Rot4.West, _ => Rot4.North,
                    });
            }
            Find.DesignatorManager.Select(d);
            return UiState();
        }

        private static Designator RequireDesignator()
        {
            if (!InGame) throw new Exception("игра не запущена");
            return Find.DesignatorManager.SelectedDesignator
                ?? throw new Exception("Не выбран инструмент: ui_category и ui_designator (как игрок выбирает кнопку в «Архитекторе»).");
        }

        private static string UiPlace(JObject cmd)
        {
            var d = RequireDesignator();
            var cell = new IntVec3(cmd["x"].Value<int>(), 0, cmd["z"].Value<int>());
            if (!cell.InBounds(Find.CurrentMap)) throw new Exception("клетка вне карты");
            var report = d.CanDesignateCell(cell);
            if (!report.Accepted)
                throw new Exception($"Игра не разрешает {d.Label} на ({cell.x},{cell.z}): " +
                                    (string.IsNullOrEmpty(report.Reason) ? "причина не названа" : report.Reason));
            d.DesignateSingleCell(cell);
            d.Finalize(true);
            bool queued = cell.GetThingList(Find.CurrentMap).Any(t => t is Blueprint || t is Frame);
            return UiState(new JObject { ["action"] = "place", ["x"] = cell.x, ["z"] = cell.z, ["blueprint_there"] = queued });
        }

        private static string UiDrag(JObject cmd)
        {
            var d = RequireDesignator();
            var map = Find.CurrentMap;
            var a = new IntVec3(cmd["x1"].Value<int>(), 0, cmd["z1"].Value<int>());
            var b = new IntVec3(cmd["x2"].Value<int>(), 0, cmd["z2"].Value<int>());
            var shape = cmd["shape"]?.Value<string>() ?? ((a.x == b.x || a.z == b.z) ? "line" : "rect");   // стены линией, комнаты и зоны прямоугольником
            var cells = shape == "line" ? BresenhamLine(a, b).ToList() : CellRect.FromLimits(a, b).Cells.ToList();
            cells = cells.Where(c => c.InBounds(map)).ToList();
            var ok = cells.Where(c => d.CanDesignateCell(c).Accepted).ToList();
            if (ok.Count == 0) throw new Exception($"Игра не разрешает {d.Label} ни в одной из {cells.Count} клеток (занято или нельзя).");
            d.DesignateMultiCell(ok);
            d.Finalize(true);
            return JsonConvert.SerializeObject(new { placed = ok.Count, skipped = cells.Count - ok.Count });
        }

        private static string UiCancel()
        {
            if (!InGame) throw new Exception("игра не запущена");
            Find.DesignatorManager.Deselect();
            return UiState();
        }
    }
}

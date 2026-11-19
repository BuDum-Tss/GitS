using System;
using System.Collections.Generic;
using System.Linq;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using RimWorld;
using Verse;

namespace AgentBridge
{
    /// Остальное меню «Архитектор»: пол (покрытие), крыша, разрешённые зоны колонистов, приказы охоты, сбора и переноса.
    /// Команды отвечают JSON-сводкой «сделано / пропущено (причины)»: агент видит, что не вышло, а не тихие дыры.
    public static partial class CommandExecutor
    {
        private const int FloorMaxCells = 400;       // клеток пола за вызов
        private const int RoofMaxCells = 900;        // клеток крыши за вызов (комната 30x30)
        private const int AreaMaxCells = 2500;       // клеток разрешённой зоны за вызов
        private const int OrderMaxTargets = 200;     // целей приказа охоты, сбора и переноса за вызов

        // ── Общие помощники ──────────────────────────────────────────────────────

        /// Клетки из прямоугольника (x1,z1,x2,z2, порядок углов любой, границы входят) или из списка cells [{x,z}]. Без дублей.
        private static List<IntVec3> ParseAreaCells(JObject cmd, int max)
        {
            var seen = new HashSet<IntVec3>();
            var list = new List<IntVec3>();
            if (cmd["x1"] != null && cmd["z1"] != null && cmd["x2"] != null && cmd["z2"] != null)
            {
                int x1 = cmd["x1"].Value<int>(), x2 = cmd["x2"].Value<int>(), z1 = cmd["z1"].Value<int>(), z2 = cmd["z2"].Value<int>();
                long count = (long)(Math.Abs(x2 - x1) + 1) * (Math.Abs(z2 - z1) + 1);
                if (count > max) throw new Exception($"Слишком много клеток: {count}, за вызов не больше {max}. Раздели область на части.");
                for (int x = Math.Min(x1, x2); x <= Math.Max(x1, x2); x++)
                    for (int z = Math.Min(z1, z2); z <= Math.Max(z1, z2); z++)
                        list.Add(new IntVec3(x, 0, z));
                return list;
            }
            var cells = cmd["cells"]?.ToObject<List<PositionDto>>();
            if (cells == null || cells.Count == 0) throw new Exception("Нужен прямоугольник (x1,z1,x2,z2) или cells=[{x,z}...].");
            foreach (var c in cells)
            {
                var v = new IntVec3(c.X, 0, c.Z);
                if (seen.Add(v)) list.Add(v);
            }
            if (list.Count > max) throw new Exception($"Слишком много клеток: {list.Count}, за вызов не больше {max}. Раздели область на части.");
            return list;
        }

        /// Список причин как «причина: число» по убыванию (для сводки).
        private static List<object> ReasonCounts(Dictionary<string, int> reasons)
            => reasons.OrderByDescending(kv => kv.Value).Select(kv => (object)new { reason = kv.Key, count = kv.Value }).ToList();

        private static void Bump(Dictionary<string, int> reasons, string reason)
            => reasons[reason] = (reasons.TryGetValue(reason, out var n) ? n : 0) + 1;

        // ── Пол (меню «Покрытие», TerrainDef) ───────────────────────────────────

        private static TerrainDef GetTerrainDefOrThrow(string name)
        {
            var def = DefDatabase<TerrainDef>.GetNamed(name, false);
            if (def == null)
                throw new Exception($"Неизвестный TerrainDef (пол): '{name}'. Найди точное имя через search_terrain_defs(query), например WoodPlankFloor, TileSandstone, CarpetRed.");
            return def;
        }

        /// Характеристика пола; 0, если у пола такой нет или расчёт не применим (описание не должно падать из-за одной цифры).
        private static float SafeStat(BuildableDef def, StatDef stat)
        {
            try { return def.GetStatValueAbstract(stat); }
            catch (Exception) { return 0f; }
        }

        private static List<ResearchProjectDef> UnfinishedResearch(BuildableDef def)
            => def.researchPrerequisites?.Where(r => !r.IsFinished).ToList() ?? new List<ResearchProjectDef>();

        /// Материал пола: нужен, только если пол делается из материала (MadeFromStuff); обычные полы это costList.
        private static ThingDef ResolveTerrainStuff(TerrainDef def, JObject cmd)
        {
            var mat = cmd["material"]?.Value<string>();
            if (string.IsNullOrEmpty(mat))
            {
                if (def.MadeFromStuff)
                    throw new Exception($"{def.defName} требует материал: передай material (например WoodLog, Steel, BlocksGranite).");
                return null;
            }
            var stuff = DefDatabase<ThingDef>.GetNamedSilentFail(mat)
                ?? throw new Exception($"Неизвестный материал: '{mat}'. Примеры: WoodLog, Steel, BlocksGranite.");
            if (!def.MadeFromStuff)
                throw new Exception($"{def.defName} не делается из материала: параметр material не нужен (цену задаёт сам пол, см. describe_terrain_def).");
            if (!stuff.IsStuff || !stuff.stuffProps.CanMake(def))
                throw new Exception($"'{mat}' не подходит как материал для {def.defName}.");
            return stuff;
        }

        /// search_terrain_defs: полы по части названия (покрытие). Пустой query покажет все доступные.
        private static string SearchTerrainDefs(JObject cmd)
        {
            var query = (cmd["query"]?.Value<string>() ?? "").Trim().ToLowerInvariant();
            var found = DefDatabase<TerrainDef>.AllDefsListForReading
                .Where(d => d.designationCategory != null)
                .Where(d => query.Length == 0 || d.defName.ToLowerInvariant().Contains(query) || (d.label?.ToLowerInvariant().Contains(query) ?? false))
                .ToList();
            var results = found.Take(30).Select(d =>
            {
                var locked = UnfinishedResearch(d);
                return new
                {
                    def_name = d.defName,
                    label = d.label,
                    made_from_stuff = d.MadeFromStuff,
                    category = d.designationCategory?.defName,
                    cost = d.CostList?.Select(c => new { c.thingDef.defName, c.count }).ToList(),
                    requires_research = locked.Select(r => r.defName).ToList(),
                    available = locked.Count == 0,
                };
            }).ToList();
            return JsonConvert.SerializeObject(new { count = results.Count, total = found.Count, results });
        }

        /// describe_terrain_def: что нужно для пола (стоимость, работа, красота, исследование) и сколько выйдет на count клеток.
        /// Поле requires.research совпадает с describe_def: по нему работает unlock_for.
        private static string DescribeTerrainDef(JObject cmd)
        {
            var map = Find.CurrentMap;
            var def = GetTerrainDefOrThrow(cmd["def_name"].Value<string>());
            var count = Math.Max(1, cmd["count"]?.Value<int>() ?? 1);
            ThingDef stuff = null;
            if (def.MadeFromStuff && !string.IsNullOrEmpty(cmd["material"]?.Value<string>())) stuff = ResolveTerrainStuff(def, cmd);
            var problems = new List<string>();
            object cost = null;
            if (!def.MadeFromStuff || stuff != null)
                cost = CostReport(map, new[] { new CostLine { Def = def, Stuff = stuff, Count = count } }, problems);
            var research = def.researchPrerequisites?.Select(r => new { r.defName, done = r.IsFinished }).ToList();
            var stuffs = def.MadeFromStuff
                ? GenStuff.AllowedStuffsFor(def).Take(14).Select(st => new { stuff = st.defName, in_stock = map.resourceCounter.GetCount(st) }).ToList<object>()
                : new List<object>();
            return JsonConvert.SerializeObject(new
            {
                def = def.defName,
                label = def.label,
                description = (def.description ?? "").Length > 220 ? def.description.Substring(0, 220) + "…" : def.description,
                category = def.designationCategory?.defName,
                made_from_stuff = def.MadeFromStuff,
                materials = stuffs,
                per_cell = new
                {
                    cost = def.CostList?.Select(c => new { c.thingDef.defName, c.count, in_stock = map.resourceCounter.GetCount(c.thingDef) }).ToList(),
                    work = (int)SafeStat(def, StatDefOf.WorkToBuild),
                    beauty = Math.Round(SafeStat(def, StatDefOf.Beauty), 1),
                    cleanliness = Math.Round(SafeStat(def, StatDefOf.Cleanliness), 2),
                },
                count,
                cost,
                problems,
                requires = new { research, construction_skill = def.constructionSkillPrerequisite },
            });
        }

        /// build_floor: чертежи пола на прямоугольнике или списке клеток. Пропускает клетки, где уже такой пол или чертёж, и
        /// те, где нельзя; в ответе число поставленных и причины пропуска. Закрытый исследованием пол отвергается целиком.
        private static string BuildFloor(JObject cmd)
        {
            var map = Find.CurrentMap;
            var def = GetTerrainDefOrThrow(cmd["def_name"].Value<string>());
            var locked = UnfinishedResearch(def);
            if (locked.Count > 0)
                throw new Exception($"ЗАКРЫТО ИССЛЕДОВАНИЕМ: пол {def.defName} откроется после исследования: {string.Join(", ", locked.Select(r => r.defName))}.");
            var stuff = ResolveTerrainStuff(def, cmd);
            var cells = ParseAreaCells(cmd, FloorMaxCells);

            int placed = 0;
            var reasons = new Dictionary<string, int>();
            var samples = new List<object>();
            foreach (var cell in cells)
            {
                string why = null;
                if (!cell.InBounds(map)) why = "вне карты";
                else if (map.terrainGrid.TerrainAt(cell) == def) why = "уже такой пол";
                else if (cell.GetThingList(map).Any(t => t is IConstructible && t.def.entityDefToBuild == def)) why = "уже есть чертёж или стройка этого пола";
                else
                {
                    var report = GenConstruct.CanPlaceBlueprintAt(def, cell, Rot4.North, map);
                    if (!report.Accepted) why = string.IsNullOrEmpty(report.Reason) ? "нельзя построить" : report.Reason;
                }
                if (why != null)
                {
                    Bump(reasons, why);
                    if (samples.Count < 8) samples.Add(new { x = cell.x, z = cell.z, reason = why });
                    continue;
                }
                GenConstruct.PlaceBlueprintForBuild(def, cell, map, Rot4.North, Faction.OfPlayer, stuff);
                placed++;
            }
            var problems = new List<string>();
            object cost = placed > 0
                ? CostReport(map, new[] { new CostLine { Def = def, Stuff = stuff, Count = placed } }, problems) : null;
            return JsonConvert.SerializeObject(new
            {
                def = def.defName, requested = cells.Count, placed, skipped = cells.Count - placed,
                skipped_reasons = ReasonCounts(reasons), skipped_examples = samples, cost, problems,
            });
        }

        // ── Крыша ────────────────────────────────────────────────────────────────

        /// designate_roof: область «Строить крышу» (mode=build) или «Убрать крышу» (mode=remove): те же дизайнаторы игры,
        /// так что проверка опоры (крыша держится не дальше ~6 клеток от стены или колонны), туман и прочее настоящие.
        private static string DesignateRoof(JObject cmd)
        {
            var map = Find.CurrentMap;
            var mode = (cmd["mode"]?.Value<string>() ?? "build").Trim().ToLowerInvariant();
            if (mode != "build" && mode != "remove") throw new Exception("mode должен быть build (строить крышу) или remove (убрать крышу).");
            var cells = ParseAreaCells(cmd, RoofMaxCells);
            Designator designator = mode == "build" ? (Designator)new Designator_AreaBuildRoof() : new Designator_AreaNoRoof();

            int done = 0;
            var reasons = new Dictionary<string, int>();
            var samples = new List<object>();
            foreach (var cell in cells)
            {
                string why = null;
                if (!cell.InBounds(map)) why = "вне карты";
                else if (mode == "build" && cell.Roofed(map)) why = "уже под крышей";
                else if (mode == "remove" && !cell.Roofed(map)) why = "крыши и так нет";
                else
                {
                    var report = designator.CanDesignateCell(cell);
                    if (!report.Accepted) why = string.IsNullOrEmpty(report.Reason) ? "нельзя или уже помечено" : report.Reason;
                }
                if (why != null)
                {
                    Bump(reasons, why);
                    if (samples.Count < 8) samples.Add(new { x = cell.x, z = cell.z, reason = why });
                    continue;
                }
                designator.DesignateSingleCell(cell);
                done++;
            }
            return JsonConvert.SerializeObject(new
            {
                mode, requested = cells.Count, designated = done, skipped = cells.Count - done,
                skipped_reasons = ReasonCounts(reasons), skipped_examples = samples,
                note = done > 0 ? "Область помечена, крышу строят колонисты (работа «Строительство»); убирают тоже они." : null,
            });
        }

        // ── Разрешённые зоны колонистов ──────────────────────────────────────────

        private static Area_Allowed FindAllowedArea(Map map, string name)
            => map.areaManager.AllAreas.OfType<Area_Allowed>().FirstOrDefault(a => string.Equals(a.Label, name, StringComparison.OrdinalIgnoreCase));

        /// create_allowed_area: новая разрешённая зона с именем и клетками; если зона с таким именем уже есть, клетки добавляются.
        private static string CreateAllowedArea(JObject cmd)
        {
            var map = Find.CurrentMap;
            var name = (cmd["name"]?.Value<string>() ?? "").Trim();
            if (name.Length == 0) throw new Exception("Нужно имя зоны (name).");
            var cells = ParseAreaCells(cmd, AreaMaxCells);
            var area = FindAllowedArea(map, name);
            bool created = false;
            if (area == null)
            {
                if (!map.areaManager.TryMakeNewAllowed(out area))
                    throw new Exception("Новую разрешённую зону создать нельзя: достигнут предел зон. Удали ненужную (delete_allowed_area).");
                area.SetLabel(name);
                created = true;
            }
            int added = 0, outside = 0;
            foreach (var cell in cells)
            {
                if (!cell.InBounds(map)) { outside++; continue; }
                if (!area[cell]) { area[cell] = true; added++; }
            }
            return JsonConvert.SerializeObject(new
            {
                name = area.Label, created, added, already_in_area = cells.Count - added - outside, outside_map = outside, total_cells = area.TrueCount,
            });
        }

        private static string DeleteAllowedArea(JObject cmd)
        {
            var map = Find.CurrentMap;
            var name = (cmd["name"]?.Value<string>() ?? "").Trim();
            var area = FindAllowedArea(map, name)
                ?? throw new Exception($"Разрешённой зоны «{name}» нет. Есть: {string.Join(", ", map.areaManager.AllAreas.OfType<Area_Allowed>().Select(a => a.Label))}.");
            int cellsCount = area.TrueCount;
            area.Delete();
            return JsonConvert.SerializeObject(new { deleted = name, cells = cellsCount });
        }

        /// set_pawn_area: ограничить пешку зоной по имени; пустое имя снимает ограничение.
        private static string SetPawnArea(JObject cmd)
        {
            var map = Find.CurrentMap;
            var pawn = GetPawn(cmd);
            if (pawn.playerSettings == null) throw new Exception($"{pawn.LabelShort}: зону этой пешке задать нельзя (не колонист и не твоё животное).");
            var name = (cmd["area_name"]?.Value<string>() ?? "").Trim();
            Area_Allowed area = null;
            if (name.Length > 0)
                area = FindAllowedArea(map, name)
                    ?? throw new Exception($"Разрешённой зоны «{name}» нет. Есть: {string.Join(", ", map.areaManager.AllAreas.OfType<Area_Allowed>().Select(a => a.Label))}. Создай её (create_allowed_area).");
            pawn.playerSettings.AreaRestrictionInPawnCurrentMap = area;
            return JsonConvert.SerializeObject(new { pawn = pawn.LabelShort, id = pawn.ThingID, area = area?.Label, restricted = area != null });
        }

        private static string ListAllowedAreas()
        {
            var map = Find.CurrentMap;
            var areas = map.areaManager.AllAreas.OfType<Area_Allowed>().Select(a =>
            {
                var cells = a.ActiveCells.ToList();
                var pawns = map.mapPawns.AllPawnsSpawned
                    .Where(p => p.playerSettings != null && p.playerSettings.AreaRestrictionInPawnCurrentMap == a)
                    .Select(p => new { id = p.ThingID, name = p.LabelShort }).ToList();
                return new
                {
                    name = a.Label,
                    cells = cells.Count,
                    bounds = cells.Count == 0 ? null : new
                    {
                        x1 = cells.Min(c => c.x), z1 = cells.Min(c => c.z), x2 = cells.Max(c => c.x), z2 = cells.Max(c => c.z),
                    },
                    pawns,
                };
            }).ToList();
            return JsonConvert.SerializeObject(new { count = areas.Count, can_create_more = map.areaManager.CanMakeNewAllowed(), areas });
        }

        // ── Приказы: охота, сбор урожая и срезка, перенос ─────────────────────────

        private static List<string> IdList(JObject cmd, string many, string one)
        {
            var ids = new List<string>();
            if (cmd[many] is JArray arr) ids.AddRange(arr.Select(t => t.Value<string>()));
            var single = cmd[one]?.Value<string>();
            if (!string.IsNullOrEmpty(single)) ids.AddRange(single.Split(',').Select(s => s.Trim()));
            ids = ids.Where(s => !string.IsNullOrEmpty(s)).Distinct().ToList();
            if (ids.Count == 0) throw new Exception($"Нужен {one} или список {many}.");
            if (ids.Count > OrderMaxTargets) throw new Exception($"Слишком много целей: {ids.Count}, за вызов не больше {OrderMaxTargets}.");
            return ids;
        }

        /// designate_hunt: приказ охоты на диких животных (как кнопка «Охота»). Своих и чужих животных не трогает.
        private static string DesignateHunt(JObject cmd)
        {
            var map = Find.CurrentMap;
            var ids = IdList(cmd, "animal_ids", "animal_id");
            int done = 0;
            var skipped = new List<object>();
            foreach (var id in ids)
            {
                var animal = map.mapPawns.AllPawnsSpawned.FirstOrDefault(p => p.ThingID == id);
                string why = null;
                if (animal == null) why = "не найдено на карте";
                else if (!animal.RaceProps.Animal) why = "это не животное";
                else if (animal.Faction != null) why = "не дикое (приручено или чужое): охота только на диких";
                else if (animal.Dead) why = "уже мертво";
                else if (map.designationManager.DesignationOn(animal, DesignationDefOf.Hunt) != null) why = "охота уже назначена";
                if (why != null) { skipped.Add(new { id, reason = why }); continue; }
                map.designationManager.AddDesignation(new Designation(animal, DesignationDefOf.Hunt));
                done++;
            }
            var hunters = map.mapPawns.FreeColonistsSpawned
                .Count(p => !p.WorkTypeIsDisabled(WorkTypeDefOf.Hunting) && p.equipment?.Primary != null && p.equipment.Primary.def.IsRangedWeapon);
            return JsonConvert.SerializeObject(new
            {
                designated = done, skipped_count = skipped.Count, skipped,
                warning = done > 0 && hunters == 0 ? "нет колониста с оружием дальнего боя и включённой работой «Охота»: добыча будет ждать" : null,
            });
        }

        /// designate_harvest: сбор урожая (mode=harvest, спелые растения со съедобным или сырьевым урожаем) или срезка (mode=cut,
        /// растение просто убирают). Цели: plant_ids или cells (растение в клетке).
        private static string DesignateHarvest(JObject cmd)
        {
            var map = Find.CurrentMap;
            var mode = (cmd["mode"]?.Value<string>() ?? "harvest").Trim().ToLowerInvariant();
            if (mode != "harvest" && mode != "cut") throw new Exception("mode должен быть harvest (собрать урожай) или cut (срезать растение).");
            var des = mode == "harvest" ? DesignationDefOf.HarvestPlant : DesignationDefOf.CutPlant;
            var targets = new List<(string label, Plant plant)>();
            if (cmd["plant_ids"] != null || cmd["plant_id"] != null)
            {
                foreach (var id in IdList(cmd, "plant_ids", "plant_id"))
                    targets.Add((id, map.listerThings.AllThings.OfType<Plant>().FirstOrDefault(p => p.ThingID == id)));
            }
            else
            {
                foreach (var cell in ParseAreaCells(cmd, FloorMaxCells * 4))
                {
                    var plants = cell.InBounds(map) ? cell.GetThingList(map).OfType<Plant>().ToList() : new List<Plant>();
                    if (plants.Count == 0) targets.Add(($"({cell.x},{cell.z})", null));
                    foreach (var p in plants) targets.Add(($"({cell.x},{cell.z})", p));
                }
            }
            int done = 0;
            var reasons = new Dictionary<string, int>();
            var samples = new List<object>();
            foreach (var (label, plant) in targets)
            {
                string why = null;
                if (plant == null) why = "там нет растения";
                else if (map.designationManager.DesignationOn(plant, des) != null) why = "приказ уже назначен";
                else if (mode == "harvest" && plant.def.plant.harvestedThingDef == null) why = "с этого растения нечего собирать (для срезки mode=cut)";
                else if (mode == "harvest" && !plant.HarvestableNow) why = "ещё не созрело";
                if (why != null)
                {
                    Bump(reasons, why);
                    if (samples.Count < 8) samples.Add(new { target = label, reason = why });
                    continue;
                }
                map.designationManager.AddDesignation(new Designation(plant, des));
                done++;
            }
            return JsonConvert.SerializeObject(new
            {
                mode, requested = targets.Count, designated = done, skipped = targets.Count - done,
                skipped_reasons = ReasonCounts(reasons), skipped_examples = samples,
            });
        }

        /// designate_haul: приказ перенести предметы на склад (кнопка «Перенести»). Только переносимые, лежащие на карте.
        private static string DesignateHaul(JObject cmd)
        {
            var map = Find.CurrentMap;
            var ids = IdList(cmd, "thing_ids", "thing_id");
            var byId = map.spawnedThings.Where(t => ids.Contains(t.ThingID)).ToDictionary(t => t.ThingID, t => t);
            int done = 0;
            var skipped = new List<object>();
            foreach (var id in ids)
            {
                string why = null;
                byId.TryGetValue(id, out var thing);
                if (thing == null) why = "не найдено на карте";
                else if (!thing.def.designateHaulable) why = "этот предмет не переносят приказом (здание, растение или слишком тяжёлое)";
                else if (map.designationManager.DesignationOn(thing, DesignationDefOf.Haul) != null) why = "перенос уже назначен";
                if (why != null) { skipped.Add(new { id, reason = why }); continue; }
                map.designationManager.AddDesignation(new Designation(thing, DesignationDefOf.Haul));
                done++;
            }
            return JsonConvert.SerializeObject(new { designated = done, skipped_count = skipped.Count, skipped });
        }
    }
}

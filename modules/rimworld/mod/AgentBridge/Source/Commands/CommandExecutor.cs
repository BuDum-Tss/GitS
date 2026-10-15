using System;
using System.Collections.Generic;
using System.Linq;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using RimWorld;
using UnityEngine;
using Verse;
using Verse.AI;

namespace AgentBridge
{
    public static partial class CommandExecutor
    {
        // ── Очередь: последовательное исполнение ─────────────────────────────────
        // Команда выполняется как ход живого игрока: сначала камера доезжает до места (и выделяется пешка), потом действие,
        // потом короткая пауза, чтобы действие было видно. Команды идут строго по одной. Включено вместе с автокамерой.
        private static string _stagedJson;
        private static System.Threading.Tasks.TaskCompletionSource<string> _stagedTcs;
        private static float _notBefore;                 // до этого момента следующую команду не берём

        private static readonly HashSet<string> StageTypes = new HashSet<string>
        {
            "build", "place_line", "cancel_construction", "deconstruct", "designate_mine", "designate_chop", "designate_smooth",
            "set_zone_home", "create_grow_zone", "create_stockpile", "delete_zone", "plan_room", "build_room",
            "draft", "undraft", "move", "attack", "cancel_job", "equip", "wear", "rescue", "capture", "tend", "force_work",
            "clear_work_queue", "add_bill", "cancel_bill", "order_tame", "order_slaughter", "open_tab",
            "set_work_priority", "set_manual_priorities", "set_schedule", "set_schedule_range", "set_medical_care", "research",
            "look_at", "forbid", "unforbid",   // осмотр клетки и запрет предмета: камера тоже едет туда
        };

        public static void ProcessQueue()
        {
            while (true)
            {
                var now = Time.realtimeSinceStartup;
                if (now < _notBefore) return;
                string json; System.Threading.Tasks.TaskCompletionSource<string> tcs;
                bool staged = false;
                if (_stagedJson != null) { json = _stagedJson; tcs = _stagedTcs; _stagedJson = null; _stagedTcs = null; staged = true; }
                else if (!AgentBridgeMod.Commands.TryDequeue(out json, out tcs)) return;

                bool compact = AgentBridgeMod.Commands.Count > 2;      // очередь копится (пачка): показ сжимается, но не пропускается
                if (!staged && AutoCamera && InGame)
                {
                    float delay = 0f;
                    try
                    {
                        var sent = JObject.Parse(json);
                        var t = sent["type"]?.Value<string>();
                        if (t != null && StageTypes.Contains(t)) delay = AutoCameraFor(t, sent, compact);
                    }
                    catch (Exception camErr) { Log.Warning($"[AgentBridge] camera: {camErr.Message}"); }
                    if (delay > 0f) { _stagedJson = json; _stagedTcs = tcs; _notBefore = now + delay; return; }
                }

                if (staged) { string stagedType = null; try { stagedType = JObject.Parse(json)["type"]?.Value<string>(); } catch (Exception) { } ApplyStageClick(stagedType); }
                try
                {
                    var result = Execute(json);
                    tcs?.TrySetResult(result ?? "OK");
                    if (AutoCamera && InGame) _notBefore = Time.realtimeSinceStartup + DwellAfter(json, compact);
                    if (AutoCamera && AgentBridgeMod.AiCursorActive) AgentBridgeMod.AiCursorLastUpdate = Time.realtimeSinceStartup;   // курсор живёт, пока идёт игра
                }
                catch (Exception e)
                {
                    Log.Error($"[AgentBridge] Command error: {e.Message}\n{json}");
                    tcs?.TrySetResult($"ERROR: {e.Message}");
                }
            }
        }

        /// Пауза после действия, чтобы зритель успел его увидеть (только для действий в мире).
        private static float DwellAfter(string json, bool compact)
        {
            try
            {
                var type = JObject.Parse(json)["type"]?.Value<string>() ?? "";
                float dwell = RequiredTab.ContainsKey(type) ? 0.35f     // правки в таблице идут по одной, цифры меняются на глазах
                    : StageTypes.Contains(type) ? 0.7f : 0f;
                return compact ? Math.Min(dwell, 0.1f) : dwell;        // в пачке короткая пауза: зритель видит, но очередь не стоит
            }
            catch (Exception) { return 0f; }
        }

        /// Возвращает null для обычных команд (тогда ProcessQueue подставит "OK"),
        /// либо JSON-строку с данными для команд-запросов (find_buildable_spot и т.п.).
        private static string Execute(string json)
        {
            var cmd = JObject.Parse(json);
            var type = cmd["type"]?.Value<string>()
                ?? throw new ArgumentException("Missing 'type' field");

            Log.Message($"[AgentBridge] Executing: {type}");
            CheckTabGate(type);

            switch (type)
            {
                // ── Пешки ───────────────────────────────────────────────────
                case "set_work_priority":  SetWorkPriority(cmd); break;
                case "set_schedule":       SetSchedule(cmd); break;
                case "set_medical_care":   SetMedicalCare(cmd); break;
                case "draft":              SetDrafted(cmd, true); break;
                case "undraft":            SetDrafted(cmd, false); break;
                case "move":               MovePawn(cmd); break;
                case "attack":             AttackSmart(cmd); break;
                case "cancel_job":         CancelJob(cmd); break;
                case "equip":              EquipViaJob(cmd); break;
                case "wear":               WearViaJob(cmd); break;
                case "rescue":             Rescue(cmd); break;
                case "capture":            Capture(cmd); break;
                case "tend":               Tend(cmd); break;
                case "get_items":          return GetItems(cmd);
                case "force_work":         return ForceWork(cmd);
                case "clear_work_queue":   ClearWorkQueue(cmd); break;
                case "set_manual_priorities": SetManualPriorities(cmd); break;
                case "capabilities":       return Capabilities();

                // ── Строительство ────────────────────────────────────────────
                case "build":              PlaceBuild(cmd); break;
                case "cancel_construction":CancelConstruction(cmd); break;
                case "deconstruct":        Deconstruct(cmd); break;
                case "designate_mine":     DesignateMine(cmd); break;
                case "designate_chop":     DesignateChop(cmd); break;
                case "designate_smooth":   DesignateSmooth(cmd); break;
                case "find_buildable_spot":return FindBuildableSpot(cmd);
                case "place_line":         return PlaceLine(cmd);
                case "plan_room":          return PlanRoom(cmd);
                case "check_enclosure":    return CheckEnclosure(cmd);
                case "fit_room":           return FitRoom(cmd);
                case "survey_map":         return SurveyMap(cmd);
                case "estimate_build":     return EstimateBuild(cmd);
                case "describe_def":       return DescribeDef(cmd);
                case "get_work_table":     return GetWorkTable(cmd);
                case "analyze_area":       return AnalyzeArea(cmd);
                case "camera_jump":        return CameraJump(cmd);
                case "camera_follow":      return CameraFollow(cmd);
                case "open_tab":           return OpenTab(cmd);
                case "select":             return SelectTarget(cmd);
                case "set_auto_camera":    AutoCamera = cmd["enabled"]?.Value<bool>() ?? true; break;
                case "set_user_camera_override":
                    UserCamera.Enabled = cmd["enabled"]?.Value<bool>() ?? true;
                    UserCamera.OverrideSeconds = Mathf.Clamp(cmd["seconds"]?.Value<float>() ?? UserCamera.DefaultOverrideSeconds, 1f, 30f);
                    break;
                case "set_strict_ui":      StrictUi = cmd["enabled"]?.Value<bool>() ?? true; break;
                case "camera_zoom":        return CameraZoom(cmd);
                case "camera_move":        return CameraMove(cmd);
                case "camera_view":        return CameraView();
                case "build_room":         return BuildRoom(cmd);
                case "clear_plans":        return ClearPlans(cmd);
                case "room_info":          return RoomInfo(cmd);
                case "find_nearest":       return FindNearest(cmd);
                case "search_thing_defs":  return SearchThingDefs(cmd);
                case "set_ai_cursor":
                    AgentBridgeMod.AiCursorActive = true;
                    AgentBridgeMod.AiCursorX = cmd["x"].Value<float>();
                    AgentBridgeMod.AiCursorY = cmd["y"].Value<float>();
                    AgentBridgeMod.AiCursorLastUpdate = Time.realtimeSinceStartup;
                    break;
                case "clear_ai_cursor":
                    AgentBridgeMod.AiCursorActive = false;
                    break;

                // ── Зоны ─────────────────────────────────────────────────────
                case "set_zone_home":      SetHomeZone(cmd); break;
                case "create_grow_zone":   CreateGrowZone(cmd); break;
                case "create_stockpile":   CreateStockpile(cmd); break;
                case "delete_zone":        DeleteZone(cmd); break;

                // ── Производство ─────────────────────────────────────────────
                case "add_bill":           AddBill(cmd); break;
                case "list_recipes":       return ListRecipes(cmd);
                case "look_at":            return LookAt(cmd);
                case "cancel_bill":        CancelBill(cmd); break;

                // ── Животные ─────────────────────────────────────────────────
                case "order_tame":         OrderTame(cmd); break;
                case "order_slaughter":    OrderSlaughter(cmd); break;

                // ── Предметы ─────────────────────────────────────────────────
                case "forbid":             SetForbidden(cmd, true); break;
                case "unforbid":           SetForbidden(cmd, false); break;

                // ── Исследования ─────────────────────────────────────────────
                case "research":           SetResearch(cmd); break;

                // ── Камера ───────────────────────────────────────────────────
                case "camera_mode": case "camera_goto": case "camera_track": case "camera_release": case "camera_state":
                    return CameraCommand(type, cmd);

                // ── Управление ───────────────────────────────────────────────
                case "pause":
                    if (!Find.TickManager.Paused) Find.TickManager.TogglePaused();
                    break;
                case "resume":
                    if (Find.TickManager.Paused) Find.TickManager.TogglePaused();
                    break;
                case "set_time_speed":
                    Find.TickManager.CurTimeSpeed = (TimeSpeed)cmd["speed"].Value<int>();
                    break;

                default:
                    Log.Warning($"[AgentBridge] Unknown command: {type}");
                    throw new Exception($"unknown command: {type} (мод старее инструментов агента: нужна пересборка мода)");
            }

            CameraDirector.OnCommand(type, cmd);
            return null;
        }

        // ── Пешки ──────────────────────────────────────────────────────────────

        private static void SetWorkPriority(JObject cmd)
        {
            var pawn = GetPawn(cmd);
            var workTypeDef = DefDatabase<WorkTypeDef>.GetNamed(cmd["work_type"].Value<string>());
            pawn.workSettings.SetPriority(workTypeDef, cmd["priority"].Value<int>());
        }

        private static void SetSchedule(JObject cmd)
        {
            var pawn = GetPawn(cmd);
            if (pawn.timetable == null) return;
            var hour = cmd["hour"].Value<int>();
            var typeName = cmd["schedule_type"].Value<string>(); // Work/Joy/Sleep/Anything/Meditate
            var def = DefDatabase<TimeAssignmentDef>.GetNamed(typeName);
            pawn.timetable.SetAssignment(hour, def);
        }

        private static void SetMedicalCare(JObject cmd)
        {
            var pawn = GetPawn(cmd);
            if (pawn.playerSettings == null) return;
            pawn.playerSettings.medCare = (MedicalCareCategory)cmd["level"].Value<int>();
        }

        private static void SetDrafted(JObject cmd, bool drafted)
        {
            var ids = cmd["pawn_ids"]?.Values<string>().ToList()
                ?? new List<string> { cmd["pawn_id"].Value<string>() };
            foreach (var id in ids)
            {
                var pawn = GetPawnById(id);
                if (pawn.drafter != null)
                    pawn.drafter.Drafted = drafted;
            }
        }

        private static void MovePawn(JObject cmd)
        {
            var pawn = GetPawn(cmd);
            var dest = new IntVec3(cmd["x"].Value<int>(), 0, cmd["z"].Value<int>());
            pawn.jobs.TryTakeOrderedJob(JobMaker.MakeJob(JobDefOf.Goto, dest), JobTag.Misc);
        }

        private static void AttackTarget(JObject cmd)
        {
            var pawn = GetPawn(cmd);
            var targetId = cmd["target_id"].Value<string>();
            var target = Find.CurrentMap.mapPawns.AllPawnsSpawned
                .FirstOrDefault(p => p.ThingID == targetId)
                ?? throw new Exception($"Target not found: {targetId}");
            pawn.jobs.TryTakeOrderedJob(JobMaker.MakeJob(JobDefOf.AttackMelee, target), JobTag.Misc);
        }

        private static void CancelJob(JObject cmd)
        {
            GetPawn(cmd).jobs.EndCurrentJob(JobCondition.InterruptForced, true);
        }

        private static void EquipItem(JObject cmd)
        {
            var pawn   = GetPawn(cmd);
            var itemId = cmd["item_id"].Value<string>();
            var item   = Find.CurrentMap.spawnedThings
                .OfType<ThingWithComps>()
                .FirstOrDefault(t => t.ThingID == itemId)
                ?? throw new Exception($"Item not found: {itemId}");

            if (item.def.equipmentType == EquipmentType.None)
                throw new Exception($"{item.def.defName} is not equippable");

            ThingWithComps dropped = null;
            pawn.equipment.TryDropEquipment(pawn.equipment.Primary, out dropped, pawn.Position);
            pawn.equipment.AddEquipment(item);
        }

        // ── Строительство ──────────────────────────────────────────────────────

        private static void PlaceBuild(JObject cmd)
        {
            var map  = Find.CurrentMap;
            var def  = GetThingDefOrThrow(cmd["def_name"].Value<string>());
            var pos  = new IntVec3(cmd["x"].Value<int>(), 0, cmd["z"].Value<int>());
            var rot  = (cmd["rotation"]?.Value<string>() ?? "North") switch
            {
                "East"  => Rot4.East,
                "South" => Rot4.South,
                "West"  => Rot4.West,
                _       => Rot4.North
            };
            var stuff = ResolveStuff(def, cmd);

            var inWall = WallLineConflict(map, def, pos, rot);
            if (inWall != null) throw new Exception(inWall);

            var already = AlreadyThere(map, pos, def, stuff);
            if (already != null)
                throw new Exception($"На ({pos.x},{pos.z}) уже {already}: ставить то же самое поверх не нужно. " +
                                    $"Проверь look_at(x, z), прежде чем размечать.");

            var report = GenConstruct.CanPlaceBlueprintAt(def, pos, rot, map);
            if (!report.Accepted)
            {
                var reason = string.IsNullOrEmpty(report.Reason) ? "неизвестная причина" : report.Reason;
                throw new Exception($"Нельзя построить {def.defName} на ({pos.x},{pos.z}): {reason}. " +
                                    $"Используй find_buildable_spot чтобы найти подходящую клетку рядом.");
            }

            GenConstruct.PlaceBlueprintForBuild(def, pos, map, rot, Faction.OfPlayer, stuff);
        }

        /// Ставит чертежи вдоль прямой линии между двумя точками (алгоритм Брезенхэма) —
        /// для стен/заборов, чтобы не полагаться на ручной перебор координат по одной,
        /// который на практике даёт "столбики через промежутки" вместо сплошной линии.
        private static string PlaceLine(JObject cmd)
        {
            var map   = Find.CurrentMap;
            var def   = GetThingDefOrThrow(cmd["def_name"].Value<string>());
            var start = new IntVec3(cmd["x1"].Value<int>(), 0, cmd["z1"].Value<int>());
            var end   = new IntVec3(cmd["x2"].Value<int>(), 0, cmd["z2"].Value<int>());
            var stuff = ResolveStuff(def, cmd);

            // сначала проверяем ВСЮ линию: если хоть одна клетка занята, не ставим ничего и называем, чем занята
            // (иначе в стене остаются тихие дыры, а контур не замыкается)
            bool strict = cmd["strict"]?.Value<bool>() ?? true;
            var todo = new List<IntVec3>();
            var blocked = new List<object>();
            foreach (var cell in BresenhamLine(start, end))
            {
                if (!cell.InBounds(map)) { blocked.Add(new { x = cell.x, z = cell.z, reason = "вне карты" }); continue; }
                if (AlreadyThere(map, cell, def, stuff) != null) continue;
                var report = GenConstruct.CanPlaceBlueprintAt(def, cell, Rot4.North, map);
                if (report.Accepted) todo.Add(cell);
                else blocked.Add(new { x = cell.x, z = cell.z, reason = (report.Reason ?? "нельзя") + Occupants(map, cell) });
            }
            if (blocked.Count > 0 && strict)
                return JsonConvert.SerializeObject(new
                {
                    placed = 0,
                    error = $"линия не построена: {blocked.Count} клеток заняты. Стена с дырами не замкнёт контур. Подбери маршрут вокруг (fit_room), убери помеху или передай strict=false.",
                    blocked = blocked.Take(12),
                });
            int placed = 0;
            foreach (var cell in todo) { GenConstruct.PlaceBlueprintForBuild(def, cell, map, Rot4.North, Faction.OfPlayer, stuff); placed++; }
            return JsonConvert.SerializeObject(new { placed, skipped_count = blocked.Count, skipped = blocked.Take(10) });
        }

        private static IEnumerable<IntVec3> BresenhamLine(IntVec3 a, IntVec3 b)
        {
            int x0 = a.x, z0 = a.z, x1 = b.x, z1 = b.z;
            int dx = Math.Abs(x1 - x0), dz = Math.Abs(z1 - z0);
            int sx = x0 < x1 ? 1 : -1;
            int sz = z0 < z1 ? 1 : -1;
            int err = dx - dz;

            while (true)
            {
                yield return new IntVec3(x0, 0, z0);
                if (x0 == x1 && z0 == z1) yield break;
                int e2 = 2 * err;
                if (e2 > -dz) { err -= dz; x0 += sx; }
                if (e2 < dx)  { err += dx; z0 += sz; }
            }
        }

        /// Ищет ближайшую к (x,z) клетку, на которой реально можно построить def_name,
        /// перебирая клетки по возрастанию расстояния (макс. radius).
        private static string FindBuildableSpot(JObject cmd)
        {
            var map      = Find.CurrentMap;
            var def      = GetThingDefOrThrow(cmd["def_name"].Value<string>());
            var center   = new IntVec3(cmd["x"].Value<int>(), 0, cmd["z"].Value<int>());
            var radius   = cmd["radius"]?.Value<int>() ?? 15;
            var rot      = (cmd["rotation"]?.Value<string>() ?? "North") switch
            {
                "East"  => Rot4.East,
                "South" => Rot4.South,
                "West"  => Rot4.West,
                _       => Rot4.North
            };

            foreach (var cell in GenRadial.RadialCellsAround(center, radius, true))
            {
                if (!cell.InBounds(map)) continue;
                if (GenConstruct.CanPlaceBlueprintAt(def, cell, rot, map).Accepted)
                    return JsonConvert.SerializeObject(new { found = true, x = cell.x, z = cell.z });
            }

            return JsonConvert.SerializeObject(new
            {
                found = false,
                message = $"Не найдено подходящей клетки для {def.defName} в радиусе {radius} от ({center.x},{center.z})"
            });
        }

        /// Ищет ближайшие объекты на карте по defName или по категории
        /// (tree/mineable/water) — аналог игрового поиска/фильтра по карте.
        private static string FindNearest(JObject cmd)
        {
            var map        = Find.CurrentMap;
            var query      = cmd["query"].Value<string>();
            var center     = new IntVec3(cmd["x"].Value<int>(), 0, cmd["z"].Value<int>());
            var radius     = cmd["radius"]?.Value<int>() ?? 60;
            var maxResults = cmd["max_results"]?.Value<int>() ?? 5;

            if (query.ToLower() == "water")
                return FindNearestWaterCell(map, center, radius);

            IEnumerable<Thing> candidates = query.ToLower() switch
            {
                "tree"     => map.listerThings.AllThings.Where(t => t is Plant p && p.def.plant != null && p.def.plant.IsTree),
                "mineable" => map.listerThings.AllThings.Where(t => t.def.mineable),
                "rock"     => map.listerThings.AllThings.Where(t => t.def.mineable),
                "ore"      => map.listerThings.AllThings.Where(t => t.def.mineable),
                _          => map.listerThings.AllThings.Where(t => t.def.defName == query),
            };

            var results = candidates
                .Where(t => t.Position.InBounds(map) && t.Position.DistanceTo(center) <= radius)
                .OrderBy(t => t.Position.DistanceTo(center))
                .Take(maxResults)
                .Select(t => new { id = t.ThingID, def = t.def.defName, x = t.Position.x, z = t.Position.z })
                .ToList();

            return JsonConvert.SerializeObject(new { count = results.Count, results });
        }

        /// Ищет ThingDef по подстроке в имени/названии — чтобы не гадать точное
        /// имя (например "Wall_Steel" не существует, правильно def_name="Wall"
        /// + material="Steel"). Только сооружаемые постройки (BuildableByPlayer).
        private static string SearchThingDefs(JObject cmd)
        {
            var query = cmd["query"].Value<string>().ToLower();
            var results = DefDatabase<ThingDef>.AllDefsListForReading
                .Where(d => d.category == ThingCategory.Building && d.BuildableByPlayer)
                .Where(d => d.defName.ToLower().Contains(query) ||
                           (d.label?.ToLower().Contains(query) ?? false))
                .Take(15)
                .Select(d => new
                {
                    def_name       = d.defName,
                    label          = d.label,
                    made_from_stuff = d.MadeFromStuff,
                })
                .ToList();

            return JsonConvert.SerializeObject(new { count = results.Count, results });
        }

        private static string FindNearestWaterCell(Map map, IntVec3 center, int radius)
        {
            foreach (var cell in GenRadial.RadialCellsAround(center, radius, true))
            {
                if (!cell.InBounds(map)) continue;
                if (cell.GetEdifice(map) == null && !cell.Standable(map))
                    return JsonConvert.SerializeObject(new
                    {
                        count = 1,
                        results = new[] { new { x = cell.x, z = cell.z } }
                    });
            }
            return JsonConvert.SerializeObject(new { count = 0, results = new object[0] });
        }

        private static void CancelConstruction(JObject cmd)
        {
            var pos = new IntVec3(cmd["x"].Value<int>(), 0, cmd["z"].Value<int>());
            var map = Find.CurrentMap;
            foreach (var thing in map.thingGrid.ThingsListAt(pos).ToList())
            {
                if (thing is Blueprint || thing is Frame)
                    thing.Destroy(DestroyMode.Cancel);
            }
        }

        private static void Deconstruct(JObject cmd)
        {
            var buildingId = cmd["building_id"].Value<string>();
            var building   = Find.CurrentMap.listerBuildings.allBuildingsColonist
                .FirstOrDefault(b => b.ThingID == buildingId)
                ?? throw new Exception($"Building not found: {buildingId}");
            var map = Find.CurrentMap;
            if (map.designationManager.DesignationOn(building, DesignationDefOf.Deconstruct) == null)
                map.designationManager.AddDesignation(new Designation(building, DesignationDefOf.Deconstruct));
        }

        private static void DesignateMine(JObject cmd)
        {
            var map   = Find.CurrentMap;
            var cells = cmd["cells"].ToObject<List<PositionDto>>();
            foreach (var cell in cells)
            {
                var pos = new IntVec3(cell.X, 0, cell.Z);
                var mineable = map.thingGrid.ThingAt(pos, ThingCategory.Building);
                if (mineable != null && mineable.def.mineable
                    && map.designationManager.DesignationOn(mineable, DesignationDefOf.Mine) == null)
                    map.designationManager.AddDesignation(new Designation(mineable, DesignationDefOf.Mine));
            }
        }

        private static void DesignateChop(JObject cmd)
        {
            var map   = Find.CurrentMap;
            var cells = cmd["cells"].ToObject<List<PositionDto>>();
            foreach (var cell in cells)
            {
                var pos  = new IntVec3(cell.X, 0, cell.Z);
                var tree = map.thingGrid.ThingAt(pos, ThingCategory.Plant) as Plant;
                if (tree != null && tree.def.plant?.IsTree == true
                    && map.designationManager.DesignationOn(tree, DesignationDefOf.CutPlant) == null)
                    map.designationManager.AddDesignation(new Designation(tree, DesignationDefOf.CutPlant));
            }
        }

        private static void DesignateSmooth(JObject cmd)
        {
            var map   = Find.CurrentMap;
            var cells = cmd["cells"].ToObject<List<PositionDto>>();
            foreach (var cell in cells)
            {
                var pos    = new IntVec3(cell.X, 0, cell.Z);
                var target = new LocalTargetInfo(pos);
                if (map.designationManager.DesignationAt(pos, DesignationDefOf.SmoothFloor) == null)
                    map.designationManager.AddDesignation(new Designation(target, DesignationDefOf.SmoothFloor));
            }
        }

        // ── Зоны ───────────────────────────────────────────────────────────────

        private static void SetHomeZone(JObject cmd)
        {
            var map     = Find.CurrentMap;
            var cells   = cmd["cells"]?.ToObject<List<PositionDto>>();
            if (cells == null) return;
            var enabled = cmd["enabled"]?.Value<bool>() ?? true;
            foreach (var cell in cells)
                map.areaManager.Home[new IntVec3(cell.X, 0, cell.Z)] = enabled;
        }

        private static void CreateGrowZone(JObject cmd)
        {
            var map   = Find.CurrentMap;
            var cells = cmd["cells"].ToObject<List<PositionDto>>();
            var zone  = new Zone_Growing(map.zoneManager);
            map.zoneManager.RegisterZone(zone);
            foreach (var cell in cells)
                zone.AddCell(new IntVec3(cell.X, 0, cell.Z));
        }

        private static void CreateStockpile(JObject cmd)
        {
            var map      = Find.CurrentMap;
            var cells    = cmd["cells"].ToObject<List<PositionDto>>();
            var priority = (StoragePriority)(cmd["priority"]?.Value<int>() ?? 2);
            var zone     = new Zone_Stockpile(StorageSettingsPreset.DefaultStockpile, map.zoneManager);
            zone.settings.Priority = priority;
            map.zoneManager.RegisterZone(zone);
            foreach (var cell in cells)
                zone.AddCell(new IntVec3(cell.X, 0, cell.Z));
        }

        private static void DeleteZone(JObject cmd)
        {
            var zoneId = cmd["zone_id"].Value<string>();
            var zone   = Find.CurrentMap.zoneManager.AllZones
                .FirstOrDefault(z => z.GetUniqueLoadID() == zoneId)
                ?? throw new Exception($"Zone not found: {zoneId}");
            zone.Delete();
        }

        // ── Производство ───────────────────────────────────────────────────────

        // Рецепты, которые можно поставить на верстак СЕЙЧАС (с точными именами RecipeDef): без этого агент угадывает имена.
        /// Есть ли в клетке уже то же самое: готовая постройка того же вида и материала, чертёж или каркас. Иначе null.
        private static string AlreadyThere(Map map, IntVec3 cell, ThingDef def, ThingDef stuff)
        {
            foreach (var t in cell.GetThingList(map))
            {
                if (t is Building b && b.def == def && b.Stuff == stuff) return $"стоит готовая постройка {def.defName}";
                if (t is IConstructible && t.def.entityDefToBuild == def)
                    return t.def.IsBlueprint ? $"есть чертёж {def.defName}" : $"строится каркас {def.defName}";
            }
            return null;
        }

        // Что реально лежит и стоит вокруг клетки: чтобы агент не ставил вслепую и мог проверить результат разметки.
        private static string LookAt(JObject cmd)
        {
            var map = Find.CurrentMap;
            var c = new IntVec3(cmd["x"].Value<int>(), 0, cmd["z"].Value<int>());
            int radius = Math.Min(cmd["radius"]?.Value<int>() ?? 3, 6);
            var things = new List<object>();
            foreach (var cell in GenRadial.RadialCellsAround(c, radius, true))
            {
                if (!cell.InBounds(map)) continue;
                foreach (var t in cell.GetThingList(map))
                {
                    string kind = t is Pawn ? "pawn" : t is Frame ? "frame" : t.def.IsBlueprint ? "blueprint"
                        : t is Building ? "building" : t is Plant ? "plant" : t.def.category == ThingCategory.Item ? "item" : "other";
                    string def = t.def.IsBlueprint || t is Frame ? (t.def.entityDefToBuild?.defName ?? t.def.defName) : t.def.defName;
                    things.Add(new { x = cell.x, z = cell.z, kind, def, stuff = t.Stuff?.defName, id = t.ThingID });
                    if (things.Count >= 80) break;
                }
                var des = map.designationManager.AllDesignationsAt(cell).FirstOrDefault();
                if (des != null) things.Add(new { x = cell.x, z = cell.z, kind = "designation", def = des.def.defName, stuff = (string)null, id = (string)null });
                if (things.Count >= 80) break;
            }
            return JsonConvert.SerializeObject(new { center = new { x = c.x, z = c.z }, radius, count = things.Count, things });
        }

        private static string ListRecipes(JObject cmd)
        {
            var workbenchId = cmd["workbench_id"]?.Value<string>();
            var query = cmd["query"]?.Value<string>()?.ToLower() ?? "";
            var workbench = Find.CurrentMap.listerBuildings.allBuildingsColonist
                .FirstOrDefault(b => b.ThingID == workbenchId)
                ?? throw new Exception($"Workbench not found: {workbenchId}");
            var recipes = workbench.def.AllRecipes
                .Where(r => r.AvailableNow)
                .Where(r => query == "" || r.defName.ToLower().Contains(query) || (r.label?.ToLower().Contains(query) ?? false))
                .Take(40)
                .Select(r => new { recipe = r.defName, label = r.label })
                .ToList();
            return JsonConvert.SerializeObject(new { workbench = workbench.def.defName, count = recipes.Count, recipes });
        }

        private static void AddBill(JObject cmd)
        {
            var workbenchId = cmd["workbench_id"].Value<string>();
            var recipeName  = cmd["recipe"].Value<string>();
            var count       = cmd["count"]?.Value<int>() ?? 1;

            var workbench = Find.CurrentMap.listerBuildings.allBuildingsColonist
                .FirstOrDefault(b => b.ThingID == workbenchId)
                ?? throw new Exception($"Workbench not found: {workbenchId}");

            if (workbench is not IBillGiver billGiver)
                throw new Exception($"{workbenchId} is not a bill giver");

            var recipe = DefDatabase<RecipeDef>.GetNamed(recipeName, false)
                ?? throw new Exception($"Неизвестный рецепт: '{recipeName}'. Вызови list_recipes(workbench_id), " +
                                       $"чтобы получить точные имена рецептов этого верстака.");
            var bill   = (Bill_Production)recipe.MakeNewBill();
            bill.repeatCount = count;
            billGiver.BillStack.AddBill(bill);
        }

        private static void CancelBill(JObject cmd)
        {
            var workbenchId = cmd["workbench_id"].Value<string>();
            var billId      = cmd["bill_id"].Value<string>();

            var workbench = Find.CurrentMap.listerBuildings.allBuildingsColonist
                .FirstOrDefault(b => b.ThingID == workbenchId)
                ?? throw new Exception($"Workbench not found: {workbenchId}");

            if (workbench is not IBillGiver billGiver)
                throw new Exception($"{workbenchId} is not a bill giver");

            var bill = billGiver.BillStack.Bills.FirstOrDefault(b => b.GetUniqueLoadID() == billId)
                ?? throw new Exception($"Bill not found: {billId}");
            billGiver.BillStack.Delete(bill);
        }

        // ── Животные ───────────────────────────────────────────────────────────

        private static void OrderTame(JObject cmd)
        {
            var animalId = cmd["animal_id"].Value<string>();
            var animal   = Find.CurrentMap.mapPawns.AllPawnsSpawned
                .FirstOrDefault(p => p.ThingID == animalId && p.RaceProps.Animal)
                ?? throw new Exception($"Animal not found: {animalId}");
            var map = Find.CurrentMap;
            if (map.designationManager.DesignationOn(animal, DesignationDefOf.Tame) == null)
                map.designationManager.AddDesignation(new Designation(animal, DesignationDefOf.Tame));
        }

        private static void OrderSlaughter(JObject cmd)
        {
            var animalId = cmd["animal_id"].Value<string>();
            var animal   = Find.CurrentMap.mapPawns.AllPawnsSpawned
                .FirstOrDefault(p => p.ThingID == animalId && p.RaceProps.Animal)
                ?? throw new Exception($"Animal not found: {animalId}");
            var map = Find.CurrentMap;
            if (map.designationManager.DesignationOn(animal, DesignationDefOf.Slaughter) == null)
                map.designationManager.AddDesignation(new Designation(animal, DesignationDefOf.Slaughter));
        }

        // ── Предметы ───────────────────────────────────────────────────────────

        private static void SetForbidden(JObject cmd, bool forbidden)
        {
            var thingId = cmd["thing_id"].Value<string>();
            var thing   = Find.CurrentMap.spawnedThings
                .FirstOrDefault(t => t.ThingID == thingId)
                ?? throw new Exception($"Thing not found: {thingId}");
            thing.SetForbidden(forbidden);
        }

        // ── Исследования ───────────────────────────────────────────────────────

        private static void SetResearch(JObject cmd)
        {
            var techName = cmd["tech_name"].Value<string>();
            var project  = DefDatabase<ResearchProjectDef>.GetNamed(techName, false)
                ?? throw new Exception($"Неизвестный проект исследования: '{techName}'. " +
                                       $"Смотри research.available в get_colony_state за точными именами.");
            Find.ResearchManager.SetCurrentProject(project);
        }

        // ── Helpers ────────────────────────────────────────────────────────────

        /// DefDatabase.GetNamed на несуществующее имя тихо возвращает null (не
        /// бросает исключение), и код дальше падает с бестолковым
        /// NullReferenceException. Тут — явная понятная ошибка вместо этого.
        /// Разбирает параметр material: если def требует материал (MadeFromStuff)
        /// и материал не задан/не найден — понятная ошибка вместо тихого null,
        /// который потом ломается глубоко внутри RimWorld ("...with null Stuff").
        private static ThingDef ResolveStuff(ThingDef def, JObject cmd)
        {
            ThingDef stuff = null;
            var mat = cmd["material"]?.Value<string>();
            if (!string.IsNullOrEmpty(mat))
            {
                stuff = DefDatabase<ThingDef>.GetNamedSilentFail(mat);
                if (stuff == null)
                    throw new Exception($"Неизвестный материал: '{mat}'. Примеры: Steel, WoodLog, BlocksGranite, Plasteel.");
                // «Granite» это природная порода, а не материал: чертёж из неё не строится и без карточки (i). Нужны блоки (BlocksGranite).
                if (!stuff.IsStuff || (def.MadeFromStuff && !stuff.stuffProps.CanMake(def)))
                {
                    var allowed = DefDatabase<ThingDef>.AllDefsListForReading
                        .Where(t => t.IsStuff && t.stuffProps.CanMake(def)
                                    && Find.CurrentMap.resourceCounter.GetCount(t) > 0)
                        .Select(t => t.defName).Take(12);
                    throw new Exception($"'{mat}' не подходит как материал для {def.defName}. " +
                                        $"Сейчас на складе годятся: {string.Join(", ", allowed)}. " +
                                        $"Камень это блоки (BlocksGranite, BlocksLimestone...), а не Granite.");
                }
            }
            if (def.MadeFromStuff && stuff == null)
                throw new Exception($"{def.defName} требует материал — передай параметр material " +
                                    $"(например Steel, WoodLog, BlocksGranite — смотри colony.resources что есть в наличии).");
            return stuff;
        }

        private static ThingDef GetThingDefOrThrow(string name)
        {
            var def = DefDatabase<ThingDef>.GetNamed(name, false);
            if (def == null)
                throw new Exception(
                    $"Неизвестный ThingDef: '{name}'. Материал (Steel/WoodLog/...) передаётся " +
                    $"отдельным параметром material, а не частью имени (правильно: def_name=\"Wall\", " +
                    $"material=\"Steel\", а не \"Wall_Steel\"). Зоны (склад/грядка) — это не постройки, " +
                    $"используй create_stockpile/create_grow_zone. Если не уверена в точном имени — " +
                    $"вызови search_thing_defs(query) чтобы найти правильное.");
            return def;
        }

        private static Pawn GetPawn(JObject cmd)
        {
            var id = cmd["pawn_id"]?.Value<string>()
                ?? throw new ArgumentException("Missing 'pawn_id'");
            return GetPawnById(id);
        }

        private static Pawn GetPawnById(string id)
        {
            return Find.CurrentMap.mapPawns.AllPawns
                .FirstOrDefault(p => p.ThingID == id)
                ?? throw new Exception($"Pawn not found: {id}");
        }
    }
}

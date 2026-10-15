using System;
using System.Collections.Generic;
using System.Linq;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using RimWorld;
using Verse;

namespace AgentBridge
{
    /// Планирование построек: проверка, что стены и двери реально встанут и что из них получится ПОМЕЩЕНИЕ
    /// (игра считает комнатой только замкнутый контур под крышей), и рисование плана игровым инструментом
    /// «Планирование» (Verse.Plan) без постройки. Чертежи ставятся отдельной командой, и только если проверка пройдена.
    public static partial class CommandExecutor
    {
        private const float RoofSupportDistance = 6.9f;   // дальше этого от стены/колонны перекрытие не держится
        private const int MaxRoomCells = 900;             // больше этого «помещение» уже не комната, а утечка в поле
        private const string PlanPrefix = "agent: ";

        private static readonly IntVec3[] Dirs4 = { new IntVec3(1, 0, 0), new IntVec3(-1, 0, 0), new IntVec3(0, 0, 1), new IntVec3(0, 0, -1) };

        private class PlanSpec
        {
            public Map Map;
            public ThingDef WallDef, DoorDef, WallStuff, DoorStuff;
            public List<IntVec3> Walls, Doors;
            public IntVec3 Inside;
            public bool Existing;      // проверка уже стоящего и размеченного контура: ничего не добавляем
        }

        private static List<IntVec3> ReadCells(JObject cmd, string key) =>
            (cmd[key]?.ToObject<List<PositionDto>>() ?? new List<PositionDto>())
                .Select(p => new IntVec3(p.X, 0, p.Z)).Distinct().ToList();

        private static PlanSpec ReadPlanSpec(JObject cmd)
        {
            var map = Find.CurrentMap;
            var wallDef = GetThingDefOrThrow(cmd["wall_def"]?.Value<string>() ?? "Wall");
            var doorDef = GetThingDefOrThrow(cmd["door_def"]?.Value<string>() ?? "Door");
            var spec = new PlanSpec
            {
                Map = map, WallDef = wallDef, DoorDef = doorDef,
                WallStuff = ResolveStuff(wallDef, cmd),
                Walls = ReadCells(cmd, "walls"), Doors = ReadCells(cmd, "doors"),
            };
            // материал двери: тот же, если из него можно сделать дверь; иначе игра подберёт сама
            var mat = cmd["door_material"]?.Value<string>();
            if (!string.IsNullOrEmpty(mat))
                spec.DoorStuff = DefDatabase<ThingDef>.GetNamedSilentFail(mat);
            else if (spec.WallStuff != null && doorDef.MadeFromStuff && spec.WallStuff.stuffProps.CanMake(doorDef))
                spec.DoorStuff = spec.WallStuff;
            var inside = cmd["inside"]?.ToObject<PositionDto>();
            if (inside == null) throw new Exception("Нужна клетка внутри будущего помещения: inside={x, z}.");
            spec.Inside = new IntVec3(inside.X, 0, inside.Z);
            if (spec.Walls.Count == 0) throw new Exception("Список стен walls пуст.");
            return spec;
        }

        /// Блокирует ли клетка комнату: готовая стена/скала/дверь или уже стоящий чертёж стены и двери.
        private static bool BlocksRoom(Map map, IntVec3 c)
        {
            foreach (var t in c.GetThingList(map))
            {
                var def = t.def;
                if (def.IsBlueprint || def.IsFrame) def = def.entityDefToBuild as ThingDef ?? def;
                if (def == null) continue;
                if (def.passability == Traversability.Impassable && def.category != ThingCategory.Plant && def.category != ThingCategory.Pawn
                    && (def.building != null || def.IsEdifice())) return true;
                if (def.IsDoor) return true;
            }
            return false;
        }

        private static bool HoldsRoof(Map map, IntVec3 c)
        {
            var e = c.GetEdifice(map);
            return e != null && e.def.holdsRoof;
        }

        /// Главная проверка: можно ли поставить стены и двери и будет ли внутри помещение.
        private static object CheckPlan(PlanSpec s)
        {
            var map = s.Map;
            var problems = new List<string>();
            var wallSet = new HashSet<IntVec3>(s.Walls);
            var doorSet = new HashSet<IntVec3>(s.Doors);

            foreach (var c in wallSet.Intersect(doorSet))
                problems.Add($"({c.x},{c.z}) указана и стеной, и дверью: оставь только дверь.");

            foreach (var c in s.Walls.Where(c => !doorSet.Contains(c)))
                CheckCell(map, c, s.WallDef, "стена", problems);
            foreach (var c in s.Doors)
            {
                CheckCell(map, c, s.DoorDef, "дверь", problems);
                bool NeighborBarrier(IntVec3 n) => wallSet.Contains(n) || (!doorSet.Contains(n) && BlocksRoom(map, n));
                bool inLine = (NeighborBarrier(c + Dirs4[0]) && NeighborBarrier(c + Dirs4[1]))
                           || (NeighborBarrier(c + Dirs4[2]) && NeighborBarrier(c + Dirs4[3]));
                if (!inLine)
                    problems.Add($"дверь ({c.x},{c.z}) не встроена в стену: по обе стороны вдоль одной оси должны быть стены.");
            }
            if (s.Doors.Count == 0 && !s.Existing)
                problems.Add("в помещении нет двери: колонисты не смогут войти.");

            // замкнутость: заливка от клетки внутри по 4 соседям (так игра делит карту на области)
            bool Barrier(IntVec3 c) => wallSet.Contains(c) || doorSet.Contains(c) || BlocksRoom(map, c);
            var inside = new List<IntVec3>();
            string leak = null;
            if (!s.Inside.InBounds(map)) problems.Add("клетка inside вне карты.");
            else if (Barrier(s.Inside)) problems.Add($"клетка inside ({s.Inside.x},{s.Inside.z}) сама оказалась стеной или дверью: выбери клетку внутри помещения.");
            else
            {
                var seen = new HashSet<IntVec3> { s.Inside };
                var queue = new Queue<IntVec3>();
                queue.Enqueue(s.Inside);
                while (queue.Count > 0 && leak == null)
                {
                    var c = queue.Dequeue();
                    inside.Add(c);
                    if (inside.Count > MaxRoomCells) { leak = $"область больше {MaxRoomCells} клеток: контур не замкнут, это уже открытая местность"; break; }
                    foreach (var d in Dirs4)
                    {
                        var n = c + d;
                        if (!n.InBounds(map)) { leak = $"контур не замкнут: заливка вышла к краю карты у ({c.x},{c.z})"; break; }
                        if (Barrier(n) || !seen.Add(n)) continue;
                        queue.Enqueue(n);
                    }
                }
                if (leak != null)
                {
                    problems.Add(leak + ". Проверь углы и щели: диагональ стенами не закрывается, нужны стены по сторонам клеток.");
                    inside.Clear();
                }
            }
            bool enclosed = leak == null && inside.Count > 0;

            // перекрытие: клетки дальше ~7 от стены/колонны крышей не накрываются
            int unsupported = 0;
            IntVec3 firstBad = IntVec3.Invalid;
            if (enclosed)
            {
                var holders = new List<IntVec3>(wallSet);
                var box = CellRect.FromCellList(inside).ExpandedBy(8);
                foreach (var c in box.Cells)
                    if (c.InBounds(map) && !wallSet.Contains(c) && HoldsRoof(map, c)) holders.Add(c);
                foreach (var c in inside)
                {
                    var roof = map.roofGrid.RoofAt(c);
                    if (roof != null && roof.isThickRoof) continue;
                    if (holders.Any(h => (h - c).LengthHorizontal <= RoofSupportDistance)) continue;
                    unsupported++;
                    if (!firstBad.IsValid) firstBad = c;
                }
                if (unsupported > 0)
                    problems.Add($"{unsupported} клеток внутри дальше {RoofSupportDistance:0.#} от стены (например ({firstBad.x},{firstBad.z})): крыша не удержится, " +
                                 "нужны колонны (Column) внутри или меньшее помещение.");
            }

            // помехи внутри: деревья и камни мебель не пустят, их нужно убрать до расстановки
            int blockers = 0;
            foreach (var c in inside)
                if (c.GetThingList(map).Any(t => (t.def.plant != null && t.def.plant.IsTree) || (t.def.category == ThingCategory.Building && t.def.mineable)))
                    blockers++;
            if (blockers > 0)
                problems.Add($"внутри {blockers} клеток заняты деревьями или камнями: мебель туда не встанет. Сначала designate_chop или designate_mine, либо сдвинь комнату.");
            // цена и объём: считаем только то, что ещё предстоит поставить (готовое и размеченное не в счёт)
            object cost = null;
            if (!s.Existing)
            {
                int newWalls = s.Walls.Count(c => !doorSet.Contains(c) && c.InBounds(map) && AlreadyThere(map, c, s.WallDef, s.WallStuff) == null
                                                  && !c.GetThingList(map).Any(t => t is Building b && b.def == s.WallDef));
                int newDoors = s.Doors.Count(c => c.InBounds(map) && AlreadyThere(map, c, s.DoorDef, s.DoorStuff) == null
                                                  && !c.GetThingList(map).Any(t => t is Building b && b.def == s.DoorDef));
                cost = CostReport(map, new[] { new CostLine { Def = s.WallDef, Stuff = s.WallStuff, Count = newWalls },
                                               new CostLine { Def = s.DoorDef, Stuff = s.DoorStuff, Count = newDoors } }, problems);
            }
            return new
            {
                ok = problems.Count == 0,
                would_be_room = problems.Count == 0 && enclosed,
                enclosed,
                interior_cells = inside.Count,
                walls = s.Walls.Count, doors = s.Doors.Count,
                cost,
                problems,
            };
        }


        // ── Подбор места под стены: стены ставятся вокруг того, что уже стоит, а не сквозь него ─────────────────────

        /// Что занимает клетку (для понятных отказов): постройки, растения, камни, вещи.
        private static string Occupants(Map map, IntVec3 c)
        {
            var names = c.GetThingList(map).Where(t => t.def.category == ThingCategory.Building || t.def.category == ThingCategory.Plant
                                                        || t.def.category == ThingCategory.Item || t.def.IsBlueprint || t.def.IsFrame)
                .Select(t => t.LabelShort).Distinct().Take(3).ToList();
            return names.Count == 0 ? "" : " (занято: " + string.Join(", ", names) + ")";
        }

        private static bool WallCellFree(Map map, IntVec3 c, ThingDef wallDef) =>
            c.InBounds(map) && (c.GetThingList(map).Any(t => t.def == wallDef || t.def.entityDefToBuild == wallDef)
                                || GenConstruct.CanPlaceBlueprintAt(wallDef, c, Rot4.North, map).Accepted);

        private static IEnumerable<IntVec3> RectPerimeter(int x1, int z1, int x2, int z2)
        {
            for (int x = x1; x <= x2; x++)
                for (int z = z1; z <= z2; z++)
                    if (x == x1 || x == x2 || z == z1 || z == z2) yield return new IntVec3(x, 0, z);
        }

        /// fit_room: подобрать прямоугольник комнаты, у которого ВЕСЬ контур реально можно построить.
        /// enclose=[{x,z}...]: что должно оказаться внутри (уже стоящая мебель): стены пойдут вокруг неё, а не через неё.
        /// Иначе нужны w и h (внешний размер вместе со стенами): ищется свободное место рядом с (x, z) или центром колонии.
        private static string FitRoom(JObject cmd)
        {
            var map = Find.CurrentMap;
            var wallDef = GetThingDefOrThrow(cmd["wall_def"]?.Value<string>() ?? "Wall");
            var enclose = ReadCells(cmd, "enclose");
            var center = cmd["x"] != null ? new IntVec3(cmd["x"].Value<int>(), 0, cmd["z"].Value<int>()) : ColonyCenter(map);
            int radius = Math.Max(5, Math.Min(cmd["radius"]?.Value<int>() ?? 25, 45));

            List<object> Bad(int x1, int z1, int x2, int z2) =>
                RectPerimeter(x1, z1, x2, z2).Where(c => !WallCellFree(map, c, wallDef))
                    .Select(c => (object)new { x = c.x, z = c.z, what = Occupants(map, c).Trim() }).ToList();
            int Blockers(int x1, int z1, int x2, int z2)
            {
                int n = 0;
                for (int x = x1 + 1; x < x2; x++)
                    for (int z = z1 + 1; z < z2; z++)
                    {
                        var c = new IntVec3(x, 0, z);
                        if (!c.InBounds(map) || c.GetThingList(map).Any(t => (t.def.plant != null && t.def.plant.IsTree) || (t.def.category == ThingCategory.Building && t.def.mineable))
                            || !c.Standable(map) && c.GetEdifice(map) == null) n++;
                    }
                return n;
            }

            (int x1, int z1, int x2, int z2)? best = null;
            List<object> bestBad = null;
            long bestArea = long.MaxValue;
            if (enclose.Count > 0)
            {
                int bx1 = enclose.Min(c => c.x), bx2 = enclose.Max(c => c.x), bz1 = enclose.Min(c => c.z), bz2 = enclose.Max(c => c.z);
                for (int l = 1; l <= 3; l++) for (int r = 1; r <= 3; r++) for (int t = 1; t <= 3; t++) for (int b = 1; b <= 3; b++)
                {
                    int x1 = bx1 - l, x2 = bx2 + r, z1 = bz1 - b, z2 = bz2 + t;
                    long area = (long)(x2 - x1 + 1) * (z2 - z1 + 1);
                    var bad = Bad(x1, z1, x2, z2);
                    if (bad.Count == 0 && Blockers(x1, z1, x2, z2) == 0 && area < bestArea) { best = (x1, z1, x2, z2); bestArea = area; bestBad = null; }
                    else if (best == null && (bestBad == null || bad.Count < bestBad.Count)) { bestBad = bad; bestArea = Math.Min(bestArea, long.MaxValue); }
                }
                if (best == null)
                    return JsonConvert.SerializeObject(new
                    {
                        found = false,
                        reason = "вокруг этой мебели нельзя замкнуть стены: на всех вариантах контура есть занятые клетки. Убери мешающее (deconstruct, designate_chop) или сдвинь мебель.",
                        example_blocked = bestBad,
                    });
            }
            else
            {
                int w = cmd["w"]?.Value<int>() ?? 0, h = cmd["h"]?.Value<int>() ?? 0;
                if (w < 3 || h < 3) throw new Exception("нужны w и h (внешний размер со стенами, не меньше 3x3) либо enclose=[{x,z}...]");
                foreach (var cell in GenRadial.RadialCellsAround(center, radius, true))
                {
                    int x1 = cell.x - w / 2, z1 = cell.z - h / 2, x2 = x1 + w - 1, z2 = z1 + h - 1;
                    if (Bad(x1, z1, x2, z2).Count == 0 && Blockers(x1, z1, x2, z2) == 0) { best = (x1, z1, x2, z2); break; }
                }
                if (best == null)
                    return JsonConvert.SerializeObject(new { found = false, reason = $"в радиусе {radius} от ({center.x},{center.z}) нет места {w}x{h}, где весь контур можно построить, а внутри нет деревьев и камней. Уменьши размер, увеличь radius или расчисти место (designate_chop/designate_mine)." });
            }

            var (rx1, rz1, rx2, rz2) = best.Value;
            // дверь: середина стороны, обращённой к центру колонии (не угол); если там нельзя, ближайшая подходящая клетка контура
            var col = ColonyCenter(map);
            var door = RectPerimeter(rx1, rz1, rx2, rz2)
                .Where(c => !((c.x == rx1 || c.x == rx2) && (c.z == rz1 || c.z == rz2)))
                .Where(c => GenConstruct.CanPlaceBlueprintAt(ThingDefOf.Door, c, Rot4.North, map).Accepted || c.GetThingList(map).Any(t => t.def == ThingDefOf.Door))
                .OrderBy(c => (c - col).LengthHorizontal).FirstOrDefault();
            var inside = new IntVec3((rx1 + rx2) / 2, 0, (rz1 + rz2) / 2);
            return JsonConvert.SerializeObject(new
            {
                found = true,
                rect = new { x1 = rx1, z1 = rz1, x2 = rx2, z2 = rz2 },
                door = door.IsValid ? new { x = door.x, z = door.z } : null,
                inside = new { x = inside.x, z = inside.z },
                interior_cells = (rx2 - rx1 - 1) * (rz2 - rz1 - 1),
                note = "Дальше: plan_room(x1,z1,x2,z2,door_x,door_z) для проверки стоимости и замкнутости, затем build_room. Весь контур свободен и внутри нет деревьев и камней.",
            });
        }

        private static void CheckCell(Map map, IntVec3 c, ThingDef def, string what, List<string> problems)
        {
            if (!c.InBounds(map)) { problems.Add($"{what} ({c.x},{c.z}) вне карты."); return; }
            var stuffless = AlreadyThere(map, c, def, null);
            if (stuffless != null || c.GetThingList(map).Any(t => t is Building b && b.def == def)) return;   // уже стоит или размечено
            var report = GenConstruct.CanPlaceBlueprintAt(def, c, Rot4.North, map);
            if (!report.Accepted)
                problems.Add($"{what} ({c.x},{c.z}): {(string.IsNullOrEmpty(report.Reason) ? "поставить нельзя" : report.Reason)}{Occupants(map, c)}. Стена не встанет: подбери контур вокруг этого (fit_room) или убери помеху.");
        }

        // ── Команды ────────────────────────────────────────────────────────────

        /// plan_room: проверить и (по умолчанию) нарисовать план инструментом «Планирование», ничего не строя.
        private static string PlanRoom(JObject cmd)
        {
            var s = ReadPlanSpec(cmd);
            var result = CheckPlan(s);
            var drawn = new List<string>();
            if (cmd["draw"]?.Value<bool>() ?? true)
            {
                ClearAgentPlans(s.Map);
                DrawPlan(s.Map, s.Walls.Where(c => !s.Doors.Contains(c)), "стены", 0, drawn);
                DrawPlan(s.Map, s.Doors, "двери", 1, drawn);
            }
            var json = JObject.FromObject(result);
            json["plans"] = JArray.FromObject(drawn);
            return json.ToString(Formatting.None);
        }

        /// build_room: заново проверить и поставить чертежи стен и дверей. Если проверка не пройдена, не строит ничего.
        private static string BuildRoom(JObject cmd)
        {
            var s = ReadPlanSpec(cmd);
            var result = JObject.FromObject(CheckPlan(s));
            if (!result["ok"].Value<bool>())
            {
                result["built"] = false;
                result["message"] = "Не построено: исправь problems и вызови plan_room снова.";
                return result.ToString(Formatting.None);
            }
            int walls = 0, doors = 0;
            foreach (var c in s.Walls.Where(c => !s.Doors.Contains(c)))
                if (PlaceIfFree(s.Map, c, s.WallDef, s.WallStuff)) walls++;
            foreach (var c in s.Doors)
                if (PlaceIfFree(s.Map, c, s.DoorDef, s.DoorStuff)) doors++;
            ClearAgentPlans(s.Map);
            result["built"] = true;
            result["blueprints"] = JObject.FromObject(new { walls, doors });
            return result.ToString(Formatting.None);
        }

        private static bool PlaceIfFree(Map map, IntVec3 c, ThingDef def, ThingDef stuff)
        {
            if (AlreadyThere(map, c, def, stuff) != null) return false;
            if (!GenConstruct.CanPlaceBlueprintAt(def, c, Rot4.North, map).Accepted) return false;
            GenConstruct.PlaceBlueprintForBuild(def, c, map, Rot4.North, Faction.OfPlayer, stuff);
            return true;
        }

        /// clear_plans: убрать наши планы с карты (только созданные агентом).
        private static string ClearPlans(JObject cmd)
        {
            int n = ClearAgentPlans(Find.CurrentMap);
            return JsonConvert.SerializeObject(new { removed = n });
        }

        private static int ClearAgentPlans(Map map)
        {
            var mine = map.planManager.AllPlans.Where(p => (p.RenamableLabel ?? "").StartsWith(PlanPrefix)).ToList();
            foreach (var p in mine) p.Delete();
            return mine.Count;
        }

        private static void DrawPlan(Map map, IEnumerable<IntVec3> cells, string label, int colorIndex, List<string> drawn)
        {
            var list = cells.Where(c => c.InBounds(map)).ToList();
            if (list.Count == 0) return;
            var colors = DefDatabase<ColorDef>.AllDefs.Where(d => d.defName.StartsWith("Plan")).OrderBy(d => d.defName).ToList();
            if (colors.Count == 0) colors = DefDatabase<ColorDef>.AllDefs.ToList();
            var color = colors[colorIndex % colors.Count];
            var plan = new Plan(color, map.planManager);
            if (!map.planManager.AllPlans.Contains(plan)) map.planManager.RegisterPlan(plan);
            plan.RenamableLabel = PlanPrefix + label;
            foreach (var c in list) plan.AddCell(c);
            plan.Dirty();
            drawn.Add($"{label}: {list.Count} клеток, цвет {color.defName}");
        }

        /// check_enclosure: замкнут ли контур вокруг клетки СЕЙЧАС (готовые стены, двери и чертежи). Если нет, называет клетку утечки.
        private static string CheckEnclosure(JObject cmd)
        {
            var map = Find.CurrentMap;
            var spec = new PlanSpec
            {
                Map = map, Existing = true, Walls = new List<IntVec3>(), Doors = new List<IntVec3>(),
                Inside = new IntVec3(cmd["x"].Value<int>(), 0, cmd["z"].Value<int>()),
            };
            return JsonConvert.SerializeObject(CheckPlan(spec));
        }

        /// Мебель в линии стены: если по бокам клетки стоят стены, здание на ней оставит дыру в контуре (станок, кровать
        /// комнату не замыкают). Возвращает причину отказа или null.
        private static string WallLineConflict(Map map, ThingDef def, IntVec3 pos, Rot4 rot)
        {
            if (def.category != ThingCategory.Building || def.IsDoor || def.passability == Traversability.Impassable
                || def.building?.canPlaceOverWall == true || def.IsBlueprint || def.IsFrame) return null;
            var rect = GenAdj.OccupiedRect(pos, rot, def.size);
            foreach (var c in rect.Cells)
            {
                bool Side(IntVec3 n) => !rect.Contains(n) && BlocksRoom(map, n);
                if ((Side(c + Dirs4[0]) && Side(c + Dirs4[1])) || (Side(c + Dirs4[2]) && Side(c + Dirs4[3])))
                    return $"клетка ({c.x},{c.z}) стоит В ЛИНИИ СТЕНЫ (по бокам стены): {def.defName} оставит здесь дыру в контуре, станки и кровати комнату не замыкают. " +
                           "Сначала поставь здесь стену или дверь (plan_room), а мебель двигай внутрь на клетку от стены.";
            }
            return null;
        }


        /// room_info: что игра на самом деле считает помещением в клетке (после постройки).
        private static string RoomInfo(JObject cmd)
        {
            var map = Find.CurrentMap;
            var c = new IntVec3(cmd["x"].Value<int>(), 0, cmd["z"].Value<int>());
            if (!c.InBounds(map)) throw new Exception("Клетка вне карты.");
            var room = c.GetRoom(map);
            if (room == null)
                return JsonConvert.SerializeObject(new { room = false, message = "в этой клетке нет области (стена или препятствие)" });
            return JsonConvert.SerializeObject(new
            {
                room = true,
                is_proper_room = room.ProperRoom,
                role = room.Role?.label,
                cells = room.CellCount,
                outdoors = room.PsychologicallyOutdoors,
                open_roof_cells = room.OpenRoofCount,
                touches_map_edge = room.TouchesMapEdge,
                roofed = room.OpenRoofCount == 0,
            });
        }
    }
}

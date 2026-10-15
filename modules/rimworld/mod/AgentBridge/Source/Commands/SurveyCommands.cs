using System;
using System.Collections.Generic;
using System.Linq;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using RimWorld;
using Verse;

namespace AgentBridge
{
    /// Разведка карты: обзор местности (вода, горы, лес, плодородная земля, руда, руины) и подробный анализ участка
    /// (руины: целые стены, дыры в них, находки, опасности). Агент по миникарте этого не видел.
    public static partial class CommandExecutor
    {
        private const int Block = 10;

        private static IntVec3 ColonyCenter(Map map)
        {
            var pawns = map.mapPawns.FreeColonistsSpawned;
            if (pawns.Count == 0) return map.Center;
            return new IntVec3((int)pawns.Average(p => p.Position.x), 0, (int)pawns.Average(p => p.Position.z));
        }

        private static bool IsRuinWall(Thing t) =>
            t is Building b && b.Faction == null && b.def.passability == Traversability.Impassable
            && b.def.building != null && !b.def.building.isNaturalRock && !b.def.mineable && b.def.category == ThingCategory.Building;

        private static bool IsRuinThing(Thing t) =>
            t is Building b && b.Faction == null && b.def.building != null && !b.def.building.isNaturalRock && !b.def.mineable
            && b.def.category == ThingCategory.Building;

        /// survey_map: что есть на карте и где (по блокам 10x10), ближайшее к колонии первым. Плюс обзорная ASCII-схема.
        private static string SurveyMap(JObject cmd)
        {
            var map = Find.CurrentMap;
            var center = ColonyCenter(map);
            int bw = (map.Size.x + Block - 1) / Block, bh = (map.Size.z + Block - 1) / Block;
            var water = new int[bw, bh]; var rock = new int[bw, bh]; var trees = new int[bw, bh];
            var fertile = new int[bw, bh]; var ruins = new int[bw, bh];
            var ores = new Dictionary<string, int[,]>();

            foreach (var c in map.AllCells)
            {
                int bx = c.x / Block, bz = c.z / Block;
                var terrain = c.GetTerrain(map);
                if (terrain.IsWater) water[bx, bz]++;
                else if (terrain.fertility >= 1.0f && c.GetEdifice(map) == null) fertile[bx, bz]++;
            }
            foreach (var t in map.listerThings.AllThings)
            {
                var c = t.Position;
                if (!c.InBounds(map)) continue;
                int bx = c.x / Block, bz = c.z / Block;
                if (t.def.plant != null && t.def.plant.IsTree) trees[bx, bz]++;
                else if (t.def.mineable)
                {
                    if (t.def.building != null && t.def.building.isResourceRock)
                    {
                        if (!ores.TryGetValue(t.def.defName, out var grid)) ores[t.def.defName] = grid = new int[bw, bh];
                        grid[bx, bz]++;
                    }
                    else rock[bx, bz]++;
                }
                else if (IsRuinThing(t)) ruins[bx, bz]++;
            }

            List<object> Top(int[,] g, int min, int take = 4)
            {
                var list = new List<(int bx, int bz, int n, float d)>();
                for (int x = 0; x < bw; x++)
                    for (int z = 0; z < bh; z++)
                        if (g[x, z] >= min)
                        {
                            float d = (new IntVec3(x * Block + Block / 2, 0, z * Block + Block / 2) - center).LengthHorizontal;
                            list.Add((x, z, g[x, z], d));
                        }
                return list.OrderBy(i => i.d).Take(take)
                    .Select(i => (object)new { x = i.bx * Block + Block / 2, z = i.bz * Block + Block / 2, cells = i.n, dist = (int)i.d }).ToList();
            }

            var oreSummary = ores.ToDictionary(kv => kv.Key, kv => Top(kv.Value, 3, 3));

            // обзорная схема: символ на блок. C колония, ~ вода, ^ скала, T лес, f плодородная земля, R руины, o руда
            var ascii = new System.Text.StringBuilder();
            for (int z = bh - 1; z >= 0; z--)
            {
                for (int x = 0; x < bw; x++)
                {
                    char ch = '.';
                    int bestN = 0;
                    void Try(int n, char c) { if (n > bestN) { bestN = n; ch = c; } }
                    Try(water[x, z], '~'); Try(rock[x, z], '^'); Try(trees[x, z] * 6, 'T'); Try(fertile[x, z] / 2, 'f');
                    foreach (var g in ores.Values) Try(g[x, z] * 12, 'o');
                    Try(ruins[x, z] * 15, 'R');
                    if (center.x / Block == x && center.z / Block == z) ch = 'C';
                    ascii.Append(ch);
                }
                ascii.Append('\n');
            }

            return JsonConvert.SerializeObject(new
            {
                map = new { w = map.Size.x, h = map.Size.z, biome = map.Biome?.label, outdoor_temp = (int)map.mapTemperature.OutdoorTemp },
                colony_center = new { x = center.x, z = center.z },
                note = "Блоки 10x10 клеток, координаты это центр блока, dist в клетках от колонии. Схема: C колония, ~ вода, ^ скала, T лес, f плодородная земля, o руда, R руины; строки идут с севера (верх) на юг.",
                water = Top(water, 25), forest = Top(trees, 8), fertile = Top(fertile, 70), ruins = Top(ruins, 3), ores = oreSummary,
                scheme = ascii.ToString(),
            });
        }

        /// analyze_area: подробно про участок вокруг (x, z): руины (стены, двери, дыры, помещения), находки и опасности.
        private static string AnalyzeArea(JObject cmd)
        {
            var map = Find.CurrentMap;
            var c0 = new IntVec3(cmd["x"].Value<int>(), 0, cmd["z"].Value<int>());
            int radius = Math.Max(5, Math.Min(cmd["radius"]?.Value<int>() ?? 25, 45));
            var rect = CellRect.CenteredOn(c0, radius).ClipInsideMap(map);

            var ruinWalls = new HashSet<IntVec3>();
            var byDef = new Dictionary<string, int>();
            var loot = new List<(string def, int x, int z, int n, float value)>();
            var dangers = new List<string>();
            int doors = 0;
            foreach (var t in map.listerThings.AllThings)
            {
                if (!rect.Contains(t.Position)) continue;
                if (IsRuinWall(t)) ruinWalls.Add(t.Position);
                if (IsRuinThing(t))
                {
                    byDef[t.def.defName] = (byDef.TryGetValue(t.def.defName, out var n0) ? n0 : 0) + 1;
                    if (t.def.IsDoor) doors++;
                    if (t is Building_Casket cs && t.def.defName.Contains("Cryptosleep")) dangers.Add($"древний криптосон-контейнер ({t.Position.x},{t.Position.z}): внутри может быть спящий (опасно или полезно), не вскрывай без бойцов");
                }
                else if (t.def.category == ThingCategory.Item && !t.IsForbidden(Faction.OfPlayer) && t.def.BaseMarketValue * t.stackCount >= 20f)
                    loot.Add((t.def.defName, t.Position.x, t.Position.z, t.stackCount, t.def.BaseMarketValue * t.stackCount));
                if (t is Pawn p && !p.Dead && p.Faction != Faction.OfPlayer && p.HostileTo(Faction.OfPlayer) && rect.Contains(p.Position))
                    dangers.Add($"враждебный {p.LabelShort} ({p.Position.x},{p.Position.z})");
            }

            // кластеры стен руин (8 соседей) и их прямоугольные границы: где стены целы, а где дыры
            var seen = new HashSet<IntVec3>();
            var clusters = new List<object>();
            foreach (var start in ruinWalls)
            {
                if (seen.Contains(start)) continue;
                var comp = new List<IntVec3>(); var q = new Queue<IntVec3>(); q.Enqueue(start); seen.Add(start);
                while (q.Count > 0)
                {
                    var c = q.Dequeue(); comp.Add(c);
                    for (int dx = -1; dx <= 1; dx++)
                        for (int dz = -1; dz <= 1; dz++)
                        {
                            var n = new IntVec3(c.x + dx, 0, c.z + dz);
                            if (ruinWalls.Contains(n) && seen.Add(n)) q.Enqueue(n);
                        }
                }
                if (comp.Count < 6) continue;
                int minX = comp.Min(c => c.x), maxX = comp.Max(c => c.x), minZ = comp.Min(c => c.z), maxZ = comp.Max(c => c.z);
                if (maxX - minX < 2 || maxZ - minZ < 2) continue;
                var gaps = new List<object>();
                int perimeter = 0, present = 0;
                for (int x = minX; x <= maxX; x++)
                    for (int z = minZ; z <= maxZ; z++)
                    {
                        if (x != minX && x != maxX && z != minZ && z != maxZ) continue;
                        perimeter++;
                        var cell = new IntVec3(x, 0, z);
                        if (BlocksRoom(map, cell)) { present++; continue; }
                        if (gaps.Count < 14) gaps.Add(new { x, z, can_wall = GenConstruct.CanPlaceBlueprintAt(ThingDefOf.Wall, cell, Rot4.North, map).Accepted });
                    }
                int gapCount = perimeter - present;
                var inside = new IntVec3((minX + maxX) / 2, 0, (minZ + maxZ) / 2);
                clusters.Add(new
                {
                    x1 = minX, z1 = minZ, x2 = maxX, z2 = maxZ, walls = comp.Count,
                    wall_coverage = (int)(100f * present / Math.Max(1, perimeter)) + "%",
                    gaps = gapCount, gap_cells = gaps,
                    inside_hint = new { x = inside.x, z = inside.z },
                    verdict = gapCount == 0 ? "контур цел: можно использовать как комнату (поставь дверь, проверь крышу check_enclosure)"
                              : gapCount <= 8 ? $"почти целая: закрой {gapCount} дыр(ы) (plan_room с walls=gap_cells, inside=inside_hint) и получится комната"
                              : "руины разрушены: проще строить своё, но стены можно взять как часть контура",
                    dist = (int)(inside - c0).LengthHorizontal,
                });
            }

            return JsonConvert.SerializeObject(new
            {
                center = new { x = c0.x, z = c0.z }, radius,
                ruin_buildings = byDef.OrderByDescending(kv => kv.Value).Take(15).ToDictionary(kv => kv.Key, kv => kv.Value),
                ruin_doors = doors,
                ruin_structures = clusters,
                loot = loot.OrderByDescending(l => l.value).Take(12).Select(l => new { l.def, l.x, l.z, count = l.n }).ToList(),
                dangers = dangers.Take(8),
            });
        }
    }
}

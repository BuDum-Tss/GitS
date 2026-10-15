using System;
using System.Collections.Generic;
using System.Linq;
using System.Text;
using Newtonsoft.Json;
using RimWorld;
using RimWorld.Planet;
using Verse;

namespace AgentBridge
{
    public static class StateSerializer
    {
        public static string Serialize()
        {
            try
            {
                if (Current.Game == null)
                    return "{\"status\":\"no_game\"}";

                var map = Find.CurrentMap;
                if (map == null)
                    return "{\"status\":\"no_map\"}";

                var tile = map.Tile;
                var longLat = Find.WorldGrid.LongLatOf(tile);
                var ticksAbs = Find.TickManager.TicksAbs;

                var state = new GameStateDto
                {
                    Tick         = Find.TickManager.TicksGame,
                    Year         = GenDate.Year(ticksAbs, longLat.x),
                    Season       = GenDate.Season(ticksAbs, longLat).ToString(),
                    Weather      = map.weatherManager.curWeather?.label ?? "Unknown",
                    ThreatPoints = StorytellerUtility.DefaultThreatPointsNow(map),
                    TimeSpeed    = (int)Find.TickManager.CurTimeSpeed,
                    Paused       = Find.TickManager.Paused,
                    ManualPriorities = Current.Game.playSettings.useWorkPriorities,
                    SaveId       = BuildSaveId(),
                    Colony       = SerializeColony(map),
                    Minimap      = SerializeMinimap(map),
                    Threats      = SerializeThreats(map),
                    PendingEvents= AgentBridgeMod.Events.Peek(),
                    GameLog      = LogGuard.Summary
                };

                return JsonConvert.SerializeObject(state, Formatting.None);
            }
            catch (Exception e)
            {
                return JsonConvert.SerializeObject(new { status = "error", error = e.Message });
            }
        }

        // Уникальный идентификатор сейва — чтобы контроллер понимал что это
        // другая колония/загрузка, а не продолжение той же сессии.
        private static string BuildSaveId()
        {
            var seed      = Find.World?.info?.seedString ?? "noworld";
            var factionId = Faction.OfPlayer?.loadID ?? -1;
            return $"{seed}_{factionId}";
        }

        // Компактная ASCII-миникарта рельефа — даёт пространственное мышление
        // без передачи полной сетки карты (могло бы быть 250x250+ клеток).
        // Каждый символ = квадрат NxN клеток, целимся в сетку ~40 на длинной стороне.
        private static string SerializeMinimap(Map map)
        {
            int mapW = map.Size.x, mapH = map.Size.z;
            int cellSize = Math.Max(1, Math.Max(mapW, mapH) / 40);
            int gridW = (mapW + cellSize - 1) / cellSize;
            int gridH = (mapH + cellSize - 1) / cellSize;

            var sb = new StringBuilder();
            sb.Append($"Карта {mapW}x{mapH}, 1 символ = {cellSize}x{cellSize} клеток, ")
              .Append("(0,0)=левый верхний угол. # скала/гора, ~ непроходимо (вода/пропасть), ")
              .Append("B постройка, b чертёж/недостроено, H домашняя зона, . открыто\n");

            for (int gz = 0; gz < gridH; gz++)
            {
                for (int gx = 0; gx < gridW; gx++)
                {
                    int sx = Math.Min(mapW - 1, gx * cellSize + cellSize / 2);
                    int sz = Math.Min(mapH - 1, gz * cellSize + cellSize / 2);
                    sb.Append(ClassifyMinimapCell(map, new IntVec3(sx, 0, sz)));
                }
                sb.Append('\n');
            }
            return sb.ToString();
        }

        private static char ClassifyMinimapCell(Map map, IntVec3 cell)
        {
            if (!cell.InBounds(map)) return ' ';

            var edifice = cell.GetEdifice(map);
            if (edifice != null)
                return edifice.def.mineable ? '#' : 'B';

            foreach (var t in cell.GetThingList(map))
                if (t is Blueprint_Build || t is Frame) return 'b';

            if (!cell.Standable(map))
                return '~'; // непроходимая клетка без здания — почти всегда вода/пропасть

            if (map.areaManager.Home[cell]) return 'H';

            return '.';
        }

        // ── Colony ─────────────────────────────────────────────────────────────

        private static ColonyDto SerializeColony(Map map)
        {
            var colonists = map.mapPawns.FreeColonistsAndPrisoners.ToList();
            return new ColonyDto
            {
                Name      = Faction.OfPlayer?.Name ?? "Unknown",
                Resources = SerializeResources(map),
                Pawns     = colonists.Select(SerializePawn).ToList(),
                Animals   = SerializeAnimals(map),
                Buildings = SerializeBuildings(map),
                Zones     = SerializeZones(map),
                Research  = SerializeResearch(),
                FoodDays  = EstimateFoodDays(map, colonists),
                ForbiddenItems = SerializeForbiddenItems(map),
                PendingConstruction = SerializePendingConstruction(map),
                Visitors  = SerializeVisitors(map),
                Alerts    = SerializeAlerts(map),
            };
        }

        private static List<ForbiddenItemDto> SerializeForbiddenItems(Map map)
        {
            return map.listerThings.AllThings
                .Where(t => t.def.category == ThingCategory.Item && t.IsForbidden(Faction.OfPlayer))
                .Take(40)
                .Select(t => new ForbiddenItemDto
                {
                    Id       = t.ThingID,
                    DefName  = t.def.defName,
                    Count    = t.stackCount,
                    Position = new PositionDto { X = t.Position.x, Z = t.Position.z },
                })
                .ToList();
        }

        private static Dictionary<string, int> SerializeResources(Map map)
        {
            var res = new Dictionary<string, int>();
            foreach (var def in DefDatabase<ThingDef>.AllDefsListForReading)
            {
                if (def.category != ThingCategory.Item) continue;
                if (def.IsCorpse || def.isUnfinishedThing) continue;
                var count = map.resourceCounter.GetCount(def);
                if (count > 0) res[def.defName] = count;
            }
            return res;
        }

        private static PawnDto SerializePawn(Pawn pawn)
        {
            var skills = new Dictionary<string, int>();
            if (pawn.skills != null)
                foreach (var s in pawn.skills.skills)
                    skills[s.def.defName] = s.Level;

            var workPriorities = new Dictionary<string, int>();
            if (pawn.workSettings != null)
                foreach (var wt in DefDatabase<WorkTypeDef>.AllDefsListForReading)
                {
                    var p = pawn.workSettings.GetPriority(wt);
                    if (p > 0) workPriorities[wt.defName] = p;
                }

            // 24-hour schedule
            var schedule = new List<string>(24);
            for (int h = 0; h < 24; h++)
            {
                var assignment = pawn.timetable?.GetAssignment(h);
                schedule.Add(assignment?.defName ?? "Anything");
            }

            var needs = new Dictionary<string, float>();
            if (pawn.needs?.AllNeeds != null)
                foreach (var n in pawn.needs.AllNeeds)
                    needs[n.def.defName] = (float)Math.Round(n.CurLevelPercentage, 2);

            var moodThoughts = SerializeMoodThoughts(pawn);

            var conditions = pawn.health?.hediffSet?.hediffs?
                .Select(h => h.def.defName).ToList() ?? new List<string>();

            var traits = pawn.story?.traits?.allTraits?
                .Select(t => t.def.defName).ToList() ?? new List<string>();

            string equipped = pawn.equipment?.Primary?.def?.defName;
            var wounds = pawn.health?.hediffSet?.hediffs?
                .Where(h => h is Hediff_Injury || h.Bleeding)
                .Select(h => $"{h.def.label}{(h.Part != null ? " (" + h.Part.Label + ")" : "")}")
                .Take(8).ToList() ?? new List<string>();
            var incapable = DefDatabase<WorkTypeDef>.AllDefsListForReading
                .Where(wt => pawn.WorkTypeIsDisabled(wt)).Select(wt => wt.defName).ToList();
            var wearing = pawn.apparel?.WornApparel?.Select(a => a.def.defName).ToList() ?? new List<string>();
            string medCare  = pawn.playerSettings?.medCare.ToString() ?? "Best";

            return new PawnDto
            {
                Id             = pawn.ThingID,
                Name           = pawn.Name?.ToStringShort ?? "Unknown",
                Age            = pawn.ageTracker?.AgeBiologicalYears ?? 0,
                Gender         = pawn.gender.ToString(),
                HealthPercent  = (float)Math.Round(pawn.health?.summaryHealth?.SummaryHealthPercent ?? 1f, 2),
                MoodPercent    = (float)Math.Round(pawn.needs?.mood?.CurLevelPercentage ?? 0.5f, 2),
                Skills         = skills,
                WorkPriorities = workPriorities,
                Schedule       = schedule,
                Needs          = needs,
                MoodThoughts   = moodThoughts,
                CurrentJob     = pawn.CurJobDef?.defName,
                IsDrafted      = pawn.Drafted,
                IsPrisoner     = pawn.IsPrisoner,
                IsDowned       = pawn.Downed,
                Bleeding       = (float)Math.Round(pawn.health?.hediffSet?.BleedRateTotal ?? 0f, 2),
                Wounds         = wounds,
                IncapableOf    = incapable,
                WeaponRanged   = pawn.equipment?.Primary?.def?.IsRangedWeapon ?? false,
                Apparel        = wearing,
                MedicalCare    = medCare,
                Traits         = traits,
                HealthConditions = conditions,
                Equipped       = equipped,
                Position       = new PositionDto { X = pawn.Position.x, Z = pawn.Position.z }
            };
        }

        private static List<MoodThoughtDto> SerializeMoodThoughts(Pawn pawn)
        {
            var result = new List<MoodThoughtDto>();
            var handler = pawn.needs?.mood?.thoughts;
            if (handler == null) return result;

            var thoughts = new List<Thought>();
            try { thoughts.AddRange(handler.memories.Memories); } catch { }
            try { handler.situational.AppendMoodThoughts(thoughts); } catch { }

            var grouped = new Dictionary<string, MoodThoughtDto>();
            foreach (var t in thoughts)
            {
                try
                {
                    var stage = t.CurStage;
                    if (stage == null || stage.baseMoodEffect == 0f) continue;

                    var key = t.LabelCap.ToString();
                    if (grouped.TryGetValue(key, out var existing))
                        existing.MoodEffect += stage.baseMoodEffect;
                    else
                        grouped[key] = new MoodThoughtDto { Label = key, MoodEffect = stage.baseMoodEffect };
                }
                catch { /* некоторые мысли могут кидать на CurStage в редких случаях */ }
            }

            return grouped.Values
                .OrderBy(m => m.MoodEffect)  // самые вредные (отрицательные) — первыми
                .Take(8)
                .Select(m => new MoodThoughtDto { Label = m.Label, MoodEffect = (float)Math.Round(m.MoodEffect, 1) })
                .ToList();
        }

        private static List<PendingConstructionDto> SerializePendingConstruction(Map map)
        {
            var result = new List<PendingConstructionDto>();
            foreach (var t in map.listerThings.AllThings)
            {
                if (t is Blueprint_Build bb)
                {
                    result.Add(new PendingConstructionDto
                    {
                        DefName  = bb.def.entityDefToBuild?.defName ?? bb.def.defName,
                        Position = new PositionDto { X = bb.Position.x, Z = bb.Position.z },
                        Status   = "blueprint",
                    });
                }
                else if (t is Frame frame)
                {
                    var pct = frame.WorkToBuild > 0
                        ? (int)Math.Round(100f * frame.workDone / frame.WorkToBuild)
                        : 0;
                    result.Add(new PendingConstructionDto
                    {
                        DefName  = frame.def.entityDefToBuild?.defName ?? frame.def.defName,
                        Position = new PositionDto { X = frame.Position.x, Z = frame.Position.z },
                        Status   = $"building {pct}%",
                    });
                }
            }
            return result;
        }

        private static List<AnimalDto> SerializeAnimals(Map map)
        {
            return map.mapPawns.AllPawnsSpawned
                .Where(p => p.RaceProps.Animal && p.Faction == Faction.OfPlayer && !p.Dead)
                .Select(p => new AnimalDto
                {
                    Id           = p.ThingID,
                    Name         = p.Name?.ToStringShort ?? p.def.defName,
                    DefName      = p.def.defName,
                    HealthPercent= (float)Math.Round(p.health?.summaryHealth?.SummaryHealthPercent ?? 1f, 2),
                    IsTrained    = p.training?.HasLearned(TrainableDefOf.Obedience) ?? false,
                    MasterPawnId = p.playerSettings?.Master?.ThingID,
                    Position     = new PositionDto { X = p.Position.x, Z = p.Position.z }
                })
                .ToList();
        }

        private static List<ThreatDto> SerializeThreats(Map map)
        {
            return map.mapPawns.AllPawnsSpawned
                .Where(p => !p.Dead && p.HostileTo(Faction.OfPlayer))
                .Select(p => new ThreatDto
                {
                    Id           = p.ThingID,
                    Name         = p.LabelShort,
                    DefName      = p.def.defName,
                    Faction      = p.Faction?.Name ?? "Unknown",
                    HealthPercent= (float)Math.Round(p.health?.summaryHealth?.SummaryHealthPercent ?? 1f, 2),
                    IsDowned     = p.Downed,
                    Kind         = p.RaceProps.Humanlike ? "human" : p.RaceProps.IsMechanoid ? "mech" : p.RaceProps.Animal ? "animal" : "other",
                    IsFleeing    = p.mindState?.mentalStateHandler?.CurStateDef == MentalStateDefOf.PanicFlee || p.CurJobDef == JobDefOf.Flee,
                    IsRanged     = p.equipment?.Primary?.def?.IsRangedWeapon ?? false,
                    Job          = p.CurJobDef?.defName,
                    Weapon       = p.equipment?.Primary?.def.defName,
                    Position     = new PositionDto { X = p.Position.x, Z = p.Position.z }
                })
                .ToList();
        }

        private static List<BuildingDto> SerializeBuildings(Map map)
        {
            return map.listerBuildings.allBuildingsColonist
                .Where(b => b?.def != null)
                .Select(b =>
                {
                    List<BillDto> bills = null;
                    if (b is IBillGiver billGiver)
                        bills = billGiver.BillStack.Bills
                            .OfType<Bill_Production>()
                            .Select(bill => new BillDto
                            {
                                Id     = bill.GetUniqueLoadID(),
                                Recipe = bill.recipe?.defName,
                                Count  = bill.repeatCount,
                            })
                            .ToList();

                    return new BuildingDto
                    {
                        Id       = b.ThingID,
                        DefName  = b.def.defName,
                        Label    = b.Label,
                        Position = new PositionDto { X = b.Position.x, Z = b.Position.z },
                        HpPercent= b.MaxHitPoints > 0
                            ? (float)Math.Round((float)b.HitPoints / b.MaxHitPoints, 2)
                            : 1f,
                        Bills    = bills,
                    };
                })
                .ToList();
        }

        private static List<ZoneDto> SerializeZones(Map map)
        {
            return map.zoneManager.AllZones
                .Select(z => new ZoneDto
                {
                    Id    = z.GetUniqueLoadID(),
                    Type  = z.GetType().Name,
                    Label = z.label,
                })
                .ToList();
        }

        private static ResearchDto SerializeResearch()
        {
            var rm = Find.ResearchManager;
            var completed = DefDatabase<ResearchProjectDef>.AllDefsListForReading
                .Where(p => p.IsFinished).Select(p => p.defName).ToList();

            var available = DefDatabase<ResearchProjectDef>.AllDefsListForReading
                .Where(p => !p.IsFinished && p.CanStartNow).Select(p => p.defName).ToList();

            var currentProject = DefDatabase<ResearchProjectDef>.AllDefsListForReading
                .FirstOrDefault(p => rm.IsCurrentProject(p));

            float progress = currentProject != null ? rm.GetProgress(currentProject) : 0f;

            return new ResearchDto
            {
                CurrentProject  = currentProject?.defName,
                CurrentProgress = (float)Math.Round(progress, 3),
                Available       = available,
                Completed       = completed
            };
        }

        private static List<VisitorDto> SerializeVisitors(Map map)
        {
            // Получаем всех пешек на карте, которые не враждебны и не твои колонисты.
            // Это торговцы, гости, беженцы и т.д.
            return map.mapPawns.AllPawnsSpawned
                .Where(p => !p.Dead &&
                           !p.HostileTo(Faction.OfPlayer) &&
                           p.Faction != Faction.OfPlayer &&
                           p.Faction != null)
                .Take(20)  // ограничим 20 посетителями чтобы не раздувать JSON
                .Select(p => new VisitorDto
                {
                    Id       = p.ThingID,
                    Name     = p.Name?.ToStringShort ?? p.def.defName,
                    Faction  = p.Faction?.Name ?? "Unknown",
                    IsTrader = p.trader != null,  // true если это торговец
                    Goods    = GetTraderGoods(p),
                    Position = new PositionDto { X = p.Position.x, Z = p.Position.z }
                })
                .ToList();
        }

        private static List<string> GetTraderGoods(Pawn pawn)
        {
            if (pawn?.inventory?.innerContainer == null)
                return new List<string>();

            return pawn.inventory.innerContainer
                .Select(t => t.def.defName)
                .Distinct()
                .Take(10)
                .ToList();
        }

        private static float EstimateFoodDays(Map map, List<Pawn> colonists)
        {
            if (colonists.Count == 0) return 999f;

            float totalNutrition = 0f;
            foreach (var thing in map.listerThings.ThingsInGroup(ThingRequestGroup.FoodSourceNotPlantOrTree))
            {
                if (thing.def.IsNutritionGivingIngestible && !thing.def.IsCorpse)
                    totalNutrition += thing.GetStatValue(StatDefOf.Nutrition) * thing.stackCount;
            }

            float dailyNeed = colonists.Count(p => !p.IsPrisoner) * 1.6f;
            return dailyNeed > 0 ? (float)Math.Round(totalNutrition / dailyNeed, 1) : 999f;
        }

        private static List<AlertDto> SerializeAlerts(Map map)
        {
            var alerts = new List<AlertDto>();

            // Критичные alert'ы — быстрые проверки состояния базы
            var colonists = map.mapPawns.FreeColonistsAndPrisoners.ToList();

            // Нет колонистов
            if (colonists.Count == 0)
                alerts.Add(new AlertDto
                {
                    Category = "colony_empty",
                    Message  = "Все колонисты мертвы. Колония потеряна.",
                    Severity = "critical"
                });

            // Пищевой кризис
            float foodDays = EstimateFoodDays(map, colonists);
            if (foodDays < 1)
                alerts.Add(new AlertDto
                {
                    Category = "food_critical",
                    Message  = $"Критично низкие запасы еды (~{foodDays:F1} дней)",
                    Severity = "critical"
                });
            else if (foodDays < 5)
                alerts.Add(new AlertDto
                {
                    Category = "food_low",
                    Message  = $"Низкие запасы еды (~{foodDays:F1} дней)",
                    Severity = "warning"
                });

            // Пожары
            int fireCount = 0;
            foreach (var thing in map.listerThings.AllThings)
                if (thing is Fire) fireCount++;
            if (fireCount > 0)
                alerts.Add(new AlertDto
                {
                    Category = "fires",
                    Message  = $"{fireCount} очагов пожара на карте",
                    Severity = "critical"
                });

            // Враждебные пешки на карте (рейды, бежавшие животные)
            var hostiles = map.mapPawns.AllPawnsSpawned
                .Where(p => !p.Dead && p.HostileTo(Faction.OfPlayer))
                .Count();
            if (hostiles > 0)
                alerts.Add(new AlertDto
                {
                    Category = "hostiles_on_map",
                    Message  = $"{hostiles} враждебных существ на карте",
                    Severity = "critical"
                });

            // Лимитируем до 10 алертов чтобы не раздувать JSON
            return alerts.Take(10).ToList();
        }
    }
}

using System;
using System.Collections.Generic;
using System.Linq;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using RimWorld;
using Verse;
using Verse.AI;

namespace AgentBridge
{
    // Волна 1 (бой): предметы, одежда, спасение, арест, лечение, режимы игры.
    // Все команды бросают исключение с понятным текстом, если сделать нельзя: оно уходит агенту как ERROR.
    public static partial class CommandExecutor
    {
        // Полный список команд: агент по нему проверяет, что мод новее его инструментов.
        private static readonly string[] SupportedCommands =
        {
            "set_work_priority", "set_schedule", "set_medical_care", "draft", "undraft", "move", "attack",
            "cancel_job", "equip", "wear", "rescue", "capture", "tend", "build", "cancel_construction",
            "deconstruct", "designate_mine", "designate_chop", "designate_smooth", "find_buildable_spot",
            "place_line", "find_nearest", "search_thing_defs", "set_ai_cursor", "clear_ai_cursor",
            "set_zone_home", "create_grow_zone", "create_stockpile", "delete_zone", "add_bill", "cancel_bill", "list_recipes", "look_at",
            "order_tame", "order_slaughter", "forbid", "unforbid", "research", "pause", "resume",
            "set_time_speed", "get_items", "set_manual_priorities", "force_work", "clear_work_queue", "capabilities",
            "plan_room", "build_room", "clear_plans", "room_info",
            "check_enclosure", "camera_jump", "camera_follow", "open_tab", "select", "set_auto_camera", "set_user_camera_override", "set_strict_ui", "camera_zoom", "camera_move", "camera_view", "survey_map", "analyze_area", "estimate_build", "describe_def", "fit_room", "get_work_table",
            "camera_mode", "camera_goto", "camera_track", "camera_release", "camera_state",
        };

        private static string Capabilities()
        {
            return JsonConvert.SerializeObject(new { wave = 1, commands = SupportedCommands });
        }

        // ── Предметы на карте ──────────────────────────────────────────────────

        private static string GetItems(JObject cmd)
        {
            var map = Find.CurrentMap;
            var category = (cmd["category"]?.Value<string>() ?? "weapon").ToLowerInvariant();
            var max = Math.Min(cmd["max_results"]?.Value<int>() ?? 30, 80);
            var center = new IntVec3(cmd["x"]?.Value<int>() ?? map.Center.x, 0, cmd["z"]?.Value<int>() ?? map.Center.z);
            var onlyFree = cmd["only_unforbidden"]?.Value<bool>() ?? false;

            Func<ThingDef, bool> match = category switch
            {
                "weapon"   => d => d.IsWeapon,
                "ranged"   => d => d.IsRangedWeapon,
                "melee"    => d => d.IsMeleeWeapon,
                "apparel"  => d => d.IsApparel,
                "medicine" => d => d.IsMedicine,
                "food"     => d => d.IsNutritionGivingIngestible && !d.IsCorpse,
                _ => throw new Exception($"category должна быть weapon/ranged/melee/apparel/medicine/food, а не '{category}'"),
            };

            var found = map.listerThings.AllThings
                .Where(t => t.def.category == ThingCategory.Item && t.Spawned && match(t.def))
                .Where(t => !onlyFree || !t.IsForbidden(Faction.OfPlayer))
                .OrderBy(t => t.Position.DistanceTo(center))
                .Take(max)
                .Select(t => DescribeItem(t))
                .ToList();

            return JsonConvert.SerializeObject(new { count = found.Count, items = found });
        }

        private static object DescribeItem(Thing t)
        {
            string quality = t.TryGetQuality(out var q) ? q.ToString() : null;
            double? armorSharp = null, armorBlunt = null;
            if (t.def.IsApparel)
            {
                armorSharp = Math.Round(t.GetStatValue(StatDefOf.ArmorRating_Sharp), 2);
                armorBlunt = Math.Round(t.GetStatValue(StatDefOf.ArmorRating_Blunt), 2);
            }
            float? range = null;
            if (t.def.IsRangedWeapon && !t.def.Verbs.NullOrEmpty())
                range = t.def.Verbs[0].range;
            return new
            {
                id = t.ThingID,
                def = t.def.defName,
                label = t.LabelNoCount,
                count = t.stackCount,
                x = t.Position.x,
                z = t.Position.z,
                forbidden = t.IsForbidden(Faction.OfPlayer),
                quality,
                hp_pct = t.MaxHitPoints > 0 ? Math.Round((double)t.HitPoints / t.MaxHitPoints, 2) : 1.0,
                ranged = t.def.IsRangedWeapon,
                range,
                armor_sharp = armorSharp,
                armor_blunt = armorBlunt,
            };
        }

        // ── Предметы: взять в руки и надеть через работу пешки ───────────────────

        private static Thing GetItemOrThrow(JObject cmd)
        {
            var itemId = cmd["item_id"]?.Value<string>() ?? throw new ArgumentException("Missing 'item_id'");
            return Find.CurrentMap.listerThings.AllThings
                .FirstOrDefault(t => t.ThingID == itemId && t.Spawned)
                ?? throw new Exception($"Item not found: {itemId} (его нет на карте или его уже кто-то держит)");
        }

        private static void EquipViaJob(JObject cmd)
        {
            var pawn = GetPawn(cmd);
            var item = GetItemOrThrow(cmd);
            if (!(item is ThingWithComps) || item.def.equipmentType == EquipmentType.None)
                throw new Exception($"{item.def.defName} не оружие и не инструмент: в руки взять нельзя");
            if (pawn.WorkTagIsDisabled(WorkTags.Violent) && item.def.IsWeapon)
                throw new Exception($"{pawn.LabelShort} по характеру не может держать оружие");
            item.SetForbidden(false, false);
            var job = JobMaker.MakeJob(JobDefOf.Equip, item);
            if (!pawn.jobs.TryTakeOrderedJob(job, JobTag.Misc))
                throw new Exception($"{pawn.LabelShort} не смогла взяться: недоступно или занято");
        }

        private static void WearViaJob(JObject cmd)
        {
            var pawn = GetPawn(cmd);
            var item = GetItemOrThrow(cmd);
            if (!(item is Apparel apparel))
                throw new Exception($"{item.def.defName} не одежда");
            if (!ApparelUtility.HasPartsToWear(pawn, apparel.def))
                throw new Exception($"{pawn.LabelShort} не может это надеть (нет подходящих частей тела)");
            item.SetForbidden(false, false);
            var job = JobMaker.MakeJob(JobDefOf.Wear, apparel);
            if (!pawn.jobs.TryTakeOrderedJob(job, JobTag.Misc))
                throw new Exception($"{pawn.LabelShort} не смогла взяться: недоступно или занято");
        }

        // ── Атака: вблизи или стрельбой, смотря что у бойца в руках ───────────────

        private static void AttackSmart(JObject cmd)
        {
            var pawn = GetPawn(cmd);
            var targetId = cmd["target_id"].Value<string>();
            var target = Find.CurrentMap.mapPawns.AllPawnsSpawned
                .FirstOrDefault(p => p.ThingID == targetId)
                ?? throw new Exception($"Target not found: {targetId}");
            if (pawn.Downed) throw new Exception($"{pawn.LabelShort} лежит и сражаться не может");

            var verb = pawn.TryGetAttackVerb(target, true);
            Job job;
            if (verb != null && !verb.verbProps.IsMeleeAttack)
            {
                job = JobMaker.MakeJob(JobDefOf.AttackStatic, target);
                job.verbToUse = verb;
            }
            else
            {
                job = JobMaker.MakeJob(JobDefOf.AttackMelee, target);
            }
            if (!pawn.jobs.TryTakeOrderedJob(job, JobTag.Misc))
                throw new Exception($"{pawn.LabelShort} не смогла начать атаку");
        }

        // ── Раненые и пленные ────────────────────────────────────────────────────

        private static void Rescue(JObject cmd)
        {
            var rescuer = GetPawn(cmd);
            var victim = GetPawnById(cmd["target_id"]?.Value<string>() ?? throw new ArgumentException("Missing 'target_id'"));
            if (!victim.Downed) throw new Exception($"{victim.LabelShort} не лежит, спасать нечего");
            var bed = RestUtility.FindBedFor(victim, rescuer, false);
            if (bed == null) throw new Exception("нет свободной кровати для раненого: построй кровать или освободи");
            var job = JobMaker.MakeJob(JobDefOf.Rescue, victim, bed);
            job.count = 1;
            if (!rescuer.jobs.TryTakeOrderedJob(job, JobTag.Misc))
                throw new Exception($"{rescuer.LabelShort} не может спасти {victim.LabelShort}: недостижим или занято");
        }

        private static void Capture(JObject cmd)
        {
            var captor = GetPawn(cmd);
            var victim = GetPawnById(cmd["target_id"]?.Value<string>() ?? throw new ArgumentException("Missing 'target_id'"));
            if (!victim.Downed) throw new Exception($"{victim.LabelShort} на ногах: арестовывать можно только лежащего");
            if (!victim.HostileTo(Faction.OfPlayer)) throw new Exception($"{victim.LabelShort} не враг");
            var bed = RestUtility.FindBedFor(victim, captor, false, false, GuestStatus.Prisoner);
            if (bed == null) throw new Exception("нет свободной кровати для пленного: построй кровать");
            var job = JobMaker.MakeJob(JobDefOf.Capture, victim, bed);
            job.count = 1;
            if (!captor.jobs.TryTakeOrderedJob(job, JobTag.Misc))
                throw new Exception($"{captor.LabelShort} не может взять в плен {victim.LabelShort}");
        }

        private static void Tend(JObject cmd)
        {
            var doctor = GetPawn(cmd);
            var patient = GetPawnById(cmd["target_id"]?.Value<string>() ?? throw new ArgumentException("Missing 'target_id'"));
            if (doctor.WorkTypeIsDisabled(WorkTypeDefOf.Doctor))
                throw new Exception($"{doctor.LabelShort} не может быть врачом");
            if (!patient.health.HasHediffsNeedingTend())
                throw new Exception($"{patient.LabelShort} лечения не требует");
            var job = JobMaker.MakeJob(JobDefOf.TendPatient, patient);
            job.draftedTend = doctor.Drafted;
            if (!doctor.jobs.TryTakeOrderedJob(job, JobTag.Misc))
                throw new Exception($"{doctor.LabelShort} не может заняться {patient.LabelShort}");
        }

        // ── Режимы игры ───────────────────────────────────────────────────────────

        private static void SetManualPriorities(JObject cmd)
        {
            Current.Game.playSettings.useWorkPriorities = cmd["enabled"]?.Value<bool>() ?? true;
        }
    }
}

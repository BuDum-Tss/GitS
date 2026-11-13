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
    // Принудительные работы: то же, что «приоритет» в меню правой кнопки мыши.
    // Пешка бросает текущее дело и выполняет работу на указанной клетке, а следующие цели встают в очередь
    // по порядку (как с зажатым Shift). Работу подбирает сама игра по ее правилам (WorkGiver с forced=true),
    // поэтому годится любое занятие: стройка, рубка, добыча, перевозка, готовка по заказу, лечение, уборка, посев.
    public static partial class CommandExecutor
    {
        private static string ForceWork(JObject cmd)
        {
            var pawn = GetPawn(cmd);
            var targets = cmd["targets"] as JArray;
            if (targets == null || targets.Count == 0)
                throw new Exception("targets пуст: нужен список клеток [{x, z}] в том порядке, в каком их делать");
            if (pawn.Downed) throw new Exception($"{pawn.LabelShort} лежит и работать не может");
            if (pawn.Dead) throw new Exception($"{pawn.LabelShort} мертва");

            var appendQueue = cmd["queue"]?.Value<bool>() ?? false;   // false: заменить текущее дело
            var globalFilter = cmd["work"]?.Value<string>();
            var accepted = new List<string>();
            var rejected = new List<string>();
            bool first = !appendQueue;

            if (!appendQueue)
                pawn.jobs.ClearQueuedJobs();

            foreach (var t in targets)
            {
                int x = t["x"]?.Value<int>() ?? throw new Exception("у цели нет x");
                int z = t["z"]?.Value<int>() ?? throw new Exception("у цели нет z");
                var filter = t["work"]?.Value<string>() ?? globalFilter;
                var cell = new IntVec3(x, 0, z);
                if (!cell.InBounds(pawn.Map))
                {
                    rejected.Add($"({x},{z}): вне карты");
                    continue;
                }

                var job = FindForcedJob(pawn, cell, filter, out var giver, out var why);
                if (job == null)
                {
                    rejected.Add($"({x},{z}): {why}");
                    continue;
                }

                if (!pawn.jobs.TryTakeOrderedJob(job, giver.tagToGive, !first))
                {
                    rejected.Add($"({x},{z}): {pawn.LabelShort} не смогла взяться ({giver.defName})");
                    continue;
                }
                first = false;
                accepted.Add($"({x},{z}) {giver.workType?.defName ?? giver.defName}");
            }

            if (accepted.Count == 0)
                throw new Exception("ничего не принято: " + string.Join("; ", rejected));
            return JsonConvert.SerializeObject(new
            {
                pawn = pawn.LabelShort,
                accepted,
                rejected,
            });
        }

        // Ищет для пешки работу на клетке: сначала на вещах (чертежи, деревья, предметы, верстаки), затем на самой клетке.
        private static Job FindForcedJob(Pawn pawn, IntVec3 cell, string filter, out WorkGiverDef usedGiver, out string why)
        {
            usedGiver = null;
            why = "на клетке нет работы, которую она может сделать";
            var things = cell.GetThingList(pawn.Map).ToList();
            var givers = DefDatabase<WorkGiverDef>.AllDefsListForReading
                .Where(g => g.Worker is WorkGiver_Scanner)
                .Where(g => g.workType == null || !pawn.WorkTypeIsDisabled(g.workType))
                .Where(g => !pawn.WorkTagIsDisabled(g.workTags))
                .Where(g => string.IsNullOrEmpty(filter)
                            || string.Equals(g.defName, filter, StringComparison.OrdinalIgnoreCase)
                            || string.Equals(g.workType?.defName, filter, StringComparison.OrdinalIgnoreCase))
                .OrderByDescending(g => g.priorityInType)
                .ToList();

            if (givers.Count == 0)
            {
                why = string.IsNullOrEmpty(filter)
                    ? "для этой пешки нет доступных видов работы"
                    : $"вид работы «{filter}» ей недоступен или названия такого нет";
                return null;
            }

            foreach (var giver in givers)
            {
                var scanner = (WorkGiver_Scanner)giver.Worker;
                try
                {
                    if (scanner.ShouldSkip(pawn, true)) continue;
                    foreach (var thing in things)
                    {
                        if (!scanner.HasJobOnThing(pawn, thing, true)) continue;
                        var job = scanner.JobOnThing(pawn, thing, true);
                        if (job != null) { usedGiver = giver; return job; }
                    }
                    if (scanner.HasJobOnCell(pawn, cell, true))
                    {
                        var job = scanner.JobOnCell(pawn, cell, true);
                        if (job != null) { usedGiver = giver; return job; }
                    }
                }
                catch (Exception)
                {
                    // некоторые WorkGiver падают на чужих вещах: пропускаем, ищем дальше
                }
            }
            return null;
        }

        private static void ClearWorkQueue(JObject cmd)
        {
            var pawn = GetPawn(cmd);
            pawn.jobs.ClearQueuedJobs();
            if (cmd["stop_current"]?.Value<bool>() ?? false)
                pawn.jobs.EndCurrentJob(JobCondition.InterruptForced, true);
        }
    }
}

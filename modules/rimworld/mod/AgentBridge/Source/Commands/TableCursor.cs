using System;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using HarmonyLib;
using Newtonsoft.Json.Linq;
using RimWorld;
using UnityEngine;
using Verse;

namespace AgentBridge
{
    /// Курсор к реальной ячейке таблицы (ТЗ 20, курсор на таблицах): правки приоритетов (окно «Работа»), расписания
    /// («Режим дня») и медицинского ухода («Назначение») показываются так, что рука ГЛаДОС едет к строке пешки и к столбцу
    /// работы, а число меняется уже после клика.
    ///
    /// Откуда положение (всё приблизительно там, где игра не отдаёт точных данных; ограничения в docs/MOD_ROADMAP.md):
    ///   - origin таблицы: постфикс PawnTable.PawnTableOnGUI запоминает позицию, с которой таблица нарисована (UI-координаты);
    ///   - высоты строк и ширины колонок: приватные кэши самой таблицы cachedRowHeights / cachedColumnWidths (рефлексия),
    ///     вертикальная прокрутка scrollPosition, высота шапки PawnTable.HeaderHeight (если поля нет, 40 пикселей);
    ///   - строка: позиция пешки в PawnsListForReading; столбец работы: колонка PawnTableDef.columns, воркер которой
    ///     держит этот WorkTypeDef; столбец ухода: воркер с именем «MedicalCare»;
    ///   - без таблицы: строка по порядку колонистов, столбец по порядку работы, расписание по часам на ширину экрана.
    public static partial class CommandExecutor
    {
        public static PawnTable LastTable;         // таблица, которую игра нарисовала последней (пишет постфикс)
        public static Vector2 LastOrigin;          // её верхний левый угол в UI-координатах

        private const float SequenceGap = 2f;      // дольше этого без правок в таблице: новая серия
        private static float _tableSeqStart = -100f;
        private static float _tableSeqLast = -100f;

        /// Ожидание перед правкой ячейки и курсор к ней. Возвращает секунды до самой правки (минимум 0.05, чтобы сработала вспышка клика).
        private static float AimAtTable(string type, JObject cmd)
        {
            var spot = TableSpot(type, cmd);
            var now = Time.realtimeSinceStartup;
            bool first = now - _tableSeqLast > SequenceGap;
            if (first) _tableSeqStart = now;
            _tableSeqLast = now;
            var from = AgentBridgeMod.AiCursorActive ? new Vector2(AgentBridgeMod.AiCursorX, AgentBridgeMod.AiCursorY) : UI.MousePositionOnUIInverted;
            float wait = TableGeometry.WaitFor(Vector2.Distance(from, spot), first, now - _tableSeqStart);
            CursorGlide(spot);
            return Mathf.Max(wait, 0.05f);
        }

        /// Точка экрана, куда ехать: ячейка пешки и работы/ухода или приблизительная точка, если таблицы нет.
        private static Vector2 TableSpot(string type, JObject cmd)
        {
            var table = OpenPawnTable();
            var origin = table != null && ReferenceEquals(LastTable, table) ? LastOrigin : TableGeometry.FallbackOrigin();

            if (type == "set_manual_priorities")       // переключатель «ручные приоритеты» стоит над таблицей (приближённо)
                return TableGeometry.OnScreen(origin + new Vector2(80f, -20f));

            var pawn = Find.CurrentMap.mapPawns.AllPawns.FirstOrDefault(p => p.ThingID == cmd["pawn_id"]?.Value<string>());
            int row = RowIndex(table, pawn);
            var colWidths = table != null ? PrivateList(table, "cachedColumnWidths") : null;
            var rowHeights = table != null ? PrivateList(table, "cachedRowHeights") : null;
            float header = table != null ? HeaderHeight(table) : TableGeometry.DefaultHeaderHeight;
            float scrollY = table != null && PrivateValue(table, "scrollPosition") is Vector2 sp ? sp.y : 0f;

            switch (type)
            {
                case "set_work_priority":
                {
                    var work = DefDatabase<WorkTypeDef>.GetNamedSilentFail(cmd["work_type"]?.Value<string>() ?? "");
                    int col = WorkColumn(table, work);
                    return TableGeometry.OnScreen(TableGeometry.CellCenter(origin, colWidths, rowHeights, header, scrollY, row, col));
                }
                case "set_medical_care":
                {
                    int col = table != null ? ColumnIndexWhere(table, c => WorkerNameContains(c, "MedicalCare")) : -1;
                    if (col < 0) col = 1;
                    return TableGeometry.OnScreen(TableGeometry.CellCenter(origin, colWidths, rowHeights, header, scrollY, row, col));
                }
                case "set_schedule":
                case "set_schedule_range":
                {
                    // расписание: часы по ширине окна (колонки по часам); строка пешки из таблицы, если она есть
                    int hour = Mathf.Clamp(cmd["hour"]?.Value<int>() ?? 0, 0, 23);
                    return TableGeometry.OnScreen(TableGeometry.HourCell(origin, row, hour, header, rowHeights, scrollY));
                }
            }
            return TableGeometry.OnScreen(origin);
        }

        /// Открытое окно таблицы (MainTabWindow, в котором есть PawnTable), иначе null.
        private static PawnTable OpenPawnTable()
        {
            var windows = Find.WindowStack?.Windows;
            if (windows == null) return null;
            foreach (var w in windows.OfType<MainTabWindow>())
            {
                var t = ValuesOfType(w, typeof(PawnTable)).OfType<PawnTable>().FirstOrDefault();
                if (t != null) return t;
            }
            return null;
        }

        private static int RowIndex(PawnTable table, Pawn pawn)
        {
            if (table != null && pawn != null)
            {
                int i = table.PawnsListForReading.ToList().IndexOf(pawn);
                if (i >= 0) return i;
            }
            var colonists = Find.CurrentMap.mapPawns.FreeColonistsSpawned.ToList();
            return Mathf.Max(0, colonists.IndexOf(pawn));
        }

        /// Столбец работы: колонка таблицы, чей воркер держит этот WorkTypeDef. Нет такой: порядок видимых работ после
        /// остальных колонок (приблизительно).
        private static int WorkColumn(PawnTable table, WorkTypeDef work)
        {
            if (table != null && work != null)
            {
                int i = ColumnIndexWhere(table, c => ValuesOfType(c.Worker, typeof(WorkTypeDef)).Any(v => ReferenceEquals(v, work)));
                if (i >= 0) return i;
            }
            var types = DefDatabase<WorkTypeDef>.AllDefsListForReading.Where(w => w.visible).OrderByDescending(w => w.naturalPriority).ToList();
            int index = Mathf.Max(0, types.IndexOf(work));
            int offset = table != null ? Mathf.Max(0, (PrivateList(table, "cachedColumnWidths")?.Count ?? types.Count) - types.Count) : 0;
            return offset + index;
        }

        private static int ColumnIndexWhere(PawnTable table, Func<PawnColumnDef, bool> match)
        {
            var def = ValuesOfType(table, typeof(PawnTableDef)).OfType<PawnTableDef>().FirstOrDefault();
            if (def == null) return -1;
            var cols = def.columns;
            for (int i = 0; i < cols.Count; i++)
                if (cols[i] != null && match(cols[i])) return i;
            return -1;
        }

        private static bool WorkerNameContains(PawnColumnDef col, string part)
        {
            var worker = col.Worker;
            return worker != null && worker.GetType().Name.IndexOf(part, StringComparison.OrdinalIgnoreCase) >= 0;
        }

        private static float HeaderHeight(PawnTable table)
        {
            var field = AccessTools.Field(typeof(PawnTable), "HeaderHeight");
            if (field != null && field.GetValue(null) is float h) return h;
            return TableGeometry.DefaultHeaderHeight;
        }

        private static IList<float> PrivateList(object obj, string name) => PrivateValue(obj, name) as IList<float>;

        private static object PrivateValue(object obj, string name)
        {
            var field = AccessTools.Field(obj.GetType(), name);
            return field?.GetValue(obj);
        }

        /// Значения полей и свойств объекта (включая базовые классы), чей тип подходит. Для поиска таблицы и воркеров без
        /// привязки к именам, которые могут отличаться в версиях игры.
        private static IEnumerable<object> ValuesOfType(object obj, Type type)
        {
            if (obj == null) yield break;
            const BindingFlags flags = BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly;
            for (var t = obj.GetType(); t != null && t != typeof(object); t = t.BaseType)
            {
                foreach (var f in t.GetFields(flags))
                {
                    if (!type.IsAssignableFrom(f.FieldType)) continue;
                    var v = f.GetValue(obj);
                    if (v != null) yield return v;
                }
                foreach (var p in t.GetProperties(flags))
                {
                    if (!p.CanRead || p.GetIndexParameters().Length > 0 || !type.IsAssignableFrom(p.PropertyType)) continue;
                    object v = null;
                    try { v = p.GetValue(obj, null); } catch (Exception) { }
                    if (v != null) yield return v;
                }
            }
        }
    }

    /// Чистая геометрия курсора: без объектов игры, по индексам и размерам (проверяется глазами в игре; см. MOD_ROADMAP).
    public static class TableGeometry
    {
        public const float DefaultHeaderHeight = 40f;
        public const float DefaultRowHeight = 26f;
        public const float DefaultColWidth = 46f;
        public const float TravelSpeed = 420f;      // пикселей в секунду: минимальная скорость руки (CursorMinSpeed в CameraCommands)
        public const float SequenceBudget = 4f;     // общий потолок ожидания серии правок
        public const float ShortHopMax = 0.6f;      // соседняя ячейка внутри серии: не дольше
        public const float ShortHopDistance = 300f; // соседняя = ближе этого, пикселей
        public const float FirstMoveMax = 3f;       // первый (дальний) перелёт в серии

        public static float ColumnAt(IList<float> widths, int i) => widths != null && i >= 0 && i < widths.Count ? widths[i] : DefaultColWidth;
        public static float RowAt(IList<float> heights, int i) => heights != null && i >= 0 && i < heights.Count ? heights[i] : DefaultRowHeight;

        /// Центр ячейки (UI-координаты). origin: верхний левый угол таблицы (шапка сверху), scrollY: прокрутка строк.
        public static Vector2 CellCenter(Vector2 origin, IList<float> colWidths, IList<float> rowHeights, float headerHeight,
                                         float scrollY, int row, int col)
        {
            float x = origin.x, y = origin.y + headerHeight;
            for (int i = 0; i < col; i++) x += ColumnAt(colWidths, i);
            for (int i = 0; i < row; i++) y += RowAt(rowHeights, i);
            return new Vector2(x + ColumnAt(colWidths, col) / 2f, y + RowAt(rowHeights, row) / 2f - scrollY);
        }

        /// Клетка расписания: часы делят ширину от origin до правого края экрана поровну (приблизительно).
        public static Vector2 HourCell(Vector2 origin, int row, int hour, float headerHeight, IList<float> rowHeights, float scrollY)
        {
            float span = Mathf.Max(200f, UI.screenWidth - origin.x - 20f);
            float x = origin.x + (hour + 0.5f) * span / 24f;
            float y = origin.y + headerHeight;
            for (int i = 0; i < row; i++) y += RowAt(rowHeights, i);
            return new Vector2(x, y + RowAt(rowHeights, row) / 2f - scrollY);
        }

        /// Если таблица не найдена: левый верх рабочей области под вкладками.
        public static Vector2 FallbackOrigin() => new Vector2(UI.screenWidth * 0.02f, UI.screenHeight * 0.12f);

        public static Vector2 OnScreen(Vector2 p) =>
            new Vector2(Mathf.Clamp(p.x, 2f, UI.screenWidth - 2f), Mathf.Clamp(p.y, 2f, UI.screenHeight - 2f));

        /// Сколько ждать перед правкой. Первый перелёт в серии: как едет рука (до FirstMoveMax). Следующие ячейки
        /// (соседние): не дольше ShortHopMax и не дольше остатка общего бюджета SequenceBudget.
        public static float WaitFor(float distance, bool firstInSequence, float sequenceElapsed)
        {
            float travel = 0.1f + distance / TravelSpeed;
            if (firstInSequence) return Mathf.Min(travel, FirstMoveMax);
            float cap = distance <= ShortHopDistance ? ShortHopMax : FirstMoveMax;
            return Mathf.Min(travel, cap, Mathf.Max(0f, SequenceBudget - sequenceElapsed));
        }
    }

    /// Постфикс отрисовки таблицы: запоминаем, где она нарисована (UI-координаты), для курсора. Ничего не меняет в игре.
    [HarmonyPatch(typeof(PawnTable), nameof(PawnTable.PawnTableOnGUI))]
    public static class PawnTableDrawRecorder
    {
        public static void Postfix(PawnTable __instance, object[] __args)
        {
            if (__args != null && __args.Length > 0 && __args[0] is Vector2 pos)
            {
                CommandExecutor.LastTable = __instance;
                CommandExecutor.LastOrigin = pos;
            }
        }
    }
}

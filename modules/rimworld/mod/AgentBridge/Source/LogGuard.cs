using System;
using System.Collections.Generic;
using System.Linq;
using HarmonyLib;
using Verse;

namespace AgentBridge
{
    // ТЗ 11c, GT-5: пока к моду подключён агент, ошибки игры не должны ни открывать журнал отладки поверх обзора, ни ставить игру
    // на паузу. Механизмы RimWorld 1.6 (проверено рефлексией по Assembly-CSharp.dll, не угадано):
    //   Log.Error   -> если DebugSettings.pauseOnError и идёт игра: Find.TickManager.Pause();
    //               -> если Prefs.DevMode: Log.TryOpenLogWindow() -> LudeonTK.EditWindow_Log.TryAutoOpen();
    //   Log.Warning -> если Prefs.OpenLogOnWarnings: то же самое;
    //   TryAutoOpen -> если EditWindow_Log.canAutoOpen: EditWindow_Log.wantsToOpen = true (окно откроет отладочный интерфейс).
    // Поэтому пока агент подключён, мы держим canAutoOpen = false, wantsToOpen = false, pauseOnError = false и закрываем
    // уже открытое окно журнала; прежние значения возвращаются, когда агента нет дольше таймаута. Сами ошибки не пропадают:
    // счётчик и последние строки идут агенту в /state (поле game_log), игра по-прежнему пишет их в свой журнал.
    //
    // ТЗ 11d, DP-1: окно «Dev palette» (LudeonTK.Dialog_DevPalette : Window_Dev, класс найден рефлексией по Assembly-CSharp.dll
    // RimWorld 1.6) висит в левом верхнем углу, пока включён devMode, и закрывает обзор. Открывают его не только при старте:
    // Verse.DebugWindowsOpener.TryOpenOrClosePalette вызывают UIRoot_Play.Init и Game.FinalizeInit (загрузка/смена карты),
    // DevToolStarterOnGUI и DrawButtons. Поэтому пока агент подключён, мы закрываем окно повторно, каждые ~30 кадров.
    // Без агента окно не трогаем.
    public static class LogGuard
    {
        public const long AgentTimeoutMs = 60000;       // агент считается подключённым, если запрос был не раньше этого
        public const int LastErrorsKept = 20;
        public const int LineMax = 400;

        private static long _lastRequestMs;             // момент последнего запроса к моду; 0: не было
        private static int _clientAborts;
        private static int _windowsClosed;
        private static int _paletteClosed;
        private static bool _suppressing;
        private static bool _savedCanAutoOpen;
        private static bool _savedPauseOnError;

        private static readonly Type LogWindowType = AccessTools.TypeByName("LudeonTK.EditWindow_Log");
        private static readonly System.Reflection.FieldInfo CanAutoOpen = LogWindowType == null ? null : AccessTools.Field(LogWindowType, "canAutoOpen");
        private static readonly System.Reflection.FieldInfo WantsToOpen = LogWindowType == null ? null : AccessTools.Field(LogWindowType, "wantsToOpen");
        private static readonly Type PaletteWindowType = AccessTools.TypeByName("LudeonTK.Dialog_DevPalette");
        private static readonly System.Reflection.FieldInfo PauseOnError = AccessTools.Field(typeof(DebugSettings), "pauseOnError");

        private static volatile GameLogDto _summary = new GameLogDto();
        public static GameLogDto Summary => _summary;

        private static long NowMs() { return System.Diagnostics.Stopwatch.GetTimestamp() * 1000 / System.Diagnostics.Stopwatch.Frequency; }

        public static void NoteRequest() { _lastRequestMs = NowMs(); }
        public static void NoteClientAbort() { _clientAborts++; }

        public static bool AgentConnected
        {
            get
            {
                long last = _lastRequestMs;
                return last != 0 && NowMs() - last < AgentTimeoutMs;
            }
        }

        // Клиент закрыл соединение раньше ответа (тайм-аут запроса у агента, обрыв сети): ответ писать некому, команда при этом
        // уже выполнена. Это не ошибка мода и в журнал игры не пишется (даже Warning открыл бы журнал при openLogOnWarnings).
        public static bool IsClientAbort(Exception e)
        {
            for (var x = e; x != null; x = x.InnerException)
            {
                if (x is System.IO.IOException || x is System.Net.HttpListenerException || x is ObjectDisposedException)
                    return true;
                if (x.Message != null && x.Message.IndexOf("transport connection", StringComparison.OrdinalIgnoreCase) >= 0)
                    return true;
            }
            return false;
        }

        // Главный поток (AgentBridgeTicker), раз в ~30 кадров.
        public static void Tick()
        {
            if (AgentConnected)
            {
                if (!_suppressing)
                {
                    _suppressing = true;
                    _savedCanAutoOpen = ReadBool(CanAutoOpen);
                    _savedPauseOnError = ReadBool(PauseOnError);
                }
                WriteBool(CanAutoOpen, false);
                WriteBool(WantsToOpen, false);
                WriteBool(PauseOnError, false);
                CloseLogWindow();
                ClosePaletteWindow();
            }
            else if (_suppressing)
            {
                _suppressing = false;                    // агент ушёл: игра ведёт себя как обычно
                WriteBool(CanAutoOpen, _savedCanAutoOpen);
                WriteBool(PauseOnError, _savedPauseOnError);
            }
            _summary = BuildSummary();
        }

        private static void CloseLogWindow()
        {
            try
            {
                var stack = Find.WindowStack;
                if (stack == null || LogWindowType == null) return;
                if (stack.TryRemove(LogWindowType, false)) _windowsClosed++;
            }
            catch (Exception) { }
        }

        private static void ClosePaletteWindow()
        {
            try
            {
                var stack = Find.WindowStack;
                if (stack == null || PaletteWindowType == null) return;
                if (stack.TryRemove(PaletteWindowType, false)) _paletteClosed++;
            }
            catch (Exception) { }
        }

        private static GameLogDto BuildSummary()
        {
            var dto = new GameLogDto
            {
                AgentConnected = AgentConnected,
                ClientAborts = _clientAborts,
                LogWindowsClosed = _windowsClosed,
                PaletteWindowsClosed = _paletteClosed,
                LastErrors = new List<string>()
            };
            try
            {
                foreach (var m in Log.Messages.ToList())
                {
                    if (m.type == LogMessageType.Error)
                    {
                        dto.Errors += Math.Max(1, m.repeats);
                        var line = (m.text ?? "").Replace("\r", " ").Replace("\n", " ");
                        dto.LastErrors.Add(line.Length > LineMax ? line.Substring(0, LineMax) : line);
                    }
                    else if (m.type == LogMessageType.Warning)
                        dto.Warnings += Math.Max(1, m.repeats);
                }
                if (dto.LastErrors.Count > LastErrorsKept)
                    dto.LastErrors = dto.LastErrors.Skip(dto.LastErrors.Count - LastErrorsKept).ToList();
            }
            catch (Exception) { }                        // очередь журнала менялась во время обхода: возьмём в следующий раз
            return dto;
        }

        private static bool ReadBool(System.Reflection.FieldInfo f)
        {
            try { return f != null && (bool)f.GetValue(null); } catch (Exception) { return false; }
        }

        private static void WriteBool(System.Reflection.FieldInfo f, bool value)
        {
            try { if (f != null) f.SetValue(null, value); } catch (Exception) { }
        }
    }
}

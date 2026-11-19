using System;
using System.Text.RegularExpressions;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using Verse;

namespace AgentBridge
{
    // Команда save_game {name?}: записать текущую игру в сохранение. Нужна инструменту restart_game: перед перезапуском
    // игры колония сохраняется, чтобы не потерять прогресс. Запись синхронная: ответ приходит, когда файл уже записан.
    public static partial class CommandExecutor
    {
        private const string DefaultSaveName = "autosave_agent";
        private static readonly Regex SaveNameRe = new Regex(@"^[A-Za-z0-9_-]{1,60}$");

        private static string SaveGame(JObject cmd)
        {
            if (!InGame) throw new Exception("игра не загружена (главное меню): сохранять нечего");
            var name = (cmd["name"]?.Value<string>() ?? "").Trim();
            if (name.Length == 0) name = DefaultSaveName;
            if (!SaveNameRe.IsMatch(name)) throw new Exception("name: только латиница, цифры, _ и - (до 60 символов)");
            GameDataSaveLoader.SaveGame(name);
            return JsonConvert.SerializeObject(new { saved = true, name });
        }
    }
}

using System;
using Newtonsoft.Json.Linq;

namespace AgentBridge
{
    // Команды режиссёра камеры (camera_mode, camera_goto, camera_track, camera_release, camera_state): плавные кадры для записи видео. Ничего не меняет в колонии.
    public static partial class CommandExecutor
    {
        private static float? Opt(JObject cmd, string key) => cmd[key] == null ? (float?)null : cmd[key].Value<float>();

        private static string CameraCommand(string type, JObject cmd)
        {
            switch (type)
            {
                case "camera_mode":
                    var mode = cmd["mode"]?.Value<string>() ?? "auto";
                    if (mode != "auto" && mode != "off") throw new Exception("mode: auto или off");
                    CameraDirector.Mode = mode;
                    if (mode == "off") CameraDirector.Release();
                    return null;
                case "camera_goto":
                    CameraDirector.GoTo(cmd["x"].Value<float>(), cmd["z"].Value<float>(), Opt(cmd, "size"), Opt(cmd, "smooth"),
                        Opt(cmd, "hold") ?? 6f, cmd["soft"]?.Value<bool>() ?? false);
                    return null;
                case "camera_track":
                    CameraDirector.Follow(cmd["pawn_id"].Value<string>(), Opt(cmd, "size"), Opt(cmd, "smooth"),
                        Opt(cmd, "seconds") ?? 10f, cmd["soft"]?.Value<bool>() ?? false);
                    return null;
                case "camera_release":
                    CameraDirector.Release();
                    return null;
                default:
                    return CameraDirector.StateJson();       // camera_state
            }
        }
    }
}

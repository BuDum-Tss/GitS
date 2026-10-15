using System;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using RimWorld;
using UnityEngine;
using Verse;

namespace AgentBridge
{
    // Плавная камера для записи забега. Каждый кадр камера мягко едет к цели (позиция и масштаб, SmoothDamp), поэтому
    // нет рывков. Цель задают команды camera_goto / camera_track, а в режиме "auto" камера сама смотрит на то, что
    // делает агент: на колониста, которому он отдал приказ, или на клетку стройки.
    public static class CameraDirector
    {
        public const float SizeMin = 6f, SizeMax = 40f;          // ортографический размер камеры: меньше значит ближе
        public static string Mode = "off";                       // "off": камера как у игрока; "auto": следит за действиями
        private static bool _init;
        private static Vector2 _pos, _target, _vel;
        private static float _size = 20f, _targetSize = 20f, _velSize;
        private static float _smooth = 0.7f;
        private static string _followId;
        private static float _followUntil;                       // realtime, после него слежение заканчивается
        private static float _holdUntil;                         // realtime, до него автофокус не перебивает кадр
        private static string _what = "";
        private static bool _hard;                               // кадр важный (приказ агента, событие): лёгкие кадры его не перебивают

        /// Режиссёр сейчас ведёт камеру (запись забега, заданный кадр или слежение): камера стрима из CameraCommands.cs
        /// в это время не двигает кадр, иначе две камеры дерутся каждый кадр.
        // Режим "auto" (запись) сам по себе камеру не отдаёт: режиссёр ведёт кадр только пока держит цель (после команды агента
        // или слежения). Между действиями кадр у пользователя и у автокамеры стрима (CameraCommands.cs).
        public static bool Active => _followId != null || Time.realtimeSinceStartup <= _holdUntil;

        private static bool Ready =>
            Current.ProgramState == ProgramState.Playing && Find.CurrentMap != null && Find.CameraDriver != null;

        private static void Init()
        {
            var t = Find.Camera.transform.position;
            _pos = _target = new Vector2(t.x, t.z);
            _size = _targetSize = Mathf.Clamp(Find.Camera.orthographicSize, SizeMin, SizeMax);
            _vel = Vector2.zero;
            _velSize = 0f;
            _init = true;
        }

        /// Вызывается каждый кадр из AgentBridgeTicker.
        public static void Tick()
        {
            if (!Ready) { _init = false; return; }
            float now = Time.realtimeSinceStartup;
            var cam = Find.CameraDriver;
            if (UserCamera.Check(CamPos(), cam.RootSize)) { _followId = null; _holdUntil = now; _hard = false; }   // пользователь сам двигает кадр: режиссёр отступает
            if (_followId == null && now > _holdUntil)     // никто не ведёт: кадр у пользователя, цель следует за ним
            {
                if (_init) { _pos = _target = CamPos(); _vel = Vector2.zero; _size = _targetSize = cam.RootSize; _velSize = 0f; }
                UserCamera.Remember(CamPos(), cam.RootSize);
                return;
            }
            if (!_init) Init();
            if (_followId != null)
            {
                var pawn = FindPawn(_followId);
                if (pawn == null || pawn.Dead || now > _followUntil) _followId = null;
                else
                {
                    var d = pawn.DrawPos;
                    _target = new Vector2(d.x, d.z);
                }
            }
            _pos = Vector2.SmoothDamp(_pos, _target, ref _vel, _smooth, 80f, Time.unscaledDeltaTime);
            _size = Mathf.SmoothDamp(_size, _targetSize, ref _velSize, _smooth * 1.3f, 30f, Time.unscaledDeltaTime);
            Find.CameraDriver.SetRootPosAndSize(_pos, Mathf.Clamp(_size, SizeMin, SizeMax));
            UserCamera.Remember(CamPos(), Find.CameraDriver.RootSize);
        }

        private static Vector3 CamPos()
        {
            var p = Find.Camera.transform.position;
            return new Vector3(p.x, 0f, p.z);
        }

        private static Pawn FindPawn(string id)
        {
            foreach (var p in Find.CurrentMap.mapPawns.AllPawnsSpawned)
                if (p.ThingID == id) return p;
            return null;
        }

        public static void GoTo(float x, float z, float? size, float? smooth, float hold, bool soft = false)
        {
            if (!Ready) throw new Exception("нет активной карты");
            if (soft && _hard && Time.realtimeSinceStartup < _holdUntil) return;
            _hard = !soft;
            if (!_init) Init();
            _followId = null;
            _target = new Vector2(x + 0.5f, z + 0.5f);
            if (size.HasValue) _targetSize = Mathf.Clamp(size.Value, SizeMin, SizeMax);
            _smooth = Mathf.Clamp(smooth ?? 0.7f, 0.15f, 4f);
            _holdUntil = Time.realtimeSinceStartup + hold;
            _what = $"точка {x},{z}";
        }

        public static void Follow(string pawnId, float? size, float? smooth, float seconds, bool soft = false)
        {
            if (!Ready) throw new Exception("нет активной карты");
            if (soft && _hard && Time.realtimeSinceStartup < _holdUntil) return;
            _hard = !soft;
            if (FindPawn(pawnId) == null) throw new Exception($"колонист не найден на карте: {pawnId}");
            if (!_init) Init();
            _followId = pawnId;
            _followUntil = Time.realtimeSinceStartup + seconds;
            _holdUntil = _followUntil;
            if (size.HasValue) _targetSize = Mathf.Clamp(size.Value, SizeMin, SizeMax);
            _smooth = Mathf.Clamp(smooth ?? 0.5f, 0.15f, 4f);
            _what = $"следит за {pawnId}";
        }

        public static void Release() { _followId = null; _holdUntil = 0; _hard = false; _what = ""; }

        /// Агент выполнил команду: в режиме "auto" камера переводится на место действия. Не мешает заданным кадрам.
        public static void OnCommand(string type, JObject cmd)
        {
            if (Mode != "auto" || !Ready || UserCamera.Overridden || type.StartsWith("camera") || type.StartsWith("get_") || type == "capabilities"
                || type == "look_at" || type == "find_nearest" || type == "search_thing_defs" || type == "list_recipes"
                || type == "find_buildable_spot" || type == "set_ai_cursor" || type == "clear_ai_cursor") return;
            if (_hard && Time.realtimeSinceStartup < _holdUntil - 1f) return;   // идёт важный кадр
            try
            {
                var pid = cmd["pawn_id"]?.Value<string>();
                if (pid != null && FindPawn(pid) != null)
                {
                    Follow(pid, 14f, 0.6f, 6f);
                    return;
                }
                var x = cmd["x"]?.Value<float>() ?? cmd["x1"]?.Value<float>();
                var z = cmd["z"]?.Value<float>() ?? cmd["z1"]?.Value<float>();
                if (x == null || z == null)
                {
                    var first = (cmd["cells"] as JArray ?? cmd["targets"] as JArray)?.First;
                    x = first?["x"]?.Value<float>();
                    z = first?["z"]?.Value<float>();
                }
                if (x != null && z != null) GoTo(x.Value, z.Value, 16f, 0.8f, 4f);
            }
            catch (Exception) { /* кадр не получился: игре и приказу это не мешает */ }
        }

        public static string StateJson() => JsonConvert.SerializeObject(new
        {
            mode = Mode, ready = Ready, hard = _hard, x = _pos.x, z = _pos.y, size = _size, following = _followId, what = _what
        });
    }
}

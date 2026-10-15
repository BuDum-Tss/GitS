using System;
using System.Collections.Generic;
using System.Linq;
using Newtonsoft.Json.Linq;
using RimWorld;
using UnityEngine;
using Verse;

namespace AgentBridge
{
    /// Камера и окна для стрима: зритель должен видеть, что делает агент. Явные команды (camera_jump, camera_follow,
    /// open_tab, select) и автокамера: при включении (set_auto_camera) сама переводит кадр к месту и пешке каждой
    /// команды агента и открывает нужную вкладку (работа, расписание, исследования), а через несколько секунд закрывает.
    public static partial class CommandExecutor
    {
        public static bool AutoCamera = true;     // игра как живой игрок: камера, курсор и окна; выключается set_auto_camera
        public static bool StrictUi = true;        // окно-зависимые команды работают только при открытом окне
        private const float TabIdleSeconds = 25f;  // окно, открытое явно, закрывается само после простоя
        private static float _lastAutoMove;
        private static string _followId;
        private static float _followUntil;
        private static MainButtonDef _openedTab;
        private static float _closeTabAt;

        private const float AutoMoveGap = 1.2f;       // не дёргать кадр чаще
        private const float TabOpenSeconds = 9f;      // вкладку, открытую автокамерой, закрываем через столько
        private const float FollowSeconds = 8f;       // слежение за пешкой после её приказа

        private static readonly Dictionary<string, string> TabAliases = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
        {
            ["work"] = "Work", ["restrict"] = "Restrict", ["schedule"] = "Restrict", ["assign"] = "Assign", ["medical"] = "Assign",
            ["animals"] = "Animals", ["wildlife"] = "Wildlife", ["history"] = "History", ["architect"] = "Architect",
            ["research"] = "Research", ["inspect"] = "Inspect", ["menu"] = "Menu", ["quests"] = "Quests", ["factions"] = "Factions",
        };

        // Команда, которая в игре делается в окне: без открытого окна не выполняется (как у живого игрока)
        private static readonly Dictionary<string, string> RequiredTab = new Dictionary<string, string>
        {
            ["set_work_priority"] = "Work", ["set_manual_priorities"] = "Work",
            ["set_schedule"] = "Restrict", ["set_schedule_range"] = "Restrict",
            ["set_medical_care"] = "Assign", ["research"] = "Research",
        };
        private static readonly Dictionary<string, string> TabTool = new Dictionary<string, string>
        {
            ["Work"] = "work", ["Restrict"] = "schedule", ["Assign"] = "assign", ["Research"] = "research",
        };

        /// Запрет: окно-зависимая команда без открытого окна. Подсказывает правильную последовательность.
        private static void CheckTabGate(string type)
        {
            if (!StrictUi || !InGame || !RequiredTab.TryGetValue(type, out var need)) return;
            var open = Find.MainTabsRoot.OpenTab;
            if (open != null && open.defName == need)
            {
                if (_openedTab == open) _closeTabAt = Time.realtimeSinceStartup + TabIdleSeconds;   // работает с окном: не закрывать
                return;
            }
            throw new Exception($"Окно «{need}» закрыто: это действие делается только в нём. Сначала open_game_tab(\"{TabTool[need]}\"), " +
                                $"затем это действие, в конце open_game_tab(\"close\").");
        }

        private static bool InGame => Current.ProgramState == ProgramState.Playing && Find.CurrentMap != null;

        // ── явные команды ──────────────────────────────────────────────────────

        private static string CameraJump(JObject cmd)
        {
            if (!InGame) throw new Exception("игра не запущена");
            var cell = new IntVec3(cmd["x"].Value<int>(), 0, cmd["z"].Value<int>());
            if (!cell.InBounds(Find.CurrentMap)) throw new Exception("клетка вне карты");
            FocusCell(cell, cmd["size"]?.Value<float>(), cmd["smooth"]?.Value<bool>() ?? true);
            return "{\"ok\":true}";
        }

        private static string CameraFollow(JObject cmd)
        {
            if (!InGame) throw new Exception("игра не запущена");
            var id = cmd["pawn_id"]?.Value<string>();
            if (string.IsNullOrEmpty(id)) { _followId = null; return "{\"following\":null}"; }
            var pawn = GetPawnById(id);
            _followId = id;
            var seconds = cmd["seconds"]?.Value<float>() ?? 40f;
            _followUntil = seconds <= 0f ? float.MaxValue : Time.realtimeSinceStartup + seconds;   // 0: следить, пока не отменят
            _followMoving = true;
            _zoomTarget = Mathf.Clamp(cmd["size"]?.Value<float>() ?? 18f, 8f, 40f);
            return "{\"following\":\"" + pawn.LabelShort + "\"}";
        }

        private static string OpenTab(JObject cmd)
        {
            if (!InGame) throw new Exception("игра не запущена");
            var name = cmd["tab"]?.Value<string>() ?? "close";
            if (name.Equals("close", StringComparison.OrdinalIgnoreCase))
            {
                Find.MainTabsRoot.EscapeCurrentTab(false);
                _openedTab = null;
                return "{\"closed\":true}";
            }
            ShowTab(name, true, TabIdleSeconds);
            return "{\"opened\":\"" + name + "\"}";
        }

        private static string SelectTarget(JObject cmd)
        {
            if (!InGame) throw new Exception("игра не запущена");
            var map = Find.CurrentMap;
            Thing thing = null;
            if (cmd["pawn_id"] != null) thing = GetPawnById(cmd["pawn_id"].Value<string>());
            else if (cmd["thing_id"] != null)
                thing = map.listerThings.AllThings.FirstOrDefault(t => t.ThingID == cmd["thing_id"].Value<string>());
            else if (cmd["x"] != null)
            {
                var c = new IntVec3(cmd["x"].Value<int>(), 0, cmd["z"].Value<int>());
                thing = c.GetThingList(map).FirstOrDefault(t => t is Building) ?? c.GetThingList(map).FirstOrDefault(t => t is Pawn);
            }
            if (thing == null) throw new Exception("нечего выбирать: объект не найден");
            Find.Selector.ClearSelection();
            Find.Selector.Select(thing, false, true);
            FocusCell(thing.Position, null, true);
            return "{\"selected\":\"" + thing.LabelShort + "\"}";
        }

        // ── вспомогательное ────────────────────────────────────────────────────

        private static float? _zoomTarget;                       // плавный зум: размер кадра, к которому едем
        private static Vector3? _camTarget;                      // куда плавно едет центр кадра
        private static Vector3 _camVel;
        private static float _sizeVel;
        private static float _camSmooth = 0.5f;                  // секунд на «догон»: больше расстояние, дольше
        private static bool _followMoving;                       // слежение: камера сейчас догоняет пешку (с гистерезисом)
        private static Vector2 _cursorTarget;
        private static bool _cursorGliding;
        private static string _pendingSelectId;                  // выделить пешку, когда курсор «доедет» (клик)
        private const float CursorMinSpeed = 420f;               // пикселей в секунду: быстрее взгляда, медленнее телепорта

        /// Где сейчас центр кадра (реальная позиция камеры, не округлённая до клеток: иначе слежение дрожит).
        private static Vector3 CamPos()
        {
            var p = Find.Camera.transform.position;
            return new Vector3(p.x, 0f, p.z);
        }

        private static void FocusCell(IntVec3 cell, float? size, bool smooth)
        {
            if (CameraDirector.Active) return;      // кадром владеет режиссёр записи (CameraDirector)
            if (UserCamera.Overridden) return;      // пользователь перехватил камеру: агент подчиняется (UserCamera)
            var driver = Find.CameraDriver;
            if (size.HasValue) _zoomTarget = Mathf.Clamp(size.Value, 8f, 60f);
            if (!smooth) { _camTarget = null; _camVel = Vector3.zero; driver.JumpToCurrentMapLoc(cell); return; }
            var target = cell.ToVector3Shifted(); target.y = 0f;
            _camTarget = target;
            // чем дальше, тем дольше едем (рука игрока), но без рывков: сглаживание с мягким стартом и торможением
            _camSmooth = Mathf.Clamp(0.45f + (target - CamPos()).magnitude / 110f, 0.5f, 1.3f);
        }

        private static void ShowTab(string name, bool auto, float seconds = TabOpenSeconds)
        {
            var defName = TabAliases.TryGetValue(name, out var mapped) ? mapped : name;
            var def = DefDatabase<MainButtonDef>.GetNamedSilentFail(defName)
                ?? throw new Exception($"нет вкладки «{name}». Есть: {string.Join(", ", TabAliases.Keys)}");
            Find.MainTabsRoot.SetCurrentTab(def, false);
            if (auto) { _openedTab = def; _closeTabAt = Time.realtimeSinceStartup + seconds; }
        }

        // ── курсор: рука игрока ────────────────────────────────────────────────

        private static Vector2 UiOfWorld(Vector3 world)
        {
            var sp = (Vector2)Find.Camera.WorldToScreenPoint(world);
            float k = (float)UI.screenWidth / Screen.width;
            return new Vector2(sp.x * k, (Screen.height - sp.y) * k);
        }

        /// Примерная позиция кнопки вкладки внизу экрана: текстовые кнопки делят левые ~78% ширины поровну.
        private static Vector2? TabButtonPos(MainButtonDef def)
        {
            var buttons = DefDatabase<MainButtonDef>.AllDefsListForReading.Where(d => d.Worker.Visible && string.IsNullOrEmpty(d.iconPath))
                .OrderBy(d => d.order).ToList();
            int i = buttons.IndexOf(def);
            if (i < 0) return null;
            return new Vector2((i + 0.5f) * UI.screenWidth * 0.78f / buttons.Count, UI.screenHeight - 17f);
        }

        /// Курсор плывёт к точке экрана; возвращает, сколько секунд это займёт.
        private static float CursorGlide(Vector2 target)
        {
            if (!AgentBridgeMod.AiCursorActive)
            {
                var mouse = UI.MousePositionOnUIInverted;
                AgentBridgeMod.AiCursorX = mouse.x; AgentBridgeMod.AiCursorY = mouse.y;       // рука начинает там, где мышь
            }
            AgentBridgeMod.AiCursorActive = true;
            AgentBridgeMod.AiCursorLastUpdate = Time.realtimeSinceStartup;
            _cursorTarget = target; _cursorGliding = true;
            var from = new Vector2(AgentBridgeMod.AiCursorX, AgentBridgeMod.AiCursorY);
            return 0.2f + Vector2.Distance(from, target) / 1200f;
        }

        private static float PanTime(IntVec3 cell)
        {
            var center = Find.CameraDriver.CurrentViewRect.CenterCell;
            return Mathf.Clamp(0.7f + (cell - center).LengthHorizontal / 45f, 0.8f, 3f);
        }

        // ── автокамера: действие как у живого игрока ───────────────────────────

        /// Перед действием: камера едет к месту (плавно, со скоростью, как у руки игрока), курсор плывёт к цели, пешка
        /// выделяется кликом. Возвращает, сколько секунд подождать до самого действия.
        /// compact: очередь копится (пачка команд). Показ не пропускаем, а сокращаем: ожидание не больше CompactWait,
        /// камера всё равно едет к месту команды плавно (CameraTick), наверстывания без показа нет.
        public const float CompactWait = 0.25f;

        public static float AutoCameraFor(string type, JObject cmd, bool compact = false)
        {
            float wait = AutoCameraWait(type, cmd);
            return compact ? Mathf.Min(wait, CompactWait) : wait;
        }

        private static float AutoCameraWait(string type, JObject cmd)
        {
            if (!AutoCamera || !InGame) return 0f;
            var now = Time.realtimeSinceStartup;
            var map = Find.CurrentMap;

            if (RequiredTab.ContainsKey(type)) return AimAtTable(type, cmd);   // правка в открытом окне: курсор едет к ячейке (TableCursor.cs)

            if (type == "open_tab")
            {
                var name = cmd["tab"]?.Value<string>() ?? "close";
                if (name.Equals("close", StringComparison.OrdinalIgnoreCase)) return 0f;
                var def = DefDatabase<MainButtonDef>.GetNamedSilentFail(TabAliases.TryGetValue(name, out var m) ? m : name);
                var pos = def != null ? TabButtonPos(def) : null;
                return pos.HasValue ? CursorGlide(pos.Value) + 0.15f : 0.3f;
            }

            var pawnId = cmd["pawn_id"]?.Value<string>();
            if (!string.IsNullOrEmpty(pawnId))
            {
                var pawn = map.mapPawns.AllPawns.FirstOrDefault(p => p.ThingID == pawnId);
                if (pawn == null || !pawn.Spawned) return 0f;
                _pendingSelectId = pawnId;
                _followId = pawnId;
                _followUntil = now + FollowSeconds;
                if (!CameraDirector.Active && !Find.CameraDriver.CurrentViewRect.ContractedBy(3).Contains(pawn.Position))
                {
                    var wait = PanTime(pawn.Position);
                    FocusCell(pawn.Position, 20f, true);
                    return Mathf.Max(wait, CursorGlide(new Vector2(UI.screenWidth / 2f, UI.screenHeight / 2f))) + 0.15f;
                }
                return CursorGlide(UiOfWorld(pawn.DrawPos)) + 0.15f;
            }

            var target = CellOfCommand(cmd);
            if (target.HasValue && target.Value.InBounds(map))
            {
                if (!CameraDirector.Active && !Find.CameraDriver.CurrentViewRect.ContractedBy(4).Contains(target.Value))
                {
                    var wait = PanTime(target.Value);
                    FocusCell(target.Value, 16f, true);
                    return Mathf.Max(wait, CursorGlide(new Vector2(UI.screenWidth / 2f, UI.screenHeight / 2f)));
                }
                return CursorGlide(UiOfWorld(target.Value.ToVector3Shifted())) + 0.1f;
            }
            return 0f;
        }

        /// Курсор «доехал»: клик (выделение пешки, вспышка кольца). Вызывается прямо перед выполнением команды.
        private static void ApplyStageClick(string type)
        {
            if (!InGame) return;
            // выделение пешки открывает панель «Осмотр» и закрывает открытое окно (работа, режим дня): для правок в окне не выделяем
            if (type == "open_tab" || RequiredTab.ContainsKey(type ?? "")) _pendingSelectId = null;
            if (_pendingSelectId != null)
            {
                var pawn = Find.CurrentMap.mapPawns.AllPawns.FirstOrDefault(p => p.ThingID == _pendingSelectId);
                if (pawn != null && pawn.Spawned) { Find.Selector.ClearSelection(); Find.Selector.Select(pawn, false, true); }
                _pendingSelectId = null;
            }
            AgentBridgeMod.AiCursorClickUntil = Time.realtimeSinceStartup + 0.3f;
            AgentBridgeMod.AiCursorLastUpdate = Time.realtimeSinceStartup;
        }

        // ── зум, сдвиг, где сейчас камера ──────────────────────────────────────

        private static string CameraZoom(JObject cmd)
        {
            if (!InGame) throw new Exception("игра не запущена");
            _zoomTarget = Mathf.Clamp(cmd["size"].Value<float>(), 8f, 60f);
            return CameraView();
        }

        private static string CameraMove(JObject cmd)
        {
            if (!InGame) throw new Exception("игра не запущена");
            var map = Find.CurrentMap;
            var c0 = Find.CameraDriver.CurrentViewRect.CenterCell;
            var cell = new IntVec3(c0.x + cmd["dx"].Value<int>(), 0, c0.z + cmd["dz"].Value<int>());
            cell.x = Mathf.Clamp(cell.x, 0, map.Size.x - 1);
            cell.z = Mathf.Clamp(cell.z, 0, map.Size.z - 1);
            FocusCell(cell, null, true);
            return "{\"to\":{\"x\":" + cell.x + ",\"z\":" + cell.z + "}}";
        }

        /// Где камера: центр, видимый прямоугольник, масштаб. Чтобы она знала, что сейчас видят зрители.
        private static string CameraView()
        {
            if (!InGame) throw new Exception("игра не запущена");
            var d = Find.CameraDriver;
            var r = d.CurrentViewRect;
            var c = r.CenterCell;
            return Newtonsoft.Json.JsonConvert.SerializeObject(new
            {
                center = new { x = c.x, z = c.z },
                view = new { x1 = r.minX, z1 = r.minZ, x2 = r.maxX, z2 = r.maxZ },
                size = d.RootSize,
                open_tab = Find.MainTabsRoot.OpenTab?.defName,
            });
        }

        private static IntVec3? CellOfCommand(JObject cmd)
        {
            if (cmd["x"] != null && cmd["z"] != null) return new IntVec3(cmd["x"].Value<int>(), 0, cmd["z"].Value<int>());
            if (cmd["x1"] != null && cmd["z1"] != null)
                return new IntVec3((cmd["x1"].Value<int>() + (cmd["x2"]?.Value<int>() ?? cmd["x1"].Value<int>())) / 2, 0,
                                   (cmd["z1"].Value<int>() + (cmd["z2"]?.Value<int>() ?? cmd["z1"].Value<int>())) / 2);
            if (cmd["inside"] != null) return new IntVec3(cmd["inside"]["x"].Value<int>(), 0, cmd["inside"]["z"].Value<int>());
            // предмет или верстак: камера едет к нему (forbid, add_bill, cancel_bill и т.п. адресуются id)
            var thingId = cmd["thing_id"]?.Value<string>() ?? cmd["workbench_id"]?.Value<string>();
            if (thingId != null && Find.CurrentMap != null)
            {
                var thing = Find.CurrentMap.listerThings.AllThings.FirstOrDefault(t => t.ThingID == thingId);
                if (thing != null) return thing.Position;
            }
            var first = (cmd["cells"] as JArray)?.FirstOrDefault() ?? (cmd["walls"] as JArray)?.FirstOrDefault();
            if (first != null && first["x"] != null) return new IntVec3(first["x"].Value<int>(), 0, first["z"].Value<int>());
            return null;
        }

        /// Каждый кадр (из AgentBridgeTicker): плавный зум, курсор, мягкое слежение за пешкой, закрытие окна после простоя.
        public static void CameraTick()
        {
            if (!InGame) return;
            var now = Time.realtimeSinceStartup;
            var dt = Mathf.Min(Time.unscaledDeltaTime, 0.1f);
            // пользователь сам сдвинул или приблизил кадр (WASD, края, колесо): цели агента сбрасываются, агент подчиняется 5 с
            if (!CameraDirector.Active && UserCamera.Check(CamPos(), Find.CameraDriver.RootSize))
            { _camTarget = null; _camVel = Vector3.zero; _zoomTarget = null; _followId = null; _followMoving = false; }

            if (_cursorGliding)
            {
                var pos = new Vector2(AgentBridgeMod.AiCursorX, AgentBridgeMod.AiCursorY);
                var dist = Vector2.Distance(pos, _cursorTarget);
                var step = Mathf.Max(dist * 7f * dt, CursorMinSpeed * dt);          // ускорение к цели и плавное торможение
                pos = step >= dist ? _cursorTarget : Vector2.MoveTowards(pos, _cursorTarget, step);
                AgentBridgeMod.AiCursorX = pos.x; AgentBridgeMod.AiCursorY = pos.y;
                AgentBridgeMod.AiCursorLastUpdate = now;
                if (pos == _cursorTarget) _cursorGliding = false;
            }

            var drv = Find.CameraDriver;
            if (CameraDirector.Active)       // кадром владеет режиссёр записи: свои цели камеры стрима сбрасываем, чтобы не рвануть после него
            {
                _followId = null; _followMoving = false; _camTarget = null; _camVel = Vector3.zero; _zoomTarget = null; _sizeVel = 0f;
            }
            if (_followId != null)
            {
                if (now > _followUntil) { _followId = null; _followMoving = false; }
                else
                {
                    var pawn = Find.CurrentMap.mapPawns.AllPawns.FirstOrDefault(p => p.ThingID == _followId);
                    if (pawn != null && pawn.Spawned)
                    {
                        // мёртвая зона: пока пешка в центре кадра, камера стоит; вышла за 28% высоты кадра, плавно догоняем до 8%
                        var to = pawn.DrawPos; to.y = 0f;
                        var gap = (to - CamPos()).magnitude;
                        if (gap > drv.RootSize * 0.28f) _followMoving = true;
                        else if (gap < drv.RootSize * 0.08f) _followMoving = false;
                        if (_followMoving) { _camTarget = to; _camSmooth = 0.55f; }
                    }
                    else { _followId = null; _followMoving = false; }
                }
            }

            if (UserCamera.Overridden) { _camTarget = null; _followMoving = false; _zoomTarget = null; }   // пока пользователь перехватил камеру, агент её не двигает
            if (_camTarget.HasValue)
            {
                var cur = CamPos();
                var next = Vector3.SmoothDamp(cur, _camTarget.Value, ref _camVel, _camSmooth, 80f, dt);
                drv.JumpToCurrentMapLoc(next);
                if ((next - _camTarget.Value).magnitude < 0.05f && _camVel.magnitude < 0.05f && !(_followId != null && _followMoving))
                    { _camTarget = null; _camVel = Vector3.zero; }
            }

            if (_zoomTarget.HasValue)
            {
                var size = Mathf.SmoothDamp(drv.RootSize, _zoomTarget.Value, ref _sizeVel, 0.7f, 30f, dt);
                drv.SetRootSize(size);
                if (Mathf.Abs(size - _zoomTarget.Value) < 0.03f) { _zoomTarget = null; _sizeVel = 0f; }
            }

            if (_openedTab != null && now > _closeTabAt)
            {
                if (Find.MainTabsRoot.OpenTab == _openedTab) Find.MainTabsRoot.EscapeCurrentTab(false);
                _openedTab = null;
            }

            if (!CameraDirector.Active) UserCamera.Remember(CamPos(), drv.RootSize);   // где камера сейчас: отметка для проверки следующего кадра
        }
    }
}

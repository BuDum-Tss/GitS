using System.Linq;
using RimWorld;
using UnityEngine;
using Verse;

namespace AgentBridge
{
    /// Ввод пользователя в камеру (WASD, края экрана, колесо). Мод помнит, где камеру оставил он сам (после своей записи
    /// в кадре); если между кадрами позиция или размер ушли без нас, это явное движение пользователя. Тогда агент
    /// подчиняется: его движения камеры не применяются OverrideSeconds (настройка user_camera_override). Пока агент действует,
    /// мелкий дрейф не считается движением: цель камеры перезаписывается каждый кадр.
    public static class UserCamera
    {
        public const float DefaultOverrideSeconds = 5f;
        public const float MoveEpsilon = 0.6f;     // клеток: меньше этого не считаем движением
        public const float SizeEpsilon = 0.4f;     // размер кадра: меньше этого не считаем зумом

        public static bool Enabled = true;                       // user_camera_override: пользователь может перехватить камеру
        public static float OverrideSeconds = DefaultOverrideSeconds;
        private static bool _have;
        private static Vector3 _lastPos;
        private static float _lastSize;
        private static float _until;                             // realtime: до этого агент камеру не двигает

        /// Главный переключатель (кнопка у края экрана в игре, команда set_agent_camera): true - камерой управляет агент,
        /// false - камера у игрока, агент её не двигает вообще. Включён при каждом запуске игры.
        public static bool AgentHasCamera = true;

        /// Агент не двигает камеру: игрок забрал её переключателем или недавно сам её сдвинул.
        public static bool Overridden => !AgentHasCamera || (Enabled && Time.realtimeSinceStartup < _until);

        /// Пользователь прямо сейчас управляет камерой: колесо мыши, клавиши перемещения и масштаба или курсор у края экрана
        /// (игра сама двигает камеру). Сдвиг за кадр при медленной прокрутке и шаге колеса меньше порогов `MoveEpsilon` и
        /// `SizeEpsilon`, поэтому по одному сдвигу его не заметить, и агент каждый кадр тянул камеру обратно (колесо «не работало»,
        /// движение шло рывками).
        // Колесо мыши читаем рефлексией: загрузчик модов RimWorld не находит сборку UnityEngine.InputLegacyModule при прямой
        // ссылке (FileNotFoundException при первом вызове), а в процессе игры она загружена.
        private static System.Reflection.PropertyInfo _scrollProp;
        private static bool _scrollTried;

        private static float ScrollDelta()
        {
            if (!_scrollTried)
            {
                _scrollTried = true;
                try
                {
                    var asm = System.AppDomain.CurrentDomain.GetAssemblies().FirstOrDefault(a => a.GetName().Name == "UnityEngine.InputLegacyModule");
                    _scrollProp = asm?.GetType("UnityEngine.Input")?.GetProperty("mouseScrollDelta");
                }
                catch (System.Exception) { }
            }
            try { return _scrollProp == null ? 0f : ((Vector2)_scrollProp.GetValue(null, null)).y; }
            catch (System.Exception) { return 0f; }
        }

        public static bool UserInput()
        {
            try
            {
                if (Mathf.Abs(ScrollDelta()) > 0.01f) return true;
                if (KeyBindingDefOf.MapDolly_Left.IsDown || KeyBindingDefOf.MapDolly_Right.IsDown ||
                    KeyBindingDefOf.MapDolly_Up.IsDown || KeyBindingDefOf.MapDolly_Down.IsDown ||
                    KeyBindingDefOf.MapZoom_In.IsDown || KeyBindingDefOf.MapZoom_Out.IsDown) return true;
                if (Prefs.EdgeScreenScroll && Application.isFocused)
                {
                    var m = UI.MousePositionOnUIInverted;          // y сверху, как в экране игры
                    const float edge = 2f;
                    if (m.x <= edge || m.y <= edge || m.x >= UI.screenWidth - edge || m.y >= UI.screenHeight - edge) return true;
                }
            }
            catch (System.Exception) { }
            return false;
        }

        /// Начало кадра: true, если камеру явно сдвинул пользователь. Тогда отметка перехвата ставится на OverrideSeconds.
        public static bool Check(Vector3 pos, float size)
        {
            if (!Enabled) return false;
            if (UserInput()) { _until = Time.realtimeSinceStartup + OverrideSeconds; return true; }
            if (!_have) return false;
            bool moved = (pos - _lastPos).magnitude > MoveEpsilon || Mathf.Abs(size - _lastSize) > SizeEpsilon;
            if (moved) _until = Time.realtimeSinceStartup + OverrideSeconds;
            return moved;
        }

        /// Конец записи кадра: запомнить, что камера сейчас там, где мы её оставили.
        public static void Remember(Vector3 pos, float size)
        {
            _lastPos = pos; _lastSize = size; _have = true;
        }

        public static void Reset()
        {
            _have = false; _until = 0f;
        }
    }
}

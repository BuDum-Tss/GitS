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

        /// Агент не двигает камеру, пока пользователь её перехватил.
        public static bool Overridden => Enabled && Time.realtimeSinceStartup < _until;

        /// Начало кадра: true, если камеру явно сдвинул пользователь. Тогда отметка перехвата ставится на OverrideSeconds.
        public static bool Check(Vector3 pos, float size)
        {
            if (!Enabled || !_have) return false;
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

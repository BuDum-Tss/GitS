using UnityEngine;

namespace AgentBridge
{
    // Живёт вне Game — работает всегда: в главном меню, во время загрузки,
    // на паузе. Только так StateCache гарантированно выходит из заглушки
    // "initializing" даже когда игра ещё не запущена (GameComponent для
    // этого недоступен — он существует только внутри активной Game).
    public class AgentBridgeTicker : MonoBehaviour
    {
        private int _frameCounter = 0;
        private int _guardCounter = 0;

        public void Update()
        {
            CameraDirector.Tick();
            _frameCounter++;
            if (++_guardCounter >= 30)
            {
                _guardCounter = 0;
                try { LogGuard.Tick(); } catch (System.Exception) { }
            }
            try { CommandExecutor.CameraTick(); } catch (System.Exception) { }
            if (_frameCounter >= 60)
            {
                _frameCounter = 0;
                AgentBridgeMod.StateCache.Refresh();
            }
        }
    }
}

using System.Linq;
using Verse;

namespace AgentBridge
{
    // GameComponentUpdate вызывается каждый кадр Unity — в том числе на паузе.
    // Это позволяет обрабатывать команды (pause/resume) даже когда тики стоят.
    //
    // Обновление StateCache сюда НЕ входит — оно вынесено в AgentBridgeTicker
    // (глобальный MonoBehaviour, живёт вне Game), чтобы кэш обновлялся всегда:
    // и на паузе, и в главном меню, а не только когда есть активная игра с
    // идущими тиками. Раньше на паузе/в меню StateCache застревал на заглушке
    // "initializing" навсегда — GLaDOS "молчала", потому что статус никогда
    // не становился валидным.
    public class AgentGameComponent : GameComponent
    {
        private bool _colonyWasAlive = false;
        private int _wipeCheckCounter = 0;

        public AgentGameComponent(Game game) : base() { }

        public override void GameComponentUpdate()
        {
            CommandExecutor.ProcessQueue();

            // Проверяем гибель колонии не каждый кадр — раз в ~30 кадров достаточно
            _wipeCheckCounter++;
            if (_wipeCheckCounter >= 30)
            {
                _wipeCheckCounter = 0;
                CheckColonyWipe();
            }
        }

        private void CheckColonyWipe()
        {
            var map = Find.CurrentMap;
            if (map == null) return;

            bool alive = map.mapPawns.FreeColonists.Any(p => !p.Dead);
            if (_colonyWasAlive && !alive)
            {
                AgentBridgeMod.Events.Push("colony_died", "critical", new
                {
                    colony = Find.FactionManager?.OfPlayer?.Name ?? "Unknown",
                    tick   = Find.TickManager?.TicksGame ?? 0,
                });
                Log.Message("[AgentBridge] Колония погибла — событие colony_died отправлено.");
            }
            _colonyWasAlive = alive;
        }
    }
}

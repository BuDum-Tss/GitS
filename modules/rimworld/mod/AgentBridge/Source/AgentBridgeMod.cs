using HarmonyLib;
using UnityEngine;
using Verse;

namespace AgentBridge
{
    public class AgentBridgeMod : Mod
    {
        public static EventQueue Events   { get; private set; }
        public static CommandQueue Commands { get; private set; }
        public static StateCache StateCache { get; private set; }

        // Визуальный курсор GLaDOS поверх игры (аналогично курсорам других
        // игроков в Multiplayer-моде) — чисто косметика, координаты экрана
        // окна игры, обновляется через команду set_ai_cursor.
        public static bool  AiCursorActive;
        public static float AiCursorX;
        public static float AiCursorY;
        public static float AiCursorLastUpdate;
        public static float AiCursorClickUntil;     // до этого момента вокруг курсора кольцо клика

        private static HttpServer _server;

        public AgentBridgeMod(ModContentPack content) : base(content)
        {
            Events    = new EventQueue();
            Commands  = new CommandQueue();
            StateCache = new StateCache();

            new Harmony("com.glados.agentbridge").PatchAll();

            _server = new HttpServer(8765);
            _server.Start();

            // Глобальный тикер вне Game — чтобы StateCache обновлялся даже
            // в главном меню, а не только внутри активной игры.
            var tickerObj = new GameObject("AgentBridgeTicker");
            Object.DontDestroyOnLoad(tickerObj);
            tickerObj.AddComponent<AgentBridgeTicker>();

            Log.Message("[AgentBridge] GLaDOS has assumed control of the facility.");
        }
    }
}

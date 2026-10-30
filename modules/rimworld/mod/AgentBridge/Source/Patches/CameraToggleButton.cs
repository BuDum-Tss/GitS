using HarmonyLib;
using RimWorld;
using UnityEngine;
using Verse;

namespace AgentBridge
{
    /// Кнопка-переключатель камеры у правого края экрана: «CAM: AGENT» (камерой управляет агент) или «CAM: ME» (камера у
    /// игрока, агент её не двигает). Клик переключает `UserCamera.AgentHasCamera`; то же делает команда set_agent_camera.
    /// Надпись латиницей: так она читается при любом языке игры. Рисуется только в игре (не в главном меню).
    [HarmonyPatch(typeof(UIRoot), nameof(UIRoot.UIRootOnGUI))]
    public static class CameraToggleButtonPatch
    {
        private const float W = 108f, H = 34f;
        private static readonly Color AgentColor = new Color(0.18f, 0.62f, 0.27f, 0.92f);
        private static readonly Color PlayerColor = new Color(0.29f, 0.31f, 0.35f, 0.92f);

        public static void Postfix()
        {
            if (Current.ProgramState != ProgramState.Playing || Find.CurrentMap == null) return;
            var r = new Rect(UI.screenWidth - W, UI.screenHeight * 0.5f - H * 0.5f, W, H);
            bool agent = UserCamera.AgentHasCamera;
            var prevColor = GUI.color;
            var prevFont = Text.Font;
            var prevAnchor = Text.Anchor;
            GUI.color = agent ? AgentColor : PlayerColor;
            GUI.DrawTexture(r, BaseContent.WhiteTex);
            GUI.color = Color.white;
            Text.Font = GameFont.Small;
            Text.Anchor = TextAnchor.MiddleCenter;
            Widgets.Label(r, agent ? "CAM: AGENT" : "CAM: ME");
            Text.Anchor = prevAnchor;
            Text.Font = prevFont;
            GUI.color = prevColor;
            TooltipHandler.TipRegion(r, agent ? "Camera is controlled by the agent. Click to take it." :
                                                 "Camera is yours, the agent does not move it. Click to give it to the agent.");
            if (Widgets.ButtonInvisible(r))
                UserCamera.AgentHasCamera = !agent;
        }
    }
}

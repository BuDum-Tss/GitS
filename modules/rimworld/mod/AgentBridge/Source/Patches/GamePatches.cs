using System.Linq;
using HarmonyLib;
using RimWorld;
using UnityEngine;
using Verse;
using Verse.AI;

namespace AgentBridge
{
    // Регистрируем GameComponent при инициализации игры.
    // GameComponentUpdate() работает каждый кадр (даже на паузе) — там команды.
    // GameComponentTick() работает только при тиках — там Refresh кэша.
    [HarmonyPatch(typeof(Game), nameof(Game.FinalizeInit))]
    public static class GameFinalizeInitPatch
    {
        public static void Postfix(Game __instance)
        {
            // Добавляем компонент если ещё не добавлен
            if (!__instance.components.OfType<AgentGameComponent>().Any())
                __instance.components.Add(new AgentGameComponent(__instance));

            // автокамера включена после каждой загрузки сохранения: агент не может оставить её выключенной навсегда (ТЗ 20)
            CommandExecutor.AutoCamera = true;
            UserCamera.Reset();

            AgentBridgeMod.StateCache.Refresh();
            var colonyName = Find.CurrentMap?.info?.parent?.Label ?? "Unknown";
            AgentBridgeMod.Events.Push("game_loaded", "high", new
            {
                colony = colonyName,
                tick   = Find.TickManager?.TicksGame ?? 0,
            });
            Log.Message($"[AgentBridge] Game loaded: {colonyName}");
        }
    }

    // Capture all letters as events (covers raids, quests, traders, etc.)
    // In RimWorld 1.6 LetterStack is in Verse; overload is (Letter, string, int, bool)
    [HarmonyPatch(typeof(LetterStack), nameof(LetterStack.ReceiveLetter),
        new[] { typeof(Letter), typeof(string), typeof(int), typeof(bool) })]
    public static class LetterPatch
    {
        public static void Postfix(Letter let)
        {
            if (let?.def == null) return;

            var priority = let.def.defName switch
            {
                "ThreatBig"     => "critical",
                "ThreatSmall"   => "high",
                "Death"         => "high",
                "NegativeEvent" => "high",
                _               => "medium"
            };

            AgentBridgeMod.Events.Push("letter", priority, new
            {
                label      = let.Label,
                letter_def = let.def.defName,
                text       = (let is ChoiceLetter cl) ? cl.Text.RawText : ""
            });
        }
    }

    // Pawn downed: our colonist needs rescue, an enemy can be captured or finished
    [HarmonyPatch(typeof(Pawn_HealthTracker), "MakeDowned")]
    public static class PawnDownedPatch
    {
        public static void Postfix(Pawn ___pawn)
        {
            if (___pawn == null || !___pawn.Spawned) return;
            if (___pawn.IsColonist)
                AgentBridgeMod.Events.Push("pawn_downed", "high", new
                {
                    pawn_id = ___pawn.ThingID,
                    name    = ___pawn.Name?.ToStringShort ?? "Unknown"
                });
            else if (___pawn.HostileTo(Faction.OfPlayer) && ___pawn.RaceProps.Humanlike)
                AgentBridgeMod.Events.Push("enemy_downed", "medium", new
                {
                    pawn_id = ___pawn.ThingID,
                    name    = ___pawn.Name?.ToStringShort ?? "Enemy"
                });
        }
    }

    // Start of a raid or another threat: the type and its strength
    [HarmonyPatch(typeof(IncidentWorker), nameof(IncidentWorker.TryExecute))]
    public static class IncidentPatch
    {
        public static void Postfix(IncidentWorker __instance, IncidentParms parms, bool __result)
        {
            if (!__result || __instance?.def == null) return;
            var cat = __instance.def.category;
            if (cat != IncidentCategoryDefOf.ThreatBig && cat != IncidentCategoryDefOf.ThreatSmall) return;
            AgentBridgeMod.Events.Push("incident", cat == IncidentCategoryDefOf.ThreatBig ? "critical" : "high", new
            {
                incident = __instance.def.defName,
                label    = __instance.def.label,
                points   = parms?.points ?? 0f
            });
        }
    }

    // Colonist death
    [HarmonyPatch(typeof(Pawn), nameof(Pawn.Kill))]
    public static class PawnDeathPatch
    {
        public static void Postfix(Pawn __instance, DamageInfo? dinfo)
        {
            if (__instance == null) return;
            if (!(__instance.IsColonist || __instance.IsPrisoner))
            {
                if (__instance.RaceProps.Humanlike && __instance.HostileTo(Faction.OfPlayer))
                    AgentBridgeMod.Events.Push("enemy_killed", "medium", new
                    {
                        name = __instance.Name?.ToStringShort ?? "Enemy",
                        faction = __instance.Faction?.Name ?? "Unknown"
                    });
                return;
            }

            AgentBridgeMod.Events.Push("pawn_died", "critical", new
            {
                pawn_id = __instance.ThingID,
                name    = __instance.Name?.ToStringShort ?? "Unknown",
                cause   = dinfo?.Def?.defName ?? "unknown",
                age     = __instance.ageTracker?.AgeBiologicalYears ?? 0
            });
        }
    }

    // Fire started — only if near colony
    [HarmonyPatch(typeof(Fire), nameof(Fire.SpawnSetup))]
    public static class FirePatch
    {
        public static void Postfix(Fire __instance, Map map, bool respawningAfterLoad)
        {
            if (respawningAfterLoad || map == null) return;
            if (map != Find.CurrentMap) return;

            // Only report if fire is inside home area
            if (!map.areaManager.Home[__instance.Position]) return;

            AgentBridgeMod.Events.Push("fire_started", "critical", new
            {
                x = __instance.Position.x,
                z = __instance.Position.z
            });
        }
    }

    // Рисуем цветной курсор GLaDOS поверх игры — аналогично тому, как
    // Multiplayer-мод показывает курсоры других игроков. Чистая косметика,
    // не влияет на реальный ввод. Координаты — экран ОКНА игры (0,0 —
    // левый верхний угол клиентской области), совпадают с тем, что
    // использует game_ui.py на стороне Python.
    [HarmonyPatch(typeof(UIRoot), nameof(UIRoot.UIRootOnGUI))]
    public static class DrawAiCursorPatch
    {
        private static Texture2D _arrowTex, _ringTex;
        private const int ArrowW = 24, ArrowH = 32;

        // Стрелка как у обычного курсора: заливка цвета ГЛаДОС, тёмная обводка, кончик в точке (x, y).
        // Рисуется один раз по многоугольнику с лёгким сглаживанием краёв (суперсэмплинг 4x4).
        private static Texture2D MakeArrow()
        {
            var poly = new[]
            {
                new Vector2(1f, 1f), new Vector2(1f, 24f), new Vector2(6.5f, 19f), new Vector2(10.5f, 28.5f),
                new Vector2(14.5f, 26.8f), new Vector2(10.6f, 17.6f), new Vector2(18f, 17.6f),
            };
            bool Inside(float px, float py)
            {
                bool c = false;
                for (int i = 0, j = poly.Length - 1; i < poly.Length; j = i++)
                    if ((poly[i].y > py) != (poly[j].y > py) &&
                        px < (poly[j].x - poly[i].x) * (py - poly[i].y) / (poly[j].y - poly[i].y) + poly[i].x) c = !c;
                return c;
            }
            float Cover(float cx, float cy, float grow)
            {
                int hit = 0;
                for (int sy = 0; sy < 4; sy++)
                    for (int sx = 0; sx < 4; sx++)
                    {
                        float px = cx + (sx + 0.5f) / 4f - 0.5f, py = cy + (sy + 0.5f) / 4f - 0.5f;
                        bool inside = Inside(px, py);
                        if (!inside && grow > 0f)        // обводка: клетка рядом с контуром
                            for (int k = 0; k < 8 && !inside; k++)
                            {
                                float ang = k * Mathf.PI / 4f;
                                inside = Inside(px + Mathf.Cos(ang) * grow, py + Mathf.Sin(ang) * grow);
                            }
                        if (inside) hit++;
                    }
                return hit / 16f;
            }
            var tex = new Texture2D(ArrowW, ArrowH, TextureFormat.RGBA32, false) { filterMode = FilterMode.Bilinear };
            var fill = new Color(1f, 0.25f, 0.8f, 1f);
            for (int y = 0; y < ArrowH; y++)
                for (int x = 0; x < ArrowW; x++)
                {
                    // texture y растёт вверх, экранный y вниз: переворачиваем
                    float cy = ArrowH - 1 - y, cx = x;
                    float body = Cover(cx, cy, 0f), edge = Cover(cx, cy, 1.4f);
                    var c = Color.Lerp(new Color(0f, 0f, 0f, 0f), new Color(0f, 0f, 0f, 0.9f), edge);   // обводка
                    c = Color.Lerp(c, fill, body);
                    c.a = Mathf.Max(edge * 0.9f, body);
                    tex.SetPixel(x, y, c);
                }
            tex.Apply();
            return tex;
        }

        private static Texture2D MakeRing()
        {
            const int n = 40;
            var tex = new Texture2D(n, n, TextureFormat.RGBA32, false) { filterMode = FilterMode.Bilinear };
            for (int y = 0; y < n; y++)
                for (int x = 0; x < n; x++)
                {
                    float d = Vector2.Distance(new Vector2(x + 0.5f, y + 0.5f), new Vector2(n / 2f, n / 2f));
                    float a = Mathf.Clamp01(1f - Mathf.Abs(d - 16f) / 2.2f);
                    tex.SetPixel(x, y, new Color(1f, 0.25f, 0.8f, a * 0.9f));
                }
            tex.Apply();
            return tex;
        }

        public static void Postfix()
        {
            if (!AgentBridgeMod.AiCursorActive) return;
            if (Time.realtimeSinceStartup - AgentBridgeMod.AiCursorLastUpdate > 45f)
            {
                AgentBridgeMod.AiCursorActive = false;  // авто-скрытие после 45с бездействия (на стриме курсор должен быть виден)
                return;
            }
            if (_arrowTex == null) { _arrowTex = MakeArrow(); _ringTex = MakeRing(); }

            float x = AgentBridgeMod.AiCursorX;
            float y = AgentBridgeMod.AiCursorY;

            if (Time.realtimeSinceStartup < AgentBridgeMod.AiCursorClickUntil)       // вспышка клика: кольцо вокруг кончика
                GUI.DrawTexture(new Rect(x - 20f, y - 20f, 40f, 40f), _ringTex);
            GUI.DrawTexture(new Rect(x - 1f, y - 1f, ArrowW, ArrowH), _arrowTex);    // кончик стрелки ровно в (x, y)

            var prevFont  = Text.Font;
            var prevColor = GUI.color;
            Text.Font = GameFont.Tiny;
            GUI.color = new Color(1f, 0.25f, 0.8f);
            Widgets.Label(new Rect(x + 16f, y + 14f, 90f, 20f), "GLaDOS");
            GUI.color = prevColor;
            Text.Font = prevFont;
        }
    }
}

using System.Collections.Generic;
using Newtonsoft.Json;

namespace AgentBridge
{
    public class GameStateDto
    {
        [JsonProperty("tick")]          public int Tick { get; set; }
        [JsonProperty("year")]          public int Year { get; set; }
        [JsonProperty("season")]        public string Season { get; set; }
        [JsonProperty("weather")]       public string Weather { get; set; }
        [JsonProperty("threat_points")] public float ThreatPoints { get; set; }
        [JsonProperty("time_speed")]    public int TimeSpeed { get; set; }
        [JsonProperty("manual_priorities")] public bool ManualPriorities { get; set; }
        [JsonProperty("paused")]        public bool Paused { get; set; }
        [JsonProperty("save_id")]       public string SaveId { get; set; }
        [JsonProperty("colony")]        public ColonyDto Colony { get; set; }
        [JsonProperty("minimap")]       public string Minimap { get; set; }
        [JsonProperty("threats")]       public List<ThreatDto> Threats { get; set; }
        [JsonProperty("pending_events")]public List<GameEventDto> PendingEvents { get; set; }
        [JsonProperty("game_log")]      public GameLogDto GameLog { get; set; }
    }

    // Ошибки игры (ТЗ 11c, GT-5): пока агент подключён, журнал отладки не открывается сам, а ошибки приходят сюда.
    public class GameLogDto
    {
        [JsonProperty("agent_connected")]     public bool AgentConnected { get; set; }
        [JsonProperty("errors")]              public int Errors { get; set; }
        [JsonProperty("warnings")]            public int Warnings { get; set; }
        [JsonProperty("last_errors")]         public List<string> LastErrors { get; set; }
        [JsonProperty("client_aborts")]       public int ClientAborts { get; set; }
        [JsonProperty("log_windows_closed")]  public int LogWindowsClosed { get; set; }
        [JsonProperty("palette_windows_closed")] public int PaletteWindowsClosed { get; set; }
    }

    public class ColonyDto
    {
        [JsonProperty("name")]       public string Name { get; set; }
        [JsonProperty("resources")]  public Dictionary<string, int> Resources { get; set; }
        [JsonProperty("pawns")]      public List<PawnDto> Pawns { get; set; }
        [JsonProperty("animals")]    public List<AnimalDto> Animals { get; set; }
        [JsonProperty("buildings")]  public List<BuildingDto> Buildings { get; set; }
        [JsonProperty("zones")]      public List<ZoneDto> Zones { get; set; }
        [JsonProperty("research")]   public ResearchDto Research { get; set; }
        [JsonProperty("food_days")]  public float FoodDays { get; set; }
        [JsonProperty("forbidden_items")] public List<ForbiddenItemDto> ForbiddenItems { get; set; }
        [JsonProperty("pending_construction")] public List<PendingConstructionDto> PendingConstruction { get; set; }
        [JsonProperty("visitors")]   public List<VisitorDto> Visitors { get; set; }
        [JsonProperty("alerts")]     public List<AlertDto> Alerts { get; set; }
    }

    public class PendingConstructionDto
    {
        [JsonProperty("def")]      public string DefName { get; set; }
        [JsonProperty("position")] public PositionDto Position { get; set; }
        [JsonProperty("status")]   public string Status { get; set; }
    }

    public class ForbiddenItemDto
    {
        [JsonProperty("id")]       public string Id { get; set; }
        [JsonProperty("def")]      public string DefName { get; set; }
        [JsonProperty("count")]    public int Count { get; set; }
        [JsonProperty("position")] public PositionDto Position { get; set; }
    }

    public class PawnDto
    {
        [JsonProperty("id")]               public string Id { get; set; }
        [JsonProperty("name")]             public string Name { get; set; }
        [JsonProperty("age")]              public int Age { get; set; }
        [JsonProperty("gender")]           public string Gender { get; set; }
        [JsonProperty("health_pct")]       public float HealthPercent { get; set; }
        [JsonProperty("mood_pct")]         public float MoodPercent { get; set; }
        [JsonProperty("skills")]           public Dictionary<string, int> Skills { get; set; }
        [JsonProperty("work_priorities")]  public Dictionary<string, int> WorkPriorities { get; set; }
        [JsonProperty("schedule")]         public List<string> Schedule { get; set; }
        [JsonProperty("needs")]            public Dictionary<string, float> Needs { get; set; }
        [JsonProperty("mood_thoughts")]    public List<MoodThoughtDto> MoodThoughts { get; set; }
        [JsonProperty("current_job")]      public string CurrentJob { get; set; }
        [JsonProperty("is_drafted")]       public bool IsDrafted { get; set; }
        [JsonProperty("is_prisoner")]      public bool IsPrisoner { get; set; }
        [JsonProperty("medical_care")]     public string MedicalCare { get; set; }
        [JsonProperty("traits")]           public List<string> Traits { get; set; }
        [JsonProperty("conditions")]       public List<string> HealthConditions { get; set; }
        [JsonProperty("equipped")]         public string Equipped { get; set; }
        [JsonProperty("is_downed")]        public bool IsDowned { get; set; }
        [JsonProperty("bleeding")]         public float Bleeding { get; set; }
        [JsonProperty("wounds")]           public List<string> Wounds { get; set; }
        [JsonProperty("incapable_of")]     public List<string> IncapableOf { get; set; }
        [JsonProperty("weapon_ranged")]    public bool WeaponRanged { get; set; }
        [JsonProperty("apparel")]          public List<string> Apparel { get; set; }
        [JsonProperty("position")]         public PositionDto Position { get; set; }
    }

    public class MoodThoughtDto
    {
        [JsonProperty("label")]  public string Label { get; set; }
        [JsonProperty("effect")] public float MoodEffect { get; set; }
    }

    public class AnimalDto
    {
        [JsonProperty("id")]        public string Id { get; set; }
        [JsonProperty("name")]      public string Name { get; set; }
        [JsonProperty("def")]       public string DefName { get; set; }
        [JsonProperty("health_pct")]public float HealthPercent { get; set; }
        [JsonProperty("trained")]   public bool IsTrained { get; set; }
        [JsonProperty("master")]    public string MasterPawnId { get; set; }
        [JsonProperty("position")]  public PositionDto Position { get; set; }
    }

    public class ThreatDto
    {
        [JsonProperty("id")]        public string Id { get; set; }
        [JsonProperty("name")]      public string Name { get; set; }
        [JsonProperty("def")]       public string DefName { get; set; }
        [JsonProperty("faction")]   public string Faction { get; set; }
        [JsonProperty("health_pct")]public float HealthPercent { get; set; }
        [JsonProperty("is_downed")] public bool IsDowned { get; set; }
        [JsonProperty("kind")]      public string Kind { get; set; }
        [JsonProperty("is_fleeing")]public bool IsFleeing { get; set; }
        [JsonProperty("is_ranged")] public bool IsRanged { get; set; }
        [JsonProperty("job")]       public string Job { get; set; }
        [JsonProperty("weapon")]    public string Weapon { get; set; }
        [JsonProperty("position")]  public PositionDto Position { get; set; }
    }

    public class BuildingDto
    {
        [JsonProperty("id")]       public string Id { get; set; }
        [JsonProperty("def")]      public string DefName { get; set; }
        [JsonProperty("label")]    public string Label { get; set; }
        [JsonProperty("position")] public PositionDto Position { get; set; }
        [JsonProperty("hp_pct")]   public float HpPercent { get; set; }
        [JsonProperty("bills")]    public List<BillDto> Bills { get; set; }
    }

    public class BillDto
    {
        [JsonProperty("id")]      public string Id { get; set; }
        [JsonProperty("recipe")]  public string Recipe { get; set; }
        [JsonProperty("count")]   public int Count { get; set; }
    }

    public class ZoneDto
    {
        [JsonProperty("id")]    public string Id { get; set; }
        [JsonProperty("type")]  public string Type { get; set; }
        [JsonProperty("label")] public string Label { get; set; }
    }

    public class ResearchDto
    {
        [JsonProperty("current")]   public string CurrentProject { get; set; }
        [JsonProperty("progress")]  public float CurrentProgress { get; set; }
        [JsonProperty("available")] public List<string> Available { get; set; }
        [JsonProperty("completed")] public List<string> Completed { get; set; }
    }

    public class PositionDto
    {
        [JsonProperty("x")] public int X { get; set; }
        [JsonProperty("z")] public int Z { get; set; }
    }

    public class GameEventDto
    {
        [JsonProperty("type")]     public string Type { get; set; }
        [JsonProperty("priority")] public string Priority { get; set; }
        [JsonProperty("tick")]     public int Tick { get; set; }
        [JsonProperty("data")]     public object Data { get; set; }
    }

    public class VisitorDto
    {
        [JsonProperty("id")]           public string Id { get; set; }
        [JsonProperty("name")]         public string Name { get; set; }
        [JsonProperty("faction")]      public string Faction { get; set; }
        [JsonProperty("is_trader")]    public bool IsTrader { get; set; }
        [JsonProperty("goods")]        public List<string> Goods { get; set; }
        [JsonProperty("position")]     public PositionDto Position { get; set; }
    }

    public class AlertDto
    {
        [JsonProperty("category")]     public string Category { get; set; }
        [JsonProperty("message")]      public string Message { get; set; }
        [JsonProperty("severity")]     public string Severity { get; set; }
    }
}

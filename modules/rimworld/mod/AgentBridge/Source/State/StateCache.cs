namespace AgentBridge
{
    // Written on the main game thread; read on the HTTP thread.
    // volatile string guarantees the HTTP thread sees the latest pointer.
    public class StateCache
    {
        private volatile string _json = "{\"status\":\"initializing\"}";

        public void Refresh() => _json = StateSerializer.Serialize();

        public string Get() => _json;
    }
}

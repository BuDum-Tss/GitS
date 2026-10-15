using System.Collections.Concurrent;
using System.Collections.Generic;
using Newtonsoft.Json;
using Verse;

namespace AgentBridge
{
    public class EventQueue
    {
        private readonly ConcurrentQueue<GameEventDto> _queue = new ConcurrentQueue<GameEventDto>();

        public void Push(string type, string priority, object data = null)
        {
            _queue.Enqueue(new GameEventDto
            {
                Type = type,
                Priority = priority,
                Tick = Find.TickManager?.TicksGame ?? 0,
                Data = data
            });
        }

        // Drain all events (called by GET /events — clears the queue)
        public string DrainJson()
        {
            var batch = new List<GameEventDto>();
            while (_queue.TryDequeue(out var evt))
                batch.Add(evt);
            return JsonConvert.SerializeObject(batch);
        }

        // Peek for embedding in /state response (does NOT clear)
        public List<GameEventDto> Peek()
        {
            return new List<GameEventDto>(_queue);
        }
    }
}

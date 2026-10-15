using System.Collections.Concurrent;
using System.Threading.Tasks;

namespace AgentBridge
{
    public class CommandQueue
    {
        private readonly ConcurrentQueue<(string json, TaskCompletionSource<string> tcs)> _queue
            = new ConcurrentQueue<(string, TaskCompletionSource<string>)>();

        /// Ставит команду в очередь и возвращает Task с реальным результатом
        /// исполнения на главном потоке ("OK" или "ERROR: ...").
        public Task<string> Enqueue(string commandJson)
        {
            var tcs = new TaskCompletionSource<string>(TaskCreationOptions.RunContinuationsAsynchronously);
            _queue.Enqueue((commandJson, tcs));
            return tcs.Task;
        }

        public int Count => _queue.Count;

        public bool TryDequeue(out string json, out TaskCompletionSource<string> tcs)
        {
            if (_queue.TryDequeue(out var item))
            {
                json = item.json;
                tcs  = item.tcs;
                return true;
            }
            json = null;
            tcs  = null;
            return false;
        }
    }
}

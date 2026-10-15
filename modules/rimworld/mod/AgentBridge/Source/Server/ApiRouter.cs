using System;
using Newtonsoft.Json;

namespace AgentBridge
{
    public class ApiRouter
    {
        public (int status, string json) Route(string method, string path, string body)
        {
            try
            {
                return (method, path) switch
                {
                    ("GET",  "/state")    => (200, AgentBridgeMod.StateCache.Get()),
                    ("GET",  "/events")   => (200, AgentBridgeMod.Events.DrainJson()),
                    ("GET",  "/ping")     => (200, "{\"status\":\"ok\",\"mod\":\"AgentBridge\"}"),
                    ("POST", "/command")  => HandleCommand(body),
                    _ => (404, "{\"error\":\"not found\"}")
                };
            }
            catch (Exception e)
            {
                return (500, JsonConvert.SerializeObject(new { error = e.Message }));
            }
        }

        private (int, string) HandleCommand(string body)
        {
            if (string.IsNullOrEmpty(body))
                return (400, "{\"error\":\"empty body\"}");

            var task = AgentBridgeMod.Commands.Enqueue(body);

            // Команды исполняются на главном потоке каждый Unity-кадр, так что
            // обычно результат готов за доли секунды. Ждём его, чтобы вызывающий
            // (агент) видел РЕАЛЬНЫЙ результат, а не просто факт постановки в очередь.
            if (task.Wait(20000))
            {
                var result = task.Result;
                if (result.StartsWith("ERROR"))
                    return (200, JsonConvert.SerializeObject(new { status = "error", message = result }));
                // result == "OK" для обычных команд, либо JSON-payload для команд-запросов
                // (например find_buildable_spot) — прокидываем его целиком дальше.
                return (200, JsonConvert.SerializeObject(new { status = "ok", result }));
            }

            return (202, "{\"status\":\"queued_timeout\",\"message\":\"не выполнено за 3с\"}");
        }
    }
}

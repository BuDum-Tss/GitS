using System;
using System.IO;
using System.Net;
using System.Text;
using System.Threading;
using Verse;

namespace AgentBridge
{
    public class HttpServer
    {
        private readonly HttpListener _listener;
        private readonly ApiRouter _router;
        private Thread _thread;

        public HttpServer(int port)
        {
            _listener = new HttpListener();
            _listener.Prefixes.Add($"http://localhost:{port}/");
            _router = new ApiRouter();
        }

        public void Start()
        {
            try
            {
                _listener.Start();
                _thread = new Thread(Listen) { IsBackground = true, Name = "AgentBridge-HTTP" };
                _thread.Start();
                Log.Message("[AgentBridge] Listening on http://localhost:8765/");
            }
            catch (Exception e)
            {
                Log.Error($"[AgentBridge] Failed to start HTTP server: {e.Message}");
            }
        }

        public void Stop()
        {
            try { _listener.Stop(); } catch { }
        }

        private void Listen()
        {
            while (_listener.IsListening)
            {
                try
                {
                    var ctx = _listener.GetContext();
                    ThreadPool.QueueUserWorkItem(_ => Handle(ctx));
                }
                catch (Exception)
                {
                    // Сбой приёма одного соединения не должен убивать сервер (раньше любой такой сбой выходил из цикла
                    // насовсем). Выходим только когда слушатель остановлен.
                    if (!_listener.IsListening) break;
                }
            }
        }

        private void Handle(HttpListenerContext ctx)
        {
            LogGuard.NoteRequest();
            try
            {
                ctx.Response.Headers.Add("Access-Control-Allow-Origin", "*");
                ctx.Response.Headers.Add("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
                ctx.Response.ContentType = "application/json; charset=utf-8";

                if (ctx.Request.HttpMethod == "OPTIONS")
                {
                    ctx.Response.StatusCode = 204;
                    ctx.Response.Close();
                    return;
                }

                string body = null;
                if (ctx.Request.HttpMethod == "POST")
                {
                    using var reader = new StreamReader(ctx.Request.InputStream, Encoding.UTF8);
                    body = reader.ReadToEnd();
                }

                var (status, json) = _router.Route(
                    ctx.Request.HttpMethod,
                    ctx.Request.Url.AbsolutePath,
                    body
                );

                ctx.Response.StatusCode = status;
                var bytes = Encoding.UTF8.GetBytes(json);
                ctx.Response.OutputStream.Write(bytes, 0, bytes.Length);
            }
            catch (Exception e)
            {
                // GT-6: обрыв соединения клиентом (тайм-аут запроса у агента) безвреден и в журнал игры не идёт: там даже
                // Warning открывает окно журнала при включённом openLogOnWarnings. Остальное остаётся предупреждением.
                if (LogGuard.IsClientAbort(e)) LogGuard.NoteClientAbort();
                else Log.Warning($"[AgentBridge] Request error: {e.Message}");
            }
            finally
            {
                try { ctx.Response.Close(); } catch { }
            }
        }
    }
}

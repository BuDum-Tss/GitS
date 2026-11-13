using System;
using ManagedCommon;
using System.Collections.Generic;
using System.Net.Http;
using System.Text;
using System.Text.Json;
using System.Threading.Tasks;
using Wox.Plugin;

namespace Community.PowerToys.Run.Plugin.Assistant
{
    /// <summary>
    /// Плагин PowerToys Run: «@ привет» отправляет сообщение ассистенту
    /// (тот же вход, что у чата в дашборде, http://127.0.0.1:8766/api/chat).
    /// Ответ приходит облачком питомца, в чат и голосом.
    /// </summary>
    public class Main : IPlugin
    {
        public static string PluginID => "5C3E7A1B9D2F4E6A8B0C1D2E3F4A5B6C";

        public string Name => "Ассистент";
        public string Description => "Написать сообщение ассистенту: @ текст";

        private const string ChatUrl = "http://127.0.0.1:8766/api/chat";
        private static readonly HttpClient Http = new HttpClient { Timeout = TimeSpan.FromSeconds(5) };
        private PluginInitContext _context;
        private string _iconPath = "Images/assistant.dark.png";

        public void Init(PluginInitContext context)
        {
            _context = context;
            _context.API.ThemeChanged += OnThemeChanged;
            UpdateIconPath(_context.API.GetCurrentTheme());
        }

        public List<Result> Query(Query query)
        {
            var text = (query.Search ?? string.Empty).Trim();
            if (text.Length == 0)
            {
                return new List<Result>
                {
                    new Result
                    {
                        Title = "Напиши сообщение ассистенту",
                        SubTitle = "Например: @ привет. Enter отправит",
                        IcoPath = _iconPath,
                    },
                };
            }

            return new List<Result>
            {
                new Result
                {
                    Title = text,
                    SubTitle = "Отправить ассистенту (Enter)",
                    IcoPath = _iconPath,
                    Action = ctx =>
                    {
                        _ = SendAsync(text);
                        return true;   // закрыть строку PowerToys Run
                    },
                },
            };
        }

        private static async Task SendAsync(string text)
        {
            try
            {
                var json = JsonSerializer.Serialize(new { message = text });
                using var content = new StringContent(json, Encoding.UTF8, "application/json");
                await Http.PostAsync(ChatUrl, content).ConfigureAwait(false);
            }
            catch (Exception)
            {
                // ядро не запущено: сообщению некуда идти
            }
        }

        private void OnThemeChanged(Theme currentTheme, Theme newTheme) => UpdateIconPath(newTheme);

        private void UpdateIconPath(Theme theme) =>
            _iconPath = theme == Theme.Light || theme == Theme.HighContrastWhite
                ? "Images/assistant.light.png"
                : "Images/assistant.dark.png";
    }
}

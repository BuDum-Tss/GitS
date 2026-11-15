# voice_probe

Ручная проба голоса Discord с DAVE: вход в канал, приём расшифрованного звука каждого участника, воспроизведение тона.

```
docker exec -it <контейнер> python /app/workspaces/discord/voice_probe/probe.py --guild ID --channel ID --seconds 60 [--save]
```

- Токен: переменная окружения `DISCORD_BOT_TOKEN` (не печатается).
- Звук по умолчанию нигде не сохраняется; `--save` пишет по 5 с на участника в WAV 16 кГц моно (`--save-dir`, по умолчанию `/tmp`).
- Нужны права бота Connect, Speak, Use Voice Activity и пакеты из `docker/core/requirements.txt`.
- Тесты без сети: `python -m pytest -q workspaces/discord/voice_probe/tests -p no:cacheprovider`.

Полное описание, критерии успеха и что делать при провале: `docs/DISCORD_VOICE_PROBE.md`.

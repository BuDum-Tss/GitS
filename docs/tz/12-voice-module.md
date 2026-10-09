# 12. Модуль `voice`: микрофон, распознавание, узнавание голосов, синтез, воспроизведение, слух

Этап 2. Репозиторий: `GitS`. Ветка: `tz/12-13-voice-pet` — **одна ветка и одна задача вместе с ТЗ 13**: воспроизведение
переезжает из питомца в голос, и по отдельности они ломают озвучку.
Читать перед этим: `00-architecture.md`, `01-module-contract.md`, `02-core.md`, `03-attention.md`, раздел 3 ТЗ 10
(правила модулей монорепо — обязательны и здесь).

## 1. Цель

Весь звук в одном модуле-процессе `voice`. Ядро решает, ЧТО сказать; модуль решает, КАК это прозвучит, и сообщает,
что сказал пользователь, когда она говорит и когда её перебили. Ядро остаётся без звуковых зависимостей.

## 2. Исходное состояние (проверь)

- `tts_engine.py`: Fish Speech (`FISH_URL`, референс `resources/tts/references/GlaDos.wav`) с запасным edge-tts;
  очередь из одного элемента; `speak_sequence(segments)` прерывает прежнее (поколение `_gen`); воспроизведение
  через PowerShell PresentationCore; `set_muted`, `set_volume`, `get_current_segment`, `synth_file`.
  **Воспроизведение сейчас идёт в процессе питомца** (`desktop_pet.py`: опрос `/api/state` каждые 2 с, новая реплика
  → `_split_sentences` → `tts_engine.speak_sequence`).
- `mic_listener.py`: VAD (webrtcvad), `UtteranceSegmenter`, поток записи, «ворота» (`_gate`: микрофон закрыт, пока
  она говорит, и `ECHO_TAIL` после), регистрация её собственного голоса (`_ensure_self_voice`), `handle_utterance` →
  WhisperX (`stt_client.STTClient`, :9000) → `voice_id.resolve` → `companion.on_voice(text, info)`.
- `voice_id.py`: эмбеддинги голосов людей в `memory/` (владелец, имена, её голос `SELF_PERSON_ID`).
- `hearing.py`: слух системного звука (faster-whisper локально), `recent_text`, `note_own_speech`, сводка услышанного
  (`_maybe_gist` → `UserMemory.add_activity` и журнал).
- `opus_encoder.py`: ffmpeg → OGG/Opus и PCM 16 кГц.
- Инструменты в `environments/builtin.py`: `system.recent_audio`; `assistant.name_voice` (назвать последний
  услышанный голос, `vid.set_name`, `vid.set_owner`; берёт `companion.last_voice`).
- `core_service.py`: создание `Hearing`, `VoiceIdentity`, `MicListener` с `own_voice_sample`.

## 3. Перенос

- **VOICE-1 (ОБЯЗАТЕЛЬНО).** В `GitS/modules/voice/` переезжают: `tts_engine.py`, `mic_listener.py`, `stt_client.py`,
  `voice_id.py`, `hearing.py`, `opus_encoder.py` (копия у модуля `telegram` из ТЗ 10 удаляется: он получает OGG через
  `core.tts`), `resources/tts/` (референс голоса), `_split_sentences` из `desktop_pet.py` (в `segmenter.py` модуля).
  Из ядра — удалить эти файлы и их создание в `core_service.py`; `requirements.txt` ядра — убрать то, что нужно
  только звуку (`edge-tts`, `soundcard`, `faster-whisper`, `ormsgpack` — проверь, что ядро их больше не импортирует).
- **VOICE-2 (ОБЯЗАТЕЛЬНО).** Данные остаются на месте: `voice_id` хранит файлы в `MEMORY_DIR` под прежними именами.
  `UserMemory` в `hearing.py` больше не используется (VOICE-14).

## 4. Манифест

`id: voice`, `title: Голос`, `capabilities: ["voice.output", "voice.input"]`,
`subscribe: ["settings", "module"]`, `shared_settings: ["voice_muted", "volume", "mic", "hearing", "dnd",
"quiet_until"]`, `state: true`. `settings_schema`:

| Ключ | Тип | По умолчанию | Смысл |
|---|---|---|---|
| `barge_in` | boolean | false | перебивание: её речь обрывается, когда говорит владелец (VOICE-10) |
| `echo_tail_ms` | integer (0–3000) | нынешнее `ECHO_TAIL` в мс | сколько микрофон закрыт после её речи |

Инструменты:

| name | Был | availability | risk | params |
|---|---|---|---|---|
| `recent_audio` | `system.recent_audio` | always | auto | — (описание и `keywords` перенести) |
| `name_voice` | `assistant.name_voice` | always | auto | как сейчас (имя; владелец ли) — перенести без изменения смысла; «последний услышанный голос» модуль помнит сам (последний `info` из `handle_utterance`) |

События (`emits`):

| kind | Через внимание? | Смысл |
|---|---|---|
| `voice.utterance` | **нет** — ядро передаёт в разговор (VOICE-6) | пользователь или другой человек что-то сказал |
| `voice.speech` | **нет** — ядро обновляет `presence` | начало, сегмент, конец, обрыв её речи |
| `voice.user_speaking` | **нет** — ядро обновляет `presence.user_speaking` | человек начал или закончил говорить (по VAD) |
| `voice.heard` | да, `default_urgency 1` | сводка услышанного в колонках (`_maybe_gist`) |

## 5. Протокол речи (ядро ↔ voice)

Ядро → модуль (не запросы):

```json
{"type": "speak", "body": {"utterance_id": "u-1a2b3c4d", "text": "Полный текст реплики.", "emotion": "angry",
                           "replace": true}}
{"type": "speak.stop", "body": {"utterance_id": "u-1a2b3c4d", "at": "segment_end"}}
```

- `replace: true` — прервать то, что звучит, сразу (нынешнее поведение `speak_sequence`); `false` — поставить после
  текущей реплики (очередь до 3 реплик; при переполнении выбрасывается самая старая из ожидающих).
- `speak.stop`: `utterance_id: null` — остановить всё и очистить очередь; `at: "now"` — оборвать воспроизведение
  немедленно; `at: "segment_end"` — договорить текущее предложение и остановиться.

Модуль → ядро (события, `kind: voice.speech`, `data`):

```json
{"phase": "started",  "utterance_id": "u-1a2b3c4d", "seq": 0, "segment": "Первое предложение."}
{"phase": "segment",  "utterance_id": "u-1a2b3c4d", "seq": 1, "segment": "Второе предложение."}
{"phase": "ended",    "utterance_id": "u-1a2b3c4d", "completed": true,  "spoken_upto": 2}
{"phase": "ended",    "utterance_id": "u-1a2b3c4d", "completed": false, "spoken_upto": 1, "reason": "stop|replaced|barge_in|muted|error"}
```

Запрос ядро → модуль:

```json
{"type": "tts.synthesize", "body": {"text": "...", "emotion": null, "format": "wav"}}
→ {"ok": true, "value": {"path": "C:\\...\\tts_7f3a.wav", "duration_ms": 2350}}
```

- **VOICE-3 (ОБЯЗАТЕЛЬНО).** Разбивка на предложения — в модуле (`_split_sentences`, перенесённая без изменений).
  Синтез следующего сегмента начинается, пока звучит текущий (предзагрузка одного сегмента вперёд), чтобы паузы
  между предложениями не росли. Если это меняет поведение очереди `tts_engine` — переписать воркер, сохранив
  поколения для прерывания.
- **VOICE-4 (ОБЯЗАТЕЛЬНО).** `voice_muted`: `speak` не воспроизводится, но события `started` и `ended`
  (`completed: false`, `reason: "muted"`) шлются сразу, чтобы ядро не ждало. Громкость `volume` применяется к
  воспроизведению, как сейчас.
- **VOICE-5 (ОБЯЗАТЕЛЬНО).** `tts.synthesize`: файлы — во временную папку модуля (`%TEMP%\glados_voice\`); модуль
  удаляет файлы старше 1 часа при старте и раз в час. `format: ogg` — через `opus_encoder`. Синтез в
  `asyncio.to_thread`. Ошибка синтеза → `unavailable`.

## 6. Входящая речь

- **VOICE-6 (ОБЯЗАТЕЛЬНО).** Распознанная реплика (бывший вызов `companion.on_voice(text, info)`) → событие
  `voice.utterance`: `text` — распознанный текст (это не факт, а сама реплика; исключение из правила «text — факт»
  для этого вида), `data: {"speaker": info}` (поля `info` как сейчас: `id`, `name`, `new`, `owner`, `has_owner`),
  `sender: {"id": "voice:<id>", "name", "is_owner": owner}`. В ядре (`module_hub`) событие этого вида не идёт в
  `attention.submit`, а вызывает `companion.on_voice(text, speaker)` с `channel.kind = "voice"`.
- **VOICE-7 (ОБЯЗАТЕЛЬНО).** «Ворота» микрофона (`_gate`) берут состояние «говорит ли она» не из колбэка ядра, а из
  собственного воспроизведения модуля (точно, а не по оценке длины текста). `ECHO_TAIL` — из `echo_tail_ms`.
- **VOICE-8 (ОБЯЗАТЕЛЬНО).** `voice.user_speaking`: `data: {"speaking": true|false}` по VAD-сегментатору — начало
  речи (первый кадр речи после тишины, кроме кадров, пока закрыты «ворота») и конец (сегмент закрыт). Не чаще 5 раз в
  секунду. Ядро: `presence.user_speaking`, а `last_user_ts` обновляется по `speaking: false`.
- **VOICE-9 (ОБЯЗАТЕЛЬНО).** Регистрация её голоса (`_ensure_self_voice`): образец синтезируется внутри модуля
  (`tts_engine.synth_file` + `opus_encoder.audio_file_to_pcm16k`), колбэк `own_voice_sample` из `core_service`
  удаляется.

## 7. Перебивание (новое, по умолчанию выключено)

- **VOICE-10 (ОБЯЗАТЕЛЬНО).** При `barge_in: true`: пока она говорит, микрофон не закрывается полностью; кадры речи
  копятся; если непрерывная речь длится ≥ 800 мс, кусок отправляется в WhisperX только за эмбеддингом голоса (или
  целиком, если эмбеддинг отдельно не получить) и `voice_id.resolve` — если это НЕ её голос (`SELF_PERSON_ID`) и это
  владелец или любой человек при `has_owner == False` — воспроизведение обрывается (`at: "now"`), событие
  `voice.speech` `ended` с `reason: "barge_in"`, сегментатор продолжает запись реплики как обычной. Если голос её —
  ничего не делать.
- **VOICE-11 (ОБЯЗАТЕЛЬНО).** В отчёте описать проверку перебивания в наушниках и через колонки (на колонках без
  подавления эха ожидаются ложные срабатывания — поэтому выключено по умолчанию). Цель: обрыв речи ≤ 1,2 с от начала
  фразы владельца.

## 8. Слух системного звука

- **VOICE-12 (ОБЯЗАТЕЛЬНО).** `hearing.py` переносится без изменения логики распознавания. `note_own_speech` вызывает
  сам модуль при каждом сегменте её речи.
- **VOICE-13 (ОБЯЗАТЕЛЬНО).** Разрешение слушать (`_allowed_now`) — по `hearing` из общих настроек.
- **VOICE-14 (ОБЯЗАТЕЛЬНО).** Сводка услышанного (`_maybe_gist`) → событие `voice.heard`, `text` — факт «В колонках:
  <summary>», `memorable: false`. Ядро в обработчике этого вида дополнительно делает то, что раньше делал
  `hearing` (`UserMemory.add_activity("звук", "audio", summary)`), — общее правило: для вида `voice.heard` ядро пишет
  активность. `fast_brain` вызывается из модуля (разрешённый импорт).

## 9. Изменения в ядре в рамках этого ТЗ

- **VOICE-15 (ОБЯЗАТЕЛЬНО).** `speech_out.VoiceModuleSpeech(SpeechOutput)`: `speak` → сообщение `speak` модулю
  (`replace = True`, если внимание прерывает, иначе `False`); `stop` → `speak.stop`; `is_speaking()` и
  `current_segment()` — по событиям `voice.speech` (точно, без оценки длины). Выбор реализации: если подключён модуль
  с `voice.output` — `VoiceModuleSpeech`, иначе — «без голоса» (`TextOnlySpeech`: реплики только в чат дашборда и
  `presence`, `is_speaking()` всегда `False`). `LegacyPetSpeech` и `say.json` удалить вместе с ТЗ 13.
- **VOICE-16 (ОБЯЗАТЕЛЬНО).** `core.tts` (ТЗ 02 §10.2) → запрос `tts.synthesize` модулю `voice`; если модуля нет —
  `unavailable`.
- **VOICE-17 (ОБЯЗАТЕЛЬНО).** Ядро публикует `presence` по событиям `voice.speech` (`speaking`, `utterance_id`,
  `segment`) и `voice.user_speaking`.
- **VOICE-18 (ОБЯЗАТЕЛЬНО).** `modules.yaml`: модуль `voice` (`{GITS}/modules/voice`, `{PYTHON}`, `autostart: true`,
  `restart: on-failure`).

## 10. Задержка

- **VOICE-19 (ОБЯЗАТЕЛЬНО).** Замерить и записать в отчёт и CHANGELOG (по 20 замеров, Fish Speech прогрет):
  от `speak` до начала звука первого сегмента; паузу между сегментами; от конца фразы пользователя (VAD) до события
  `voice.utterance`. Сравнить с тем же до переноса (замер на ветке `master` тем же способом). Ухудшение больше чем на
  100 мс по любому замеру — сообщить и искать причину до сдачи.

## 11. Тесты

- **VOICE-20 (ОБЯЗАТЕЛЬНО).** `GitS/modules/voice/tests/` (без микрофона, колонок, Fish Speech и WhisperX — заглушки
  синтеза, воспроизведения и STT):
  - `speak` → события `started`, `segment`…, `ended completed`; порядок `seq`;
  - `replace: true` обрывает текущую (`ended reason replaced`); `false` ставит в очередь; переполнение очереди;
  - `speak.stop` `now` и `segment_end`; `utterance_id: null` чистит очередь;
  - `voice_muted` → мгновенные `started`/`ended muted`;
  - `tts.synthesize` wav и ogg (заглушка ffmpeg), чистка старых файлов;
  - `handle_utterance` → событие `voice.utterance` с полями говорящего; свой голос → ничего;
  - перебивание: заглушка `resolve` возвращает владельца → обрыв; её голос → без обрыва;
  - перенесённые тесты микрофона, слуха и `voice_id` проходят.
- **VOICE-21 (ОБЯЗАТЕЛЬНО).** Тесты ядра: `VoiceModuleSpeech` против `FakeModule` (модуль-заглушка на SDK);
  `voice.utterance` → `companion.on_voice`; `presence` по событиям; `core.tts` → `tts.synthesize`; без модуля голоса —
  `TextOnlySpeech`.

## 12. Приёмка (вместе с ТЗ 13)

1. `.\check.ps1` зелёный; в процессе ядра нет импортов `tts_engine`, `mic_listener`, `hearing`, `voice_id`,
   `sounddevice`/`soundcard`.
2. Ручная проверка: она говорит голосом (Fish Speech; при выключенном docker — edge-tts), облачко питомца показывает
   текущее предложение; разговор голосом работает; свой голос из колонок не распознаётся как реплика; «замолчи»
   обрывает речь; «что сейчас играет?» отвечает по слуху; голосовой пост в Telegram (`telegram_voice`) уходит.
3. Выключение модуля `voice`: она отвечает только текстом в чате дашборда, ядро не падает; включение возвращает голос.
4. Замеры VOICE-19 в CHANGELOG.

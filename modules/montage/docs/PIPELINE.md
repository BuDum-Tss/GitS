# Как устроен конвейер монтажа

Кратко, для людей. Полная постановка: `C:\MyProjects\GitS\docs\tz\20-montage-module.md` (конвейер, песочница, контроль
качества), `21-aperture-style.md` (стиль), `22-editing-craft.md` (ремесло). Состояние: шаблон Remotion, регламент и ресурсы
готовы (шаг 1); конвейер на Python, агент Claude, песочница и контроль качества сделаны на шаге 2.

## Общая схема

```
ядро GitS  --(tool.call montage.start)-->  agent/main.py (ModuleClient, очередь, одна задача за раз)
                                              |
        jobs/<job_id>/  <---- pipeline.py: стадии ----> агент Claude (claude_runner.py, sandbox.py, скиллы Remotion)
                                              |
                       template/ (копия в work/)  --npx remotion render-->  renders/*.mp4
```

Модуль сам подключается к ядру (WebSocket, SDK `gits_sdk`), объявляет инструменты `start`, `status`, `revise`,
`cancel`, ведёт задачу с вопросами пользователю через ядро (`core.ask`, `job.update`) и ничего не публикует.

## Стадии задачи

| Стадия | Что происходит |
|---|---|
| `intake` | проверка файлов (`ffprobe`), HEVC в H.264, ссылки в `sources/`, определение вида исходников |
| `clarify` | не больше 2 вопросов пользователю (площадка, длительность, какой дубль) |
| `transcribe` | звук 16 кГц, WhisperX со словами, уточнение границ по паузам (`words.json`) |
| `story` | агент пишет историю ролика: логлайн, крючок, биты, подготовки и отдачи (`plan/story.json`) |
| `plan` | агент пишет `plan.json` и `plan.md` по истории, регламенту стиля и руководству ремесла |
| `plan_approval` | вопрос «утвердить / переделать» (история, затем план) |
| `voiceover` | строки закадра из плана, синтез её голосом (`core.tts`), файлы в `work/public/vo/` |
| `build` | агент собирает проект Remotion в `work/` по плану, `npx tsc --noEmit`, кадры `npx remotion still` |
| `preview` | черновик `--scale 0.5 --crf 28`, автоматический контроль качества |
| `review` | вопрос «готово / правки»; правки возвращают на `build` |
| `final` | рендер `--crf 16`, затем `loudnorm` до -14 LUFS, копия в `output_dir` |

## Шаблон Remotion (`template/`)

Одна композиция `Main`. Размер, fps и длительность берёт `calculateMetadata` из `plan.json` (props), перед этим план
проверяется схемой zod `src/schema.ts`, ждут шрифты (`@remotion/fonts`) и подгружаются слова расшифровки (поле
`captions.words_file`, по умолчанию `transcript/words.json` в `public/`).

### Папки задачи -> шаблон

| В плане | В шаблоне |
|---|---|
| `segments[].source`, например `sources/clip_h264.mp4` | `work/public/media/clip_h264.mp4` (берётся только имя файла) |
| `voiceover[].id` или `tts_path` | `work/public/vo/<id>.wav` |
| `captions.words_file` | `work/public/<words_file>`, формат `[{"i","text","start_ms","end_ms","conf"}]` |
| `sfx[].name` | `public/sfx/<name>.wav` (из `resources/<Имя>/montage/assets/sfx`) |
| шрифты | `public/fonts/` (из `resources/<Имя>/montage/assets/fonts`) |

### Компоненты (имена в коде и вид вставки в плане)

| Компонент | Где в плане | Регламент |
|---|---|---|
| `Segments` | `segments` (склейка, `crop.mode` `center`/`face`, `zoom` до 0.04, источник под закадром приглушается) | 9 |
| `Captions` | `captions` (слова по страницам, `source` `transcript`/`voiceover`/`both`, `overrides`, `y_ratio`); рисуются только в вертикали (ТЗ 23, SC-4) | 4.4 |
| `Voiceover` | `voiceover` | 5 |
| `Intro` | `inserts[].kind = intro` (`subtitle`, `wordmark`; 4 с) | 4.1 |
| `Blueprint` | `blueprint` (`title`, `kicker`, `note`, `callouts[]` до 4, `drawing[]`: line/rect/circle/path в долях кадра) | 4.2 |
| `GladosRoom` | `glados_room` (`moods[]`, `speech[]`; голос из `voiceover` и `public/vo/<id>.wav`) | 4.3 |
| `ChamberSign`, `ResultSign` | `chamber_sign`, `result_sign` (`big`, `small`, `done`, `total`, `active[]`, `title`; 2,5-3,5 с) | 4.5 |
| `Icon` | внутри табличек: 14 залитых иконок, имена в `src/style/icons.ts` | 4.6 |
| `FootageFrame` | у сегмента `frame: "hud"` и `frame_label` (рамка `public/hud/hud-frame.svg`, горизонтальные ролики) | 4.7 |
| `Blackout` | `blackout` (поверх видео, звук и субтитры остаются) | 4.8 |
| `JournalLog` | `custom` с `props.component = "JournalLog"` | 4.9 |
| `Sfx` | `sfx` (и `music`) | 5 |
| `Cta` | `cta` | 8 |

Свои компоненты агент кладёт в `work/src/custom/` и регистрирует в `work/src/custom/index.ts`; в плане это вставка
`kind: "custom"` с `props.component`. `FontSpecimen` и `PictogramSheet` служебные (проверка шрифтов и старого набора пиктограмм; в плане роликов пиктограмм больше нет, их заменили иконки табличек).

Звуковые эффекты сами не включаются: звук к вставке (`chime_up` к табличке и успеху, `chime_down` к провалу) агент добавляет в
`sfx` плана отдельно, не больше одного эффекта в секунду.

### Токены и зоны

Цвета, шрифты, размеры, движение и зоны компонентов: `template/src/style/tokens.ts` (повторяет регламент; тест сверяет
цвета с таблицей в `resources/<Имя>/montage/APERTURE_STYLE.md`, а `npm run check-scenes` значения с `template/design/tokens.json`
дизайн-системы v2 и проверяет математику сцен без браузера). Зоны экспортируются в `template/qa_zones.json` (`npm run gen:qa-zones`) для
проверок качества: доли кадра (x, y, w, h) по форматам, безопасные зоны площадок, признак «вытесняет субтитры».
Схема плана для сверки с `agent/plan_schema.py`: `template/plan.schema.json` (`npm run gen:schema`).

Поля плана сверх ТЗ 20 §7 (консервативные добавления, все необязательные, кроме `beat`): `beat` у сегментов и вставок
(ТЗ 22 CRAFT-2, обязательное), `intent: "jump_cut"` и `zoom` у сегмента, `duck` у закадра, `captions.source` и
`captions.words_file`, `overrides[].highlight`, `blackout[].opacity`, `sfx[].volume`, `music`.

## Команды

Из `template/` (или из `jobs/<id>/work/`, где лежит копия):

```
npx tsc --noEmit
npx remotion compositions src/index.ts
npx remotion still  src/index.ts Main out/frame.png --props=../plan/plan.json --frame=90      # кадр, смотреть глазами
npx remotion render src/index.ts Main out/preview.mp4 --props=../plan/plan.json --scale=0.5 --crf=28     # черновик
npx remotion render src/index.ts Main out/final.mp4   --props=../plan/plan.json --crf=16                 # финал
node --disable-warning=MODULE_TYPELESS_PACKAGE_JSON scripts/validate-plan.mjs ../plan/plan.json          # проверка плана
```

Демо и проверка шаблона:

```
npm run demo:plans      # демо-планы demo/plan.*.json и public/demo/words.json (коммитятся)
npm run demo:media      # синтетические видео и закадр public/media, public/vo (в .gitignore)
npm run demo:stills     # кадры всех компонентов и проверка кириллицы в demo/out/
npx remotion render src/index.ts Main demo/out/demo_vertical.mp4 --props=demo/plan.vertical.short.json
```

После правок `resources/<Имя>/montage/assets/` выполнить `npm run sync-assets` (копии в `public/` и пиктограммы в `src/style/`).

## Субтитры

Субтитры вжигаются только в вертикальные короткие ролики (shorts, reels, telegram). В горизонтальных (youtube) `captions.enabled`
равен `false`, а рядом с финалом конвейер кладёт файл `.srt` из тех же слов (расшифровка или журнал речи и текст закадра) и
копирует его в `output_dir`. `plan_schema.py` отвергает вжигаемые субтитры в горизонтальном плане.

## Откуда слова (расшифровка)

Распознавание живой речи (WhisperX) отложено владельцем: источник `talking_head` на стадии `transcribe` завершает задачу с
`unavailable`. Для записей RimWorld и всего, где говорит только она, слова берутся из журнала речи (MON-26a): в `sources/` лежат
`speech_log.json` (события `say` и, если есть, `voice.speech`), `recording.json` (`started_at` записи, `duration_s`, куски звука с
`started_at`) и `scenario.json`. Время слов внутри реплики делится по длине слов; реплика, которой нет в звуке записи, субтитра не
получает. Результат `transcript/words.json`, происхождение `transcript/meta.json`.

## Автоисправление и приёмка

`preview`: рендер черновика, технические проверки (`qa.py`), смысловая проверка отдельным вызовом агента (кадры, план, рубрика
`prompts/qa_rubric.md`). Ошибки уходят монтажёру как правки (стадия `build`), не больше двух кругов; смысловая проверка идёт, когда
технических ошибок нет или на последнем круге. Человек получает черновик с отчётом `qa/report.md`. Решения человека на `plan_approval`
и `review` пишутся в `jobs/qa_verdicts.jsonl`; режим `review_mode` (`human`, `auto_then_human`, `auto`) и согласие автопроверки с
человеком: `agent/review_mode.py`. `auto` включается только при ≥ 20 решениях и согласии ≥ 90 %, иначе работает как
`auto_then_human`.

## Что проверяет qa.py

Формат (размер, fps, кодеки, длительность), громкость (`ebur128`, финал: -14 ± 1 LUFS и пик ≤ -1 dBTP, черновик: предупреждения),
`blackdetect`, `freezedetect`, `silencedetect` против плана, страницы субтитров (порт `template/src/timeline.ts`) против слов, зоны
вставок и безопасные зоны площадки (`template/qa_zones.json`), OCR текста на кадрах (RapidOCR; если не установлен, пропуск с
предупреждением), проверки ремесла (CRAFT-4): первый бит, тишина и неподвижная картинка в первых 3 секундах, ритм, перебивки, закадр
поверх живой речи, концовка. Появление субтитров в самом видео по пикселям не проверяется.

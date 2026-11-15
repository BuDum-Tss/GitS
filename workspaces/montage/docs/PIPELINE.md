# Как устроен монтаж

Кратко, для людей. Монтирует сама ГЛаДОС (агент ядра), модуль даёт ей рабочее место: инструменты, команды, правила и сайт для человека.
Полная постановка: `docs/tz/20-montage-module.md`, `21-aperture-style.md` (стиль), `22-editing-craft.md` (ремесло).

## Общая схема

```
ядро GitS  <--WebSocket (gits_sdk)-->  agent/main.py  (контейнер montage, аренда на хозяина: agent/lease_host.py)
                                          |
   ГЛаДОС: workspace.enter -> инструменты skills, files, work, run, resources, site -> workspace.exit
                                          |
   jobs/<id>/  план, сцены, исходники, расшифровка, work/ (копия template/), renders/, qa/
                                          |
   template/ (Remotion)  --npx remotion render-->  renders/preview.mp4 (черновик)  и  final (в output_dir)
```

Агент берёт контейнер в аренду на время работы и возвращает его: идущие операции (озвучка, рендер) доделываются после выхода.
Файлы роликов лежат на диске хозяина и переживают возврат аренды. Remotion Studio живёт в контейнере и гаснет только после
30 минут без обращений или при отмене ролика.

## Инструменты и команды

Инструменты вызывает агент; их список и параметры — в `agent/manifest.py` и на начальной странице сайта.

| Инструмент | Что делает |
|---|---|
| `skills` | читает навыки: `montage-regulation` (START.md, SKILL.md), Remotion и др. |
| `files` | читает ресурсы и файлы ролика, пишет `plan/idea.md`, `story.md`, `plan.json` и свои вставки в `work/src/custom/` |
| `work` | `create`, `list`, `status`, `update`, `scenes`, `cancel`: единицы (ролики) и их сцены |
| `run` | команды окружения (ниже), первым аргументом id ролика |
| `resources` | ресурсы персонажа: интро, комната, питомец, рамки, чертежи, шрифты, звуки |
| `site` | адрес сайта монтажа и страницы ролика |

Команды `run`: `intake` (приём исходников), `transcribe`, `compose` (лист из сцен), `voiceover` (озвучка готового плана), `render`
(черновик), `still` (кадр), `compositions`, `check`, `final`.

## Порядок работы

1. **Вход.** Краткая инструкция среды отправляет читать `START.md` и вести список задач ядра.
2. **Ролик.** `work list/status` находит существующий, `work create` заводит новый (бриф, площадка, ориентация, режим закадра).
3. **Исходники** (если есть): `run intake` копирует и перекодирует файлы, `run transcribe` готовит слова для субтитров.
4. **Сцены.** `work scenes`: список сцен без времён. Сцена: `kind` (`intro`, `glados_room`, `chamber_sign`, `result_sign`,
   `blueprint`, `custom`), `description` (что на картинке), `say` (фразы), `assets` (материалы), при необходимости `props`, `mood`,
   `emotion`, `hold_ms`. Описание хранится в `plan/scenes.json`, из него сайт показывает «Описание» и «Материалы».
5. **Сборка листа.** `run compose` озвучивает фразы (`core.tts`, файлы `work/public/vo/<id>.wav`), раскладывает сцены встык, ставит
   реплики внутрь своей сцены (400 мс от начала, 300 мс между репликами), растягивает сцену на всю речь и пишет `plan/plan.json`
   только если он прошёл правила `plan_schema.py`. Для роликов с исходным видео `plan.json` пишется вручную через `files`, затем
   `run voiceover`.
6. **Черновик.** `run render`: `--scale 0.5 --crf 28`, файл `renders/preview.mp4`. Remotion Studio на сайте показывает композицию без рендера.
7. **Проверка.** `run check`: правила плана, типы проекта (`tsc`), наличие озвучки и своих компонентов, формат файла, субтитры, зоны.
   Отчёт `<id>/qa/report.md` (читается через `files`, путь начинается с id ролика). Смысл и вид кадров агент проверяет сам по `qa-rubric.md`.
8. **Финал.** `run final`: рендер `--crf 16`, `loudnorm` до -14 LUFS, копия в `output_dir`. Без свежей успешной проверки отказ.

## Правила, которые держит код

- Заставка Aperture Boot идёт первой и полностью (7100 мс) со своим звуком; поверх неё нет реплик, эффектов, призыва и других вставок.
- Без исходного видео реплика целиком лежит внутри вставок: нет картинки под голосом, нет и ролика.
- Если в сценах есть фразы, режим закадра ролика сам становится «голос персонажа».
- Агент пишет только план, сцены, идею, сценарий и свои вставки; исходники, рендеры, отчёты и расшифровка только для чтения.
- `YoutubeSequence` и `ShortsSequence` из библиотеки нужны только как референс для обозначения эксперимента.

## Шаблон Remotion (`template/`)

Одна композиция `Main`. Размер, fps и длительность берёт `calculateMetadata` из `plan.json` (props), перед этим план проверяется
схемой zod `src/schema.ts`, ждут шрифты (`@remotion/fonts`) и подгружаются слова расшифровки (`captions.words_file`, по умолчанию
`transcript/words.json` в `public/`).

### Папки ролика -> шаблон

| В плане | В шаблоне |
|---|---|
| `segments[].source`, например `sources/clip_h264.mp4` | `work/public/media/clip_h264.mp4` (берётся только имя файла) |
| `voiceover[].id` | `work/public/vo/<id>.wav` |
| `captions.words_file` | `work/public/<words_file>`, формат `[{"i","text","start_ms","end_ms","conf"}]` |
| `sfx[].name` | `public/sfx/<name>.wav` (из `resources/<Имя>/montage/assets/sfx`) |
| шрифты | `public/fonts/` (из `resources/<Имя>/montage/assets/fonts`) |

### Компоненты (имена в коде и вид вставки в плане)

| Компонент | Где в плане | Регламент |
|---|---|---|
| `Segments` | `segments` (склейка, `crop.mode` `center`/`face`, `zoom` до 0.04, источник под закадром приглушается) | 9 |
| `Captions` | `captions` (слова по страницам, `source` `transcript`/`voiceover`/`both`, `overrides`, `y_ratio`); только вертикаль | 4.4 |
| `Voiceover` | `voiceover` | 5 |
| `Intro` | `inserts[].kind = intro` (`subtitle`, `wordmark`; 3400–7100 мс, полный клип 7100 со звуком) | 4.1 |
| `Blueprint` | `blueprint` (`title`, `kicker`, `note`, `callouts[]` до 4, `drawing[]`: line/rect/circle/path в долях кадра) | 4.2 |
| `GladosRoom` | `glados_room` (`moods[]`, `speech[]`; при сборке из сцен `speech` ставится по фразам) | 4.3 |
| `ChamberSign`, `ResultSign` | `chamber_sign`, `result_sign` (`big`, `small`, `done`, `total`, `active[]`, `title`; 2,5–3,5 с) | 4.5 |
| `Icon` | внутри табличек: 14 залитых иконок, имена в `src/style/icons.ts` | 4.6 |
| `FootageFrame` | у сегмента `frame: "hud"` и `frame_label` (горизонтальные ролики) | 4.7 |
| `Blackout` | `blackout` (поверх видео, звук и субтитры остаются) | 4.8 |
| `JournalLog` | `custom` с `props.component = "JournalLog"` | 4.9 |
| `Sfx` | `sfx` (и `music`) | 5 |
| `Cta` | `cta` | 8 |

Свои компоненты агент кладёт в `work/src/custom/` и регистрирует в `work/src/custom/index.ts`; в плане это вставка `kind: "custom"`
с `props.component`. Звуковые эффекты сами не включаются: не больше одного в секунду, не на заставке.

### Токены и зоны

Цвета, шрифты, размеры, движение и зоны компонентов: `template/src/style/tokens.ts`. Зоны экспортируются в `template/qa_zones.json`
(`npm run gen:qa-zones`) для проверок: доли кадра по форматам, безопасные зоны площадок, признак «вытесняет субтитры». Схема плана для
сверки с `agent/plan_schema.py`: `template/plan.schema.json` (`npm run gen:schema`).

## Команды Remotion

Из `template/` (или из `jobs/<id>/work/`, где лежит копия):

```
npx tsc --noEmit
npx remotion compositions src/index.ts
npx remotion still  src/index.ts Main out/frame.png --props=../plan/plan.json --frame=90
npx remotion render src/index.ts Main out/preview.mp4 --props=../plan/plan.json --scale=0.5 --crf=28
npx remotion render src/index.ts Main out/final.mp4   --props=../plan/plan.json --crf=16
```

Демо и проверка шаблона:

```
npm run demo:plans      # демо-планы demo/plan.*.json и public/demo/words.json (коммитятся)
npm run demo:media      # синтетические видео и закадр public/media, public/vo (в .gitignore)
npm run demo:stills     # кадры всех компонентов и проверка кириллицы в demo/out/
```

После правок `resources/<Имя>/montage/assets/` выполнить `npm run sync-assets`.

## Субтитры

Субтитры вжигаются только в вертикальные короткие ролики (shorts, reels, telegram). В горизонтальных (youtube) `captions.enabled`
равен `false`, а рядом с финалом кладётся файл `.srt` из тех же слов и копируется в `output_dir`.

## Откуда слова

Для записей RimWorld и всего, где говорит только она, слова берутся из журнала речи: в `sources/` лежат `speech_log.json`,
`recording.json` и `scenario.json`. Время слов внутри реплики делится по длине слов; реплика, которой нет в звуке записи, субтитра
не получает. Результат: `transcript/words.json`, происхождение `transcript/meta.json`. Распознавание живой речи отложено.

## Что проверяет `qa.py`

Формат файла (размер, fps, кодеки, длительность), громкость в финале (-14 ± 1 LUFS, пик ≤ -1 dBTP), страницы субтитров против слов,
зоны вставок и безопасные зоны площадки, OCR текста на кадрах (если RapidOCR установлен), правила ремесла (первый бит, ритм, рваные
склейки, закадр поверх живой речи, концовка). Догадки по тишине, неподвижному кадру и громкости черновика убраны: заставка со
звуком и паузы давали ложные отказы.

# Changelog

Формат: [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/). Запись добавляется после каждой завершённой задачи.

## [Unreleased]

### Changed
- ТЗ 05 (CHR-1, CHR-2, CHR-3, CHR-4): SDK `gits-sdk` (импорт `gits_sdk`, в проверках `PYTHONPATH` на `sdk` worktree GitS). Переменная
  `GLADOS_CORE_URL` переименована в `GITS_CORE_URL`; `agent/envcompat.py` читает новое имя и старое с предупреждением.
- Режим закадра `glados` переименован в `character` (голос персонажа, ядро озвучивает по его карточке). Схема и разбор заданий
  принимают прежнее значение `glados` и читают его как `character` (`manifest.normalize_voiceover`).


### Added (ТЗ 18: политики фокуса, FOC-1)
- Манифест объявляет `policies` и `default_policy`: одна фоновая политика `idle` (по умолчанию). Активной политики пока нет: модуль не умеет `policy.activate` и `focus.release`, и ядро не должно отпускать фокус другого модуля ради монтажа. Когда модуль научится переходам, добавляются `working` (active) и `waiting_for_materials` (блокировка). Тест: `agent/tests/test_manifest.py::test_manifest_declares_background_policy_for_focus`. Требование FOC-1 (ТЗ 18, GitS).

### Changed (ТЗ 15b: одобрение по этапам в панели)
- Кнопка «Одобрить» для всех вопросов убрана. У задачи в ожидании ответа панель (`agent/approval_panel.py`, `Manager.panel_blocks`) показывает подпись этапа и кнопки именно этого этапа:
  - история и план (вопрос `plan_approval`): «Одобрить историю и план» (`approve_plan`, ответ «утвердить»), «Вернуть на доработку» (`rework_plan`, «переделать план»), «Отклонить (переделать историю)» (`reject_plan`, «переделать историю»); ссылки на `story.md` и `plan.md` через `/api/artifact` (раздача из `serve_dirs`);
  - черновик (вопрос `review`): «Одобрить черновик» (`approve_draft`, «готово»), «Вернуть на доработку» (`rework_draft`, «правки»); видео черновика блоком `video`; отклонять черновик нельзя;
  - вопросы агента: `approval` - «Одобрить» и «Отклонить» (`answer_question` с `answer: true|false`), `choice` - по кнопке на вариант (`answer_question`); текстовый вопрос кнопок не имеет, ответ пишется в обзоре или в чате.
- Ответ уходит тем же путём, что и из обзора: `Manager.panel_decide` проверяет, что вопрос ещё открыт (тот же `question_id`) и подходит кнопке этапа, затем `Pipeline.deliver_answer`. Старый вопрос, чужой этап или текст вместо выбора отклоняются, ответ не уходит. Конвейер, режимы приёмки и формулировки вопросов не менялись.
- Ограничение: панель не умеет принимать текст (в блоках дашборда нет поля ввода). «Вернуть на доработку» отвечает выбором, после него конвейер сам спрашивает замечание текстом (в обзоре или в чате). Поле замечания в самой панели потребует нового типа блока в протоколе пространства (SDK и дашборд ядра), это решение владельца.
- Манифест (`agent/manifest.py`): `panel.actions` = `open_studio`, `approve_plan`, `rework_plan`, `reject_plan`, `approve_draft`, `rework_draft`, `answer_question`; `approve` удалён. Обработчики в `agent/main.py` (одна функция на действие, `Manager.panel_decide`). Удалена `affirmative_option` (ей больше не пользуются).
- Тесты: `agent/tests/test_stage_approvals.py` (блоки и кнопки каждого этапа, проверка через `validate_panel_set`, ответ доходит до задачи по каждой кнопке, отказы на устаревшие и чужие нажатия, сквозной ход «вернуть на доработку» → замечание → стадия `plan`), `agent/tests/test_studio_panel.py` (старые тесты на `approve` заменены), `agent/tests/test_manifest.py`.

### Added (ТЗ 15: TAB-4, TAB-9, пространство «Монтаж» в дашборде)
- Remotion Studio по задаче (`agent/studio.py`, TAB-4): инструмент `open_studio` (`npx remotion studio` в рабочей копии `jobs/<id>/work` с `--props=../plan/plan.json`, свободный порт на 127.0.0.1, один Studio на задачу). Studio гаснет вместе с задачей и после 30 минут без обращений (проверка раз в минуту). Новый запуск объявляется событием `montage.launch` (факт, `announce: true`, правило 14); рендера в Studio нет. Тесты `agent/tests/test_studio_panel.py` (процессы подменены).
- Манифест (`agent/manifest.py`, TAB-4): поле `panel` (заголовок «Монтаж», иконка, ссылки «Открыть в Remotion Studio» (инструмент `open_studio` по задаче), «Папка роликов» (`output_dir`), «Папка задач» (`jobs_dir`); кнопки `open_studio` и `approve`). `build_manifest(serve)` добавляет `serve_dirs`: папку задач и `output_dir` на момент запуска (смена `output_dir` требует перезапуска модуля). Ядро раздаёт файлы задач (`final.mp4`, `preview.mp4`, `.srt`, `plan.json`) по этим каталогам.
- Пространство в дашборде (`Manager.panel_blocks`, TAB-9): сводка (в работе, ждут ответа, готово, режим приёмки), таблица роликов с кнопками «Studio» и «Одобрить», согласие с вами, видео последнего готового ролика и начало отчёта проверки. Блоки уходят ядру отдельным циклом раз в 2 секунды (`_panel_loop`), а не из пути состояния конвейера.
- Кнопка «Одобрить» (`Manager.approve`): отвечает на текущий вопрос задачи, если это `approval` или `choice` с вариантом «утвердить»/«готово»/«одобряю» (начало слова); текстовые вопросы кнопкой не закрываются.
- `state` дополнен полями `jobs_dir` и `output_dir` (ядро открывает папки из пространства по `path_from`).
- Тесты: `agent/tests/test_manifest.py` (набор инструментов с `open_studio`, панель и каталоги раздачи).

### Added (шаг 2: конвейер, агент, песочница, контроль качества)
- Конвейер `agent/pipeline.py` (ТЗ 20 §6: MON-7…MON-12; ТЗ 22 CRAFT-2): стадии `intake`, `clarify`, `transcribe`, `story`, `plan`, `plan_approval`, `voiceover`, `build`, `preview`, `review`, `final`, `done`; возвраты `plan_approval` -> `story`/`plan` и `review` -> `build`; `job.update` на каждой смене стадии и состояния, прогресс не чаще раза в 2 с; восстановление с начала текущей стадии после падения (вопрос задаётся снова с тем же `question_id`); одна повторная попытка стадии, затем `failed` с кодом ТЗ 01; пауза `paused_battery` по теме `battery`; отмена с убийством дерева процессов; уборка старых задач без обхода junction `node_modules`.
- Очередь и инструменты (`agent/manager.py`, `agent/main.py`, MON-5, MON-5a, MON-33): `start` (проверка путей, батарейки, места на диске ≥ 5 × размер исходников, шрифтов, ffmpeg, Node), `status`, `revise`, `cancel`; одна задача выполняется, остальные в очереди; `state` со сводкой и согласием автопроверки с человеком; объявление `montage.launch` (`announce: true`) перед финальным рендером; события `montage.done`, `montage.failed`.
- Агент на Claude Agent SDK (`agent/claude_runner.py`, `agent/agent_tools.py`, MON-15, MON-16): клиент на стадию с продолжением сессии (`resume`), системный промпт файлом (`editor_system.md` + `core.persona` + регламент + ремесло), `setting_sources=["project"]`, свои инструменты `ask_user`, `progress`, `synthesize_voice`, `kb_lookup`, `transcript_words`, учёт расхода через `core.claude_usage`. Отступление от MON-15: встроенные инструменты задаются через `tools`, а `allowed_tools` пуст, иначе SDK пропускает их мимо `can_use_tool`.
- Песочница `agent/sandbox.py` (MON-17, MON-18): Read/Glob/Grep внутри задачи, `template/`, `assets/`, `docs/`, `.claude/skills/`; Write/Edit только `work/` (без `node_modules`) и `plan/`; Bash только `ffmpeg`, `ffprobe`, `npx remotion render|still|compositions`, `npx tsc --noEmit`, `node` для скриптов `work/scripts/`; без `; && || | ` $( <`, пути-аргументы внутрь задачи; каждое решение пишется в `log.jsonl`. Таблица из 70 случаев в `test_sandbox.py`.
- План и история (`agent/plan_schema.py`, MON-13, MON-28, CRAFT-2): ручная проверка без новых зависимостей (pydantic в requirements нет), ошибки возвращаются агенту (до 3 попыток), биты без материала и сегменты/вставки без `beat` отвергаются. Допустимые `kind` вставок читаются из `template/plan.schema.json` (новые компоненты шаблона добавляются без правки Python). Тест сверяет проверку со схемой шаблона.
- Субтитры (решение владельца 2026-10-11): вжигаются только в вертикальные короткие ролики (shorts, reels, telegram); у горизонтальных (youtube) `captions.enabled: false`, рядом с финалом `final_<площадка>.srt` из тех же слов (`agent/srt.py`), копия в `output_dir`.
- Субтитры из журнала речи (MON-26a, `agent/transcribe.py`): `speech_log.json` (события `say` и `voice.speech`), `recording.json` (время начала записи и кусков звука), слова по длине в символах, реплика вне звука записи субтитра не получает. Отступление от ТЗ: поле `source: "speech_log"` в `words.json` не пишется (оно в шаблоне означает исходник сегмента), происхождение в `transcript/meta.json`. Живая речь (WhisperX, MON-25…MON-27, MON-29, MON-38) отложена владельцем: `talking_head` завершается с `unavailable`.
- Контроль качества (`agent/qa.py`, `agent/review_mode.py`, MON-QA-1…QA-6, CRAFT-3, CRAFT-4): формат, громкость, `blackdetect`, `freezedetect`, `silencedetect`, страницы субтитров против слов, зоны и безопасные зоны, OCR (RapidOCR, если установлен), проверки ремесла; смысловая проверка отдельным вызовом по рубрике `prompts/qa_rubric.md`; автоисправление не больше двух кругов; отчёт `qa/report.json` и `qa/report.md`; журнал решений `jobs/qa_verdicts.jsonl`; `review_mode` (`human`, `auto_then_human`, `auto` только при ≥ 20 решениях и согласии ≥ 90 %).
- Опыт и справочник (MON-31, MON-32): регламент и ремесло уходят в `montage.style` при подключении и при смене хеша; после правок агент формулирует выводы, они идут в `core.experience.add`.
- Промпты `agent/prompts/`: `editor_system.md` (запреты из `docs/SKILLS_AUDIT.md`: мемные звуки, ElevenLabs, Studio, `basic-captions`, сеть и npm; правила шаблона), `stage_clarify|story|plan|build|review|experience.md`, `qa_rubric.md`. Папка `references/` читается перед `story` (CRAFT-5).
- Тесты (MON-37): конвейер на заглушках (все стадии, возвраты, `auto_final`, восстановление на каждой стадии, батарейка, отмена, уборка), модуль против `FakeCore`, песочница, `plan_schema`, QA на настоящем ffmpeg, журнал речи, intake, рабочая копия шаблона.

### Added (шаг 1)
- Репозиторий `montage-module` (ТЗ 20, шаг 1: MON-1, MON-2, MON-3): структура папок, `pyproject.toml` (ruff, pytest), `requirements.txt`
  и `dev-requirements.txt`, `.gitignore`, `.gitattributes`, `AGENTS.md` (правила и карта модулей), `README.md`, `check.ps1`
  (ruff, pytest, `npx tsc --noEmit`, `npx remotion compositions`; `-Render` рендерит демо).
- Окружение `.venv` на Python 3.12.10: `aiohttp`, `httpx`, `claude-agent-sdk` 0.2.157 (как в GitS), `numpy`, `glados-module`
  (`pip install -e GitS\sdk`).
- Заготовки `agent/main.py` (проверяет манифест, конвейера ещё нет), `agent/manifest.py` (полный манифест по ТЗ 20 раздел 5:
  инструменты `start`, `status`, `revise`, `cancel`, настройки, знания, `announce` у `start`), `agent/media.py` (`find_ffmpeg`,
  `find_ffprobe`, функция скопирована из GitS). Тесты: манифест валиден по контракту, поиск ffmpeg.
- `docs/APERTURE_STYLE.md` (ТЗ 21, STY-1): регламент монтажа в стиле Aperture Science, текст из ТЗ 21 без сокращений. Единственный
  источник стиля для агента-монтажёра.
- `docs/EDITING_CRAFT.md` (ТЗ 22, CRAFT-1): руководство «Ремесло монтажа» (история, ритм, склейка, звук, чек-лист), текст из ТЗ 22
  без сокращений.
- `.claude/skills/` (ТЗ 20, MON-4): закреплённые копии 12 скиллов `remotion-*` (версия 4.0.534, источник `remotion-dev/skills`,
  установлены 2026-10-09), байт в байт с `C:\Users\coffe\.agents\skills`. Отчёт о том, что в них исполняемого и где они
  расходятся с регламентом (звуки remotion.media, ElevenLabs, Studio): `docs/SKILLS_AUDIT.md`. Источник и дата в `README.md`.
- Шрифты IBM Plex (ТЗ 21, STY-2; ТЗ 20 MON-24): `assets/fonts/` из Google Fonts (SIL OFL 1.1, `OFL.txt`), `fonts.json`, `README.md`. Проверка кириллицы
  нашла, что статичной Plex Sans Condensed с русскими буквами в Google Fonts нет (686 глифов, только латиница): три начертания Plex Sans
  (Condensed SemiBold, Condensed Bold, Medium) сделаны из вариативного файла `IBMPlexSans[wdth,wght]` (оси wght и wdth 75) под именами из
  регламента, начертание задают weight и stretch. Тест `test_fonts.py` проверяет глифы строки из ТЗ 21 по `cmap`.
- Звуки (MON-23): `assets/sfx/generate.py` (numpy + wave, детерминированный синтез): `chime_up`, `chime_down`, `beep`, `tick`, `whoosh`,
  `buzz_error`, `elevator_ding`, 44,1 кГц моно, пик -3 dBFS; готовые `.wav` в репозитории. Тест `test_sfx_generate.py`: длительность,
  частоты, детерминизм, совпадение файлов с генератором.
- Пиктограммы (STY-3): `assets/pictograms/*.svg`, 12 знаков из регламента 4.6 (`cube`, `button`, `person`, `hazard_water`, `laser`, `exit`,
  `warning`, `clock`, `gear`, `eye`, `speaker`, `camera`), свои, контур 6 % размера, цвет `currentColor`. Тест `test_pictograms.py`.
- Шаблон Remotion `template/` (ТЗ 20, MON-19, MON-20, MON-21, MON-22): точные версии без `^` (Remotion и все `@remotion/*` 4.0.534, zod 4.5.4,
  React 19.2.8, TypeScript 5.9.3; сверх списка ТЗ добавлен `@remotion/media`: его рекомендуют скиллы для `Video` и `Audio`), `package-lock.json`.
  Композиция `Main` с `calculateMetadata`: проверка плана схемой zod, ожидание шрифтов (`@remotion/fonts`), подгрузка слов расшифровки, размер,
  fps и длительность из plan.json. Компоненты: `Segments`, `Captions`, `Voiceover`, `ChamberCard`, `HazardBanner`, `PixelTransition`, `TestResult`,
  `LowerThird`, `Blackout`, `Sfx`, `Cta`, `Pictogram`; вставки `custom` через реестр `src/custom/` (`JournalLog` по регламенту 4.9; служебные
  `FontSpecimen`, `PictogramSheet`).
- Схема плана `template/src/schema.ts` (zod) по ТЗ 20 раздел 7 плюс `beat` у сегментов и вставок (ТЗ 22 CRAFT-2, обязательное) и необязательные
  поля `intent`, `zoom`, `duck`, `captions.source`, `captions.words_file`, `overrides[].highlight`, `blackout[].opacity`, `sfx[].volume`, `music`.
  Скрипты: `scripts/validate-plan.mjs` (проверка плана), `scripts/export-schema.mjs` (`plan.schema.json` для сверки с `agent/plan_schema.py`).
- Токены стиля (STY-4): `template/src/style/tokens.ts` (цвета из регламента, шрифты, размеры, движение, зоны); экспорт зон компонентов в
  `template/qa_zones.json` (`npm run gen:qa-zones`) для проверок качества ТЗ 20 MON-QA-1. Тест сверяет цвета токенов с таблицей регламента.
- STY-5: в коде шаблона нет русских констант (тест `test_template.py`); тексты на экране приходят из плана.
- Демо (MON-22, STY-6): `template/demo/plan.{vertical,horizontal,square}.json` (24 с, все компоненты), `plan.vertical.short.json` (9 с без CTA),
  `plan.cyrillic.json`, `plan.pictograms.json`; генераторы `make-demo-plans.mjs`, `make-demo-media.mjs` (синтетические исходники через ffmpeg),
  `render-demo-stills.mjs` (кадры через `npx remotion still` в `demo/out/`). Проверка кириллицы (STY-2) отрендерена и просмотрена.
- `docs/PIPELINE.md`: как устроен конвейер, соответствие плана и шаблона, команды рендера и проверки.
- `check.ps1` проходит: ruff, pytest (59 тестов), `npx tsc --noEmit`, `npx remotion compositions`; `-Render` добавляет демо-рендер.

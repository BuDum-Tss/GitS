# Скиллы Remotion: что в них исполняемого (MON-4)

Источник: `remotion-dev/skills`, версия скиллов 4.0.534, установлены в `C:\Users\coffe\.agents\skills` (ссылки из
`C:\Users\coffe\.claude\skills`) 2026-10-09 по локальному времени (запись в `.skill-lock.json`: 2026-10-08 23:34 UTC).
Скопированы сюда 2026-10-11 без изменений (`diff -rq` с источником пуст). Всего 279 файлов, 3 МБ.

Проверено 2026-10-11: прочитаны все 12 `SKILL.md`, затем поиск по всем файлам скиллов: команды, скрипты `.mjs/.ts/.tsx`,
адреса, переменные окружения.

## Состав

| Скилл | Что внутри | Исполняемое |
|---|---|---|
| `remotion-best-practices` | маршрутизатор; внутри полные копии остальных скиллов в подпапках (`REFERENCE.md` вместо `SKILL.md`) | нет собственных скриптов; дублирует остальные |
| `remotion-captions` | расшифровка, показ и импорт субтитров | команды `npx remotion add ...` (установка npm-пакетов); совет скопировать `basic-captions.tsx` с remotion.dev |
| `remotion-create` | создание проекта и композиции, раскладка кадра, Tailwind | `npx create-video@latest --yes --blank ...`, `npm i` |
| `remotion-docs` | поиск по документации | HTTP POST на Algolia с публичным поисковым ключом, затем загрузка страниц `remotion.dev/docs/*.md` |
| `remotion-interactivity` | разметка, редактируемая в Studio (`Interactive.*`) | `bunx remotion add @remotion/paths` |
| `remotion-maps` | карты (Mapbox, MapLibre, MapTiler, Cesium, статичная) | два скрипта Node: `techniques/cesium/scripts/prep-cesium-path.mjs`, `techniques/maptiler/scripts/prep-geo.mjs` (читают и пишут локальные файлы через `fs`, сети, `child_process` и `eval` нет); шаблонные `.tsx/.ts` с чтением ключей из `process.env` (`REMOTION_MAPBOX_TOKEN`, `REMOTION_MAPTILER_KEY`, `REMOTION_GOOGLE_MAPS_API_KEY`) и обращениями к api.maptiler.com, tile.googleapis.com, unpkg.com; `npm i mapbox-gl maplibre-gl @turf/turf` |
| `remotion-markup` | 31 справка по разметке: аудио, видео, шрифты, переходы, эффекты, `ffmpeg`, голос, субтитры | `npx remotion add <пакет>`; `npx remotion ffmpeg|ffprobe` (в том числе `silencedetect`, `loudnorm`); `voiceover.md`: скрипт `node --strip-types generate-voiceover.ts` с вызовом ElevenLabs (нужен `ELEVENLABS_API_KEY`); `sfx.md`: список звуков на `remotion.media` |
| `remotion-multimedia` | Mediabunny: длительность, размеры | нет |
| `remotion-render` | `npx remotion render|still`, прозрачное видео | команды рендера |
| `remotion-saas` | приложения на Remotion, Player, рендер на Lambda/Vercel | ссылки на шаблоны GitHub |
| `remotion-studio` | `npx remotion studio` | запуск долгоживущего сервера Studio |
| `remotion-upgrade` | обновление Remotion | `npx remotion upgrade`, `npx skills update ... --yes` (скачивает и перезаписывает скиллы) |

Собственных исполняемых файлов, которые запускаются сами при загрузке скилла, нет. Скилл это инструкции, которым
следует агент: все перечисленные команды и сетевые обращения агент выполняет только через свои инструменты, то есть через
песочницу `can_use_tool` (ТЗ 20 MON-17).

## Что песочница MON-17 из этого запретит, и на что нужно указание в `editor_system.md`

Запрещены политикой песочницы (агент получит отказ и должен искать другой путь): `npm`, `npx remotion add|upgrade`,
`npx create-video`, `npx skills`, `bunx`, `curl`, `wget`, любые обращения к сети (`WebFetch`/`WebSearch` отключены),
`node --strip-types generate-voiceover.ts` (вне `work/scripts/` и за ключом). Поэтому в шаблоне уже стоят все нужные
пакеты (`@remotion/media`, `@remotion/captions`, `@remotion/fonts`, `@remotion/media-utils`, `@remotion/zod-types`), а
`remotion-docs` для монтажёра бесполезен (нет сети).

Места, где скиллы расходятся с нашим регламентом. В `prompts/editor_system.md` (шаг 2) должно быть сказано, что побеждают
`APERTURE_STYLE.md`, `EDITING_CRAFT.md` и устройство `template/`:

1. `sfx.md` перечисляет звуки `remotion.media` (мемные, игровые и киношные: `minecraft-hurt`, `windows-xp-error`,
   `wilhelm-scream`, `price-is-right-fail` и др.). Нам нельзя: только синтезированные `assets/sfx/` (ТЗ 21 §5, права Valve
   и чужих правообладателей).
2. `voiceover.md` предлагает ElevenLabs. У нас закадр только через инструмент `synthesize_voice` (`core.tts`, ТЗ 12).
3. `remotion-best-practices`, `remotion-studio`, `remotion-create`: «открой Studio» и «не рендери без явной просьбы».
   Монтажёр Studio не открывает (нет браузера и долгоживущих процессов); он проверяет `npx remotion still` и смотрит кадры.
4. `remotion-captions/display-captions.md` предлагает скопировать `basic-captions.tsx` с remotion.dev. У нас субтитры это
   компонент `Captions` шаблона (`createTikTokStyleCaptions`, ТЗ 21 §4.1).
5. `remotion-interactivity` и `remotion-markup` требуют `Interactive.withSchema`, `premountFor`, подключения компонентов как
   «связанные композиции». Шаблон их не использует (одна композиция `Main`, все параметры в `plan.json`), и для рендера
   без Studio это не нужно.
6. `remotion-create`: `npx create-video`, новая папка проекта. У нас проект уже есть (`work/` это копия `template/`).
7. `remotion-maps`: карты и ключи. Для роликов этого модуля не используются; ключей у модуля нет.

## Рекомендация по составу

Для монтажёра нужны `remotion-best-practices` (маршрутизатор), `remotion-markup`, `remotion-captions`, `remotion-render`,
`remotion-multimedia`. Остальные (`remotion-saas`, `remotion-maps`, `remotion-upgrade`, `remotion-studio`,
`remotion-create`, `remotion-interactivity`, `remotion-docs`) в песочнице почти бесполезны, но копии закреплены целиком,
как требует MON-4; сузить состав можно отдельным решением владельца.

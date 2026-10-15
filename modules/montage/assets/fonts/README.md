# Шрифты

IBM Plex, лицензия SIL Open Font License 1.1 (`OFL.txt`, Copyright 2017 IBM Corp., зарезервированное имя «Plex»).
Источник: репозиторий `google/fonts` (Google Fonts), каталоги `ofl/ibmplexsans` и `ofl/ibmplexmono`; загружено 2026-10-11
с разрешения владельца. Файлы не изменялись.

| Файл (имя из регламента, ТЗ 21 §2) | Что это на самом деле | Начертание в шаблоне |
|---|---|---|
| `IBMPlexSansCondensed-SemiBold.ttf` | вариативная IBM Plex Sans | `wght 600`, `wdth 75` (узкая) |
| `IBMPlexSansCondensed-Bold.ttf` | та же вариативная IBM Plex Sans | `wght 700`, `wdth 75` |
| `IBMPlexSans-Medium.ttf` | та же вариативная IBM Plex Sans | `wght 500`, `wdth 100` |
| `IBMPlexMono-Regular.ttf` | статичная IBM Plex Mono | `wght 400` |

## Находка STY-2: в Google Fonts у Plex Sans Condensed нет кириллицы

Проверка таблицы символов (`cmap`) показала: статичные `IBMPlexSansCondensed-SemiBold.ttf` и `IBMPlexSansCondensed-Bold.ttf`
из Google Fonts (686 глифов) содержат только латиницу. Не хватает всех русских букв проверочной строки «Испытание № 07.
ЙЁЖЩЪЫЭЮЯ ИЙ ийёжщ — «ёлка»» (24 разные буквы). Статичной `IBMPlexSans-Medium.ttf` в Google Fonts больше нет: семейство
выложено одним вариативным файлом `IBMPlexSans[wdth,wght].ttf` (оси `wght` 100-700 и `wdth` 75-100, 1025 глифов,
кириллица есть). Ось `wdth 75` это то же узкое начертание, что Plex Sans Condensed, поэтому:

- настоящий вариативный файл положен под три имени из регламента, чтобы текст регламента, таблица шрифтов и проверка
  «есть ли файл» (MON-24, правило «СТОП: проверка шрифтов») работали без правок;
- начертание выбирают CSS `font-weight` и `font-stretch` через `loadFont({weight, stretch})` (см. `fonts.json` и
  `template/src/style/tokens.ts`);
- файлы побайтно совпадают (sha256 одинаковый), в git лежит один блоб;
- `IBMPlexMono-Regular.ttf` содержит кириллицу и оставлен как есть.

Тест `agent/tests/test_fonts.py` проверяет покрытие кириллицы по `cmap`, наличие осей и совпадение с `template/public/fonts/`.
Визуальная проверка (STY-2): кадры `template/demo/out/cyrillic_*.png`.

Это замена шрифта в рамках той же семьи IBM Plex; решение о ней остаётся за владельцем (ТЗ 21 §2 и STY-2). Если нужна
отдельная статичная Plex Sans Condensed с кириллицей, её нужно скачать из апстрима IBM (`github.com/IBM/plex`), а не из
Google Fonts; для этого потребуется разрешение.

## Копии

`template/public/fonts/` содержит те же файлы (шаблон копируется в `jobs/<id>/work/`, ему нужны свои `public/fonts`).
Обновление копий: `node template/scripts/sync-assets.mjs`.

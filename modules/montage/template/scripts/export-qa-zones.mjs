// Экспорт зон компонентов в qa_zones.json (STY-4, ТЗ 20 MON-QA-1): проверки качества берут зоны отсюда, а не из кода.
// Источник один: src/style/tokens.ts (те же доли кадра использует расстановка компонентов). Запуск: node scripts/export-qa-zones.mjs [файл]
import {writeFileSync} from 'node:fs';
import {dirname, join, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const T = await import(new URL('../src/style/tokens.ts', import.meta.url).href);

const round = (v) => Math.round(v * 10000) / 10000;
const rect = (r) => ({x: round(r.x), y: round(r.y), w: round(r.w), h: round(r.h)});

const out = {
	version: 1,
	source: 'template/src/style/tokens.ts',
	note: 'Прямоугольники в долях кадра (x, y, w, h от левого верхнего угла). avoid_captions: зона вытесняет субтитры.',
	frames: T.formats,
	safe_zones: T.safe,
	captions: {
		default_y_ratio: T.captions.defaultYRatio,
		font_px: T.captions.fontPx,
		max_chars: T.captions.maxChars,
		max_width_ratio: T.captions.maxWidthRatio,
		height_ratio: Object.fromEntries(
			Object.entries(T.formats).map(([k, f]) => [k, round(T.captionHeightRatio(f.width, f.height))]),
		),
		zone_at_default: Object.fromEntries(
			Object.entries(T.formats).map(([k, f]) => [k, rect(T.captionZone(f.width, f.height, T.captions.defaultYRatio[k]))]),
		),
	},
	components: Object.fromEntries(
		Object.entries(T.zones).map(([kind, byFormat]) => [
			kind,
			{
				avoid_captions: T.captionAvoidKinds.includes(kind),
				...Object.fromEntries(Object.entries(byFormat).map(([k, r]) => [k, rect(r)])),
			},
		]),
	),
};
const target = process.argv[2] ?? join(root, 'qa_zones.json');
writeFileSync(target, JSON.stringify(out, null, 2) + '\n');
console.log(target + ' записан');

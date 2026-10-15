// Демо-кадры каждого компонента (STY-6) и проверка кириллицы (STY-2) через `npx remotion still`.
// Результат в demo/out/ (в .gitignore). Запуск: node demo/render-demo-stills.mjs [часть имени кадра ...]
import {spawnSync} from 'node:child_process';
import {mkdirSync} from 'node:fs';
import {dirname, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
mkdirSync(resolve(root, 'demo', 'out'), {recursive: true});

const sec = (s) => Math.round(s * 30); // кадр при 30 fps
// [имя файла, план, секунда ролика]
const stills = [
	['cyrillic_all_faces', 'cyrillic', 0.8],
	['pictograms', 'pictograms', 0.5],
	['vertical_chamber_card', 'vertical', 1.4],
	['vertical_lower_third_captions', 'vertical', 5.0],
	['vertical_hazard_banner', 'vertical', 8.0],
	['vertical_captions_voiceover_mark', 'vertical', 11.6],
	['vertical_pixel_transition', 'vertical', 12.7],
	['vertical_blackout_journal', 'vertical', 14.6],
	['vertical_captions_highlight', 'vertical', 17.0],
	['vertical_test_result_fail', 'vertical', 19.8],
	['vertical_cta', 'vertical', 23.0],
	['horizontal_chamber_card', 'horizontal', 1.4],
	['horizontal_hazard_banner', 'horizontal', 8.0],
	['horizontal_test_result_fail', 'horizontal', 19.8],
	['horizontal_cta', 'horizontal', 23.0],
	['square_chamber_card', 'square', 1.4],
	['square_hazard_banner', 'square', 8.0],
	['square_test_result_fail', 'square', 19.8],
	['square_cta', 'square', 23.0],
];

const filters = process.argv.slice(2);
for (const [name, plan, t] of stills) {
	if (filters.length && !filters.some((f) => name.includes(f))) continue;
	const out = `demo/out/${name}.png`;
	const r = spawnSync('npx', ['remotion', 'still', 'src/index.ts', 'Main', out, `--props=demo/plan.${plan}.json`, `--frame=${sec(t)}`], {
		cwd: root,
		encoding: 'utf8',
		shell: true,
	});
	if (r.status !== 0) {
		console.error(`${name}: ошибка\n${r.stdout}\n${r.stderr}`);
		process.exit(1);
	}
	console.log('готово', out);
}

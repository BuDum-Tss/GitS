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
	// шортс: интро (ирис в пути и собран), комната, схема, игра с субтитрами, итог, призыв
	['vertical_intro_mid', 'vertical', 1.3],
	['vertical_intro_final', 'vertical', 3.7],
	['vertical_room', 'vertical', 6.0],
	['vertical_blueprint', 'vertical', 13.5],
	['vertical_game_captions', 'vertical', 17.0],
	['vertical_game_captions_voice', 'vertical', 5.6],
	['vertical_result_sign', 'vertical', 27.5],
	['vertical_cta', 'vertical', 30.5],
	// YouTube: интро, отбивка акта, комната, схема, игра в рамке HUD (субтитров нет), итог, призыв
	['horizontal_intro_final', 'horizontal', 3.7],
	['horizontal_chamber_sign', 'horizontal', 5.5],
	['horizontal_room', 'horizontal', 9.6],
	['horizontal_blueprint', 'horizontal', 16.5],
	['horizontal_game_hud', 'horizontal', 22.0],
	['horizontal_result_sign', 'horizontal', 28.5],
	['horizontal_cta', 'horizontal', 31.5],
	// квадрат: без субтитров
	['square_intro_final', 'square', 3.7],
	['square_room', 'square', 6.0],
	['square_journal_blackout', 'square', 11.0],
	['square_blueprint', 'square', 18.5],
	['square_result_sign', 'square', 22.0],
];

const filters = process.argv.slice(2);
for (const [name, plan, t] of stills) {
	if (filters.length && !filters.some((f) => name.includes(f))) continue;
	const out = `demo/out/${name}.png`;
	const r = spawnSync('npx', ['remotion', 'still', 'src/index.ts', 'Main', out, `--props=demo/plan.${plan}.json`, `--frame=${sec(t)}`, '--timeout=120000'], {
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

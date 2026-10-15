// Копирует ресурсы репозитория в шаблон: шрифты и звуки в public/, пиктограммы в src/style/pictograms.generated.ts.
// Запуск: node scripts/sync-assets.mjs  (из template/). Результат коммитится: шаблон копируется в jobs/<id>/work/.
import {copyFileSync, mkdirSync, readFileSync, readdirSync, writeFileSync} from 'node:fs';
import {dirname, join, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const tpl = resolve(here, '..');
const assets = resolve(tpl, '..', 'assets');

function copyDir(from, to, filter) {
	mkdirSync(to, {recursive: true});
	for (const name of readdirSync(from)) {
		if (filter(name)) copyFileSync(join(from, name), join(to, name));
	}
}

copyDir(join(assets, 'fonts'), join(tpl, 'public', 'fonts'), (n) => n.endsWith('.ttf') || n === 'OFL.txt');
copyDir(join(assets, 'sfx'), join(tpl, 'public', 'sfx'), (n) => n.endsWith('.wav'));

// пиктограммы: внутренность <svg> как строка, цвет задаёт компонент через currentColor
const pictDir = join(assets, 'pictograms');
const entries = [];
for (const file of readdirSync(pictDir).filter((n) => n.endsWith('.svg')).sort()) {
	const text = readFileSync(join(pictDir, file), 'utf8');
	const m = text.match(/<svg[^>]*>([\s\S]*?)<\/svg>/);
	const stroke = text.match(/stroke-width="([\d.]+)"/);
	if (!m || !stroke) throw new Error(`не разобрать ${file}`);
	const inner = m[1].replace(/\s*\n\s*/g, '').trim();
	entries.push(`\t${file.replace('.svg', '')}: {strokeWidth: ${stroke[1]}, markup: ${JSON.stringify(inner)}},`);
}
const out = `// Создано scripts/sync-assets.mjs из assets/pictograms/*.svg. Не править вручную.
export const pictogramData = {
${entries.join('\n')}
} as const;

export type PictogramName = keyof typeof pictogramData;
export const pictogramNames = Object.keys(pictogramData) as PictogramName[];
`;
writeFileSync(join(tpl, 'src', 'style', 'pictograms.generated.ts'), out);
console.log(`ресурсы синхронизированы: ${entries.length} пиктограмм`);

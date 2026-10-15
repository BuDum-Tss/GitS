// Проверка plan.json схемой шаблона (та же схема, что в calculateMetadata). Запуск: node scripts/validate-plan.mjs plan.json [...]
// Код выхода 0: все планы верны; иначе 1, ошибки печатаются JSON-ом в stdout (для возврата агенту).
import {readFileSync} from 'node:fs';

const S = await import(new URL('../src/schema.ts', import.meta.url).href);
const files = process.argv.slice(2);
if (!files.length) {
	console.error('usage: node scripts/validate-plan.mjs plan.json [...]');
	process.exit(2);
}
let bad = 0;
const report = {};
for (const f of files) {
	const r = S.mainPropsSchema.safeParse(JSON.parse(readFileSync(f, 'utf8')));
	report[f] = r.success ? 'ok' : r.error.issues.map((i) => ({path: i.path.join('.'), message: i.message}));
	if (!r.success) bad++;
}
console.log(JSON.stringify(report, null, 2));
process.exit(bad ? 1 : 0);

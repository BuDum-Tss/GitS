// JSON Schema плана из zod-схемы (src/schema.ts): по нему agent/plan_schema.py сверяется с шаблоном (ТЗ 20 MON-20).
// Запуск: node scripts/export-schema.mjs [файл]  ->  plan.schema.json
import {writeFileSync} from 'node:fs';
import {dirname, join, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import {z} from 'zod';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const S = await import(new URL('../src/schema.ts', import.meta.url).href);
const json = z.toJSONSchema(S.mainPropsSchema, {io: 'input', unrepresentable: 'any'});
const target = process.argv[2] ?? join(root, 'plan.schema.json');
writeFileSync(target, JSON.stringify(json, null, 2) + '\n');
console.log(target + ' записан');

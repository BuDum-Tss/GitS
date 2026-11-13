// Имена иконок табличек (ТЗ 23, SC-2): 14 залитых иконок дизайн-системы v2. Только данные, без React: имена читает и схема плана.
// Порядок первых двенадцати это сетка 6x2 на табличке; «button» и «subject» занимают свободные места, если их выбрали.
export const iconNames = [
	'colonists',
	'build',
	'sleep',
	'research',
	'fire',
	'raid',
	'loss',
	'animals',
	'cold',
	'night',
	'health',
	'cube',
	'button',
	'subject',
] as const;
export type IconName = (typeof iconNames)[number];

export const signGridIcons: readonly IconName[] = iconNames.slice(0, 12);

// Какие иконки показывает табличка: стандартные двенадцать; выбранные из остальных (button, subject) заменяют последние
// невыбранные места в сетке. Возвращает пары (имя, включена).
export function signIcons(active: readonly IconName[]): {name: IconName; on: boolean}[] {
	const on = new Set(active);
	const cells = signGridIcons.map((name) => ({name, on: on.has(name)}));
	const extras = active.filter((n) => !signGridIcons.includes(n));
	for (const extra of extras) {
		let idx = -1;
		for (let i = cells.length - 1; i >= 0; i--) {
			if (!cells[i].on) {
				idx = i;
				break;
			}
		}
		if (idx < 0) break;
		cells[idx] = {name: extra, on: true};
	}
	return cells;
}

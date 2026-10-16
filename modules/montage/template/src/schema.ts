// Схема plan.json (zod): ТЗ 20 раздел 7 (MON-13, MON-20) + поле `beat` из ТЗ 22 (CRAFT-2) + сцены дизайн-системы v2 (ТЗ 23, SC-3). Это входные props композиции Main.
// Единственный источник правды о форме плана: scripts/export-schema.mjs пишет из неё plan.schema.json, по которому
// agent/plan_schema.py проверяется тестом на совпадение (шаг 2). Файл читает и Node, поэтому импорты только с .ts.
import {z} from 'zod';
import {iconNames} from './style/icons.ts';
import {durations, limits, room} from './style/tokens.ts';

export const insertKinds = ['intro', 'blueprint', 'glados_room', 'chamber_sign', 'result_sign', 'custom'] as const;
export const sfxNames = ['chime_up', 'chime_down', 'beep', 'tick', 'whoosh', 'buzz_error', 'elevator_ding'] as const;
export const platforms = ['shorts', 'reels', 'telegram', 'youtube', 'none'] as const;
export const beatRoles = ['hook', 'setup', 'inciting', 'rise', 'turn', 'climax', 'payoff', 'button', 'cta'] as const;

const ms = z.number().int().min(0);
const text = (max: number) => z.string().min(1).max(max);
const beatId = z.string().min(1); // id бита из story.json (CRAFT-2): бит без материала план не пройдёт (проверяет plan_schema.py)

export const segmentSchema = z.strictObject({
	id: z.string().min(1),
	source: z.string().min(1), // путь внутри папки задачи, например sources/cube_h264.mp4
	in_ms: ms,
	out_ms: ms,
	at_ms: ms,
	volume: z.number().min(0).max(2).default(1),
	crop: z.strictObject({mode: z.enum(['center', 'face'])}).default({mode: 'center'}), // face пока равен center
	zoom: z.number().min(0).max(0.04).default(0), // медленный наезд за сцену, не больше 4 %
	beat: beatId,
	intent: z.enum(['jump_cut']).optional(), // CRAFT-4: осознанная перебивка
	frame: z.enum(['hud']).optional(), // ТЗ 23, SC-3: кадр игры в рамке HUD (горизонтальные ролики)
	frame_label: z.string().min(1).max(limits.hudLabel).optional(), // подпись в верхней планке рамки HUD
});

export const captionsSchema = z.strictObject({
	enabled: z.boolean(),
	source: z.enum(['transcript', 'voiceover', 'both']).default('transcript'), // voiceover: по тексту закадра
	style: z.literal('aperture_default').default('aperture_default'),
	position: z.strictObject({y_ratio: z.number().min(0.05).max(0.95)}).optional(), // по умолчанию из токенов формата
	overrides: z
		.array(
			z.strictObject({
				word_index: z.number().int().min(0), // поле i из words.json
				text: z.string().optional(), // исправленный текст слова (времена менять нельзя)
				highlight: z.boolean().optional(), // выделить слово цветом portalOrange (не чаще раза в 3 с)
			}),
		)
		.default([]),
	words_file: z.string().default('transcript/words.json'), // относительно public/
});

export const voiceoverSchema = z.strictObject({
	id: z.string().min(1),
	at_ms: ms,
	text: text(limits.voiceover),
	emotion: z.string().default('neutral'),
	tts_path: z.string().nullable().default(null), // по умолчанию public/vo/<id>.wav
	duration_ms: ms.nullable().default(null), // заполняет конвейер после синтеза
	beat: beatId.optional(),
	duck: z.boolean().default(false), // CRAFT-4: закадр намеренно идёт поверх живой речи
});

const insertBase = {id: z.string().min(1), at_ms: ms, beat: beatId};
const dur = (min: number, max?: number) => (max === undefined ? z.number().int().min(min) : z.number().int().min(min).max(max));

const frac = z.number().min(-0.1).max(1.1); // доля кадра; чуть за краем допустима
const weight = z.enum(['main', 'thin', 'wall']).default('main'); // линия 3 px, вспомогательная 1,6 px, стена 10 px

// Рисунок чертежа: простые SVG-примитивы в долях кадра (x, w, r: от ширины; y, h: от высоты; радиус круга от ширины).
// Путь: абсолютные команды M L H V C S Q T A Z, координаты в долях кадра (у A: радиусы и точка, флаги 0/1).
export const drawingPrimitive = z.discriminatedUnion('type', [
	z.strictObject({type: z.literal('line'), x1: frac, y1: frac, x2: frac, y2: frac, weight}),
	z.strictObject({type: z.literal('rect'), x: frac, y: frac, w: z.number().min(0).max(1.2), h: z.number().min(0).max(1.2), weight}),
	z.strictObject({type: z.literal('circle'), cx: frac, cy: frac, r: z.number().min(0).max(1), weight}),
	z.strictObject({type: z.literal('path'), d: z.string().min(2).max(600).regex(/^[MLHVCSQTAZ0-9eE+.,\s-]+$/), weight}),
]);

const iconRef = z.enum(iconNames);
const signProps = {
	big: text(limits.signBig), // огромное число или слово итога (sign-number)
	small: text(limits.signSmall), // строка-дробь (sign-small)
	done: z.number().int().min(0).max(limits.ticks), // сколько штрихов прогресса пройдено
	total: z.number().int().min(1).max(limits.ticks).default(19),
	active: z.array(iconRef).max(12).default([]), // какие иконки включены
	title: text(limits.signTitle), // заголовок капсом (sign-title)
};
const range = {from_ms: ms, to_ms: ms};

export const insertSchema = z.discriminatedUnion('kind', [
	z.strictObject({
		...insertBase,
		dur_ms: dur(durations.intro - 600, durations.intro + 2000),
		kind: z.literal('intro'),
		props: z.strictObject({subtitle: text(limits.subtitle), wordmark: text(limits.wordmark).default('APERTURE SCIENCE')}),
	}),
	z.strictObject({
		...insertBase,
		dur_ms: dur(durations.bpDraw + 500),
		kind: z.literal('blueprint'),
		props: z.strictObject({
			title: text(limits.bpTitle),
			kicker: text(limits.bpKicker),
			note: text(limits.bpNote),
			callouts: z.array(z.strictObject({x: z.number().min(0).max(1), y: z.number().min(0).max(1), label: text(limits.bpLabel)})).max(limits.callouts).default([]),
			drawing: z.array(drawingPrimitive).min(1).max(limits.drawing),
		}),
	}),
	z.strictObject({
		...insertBase,
		dur_ms: dur(1),
		kind: z.literal('glados_room'),
		props: z.strictObject({
			moods: z.array(z.strictObject({...range, mood: z.enum(room.moods)})).max(60).default([]), // время ролика, как at_ms
			speech: z.array(z.strictObject(range)).max(200).default([]), // отрезки её речи по журналу (время ролика); голос закадра берётся из voiceover
		}),
	}),
	z.strictObject({
		...insertBase,
		dur_ms: dur(durations.bumperHold.min, durations.bumperHold.max),
		kind: z.literal('chamber_sign'),
		props: z.strictObject(signProps),
	}),
	z.strictObject({
		...insertBase,
		dur_ms: dur(durations.bumperHold.min, durations.bumperHold.max),
		kind: z.literal('result_sign'),
		props: z.strictObject(signProps),
	}),
	z.strictObject({
		...insertBase,
		dur_ms: dur(1),
		kind: z.literal('custom'),
		props: z.looseObject({component: z.string().min(1)}), // компонент из work/src/custom/index.ts
	}),
]);

export const planSchema = z.strictObject({
	version: z.literal(1),
	format: z.strictObject({
		width: z.number().int().min(16),
		height: z.number().int().min(16),
		fps: z.number().int().min(1).max(60),
	}),
	duration_ms: z.number().int().min(100),
	title: z.string(),
	segments: z.array(segmentSchema),
	captions: captionsSchema,
	voiceover: z.array(voiceoverSchema).default([]),
	inserts: z.array(insertSchema).default([]),
	blackout: z.array(z.strictObject({from_ms: ms, to_ms: ms, opacity: z.number().min(0).max(1).default(1)})).default([]),
	sfx: z.array(z.strictObject({at_ms: ms, name: z.enum(sfxNames), volume: z.number().min(0).max(1).optional()})).default([]),
	music: z.strictObject({src: z.string().min(1), volume: z.number().min(0).max(1).default(0.12)}).nullable().default(null),
	cta: z
		.strictObject({platform: z.enum(platforms), at_ms: ms, text: text(limits.cta), beat: beatId.optional()})
		.nullable()
		.default(null),
});

export const wordSchema = z.strictObject({
	i: z.number().int().min(0),
	text: z.string(),
	start_ms: ms,
	end_ms: ms,
	conf: z.number().nullable().optional(),
	source: z.string().optional(), // если задан, слово относится только к сегментам этого source
});

// props композиции Main: план + слова расшифровки, которые calculateMetadata подгружает из words_file
export const mainPropsSchema = planSchema.extend({words: z.array(wordSchema).optional()});

export type Plan = z.infer<typeof planSchema>;
export type PlanInput = z.input<typeof planSchema>;
export type Segment = z.infer<typeof segmentSchema>;
export type Insert = z.infer<typeof insertSchema>;
export type InsertOf<K extends Insert['kind']> = Extract<Insert, {kind: K}>;
export type DrawingPrimitive = z.infer<typeof drawingPrimitive>;
export type Voiceover = z.infer<typeof voiceoverSchema>;
export type Word = z.infer<typeof wordSchema>;
export type MainProps = z.infer<typeof mainPropsSchema>;

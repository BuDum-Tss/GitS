// Схема plan.json (zod): ТЗ 20 раздел 7 (MON-13, MON-20) + поле `beat` из ТЗ 22 (CRAFT-2). Это входные props композиции Main.
// Единственный источник правды о форме плана: scripts/export-schema.mjs пишет из неё plan.schema.json, по которому
// agent/plan_schema.py проверяется тестом на совпадение (шаг 2). Файл читает и Node, поэтому импорты только с .ts.
import {z} from 'zod';
import {pictogramNames} from './style/pictograms.generated.ts';
import {limits} from './style/tokens.ts';

export const insertKinds = ['chamber_card', 'hazard_banner', 'pixel_transition', 'test_result', 'lower_third', 'custom'] as const;
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

const pictogramRef = z.union([
	z.enum(pictogramNames as [string, ...string[]]),
	z.strictObject({name: z.enum(pictogramNames as [string, ...string[]]), active: z.boolean().default(true), label: text(24).optional()}),
]);

const insertBase = {id: z.string().min(1), at_ms: ms, dur_ms: z.number().int().min(1), beat: beatId};

export const insertSchema = z.discriminatedUnion('kind', [
	z.strictObject({
		...insertBase,
		kind: z.literal('chamber_card'),
		props: z.strictObject({
			chamber: z.number().int().min(0).max(999),
			of: z.number().int().min(1).max(99).default(19),
			title: text(limits.title),
			pictograms: z.array(pictogramRef).min(2).max(5),
		}),
	}),
	z.strictObject({...insertBase, kind: z.literal('hazard_banner'), props: z.strictObject({text: text(limits.onScreen)})}),
	z.strictObject({
		...insertBase,
		kind: z.literal('pixel_transition'),
		props: z.strictObject({direction: z.enum(['in', 'out'])}),
	}),
	z.strictObject({
		...insertBase,
		kind: z.literal('test_result'),
		props: z.strictObject({result: z.enum(['pass', 'fail']), label: text(limits.title).optional(), text: text(limits.onScreen)}),
	}),
	z.strictObject({
		...insertBase,
		kind: z.literal('lower_third'),
		props: z.strictObject({text: text(limits.onScreen), sub: text(limits.onScreen).optional()}),
	}),
	z.strictObject({
		...insertBase,
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
export type Voiceover = z.infer<typeof voiceoverSchema>;
export type Word = z.infer<typeof wordSchema>;
export type MainProps = z.infer<typeof mainPropsSchema>;

// Строит демо-планы (MON-22, ТЗ 23 SC-7) и слова расшифровки для них. Результат коммитится:
//   demo/plan.vertical.json     шортс: интро -> комната -> схема -> игра с субтитрами -> итог (+ призыв), 32 с
//   demo/plan.horizontal.json   YouTube: интро -> отбивка акта -> комната -> схема -> игра в рамке HUD -> итог (+ призыв), 33 с
//   demo/plan.square.json       квадрат без субтитров: интро, комната, журнал с затемнением, схема, итог
//   demo/plan.vertical.short.json  короткий (9 с) для проверочного рендера: отбивка, комната, итог
//   demo/plan.cyrillic.json     строка проверки кириллицы четырьмя начертаниями
//   demo/plan.pictograms.json   лист пиктограмм (служебный, старый набор)
//   public/demo/words.json      слова «расшифровки» к демо-видео
// Тексты на экране здесь это данные демо (в шаблоне надписей-констант нет, STY-5). Запуск: node demo/make-demo-plans.mjs
import {mkdirSync, writeFileSync} from 'node:fs';
import {dirname, join, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const write = (path, data) => {
	mkdirSync(dirname(path), {recursive: true});
	writeFileSync(path, JSON.stringify(data, null, 2) + '\n');
};

// ---------- слова: фразы с произвольными, но правдоподобными временами ----------
const phrases = [
	{at: 500, text: 'Подопытный нажимает кнопку, куб падает на пол.'},
	{at: 4200, text: 'Результат испытания: куб цел, подопытный нет.'},
	{at: 15800, text: 'Мы записали это в протокол, потому что наука требует жертв.'},
	{at: 19800, text: 'Следующая камера будет сложнее.'},
];
const words = [];
for (const ph of phrases) {
	let t = ph.at;
	for (const w of ph.text.split(' ')) {
		const dur = 120 + w.length * 55;
		words.push({i: words.length, text: w, start_ms: t, end_ms: t + dur, conf: 0.93});
		t += dur + 70;
	}
}
write(join(root, 'public', 'demo', 'words.json'), words);
const idx = (text) => words.find((w) => w.text.startsWith(text)).i;

// ---------- общие части ----------
const fmt = {
	vertical: {width: 1080, height: 1920, file: 'sources/demo_vertical_h264.mp4'},
	horizontal: {width: 1920, height: 1080, file: 'sources/demo_horizontal_h264.mp4'},
	square: {width: 1080, height: 1080, file: 'sources/demo_square_h264.mp4'},
};
const r4 = (v) => Math.round(v * 10000) / 10000;

// Рисунок чертежа «спальня 4x4» как в превью дизайн-системы: комната 368 px со сеткой 4x4, стены 10 px с проёмом двери и
// дугой, кровать с подушкой. Центр комнаты чуть левее центра кадра (место для выносок справа). Всё в долях кадра.
function bedroom(W, H, s = 368) {
	const k = s / 368; // уменьшение для узкого кадра
	const c = s / 4;
	const x0 = W / 2 - 46 - s / 2;
	const y0 = H / 2 - s / 2;
	const fx = (v) => r4(v / W);
	const fy = (v) => r4(v / H);
	const wall = 10 * k;
	const doorL = x0 + 0.4 * s;
	const doorR = doorL + c;
	const drawing = [{type: 'rect', x: fx(x0), y: fy(y0), w: fx(s), h: fy(s)}];
	for (let k = 1; k < 4; k++) {
		drawing.push({type: 'line', x1: fx(x0 + c * k), y1: fy(y0), x2: fx(x0 + c * k), y2: fy(y0 + s), weight: 'thin'});
		drawing.push({type: 'line', x1: fx(x0), y1: fy(y0 + c * k), x2: fx(x0 + s), y2: fy(y0 + c * k), weight: 'thin'});
	}
	const p = (x, y) => `${r4(x / W)} ${r4(y / H)}`;
	drawing.push({
		type: 'path',
		weight: 'wall',
		d: `M ${p(x0 - wall, y0 + s + wall)} V ${r4((y0 - wall) / H)} H ${r4((x0 + s + wall) / W)} V ${r4((y0 + s + wall) / H)} H ${r4(doorR / W)} M ${p(doorL, y0 + s + wall)} H ${r4((x0 - wall) / W)}`,
	});
	drawing.push({type: 'path', d: `M ${p(doorL, y0 + s + wall)} A ${r4(c / W)} ${r4(c / W)} 0 0 1 ${p(doorR, y0 + s + wall - c)}`});
	drawing.push({type: 'rect', x: fx(x0 + 23 * k), y: fy(y0 + 23 * k), w: fx(82.8 * k), h: fy(138 * k)});
	drawing.push({type: 'rect', x: fx(x0 + 32.2 * k), y: fy(y0 + 32.2 * k), w: fx(64.4 * k), h: fy(32.2 * k)});
	const dot = (x, y, label) => ({x: r4(x / W), y: r4(y / H), label});
	return {
		drawing,
		callouts: [
			dot(x0 + s + wall, y0 + c, 'Стена: гранит, 16 блоков'),
			dot((doorL + doorR) / 2, y0 + s + wall, 'Дверь: одна, наружу не открывать'),
			dot(x0 + 64.4 * k, y0 + c, 'Кровать: одна на трёх подопытных'),
			dot(x0 + s / 2, y0 + 2 * c, 'Площадь: 4×4 клетки, впечатление «тесно»'),
		],
	};
}

const blueprint = (id, at, dur, beat, W, H, size = 368) => ({
	id,
	kind: 'blueprint',
	at_ms: at,
	dur_ms: dur,
	beat,
	props: {
		title: 'Спальня 4×4',
		kicker: 'Отчёт для подопытных',
		note: '«Комната» — помещение по протоколу колонии, ред. 12 мартомая',
		...bedroom(W, H, size),
	},
});
const intro = (id, at, beat) => ({id, kind: 'intro', at_ms: at, dur_ms: 4000, beat, props: {subtitle: 'Лаборатория ГЛаДОС · отчёт об испытании № 07', wordmark: 'APERTURE SCIENCE'}});
const chamberSign = (id, at, beat) => ({
	id,
	kind: 'chamber_sign',
	at_ms: at,
	dur_ms: 3000,
	beat,
	props: {big: '07', small: '07/19 · колония «Поселение» · день 1', done: 7, total: 19, active: ['colonists', 'build', 'sleep', 'night'], title: 'Испытание: первая ночь'},
});
const resultSign = (id, at, beat) => ({
	id,
	kind: 'result_sign',
	at_ms: at,
	dur_ms: 3000,
	beat,
	props: {big: 'ПРОЙДЕНО', small: 'потерь 0 · дней 12 · набегов отбито 2', done: 19, total: 19, active: ['colonists', 'build', 'research', 'raid'], title: 'Итог испытания'},
});
const room = (id, at, dur, beat, moods, speech = []) => ({id, kind: 'glados_room', at_ms: at, dur_ms: dur, beat, props: {moods, speech}});
const seg = (f, id, inMs, outMs, atMs, beat, extra = {}) => ({id, source: f.file, in_ms: inMs, out_ms: outMs, at_ms: atMs, volume: 1, crop: {mode: 'center'}, zoom: 0, beat, ...extra});
const vo = (id, at, text, emotion, dur, beat) => ({id, at_ms: at, text, emotion, tts_path: null, duration_ms: dur, beat, duck: false});
const captionsOff = {enabled: false, source: 'transcript', style: 'aperture_default', overrides: [], words_file: 'demo/words.json'};
const base = (kind, duration, extra) => ({
	version: 1,
	format: {width: fmt[kind].width, height: fmt[kind].height, fps: 30},
	duration_ms: duration,
	title: `demo_${kind}`,
	blackout: [],
	music: null,
	...extra,
});

// ---------- шортс (вертикаль): интро -> комната -> схема -> игра с субтитрами -> итог -> призыв ----------
{
	const f = fmt.vertical;
	write(
		join(root, 'demo', 'plan.vertical.json'),
		base('vertical', 32000, {
			segments: [seg(f, 's1', 0, 11000, 15000, 'b4', {zoom: 0.03}), seg(f, 's2', 19000, 22000, 29000, 'b5')],
			captions: {
				enabled: true,
				source: 'both',
				style: 'aperture_default',
				overrides: [{word_index: idx('куб'), highlight: true}],
				words_file: 'demo/words.json',
			},
			voiceover: [vo('vo1', 4400, 'Наблюдение: подопытный уверен, что кнопка нажимается сама.', 'neutral', 3400, 'b2')],
			inserts: [
				intro('i1', 0, 'b1'),
				room('i2', 4000, 6000, 'b2', [
					{from_ms: 4000, to_ms: 6600, mood: 'thinking'},
					{from_ms: 6600, to_ms: 8400, mood: 'happy'},
				]),
				blueprint('i3', 10000, 5000, 'b3', f.width, f.height, 300),
				resultSign('i4', 26000, 'b5'),
			],
			sfx: [
				{at_ms: 300, name: 'chime_up'},
				{at_ms: 4000, name: 'whoosh'},
				{at_ms: 10000, name: 'tick'},
				{at_ms: 26000, name: 'chime_up'},
			],
			cta: {platform: 'shorts', at_ms: 29200, text: 'Следующие испытания уже ждут вас в лаборатории', beat: 'b5'},
		}),
	);
}

// ---------- YouTube (горизонталь): интро -> отбивка -> комната -> схема -> игра в рамке HUD -> итог -> призыв ----------
{
	const f = fmt.horizontal;
	write(
		join(root, 'demo', 'plan.horizontal.json'),
		base('horizontal', 33000, {
			segments: [
				seg(f, 's1', 0, 9000, 18000, 'b5', {zoom: 0.03, frame: 'hud', frame_label: 'КАМЕРА 07 · ЗАПИСЬ ИСПЫТАНИЯ'}),
				seg(f, 's2', 19000, 22000, 30000, 'b6', {frame: 'hud', frame_label: 'КАМЕРА 07 · ЗАПИСЬ ИСПЫТАНИЯ'}),
			],
			captions: captionsOff,
			voiceover: [vo('vo1', 7400, 'Наблюдение: подопытный уверен, что кнопка нажимается сама.', 'neutral', 3400, 'b3')],
			inserts: [
				intro('i1', 0, 'b1'),
				chamberSign('i2', 4000, 'b2'),
				room(
					'i3',
					7000,
					6000,
					'b3',
					[
						{from_ms: 7000, to_ms: 9200, mood: 'thinking'},
						{from_ms: 9200, to_ms: 11000, mood: 'surprised'},
					],
					[{from_ms: 11200, to_ms: 12600}],
				),
				blueprint('i4', 13000, 5000, 'b4', f.width, f.height),
				resultSign('i5', 27000, 'b6'),
			],
			sfx: [
				{at_ms: 300, name: 'chime_up'},
				{at_ms: 4000, name: 'whoosh'},
				{at_ms: 7000, name: 'tick'},
				{at_ms: 27000, name: 'chime_up'},
			],
			cta: {platform: 'youtube', at_ms: 30200, text: 'Следующие испытания уже ждут вас в лаборатории', beat: 'b6'},
		}),
	);
}

// ---------- квадрат: без субтитров; интро, комната, журнал с затемнением, схема, итог ----------
{
	const f = fmt.square;
	write(
		join(root, 'demo', 'plan.square.json'),
		base('square', 24000, {
			segments: [seg(f, 's1', 0, 6000, 9000, 'b3')],
			captions: captionsOff,
			voiceover: [vo('vo1', 4300, 'Наблюдение: подопытный уверен, что кнопка нажимается сама.', 'neutral', 3400, 'b2')],
			inserts: [
				intro('i1', 0, 'b1'),
				room('i2', 4000, 5000, 'b2', [{from_ms: 4000, to_ms: 7000, mood: 'question'}]),
				{id: 'i3', kind: 'custom', at_ms: 10000, dur_ms: 2200, beat: 'b3', props: {component: 'JournalLog', lines: ['00:13 нажатие: кнопка 1', '00:14 падение: куб 1', '00:15 вывод: протокол нарушен']}},
				blueprint('i4', 15000, 5000, 'b4', f.width, f.height, 300),
				resultSign('i5', 20500, 'b4'),
			],
			blackout: [{from_ms: 9800, to_ms: 12400, opacity: 1}],
			sfx: [
				{at_ms: 300, name: 'chime_up'},
				{at_ms: 4000, name: 'whoosh'},
				{at_ms: 15000, name: 'tick'},
				{at_ms: 20500, name: 'chime_up'},
			],
			cta: {platform: 'none', at_ms: 20500, text: 'Следующие испытания уже ждут вас в лаборатории', beat: 'b4'},
		}),
	);
}

// ---------- короткий вертикальный демо (до 10 с, без CTA) ----------
{
	const f = fmt.vertical;
	write(join(root, 'demo', 'plan.vertical.short.json'), {
		version: 1,
		format: {width: f.width, height: f.height, fps: 30},
		duration_ms: 9000,
		title: 'demo_vertical_short',
		segments: [],
		captions: {enabled: true, source: 'voiceover', style: 'aperture_default', overrides: [], words_file: 'demo/words.json'},
		voiceover: [vo('vo1', 3400, 'Наблюдение: подопытный уверен, что кнопка нажимается сама.', 'neutral', 3000, 'b2')],
		inserts: [chamberSign('i1', 200, 'b1'), room('i2', 3200, 3000, 'b2', [{from_ms: 3200, to_ms: 5200, mood: 'happy'}]), resultSign('i3', 6200, 'b2')],
		blackout: [],
		sfx: [{at_ms: 200, name: 'chime_up'}, {at_ms: 3200, name: 'whoosh'}, {at_ms: 6200, name: 'chime_up'}],
		music: null,
		cta: null,
	});
}

// ---------- проверка кириллицы (STY-2): строка из ТЗ 21 каждым начертанием ----------
write(join(root, 'demo', 'plan.cyrillic.json'), {
	version: 1,
	format: {width: 1920, height: 1080, fps: 30},
	duration_ms: 2000,
	title: 'demo_cyrillic',
	segments: [],
	captions: captionsOff,
	voiceover: [],
	inserts: [{id: 'i1', kind: 'custom', at_ms: 0, dur_ms: 2000, beat: 'b1', props: {component: 'FontSpecimen', text: 'Испытание № 07. ЙЁЖЩЪЫЭЮЯ ИЙ ийёжщ — «ёлка»'}}],
	blackout: [],
	sfx: [],
	music: null,
	cta: null,
});
// ---------- лист пиктограмм (служебный, старый набор STY-3) ----------
write(join(root, 'demo', 'plan.pictograms.json'), {
	version: 1,
	format: {width: 1080, height: 1080, fps: 30},
	duration_ms: 1000,
	title: 'demo_pictograms',
	segments: [],
	captions: captionsOff,
	voiceover: [],
	inserts: [{id: 'i1', kind: 'custom', at_ms: 0, dur_ms: 1000, beat: 'b1', props: {component: 'PictogramSheet'}}],
	blackout: [],
	sfx: [],
	music: null,
	cta: null,
});
console.log('демо-планы записаны');

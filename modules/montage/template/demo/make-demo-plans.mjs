// Строит демо-планы (MON-22) и слова расшифровки для них. Результат коммитится:
//   demo/plan.vertical.json, plan.horizontal.json, plan.square.json   полный набор компонентов, 24 с
//   demo/plan.vertical.short.json                                      короткий (9 с) для проверочного рендера
//   demo/plan.cyrillic.json                                            строка проверки кириллицы четырьмя начертаниями
//   demo/plan.pictograms.json                                          лист пиктограмм
//   public/demo/words.json                                             слова «расшифровки» к демо-видео
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
const pictograms = [
	'cube',
	{name: 'button', active: true},
	{name: 'gear', active: true, label: 'x2'},
	{name: 'clock', active: false},
];
const fmt = {
	vertical: {width: 1080, height: 1920, file: 'sources/demo_vertical_h264.mp4'},
	horizontal: {width: 1920, height: 1080, file: 'sources/demo_horizontal_h264.mp4'},
	square: {width: 1080, height: 1080, file: 'sources/demo_square_h264.mp4'},
};

function full(kind) {
	const f = fmt[kind];
	const seg = (id, inMs, outMs, beat, zoom = 0) => ({id, source: f.file, in_ms: inMs, out_ms: outMs, at_ms: inMs, volume: 1, crop: {mode: 'center'}, zoom, beat});
	return {
		version: 1,
		format: {width: f.width, height: f.height, fps: 30},
		duration_ms: 24000,
		title: `demo_${kind}`,
		segments: [seg('s1', 0, 9000, 'b1'), seg('s2', 9000, 18000, 'b2', 0.03), seg('s3', 18000, 24000, 'b3')],
		captions: {
			enabled: true,
			source: 'both',
			style: 'aperture_default',
			overrides: [{word_index: idx('протокол'), highlight: true}, {word_index: idx('жертв'), text: 'жертв.'}],
			words_file: 'demo/words.json',
		},
		voiceover: [{id: 'vo1', at_ms: 9500, text: 'Наблюдение: подопытный уверен, что кнопка нажимается сама.', emotion: 'neutral', tts_path: null, duration_ms: 3400, beat: 'b2', duck: false}],
		inserts: [
			{id: 'i1', kind: 'chamber_card', at_ms: 300, dur_ms: 2400, beat: 'b1', props: {chamber: 7, of: 19, title: 'Куб-компаньон: падение', pictograms}},
			{id: 'i2', kind: 'lower_third', at_ms: 3200, dur_ms: 3400, beat: 'b1', props: {text: 'Подопытный № 07', sub: 'Камера 7, северное крыло'}},
			{id: 'i3', kind: 'hazard_banner', at_ms: 7000, dur_ms: 2000, beat: 'b1', props: {text: 'Нарушение протокола'}},
			{id: 'i4', kind: 'pixel_transition', at_ms: 12400, dur_ms: 600, beat: 'b2', props: {direction: 'out'}},
			{id: 'i5', kind: 'custom', at_ms: 13200, dur_ms: 2200, beat: 'b2', props: {component: 'JournalLog', lines: ['00:13 нажатие: кнопка 1', '00:14 падение: куб 1', '00:15 вывод: протокол нарушен']}},
			{id: 'i6', kind: 'pixel_transition', at_ms: 15600, dur_ms: 600, beat: 'b2', props: {direction: 'in'}},
			{id: 'i7', kind: 'test_result', at_ms: 18500, dur_ms: 3200, beat: 'b3', props: {result: 'fail', label: 'Провал', text: 'Куб цел, подопытный нет'}},
		],
		blackout: [{from_ms: 13000, to_ms: 15600, opacity: 1}],
		sfx: [
			{at_ms: 300, name: 'chime_up'},
			{at_ms: 3200, name: 'tick'},
			{at_ms: 7000, name: 'buzz_error'},
			{at_ms: 12400, name: 'whoosh'},
			{at_ms: 18500, name: 'chime_down'},
			{at_ms: 21500, name: 'elevator_ding'},
		],
		music: null,
		cta: {platform: kind === 'horizontal' ? 'youtube' : 'shorts', at_ms: 21500, text: 'Следующие испытания уже ждут вас в лаборатории', beat: 'b3'},
	};
}

for (const kind of Object.keys(fmt)) write(join(root, 'demo', `plan.${kind}.json`), full(kind));

// ---------- короткий вертикальный демо (до 10 с, без CTA) ----------
{
	const f = fmt.vertical;
	write(join(root, 'demo', 'plan.vertical.short.json'), {
		version: 1,
		format: {width: f.width, height: f.height, fps: 30},
		duration_ms: 9000,
		title: 'demo_vertical_short',
		segments: [{id: 's1', source: f.file, in_ms: 0, out_ms: 9000, at_ms: 0, volume: 1, crop: {mode: 'center'}, zoom: 0.02, beat: 'b1'}],
		captions: {enabled: true, source: 'both', style: 'aperture_default', overrides: [], words_file: 'demo/words.json'},
		voiceover: [{id: 'vo1', at_ms: 5800, text: 'Наблюдение: подопытный уверен, что кнопка нажимается сама.', emotion: 'neutral', tts_path: null, duration_ms: 3000, beat: 'b2', duck: false}],
		inserts: [
			{id: 'i1', kind: 'chamber_card', at_ms: 200, dur_ms: 2300, beat: 'b1', props: {chamber: 7, of: 19, title: 'Куб-компаньон: падение', pictograms}},
			{id: 'i2', kind: 'hazard_banner', at_ms: 3600, dur_ms: 1800, beat: 'b1', props: {text: 'Нарушение протокола'}},
			{id: 'i3', kind: 'test_result', at_ms: 6000, dur_ms: 2800, beat: 'b2', props: {result: 'pass', label: 'Успех', text: 'Куб на месте'}},
		],
		blackout: [],
		sfx: [{at_ms: 200, name: 'chime_up'}, {at_ms: 3600, name: 'buzz_error'}, {at_ms: 6000, name: 'chime_up'}],
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
	captions: {enabled: false, source: 'transcript', style: 'aperture_default', overrides: [], words_file: 'demo/words.json'},
	voiceover: [],
	inserts: [{id: 'i1', kind: 'custom', at_ms: 0, dur_ms: 2000, beat: 'b1', props: {component: 'FontSpecimen', text: 'Испытание № 07. ЙЁЖЩЪЫЭЮЯ ИЙ ийёжщ — «ёлка»'}}],
	blackout: [],
	sfx: [],
	music: null,
	cta: null,
});
// ---------- лист пиктограмм (STY-3, STY-6) ----------
write(join(root, 'demo', 'plan.pictograms.json'), {
	version: 1,
	format: {width: 1080, height: 1080, fps: 30},
	duration_ms: 1000,
	title: 'demo_pictograms',
	segments: [],
	captions: {enabled: false, source: 'transcript', style: 'aperture_default', overrides: [], words_file: 'demo/words.json'},
	voiceover: [],
	inserts: [{id: 'i1', kind: 'custom', at_ms: 0, dur_ms: 1000, beat: 'b1', props: {component: 'PictogramSheet'}}],
	blackout: [],
	sfx: [],
	music: null,
	cta: null,
});
console.log('демо-планы записаны');

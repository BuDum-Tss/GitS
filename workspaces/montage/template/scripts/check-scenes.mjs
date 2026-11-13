// Проверка чистой математики сцен без браузера (ТЗ 23): тайминги интро, геометрия ириса, эмоции питомца, огибающая голоса,
// раскладка выносок, пути чертежа, размер слова на табличке, а также равенство токенов tokens.ts и design/tokens.json
// дизайн-системы v2 (SC-1). Запуск: node scripts/check-scenes.mjs. Код выхода 0: всё верно; иначе список ошибок в stdout.
import {readFileSync} from 'node:fs';
import {dirname, join, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const M = await import(new URL('../src/sceneMath.ts', import.meta.url).href);
const T = await import(new URL('../src/style/tokens.ts', import.meta.url).href);
const I = await import(new URL('../src/style/icons.ts', import.meta.url).href);
const design = JSON.parse(readFileSync(join(root, 'design', 'tokens.json'), 'utf8'));

const errors = [];
const check = (cond, msg) => {
	if (!cond) errors.push(msg);
};
const near = (a, b, eps = 1e-6) => Math.abs(a - b) <= eps;

// ---------- токены против tokens.json дизайн-системы ----------
const norm = (v) => String(v).toLowerCase().replace(/\s+/g, '');
for (const tok of design.color.tokens) {
	check(T.colors[tok.name] !== undefined, `цвет ${tok.name} из tokens.json нет в tokens.ts`);
	if (T.colors[tok.name] !== undefined) check(norm(T.colors[tok.name]) === norm(tok.value), `цвет ${tok.name}: ${T.colors[tok.name]} != ${tok.value}`);
}
for (const name of Object.keys(T.colors)) check(design.color.tokens.some((t) => t.name === name), `цвет ${name} из tokens.ts нет в tokens.json`);
for (const gone of ['hazardYellow', 'alertRed']) check(T.colors[gone] === undefined, `цвет ${gone} должен быть удалён`);

const px = (s) => parseFloat(s);
const dur = Object.fromEntries(design.duration.tokens.map((t) => [t.name, t.value]));
check(px(dur.intro) === T.durations.intro, 'duration intro');
check(px(dur['bp-draw']) === T.durations.bpDraw, 'duration bp-draw');
check(px(dur['pet-bob']) === T.durations.petBob, 'duration pet-bob');
check(px(dur['pet-talk']) === T.durations.petTalk, 'duration pet-talk');
check(px(dur['caption-crossfade']) === T.durations.captionCrossfade && T.captions.crossfadeMs === T.durations.captionCrossfade, 'duration caption-crossfade');
const hold = dur['bumper-hold'].split(/\s+/).map(px);
check(hold[0] === T.durations.bumperHold.min && hold[1] === T.durations.bumperHold.max, 'duration bumper-hold');

const sp = Object.fromEntries(design.spacing.tokens.map((t) => [t.name, px(t.value)]));
check(sp['plate-pad-x'] === T.captions.plate.padX && sp['plate-pad-y'] === T.captions.plate.padY, 'spacing plate-pad');
check(sp['voice-bar'] === T.captions.voiceBarPx, 'spacing voice-bar');
check(sp['bp-cell'] === T.blueprint.cell && sp['bp-cell-major'] === T.blueprint.cellMajor, 'spacing bp-cell');
check(sp['sign-box'] === T.sign.boxPx && sp['sign-tick'] === T.sign.tickW && sp['sign-pad'] === T.sign.padX, 'spacing sign-*');
check(design.radius.tokens.find((t) => t.name === 'radius-plate').value === `${T.captions.plate.radius}px`, 'radius-plate');
const op = Object.fromEntries(design.opacity.tokens.map((t) => [t.name, parseFloat(t.value)]));
check(op['opacity-caption-plate'] === T.opacity.captionPlate && op['opacity-hud'] === T.opacity.hud, 'opacity-*');

const styles = Object.fromEntries(design.type.groups.flatMap((g) => g.styles.map((s) => [s.name, px(s.fontSize)])));
check(styles['sign-number'] === T.sign.bigPx && styles['sign-small'] === T.sign.smallPx && styles['sign-title'] === T.sign.titlePx, 'type sign-*');
check(styles['intro-word'] === T.intro.wordPx && styles['intro-sub'] === T.intro.subPx, 'type intro-*');
check(styles['bp-title'] === T.blueprint.titlePx && styles['bp-kicker'] === T.blueprint.kickerPx && styles['bp-label'] === T.blueprint.labelPx, 'type bp-*');
check(styles.caption === T.captions.fontPx.vertical && styles['hud-label'] === T.hud.labelPx, 'type caption, hud-label');

// ---------- ирис ----------
const blades = M.irisBlades();
check(blades.length === 8, 'ирис: 8 лопастей');
check(blades[0] === '150.0,0.0 106.1,106.1 18.6,48.5 47.5,21.2', `ирис: первая лопасть ${blades[0]}`);
check(blades[1] === '106.1,106.1 0.0,150.0 -21.2,47.5 18.6,48.5', `ирис: вторая лопасть ${blades[1]}`);

// ---------- интро ----------
const i0 = M.introState(0);
check(i0.irisRotDeg === -120 && i0.irisScale === 0.4 && i0.irisOpacity === 0 && i0.wordOpacity === 0 && i0.subOpacity === 0, 'интро: начало');
const i4 = M.introState(4000);
check(i4.irisRotDeg === 0 && i4.irisScale === 1 && i4.wordOpacity === 1 && i4.subOpacity === 1 && near(i4.wordTrackEm, 0.14), 'интро: конец');
check(M.introState(1500).wordOpacity === 0 && M.introState(2000).wordOpacity > 0, 'интро: надпись с 1,6 с');
check(M.introState(2500).subOpacity === 0 && M.introState(3000).subOpacity > 0, 'интро: подзаголовок с 2,6 с');
check(M.introState(2200).irisRotDeg === 0, 'интро: ирис собран к 2,2 с');
let prev = -1;
for (let t = 0; t <= 4000; t += 50) {
	const s = M.introState(t);
	check(s.irisScale >= prev - 1e-9 && s.irisScale <= 1.000001, 'интро: масштаб ириса растёт без отскока');
	prev = s.irisScale;
}

// ---------- чертёж ----------
check(M.drawProgress(0) === 0 && M.drawProgress(2500) === 1 && M.drawProgress(1250) > 0.4 && M.drawProgress(1250) < 0.6, 'чертёж: прорисовка за 2,5 с');
check(M.labelOpacity(2500) === 0 && M.labelOpacity(4000) === 1, 'чертёж: подписи после линий');
const px1 = M.pathToPx('M 0.1 0.2 H 0.5 V 0.8 A 0.05 0.05 0 0 1 0.6 0.9 Z', 1000, 500);
check(px1 === 'M 100.0 100.0 H 500.0 V 400.0 A 50.0 50.0 0 0 1 600.0 450.0 Z', `чертёж: путь ${px1}`);
check(M.toPx({type: 'circle', cx: 0.5, cy: 0.5, r: 0.1, weight: 'main'}, 1000, 500).r === 100, 'чертёж: радиус круга от ширины');
let threw = false;
try {
	M.pathToPx('M 0.1 0.2 X 1', 100, 100);
} catch {
	threw = true;
}
check(threw || /^M 10\.0 20\.0/.test(M.pathToPx('M 0.1 0.2', 100, 100)), 'чертёж: путь без лишнего');
const bounds = M.drawingBounds([M.toPx({type: 'rect', x: 0.3, y: 0.4, w: 0.2, h: 0.2, weight: 'main'}, 1000, 1000), M.toPx({type: 'path', d: 'M 0.1 0.1 H 0.2 V 0.9', weight: 'wall'}, 1000, 1000)]);
check(bounds.x0 === 100 && bounds.x1 === 500 && bounds.y0 === 100 && bounds.y1 === 900, `чертёж: границы ${JSON.stringify(bounds)}`);
for (const [w, h] of [[1920, 1080], [1080, 1920], [1080, 1080]]) {
	const calls = [
		{x: 0.62, y: 0.42, label: 'Стена: гранит, 16 блоков'},
		{x: 0.5, y: 0.66, label: 'Дверь: одна, наружу не открывать'},
		{x: 0.4, y: 0.42, label: 'Кровать: одна на трёх подопытных'},
		{x: 0.48, y: 0.5, label: 'Площадь: 4×4 клетки, впечатление «тесно»'},
	];
	const b = {x0: 0.35 * w, x1: 0.6 * w};
	const L = M.layoutCallouts(calls, w, h, b);
	check(L.length === 4, 'выноски: четыре');
	for (const [k, l] of L.entries()) {
		const left = l.side === 'right' ? l.anchorX : l.anchorX - l.boxWidth;
		check(left >= T.blueprint.padX - 1 && left + l.boxWidth <= w - T.blueprint.padX + 1, `выноски ${w}x${h}[${k}]: подпись вне кадра`);
		if (l.side === 'right') check(l.anchorX >= b.x1, `выноски ${w}x${h}[${k}]: справа подпись на рисунке`);
		else check(l.anchorX <= b.x0, `выноски ${w}x${h}[${k}]: слева подпись на рисунке`);
	}
	for (const side of ['left', 'right']) {
		const s = L.filter((l) => l.side === side).sort((a, c) => a.labelY - c.labelY);
		for (let i = 1; i < s.length; i++) check(s[i].labelY - s[i - 1].labelY >= (s[i - 1].lines * T.blueprint.labelPx * 1.2) / 2 + (s[i].lines * T.blueprint.labelPx * 1.2) / 2, `выноски ${w}x${h}: подписи наезжают`);
	}
}

// ---------- комната ----------
const mood = (from_ms, to_ms, m) => ({from_ms, to_ms, mood: m});
const moods = [mood(1000, 2000, 'thinking'), mood(2000, 3000, 'happy')];
check(M.moodLayers(moods, 500).length === 1 && M.moodLayers(moods, 500)[0].mood === 'idle', 'эмоции: до первого отрезка idle');
let l = M.moodLayers(moods, 1000);
check(l.length === 2 && l[0].mood === 'idle' && l[1].mood === 'thinking' && l[0].opacity === 1 && l[1].opacity === 0 && l[1].scale === 0.96, 'эмоции: начало кроссфейда');
l = M.moodLayers(moods, 1075);
check(l.length === 2 && near(l[0].opacity + l[1].opacity, 1) && l[1].scale > 0.96 && l[1].scale < 1, 'эмоции: середина кроссфейда сумма непрозрачностей 1');
l = M.moodLayers(moods, 1200);
check(l.length === 1 && l[0].mood === 'thinking' && l[0].scale === 1 && l[0].opacity === 1, 'эмоции: после 150 мс одна');
check(M.moodLayers(moods, 5000)[0].mood === 'idle', 'эмоции: после последнего отрезка idle');
check(M.moodLayers([], 100)[0].mood === 'idle', 'эмоции: без отрезков idle');
const bob = [0, 800, 1600, 2400, 3200].map((t) => M.petBob(t));
check(near(bob[0].y, -8) && near(bob[2].y, 10) && near(bob[4].y, -8), 'питомец: покачивание -8..10 px за 3,2 с');
check(near(bob[0].deg, -1.2) && near(bob[2].deg, 1.2), 'питомец: ±1,2°');
check(M.talkScale(0) === 1 && near(M.talkScale(1), 1.025) && M.talkScale(2) === M.talkScale(1), 'питомец: масштаб до 1,025');
const wave = Float32Array.from({length: 24000}, (_, i) => (i < 12000 ? 0.8 : 0.0) * Math.sin(i / 7));
const env = M.buildEnvelope(wave, 24000, 33);
check(env.length === Math.ceil(24000 / 792) && Math.max(...env) === 1 && env[env.length - 1] < 0.05, 'голос: огибающая громкая в начале и тихая в конце');
const clips = [{at_ms: 1000, duration_ms: 1000, env}];
check(M.talkLevel(500, clips, []) === 0 && M.talkLevel(1200, clips, []) > 0.5 && M.talkLevel(1900, clips, []) < 0.2, 'голос: уровень по огибающей реплики');
check(M.talkLevel(100, [], [{from_ms: 0, to_ms: 400}]) > 0 || M.talkLevel(140, [], [{from_ms: 0, to_ms: 400}]) > 0, 'голос: отрезок журнала речи пульсирует');
check(M.talkLevel(500, [{at_ms: 0, duration_ms: 1000, env: null}], []) >= 0, 'голос: без файла пульс');
check(M.petHeightPx(1920, 1080) === Math.round(0.62 * 1080) && M.petHeightPx(1080, 1920) === Math.round(0.42 * 1920), 'питомец: высота 62 % и 42 %');

// ---------- таблички ----------
for (const [text, content] of [['07', 648], ['ПРОЙДЕНО', 718], ['ПРОВАЛ', 788], ['ПРОЙДЕНО', 788]]) {
	const px_ = M.signBigPx(text, content);
	const em = /^\d+$/.test(text) ? 0.56 : 0.62;
	check(px_ <= 300 && px_ >= 96 && px_ * [...text].length * em <= content + 1, `табличка: «${text}» ${px_} px не влезает в ${content}`);
}
check(M.signBigPx('07', 900) === 300, 'табличка: число 300 px');
const ph0 = M.signPhase(0, 3000);
const ph1 = M.signPhase(1500, 3000);
const ph2 = M.signPhase(3000, 3000);
check(ph0.slide === 0 && ph0.fade === 1 && ph1.slide === 1 && ph1.fade === 1 && ph2.fade === 0, 'табличка: выезд 300 мс, держится, уход к белому 250 мс');
const cells = I.signIcons(['colonists', 'button', 'subject']);
check(cells.length === 12 && cells[0].on && cells.filter((c) => c.on).length === 3 && cells.some((c) => c.name === 'button') && cells.some((c) => c.name === 'subject'), 'иконки: button и subject занимают свободные места');
check(I.iconNames.length === 14, 'иконок 14');
check(I.signIcons([]).every((c) => !c.on) && I.signIcons([]).map((c) => c.name).join() === I.signGridIcons.join(), 'иконки: порядок сетки');

// ---------- субтитры только в вертикали ----------
check(T.captionsBurned(1080, 1920) && !T.captionsBurned(1920, 1080) && !T.captionsBurned(1080, 1080), 'субтитры: только вертикаль');

if (errors.length) {
	console.log(JSON.stringify(errors, null, 2));
	process.exit(1);
}
console.log('check-scenes: ok');

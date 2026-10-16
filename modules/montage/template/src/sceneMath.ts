// Чистая математика сцен дизайн-системы v2 (ТЗ 23): тайминги интро, прорисовка чертежа, раскладка выносок, эмоции питомца,
// огибающая голоса. Без React и без Remotion: файл читает и Node (scripts/check-scenes.mjs, тесты). Анимации превью
// дизайн-системы (CSS keyframes) переведены в функции от времени в миллисекундах.
import type {DrawingPrimitive} from './schema.ts';
import {blueprint, durations, intro, iris, orientationOf, room, sign, type PetMood} from './style/tokens.ts';

export const clamp01 = (v: number): number => Math.min(1, Math.max(0, v));
export const lerp = (a: number, b: number, k: number): number => a + (b - a) * k;
// прогресс 0..1 внутри отрезка [a, b] мс
export const span = (t: number, a: number, b: number): number => clamp01((t - a) / Math.max(1e-9, b - a));

// cubic-bezier как в CSS (Ньютон + запасной бисекционный шаг)
export function cubicBezier(x1: number, y1: number, x2: number, y2: number): (x: number) => number {
	const cx = 3 * x1;
	const bx = 3 * (x2 - x1) - cx;
	const ax = 1 - cx - bx;
	const cy = 3 * y1;
	const by = 3 * (y2 - y1) - cy;
	const ay = 1 - cy - by;
	const sx = (t: number) => ((ax * t + bx) * t + cx) * t;
	const sy = (t: number) => ((ay * t + by) * t + cy) * t;
	const dx = (t: number) => (3 * ax * t + 2 * bx) * t + cx;
	return (x: number) => {
		if (x <= 0) return 0;
		if (x >= 1) return 1;
		let t = x;
		for (let i = 0; i < 8; i++) {
			const e = sx(t) - x;
			if (Math.abs(e) < 1e-6) return sy(t);
			const d = dx(t);
			if (Math.abs(d) < 1e-6) break;
			t -= e / d;
		}
		let lo = 0;
		let hi = 1;
		t = x;
		for (let i = 0; i < 40; i++) {
			const e = sx(t);
			if (Math.abs(e - x) < 1e-6) break;
			if (x > e) lo = t;
			else hi = t;
			t = (lo + hi) / 2;
		}
		return sy(t);
	};
}

export const easeOutCss = cubicBezier(0, 0, 0.58, 1); // CSS ease-out: вход без отскока
export const easeInOutCss = cubicBezier(0.42, 0, 0.58, 1); // CSS ease-in-out
export const easeIris = cubicBezier(0.2, 0.7, 0.1, 1); // кривая ириса в превью
export const easeOutCubic = (x: number): number => 1 - Math.pow(1 - clamp01(x), 3);
export const easeInCubic = (x: number): number => Math.pow(clamp01(x), 3);

// ---------- интро ----------
export type IntroState = {irisRotDeg: number; irisScale: number; irisOpacity: number; wordOpacity: number; wordTrackEm: number; subOpacity: number};

export function introState(tMs: number): IntroState {
	const k = easeIris(span(tMs, intro.irisMs[0], intro.irisMs[1]));
	const o = easeIris(span(tMs, 0, intro.irisFadeMs));
	const w = easeOutCss(span(tMs, intro.wordMs[0], intro.wordMs[1]));
	const s = easeOutCss(span(tMs, intro.subMs[0], intro.subMs[1]));
	return {
		irisRotDeg: lerp(intro.irisFromDeg, 0, k),
		irisScale: lerp(intro.irisFromScale, 1, k),
		irisOpacity: o,
		wordOpacity: w,
		wordTrackEm: lerp(intro.trackFrom, intro.trackTo, w),
		subOpacity: s,
	};
}

// Размер надписи интро: 104 px, а в узком кадре (вертикаль) уменьшается, чтобы строка с разрядкой влезла в 88 % ширины
export function introWordPx(text: string, width: number): number {
	const n = Math.max(1, [...text].length);
	return Math.max(40, Math.min(intro.wordPx, Math.floor((width * 0.88) / (n * (0.58 + intro.trackTo)))));
}

const rad = (deg: number): number => (deg * Math.PI) / 180;
const fmt = (v: number): string => (Math.round(v * 10) / 10).toFixed(1);
const polar = (r: number, deg: number): string => `${fmt(r * Math.cos(rad(deg)))},${fmt(r * Math.sin(rad(deg)))}`;

// точки лопастей ириса: [R(150,a), R(150,a+45), r(52,a+45+24), r(52,a+24)] для a = k * 45
export function irisBlades(): string[] {
	const out: string[] = [];
	for (let k = 0; k < iris.blades; k++) {
		const a = k * iris.step;
		out.push([polar(iris.outer, a), polar(iris.outer, a + iris.step), polar(iris.inner, a + iris.step + iris.twist), polar(iris.inner, a + iris.twist)].join(' '));
	}
	return out;
}

// ---------- чертёж ----------
export function drawProgress(tMs: number): number {
	return easeInOutCss(span(tMs, 0, durations.bpDraw));
}
// подписи появляются после того, как линии дорисованы
export function labelOpacity(tMs: number): number {
	return easeOutCss(span(tMs, durations.bpDraw, durations.bpDraw + 800));
}

const PATH_ARGS: Record<string, number> = {M: 2, L: 2, H: 1, V: 1, C: 6, S: 4, Q: 4, T: 2, A: 7, Z: 0};

// путь в долях кадра -> путь в пикселях: x и y масштабируются по ширине и высоте; у дуги A радиусы масштабируются по
// ширине (рисунок остаётся кругом), флаги и поворот не трогаются
export function pathToPx(d: string, width: number, height: number): string {
	const tokens = d.match(/[MLHVCSQTAZ]|[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?/g) ?? [];
	const out: string[] = [];
	let cmd = '';
	let i = 0;
	while (i < tokens.length) {
		const t = tokens[i];
		if (/^[A-Z]$/.test(t)) {
			cmd = t;
			out.push(t);
			i++;
			if (cmd === 'Z') continue;
		}
		const n = PATH_ARGS[cmd];
		if (n === undefined) throw new Error(`path: неизвестная команда ${cmd}`);
		if (n === 0) continue;
		const a = tokens.slice(i, i + n).map(Number);
		if (a.length < n || a.some((v) => Number.isNaN(v))) throw new Error(`path: мало чисел у команды ${cmd}`);
		i += n;
		const px: number[] = [];
		if (cmd === 'H') px.push(a[0] * width);
		else if (cmd === 'V') px.push(a[0] * height);
		else if (cmd === 'A') {
			// флаги дуги должны остаться 0 или 1 без дробной части
			out.push([fmt(a[0] * width), fmt(a[1] * width), String(a[2]), String(a[3]), String(a[4]), fmt(a[5] * width), fmt(a[6] * height)].join(' '));
			continue;
		} else for (let k = 0; k < n; k += 2) px.push(a[k] * width, a[k + 1] * height);
		out.push(px.map((v) => fmt(v)).join(' '));
	}
	return out.join(' ');
}

export type PxShape =
	| {type: 'line'; x1: number; y1: number; x2: number; y2: number; weight: string}
	| {type: 'rect'; x: number; y: number; w: number; h: number; weight: string}
	| {type: 'circle'; cx: number; cy: number; r: number; weight: string}
	| {type: 'path'; d: string; weight: string};

export function toPx(p: DrawingPrimitive, width: number, height: number): PxShape {
	switch (p.type) {
		case 'line':
			return {type: 'line', x1: p.x1 * width, y1: p.y1 * height, x2: p.x2 * width, y2: p.y2 * height, weight: p.weight};
		case 'rect':
			return {type: 'rect', x: p.x * width, y: p.y * height, w: p.w * width, h: p.h * height, weight: p.weight};
		case 'circle':
			return {type: 'circle', cx: p.cx * width, cy: p.cy * height, r: p.r * width, weight: p.weight};
		case 'path':
			return {type: 'path', d: pathToPx(p.d, width, height), weight: p.weight};
	}
}

// границы рисунка в пикселях: выноски выходят за них, чтобы подписи не ложились на чертёж
export function drawingBounds(shapes: readonly PxShape[]): {x0: number; y0: number; x1: number; y1: number} | null {
	let x0 = Infinity;
	let y0 = Infinity;
	let x1 = -Infinity;
	let y1 = -Infinity;
	const add = (x: number, y: number) => {
		x0 = Math.min(x0, x);
		y0 = Math.min(y0, y);
		x1 = Math.max(x1, x);
		y1 = Math.max(y1, y);
	};
	for (const sh of shapes) {
		if (sh.type === 'line') (add(sh.x1, sh.y1), add(sh.x2, sh.y2));
		else if (sh.type === 'rect') (add(sh.x, sh.y), add(sh.x + sh.w, sh.y + sh.h));
		else if (sh.type === 'circle') (add(sh.cx - sh.r, sh.cy - sh.r), add(sh.cx + sh.r, sh.cy + sh.r));
		else {
			// путь уже в пикселях: пары координат по командам (для A берётся конечная точка)
			const tk = sh.d.match(/[MLHVCSQTAZ]|-?\d*\.?\d+/g) ?? [];
			let cmd = '';
			let cx = 0;
			let cy = 0;
			for (let i = 0; i < tk.length; ) {
				if (/^[A-Z]$/.test(tk[i])) cmd = tk[i++];
				const n = PATH_ARGS[cmd] ?? 0;
				if (!n) continue;
				const a = tk.slice(i, i + n).map(Number);
				i += n;
				if (cmd === 'H') cx = a[0];
				else if (cmd === 'V') cy = a[0];
				else {
					cx = a[n - 2];
					cy = a[n - 1];
				}
				add(cx, cy);
			}
		}
	}
	return Number.isFinite(x0) ? {x0, y0, x1, y1} : null;
}

export type CalloutLayout = {dotX: number; dotY: number; anchorX: number; side: 'left' | 'right'; boxWidth: number; labelY: number; lines: number};

// Выноска: точка на рисунке, тонкая линия в сторону ближнего края кадра и подпись (строка переносится, если места мало)
export function calloutLayout(
	c: {x: number; y: number},
	width: number,
	height: number,
	bounds: {x0: number; x1: number} | null = null,
): CalloutLayout {
	const dotX = c.x * width;
	const dotY = c.y * height;
	const off = blueprint.leaderOffsetRatio * width;
	const lo = blueprint.padX + blueprint.labelMinPx;
	const hiLimit = width - blueprint.padX - blueprint.labelMinPx;
	// якорь подписи: не ближе off к точке и за пределами рисунка
	const rightAnchor = () => Math.min(Math.max(dotX + off, bounds ? bounds.x1 + blueprint.outsidePx : 0), hiLimit);
	const leftAnchor = () => Math.max(Math.min(dotX - off, bounds ? bounds.x0 - blueprint.outsidePx : width), lo);
	let side: 'left' | 'right' = c.x >= 0.5 ? 'right' : 'left';
	let anchorX = side === 'right' ? rightAnchor() : leftAnchor();
	if (side === 'right' && anchorX < dotX + 20) {
		side = 'left';
		anchorX = leftAnchor();
	} else if (side === 'left' && anchorX > dotX - 20) {
		side = 'right';
		anchorX = rightAnchor();
	}
	const room_ = side === 'right' ? width - blueprint.padX - anchorX : anchorX - blueprint.padX;
	return {dotX, dotY, anchorX, side, boxWidth: Math.min(blueprint.labelMaxPx, Math.max(blueprint.labelMinPx, room_)), labelY: dotY, lines: 1};
}

// Число строк подписи при жадном переносе по словам (charPx: средняя ширина знака; оценка с запасом)
export function wrapLineCount(text: string, boxWidth: number, charPx: number): number {
	const perLine = Math.max(4, Math.floor(boxWidth / charPx));
	let lines = 1;
	let cur = 0;
	for (const word of text.split(/\s+/).filter(Boolean)) {
		const n = [...word].length;
		if (cur === 0) cur = n;
		else if (cur + 1 + n <= perLine) cur += 1 + n;
		else {
			lines++;
			cur = n;
		}
		while (cur > perLine) {
			lines++;
			cur -= perLine;
		}
	}
	return lines;
}

// Все выноски: подпись переносится по ширине, а подписи одной стороны сдвигаются по вертикали, чтобы не наезжать друг на друга
// (линия выноски идёт от точки к сдвинутой подписи под углом, как в превью дизайн-системы)
export function layoutCallouts(
	callouts: readonly {x: number; y: number; label: string}[],
	width: number,
	height: number,
	bounds: {x0: number; x1: number} | null,
): CalloutLayout[] {
	const out = callouts.map((c) => {
		const l = calloutLayout(c, width, height, bounds);
		return {...l, lines: wrapLineCount(c.label, l.boxWidth, blueprint.labelPx * blueprint.labelEm)};
	});
	const lh = blueprint.labelPx * 1.2;
	for (const side of ['left', 'right'] as const) {
		const idx = out.map((_, i) => i).filter((i) => out[i].side === side).sort((a, b) => out[a].dotY - out[b].dotY);
		let bottom = -Infinity;
		for (const i of idx) {
			const h = out[i].lines * lh;
			const top = Math.max(out[i].dotY - h / 2, bottom + 12);
			out[i].labelY = top + h / 2;
			bottom = top + h;
		}
		// если пачка ушла вниз за кадр, сдвинуть вверх целиком
		const limit = height - blueprint.padBottom;
		if (bottom > limit) for (const i of idx) out[i].labelY -= bottom - limit;
	}
	return out;
}

// ---------- комната ГЛаДОС ----------
export type MoodSpan = {from_ms: number; to_ms: number; mood: PetMood};
export type MoodLayer = {mood: PetMood; opacity: number; scale: number};

// Эмоция в момент t: отрезок плана, иначе idle. Смена: кроссфейд moodCrossfade мс, новая эмоция сжата 0,96 -> 1
export function moodLayers(moods: readonly MoodSpan[], tMs: number): MoodLayer[] {
	const sorted = [...moods].sort((a, b) => a.from_ms - b.from_ms);
	// непрерывная шкала отрезков: пробелы заполняет idle
	const line: {from: number; to: number; mood: PetMood}[] = [];
	let cursor = 0;
	for (const m of sorted) {
		if (m.to_ms <= m.from_ms) continue;
		const from = Math.max(m.from_ms, cursor);
		if (from > cursor) line.push({from: cursor, to: from, mood: 'idle'});
		if (m.to_ms > from) {
			line.push({from, to: m.to_ms, mood: m.mood});
			cursor = m.to_ms;
		}
	}
	line.push({from: cursor, to: Number.POSITIVE_INFINITY, mood: 'idle'});
	const k = line.findIndex((s) => tMs >= s.from && tMs < s.to);
	const cur = line[Math.max(0, k)];
	const prev = k > 0 ? line[k - 1] : null;
	const p = easeOutCss(span(tMs, cur.from, cur.from + durations.moodCrossfade));
	if (!prev || prev.mood === cur.mood || p >= 1) return [{mood: cur.mood, opacity: 1, scale: 1}];
	return [
		{mood: prev.mood, opacity: 1 - p, scale: 1},
		{mood: cur.mood, opacity: p, scale: lerp(room.moodScaleFrom, 1, p)},
	];
}

// покачивание в покое: pet-bob 3,2 с, ±9 px и ±1,2° (ease-in-out)
export function petBob(tMs: number): {y: number; deg: number} {
	const c = Math.cos((2 * Math.PI * tMs) / durations.petBob);
	return {y: room.bobOffsetPx - room.bobPx * c, deg: -room.bobDeg * c};
}

// Огибающая голоса: СКЗ по окнам windowMs, нормированная по устойчивому пику (95-й процентиль), сжатая до 0..1
export function buildEnvelope(wave: ArrayLike<number>, sampleRate: number, windowMs = 33): number[] {
	const win = Math.max(1, Math.round((sampleRate * windowMs) / 1000));
	const rms: number[] = [];
	for (let i = 0; i < wave.length; i += win) {
		let sum = 0;
		const end = Math.min(wave.length, i + win);
		for (let j = i; j < end; j++) sum += wave[j] * wave[j];
		rms.push(Math.sqrt(sum / Math.max(1, end - i)));
	}
	if (!rms.length) return [];
	const sorted = [...rms].sort((a, b) => a - b);
	const peak = sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * 0.95))] || 1e-9;
	return rms.map((v) => clamp01(v / peak));
}

export function envelopeAt(env: readonly number[], windowMs: number, tMs: number): number {
	if (!env.length || tMs < 0) return 0;
	const x = tMs / windowMs;
	const i = Math.floor(x);
	if (i >= env.length) return 0;
	return lerp(env[i], env[Math.min(env.length - 1, i + 1)], x - i);
}

export type VoiceClip = {at_ms: number; duration_ms: number; env: readonly number[] | null};

// Уровень голоса 0..1 в момент t: по огибающей реплики закадра, а без неё (нет файла) и для отрезков журнала речи это
// плавный пульс pet-talk (280 мс туда и обратно)
export function talkLevel(tMs: number, clips: readonly VoiceClip[], speech: readonly {from_ms: number; to_ms: number}[], windowMs = 33): number {
	let level = 0;
	const pulse = 0.5 - 0.5 * Math.cos((Math.PI * tMs) / durations.petTalk);
	for (const c of clips) {
		if (tMs < c.at_ms || tMs >= c.at_ms + c.duration_ms) continue;
		level = Math.max(level, c.env ? envelopeAt(c.env, windowMs, tMs - c.at_ms) : pulse);
	}
	for (const s of speech) {
		if (tMs >= s.from_ms && tMs < s.to_ms) level = Math.max(level, pulse);
	}
	return clamp01(level);
}

export const talkScale = (level: number): number => 1 + (room.talkMaxScale - 1) * clamp01(level);

// высота питомца в пикселях кадра: 62 % в горизонтали, 42 % в вертикали
export function petHeightPx(width: number, height: number): number {
	return Math.round(room.petHeightRatio[orientationOf(width, height)] * height);
}

// ---------- таблички ----------
// Размер огромного числа или слова: 300 px для числа, 260/230 для слова, но не шире строки таблички
// Тонкое узкое начертание (wght 300, wdth 75): прописная кириллица около 0,6 em на знак, цифры около 0,54 em. Слова
// дизайн-системы (230-260 px) в таблички шириной 760 не помещаются, поэтому размер подбирается по ширине строки.
export function signBigPx(text: string, contentWidth: number): number {
	const n = [...text].length;
	const max = n <= 3 ? sign.bigPx : n <= 6 ? sign.bigMaxWordPx : sign.bigWordPx;
	const em = /^[\d\s:./-]+$/.test(text) ? 0.56 : 0.62;
	return Math.max(96, Math.min(max, Math.floor(contentWidth / (Math.max(1, n) * em))));
}

// выезд снизу 300 мс (ease-out) и уход к белому 250 мс: 0..1 для смещения и прозрачности
export function signPhase(tMs: number, durMs: number): {slide: number; fade: number} {
	return {slide: easeOutCss(span(tMs, 0, sign.slideMs)), fade: 1 - easeInCubic(span(tMs, durMs - sign.fadeMs, durMs))};
}

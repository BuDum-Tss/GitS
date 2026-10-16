// Чистая логика времени: слова расшифровки -> страницы субтитров на шкале ролика; положение субтитров рядом со вставками.
// Без React: проверяется отдельно и повторяется в проверках качества (agent/qa.py).
import {createTikTokStyleCaptions, type Caption} from '@remotion/captions';
import type {Insert, MainProps, Voiceover, Word} from './schema';
import {captionAvoidKinds, captionHeightRatio, captions as captionTokens, motion, orientationOf, safe, zones, type ZoneKind} from './style/tokens';

export type TimedWord = {text: string; startMs: number; endMs: number; isVoice: boolean; highlight: boolean};
export type CaptionPage = {startMs: number; endMs: number; isVoice: boolean; tokens: {text: string; highlight: boolean}[]};

const stripCommas = (s: string): string => s.replace(/[,，、]/g, '').trim();

// слова человека из words.json: время источника -> время ролика через сегменты (слова вне сегментов и в немых сегментах отброшены)
export function humanWords(plan: MainProps, words: Word[]): TimedWord[] {
	const overrides = new Map(plan.captions.overrides.map((o) => [o.word_index, o]));
	const out: TimedWord[] = [];
	for (const w of words) {
		const ov = overrides.get(w.i);
		const text = stripCommas(ov?.text ?? w.text);
		if (!text) continue;
		const seg = plan.segments.find((s) => (w.source === undefined || w.source === s.source) && w.start_ms >= s.in_ms && w.start_ms < s.out_ms);
		if (!seg || seg.volume === 0) continue;
		const startMs = seg.at_ms + (w.start_ms - seg.in_ms);
		const endMs = seg.at_ms + (Math.min(w.end_ms, seg.out_ms) - seg.in_ms);
		out.push({text, startMs, endMs: Math.max(endMs, startMs + 80), isVoice: false, highlight: ov?.highlight === true});
	}
	return out;
}

// слова её закадра: времени слов нет (синтез отдаёт только длительность реплики), делим её пропорционально длине слов
export function voiceoverWords(vo: Voiceover[]): TimedWord[] {
	const out: TimedWord[] = [];
	for (const v of vo) {
		const parts = v.text.split(/\s+/).map(stripCommas).filter(Boolean);
		if (!parts.length) continue;
		const totalChars = parts.reduce((a, p) => a + p.length + 1, 0);
		const duration = v.duration_ms ?? Math.max(1000, totalChars * 62);
		let cursor = v.at_ms;
		for (const p of parts) {
			const d = (duration * (p.length + 1)) / totalChars;
			out.push({text: p, startMs: Math.round(cursor), endMs: Math.round(cursor + d), isVoice: true, highlight: false});
			cursor += d;
		}
	}
	return out;
}

export function captionWords(plan: MainProps): TimedWord[] {
	const c = plan.captions;
	if (!c.enabled) return [];
	const list: TimedWord[] = [];
	if (c.source !== 'voiceover') list.push(...humanWords(plan, plan.words ?? []));
	if (c.source !== 'transcript') list.push(...voiceoverWords(plan.voiceover));
	return list.sort((a, b) => a.startMs - b.startMs);
}

// страницы: createTikTokStyleCaptions (550 мс) плюс разрывы по 28 символам, концам предложений и смене говорящего
export function buildPages(words: TimedWord[]): CaptionPage[] {
	const pages: CaptionPage[] = [];
	for (const isVoice of [false, true]) {
		const stream = words.filter((w) => w.isVoice === isVoice);
		if (!stream.length) continue;
		let line = 0;
		const breakBefore: boolean[] = stream.map((w) => {
			const add = w.text.length + (line > 0 ? 1 : 0);
			const brk = line > 0 && line + add > captionTokens.maxChars;
			line = brk ? w.text.length : line + add;
			return brk;
		});
		const caps: Caption[] = stream.map((w, k) => ({
			text: (k === 0 ? '' : ' ') + w.text,
			startMs: w.startMs,
			endMs: w.endMs,
			timestampMs: Math.round((w.startMs + w.endMs) / 2),
			confidence: null,
			// разрыв страницы: перед словом, которое не влезло в 28 символов, и после конца предложения
			pageBreakAfter: (k + 1 < stream.length && breakBefore[k + 1]) || /[.?!…]$/.test(w.text),
		}));
		const {pages: tiktok} = createTikTokStyleCaptions({captions: caps, combineTokensWithinMilliseconds: captionTokens.combineMs});
		let idx = 0;
		for (const page of tiktok) {
			const tokens = page.tokens.map((t) => {
				const w = stream[idx++];
				return {text: t.text, highlight: w.highlight};
			});
			const last = page.tokens[page.tokens.length - 1];
			pages.push({startMs: page.startMs, endMs: Math.max(last.toMs, page.startMs + 200), isVoice, tokens});
		}
	}
	pages.sort((a, b) => a.startMs - b.startMs);
	// выделение цветом: одно слово на страницу, не чаще раза в 3 секунды
	let lastHighlight = -Infinity;
	for (const p of pages) {
		let used = false;
		for (const t of p.tokens) {
			if (t.highlight && !used && p.startMs - lastHighlight >= captionTokens.highlightMinGapMs) {
				used = true;
				lastHighlight = p.startMs;
			} else {
				t.highlight = false;
			}
		}
	}
	return pages;
}

// видимый интервал страницы: держится до начала следующей (если пауза короче 700 мс), плюс кроссфейд
export function pageWindows(pages: CaptionPage[]): {startMs: number; holdEndMs: number}[] {
	return pages.map((p, k) => {
		const next = pages[k + 1];
		const holdEndMs = next && next.startMs - p.endMs < 700 ? Math.max(p.endMs, next.startMs) : p.endMs + 150;
		return {startMs: p.startMs, holdEndMs};
	});
}

// ---------- положение субтитров рядом со вставками (регламент §4.1: «во время вставок субтитры двигаются») ----------
export function insertZoneKind(ins: Insert): ZoneKind | null {
	switch (ins.kind) {
		case 'chamber_sign':
		case 'result_sign':
		case 'glados_room':
			return ins.kind;
		case 'custom':
			return ins.props.component === 'JournalLog' ? 'journal' : ins.props.component === 'FontSpecimen' ? 'specimen' : null;
		default:
			return null;
	}
}

// целевой центр субтитров, чтобы не перекрыть зону z; null если места нет ни снизу, ни сверху
export function avoidanceRatio(width: number, height: number, base: number, zone: {y: number; h: number}): number | null {
	const o = orientationOf(width, height);
	const half = captionHeightRatio(width, height) / 2;
	const gap = 0.015;
	const top = zone.y;
	const bottom = zone.y + zone.h;
	if (base + half <= top || base - half >= bottom) return base; // не перекрывает
	const below = bottom + gap + half;
	if (below + half <= 1 - safe[o].bottom) return below;
	const above = top - gap - half;
	if (above - half >= safe[o].top) return above;
	return null;
}

// y_ratio субтитров в момент t (мс): плавно уходит от активных вставок (ход 250 мс) и возвращается
export function captionYRatio(plan: MainProps, tMs: number): number {
	const {width, height} = plan.format;
	const o = orientationOf(width, height);
	const base = plan.captions.position?.y_ratio ?? captionTokens.defaultYRatio[o];
	const ramp = motion.exitMs;
	let y = base;
	let best = 0;
	for (const ins of plan.inserts) {
		const kind = insertZoneKind(ins);
		if (!kind || !captionAvoidKinds.includes(kind)) continue;
		const lo = ins.at_ms;
		const hi = ins.at_ms + ins.dur_ms;
		if (tMs < lo - ramp || tMs > hi + ramp) continue;
		const target = avoidanceRatio(width, height, base, zones[kind][o]);
		if (target === null || target === base) continue;
		const enter = Math.min(1, Math.max(0, (tMs - (lo - ramp)) / ramp));
		const exit = Math.min(1, Math.max(0, (hi + ramp - tMs) / ramp));
		const p = Math.min(enter, exit);
		if (p > best) {
			best = p;
			y = base + (target - base) * (p * p * (3 - 2 * p)); // сглаженная ступень без отскока
		}
	}
	return y;
}


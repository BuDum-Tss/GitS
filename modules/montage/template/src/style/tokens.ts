// Токены стиля Aperture Science (STY-4). Источник: docs/APERTURE_STYLE.md. Компоненты берут цвета, шрифты, размеры,
// длительности и зоны только отсюда. Файл самодостаточен (без импортов, без enum): его читает и Node для qa_zones.json.

// ---------- цвета (регламент §3; только эти) ----------
export const colors = {
	labWhite: '#F2F2EE', // основной текст, панели карточек
	labGrey: '#B9BEC4', // второстепенный текст, линии, неактивные пиктограммы
	chamberDark: '#15181C', // фон вставок, плашки субтитров
	portalBlue: '#2AA7FF', // акцент 1: процесс, информация, «вход»
	portalOrange: '#FF8B1F', // акцент 2: результат, выделенное слово, её реплики (метка)
	hazardYellow: '#F4C430', // предупреждающие полосы
	alertRed: '#E5383B', // только провал, ошибки, опасность
} as const;
export type ColorToken = keyof typeof colors;

export function withAlpha(hex: string, alpha: number): string {
	const n = parseInt(hex.slice(1), 16);
	return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
}

export const opacity = {
	captionPlate: 0.72, // плашка субтитров
	gridLine: 0.03, // сетка 48 px на полноэкранных вставках
	glow: 0.4, // свечение акцентов
} as const;

export const grid = {cell: 48} as const;

// свечение только у акцентов: drop-shadow(0 0 12px <акцент> @ 40%)
export function glow(accent: 'portalBlue' | 'portalOrange'): string {
	return `drop-shadow(0 0 12px ${withAlpha(colors[accent], opacity.glow)})`;
}

// ---------- шрифты (регламент §2; файлы в public/fonts, см. assets/fonts/README.md) ----------
// Три узких начертания и «Medium» это вариативная IBM Plex Sans: начертание задают weight и stretch.
export const fonts = {
	caption: {file: 'IBMPlexSansCondensed-SemiBold.ttf', family: 'PlexCaption', weight: 600, stretch: 75},
	heading: {file: 'IBMPlexSansCondensed-Bold.ttf', family: 'PlexHeading', weight: 700, stretch: 75},
	service: {file: 'IBMPlexSans-Medium.ttf', family: 'PlexService', weight: 500, stretch: 100},
	mono: {file: 'IBMPlexMono-Regular.ttf', family: 'PlexMono', weight: 400, stretch: 100},
} as const;
export type FontRole = keyof typeof fonts;

export function fontStyle(role: FontRole): {fontFamily: string; fontWeight: number; fontStretch: string} {
	const f = fonts[role];
	return {fontFamily: `'${f.family}', sans-serif`, fontWeight: f.weight, fontStretch: `${f.stretch}%`};
}

// ---------- форматы и безопасные зоны (регламент §8) ----------
export const formats = {
	vertical: {width: 1080, height: 1920},
	horizontal: {width: 1920, height: 1080},
	square: {width: 1080, height: 1080},
} as const;
export type Orientation = keyof typeof formats;

export function orientationOf(width: number, height: number): Orientation {
	if (height > width) return 'vertical';
	if (width > height) return 'horizontal';
	return 'square';
}

// доли кадра, где нельзя ставить важное: у вертикали верх 12 % и низ 20 % (интерфейс площадок);
// у горизонтали и квадрата поля 5 % (решение исполнителя: в регламенте не заданы)
export const safe = {
	vertical: {top: 0.12, bottom: 0.2},
	horizontal: {top: 0.05, bottom: 0.05},
	square: {top: 0.05, bottom: 0.05},
} as const;

// ---------- движение (регламент §9) ----------
export const motion = {
	enterMs: 300, // вход: ease-out cubic
	exitMs: 250, // выход: ease-in cubic
	minMs: 250,
	maxMs: 400,
	slowZoomMax: 0.04, // наезд камеры не больше 4 % за сцену
	pixelTransitionMs: [400, 600],
	pixelCell: 54, // сторона квадрата перехода, px
} as const;

// ---------- субтитры (регламент §4.1) ----------
export const captions = {
	fontPx: {vertical: 64, horizontal: 56, square: 64},
	defaultYRatio: {vertical: 0.62, horizontal: 0.82, square: 0.72}, // square: решение исполнителя
	maxChars: 28,
	combineMs: 550,
	crossfadeMs: 100,
	plate: {radius: 4, padX: 22, padY: 14},
	voiceBarPx: 4,
	maxWidthRatio: 0.92,
	highlightMinGapMs: 3000,
	lineHeight: 1.2,
} as const;

// ---------- звук (регламент §5) ----------
export const audio = {
	sfxGain: 0.25, // около -12 дБ относительно голоса
	duckGain: 0.2, // исходник под закадром, около -14 дБ
	duckRampMs: 120,
} as const;

// ---------- компоненты ----------
export const chamberCard = {
	maxTitleChars: 40,
	numberPx: 180,
	ofPx: 72,
	titlePx: {vertical: 60, horizontal: 56, square: 56},
	pictogramPx: 96,
	progressDivisions: 19,
	holdMsMin: 1500,
	holdMsMax: 2500,
} as const;

export const hazardBanner = {heightRatio: 0.12, stripeStep: 40, textPx: {vertical: 64, horizontal: 60, square: 60}} as const;
export const testResult = {borderPx: 2, labelPx: 88, textPx: 44} as const;
export const lowerThird = {linePx: 2, textPx: 40, subPx: 30} as const;
export const cta = {textPx: {vertical: 52, horizontal: 48, square: 50}, barPx: 6, maxChars: 80} as const;
export const limits = {title: 40, onScreen: 60, cta: 80, voiceover: 400} as const;

// ---------- зоны компонентов (доли кадра: x, y, w, h) ----------
// Одни и те же значения используют компоненты (размещение) и qa_zones.json (проверки качества, MON-QA-1).
export type Rect = {x: number; y: number; w: number; h: number};
export type ZoneKind =
	| 'chamber_card'
	| 'hazard_banner'
	| 'test_result'
	| 'lower_third'
	| 'cta'
	| 'journal'
	| 'specimen'
	| 'blackout'
	| 'pixel_transition';

export const zones: Record<ZoneKind, Record<Orientation, Rect>> = {
	// белая панель на половину высоты кадра: у вертикали у верхней трети, у остальных по центру
	chamber_card: {
		vertical: {x: 0.07, y: 0.14, w: 0.86, h: 0.5},
		horizontal: {x: 0.2, y: 0.25, w: 0.6, h: 0.5},
		square: {x: 0.07, y: 0.25, w: 0.86, h: 0.5},
	},
	hazard_banner: {
		vertical: {x: 0, y: 0.22, w: 1, h: 0.12},
		horizontal: {x: 0, y: 0.12, w: 1, h: 0.12},
		square: {x: 0, y: 0.15, w: 1, h: 0.12},
	},
	test_result: {
		vertical: {x: 0.08, y: 0.3, w: 0.84, h: 0.24},
		horizontal: {x: 0.25, y: 0.3, w: 0.5, h: 0.36},
		square: {x: 0.1, y: 0.3, w: 0.8, h: 0.34},
	},
	lower_third: {
		vertical: {x: 0.06, y: 0.7, w: 0.7, h: 0.08},
		horizontal: {x: 0.05, y: 0.66, w: 0.4, h: 0.1},
		square: {x: 0.06, y: 0.62, w: 0.6, h: 0.1},
	},
	cta: {
		vertical: {x: 0.06, y: 0.68, w: 0.88, h: 0.11},
		horizontal: {x: 0.2, y: 0.6, w: 0.6, h: 0.14},
		square: {x: 0.08, y: 0.62, w: 0.84, h: 0.14},
	},
	journal: {
		vertical: {x: 0.06, y: 0.14, w: 0.88, h: 0.58},
		horizontal: {x: 0.1, y: 0.12, w: 0.8, h: 0.66},
		square: {x: 0.06, y: 0.1, w: 0.88, h: 0.7},
	},
	specimen: {
		vertical: {x: 0.04, y: 0.13, w: 0.92, h: 0.66},
		horizontal: {x: 0.04, y: 0.08, w: 0.92, h: 0.84},
		square: {x: 0.04, y: 0.08, w: 0.92, h: 0.84},
	},
	blackout: {
		vertical: {x: 0, y: 0, w: 1, h: 1},
		horizontal: {x: 0, y: 0, w: 1, h: 1},
		square: {x: 0, y: 0, w: 1, h: 1},
	},
	pixel_transition: {
		vertical: {x: 0, y: 0, w: 1, h: 1},
		horizontal: {x: 0, y: 0, w: 1, h: 1},
		square: {x: 0, y: 0, w: 1, h: 1},
	},
};

// какие зоны вытесняют субтитры, пока активны (полноэкранные фоны не считаются: они лежат под субтитрами)
export const captionAvoidKinds: ZoneKind[] = ['chamber_card', 'hazard_banner', 'test_result', 'lower_third', 'journal', 'specimen'];

export function zoneRect(kind: ZoneKind, width: number, height: number): {x: number; y: number; w: number; h: number} {
	const r = zones[kind][orientationOf(width, height)];
	return {x: r.x * width, y: r.y * height, w: r.w * width, h: r.h * height};
}

// высота плашки субтитров в долях кадра (одна строка)
export function captionHeightRatio(width: number, height: number): number {
	const o = orientationOf(width, height);
	return (captions.fontPx[o] * captions.lineHeight + 2 * captions.plate.padY) / height;
}

// зона плашки субтитров при заданном y_ratio (центр плашки)
export function captionZone(width: number, height: number, yRatio: number): Rect {
	const h = captionHeightRatio(width, height);
	return {x: (1 - captions.maxWidthRatio) / 2, y: yRatio - h / 2, w: captions.maxWidthRatio, h};
}

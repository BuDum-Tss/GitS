// Токены стиля «Лаборатория Aperture», версия 2 (ТЗ 23, SC-1). Источник: дизайн-система (template/design/tokens.json,
// README и bundle.css) и регламент resources/<Имя>/montage/APERTURE_STYLE.md. Компоненты берут цвета, шрифты, размеры,
// длительности и зоны только отсюда. Файл самодостаточен (без импортов, без enum): его читает и Node для qa_zones.json.

// ---------- цвета (регламент §3; только эти) ----------
export const colors = {
	chamberDark: '#15181c', // подписи рамки HUD; основа плашки субтитров
	'chamberDark-plate': 'rgba(21, 24, 28, 0.72)', // плашка субтитров и подписи поверх видео
	labWhite: '#f2f2ee', // основной текст на chamberDark и плашках
	labGrey: '#b9bec4', // второстепенный текст и линии на chamberDark
	portalBlue: '#2aa7ff', // акцент процесса и информации (на интро и отбивках не используется)
	portalOrange: '#ff8b1f', // её голос: метка закадра и одно выделенное слово на страницу субтитров
	// белый мир отбивок, табличек и интро
	'sign-paper': '#f7f7f5',
	'sign-ink': '#111317',
	'sign-muted': '#5a6068',
	'sign-off': '#cfd3d8',
	// синий мир чертежей
	'bp-center': '#4a9be0',
	'bp-mid': '#2b6fba',
	'bp-edge': '#18467f',
	'bp-line': '#eaf4ff',
	'bp-grid': 'rgba(255, 255, 255, 0.08)',
	'bp-grid-major': 'rgba(255, 255, 255, 0.18)',
} as const;
export type ColorToken = keyof typeof colors;

export function withAlpha(hex: string, alpha: number): string {
	const n = parseInt(hex.slice(1), 16);
	return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
}

export const opacity = {
	captionPlate: 0.72, // плашка субтитров
	hud: 0.68, // рамка HUD (уже вшита в hud-frame.svg, здесь для справки и проверок)
	gridLine: 0.03, // сетка 48 px на полноэкранных вставках-затемнении
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

// Вариативный Plex Sans целиком (оси wght 100-700 и wdth 75-100): сцены v2 берут любой вес и ширину, как bundle.css
// дизайн-системы (`Aperture Cond`). Тот же файл, что и у ролей выше, зарегистрированный с диапазоном осей.
export const plex = {file: 'IBMPlexSans-Medium.ttf', family: 'PlexVariable', weight: '100 700', stretch: '75% 100%'} as const;

export function plexStyle(weight: number, stretch: 75 | 100 = 100): {fontFamily: string; fontWeight: number; fontStretch: string; fontVariationSettings: string} {
	return {fontFamily: `'${plex.family}', sans-serif`, fontWeight: weight, fontStretch: `${stretch}%`, fontVariationSettings: `"wdth" ${stretch}`};
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
} as const;

// длительности дизайн-системы v2 (tokens.json, группа duration), в миллисекундах
export const durations = {
	intro: 4000, // заставка
	bpDraw: 2500, // чертёж рисуется линиями, затем подписи
	petBob: 3200, // покачивание питомца
	petTalk: 280, // пульс питомца в речи
	captionCrossfade: 100,
	bumperHold: {min: 2500, max: 3500}, // сколько держится табличка
	moodCrossfade: 150, // смена эмоции питомца
} as const;

// ---------- субтитры (регламент §4.4): только вертикальные ролики ----------
export const captions = {
	fontPx: {vertical: 64, horizontal: 64, square: 64},
	defaultYRatio: {vertical: 0.62, horizontal: 0.82, square: 0.72}, // горизонталь и квадрат: субтитров в кадре нет, значения для .srt-проверок
	maxChars: 28,
	combineMs: 550,
	crossfadeMs: 100,
	plate: {radius: 4, padX: 22, padY: 14},
	voiceBarPx: 4,
	maxWidthRatio: 0.92,
	highlightMinGapMs: 3000,
	lineHeight: 1.2,
} as const;

// субтитры вжигаются только в вертикальные ролики (ТЗ 23, SC-4); в остальных выдаётся файл .srt
export function captionsBurned(width: number, height: number): boolean {
	return orientationOf(width, height) === 'vertical';
}

// ---------- звук (регламент §5) ----------
export const audio = {
	sfxGain: 0.25, // около -12 дБ относительно голоса
	duckGain: 0.2, // исходник под закадром, около -14 дБ
	duckRampMs: 120,
} as const;

// ---------- сцены v2 ----------
export const limits = {
	subtitle: 60, // подзаголовок интро
	wordmark: 40,
	signBig: 12, // огромное число или слово итога
	signSmall: 50, // строка-дробь
	signTitle: 30, // заголовок таблички, капсом
	bpLabel: 40, // подпись-выноска
	bpTitle: 40,
	bpKicker: 40,
	bpNote: 120,
	hudLabel: 40,
	title: 40,
	onScreen: 60,
	cta: 80,
	voiceover: 400,
	callouts: 4,
	drawing: 200,
	ticks: 30,
} as const;

// Интро: ирис из 8 лопастей. Геометрия как в превью: [R(150,a), R(150,a+45), r(52,a+45+24), r(52,a+24)]
export const iris = {blades: 8, outer: 150, inner: 52, step: 45, twist: 24, viewBox: 160, sizePx: 300, strokePx: 6} as const;
export const intro = {
	gapPx: 40,
	wordPx: 104,
	subPx: 30,
	// тайминги по keyframes превью (4 с): ирис 0-55 %, надпись 40-70 %, подзаголовок 65-85 %
	irisMs: [0, 2200] as const,
	irisFadeMs: 1200,
	wordMs: [1600, 2800] as const,
	subMs: [2600, 3400] as const,
	irisFromDeg: -120,
	irisFromScale: 0.4,
	trackFrom: 0.4, // разрядка надписи, em
	trackTo: 0.14,
} as const;

// Чертёж-blueprint
export const blueprint = {
	cell: 24,
	cellMajor: 120,
	frameInset: 28,
	frameBorderPx: 2,
	wallPx: 10,
	linePx: 3,
	thinPx: 1.6,
	thinOpacity: 0.75,
	dotR: 5,
	titlePx: 64,
	titleSkewDeg: 8,
	notePx: 20,
	kickerPx: 34,
	labelPx: 24,
	padX: 72,
	padBottom: 64,
	leaderOffsetRatio: 160 / 1920, // длина выноски в долях ширины кадра
	labelMaxPx: 460,
	labelMinPx: 170,
	labelEm: 0.6, // средняя ширина знака подписи в em (для оценки числа строк)
	outsidePx: 40, // подпись выноски начинается не ближе этого отступа от рисунка
} as const;

// Табличка камеры (отбивки)
export const sign = {
	widthPx: {vertical: 900, horizontal: 830, square: 900}, // сетка иконок 714 px: в 760 из превью она вылезает за край
	padTop: 40,
	padX: 56,
	padBottom: 48,
	gap: 18,
	bigPx: 300,
	bigMaxWordPx: 260,
	bigWordPx: 230,
	smallPx: 26,
	titlePx: 38,
	tickW: 9,
	tickGap: 7,
	tickH: 64,
	boxPx: 104,
	boxBorderPx: 4,
	iconPx: 76,
	gridCols: 6,
	gridRows: 2,
	ruleThickPx: 4,
	ruleThinPx: 2,
	heightPx: 860, // примерная высота таблички: для зоны проверок качества
	defaultTotal: 19,
	slideMs: 300, // выезд снизу
	fadeMs: 250, // уход к белому
} as const;

// Комната ГЛаДОС
export const room = {
	petHeightRatio: {vertical: 0.42, horizontal: 0.62, square: 0.62},
	petAspect: 145 / 200, // стикеры питомца 145x200 (нужна версия от 1000 px по высоте)
	bobPx: 9,
	bobDeg: 1.2,
	bobOffsetPx: 1,
	talkMaxScale: 1.025,
	moodScaleFrom: 0.96,
	petTopRatio: 0.5,
	petShiftRatio: 0.52, // translate(-50%, -52%)
	moods: ['idle', 'thinking', 'angry', 'happy', 'question', 'surprised'] as const,
} as const;
export type PetMood = (typeof room.moods)[number];

export const hud = {labelLeftPx: 420, labelTopPx: 13, labelPx: 15, trackEm: 0.12, designWidth: 1920} as const;

// эффекты как строки CSS: градиенты и тени живут только здесь (компоненты их не пишут)
export const effects = {
	blueprintBackground: [
		`linear-gradient(${colors['bp-grid']} 1px, transparent 1px) 0 0 / ${blueprint.cell}px ${blueprint.cell}px`,
		`linear-gradient(90deg, ${colors['bp-grid']} 1px, transparent 1px) 0 0 / ${blueprint.cell}px ${blueprint.cell}px`,
		`linear-gradient(${colors['bp-grid-major']} 1.5px, transparent 1.5px) 0 0 / ${blueprint.cellMajor}px ${blueprint.cellMajor}px`,
		`linear-gradient(90deg, ${colors['bp-grid-major']} 1.5px, transparent 1.5px) 0 0 / ${blueprint.cellMajor}px ${blueprint.cellMajor}px`,
		`radial-gradient(ellipse at 50% 45%, ${colors['bp-center']} 0%, ${colors['bp-mid']} 48%, ${colors['bp-edge']} 100%)`,
	].join(', '),
	blueprintFrameLine: 'rgba(234, 244, 255, 0.55)',
	roomVignette: 'radial-gradient(ellipse at 50% 55%, rgba(0, 0, 0, 0) 40%, rgba(8, 10, 12, 0.55) 100%)',
	petDrop: 'drop-shadow(0 30px 40px rgba(0, 0, 0, 0.45))',
	signLift: '0 2px 0 rgba(17, 19, 23, 0.08), 0 30px 60px rgba(17, 19, 23, 0.10)',
} as const;

// ---------- компоненты ----------
export const cta = {textPx: {vertical: 52, horizontal: 48, square: 50}, barPx: 6, maxChars: 80} as const;

// ---------- зоны компонентов (доли кадра: x, y, w, h) ----------
// Одни и те же значения используют компоненты (размещение) и qa_zones.json (проверки качества, MON-QA-1).
// Полноэкранные сцены (интро, чертёж) зон не имеют: они закрывают кадр целиком.
export type Rect = {x: number; y: number; w: number; h: number};
export type ZoneKind = 'chamber_sign' | 'result_sign' | 'glados_room' | 'cta' | 'journal' | 'specimen' | 'blackout';

// табличка по центру кадра (высота sign.heightPx, ширина sign.widthPx)
function signZone(o: Orientation): Rect {
	const f = formats[o];
	const w = sign.widthPx[o] / f.width;
	const h = sign.heightPx / f.height;
	return {x: (1 - w) / 2, y: (1 - h) / 2, w, h};
}

// питомец: по центру, высота по формату (translate -52 % по вертикали, как в превью дизайн-системы)
function petZone(o: Orientation): Rect {
	const f = formats[o];
	const h = room.petHeightRatio[o];
	const w = (h * f.height * room.petAspect) / f.width;
	return {x: (1 - w) / 2, y: room.petTopRatio - room.petShiftRatio * h, w, h};
}

export const zones: Record<ZoneKind, Record<Orientation, Rect>> = {
	chamber_sign: {vertical: signZone('vertical'), horizontal: signZone('horizontal'), square: signZone('square')},
	result_sign: {vertical: signZone('vertical'), horizontal: signZone('horizontal'), square: signZone('square')},
	glados_room: {vertical: petZone('vertical'), horizontal: petZone('horizontal'), square: petZone('square')},
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
};

// какие зоны вытесняют субтитры, пока активны (полноэкранные фоны не считаются: они лежат под субтитрами)
export const captionAvoidKinds: ZoneKind[] = ['chamber_sign', 'result_sign', 'glados_room', 'journal', 'specimen'];

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

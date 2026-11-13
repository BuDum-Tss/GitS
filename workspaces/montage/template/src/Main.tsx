import React from 'react';
import {AbsoluteFill, staticFile, useVideoConfig, type CalculateMetadataFunction} from 'remotion';
import {Blackout} from './components/Blackout';
import {Captions} from './components/Captions';
import {Cta} from './components/Cta';
import {Inserts} from './components/Inserts';
import {Segments} from './components/Segments';
import {Sfx} from './components/Sfx';
import {Voiceover} from './components/Voiceover';
import {loadFonts, useFontsReady} from './fonts';
import {msToFrames} from './motion';
import {mainPropsSchema, type MainProps} from './schema';
import {colors} from './style/tokens';

// Композиция Main (MON-20). Слои снизу вверх: исходники, затемнение, вставки, субтитры, призыв; звук: закадр, эффекты, музыка.
export const Main: React.FC<MainProps> = (plan) => {
	useFontsReady();
	const {durationInFrames} = useVideoConfig();
	return (
		<AbsoluteFill style={{backgroundColor: colors.chamberDark}}>
			<Segments segments={plan.segments} voiceover={plan.voiceover} />
			<Blackout items={plan.blackout} />
			<Inserts inserts={plan.inserts} voiceover={plan.voiceover} />
			<Captions plan={plan} />
			{plan.cta ? <Cta cta={plan.cta} durationInFrames={durationInFrames} /> : null}
			<Voiceover items={plan.voiceover} />
			<Sfx items={plan.sfx} music={plan.music} />
		</AbsoluteFill>
	);
};

// Размер, fps и длительность берутся из plan.json (props). План проверяется схемой zod: ошибка плана это понятная ошибка
// рендера, а не пустой кадр. Здесь же ждём шрифты и подгружаем слова расшифровки (captions.words_file в public/).
export const calculateMetadata: CalculateMetadataFunction<MainProps> = async ({props, abortSignal}) => {
	const parsed = mainPropsSchema.safeParse(props);
	if (!parsed.success) {
		throw new Error(`plan.json не прошёл проверку: ${JSON.stringify(parsed.error.issues.slice(0, 8))}`);
	}
	const plan = parsed.data;
	await loadFonts();
	let words = plan.words;
	if (plan.captions.enabled && plan.captions.source !== 'voiceover' && !words) {
		const res = await fetch(staticFile(plan.captions.words_file), {signal: abortSignal});
		if (!res.ok) throw new Error(`нет файла слов ${plan.captions.words_file} (${res.status}): субтитры по расшифровке невозможны`);
		words = mainPropsSchema.shape.words.unwrap().parse(await res.json());
	}
	return {
		width: plan.format.width,
		height: plan.format.height,
		fps: plan.format.fps,
		durationInFrames: Math.max(1, msToFrames(plan.duration_ms, plan.format.fps)),
		props: {...plan, words},
		defaultOutName: plan.title,
	};
};

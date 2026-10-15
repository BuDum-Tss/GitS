import React from 'react';
import {Composition} from 'remotion';
import {Main, calculateMetadata} from './Main';
import {mainPropsSchema, type MainProps} from './schema';

// Пустой план: рендер настоящего ролика всегда идёт с --props=plan.json
const emptyPlan: MainProps = {
	version: 1,
	format: {width: 1080, height: 1920, fps: 30},
	duration_ms: 1000,
	title: 'plan',
	segments: [],
	captions: {enabled: false, source: 'transcript', style: 'aperture_default', overrides: [], words_file: 'transcript/words.json'},
	voiceover: [],
	inserts: [],
	blackout: [],
	sfx: [],
	music: null,
	cta: null,
};

// Одна композиция Main (регламент §10: несколько версий ролика через props, а не копии композиций). Размер, fps и
// длительность подставляет calculateMetadata из plan.json; значения в Composition нужны только Studio и до подстановки props.
export const RemotionRoot: React.FC = () => (
	<Composition
		id="Main"
		component={Main}
		schema={mainPropsSchema}
		width={1080}
		height={1920}
		fps={30}
		durationInFrames={30}
		defaultProps={emptyPlan}
		calculateMetadata={calculateMetadata}
	/>
);

import React from 'react';
import {Sequence, useVideoConfig} from 'remotion';
import {customComponents} from '../custom';
import {msToFrames} from '../motion';
import type {Insert} from '../schema';
import {ChamberCard} from './ChamberCard';
import {HazardBanner} from './HazardBanner';
import {LowerThird} from './LowerThird';
import {PixelTransition} from './PixelTransition';
import {TestResult} from './TestResult';

const InsertView: React.FC<{insert: Insert; durationInFrames: number}> = ({insert, durationInFrames}) => {
	switch (insert.kind) {
		case 'chamber_card':
			return <ChamberCard insert={insert} durationInFrames={durationInFrames} />;
		case 'hazard_banner':
			return <HazardBanner insert={insert} durationInFrames={durationInFrames} />;
		case 'pixel_transition':
			return <PixelTransition insert={insert} durationInFrames={durationInFrames} />;
		case 'test_result':
			return <TestResult insert={insert} durationInFrames={durationInFrames} />;
		case 'lower_third':
			return <LowerThird insert={insert} durationInFrames={durationInFrames} />;
		case 'custom': {
			const Component = customComponents[insert.props.component];
			if (!Component) throw new Error(`custom: компонент "${insert.props.component}" не зарегистрирован в src/custom/index.ts`);
			return <Component props={insert.props} durationInFrames={durationInFrames} />;
		}
	}
};

// Вставки по плану; переходы «пиксели» рисуются последними (поверх остальных вставок), но под субтитрами
export const Inserts: React.FC<{inserts: Insert[]}> = ({inserts}) => {
	const {fps} = useVideoConfig();
	const ordered = [...inserts.filter((i) => i.kind !== 'pixel_transition'), ...inserts.filter((i) => i.kind === 'pixel_transition')];
	return (
		<>
			{ordered.map((ins) => {
				const dur = Math.max(1, msToFrames(ins.dur_ms, fps));
				return (
					<Sequence key={ins.id} name={`${ins.kind}:${ins.id}`} from={msToFrames(ins.at_ms, fps)} durationInFrames={dur} layout="none">
						<InsertView insert={ins} durationInFrames={dur} />
					</Sequence>
				);
			})}
		</>
	);
};

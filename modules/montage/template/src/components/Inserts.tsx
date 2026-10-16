import React from 'react';
import {Sequence, useVideoConfig} from 'remotion';
import {customComponents} from '../custom';
import {msToFrames} from '../motion';
import type {Insert, Voiceover} from '../schema';
import {Blueprint} from './Blueprint';
import {GladosRoom} from './GladosRoom';
import {Intro} from './Intro';
import {ChamberSign, ResultSign} from './Sign';

const InsertView: React.FC<{insert: Insert; durationInFrames: number; voiceover: Voiceover[]}> = ({insert, durationInFrames, voiceover}) => {
	switch (insert.kind) {
		case 'intro':
			return <Intro insert={insert} durationInFrames={durationInFrames} />;
		case 'blueprint':
			return <Blueprint insert={insert} durationInFrames={durationInFrames} />;
		case 'glados_room':
			return <GladosRoom insert={insert} durationInFrames={durationInFrames} voiceover={voiceover} />;
		case 'chamber_sign':
			return <ChamberSign insert={insert} durationInFrames={durationInFrames} />;
		case 'result_sign':
			return <ResultSign insert={insert} durationInFrames={durationInFrames} />;
		case 'custom': {
			const Component = customComponents[insert.props.component];
			if (!Component) throw new Error(`custom: компонент "${insert.props.component}" не зарегистрирован в src/custom/index.ts`);
			return <Component props={insert.props} durationInFrames={durationInFrames} />;
		}
	}
};

// Вставки по плану в порядке массива (позже в плане: выше по слоям)
export const Inserts: React.FC<{inserts: Insert[]; voiceover: Voiceover[]}> = ({inserts, voiceover}) => {
	const {fps} = useVideoConfig();
	return (
		<>
			{inserts.map((ins) => {
				const dur = Math.max(1, msToFrames(ins.dur_ms, fps));
				return (
					<Sequence key={ins.id} name={`${ins.kind}:${ins.id}`} from={msToFrames(ins.at_ms, fps)} durationInFrames={dur} layout="none">
						<InsertView insert={ins} durationInFrames={dur} voiceover={voiceover} />
					</Sequence>
				);
			})}
		</>
	);
};

import {Audio} from '@remotion/media';
import React from 'react';
import {Sequence, staticFile, useVideoConfig} from 'remotion';
import {msToFrames} from '../motion';
import type {MainProps} from '../schema';
import {audio} from '../style/tokens';

// Звуковые эффекты: только синтезированные assets/sfx; на 10-14 дБ тише голоса (audio.sfxGain), по плану, сами не включаются
export const Sfx: React.FC<{items: MainProps['sfx']; music: MainProps['music']}> = ({items, music}) => {
	const {fps} = useVideoConfig();
	return (
		<>
			{items.map((s, k) => (
				<Sequence key={`${s.name}-${k}`} from={msToFrames(s.at_ms, fps)} layout="none">
					<Audio src={staticFile(`sfx/${s.name}.wav`)} volume={s.volume ?? audio.sfxGain} />
				</Sequence>
			))}
			{music ? <Audio src={staticFile(music.src.replace(/^public\//, ''))} volume={music.volume} loop /> : null}
		</>
	);
};

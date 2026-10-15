import {Audio} from '@remotion/media';
import React from 'react';
import {Sequence, staticFile, useVideoConfig} from 'remotion';
import {msToFrames} from '../motion';
import type {Voiceover as VoiceoverItem} from '../schema';

// путь к синтезированной реплике: tts_path из плана (всё после public/) или public/vo/<id>.wav
export function voiceoverUrl(v: VoiceoverItem): string {
	if (v.tts_path) {
		const norm = v.tts_path.split(String.fromCharCode(92)).join('/');
		const i = norm.lastIndexOf('public/');
		return staticFile(i >= 0 ? norm.slice(i + 'public/'.length) : norm);
	}
	return staticFile(`vo/${v.id}.wav`);
}

// Её закадр: файлы public/vo (синтез через инструмент synthesize_voice). Голос главный: громкость 1
export const Voiceover: React.FC<{items: VoiceoverItem[]}> = ({items}) => {
	const {fps} = useVideoConfig();
	return (
		<>
			{items.map((v) => (
				<Sequence key={v.id} from={msToFrames(v.at_ms, fps)} layout="none">
					<Audio src={voiceoverUrl(v)} volume={1} />
				</Sequence>
			))}
		</>
	);
};

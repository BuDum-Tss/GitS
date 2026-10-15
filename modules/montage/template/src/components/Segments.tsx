import {Video} from '@remotion/media';
import React from 'react';
import {AbsoluteFill, Sequence, interpolate, staticFile, useCurrentFrame, useVideoConfig} from 'remotion';
import {msToFrames} from '../motion';
import type {MainProps, Segment} from '../schema';
import {audio, colors} from '../style/tokens';

// Путь из плана (sources/x_h264.mp4 в папке задачи) -> файл в public/media (ссылки на исходники, ТЗ 20 §6)
export function mediaUrl(source: string): string {
	const name = source.split(String.fromCharCode(92)).join('/').split('/').pop() as string;
	return staticFile(`media/${name}`);
}

// Коэффициент громкости исходника: под закадром (voiceover) падает до duckGain с короткими рампами (CRAFT §4)
export function duckFactor(tMs: number, vo: MainProps['voiceover']): number {
	let factor = 1;
	for (const v of vo) {
		const dur = v.duration_ms ?? Math.max(1000, v.text.length * 62);
		const a = v.at_ms;
		const b = v.at_ms + dur;
		const inRamp = interpolate(tMs, [a - audio.duckRampMs, a], [0, 1], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'});
		const outRamp = interpolate(tMs, [b, b + audio.duckRampMs], [1, 0], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'});
		const depth = Math.min(inRamp, outRamp);
		factor = Math.min(factor, 1 - depth * (1 - audio.duckGain));
	}
	return factor;
}

const SegmentView: React.FC<{seg: Segment; vo: MainProps['voiceover']}> = ({seg, vo}) => {
	const {fps} = useVideoConfig();
	const fromF = msToFrames(seg.at_ms, fps);
	const durF = Math.max(1, msToFrames(seg.out_ms - seg.in_ms, fps));
	return (
		<Sequence from={fromF} durationInFrames={durF} layout="none">
			<ZoomBox durationInFrames={durF} zoom={seg.zoom}>
				<Video
					src={mediaUrl(seg.source)}
					trimBefore={msToFrames(seg.in_ms, fps)}
					volume={(f) => seg.volume * duckFactor(((fromF + f) * 1000) / fps, vo)}
					objectFit="cover"
					style={{width: '100%', height: '100%', objectPosition: 'center'}}
				/>
			</ZoomBox>
		</Sequence>
	);
};

// медленный наезд на сцену (не больше 4 %), без тряски; origin в центре (кадрирование face пока равно center)
const ZoomBox: React.FC<{durationInFrames: number; zoom: number; children: React.ReactNode}> = ({durationInFrames, zoom, children}) => {
	const frame = useCurrentFrame();
	const scale = 1 + zoom * Math.min(1, frame / Math.max(1, durationInFrames));
	return <AbsoluteFill style={{transform: `scale(${scale})`, transformOrigin: '50% 50%', backgroundColor: colors.chamberDark}}>{children}</AbsoluteFill>;
};

export const Segments: React.FC<{segments: Segment[]; voiceover: MainProps['voiceover']}> = ({segments, voiceover}) => (
	<>
		{segments.map((s) => (
			<SegmentView key={s.id} seg={s} vo={voiceover} />
		))}
	</>
);

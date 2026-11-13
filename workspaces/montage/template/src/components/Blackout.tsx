import React from 'react';
import {AbsoluteFill, useCurrentFrame, useVideoConfig} from 'remotion';
import {presenceAt, msToFrames} from '../motion';
import type {MainProps} from '../schema';
import {GridBackground} from './Grid';

// Затемнение (4.8): chamberDark с сеткой поверх видео; звук и субтитры остаются (слои выше и аудио не затрагиваются)
export const Blackout: React.FC<{items: MainProps['blackout']}> = ({items}) => {
	const frame = useCurrentFrame();
	const {fps, width, height} = useVideoConfig();
	return (
		<>
			{items.map((b, k) => {
				const from = msToFrames(b.from_ms, fps);
				const dur = Math.max(1, msToFrames(b.to_ms, fps) - from);
				const local = frame - from;
				if (local < 0 || local >= dur) return null;
				return (
					<AbsoluteFill key={k} style={{opacity: presenceAt(local, dur, fps, 250, 250) * b.opacity}}>
						<GridBackground width={width} height={height} />
					</AbsoluteFill>
				);
			})}
		</>
	);
};

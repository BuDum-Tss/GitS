import React from 'react';
import {useCurrentFrame, useVideoConfig} from 'remotion';
import {msToFrames, presenceAt} from '../motion';
import type {MainProps} from '../schema';
import {colors, cta as T, fontStyle, orientationOf, zoneRect} from '../style/tokens';

// Призыв (CTA): текст приходит из плана (пишет модель), показывается с at_ms до конца ролика. Плашка chamberDark с
// меткой portalOrange; в безопасной зоне площадки.
export const Cta: React.FC<{cta: NonNullable<MainProps['cta']>; durationInFrames: number}> = ({cta, durationInFrames}) => {
	const frame = useCurrentFrame();
	const {fps, width, height} = useVideoConfig();
	const from = msToFrames(cta.at_ms, fps);
	const local = frame - from;
	if (local < 0) return null;
	const total = Math.max(1, durationInFrames - from);
	const presence = presenceAt(local, total, fps, 300, 250);
	const r = zoneRect('cta', width, height);
	const o = orientationOf(width, height);
	return (
		<div
			style={{
				position: 'absolute',
				left: r.x,
				top: r.y,
				width: r.w,
				minHeight: r.h,
				opacity: presence,
				transform: `translateY(${(1 - presence) * 40}px)`,
				boxSizing: 'border-box',
				backgroundColor: colors.chamberDark,
				borderLeft: `${T.barPx}px solid ${colors.portalOrange}`,
				borderRadius: 4,
				padding: '18px 32px',
				display: 'flex',
				alignItems: 'center',
				color: colors.labWhite,
				...fontStyle('caption'),
				fontSize: T.textPx[o],
				lineHeight: 1.15,
			}}
		>
			{cta.text}
		</div>
	);
};

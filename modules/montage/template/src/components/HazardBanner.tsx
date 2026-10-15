import React from 'react';
import {useVideoConfig} from 'remotion';
import {usePresence} from '../motion';
import type {InsertOf} from '../schema';
import {colors, fontStyle, hazardBanner as T, orientationOf, zoneRect} from '../style/tokens';

// Предупреждающий баннер (4.3): полоса на всю ширину высотой 12 % кадра; диагональные полосы hazardYellow/chamberDark
// (45 градусов, шаг 40 px), по центру плашка chamberDark с текстом капсом. Въезд слева 300 мс, уход 250 мс.
export const HazardBanner: React.FC<{insert: InsertOf<'hazard_banner'>; durationInFrames: number}> = ({insert, durationInFrames}) => {
	const {width, height} = useVideoConfig();
	const o = orientationOf(width, height);
	const presence = usePresence(durationInFrames);
	const r = zoneRect('hazard_banner', width, height);
	const step = T.stripeStep;
	const period = step * 2;
	const stripes: React.ReactNode[] = [];
	for (let x = -r.h; x < width + r.h; x += period) {
		// параллелограмм под 45 градусов: сдвиг по x равен высоте полосы
		stripes.push(<polygon key={x} points={`${x},${r.h} ${x + step},${r.h} ${x + step + r.h},0 ${x + r.h},0`} fill={colors.hazardYellow} />);
	}
	return (
		<div style={{position: 'absolute', left: r.x, top: r.y, width: r.w, height: r.h, transform: `translateX(${-(1 - presence) * width}px)`}}>
			<svg width={r.w} height={r.h} style={{position: 'absolute', left: 0, top: 0}}>
				<rect width={r.w} height={r.h} fill={colors.chamberDark} />
				{stripes}
			</svg>
			<div style={{position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center'}}>
				<div
					style={{
						backgroundColor: colors.chamberDark,
						color: colors.labWhite,
						padding: '0 44px',
						height: r.h * 0.72,
						display: 'flex',
						alignItems: 'center',
						maxWidth: r.w * 0.92,
						...fontStyle('heading'),
						fontSize: T.textPx[o],
						textTransform: 'uppercase',
						whiteSpace: 'nowrap',
					}}
				>
					{insert.props.text}
				</div>
			</div>
		</div>
	);
};

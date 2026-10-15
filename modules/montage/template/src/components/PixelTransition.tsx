import React, {useMemo} from 'react';
import {random, useCurrentFrame, useVideoConfig} from 'remotion';
import type {InsertOf} from '../schema';
import {colors, motion} from '../style/tokens';

// Переход «пиксели» (4.5): сетка квадратов chamberDark закрывает кадр (direction out) или открывает его (in) в случайном
// порядке за 400-600 мс; порядок один и тот же в обе стороны, поэтому вход и выход симметричны.
export const PixelTransition: React.FC<{insert: InsertOf<'pixel_transition'>; durationInFrames: number}> = ({insert, durationInFrames}) => {
	const frame = useCurrentFrame();
	const {width, height} = useVideoConfig();
	const cell = motion.pixelCell;
	const cols = Math.ceil(width / cell);
	const rows = Math.ceil(height / cell);
	const thresholds = useMemo(() => Array.from({length: cols * rows}, (_, i) => random(`pixel-${insert.id}-${i}`)), [cols, rows, insert.id]);
	const p = Math.min(1, Math.max(0, frame / Math.max(1, durationInFrames - 1)));
	const covered = insert.props.direction === 'out' ? p : 1 - p;
	const rects: React.ReactNode[] = [];
	if (covered > 0) {
		for (let i = 0; i < thresholds.length; i++) {
			if (covered * 1.0001 > thresholds[i]) {
				rects.push(<rect key={i} x={(i % cols) * cell} y={Math.floor(i / cols) * cell} width={cell + 0.5} height={cell + 0.5} />);
			}
		}
	}
	return (
		<svg width={width} height={height} style={{position: 'absolute', left: 0, top: 0}} shapeRendering="crispEdges" fill={colors.chamberDark}>
			{rects}
		</svg>
	);
};

import React from 'react';
import {useVideoConfig} from 'remotion';
import {usePresence} from '../motion';
import type {InsertOf} from '../schema';
import {colors, fontStyle, lowerThird as T, opacity, withAlpha, zoneRect} from '../style/tokens';

// Подпись (4.7): линия portalBlue 2 px и текст Medium на плашке, слева снизу. Въезд слева 300 мс, уход 250 мс.
export const LowerThird: React.FC<{insert: InsertOf<'lower_third'>; durationInFrames: number}> = ({insert, durationInFrames}) => {
	const {width, height} = useVideoConfig();
	const presence = usePresence(durationInFrames);
	const r = zoneRect('lower_third', width, height);
	return (
		<div style={{position: 'absolute', left: r.x, top: r.y, transform: `translateX(${-(1 - presence) * (r.x + r.w)}px)`, maxWidth: r.w}}>
			<div style={{height: T.linePx, backgroundColor: colors.portalBlue}} />
			<div style={{backgroundColor: withAlpha(colors.chamberDark, opacity.captionPlate), padding: '12px 24px', display: 'flex', flexDirection: 'column', gap: 4}}>
				<span style={{...fontStyle('service'), fontSize: T.textPx, lineHeight: 1.15, color: colors.labWhite}}>{insert.props.text}</span>
				{insert.props.sub ? <span style={{...fontStyle('service'), fontSize: T.subPx, lineHeight: 1.15, color: colors.labGrey}}>{insert.props.sub}</span> : null}
			</div>
		</div>
	);
};

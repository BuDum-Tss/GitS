import React from 'react';
import {useVideoConfig} from 'remotion';
import {GridBackground} from '../components/Grid';
import {colors, fontStyle, fonts, zoneRect, type FontRole} from '../style/tokens';
import type {CustomProps} from './types';

// Проверка кириллицы (STY-2): одна и та же строка (props.text) каждым начертанием из регламента. Служебная вставка для
// разработки шаблона; надписей-констант нет, строка приходит из плана (demo/plan.cyrillic.json).
export const FontSpecimen: React.FC<CustomProps> = ({props}) => {
	const {width, height} = useVideoConfig();
	const r = zoneRect('specimen', width, height);
	const text = typeof props.text === 'string' ? props.text : '';
	const roles = Object.keys(fonts) as FontRole[];
	const size = Math.min(72, (r.w - 56) / Math.max(1, text.length * 0.6)); // строка должна влезть самым широким начертанием
	return (
		<div style={{position: 'absolute', left: r.x, top: r.y, width: r.w, height: r.h, border: `2px solid ${colors.labGrey}`, boxSizing: 'border-box', overflow: 'hidden'}}>
			<GridBackground width={r.w} height={r.h} />
			<div style={{position: 'absolute', inset: 0, padding: 28, display: 'flex', flexDirection: 'column', justifyContent: 'space-around'}}>
				{roles.map((role) => (
					<div key={role}>
						<div style={{fontFamily: 'monospace', fontSize: 22, color: colors.labGrey}}>
							{role}: {fonts[role].family} / weight {fonts[role].weight} / width {fonts[role].stretch}
						</div>
						<div style={{...fontStyle(role), fontSize: size, lineHeight: 1.15, color: colors.labWhite, whiteSpace: 'nowrap'}}>{text}</div>
					</div>
				))}
			</div>
		</div>
	);
};

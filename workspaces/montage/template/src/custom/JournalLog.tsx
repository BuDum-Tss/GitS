import React from 'react';
import {useCurrentFrame, useVideoConfig} from 'remotion';
import {GridBackground} from '../components/Grid';
import {usePresence} from '../motion';
import {colors, fontStyle, zoneRect} from '../style/tokens';
import type {CustomProps} from './types';

// Журнал испытания (4.9): моноширинный текст построчно, как лог. Набор по символам не быстрее 40 символов в секунду.
// props: lines (строки журнала: время, событие, вывод; пишет модель), cps (символов в секунду, не больше 40).
const MAX_CPS = 40;

export const JournalLog: React.FC<CustomProps> = ({props, durationInFrames}) => {
	const frame = useCurrentFrame();
	const {fps, width, height} = useVideoConfig();
	const presence = usePresence(durationInFrames);
	const r = zoneRect('journal', width, height);
	const lines = Array.isArray(props.lines) ? (props.lines as unknown[]).map(String) : [];
	const cps = Math.min(MAX_CPS, typeof props.cps === 'number' ? props.cps : MAX_CPS);
	let budget = Math.floor((frame / fps) * cps);
	const shown = lines.map((line) => {
		const part = line.slice(0, Math.max(0, budget));
		budget -= line.length + 1;
		return part;
	});
	return (
		<div
			style={{
				position: 'absolute',
				left: r.x,
				top: r.y,
				width: r.w,
				height: r.h,
				opacity: presence,
				overflow: 'hidden',
				border: `2px solid ${colors.labGrey}`,
				borderRadius: 4,
				boxSizing: 'border-box',
			}}
		>
			<GridBackground width={r.w} height={r.h} />
			<div style={{position: 'absolute', inset: 0, padding: 32, ...fontStyle('mono'), fontSize: 34, lineHeight: 1.45, color: colors.labWhite, whiteSpace: 'pre-wrap'}}>
				{shown.map((s, i) => (
					<div key={i} style={{minHeight: 34 * 1.45}}>
						{s}
					</div>
				))}
			</div>
		</div>
	);
};

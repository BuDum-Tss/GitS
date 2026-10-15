import React from 'react';
import {useVideoConfig} from 'remotion';
import {usePresence} from '../motion';
import type {InsertOf} from '../schema';
import {colors, fontStyle, glow, testResult as T, zoneRect} from '../style/tokens';

// Результат испытания (4.4): панель chamberDark с рамкой 2 px (успех portalBlue, провал alertRed); подпись результата и
// вывод приходят из плана. Знак результата рисуется фигурами, надписей-констант нет (STY-5).
export const TestResult: React.FC<{insert: InsertOf<'test_result'>; durationInFrames: number}> = ({insert, durationInFrames}) => {
	const {width, height} = useVideoConfig();
	const presence = usePresence(durationInFrames);
	const r = zoneRect('test_result', width, height);
	const pass = insert.props.result === 'pass';
	const accent = pass ? colors.portalBlue : colors.alertRed;
	const mark = Math.min(r.h * 0.5, 140);
	return (
		<div
			style={{
				position: 'absolute',
				left: r.x,
				top: r.y,
				width: r.w,
				height: r.h,
				opacity: presence,
				transform: `translateY(${(1 - presence) * 40}px)`,
				boxSizing: 'border-box',
				backgroundColor: colors.chamberDark,
				border: `${T.borderPx}px solid ${accent}`,
				borderRadius: 4,
				display: 'flex',
				alignItems: 'center',
				gap: 40,
				padding: '0 48px',
				filter: pass ? glow('portalBlue') : undefined,
			}}
		>
			<svg width={mark} height={mark} viewBox="0 0 48 48" fill="none" stroke={accent} strokeWidth={4} strokeLinejoin="miter" style={{flexShrink: 0}}>
				{pass ? <path d="M7 25 L19 37 L41 11" /> : <path d="M10 10 L38 38 M38 10 L10 38" />}
			</svg>
			<div style={{display: 'flex', flexDirection: 'column', gap: 14, minWidth: 0}}>
				{insert.props.label ? (
					<span style={{...fontStyle('heading'), fontSize: T.labelPx, lineHeight: 1, color: accent, textTransform: 'uppercase'}}>{insert.props.label}</span>
				) : null}
				<span style={{...fontStyle('caption'), fontSize: T.textPx, lineHeight: 1.15, color: colors.labWhite}}>{insert.props.text}</span>
			</div>
		</div>
	);
};

import React from 'react';
import {AbsoluteFill, useCurrentFrame, useVideoConfig} from 'remotion';
import type {InsertOf} from '../schema';
import {introState, introWordPx, irisBlades} from '../sceneMath';
import {colors, fontStyle, intro as T, iris, plexStyle} from '../style/tokens';

// Интро (сцена 1, дизайн-система v2): белый лист, ирис из восьми лопастей собирается вращением (0-2,2 с), затем надпись
// (1,6-2,8 с, разрядка сжимается с 0,4 до 0,14 em) и подзаголовок (2,6-3,4 с). Анимация покадровая, без отскока.
export const Intro: React.FC<{insert: InsertOf<'intro'>; durationInFrames: number}> = ({insert}) => {
	const frame = useCurrentFrame();
	const {fps, width} = useVideoConfig();
	const s = introState((frame * 1000) / fps);
	const half = iris.viewBox;
	return (
		<AbsoluteFill style={{backgroundColor: colors['sign-paper'], display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: T.gapPx}}>
			<div style={{transform: `rotate(${s.irisRotDeg}deg) scale(${s.irisScale})`, opacity: s.irisOpacity}}>
				<svg viewBox={`${-half} ${-half} ${half * 2} ${half * 2}`} width={iris.sizePx} height={iris.sizePx} style={{display: 'block'}}>
					{irisBlades().map((points, i) => (
						<polygon key={i} points={points} fill={colors['sign-ink']} stroke={colors['sign-paper']} strokeWidth={iris.strokePx} strokeLinejoin="round" />
					))}
				</svg>
			</div>
			<div style={{...plexStyle(600, 100), fontSize: introWordPx(insert.props.wordmark, width), lineHeight: 1, letterSpacing: `${s.wordTrackEm}em`, color: colors['sign-ink'], opacity: s.wordOpacity, whiteSpace: 'nowrap'}}>
				{insert.props.wordmark}
			</div>
			<div style={{...fontStyle('mono'), fontSize: T.subPx, lineHeight: 1.2, color: colors['sign-muted'], opacity: s.subOpacity, textAlign: 'center', maxWidth: width * 0.9}}>{insert.props.subtitle}</div>
		</AbsoluteFill>
	);
};

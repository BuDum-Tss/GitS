import React from 'react';
import {AbsoluteFill, useCurrentFrame, useVideoConfig} from 'remotion';
import type {InsertOf} from '../schema';
import {signBigPx, signPhase} from '../sceneMath';
import {signIcons} from '../style/icons';
import {colors, effects, orientationOf, plexStyle, sign as T} from '../style/tokens';
import {Icon} from './Icon';

type SignInsert = InsertOf<'chamber_sign'> | InsertOf<'result_sign'>;

// Табличка тестовой камеры на белом (сцена 5): линия 4 px, огромное число или слово, линия 2 px, строка-дробь, штрихи
// прогресса и «…», линия, сетка иконок 6x2, заголовок капсом. Выезд снизу 300 мс, держится, уход к белому 250 мс.
const SignView: React.FC<{insert: SignInsert; durationInFrames: number}> = ({insert, durationInFrames}) => {
	const frame = useCurrentFrame();
	const {fps, width, height} = useVideoConfig();
	const o = orientationOf(width, height);
	const {big, small, done, total, active, title} = insert.props;
	const {slide, fade} = signPhase((frame * 1000) / fps, (durationInFrames * 1000) / fps);
	const boxW = T.widthPx[o];
	const bigPx = signBigPx(big, boxW - 2 * T.padX);
	const ink = colors['sign-ink'];
	const rule = (px: number) => <div style={{height: px, backgroundColor: ink}} />;
	return (
		<AbsoluteFill style={{backgroundColor: colors['sign-paper'], display: 'flex', alignItems: 'center', justifyContent: 'center'}}>
			<div
				style={{
					width: boxW,
					boxSizing: 'border-box',
					padding: `${T.padTop}px ${T.padX}px ${T.padBottom}px`,
					display: 'flex',
					flexDirection: 'column',
					gap: T.gap,
					backgroundColor: colors['sign-paper'],
					color: ink,
					boxShadow: effects.signLift,
					opacity: fade,
					transform: `translateY(${(1 - slide) * height * 0.75}px)`,
				}}
			>
				{rule(T.ruleThickPx)}
				<div style={{...plexStyle(300, 75), fontSize: bigPx, lineHeight: 0.86, letterSpacing: '-0.01em', margin: '6px 0 -4px', whiteSpace: 'nowrap'}}>{big}</div>
				{rule(T.ruleThinPx)}
				<div style={{...plexStyle(500, 100), fontSize: T.smallPx}}>{small}</div>
				<div style={{display: 'flex', alignItems: 'flex-end', gap: T.tickGap, height: T.tickH}}>
					{Array.from({length: total}, (_, i) => (
						<i key={i} style={{width: T.tickW, height: '100%', backgroundColor: i < done ? ink : colors['sign-off']}} />
					))}
					<b style={{...plexStyle(700, 100), fontSize: 40, lineHeight: 1, marginLeft: 6}}>…</b>
				</div>
				{rule(T.ruleThinPx)}
				<div style={{display: 'grid', gridTemplateColumns: `repeat(${T.gridCols}, ${T.boxPx}px)`, gap: T.gap, justifyContent: 'space-between'}}>
					{signIcons(active).map(({name, on}, i) => {
						const c = on ? ink : colors['sign-off'];
						return (
							<span
								key={`${name}${i}`}
								style={{
									display: 'flex',
									alignItems: 'center',
									justifyContent: 'center',
									boxSizing: 'border-box',
									width: T.boxPx,
									height: T.boxPx,
									border: `${T.boxBorderPx}px solid ${c}`,
									borderRadius: 3,
									color: c,
									backgroundColor: colors['sign-paper'],
								}}
							>
								<Icon name={name} size={T.iconPx} />
							</span>
						);
					})}
				</div>
				<div style={{...plexStyle(700, 100), fontSize: T.titlePx, textTransform: 'uppercase', letterSpacing: '0.02em'}}>{title}</div>
			</div>
		</AbsoluteFill>
	);
};

export const ChamberSign: React.FC<{insert: InsertOf<'chamber_sign'>; durationInFrames: number}> = (p) => <SignView {...p} />;
export const ResultSign: React.FC<{insert: InsertOf<'result_sign'>; durationInFrames: number}> = (p) => <SignView {...p} />;

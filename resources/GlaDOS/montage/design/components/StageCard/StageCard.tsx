import React from 'react';
import {AbsoluteFill, Sequence, useCurrentFrame, useVideoConfig} from 'remotion';
import {ChamberSign} from '../components/Sign';
import {msToFrames} from '../motion';
import type {InsertOf} from '../schema';
import {colors, plexStyle} from '../style/tokens';
import {Cursor, TypeSound, typedCount, useEnter, useExit} from './lab';
import type {CustomProps} from './types';

// Карточка этапа: слева крупно сначала «Этап», небольшая пауза, затем название этапа (всё проявляется по частям с печатью),
// и только потом справа появляется готовая табличка камеры. Тексты из plan.json.
type Line = {text: string; size: number; weight: number};
type SplitProps = {component: 'TitleSplit'; lines: Line[]; start_ms: number; cps: number; gap_ms: number; sign_at_ms: number; typing_src?: string; sign: InsertOf<'chamber_sign'>['props']};

export const TitleSplit: React.FC<CustomProps> = ({props, durationInFrames}) => {
	const p = props as unknown as SplitProps;
	const frame = useCurrentFrame();
	const {fps} = useVideoConfig();
	const enter = useEnter(0, 250);
	const exit = useExit(durationInFrames, 250);
	const starts: number[] = [];
	let at = p.start_ms;
	p.lines.forEach((l, i) => {
		starts.push(at);
		at += (l.text.length / p.cps) * 1000 + (i === 0 ? p.gap_ms : 0);
	});
	const signFrom = msToFrames(p.sign_at_ms, fps);
	const insert = {id: 'stage-sign', at_ms: 0, beat: 'stage', kind: 'chamber_sign', dur_ms: Math.round(((durationInFrames - signFrom) * 1000) / fps), props: p.sign} as InsertOf<'chamber_sign'>;
	const t = (frame * 1000) / fps;
	const lastDone = t >= starts[p.lines.length - 1] + (p.lines[p.lines.length - 1].text.length / p.cps) * 1000;
	return (
		<AbsoluteFill style={{backgroundColor: colors['sign-paper'], opacity: exit}}>
			<TypeSound src={p.typing_src} />
			<Sequence from={signFrom} layout="none">
				<AbsoluteFill style={{transform: 'translateX(470px)'}}>
					<ChamberSign insert={insert} durationInFrames={durationInFrames - signFrom} />
				</AbsoluteFill>
			</Sequence>
			<div style={{position: 'absolute', left: 90, top: 0, bottom: 0, width: 900, display: 'flex', flexDirection: 'column', justifyContent: 'center', gap: 26, opacity: enter}}>
				{p.lines.map((l, i) => {
					const n = typedCount(frame, fps, starts[i], p.cps, l.text.length);
					const active = n > 0 && n < l.text.length;
					const lastLine = i === p.lines.length - 1;
					const pausing = i === 0 && n >= l.text.length && t < starts[1];
					return (
						<div key={i} style={{...plexStyle(l.weight, 75), fontSize: l.size, lineHeight: 1.02, color: colors['sign-ink'], letterSpacing: '-0.005em', minHeight: l.size * 1.02}}>
							{l.text.slice(0, n)}
							{active || pausing || (lastLine && lastDone) ? <Cursor height={l.size * 0.78} width={Math.max(10, l.size * 0.12)} color={colors.portalOrange} /> : null}
						</div>
					);
				})}
			</div>
		</AbsoluteFill>
	);
};

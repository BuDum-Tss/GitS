import React, {useMemo} from 'react';
import {AbsoluteFill, interpolate, useCurrentFrame, useVideoConfig} from 'remotion';
import type {MainProps} from '../schema';
import {captions as T, captionsBurned, colors, fontStyle, orientationOf, withAlpha, opacity} from '../style/tokens';
import {buildPages, captionWords, captionYRatio, pageWindows} from '../timeline';

// Субтитры по словам (4.1), только вертикальные ролики (ТЗ 23, SC-4: в горизонтальных и квадратных рядом с финалом кладётся .srt): createTikTokStyleCaptions (550 мс), кроссфейд ~100 мс, без запятых, без теней и масштабирования.
// Её реплики помечены полоской portalOrange слева; одно ключевое слово на страницу можно выделить portalOrange.
export const Captions: React.FC<{plan: MainProps}> = ({plan}) => {
	const frame = useCurrentFrame();
	const {fps, width, height} = useVideoConfig();
	const pages = useMemo(() => buildPages(captionWords(plan)), [plan]);
	const windows = useMemo(() => pageWindows(pages), [pages]);
	if (!plan.captions.enabled || !captionsBurned(width, height)) return null; // субтитры только в вертикальных роликах (SC-4)
	const tMs = (frame * 1000) / fps;
	const o = orientationOf(width, height);
	const fontPx = T.fontPx[o];
	const yCenter = captionYRatio(plan, tMs) * height;
	const fade = T.crossfadeMs;

	return (
		<AbsoluteFill style={{pointerEvents: 'none'}}>
			{pages.map((p, k) => {
				const w = windows[k];
				if (tMs < w.startMs || tMs > w.holdEndMs + fade) return null;
				const op = Math.min(
					interpolate(tMs, [w.startMs, w.startMs + fade], [0, 1], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'}),
					interpolate(tMs, [w.holdEndMs, w.holdEndMs + fade], [1, 0], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'}),
				);
				return (
					<div
						key={k}
						style={{
							position: 'absolute',
							left: 0,
							width,
							top: yCenter,
							transform: 'translateY(-50%)',
							display: 'flex',
							justifyContent: 'center',
							opacity: op,
						}}
					>
						<div
							style={{
								position: 'relative',
								maxWidth: width * T.maxWidthRatio,
								padding: `${T.plate.padY}px ${T.plate.padX}px`,
								paddingLeft: T.plate.padX + (p.isVoice ? T.voiceBarPx : 0),
								borderRadius: T.plate.radius,
								backgroundColor: withAlpha(colors.chamberDark, opacity.captionPlate),
								color: colors.labWhite,
								...fontStyle('caption'),
								fontSize: fontPx,
								lineHeight: T.lineHeight,
								whiteSpace: 'nowrap',
								overflow: 'hidden',
							}}
						>
							{p.isVoice ? (
								<div style={{position: 'absolute', left: 0, top: 0, bottom: 0, width: T.voiceBarPx, backgroundColor: colors.portalOrange}} />
							) : null}
							{p.tokens.map((t, i) => (
								<span key={i} style={t.highlight ? {color: colors.portalOrange} : undefined}>
									{t.text}
								</span>
							))}
						</div>
					</div>
				);
			})}
		</AbsoluteFill>
	);
};

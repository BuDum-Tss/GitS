import React from 'react';
import {AbsoluteFill, Img, staticFile, useVideoConfig} from 'remotion';
import {colors, fontStyle, hud} from '../style/tokens';

// Рамка HUD поверх записи игры (дизайн-система v2): public/hud/hud-frame.svg на весь кадр (прозрачность opacity-hud уже
// внутри файла) и подпись hud-label в верхней планке. Рамка нарисована под 1920x1080; подпись масштабируется по ширине кадра.
export const FootageFrame: React.FC<{label?: string}> = ({label}) => {
	const {width} = useVideoConfig();
	const k = width / hud.designWidth;
	return (
		<AbsoluteFill style={{pointerEvents: 'none'}}>
			<Img src={staticFile('hud/hud-frame.svg')} style={{position: 'absolute', inset: 0, width: '100%', height: '100%'}} />
			{label ? (
				<div
					style={{
						position: 'absolute',
						left: hud.labelLeftPx * k,
						top: hud.labelTopPx * k,
						...fontStyle('mono'),
						fontSize: hud.labelPx * k,
						lineHeight: 1,
						letterSpacing: `${hud.trackEm}em`,
						color: colors.chamberDark,
						whiteSpace: 'nowrap',
					}}
				>
					{label}
				</div>
			) : null}
		</AbsoluteFill>
	);
};

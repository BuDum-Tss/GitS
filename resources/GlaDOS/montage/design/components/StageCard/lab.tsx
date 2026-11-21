import {Audio} from '@remotion/media';
import React from 'react';
import {interpolate, staticFile, useCurrentFrame, useVideoConfig} from 'remotion';
import {easeIn, easeOut} from '../motion';
import {colors, fontStyle} from '../style/tokens';

// Общие мелочи для вставок эксперимента: набор текста по символам, вход/выход без отскоков, кадрирующие скобки.
export const clamp01 = (v: number): number => Math.min(1, Math.max(0, v));

// сколько символов уже набрано: старт в мс от начала вставки, скорость в символах в секунду
export function typedCount(frame: number, fps: number, startMs: number, cps: number, total: number): number {
	const t = (frame * 1000) / fps - startMs;
	return Math.min(total, Math.max(0, Math.floor((t / 1000) * cps)));
}

// конец набора в мс от начала вставки
export const typingEndMs = (startMs: number, cps: number, total: number): number => startMs + (total / cps) * 1000;

// плавный вход (ease-out, 300 мс) после задержки; значение 0..1
export function useEnter(delayMs = 0, durMs = 300): number {
	const frame = useCurrentFrame();
	const {fps} = useVideoConfig();
	return easeOut(clamp01((((frame * 1000) / fps) - delayMs) / durMs));
}

// плавный выход в конце вставки (ease-in, 250 мс); 1 -> 0
export function useExit(durationInFrames: number, durMs = 250): number {
	const frame = useCurrentFrame();
	const {fps} = useVideoConfig();
	const left = ((durationInFrames - frame) * 1000) / fps;
	return 1 - easeIn(clamp01(1 - left / durMs));
}

// мигающий курсор набора: 530 мс период
export const Cursor: React.FC<{color?: string; height: number; width?: number; on?: boolean}> = ({color = colors.portalOrange, height, width = 14, on = true}) => {
	const frame = useCurrentFrame();
	const {fps} = useVideoConfig();
	const blink = Math.floor(((frame * 1000) / fps) / 265) % 2 === 0;
	return (
		<span
			style={{
				display: 'inline-block',
				width,
				height,
				marginLeft: 6,
				verticalAlign: 'baseline',
				backgroundColor: color,
				opacity: on && blink ? 1 : on ? 0.15 : 0,
			}}
		/>
	);
};

// угловые скобки поверх рамки монитора
export const Corners: React.FC<{size?: number; color?: string; weight?: number}> = ({size = 26, color = colors.portalBlue, weight = 3}) => {
	const common: React.CSSProperties = {position: 'absolute', width: size, height: size, borderColor: color, borderStyle: 'solid', borderWidth: 0};
	return (
		<>
			<i style={{...common, left: -8, top: -8, borderTopWidth: weight, borderLeftWidth: weight}} />
			<i style={{...common, right: -8, top: -8, borderTopWidth: weight, borderRightWidth: weight}} />
			<i style={{...common, left: -8, bottom: -8, borderBottomWidth: weight, borderLeftWidth: weight}} />
			<i style={{...common, right: -8, bottom: -8, borderBottomWidth: weight, borderRightWidth: weight}} />
		</>
	);
};

export const mono = (px: number, color: string = colors.labGrey): React.CSSProperties => ({...fontStyle('mono'), fontSize: px, color, letterSpacing: '0.06em', lineHeight: 1.2});

export {interpolate};

// звук печатания: готовая дорожка щелчков (build.py синтезирует её по времени символов); начинается с начала вставки
export const TypeSound: React.FC<{src?: string; volume?: number}> = ({src, volume = 0.5}) => (src ? <Audio src={staticFile(src)} volume={volume} /> : null);

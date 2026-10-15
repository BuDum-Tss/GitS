// Движение по регламенту §9: только ease-out (cubic) на вход и ease-in на выход, без отскоков и упругости.
import {Easing, interpolate, useCurrentFrame, useVideoConfig} from 'remotion';
import {motion} from './style/tokens';

export const easeOut = Easing.out(Easing.cubic);
export const easeIn = Easing.in(Easing.cubic);

export const msToFrames = (ms: number, fps: number): number => Math.round((ms * fps) / 1000);

const clamp01 = (v: number): number => Math.min(1, Math.max(0, v));

// «присутствие» вставки: 0 -> 1 на входе (ease-out), 1 -> 0 на выходе (ease-in); frame считается от начала вставки
export function presenceAt(frame: number, durationInFrames: number, fps: number, enterMs: number = motion.enterMs, exitMs: number = motion.exitMs): number {
	const enterF = Math.max(1, msToFrames(enterMs, fps));
	const exitF = Math.max(1, msToFrames(exitMs, fps));
	const enter = easeOut(clamp01(frame / enterF));
	const exit = easeIn(clamp01((frame - (durationInFrames - exitF)) / exitF));
	return enter * (1 - exit);
}

export function usePresence(durationInFrames: number, enterMs: number = motion.enterMs, exitMs: number = motion.exitMs): number {
	const frame = useCurrentFrame();
	const {fps} = useVideoConfig();
	return presenceAt(frame, durationInFrames, fps, enterMs, exitMs);
}

// линейная рампа 0..1 между двумя кадрами (для перехода «пиксели» и симметричных затуханий)
export function ramp(frame: number, from: number, to: number): number {
	return interpolate(frame, [from, to], [0, 1], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'});
}

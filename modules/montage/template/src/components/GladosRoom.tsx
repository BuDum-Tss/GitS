import {getAudioData} from '@remotion/media-utils';
import React, {useEffect, useMemo, useState} from 'react';
import {AbsoluteFill, Img, continueRender, delayRender, staticFile, useCurrentFrame, useVideoConfig} from 'remotion';
import type {InsertOf, Voiceover} from '../schema';
import {buildEnvelope, moodLayers, petBob, petHeightPx, talkLevel, talkScale, type VoiceClip} from '../sceneMath';
import {effects, room} from '../style/tokens';
import {voiceoverUrl} from './Voiceover';

const WINDOW_MS = 33;
type Envelopes = Record<string, {env: number[]; durationMs: number} | null>;

// Огибающая громкости её закадра: wav из public/vo декодируется один раз; нет файла или декодирование не вышло — null
// (тогда вместо громкости идёт ровный пульс pet-talk). Рендер ждёт загрузку через delayRender.
function useEnvelopes(items: Voiceover[]): Envelopes {
	const [envs, setEnvs] = useState<Envelopes>({});
	const key = items.map((v) => `${v.id}:${v.tts_path ?? ''}`).join('|');
	useEffect(() => {
		if (!items.length) return;
		const handle = delayRender('Reading voiceover envelopes', {timeoutInMilliseconds: 60000});
		let live = true;
		Promise.all(
			items.map(async (v): Promise<[string, Envelopes[string]]> => {
				try {
					const data = await getAudioData(voiceoverUrl(v));
					return [v.id, {env: buildEnvelope(data.channelWaveforms[0], data.sampleRate, WINDOW_MS), durationMs: Math.round(data.durationInSeconds * 1000)}];
				} catch {
					return [v.id, null];
				}
			}),
		)
			.then((pairs) => live && setEnvs(Object.fromEntries(pairs)))
			.finally(() => continueRender(handle));
		return () => {
			live = false;
		};
		// eslint-disable-next-line react-hooks/exhaustive-deps
	}, [key]);
	return envs;
}

// Комната ГЛаДОС (сцена 3): фон комнаты, виньетка и крупный питомец по центру. Покой: покачивание pet-bob. Речь: масштаб
// до 1,025 по громкости голоса. Смена эмоции: кроссфейд 150 мс со сжатием 0,96 -> 1. Облачков текста нет.
export const GladosRoom: React.FC<{insert: InsertOf<'glados_room'>; durationInFrames: number; voiceover: Voiceover[]}> = ({insert, voiceover}) => {
	const frame = useCurrentFrame();
	const {fps, width, height} = useVideoConfig();
	const envs = useEnvelopes(voiceover);
	const tMs = insert.at_ms + (frame * 1000) / fps; // время ролика: эмоции, речь и закадр заданы им же
	const clips = useMemo<VoiceClip[]>(
		() =>
			voiceover.map((v) => {
				const e = envs[v.id];
				return {at_ms: v.at_ms, duration_ms: v.duration_ms ?? e?.durationMs ?? Math.max(1000, v.text.length * 62), env: e?.env ?? null};
			}),
		[voiceover, envs],
	);
	const level = talkLevel(tMs, clips, insert.props.speech, WINDOW_MS);
	const bob = petBob(tMs);
	const h = petHeightPx(width, height);
	const w = Math.round(h * room.petAspect);
	const layers = moodLayers(insert.props.moods, tMs);
	return (
		<AbsoluteFill>
			<Img src={staticFile('scenes/glados-room.webp')} style={{position: 'absolute', inset: 0, width: '100%', height: '100%', objectFit: 'cover'}} />
			<div style={{position: 'absolute', inset: 0, background: effects.roomVignette}} />
			<div
				style={{
					position: 'absolute',
					left: '50%',
					top: `${room.petTopRatio * 100}%`,
					width: w,
					height: h,
					filter: effects.petDrop,
					transform: `translate(0px, ${bob.y}px) rotate(${bob.deg}deg) scale(${talkScale(level)}) translate(-50%, -${room.petShiftRatio * 100}%)`,
				}}
			>
				{layers.map((l) => (
					<Img
						key={l.mood}
						src={staticFile(`pet/${l.mood}.png`)}
						style={{position: 'absolute', inset: 0, width: '100%', height: '100%', objectFit: 'contain', opacity: l.opacity, transform: `scale(${l.scale})`}}
					/>
				))}
			</div>
		</AbsoluteFill>
	);
};

// Загрузка шрифтов IBM Plex (MON-20, регламент §10): ждём загрузку, иначе первые кадры выйдут системным шрифтом.
import {loadFont} from '@remotion/fonts';
import {useEffect, useState} from 'react';
import {cancelRender, continueRender, delayRender, staticFile} from 'remotion';
import {fonts, plex} from './style/tokens';

let loading: Promise<void> | null = null;

// одна загрузка на страницу; вызывается и из calculateMetadata, и из компонента (каждая вкладка рендера грузит свою)
export function loadFonts(): Promise<void> {
	if (!loading) {
		loading = Promise.all(
			Object.values(fonts).map((f) =>
				loadFont({
					family: f.family,
					url: staticFile(`fonts/${f.file}`),
					weight: String(f.weight),
					stretch: `${f.stretch}%`,
					display: 'block',
				}),
			).concat([
				// вариативный Plex Sans с диапазоном осей: сцены v2 берут любой вес и ширину (как bundle.css дизайн-системы)
				loadFont({family: plex.family, url: staticFile(`fonts/${plex.file}`), weight: plex.weight, stretch: plex.stretch, display: 'block'}),
			]),
		).then(() => undefined);
	}
	return loading;
}

export function useFontsReady(): void {
	const [handle] = useState(() => delayRender('Loading IBM Plex fonts'));
	useEffect(() => {
		loadFonts()
			.then(() => continueRender(handle))
			.catch((e: unknown) => cancelRender(e));
	}, [handle]);
}

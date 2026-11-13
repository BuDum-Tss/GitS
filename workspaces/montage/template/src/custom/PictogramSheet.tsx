import React from 'react';
import {useVideoConfig} from 'remotion';
import {GridBackground} from '../components/Grid';
import {Pictogram} from '../components/Pictogram';
import {pictogramNames} from '../style/pictograms.generated';
import {colors, fontStyle} from '../style/tokens';
import type {CustomProps} from './types';

// Лист пиктограмм (STY-3, STY-6): все знаки набора в 48 px (рабочий размер) и крупно, для визуальной приёмки.
// Служебная вставка для разработки шаблона; подписи это латинские имена файлов.
export const PictogramSheet: React.FC<CustomProps> = () => {
	const {width, height} = useVideoConfig();
	return (
		<div style={{position: 'absolute', left: 0, top: 0, width, height}}>
			<GridBackground width={width} height={height} />
			<div style={{position: 'absolute', inset: 0, padding: 40, display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 24, alignContent: 'start'}}>
				{pictogramNames.map((n) => (
					<div key={n} style={{display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 14, background: colors.labWhite, padding: 18, borderRadius: 4}}>
						<Pictogram name={n} size={168} />
						<div style={{display: 'flex', gap: 20, alignItems: 'center'}}>
							<Pictogram name={n} size={48} />
							<Pictogram name={n} size={48} color="labGrey" />
						</div>
						<span style={{...fontStyle('service'), fontSize: 24, color: colors.chamberDark}}>{n}</span>
					</div>
				))}
			</div>
		</div>
	);
};

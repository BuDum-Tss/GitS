import React from 'react';
import {useVideoConfig} from 'remotion';
import {usePresence} from '../motion';
import type {InsertOf} from '../schema';
import type {PictogramName} from '../style/pictograms.generated';
import {chamberCard as T, colors, fontStyle, orientationOf, withAlpha, zoneRect} from '../style/tokens';
import {Pictogram} from './Pictogram';

// Карточка камеры (4.2): белая панель на половину высоты кадра; номер, «/19», линия, заголовок, 2-5 пиктограмм,
// полоса прогресса из делений. Выезд снизу 300 мс (ease-out), уход 250 мс (ease-in).
export const ChamberCard: React.FC<{insert: InsertOf<'chamber_card'>; durationInFrames: number}> = ({insert, durationInFrames}) => {
	const {width, height} = useVideoConfig();
	const o = orientationOf(width, height);
	const presence = usePresence(durationInFrames);
	const r = zoneRect('chamber_card', width, height);
	const {chamber, of, title, pictograms} = insert.props;
	const pad = Math.round(r.w * 0.05);
	const shift = (1 - presence) * (height - r.y);
	const pict = pictograms.map((p) => (typeof p === 'string' ? {name: p, active: true, label: undefined} : p));
	return (
		<div
			style={{
				position: 'absolute',
				left: r.x,
				top: r.y,
				width: r.w,
				height: r.h,
				transform: `translateY(${shift}px)`,
				backgroundColor: colors.labWhite,
				borderRadius: 4,
				padding: pad,
				boxSizing: 'border-box',
				display: 'flex',
				flexDirection: 'column',
				justifyContent: 'space-between',
				color: colors.chamberDark,
			}}
		>
			<div style={{display: 'flex', alignItems: 'baseline', gap: 20}}>
				<span style={{...fontStyle('heading'), fontSize: T.numberPx, lineHeight: 1}}>{String(chamber).padStart(2, '0')}</span>
				<span style={{...fontStyle('heading'), fontSize: T.ofPx, lineHeight: 1, color: colors.labGrey}}>/{of}</span>
			</div>
			<div style={{height: 2, backgroundColor: colors.labGrey}} />
			<div style={{...fontStyle('heading'), fontSize: T.titlePx[o], lineHeight: 1.05, textTransform: 'uppercase', overflow: 'hidden'}}>{title}</div>
			<div style={{display: 'flex', gap: 36, alignItems: 'flex-start'}}>
				{pict.map((p, k) => (
					<div key={k} style={{display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 6}}>
						<Pictogram name={p.name as PictogramName} size={T.pictogramPx} color={p.active ? 'chamberDark' : 'labGrey'} />
						{p.label ? <span style={{...fontStyle('service'), fontSize: 24, color: p.active ? colors.chamberDark : colors.labGrey}}>{p.label}</span> : null}
					</div>
				))}
			</div>
			<div style={{display: 'flex', gap: 4, height: 16}}>
				{Array.from({length: of}, (_, i) => (
					<div key={i} style={{flex: 1, backgroundColor: i < chamber ? colors.portalBlue : withAlpha(colors.labGrey, 0.55)}} />
				))}
			</div>
		</div>
	);
};

import React from 'react';
import {AbsoluteFill, useCurrentFrame, useVideoConfig} from 'remotion';
import type {InsertOf} from '../schema';
import {drawProgress, drawingBounds, labelOpacity, layoutCallouts, toPx, type PxShape} from '../sceneMath';
import {blueprint as T, colors, effects, plexStyle} from '../style/tokens';

const stroke = (weight: string): {strokeWidth: number; opacity?: number} =>
	weight === 'wall' ? {strokeWidth: T.wallPx} : weight === 'thin' ? {strokeWidth: T.thinPx, opacity: T.thinOpacity} : {strokeWidth: T.linePx};

// Одна фигура чертежа: линия прорисовывается через pathLength=1 и dash (progress 0..1), как bp-draw в превью
const Shape: React.FC<{shape: PxShape; progress: number}> = ({shape, progress}) => {
	const common = {
		fill: 'none',
		stroke: colors['bp-line'],
		strokeLinecap: 'round' as const,
		strokeLinejoin: 'round' as const,
		pathLength: 1,
		strokeDasharray: 1,
		strokeDashoffset: 1 - progress,
		...stroke(shape.weight),
	};
	switch (shape.type) {
		case 'line':
			return <line x1={shape.x1} y1={shape.y1} x2={shape.x2} y2={shape.y2} {...common} />;
		case 'rect':
			return <rect x={shape.x} y={shape.y} width={shape.w} height={shape.h} {...common} />;
		case 'circle':
			return <circle cx={shape.cx} cy={shape.cy} r={shape.r} {...common} />;
		case 'path':
			return <path d={shape.d} {...common} />;
	}
};

// Схема-чертёж (сцена 2): синий лист в клетку со светом из центра, белые линии рисуются за bp-draw (2,5 с), затем подписи.
// Табличка слева внизу: название (наклон 8°), строка, рубрика. Рисунок и выноски приходят из плана (рисует агент).
export const Blueprint: React.FC<{insert: InsertOf<'blueprint'>; durationInFrames: number}> = ({insert}) => {
	const frame = useCurrentFrame();
	const {fps, width, height} = useVideoConfig();
	const t = (frame * 1000) / fps;
	const progress = drawProgress(t);
	const labels = labelOpacity(t);
	const {title, kicker, note, callouts, drawing} = insert.props;
	const shapes = drawing.map((p) => toPx(p, width, height));
	const bounds = drawingBounds(shapes);
	const layouts = layoutCallouts(callouts, width, height, bounds);
	return (
		<AbsoluteFill style={{background: effects.blueprintBackground, color: colors['bp-line']}}>
			<div style={{position: 'absolute', inset: T.frameInset, border: `${T.frameBorderPx}px solid ${effects.blueprintFrameLine}`}} />
			<svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} style={{position: 'absolute', left: 0, top: 0}}>
				{shapes.map((s, i) => (
					<Shape key={i} shape={s} progress={progress} />
				))}
				{layouts.map((l, i) => (
					<g key={`c${i}`}>
						<line
							x1={l.dotX}
							y1={l.dotY}
							x2={l.anchorX}
							y2={l.labelY}
							fill="none"
							stroke={colors['bp-line']}
							strokeLinecap="round"
							pathLength={1}
							strokeDasharray={1}
							strokeDashoffset={1 - progress}
							{...stroke('thin')}
						/>
						<circle cx={l.dotX} cy={l.dotY} r={T.dotR} fill={colors['bp-line']} />
					</g>
				))}
			</svg>
			{layouts.map((l, i) => (
				<div
					key={`l${i}`}
					style={{
						position: 'absolute',
						left: l.side === 'right' ? l.anchorX + 14 : l.anchorX - 14 - l.boxWidth,
						top: l.labelY,
						width: l.boxWidth,
						transform: 'translateY(-50%)',
						textAlign: l.side === 'right' ? 'left' : 'right',
						...plexStyle(500, 100),
						fontSize: T.labelPx,
						lineHeight: 1.2,
						opacity: labels,
					}}
				>
					{callouts[i].label}
				</div>
			))}
			<div style={{position: 'absolute', left: T.padX, bottom: T.padBottom, maxWidth: width - 2 * T.padX, display: 'flex', flexDirection: 'column', gap: 6}}>
				<b style={{...plexStyle(600, 100), fontSize: T.titlePx, lineHeight: 1, fontStyle: `oblique ${T.titleSkewDeg}deg`}}>{title}</b>
				<span style={{...plexStyle(400, 100), fontSize: T.notePx, lineHeight: 1.3, opacity: 0.85}}>{note}</span>
				<em style={{...plexStyle(300, 100), fontSize: T.kickerPx, lineHeight: 1.1, fontStyle: 'normal', letterSpacing: '0.06em', textTransform: 'uppercase'}}>{kicker}</em>
			</div>
		</AbsoluteFill>
	);
};

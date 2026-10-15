import React from 'react';
import {AbsoluteFill} from 'remotion';
import {colors, grid, opacity} from '../style/tokens';

// Фон полноэкранных вставок: chamberDark с сеткой 48 px линиями labWhite (прозрачность 0.03); без градиентов
export const GridBackground: React.FC<{width: number; height: number; style?: React.CSSProperties}> = ({width, height, style}) => (
	<AbsoluteFill style={{backgroundColor: colors.chamberDark, ...style}}>
		<svg width={width} height={height} style={{position: 'absolute', left: 0, top: 0}}>
			<defs>
				<pattern id="lab-grid" width={grid.cell} height={grid.cell} patternUnits="userSpaceOnUse">
					<path d={`M ${grid.cell} 0 V ${grid.cell} M 0 ${grid.cell} H ${grid.cell}`} fill="none" stroke={colors.labWhite} strokeOpacity={opacity.gridLine} strokeWidth={1} />
				</pattern>
			</defs>
			<rect width={width} height={height} fill="url(#lab-grid)" />
		</svg>
	</AbsoluteFill>
);

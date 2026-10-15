import React from 'react';
import {pictogramData, type PictogramName} from '../style/pictograms.generated';
import {colors, type ColorToken} from '../style/tokens';

// Пиктограмма из assets/pictograms (STY-3): вставляется как разметка, цвет задаётся props, контур 6 % размера
export const Pictogram: React.FC<{name: PictogramName; size: number; color?: ColorToken; style?: React.CSSProperties}> = ({
	name,
	size,
	color = 'chamberDark',
	style,
}) => {
	const data = pictogramData[name];
	return (
		<svg
			width={size}
			height={size}
			viewBox="0 0 48 48"
			fill="none"
			stroke="currentColor"
			strokeWidth={data.strokeWidth}
			strokeLinejoin="miter"
			strokeLinecap="butt"
			style={{color: colors[color], display: 'block', flexShrink: 0, ...style}}
			dangerouslySetInnerHTML={{__html: data.markup}}
		/>
	);
};

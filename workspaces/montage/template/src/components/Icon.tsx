import React from 'react';
import type {IconName} from '../style/icons';
import {colors} from '../style/tokens';

// 14 иконок табличек дизайн-системы v2 (вкладка Icons): залитые скруглённые силуэты, сетка 48x48. Рисуются currentColor;
// детали-вырезы цветом фона (`bg`, по умолчанию sign-paper). Контуры один в один из превью дизайн-системы.
const draw = (name: IconName, bg: string): React.ReactNode => {
	switch (name) {
		case 'colonists':
			return (
				<g fill="currentColor">
					<circle cx="15" cy="12" r="5.5" />
					<path d="M6 44V30a9 9 0 0 1 18 0v14z" />
					<circle cx="33" cy="12" r="5.5" />
					<path d="M24 44V30a9 9 0 0 1 18 0v14z" />
				</g>
			);
		case 'subject':
			return (
				<g fill="currentColor">
					<circle cx="24" cy="11" r="6.5" />
					<path d="M13 44V31a11 11 0 0 1 22 0v13h-6V35H19v9z" />
				</g>
			);
		case 'cube':
			return (
				<>
					<path d="M24 4L42 14V34L24 44L6 34V14Z" fill="currentColor" />
					<path d="M6 14L24 24L42 14M24 24V44" fill="none" stroke={bg} strokeWidth="2.6" strokeLinejoin="round" />
					<circle cx="15" cy="29.5" r="3.3" fill={bg} />
					<circle cx="33" cy="29.5" r="3.3" fill={bg} />
				</>
			);
		case 'button':
			return (
				<>
					<g fill="currentColor">
						<path d="M5 38h38v6H5z" />
						<path d="M10 37c0-9 6-14 14-14s14 5 14 14z" />
					</g>
					<path d="M24 5v8M14 8.5l3.5 6M34 8.5l-3.5 6" stroke="currentColor" strokeWidth="3.6" strokeLinecap="round" />
				</>
			);
		case 'fire':
			return (
				<path
					fill="currentColor"
					fillRule="evenodd"
					d="M24 3c4 9 15 14 15 27a15 15 0 0 1-30 0c0-7 4-11 7-15 1 5 3 7 5 8-1-8 0-14 3-20zM24 30c3 3 6 5 6 9a6 6 0 0 1-12 0c0-4 3-6 6-9z"
				/>
			);
		case 'raid':
			return (
				<>
					<circle cx="24" cy="24" r="15.5" fill="none" stroke="currentColor" strokeWidth="4.5" />
					<circle cx="24" cy="24" r="5" fill="currentColor" />
					<path d="M24 2.5v9M24 36.5v9M2.5 24h9M36.5 24h9" stroke="currentColor" strokeWidth="4.5" strokeLinecap="round" />
				</>
			);
		case 'loss':
			return (
				<>
					<path d="M13 41V20a11 11 0 0 1 22 0v21z" fill="currentColor" />
					<path d="M24 15v13M19 20h10" stroke={bg} strokeWidth="3.2" strokeLinecap="round" />
					<path d="M6 43.5h36" stroke="currentColor" strokeWidth="3.6" strokeLinecap="round" />
				</>
			);
		case 'sleep':
			return (
				<>
					<path d="M5 11v33M5 31h38v13" fill="none" stroke="currentColor" strokeWidth="5" strokeLinecap="round" strokeLinejoin="round" />
					<g fill="currentColor">
						<rect x="9.5" y="20" width="11" height="8" rx="3.5" />
						<path d="M23 20h16a4 4 0 0 1 4 4v4H23z" />
					</g>
				</>
			);
		case 'research':
			return (
				<>
					<path
						fill="currentColor"
						d="M18 5h12a1.5 1.5 0 0 1 0 3h-1v9l11 19c2.4 4.2.3 8-4.3 8H12.3C7.7 44 5.6 40.2 8 36l11-19V8h-1a1.5 1.5 0 0 1 0-3z"
					/>
					<circle cx="20" cy="36" r="2.6" fill={bg} />
					<circle cx="27" cy="31" r="1.9" fill={bg} />
				</>
			);
		case 'cold':
			return (
				<g stroke="currentColor" strokeWidth="4" strokeLinecap="round" fill="none">
					<path d="M24 4v40M6.7 14l34.6 20M6.7 34l34.6-20" />
					<path d="M18.5 7.5l5.5 4.5 5.5-4.5M18.5 40.5l5.5-4.5 5.5 4.5" />
				</g>
			);
		case 'animals':
			return (
				<g fill="currentColor">
					<ellipse cx="10.5" cy="21" rx="4.6" ry="6" />
					<ellipse cx="19" cy="11.5" rx="4.6" ry="6" />
					<ellipse cx="29" cy="11.5" rx="4.6" ry="6" />
					<ellipse cx="37.5" cy="21" rx="4.6" ry="6" />
					<path d="M24 23c7 0 13 9 13 15 0 4-3 6-6 6-3 0-4-2-7-2s-4 2-7 2c-3 0-6-2-6-6 0-6 6-15 13-15z" />
				</g>
			);
		case 'build':
			return (
				<g transform="rotate(-35 24 24)">
					<rect x="9" y="6" width="27" height="11" rx="3" fill="currentColor" />
					<path d="M22.5 18v25" stroke="currentColor" strokeWidth="6" strokeLinecap="round" />
				</g>
			);
		case 'night':
			return <path fill="currentColor" d="M30 5a19 19 0 1 0 13 30A16 16 0 0 1 30 5z" />;
		case 'health':
			return <path fill="currentColor" d="M24 42 8.2 26.2a9.6 9.6 0 0 1 15.8-11 9.6 9.6 0 0 1 15.8 11z" />;
	}
};

export const Icon: React.FC<{name: IconName; size?: number; bg?: string; style?: React.CSSProperties}> = ({name, size = 76, bg = colors['sign-paper'], style}) => (
	<svg viewBox="0 0 48 48" width={size} height={size} style={{display: 'block', ...style}}>
		{draw(name, bg)}
	</svg>
);

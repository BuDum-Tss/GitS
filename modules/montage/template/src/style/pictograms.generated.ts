// Создано scripts/sync-assets.mjs из assets/pictograms/*.svg. Не править вручную.
export const pictogramData = {
	button: {strokeWidth: 2.88, markup: "<path d=\"M6 40 H42 V33 H6 Z\"/><path d=\"M12 33 V29 Q12 21 24 21 Q36 21 36 29 V33\"/><path d=\"M24 6 V13 M17 9 L19.5 14.5 M31 9 L28.5 14.5\"/>"},
	camera: {strokeWidth: 2.88, markup: "<path d=\"M4 14 H15 L18 9 H30 L33 14 H44 V39 H4 Z\"/><circle cx=\"24\" cy=\"26\" r=\"8\"/><circle cx=\"38\" cy=\"19\" r=\"1.8\" fill=\"currentColor\" stroke=\"none\"/>"},
	clock: {strokeWidth: 2.88, markup: "<circle cx=\"24\" cy=\"24\" r=\"19\"/><path d=\"M24 11 V24 L33 30\"/>"},
	cube: {strokeWidth: 2.88, markup: "<path d=\"M24 5 L41 14.5 V33.5 L24 43 L7 33.5 V14.5 Z\"/><path d=\"M7 14.5 L24 24 L41 14.5 M24 24 V43\"/>"},
	exit: {strokeWidth: 2.88, markup: "<path d=\"M20 6 H8 V42 H20\"/><path d=\"M16 24 H42 M33 15 L42 24 L33 33\"/>"},
	eye: {strokeWidth: 2.88, markup: "<path d=\"M3 24 Q24 6 45 24 Q24 42 3 24 Z\"/><circle cx=\"24\" cy=\"24\" r=\"6.5\"/>"},
	gear: {strokeWidth: 2.88, markup: "<path d=\"M37.13 20.85 L41.84 21.60 L41.84 26.40 L37.13 27.15 L35.51 31.05 L38.31 34.92 L34.92 38.31 L31.05 35.51 L27.15 37.13 L26.40 41.84 L21.60 41.84 L20.85 37.13 L16.95 35.51 L13.08 38.31 L9.69 34.92 L12.49 31.05 L10.87 27.15 L6.16 26.40 L6.16 21.60 L10.87 20.85 L12.49 16.95 L9.69 13.08 L13.08 9.69 L16.95 12.49 L20.85 10.87 L21.60 6.16 L26.40 6.16 L27.15 10.87 L31.05 12.49 L34.92 9.69 L38.31 13.08 L35.51 16.95 Z\"/><circle cx=\"24\" cy=\"24\" r=\"5.5\"/>"},
	hazard_water: {strokeWidth: 2.88, markup: "<path d=\"M24 4 L33.5 19 Q36 23 36 26.5 A12 12 0 0 1 12 26.5 Q12 23 14.5 19 Z\"/><path d=\"M5 43 L10.5 39 L16 43 L21.5 39 L27 43 L32.5 39 L38 43 L43.5 39\"/>"},
	laser: {strokeWidth: 2.88, markup: "<path d=\"M4 17 H15 V31 H4 Z\"/><path d=\"M15 24 H36\"/><path d=\"M36 24 L44 17 M36 24 L44 24 M36 24 L44 31\"/><circle cx=\"9.5\" cy=\"24\" r=\"1.8\" fill=\"currentColor\" stroke=\"none\"/>"},
	person: {strokeWidth: 2.88, markup: "<circle cx=\"24\" cy=\"13.5\" r=\"7.5\"/><path d=\"M8 43 V38 Q8 28 24 28 Q40 28 40 38 V43 Z\"/>"},
	speaker: {strokeWidth: 2.88, markup: "<path d=\"M5 18 H14 L25 8 V40 L14 30 H5 Z\"/><path d=\"M32 17 Q37 24 32 31 M37 11 Q46 24 37 37\"/>"},
	warning: {strokeWidth: 2.88, markup: "<path d=\"M24 5 L44 41 H4 Z\"/><path d=\"M24 17 V29\"/><circle cx=\"24\" cy=\"35\" r=\"2\" fill=\"currentColor\" stroke=\"none\"/>"},
} as const;

export type PictogramName = keyof typeof pictogramData;
export const pictogramNames = Object.keys(pictogramData) as PictogramName[];

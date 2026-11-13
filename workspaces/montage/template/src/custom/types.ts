// Контракт пользовательских вставок (kind: "custom"): агент добавляет компоненты в work/src/custom/ и регистрирует в index.ts
export type CustomProps = {
	props: {component: string} & Record<string, unknown>; // props вставки из plan.json
	durationInFrames: number; // длительность вставки в кадрах; useCurrentFrame() внутри отсчитывается от её начала
};

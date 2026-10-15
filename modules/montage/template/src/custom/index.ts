// Реестр пользовательских вставок: ключ это props.component из plan.json. Агент-монтажёр добавляет свои компоненты в эту
// папку (work/src/custom/) и регистрирует их здесь. Встроенные: JournalLog (регламент 4.9); служебные FontSpecimen (проверка шрифтов) и PictogramSheet (лист пиктограмм).
import type React from 'react';
import {FontSpecimen} from './FontSpecimen';
import {JournalLog} from './JournalLog';
import {PictogramSheet} from './PictogramSheet';
import type {CustomProps} from './types';

export const customComponents: Record<string, React.FC<CustomProps>> = {
	JournalLog,
	FontSpecimen,
	PictogramSheet,
};

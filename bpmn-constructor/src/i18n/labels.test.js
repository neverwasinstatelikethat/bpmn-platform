import { elementTypeName, humanizeBpmnNote, roleLabel, stripBpmnIds } from './labels';

describe('roleLabel', () => {
    it('переводит предопределённые роли', () => {
        expect(roleLabel('admin')).toBe('Администратор');
        expect(roleLabel('editor')).toBe('Редактор');
        expect(roleLabel('viewer')).toBe('Наблюдатель');
    });

    it('не выдумывает перевод для неизвестной роли', () => {
        expect(roleLabel('auditor')).toBe('auditor');
    });
});

describe('humanizeBpmnNote', () => {
    it('убирает идентификаторы и английские типы из заметки о типе', () => {
        expect(humanizeBpmnNote('Неизвестный тип «sub process» элемента Task_0abc → task'))
            .toBe('Тип «подпроцесс» не распознан — элемент стал: задача');
    });

    it('переписывает заметку об удалённом переходе без id', () => {
        expect(humanizeBpmnNote('Поток Start_1 → Task_2a удалён: шаг не может вести в пул'))
            .toBe('Переход удалён: шаг не может вести в пул');
    });

    it('для неизвестного формата снимает id, не сочиняя перевод', () => {
        expect(humanizeBpmnNote('Лишние элементы отброшены (максимум 60)')).toBe('Лишние элементы отброшены (максимум 60)');
        expect(stripBpmnIds('Элемент Flow_1a2b3c без названия')).toBe('Элемент без названия');
    });

    it('снимает и короткие хвосты идентификаторов', () => {
        expect(humanizeBpmnNote('Идентификатор элемента Task_3a приведён к «Task_1»'))
            .toBe('Идентификатор элемента исправлен');
        expect(stripBpmnIds('Поток Start_1 → Task_2a удалён')).toBe('Поток → удалён');
    });

    it('не роняет пустый ввод', () => {
        expect(humanizeBpmnNote(undefined)).toBe('');
        expect(humanizeBpmnNote('')).toBe('');
    });
});

describe('elementTypeName', () => {
    it('переводит известное имя типа независимо от регистра', () => {
        expect(elementTypeName('Gateway')).toBe('шлюз');
        expect(elementTypeName('sub process')).toBe('подпроцесс');
    });

    it('оставляет как есть то, чего нет в словаре', () => {
        expect(elementTypeName('boundaryEvent')).toBe('boundaryEvent');
    });
});

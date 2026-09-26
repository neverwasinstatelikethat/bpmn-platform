jest.mock('axios', () => ({
    create: () => ({ interceptors: { request: { use: jest.fn() } } }),
}));

import { toUserMessage } from './client';

describe('toUserMessage', () => {
    it('hides implementation details for server failures', () => {
        expect(toUserMessage({ response: { status: 500, data: { detail: 'traceback secret' } } }))
            .toBe('Сервис временно недоступен. Попробуйте ещё раз.');
    });

    it('keeps a safe client validation message', () => {
        expect(toUserMessage({ response: { status: 422, data: { detail: 'Введите корректный email' } } }))
            .toBe('Введите корректный email');
    });

    it('переводит пирамиду 422 от Pydantic в причину', () => {
        const error = { response: { status: 422, data: { detail: [
            { type: 'string_too_long', loc: ['body', 'name'], msg: 'String should have at most 100 characters', ctx: { max_length: 100 } },
        ] } } };
        expect(toUserMessage(error, 'Не удалось создать папку.')).toBe('«название» длиннее допустимого: не более 100 символов.');
    });

    it('не выдаёт локальную ошибку за недоступный сервис', () => {
        expect(toUserMessage(new Error('html2canvas failed'), 'Не удалось сохранить PDF.'))
            .toBe('Не удалось сохранить PDF.');
    });

    it('не показывает английское сообщение бэкенда', () => {
        expect(toUserMessage({ response: { status: 400, data: { detail: 'Invalid team name' } } }, 'Не удалось создать команду.'))
            .toBe('Не удалось создать команду.');
    });
});

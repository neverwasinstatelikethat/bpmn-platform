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
});

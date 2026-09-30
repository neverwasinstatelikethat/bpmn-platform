import { apiClient } from './client';
import {
    TaskCancelledError,
    TaskTimeoutError,
    cancelTask,
    isTerminal,
    runTask,
    runTaskWithRetry,
    taskFailureMessage,
} from './tasks';

jest.mock('./client', () => ({
    apiClient: { post: jest.fn(), get: jest.fn() },
    toUserMessage: (error, fallback) => fallback,
}));

const descriptor = (over = {}) => ({
    task_id: 'task-1',
    kind: 'generate',
    queue: 'ai_llm',
    status: 'queued',
    poll_after: 2,
    error: null,
    error_code: null,
    result: null,
    replayed: false,
    ...over,
});

beforeEach(() => {
    apiClient.post.mockReset();
    apiClient.get.mockReset();
});

describe('runTask', () => {
    it('не опрашивает задачу, которая завершилась в процессе запроса', async () => {
        // Режим без Redis: результат приходит первым же ответом, и лишний
        // круг опроса удваивал бы задержку там, где ждать уже нечего.
        apiClient.post.mockResolvedValue({
            data: descriptor({ status: 'done', result: { bpmn: '<definitions/>' } }),
        });
        const result = await runTask('/api/generate', { text: 'заявка' });
        expect(result.result.bpmn).toBe('<definitions/>');
        expect(apiClient.get).not.toHaveBeenCalled();
    });

    it('опрашивает задачу до терминального статуса и берёт poll_after', async () => {
        // Jest 27 в CRA не умеет прокручивать таймеры асинхронно, поэтому
        // подсказка сервера задаётся малой: цикл проходит настоящим сном, но
        // читается то же самое — шаг взят из ответа, а не из константы 2 с.
        apiClient.post.mockResolvedValue({ data: descriptor() });
        apiClient.get
            .mockResolvedValueOnce({ data: descriptor({ status: 'running', poll_after: 0.05 }) })
            .mockResolvedValueOnce({
                data: descriptor({ status: 'done', result: { score: 91 }, poll_after: null }),
            });
        const startedAt = Date.now();
        const controller = new AbortController();
        const result = await runTask('/api/evaluate', { bpmn_xml: '<x/>' },
            { signal: controller.signal });
        expect(result).toMatchObject({ status: 'done', result: { score: 91 } });
        expect(apiClient.get).toHaveBeenCalledTimes(2);
        expect(apiClient.get).toHaveBeenCalledWith('/api/tasks/task-1',
            expect.objectContaining({ signal: expect.any(AbortSignal) }));
        // Суммарно 50 + 75 мс; при шаге в 2 с upper bound недостижим.
        expect(Date.now() - startedAt).toBeLessThan(800);
    });

    it('посылает ключ идемпотентности и принимает ответ-дубль как есть', async () => {
        apiClient.post.mockResolvedValue({
            data: descriptor({ status: 'done', replayed: true, result: { bpmn: 'x' } }),
        });
        const key = 'key-1';
        const result = await runTask('/api/generate', { text: 'заявка' },
            { idempotencyKey: key });
        expect(apiClient.post).toHaveBeenCalledWith('/api/generate',
            { text: 'заявка', idempotency_key: key }, expect.anything());
        expect(result.replayed).toBe(true);
    });

    it('без ключа создаёт новый, а не общий на все запросы', async () => {
        apiClient.post.mockResolvedValue({
            data: descriptor({ status: 'done', result: {} }),
        });
        await runTask('/api/generate', { text: 'раз' });
        await runTask('/api/generate', { text: 'два' });
        const [first, second] = apiClient.post.mock.calls.map((call) => call[1].idempotency_key);
        expect(first).toBeTruthy();
        expect(first).not.toBe(second);
    });
});

describe('отказ задачи', () => {
    it('отдаёт текст ошибки из дескриптора, а не из HTTP-статуса', async () => {
        apiClient.post.mockResolvedValue({
            data: descriptor({
                status: 'failed',
                error: 'Схема слишком велика для ИИ-улучшения',
                error_code: 'llm_too_large',
            }),
        });
        const result = await runTask('/api/ai/improve', { bpmn_xml: '<x/>', prompt: 'улучши' });
        expect(taskFailureMessage(result)).toBe('Схема слишком велика для ИИ-улучшения');
        expect(isTerminal(result.status)).toBe(true);
    });

    it('отказ провайдера звучит как «повторите позже», а не как пустой текст', () => {
        const failed = descriptor({ status: 'failed', error_code: 'unavailable' });
        expect(taskFailureMessage(failed))
            .toBe('Сервис временно недоступен. Попробуйте ещё раз через минуту.');
    });

    it('отменённая задача не выглядит сломанной', () => {
        expect(taskFailureMessage(descriptor({ status: 'cancelled' })))
            .toBe('Задача отменена.');
    });

    it('успех не имеет сообщения об отказе', () => {
        expect(taskFailureMessage(descriptor({ status: 'done' }))).toBeNull();
    });
});

describe('отмена', () => {
    it('прерванный опрос снимает задачу с очереди ровно один раз', async () => {
        const controller = new AbortController();
        apiClient.post.mockResolvedValue({ data: descriptor() });
        apiClient.get.mockImplementation(async () => {
            // Пользователь ушёл с экрана в момент опроса: сервер ещё не отдал
            // терминальный статус, так что отменять есть чего.
            controller.abort();
            return { data: descriptor({ status: 'running' }) };
        });
        await expect(runTask('/api/generate', { text: 'заявка' },
            { signal: controller.signal })).rejects.toBeInstanceOf(TaskCancelledError);
        const cancels = apiClient.post.mock.calls
            .filter((call) => call[0] === '/api/tasks/task-1/cancel');
        expect(cancels).toHaveLength(1);
    });

    it('просроченная задача тоже снимается с очереди и не молчит', async () => {
        apiClient.post.mockResolvedValue({ data: descriptor() });
        apiClient.get.mockResolvedValue({ data: descriptor({ status: 'running' }) });
        // Нулевой дедлайн: проверка «ждать больше нечего» срабатывает до
        // первого сна, поэтому тест не зависит от таймеров.
        await expect(runTask('/api/generate', { text: 'заявка' }, { timeoutMs: 0 }))
            .rejects.toBeInstanceOf(TaskTimeoutError);
        expect(apiClient.post).toHaveBeenCalledWith('/api/tasks/task-1/cancel');
    });

    it('сбой сервера при отмене не превращается в ошибку пользователя', async () => {
        apiClient.post.mockRejectedValue(new Error('transport'));
        await expect(cancelTask('task-1')).resolves.toBeNull();
    });
});

describe('runTaskWithRetry', () => {
    it('повторяет только отказ провайдера', async () => {
        apiClient.post
            .mockResolvedValueOnce({
                data: descriptor({ status: 'failed', error_code: 'unavailable' }),
            })
            .mockResolvedValueOnce({
                data: descriptor({ status: 'done', result: { bpmn: 'x' } }),
            });
        const result = await runTaskWithRetry('/api/generate', { text: 'заявка' });
        expect(result.status).toBe('done');
        expect(apiClient.post.mock.calls.filter((c) => c[0] === '/api/generate')).toHaveLength(2);
    });

    it('не повторяет отказ по входу', async () => {
        apiClient.post.mockResolvedValue({
            data: descriptor({ status: 'failed', error_code: 'llm_truncated', error: 'Обрезан' }),
        });
        const result = await runTaskWithRetry('/api/generate', { text: 'заявка' });
        expect(result.error_code).toBe('llm_truncated');
        expect(apiClient.post.mock.calls.filter((c) => c[0] === '/api/generate')).toHaveLength(1);
    });

    it('повтор идёт под новым ключом', async () => {
        // Прежний ключ указывал бы на уже отменённую или проваленную задачу, и
        // «повтор» вернул бы её статус вместо нового исполнения.
        apiClient.post
            .mockResolvedValueOnce({
                data: descriptor({ status: 'failed', error_code: 'unavailable' }),
            })
            .mockResolvedValueOnce({ data: descriptor({ status: 'done', result: {} }) });
        await runTaskWithRetry('/api/generate', { text: 'заявка' }, { retryDelayMs: 0 });
        const keys = apiClient.post.mock.calls
            .filter((call) => call[0] === '/api/generate')
            .map((call) => call[1].idempotency_key);
        expect(new Set(keys).size).toBe(2);
    });
});

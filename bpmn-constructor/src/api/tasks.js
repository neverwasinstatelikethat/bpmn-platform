import { v4 as uuid } from 'uuid';
import { apiClient, toUserMessage } from './client';

// Долгие ИИ-вызовы ушли из HTTP-запроса в очередь задач: маршрут отвечает 202
// с идентификатором, а результат читается опросом. Здесь — единственный слой,
// который знает об этом контракте, поэтому экраны работают с готовым результатом
// и не копируют логику ожидания.
const FIRST_POLL_MS = 2000;
const MAX_POLL_MS = 5000;
// Сценарная граница ожидания: дольше пяти минут человек результат не ждёт, и
// лучше отдать ему понятный отказ, чем крутить опросы до закрытия вкладки.
export const TASK_TIMEOUT_MS = 5 * 60 * 1000;
// Медленнее 5 с опрашивать нельзя: результат уже лежит в базе, и большой шаг
// только добавляет видимую задержку после окончания работы.
const BACKOFF_STEP = 1.5;

const TERMINAL = new Set(['done', 'failed', 'cancelled']);

export const isTerminal = (status) => TERMINAL.has(status);

// Ошибка клиента, а не провайдера: эти коды стоит повторить позже, остальные
// означают свойства входа и повтор их не лечит.
const RETRYABLE_CODES = new Set(['unavailable', 'queue_unavailable', 'worker_lost']);

export class TaskTimeoutError extends Error {
    constructor(taskId) {
        super(`Задача не завершилась за отведённое время (${taskId}).`);
        this.name = 'TaskTimeoutError';
        this.taskId = taskId;
    }
}

export class TaskCancelledError extends Error {
    constructor(taskId) {
        super('Задача отменена.');
        this.name = 'TaskCancelledError';
        this.taskId = taskId;
    }
}

/**
 * Ключ повторной отправки. Создаётся один раз на действие пользователя:
 * повтор того же запроса (отвалившееся соединение, двойная отправка) обязан
 * вернуть ту же задачу, а не оплатить второй вызов модели. Новое действие
 * всегда получает новый ключ — иначе отменённую задачу нельзя было бы
 * перезапросить.
 */
export const newIdempotencyKey = () => uuid();

const wait = (ms, signal) => new Promise((resolve, reject) => {
    const timer = setTimeout(resolve, ms);
    const onAbort = () => {
        clearTimeout(timer);
        reject(new DOMException('Aborted', 'AbortError'));
    };
    if (signal) {
        if (signal.aborted) {
            clearTimeout(timer);
            reject(new DOMException('Aborted', 'AbortError'));
            return;
        }
        signal.addEventListener('abort', onAbort, { once: true });
    }
});

/** Отправить задачу и сразу вернуть её дескриптор. */
export async function submitTask(path, body, { idempotencyKey, signal } = {}) {
    const { data } = await apiClient.post(path, {
        ...body,
        idempotency_key: idempotencyKey || newIdempotencyKey(),
    }, { signal });
    return data;
}

export const fetchTask = async (taskId, { signal } = {}) => {
    const { data } = await apiClient.get(`/api/tasks/${taskId}`, { signal });
    return data;
};

/**
 * Просьба об отмене. Ошибка транспорта намеренно проглочена: пользователь уже
 * ушёл, и в этот момент отличить «задача отменена» от «сервер не ответил»
 * нечем, а показывать ему технический текст про отмену — значит врать про
 * собственное действие.
 */
export async function cancelTask(taskId) {
    try {
        const { data } = await apiClient.post(`/api/tasks/${taskId}/cancel`);
        return data;
    } catch (error) {
        console.warn('Отмену задачи нельзя подтвердить:', error?.message || error);
        return null;
    }
}

/** Опрос до терминального статуса с замедлением 2 с → 5 с. */
export async function waitForTask(taskId, {
    signal,
    onStatus,
    timeoutMs = TASK_TIMEOUT_MS,
    deadlineAt,
} = {}) {
    const deadline = deadlineAt ?? (Date.now() + timeoutMs);
    let attempt = 0;
    for (;;) {
        const descriptor = await fetchTask(taskId, { signal });
        if (onStatus) onStatus(descriptor);
        if (isTerminal(descriptor.status)) return descriptor;
        // Первый шаг берётся от сервера: он знает, в каком режиме исполняется
        // задача, и в режиме без очереди сразу отдаёт терминальный статус.
        const base = (descriptor.poll_after || FIRST_POLL_MS / 1000) * 1000;
        const delay = Math.min(MAX_POLL_MS, base * Math.pow(BACKOFF_STEP, attempt));
        const remaining = deadline - Date.now();
        if (remaining <= 0) throw new TaskTimeoutError(taskId);
        attempt += 1;
        await wait(Math.min(delay, remaining), signal);
    }
}

/**
 * Полный путь: постановка + ожидание. Возвращает дескриптер задачи в
 * терминальном статусе; `result` внутри него — ровно тот объект, который
 * маршрут отдавал напрямую.
 */
export async function runTask(path, body, {
    signal,
    onStatus,
    idempotencyKey,
    timeoutMs = TASK_TIMEOUT_MS,
} = {}) {
    const key = idempotencyKey || newIdempotencyKey();
    let descriptor = await submitTask(path, body, { idempotencyKey: key, signal });
    if (isTerminal(descriptor.status)) return descriptor;
    const startedAt = Date.now();
    try {
        return await waitForTask(descriptor.task_id, {
            signal,
            onStatus,
            timeoutMs,
            // Очередь уже могла держать задачу, пока шёл запрос постановки,
            // поэтому отсчёт ведём от дедлайна всей операции, а не от 5 с.
            deadlineAt: startedAt + timeoutMs,
        });
    } catch (error) {
        if (error?.name === 'AbortError' || error instanceof TaskTimeoutError) {
            await cancelTask(descriptor.task_id);
            throw error instanceof TaskTimeoutError
                ? error
                : new TaskCancelledError(descriptor.task_id);
        }
        throw error;
    }
}

/** Русская формулировка отказа задачи; `null`, если задача успешна. */
export function taskFailureMessage(descriptor, fallback = 'Задача не выполнена.') {
    if (!descriptor || descriptor.status === 'done') return null;
    if (descriptor.status === 'cancelled') return 'Задача отменена.';
    if (descriptor.error) return descriptor.error;
    if (descriptor.error_code && RETRYABLE_CODES.has(descriptor.error_code)) {
        return 'Сервис временно недоступен. Попробуйте ещё раз через минуту.';
    }
    return fallback;
}

/**
 * Сообщение об ошибке транспорта или задачи. Отказ задачи несёт текст в
 * дескрипторе, а не в HTTP-ответе, поэтому обычный `toUserMessage` его не
 * увидит: у ошибки транспорта здесь просто нет `response`.
 */
export function describeTaskError(error, descriptor, fallback) {
    if (descriptor) return taskFailureMessage(descriptor, fallback) || fallback;
    return toUserMessage(error, fallback);
}

export const isRetryableFailure = (descriptor) => Boolean(
    descriptor?.error_code && RETRYABLE_CODES.has(descriptor.error_code));

/**
 * Один автоматический повтор задачи после отказа провайдера.
 *
 * Воркер сам откладывает задачу 5 и 25 с, поэтому второй отсчёт с той же
 * сеткой здесь был бы просто удвоенным ожиданием того же самого. Пауза в одну
 * секунда нужна для другого случая: воркер успел закрыть задачу ошибкой, и
 * следующая постановка попадёт уже в свободную очередь.
 */
export async function runTaskWithRetry(path, body, { retryDelayMs = 1000, ...options } = {}) {
    const descriptor = await runTask(path, body, options);
    if (descriptor.status !== 'failed' || !isRetryableFailure(descriptor)) {
        return descriptor;
    }
    const { signal, onStatus } = options;
    await wait(retryDelayMs, signal);
    // Второй запрос — новое действие с новым ключом: прежний ключ вернул бы
    // отменённую задачу вместо исполнения.
    const again = await runTask(path, body, { ...options, idempotencyKey: undefined });
    if (onStatus) onStatus(again);
    return again;
}

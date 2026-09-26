import axios from 'axios';
import { API_BASE_URL } from '../config';

let sessionToken = null;

export const apiClient = axios.create({
    baseURL: API_BASE_URL,
    timeout: 30000,
});

apiClient.interceptors.request.use((config) => {
    if (sessionToken) {
        config.headers.Authorization = `Bearer ${sessionToken}`;
    }
    return config;
});

export const setSessionToken = (token) => {
    sessionToken = token || null;
};

const FIELD_LABELS = {
    name: 'название',
    email: 'рабочая почта',
    password: 'пароль',
    new_password: 'новый пароль',
    color: 'цвет',
    role: 'роль',
    token: 'токен',
    domain: 'домен',
};

const fieldLabel = (path) => FIELD_LABELS[path[path.length - 1]] ?? 'поле';

// Pydantic отвечает на 422 списком англоязычных сообщений — без разбора
// пользователь видел бы «Не удалось создать папку» вместо причины.
const validationMessage = (items) => {
    const first = items.find((item) => item?.type) ?? items[0];
    if (!first) return null;
    const path = Array.isArray(first.loc) ? first.loc.filter((part) => part !== 'body') : [];
    const field = path.length ? fieldLabel(path) : 'поле';
    const limit = first.ctx?.max_length ?? first.ctx?.le ?? first.ctx?.ge;

    switch (first.type) {
        case 'string_too_long': return `«${field}» длиннее допустимого: не более ${limit} символов.`;
        case 'string_too_short': return `«${field}» нельзя оставить пустым.`;
        case 'missing': return `Не заполнено: «${field}».`;
        case 'greater_than_equal': return `«${field}»: значение не меньше ${limit}.`;
        case 'less_than_equal': return `«${field}»: значение не больше ${limit}.`;
        case 'value_error': return typeof first.msg === 'string' ? first.msg.replace(/^Value error,\s*/, '') : null;
        default: return null;
    }
};

export const toUserMessage = (error, fallback = 'Не удалось выполнить действие. Попробуйте ещё раз.') => {
    const status = error?.response?.status;
    const detail = error?.response?.data?.detail;

    if (!status) {
        // Отличаем транспорт от локальной ошибки: «сервис недоступен» про
        // упавший html2canvas — неправда, и она прячет настоящий fallback.
        const transport = Boolean(error?.isAxiosError || error?.request
            || /ERR_NETWORK|ERR_TIMEDOUT|ECONNREFUSED|ETIMEDOUT/.test(String(error?.code ?? '')));
        return transport ? 'Сервис временно недоступен. Попробуйте ещё раз.' : fallback;
    }

    if (status >= 500) {
        return 'Сервис временно недоступен. Попробуйте ещё раз.';
    }

    if (status === 401) return 'Сессия истекла. Войдите в аккаунт ещё раз.';
    if (status === 403) return 'У вас недостаточно прав для этого действия.';

    if (Array.isArray(detail)) {
        return validationMessage(detail) ?? fallback;
    }

    // Английский detail бэкенда показывать нельзя: на русском экране он читается
    // как сообщение из чужого продукта.
    if (typeof detail === 'string' && detail.length <= 180 && /[а-яА-ЯёЁ]/.test(detail)) {
        return detail;
    }

    return fallback;
};

export default apiClient;

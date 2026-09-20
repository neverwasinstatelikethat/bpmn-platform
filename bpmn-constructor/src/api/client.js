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

export const toUserMessage = (error, fallback = 'Не удалось выполнить действие. Попробуйте ещё раз.') => {
    const status = error?.response?.status;
    const detail = error?.response?.data?.detail;

    if (status >= 500 || !status) {
        return 'Сервис временно недоступен. Попробуйте ещё раз.';
    }

    if (status === 401) return 'Сессия истекла. Войдите в аккаунт ещё раз.';
    if (status === 403) return 'У вас недостаточно прав для этого действия.';
    if (status >= 400 && status < 500 && typeof detail === 'string' && detail.length <= 180) {
        return detail;
    }

    return fallback;
};

export default apiClient;

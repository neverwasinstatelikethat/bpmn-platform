import React, { createContext, useCallback, useContext, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { accountApi } from '../api/account';
import { setSessionToken, toUserMessage } from '../api/client';

const AuthContext = createContext();

export const AuthProvider = ({ children }) => {
    const [user, setUser] = useState(null);
    // loading — только восстановление сессии при загрузке приложения:
    // ProtectedRoute по нему решает «показать лоадер» или «отправить на /login»,
    // поэтому мутации (вход, регистрация) его не трогают и не мигают лоадером.
    const [loading, setLoading] = useState(true);
    const navigate = useNavigate();

    useEffect(() => {
        let active = true;
        const restoreSession = async () => {
            const token = localStorage.getItem('token');
            if (!token) {
                if (active) setLoading(false);
                return;
            }

            setSessionToken(token);
            try {
                const { data } = await accountApi.getMe();
                if (active) setUser({ ...data, token });
            } catch {
                localStorage.removeItem('token');
                setSessionToken(null);
                if (active) setUser(null);
            } finally {
                if (active) setLoading(false);
            }
        };

        restoreSession();
        return () => { active = false; };
    }, []);

    const logout = useCallback(() => {
        localStorage.removeItem('token');
        setSessionToken(null);
        setUser(null);
        navigate('/login');
    }, [navigate]);

    /**
     * Вход по учётным данным. `returnTo` — маршрут, с которого человека
     * выгнала защита (в том числе /invite/:token): возврат делает этот метод,
     * а не экран, иначе два navigate спорят за адресата.
     */
    const login = useCallback(async (email, password, { returnTo } = {}) => {
        let token = null;
        try {
            const { data } = await accountApi.login(email, password);
            token = data?.access_token || null;
        } catch (error) {
            // 401 на входе — это неверные данные, а не протухшая сессия:
            // общий разбор иначе отвечает «Сессия истекла…».
            setSessionToken(null);
            localStorage.removeItem('token');
            const reason = error?.response?.status === 401
                ? 'Неверные учётные данные.'
                : toUserMessage(error, 'Не удалось войти.');
            throw new Error(reason);
        }

        if (!token) throw new Error('Не удалось начать сессию. Попробуйте ещё раз.');

        setSessionToken(token);
        let profile = null;
        try {
            const { data } = await accountApi.getMe();
            profile = { ...data, token };
            setUser(profile);
        } catch (error) {
            setSessionToken(null);
            localStorage.removeItem('token');
            throw new Error(toUserMessage(error, 'Не удалось загрузить профиль. Войдите ещё раз.'));
        }

        localStorage.setItem('token', token);
        navigate(returnTo || '/my-schemas', { replace: true });
        return profile;
    }, [navigate]);

    const register = useCallback(async (name, email, password) => {
        // Отвечаем только за отказ /api/register: автовход делает login(), и его
        // сообщение он формирует сам. Раньше ошибка входа переворачивалась здесь
        // вторично и превращалась в «Сервис временно недоступен», а loading
        // мигал дважды.
        try {
            await accountApi.register(name, email, password);
        } catch (error) {
            throw new Error(toUserMessage(error, 'Не удалось зарегистрироваться.'));
        }
        return login(email, password);
    }, [login]);

    const resetPasswordRequest = useCallback(async (email) => {
        try {
            return (await accountApi.requestPasswordReset(email)).data;
        } catch (error) {
            throw new Error(toUserMessage(error, 'Не удалось отправить письмо для восстановления.'));
        }
    }, []);

    const resetPassword = useCallback(async (token, newPassword) => {
        try {
            return (await accountApi.resetPassword(token, newPassword)).data;
        } catch (error) {
            throw new Error(toUserMessage(error, 'Не удалось обновить пароль.'));
        }
    }, []);

    /**
     * Годится ли ссылка восстановления. Никогда не бросает: false означает
     * «ссылку не принять», и экран показывает это сразу, а не после отправки
     * нового пароля. Вызывается из Login при входе в режим reset.
     */
    const verifyResetToken = useCallback(async (token) => {
        try {
            return Boolean((await accountApi.verifyResetToken(token)).data.valid);
        } catch {
            return false;
        }
    }, []);

    const updateUserProfile = useCallback(async (profileData) => {
        try {
            const { data } = await accountApi.updateProfile(profileData);
            setUser((current) => current ? { ...current, ...data } : current);
            return data;
        } catch (error) {
            throw new Error(toUserMessage(error, 'Не удалось обновить профиль.'));
        }
    }, []);

    return (
        <AuthContext.Provider value={{
            user, loading, login, register, logout, resetPasswordRequest,
            resetPassword, verifyResetToken, updateUserProfile,
        }}>
            {children}
        </AuthContext.Provider>
    );
};

export const useAuth = () => {
    const context = useContext(AuthContext);
    if (!context) throw new Error('useAuth must be used within an AuthProvider');
    return context;
};

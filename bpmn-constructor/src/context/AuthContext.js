import React, { createContext, useCallback, useContext, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { accountApi } from '../api/account';
import { setSessionToken, toUserMessage } from '../api/client';

const AuthContext = createContext();

export const AuthProvider = ({ children }) => {
    const [user, setUser] = useState(null);
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

    const login = useCallback(async (email, password) => {
        setLoading(true);
        try {
            const { data: loginData } = await accountApi.login(email, password);
            const token = loginData.access_token;
            if (!token) throw new Error('Не удалось начать сессию.');

            setSessionToken(token);
            const { data: profile } = await accountApi.getMe();
            const userData = { ...profile, token };
            localStorage.setItem('token', token);
            setUser(userData);
            navigate('/my-schemas');
            return userData;
        } catch (error) {
            setSessionToken(null);
            localStorage.removeItem('token');
            throw new Error(toUserMessage(error, 'Неверные учётные данные.'));
        } finally {
            setLoading(false);
        }
    }, [navigate]);

    const register = useCallback(async (name, email, password) => {
        setLoading(true);
        try {
            await accountApi.register(name, email, password);
            return await login(email, password);
        } catch (error) {
            throw new Error(toUserMessage(error, 'Не удалось зарегистрироваться.'));
        } finally {
            setLoading(false);
        }
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

import { apiClient } from './client';

export const accountApi = {
    login: (email, password) => apiClient.post('/api/login', { email, password }),
    register: (name, email, password) => apiClient.post('/api/register', { name, email, password }),
    getMe: () => apiClient.get('/api/me'),
    requestPasswordReset: (email) => apiClient.post('/api/reset-password-request', {
        email: email.trim().toLowerCase(),
    }),
    resetPassword: (token, newPassword) => apiClient.post('/api/reset-password', {
        token,
        new_password: newPassword,
    }),
    verifyResetToken: (token) => apiClient.post('/api/verify-reset-token', null, { params: { token } }),
    updateProfile: (profileData) => apiClient.put('/api/profile', profileData),
};

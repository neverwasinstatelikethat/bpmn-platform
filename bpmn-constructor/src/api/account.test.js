jest.mock('./client', () => ({
    apiClient: { post: jest.fn(), get: jest.fn(), put: jest.fn() },
}));

import { accountApi } from './account';
import { apiClient } from './client';

describe('accountApi', () => {
    it('requests a password reset using a normalized email', async () => {
        apiClient.post.mockResolvedValue({ data: { success: true } });

        await accountApi.requestPasswordReset('USER@VKUSVILL.RU');

        expect(apiClient.post).toHaveBeenCalledWith('/api/reset-password-request', {
            email: 'user@vkusvill.ru',
        });
    });
});

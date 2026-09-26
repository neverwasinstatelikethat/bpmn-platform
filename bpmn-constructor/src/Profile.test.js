import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import Profile from './Profile';

const mockUpdate = jest.fn();
// Страница собирается на контролах из components/ui (Button ссылается на
// react-router-dom) — мокаем роутер по образцу Login.test.js.
jest.mock('react-router-dom', () => ({
    Link: ({ children, ...props }) => <a {...props}>{children}</a>,
}), { virtual: true });
jest.mock('./context/AuthContext', () => ({ useAuth: () => ({ user: { name: 'Марина', email: 'marina@vkusvill.ru' }, updateUserProfile: mockUpdate }) }));
jest.mock('./api/client', () => ({ apiClient: { get: () => Promise.resolve({ data: [] }), post: jest.fn() }, toUserMessage: () => 'Ошибка' }));

describe('Profile', () => {
    it('updates personal data from the profile form', async () => {
        render(<Profile />);
        fireEvent.click(screen.getByRole('button', { name: 'Редактировать профиль' }));
        fireEvent.change(screen.getByLabelText('Имя'), { target: { value: 'Марина К.' } });
        fireEvent.click(screen.getByRole('button', { name: 'Сохранить изменения' }));
        await waitFor(() => expect(mockUpdate).toHaveBeenCalledWith(expect.objectContaining({ name: 'Марина К.' })));
    });
});

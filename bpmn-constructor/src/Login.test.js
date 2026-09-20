import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import Login from './Login';

const mockLogin = jest.fn();
jest.mock('react-router-dom', () => ({
    Link: ({ children, ...props }) => <a {...props}>{children}</a>,
    useNavigate: () => jest.fn(),
    useLocation: () => ({ state: null }),
}), { virtual: true });
jest.mock('./context/AuthContext', () => ({ useAuth: () => ({ login: mockLogin, resetPasswordRequest: jest.fn(), resetPassword: jest.fn() }) }));

describe('Login', () => {
    it('submits the credentials entered in the accessible form', async () => {
        mockLogin.mockResolvedValue({});
        render(<Login />);

        fireEvent.change(screen.getByLabelText('Рабочая почта'), { target: { value: 'user@vkusvill.ru' } });
        fireEvent.change(screen.getByLabelText('Пароль'), { target: { value: 'secret' } });
        fireEvent.click(screen.getByRole('button', { name: 'Войти' }));

        await waitFor(() => expect(mockLogin).toHaveBeenCalledWith('user@vkusvill.ru', 'secret'));
    });
});

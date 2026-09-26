import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import Login from './Login';

const mockLogin = jest.fn();
const mockResetPasswordRequest = jest.fn();
const mockResetPassword = jest.fn();
const mockVerifyResetToken = jest.fn();
const mockNavigate = jest.fn();
let mockLocation = { state: null, pathname: '/login' };
let mockSearchParams = new URLSearchParams();

jest.mock('react-router-dom', () => ({
    Link: ({ children, ...props }) => <a {...props}>{children}</a>,
    useNavigate: () => mockNavigate,
    useLocation: () => mockLocation,
    useSearchParams: () => [mockSearchParams, jest.fn()],
}), { virtual: true });
jest.mock('./context/AuthContext', () => ({
    useAuth: () => ({
        login: mockLogin,
        resetPasswordRequest: mockResetPasswordRequest,
        resetPassword: mockResetPassword,
        verifyResetToken: mockVerifyResetToken,
    }),
}));

describe('Login', () => {
    beforeEach(() => {
        mockLocation = { state: null, pathname: '/login' };
        mockSearchParams = new URLSearchParams();
        mockLogin.mockReset().mockResolvedValue({});
        mockVerifyResetToken.mockReset().mockResolvedValue(true);
    });

    it('submits the credentials entered in the accessible form', async () => {
        render(<Login />);

        fireEvent.change(screen.getByLabelText('Рабочая почта'), { target: { value: 'user@vkusvill.ru' } });
        fireEvent.change(screen.getByLabelText('Пароль'), { target: { value: 'secret' } });
        fireEvent.click(screen.getByRole('button', { name: 'Войти' }));

        await waitFor(() => expect(mockLogin).toHaveBeenCalledWith('user@vkusvill.ru', 'secret', { returnTo: null }));
    });

    it('hands the return route to login so an invited user goes back to the invite', async () => {
        mockLocation = { state: { from: '/invite/t-1' }, pathname: '/login' };
        render(<Login />);

        fireEvent.change(screen.getByLabelText('Рабочая почта'), { target: { value: 'user@vkusvill.ru' } });
        fireEvent.change(screen.getByLabelText('Пароль'), { target: { value: 'secret' } });
        fireEvent.click(screen.getByRole('button', { name: 'Войти' }));

        await waitFor(() => expect(mockLogin).toHaveBeenCalledWith('user@vkusvill.ru', 'secret', { returnTo: '/invite/t-1' }));
    });

    it('checks the reset link before asking for a new password', async () => {
        mockLocation = { state: null, pathname: '/reset-password' };
        mockSearchParams = new URLSearchParams('token=abc');
        render(<Login />);

        await waitFor(() => expect(mockVerifyResetToken).toHaveBeenCalledWith('abc'));
        // selector: заголовок режима тоже называется «Новый пароль».
        expect(screen.getByLabelText('Новый пароль', { selector: 'input' })).toBeInTheDocument();
        await waitFor(() => expect(screen.getByRole('button', { name: 'Обновить пароль' })).toBeEnabled());
    });

    it('holds the submit while the link is being checked instead of guessing', async () => {
        mockLocation = { state: null, pathname: '/reset-password' };
        mockSearchParams = new URLSearchParams('token=abc');
        let release;
        mockVerifyResetToken.mockReturnValue(new Promise((resolve) => { release = resolve; }));
        render(<Login />);

        expect(screen.getByRole('button', { name: 'Обновить пароль' })).toBeDisabled();
        expect(screen.getByText('Проверяем ссылку — это займёт секунду.')).toBeInTheDocument();

        await act(async () => release(false));
        expect(screen.getByRole('alert')).toHaveTextContent('Ссылка недействительна');
        expect(screen.queryByRole('button', { name: 'Обновить пароль' })).not.toBeInTheDocument();
    });

    it('explains a reset route without a token instead of silently showing sign-in', async () => {
        mockLocation = { state: null, pathname: '/reset-password' };
        render(<Login />);

        expect(screen.getByRole('alert')).toHaveTextContent('не нашлось токена');
        expect(screen.getByRole('button', { name: 'Запросить новое письмо' })).toBeInTheDocument();
        expect(screen.queryByLabelText('Пароль', { selector: 'input' })).not.toBeInTheDocument();
    });

    it('keeps typed credentials when the server rejects them', async () => {
        mockLogin.mockRejectedValue(new Error('Неверные учётные данные.'));
        render(<Login />);

        fireEvent.change(screen.getByLabelText('Рабочая почта'), { target: { value: 'user@vkusvill.ru' } });
        fireEvent.change(screen.getByLabelText('Пароль'), { target: { value: 'secret' } });
        fireEvent.click(screen.getByRole('button', { name: 'Войти' }));

        expect(await screen.findByRole('alert')).toHaveTextContent('Неверные учётные данные.');
        expect(screen.getByLabelText('Рабочая почта')).toHaveValue('user@vkusvill.ru');
        expect(screen.getByLabelText('Пароль')).toHaveValue('secret');
    });
});

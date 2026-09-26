import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import Register from './Register';

const mockRegister = jest.fn();
const mockSearchParams = new URLSearchParams();

jest.mock('react-router-dom', () => ({
    Link: ({ children, ...props }) => <a {...props}>{children}</a>,
    useSearchParams: () => [mockSearchParams, jest.fn()],
}), { virtual: true });
jest.mock('./context/AuthContext', () => ({ useAuth: () => ({ register: mockRegister }) }));

const fill = (password, confirmation) => {
    fireEvent.change(screen.getByLabelText('Имя'), { target: { value: 'Марина' } });
    fireEvent.change(screen.getByLabelText('Рабочая почта'), { target: { value: 'marina@vkusvill.ru' } });
    fireEvent.change(screen.getByLabelText('Пароль'), { target: { value: password } });
    fireEvent.change(screen.getByLabelText('Повторите пароль'), { target: { value: confirmation } });
};

describe('Register', () => {
    beforeEach(() => mockRegister.mockReset().mockResolvedValue({}));

    it('submits the account details once both passwords match', async () => {
        render(<Register />);
        fill('secret1', 'secret1');
        fireEvent.click(screen.getByRole('button', { name: 'Создать аккаунт' }));

        await waitFor(() => expect(mockRegister).toHaveBeenCalledWith('Марина', 'marina@vkusvill.ru', 'secret1'));
    });

    it('explains a password mismatch and keeps everything that was typed', async () => {
        render(<Register />);
        fill('secret1', 'secret2');
        fireEvent.click(screen.getByRole('button', { name: 'Создать аккаунт' }));

        expect(await screen.findByRole('alert')).toHaveTextContent('Пароли не совпадают.');
        expect(mockRegister).not.toHaveBeenCalled();
        expect(screen.getByLabelText('Имя')).toHaveValue('Марина');
        expect(screen.getByLabelText('Повторите пароль')).toHaveValue('secret2');
    });
});

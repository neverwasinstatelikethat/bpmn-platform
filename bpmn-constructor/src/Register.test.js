import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import Register from './Register';

const mockRegister = jest.fn();
jest.mock('react-router-dom', () => ({
    Link: ({ children, ...props }) => <a {...props}>{children}</a>,
    useSearchParams: () => [new URLSearchParams(), jest.fn()],
}), { virtual: true });
jest.mock('./context/AuthContext', () => ({ useAuth: () => ({ register: mockRegister }) }));

describe('Register', () => {
    it('does not submit until matching passwords and consent are present', async () => {
        render(<Register />);
        fireEvent.change(screen.getByLabelText('Имя'), { target: { value: 'Марина' } });
        fireEvent.change(screen.getByLabelText('Рабочая почта'), { target: { value: 'marina@vkusvill.ru' } });
        fireEvent.change(screen.getByLabelText('Пароль'), { target: { value: 'secret1' } });
        fireEvent.change(screen.getByLabelText('Повторите пароль'), { target: { value: 'secret1' } });
        fireEvent.click(screen.getByRole('checkbox', { name: /условия использования/i }));
        fireEvent.click(screen.getByRole('button', { name: 'Создать аккаунт' }));

        await waitFor(() => expect(mockRegister).toHaveBeenCalledWith('Марина', 'marina@vkusvill.ru', 'secret1'));
    });
});

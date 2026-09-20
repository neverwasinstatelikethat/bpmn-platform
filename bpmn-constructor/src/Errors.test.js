import { fireEvent, render, screen } from '@testing-library/react';
import Errors from './Errors';

describe('Errors', () => {
    it('opens a practical correction for a BPMN error', () => {
        render(<Errors />);
        fireEvent.click(screen.getByRole('button', { name: /обработки исключения/i }));
        expect(screen.getByText(/граничное событие ошибки/i)).toBeInTheDocument();
    });
});

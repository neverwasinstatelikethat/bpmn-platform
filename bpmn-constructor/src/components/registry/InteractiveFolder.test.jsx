import { fireEvent, render, screen } from '@testing-library/react';
import InteractiveFolder from './InteractiveFolder';

describe('InteractiveFolder', () => {
    it('reveals its diagram count when opened', () => {
        render(<InteractiveFolder name="Закупки" diagrams={[{ id: '1' }, { id: '2' }]} />);
        fireEvent.click(screen.getByRole('button', { name: /закупки/i }));
        expect(screen.getByText('2 схемы', { selector: 'strong' })).toBeInTheDocument();
    });
});

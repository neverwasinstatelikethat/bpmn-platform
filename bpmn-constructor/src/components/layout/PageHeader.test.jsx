import { render, screen } from '@testing-library/react';
import PageHeader from './PageHeader';

describe('PageHeader', () => {
    it('keeps page title, description and action in one labelled header', () => {
        render(<PageHeader title="Реестр схем" description="Ваши процессы" action={<button>Новая схема</button>} />);

        expect(screen.getByRole('heading', { name: 'Реестр схем' })).toBeInTheDocument();
        expect(screen.getByText('Ваши процессы')).toBeInTheDocument();
        expect(screen.getByRole('button', { name: 'Новая схема' })).toBeInTheDocument();
    });
});

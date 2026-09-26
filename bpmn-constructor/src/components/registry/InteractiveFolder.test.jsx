import { fireEvent, render, screen } from '@testing-library/react';
import InteractiveFolder from './InteractiveFolder';

const diagrams = [
    { id: '1', name: 'Приёмка товаров', score: 82 },
    { id: '2', name: 'Возврат брака', score: 0 },
    { id: '3', name: 'Пересмотр матрицы', score: 44 },
    { id: '4', name: 'Четвёртая схема', score: 60 },
];

describe('InteractiveFolder', () => {
    it('names the folder and its count for assistive tech', () => {
        render(<InteractiveFolder name="Закупки" diagrams={diagrams} />);
        expect(screen.getByRole('button', { name: 'Папка «Закупки» — 4 схемы' })).toHaveAttribute('aria-expanded', 'false');
    });

    it('fans out real diagrams only after the folder is opened', () => {
        render(<InteractiveFolder name="Закупки" diagrams={diagrams} />);
        expect(screen.queryByRole('button', { name: /открыть в редакторе/i })).not.toBeInTheDocument();

        fireEvent.click(screen.getByRole('button', { name: /закупки/i }));
        const papers = screen.getAllByRole('button', { name: /открыть в редакторе/i });
        expect(papers).toHaveLength(3);
        expect(papers[0]).toHaveAccessibleName('Открыть в редакторе: Приёмка товаров');
    });

    it('opens the diagram picked from a paper', () => {
        const onOpenDiagram = jest.fn();
        render(<InteractiveFolder name="Закупки" diagrams={diagrams} onOpenDiagram={onOpenDiagram} />);
        fireEvent.click(screen.getByRole('button', { name: /закупки/i }));
        fireEvent.click(screen.getByRole('button', { name: /возврат брака/i }));
        expect(onOpenDiagram).toHaveBeenCalledWith('2');
    });

    it('marks unscored papers instead of showing them as zero', () => {
        render(<InteractiveFolder name="Закупки" diagrams={diagrams} />);
        fireEvent.click(screen.getByRole('button', { name: /закупки/i }));
        expect(screen.getByText('82/100')).toBeInTheDocument();
        expect(screen.getByText('без оценки')).toBeInTheDocument();
    });

    it('says so when the folder holds nothing', () => {
        render(<InteractiveFolder name="Закупки" diagrams={[]} />);
        fireEvent.click(screen.getByRole('button', { name: /закупки/i }));
        expect(screen.getByText('В папке пока нет схем')).toBeInTheDocument();
    });

    it('keeps management actions available while open', () => {
        render(<InteractiveFolder name="Закупки" diagrams={diagrams}><button>Удалить папку</button></InteractiveFolder>);
        fireEvent.click(screen.getByRole('button', { name: /закупки/i }));
        expect(screen.getByRole('button', { name: 'Удалить папку' })).toBeInTheDocument();
    });
});

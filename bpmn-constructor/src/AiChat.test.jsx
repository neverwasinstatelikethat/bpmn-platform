import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import React from 'react';
import GenerateChat from './GenerateChat';
import ImproveChat from './ImproveChat';

// Кнопка из components/ui тянет react-router-dom — мокаем как в Login.test.js:
// в jest-разрешении у пакета нет CJS-входа, а сам роутер чату не нужен.
jest.mock('react-router-dom', () => ({
    Link: ({ children, ...props }) => <a {...props}>{children}</a>,
    useNavigate: () => jest.fn(),
    useLocation: () => ({ state: null }),
}), { virtual: true });

const withMatchMedia = (reduced) => {
    window.matchMedia = jest.fn().mockImplementation((query) => ({
        media: query,
        matches: reduced,
        addEventListener: jest.fn(),
        removeEventListener: jest.fn(),
        addListener: jest.fn(),
        removeListener: jest.fn(),
        dispatchEvent: jest.fn(),
    }));
};

const chatProps = {
    isOpen: true,
    isExpanded: false,
    onClose: jest.fn(),
    onToggleExpand: jest.fn(),
};

describe('AiChat (общий чат генерации и улучшения)', () => {
    const originalMatchMedia = window.matchMedia;

    beforeEach(() => withMatchMedia(false));
    afterEach(() => {
        window.matchMedia = originalMatchMedia;
        delete window.webkitSpeechRecognition;
        jest.restoreAllMocks();
    });

    it('держит ровно один честный индикатор загрузки и не печатает заготовленную реплику', async () => {
        const messages = [];
        let release;
        const inFlight = new Promise((resolve) => { release = resolve; });
        let force;

        const Harness = () => {
            [, force] = React.useReducer((x) => x + 1, 0);
            return (
                <GenerateChat
                    {...chatProps}
                    messages={messages}
                    onGenerate={(prompt) => {
                        messages.push(
                            { sender: 'user', text: prompt, id: 1 },
                            { sender: 'AI', isLoading: true, id: 2 }
                        );
                        force();
                        return inFlight;
                    }}
                />
            );
        };

        render(<Harness />);
        fireEvent.change(screen.getByLabelText('Запрос к ИИ'), { target: { value: 'Воронка продаж' } });
        fireEvent.click(screen.getByRole('button', { name: 'Генерировать' }));

        await waitFor(() => expect(messages).toHaveLength(2));
        expect(document.querySelectorAll('.typing-indicator')).toHaveLength(1);
        expect(screen.getByText('ИИ генерирует схему...')).toBeInTheDocument();
        expect(screen.queryByText(/Схема генерируется/)).not.toBeInTheDocument();
        await act(async () => { release(); });
    });

    it('не предлагает принять то, где XML не менялся, и даёт решить один раз', async () => {
        const analysisOnly = {
            sender: 'AI', text: 'Узкие места: два параллельных шлюза', id: 1,
            improvementId: 'imp-1', status: 'analysis_only'
        };
        const { rerender } = render(
            <ImproveChat {...chatProps} messages={[analysisOnly]} onImprove={jest.fn()} />
        );
        expect(screen.queryByRole('button', { name: /Принять изменения/ })).not.toBeInTheDocument();

        const onAccept = jest.fn().mockResolvedValue(undefined);
        rerender(
            <ImproveChat
                {...chatProps}
                messages={[{ ...analysisOnly, status: 'success' }]}
                onImprove={jest.fn()}
                onAcceptImprovement={onAccept}
                onRejectImprovement={jest.fn()}
            />
        );
        fireEvent.click(screen.getByRole('button', { name: /Принять изменения/ }));

        await waitFor(() => expect(onAccept).toHaveBeenCalledWith('imp-1'));
        expect(onAccept).toHaveBeenCalledTimes(1);
        await waitFor(() => expect(screen.getByText('Изменения приняты.')).toBeInTheDocument());
        expect(screen.queryByRole('button', { name: /Принять изменения/ })).not.toBeInTheDocument();
    });

    it('возвращает черновик в поле ввода после восстановимой ошибки', async () => {
        const onGenerate = jest.fn().mockRejectedValue(new Error('сервис недоступен'));
        render(<GenerateChat {...chatProps} messages={[]} onGenerate={onGenerate} />);

        fireEvent.change(screen.getByLabelText('Запрос к ИИ'), { target: { value: 'Согласование договора' } });
        fireEvent.click(screen.getByRole('button', { name: 'Генерировать' }));

        await waitFor(() => expect(screen.getByRole('button', { name: /Повторить/ })).toBeInTheDocument());
        expect(screen.getByLabelText('Запрос к ИИ')).toHaveValue('Согласование договора');
    });

    it('не дублирует черновик пользователя в ленте сообщений', async () => {
        let release;
        const inFlight = new Promise((resolve) => { release = resolve; });
        render(
            <GenerateChat
                {...chatProps}
                messages={[]}
                onGenerate={() => inFlight}
            />
        );
        fireEvent.change(screen.getByLabelText('Запрос к ИИ'), { target: { value: 'Обработка заявки' } });
        fireEvent.click(screen.getByRole('button', { name: 'Генерировать' }));

        // Сообщение пользователя кладёт Editor; чат сам ничего в ленту не добавляет.
        expect(document.querySelectorAll('.message.user')).toHaveLength(0);
        await act(async () => { release(); });
    });

    it('обрывает распознавание речи при закрытии панели и при unmount', () => {
        let instance = null;
        window.webkitSpeechRecognition = class {
            constructor() {
                this.onresult = null;
                this.onstart = null;
                this.onend = null;
                this.onerror = null;
                this.aborted = false;
                instance = this;
            }

            start() {}

            abort() { this.aborted = true; }
        };

        const { rerender, unmount } = render(
            <GenerateChat {...chatProps} messages={[]} onGenerate={jest.fn()} />
        );
        fireEvent.click(screen.getByRole('button', { name: 'Голосовой ввод' }));
        act(() => instance.onstart());
        expect(screen.getByRole('button', { name: 'Остановить запись' })).toBeInTheDocument();

        rerender(<GenerateChat {...chatProps} isOpen={false} messages={[]} onGenerate={jest.fn()} />);
        expect(instance.aborted).toBe(true);
        expect(instance.onresult).toBeNull();

        unmount();
    });

    it('показывает финальный текст целиком при prefers-reduced-motion', async () => {
        withMatchMedia(true);
        render(
            <ImproveChat
                {...chatProps}
                messages={[{ sender: 'AI', text: 'Добавлен узел «Проверка платежа».', id: 1 }]}
                onImprove={jest.fn()}
            />
        );
        await waitFor(() =>
            expect(screen.getByText('Добавлен узел «Проверка платежа».')).toBeInTheDocument());
    });
});

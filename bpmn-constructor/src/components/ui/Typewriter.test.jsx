import { act, render, screen } from '@testing-library/react';
import Typewriter, { usePrefersReducedMotion } from './Typewriter';

const mockMatchMedia = (reduced) => jest.fn().mockImplementation((query) => ({
    media: query,
    matches: reduced,
    addEventListener: jest.fn(),
    removeEventListener: jest.fn(),
    addListener: jest.fn(),
    removeListener: jest.fn(),
    dispatchEvent: jest.fn(),
}));

describe('Typewriter', () => {
    const originalMatchMedia = window.matchMedia;

    beforeEach(() => {
        jest.useFakeTimers();
        window.matchMedia = mockMatchMedia(false);
    });
    afterEach(() => {
        jest.useRealTimers();
        window.matchMedia = originalMatchMedia;
    });

    it('reveals one character after the requested 50ms delay', () => {
        render(<Typewriter text="ИИ" speed={50} />);
        act(() => jest.advanceTimersByTime(50));
        expect(screen.getByText('И')).toBeInTheDocument();
    });

    it('cancels the pending timer on unmount', () => {
        const clearSpy = jest.spyOn(global, 'clearTimeout');
        const { unmount } = render(<Typewriter text="ИИ" speed={50} />);
        clearSpy.mockClear();

        unmount();
        expect(clearSpy).toHaveBeenCalled();
    });

    it('shows stable final text immediately for prefers-reduced-motion', () => {
        window.matchMedia = mockMatchMedia(true);
        render(<Typewriter text="ИИ печатает ответ" speed={50} startDelay={400} />);

        expect(screen.getByText('ИИ печатает ответ')).toBeInTheDocument();
        // Мигающей каретки нет — она тоже движение.
        expect(document.querySelector('.ui-typewriter__caret')).toBeNull();

        act(() => jest.advanceTimersByTime(5000));
        expect(screen.getByText('ИИ печатает ответ')).toBeInTheDocument();
    });

    it('usePrefersReducedMotion follows the media query', () => {
        window.matchMedia = mockMatchMedia(true);
        let value;
        const Probe = () => {
            value = usePrefersReducedMotion();
            return null;
        };
        render(<Probe />);
        expect(value).toBe(true);
    });
});

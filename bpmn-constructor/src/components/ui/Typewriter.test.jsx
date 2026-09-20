import { act, render, screen } from '@testing-library/react';
import Typewriter from './Typewriter';

describe('Typewriter', () => {
    beforeEach(() => jest.useFakeTimers());
    afterEach(() => jest.useRealTimers());

    it('reveals one character after the requested 50ms delay', () => {
        render(<Typewriter text="ИИ" speed={50} />);
        act(() => jest.advanceTimersByTime(50));
        expect(screen.getByText('И')).toBeInTheDocument();
    });
});

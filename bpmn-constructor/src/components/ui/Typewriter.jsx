import { useEffect, useMemo, useState } from 'react';
import './ui.css';

// Печатная машинка: посимвольное раскрытие текста.
// Варианты: одна строка (text) или цикл по массиву фраз (phrases).
const Typewriter = ({
    text,
    phrases,
    speed = 50,
    startDelay = 0,
    pause = 1600,
    loop = false,
    className = '',
}) => {
    const list = useMemo(() => (phrases && phrases.length ? phrases : [text || '']), [phrases, text]);
    const [pos, setPos] = useState({ phrase: 0, chars: 0, deleting: false });
    const [started, setStarted] = useState(startDelay === 0);

    useEffect(() => {
        if (started) return undefined;
        const timer = setTimeout(() => setStarted(true), startDelay);
        return () => clearTimeout(timer);
    }, [started, startDelay]);

    useEffect(() => {
        if (!started) return undefined;

        const current = list[pos.phrase] || '';
        let delay;

        if (!pos.deleting) {
            if (pos.chars < current.length) {
                delay = speed;
            } else if (loop || pos.phrase < list.length - 1) {
                delay = pause;
            } else {
                return undefined; // допечатано до конца без цикла
            }
        } else {
            delay = 22;
        }

        const timer = setTimeout(() => {
            setPos((p) => {
                const cur = list[p.phrase] || '';
                if (!p.deleting) {
                    if (p.chars < cur.length) return { ...p, chars: p.chars + 1 };
                    return { ...p, deleting: true };
                }
                if (p.chars > 0) return { ...p, chars: p.chars - 1 };
                return { phrase: (p.phrase + 1) % list.length, chars: 0, deleting: false };
            });
        }, delay);

        return () => clearTimeout(timer);
    }, [started, pos, list, speed, pause, loop]);

    const shown = (list[pos.phrase] || '').slice(0, pos.chars);

    return (
        <span className={`ui-typewriter ${className}`.trim()} aria-live={loop ? 'off' : 'polite'}>
            {shown}
            <span className="ui-typewriter__caret" aria-hidden="true" />
        </span>
    );
};

export default Typewriter;

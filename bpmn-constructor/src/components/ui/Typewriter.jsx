import { useEffect, useMemo, useState } from 'react';
import './ui.css';

// Запрос prefers-reduced-motion:reduce. Паттерн тот же, что в ImageTrail.jsx:16:
// MotionConfig reducedMotion="user" (App.js) влияет только на анимации framer-motion,
// но не на таймерах посимвольной печати — её приходится гасить здесь.
// Один хук на три места (Typewriter, TypewriterMessage, AiChat); оркестратор может
// вынести его в src/hooks/useReducedMotion.js и переписать на него ImageTrail.
export const REDUCED_MOTION_QUERY = '(prefers-reduced-motion: reduce)';

export function usePrefersReducedMotion() {
    const [reduced, setReduced] = useState(() => {
        if (typeof window === 'undefined' || !window.matchMedia) return false;
        return window.matchMedia(REDUCED_MOTION_QUERY).matches;
    });

    useEffect(() => {
        if (typeof window === 'undefined' || !window.matchMedia) return undefined;
        const query = window.matchMedia(REDUCED_MOTION_QUERY);
        const onChange = (event) => setReduced(event.matches);
        if (typeof query.addEventListener === 'function') {
            query.addEventListener('change', onChange);
            return () => query.removeEventListener('change', onChange);
        }
        // Safari < 14: только устаревший addListener.
        query.addListener(onChange);
        return () => query.removeListener(onChange);
    }, []);

    return reduced;
}

// Печатная машинка: посимвольное раскрытие текста.
// Варианты: одна строка (text) или цикл по массиву фраз (phrases).
// При prefers-reduced-motion:reduce таймеры не заводятся вовсе — сразу
// стабильный финальный текст (design.md:34-35).
const Typewriter = ({
    text,
    phrases,
    speed = 50,
    startDelay = 0,
    pause = 1600,
    loop = false,
    className = '',
}) => {
    const reducedMotion = usePrefersReducedMotion();
    const list = useMemo(() => (phrases && phrases.length ? phrases : [text || '']), [phrases, text]);
    const [pos, setPos] = useState({ phrase: 0, chars: 0, deleting: false });
    const [started, setStarted] = useState(startDelay === 0 || reducedMotion);

    useEffect(() => {
        if (started || reducedMotion) return undefined;
        const timer = setTimeout(() => setStarted(true), startDelay);
        return () => clearTimeout(timer);
    }, [started, startDelay, reducedMotion]);

    useEffect(() => {
        if (!started || reducedMotion) return undefined;

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
    }, [started, pos, list, speed, pause, loop, reducedMotion]);

    const shown = reducedMotion
        ? (list[0] || '')
        : (list[pos.phrase] || '').slice(0, pos.chars);

    return (
        <span className={`ui-typewriter ${className}`.trim()} aria-live={loop ? 'off' : 'polite'}>
            {shown}
            {/* Каретка мигает через ui.css — в режиме reduced motion её нет. */}
            {!reducedMotion && <span className="ui-typewriter__caret" aria-hidden="true" />}
        </span>
    );
};

export default Typewriter;

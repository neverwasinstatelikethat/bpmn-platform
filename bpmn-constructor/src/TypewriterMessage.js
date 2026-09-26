import React, { useState, useEffect, useRef } from 'react';
import { usePrefersReducedMotion } from './components/ui/Typewriter';

/**
 * Печатающийся текст ответа ИИ.
 * `shouldAnimate` — печатать ли при первом появлении текста;
 * дальше текст показывается целиком, чтобы не перепечатывать
 * на каждом рендере родителя.
 *
 * `prefers-reduced-motion: reduce` — анимации нет вовсе: сразу стабильный
 * финальный текст (design.md:34-35). Оба таймера снимаются при unmount.
 */
const TypewriterMessage = ({ text, speed = 50, shouldAnimate = true }) => {
    const [displayText, setDisplayText] = useState('');
    const [isTyping, setIsTyping] = useState(false);
    const animationRef = useRef(null);
    const startTimerRef = useRef(null);
    const hasAnimatedRef = useRef(false);
    const reducedMotion = usePrefersReducedMotion();

    useEffect(() => {
        const stop = () => {
            clearTimeout(startTimerRef.current);
            startTimerRef.current = null;
            if (animationRef.current) {
                clearInterval(animationRef.current);
                animationRef.current = null;
            }
        };

        if (!text) {
            setDisplayText('');
            setIsTyping(false);
            return stop;
        }

        // Уже печатали (либо печать не нужна) — показываем актуальный текст
        // целиком без новой анимации.
        if (reducedMotion || !shouldAnimate || hasAnimatedRef.current) {
            setDisplayText(text);
            setIsTyping(false);
            return stop;
        }
        hasAnimatedRef.current = true;

        stop();
        setIsTyping(true);
        setDisplayText('');

        let i = 0;
        const startAnimation = () => {
            animationRef.current = setInterval(() => {
                if (i < text.length) {
                    setDisplayText(text.substring(0, i + 1));
                    i++;
                } else {
                    clearInterval(animationRef.current);
                    animationRef.current = null;
                    setIsTyping(false);
                }
            }, speed);
        };

        // Задержка старта, чтобы печать не накладывалась на перерисовку ленты.
        startTimerRef.current = setTimeout(startAnimation, 50);

        return stop;
    }, [text, speed, shouldAnimate, reducedMotion]);

    // Пустой ответ: только курсор, без мёртвой области.
    if (!text) {
        return (
            <div className="message-text message-text--empty">
                <span className="cursor" aria-hidden="true">|</span>
            </div>
        );
    }

    if (!displayText && !isTyping) return null;

    return (
        <div className="message-text">
            {displayText
                ? displayText.split('\n').map((line, i) => (
                    <p key={i}>{line || '\u00A0'}</p>
                ))
                : <p>{'\u00A0'}</p>}
            {isTyping && <span className="cursor" aria-hidden="true">|</span>}
        </div>
    );
};

export default TypewriterMessage;

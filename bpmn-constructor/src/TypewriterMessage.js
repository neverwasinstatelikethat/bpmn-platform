import React, { useState, useEffect, useRef } from 'react';

/**
 * Печатающийся текст ответа ИИ.
 * `shouldAnimate` — печатать ли при первом появлении текста;
 * дальше текст показывается целиком, чтобы не перепечатывать
 * на каждом рендере родителя.
 */
const TypewriterMessage = ({ text, speed = 50, shouldAnimate = true }) => {
    const [displayText, setDisplayText] = useState('');
    const [isTyping, setIsTyping] = useState(false);
    const animationRef = useRef(null);
    const startTimerRef = useRef(null);
    const hasAnimatedRef = useRef(false);

    useEffect(() => {
        if (!text) {
            setDisplayText('');
            setIsTyping(false);
            return undefined;
        }

        // Уже печатали — показываем актуальный текст без новой анимации.
        if (!shouldAnimate || hasAnimatedRef.current) {
            setDisplayText(text);
            setIsTyping(false);
            return undefined;
        }
        hasAnimatedRef.current = true;

        if (animationRef.current) {
            clearInterval(animationRef.current);
        }

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

        // Небольшая задержка для предотвращения конфликтов
        startTimerRef.current = setTimeout(startAnimation, 50);

        return () => {
            clearTimeout(startTimerRef.current);
            if (animationRef.current) {
                clearInterval(animationRef.current);
                animationRef.current = null;
            }
        };
    }, [text, speed, shouldAnimate]);

    // Пустой ответ: только курсор, без мёртвой области.
    if (!text) {
        return (
            <div className="message-text message-text--empty">
                <span className="cursor">|</span>
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
            {isTyping && <span className="cursor">|</span>}
        </div>
    );
};

export default TypewriterMessage;

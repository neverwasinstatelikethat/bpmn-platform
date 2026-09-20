import React, { useState, useEffect, useRef } from 'react';

const TypewriterMessage = ({ text, speed = 50, shouldAnimate = true }) => {
    const [displayText, setDisplayText] = useState('');
    const [isTyping, setIsTyping] = useState(false);
    const animationRef = useRef(null);
    const hasAnimatedRef = useRef(false);

    useEffect(() => {
        if (!text) {
            setDisplayText('');
            setIsTyping(false);
            return;
        }

        // Сброс состояния анимации при новом тексте
        if (shouldAnimate) {
            hasAnimatedRef.current = false;
        }

        if (!shouldAnimate || hasAnimatedRef.current) {
            setDisplayText(text);
            setIsTyping(false);
            return;
        }

        // Очищаем предыдущий интервал если он есть
        if (animationRef.current) {
            clearInterval(animationRef.current);
        }

        setIsTyping(true);
        setDisplayText(''); // Начинаем с пустой строки
        hasAnimatedRef.current = true;

        let i = 0; // Начинаем с индекса 0

        // Используем небольшую задержку перед началом анимации
        const startAnimation = () => {
            animationRef.current = setInterval(() => {
                if (i < text.length) {
                    setDisplayText(text.substring(0, i + 1)); // Используем substring для получения корректной подстроки
                    i++;
                } else {
                    clearInterval(animationRef.current);
                    setIsTyping(false);
                }
            }, speed);
        };

        // Небольшая задержка для предотвращения конфликтов
        const startTimer = setTimeout(startAnimation, 50);

        return () => {
            clearTimeout(startTimer);
            if (animationRef.current) {
                clearInterval(animationRef.current);
            }
        };
    }, [text, speed, shouldAnimate]);

    // Предотвращаем мигание контента
    const renderText = () => {
        if (!displayText && !isTyping) return null;

        // Если текст пустой но идет анимация, показываем пустую строку с курсором
        if (!displayText && isTyping) {
            return <p style={{
                opacity: 1,
                margin: '3px 0',
                padding: '2px 0',
                minHeight: '1.6em'
            }}>{'\u00A0'}</p>;
        }

        return displayText.split('\n').map((line, i) => (
            <p key={i} style={{
                opacity: 1,
                margin: '3px 0',
                padding: '2px 0',
                minHeight: '1.6em',
                wordWrap: 'break-word',
                overflowWrap: 'break-word'
            }}>
                {line || '\u00A0'} {/* Используем неразрывный пробел для пустых строк */}
            </p>
        ));
    };

    return (
        <div className="message-text" style={{
            minHeight: '1.6em',
            width: '100%'
        }}>
            {renderText()}
            {isTyping && <span className="cursor">|</span>}
        </div>
    );
};

export default TypewriterMessage;

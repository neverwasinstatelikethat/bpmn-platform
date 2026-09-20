import React, { useState, useEffect, useRef } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { FontAwesomeIcon } from '@fortawesome/react-fontawesome';
import { faLightbulb, faChevronDown, faChevronUp } from '@fortawesome/free-solid-svg-icons';

/**
 * Сворачиваемый блок размышлений ИИ.
 * `isTyping` — идёт ли прямо сейчас генерация ответа (тогда показываем точки).
 * Текст печатается один раз на экземпляр: повторные рендеры родителя и рост
 * контента не перезапускают анимацию.
 */
const ThinkBlock = ({ content, isTyping = false }) => {
    const [isExpanded, setIsExpanded] = useState(true);
    const [displayContent, setDisplayContent] = useState('');
    const hasAnimatedRef = useRef(false);
    const animationRef = useRef(null);

    useEffect(() => {
        if (!content) {
            setDisplayContent('');
            return;
        }

        // Уже печатали — просто показываем актуальный текст целиком.
        if (hasAnimatedRef.current) {
            setDisplayContent(content);
            return;
        }
        hasAnimatedRef.current = true;

        setDisplayContent('');
        let i = 0;
        animationRef.current = setInterval(() => {
            i += 1;
            setDisplayContent(content.slice(0, i));
            if (i >= content.length) {
                clearInterval(animationRef.current);
                animationRef.current = null;
            }
        }, 20);

        return () => {
            if (animationRef.current) {
                clearInterval(animationRef.current);
                animationRef.current = null;
            }
        };
    }, [content]);

    if (!content) return null;

    return (
        <motion.div
            className="think-section"
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            transition={{ duration: 0.4 }}
        >
            <button
                type="button"
                className="think-header"
                onClick={() => setIsExpanded(!isExpanded)}
                aria-expanded={isExpanded}
            >
                <FontAwesomeIcon icon={faLightbulb} className="think-header__icon" />
                <span>Размышления ИИ</span>
                <FontAwesomeIcon
                    icon={isExpanded ? faChevronUp : faChevronDown}
                    className="think-header__chevron"
                />
            </button>

            <AnimatePresence>
                {isExpanded && (
                    <motion.div
                        className="think-content"
                        initial={{ opacity: 0, height: 0 }}
                        animate={{ opacity: 1, height: 'auto' }}
                        exit={{ opacity: 0, height: 0 }}
                        transition={{ duration: 0.3 }}
                    >
                        {displayContent.split('\n').map((line, i) => (
                            <p key={i}>{line}</p>
                        ))}

                        {isTyping && (
                            <div className="think-typing">
                                <span className="think-typing__dot"></span>
                                <span className="think-typing__dot"></span>
                                <span className="think-typing__dot"></span>
                            </div>
                        )}
                    </motion.div>
                )}
            </AnimatePresence>
        </motion.div>
    );
};

export default ThinkBlock;

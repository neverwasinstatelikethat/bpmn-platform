import React, { useState, useEffect, useRef } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { FontAwesomeIcon } from '@fortawesome/react-fontawesome';
import { faLightbulb, faChevronDown, faChevronUp } from '@fortawesome/free-solid-svg-icons';

const ThinkBlock = ({ content, shouldAnimate = true }) => {
    const [isExpanded, setIsExpanded] = useState(true);
    const [displayContent, setDisplayContent] = useState('');
    const [isTyping, setIsTyping] = useState(false);
    const animationRef = useRef(null);
    const hasAnimatedRef = useRef(false);

    useEffect(() => {
        if (!content) {
            setDisplayContent('');
            setIsTyping(false);
            return;
        }

        if (shouldAnimate) {
            hasAnimatedRef.current = false;
        }

        if (!shouldAnimate || hasAnimatedRef.current) {
            setDisplayContent(content);
            setIsTyping(false);
            return;
        }

        setIsTyping(true);
        setDisplayContent('');
        hasAnimatedRef.current = true;

        let i = 0;
        animationRef.current = setInterval(() => {
            if (i < content.length) {
                setDisplayContent(prev => prev + content.charAt(i));
                i++;
            } else {
                clearInterval(animationRef.current);
                setIsTyping(false);
            }
        }, 20);

        return () => {
            if (animationRef.current) {
                clearInterval(animationRef.current);
            }
        };
    }, [content, shouldAnimate]);

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
                <FontAwesomeIcon
                    icon={faLightbulb}
                    className="text-warning mr-2"
                />
                <span>Размышления ИИ</span>
                <FontAwesomeIcon
                    icon={isExpanded ? faChevronUp : faChevronDown}
                    className="ml-auto text-sm"
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
                                <span></span>
                                <span></span>
                                <span></span>
                            </div>
                        )}
                    </motion.div>
                )}
            </AnimatePresence>
        </motion.div>
    );
};

export default ThinkBlock;

// ImproveChat.js - Обновленная версия
import React, { useState, useEffect, useRef } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { Button } from 'primereact/button';
import { InputTextarea } from 'primereact/inputtextarea';
import { FontAwesomeIcon } from '@fortawesome/react-fontawesome';
import {
    faRobot, faUser, faTimes, faExpand, faCompress,
    faMicrophone, faPaperPlane, faLightbulb, faCheckCircle
} from '@fortawesome/free-solid-svg-icons';
import ThinkBlock from './ThinkBlock';
import TypewriterMessage from './TypewriterMessage';
import './AiChat.css';

const ImproveChat = ({
    isOpen,
    onClose,
    messages,
    onImprove,
    onAcceptImprovement,
    isExpanded,
    onToggleExpand,
    chatHeight
}) => {
    const [input, setInput] = useState('');
    const [isListening, setIsListening] = useState(false);
    const [isProcessing, setIsProcessing] = useState(false);
    const messagesEndRef = useRef(null);
    const recognitionRef = useRef(null);
    const [voiceLevel, setVoiceLevel] = useState(0);

    const examples = [
        "Добавь проверку платежа перед отправкой товара",
        "Оптимизируй параллельные процессы доставки",
        "Проверь на наличие тупиковых состояний",
        "Добавь этап подтверждения заказа клиентом"
    ];

    useEffect(() => {
        scrollToBottom();
    }, [messages, input, isProcessing]);

    useEffect(() => {
        // Сбрасываем состояние обработки при получении ответа
        const lastAIMessage = messages.filter(m => m.sender === 'AI').pop();
        if (lastAIMessage && !lastAIMessage.isLoading && isProcessing) {
            setIsProcessing(false);
        }
    }, [messages, isProcessing]);

    const scrollToBottom = () => {
        setTimeout(() => {
            messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
        }, 100);
    };

    const handleSend = () => {
        if (input.trim()) {
            setIsProcessing(true);
            onImprove(input);
            setInput('');
        }
    };

    const handleExampleClick = (example) => {
        setInput(example);
        setTimeout(() => handleSend(), 300);
    };

    const handleKeyDown = (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            handleSend();
        }
    };

    const startVoiceInput = () => {
        if ('webkitSpeechRecognition' in window) {
            recognitionRef.current = new window.webkitSpeechRecognition();
            recognitionRef.current.lang = 'ru-RU';
            recognitionRef.current.interimResults = true;
            recognitionRef.current.continuous = true;

            recognitionRef.current.onresult = (event) => {
                const transcript = Array.from(event.results)
                    .map((result) => result[0].transcript)
                    .join('');
                setInput(transcript);

                if (event.results[0] && event.results[0][0]) {
                    const confidence = event.results[0][0].confidence;
                    setVoiceLevel(Math.min(1, confidence * 2));
                }
            };

            recognitionRef.current.onstart = () => {
                setIsListening(true);
            };

            recognitionRef.current.onend = () => {
                setIsListening(false);
                setVoiceLevel(0);
            };

            recognitionRef.current.start();
        } else {
            alert('Голосовой ввод не поддерживается в этом браузере.');
        }
    };

    const stopVoiceInput = () => {
        if (recognitionRef.current) {
            recognitionRef.current.stop();
            setIsListening(false);
            setVoiceLevel(0);
        }
    };

    // Функция для парсинга think-блоков
    const parseThinkBlock = (text) => {
        if (!text.includes('<think>')) return { think: '', main: text };

        const thinkStart = text.indexOf('<think>') + 7;
        const thinkEnd = text.indexOf('</think>');
        const thinkContent = text.substring(thinkStart, thinkEnd);
        const mainContent = text.substring(thinkEnd + 8);

        return { think: thinkContent, main: mainContent };
    };

    return (
        <AnimatePresence>
            {isOpen && (
                <motion.div
                    className={`ai-chat-container improve ${isExpanded ? 'expanded' : ''}`}
                    initial={{ x: '100%', opacity: 0 }}
                    animate={{ x: 0, opacity: 1 }}
                    exit={{ x: '100%', opacity: 0 }}
                    transition={{
                        type: 'spring',
                        damping: 20,
                        stiffness: 300
                    }}
                    style={{ height: chatHeight }}
                >
                    <div className="ai-chat-header">
                        <div className="flex align-items-center">
                            <FontAwesomeIcon icon={faRobot} className="mr-3 text-2xl text-primary" />
                            <h3>Улучшение схемы</h3>
                        </div>
                        <div className="ai-chat-header-buttons">
                            <button
                                className="header-button"
                                onClick={() => onToggleExpand(!isExpanded)}
                                aria-label={isExpanded ? "Свернуть чат" : "Расширить чат"}
                            >
                                <FontAwesomeIcon icon={isExpanded ? faCompress : faExpand} />
                            </button>
                            <button
                                className="header-button"
                                onClick={onClose}
                                aria-label="Закрыть чат"
                            >
                                <FontAwesomeIcon icon={faTimes} />
                            </button>
                        </div>
                    </div>

                    <div className="ai-chat-messages">
                        {messages.length === 0 && !input && !isProcessing ? (
                            <div className="welcome-message">
                                <div className="text-center mb-5">
                                    <div className="ai-icon mb-3">
                                        <FontAwesomeIcon icon={faRobot} className="text-5xl text-primary" />
                                    </div>
                                    <h3 className="welcome-title">Оптимизируйте вашу схему</h3>
                                    <p className="welcome-subtitle">Предложите улучшения или попросите ИИ проанализировать схему</p>
                                </div>

                                <div className="examples-container">
                                    {examples.map((example, index) => (
                                        <motion.div
                                            key={index}
                                            className="example-card"
                                            onClick={() => handleExampleClick(example)}
                                            whileHover={{ y: -5 }}
                                            whileTap={{ scale: 0.98 }}
                                        >
                                            <div className="example-icon">
                                                <FontAwesomeIcon icon={faLightbulb} className="text-warning" />
                                            </div>
                                            <p className="example-text">{example}</p>
                                        </motion.div>
                                    ))}
                                </div>
                            </div>
                        ) : (
                            <>
                                {messages.map((message) => {
                                    const { think, main } = parseThinkBlock(message.text);

                                    return (
                                        <motion.div
                                            key={message.id}
                                            className={`message ${message.sender}`}
                                            initial={{ opacity: 0, y: 20 }}
                                            animate={{ opacity: 1, y: 0 }}
                                            transition={{ duration: 0.4 }}
                                        >
                                            {message.sender === 'AI' && (
                                                <div className="message-avatar">
                                                    <FontAwesomeIcon icon={faRobot} />
                                                </div>
                                            )}
                                            <div className="message-content">
                                                {message.isLoading ? (
                                                    <div className="typing-indicator">
                                                        <span></span>
                                                        <span></span>
                                                        <span></span>
                                                        <span>ИИ анализирует схему...</span>
                                                    </div>
                                                ) : (
                                                    <>
                                                        {think && (
                                                            <ThinkBlock
                                                                content={think}
                                                                isTyping={isProcessing && message.id === messages[messages.length - 1]?.id}
                                                            />
                                                        )}
                                                        {main && (
                                                            <TypewriterMessage
                                                                text={main}
                                                                speed={50}
                                                                key={message.id} // Добавлен ключ для принудительного обновления
                                                            />
                                                        )}

                                                        {message.improvementId && (
                                                            <Button
                                                                label="Принять изменения"
                                                                icon={<FontAwesomeIcon icon={faCheckCircle} className="mr-2" />}
                                                                className="accept-improvement-btn mt-3"
                                                                onClick={() => onAcceptImprovement(message.improvementId)}
                                                            />
                                                        )}
                                                    </>
                                                )}
                                            </div>
                                            {message.sender === 'user' && (
                                                <div className="message-avatar">
                                                    <FontAwesomeIcon icon={faUser} />
                                                </div>
                                            )}
                                        </motion.div>
                                    );
                                })}

                                {isProcessing && (
                                    <motion.div
                                        className="message AI"
                                        initial={{ opacity: 0, y: 20 }}
                                        animate={{ opacity: 1, y: 0 }}
                                        transition={{ delay: 0.3 }}
                                    >
                                        <div className="message-avatar">
                                            <FontAwesomeIcon icon={faRobot} />
                                        </div>
                                        <div className="message-content">
                                            <div className="typing-indicator">
                                                <span></span>
                                                <span></span>
                                                <span></span>
                                                <span>ИИ оптимизирует схему...</span>
                                            </div>
                                        </div>
                                    </motion.div>
                                )}

                                {input && (
                                    <motion.div
                                        className="message user"
                                        initial={{ opacity: 0, y: 20 }}
                                        animate={{ opacity: 1, y: 0 }}
                                        transition={{ duration: 0.4 }}
                                    >
                                        <div className="message-content">
                                            <p>{input}</p>
                                        </div>
                                        <div className="message-avatar">
                                            <FontAwesomeIcon icon={faUser} />
                                        </div>
                                    </motion.div>
                                )}
                                <div ref={messagesEndRef} />
                            </>
                        )}
                    </div>

                    <div className="ai-chat-input">
                        {isListening && (
                            <motion.div
                                className="voice-input-animation"
                                style={{ height: `${4 + voiceLevel * 10}px` }}
                                animate={{ height: `${4 + voiceLevel * 10}px` }}
                                transition={{ duration: 0.1 }}
                            />
                        )}
                        <InputTextarea
                            value={input}
                            onChange={(e) => setInput(e.target.value)}
                            onKeyDown={handleKeyDown}
                            placeholder="Опишите, как улучшить текущую схему..."
                            rows={1}
                            autoResize
                            className="w-full"
                        />
                        <div className="ai-chat-buttons">
                            <Button
                                label="Отправить"
                                icon={<FontAwesomeIcon icon={faPaperPlane} className="mr-2" />}
                                className="send-button"
                                onClick={handleSend}
                                disabled={!input.trim()}
                            />
                            <Button
                                icon={<FontAwesomeIcon icon={faMicrophone} />}
                                className={`voice-button ${isListening ? 'active' : ''}`}
                                onClick={isListening ? stopVoiceInput : startVoiceInput}
                                tooltip={isListening ? "Остановить запись" : "Голосовой ввод"}
                                tooltipOptions={{ position: 'top' }}
                            />
                        </div>
                    </div>
                </motion.div>
            )}
        </AnimatePresence>
    );
};

export default ImproveChat;

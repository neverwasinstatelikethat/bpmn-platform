// GenerateChat.js — чат генерации BPMN-схемы
import React, { useState, useEffect, useRef } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { FontAwesomeIcon } from '@fortawesome/react-fontawesome';
import {
    faRobot, faUser, faTimes, faExpand, faCompress,
    faMicrophone, faPaperPlane, faLightbulb, faRotateRight
} from '@fortawesome/free-solid-svg-icons';
import { Button } from './components/ui';
import ThinkBlock from './ThinkBlock';
import TypewriterMessage from './TypewriterMessage';
import './AiChat.css';

const GenerateChat = ({
    isOpen,
    onClose,
    onGenerate,
    messages,
    isExpanded,
    onToggleExpand,
    chatHeight = '70vh'
}) => {
    const [input, setInput] = useState('');
    const [isListening, setIsListening] = useState(false);
    const [isGenerating, setIsGenerating] = useState(false);
    const [lastPrompt, setLastPrompt] = useState('');
    const [voiceLevel, setVoiceLevel] = useState(0);
    // Локальное сообщение ИИ (ошибка запроса либо недоступный голосовой ввод):
    // messages приходит из Editor пропсом, поэтому свои реплики чат держит у себя.
    // canRetry — показывать кнопку «Повторить» с lastPrompt.
    const [notice, setNotice] = useState(null);
    const messagesEndRef = useRef(null);
    const recognitionRef = useRef(null);

    const examples = [
        "Процесс оформления заказа на сайте интернет-магазина",
        "Воронка продаж для B2B компании",
        "Процесс согласования договора в юридическом отделе",
        "Обработка заявки на кредит в банке"
    ];

    useEffect(() => {
        scrollToBottom();
    }, [messages, input, isGenerating, notice]);

    useEffect(() => {
        // Сбрасываем состояние генерации при получении ответа
        const lastAIMessage = messages.filter(m => m.sender === 'AI').pop();
        if (lastAIMessage && !lastAIMessage.isLoading && isGenerating) {
            setIsGenerating(false);
        }
    }, [messages, isGenerating]);

    const scrollToBottom = () => {
        setTimeout(() => {
            messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
        }, 100);
    };

    // Общий путь отправки: и для нового запроса, и для повтора после ошибки.
    const sendPrompt = async (prompt) => {
        const text = (prompt || '').trim();
        if (!text) return;

        setLastPrompt(text);
        setNotice(null);
        setIsGenerating(true);
        try {
            await onGenerate(text);
        } catch (err) {
            setNotice({
                text: `Не удалось сгенерировать схему: ${err.message}`,
                canRetry: true
            });
        } finally {
            setIsGenerating(false);
        }
    };

    const handleSend = () => {
        const text = input.trim();
        if (!text) return;
        setInput('');
        sendPrompt(text);
    };

    const handleRetry = () => {
        sendPrompt(lastPrompt);
    };

    const handleExampleClick = (example) => {
        setInput('');
        sendPrompt(example);
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
            setNotice({ text: 'Голосовой ввод недоступен в этом браузере.', canRetry: false });
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

    const showWelcome = messages.length === 0 && !input && !isGenerating && !notice;

    const renderTyping = (label) => (
        <div className="typing-indicator">
            <span className="typing-indicator__dot"></span>
            <span className="typing-indicator__dot"></span>
            <span className="typing-indicator__dot"></span>
            <span className="typing-indicator__label">{label}</span>
        </div>
    );

    return (
        <AnimatePresence>
            {isOpen && (
                <motion.div
                    className={`ai-chat-container generate ${isExpanded ? 'expanded' : ''}`}
                    initial={{ x: '100%', opacity: 0 }}
                    animate={{ x: 0, opacity: 1 }}
                    exit={{ x: '100%', opacity: 0 }}
                    transition={{
                        type: 'spring',
                        damping: 20,
                        stiffness: 300
                    }}
                    style={{ '--chat-height': chatHeight }}
                >
                    <div className="ai-chat-header">
                        <div className="ai-chat-header__title">
                            <span className="ai-chat-header__icon">
                                <FontAwesomeIcon icon={faRobot} />
                            </span>
                            <h3>Генерация схемы</h3>
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
                        {showWelcome ? (
                            <div className="welcome-message">
                                <div className="welcome-message__hero">
                                    <div className="ai-icon">
                                        <FontAwesomeIcon icon={faRobot} />
                                    </div>
                                    <h3 className="welcome-title">Создайте BPMN-диаграмму</h3>
                                    <p className="welcome-subtitle">Опишите бизнес-процесс, и ИИ сгенерирует схему</p>
                                </div>

                                <div className="examples-container">
                                    {examples.map((example, index) => (
                                        <motion.button
                                            key={index}
                                            type="button"
                                            className="example-card"
                                            onClick={() => handleExampleClick(example)}
                                            whileHover={{ y: -5 }}
                                            whileTap={{ scale: 0.98 }}
                                        >
                                            <span className="example-icon">
                                                <FontAwesomeIcon icon={faLightbulb} />
                                            </span>
                                            <span className="example-text">{example}</span>
                                        </motion.button>
                                    ))}
                                </div>
                            </div>
                        ) : (
                            <>
                                {messages.map((message) => {
                                    const { think, main } = parseThinkBlock(message.text || '');

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
                                                    renderTyping('ИИ генерирует схему...')
                                                ) : (
                                                    <>
                                                        {think && (
                                                            <ThinkBlock
                                                                content={think}
                                                                isTyping={isGenerating && message.id === messages[messages.length - 1]?.id}
                                                            />
                                                        )}
                                                        {main && (
                                                            <TypewriterMessage
                                                                text={main}
                                                                speed={50}
                                                                key={message.id} // Добавлен ключ для принудительного обновления
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

                                {isGenerating && (
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
                                            {renderTyping('ИИ создает схему...')}
                                        </div>
                                    </motion.div>
                                )}

                                {notice && (
                                    <motion.div
                                        className="message AI message--alert"
                                        initial={{ opacity: 0, y: 20 }}
                                        animate={{ opacity: 1, y: 0 }}
                                        transition={{ duration: 0.4 }}
                                    >
                                        <div className="message-avatar">
                                            <FontAwesomeIcon icon={faRobot} />
                                        </div>
                                        <div className="message-content">
                                            <p>{notice.text}</p>
                                            {notice.canRetry && (
                                                <Button
                                                    variant="secondary"
                                                    size="sm"
                                                    className="chat-retry-btn"
                                                    onClick={handleRetry}
                                                >
                                                    <span className="chat-btn__icon">
                                                        <FontAwesomeIcon icon={faRotateRight} />
                                                    </span>
                                                    Повторить
                                                </Button>
                                            )}
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
                        <div className="ai-chat-input__row">
                            <textarea
                                className="chat-input"
                                value={input}
                                onChange={(e) => setInput(e.target.value)}
                                onKeyDown={handleKeyDown}
                                placeholder="Опишите процесс для генерации схемы..."
                                rows={2}
                                aria-label="Запрос к ИИ"
                            />
                            <div className="ai-chat-buttons">
                                <Button
                                    variant="primary"
                                    size="sm"
                                    className="chat-send-btn"
                                    onClick={handleSend}
                                    disabled={!input.trim() || isGenerating}
                                >
                                    <span className="chat-btn__icon">
                                        <FontAwesomeIcon icon={faPaperPlane} />
                                    </span>
                                    Генерировать
                                </Button>
                                <Button
                                    variant="ghost"
                                    size="sm"
                                    className={`chat-voice-btn ${isListening ? 'is-active' : ''}`}
                                    onClick={isListening ? stopVoiceInput : startVoiceInput}
                                    title={isListening ? "Остановить запись" : "Голосовой ввод"}
                                    aria-label={isListening ? "Остановить запись" : "Голосовой ввод"}
                                >
                                    <span className="chat-btn__icon">
                                        <FontAwesomeIcon icon={faMicrophone} />
                                    </span>
                                </Button>
                            </div>
                        </div>
                    </div>
                </motion.div>
            )}
        </AnimatePresence>
    );
};

export default GenerateChat;

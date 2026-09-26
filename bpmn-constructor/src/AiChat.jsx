// AiChat.jsx — один чат ИИ для двух режимов: «сгенерировать» и «улучшить».
// GenerateChat.js и ImproveChat.js — тонкие обёртки над этим компонентом,
// поэтому Editor.js продолжает импортировать прежние имена.
//
// Честность состояний (красные линии владельца):
//  - индикатор «ИИ работает» — один, он не притворяется ответом модели:
//    ответ печатается только текстом, который реально пришёл с сервера;
//  - кнопка «Принять изменения» есть только там, где бэкенд предложение XML
//    действительно вернул (status === 'success'), и только пока решение не принято;
//  - черновик в поле ввода не теряется ни при ошибке запроса, ни при закрытии панели.
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { FontAwesomeIcon } from '@fortawesome/react-fontawesome';
import {
    faTimes, faExpand, faCompress,
    faMicrophone, faCheckCircle, faRotateRight, faBan
} from '@fortawesome/free-solid-svg-icons';
import { Button } from './components/ui';
// Паттерн тот же, что в ImageTrail.jsx:16: MotionConfig reducedMotion="user"
// на setInterval не влияет, поэтому про prefers-reduced-motion спрашиваем напрямую.
import { usePrefersReducedMotion } from './components/ui/Typewriter';
import TypewriterMessage from './TypewriterMessage';
import './AiChat.css';

// Тексты и действия режимов. Всё остальное в двух чатах было общим.
// Формулировки рабочие: панель объясняет, что делает аналитик, а не
// «продаёт» ИИ — поэтому без императивов-лозунгов и восклицаний.
const MODES = {
    generate: {
        className: 'generate',
        headerTitle: 'Генерация схемы',
        welcomeTitle: 'Опишите процесс своими словами',
        welcomeSubtitle: 'Участники, шаги, развилки и сроки — ИИ соберёт схему на холсте.',
        placeholder: 'Например: согласование договора с юристом',
        sendLabel: 'Генерировать',
        loadingLabel: 'ИИ генерирует схему...',
        errorLabel: 'Не удалось сгенерировать схему',
        examplesLabel: 'Типовые задания',
        examples: [
            'Процесс оформления заказа на сайте интернет-магазина',
            'Воронка продаж для B2B компании',
            'Процесс согласования договора в юридическом отделе',
            'Обработка заявки на кредит в банке'
        ]
    },
    improve: {
        className: 'improve',
        headerTitle: 'Улучшение схемы',
        welcomeTitle: 'Что улучшить в текущей схеме',
        welcomeSubtitle: 'Опишите проблему или попросите разобрать процесс на узкие места.',
        placeholder: 'Например: добавить проверку платежа перед отгрузкой',
        sendLabel: 'Отправить',
        loadingLabel: 'ИИ анализирует схему...',
        errorLabel: 'Не удалось улучшить схему',
        examplesLabel: 'Типовые задания',
        examples: [
            'Добавь проверку платежа перед отправкой товара',
            'Оптимизируй параллельные процессы доставки',
            'Проверь на наличие тупиковых состояний',
            'Добавь этап подтверждения заказа клиентом'
        ]
    }
};

// Индикатор загрузки — один на чат. role="status" доносит смену состояния до
// скринридера, точки при prefers-reduced-motion статичны (AiChat.css).
// Он не изображает ответ модели: текст ответа печатается только из сообщений,
// которые реально вернул сервер.
const TypingIndicator = ({ label }) => (
    <div className="typing-indicator" role="status">
        <span className="typing-indicator__dot" aria-hidden="true"></span>
        <span className="typing-indicator__dot" aria-hidden="true"></span>
        <span className="typing-indicator__dot" aria-hidden="true"></span>
        <span className="typing-indicator__label">{label}</span>
    </div>
);

/* На узком экране панель — нижний лист (AiChat.css, 760px) и выезжает она
   снизу; на широком она стоит в ряду редактора и приезжает от правого края.
   Направление задаёт JS: framer-motion пишет transform инлайн-стилем,
   и медиазапросом его не перебить. */
const NARROW_QUERY = '(max-width: 760px)';
const useIsNarrow = () => {
    const [narrow, setNarrow] = useState(
        () => typeof window !== 'undefined' && !!window.matchMedia
            && window.matchMedia(NARROW_QUERY).matches
    );
    useEffect(() => {
        if (typeof window === 'undefined' || !window.matchMedia) return undefined;
        const query = window.matchMedia(NARROW_QUERY);
        const onChange = (event) => setNarrow(event.matches);
        query.addEventListener('change', onChange);
        return () => query.removeEventListener('change', onChange);
    }, []);
    return narrow;
};

const AiChat = ({
    mode = 'generate',
    isOpen,
    onClose,
    onAction,
    messages = [],
    isExpanded = false,
    onToggleExpand,
    onAcceptImprovement,
    onRejectImprovement
}) => {
    const config = MODES[mode] || MODES.generate;
    const reducedMotion = usePrefersReducedMotion();
    const isNarrow = useIsNarrow();
    const slide = isNarrow
        ? {
            initial: { y: '60%', opacity: 0 },
            animate: { y: 0, opacity: 1 },
            exit: { y: '60%', opacity: 0 },
        }
        : {
            initial: { x: '100%', opacity: 0 },
            animate: { x: 0, opacity: 1 },
            exit: { x: '100%', opacity: 0 },
        };

    const [input, setInput] = useState('');
    const [isListening, setIsListening] = useState(false);
    const [pending, setPending] = useState(false);
    const [lastPrompt, setLastPrompt] = useState('');
    const [notice, setNotice] = useState(null);
    // Решения по предложениям ИИ: id -> 'accepted' | 'rejected'. Нужен локально,
    // потому что список сообщений живёт в Editor, а действие одноразовое:
    // второй клик по «Принять» бэкенд уже не примет (404).
    const [decisions, setDecisions] = useState({});
    const [decisionBusy, setDecisionBusy] = useState(null);

    const messagesEndRef = useRef(null);
    const inputRef = useRef(null);
    const scrollTimerRef = useRef(null);
    const recognitionRef = useRef(null);
    // Черновик на момент старта записи: распознавание дописывает к нему,
    // а не затирает то, что пользователь уже набрал.
    const voiceBaseRef = useRef('');
    // Живой текст запроса, пока запрос в полёте: чтобы вернуть его в поле
    // ввода при ошибке и не затереть то, что пользователь набрал за это время.
    const inFlightPromptRef = useRef('');

    const stopVoiceInput = useCallback(() => {
        const recognition = recognitionRef.current;
        if (!recognition) return;
        // Снима обработчики первыми: распознавание может дёрнуть onresult
        // уже после закрытия панели или размонтирования.
        recognition.onresult = null;
        recognition.onstart = null;
        recognition.onend = null;
        recognition.onerror = null;
        try {
            recognition.abort();
        } catch {
            // уже остановлено — состояние ниже всё равно честное
        }
        recognitionRef.current = null;
        setIsListening(false);
    }, []);

    // Останавливаем распознавание при размонтировании и при закрытии панели.
    useEffect(() => {
        if (!isOpen) stopVoiceInput();
    }, [isOpen, stopVoiceInput]);

    useEffect(() => () => stopVoiceInput(), [stopVoiceInput]);

    // Прокрутка к последнему сообщению. Таймер обязательно снимается — и при
    // каждой смене зависимостей, и при unmount.
    useEffect(() => {
        if (!isOpen) return undefined;
        clearTimeout(scrollTimerRef.current);
        scrollTimerRef.current = setTimeout(() => {
            messagesEndRef.current?.scrollIntoView({
                behavior: reducedMotion ? 'auto' : 'smooth'
            });
        }, 100);
        return () => clearTimeout(scrollTimerRef.current);
    }, [messages, input, pending, notice, decisions, isOpen, reducedMotion]);

    // Поле растёт вместе с текстом: на узкой панели две строки давали
    // внутренний скролл уже на второй строке запроса.
    useEffect(() => {
        const el = inputRef.current;
        if (!el) return;
        el.style.height = 'auto';
        el.style.height = `${Math.min(el.scrollHeight, 140)}px`;
    }, [input, isOpen]);

    const sendPrompt = async (prompt) => {
        const text = (prompt || '').trim();
        if (!text || pending) return;

        setLastPrompt(text);
        setNotice(null);
        setPending(true);
        inFlightPromptRef.current = text;
        try {
            await onAction(text);
        } catch (err) {
            setNotice({
                text: `${config.errorLabel}: ${err?.message || 'сервис недоступен'}`,
                canRetry: true
            });
            // Восстановимая ошибка: черновик не должен пропадать.
            const draft = inFlightPromptRef.current;
            setInput((prev) => (prev.trim() ? prev : draft));
        } finally {
            inFlightPromptRef.current = '';
            setPending(false);
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
        if (recognitionRef.current) return;
        if (!('webkitSpeechRecognition' in window)) {
            setNotice({ text: 'Голосовой ввод недоступен в этом браузере.', canRetry: false });
            return;
        }

        const recognition = new window.webkitSpeechRecognition();
        recognition.lang = 'ru-RU';
        recognition.interimResults = true;
        recognition.continuous = true;
        voiceBaseRef.current = input;

        recognition.onresult = (event) => {
            const transcript = Array.from(event.results)
                .map((result) => result[0].transcript)
                .join('');
            const base = voiceBaseRef.current.trim();
            setInput([base, transcript.trim()].filter(Boolean).join(' '));
        };

        recognition.onstart = () => setIsListening(true);
        recognition.onend = () => {
            recognitionRef.current = null;
            setIsListening(false);
        };
        recognition.onerror = (event) => {
            recognitionRef.current = null;
            setIsListening(false);
            const reason = event?.error === 'not-allowed'
                ? 'Браузер запретил доступ к микрофону.'
                : 'Распознавание речи прервалось, говорите снова или наберите текст.';
            setNotice({ text: reason, canRetry: false });
        };

        recognitionRef.current = recognition;
        try {
            recognition.start();
        } catch {
            recognitionRef.current = null;
            setIsListening(false);
            setNotice({ text: 'Не удалось начать запись. Попробуйте ещё раз.', canRetry: false });
        }
    };

    // --- Принятие / отклонение предложения (только режим «улучшить») ---

    const decide = async (improvementId, kind) => {
        const handler = kind === 'accepted' ? onAcceptImprovement : onRejectImprovement;
        if (!handler || decisionBusy) return;

        setDecisionBusy(improvementId);
        setNotice(null);
        try {
            const result = await handler(improvementId);
            // Editor пока глотает ошибку и резолвится; пусть вернёт false
            // или бросит — тогда действие останется доступным для повтора.
            if (result === false) throw new Error('сервис не подтвердил решение');
            setDecisions((prev) => ({ ...prev, [improvementId]: kind }));
        } catch (err) {
            setNotice({
                text: `${kind === 'accepted' ? 'Не удалось принять' : 'Не удалось отклонить'} изменения: ${err?.message || 'сервис недоступен'}`,
                canRetry: false
            });
        } finally {
            setDecisionBusy(null);
        }
    };

    const renderImprovementActions = (message) => {
        const improvementId = message.improvementId;
        if (!improvementId) return null;

        const decided = decisions[improvementId];
        if (decided) {
            return (
                <p className="message-decision">
                    {decided === 'accepted' ? 'Изменения приняты.' : 'Изменения отклонены.'}
                </p>
            );
        }

        // Статус присылает бэкенд: 'success' — предложен XML, 'analysis_only' —
        // только разбор, принимать нечего (app/routers/ai.py:127-132).
        if (message.status !== 'success') return null;

        if (decisionBusy === improvementId) {
            return <TypingIndicator label="Применяем решение..." />;
        }

        return (
            <div className="message-actions">
                <Button
                    variant="primary"
                    size="sm"
                    className="accept-improvement-btn"
                    onClick={() => decide(improvementId, 'accepted')}
                >
                    <span className="chat-btn__icon">
                        <FontAwesomeIcon icon={faCheckCircle} />
                    </span>
                    Принять изменения
                </Button>
                {onRejectImprovement && (
                    <Button
                        variant="secondary"
                        size="sm"
                        className="reject-improvement-btn"
                        onClick={() => decide(improvementId, 'rejected')}
                    >
                        <span className="chat-btn__icon">
                            <FontAwesomeIcon icon={faBan} />
                        </span>
                        Отклонить
                    </Button>
                )}
            </div>
        );
    };

    const showWelcome = messages.length === 0 && !input && !pending && !notice;
    // Один честный индикатор: если загрузку уже описывает сообщение с
    // isLoading: true, отдельный пузырёк не добавляем.
    const hasLoadingMessage = messages.some((message) => message.isLoading);
    const isImproving = mode === 'improve';

    return (
        <AnimatePresence>
            {isOpen && (
                <motion.div
                    className={`ai-chat-container ${config.className}${isExpanded ? ' expanded' : ''}`}
                    {...slide}
                    transition={{ type: 'spring', damping: 20, stiffness: 300 }}
                >
                    <div className="ai-chat-header">
                        <div className="ai-chat-header__title">
                            <h3>{config.headerTitle}</h3>
                        </div>
                        <div className="ai-chat-header-buttons">
                            <button
                                type="button"
                                className="header-button"
                                onClick={() => onToggleExpand(!isExpanded)}
                                aria-label={isExpanded ? 'Свернуть чат' : 'Расширить чат'}
                            >
                                <FontAwesomeIcon icon={isExpanded ? faCompress : faExpand} />
                            </button>
                            <button
                                type="button"
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
                                    <h3 className="welcome-title">{config.welcomeTitle}</h3>
                                    <p className="welcome-subtitle">{config.welcomeSubtitle}</p>
                                </div>

                                <div className="examples-container">
                                    <p className="examples-label">{config.examplesLabel}</p>
                                    {config.examples.map((example) => (
                                        <button
                                            key={example}
                                            type="button"
                                            className="example-card"
                                            onClick={() => handleExampleClick(example)}
                                        >
                                            <span className="example-text">{example}</span>
                                        </button>
                                    ))}
                                </div>
                            </div>
                        ) : (
                            <>
                                {messages.map((message, index) => (
                                    <motion.div
                                        key={message.id ?? `m${index}`}
                                        className={`message ${message.sender}`}
                                        initial={{ opacity: 0, y: 8 }}
                                        animate={{ opacity: 1, y: 0 }}
                                        transition={{ duration: 0.24 }}
                                    >
                                        <div className="message-content">
                                            {message.isLoading ? (
                                                <TypingIndicator label={config.loadingLabel} />
                                            ) : (
                                                <>
                                                    {message.text && (
                                                        <TypewriterMessage
                                                            text={message.text}
                                                            speed={50}
                                                        />
                                                    )}
                                                    {isImproving && renderImprovementActions(message)}
                                                </>
                                            )}
                                        </div>
                                    </motion.div>
                                ))}

                                {pending && !hasLoadingMessage && (
                                    <motion.div
                                        className="message AI"
                                        initial={{ opacity: 0, y: 8 }}
                                        animate={{ opacity: 1, y: 0 }}
                                    >
                                        <div className="message-content">
                                            <TypingIndicator label={config.loadingLabel} />
                                        </div>
                                    </motion.div>
                                )}

                                {notice && (
                                    <motion.div
                                        className="message AI message--alert"
                                        initial={{ opacity: 0, y: 8 }}
                                        animate={{ opacity: 1, y: 0 }}
                                        transition={{ duration: 0.24 }}
                                    >
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

                                <div ref={messagesEndRef} />
                            </>
                        )}
                    </div>

                    <div className="ai-chat-input">
                        {isListening && (
                            <p className="voice-hint" role="status">
                                Слушаю — распознанный текст появится в поле ввода.
                            </p>
                        )}
                        <div className="ai-chat-input__field">
                            <textarea
                                ref={inputRef}
                                className="chat-input"
                                value={input}
                                onChange={(e) => setInput(e.target.value)}
                                onKeyDown={handleKeyDown}
                                placeholder={config.placeholder}
                                rows={3}
                                aria-label="Запрос к ИИ"
                            />
                            <div className="ai-chat-buttons">
                                <Button
                                    variant="ghost"
                                    size="sm"
                                    className={`chat-voice-btn ${isListening ? 'is-active' : ''}`}
                                    onClick={isListening ? stopVoiceInput : startVoiceInput}
                                    title={isListening ? 'Остановить запись' : 'Голосовой ввод'}
                                    aria-label={isListening ? 'Остановить запись' : 'Голосовой ввод'}
                                    aria-pressed={isListening}
                                >
                                    <span className="chat-btn__icon">
                                        <FontAwesomeIcon icon={faMicrophone} />
                                    </span>
                                </Button>
                                <Button
                                    variant="primary"
                                    size="sm"
                                    className="chat-send-btn"
                                    onClick={handleSend}
                                    disabled={!input.trim() || pending}
                                >
                                    {config.sendLabel}
                                </Button>
                            </div>
                        </div>
                    </div>
                </motion.div>
            )}
        </AnimatePresence>
    );
};

export default AiChat;

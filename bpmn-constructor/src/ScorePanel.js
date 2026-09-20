import React, { useRef, useEffect, memo } from 'react';
import { motion } from 'framer-motion';
import { FontAwesomeIcon } from '@fortawesome/react-fontawesome';
import { faTimes, faExpand, faCompress } from '@fortawesome/free-solid-svg-icons';
import './ScorePanel.css';

const ScorePanel = memo(({
    score = 0,
    recommendations = [],
    errors = {},
    onClose,
    isExpanded,
    onToggleExpand,
    busy = false,
    chatHeight = '70vh'
}) => {
    const scrollRef = useRef(null);

    // Проп position из Editor здесь намеренно не применяется: панель стоит
    // в flex-потоке редактора, её место и наложение задаёт ScorePanel.css.

    const value = Math.max(0, Math.min(100, Number(score) || 0));

    const getScoreColor = (scoreValue) => {
        if (scoreValue >= 80) return 'var(--score-good)';
        if (scoreValue >= 50) return 'var(--score-warn)';
        return 'var(--score-bad)';
    };

    const getScoreLabel = (scoreValue) => {
        if (scoreValue >= 80) return 'Отлично!';
        if (scoreValue >= 50) return 'Неплохо';
        return 'Требует доработки';
    };

    const errorMessages = {
        'start_event': 'Количество стартовых событий не соответствует числу участников',
        'end_event': 'Отсутствует конечное событие',
        'gateway_conditions': 'Эксклюзивные шлюзы без условий на выходах',
        'sequence_flows': 'Элементы не связаны последовательностями',
        'direction': 'Некорректное направление процесса',
        'naming': 'Элементы без осмысленных названий',
        'no_loops': 'Обнаружены бесконечные циклы',
        'element_count': 'Слишком много элементов в схеме',
        'no_isolated': 'Обнаружены изолированные элементы',
        'task_types': 'Недостаточное разнообразие типов задач',
        'pool_lanes': 'Отсутствуют или некорректно используются пулы и дорожки',
        'event_types': 'Отсутствуют промежуточные события',
        'documentation': 'Недостаточно документации у элементов'
    };

    useEffect(() => {
        if (scrollRef.current) {
            scrollRef.current.scrollTop = 0;
        }
    }, [recommendations, errors]);

    const scoreColor = getScoreColor(value);
    const errorEntries = Object.entries(errors || {})
        .filter(([, isCorrect]) => !isCorrect)
        .map(([key]) => ({ key, message: errorMessages[key] }))
        .filter((entry) => entry.message);
    const hasContent = value > 0
        || (recommendations?.length || 0) > 0
        || errorEntries.length > 0;

    return (
        <motion.div
            className={`ai-chat-container score ${isExpanded ? 'expanded' : ''}`}
            initial={{ opacity: 0, y: 20, scale: 0.95 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: 20, scale: 0.95 }}
            transition={{ type: 'spring', damping: 25, stiffness: 300 }}
            style={{ height: chatHeight }}
        >
            <div className="ai-chat-header">
                <motion.h3
                    initial={{ opacity: 0, x: -10 }}
                    animate={{ opacity: 1, x: 0 }}
                    transition={{ delay: 0.1 }}
                >
                    Оценка схемы
                </motion.h3>
                <div className="ai-chat-header-buttons">
                    <button
                        className="header-button"
                        onClick={() => onToggleExpand(!isExpanded)}
                        aria-label={isExpanded ? "Свернуть панель" : "Расширить панель"}
                    >
                        <FontAwesomeIcon icon={isExpanded ? faCompress : faExpand} />
                    </button>
                    <button
                        className="header-button"
                        onClick={onClose}
                        aria-label="Закрыть панель"
                    >
                        <FontAwesomeIcon icon={faTimes} />
                    </button>
                </div>
            </div>

            {busy && (
                <div className="score-busy" role="status">
                    <span className="score-busy__dot"></span>
                    <span className="score-busy__dot"></span>
                    <span className="score-busy__dot"></span>
                    <span className="score-busy__label">Считаем оценку схемы...</span>
                </div>
            )}

            {!busy && !hasContent && (
                <div className="score-hint">
                    <p className="score-hint__title">Оценки пока нет</p>
                    <p className="score-hint__text">
                        Нажмите «Проверить» на панели инструментов — ИИ посчитает качество
                        схемы и подскажет, что улучшить.
                    </p>
                </div>
            )}

            {!busy && hasContent && (
                <div className="score-content">
                    <div className="score-fixed-section">
                        <motion.div
                            className="score-display"
                            initial={{ opacity: 0, y: 10 }}
                            animate={{ opacity: 1, y: 0 }}
                            transition={{ delay: 0.2 }}
                        >
                            <div className="score-circle-container">
                                <div
                                    className="score-circle"
                                    style={{
                                        background: `conic-gradient(${scoreColor} ${value}%, var(--score-track) ${value}% 100%)`
                                    }}
                                >
                                    <div className="score-circle-inner">
                                        <span>{value}</span>
                                    </div>
                                </div>
                                <div className="score-labels">
                                    <div className="score-value">{value}/100</div>
                                    <div className="score-status">{getScoreLabel(value)}</div>
                                </div>
                            </div>
                        </motion.div>
                    </div>

                    <div ref={scrollRef} className="score-scrollable-content">
                        <motion.div
                            className="sections-container"
                            initial={{ opacity: 0 }}
                            animate={{ opacity: 1 }}
                            transition={{ delay: 0.3 }}
                        >
                            {errorEntries.length > 0 && (
                                <div className="errors-section">
                                    <motion.h4
                                        className="score-section-title"
                                        initial={{ opacity: 0 }}
                                        animate={{ opacity: 1 }}
                                        transition={{ delay: 0.35 }}
                                    >
                                        Найденные ошибки
                                    </motion.h4>
                                    <ul>
                                        {errorEntries.map((entry, index) => (
                                            <motion.li
                                                key={entry.key}
                                                initial={{ opacity: 0, y: 5 }}
                                                animate={{ opacity: 1, y: 0 }}
                                                transition={{ delay: 0.1 * index }}
                                                whileHover={{ x: 5 }}
                                            >
                                                <div className="error-icon">!</div>
                                                <div>
                                                    <div className="error-title">{entry.message}</div>
                                                    <div className="error-solution">
                                                        Как исправить: {entry.message
                                                            .replace('Отсутствует', 'Добавьте')
                                                            .replace('без', 'с указанием')
                                                            .replace('Недостаточно', 'Добавьте больше')
                                                            .replace('Недостаточное', 'Увеличьте')
                                                            .replace('Обнаружены', 'Удалите')
                                                            .replace('не соответствует числу участников', 'согласуйте с количеством участников')}
                                                    </div>
                                                </div>
                                            </motion.li>
                                        ))}
                                    </ul>
                                </div>
                            )}

                            {recommendations?.length > 0 && (
                                <div className="recommendations-section">
                                    <motion.h4
                                        className="score-section-title"
                                        initial={{ opacity: 0 }}
                                        animate={{ opacity: 1 }}
                                        transition={{ delay: 0.4 }}
                                    >
                                        Рекомендации по улучшению
                                    </motion.h4>
                                    <ul>
                                        {recommendations.map((rec, index) => (
                                            <motion.li
                                                key={index}
                                                initial={{ opacity: 0, y: 5 }}
                                                animate={{ opacity: 1, y: 0 }}
                                                transition={{ delay: 0.1 * index + 0.45 }}
                                                whileHover={{ x: 5 }}
                                            >
                                                <div className="recommendation-icon">✓</div>
                                                {rec}
                                            </motion.li>
                                        ))}
                                    </ul>
                                </div>
                            )}
                        </motion.div>

                        <motion.div
                            className="score-footer"
                            initial={{ opacity: 0 }}
                            animate={{ opacity: 1 }}
                            transition={{ delay: 0.5 }}
                        >
                            <div className="score-progress">
                                <div
                                    className="progress-bar"
                                    style={{ width: `${value}%`, background: scoreColor }}
                                />
                            </div>
                            <div className="progress-labels">
                                <span>0</span>
                                <span>50</span>
                                <span>100</span>
                            </div>
                        </motion.div>
                    </div>
                </div>
            )}
        </motion.div>
    );
});

export default ScorePanel;

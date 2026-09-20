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
    position = { top: '20px', right: '20px' },
    chatHeight = '70vh'
}) => {
    const scrollRef = useRef(null);

    const getScoreColor = (score) => {
        if (score >= 80) return '#00A550';
        if (score >= 50) return '#FFD508';
        return '#F62369';
    };

    const getScoreLabel = (score) => {
        if (score >= 80) return 'Отлично!';
        if (score >= 50) return 'Неплохо';
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

    return (
        <motion.div
            className={`ai-chat-container score ${isExpanded ? 'expanded' : ''}`}
            initial={{ opacity: 0, y: 20, scale: 0.95 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: 20, scale: 0.95 }}
            transition={{ type: 'spring', damping: 25, stiffness: 300 }}
            style={{
                height: chatHeight,
                top: position.top,
                right: position.right,
                zIndex: 1000
            }}
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
                                    background: `conic-gradient(${getScoreColor(score)} ${score}%, #F0F2F5 ${score}% 100%)`
                                }}
                            >
                                <div className="score-circle-inner">
                                    <span>{score}</span>
                                </div>
                            </div>
                            <div className="score-labels">
                                <div className="score-value">{score}/100</div>
                                <div className="score-status">{getScoreLabel(score)}</div>
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
                        {errors && Object.keys(errors).length > 0 && Object.values(errors).some(val => !val) && (
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
                                    {Object.entries(errors).map(([key, isCorrect], index) => (
                                        !isCorrect && errorMessages[key] && (
                                            <motion.li
                                                key={key}
                                                initial={{ opacity: 0, y: 5 }}
                                                animate={{ opacity: 1, y: 0 }}
                                                transition={{ delay: 0.1 * index }}
                                                whileHover={{ x: 5 }}
                                            >
                                                <div className="error-icon">!</div>
                                                <div>
                                                    <div className="error-title">{errorMessages[key]}</div>
                                                    <div className="error-solution">
                                                        Как исправить: {errorMessages[key]
                                                            .replace('Отсутствует', 'Добавьте')
                                                            .replace('без', 'с указанием')
                                                            .replace('Недостаточно', 'Добавьте больше')
                                                            .replace('Недостаточное', 'Увеличьте')
                                                            .replace('Обнаружены', 'Удалите')
                                                            .replace('не соответствует числу участников', 'согласуйте с количеством участников')}
                                                    </div>
                                                </div>
                                            </motion.li>
                                        )
                                    ))}
                                </ul>
                            </div>
                        )}

                        {recommendations && recommendations.length > 0 && (
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
                                style={{ width: `${score}%`, background: getScoreColor(score) }}
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
        </motion.div>
    );
});

export default ScorePanel;
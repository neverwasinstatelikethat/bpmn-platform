import React, { useState, useRef, useEffect } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import BpmnModeler from 'bpmn-js/lib/Modeler';
import 'bpmn-js/dist/assets/diagram-js.css';
import 'bpmn-js/dist/assets/bpmn-font/css/bpmn.css';
import './Guideline.css';
import { guidelineExamples } from './guidelineExamples';
import WorkPage from './components/layout/WorkPage';
import PageLoader from './components/layout/PageLoader';

// SVG иконки в стиле ВкусВилл: цвет задаётся классом в Guideline.css (токены), не хардкодом.
const CheckIcon = () => (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
        <path className="guideline-icon guideline-icon--good" d="M9 16.17L4.83 12L3.41 13.41L9 19L21 7L19.59 5.59L9 16.17Z" />
    </svg>
);

const WarningIcon = () => (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
        <path className="guideline-icon guideline-icon--bad" d="M12 2C6.48 2 2 6.48 2 12C2 17.52 6.48 22 12 22C17.52 22 22 17.52 22 12C22 6.48 17.52 2 12 2ZM13 17H11V15H13V17ZM13 13H11V7H13V13Z" />
    </svg>
);

const DIAGRAM_ERROR_TEXT = 'Схему-пример не удалось отрисовать. Перезагрузите страницу или закройте и снова откройте этот раздел.';

/**
 * Теория приходит из guidelineExamples с markdown-акцентом **…** и переносами строк.
 * Разбираем текст в React-узлы (без dangerouslySetInnerHTML): акцент — <strong>,
 * перенос строки — <br />.
 */
const renderRichText = (text) => (text || '').split('\n').map((line, lineIndex) => (
    <React.Fragment key={lineIndex}>
        {lineIndex > 0 ? <br /> : null}
        {line.split(/(\*\*[^*]+\*\*)/g).filter(Boolean).map((chunk, chunkIndex) => (
            chunk.length > 4 && chunk.startsWith('**') && chunk.endsWith('**')
                ? <strong key={chunkIndex} className="guideline-em">{chunk.slice(2, -2)}</strong>
                : <React.Fragment key={chunkIndex}>{chunk}</React.Fragment>
        ))}
    </React.Fragment>
));

const GuidelineSection = ({ elementKey, data }) => {
    const [isOpen, setIsOpen] = useState(false);
    const incorrectContainerRef = useRef(null);
    const correctContainerRef = useRef(null);
    const [diagramLoading, setDiagramLoading] = useState(false);
    const [diagramErrors, setDiagramErrors] = useState({});
    const [testCompleted, setTestCompleted] = useState(false);
    const [testAnswers, setTestAnswers] = useState({});
    const [testResult, setTestResult] = useState(null);

    // Модели диаграмм живут только на время открытого раздела: при закрытии
    // контейнеры размонтируются, поэтому bpmn-js пересоздаётся при каждом открытии.
    useEffect(() => {
        if (!isOpen) return undefined;

        const diagrams = [
            { key: 'incorrect', ref: incorrectContainerRef, xml: data.incorrectExample.xml, label: 'неправильный пример' },
            { key: 'correct', ref: correctContainerRef, xml: data.correctExample.xml, label: 'правильный пример' },
        ];
        const created = [];
        let cancelled = false;
        let settled = 0;

        const settle = () => {
            settled += 1;
            if (settled === diagrams.length && !cancelled) setDiagramLoading(false);
        };

        const fail = (key, err, stage) => {
            console.error(`Ошибка bpmn-js (${stage}):`, err);
            if (!cancelled) setDiagramErrors((current) => ({ ...current, [key]: DIAGRAM_ERROR_TEXT }));
        };

        setDiagramLoading(true);
        setDiagramErrors({});

        diagrams.forEach(({ key, ref, xml, label }) => {
            try {
                if (!ref.current) throw new Error('Контейнер диаграммы недоступен');
                const modeler = new BpmnModeler({ container: ref.current });
                created.push(modeler);

                modeler.importXML(xml).then((result) => {
                    if (result && result.warnings && result.warnings.length) {
                        console.warn(`Предупреждения bpmn-js (${label}):`, result.warnings);
                    }
                    modeler.get('canvas').zoom('fit-viewport', 'auto');
                }).catch((err) => {
                    fail(key, err, `загрузка диаграммы (${label})`);
                }).finally(settle);
            } catch (err) {
                fail(key, err, `инициализация bpmn-js (${label})`);
                settle();
            }
        });

        return () => {
            cancelled = true;
            created.forEach((modeler) => {
                try {
                    modeler.destroy();
                } catch (err) {
                    console.error('Ошибка очистки bpmn-js:', err);
                }
            });
        };
    }, [isOpen, data.incorrectExample.xml, data.correctExample.xml]);

    const testQuestions = [
        { id: 'required', text: `Элемент ${elementKey} обязателен в процессе`, correct: !!data.evaluationCriteria.requiredElements?.length },
        { id: 'singleOccurrence', text: `Элемент ${elementKey} должен быть только один`, correct: data.evaluationCriteria.maxOccurrences === 1 },
        { id: 'outgoingFlows', text: `Элемент ${elementKey} требует исходящих потоков`, correct: !!data.evaluationCriteria.outgoingFlows },
        { id: 'incomingFlows', text: `Элемент ${elementKey} требует входящих потоков`, correct: !!data.evaluationCriteria.minIncomingFlows },
        { id: 'conditions', text: `Элемент ${elementKey} требует условий`, correct: !!data.evaluationCriteria.conditions },
        { id: 'attached', text: `Элемент ${elementKey} должен быть привязан к задаче`, correct: !!data.evaluationCriteria.attachedToTask },
        { id: 'calledElement', text: `Элемент ${elementKey} требует ссылки на процесс`, correct: !!data.evaluationCriteria.calledElement },
    ];

    const handleTestSubmit = (e) => {
        e.preventDefault();
        const criteria = data.evaluationCriteria;
        let score = 0;
        let total = testQuestions.length;
        let penalty = 0;

        testQuestions.forEach(({ id, correct }) => {
            const userAnswer = testAnswers[id] || false;
            if (userAnswer === correct) score += 1;
            else penalty += 0.5;
        });

        if (criteria.minOutgoingFlows) {
            const userValue = testAnswers.minOutgoingFlows || 0;
            if (userValue >= criteria.minOutgoingFlows) score += 1;
            else penalty += 0.5;
            total += 1;
        }
        if (criteria.minInternalElements) {
            const userValue = testAnswers.minInternalElements || 0;
            if (userValue >= criteria.minInternalElements) score += 1;
            else penalty += 0.5;
            total += 1;
        }

        const finalScore = Math.max(0, (score / total) * 100 - penalty * 5);
        setTestResult(finalScore);
        setTestCompleted(true);
    };

    return (
        <motion.div
            className="guideline-section"
            initial={{ opacity: 0, y: 5 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.3 }}
        >
            <motion.button
                type="button"
                className="guideline-section__trigger"
                onClick={() => setIsOpen(!isOpen)}
                aria-expanded={isOpen}
                whileHover={{ backgroundColor: 'var(--color-surface-mist)' }}
                transition={{ duration: 0.2 }}
            >
                <span>{data.title}</span>
                {testCompleted ? <CheckIcon /> : isOpen ? <WarningIcon /> : <WarningIcon />}
            </motion.button>
            <AnimatePresence>
                {isOpen && (
                    <motion.div
                        className="content"
                        initial={{ height: 0, opacity: 0 }}
                        animate={{ height: 'auto', opacity: 1 }}
                        exit={{ height: 0, opacity: 0 }}
                        transition={{ duration: 0.3 }}
                    >
                        <div className="theory">
                            <motion.h3 initial={{ x: -10, opacity: 0 }} animate={{ x: 0, opacity: 1 }} transition={{ delay: 0.1, duration: 0.2 }}>
                                Теория
                            </motion.h3>
                            <motion.p initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ delay: 0.2, duration: 0.2 }}>
                                {renderRichText(data.description)}
                            </motion.p>
                            <motion.h4 initial={{ x: -10, opacity: 0 }} animate={{ x: 0, opacity: 1 }} transition={{ delay: 0.3, duration: 0.2 }}>
                                Неправильный пример
                            </motion.h4>
                            <motion.p initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ delay: 0.4, duration: 0.2 }}>
                                {renderRichText(data.incorrectExample.description)}
                            </motion.p>
                            <motion.div className="diagram-container" initial={{ scale: 0.95, opacity: 0 }} animate={{ scale: 1, opacity: 1 }} transition={{ delay: 0.5, duration: 0.2 }}>
                                {diagramLoading && (
                                    <div className="diagram-container__overlay">
                                        <PageLoader label="Рисуем пример схемы…" />
                                    </div>
                                )}
                                <div ref={incorrectContainerRef} className="diagram" />
                            </motion.div>
                            {diagramErrors.incorrect && (
                                <p className="guideline-error" role="alert">{diagramErrors.incorrect}</p>
                            )}
                            <motion.h4 initial={{ x: -10, opacity: 0 }} animate={{ x: 0, opacity: 1 }} transition={{ delay: 0.6, duration: 0.2 }}>
                                Правильный пример
                            </motion.h4>
                            <motion.p initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ delay: 0.7, duration: 0.2 }}>
                                {renderRichText(data.correctExample.description)}
                            </motion.p>
                            <motion.div className="diagram-container" initial={{ scale: 0.95, opacity: 0 }} animate={{ scale: 1, opacity: 1 }} transition={{ delay: 0.8, duration: 0.2 }}>
                                {diagramLoading && (
                                    <div className="diagram-container__overlay">
                                        <PageLoader label="Рисуем пример схемы…" />
                                    </div>
                                )}
                                <div ref={correctContainerRef} className="diagram" />
                            </motion.div>
                            {diagramErrors.correct && (
                                <p className="guideline-error" role="alert">{diagramErrors.correct}</p>
                            )}
                        </div>
                        <div className="test">
                            <motion.h3 initial={{ x: -10, opacity: 0 }} animate={{ x: 0, opacity: 1 }} transition={{ delay: 0.9, duration: 0.2 }}>
                                Тест
                            </motion.h3>
                            <form onSubmit={handleTestSubmit}>
                                {testQuestions.map(({ id, text }, index) => (
                                    <motion.div key={id} initial={{ x: -10, opacity: 0 }} animate={{ x: 0, opacity: 1 }} transition={{ delay: 1.0 + 0.1 * index, duration: 0.2 }}>
                                        <label>
                                            <input type="checkbox" checked={testAnswers[id] || false} onChange={(e) => setTestAnswers({ ...testAnswers, [id]: e.target.checked })} /> {text}
                                        </label>
                                    </motion.div>
                                ))}
                                {data.evaluationCriteria.minOutgoingFlows && (
                                    <motion.div initial={{ x: -10, opacity: 0 }} animate={{ x: 0, opacity: 1 }} transition={{ delay: 1.0 + 0.1 * testQuestions.length, duration: 0.2 }}>
                                        <label>
                                            Минимальное количество исходящих потоков:
                                            <input type="number" value={testAnswers.minOutgoingFlows || 0} onChange={(e) => setTestAnswers({ ...testAnswers, minOutgoingFlows: parseInt(e.target.value) })} />
                                        </label>
                                    </motion.div>
                                )}
                                {data.evaluationCriteria.minInternalElements && (
                                    <motion.div initial={{ x: -10, opacity: 0 }} animate={{ x: 0, opacity: 1 }} transition={{ delay: 1.0 + 0.1 * (testQuestions.length + 1), duration: 0.2 }}>
                                        <label>
                                            Минимальное количество внутренних элементов:
                                            <input type="number" value={testAnswers.minInternalElements || 0} onChange={(e) => setTestAnswers({ ...testAnswers, minInternalElements: parseInt(e.target.value) })} />
                                        </label>
                                    </motion.div>
                                )}
                                <motion.button type="submit" whileHover={{ scale: 1.03 }} whileTap={{ scale: 0.97 }} transition={{ duration: 0.2 }}>
                                    Проверить
                                </motion.button>
                            </form>
                            {testResult !== null && (
                                <motion.div className={`result ${testResult >= 80 ? 'pass' : 'fail'}`} initial={{ opacity: 0, y: 5 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.2 }}>
                                    Ваш результат: {Math.round(testResult)}% {testResult >= 80 ? 'Отлично!' : 'Попробуйте снова.'}
                                </motion.div>
                            )}
                        </div>
                    </motion.div>
                )}
            </AnimatePresence>
        </motion.div>
    );
};

const Guideline = () => {
    const [activeGroup, setActiveGroup] = useState(null);
    const groups = {
        Подпроцесс: ['subProcess', 'callActivity'],
        Сообщения: ['messageEvent'],
        Таймер: ['timerEvent'],
        Активность: ['task'],
        Развилки: ['exclusiveGateway', 'parallelGateway'],
        События: ['startEvent', 'boundaryEvent'],
    };

    return (
        <WorkPage title="Мастерская BPMN" eyebrow="обучение" description="Разбирайте элементы схемы на коротких примерах: что сработает, что запутает команду и как это исправить." className="guideline">
            <div className="guideline-content">
                <motion.div className="guideline-sidebar" initial={{ x: -20, opacity: 0 }} animate={{ x: 0, opacity: 1 }} transition={{ delay: 0.1, duration: 0.3 }}>
                    {Object.keys(groups).map((group) => (
                        <motion.button
                            type="button"
                            key={group}
                            className={`sidebar-item ${activeGroup === group ? 'active' : ''}`}
                            onClick={() => setActiveGroup(activeGroup === group ? null : group)}
                            whileHover={{ backgroundColor: 'var(--color-surface-mist)' }}
                            transition={{ duration: 0.2 }}
                        >
                            {group}
                        </motion.button>
                    ))}
                </motion.div>
                <motion.div className="guideline-main" initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ delay: 0.2, duration: 0.3 }}>
                    <AnimatePresence>
                        {activeGroup && (
                            <motion.div
                                className="guideline-sections"
                                initial={{ opacity: 0, y: 5 }}
                                animate={{ opacity: 1, y: 0 }}
                                exit={{ opacity: 0, y: -5 }}
                                transition={{ duration: 0.3 }}
                            >
                                {groups[activeGroup].map((key) => (
                                    <GuidelineSection key={key} elementKey={key} data={guidelineExamples[key]} />
                                ))}
                            </motion.div>
                        )}
                    </AnimatePresence>
                </motion.div>
            </div>
        </WorkPage>
    );
};

export default Guideline;

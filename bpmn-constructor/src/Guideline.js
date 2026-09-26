import React, { useEffect, useRef, useState } from 'react';
import { motion } from 'framer-motion';
import BpmnViewer from 'bpmn-js/lib/NavigatedViewer';
import 'bpmn-js/dist/assets/diagram-js.css';
import './Guideline.css';
import { guidelineExamples } from './guidelineExamples';
import WorkPage from './components/layout/WorkPage';
import PageLoader from './components/layout/PageLoader';
import EmptyState from './components/layout/EmptyState';
// Компоненты берём напрямую из модулей, а не через barrel `components/ui`:
// barrel тянет Button с react-router-dom, который не разбирается сбором jest.
import Accordion from './components/ui/Accordion';
import Button from './components/ui/Button';

const DIAGRAM_ERROR_TEXT = 'Схему-пример не удалось отрисовать. Перезагрузите страницу или закройте и снова откройте этот раздел.';

// Верхняя граница числового ответа: минимум переходов или внутренних элементов
// в бизнес-схемах двузначным не бывает, а валидные границы полю нужны.
const COUNT_MAX = 10;

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

/**
 * Правила проверки строятся от человекочитаемого названия элемента (`data.title`),
 * а не от внутреннего ключа BPMN: `subProcess` и `timerEvent` остаются в данных
 * и в схеме, но подписи видит человек. Предикаты — в настоящем времени, чтобы
 * согласование не зависело от рода названия.
 */
const buildRules = (data) => {
    const criteria = data.evaluationCriteria || {};
    const name = `«${data.title}»`;
    const rules = [
        {
            id: 'required',
            kind: 'boolean',
            statement: `В процессе нужен элемент ${name}`,
            expected: !!criteria.requiredElements?.length,
        },
        {
            id: 'singleOccurrence',
            kind: 'boolean',
            statement: `${name} встречается в процессе только один раз`,
            expected: criteria.maxOccurrences === 1,
        },
        {
            id: 'outgoingFlows',
            kind: 'boolean',
            statement: `${name} требует исходящих переходов`,
            expected: !!criteria.outgoingFlows,
        },
        {
            id: 'incomingFlows',
            kind: 'boolean',
            statement: `${name} требует входящих переходов`,
            expected: !!criteria.minIncomingFlows,
        },
        {
            id: 'conditions',
            kind: 'boolean',
            statement: `${name} требует условий на переходах`,
            expected: !!criteria.conditions,
        },
        {
            id: 'attached',
            kind: 'boolean',
            statement: `${name} прикрепляется к задаче`,
            expected: !!criteria.attachedToTask,
        },
        {
            id: 'calledElement',
            kind: 'boolean',
            statement: `${name} ссылается на другой, отдельный процесс`,
            expected: !!criteria.calledElement,
        },
    ];

    if (criteria.minOutgoingFlows) {
        rules.push({
            id: 'minOutgoingFlows',
            kind: 'count',
            statement: `Минимальное количество исходящих переходов для ${name}`,
            expected: criteria.minOutgoingFlows,
        });
    }
    if (criteria.minInternalElements) {
        rules.push({
            id: 'minInternalElements',
            kind: 'count',
            statement: `Минимальное количество элементов внутри ${name}`,
            expected: criteria.minInternalElements,
        });
    }

    return rules;
};

/** Пустое или неразобранное поле — «нет ответа», а не NaN и не молчаливый ноль. */
const parseCount = (raw) => {
    if (raw === '' || raw === null || raw === undefined) return null;
    const value = Number(raw);
    return Number.isInteger(value) && value >= 0 ? value : null;
};

/**
 * Честный вердикт вместо процента: либо все правила совпали, либо перечислены
 * те, где ответ неверный. Никаких синтетических «86 %» и штрафов за ошибку.
 */
const gradeTest = (rules, answers) => {
    const failures = [];

    rules.forEach((rule) => {
        if (rule.kind === 'boolean') {
            if (Boolean(answers[rule.id]) !== rule.expected) {
                failures.push({ statement: rule.statement, answer: `правило ${rule.expected ? 'верно' : 'неверно'}` });
            }
            return;
        }
        const value = parseCount(answers[rule.id]);
        if (value === null) {
            failures.push({ statement: rule.statement, answer: `нужно число от 0 до ${COUNT_MAX}` });
        } else if (value < rule.expected) {
            failures.push({ statement: rule.statement, answer: `не меньше ${rule.expected}` });
        }
    });

    return { passed: failures.length === 0, failures };
};

/**
 * Учебная схема — только для чтения: NavigatedViewer даёт масштаб и
 * перетаскивание, но не рисует палитру и не даёт стереть узел без отката,
 * что делает BpmnModeler в обучающем блоке.
 */
const DiagramPreview = ({ xml, label }) => {
    const containerRef = useRef(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');

    useEffect(() => {
        const container = containerRef.current;
        if (!container) {
            setLoading(false);
            setError(DIAGRAM_ERROR_TEXT);
            return undefined;
        }

        let cancelled = false;
        let viewer = null;

        setLoading(true);
        setError('');

        try {
            viewer = new BpmnViewer({ container });
        } catch (initError) {
            console.error(`Ошибка инициализации bpmn-js (${label}):`, initError);
            setLoading(false);
            setError(DIAGRAM_ERROR_TEXT);
            return undefined;
        }

        viewer.importXML(xml)
            .then((result) => {
                if (result && result.warnings && result.warnings.length) {
                    console.warn(`Предупреждения bpmn-js (${label}):`, result.warnings);
                }
                viewer.get('canvas').zoom('fit-viewport', 'auto');
            })
            .catch((importError) => {
                console.error(`Ошибка импорта схемы (${label}):`, importError);
                if (!cancelled) setError(DIAGRAM_ERROR_TEXT);
            })
            .finally(() => {
                if (!cancelled) setLoading(false);
            });

        return () => {
            cancelled = true;
            try {
                viewer.destroy();
            } catch (destroyError) {
                console.error('Ошибка очистки bpmn-js:', destroyError);
            }
        };
    }, [xml, label]);

    return (
        <>
            <div className="diagram-container">
                {loading && (
                    <div className="diagram-container__overlay">
                        <PageLoader label="Рисуем пример схемы…" />
                    </div>
                )}
                <div ref={containerRef} className="diagram" />
            </div>
            {error && <p className="guideline-error" role="alert">{error}</p>}
        </>
    );
};

/**
 * Содержимое темы: теория со схемами и проверка. Вердикт отдаём наверх —
 * метку «зачтено/не зачтено» видно в заголовке аккордеона, не только внутри.
 */
const GuidelineTopic = ({ data, onVerdict }) => {
    const [answers, setAnswers] = useState({});
    const [result, setResult] = useState(null);
    const rules = buildRules(data);
    const booleanRules = rules.filter((rule) => rule.kind === 'boolean');
    const countRules = rules.filter((rule) => rule.kind === 'count');

    const setAnswer = (id, value) => {
        setAnswers((current) => ({ ...current, [id]: value }));
    };

    const handleTestSubmit = (event) => {
        event.preventDefault();
        const verdict = gradeTest(rules, answers);
        setResult(verdict);
        onVerdict(verdict);
    };

    return (
        <div className="guideline-body">
            <div className="theory">
                <h3>Теория</h3>
                <p>{renderRichText(data.description)}</p>

                <h4>Неправильный пример</h4>
                <p>{renderRichText(data.incorrectExample.description)}</p>
                <DiagramPreview xml={data.incorrectExample.xml} label="неправильный пример" />

                <h4>Правильный пример</h4>
                <p>{renderRichText(data.correctExample.description)}</p>
                <DiagramPreview xml={data.correctExample.xml} label="правильный пример" />
            </div>

            <div className="test">
                <h3>Проверьте себя</h3>
                <form onSubmit={handleTestSubmit}>
                    <p className="test__hint">
                        Отметьте утверждения, которые верны для этого элемента, и укажите нужные минимумы.
                    </p>

                    {booleanRules.map((rule) => (
                        <label className="test__option" key={rule.id}>
                            <input
                                type="checkbox"
                                checked={Boolean(answers[rule.id])}
                                onChange={(event) => setAnswer(rule.id, event.target.checked)}
                            />
                            <span>{rule.statement}</span>
                        </label>
                    ))}

                    {countRules.map((rule) => (
                        <label className="test__field" key={rule.id}>
                            <span>{rule.statement}</span>
                            <span className="test__field-line">
                                <input
                                    type="number"
                                    inputMode="numeric"
                                    min={0}
                                    max={COUNT_MAX}
                                    step={1}
                                    value={answers[rule.id] ?? ''}
                                    onChange={(event) => setAnswer(rule.id, event.target.value)}
                                />
                                <span className="test__bounds">0–{COUNT_MAX}</span>
                            </span>
                        </label>
                    ))}

                    <Button className="test__submit" type="submit" variant="primary" size="md">
                        Проверить
                    </Button>
                </form>

                {result && (
                    <div className="result" role="status">
                        <span className={`verdict-chip verdict-chip--${result.passed ? 'pass' : 'fail'}`}>
                            {result.passed ? 'Зачтено' : 'Не зачтено'}
                        </span>
                        {result.passed ? (
                            <p className="result__note">Все правила совпали с тем, как элемент используется в процессе.</p>
                        ) : (
                            <>
                                <p className="result__note">Правила, где ответ разошёлся:</p>
                                <ul className="result__list">
                                    {result.failures.map((failure) => (
                                        <li key={failure.statement}>
                                            {failure.statement} — {failure.answer}
                                        </li>
                                    ))}
                                </ul>
                            </>
                        )}
                    </div>
                )}
            </div>
        </div>
    );
};

const GROUPS = {
    'Подпроцесс': ['subProcess', 'callActivity'],
    'Сообщения': ['messageEvent'],
    'Таймер': ['timerEvent'],
    'Активность': ['task'],
    'Развилки': ['exclusiveGateway', 'parallelGateway'],
    'События': ['startEvent', 'boundaryEvent'],
};

/**
 * Аккордеон один на всю группу: `Accordion` строит id панелей по индексу внутри
 * своего списка, поэтому несколько экземпляров на странице дали бы повторяющиеся
 * `faq-trigger-0` и сломали бы `aria-labelledby`.
 */
const Guideline = () => {
    const [activeGroup, setActiveGroup] = useState(null);
    const [verdicts, setVerdicts] = useState({});

    const setVerdict = (key, verdict) => {
        setVerdicts((current) => ({ ...current, [key]: verdict }));
    };

    const topics = activeGroup ? GROUPS[activeGroup] : [];
    const items = topics.map((key) => {
        const data = guidelineExamples[key];
        const verdict = verdicts[key];
        return {
            question: (
                <span className="guideline-heading">
                    <span className="guideline-heading__title">{data.title}</span>
                    {verdict && (
                        <span className={`verdict-chip verdict-chip--${verdict.passed ? 'pass' : 'fail'}`}>
                            {verdict.passed ? 'зачтено' : 'не зачтено'}
                        </span>
                    )}
                </span>
            ),
            answer: <GuidelineTopic data={data} onVerdict={(verdict) => setVerdict(key, verdict)} />,
        };
    });

    return (
        <WorkPage
            title="Мастерская BPMN"
            eyebrow="обучение"
            description="Разбирайте элементы схемы на коротких примерах: что сработает, что запутает команду и как это исправить."
            className="guideline">
            <div className="guideline-content">
                <motion.div
                    className="guideline-sidebar"
                    initial={{ opacity: 0, x: -20 }}
                    animate={{ opacity: 1, x: 0 }}
                    transition={{ delay: 0.1, duration: 0.3 }}>
                    {Object.keys(GROUPS).map((group) => (
                        <button
                            type="button"
                            key={group}
                            className={`sidebar-item ${activeGroup === group ? 'active' : ''}`}
                            aria-pressed={activeGroup === group}
                            onClick={() => setActiveGroup(activeGroup === group ? null : group)}>
                            {group}
                        </button>
                    ))}
                </motion.div>

                <motion.div
                    className="guideline-main"
                    initial={{ opacity: 0 }}
                    animate={{ opacity: 1 }}
                    transition={{ delay: 0.2, duration: 0.3 }}>
                    {activeGroup ? (
                        <Accordion items={items} />
                    ) : (
                        <EmptyState
                            title="Выберите тему слева"
                            description="Тема — это короткое правило, два примера схемы «как нельзя» и «как надо», и проверка на несколько утверждений по этому элементу."
                        />
                    )}
                </motion.div>
            </div>
        </WorkPage>
    );
};

export default Guideline;

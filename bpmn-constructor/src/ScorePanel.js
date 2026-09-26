import React, { useRef, useEffect, memo } from 'react';
import { motion } from 'framer-motion';
import { FontAwesomeIcon } from '@fortawesome/react-fontawesome';
import { faTimes, faExpand, faCompress } from '@fortawesome/free-solid-svg-icons';
import { stripBpmnIds } from './i18n/labels';
import { Button } from './components/ui';
import './ScorePanel.css';
import { RULE_MESSAGES } from './scoreRules';

/* Сколько названий элементов выводить в одной строке отчёта. */
const MAX_ELEMENTS_IN_REPORT = 6;

/* Хвост «(элементы: Task_1a2b, Flow_1a2b3c и ещё 3)»: его добавляет к тексту
   рекомендации core/bpmn_scoring.py. В такой строке id не читаются. */
const ELEMENTS_TAIL = /\s*\(элементы:([^)]+)\)\s*$/i;
/* Хвост id бывает и из двух символов (`Task_3a`) — {2,}, иначе id протечёт в UI. */
const BPMN_ID_TOKEN = /[A-Z][a-zA-Z ]*_[0-9a-zA-Z]{2,}/g;

/** Страховка: stripBpmnIds не знает про короткие хвосты, дочищаем здесь. */
const removeInternalIds = (text) => text.replace(BPMN_ID_TOKEN, ' ')
    .replace(/\s{2,}/g, ' ')
    .replace(/\s+([,.;:])/g, '$1')
    .trim();

const plural = (count, one, few, many) => {
    const tens = count % 100;
    const unit = count % 10;
    if (unit === 1 && tens !== 11) return one;
    if (unit >= 2 && unit <= 4 && (tens < 10 || tens >= 20)) return few;
    return many;
};

const elementsWord = (count) => `${count} ${plural(count, 'элемент', 'элемента', 'элементов')}`;
const pointsWord = (count) => `${count} ${plural(count, 'балл', 'балла', 'баллов')}`;

/**
 * Строка про затронутые элементы: вместо служебных id — названия с холста.
 * Если элемент не подписан и названия нет, список id не печатаем: он ничего
 * не объясняет, честнее назвать количество.
 */
const describeElements = (ids, totalCount, elementNames) => {
    const list = ids || [];
    const total = totalCount || list.length;
    if (!total) return null;
    const named = [];
    for (const id of list) {
        const name = elementNames[id];
        if (name && !named.includes(name)) named.push(name);
        if (named.length >= MAX_ELEMENTS_IN_REPORT) break;
    }
    if (!named.length) return `Затронуто ${elementsWord(total)}.`;
    const rest = total - named.length;
    return `Элементы: ${named.join(', ')}${rest > 0 ? ` — и ещё ${rest}` : ''}.`;
};

/** Текст рекомендации: id уносим из строки в отдельную подпись под ней. */
const splitRecommendation = (text) => {
    const raw = String(text ?? '');
    const tail = raw.match(ELEMENTS_TAIL);
    const ids = tail ? (tail[1].match(BPMN_ID_TOKEN) || []) : [];
    const extra = tail ? /и ещё (\d+)/.exec(tail[1]) : null;
    return {
        text: removeInternalIds(stripBpmnIds(tail ? raw.replace(ELEMENTS_TAIL, '') : raw)),
        ids,
        total: ids.length + (extra ? Number(extra[1]) : 0),
    };
};

/* Кнопка наведения: подсвечивает элементы правила на холсте, а по клику
   ещё и наводит на них камеру. Доступна с клавиатуры — подсветка не должна
   жить только под курсором. */
const RevealButton = ({ ids, onRevealElements, onFocusElements }) => {
    if (!ids || ids.length === 0) return null;
    return (
        <button
            type="button"
            className="score-row__reveal"
            onMouseEnter={() => onRevealElements(ids)}
            onMouseLeave={() => onRevealElements([])}
            onFocus={() => onRevealElements(ids)}
            onBlur={() => onRevealElements([])}
            onClick={() => onFocusElements(ids)}
        >
            Показать на схеме
            <span className="score-row__reveal-count">{elementsWord(ids.length)}</span>
        </button>
    );
};

const ScorePanel = memo(({
    score = 0,
    recommendations = [],
    errors = {},
    detailsMeta = {},
    elementNames = {},
    onClose,
    isExpanded,
    onToggleExpand,
    busy = false,
    error = null,
    empty = false,
    onRetry,
    onRevealElements = () => {},
    onFocusElements = () => {}
}) => {
    const scrollRef = useRef(null);

    // Проп position из Editor здесь намеренно не применяется: панель стоит
    // в flex-потоке редактора, её место и наложение задаёт ScorePanel.css.
    // Высота — та же, что у чатов: растягивание по строке, а не vh.

    const value = Math.max(0, Math.min(100, Number(score) || 0));
    // Тон — только статус балла; цвета статусов живут в ScorePanel.css токенами.
    const scoreTone = value >= 80 ? 'good' : value >= 50 ? 'warn' : 'bad';

    const getScoreLabel = (scoreValue) => {
        if (scoreValue >= 80) return 'Схема качественная';
        if (scoreValue >= 50) return 'Есть замечания';
        return 'Много нарушений';
    };

    // Прокрутку отчёта возвращаем наверх только когда прилетели результаты
    // новой проверки (меняется балл). На старых зависимостях от [recommendations,
    // errors] эффект срабатывал на каждый рендер редактора, и отчёт прыгал
    // вверх при каждом перетаскивании элемента по холсту.
    useEffect(() => {
        if (scrollRef.current) {
            scrollRef.current.scrollTop = 0;
        }
    }, [value]);

    // Порядок разбора: сначала то, что стоит дороже всего, — аналитик
    // читает список сверху вниз и должен встретить главную потерю балла
    // первой строкой, а не искать её по колонке «−8».
    const errorEntries = Object.entries(errors || {})
        .filter(([, isCorrect]) => !isCorrect)
        .map(([key]) => RULE_MESSAGES[key] && ({
            key,
            title: RULE_MESSAGES[key].title,
            fix: RULE_MESSAGES[key].fix,
            weight: detailsMeta[key]?.weight,
            elements: detailsMeta[key]?.elements || [],
        }))
        .filter(Boolean)
        .sort((a, b) => (b.weight || 0) - (a.weight || 0) || a.title.localeCompare(b.title, 'ru'))
        .map((entry) => ({
            ...entry,
            line: describeElements(entry.elements, entry.elements.length, elementNames),
        }));
    const parsedRecommendations = (recommendations || []).map((rec) => {
        const { text, ids, total } = splitRecommendation(rec);
        return { text, ids, line: describeElements(ids, total, elementNames) };
    });
    // Сколько правил всего проверялось: без знаменателя строка
    // «3 ошибки» неотличима от «3 из 17».
    const rulesChecked = Object.keys(errors || {}).length;
    // После успешной проверки в `errors` всегда есть набор правил — по нему и
    // понимаем, что отчёт показывать, а не по «оценка ноль».
    const hasContent = rulesChecked > 0
        || (recommendations?.length || 0) > 0
        || errorEntries.length > 0;
    const showReport = !busy && !empty && hasContent;
    const showHint = !busy && !error && !empty && !hasContent;

    return (
        <motion.div
            className={`ai-chat-container score ${isExpanded ? 'expanded' : ''}`}
            initial={{ opacity: 0, y: 12 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: 12 }}
            transition={{ type: 'spring', damping: 25, stiffness: 300 }}
        >
            <div className="ai-chat-header">
                <div className="ai-chat-header__title">
                    <h3>Оценка схемы</h3>
                </div>
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

            {/* Упавшая проверка не должна выглядеть как «оценки пока нет,
                нажмите „Проверить“»: у ошибки есть свой экран со следующим
                шагом. */}
            {!busy && error && (
                <div className="score-error" role="alert">
                    <p className="score-hint__title">Оценку не удалось получить</p>
                    <p className="score-hint__text">{error}</p>
                    <p className="score-hint__text">
                        {hasContent
                            ? 'Показан результат предыдущей проверки — последние изменения схемы в нём не учтены.'
                            : 'Схема на холсте не изменилась: проверка её не правит.'}
                    </p>
                    {onRetry && (
                        <Button variant="secondary" size="sm" onClick={onRetry}>
                            Повторить проверку
                        </Button>
                    )}
                </div>
            )}

            {/* Пустая схема — отдельное состояние, а не ошибка и не «оценки
                нет»: запрос в бэкенд уходить не за чем. */}
            {!busy && empty && (
                <div className="score-hint">
                    <p className="score-hint__title">Проверять пока нечего</p>
                    <p className="score-hint__text">
                        На холсте стартовый шаблон: один шаг без завершения.
                        Добавьте шаги, конец процесса и участников — кнопка
                        «Проверить» станет доступной, а вместе с ней появятся
                        балл и разбор по правилам.
                    </p>
                </div>
            )}

            {showHint && (
                <div className="score-hint">
                    <p className="score-hint__title">Оценки пока нет</p>
                    <p className="score-hint__text">
                        «Проверить» — в правом столбце инструментов. Качество схемы
                        считают правила BPMN: нарушение, вес в баллах и то, как его
                        исправить.
                    </p>
                </div>
            )}

            {showReport && (
                <div className="score-content">
                    <div className={`score-summary is-${scoreTone}`}>
                        <p className="score-figure">
                            <span className="score-figure__value">{value}</span>
                            <span className="score-figure__of">из 100</span>
                        </p>
                        <p className="score-status">{getScoreLabel(value)}</p>
                        {/* Линейка с порогами, а не кольцо: кольцо только повторяло
                            цифру, а здесь видно, до какого порога не хватает. */}
                        <div className="score-scale" aria-hidden="true">
                            <span className="score-scale__fill" style={{ width: `${value}%` }} />
                            <span className="score-scale__tick" style={{ left: '50%' }} />
                            <span className="score-scale__tick" style={{ left: '80%' }} />
                        </div>
                    </div>

                    <div ref={scrollRef} className="score-scrollable-content">
                        {errorEntries.length > 0 && (
                            <section className="score-section">
                                <h4 className="score-section-title">
                                    Найденные ошибки
                                    <span className="score-section-count">
                                        {errorEntries.length} из {rulesChecked}
                                    </span>
                                </h4>
                                <p className="score-section-note">
                                    Балл считается из 100; число справа — сколько баллов стоит правило.
                                </p>
                                <ul className="score-list">
                                    {errorEntries.map((entry) => (
                                        <li key={entry.key} className="score-row is-error">
                                            <div className="score-row__head">
                                                <span className="score-row__title">{entry.title}</span>
                                                {entry.weight ? (
                                                    <span className="score-row__weight">
                                                        −{pointsWord(entry.weight)}
                                                    </span>
                                                ) : null}
                                            </div>
                                            <p className="score-row__fix">Как исправить: {entry.fix}</p>
                                            {entry.line && <p className="score-row__meta">{entry.line}</p>}
                                            <RevealButton
                                                ids={entry.elements}
                                                onRevealElements={onRevealElements}
                                                onFocusElements={onFocusElements}
                                            />
                                        </li>
                                    ))}
                                </ul>
                            </section>
                        )}

                        {parsedRecommendations.length > 0 && (
                            <section className="score-section">
                                <h4 className="score-section-title">
                                    Рекомендации
                                    <span className="score-section-count">{parsedRecommendations.length}</span>
                                </h4>
                                <ul className="score-list">
                                    {parsedRecommendations.map(({ text, ids, line }, index) => (
                                        <li key={index} className="score-row is-recommendation">
                                            <p className="score-row__title">{text}</p>
                                            {line && <p className="score-row__meta">{line}</p>}
                                            <RevealButton
                                                ids={ids}
                                                onRevealElements={onRevealElements}
                                                onFocusElements={onFocusElements}
                                            />
                                        </li>
                                    ))}
                                </ul>
                            </section>
                        )}
                    </div>
                </div>
            )}
        </motion.div>
    );
});

export default ScorePanel;

import { useState } from 'react';
import { motion } from 'framer-motion';
// Напрямую, не через `../ui`: баррель тянет Button → react-router, а папке
// роутер не нужен — тест остаётся без мока маршрутизации.
import ScoreBadge from '../ui/ScoreBadge';
import './registry.css';

const declension = (count) => `${count} ${count === 1 ? 'схема' : count > 1 && count < 5 ? 'схемы' : 'схем'}`;

const MAX_PAPERS = 3;
// Расстановка шире бумаги: при меньшем разбросе средняя страница перекрывает
// подписи двух боковых, и названия читаются обрывками.
const FAN = [
    { x: -78, y: -42, rotate: -7 },
    { x: 78, y: -42, rotate: 7 },
    { x: 0, y: -62, rotate: 2 },
];
const STACKED = { x: 0, y: 12, rotate: 0 };
const FLAP_SPRING = { type: 'spring', stiffness: 300, damping: 26 };
const PAPER_SPRING = { type: 'spring', stiffness: 260, damping: 22 };

const InteractiveFolder = ({ name, diagrams = [], selected = false, onSelect, onOpenDiagram, children }) => {
    const [open, setOpen] = useState(false);
    const papers = diagrams.slice(0, MAX_PAPERS);

    const toggle = () => {
        setOpen((value) => !value);
        onSelect?.();
    };

    // Дрейф пишется в CSS-переменные напрямую: mousemove не должен ре-рендерить реестр.
    const drift = (event) => {
        if (!open || !window.matchMedia('(pointer: fine)').matches) return;
        const rect = event.currentTarget.getBoundingClientRect();
        event.currentTarget.style.setProperty('--drift-x', `${(event.clientX - rect.left - rect.width / 2) * 0.18}px`);
        event.currentTarget.style.setProperty('--drift-y', `${(event.clientY - rect.top - rect.height / 2) * 0.18}px`);
    };

    const settle = (event) => {
        event.currentTarget.style.removeProperty('--drift-x');
        event.currentTarget.style.removeProperty('--drift-y');
    };

    return (
        <article className={`registry-folder${open ? ' is-open' : ''}${selected ? ' is-selected' : ''}`}>
            <div className="registry-folder__stage">
                <button
                    type="button"
                    className="registry-folder__button"
                    onClick={toggle}
                    aria-expanded={open}
                    aria-label={`Папка «${name}» — ${declension(diagrams.length)}`}
                >
                    <span className="registry-folder__tab" aria-hidden="true" />
                    <span className="registry-folder__back" aria-hidden="true" />
                    <motion.span className="registry-folder__flap registry-folder__flap--left" aria-hidden="true"
                        style={{ transformOrigin: 'bottom left' }} transition={FLAP_SPRING}
                        animate={open ? { skewX: 5, scaleY: 0.82, y: 2 } : { skewX: 0, scaleY: 1, y: 0 }} />
                    <motion.span className="registry-folder__flap registry-folder__flap--right" aria-hidden="true"
                        style={{ transformOrigin: 'bottom right' }} transition={FLAP_SPRING}
                        animate={open ? { skewX: -5, scaleY: 0.82, y: 2 } : { skewX: 0, scaleY: 1, y: 0 }} />
                    <span className="registry-folder__label">
                        <span className="registry-folder__name">{name}</span>
                        <small>{declension(diagrams.length)}</small>
                    </span>
                </button>

                <div className="registry-folder__papers" aria-hidden={!open}>
                    {papers.map((diagram, index) => (
                        <motion.button
                            type="button"
                            key={diagram.id}
                            className="registry-folder__paper"
                            style={{ zIndex: index + 1 }}
                            animate={open ? FAN[index] : STACKED}
                            transition={PAPER_SPRING}
                            tabIndex={open ? 0 : -1}
                            onMouseMove={drift}
                            onMouseLeave={settle}
                            onClick={() => onOpenDiagram?.(diagram.id)}
                            aria-label={`Открыть в редакторе: ${diagram.name || 'схема без названия'}`}
                        >
                            <span className="registry-folder__paper-name" title={diagram.name || 'Без названия'}>{diagram.name || 'Без названия'}</span>
                            <ScoreBadge score={diagram.score} />
                        </motion.button>
                    ))}
                    {open && !papers.length && <span className="registry-folder__void">В папке пока нет схем</span>}
                </div>
            </div>

            {open && (
                <div className="registry-folder__contents">
                    <p className="registry-folder__heading">{name}<small>{declension(diagrams.length)}</small></p>
                    {children}
                </div>
            )}
        </article>
    );
};

export default InteractiveFolder;

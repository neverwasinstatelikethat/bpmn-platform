import { useEffect, useId, useState } from 'react';
import './ui.css';

const COLLAPSE_MS = 500;

/**
 * FAQ-аккордеон с плавным раскрытием и поворотом иконки «плюс».
 * items: [{ question, answer }]
 * Содержимое размонтируется после схлопывания: тяжёлые панели (например,
 * bpmn-вьюеры) не должны жить в DOM по шесть штук за закрытыми заголовками.
 */
const Accordion = ({ items, defaultOpen = null }) => {
    const [openIndex, setOpenIndex] = useState(defaultOpen);
    const [mountedIndex, setMountedIndex] = useState(defaultOpen);
    const baseId = useId();

    useEffect(() => {
        if (openIndex !== null) {
            setMountedIndex(openIndex);
            return undefined;
        }
        const timer = setTimeout(() => setMountedIndex(null), COLLAPSE_MS);
        return () => clearTimeout(timer);
    }, [openIndex]);

    return (
        <div className="ui-accordion">
            {items.map((item, index) => {
                const isOpen = openIndex === index;
                const triggerId = `${baseId}-trigger-${index}`;
                const panelId = `${baseId}-panel-${index}`;
                return (
                    <div key={item.id ?? index} className="ui-accordion__item" data-open={isOpen}>
                        <button
                            type="button"
                            className="ui-accordion__trigger"
                            aria-expanded={isOpen}
                            aria-controls={panelId}
                            id={triggerId}
                            onClick={() => setOpenIndex(isOpen ? null : index)}
                        >
                            {item.question}
                            <span className="ui-accordion__icon" aria-hidden="true">+</span>
                        </button>
                        <div className="ui-accordion__body" id={panelId} role="region" aria-labelledby={triggerId}>
                            <div className="ui-accordion__inner">
                                {mountedIndex === index && <div className="ui-accordion__content">{item.answer}</div>}
                            </div>
                        </div>
                    </div>
                );
            })}
        </div>
    );
};

export default Accordion;

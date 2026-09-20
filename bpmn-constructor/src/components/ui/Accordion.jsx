import { useState } from 'react';
import './ui.css';

/**
 * FAQ-аккордеон с плавным раскрытием и поворотом иконки «плюс».
 * items: [{ question, answer }]
 */
const Accordion = ({ items, defaultOpen = null }) => {
    const [openIndex, setOpenIndex] = useState(defaultOpen);

    return (
        <div className="ui-accordion">
            {items.map((item, index) => {
                const isOpen = openIndex === index;
                return (
                    <div key={index} className="ui-accordion__item" data-open={isOpen}>
                        <button
                            type="button"
                            className="ui-accordion__trigger"
                            aria-expanded={isOpen}
                            aria-controls={`faq-panel-${index}`}
                            id={`faq-trigger-${index}`}
                            onClick={() => setOpenIndex(isOpen ? null : index)}
                        >
                            {item.question}
                            <span className="ui-accordion__icon" aria-hidden="true">+</span>
                        </button>
                        <div
                            className="ui-accordion__body"
                            id={`faq-panel-${index}`}
                            role="region"
                            aria-labelledby={`faq-trigger-${index}`}
                        >
                            <div className="ui-accordion__inner">
                                <div className="ui-accordion__content">{item.answer}</div>
                            </div>
                        </div>
                    </div>
                );
            })}
        </div>
    );
};

export default Accordion;

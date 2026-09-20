import './ui.css';

/**
 * Заголовок секции: рукописный «бровик», крупный титул, подзаголовок.
 * `align`: 'center' | 'left'.
 */
const SectionHeading = ({ eyebrow, title, subtitle, align = 'center' }) => (
    <div className={`ui-section-heading ${align === 'left' ? 'ui-section-heading--left' : ''}`}>
        {eyebrow && <span className="ui-section-heading__eyebrow">{eyebrow}</span>}
        <h2 className="ui-section-heading__title">{title}</h2>
        {subtitle && <p className="ui-section-heading__subtitle">{subtitle}</p>}
    </div>
);

export default SectionHeading;

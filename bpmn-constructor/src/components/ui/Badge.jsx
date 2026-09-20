import './ui.css';

/**
 * Бейдж-метка. Тоны: sage | lavender | peach | green.
 * `dot` добавляет фирменную зелёную точку.
 */
const Badge = ({ tone = 'sage', dot = false, className = '', children }) => (
    <span className={`ui-badge ui-badge--${tone} ${className}`}>
        {dot && <span className="ui-badge__dot" aria-hidden="true" />}
        {children}
    </span>
);

export default Badge;

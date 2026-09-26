import './ui.css';

/**
 * Бейдж-метка. Тоны: sage | lavender | peach | green | neutral | warning | danger.
 * `size="sm"` — плотная метка для таблиц и бумажек; по умолчанию — крупная.
 * `dot` добавляет фирменную зелёную точку.
 */
const Badge = ({ tone = 'sage', size = 'md', dot = false, className = '', children }) => (
    <span className={`ui-badge ui-badge--${tone}${size === 'sm' ? ' ui-badge--sm' : ''} ${className}`}>
        {dot && <span className="ui-badge__dot" aria-hidden="true" />}
        {children}
    </span>
);

export default Badge;

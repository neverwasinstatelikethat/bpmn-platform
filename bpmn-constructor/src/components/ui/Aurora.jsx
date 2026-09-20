import './ui.css';

// Мягкое «северное сияние» в фирменных пастельных тонах.
// Анимации останавливаются глобальным prefers-reduced-motion.
const Aurora = ({ className = '' }) => (
    <div className={`ui-aurora ${className}`.trim()} aria-hidden="true">
        <div className="ui-aurora__beam ui-aurora__beam--green" />
        <div className="ui-aurora__beam ui-aurora__beam--lavender" />
        <div className="ui-aurora__beam ui-aurora__beam--peach" />
    </div>
);

export default Aurora;

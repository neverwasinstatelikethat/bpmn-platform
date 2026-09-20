import './ui.css';

/**
 * Зерно поверх всего интерфейса: бумажная фактура,
 * 35% непрозрачности, режим наложения overlay. Не ловит клики.
 */
const GrainOverlay = () => <div className="ui-grain" aria-hidden="true" />;

export default GrainOverlay;
